"""
Media Evidence Database — forensic-grade media URL tracking and download audit log.

Created inside {case}.wfacase/media_evidence.db alongside analysis.db.
Stores ALL media URLs with original msgstore.db identifiers (not analysis DB IDs)
for forensic traceability. Every download attempt is logged with UTC timestamp,
result, errors, and chain-of-custody metadata.

Tables:
  media_url_registry  — One row per media file with all source identifiers
  download_audit_log  — Immutable append-only log of every download attempt
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
-- Registry of all media URLs with original msgstore.db identifiers
CREATE TABLE IF NOT EXISTS media_url_registry (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Original msgstore.db identifiers (forensic source)
    msgstore_msg_id         INTEGER NOT NULL,          -- message._id from msgstore.db
    msgstore_chat_id        INTEGER,                   -- message.chat_row_id from msgstore.db
    msgstore_sender_jid_id  INTEGER,                   -- message.sender_jid_row_id from msgstore.db
    message_key_id          TEXT NOT NULL,              -- message.key_id (unique WhatsApp key)

    -- JID identifiers (human-readable)
    chat_jid                TEXT,                       -- e.g., "15551234567-1621667229@g.us"
    sender_jid              TEXT,                       -- e.g., "15551234567@s.whatsapp.net"

    -- Media identifiers
    message_url             TEXT,                       -- Full CDN URL (encrypted media)
    direct_path             TEXT,                       -- CDN direct path
    media_key               BLOB,                      -- 32-byte AES-256 decryption key
    media_key_timestamp     INTEGER,                   -- When key was generated (Unix ms)
    file_hash               TEXT,                       -- SHA-256 hash of decrypted file (base64)
    enc_file_hash           TEXT,                       -- SHA-256 hash of encrypted file
    original_file_hash      TEXT,                       -- Original file hash before re-encryption

    -- Media metadata
    mime_type               TEXT,
    file_size               INTEGER,
    media_name              TEXT,
    file_path_in_wa         TEXT,                       -- Original WhatsApp file_path from msgstore

    -- Message context
    from_me                 INTEGER,                   -- 1=sent, 0=received
    msg_timestamp           INTEGER,                   -- Message timestamp (Unix ms)
    msg_type                INTEGER,                   -- WhatsApp message_type code

    -- Analysis DB cross-reference (for convenience, not forensic source)
    analysis_msg_id         INTEGER,                   -- message.id in analysis.db
    analysis_media_id       INTEGER,                   -- media.id in analysis.db
    analysis_conv_id        INTEGER,                   -- conversation.id in analysis.db

    -- Status tracking
    cdn_expiry_ts           INTEGER,                   -- CDN URL expiry timestamp (Unix seconds)
    is_on_disk              INTEGER DEFAULT 0,         -- 1 if file exists on disk
    recovery_method         TEXT,                      -- NULL, 'downloaded', 'hash_linked'
    recovery_timestamp      INTEGER,                   -- When recovered (Unix ms)
    recovered_file_path     TEXT,                       -- Path to recovered file

    -- Metadata
    created_at              TEXT DEFAULT (datetime('now', 'utc')),

    UNIQUE(message_key_id)
);

-- Immutable append-only audit log of every download attempt
-- Chain of custody: never UPDATE or DELETE rows in this table
CREATE TABLE IF NOT EXISTS download_audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc       TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    timestamp_unix_ms   INTEGER NOT NULL,              -- Unix milliseconds for precision

    -- What was attempted
    message_key_id      TEXT NOT NULL,                 -- Links to media_url_registry
    media_url           TEXT,                          -- URL that was tried
    action              TEXT NOT NULL,                 -- 'download_start', 'download_success',
                                                      -- 'download_fail', 'decrypt_success',
                                                      -- 'decrypt_fail', 'hash_link',
                                                      -- 'hash_verify_pass', 'hash_verify_fail'

    -- Result
    success             INTEGER NOT NULL DEFAULT 0,    -- 1=success, 0=failure
    error_message       TEXT,                          -- Error details if failed
    http_status_code    INTEGER,                       -- HTTP response code (200, 404, 410, etc.)
    response_size_bytes INTEGER,                       -- Size of downloaded data

    -- Verification
    expected_hash       TEXT,                          -- Expected SHA-256 hash
    actual_hash         TEXT,                          -- Computed SHA-256 hash after download
    hash_match          INTEGER,                       -- 1=match, 0=mismatch, NULL=not checked

    -- Output
    saved_file_path     TEXT,                          -- Where the file was saved
    saved_file_size     INTEGER,                       -- Size of saved file

    -- Context
    examiner            TEXT,                          -- Who performed the action
    tool_version        TEXT,                          -- WAInsight version
    case_id             TEXT,                          -- Case identifier
    notes               TEXT                           -- Additional notes
);

CREATE INDEX IF NOT EXISTS idx_registry_key_id ON media_url_registry(message_key_id);
CREATE INDEX IF NOT EXISTS idx_registry_chat_jid ON media_url_registry(chat_jid);
CREATE INDEX IF NOT EXISTS idx_registry_file_hash ON media_url_registry(file_hash);
CREATE INDEX IF NOT EXISTS idx_audit_key_id ON download_audit_log(message_key_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON download_audit_log(timestamp_unix_ms);
CREATE INDEX IF NOT EXISTS idx_audit_action ON download_audit_log(action);
"""


