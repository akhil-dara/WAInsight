"""
Scheduled event ingestion from msgstore.db -- message_event table.

Processes the ``message_event`` table which stores scheduled calls and
calendar events. Each event row links back to a message via message_row_id.

Populates the ``scheduled_event`` table in analysis.db.

Source table schema (from WhatsApp msgstore.db):
    message_event(
        message_row_id INTEGER PRIMARY KEY,
        is_canceled INTEGER DEFAULT 0,
        name TEXT NOT NULL,
        description TEXT,
        location_latitude REAL,
        location_longitude REAL,
        location_name TEXT,
        location_address TEXT,
        join_link TEXT,
        start_time DATETIME NOT NULL,   -- stored as Unix-ms
        chat_row_id INTEGER,
        event_state INTEGER NOT NULL DEFAULT 0,
        end_time DATETIME,
        allow_extra_guests INTEGER,
        is_schedule_call INTEGER,
        has_reminder INTEGER,
        reminder_offset_sec INTEGER,
        show_upcoming_banner INTEGER
    )
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_scheduled_events(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest scheduled events from msgstore.db message_event table.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of scheduled events ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_event"):
        logger.info("message_event table not found — skipping scheduled event ingestion")
        return 0

    total = reader.get_row_count("message_event")
    if total == 0:
        logger.info("No scheduled events found in message_event table")
        return 0

    logger.info("Starting scheduled event ingestion: %d total events", total)

    # Build message lookup: source_msg_id → (analysis_msg_id, conversation_id, timestamp)
    msg_rows = analysis_conn.fetchall(
        "SELECT source_msg_id, id, conversation_id, timestamp FROM message"
    )
    msg_map: dict[int, tuple[int, int, int]] = {
        r[0]: (r[1], r[2], r[3]) for r in msg_rows
    }

    # Build chat_row_id → conversation_id lookup
    chat_rows = analysis_conn.fetchall(
        "SELECT source_chat_id, id FROM conversation"
    )
    chat_to_conv: dict[int, int] = {r[0]: r[1] for r in chat_rows}

    # Detect available columns (schema may vary across WhatsApp versions)
    cols = set(reader.get_column_names("message_event"))

    # Build SELECT dynamically based on available columns
    select_cols = ["message_row_id", "name"]
    optional_cols = [
        "is_canceled", "description",
        "location_latitude", "location_longitude", "location_name", "location_address",
        "join_link", "start_time", "chat_row_id", "event_state", "end_time",
        "allow_extra_guests", "is_schedule_call", "has_reminder",
        "reminder_offset_sec", "show_upcoming_banner",
    ]
    for col in optional_cols:
        if col in cols:
            select_cols.append(col)

    sql = f"SELECT {', '.join(select_cols)} FROM message_event ORDER BY message_row_id"
    event_rows = reader.execute_raw(sql)

    insert_sql = """
        INSERT OR IGNORE INTO scheduled_event (
            source_msg_row_id, message_id, conversation_id, name, description,
            start_time, end_time,
            location_latitude, location_longitude, location_name, location_address,
            join_link, is_schedule_call, is_canceled, event_state, allow_extra_guests,
            has_reminder, reminder_offset_sec, show_upcoming_banner, timestamp
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    processed = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in event_rows:
            # Map row to dict based on available columns
            row_dict: dict = dict(zip(select_cols, row))

            source_msg_row_id = row_dict["message_row_id"]
            name = row_dict.get("name") or ""
            if not name:
                continue  # name is NOT NULL in schema; skip malformed rows

            # Resolve message_id and conversation_id
            msg_entry = msg_map.get(source_msg_row_id)
            msg_id = msg_entry[0] if msg_entry else None
            msg_timestamp = msg_entry[2] if msg_entry else None

            # Try to resolve conversation from message first, fall back to chat_row_id
            conv_id: Optional[int] = None
            if msg_entry:
                conv_id = msg_entry[1]
            elif "chat_row_id" in row_dict and row_dict["chat_row_id"] is not None:
                conv_id = chat_to_conv.get(row_dict["chat_row_id"])

            cursor.execute(insert_sql, (
                source_msg_row_id,
                msg_id,
                conv_id,
                name,
                row_dict.get("description"),
                row_dict.get("start_time"),
                row_dict.get("end_time"),
                row_dict.get("location_latitude"),
                row_dict.get("location_longitude"),
                row_dict.get("location_name"),
                row_dict.get("location_address"),
                row_dict.get("join_link"),
                1 if row_dict.get("is_schedule_call") else 0,
                1 if row_dict.get("is_canceled") else 0,
                row_dict.get("event_state", 0),
                1 if row_dict.get("allow_extra_guests") else 0,
                row_dict.get("has_reminder"),
                row_dict.get("reminder_offset_sec"),
                row_dict.get("show_upcoming_banner"),
                msg_timestamp,
            ))
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if progress_callback:
        progress_callback(processed, total)

    logger.info("Scheduled event ingestion complete: %d events", processed)
    return processed
