"""
vCard data ingestion from msgstore.db -- parses shared contact cards.

Reads ``message_vcard`` table, extracts FN (display name) and TEL (phone numbers)
from raw vCard text, and populates ``message_vcard_data`` in analysis.db.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)

# Regex to extract phone number from TEL lines (handles various vCard TEL formats)
_TEL_RE = re.compile(r"^TEL[^:]*:(.+)$", re.IGNORECASE)
_FN_RE = re.compile(r"^FN:(.+)$", re.IGNORECASE)


def _parse_vcard(vcard_text: str) -> tuple[str, str]:
    """Parse a single vCard and extract display name and phone numbers.

    Returns:
        (display_name, comma_separated_phones)
    """
    display_name = ""
    phones = []

    for line in vcard_text.split("\n"):
        line = line.strip()
        if not line:
            continue

        fn_match = _FN_RE.match(line)
        if fn_match:
            display_name = fn_match.group(1).strip()
            continue

        tel_match = _TEL_RE.match(line)
        if tel_match:
            phone = tel_match.group(1).strip()
            if phone:
                phones.append(phone)

    return display_name, ", ".join(phones)


def ingest_vcards(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest vCard data from msgstore.db message_vcard table.

    Returns:
        Number of vCard entries ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_vcard"):
        logger.warning("message_vcard table not found")
        return 0

    # Build message lookup: source_msg_id -> analysis message.id
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    # Read all vcards
    vcard_rows = reader.execute_raw(
        "SELECT message_row_id, vcard FROM message_vcard ORDER BY message_row_id, _id"
    )

    insert_sql = """
        INSERT OR IGNORE INTO message_vcard_data
            (message_id, display_name, phone_numbers, vcard_index)
        VALUES (?, ?, ?, ?)
    """

    count = 0
    index_tracker: dict[int, int] = {}  # message_id -> next vcard_index

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for msg_row_id, vcard_text in vcard_rows:
            msg_id = msg_map.get(msg_row_id)
            if msg_id is None or not vcard_text:
                continue

            display_name, phones = _parse_vcard(vcard_text)
            if not display_name and not phones:
                continue

            idx = index_tracker.get(msg_id, 0)
            index_tracker[msg_id] = idx + 1

            cursor.execute(insert_sql, (msg_id, display_name, phones, idx))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d vCard entries from %d messages", count, len(index_tracker))

    if progress_callback:
        progress_callback(count, count)

    return count
