"""
Comment thread ingestion from msgstore.db.

Reads the ``message_comment`` table to populate ``message_comment`` in analysis.db.
These are channel/announcement reply threads (WhatsApp Channels comments).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_comments(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest comment threads from msgstore.db message_comment table.

    Returns:
        Number of comment records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_comment"):
        logger.info("message_comment table not found — skipping")
        return 0

    # Build message lookup: source_msg_id -> (analysis message.id, conversation_id)
    msg_map: dict[int, tuple[int, int]] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id, conversation_id FROM message")
    for row in rows:
        msg_map[row[0]] = (row[1], row[2])

    # Query all comment relationships
    comment_rows = reader.execute_raw(
        "SELECT _id, parent_message_row_id, message_row_id FROM message_comment"
    )

    insert_sql = """
        INSERT OR IGNORE INTO message_comment
            (id, parent_message_id, reply_message_id, conversation_id)
        VALUES (?, ?, ?, ?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for src_id, parent_row, reply_row in comment_rows:
            parent_info = msg_map.get(parent_row)
            reply_info = msg_map.get(reply_row)
            if parent_info is None or reply_info is None:
                continue

            parent_msg_id, conv_id = parent_info
            reply_msg_id, _ = reply_info

            cursor.execute(insert_sql, (
                src_id, parent_msg_id, reply_msg_id, conv_id,
            ))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d comment thread records", count)

    if progress_callback:
        progress_callback(count, count)

    return count
