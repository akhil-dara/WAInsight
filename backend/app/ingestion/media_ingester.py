"""
Media metadata ingestion from msgstore.db.

Processes ``message_media``, ``message_thumbnail`` and related
tables to populate the analysis ``media`` table with file paths,
dimensions, durations, thumbnail BLOBs, encryption keys, and
resolved on-disk paths.

Media-file resolution chain:
    1. ``message_media.file_path`` concatenated with the media
       root, checked for existence.
    2. Filename search in known media subdirectories.
    3. Fallback to the ``message_thumbnail.thumbnail`` BLOB.

Covers every media type WhatsApp emits — images, stickers,
animated stickers, videos, documents (PDF / DOCX / etc.), audio,
voice notes, and GIFs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)

BATCH_SIZE = 50_000


def ingest_media(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    media_root: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest media metadata from msgstore.db into analysis.db.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        media_root: Optional path to WhatsApp media root directory.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of media records ingested.
    """
    import time as _time_mod
    _now_ts = int(_time_mod.time())

    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_media"):
        logger.warning("message_media table not found")
        return 0

    total = reader.get_row_count("message_media")
    logger.info("Starting media ingestion: %d total records", total)

    # Build message_id lookup (source_msg_id → analysis message.id)
    msg_map = _build_msg_map(analysis_conn)

    # Check available columns
    mm_cols = reader.get_column_names("message_media")

    # Load thumbnails into a lookup (message_thumbnail + media_hash_thumbnail)
    thumb_lookup = _load_thumbnails(reader)
    hash_thumb_lookup = _load_hash_thumbnails(reader)

    # Normalize media_root: if the user pointed to a subdirectory (e.g. .../media/.Links
    # instead of .../WhatsApp/), walk up to find the directory containing "Media/" subfolder.
    if media_root and media_root.exists():
        if not (media_root / "Media").is_dir():
            for ancestor in [media_root.parent] + list(media_root.parents):
                if (ancestor / "Media").is_dir():
                    logger.info("Correcting media_root from %s to %s", media_root, ancestor)
                    media_root = ancestor
                    break
                if ancestor.name in ("files", "Desktop", "Documents", ""):
                    break

    # Build media subdirectory index for fallback resolution
    media_file_index: dict[str, Path] = {}
    if media_root and media_root.exists():
        media_file_index = _build_media_file_index(media_root)
        logger.info("Indexed %d media files in %s", len(media_file_index), media_root)

    insert_sql = """
        INSERT OR IGNORE INTO media (
            message_id, file_path, resolved_file_path, file_exists,
            file_size, mime_type, width, height, duration_ms,
            thumbnail_blob, media_url, direct_path,
            file_hash, enc_file_hash, media_key,
            media_caption, media_name, is_animated_sticker, page_count,
            accessibility_label, media_status, cdn_expiry_ts,
            source_media_row_id, transcription_text,
            was_transferred
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    processed = 0

    # Get ID range
    min_max = reader.execute_raw("SELECT MIN(message_row_id), MAX(message_row_id) FROM message_media")
    if not min_max or not min_max[0][0]:
        return 0

    min_id, max_id = min_max[0]
    current_id = min_id

    # === MEDIA KEY FALLBACK CHAIN ===
    # WhatsApp wipes media_key after viewing view-once messages. Keys can be
    # recovered from multiple fallback sources:
    #
    # Priority 1: message_media.e2ee_media_key (backup copy, identical to media_key)
    # Priority 2: message_quoted_media.media_key (when someone replies to a
    # view-once, the quoted media retains the original key!)
    #
    # Build a unified fallback map: source_msg_id -> media_key bytes

    _fallback_keys: dict[int, bytes] = {}

    # Fallback 1: e2ee_media_key
    if "e2ee_media_key" in mm_cols:
        try:
            for r in reader.execute_raw(
                "SELECT message_row_id, e2ee_media_key FROM message_media "
                "WHERE e2ee_media_key IS NOT NULL AND LENGTH(e2ee_media_key) = 32 "
                "AND (media_key IS NULL OR LENGTH(media_key) = 0)"
            ):
                _fallback_keys[r[0]] = r[1]
            logger.info("Loaded %d e2ee_media_key fallbacks", len(_fallback_keys))
        except Exception as e:
            logger.warning("Failed to load e2ee_media_key: %s", e)

    # Fallback 2: message_quoted_media — keys survive in quoted replies
    if reader.table_exists("message_quoted_media"):
        try:
            # Build file_hash -> media_key from quoted media
            _hash_to_key: dict[str, bytes] = {}
            for r in reader.execute_raw(
                "SELECT file_hash, media_key FROM message_quoted_media "
                "WHERE media_key IS NOT NULL AND LENGTH(media_key) = 32 "
                "AND file_hash IS NOT NULL AND file_hash != ''"
            ):
                _hash_to_key[r[0]] = r[1]

            # Also build media_job_uuid -> media_key
            _uuid_to_key: dict[str, bytes] = {}
            for r in reader.execute_raw(
                "SELECT media_job_uuid, media_key FROM message_quoted_media "
                "WHERE media_key IS NOT NULL AND LENGTH(media_key) = 32 "
                "AND media_job_uuid IS NOT NULL AND media_job_uuid != ''"
            ):
                _uuid_to_key[r[0]] = r[1]

            # Match against view-once msgs missing keys
            before = len(_fallback_keys)
            for r in reader.execute_raw(
                "SELECT md.message_row_id, md.file_hash, md.media_job_uuid "
                "FROM message_media md "
                "JOIN message_view_once_media vo ON vo.message_row_id = md.message_row_id "
                "WHERE (md.media_key IS NULL OR LENGTH(md.media_key) = 0) "
                "AND (md.e2ee_media_key IS NULL OR LENGTH(md.e2ee_media_key) = 0)"
            ):
                msg_id, fhash, uuid = r
                if msg_id in _fallback_keys:
                    continue
                if fhash and fhash in _hash_to_key:
                    _fallback_keys[msg_id] = _hash_to_key[fhash]
                elif uuid and uuid in _uuid_to_key:
                    _fallback_keys[msg_id] = _uuid_to_key[uuid]

            quoted_recovered = len(_fallback_keys) - before
            logger.info("Recovered %d keys via message_quoted_media (total fallbacks: %d)",
                        quoted_recovered, len(_fallback_keys))
        except Exception as e:
            logger.warning("Failed to load quoted media keys: %s", e)

    while current_id <= max_id:
        batch_end = current_id + BATCH_SIZE

        # Build select based on available columns
        # Include _id for forensic provenance (source_media_row_id)
        has_media_id = "_id" in mm_cols
        select_cols = ["message_row_id"]
        if has_media_id:
            select_cols.append("_id")
        opt_map = {
            "file_path": "file_path",
            "file_size": "file_size",
            "file_length": "file_length",  # More accurate size (file_size is often 0)
            # ``transferred`` is the authoritative phone-side flag for
            # "the user actually received the bytes for THIS message".
            # WhatsApp may write a file_path metadata even when the
            # user never downloaded the file (de-duped because another
            # message with the same SHA-256 was on disk).  We need this
            # column to mark such messages as hash_linked rather than
            # claiming they were originally received here.
            "transferred": "transferred",
            "mime_type": "mime_type",
            "width": "width",
            "height": "height",
            "media_duration": "duration",
            "message_url": "media_url",
            "direct_path": "direct_path",
            "file_hash": "file_hash",
            "enc_file_hash": "enc_file_hash",
            "media_key": "media_key",
            "media_caption": "caption",
            "media_name": "media_name",
            "is_animated_sticker": "is_animated",
            "page_count": "page_count",
            "accessibility_label": "acc_label",
            "raw_transcription_text": "transcription",
        }

        available = []
        for col, alias in opt_map.items():
            if col in mm_cols:
                select_cols.append(col)
                available.append(alias)
            else:
                select_cols.append(f"NULL")
                available.append(alias)

        rows = reader.execute_raw(
            f"SELECT {', '.join(select_cols)} FROM message_media "
            f"WHERE message_row_id >= ? AND message_row_id < ? "
            f"ORDER BY message_row_id",
            (current_id, batch_end),
        )

        if not rows:
            current_id = batch_end
            continue

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for row in rows:
                source_msg_id = row[0]
                msg_id = msg_map.get(source_msg_id)
                if msg_id is None:
                    continue

                # If _id column was included, it's at index 1
                if has_media_id:
                    source_media_row_id = row[1]
                    vals = dict(zip(available, row[2:]))
                else:
                    source_media_row_id = None
                    vals = dict(zip(available, row[1:]))

                # Resolve file path
                file_path = vals.get("file_path")
                resolved_path = None
                file_exists = False

                if file_path and media_root:
                    # Try direct concatenation: media_root / "Media/WhatsApp Images/IMG.jpg"
                    candidate = media_root / file_path
                    if candidate.exists():
                        resolved_path = str(candidate)
                        file_exists = True
                    else:
                        # Try stripping leading "Media/" (double-prefix issue)
                        stripped = file_path
                        if file_path.startswith("Media/") or file_path.startswith("Media\\"):
                            stripped = file_path[6:]
                            alt = media_root / stripped
                            if alt.exists():
                                resolved_path = str(alt)
                                file_exists = True
                        # Try parent directory (media_root may be inside Media/)
                        if not file_exists and media_root.parent:
                            alt2 = media_root.parent / file_path
                            if alt2.exists():
                                resolved_path = str(alt2)
                                file_exists = True
                        # Try grandparent (media_root may point deep, e.g. .../media/.Links)
                        if not file_exists:
                            for ancestor in media_root.parents:
                                test = ancestor / file_path
                                if test.exists():
                                    resolved_path = str(test)
                                    file_exists = True
                                    break
                                # Also try with stripped path
                                if stripped != file_path:
                                    test2 = ancestor / stripped
                                    if test2.exists():
                                        resolved_path = str(test2)
                                        file_exists = True
                                        break
                                # Stop at reasonable depth (don't go above extraction root)
                                if ancestor.name in ("files", "Desktop", "Documents", ""):
                                    break
                        # Fallback: filename-only search in index
                        if not file_exists:
                            filename = Path(file_path).name
                            if filename in media_file_index:
                                resolved_path = str(media_file_index[filename])
                                file_exists = True

                # Get thumbnail: prefer per-message, fallback to hash-based
                thumbnail = thumb_lookup.get(source_msg_id)
                if not thumbnail and hash_thumb_lookup:
                    fh = vals.get("file_hash")
                    if fh:
                        thumbnail = hash_thumb_lookup.get(fh)

                # Duration in milliseconds (source stores in seconds for some types)
                duration = vals.get("duration")
                duration_ms = None
                if duration and duration > 0:
                    # WhatsApp stores duration in seconds
                    duration_ms = int(duration * 1000) if duration < 100000 else int(duration)

                # Extract filename from file_path if media_name is empty
                media_name_val = vals.get("media_name")
                if not media_name_val and file_path:
                    media_name_val = Path(file_path).name

                # Compute media_status + cdn_expiry_ts
                media_url = vals.get("media_url") or ""
                media_key_val = vals.get("media_key")
                # Fallback chain: e2ee_media_key → message_quoted_media (file_hash/uuid match)
                if (media_key_val is None or (isinstance(media_key_val, bytes) and len(media_key_val) == 0)) \
                        and source_msg_id in _fallback_keys:
                    media_key_val = _fallback_keys[source_msg_id]
                    vals["media_key"] = media_key_val

                # Parse CDN URL expiry (oe= hex timestamp, UTC seconds)
                cdn_expiry_ts = 0
                if media_url:
                    import re as _re
                    _oe = _re.search(r'oe=([0-9A-Fa-f]+)', media_url)
                    if _oe:
                        cdn_expiry_ts = int(_oe.group(1), 16)

                has_url = bool(media_url.strip())
                has_key = media_key_val is not None and (not isinstance(media_key_val, bytes) or len(media_key_val) > 0)
                url_expired = cdn_expiry_ts > 0 and cdn_expiry_ts < _now_ts

                # Forensic-correctness check.  WhatsApp's
                # message_media.transferred = 1 means the bytes were
                # actually downloaded for THIS specific message.
                # transferred = 0 (the default for forwarded / unread
                # media) means the user never received the file here -
                # WhatsApp may have written a file_path metadata
                # because the same SHA-256 exists from another
                # message, but for THIS message no file was ever
                # received.  We must mark such rows as hash_linked so
                # the renderer never falsely claims this message was
                # the original receipt.
                _transferred_val = vals.get("transferred")
                _was_actually_transferred = (
                    _transferred_val is None or int(_transferred_val or 0) == 1
                )
                _ingest_recovery_method = ""
                if file_exists and not _was_actually_transferred:
                    # File resolves on disk (because another message
                    # with same hash had it), but THIS message never
                    # received the bytes.  Flag as hash-linked.
                    _ingest_recovery_method = "hash_linked"

                if file_exists:
                    media_status = "on_disk"
                elif has_url and has_key and not url_expired:
                    media_status = "downloadable"
                elif has_url and has_key and url_expired:
                    media_status = "expired"
                elif has_url and not has_key:
                    media_status = "no_key"
                elif thumbnail is not None:
                    media_status = "thumb_only"
                else:
                    media_status = "missing"

                # Authoritative phone-side flag.  None means the older
                # WhatsApp schema didn't expose this column.
                _was_transferred_db = (
                    None if _transferred_val is None
                    else (1 if int(_transferred_val or 0) == 1 else 0)
                )

                cursor.execute(insert_sql, (
                    msg_id,
                    file_path,
                    resolved_path,
                    file_exists,
                    vals.get("file_length") or vals.get("file_size"),  # file_length is accurate; file_size is often 0
                    vals.get("mime_type"),
                    vals.get("width"),
                    vals.get("height"),
                    duration_ms,
                    thumbnail,
                    vals.get("media_url"),
                    vals.get("direct_path"),
                    vals.get("file_hash"),
                    vals.get("enc_file_hash"),
                    vals.get("media_key"),
                    vals.get("caption"),
                    media_name_val,
                    1 if vals.get("is_animated") else 0,
                    vals.get("page_count"),
                    vals.get("acc_label"),
                    media_status,
                    cdn_expiry_ts or None,
                    source_media_row_id,
                    vals.get("transcription"),
                    _was_transferred_db,
                ))
                # Set recovery_method post-insert so we don't have to
                # change the wide INSERT signature (24 placeholders).
                # Only fires for the hash-linked-on-ingest case; if the
                # source had transferred=1 (or NULL on older schemas),
                # recovery_method stays NULL = "originally received here".
                if _ingest_recovery_method:
                    cursor.execute(
                        "UPDATE media SET recovery_method = ? "
                        "WHERE message_id = ?",
                        (_ingest_recovery_method, msg_id),
                    )
                processed += 1

            analysis_conn.commit()

        except Exception:
            analysis_conn.rollback()
            raise

        if progress_callback:
            progress_callback(processed, total)

        current_id = batch_end

    logger.info("Media ingestion complete: %d records", processed)

    # Log file resolution stats
    if media_root:
        found = analysis_conn.fetchone("SELECT COUNT(*) FROM media WHERE file_exists = 1")
        missing = analysis_conn.fetchone("SELECT COUNT(*) FROM media WHERE file_exists = 0")
        logger.info(
            "Media file resolution: %d found on disk, %d missing (thumbnail fallback)",
            found[0] if found else 0, missing[0] if missing else 0,
        )

    # Auto-hash-link pass: every "missing" media row that shares a
    # SHA-256 with an "on-disk" sibling gets linked to the sibling's
    # resolved path AND tagged ``recovery_method='hash_linked'``.
    # WhatsApp itself shows this content (it's bytewise identical) but
    # didn't actually transfer it for these specific messages.
    # Without this pass the analyst would see "X Not Found" on a
    # message whose content is sitting right there in another
    # chat with the same SHA-256.
    _auto_hash_link_pass(analysis_conn)

    # HD/SD twin pass: WhatsApp's dual-quality send creates two `message`
    # rows linked via msgstore.message_association.  Two flavours:
    #   * association_type = 7  → VIDEO  pair (parent=SD, child=HD)
    #   * association_type = 12 → IMAGE  pair (parent=SD, child=HD)
    # Both follow the same parent.quality=3 / child.quality=4 +
    # child.origination_flags & 0x4000000 pattern.  Reactions, replies,
    # quotes and edits all attach to the parent.  Without this pass
    # the chat shows TWO duplicate bubbles for one logical user-action.
    # We mark the child is_hd_twin=1 so the chat viewer hides it, and
    # store hd_twin_msg_id on the parent so the renderer can prefer
    # the HD bytes for display.
    _link_hd_twins_pass(reader, analysis_conn, msg_map)

    # Post-processing: played_self_receipt → update media.first_viewed_ts
    _ingest_played_self_receipt(reader, analysis_conn, msg_map)

    return processed


def _count_recovery(analysis_conn: AnalysisConnection, value: str) -> int:
    """COUNT(*) wrapper used by ``_auto_hash_link_pass`` because
    APSW's Cursor doesn't expose ``rowcount``.  We can't tell from
    the UPDATE itself how many rows it touched, so we just count
    the rows that now carry the recovery_method we just wrote.
    Conservative — slightly over-counts if the case had pre-existing
    rows with the same recovery_method — but accurate enough for
    log-line reporting and not the actual logic gate.
    """
    try:
        cur = analysis_conn.get_cursor()
        row = cur.execute(
            "SELECT COUNT(*) FROM media WHERE recovery_method = ?", (value,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _auto_hash_link_pass(analysis_conn: AnalysisConnection) -> None:
    """Link every missing media row to a sibling's on-disk file when the
    SHA-256 matches.  Two distinct outcomes based on whether the user
    originally downloaded this file for THIS message (was_transferred):

      * was_transferred = 1 -> ``hash_linked_after_delete``
        The user DID receive the file here originally, but the local
        file was deleted later (manually, by storage cleanup, etc).
        We can still surface the bytes because the same SHA-256 sits
        in another message that wasn't deleted.  Forensically: this
        IS proof of receipt, just not proof of current possession.

      * was_transferred = 0 / NULL -> ``hash_linked``
        The user never downloaded the file for this specific message.
        WhatsApp may have written file_path metadata (dedup quirk),
        but transferred=0 confirms no receipt.  The bytes shown
        belong to a different message.

    Without distinguishing these, an analyst can't tell "deleted by
    user but originally received" (legally meaningful) from "never
    received at all" (typically not).
    """
    try:
        cursor = analysis_conn.get_cursor()
        # Pre-flight: is there any work to do?
        peek = cursor.execute(
            "SELECT COUNT(*) FROM media WHERE file_exists = 0 "
            "AND file_hash IS NOT NULL AND file_hash != '' "
            "AND EXISTS (SELECT 1 FROM media donor "
            "  WHERE donor.file_hash = media.file_hash "
            "  AND donor.file_exists = 1 "
            "  AND donor.resolved_file_path IS NOT NULL "
            "  AND donor.id != media.id)"
        ).fetchone()
        candidates = peek[0] if peek else 0
        if not candidates:
            logger.info("Auto-hash-link: 0 candidates")
            return
        analysis_conn.begin_transaction()
        try:
            # Pass 1: rows where the user ORIGINALLY received the file
            # (was_transferred=1) but the on-disk copy is gone now.
            # These get the distinctive 'hash_linked_after_delete'
            # marker so the analyst knows it's "received & deleted",
            # not "never received".
            cursor.execute("""
                UPDATE media
                   SET file_exists = 1,
                       resolved_file_path = (
                           SELECT donor.resolved_file_path FROM media donor
                            WHERE donor.file_hash = media.file_hash
                              AND donor.file_exists = 1
                              AND donor.resolved_file_path IS NOT NULL
                              AND donor.id != media.id
                            ORDER BY donor.id ASC LIMIT 1
                       ),
                       recovery_method = 'hash_linked_after_delete',
                       media_status = 'on_disk'
                 WHERE file_exists = 0
                   AND was_transferred = 1
                   AND file_hash IS NOT NULL AND file_hash != ''
                   AND EXISTS (
                       SELECT 1 FROM media donor
                        WHERE donor.file_hash = media.file_hash
                          AND donor.file_exists = 1
                          AND donor.resolved_file_path IS NOT NULL
                          AND donor.id != media.id
                   )
            """)
            # APSW's Cursor object has no ``rowcount`` attribute
            # (only the stdlib sqlite3 driver does, and the
            # ingester subprocess uses APSW), so we use a follow-
            # up COUNT(*) for the row tally instead of
            # ``cursor.rowcount``.
            n_after_delete = _count_recovery(
                analysis_conn, "hash_linked_after_delete"
            )

            # Pass 2: rows the user never actually downloaded.  Either
            # was_transferred=0 explicitly, or the older WhatsApp schema
            # didn't expose the column (was_transferred IS NULL) - in
            # the unknown case we conservatively assume "not received
            # here" because hash_linked is the safer default forensic
            # claim than implying receipt.
            cursor.execute("""
                UPDATE media
                   SET file_exists = 1,
                       resolved_file_path = (
                           SELECT donor.resolved_file_path FROM media donor
                            WHERE donor.file_hash = media.file_hash
                              AND donor.file_exists = 1
                              AND donor.resolved_file_path IS NOT NULL
                              AND donor.id != media.id
                            ORDER BY donor.id ASC LIMIT 1
                       ),
                       recovery_method = 'hash_linked',
                       media_status = 'on_disk'
                 WHERE file_exists = 0
                   AND (was_transferred = 0 OR was_transferred IS NULL)
                   AND file_hash IS NOT NULL AND file_hash != ''
                   AND EXISTS (
                       SELECT 1 FROM media donor
                        WHERE donor.file_hash = media.file_hash
                          AND donor.file_exists = 1
                          AND donor.resolved_file_path IS NOT NULL
                          AND donor.id != media.id
                   )
            """)
            n_hash_linked = _count_recovery(analysis_conn, "hash_linked")
            analysis_conn.commit()
            logger.info(
                "Auto-hash-link: %d hash_linked_after_delete (received then deleted) "
                "+ %d hash_linked (never received here)",
                n_after_delete, n_hash_linked,
            )
        except Exception:
            analysis_conn.rollback()
            raise
    except Exception as e:
        logger.warning("Auto-hash-link pass failed: %s", e)


def _link_hd_twins_pass(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    msg_map: dict[int, int],
) -> None:
    """Mark association-children that should be hidden from the chat list,
    and link the relevant ones back to their parent.

    ``message_association`` in msgstore stitches together rows that
    represent ONE user-action but get stored as multiple `message`
    rows in WhatsApp's schema.  We've validated four flavours:

      * ``association_type = 7``  — VIDEO dual-quality pair
                                     (parent = SD, child = HD)
      * ``association_type = 12`` — IMAGE dual-quality pair
                                     (parent = SD, child = HD)
      * ``association_type = 11`` — MOTION PHOTO pair
                                     (parent = still image,
                                      child = 1-2 s video clip)
      * ``association_type = 4``  — STATUS post link-preview metadata
                                     (parent = the status post,
                                      children = link-card text rows)
      * ``association_type = 6``  — CHANNEL POLL image options
                                     (parent = poll, children = the
                                      images attached to each option)

    For all five, the user-facing message identity sits on the parent
    — reactions / replies / quotes / edits / search results all
    reference the parent.  The children are scaffolding: they exist
    in msgstore so WhatsApp can render the rich bubble (HD bytes,
    motion clip, link card, image-option grid) but are NOT separate
    messages.  This pass:

      * marks every such child with ``media.is_hd_twin = 1`` so the
        chat viewer SQL filter hides them from the message list;
      * writes ``media.hd_twin_msg_id``  on parents of types 7 / 12
        so the renderer can swap in HD bytes;
      * writes ``media.motion_video_msg_id`` on parents of type 11
        so the renderer can play the motion clip on click.

    Types 4 + 6 don't need parent-side metadata — the children are
    already redundant text/image scaffolding the parent's own
    rendering re-creates.  We just hide them.
    """
    if not reader.table_exists("message_association"):
        return

    try:
        rows = reader.execute_raw(
            "SELECT parent_message_row_id, child_message_row_id, association_type "
            "FROM message_association WHERE association_type IN (4, 6, 7, 11, 12)"
        )
    except Exception as e:
        logger.warning("Pair-link pass: failed reading message_association: %s", e)
        return

    if not rows:
        logger.info("Pair-link pass: 0 relevant associations in this case")
        return

    # Categorize pairs by association_type so we can do per-type
    # parent-side updates only where they make sense.
    hd_pairs:        list[tuple[int, int]] = []   # types 7 + 12
    motion_pairs:    list[tuple[int, int]] = []   # type 11
    status_children: list[int] = []               # type 4
    poll_children:   list[int] = []               # type 6
    counts = {4: 0, 6: 0, 7: 0, 11: 0, 12: 0}

    for parent_msgstore_id, child_msgstore_id, atype in rows:
        p_aid = msg_map.get(parent_msgstore_id)
        c_aid = msg_map.get(child_msgstore_id)
        if p_aid is None or c_aid is None:
            continue
        counts[atype] = counts.get(atype, 0) + 1
        if atype in (7, 12):
            hd_pairs.append((p_aid, c_aid))
        elif atype == 11:
            motion_pairs.append((p_aid, c_aid))
        elif atype == 4:
            status_children.append(c_aid)
        elif atype == 6:
            poll_children.append(c_aid)

    total_children = (
        len(hd_pairs) + len(motion_pairs)
        + len(status_children) + len(poll_children)
    )
    if total_children == 0:
        logger.info("Pair-link pass: 0 resolvable pairs after msg_map lookup")
        return

    # Collect (child_msg_id, parent_msg_id) for ALL types so we can
    # populate the generic assoc_parent_msg_id back-pointer on each
    # child.  This is what lets the poll renderer enumerate option-
    # images attached to a poll (type-6) without re-reading msgstore.
    all_child_to_parent: list[tuple[int, int]] = []
    for p, c in hd_pairs:
        all_child_to_parent.append((c, p))
    for p, c in motion_pairs:
        all_child_to_parent.append((c, p))
    # type-4 / type-6 keep just the child msg_id list above; we need
    # parent_msg_id too for the back-pointer.  Re-walk the rows to
    # capture parent.
    for parent_msgstore_id, child_msgstore_id, atype in rows:
        if atype not in (4, 6):
            continue
        p_aid = msg_map.get(parent_msgstore_id)
        c_aid = msg_map.get(child_msgstore_id)
        if p_aid is None or c_aid is None:
            continue
        all_child_to_parent.append((c_aid, p_aid))

    cursor = analysis_conn.get_cursor()
    analysis_conn.begin_transaction()
    try:
        # ---- Mark each child with its association kind ----
        # ``is_hd_twin = 1`` flags the row as "association child"
        # (a now-misleading name kept for backward compat with
        # older schemas that didn't separate the kinds).  The new
        # ``assoc_kind`` column is what the chat-list WHERE clause
        # actually checks: 'hd' members render as separate
        # bubbles, while 'motion'/'status'/'poll' stay hidden.
        # Combining the two lets the SQL query be a cheap
        # indexed probe instead of a correlated subquery.
        cursor.executemany(
            "UPDATE media SET is_hd_twin = 1, assoc_kind = 'hd' "
            "WHERE message_id = ?",
            [(c,) for _, c in hd_pairs],
        )
        cursor.executemany(
            "UPDATE media SET is_hd_twin = 1, assoc_kind = 'motion' "
            "WHERE message_id = ?",
            [(c,) for _, c in motion_pairs],
        )
        cursor.executemany(
            "UPDATE media SET is_hd_twin = 1, assoc_kind = 'status' "
            "WHERE message_id = ?",
            [(c,) for c in status_children],
        )
        cursor.executemany(
            "UPDATE media SET is_hd_twin = 1, assoc_kind = 'poll' "
            "WHERE message_id = ?",
            [(c,) for c in poll_children],
        )

        # ---- Generic back-pointer: child → parent ----
        if all_child_to_parent:
            cursor.executemany(
                "UPDATE media SET assoc_parent_msg_id = ? WHERE message_id = ?",
                [(p, c) for c, p in all_child_to_parent],
            )

        # ---- HD twins: store parent → child pointer ----
        if hd_pairs:
            cursor.executemany(
                "UPDATE media SET hd_twin_msg_id = ? WHERE message_id = ?",
                [(c, p) for p, c in hd_pairs],
            )

        # ---- Motion photos: store parent → motion-clip pointer ----
        if motion_pairs:
            cursor.executemany(
                "UPDATE media SET motion_video_msg_id = ? WHERE message_id = ?",
                [(c, p) for p, c in motion_pairs],
            )

        analysis_conn.commit()
    except Exception as e:
        analysis_conn.rollback()
        logger.warning("Pair-link pass: UPDATE failed: %s", e)
        return

    logger.info(
        "Pair-link pass: hid %d association-child rows from chat list — "
        "HD video pairs (type-7) = %d, HD image pairs (type-12) = %d, "
        "motion-photo pairs (type-11) = %d, status link-previews "
        "(type-4) = %d, channel poll image options (type-6) = %d",
        total_children,
        counts.get(7, 0), counts.get(12, 0), counts.get(11, 0),
        counts.get(4, 0), counts.get(6, 0),
    )


def _ingest_played_self_receipt(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    msg_map: dict[int, int],
) -> None:
    """Update ``media.first_viewed_ts`` from msgstore's
    ``played_self_receipt`` table.

    That table records when the device owner opened a received
    view-once message (voice note, video note, or image).
    """
    if not reader.table_exists("played_self_receipt"):
        return

    try:
        cols = reader.get_column_names("played_self_receipt")
        has_ts = "timestamp" in cols

        rows = reader.execute_raw(
            "SELECT message_row_id" + (", timestamp" if has_ts else "")
            + " FROM played_self_receipt"
        )
        if not rows:
            return

        count = 0
        analysis_conn.begin_transaction()
        try:
            for row in rows:
                source_msg_id = row[0]
                played_ts = row[1] if has_ts and len(row) > 1 else None
                msg_id = msg_map.get(source_msg_id)
                if not msg_id:
                    continue
                # Update media.first_viewed_ts if not already set
                if played_ts:
                    analysis_conn.execute(
                        "UPDATE media SET first_viewed_ts = ? "
                        "WHERE message_id = ? AND first_viewed_ts IS NULL",
                        (played_ts, msg_id),
                    )
                    count += 1
            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        if count:
            logger.info("Updated %d media records with played_self_receipt timestamps", count)
    except Exception as e:
        logger.warning("Failed to process played_self_receipt: %s", e)


def _build_msg_map(analysis_conn: AnalysisConnection) -> dict[int, int]:
    """Build source message._id → analysis message.id lookup."""
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    return {row[0]: row[1] for row in rows}


def _load_thumbnails(reader: SourceReader) -> dict[int, bytes]:
    """Load thumbnail BLOBs from message_thumbnail table.

    Returns dict mapping message_row_id → thumbnail bytes.
    Thumbnails cover images, videos, AND documents/PDFs.
    """
    if not reader.table_exists("message_thumbnail"):
        return {}

    rows = reader.execute_raw(
        "SELECT message_row_id, thumbnail FROM message_thumbnail "
        "WHERE thumbnail IS NOT NULL"
    )
    result = {row[0]: row[1] for row in rows}
    logger.info("Loaded %d thumbnails from message_thumbnail", len(result))
    return result


def _load_hash_thumbnails(reader: SourceReader) -> dict[str, bytes]:
    """Load thumbnail BLOBs from media_hash_thumbnail table.

    This table stores one thumbnail per unique file_hash.  When a media file
    is forwarded/shared multiple times, a single hash entry covers all
    copies.  This nearly doubles thumbnail coverage compared to
    message_thumbnail alone.

    Returns dict mapping file_hash (hex string) → thumbnail bytes.
    """
    if not reader.table_exists("media_hash_thumbnail"):
        return {}

    rows = reader.execute_raw(
        "SELECT media_hash, thumbnail FROM media_hash_thumbnail "
        "WHERE thumbnail IS NOT NULL AND length(thumbnail) > 50"
    )
    result = {row[0]: row[1] for row in rows}
    logger.info("Loaded %d hash-based thumbnails from media_hash_thumbnail", len(result))
    return result


def _build_media_file_index(media_root: Path) -> dict[str, Path]:
    """Build filename → path index for fallback media resolution.

    Scans the WhatsApp media directory tree and indexes all files by
    filename for quick lookups when the stored file_path doesn't resolve.
    """
    index: dict[str, Path] = {}
    try:
        for file_path in media_root.rglob("*"):
            if file_path.is_file():
                index[file_path.name] = file_path
    except (PermissionError, OSError) as e:
        logger.warning("Error scanning media directory: %s", e)
    return index
