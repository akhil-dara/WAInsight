"""
Revoke (deleted-for-everyone) message processing.

Updates messages marked as revoked and cross-references quoted
text to recover ghost messages — deleted content preserved
inside quoted replies.

Ghost-message recovery vectors:
    1. ``message_quoted.text_data`` where the quoted message
       was later revoked.
    2. Edit history containing original text before revocation.
    3. WAL-file recovery (handled separately).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def process_revokes(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Process revoked messages and attempt ghost message recovery.

    Returns:
        Number of ghost messages recovered.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    # Count revoked messages in analysis.db
    revoked_count_row = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM message WHERE is_revoked = 1"
    )
    revoked_count = revoked_count_row[0] if revoked_count_row else 0
    logger.info("Processing %d revoked messages for ghost recovery", revoked_count)

    if revoked_count == 0:
        # Check for type-15 deleted messages instead
        deleted_row = analysis_conn.fetchone(
            "SELECT COUNT(*) FROM message WHERE message_type = 15"
        )
        deleted_count = deleted_row[0] if deleted_row else 0
        if deleted_count > 0:
            logger.info("Found %d type-15 deleted message placeholders", deleted_count)
            # Mark these as revoked
            analysis_conn.execute(
                "UPDATE message SET is_revoked = 1 WHERE message_type = 15 AND is_revoked = 0"
            )

    # Ghost message recovery: find quoted text from revoked messages
    # A ghost message is created when:
    # 1. Message A was sent
    # 2. Message B quoted (replied to) Message A, preserving A's text
    # 3. Message A was deleted (revoked) for everyone
    # 4. Message B still contains the quoted text from the now-deleted A

    ghost_sql = """
        INSERT OR IGNORE INTO ghost_message (
            revoked_msg_id, recovered_from_msg_id,
            original_text, original_type,
            revoke_timestamp, original_sender_id,
            conversation_id, recovery_method
        )
        SELECT
            revoked.id,
            quoting.id,
            quoting.quoted_text,
            quoting.quoted_type,
            revoked.revoke_timestamp,
            revoked.sender_id,
            revoked.conversation_id,
            'quoted_text'
        FROM message revoked
        JOIN message quoting ON quoting.reply_to_msg_id = revoked.id
        WHERE (revoked.is_revoked = 1 OR revoked.message_type = 15)
        AND quoting.quoted_text IS NOT NULL
        AND quoting.quoted_text != ''
    """

    analysis_conn.execute(ghost_sql)

    ghost_count_row = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM ghost_message"
    )
    ghost_count = ghost_count_row[0] if ghost_count_row else 0

    # Also try recovery via source_key_id matching (for messages not yet linked)
    if reader.table_exists("message_quoted"):
        additional = _recover_via_key_matching(reader, analysis_conn)
        ghost_count += additional

    if progress_callback:
        progress_callback(ghost_count, ghost_count)

    logger.info(
        "Ghost message recovery complete: %d deleted messages recovered from quoted text",
        ghost_count,
    )
    return ghost_count


def _recover_via_key_matching(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
) -> int:
    """Try to recover ghost messages via key_id matching.

    Some revoked messages may not have reply_to_msg_id resolved yet.
    We can match by source_key_id against message_quoted.key_id in the
    source database to find additional recoveries.
    """
    # Find revoked messages that don't already have ghost records
    # Use BOTH source_key_id AND revoked_key_id since they may differ
    revoked_keys = analysis_conn.fetchall(
        "SELECT m.id, m.source_key_id, m.revoked_key_id, "
        "m.sender_id, m.conversation_id, m.revoke_timestamp "
        "FROM message m "
        "LEFT JOIN ghost_message g ON g.revoked_msg_id = m.id "
        "WHERE (m.is_revoked = 1 OR m.message_type = 15) "
        "AND g.id IS NULL "
    )

    if not revoked_keys:
        return 0

    # Build key_id -> message info lookup using BOTH key types
    revoked_by_key: dict[str, tuple] = {}
    for msg_id, source_key, revoked_key, sender_id, conv_id, revoke_ts in revoked_keys:
        info = (msg_id, sender_id, conv_id, revoke_ts)
        if source_key and source_key != "-1":
            revoked_by_key[source_key] = info
        if revoked_key:
            revoked_by_key[revoked_key] = info

    # Search source DB for quotes that reference these keys
    quoted_rows = reader.execute_raw(
        "SELECT message_row_id, key_id, text_data, message_type "
        "FROM message_quoted "
        "WHERE text_data IS NOT NULL AND text_data != ''"
    )

    # Build source_msg_id → analysis msg.id lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    insert_sql = """
        INSERT OR IGNORE INTO ghost_message (
            revoked_msg_id, recovered_from_msg_id,
            original_text, original_type,
            revoke_timestamp, original_sender_id,
            conversation_id, recovery_method
        ) VALUES (?,?,?,?,?,?,?,?)
    """

    additional = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for quoting_msg_row, quoted_key, quoted_text, quoted_type in quoted_rows:
            if quoted_key not in revoked_by_key:
                continue

            revoked_msg_id, sender_id, conv_id, revoke_ts = revoked_by_key[quoted_key]

            quoting_msg_id = msg_map.get(quoting_msg_row)
            if quoting_msg_id is None:
                continue

            cursor.execute(insert_sql, (
                revoked_msg_id, quoting_msg_id,
                quoted_text, quoted_type,
                revoke_ts, sender_id, conv_id,
                "quoted_text_key_match",
            ))
            additional += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if additional:
        logger.info("Recovered %d additional ghost messages via key_id matching", additional)

    return additional
