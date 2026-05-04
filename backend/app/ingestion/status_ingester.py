"""
Status post ingestion from msgstore.db status@broadcast chat and status.db.

WhatsApp stores status updates ("Stories") as messages in the status@broadcast
chat within msgstore.db.  Each status post is a regular message with a
sender_jid_row_id identifying who posted it.  Additional interaction data
(views, reactions) may be available in the separate status.db.

This ingester:
1. Finds the status@broadcast conversation in analysis.db
2. Extracts all messages from that conversation as status posts
3. Groups them by sender contact
4. Optionally enriches with view/reaction counts from status.db
5. Updates contact.status_count for per-contact summary
"""

from __future__ import annotations

import logging
from typing import Optional, Callable

from app.db.connection import DatabaseManager, AnalysisConnection

logger = logging.getLogger(__name__)

# WhatsApp message_type mapping to human-readable status type labels
_TYPE_LABEL_MAP = {
    0: "text",
    1: "image",
    2: "audio",
    3: "video",
    9: "document",
    11: "gif",
    13: "gif",
    20: "sticker",
    42: "view_once_image",
    43: "view_once_video",
    82: "voice",
    116: "status",
}


def ingest_status_posts(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest status posts into the status_post table.

    Reads messages from the status@broadcast conversation in analysis.db
    (already ingested during the MESSAGES stage) and creates status_post
    records with contact grouping and media metadata.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (current, total) progress callback.

    Returns:
        Number of status posts ingested.
    """
    # Step 1: Find the status@broadcast conversation
    status_conv = analysis_conn.fetchone(
        "SELECT id FROM conversation WHERE chat_type = 'status'"
    )
    if not status_conv:
        # Try by JID pattern
        status_conv = analysis_conn.fetchone(
            "SELECT id FROM conversation WHERE jid_raw_string LIKE '%status%broadcast%'"
        )
    if not status_conv:
        logger.warning("No status@broadcast conversation found -- skipping status ingestion")
        return 0

    status_conv_id = status_conv[0]
    logger.info("Found status conversation: id=%d", status_conv_id)

    # Step 2: Get all messages from the status conversation with media info
    rows = analysis_conn.fetchall("""
        SELECT
            m.id,
            m.sender_id,
            m.conversation_id,
            m.timestamp,
            m.message_type,
            m.text_content,
            m.source_msg_id,
            med.id AS media_id,
            med.mime_type,
            med.file_path,
            med.direct_path,
            med.thumbnail_blob IS NOT NULL AS has_thumb
        FROM message m
        LEFT JOIN media med ON med.message_id = m.id
        WHERE m.conversation_id = ?
          AND m.message_type != 7
        ORDER BY m.timestamp DESC
    """, (status_conv_id,))

    if not rows:
        logger.info("No status messages found in conversation %d", status_conv_id)
        return 0

    total = len(rows)
    logger.info("Found %d status messages to ingest", total)

    # Step 3: Try to load interaction data from status.db
    view_counts: dict[int, int] = {}
    reaction_counts: dict[int, int] = {}
    _load_status_interactions(db_manager, analysis_conn, view_counts, reaction_counts)

    # Step 4: Insert status_post records
    analysis_conn.begin_transaction()
    try:
        inserts = []
        for i, row in enumerate(rows):
            msg_id = row[0]
            contact_id = row[1]
            conv_id = row[2]
            timestamp = row[3]
            msg_type = row[4]
            text_content = row[5]
            source_msg_id = row[6]
            media_id = row[7]
            mime_type = row[8]
            file_path = row[9]
            direct_path = row[10]
            has_thumb = bool(row[11])

            type_label = _TYPE_LABEL_MAP.get(msg_type, "text")
            has_media = media_id is not None
            media_downloadable = bool(direct_path)

            inserts.append((
                msg_id,
                contact_id,
                conv_id,
                timestamp,
                type_label,
                text_content,
                has_media,
                has_thumb,
                mime_type,
                file_path,
                media_downloadable,
                view_counts.get(source_msg_id, 0) if source_msg_id else 0,
                reaction_counts.get(source_msg_id, 0) if source_msg_id else 0,
                source_msg_id,
            ))

            if progress_callback and (i + 1) % 100 == 0:
                progress_callback(i + 1, total)

        analysis_conn.executemany("""
            INSERT OR IGNORE INTO status_post (
                message_id, contact_id, conversation_id, timestamp,
                type_label, text_content, has_media, thumbnail_available,
                media_mime_type, media_file_path, media_downloadable,
                view_count, reaction_count, source_msg_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, inserts)

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    count = len(inserts)
    logger.info("Ingested %d status posts", count)

    # Step 5: Update contact.status_count
    analysis_conn.execute("""
        UPDATE contact SET status_count = COALESCE((
            SELECT COUNT(*) FROM status_post sp
            WHERE sp.contact_id = contact.id
        ), 0)
        WHERE id IN (SELECT DISTINCT contact_id FROM status_post WHERE contact_id IS NOT NULL)
    """)

    contacts_with_status = analysis_conn.fetchone(
        "SELECT COUNT(DISTINCT contact_id) FROM status_post WHERE contact_id IS NOT NULL"
    )
    logger.info(
        "Updated status_count for %d contacts",
        contacts_with_status[0] if contacts_with_status else 0,
    )

    return count


def _load_status_interactions(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    view_counts: dict[int, int],
    reaction_counts: dict[int, int],
) -> None:
    """Try to load view/reaction counts from status.db.

    This is best-effort -- status.db may not exist or may be empty.
    """
    try:
        status_conn = db_manager.get_status_db()
    except (FileNotFoundError, Exception) as e:
        logger.info("status.db not available: %s", e)
        return

    from app.db.source_reader import SourceReader
    reader = SourceReader(status_conn)

    # Check for status_interactions table
    if reader.table_exists("status_interactions"):
        try:
            rows = reader.execute_raw(
                "SELECT status_row_id, view_count, reaction_count "
                "FROM status_interactions"
            )
            for row in rows:
                if row[1]:
                    view_counts[row[0]] = row[1]
                if row[2]:
                    reaction_counts[row[0]] = row[2]
            logger.info(
                "Loaded interaction data: %d view records, %d reaction records",
                len(view_counts), len(reaction_counts),
            )
        except Exception as e:
            logger.warning("Could not read status_interactions: %s", e)

    # Also try status_seen_receipt for view counts
    if reader.table_exists("status_seen_receipt"):
        try:
            rows = reader.execute_raw(
                "SELECT status_row_id, COUNT(*) "
                "FROM status_seen_receipt "
                "GROUP BY status_row_id"
            )
            for row in rows:
                # Merge: use max of interactions table and receipt count
                view_counts[row[0]] = max(view_counts.get(row[0], 0), row[1])
        except Exception as e:
            logger.warning("Could not read status_seen_receipt: %s", e)
