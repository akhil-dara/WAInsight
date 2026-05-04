"""
URL link detail ingestion from msgstore.db.

The ``message_link`` table records which messages contain URLs
(via ``message_row_id`` + ``link_index``) but does NOT store the
actual URL text — URLs must be extracted from the message's
``text_data`` column.

This stage joins ``message_link`` with ``message`` to fetch the
text, extracts URLs by regex, and stores each one with parsed
domain metadata.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional
from urllib.parse import urlparse

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)

BATCH_SIZE = 100_000


def _ensure_thumbnail_column(analysis_conn: AnalysisConnection) -> None:
    """Ensure ``message_link_detail`` has a ``thumbnail_blob`` column.

    Adds the column on older analysis DBs that pre-date it so
    URL cards can render their og:image preview.  Safe no-op
    when the column already exists.
    """
    try:
        cols = [
            r[1]
            for r in analysis_conn.fetchall("PRAGMA table_info(message_link_detail)")
        ]
        if "thumbnail_blob" not in cols:
            analysis_conn.execute("ALTER TABLE message_link_detail ADD COLUMN thumbnail_blob BLOB")
            analysis_conn.commit()
            logger.info("Added thumbnail_blob column to message_link_detail")
    except Exception as e:
        logger.warning("thumbnail_blob migration check failed: %s", e)

# Regex to extract URLs from text
_URL_REGEX = re.compile(
    r'https?://[^\s<>"\'\)]+|www\.[^\s<>"\'\)]+',
    re.IGNORECASE,
)


def ingest_links(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest URL link details from msgstore.db.

    Joins message_link with message to extract actual URLs from text_data.

    Returns:
        Number of link records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    # Ensure the schema has the thumbnail_blob column (adds it if missing).
    _ensure_thumbnail_column(analysis_conn)

    if not reader.table_exists("message_link"):
        logger.warning("message_link table not found")
        return 0

    total = reader.get_row_count("message_link")
    logger.info("Starting link ingestion: %d total records", total)

    # Build message lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    insert_sql = """
        INSERT OR IGNORE INTO message_link_detail (
            message_id, url, page_title, description, domain, link_index
        ) VALUES (?,?,?,?,?,?)
    """

    processed = 0

    # Get ID range for batching
    min_max = reader.execute_raw(
        "SELECT MIN(message_row_id), MAX(message_row_id) FROM message_link"
    )
    if not min_max or not min_max[0][0]:
        return 0

    min_id, max_id = min_max[0]
    current_id = min_id

    while current_id <= max_id:
        batch_end = current_id + BATCH_SIZE

        # Join message_link with message to get the text containing URLs
        link_rows = reader.execute_raw(
            "SELECT ml.message_row_id, ml.link_index, m.text_data "
            "FROM message_link ml "
            "JOIN message m ON m._id = ml.message_row_id "
            "WHERE ml.message_row_id >= ? AND ml.message_row_id < ? "
            "ORDER BY ml.message_row_id, ml.link_index",
            (current_id, batch_end),
        )

        if not link_rows:
            current_id = batch_end
            continue

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for msg_row, link_index, text_data in link_rows:
                msg_id = msg_map.get(msg_row)
                if msg_id is None:
                    continue

                if not text_data:
                    continue

                # Extract URLs from the text
                urls = _URL_REGEX.findall(text_data)
                if not urls:
                    continue

                # Use the link_index to pick the right URL
                idx = link_index or 0
                if idx < len(urls):
                    url = urls[idx]
                else:
                    url = urls[0]  # Fallback to first URL

                # Extract domain
                domain = None
                try:
                    if not url.startswith("http"):
                        url = "https://" + url
                    parsed = urlparse(url)
                    domain = parsed.netloc or parsed.hostname
                    if domain and domain.startswith("www."):
                        domain = domain[4:]
                except Exception:
                    pass

                cursor.execute(insert_sql, (msg_id, url, None, None, domain, idx))
                processed += 1

            analysis_conn.commit()

        except Exception:
            analysis_conn.rollback()
            raise

        if progress_callback:
            progress_callback(processed, total)

        current_id = batch_end

    logger.info("Link ingestion complete: %d records", processed)

    # Second pass: enrich with preview metadata from message_text table
    if reader.table_exists("message_text"):
        logger.info("Enriching link records with preview metadata from message_text...")
        text_rows = reader.execute_raw(
            "SELECT message_row_id, page_title, description "
            "FROM message_text "
            "WHERE page_title IS NOT NULL OR description IS NOT NULL"
        )

        enriched = 0
        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()
            for msg_row, title, desc in text_rows:
                msg_id = msg_map.get(msg_row)
                if msg_id is None:
                    continue
                cursor.execute(
                    "UPDATE message_link_detail SET page_title = ?, description = ? "
                    "WHERE message_id = ? AND page_title IS NULL",
                    (title, desc, msg_id),
                )
                enriched += 1
            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        logger.info("Enriched %d link records with preview metadata", enriched)

    # Third pass: attach og:image thumbnails from msgstore's message_thumbnail.
    #
    # Design notes
    # ------------
    # * ``message_thumbnail`` is keyed by ``message_row_id`` — ONE row per
    #   message, regardless of how many URLs that message contains.  The
    #   BLOB is the og:image of whichever URL WhatsApp was previewing at the
    #   time the message was stored.
    # * ``message_link_detail`` is per-instance (one row per (message,
    #   link_index) — we NEVER dedupe by URL, because the same URL shared on
    #   day 1 and again on day 30 can carry different page_title /
    #   description / og:image.  Each detail row is its own record.
    # * Some messages have a thumbnail but NO corresponding ``message_link``
    #   entry (WhatsApp stored the preview in ``message_text`` +
    #   ``message_thumbnail`` without populating ``message_link``).  Those
    #   messages would otherwise lose their thumbnail entirely, so we insert
    #   a synthetic link_detail row for them before attaching the thumb.
    if reader.table_exists("message_thumbnail"):
        logger.info(
            "Attaching link preview thumbnails from message_thumbnail..."
        )
        thumbs_attached = 0
        synthetic_rows = 0
        skipped_non_text = 0

        # Set of message_ids that already have at least one link_detail row.
        # Used to decide whether we need to insert a synthetic row.
        existing_link_msgs = {
            row[0] for row in analysis_conn.fetchall(
                "SELECT DISTINCT message_id FROM message_link_detail"
            )
        }

        thumb_rows = reader.execute_raw(
            "SELECT mt.message_row_id, mt.thumbnail "
            "FROM message_thumbnail mt "
            "WHERE mt.thumbnail IS NOT NULL AND LENGTH(mt.thumbnail) > 50"
        )

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()
            # APSW cursors have no .rowcount — use Connection.changes() instead.
            # ``raw_connection`` is a @property (no parens).
            raw_conn = analysis_conn.raw_connection

            for msg_row, thumb_bytes in thumb_rows:
                msg_id = msg_map.get(msg_row)
                if msg_id is None:
                    continue

                # If this message has no link_detail row yet, synthesise one
                # from the message text so the thumbnail has a home.  Only do
                # this for text messages (type 0) — thumbnails on non-text
                # messages belong to media, not links.
                if msg_id not in existing_link_msgs:
                    meta = analysis_conn.fetchone(
                        "SELECT message_type, text_content "
                        "FROM message WHERE id = ?",
                        (msg_id,),
                    )
                    if not meta or (meta[0] is not None and meta[0] != 0):
                        # Non-text messages: the thumbnail belongs to the
                        # media record, not a link card.  Skip.
                        skipped_non_text += 1
                        continue

                    text = meta[1] or ""
                    urls = _URL_REGEX.findall(text) if text else []
                    if not urls:
                        # Text message with a thumbnail but no detectable URL
                        # in its text.  We can't build a meaningful link card
                        # for it, so drop the thumbnail.
                        continue

                    url = urls[0]
                    if not url.startswith("http"):
                        url = "https://" + url
                    domain = None
                    try:
                        parsed = urlparse(url)
                        domain = parsed.netloc or parsed.hostname
                        if domain and domain.startswith("www."):
                            domain = domain[4:]
                    except Exception:
                        pass

                    cursor.execute(
                        insert_sql,
                        (msg_id, url, None, None, domain, 0),
                    )
                    existing_link_msgs.add(msg_id)
                    synthetic_rows += 1

                # Attach the thumb to the row WhatsApp was actually previewing
                # (the lowest link_index for this message — typically 0).
                # We never overwrite an existing non-empty thumbnail_blob.
                cursor.execute(
                    "UPDATE message_link_detail "
                    "SET thumbnail_blob = ? "
                    "WHERE message_id = ? "
                    "  AND link_index = ("
                    "        SELECT MIN(link_index) "
                    "        FROM message_link_detail "
                    "        WHERE message_id = ?"
                    "  ) "
                    "  AND (thumbnail_blob IS NULL OR LENGTH(thumbnail_blob) = 0)",
                    (thumb_bytes, msg_id, msg_id),
                )
                if raw_conn.changes() > 0:
                    thumbs_attached += 1
            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise
        logger.info(
            "Attached %d link preview thumbnails "
            "(%d synthetic link_detail rows created, %d non-text skipped)",
            thumbs_attached, synthetic_rows, skipped_non_text,
        )

    return processed
