"""
Pin message ingestion from msgstore.db -- reads pinned messages.

Reads ``message_add_on`` (type 79) joined with ``message_add_on_pin_in_chat``
to populate ``message_pin`` in analysis.db.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_pins(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest pinned messages from msgstore.db.

    Returns:
        Number of pin records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_add_on"):
        logger.warning("message_add_on table not found")
        return 0
    if not reader.table_exists("message_add_on_pin_in_chat"):
        logger.warning("message_add_on_pin_in_chat table not found")
        return 0

    # Check columns
    pin_cols = reader.get_column_names("message_add_on_pin_in_chat")
    has_expiry = "expiry_duration_in_secs" in pin_cols
    has_state = "pin_in_chat_state" in pin_cols

    # Build message lookup: source_msg_id -> (analysis message.id, conversation_id)
    msg_map: dict[int, tuple[int, int]] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id, conversation_id FROM message")
    for row in rows:
        msg_map[row[0]] = (row[1], row[2])

    # Build jid_row_id -> contact_id lookup for pinner resolution
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {r[0]: r[1] for r in jid_contact_rows if r[0] is not None}

    # Query pins: message_add_on type 79 joined with pin details
    query = f"""
        SELECT
            ma.parent_message_row_id,
            ma.sender_jid_row_id,
            ma.from_me,
            COALESCE(mp.sender_timestamp, ma.timestamp) AS pin_timestamp,
            {"mp.expiry_duration_in_secs" if has_expiry else "NULL"} AS expiry_duration,
            {"mp.pin_in_chat_state" if has_state else "1"} AS pin_state
        FROM message_add_on ma
        INNER JOIN message_add_on_pin_in_chat mp ON mp.message_add_on_row_id = ma._id
        WHERE ma.message_add_on_type = 79
    """

    pin_rows = reader.execute_raw(query)

    insert_sql = """
        INSERT OR IGNORE INTO message_pin
            (message_id, conversation_id, pinner_id, pin_timestamp,
             expiry_duration, pin_state)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for parent_msg_row, sender_jid_row, from_me, pin_ts, expiry, state in pin_rows:
            msg_info = msg_map.get(parent_msg_row)
            if msg_info is None:
                continue

            msg_id, conv_id = msg_info

            # Resolve pinner
            pinner_id = None
            if from_me:
                # Owner pinned - find owner contact
                owner_row = analysis_conn.fetchone(
                    "SELECT value FROM case_metadata WHERE key = 'device_owner_contact_id'"
                )
                if owner_row:
                    try:
                        pinner_id = int(owner_row[0])
                    except (ValueError, TypeError):
                        pass
            elif sender_jid_row:
                pinner_id = jid_to_contact.get(sender_jid_row)

            cursor.execute(insert_sql, (
                msg_id, conv_id, pinner_id, pin_ts,
                expiry, state or 1,
            ))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d pin records", count)

    if progress_callback:
        progress_callback(count, count)

    return count
