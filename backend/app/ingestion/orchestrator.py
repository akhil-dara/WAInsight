"""
20-stage ingestion pipeline orchestrator.

Coordinates the complete ETL process from source WhatsApp databases to
the normalized analysis.db. Each stage is executed sequentially with
progress tracking and error recovery.

Pipeline stages:
    1.  VALIDATE     - Verify source DBs, detect schema version, check media dir
    2.  HASH         - SHA-256 all source databases (evidence chain)
    3.  CONTACTS     - Build unified contact registry from 5 identity sources
    4.  CONVERSATIONS- Normalize chats
    5.  MEMBERS      - Ingest group memberships (handled in CONVERSATIONS)
    6.  MESSAGES     - Normalize messages (batched, multi-table JOIN)
    7.  MEDIA        - Normalize media records + resolve file paths
    8.  RECEIPTS     - Ingest user receipts
    9.  REACTIONS    - Ingest reactions
    10. SYSTEM_EVENTS- Parse system events (60+ action types)
    11. CALLS        - Normalize call records
    12. SCHEDULED_EVENTS - Ingest scheduled calls and calendar events
    13. POLLS        - Ingest polls + votes
    14. EDITS        - Ingest edit records
    15. REVOKES      - Process revoked messages + ghost recovery
    16. LINKS        - Process URL link records
    17. NEWSLETTERS  - Ingest newsletter-specific metadata (handled in MESSAGES)
    18. COMPANION    - Ingest companion DB data (chatsettings, stickers, media)
    19. FTS_INDEX    - Build FTS5 full-text search index
    20. PRECOMPUTE   - Pre-compute analytics tables
    21. FINALIZE     - Verify integrity, write case metadata

Runtime scales with message and media volume; on an SSD a typical
phone backup completes in tens of minutes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import AppConfig
from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.analysis_db import AnalysisDatabase
from app.db.source_reader import SourceReader
from app.ingestion.progress import PipelineProgress
from app.utils.hashing import compute_evidence_hashes

logger = logging.getLogger(__name__)


class IngestionOrchestrator:
    """Coordinates the 20-stage ingestion pipeline.

    Manages the analysis.db lifecycle, executes each stage in order,
    tracks progress, and handles errors gracefully.

    Usage::

        config = build_config(...)
        orchestrator = IngestionOrchestrator(config)
        orchestrator.run()
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db_manager: Optional[DatabaseManager] = None
        self._analysis_db: Optional[AnalysisDatabase] = None
        self._analysis_conn: Optional[AnalysisConnection] = None
        self._owner_name: Optional[str] = None
        self._owner_phone_jid: Optional[str] = None
        self.progress = PipelineProgress()

    def run(self, force_recreate: bool = True) -> None:
        """Execute the full 20-stage ingestion pipeline.

        Args:
            force_recreate: If True, delete and recreate analysis.db.
                          If False, skip if analysis.db already exists.
        """
        self.progress.start_pipeline()

        try:
            # Initialize database connections
            self._db_manager = DatabaseManager(
                databases_path=self._config.source.databases_path,
                analysis_db_path=self._config.analysis_db_path,
                mmap_size=self._config.analysis.max_mmap_size,
                cache_mb=self._config.analysis.page_cache_mb,
                extra_db_paths=self._config.source.extra_db_paths,
            )

            # Create analysis.db
            self._analysis_db = AnalysisDatabase(self._config.analysis_db_path)
            self._analysis_db.create(force=force_recreate)
            self._analysis_conn = self._db_manager.get_analysis()

            # Tell contact_resolver where ``shared_prefs`` XML files live.
            # When the GUI staged them to a temp folder, this keeps the
            # source extraction directory untouched.  Passing ``None``
            # restores the legacy ``<msgstore_dir>/../shared_prefs`` lookup.
            from app.ingestion.contact_resolver import set_prefs_dir_override
            set_prefs_dir_override(
                str(self._config.source.prefs_dir)
                if self._config.source.prefs_dir else None
            )

            # Execute stages
            self._stage_validate()
            self._stage_hash()
            self._stage_contacts()
            self._stage_conversations()
            self._stage_messages()
            self._stage_albums()
            self._stage_media()
            self._stage_media_evidence()
            self._stage_orphaned_media()
            self._stage_receipts()
            self._stage_reactions()
            self._stage_system_events()
            self._stage_group_metadata()
            self._stage_calls()
            self._stage_scheduled_events()
            self._stage_polls()
            self._stage_vcards()
            self._stage_pins()
            self._stage_edits()
            self._stage_comments()
            self._stage_revokes()
            self._stage_links()
            self._stage_mentions()
            self._stage_status()
            self._stage_companion()
            self._ingest_location_db()  # Optional: live location route points from location.db
            self._stage_avatars()
            self._stage_fts_index()
            self._stage_precompute()
            self._stage_finalize()

            self.progress.complete_pipeline()

        except Exception as e:
            logger.exception("Pipeline failed")
            self.progress.fail_pipeline(str(e))
            raise

        finally:
            if self._db_manager:
                self._db_manager.close_all()

    # ---- Individual stages ----

    def _stage_validate(self) -> None:
        """Stage 1: Validate source databases and detect schema version."""
        self.progress.start_stage("VALIDATE")

        errors = self._config.source.validate()
        if errors:
            for err in errors:
                logger.error("Validation error: %s", err)
            self.progress.fail_stage("VALIDATE", "; ".join(errors))
            raise RuntimeError(f"Source validation failed: {errors}")

        # Detect schema version
        msgstore = self._db_manager.get_msgstore()
        reader = SourceReader(msgstore)
        version = reader.get_schema_version()
        logger.info("Detected WhatsApp schema version: %d", version)

        # Log available databases
        dbs = self._config.source.list_available_databases()
        logger.info("Available databases: %s", [db.name for db in dbs])

        # Log table counts for key tables
        stats = reader.get_database_stats()
        for table in stats.tables[:20]:
            if table.row_count > 0:
                logger.info("  %s: %d rows", table.name, table.row_count)

        self.progress.complete_stage("VALIDATE", len(dbs))

    def _stage_hash(self) -> None:
        """Stage 2: Compute SHA-256 evidence hashes for all source databases.

        Also records the absolute source paths + file sizes + hashes into
        both (a) the analysis.db case_metadata table, and (b) the case's
        external metadata.json so forensic reviewers can trace evidence
        provenance without opening the DB.
        """
        self.progress.start_stage("HASH")

        src = self._config.source

        # Hash ONLY databases the user explicitly selected.
        #
        # Previously this stage walked every known WhatsApp .db name
        # (msgstore, wa, status, location, axolotl, chatsettings,
        # media, stickers) and hashed any that happened to exist
        # alongside msgstore.db.  That auto-discovery hashed (and
        # surfaced in the forensic audit trail) databases the analyst
        # never asked for — exactly the "we are hashing more DBs than
        # needed" complaint.
        #
        # The new policy:
        #   * msgstore.db is ALWAYS hashed (it's the primary source).
        #   * Every entry in ``extra_db_paths`` is hashed (each one is
        #     an explicit GUI / CLI selection).
        #   * Nothing else.  Files that merely co-exist in the source
        #     folder are left untouched — no hash, no audit-trail row.
        _candidate_paths: dict[str, Path] = {}

        # Primary
        try:
            _msg = src.msgstore_path
            if _msg and Path(_msg).exists():
                _candidate_paths["msgstore.db"] = Path(_msg)
        except Exception as e:
            logger.warning("hash stage: msgstore.db resolve failed: %s", e)

        # User-selected extras (CLI ``--extra-db`` / GUI checklist).
        # The dict key is the logical filename (``wa.db`` /
        # ``location.db`` / etc.) and the value is the absolute path
        # the user picked.
        if src.extra_db_paths:
            for logical_name, raw_path in src.extra_db_paths.items():
                try:
                    p = Path(raw_path)
                    if p.exists() and logical_name not in _candidate_paths:
                        _candidate_paths[logical_name] = p
                except Exception as e:
                    logger.warning(
                        "hash stage: extra-db resolve failed for %s: %s",
                        logical_name, e,
                    )

        files_to_hash: list[Path] = list(_candidate_paths.values())
        logger.info(
            "hash stage: hashing %d explicitly-selected database(s): %s",
            len(files_to_hash),
            ", ".join(_candidate_paths.keys()) or "(none)",
        )
        hashes = compute_evidence_hashes(
            src.databases_path, only_files=files_to_hash,
        )

        # Store hashes in case_metadata (analysis.db)
        for filename, hash_val in hashes.items():
            self._analysis_db.set_case_metadata(f"source_hash_{filename}", hash_val)
            logger.info("  %s: %s", filename, hash_val[:16] + "...")

        self._analysis_db.set_case_metadata(
            "hash_timestamp", str(int(time.time() * 1000))
        )

        # ---- Record source-path provenance ----
        # Build {logical_name: {path, size_bytes, sha256}} for ONLY
        # the databases the user actually selected — same scope as
        # the hashing pass above, no auto-discovery.
        source_info: dict[str, dict] = {}
        for name, p in _candidate_paths.items():
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            source_info[name] = {
                "path": str(p.resolve()),
                "size_bytes": size,
                "sha256": hashes.get(name),
            }
            self._analysis_db.set_case_metadata(
                f"source_path_{name}", str(p.resolve())
            )
            if size is not None:
                self._analysis_db.set_case_metadata(
                    f"source_size_{name}", str(size)
                )

        # Root "databases/" folder too
        try:
            dbs_root = str(src.databases_path.resolve())
            self._analysis_db.set_case_metadata("source_databases_dir", dbs_root)
        except Exception:
            dbs_root = str(src.databases_path)

        # ---- Sync to external metadata.json (case folder) ----
        try:
            meta_path = self._config.analysis_db_path.parent / "metadata.json"
            if meta_path.exists():
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                existing = {}
            paths_section = existing.setdefault("source_paths", {})
            paths_section["databases_dir"] = dbs_root
            paths_section["databases"] = source_info
            existing["source_paths_updated"] = datetime.now().isoformat()
            meta_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(
                "Recorded %d source-DB paths in metadata.json (%s)",
                len(source_info), meta_path,
            )
        except Exception as meta_err:
            logger.warning("Could not update metadata.json: %s", meta_err)

        self.progress.complete_stage("HASH", len(hashes))

    def _stage_contacts(self) -> None:
        """Stage 3: Build unified contact registry from 5 identity sources."""
        self.progress.start_stage("CONTACTS")

        from app.ingestion.contact_resolver import resolve_contacts
        count, self._owner_name, self._owner_phone_jid = resolve_contacts(
            self._db_manager, self._analysis_conn
        )

        self.progress.complete_stage("CONTACTS", count)

    def _stage_conversations(self) -> None:
        """Stage 4 + 5: Normalize conversations and group memberships."""
        self.progress.start_stage("CONVERSATIONS")

        from app.ingestion.chat_ingester import ingest_conversations
        count = ingest_conversations(self._db_manager, self._analysis_conn)

        self.progress.complete_stage("CONVERSATIONS", count)

        # Members are ingested as part of conversations
        self.progress.skip_stage("MEMBERS", "Handled in CONVERSATIONS stage")

    def _stage_messages(self) -> None:
        """Stage 6: Normalize messages in batches."""
        msgstore = self._db_manager.get_msgstore()
        reader = SourceReader(msgstore)
        total = reader.get_row_count("message")

        self.progress.start_stage("MESSAGES", total)

        from app.ingestion.message_ingester import ingest_messages
        count = ingest_messages(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("MESSAGES", p, t),
        )

        # Post-message: flag bot messages from bot_message_info table
        self._flag_bot_messages()

        self.progress.complete_stage("MESSAGES", count)

    def _stage_albums(self) -> None:
        """Stage 6b: Group multi-photo / multi-video posts (message_album +
        message_association).  Without this, an album of 28 photos lands as
        28 standalone tiles + an empty parent.  Runs after MESSAGES so
        source_msg_id->id mapping is ready, before MEDIA so renderers can
        already see the parent-child link when media metadata flows in."""
        self.progress.start_stage("ALBUMS")

        from app.ingestion.album_ingester import ingest_albums
        count = ingest_albums(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("ALBUMS", p, t),
        )

        self.progress.complete_stage("ALBUMS", count)

    def _stage_media(self) -> None:
        """Stage 7: Normalize media records and resolve file paths."""
        self.progress.start_stage("MEDIA")

        from app.ingestion.media_ingester import ingest_media
        count = ingest_media(
            self._db_manager,
            self._analysis_conn,
            media_root=self._config.source.media_path,
            progress_callback=lambda p, t: self.progress.update_stage("MEDIA", p, t),
        )

        self.progress.complete_stage("MEDIA", count)

    def _stage_media_evidence(self) -> None:
        """Stage 7b: Populate media evidence DB with source identifiers."""
        self.progress.start_stage("MEDIA_EVIDENCE")
        try:
            from app.db.media_evidence_db import MediaEvidenceDB
            case_path = self._config.output_path
            if not case_path:
                self.progress.complete_stage("MEDIA_EVIDENCE", 0)
                return

            evidence_db = MediaEvidenceDB.get()
            evidence_db.init(case_path)

            reader = SourceReader(self._db_manager.get_msgstore())

            # Build JID lookup for human-readable JIDs
            jid_map = {}
            if reader.table_exists("jid"):
                for r in reader.execute_raw("SELECT _id, raw_string FROM jid"):
                    jid_map[r[0]] = r[1]

            # Build analysis DB cross-reference (msgstore._id -> analysis.id)
            msg_map = {}
            for r in self._analysis_conn.fetchall(
                "SELECT source_msg_id, id, conversation_id FROM message"
            ):
                msg_map[r[0]] = (r[1], r[2])

            media_map = {}
            for r in self._analysis_conn.fetchall(
                "SELECT me.id, m.source_msg_id FROM media me JOIN message m ON m.id = me.message_id"
            ):
                media_map[r[1]] = r[0]

            # Bulk insert from msgstore message_media + message
            rows = reader.execute_raw("""
                SELECT mm.message_row_id, mm.chat_row_id, mm.file_path,
                       mm.file_size, mm.media_key, mm.message_url, mm.mime_type,
                       mm.file_hash, mm.enc_file_hash, mm.original_file_hash,
                       mm.media_name, mm.direct_path, mm.media_key_timestamp,
                       m.key_id, m.timestamp, m.from_me, m.sender_jid_row_id,
                       m.message_type
                FROM message_media mm
                JOIN message m ON m._id = mm.message_row_id
            """)

            conn = evidence_db._conn
            count = 0
            batch = []
            for r in rows:
                msg_row_id = r[0]
                chat_row_id = r[1]
                key_id = r[13]
                if not key_id:
                    continue

                chat_jid = jid_map.get(chat_row_id, "")
                sender_jid = jid_map.get(r[16], "")
                analysis_ref = msg_map.get(msg_row_id, (None, None))

                # Parse CDN expiry from URL
                cdn_expiry = None
                url = r[5] or ""
                if "oe=" in url:
                    try:
                        oe_hex = url.split("oe=")[1].split("&")[0]
                        cdn_expiry = int(oe_hex, 16)
                    except (ValueError, IndexError):
                        pass

                batch.append((
                    msg_row_id, chat_row_id, r[16],
                    key_id, chat_jid, sender_jid,
                    r[5], r[11],  # message_url, direct_path
                    r[4],  # media_key (blob)
                    r[12],  # media_key_timestamp
                    r[7], r[8], r[9],  # file_hash, enc_file_hash, original_file_hash
                    r[6], r[3], r[10], r[2],  # mime, size, name, file_path
                    r[15], r[14], r[17],  # from_me, timestamp, msg_type
                    analysis_ref[0], media_map.get(msg_row_id), analysis_ref[1],
                    cdn_expiry,
                    1 if r[2] else 0,  # is_on_disk (has file_path = has file)
                ))

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO media_url_registry "
                        "(msgstore_msg_id, msgstore_chat_id, msgstore_sender_jid_id, "
                        " message_key_id, chat_jid, sender_jid, "
                        " message_url, direct_path, media_key, media_key_timestamp, "
                        " file_hash, enc_file_hash, original_file_hash, "
                        " mime_type, file_size, media_name, file_path_in_wa, "
                        " from_me, msg_timestamp, msg_type, "
                        " analysis_msg_id, analysis_media_id, analysis_conv_id, "
                        " cdn_expiry_ts, is_on_disk) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    conn.commit()
                    count += len(batch)
                    batch.clear()

            if batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO media_url_registry "
                    "(msgstore_msg_id, msgstore_chat_id, msgstore_sender_jid_id, "
                    " message_key_id, chat_jid, sender_jid, "
                    " message_url, direct_path, media_key, media_key_timestamp, "
                    " file_hash, enc_file_hash, original_file_hash, "
                    " mime_type, file_size, media_name, file_path_in_wa, "
                    " from_me, msg_timestamp, msg_type, "
                    " analysis_msg_id, analysis_media_id, analysis_conv_id, "
                    " cdn_expiry_ts, is_on_disk) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                conn.commit()
                count += len(batch)

            logger.info("Media evidence DB populated: %d records", count)
            self.progress.complete_stage("MEDIA_EVIDENCE", count)

        except Exception as e:
            logger.error("Media evidence stage failed: %s", e)
            import traceback
            traceback.print_exc()
            self.progress.complete_stage("MEDIA_EVIDENCE", 0)

    def _stage_orphaned_media(self) -> None:
        """Stage 7c: Discover orphaned media files not linked to any message."""
        self.progress.start_stage("ORPHANED_MEDIA")

        media_path = self._config.source.media_path

        # Auto-detect media root from resolved file paths if not configured
        if not media_path:
            try:
                sample = self._analysis_conn.fetchone(
                    "SELECT resolved_file_path FROM media "
                    "WHERE resolved_file_path IS NOT NULL AND file_exists = 1 LIMIT 1"
                )
                if sample and sample[0]:
                    # Walk up from resolved path to find the "media" or "Media" parent
                    p = Path(sample[0])
                    for parent in p.parents:
                        if parent.name.lower() == "media":
                            media_path = parent
                            logger.info("Auto-detected media root: %s", media_path)
                            break
            except Exception:
                pass

        if not media_path:
            logger.info("No media path configured or detected, skipping orphaned media scan")
            self.progress.complete_stage("ORPHANED_MEDIA", 0)
            return

        try:
            from app.ingestion.orphaned_media_ingester import (
                auto_orphan_rescue_pass,
                ingest_orphaned_media,
            )
            count = ingest_orphaned_media(
                self._analysis_conn,
                Path(media_path) if not isinstance(media_path, Path) else media_path,
                progress_callback=lambda p, t: self.progress.update_stage("ORPHANED_MEDIA", p, t),
            )
            self.progress.complete_stage("ORPHANED_MEDIA", count)
            # Automatic orphan→media rescue: hashes the size-pre-filtered
            # subset of orphans whose file_size matches a missing-media
            # row, then writes recovery_method='orphan_recovered' on
            # any media row whose SHA-256 matches an orphan.  This
            # closes the previously-manual loop where the user had to
            # click "Run Hash Match" on the Orphaned Media page to
            # rescue missing chat-message files from disk-only
            # orphaned copies.  Now happens at ingestion time so a
            # freshly-loaded case is forensically complete out of
            # the box.
            try:
                matched, rescued = auto_orphan_rescue_pass(
                    self._analysis_conn,
                    progress_callback=lambda p, t: self.progress.update_stage(
                        "ORPHANED_MEDIA", p, t),
                )
                if rescued:
                    logger.info(
                        "Auto-orphan-rescue: %d missing message files now "
                        "served from orphaned-media siblings (recovery_method"
                        "='orphan_recovered'); %d orphans linked back to "
                        "their owning messages",
                        rescued, matched,
                    )
            except Exception as e:
                logger.warning("auto_orphan_rescue_pass failed: %s", e)
        except Exception as e:
            logger.error("Orphaned media scan failed: %s", e)
            import traceback
            traceback.print_exc()
            self.progress.complete_stage("ORPHANED_MEDIA", 0)

    def _stage_receipts(self) -> None:
        """Stage 8: Ingest user delivery / read / played receipts."""
        self.progress.start_stage("RECEIPTS")

        from app.ingestion.receipt_ingester import ingest_receipts
        count = ingest_receipts(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("RECEIPTS", p, t),
        )

        self.progress.complete_stage("RECEIPTS", count)

    def _stage_reactions(self) -> None:
        """Stage 9: Ingest emoji reactions."""
        self.progress.start_stage("REACTIONS")

        from app.ingestion.reaction_ingester import ingest_reactions
        count = ingest_reactions(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("REACTIONS", p, t),
        )

        self.progress.complete_stage("REACTIONS", count)

    def _stage_system_events(self) -> None:
        """Stage 10: Parse system events (60+ action types)."""
        self.progress.start_stage("SYSTEM_EVENTS")

        from app.ingestion.system_event_ingester import ingest_system_events
        count = ingest_system_events(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("SYSTEM_EVENTS", p, t),
        )

        self.progress.complete_stage("SYSTEM_EVENTS", count)

    def _stage_group_metadata(self) -> None:
        """Stage 10b: Extract group metadata change timeline (subject/description/icon/settings)."""
        self.progress.start_stage("GROUP_METADATA")

        from app.ingestion.group_metadata_ingester import ingest_group_metadata_changes
        count = ingest_group_metadata_changes(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("GROUP_METADATA", p, t),
        )

        self.progress.complete_stage("GROUP_METADATA", count)

    def _stage_calls(self) -> None:
        """Stage 11: Normalize call records."""
        self.progress.start_stage("CALLS")

        from app.ingestion.call_ingester import ingest_calls
        count = ingest_calls(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("CALLS", p, t),
        )

        self.progress.complete_stage("CALLS", count)

    def _stage_scheduled_events(self) -> None:
        """Ingest scheduled calls and calendar events from message_event table."""
        self.progress.start_stage("SCHEDULED_EVENTS") if hasattr(self.progress, 'start_stage') else None

        from app.ingestion.event_ingester import ingest_scheduled_events
        count = ingest_scheduled_events(
            self._db_manager,
            self._analysis_conn,
        )

        if hasattr(self.progress, 'complete_stage'):
            self.progress.complete_stage("SCHEDULED_EVENTS", count)
        logger.info("Scheduled events stage complete: %d events ingested", count)

    def _stage_polls(self) -> None:
        """Stage 12: Ingest polls and votes."""
        self.progress.start_stage("POLLS")

        from app.ingestion.poll_ingester import ingest_polls
        count = ingest_polls(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("POLLS", p, t),
        )

        self.progress.complete_stage("POLLS", count)

    def _stage_vcards(self) -> None:
        """Stage 12b: Ingest vCard contact data from shared contacts."""
        self.progress.start_stage("VCARDS")

        from app.ingestion.vcard_ingester import ingest_vcards
        count = ingest_vcards(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("VCARDS", p, t),
        )

        self.progress.complete_stage("VCARDS", count)

    def _stage_pins(self) -> None:
        """Stage 12c: Ingest pinned messages from message_add_on."""
        self.progress.start_stage("PINS")

        from app.ingestion.pin_ingester import ingest_pins
        count = ingest_pins(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("PINS", p, t),
        )

        self.progress.complete_stage("PINS", count)

    def _stage_edits(self) -> None:
        """Stage 13: Ingest edit history records."""
        self.progress.start_stage("EDITS")

        from app.ingestion.edit_ingester import ingest_edits
        count = ingest_edits(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("EDITS", p, t),
        )

        self.progress.complete_stage("EDITS", count)

    def _stage_comments(self) -> None:
        """Stage 14: Ingest channel comment threads."""
        self.progress.start_stage("COMMENTS")

        from app.ingestion.comment_ingester import ingest_comments
        count = ingest_comments(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("COMMENTS", p, t),
        )

        self.progress.complete_stage("COMMENTS", count)

    def _stage_revokes(self) -> None:
        """Stage 14: Process revoked messages + ghost recovery."""
        self.progress.start_stage("REVOKES")

        from app.ingestion.revoke_ingester import process_revokes
        count = process_revokes(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("REVOKES", p, t),
        )

        self.progress.complete_stage("REVOKES", count)

    def _stage_links(self) -> None:
        """Stage 15: Process URL link records."""
        self.progress.start_stage("LINKS")

        from app.ingestion.link_ingester import ingest_links
        count = ingest_links(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("LINKS", p, t),
        )

        self.progress.complete_stage("LINKS", count)

    def _stage_mentions(self) -> None:
        """Ingest @mentions (replaces the NEWSLETTERS stage slot)."""
        self.progress.start_stage("NEWSLETTERS")  # Reuse the stage slot

        from app.ingestion.mention_ingester import ingest_mentions
        count = ingest_mentions(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("NEWSLETTERS", p, t),
        )

        # Also ingest locations
        from app.ingestion.location_ingester import ingest_locations
        loc_count = ingest_locations(
            self._db_manager,
            self._analysis_conn,
        )

        self.progress.complete_stage("NEWSLETTERS", count + loc_count)

    def _ingest_location_db(self) -> None:
        """Ingest live location route points from location.db (optional)."""
        from app.ingestion.location_db_ingester import ingest_location_db
        count = ingest_location_db(
            self._db_manager,
            self._analysis_conn,
        )
        if count > 0:
            logger.info("location.db: %d route points / sharers ingested", count)

    def _flag_bot_messages(self) -> None:
        """Flag bot messages from bot_message_info table."""
        msgstore = self._db_manager.get_msgstore()
        reader = SourceReader(msgstore)

        if not reader.table_exists("bot_message_info"):
            logger.info("bot_message_info table not found - skipping bot flagging")
            return

        # Build message ID lookup
        msg_rows = self._analysis_conn.fetchall(
            "SELECT source_msg_id, id FROM message"
        )
        msg_map: dict[int, int] = {row[0]: row[1] for row in msg_rows}

        bot_rows = reader.execute_raw("SELECT message_row_id FROM bot_message_info")

        # Collect all bot message IDs first, then batch-update
        bot_ids = []
        for (bot_msg_row,) in bot_rows:
            msg_id = msg_map.get(bot_msg_row)
            if msg_id:
                bot_ids.append(msg_id)

        bot_count = len(bot_ids)
        if bot_ids:
            self._analysis_conn.begin_transaction()
            try:
                cursor = self._analysis_conn.get_cursor()
                # Batch update in chunks of 500 using IN clause
                for i in range(0, len(bot_ids), 500):
                    chunk = bot_ids[i:i + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cursor.execute(
                        f"UPDATE message SET is_bot_message = 1 WHERE id IN ({placeholders})",
                        chunk,
                    )
                self._analysis_conn.commit()
            except Exception:
                self._analysis_conn.rollback()
                raise

        logger.info("Flagged %d bot messages from bot_message_info", bot_count)

    def _stage_status(self) -> None:
        """Ingest status posts from status@broadcast chat and status.db."""
        self.progress.start_stage("STATUS")

        from app.ingestion.status_ingester import ingest_status_posts
        count = ingest_status_posts(
            self._db_manager,
            self._analysis_conn,
            progress_callback=lambda p, t: self.progress.update_stage("STATUS", p, t),
        )

        self.progress.complete_stage("STATUS", count)

    def _stage_companion(self) -> None:
        """Stage 17: Ingest companion database data."""
        self.progress.start_stage("COMPANION")

        count = 0
        # Chatsettings.db mute data is already handled in chat_ingester
        # Stickers.db could provide sticker pack metadata
        # For now, mark as completed with a note
        logger.info("Companion DB ingestion: mute data handled in CONVERSATIONS stage")

        self.progress.complete_stage("COMPANION", count)

    def _stage_avatars(self) -> None:
        """Ingest profile pictures from WhatsApp's Avatars directory."""
        self.progress.start_stage("AVATARS")

        from app.ingestion.avatar_ingester import ingest_avatars
        count = ingest_avatars(
            self._db_manager,
            self._analysis_conn,
            avatars_path=self._config.source.avatars_path,
        )

        self.progress.complete_stage("AVATARS", count)

    def _stage_fts_index(self) -> None:
        """Stage 18: Build the FTS5 full-text search index and install sync triggers.

        Performed in three steps:

        1. ``rebuild`` -- populate ``message_fts`` from every row in
           ``message`` in a single batch pass.  Much faster than firing
           a per-row tokenising trigger during the MESSAGES stage.
        2. ``optimize`` -- merge the FTS index segments for faster reads.
        3. Install FTS sync triggers so subsequent GUI-side edits /
           tag writes keep the index in sync incrementally.
        """
        self.progress.start_stage("FTS_INDEX")

        # 1. Batch build.  FTS triggers are NOT live yet (installed in step 3),
        #    so no per-row overhead was paid during MESSAGES / EDITS / etc.
        logger.info("Building FTS5 index via rebuild...")
        self._analysis_conn.execute(
            "INSERT INTO message_fts(message_fts) VALUES('rebuild')"
        )
        fts_count = self._analysis_conn.fetchone(
            "SELECT COUNT(*) FROM message_fts"
        )
        count = fts_count[0] if fts_count else 0
        logger.info("FTS5 index built: %d entries", count)

        # 2. Optimize segments.
        self._analysis_conn.execute(
            "INSERT INTO message_fts(message_fts) VALUES('optimize')"
        )
        logger.info("FTS5 index optimized")

        # 3. Attach sync triggers for future incremental updates from the GUI.
        from app.db.schema import create_fts_triggers
        create_fts_triggers(self._analysis_conn.raw_connection)

        self.progress.complete_stage("FTS_INDEX", count)

    def _stage_precompute(self) -> None:
        """Stage 19: Pre-compute analytics tables."""
        self.progress.start_stage("PRECOMPUTE")

        count = 0

        # Daily activity stats
        logger.info("Computing daily activity statistics...")
        self._analysis_conn.execute("""
            INSERT OR IGNORE INTO stats_daily_activity (
                conversation_id, date_str,
                total_messages, sent_messages, received_messages,
                text_count, media_count, sticker_count,
                reaction_count, deleted_count, edited_count
            )
            SELECT
                m.conversation_id,
                strftime('%Y-%m-%d', m.timestamp / 1000, 'unixepoch') AS date_str,
                COUNT(*) AS total_messages,
                SUM(CASE WHEN m.from_me = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.from_me = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type IN (1,2,3,9,11,13) THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 20 THEN 1 ELSE 0 END),
                0,
                SUM(CASE WHEN m.is_revoked = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.is_edited = 1 THEN 1 ELSE 0 END)
            FROM message m
            WHERE m.message_type != 7
            GROUP BY m.conversation_id, date_str
        """)

        daily_row = self._analysis_conn.fetchone(
            "SELECT COUNT(*) FROM stats_daily_activity"
        )
        daily_count = daily_row[0] if daily_row else 0
        logger.info("Computed %d daily activity records", daily_count)
        count += daily_count

        # Contact activity stats
        logger.info("Computing contact activity statistics...")
        self._analysis_conn.execute("""
            INSERT OR IGNORE INTO stats_contact_activity (
                contact_id, conversation_id,
                total_messages, total_text, total_media,
                total_images, total_videos, total_audio,
                total_documents, total_stickers, total_gifs,
                total_forwards, total_edits, total_deletes,
                first_message_ts, last_message_ts
            )
            SELECT
                m.sender_id,
                m.conversation_id,
                COUNT(*),
                SUM(CASE WHEN m.message_type = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type IN (1,2,3,9,11,13) THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 3 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 2 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 9 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type = 20 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.message_type IN (11,13) THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.is_forwarded = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.is_edited = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.is_revoked = 1 THEN 1 ELSE 0 END),
                MIN(m.timestamp),
                MAX(m.timestamp)
            FROM message m
            WHERE m.sender_id IS NOT NULL
            AND m.message_type != 7
            GROUP BY m.sender_id, m.conversation_id
        """)

        contact_row = self._analysis_conn.fetchone(
            "SELECT COUNT(*) FROM stats_contact_activity"
        )
        contact_count = contact_row[0] if contact_row else 0
        logger.info("Computed %d contact activity records", contact_count)
        count += contact_count

        # Hourly heatmap
        logger.info("Computing hourly activity heatmap...")
        self._analysis_conn.execute("""
            INSERT OR IGNORE INTO stats_hourly_heatmap (
                contact_id, conversation_id, day_of_week, hour_of_day, message_count
            )
            SELECT
                m.sender_id,
                m.conversation_id,
                CAST(strftime('%w', m.timestamp / 1000, 'unixepoch') AS INTEGER),
                CAST(strftime('%H', m.timestamp / 1000, 'unixepoch') AS INTEGER),
                COUNT(*)
            FROM message m
            WHERE m.message_type != 7
            AND m.sender_id IS NOT NULL
            GROUP BY m.sender_id, m.conversation_id,
                     strftime('%w', m.timestamp / 1000, 'unixepoch'),
                     strftime('%H', m.timestamp / 1000, 'unixepoch')
        """)

        heatmap_row = self._analysis_conn.fetchone(
            "SELECT COUNT(*) FROM stats_hourly_heatmap"
        )
        heatmap_count = heatmap_row[0] if heatmap_row else 0
        logger.info("Computed %d hourly heatmap records", heatmap_count)
        count += heatmap_count

        # Platform estimates for contacts
        logger.info("Computing contact platform estimates...")

        # Device owner = android (this is an Android extraction)
        self._analysis_conn.execute(
            "UPDATE contact SET platform_estimate = 'android', platform_confidence = 1.0 "
            "WHERE phone_jid = ("
            "  SELECT value FROM case_metadata WHERE key = 'device_owner_jid'"
            ")"
        )

        # Determine platform from key_id-classified platform_label in message_device.
        # NOTE: device_agent is the JID protocol version (0=phone-number, 1=LID),
        # NOT a device indicator. Only device_number > 0 indicates a companion device.
        # The platform_label was set during message ingestion via keyid_classifier.

        # Step 1: Compute dominant platform per contact using RECENT data (last 90 days)
        # If no recent data, fall back to all-time. This handles device switches correctly.
        import time as _time
        _recent_cutoff = int((_time.time() - 90 * 86400) * 1000)
        self._analysis_conn.execute(f"""
            CREATE TEMP TABLE IF NOT EXISTS _tmp_platform AS
            SELECT sender_id, platform_label, cnt,
                   ROUND(CAST(cnt AS REAL) / SUM(cnt) OVER (PARTITION BY sender_id), 2) AS conf
            FROM (
                -- Prefer recent (last 90 days) if available, else all-time
                SELECT m.sender_id, md.platform_label, COUNT(*) AS cnt
                FROM message_device md
                JOIN message m ON m.id = md.message_id
                WHERE md.device_number = 0
                  AND md.platform_label IN ('android', 'iphone')
                  AND m.sender_id IS NOT NULL
                  AND m.message_type NOT IN (7, 64)
                  AND m.timestamp >= {_recent_cutoff}
                GROUP BY m.sender_id, md.platform_label
                UNION ALL
                -- All-time for contacts with no recent messages
                SELECT m.sender_id, md.platform_label, COUNT(*) AS cnt
                FROM message_device md
                JOIN message m ON m.id = md.message_id
                WHERE md.device_number = 0
                  AND md.platform_label IN ('android', 'iphone')
                  AND m.sender_id IS NOT NULL
                  AND m.message_type NOT IN (7, 64)
                  AND m.sender_id NOT IN (
                      SELECT DISTINCT m2.sender_id FROM message m2
                      JOIN message_device md2 ON md2.message_id = m2.id
                      WHERE md2.device_number = 0
                        AND md2.platform_label IN ('android', 'iphone')
                        AND m2.timestamp >= {_recent_cutoff}
                        AND m2.sender_id IS NOT NULL
                  )
                GROUP BY m.sender_id, md.platform_label
            )
        """)
        # Step 2: Pick the dominant platform per contact
        self._analysis_conn.execute("""
            UPDATE contact SET
                platform_estimate = tp.platform_label,
                platform_confidence = tp.conf
            FROM (
                SELECT sender_id, platform_label, conf
                FROM _tmp_platform
                WHERE (sender_id, cnt) IN (
                    SELECT sender_id, MAX(cnt) FROM _tmp_platform GROUP BY sender_id
                )
            ) tp
            WHERE contact.id = tp.sender_id
              AND contact.platform_estimate IS NULL
        """)
        self._analysis_conn.execute("DROP TABLE IF EXISTS _tmp_platform")

        # Contacts with companion device usage (device_number > 0) get multi_device
        self._analysis_conn.execute(
            "UPDATE contact SET platform_estimate = "
            "  COALESCE(platform_estimate, 'multi_device'), "
            "  platform_confidence = COALESCE(platform_confidence, 0.5) "
            "WHERE id IN ("
            "  SELECT DISTINCT m.sender_id FROM message m "
            "  JOIN message_device md ON md.message_id = m.id "
            "  WHERE md.device_number > 0 AND m.sender_id IS NOT NULL"
            ") AND platform_estimate IS NULL"
        )

        # Remaining contacts with message_device data but no platform classified
        self._analysis_conn.execute(
            "UPDATE contact SET platform_estimate = 'phone', platform_confidence = 0.3 "
            "WHERE id IN ("
            "  SELECT DISTINCT m.sender_id FROM message m "
            "  JOIN message_device md ON md.message_id = m.id "
            "  WHERE m.sender_id IS NOT NULL"
            ") AND platform_estimate IS NULL"
        )

        platform_row = self._analysis_conn.fetchone(
            "SELECT COUNT(*) FROM contact WHERE platform_estimate IS NOT NULL"
        )
        platform_count = platform_row[0] if platform_row else 0
        logger.info("Computed platform estimates for %d contacts", platform_count)

        # Linked / companion device counts from message_device (historical)
        # This counts unique companion device numbers ever seen in messages per
        # contact, which is more forensically useful than current sessions only.
        logger.info("Computing linked device counts from message_device (historical)...")
        try:
            self._analysis_conn.execute("""
                UPDATE contact SET linked_device_count = COALESCE((
                    SELECT COUNT(DISTINCT md.device_number)
                    FROM message_device md
                    JOIN message m ON m.id = md.message_id
                    WHERE m.sender_id = contact.id AND md.device_number > 0
                ), 0)
            """)
            dev_count_row = self._analysis_conn.fetchone(
                "SELECT COUNT(*) FROM contact WHERE linked_device_count > 0"
            )
            logger.info(
                "Linked device counts: %d contacts with companion device history",
                dev_count_row[0] if dev_count_row else 0,
            )
        except Exception as e:
            logger.warning("Could not compute linked device counts: %s", e)

        # Pre-compute contact aggregate counts and flags
        logger.info("Computing contact aggregate counts...")
        self._analysis_conn.execute("""
            UPDATE contact SET message_count = COALESCE((
                SELECT SUM(sa.total_messages)
                FROM stats_contact_activity sa
                WHERE sa.contact_id = contact.id
            ), 0)
        """)
        self._analysis_conn.execute("""
            UPDATE contact SET conversation_count = COALESCE((
                SELECT COUNT(DISTINCT sa.conversation_id)
                FROM stats_contact_activity sa
                WHERE sa.contact_id = contact.id
            ), 0)
        """)
        # Personal vs group message counts
        self._analysis_conn.execute("""
            UPDATE contact SET personal_msg_count = COALESCE((
                SELECT SUM(sa.total_messages)
                FROM stats_contact_activity sa
                JOIN conversation cv ON cv.id = sa.conversation_id
                WHERE sa.contact_id = contact.id AND cv.chat_type = 'personal'
            ), 0)
        """)
        self._analysis_conn.execute("""
            UPDATE contact SET group_msg_count = COALESCE((
                SELECT SUM(sa.total_messages)
                FROM stats_contact_activity sa
                JOIN conversation cv ON cv.id = sa.conversation_id
                WHERE sa.contact_id = contact.id AND cv.chat_type != 'personal'
            ), 0)
        """)
        # is_saved flag (contact saved in phone address book)
        self._analysis_conn.execute("""
            UPDATE contact SET is_saved = CASE
                WHEN display_name IS NOT NULL AND display_name != '' THEN 1
                ELSE 0
            END
        """)
        logger.info("Contact aggregate counts and flags populated")

        # --- Pre-compute rendered_sender on message table ---
        # Build contact name dict once — fits comfortably in
        # memory even on large cases.
        from shared.system_event_formatter import fmt_phone, build_system_text

        contacts = self._analysis_conn.fetchall(
            "SELECT id, is_saved, display_name, resolved_name, wa_name, "
            "phone_number, phone_jid, lid_jid FROM contact"
        )
        name_map: dict[int, str] = {}
        for c in contacts:
            cid, is_saved, dn, rn, wn, pn, pj, lj = c
            dn = (dn or "").strip()
            rn = (rn or "").strip()
            wn = (wn or "").strip()
            pn = (pn or "").strip()
            pj = (pj or "").strip()
            lj = (lj or "").strip()
            pn_fmt = fmt_phone(pn) if pn else ""
            if is_saved and dn:
                name_map[cid] = f"{dn} ({pn_fmt})" if pn else dn
            elif is_saved and rn:
                rn_digits = rn.lstrip("+").replace(" ", "")
                if pn and rn_digits != pn and rn != f"+{pn}":
                    name_map[cid] = f"{rn} ({pn_fmt})" if pn else rn
                elif not pn:
                    name_map[cid] = rn
                else:
                    name_map[cid] = pn_fmt if pn else "Unknown"
            elif wn:
                name_map[cid] = f"~{wn} ({pn_fmt})" if pn else f"~{wn}"
            elif pn:
                name_map[cid] = pn_fmt
            elif pj:
                name_map[cid] = "+" + pj.replace("@s.whatsapp.net", "")
            elif lj:
                name_map[cid] = f"LID:{lj[:12]}..."
            else:
                name_map[cid] = "Unknown"

        # Owner label
        owner_phone = ""
        owner_name = ""
        owner_cid = 0
        try:
            row = self._analysis_conn.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'"
            )
            if row:
                owner_phone = row[0]
            row = self._analysis_conn.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_name'"
            )
            if row:
                owner_name = row[0]
            if owner_phone:
                row = self._analysis_conn.fetchone(
                    "SELECT id FROM contact WHERE phone_number = ? OR phone_jid = ?",
                    (owner_phone, f"{owner_phone}@s.whatsapp.net"),
                )
                if row:
                    owner_cid = row[0]
        except Exception as e:
            logger.warning("Could not load owner info for rendered_sender: %s", e)

        owner_fmt = fmt_phone(owner_phone) if owner_phone else ""
        owner_label = (
            f"You (Owner: {owner_name}, {owner_fmt})" if owner_name and owner_fmt
            else f"You ({owner_fmt})" if owner_fmt
            else "You"
        )

        # Batch update rendered_sender (50K per batch)
        logger.info("Pre-computing rendered_sender for all messages...")
        BATCH = 50000
        offset = 0
        sender_count = 0
        while True:
            rows = self._analysis_conn.fetchall(
                "SELECT id, sender_id, from_me, is_bot_message "
                "FROM message ORDER BY id LIMIT ? OFFSET ?",
                (BATCH, offset),
            )
            if not rows:
                break
            updates = []
            for mid, sid, from_me, is_bot in rows:
                if from_me:
                    name = owner_label
                elif is_bot and sid is None:
                    name = "Meta AI"
                elif sid and sid in name_map:
                    name = name_map[sid]
                elif sid:
                    name = "Unknown"
                else:
                    name = "Unknown"
                updates.append((name, mid))
            self._analysis_conn.executemany(
                "UPDATE message SET rendered_sender = ? WHERE id = ?",
                updates,
            )
            sender_count += len(rows)
            offset += BATCH
        logger.info("rendered_sender populated for %d messages", sender_count)

        # --- Pre-compute rendered_system_text for system events ---
        logger.info("Pre-computing rendered_system_text for system events...")
        se_rows = self._analysis_conn.fetchall("""
            SELECT m.id, m.from_me, COALESCE(m.text_content, ''),
                   se.event_label, se.event_data, se.actor_id, se.target_id,
                   se.community_name,
                   conv.display_name, conv.chat_type,
                   m.type_label, m.is_bot_message, m.sender_id,
                   m.ephemeral_duration, m.revoked_by_admin_id,
                   se.id AS se_id
            FROM message m
            JOIN system_event se ON se.message_id = m.id
            JOIN conversation conv ON conv.id = m.conversation_id
            WHERE m.message_type = 7
        """)

        # Load number_change data — resolve JID row IDs to actual phone strings
        # IMPORTANT: Use jid_to_contact.jid_raw_string (the JID at ingestion time),
        # NOT contact.phone_jid (which may have been updated if the person
        # changed number and someone else took the old number).
        nc_map: dict[int, tuple[str, str]] = {}
        try:
            nc_rows = self._analysis_conn.fetchall("""
                SELECT nc.system_event_id,
                       COALESCE(j_old.jid_raw_string, '') AS old_jid,
                       COALESCE(j_new.jid_raw_string, '') AS new_jid
                FROM number_change nc
                LEFT JOIN jid_to_contact j_old ON j_old.jid_row_id = nc.old_jid_row_id
                LEFT JOIN jid_to_contact j_new ON j_new.jid_row_id = nc.new_jid_row_id
            """)
            for nr in nc_rows:
                old_j = (nr[1] or "").replace("@s.whatsapp.net", "")
                new_j = (nr[2] or "").replace("@s.whatsapp.net", "")
                nc_map[nr[0]] = (old_j, new_j)
        except Exception:
            pass

        # Load revoked_by_admin names
        admin_name_map: dict[int, str] = {}
        try:
            admin_ids = {r[14] for r in se_rows if r[14]}
            if admin_ids:
                for aid in admin_ids:
                    admin_name_map[aid] = name_map.get(aid, "Admin")
        except Exception:
            pass

        se_updates = []
        for row in se_rows:
            mid = row[0]
            actor_id = row[5]
            target_id = row[6]
            se_id = row[15]
            msg = {
                "from_me": bool(row[1]),
                "text_content": row[2],
                "display_text": row[2],
                "message_type": 7,
                "system_event_label": row[3] or "",
                "system_event_data": row[4] or "",
                "se_actor_id": actor_id,
                "se_target_id": target_id,
                "system_event_actor": name_map.get(actor_id, "") if actor_id else "",
                "system_event_target": name_map.get(target_id, "") if target_id else "",
                "type_label": row[10] or "",
                "is_bot_message": bool(row[11]),
                "sender_id": row[12],
                "sender_name": name_map.get(row[12], "") if row[12] else "",
                "ephemeral_duration": row[13],
                "community_name": row[7] or "",
                "revoked_by_admin_id": row[14],
                "revoked_by_admin_name": admin_name_map.get(row[14], "") if row[14] else "",
            }
            # Number change data
            if se_id and se_id in nc_map:
                msg["nc_old_phone"] = nc_map[se_id][0]
                msg["nc_new_phone"] = nc_map[se_id][1]

            try:
                text = build_system_text(
                    msg,
                    owner_phone=owner_phone,
                    owner_name=owner_name,
                    owner_contact_id=owner_cid,
                    conv_name=row[8] or "",
                    chat_type=row[9] or "",
                )
            except Exception:
                text = msg.get("display_text", "")
            se_updates.append((text, mid))

        if se_updates:
            self._analysis_conn.executemany(
                "UPDATE message SET rendered_system_text = ? WHERE id = ?",
                se_updates,
            )
        logger.info("rendered_system_text populated for %d system events", len(se_updates))

        self.progress.complete_stage("PRECOMPUTE", count)

    def _stage_finalize(self) -> None:
        """Stage 21: Verify integrity and write case metadata."""
        self.progress.start_stage("FINALIZE")

        # Write case metadata
        self._analysis_db.set_case_metadata("ingestion_complete", "true")
        self._analysis_db.set_case_metadata(
            "ingestion_timestamp", str(int(time.time() * 1000))
        )
        self._analysis_db.set_case_metadata("tool_version", "0.1.0")

        # Device owner metadata
        if self._owner_name:
            self._analysis_db.set_case_metadata("device_owner_name", self._owner_name)
        if self._owner_phone_jid:
            phone = self._owner_phone_jid.split("@")[0]
            self._analysis_db.set_case_metadata("device_owner_phone", phone)
            self._analysis_db.set_case_metadata("device_owner_jid", self._owner_phone_jid)

        if self._config.case.case_id:
            self._analysis_db.set_case_metadata("case_id", self._config.case.case_id)
        if self._config.case.examiner:
            self._analysis_db.set_case_metadata("examiner", self._config.case.examiner)

        # Verify key table counts
        stats = self._analysis_db.get_stats()
        logger.info("=== Final Analysis Database Statistics ===")
        for table, count in sorted(stats.items()):
            if count > 0:
                logger.info("  %-30s %10d rows", table, count)

        # Compute total elapsed
        elapsed = self.progress.pipeline_elapsed_seconds
        logger.info("=== Pipeline completed in %.1f seconds (%.1f minutes) ===", elapsed, elapsed / 60)

        self.progress.complete_stage("FINALIZE", len(stats))


def run_ingestion(config: AppConfig, force_recreate: bool = True) -> PipelineProgress:
    """Top-level function to run the complete ingestion pipeline.

    Args:
        config: Application configuration.
        force_recreate: Delete and recreate analysis.db if it exists.

    Returns:
        PipelineProgress with the final state of all stages.
    """
    orchestrator = IngestionOrchestrator(config)
    orchestrator.run(force_recreate=force_recreate)
    return orchestrator.progress
