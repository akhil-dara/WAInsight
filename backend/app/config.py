"""
Application configuration management.

Handles all paths, ports, and settings for the forensic analyzer.
Supports both CLI arguments and environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SourceConfig:
    """Configuration for source WhatsApp database files."""

    databases_path: Path
    """Path to the extracted WhatsApp databases directory (contains msgstore.db, wa.db, etc.)."""

    media_path: Optional[Path] = None
    """Path to the WhatsApp media root directory (contains Media/ subfolder)."""

    avatars_path: Optional[Path] = None
    """Path to WhatsApp Avatars folder (contains .j profile picture files)."""

    extra_db_paths: dict[str, str] | None = None
    """Explicit paths for individual databases. Overrides databases_path lookup.
    Example: {"wa.db": "/path/to/wa.db", "location.db": "/other/path/location.db"}"""

    prefs_dir: Optional[Path] = None
    """Optional directory containing WhatsApp ``shared_prefs`` XML files
    (``startup_prefs.xml``, etc).  When set, ingestion reads pref files
    from here instead of assuming ``<databases_path>/../shared_prefs/``.

    Set by the GUI to a *temp staging folder* so pref files never have to
    live next to the source msgstore.db — writing into the source folder
    would break forensic read-only integrity.  ``None`` = fall back to
    the legacy ``../shared_prefs/`` lookup relative to the msgstore
    directory."""

    def validate(self) -> list[str]:
        """Validate that required source files exist. Returns list of errors."""
        errors: list[str] = []
        if not self.databases_path.exists():
            errors.append(f"Databases directory not found: {self.databases_path}")
            return errors

        msgstore = (
            Path(self.extra_db_paths["msgstore.db"])
            if self.extra_db_paths and self.extra_db_paths.get("msgstore.db")
            else self.databases_path / "msgstore.db"
        )
        if not msgstore.exists():
            errors.append(f"msgstore.db not found: {msgstore}")

        if self.media_path and not self.media_path.exists():
            errors.append(f"Media directory not found: {self.media_path}")

        return errors

    @property
    def msgstore_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("msgstore.db"):
            return Path(self.extra_db_paths["msgstore.db"])
        return self.databases_path / "msgstore.db"

    @property
    def wa_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("wa.db"):
            return Path(self.extra_db_paths["wa.db"])
        return self.databases_path / "wa.db"

    @property
    def status_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("status.db"):
            return Path(self.extra_db_paths["status.db"])
        return self.databases_path / "status.db"

    @property
    def axolotl_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("axolotl.db"):
            return Path(self.extra_db_paths["axolotl.db"])
        return self.databases_path / "axolotl.db"

    @property
    def chatsettings_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("chatsettings.db"):
            return Path(self.extra_db_paths["chatsettings.db"])
        return self.databases_path / "chatsettings.db"

    @property
    def stickers_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("stickers.db"):
            return Path(self.extra_db_paths["stickers.db"])
        return self.databases_path / "stickers.db"

    @property
    def media_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("media.db"):
            return Path(self.extra_db_paths["media.db"])
        return self.databases_path / "media.db"

    @property
    def companion_devices_db_path(self) -> Path:
        if self.extra_db_paths and self.extra_db_paths.get("companion_devices.db"):
            return Path(self.extra_db_paths["companion_devices.db"])
        return self.databases_path / "companion_devices.db"

    def list_available_databases(self) -> list[Path]:
        """Return all .db files found in the databases directory."""
        if not self.databases_path.exists():
            return []
        return sorted(self.databases_path.glob("*.db"))

    def list_wal_files(self) -> list[Path]:
        """Return all WAL files found (forensic recovery targets)."""
        if not self.databases_path.exists():
            return []
        wal_files: list[Path] = []
        wal_files.extend(self.databases_path.glob("*.db-wal"))
        wal_files.extend(self.databases_path.glob("*.db-shm"))
        wal_files.extend(self.databases_path.glob("*.db-journal"))
        return sorted(wal_files)


@dataclass
class CaseConfig:
    """Forensic case metadata for evidence chain."""

    case_id: str = ""
    examiner: str = ""
    notes: str = ""


@dataclass
class ServerConfig:
    """FastAPI server configuration."""

    host: str = "127.0.0.1"
    port: int = 8741
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    debug: bool = False


@dataclass
class AnalysisConfig:
    """Analysis processing configuration."""

    batch_size: int = 50_000
    """Number of messages to process per batch during ingestion."""

    fts_batch_size: int = 50_000
    """Number of records per batch when building FTS5 index."""

    enable_ocr: bool = False
    """Whether to run OCR on document/image media (requires pytesseract)."""

    enable_device_receipts: bool = False
    """Whether to ingest per-device receipt records (slow, optional —
    receipt_device tables can be very large on busy accounts)."""

    max_mmap_size: int = 4_294_967_296
    """SQLite mmap_size pragma value (4GB default)."""

    page_cache_mb: int = 64
    """SQLite page cache size in MB."""


@dataclass
class AppConfig:
    """Root application configuration."""

    source: SourceConfig
    output_path: Path
    case: CaseConfig = field(default_factory=CaseConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)

    @property
    def analysis_db_path(self) -> Path:
        return self.output_path / "analysis.db"

    @property
    def recovered_media_path(self) -> Path:
        return self.output_path / "recovered_media"

    @property
    def reports_path(self) -> Path:
        return self.output_path / "reports"

    @property
    def exports_path(self) -> Path:
        return self.output_path / "exports"

    def ensure_output_dirs(self) -> None:
        """Create output directories if they don't exist."""
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.recovered_media_path.mkdir(parents=True, exist_ok=True)
        self.reports_path.mkdir(parents=True, exist_ok=True)
        self.exports_path.mkdir(parents=True, exist_ok=True)


def build_config(
    databases_path: str,
    output_path: str,
    media_path: str | None = None,
    avatars_path: str | None = None,
    case_id: str = "",
    examiner: str = "",
    port: int = 8741,
    enable_ocr: bool = False,
    enable_device_receipts: bool = False,
    debug: bool = False,
    extra_db_paths: dict[str, str] | None = None,
    prefs_dir: str | None = None,
) -> AppConfig:
    """Build application configuration from CLI/API arguments.

    Args:
        databases_path: Path to WhatsApp databases directory.
        output_path: Path for analysis.db and output files.
        media_path: Optional path to WhatsApp media directory.
        case_id: Forensic case identifier.
        examiner: Name of the forensic examiner.
        port: FastAPI server port.
        enable_ocr: Enable OCR processing for documents.
        enable_device_receipts: Enable device-level receipt ingestion.
        debug: Enable debug mode.

    Returns:
        Fully configured AppConfig instance.
    """
    return AppConfig(
        source=SourceConfig(
            databases_path=Path(databases_path),
            media_path=Path(media_path) if media_path else None,
            avatars_path=Path(avatars_path) if avatars_path else None,
            extra_db_paths=extra_db_paths,
            prefs_dir=Path(prefs_dir) if prefs_dir else None,
        ),
        output_path=Path(output_path),
        case=CaseConfig(case_id=case_id, examiner=examiner),
        server=ServerConfig(port=port, debug=debug),
        analysis=AnalysisConfig(
            enable_ocr=enable_ocr,
            enable_device_receipts=enable_device_receipts,
        ),
    )
