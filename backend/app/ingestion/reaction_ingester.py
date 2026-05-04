"""
Reaction ingestion from msgstore.db.

Processes the ``message_add_on`` table filtered by ``type =
56`` (reaction) to populate the analysis ``reaction`` table
with emoji, reactor, timestamp, and conversation context.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_reactions(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest emoji reactions from msgstore.db into analysis.db.

    Reactions are stored in message_add_on with message_add_on_type=56.
    The reaction emoji and metadata are in the linked message row.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of reactions ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_add_on"):
        logger.warning("message_add_on table not found")
        return 0

    # Count reactions (type 56)
    total_row = reader.execute_raw(
        "SELECT COUNT(*) FROM message_add_on WHERE message_add_on_type = 56"
    )
    total = total_row[0][0] if total_row else 0
    logger.info("Starting reaction ingestion: %d total reactions", total)

    if total == 0:
        return 0

    # Build message_id lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    # Build conversation lookup from messages
    msg_conv_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT id, conversation_id FROM message")
    msg_conv_map = {row[0]: row[1] for row in rows}

    # Pre-load jid_row_id -> contact_id for fast reactor resolution
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    # Pre-load JID strings from source for fallback resolution
    jid_strings: dict[int, str] = {}
    try:
        for r in reader.execute_raw(
            "SELECT _id, raw_string FROM jid WHERE raw_string IS NOT NULL"
        ):
            jid_strings[r[0]] = r[1]
    except Exception:
        pass

    # Pre-load phone_jid -> contact_id for fallback
    phone_to_contact: dict[str, int] = {}
    for r in analysis_conn.fetchall(
        "SELECT phone_jid, id FROM contact WHERE phone_jid IS NOT NULL"
    ):
        phone_to_contact[r[0]] = r[1]

    # Query reactions: message_add_on links to the parent message,
    # and the reaction data is in the add-on message itself
    # The add-on message's text_data contains the emoji
    # Check if message_add_on_reaction exists for the actual emoji text
    has_reaction_table = reader.table_exists("message_add_on_reaction")

    if has_reaction_table:
        reaction_query = """
            SELECT
                mao.parent_message_row_id,
                mao._id,
                mar.reaction,
                mao.sender_jid_row_id,
                mao.from_me,
                mao.timestamp
            FROM message_add_on mao
            JOIN message_add_on_reaction mar ON mar.message_add_on_row_id = mao._id
            WHERE mao.message_add_on_type = 56
            AND mar.reaction IS NOT NULL
            AND mar.reaction != ''
        """
    else:
        # Fallback: join with message table to get text_data as the emoji
        # (the add-on message's text_data contains the reaction emoji)
        reaction_query = """
            SELECT
                mao.parent_message_row_id,
                mao._id,
                m.text_data,
                mao.sender_jid_row_id,
                mao.from_me,
                mao.timestamp
            FROM message_add_on mao
            JOIN message m ON m._id = mao._id
            WHERE mao.message_add_on_type = 56
            AND m.text_data IS NOT NULL
            AND m.text_data != ''
        """

    reaction_rows = reader.execute_raw(reaction_query)

    insert_sql = """
        INSERT OR IGNORE INTO reaction (
            message_id, conversation_id, reactor_id, from_me, emoji, timestamp
        ) VALUES (?,?,?,?,?,?)
    """

    processed = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in reaction_rows:
            parent_msg_row, addon_msg_row, emoji, sender_jid_row, from_me, timestamp = row

            # Resolve parent message
            parent_msg_id = msg_map.get(parent_msg_row)
            if parent_msg_id is None:
                continue

            conv_id = msg_conv_map.get(parent_msg_id)
            if conv_id is None:
                continue

            # Resolve reactor contact via pre-loaded mapping
            reactor_id = jid_to_contact.get(sender_jid_row) if sender_jid_row else None
            # Fallback: if jid_to_contact misses this JID, try by phone_jid string
            if reactor_id is None and sender_jid_row:
                jid_str = jid_strings.get(sender_jid_row)
                if jid_str:
                    reactor_id = phone_to_contact.get(jid_str)

            cursor.execute(insert_sql, (
                parent_msg_id, conv_id, reactor_id,
                from_me or 0, emoji, timestamp,
            ))
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if progress_callback:
        progress_callback(processed, total)

    logger.info("Reaction ingestion complete: %d reactions", processed)
    return processed
