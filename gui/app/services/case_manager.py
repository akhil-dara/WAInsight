"""
Case Manager -- portable .wfacase folder management.

Case folder structure:
    MyCase.wfacase/
        analysis.db          — the analysis database (current version)
        metadata.json        — case ID, examiner, creation date, notes, source paths
        exports/             — HTML exports, CSVs (auto-populated)
        recovered_media/     — downloaded/decrypted media
        archives/            — timestamped backups of analysis.db before re-ingestion
            archive_manifest.json  — SHA-256 hashes, timestamps, chain of custody log
            analysis_YYYYMMDD_HHMMSS.db  — archived database snapshots
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings


CASE_EXT = ".wfacase"
METADATA_FILE = "metadata.json"
MAX_RECENT = 10


class CaseManager:
    """Manages portable forensic case folders."""

    _instance: Optional[CaseManager] = None

    @classmethod
    def get(cls) -> CaseManager:
        if cls._instance is None:
            cls._instance = CaseManager()
        return cls._instance

    def __init__(self):
        self._case_path: Optional[Path] = None
        self._metadata: dict = {}

    @property
    def is_open(self) -> bool:
        return self._case_path is not None

    @property
    def case_path(self) -> Optional[Path]:
        return self._case_path

    @property
    def case_name(self) -> str:
        if self._case_path:
            return self._case_path.stem
        return ""

    @property
    def metadata(self) -> dict:
        return dict(self._metadata)

    @property
    def analysis_db_path(self) -> Optional[Path]:
        if self._case_path:
            return self._case_path / "analysis.db"
        return None

    @property
    def exports_dir(self) -> Optional[Path]:
        if self._case_path:
            return self._case_path / "exports"
        return None

    @property
    def recovered_media_dir(self) -> Optional[Path]:
        if self._case_path:
            return self._case_path / "recovered_media"
        return None

    def create_case(
        self,
        folder_path: str,
        case_id: str = "",
        examiner: str = "",
        notes: str = "",
        analysis_db_source: Optional[str] = None,
    ) -> Path:
        """Create a new case folder structure.

        Args:
            folder_path: Path where the .wfacase folder will be created.
            case_id: Optional case identifier.
            examiner: Optional examiner name.
            notes: Optional case notes.
            analysis_db_source: Path to existing analysis.db to copy in.

        Returns:
            Path to the created case folder.
        """
        case_dir = Path(folder_path)
        if not case_dir.name.endswith(CASE_EXT):
            case_dir = case_dir.with_suffix(CASE_EXT)

        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "exports").mkdir(exist_ok=True)
        (case_dir / "recovered_media").mkdir(exist_ok=True)
        (case_dir / "archives").mkdir(exist_ok=True)

        metadata = {
            "case_id": case_id,
            "examiner": examiner,
            "notes": notes,
            "created": datetime.now().isoformat(),
            "modified": datetime.now().isoformat(),
            "source_paths": {},
        }

        if analysis_db_source and Path(analysis_db_source).exists():
            dest = case_dir / "analysis.db"
            shutil.copy2(analysis_db_source, dest)
            metadata["source_paths"]["analysis_db"] = str(analysis_db_source)

        (case_dir / METADATA_FILE).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        self._case_path = case_dir
        self._metadata = metadata
        self._add_to_recent(str(case_dir))
        return case_dir

    def open_case(self, folder_path: str) -> Path:
        """Open an existing case folder.

        Args:
            folder_path: Path to the .wfacase folder.

        Returns:
            Path to analysis.db inside the case.

        Raises:
            FileNotFoundError: If the case folder or analysis.db is missing.
        """
        case_dir = Path(folder_path)
        if not case_dir.exists():
            raise FileNotFoundError(f"Case folder not found: {case_dir}")

        db_path = case_dir / "analysis.db"
        if not db_path.exists():
            raise FileNotFoundError(
                f"analysis.db not found in case folder: {case_dir}"
            )

        meta_path = case_dir / METADATA_FILE
        if meta_path.exists():
            self._metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            self._metadata = {}

        self._case_path = case_dir
        self._add_to_recent(str(case_dir))

        # Initialize chain of custody logging
        try:
            from app.services.chain_of_custody import ChainOfCustody
            coc = ChainOfCustody.get()
            coc.initialize(
                case_dir,
                case_id=self._metadata.get("case_id", ""),
                examiner=self._metadata.get("examiner", ""),
            )
            coc.log("case_opened", {
                "case_path": str(case_dir),
                "analysis_db": str(db_path),
                "analysis_db_size": db_path.stat().st_size if db_path.exists() else 0,
            })
        except Exception:
            pass

        return db_path

    def save_metadata(self, **kwargs) -> None:
        """Update case metadata with given key-value pairs."""
        if not self._case_path:
            return
        self._metadata.update(kwargs)
        self._metadata["modified"] = datetime.now().isoformat()
        meta_path = self._case_path / METADATA_FILE
        meta_path.write_text(
            json.dumps(self._metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def close_case(self) -> None:
        self._case_path = None
        self._metadata = {}

    def recent_cases(self) -> list[dict]:
        """Return list of recently opened cases from QSettings."""
        settings = QSettings()
        recent = settings.value("recent_cases", [])
        if not isinstance(recent, list):
            recent = []

        result = []
        for path_str in recent:
            p = Path(path_str)
            if p.exists():
                meta_path = p / METADATA_FILE
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass
                has_db = (p / "analysis.db").exists()
                result.append({
                    "path": str(p),
                    "name": p.stem,
                    "case_id": meta.get("case_id", ""),
                    "examiner": meta.get("examiner", ""),
                    "created": meta.get("created", ""),
                    "has_db": has_db,
                })
        return result

    @property
    def archives_dir(self) -> Optional[Path]:
        if self._case_path:
            return self._case_path / "archives"
        return None

    def list_archives(self) -> list[dict]:
        """List all archived analysis.db versions with metadata.

        Returns:
            List of dicts with filename, archived_at, sha256, size_bytes, etc.
        """
        if not self._case_path:
            return []
        manifest_path = self._case_path / "archives" / "archive_manifest.json"
        if not manifest_path.exists():
            return []
        try:
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
            return entries if isinstance(entries, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _add_to_recent(self, path_str: str) -> None:
        settings = QSettings()
        recent = settings.value("recent_cases", [])
        if not isinstance(recent, list):
            recent = []
        # Remove existing entry and add to front
        recent = [p for p in recent if p != path_str]
        recent.insert(0, path_str)
        recent = recent[:MAX_RECENT]
        settings.setValue("recent_cases", recent)
