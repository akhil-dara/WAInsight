"""
Receipt ingestion from msgstore.db.

Processes the ``receipt_user`` table, which records per-
recipient delivery / read / played timestamps for outgoing
messages.  This data is critical for forensic timeline
reconstruction and proving message delivery.

Covers personal chats, group chats, and played-receipts for
voice notes and video messages.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)

BATCH_SIZE = 50_000


def ingest_receipts(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest receipt records from msgstore.db into analysis.db.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of receipt records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("receipt_user"):
        logger.warning("receipt_user table not found")
        return 0

    total = reader.get_row_count("receipt_user")
    logger.info("Starting receipt ingestion: %d total records", total)

    # Build lookups
    msg_map = _build_msg_map(analysis_conn)

    # Pre-load jid_row_id -> contact_id for fast lookups (avoids N+1)
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    # Fallback: JID string → contact for unresolved JIDs
    _jid_strings: dict[int, str] = {}
    try:
        for r in reader.execute_raw(
            "SELECT _id, raw_string FROM jid WHERE raw_string IS NOT NULL"
        ):
            _jid_strings[r[0]] = r[1]
    except Exception:
        pass
    _phone_to_contact: dict[str, int] = {}
    for r in analysis_conn.fetchall(
        "SELECT phone_jid, id FROM contact WHERE phone_jid IS NOT NULL"
    ):
        _phone_to_contact[r[0]] = r[1]
    # Also map LID JIDs
    for r in analysis_conn.fetchall(
        "SELECT lid_jid, id FROM contact WHERE lid_jid IS NOT NULL"
    ):
        _phone_to_contact[r[0]] = r[1]

    # Check available columns
    ru_cols = reader.get_column_names("receipt_user")

    insert_sql = """
        INSERT OR IGNORE INTO receipt (
            message_id, recipient_id,
            delivered_ts, read_ts, played_ts,
            delivery_delay_ms, read_delay_ms
        ) VALUES (?,?,?,?,?,?,?)
    """

    # Get message timestamps for delay computation
    msg_ts_map: dict[int, int] = {}
    ts_rows = analysis_conn.fetchall("SELECT id, timestamp FROM message WHERE from_me = 1")
    msg_ts_map = {row[0]: row[1] for row in ts_rows}

    processed = 0

    for batch in reader.iter_batched(
        table="receipt_user",
        batch_size=BATCH_SIZE,
        order_by="message_row_id",
    ):
        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for row in batch:
                # receipt_user columns: message_row_id, receipt_user_jid_row_id,
                # receipt_timestamp, read_timestamp, played_timestamp
                row_dict = dict(zip(ru_cols, row))

                source_msg_id = row_dict.get("message_row_id")
                recipient_jid_row = row_dict.get("receipt_user_jid_row_id")

                msg_id = msg_map.get(source_msg_id)
                if msg_id is None:
                    continue

                # Resolve recipient contact via pre-loaded mapping
                recipient_id = jid_to_contact.get(recipient_jid_row)
                # Fallback: resolve via JID string → phone_jid/lid_jid
                if recipient_id is None and recipient_jid_row:
                    jid_str = _jid_strings.get(recipient_jid_row)
                    if jid_str:
                        recipient_id = _phone_to_contact.get(jid_str)
                if recipient_id is None:
                    continue

                delivered_ts = row_dict.get("receipt_timestamp")
                read_ts = row_dict.get("read_timestamp")
                played_ts = row_dict.get("played_timestamp")

                # Compute delays
                msg_ts = msg_ts_map.get(msg_id, 0)
                delivery_delay = None
                read_delay = None

                if delivered_ts and msg_ts and delivered_ts > 0 and msg_ts > 0:
                    delivery_delay = delivered_ts - msg_ts

                if read_ts and delivered_ts and read_ts > 0 and delivered_ts > 0:
                    read_delay = read_ts - delivered_ts

                cursor.execute(insert_sql, (
                    msg_id, recipient_id,
                    delivered_ts, read_ts, played_ts,
                    delivery_delay, read_delay,
                ))
                processed += 1

            analysis_conn.commit()

        except Exception:
            analysis_conn.rollback()
            raise

        if progress_callback:
            progress_callback(processed, total)

    logger.info("Receipt ingestion complete: %d records", processed)

    # Ingest device-level receipt records (which companion device saw the message)
    device_count = _ingest_receipt_devices(reader, analysis_conn, msg_map, jid_to_contact)

    return processed


def _ingest_receipt_devices(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    msg_map: dict[int, int],
    jid_to_contact: dict[int, int],
) -> int:
    """Ingest receipt_device records -- per-device read/delivery timestamps.

    In multi-device WhatsApp, each companion device (Web, Desktop, iPad, etc.)
    sends its own receipt.  This data reveals which specific device a recipient
    used to read a message and when.

    Args:
        reader: Source database reader.
        analysis_conn: Write connection to analysis.db.
        msg_map: source_msg_id → analysis message.id mapping.
        jid_to_contact: jid_row_id → contact_id mapping.

    Returns:
        Number of receipt_device_record entries created.
    """
    if not reader.table_exists("receipt_device"):
        logger.info("receipt_device table not found, skipping device receipts")
        return 0

    total = reader.get_row_count("receipt_device")
    logger.info("Starting receipt_device ingestion: %d total records", total)

    # Pre-build JID row → device number mapping from source jid table
    jid_device_rows = reader.execute_raw(
        "SELECT _id, user, device FROM jid WHERE device IS NOT NULL"
    )
    jid_device_map: dict[int, int] = {r[0]: r[2] for r in jid_device_rows}
    # Map device JID row → owner user JID row (strip device part)
    jid_user_map: dict[int, str] = {r[0]: r[1] for r in jid_device_rows}

    insert_sql = """
        INSERT OR IGNORE INTO receipt_device_record (
            message_id, device_jid_row_id, device_contact_id,
            receipt_ts, primary_device_version
        ) VALUES (?,?,?,?,?)
    """

    processed = 0
    min_max = reader.execute_raw(
        "SELECT MIN(_id), MAX(_id) FROM receipt_device"
    )
    if not min_max or not min_max[0][0]:
        return 0

    min_id, max_id = min_max[0]
    current_id = min_id

    while current_id <= max_id:
        batch_end = current_id + BATCH_SIZE

        rows = reader.execute_raw(
            "SELECT _id, message_row_id, receipt_device_jid_row_id, "
            "receipt_device_timestamp, primary_device_version "
            "FROM receipt_device WHERE _id >= ? AND _id < ? ORDER BY _id",
            (current_id, batch_end),
        )

        if not rows:
            current_id = batch_end
            continue

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for row in rows:
                _, source_msg_id, device_jid_row, receipt_ts, pdv = row
                msg_id = msg_map.get(source_msg_id)
                if msg_id is None:
                    continue

                # Resolve device owner's contact_id via user part of JID
                device_contact_id = jid_to_contact.get(device_jid_row)

                cursor.execute(insert_sql, (
                    msg_id, device_jid_row, device_contact_id,
                    receipt_ts, pdv,
                ))
                processed += 1

            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        current_id = batch_end

    logger.info("Receipt device ingestion complete: %d records", processed)
    return processed


def _build_msg_map(analysis_conn: AnalysisConnection) -> dict[int, int]:
    """Build source message._id → analysis message.id lookup."""
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    return {row[0]: row[1] for row in rows}
