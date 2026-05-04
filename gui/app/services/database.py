"""
Read-only SQLite connection manager for analysis.db.

Uses ``mode=ro`` URI access with aggressive PRAGMAs tuned for
read performance on large forensic databases.  Thread-safe for
concurrent reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


class Database:
    """Singleton read-only connection to analysis.db.

    The primary connection is immutable for maximum read performance.
    A separate writable connection is opened on-demand for updates
    (e.g. media re-mapping after download).
    """

    _instance: Optional[Database] = None
    _conn: Optional[sqlite3.Connection] = None
    _write_conn: Optional[sqlite3.Connection] = None
    _db_path: Optional[Path] = None

    @classmethod
    def init(cls, db_path: str | Path) -> Database:
        """Initialize the database connection."""
        inst = cls()
        inst._db_path = Path(db_path)
        if not inst._db_path.exists():
            raise FileNotFoundError(f"Database not found: {db_path}")
        inst._run_migrations()
        inst._connect()
        cls._instance = inst
        return inst

    @classmethod
    def get(cls) -> Database:
        """Get the singleton instance."""
        if cls._instance is None:
            raise RuntimeError("Database not initialized. Call Database.init() first.")
        return cls._instance

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def size_mb(self) -> float:
        return self._db_path.stat().st_size / (1024 * 1024)

    def _run_migrations(self) -> None:
        """Add missing columns to older databases for compatibility."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(contact)").fetchall()}
            changed = False

            # Add missing columns
            new_cols = {
                "message_count": "INTEGER DEFAULT 0",
                "conversation_count": "INTEGER DEFAULT 0",
                "personal_msg_count": "INTEGER DEFAULT 0",
                "group_msg_count": "INTEGER DEFAULT 0",
                "is_saved": "BOOLEAN DEFAULT 0",
                "linked_device_count": "INTEGER DEFAULT 0",
            }
            for col, typ in new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE contact ADD COLUMN {col} {typ}")
                    changed = True

            if changed:
                # Populate aggregate counts
                conn.execute("""
                    UPDATE contact SET message_count = COALESCE((
                        SELECT COUNT(*) FROM message
                        WHERE sender_id = contact.id AND message_type != 7
                    ), 0)
                """)
                conn.execute("""
                    UPDATE contact SET conversation_count = COALESCE((
                        SELECT COUNT(DISTINCT conversation_id)
                        FROM group_member WHERE contact_id = contact.id
                    ), 0)
                """)
                conn.execute("""
                    UPDATE contact SET personal_msg_count = COALESCE((
                        SELECT COUNT(*) FROM message m
                        JOIN conversation cv ON cv.id = m.conversation_id
                        WHERE m.sender_id = contact.id AND m.message_type != 7
                        AND cv.chat_type = 'personal'
                    ), 0)
                """)
                conn.execute("""
                    UPDATE contact SET group_msg_count = COALESCE((
                        SELECT COUNT(*) FROM message m
                        JOIN conversation cv ON cv.id = m.conversation_id
                        WHERE m.sender_id = contact.id AND m.message_type != 7
                        AND cv.chat_type != 'personal'
                    ), 0)
                """)
                conn.execute("""
                    UPDATE contact SET is_saved = CASE
                        WHEN display_name IS NOT NULL AND display_name != '' THEN 1
                        ELSE 0
                    END
                """)
                conn.commit()

            # Backfill media_name from file_path where missing
            try:
                rows = conn.execute(
                    "SELECT id, file_path FROM media "
                    "WHERE (media_name IS NULL OR media_name = '') "
                    "AND file_path IS NOT NULL AND file_path != ''"
                ).fetchall()
                for row in rows:
                    mid, fpath = row[0], row[1]
                    # Normalise separators and extract filename after last slash
                    normalised = fpath.replace("\\", "/")
                    last_slash = normalised.rfind("/")
                    fname = normalised[last_slash + 1:] if last_slash >= 0 else normalised
                    if fname:
                        conn.execute(
                            "UPDATE media SET media_name = ? WHERE id = ?",
                            (fname, mid),
                        )
                conn.commit()
            except Exception:
                pass  # Non-critical

            # Message table migrations (origin + origination_flags for multi-device)
            msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
            msg_new = {
                "origin": "INTEGER DEFAULT 0",
                "origination_flags": "INTEGER DEFAULT 0",
            }
            for col, typ in msg_new.items():
                if col not in msg_cols:
                    conn.execute(f"ALTER TABLE message ADD COLUMN {col} {typ}")
            conn.commit()

            # Media table migrations (recovery tracking + HD twin pairs)
            media_cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
            media_new = {
                "recovery_method": "TEXT",
                "recovery_timestamp": "INTEGER",
                "cdn_expiry_ts": "INTEGER",
                # WhatsApp dual-quality send tracking — see
                # schema.py for the full state machine.
                "is_hd_twin": "INTEGER DEFAULT 0",
                "hd_twin_msg_id": "INTEGER",
                # Motion Photo pair (Android Motion Photos / Samsung Single
                # Take): parent = still image, child = 1-2 second video.
                # Linked via msgstore.message_association.association_type = 11.
                "motion_video_msg_id": "INTEGER",
                # Generic back-pointer from association-child rows to
                # their parent's analysis msg_id (used for channel-poll
                # image options + future association lookups).
                "assoc_parent_msg_id": "INTEGER",
                # Tag identifying which association_type this child is
                # ('hd', 'motion', 'status', 'poll', NULL).  Lets the
                # chat-list WHERE clause cheaply distinguish HD pair
                # members (render as separate bubbles) from scaffolding
                # children (hide).  See schema.py for the full state
                # machine.
                "assoc_kind": "TEXT",
                "was_transferred": "INTEGER",
            }
            for col, typ in media_new.items():
                if col not in media_cols:
                    try:
                        conn.execute(f"ALTER TABLE media ADD COLUMN {col} {typ}")
                    except Exception:
                        pass  # Column may already exist
            conn.commit()

            # Backfill ``assoc_kind`` for existing databases ingested
            # before this column existed.  Without this the chat-list
            # WHERE clause would treat every is_hd_twin=1 row as 'hide'
            # (the safe fallback) and the HD bubble wouldn't show up
            # alongside its SD parent until re-ingestion.  Best-effort
            # — uses the parent-side back-pointers populated by
            # ``_link_hd_twins_pass`` to infer the kind:
            #   * HD pair child   — the row appears in some other
            #                       row's ``hd_twin_msg_id`` pointer.
            #   * Motion child    — the row appears in some other
            #                       row's ``motion_video_msg_id`` pointer.
            #   * Other (status/poll/unknown) — left NULL; chat-list
            #     hides them just like before.  Re-ingest rebuilds
            #     these accurately.
            try:
                conn.execute("""
                    UPDATE media SET assoc_kind = 'hd'
                     WHERE is_hd_twin = 1
                       AND assoc_kind IS NULL
                       AND message_id IN (
                           SELECT hd_twin_msg_id FROM media
                            WHERE hd_twin_msg_id IS NOT NULL
                       )
                """)
                conn.execute("""
                    UPDATE media SET assoc_kind = 'motion'
                     WHERE is_hd_twin = 1
                       AND assoc_kind IS NULL
                       AND message_id IN (
                           SELECT motion_video_msg_id FROM media
                            WHERE motion_video_msg_id IS NOT NULL
                       )
                """)
                conn.commit()
            except Exception:
                pass

            # Indexes for the message_association linkage columns.
            # Partial — only association children carry non-null
            # values for these columns, so the index stays small
            # and selective.  See schema.py for full rationale.
            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_media_is_hd_twin "
                "ON media(is_hd_twin) WHERE is_hd_twin = 1",
                "CREATE INDEX IF NOT EXISTS idx_media_hd_twin_msg_id "
                "ON media(hd_twin_msg_id) WHERE hd_twin_msg_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_media_assoc_parent_msg_id "
                "ON media(assoc_parent_msg_id) WHERE assoc_parent_msg_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_media_assoc_kind "
                "ON media(assoc_kind) WHERE assoc_kind IS NOT NULL",
            ):
                try:
                    conn.execute(idx_sql)
                except Exception:
                    pass
            conn.commit()

        except Exception:
            pass  # Non-critical migration
        finally:
            conn.close()

    def _connect(self) -> None:
        # This connection is READ-ONLY but the DB itself is NOT
        # immutable — a sibling write connection (see
        # ``_get_write_conn``) runs UPDATEs for hash-link / orphan-
        # rescue / download / tag / rebuild actions.  ``mode=ro``
        # gives us read-only access while still tracking WAL writes
        # from the sibling; ``query_only=ON`` is a belt-and-braces
        # guard against accidental writes through this connection.
        # (``?immutable=1`` would be wrong here — it tells SQLite
        # the file won't change while open, which isn't true.)
        uri = f"file:{self._db_path.as_posix()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Read-only performance PRAGMAs.  journal_mode is left at
        # whatever the writer set (WAL).  Overriding to OFF on a
        # read-only connection would itself attempt a journal-
        # header write.
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA mmap_size=2000000000")
        self._conn.execute("PRAGMA cache_size=-200000")  # 200MB
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA query_only=ON")

    def _get_write_conn(self) -> sqlite3.Connection:
        """Get or create a writable connection (non-immutable)."""
        if self._write_conn is None:
            self._write_conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False
            )
            self._write_conn.row_factory = sqlite3.Row
            self._write_conn.execute("PRAGMA journal_mode=WAL")
            self._write_conn.execute("PRAGMA synchronous=NORMAL")
        return self._write_conn

    def execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement (INSERT/UPDATE/DELETE) on a writable connection."""
        conn = self._get_write_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur

    def reconnect_read(self) -> None:
        """Reopen the immutable read connection to pick up writes."""
        if self._conn:
            self._conn.close()
        self._connect()

    def checkpoint_and_reconnect(self) -> None:
        """Checkpoint WAL on write connection, close it, then reconnect read.

        This ensures schema changes (CREATE TABLE) made via execute_write()
        are flushed into the main DB file so the immutable read connection
        can see them.
        """
        if self._write_conn is not None:
            try:
                self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._write_conn.close()
            self._write_conn = None
        self.reconnect_read()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple = ()):
        row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        if self._write_conn:
            self._write_conn.close()
            self._write_conn = None
        if self._conn:
            self._conn.close()
            self._conn = None
        Database._instance = None
