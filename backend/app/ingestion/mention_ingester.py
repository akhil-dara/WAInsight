"""
Mention ingestion from msgstore.db.

Processes ``message_mentions`` table to populate ``mention``
table.  Handles three mention types (confirmed via JADX APK
decompile):
    - type 0 / NULL: Regular @mention of a specific person
    - type 1: Group mention
    - type 2: @all / mention everyone

LID mentions are resolved to contact IDs via ``jid_to_contact``.

Only entries in ``message_mentions`` are real @tags — text that
contains a name without being tagged is NOT a mention.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_mentions(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest @mentions from msgstore.db into analysis.db.

    Returns:
        Number of mentions ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_mentions"):
        logger.warning("message_mentions table not found")
        return 0

    total = reader.get_row_count("message_mentions")
    logger.info("Starting mention ingestion: %d total mentions", total)

    # Build message lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    # Check available columns
    cols = reader.get_column_names("message_mentions")
    has_mention_type = "mention_type" in cols
    has_display_name = "display_name" in cols

    mention_rows = reader.execute_raw(
        "SELECT message_row_id, jid_row_id"
        + (", mention_type" if has_mention_type else "")
        + (", display_name" if has_display_name else "")
        + " FROM message_mentions"
    )

    # Pre-load jid_row_id -> contact_id for fast lookups
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    # Pre-load bot JID rows (type 26/27 = @bot) so we can label them "Meta AI"
    # Map jid_row_id -> bot_number (the numeric part before @bot)
    bot_jid_numbers: dict[int, str] = {}
    if reader.table_exists("jid"):
        try:
            bot_rows = reader.execute_raw(
                "SELECT _id, raw_string FROM jid WHERE raw_string LIKE '%@bot' OR type IN (26, 27)"
            )
            for r in bot_rows:
                # Extract number from "867051314767696@bot" or "867051314767696:0@bot"
                raw = r[1] or ""
                num = raw.split("@")[0].split(":")[0]
                bot_jid_numbers[r[0]] = num
        except Exception:
            pass

    insert_sql = """
        INSERT INTO mention (message_id, mentioned_id, display_name, mention_type)
        VALUES (?,?,?,?)
    """

    processed = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in mention_rows:
            msg_row = row[0]
            jid_row = row[1]

            # Dynamic index tracking (handles missing optional columns)
            idx = 2
            mention_type = row[idx] if has_mention_type and len(row) > idx else 0
            if has_mention_type:
                idx += 1
            display_name = row[idx] if has_display_name and len(row) > idx else None

            msg_id = msg_map.get(msg_row)
            if msg_id is None:
                continue

            # Resolve mentioned contact via pre-loaded mapping
            mentioned_id = jid_to_contact.get(jid_row) if jid_row else None

            # If JID is a bot (Meta AI), set display_name and record bot number
            if not mentioned_id and jid_row and jid_row in bot_jid_numbers:
                bot_num = bot_jid_numbers[jid_row]
                if not display_name:
                    display_name = "Meta AI"
                # Store bot number in display_name so renderer can match @botnum in text
                # Format: "Meta AI|867051314767696" — renderer splits on |
                if bot_num and "|" not in (display_name or ""):
                    display_name = f"{display_name}|{bot_num}"

            cursor.execute(insert_sql, (msg_id, mentioned_id, display_name, mention_type))
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    # Post-process: fill blank display_name from contact table
    try:
        analysis_conn.begin_transaction()
        analysis_conn.get_cursor().execute("""
            UPDATE mention SET display_name = (
                SELECT COALESCE(
                    NULLIF(c.resolved_name, ''),
                    NULLIF(c.display_name, ''),
                    NULLIF(c.wa_name, ''),
                    CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                         THEN '+' || c.phone_number ELSE NULL END
                )
                FROM contact c WHERE c.id = mention.mentioned_id
            )
            WHERE (mention.display_name IS NULL OR mention.display_name = '')
                  AND mention.mentioned_id IS NOT NULL
        """)
        analysis_conn.commit()
        logger.info("Resolved blank mention display_names from contact table")
    except Exception as e:
        analysis_conn.rollback()
        logger.warning("Failed to resolve mention display_names: %s", e)

    if progress_callback:
        progress_callback(processed, total)

    logger.info("Mention ingestion complete: %d mentions", processed)
    return processed
