"""
Edit history ingestion from msgstore.db.

Processes ``message_edit_info`` table to populate ``edit_history`` with
version tracking for edited messages. Each edit event records the server
and sender timestamps, plus the original (pre-edit) text recovered from
WhatsApp's FTS index (message_ftsv2_content.c0content).

Additionally recovers **intermediate text versions** from quoted replies
(message_quoted.text_data) — when someone quotes a message before the
sender edits it, the quote preserves the text at that point in time.

Also ingests original delivery receipts from ``message_add_on_receipt_device``
which are overwritten in ``receipt_device`` after edits.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_edits(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest edit history records from msgstore.db.

    Returns:
        Number of edit records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_edit_info"):
        logger.warning("message_edit_info table not found")
        return 0

    total = reader.get_row_count("message_edit_info")
    logger.info("Starting edit history ingestion: %d total records", total)

    # Build message lookup: source _id → analysis message.id
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    # Check columns
    cols = reader.get_column_names("message_edit_info")
    has_edit_version = "edit_version" in cols

    # Check if FTS content table exists for original text recovery
    has_fts = reader.table_exists("message_ftsv2_content")

    # Build original text lookup from FTS index
    fts_text: dict[int, str] = {}
    if has_fts:
        try:
            fts_rows = reader.execute_raw(
                "SELECT mei.message_row_id, ftsc.c0content "
                "FROM message_edit_info mei "
                "LEFT JOIN message_ftsv2_content ftsc "
                "    ON ftsc.docid = mei.message_row_id "
                "WHERE ftsc.c0content IS NOT NULL AND ftsc.c0content != ''"
            )
            for fr in fts_rows:
                fts_text[fr[0]] = fr[1]
            logger.info("Recovered original text for %d edited messages from FTS index", len(fts_text))
        except Exception as e:
            logger.warning("Could not read FTS content for edit history: %s", e)

    select_parts = ["message_row_id", "original_key_id", "edited_timestamp", "sender_timestamp"]
    if has_edit_version:
        select_parts.append("edit_version")

    edit_rows = reader.execute_raw(
        f"SELECT {', '.join(select_parts)} FROM message_edit_info ORDER BY message_row_id"
    )

    insert_sql = """
        INSERT OR IGNORE INTO edit_history (
            message_id, original_key_id, edited_timestamp,
            sender_timestamp, version, original_text, recovery_method
        ) VALUES (?,?,?,?,?,?,?)
    """

    processed = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in edit_rows:
            msg_row = row[0]
            orig_key = row[1]
            edited_ts = row[2]
            sender_ts = row[3]
            version = row[4] if has_edit_version and len(row) > 4 else 1

            msg_id = msg_map.get(msg_row)
            if msg_id is None:
                continue

            original_text = fts_text.get(msg_row)
            recovery = "fts_index" if original_text else None

            cursor.execute(insert_sql, (
                msg_id, orig_key, edited_ts, sender_ts, version or 1,
                original_text, recovery,
            ))
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    # ── Stage 2: Recover intermediate versions from quoted replies ──
    quote_versions = _recover_quote_versions(reader, analysis_conn, msg_map)

    # ── Stage 3: Ingest original delivery receipts from message_add_on ──
    addon_receipts = _ingest_addon_receipts(reader, analysis_conn, msg_map)

    if progress_callback:
        progress_callback(processed, total)

    logger.info(
        "Edit history ingestion complete: %d records (%d with FTS text, "
        "%d quote-recovered versions, %d add-on receipts)",
        processed, len(fts_text), quote_versions, addon_receipts,
    )
    return processed


def _recover_quote_versions(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    msg_map: dict[int, int],
) -> int:
    """Recover intermediate text versions from quoted replies to edited messages.

    When someone quotes a message before the sender edits it, the
    ``message_quoted.text_data`` preserves the text at the time of quoting.
    This gives us intermediate versions that neither the FTS index nor
    ``message.text_data`` provide.
    """
    if not reader.table_exists("message_quoted"):
        return 0
    if not reader.table_exists("message_edit_info"):
        return 0

    logger.info("Recovering intermediate edit versions from quoted replies...")

    try:
        # Find quotes of edited messages where text differs from current.
        # Split the OR condition into UNION for better index usage.
        quote_rows = reader.execute_raw(
            "SELECT edited_msg_id, quoted_text, quote_timestamp, "
            "       quoting_msg_id, edited_timestamp, current_text "
            "FROM ("
            "  SELECT mei.message_row_id AS edited_msg_id, "
            "    mq.text_data AS quoted_text, "
            "    quoting.timestamp AS quote_timestamp, "
            "    quoting._id AS quoting_msg_id, "
            "    mei.edited_timestamp, "
            "    m.text_data AS current_text "
            "  FROM message_edit_info mei "
            "  JOIN message m ON m._id = mei.message_row_id "
            "  JOIN message_quoted mq ON mq.key_id = m.key_id "
            "  JOIN message quoting ON quoting._id = mq.message_row_id "
            "  WHERE mq.text_data IS NOT NULL AND mq.text_data != '' "
            "  UNION "
            "  SELECT mei.message_row_id AS edited_msg_id, "
            "    mq.text_data AS quoted_text, "
            "    quoting.timestamp AS quote_timestamp, "
            "    quoting._id AS quoting_msg_id, "
            "    mei.edited_timestamp, "
            "    m.text_data AS current_text "
            "  FROM message_edit_info mei "
            "  JOIN message m ON m._id = mei.message_row_id "
            "  JOIN message_quoted mq ON mq.key_id = mei.original_key_id "
            "  JOIN message quoting ON quoting._id = mq.message_row_id "
            "  WHERE mq.text_data IS NOT NULL AND mq.text_data != '' "
            ") ORDER BY edited_msg_id, quote_timestamp"
        )
    except Exception as e:
        logger.warning("Could not query quoted text for edit recovery: %s", e)
        return 0

    insert_sql = """
        INSERT OR IGNORE INTO edit_version (
            message_id, captured_text, captured_timestamp,
            quote_source_msg_id, recovery_method, is_pre_edit
        ) VALUES (?,?,?,?,?,?)
    """

    count = 0
    seen: set[tuple[int, str]] = set()  # (message_id, text) to deduplicate
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in quote_rows:
            edited_msg_src = row[0]
            quoted_text = row[1]
            quote_ts = row[2]
            quoting_msg_src = row[3]
            edit_ts = row[4]
            current_text = row[5]

            msg_id = msg_map.get(edited_msg_src)
            quoting_id = msg_map.get(quoting_msg_src)
            if msg_id is None:
                continue

            # Skip if quoted text is identical to current text (no recovery value)
            if quoted_text == current_text:
                continue

            # Deduplicate: same message + same text
            dedup_key = (msg_id, quoted_text)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            is_pre_edit = 1 if (quote_ts and edit_ts and quote_ts < edit_ts) else 0

            cursor.execute(insert_sql, (
                msg_id, quoted_text, quote_ts,
                quoting_id, "quoted_reply", is_pre_edit,
            ))
            count += 1

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Recovered %d intermediate edit versions from quoted replies", count)
    return count


def _ingest_addon_receipts(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    msg_map: dict[int, int],
) -> int:
    """Ingest original delivery receipts from message_add_on_receipt_device.

    When a message is edited, WhatsApp OVERWRITES ``receipt_device`` timestamps
    with the edit delivery time. The original delivery receipt is preserved in
    ``message_add_on_receipt_device`` (linked via ``message_add_on`` type=74).

    Also fetches the overwritten receipt_device timestamp for comparison.
    """
    if not reader.table_exists("message_add_on"):
        return 0
    if not reader.table_exists("message_add_on_receipt_device"):
        return 0

    logger.info("Ingesting original delivery receipts for edited messages...")

    try:
        # Get original receipts from message_add_on_receipt_device
        # message_add_on type=74 = edit events
        # message_add_on_receipt_device stores the ORIGINAL delivery receipt
        addon_rows = reader.execute_raw(
            "SELECT "
            "  mao.message_row_id, "
            "  mard.receipt_device_timestamp AS original_receipt_ts, "
            "  mao.key_id AS addon_key_id "
            "FROM message_add_on mao "
            "JOIN message_add_on_receipt_device mard ON mard.message_add_on_row_id = mao._id "
            "WHERE mao.message_add_on_type = 74 "
            "ORDER BY mao.message_row_id"
        )
    except Exception as e:
        logger.warning("Could not read message_add_on_receipt_device: %s", e)
        return 0

    # Also get the overwritten receipt_device timestamps for comparison
    edit_receipt_map: dict[int, int] = {}  # message_row_id → receipt_device_timestamp
    try:
        rd_rows = reader.execute_raw(
            "SELECT mei.message_row_id, rd.receipt_device_timestamp "
            "FROM message_edit_info mei "
            "JOIN receipt_device rd ON rd.message_row_id = mei.message_row_id "
            "ORDER BY mei.message_row_id"
        )
        for rr in rd_rows:
            # Keep the earliest receipt_device timestamp per message
            if rr[0] not in edit_receipt_map:
                edit_receipt_map[rr[0]] = rr[1]
    except Exception as e:
        logger.warning("Could not read receipt_device for edits: %s", e)

    insert_sql = """
        INSERT OR IGNORE INTO edit_addon_receipt (
            message_id, original_receipt_ts, edit_receipt_ts,
            addon_key_id
        ) VALUES (?,?,?,?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in addon_rows:
            msg_row_id = row[0]
            orig_ts = row[1]
            addon_key = row[2]

            msg_id = msg_map.get(msg_row_id)
            if msg_id is None:
                continue

            edit_ts = edit_receipt_map.get(msg_row_id)

            cursor.execute(insert_sql, (
                msg_id, orig_ts, edit_ts, addon_key,
            ))
            count += 1

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d original delivery receipts for edited messages", count)
    return count