class MediaEvidenceDB:
    """Manages the media evidence database for forensic-grade URL tracking."""

    _instance: Optional[MediaEvidenceDB] = None

    @classmethod
    def get(cls) -> MediaEvidenceDB:
        if cls._instance is None:
            cls._instance = MediaEvidenceDB()
        return cls._instance

    def __init__(self):
        self._conn: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None

    def init(self, case_path: Path):
        """Initialize/open the media evidence DB in the case folder."""
        self._db_path = case_path / "media_evidence.db"
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        logger.info("Media evidence DB initialized: %s", self._db_path)

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def register_media(self, **kwargs) -> int:
        """Register a media URL in the registry. Returns the registry ID."""
        if not self._conn:
            return -1
        cols = list(kwargs.keys())
        vals = list(kwargs.values())
        placeholders = ",".join(["?"] * len(cols))
        col_str = ",".join(cols)
        try:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO media_url_registry ({col_str}) VALUES ({placeholders})",
                vals,
            )
            self._conn.commit()
            return cur.lastrowid or -1
        except Exception as e:
            logger.error("Failed to register media: %s", e)
            return -1

    def log_download(self, message_key_id: str, action: str, success: bool,
                     error_message: str = None, http_status: int = None,
                     response_size: int = None, expected_hash: str = None,
                     actual_hash: str = None, saved_path: str = None,
                     saved_size: int = None, examiner: str = None,
                     case_id: str = None, notes: str = None,
                     media_url: str = None, tool_version: str = None):
        """Append an immutable audit log entry for a download attempt."""
        if not self._conn:
            return
        now_ms = int(time.time() * 1000)
        hash_match = None
        if expected_hash and actual_hash:
            hash_match = 1 if expected_hash == actual_hash else 0

        try:
            self._conn.execute(
                "INSERT INTO download_audit_log "
                "(timestamp_unix_ms, message_key_id, media_url, action, success, "
                " error_message, http_status_code, response_size_bytes, "
                " expected_hash, actual_hash, hash_match, "
                " saved_file_path, saved_file_size, "
                " examiner, tool_version, case_id, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms, message_key_id, media_url, action, 1 if success else 0,
                 error_message, http_status, response_size,
                 expected_hash, actual_hash, hash_match,
                 saved_path, saved_size,
                 examiner, tool_version, case_id, notes),
            )
            self._conn.commit()
        except Exception as e:
            logger.error("Failed to log download: %s", e)

    def update_registry_status(self, message_key_id: str, **kwargs):
        """Update status fields in the registry (recovery_method, recovered_file_path, etc.)."""
        if not self._conn or not kwargs:
            return
        set_parts = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [message_key_id]
        try:
            self._conn.execute(
                f"UPDATE media_url_registry SET {set_parts} WHERE message_key_id = ?",
                vals,
            )
            self._conn.commit()
        except Exception as e:
            logger.error("Failed to update registry: %s", e)

    def get_stats(self) -> dict:
        """Get summary statistics for the evidence DB."""
        if not self._conn:
            return {}
        try:
            total = self._conn.execute("SELECT count(*) FROM media_url_registry").fetchone()[0]
            with_url = self._conn.execute(
                "SELECT count(*) FROM media_url_registry WHERE message_url IS NOT NULL").fetchone()[0]
            on_disk = self._conn.execute(
                "SELECT count(*) FROM media_url_registry WHERE is_on_disk = 1").fetchone()[0]
            downloaded = self._conn.execute(
                "SELECT count(*) FROM media_url_registry WHERE recovery_method = 'downloaded'").fetchone()[0]
            hash_linked = self._conn.execute(
                "SELECT count(*) FROM media_url_registry WHERE recovery_method = 'hash_linked'").fetchone()[0]
            audit_count = self._conn.execute("SELECT count(*) FROM download_audit_log").fetchone()[0]
            failed = self._conn.execute(
                "SELECT count(*) FROM download_audit_log WHERE success = 0").fetchone()[0]
            return {
                "total_media": total, "with_url": with_url, "on_disk": on_disk,
                "downloaded": downloaded, "hash_linked": hash_linked,
                "audit_entries": audit_count, "failed_attempts": failed,
            }
        except Exception:
            return {}
