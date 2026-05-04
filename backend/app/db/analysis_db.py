"""
Analysis database creation and lifecycle management.

Handles creating analysis.db with the complete normalized schema,
running migrations, and providing status information about the
analysis database state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import apsw

from app.db.schema import SCHEMA_VERSION, create_schema

logger = logging.getLogger(__name__)


class AnalysisDatabase:
    """Manages the analysis.db lifecycle - creation, migration, and status.

    The analysis.db is the normalized forensic database that ingestion
    populates from the raw WhatsApp source databases. It contains all
    30+ tables defined in schema.py.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialize analysis database manager.

        Args:
            db_path: Path where analysis.db will be created/opened.
        """
        self.db_path = db_path
        self._conn: Optional[apsw.Connection] = None

    @property
    def exists(self) -> bool:
        """Check if the analysis database file already exists."""
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    def get_connection(self) -> apsw.Connection:
        """Get or create a connection to analysis.db.

        Returns:
            APSW connection with WAL mode and optimized settings.
        """
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = apsw.Connection(str(self.db_path))
            self._configure_connection(self._conn)
        return self._conn

    def _configure_connection(self, conn: apsw.Connection) -> None:
        """Apply performance PRAGMAs to analysis.db connection.

        Uses WAL journal mode for concurrent reads during API serving,
        and NORMAL synchronous for balanced safety/performance.
        """
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA cache_size = -65536")  # 64MB
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA mmap_size = 2147483648")  # 2GB for analysis.db

    def create(self, force: bool = False) -> None:
        """Create the analysis database with the complete schema.

        Args:
            force: If True, delete existing database and recreate.
                   If False, skip creation if database already exists.

        Raises:
            FileExistsError: If database exists and force=False.
        """
        if self.exists and not force:
            logger.info("Analysis database already exists at %s", self.db_path)
            return

        if self.exists and force:
            logger.warning("Archiving existing analysis database before re-creation: %s", self.db_path)
            self.close()
            self._archive_existing()
            self.db_path.unlink()

        logger.info("Creating analysis database at %s", self.db_path)
        conn = self.get_connection()
        create_schema(conn)

        # Store schema version
        conn.cursor().execute(
            "INSERT OR REPLACE INTO case_metadata (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

        logger.info("Analysis database created successfully with schema v%d", SCHEMA_VERSION)

    def _archive_existing(self) -> None:
        """Archive the current analysis.db before overwriting.

        Creates an archives/ directory alongside analysis.db and copies
        the database with a timestamped filename. Writes an archive manifest
        entry with SHA-256 hash, size, and timestamp for chain of custody.
        """
        if not self.db_path.exists():
            return

        archives_dir = self.db_path.parent / "archives"
        archives_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"analysis_{ts}.db"
        archive_path = archives_dir / archive_name

        # Compute SHA-256 of the original before copying
        sha256 = self._compute_sha256(self.db_path)
        file_size = self.db_path.stat().st_size

        # Extract key metadata from the DB before archiving
        ingestion_ts = ""
        case_id = ""
        try:
            tmp_conn = apsw.Connection(str(self.db_path), flags=apsw.SQLITE_OPEN_READONLY)
            cur = tmp_conn.cursor()
            for key, value in cur.execute("SELECT key, value FROM case_metadata"):
                if key == "ingestion_timestamp":
                    ingestion_ts = value
                elif key == "case_id":
                    case_id = value
            tmp_conn.close()
        except Exception:
            pass

        # Copy file (preserving metadata)
        shutil.copy2(str(self.db_path), str(archive_path))
        logger.info("Archived analysis.db -> %s (SHA-256: %s)", archive_path, sha256)

        # Update archive manifest
        manifest_path = archives_dir / "archive_manifest.json"
        manifest: list = []
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = []

        manifest.append({
            "filename": archive_name,
            "archived_at": datetime.now().isoformat(),
            "sha256": sha256,
            "size_bytes": file_size,
            "ingestion_timestamp": ingestion_ts,
            "case_id": case_id,
            "reason": "pre_reingest_backup",
        })

        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        """Compute SHA-256 hex digest of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):  # 1MB chunks
                h.update(chunk)
        return h.hexdigest()

    def get_schema_version(self) -> Optional[int]:
        """Get the current schema version of the analysis database.

        Returns:
            Schema version integer, or None if not set.
        """
        if not self.exists:
            return None

        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT value FROM case_metadata WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else None
        except (apsw.SQLError, apsw.DatabaseError):
            return None

    def get_stats(self) -> dict[str, int]:
        """Get row counts for all major tables in analysis.db.

        Returns:
            Dictionary mapping table name to row count.
        """
        if not self.exists:
            return {}

        conn = self.get_connection()
        cursor = conn.cursor()
        stats: dict[str, int] = {}

        # Get all table names
        tables = [
            row[0] for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
            ).fetchall()
        ]

        for table in tables:
            try:
                count = cursor.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()
                stats[table] = count[0] if count else 0
            except apsw.SQLError:
                stats[table] = -1

        return stats

    def get_case_metadata(self) -> dict[str, str]:
        """Retrieve all case metadata key-value pairs.

        Returns:
            Dictionary of case metadata.
        """
        if not self.exists:
            return {}

        conn = self.get_connection()
        cursor = conn.cursor()
        rows = cursor.execute("SELECT key, value FROM case_metadata").fetchall()
        return {row[0]: row[1] for row in rows}

    def set_case_metadata(self, key: str, value: str) -> None:
        """Set a case metadata value.

        Args:
            key: Metadata key (e.g., 'case_id', 'examiner').
            value: Metadata value.
        """
        conn = self.get_connection()
        conn.cursor().execute(
            "INSERT OR REPLACE INTO case_metadata (key, value) VALUES (?, ?)",
            (key, value),
        )

    def is_ingestion_complete(self) -> bool:
        """Check if ingestion has been completed.

        Returns:
            True if the 'ingestion_complete' metadata flag is set.
        """
        metadata = self.get_case_metadata()
        return metadata.get("ingestion_complete") == "true"

    def execute(self, sql: str, params: tuple = ()) -> list:
        """Execute a query and return all results.

        Args:
            sql: SQL query string.
            params: Query parameters.

        Returns:
            List of result tuples.
        """
        conn = self.get_connection()
        return conn.cursor().execute(sql, params).fetchall()

    def executemany(self, sql: str, params_seq) -> None:
        """Execute a query with multiple parameter sets.

        Args:
            sql: SQL query with placeholders.
            params_seq: Iterable of parameter tuples.
        """
        conn = self.get_connection()
        # Use executemany for bulk inserts
        for params in params_seq:
            conn.cursor().execute(sql, params)

    def execute_script(self, sql: str) -> None:
        """Execute multiple SQL statements separated by semicolons.

        Args:
            sql: Multi-statement SQL string.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)

    def begin_transaction(self) -> None:
        """Begin an explicit transaction."""
        self.get_connection().cursor().execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        """Commit the current transaction."""
        self.get_connection().cursor().execute("COMMIT")

    def rollback(self) -> None:
        """Rollback the current transaction."""
        try:
            self.get_connection().cursor().execute("ROLLBACK")
        except apsw.SQLError:
            pass  # No active transaction

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except apsw.ConnectionClosedError:
                pass
            self._conn = None

    def __enter__(self) -> "AnalysisDatabase":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
