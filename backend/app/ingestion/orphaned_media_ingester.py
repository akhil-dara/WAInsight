"""
Orphaned media ingester — discover media files on disk not linked to any message.

Scans the WhatsApp media directory tree, compares against known file paths in
the analysis DB, and stores orphaned files with parsed dates from filenames.

WhatsApp filename convention:
  IMG-20230116-WA0004.jpeg  → Jan 16, 2023
  VID-20230116-WA0004.mp4   → Jan 16, 2023
  PTT-20230116-WA0004.opus  → Jan 16, 2023
  STK-20230116-WA0004.webp  → Jan 16, 2023
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from app.db.connection import AnalysisConnection
except ImportError:
    AnalysisConnection = object  # type stub for when imported from GUI context

logger = logging.getLogger(__name__)

# Regex to parse date from WhatsApp filenames
_DATE_RE = re.compile(
    r"(?:IMG|VID|PTT|DOC|STK|AUD|VOICE|AUDIO)-(\d{8})-WA\d+",
    re.IGNORECASE,
)

# MIME type mapping by extension
_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".3gp": "video/3gpp", ".mkv": "video/x-matroska",
    ".opus": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".amr": "audio/amr",
    ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint", ".zip": "application/zip",
    ".apk": "application/vnd.android.package-archive",
}

# Extensions we care about
_MEDIA_EXTS = set(_MIME_MAP.keys())


def _parse_date(filename: str) -> tuple[str | None, int | None]:
    """Parse date from WhatsApp filename. Returns (YYYY-MM-DD, unix_ms) or (None, None)."""
    m = _DATE_RE.search(filename)
    if not m:
        return None, None
    ds = m.group(1)  # "20230116"
    try:
        dt = datetime.strptime(ds, "%Y%m%d")
        return dt.strftime("%Y-%m-%d"), int(dt.timestamp() * 1000)
    except ValueError:
        return None, None


def _get_mime(ext: str) -> str:
    return _MIME_MAP.get(ext.lower(), "application/octet-stream")


def _make_thumbnail(file_path: str, mime: str) -> tuple[bytes | None, int, int]:
    """Generate a small JPEG thumbnail for image files. Returns (blob, width, height)."""
    if not mime.startswith("image/"):
        return None, 0, 0
    try:
        from PIL import Image
        img = Image.open(file_path)
        w, h = img.size
        img.thumbnail((200, 200))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        return buf.getvalue(), w, h
    except Exception:
        return None, 0, 0


def ingest_orphaned_media(
    analysis_conn: AnalysisConnection,
    media_root: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Discover orphaned media files and store in orphaned_media table.

    Args:
        analysis_conn: Write connection to analysis.db.
        media_root: Path to WhatsApp media root (contains WhatsApp Images/, Video/, etc.)
        progress_callback: Optional (current, total) callback.

    Returns:
        Number of orphaned files discovered.
    """
    if not media_root or not media_root.exists():
        logger.warning("Media root not found: %s", media_root)
        return 0

    # Ensure table exists.  CREATE TABLE auto-commits in APSW (no
    # active transaction is opened by a DDL), so calling commit()
    # immediately after raises ``apsw.SQLError: cannot commit -
    # no transaction is active`` and aborts the surrounding
    # stage.  Wrap in explicit begin/commit so APSW sees a real
    # transaction it can close.
    try:
        analysis_conn.begin_transaction()
        analysis_conn.execute("""
            CREATE TABLE IF NOT EXISTS orphaned_media (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT, folder TEXT, file_size INTEGER, mime_type TEXT,
                parsed_date TEXT, parsed_date_ts INTEGER,
                file_hash TEXT, width INTEGER, height INTEGER, duration_ms INTEGER,
                matched_message_id INTEGER, matched_conversation_id INTEGER,
                matched_conv_name TEXT, thumbnail_blob BLOB
            )
        """)
        analysis_conn.commit()
    except Exception as e:
        # Defensive: if BEGIN fails because a transaction was somehow
        # already open from the caller, don't blow up.  Try the DDL
        # standalone — sqlite/apsw will autocommit it implicitly.
        logger.debug("orphaned_media CREATE TABLE wrap failed (%s); "
                     "retrying as standalone DDL", e)
        try:
            analysis_conn.rollback()
        except Exception:
            pass
        try:
            analysis_conn.execute("""
                CREATE TABLE IF NOT EXISTS orphaned_media (
                    id INTEGER PRIMARY KEY,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT, folder TEXT, file_size INTEGER, mime_type TEXT,
                    parsed_date TEXT, parsed_date_ts INTEGER,
                    file_hash TEXT, width INTEGER, height INTEGER, duration_ms INTEGER,
                    matched_message_id INTEGER, matched_conversation_id INTEGER,
                    matched_conv_name TEXT, thumbnail_blob BLOB
                )
            """)
        except Exception:
            logger.warning("Could not create orphaned_media table; "
                           "stage will be skipped")
            return 0

    # Build set of known file paths from media table
    logger.info("Building known paths set from media table...")
    known_paths = set()
    for r in analysis_conn.fetchall(
        "SELECT resolved_file_path FROM media WHERE resolved_file_path IS NOT NULL"
    ):
        if r[0]:
            known_paths.add(os.path.normpath(r[0]).lower())
    for r in analysis_conn.fetchall(
        "SELECT file_path FROM media WHERE file_path IS NOT NULL AND file_path != ''"
    ):
        if r[0]:
            # file_path is relative — try to resolve against media_root
            abs_path = media_root / r[0].replace("Media/", "")
            known_paths.add(os.path.normpath(str(abs_path)).lower())
    logger.info("Known paths: %d", len(known_paths))

    # Also exclude already-ingested orphaned files
    existing_orphans = set()
    try:
        for r in analysis_conn.fetchall("SELECT file_path FROM orphaned_media"):
            if r[0]:
                existing_orphans.add(os.path.normpath(r[0]).lower())
    except Exception:
        pass
    logger.info("Existing orphaned records: %d", len(existing_orphans))

    # Scan media directory
    logger.info("Scanning media directory: %s", media_root)
    all_files = []
    for root, dirs, files in os.walk(str(media_root)):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in _MEDIA_EXTS:
                fp = os.path.join(root, f)
                norm = os.path.normpath(fp).lower()
                if norm not in known_paths and norm not in existing_orphans:
                    all_files.append(fp)

    total = len(all_files)
    logger.info("Found %d orphaned media files to process", total)

    if total == 0:
        return 0

    insert_sql = """
        INSERT OR IGNORE INTO orphaned_media
        (file_path, file_name, folder, file_size, mime_type,
         parsed_date, parsed_date_ts, source_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    count = 0
    batch = []
    analysis_conn.begin_transaction()

    try:
        for i, fp in enumerate(all_files):
            fname = os.path.basename(fp)
            parent_dir = os.path.basename(os.path.dirname(fp))
            grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(fp)))
            ext = os.path.splitext(fname)[1].lower()
            mime = _get_mime(ext)

            # Determine folder (use grandparent if parent is Sent/Private)
            if parent_dir.lower() in ("sent", "private"):
                folder = grandparent_dir
            else:
                folder = parent_dir

            # Classify source type
            _lower_path = fp.lower().replace(chr(92), "/")
            if "/sent/" in _lower_path:
                source_type = "sent"
            elif "/private/" in _lower_path:
                source_type = "private"
            elif ".statuses" in _lower_path or "whatsapp_statuses" in _lower_path:
                source_type = "status"
            elif "gbwhatsapp" in _lower_path or "saved_viewonce" in _lower_path:
                source_type = "gbwhatsapp"
            elif ".links" in _lower_path:
                source_type = "links"
            else:
                source_type = "received"

            try:
                fsize = os.path.getsize(fp)
            except OSError:
                fsize = 0

            parsed_date, parsed_ts = _parse_date(fname)

            # No thumbnail during scan — generated on-demand in GUI for speed
            batch.append((
                fp, fname, folder, fsize, mime,
                parsed_date, parsed_ts, source_type,
            ))

            if len(batch) >= 5000:
                cursor = analysis_conn.get_cursor()
                cursor.executemany(insert_sql, batch)
                analysis_conn.commit()
                count += len(batch)
                batch.clear()
                analysis_conn.begin_transaction()

            if progress_callback and (i + 1) % 500 == 0:
                progress_callback(i + 1, total)

        # Flush remaining
        if batch:
            cursor = analysis_conn.get_cursor()
            cursor.executemany(insert_sql, batch)
            analysis_conn.commit()
            count += len(batch)

    except Exception:
        analysis_conn.rollback()
        raise

    if progress_callback:
        progress_callback(total, total)

    logger.info("Orphaned media ingestion complete: %d files", count)
    return count


def auto_orphan_rescue_pass(
    analysis_conn,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int]:
    """Compute SHA-256 for orphans whose size matches a missing media row,
    then rescue any missing media whose hash matches an orphan.

    Two outputs (returned as tuple):
      * matched_count   — orphans now linked to messages
      * rescued_count   — media rows whose recovery_method became
                          'orphan_recovered' (file_exists flipped to 1
                          and resolved_file_path now points at the
                          orphan's on-disk file)

    Why the size pre-filter?  Hashing every orphan file is
    disk-bound and slow on large extractions.  A SHA-256 match
    requires a byte-length match, so we only need to hash
    orphans whose ``file_size`` matches at least one missing-
    media row's ``file_size``.  In practice this cuts the work
    by an order of magnitude or more (most orphans have unique
    sizes), with no loss of forensic value.
    """
    import base64
    import hashlib

    cursor = analysis_conn.get_cursor()

    # Pre-flight 0: skip cleanly if there are no missing media at all
    try:
        peek = cursor.execute(
            "SELECT COUNT(*) FROM media "
            "WHERE file_exists = 0 AND file_hash IS NOT NULL AND file_hash != ''"
        ).fetchone()
        missing_with_hash = peek[0] if peek else 0
    except Exception:
        missing_with_hash = 0
    if missing_with_hash == 0:
        logger.info("Auto-orphan-rescue: no missing media with hash, skipping")
        return 0, 0

    # Find orphans needing hashing where size could plausibly match a
    # missing-media row.  This is a JOIN-as-set-intersection trick:
    # we want orphans where file_size IN (set of sizes of missing
    # media).  Plus we skip orphans that already have a hash from a
    # previous run.
    try:
        rows = cursor.execute(
            """
            SELECT om.id, om.file_path, om.file_size FROM orphaned_media om
             WHERE om.file_hash IS NULL
               AND om.file_size > 0
               AND om.file_size IN (
                   SELECT DISTINCT file_size FROM media
                    WHERE file_exists = 0
                      AND file_hash IS NOT NULL
                      AND file_hash != ''
                      AND file_size > 0
               )
            """
        ).fetchall()
    except Exception as e:
        logger.warning("Auto-orphan-rescue: candidate query failed: %s", e)
        return 0, 0

    total = len(rows)
    logger.info(
        "Auto-orphan-rescue: %d orphans need hashing "
        "(size-prefiltered against %d missing media rows)",
        total, missing_with_hash,
    )

    # Hash the candidates.  Each in its own try/except so a single
    # unreadable file doesn't kill the pass.
    hashed = 0
    failed = 0
    for i, r in enumerate(rows):
        oid, fp, _sz = r[0], r[1], r[2]
        if not fp or not os.path.isfile(fp):
            failed += 1
            continue
        try:
            h = hashlib.sha256()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            file_hash = base64.b64encode(h.digest()).decode("ascii")
            cursor.execute(
                "UPDATE orphaned_media SET file_hash = ? WHERE id = ?",
                (file_hash, oid),
            )
            hashed += 1
        except Exception:
            failed += 1
        if progress_callback and (i + 1) % 50 == 0:
            progress_callback(i + 1, total)
    if hashed:
        analysis_conn.commit()
    if progress_callback:
        progress_callback(total, total)
    logger.info(
        "Auto-orphan-rescue: hashed %d/%d orphans (%d failed)",
        hashed, total, failed,
    )

    # Now do the cross-match + rescue passes.  Same SQL HashMatchWorker
    # uses, but executed inline at ingestion time so a freshly-ingested
    # case is forensically complete without the user having to click a
    # button.
    matched_count = 0
    rescued_count = 0
    try:
        cursor.execute(
            """
            UPDATE orphaned_media SET
                matched_message_id = (
                    SELECT me.message_id FROM media me
                     WHERE me.file_hash = orphaned_media.file_hash
                       AND me.file_hash IS NOT NULL LIMIT 1
                ),
                matched_conversation_id = (
                    SELECT m.conversation_id FROM media me
                      JOIN message m ON m.id = me.message_id
                     WHERE me.file_hash = orphaned_media.file_hash
                       AND me.file_hash IS NOT NULL LIMIT 1
                ),
                matched_conv_name = (
                    SELECT COALESCE(cv.display_name, cv.jid_raw_string)
                      FROM media me
                      JOIN message m ON m.id = me.message_id
                      JOIN conversation cv ON cv.id = m.conversation_id
                     WHERE me.file_hash = orphaned_media.file_hash
                       AND me.file_hash IS NOT NULL LIMIT 1
                )
            WHERE file_hash IS NOT NULL
              AND file_hash != ''
              AND file_hash IN (
                  SELECT file_hash FROM media WHERE file_hash IS NOT NULL
              )
            """
        )
        peek2 = cursor.execute(
            "SELECT COUNT(*) FROM orphaned_media WHERE matched_message_id IS NOT NULL"
        ).fetchone()
        matched_count = peek2[0] if peek2 else 0
    except Exception as e:
        logger.warning("Auto-orphan-rescue: orphan→msg link failed: %s", e)

    try:
        now_ts = int(datetime.now().timestamp() * 1000)
        cursor.execute(
            """
            UPDATE media SET
                file_exists = 1,
                resolved_file_path = (
                    SELECT om.file_path FROM orphaned_media om
                     WHERE om.file_hash = media.file_hash
                       AND om.file_hash IS NOT NULL
                       AND om.file_hash != ''
                     ORDER BY om.id ASC
                     LIMIT 1
                ),
                recovery_method = 'orphan_recovered',
                recovery_timestamp = ?,
                media_status = 'on_disk'
            WHERE file_exists = 0
              AND file_hash IS NOT NULL
              AND file_hash != ''
              AND (recovery_method IS NULL OR recovery_method = '')
              AND EXISTS (
                  SELECT 1 FROM orphaned_media om
                   WHERE om.file_hash = media.file_hash
                     AND om.file_hash IS NOT NULL
                     AND om.file_hash != ''
              )
            """,
            (now_ts,),
        )
        peek3 = cursor.execute(
            "SELECT COUNT(*) FROM media WHERE recovery_method = 'orphan_recovered'"
        ).fetchone()
        rescued_count = peek3[0] if peek3 else 0
    except Exception as e:
        logger.warning("Auto-orphan-rescue: media-rescue failed: %s", e)

    analysis_conn.commit()
    logger.info(
        "Auto-orphan-rescue complete: %d orphans now linked to messages, "
        "%d previously-missing media rescued from orphaned files",
        matched_count, rescued_count,
    )
    return matched_count, rescued_count
