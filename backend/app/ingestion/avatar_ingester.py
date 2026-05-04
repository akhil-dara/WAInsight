"""
Profile picture (avatar) ingestion from WhatsApp's Avatars directory.

WhatsApp stores profile pictures as JPEG files in:
    files/data/data/com.whatsapp/files/Avatars/

File naming conventions:
    - {phone}@s.whatsapp.net.j     — Individual user (full-size photo)
    - {lid}@lid.j                   — LID user (full-size photo)
    - {group_id}@g.us.j            — Group icon (full-size photo)

This ingester reads each JPEG file, matches it to the corresponding
contact or conversation record in analysis.db, and stores the image
data as a BLOB in the contact.avatar_blob column.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.db.connection import DatabaseManager, AnalysisConnection

logger = logging.getLogger(__name__)


def ingest_avatars(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    avatars_path: Optional[Path] = None,
) -> int:
    """Ingest profile pictures from the Avatars directory into analysis.db.

    Scans for *.j files and matches them to contacts by JID string.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        avatars_path: Path to the Avatars directory. If None, tries to
            locate it relative to the databases path.

    Returns:
        Number of avatars ingested.
    """
    # Find the Avatars directory
    if avatars_path is None:
        # Standard path: databases_path is .../databases/
        # Avatars are at .../files/Avatars/
        db_path = db_manager.databases_path
        # Try multiple possible relative paths
        candidates = [
            db_path.parent / "files" / "Avatars",           # files/Avatars
            db_path / ".." / "files" / "Avatars",            # ../files/Avatars
            db_path / ".." / ".." / "files" / "Avatars",     # ../../files/Avatars
        ]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_dir():
                avatars_path = resolved
                break

    if avatars_path is None or not avatars_path.is_dir():
        logger.warning("Avatars directory not found - skipping profile picture ingestion")
        return 0

    logger.info("Found Avatars directory: %s", avatars_path)

    # Find all .j files (JPEG profile pictures)
    avatar_files = list(avatars_path.glob("*.j"))
    if not avatar_files:
        logger.info("No avatar files found in %s", avatars_path)
        return 0

    logger.info("Found %d avatar files", len(avatar_files))

    # Build JID → contact_id mapping from analysis.db
    # We need both phone_jid and lid_jid mappings
    contact_rows = analysis_conn.fetchall(
        "SELECT id, phone_jid, lid_jid FROM contact"
    )
    jid_to_contact_id: dict[str, int] = {}
    for row in contact_rows:
        cid, phone_jid, lid_jid = row
        if phone_jid:
            jid_to_contact_id[phone_jid] = cid
        if lid_jid:
            jid_to_contact_id[lid_jid] = cid

    # Also build JID → conversation_id for group avatars
    conv_rows = analysis_conn.fetchall(
        "SELECT id, jid_raw_string FROM conversation WHERE jid_raw_string IS NOT NULL"
    )
    jid_to_conv_id: dict[str, int] = {}
    for row in conv_rows:
        conv_id, jid_raw = row
        if jid_raw:
            jid_to_conv_id[jid_raw] = conv_id

    count = 0
    contact_count = 0
    group_count = 0

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for avatar_file in avatar_files:
            # Extract JID from filename: "12345@s.whatsapp.net.j" → "12345@s.whatsapp.net"
            jid = avatar_file.stem  # removes .j extension
            if not jid or "@" not in jid:
                continue

            # Read the JPEG data
            try:
                blob = avatar_file.read_bytes()
            except (OSError, IOError):
                continue

            if not blob or len(blob) < 100:  # Skip corrupt/empty files
                continue

            # Try to match to a contact
            contact_id = jid_to_contact_id.get(jid)
            if contact_id:
                cursor.execute(
                    "UPDATE contact SET avatar_blob = ? WHERE id = ?",
                    (blob, contact_id),
                )
                contact_count += 1
                count += 1
                continue

            # Try to match to a conversation (group avatar)
            conv_id = jid_to_conv_id.get(jid)
            if conv_id:
                cursor.execute(
                    "UPDATE conversation SET avatar_blob = ? WHERE id = ?",
                    (blob, conv_id),
                )
                group_count += 1
                count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info(
        "Avatar ingestion complete: %d total (%d contacts, %d groups)",
        count, contact_count, group_count,
    )
    return count
