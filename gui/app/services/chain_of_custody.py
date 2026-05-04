"""
Chain of Custody Logger for WAInsight — WhatsApp Forensic Suite.

Maintains an immutable audit trail of all forensic actions:
- Case creation/opening
- Database ingestion (with SHA-256 hashes of source files)
- Media downloads (with hash verification)
- Evidence export (HTML/PDF reports)
- User actions (tagging, annotation)

Log format: JSON Lines (.jsonl), one entry per line, append-only.
Each entry has: timestamp, action, actor (examiner), details, integrity_hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import getpass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class ChainOfCustody:
    """Singleton chain of custody logger."""

    _instance: Optional[ChainOfCustody] = None

    @classmethod
    def get(cls) -> ChainOfCustody:
        if cls._instance is None:
            cls._instance = ChainOfCustody()
        return cls._instance

    def __init__(self):
        self._log_path: Optional[Path] = None
        self._examiner: str = ""
        self._case_id: str = ""
        self._entry_count: int = 0
        self._last_hash: str = "0" * 64  # genesis hash

    def initialize(self, case_path: Path, case_id: str = "", examiner: str = ""):
        """Initialize logging for a case."""
        self._log_path = case_path / "chain_of_custody.jsonl"
        self._case_id = case_id
        self._examiner = examiner or getpass.getuser()

        # Read existing entries to get the last hash for integrity chain
        if self._log_path.exists():
            try:
                with open(self._log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._entry_count += 1
                            try:
                                entry = json.loads(line)
                                self._last_hash = entry.get("integrity_hash", self._last_hash)
                            except json.JSONDecodeError:
                                pass
            except OSError:
                pass

        # Log session start
        self.log("session_started", {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "user": getpass.getuser(),
            "tool_version": "2.0.0",
            "python_version": platform.python_version(),
            "existing_entries": self._entry_count,
        })

    def log(self, action: str, details: dict = None) -> None:
        """Append a chain-of-custody entry."""
        if not self._log_path:
            return

        entry = {
            "seq": self._entry_count + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "timestamp_local": datetime.now().isoformat(timespec="milliseconds"),
            "action": action,
            "examiner": self._examiner,
            "case_id": self._case_id,
        }
        if details:
            entry["details"] = details

        # Integrity chain: hash of (previous_hash + current_entry)
        entry_str = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        chain_input = f"{self._last_hash}:{entry_str}"
        integrity_hash = hashlib.sha256(chain_input.encode("utf-8")).hexdigest()
        entry["integrity_hash"] = integrity_hash
        entry["previous_hash"] = self._last_hash

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._last_hash = integrity_hash
            self._entry_count += 1
        except OSError:
            pass

    def log_file_hash(self, action: str, file_path: str, extra: dict = None) -> str:
        """Log a file action with its SHA-256 hash."""
        sha256 = ""
        try:
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            sha256 = h.hexdigest()
        except (OSError, IOError):
            sha256 = "ERROR_READING_FILE"

        details = {
            "file_path": str(file_path),
            "file_size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
            "sha256": sha256,
        }
        if extra:
            details.update(extra)
        self.log(action, details)
        return sha256

    def log_download(self, message_id: int, save_path: str, sha256: str,
                     url_domain: str = "", media_type: str = "",
                     file_size: int = 0, conversation_id: int = 0) -> None:
        """Log a media download with full provenance."""
        self.log("media_downloaded", {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "save_path": str(save_path),
            "sha256": sha256,
            "file_size": file_size,
            "media_type": media_type,
            "url_domain": url_domain,
        })

    def log_export(self, export_type: str, file_path: str,
                   contact_id: int = 0, contact_name: str = "") -> None:
        """Log a report/evidence export."""
        sha256 = ""
        if os.path.exists(file_path):
            try:
                h = hashlib.sha256()
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        h.update(chunk)
                sha256 = h.hexdigest()
            except OSError:
                pass

        self.log("evidence_exported", {
            "export_type": export_type,
            "file_path": str(file_path),
            "file_size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
            "sha256": sha256,
            "contact_id": contact_id,
            "contact_name": contact_name,
        })

    def log_ingestion(self, source_files: dict[str, str], output_path: str,
                      message_count: int = 0, contact_count: int = 0) -> None:
        """Log database ingestion with source file hashes."""
        self.log("ingestion_completed", {
            "source_files": source_files,  # {filename: sha256}
            "output_path": str(output_path),
            "message_count": message_count,
            "contact_count": contact_count,
        })

    def verify_integrity(self) -> tuple[bool, int, int]:
        """Verify the integrity chain of the log file.

        Returns:
            (is_valid, total_entries, first_broken_entry)
        """
        if not self._log_path or not self._log_path.exists():
            return True, 0, 0

        prev_hash = "0" * 64
        total = 0
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        entry = json.loads(line)
                        stored_hash = entry.pop("integrity_hash", "")
                        stored_prev = entry.pop("previous_hash", "")

                        if stored_prev != prev_hash:
                            return False, total, total

                        entry_str = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                        chain_input = f"{prev_hash}:{entry_str}"
                        computed = hashlib.sha256(chain_input.encode("utf-8")).hexdigest()

                        if computed != stored_hash:
                            return False, total, total

                        prev_hash = stored_hash
                    except (json.JSONDecodeError, KeyError):
                        return False, total, total
        except OSError:
            return False, 0, 0

        return True, total, 0
