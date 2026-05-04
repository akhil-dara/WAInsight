"""
APSW-based database connection management for forensic analysis.

Provides optimized read-only connections to source WhatsApp databases
(msgstore.db, wa.db, etc.) and read-write connections to analysis.db.

Key design decisions:
- APSW over stdlib sqlite3: Faster, always includes latest SQLite with FTS5,
  supports mmap_size beyond 2GB.
- Immutable mode for source DBs: Skips all locking and journal checks.
- Separate connections per thread: SQLite releases GIL during C calls,
  so concurrent reads work with a connection pool.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator, Optional, Sequence

import apsw

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_size_pages(cache_mb: int) -> int:
    """Convert a cache size in megabytes to negative-KiB value for SQLite.

    SQLite interprets a *negative* ``cache_size`` PRAGMA as KiB rather than
    pages, which makes the setting independent of the page size.

    Example:
        ``_cache_size_pages(64)`` returns ``-65536`` (64 * 1024 KiB).
    """
    return -(cache_mb * 1024)


def _apply_pragmas(cursor: apsw.Cursor, pragmas: dict[str, Any]) -> None:
    """Apply a dictionary of PRAGMA settings to *cursor*.

    Each key is the pragma name; the value is coerced to a string.  The
    ``journal_mode`` pragma is treated specially because it returns a result
    row that must be consumed.
    """
    for name, value in pragmas.items():
        stmt = f"PRAGMA {name} = {value};"
        if name == "journal_mode":
            # journal_mode returns the new mode -- consume the row.
            cursor.execute(stmt)
            result = cursor.fetchone()
            logger.debug("PRAGMA journal_mode set to %s", result[0] if result else "?")
        else:
            cursor.execute(stmt)


# ---------------------------------------------------------------------------
# SourceConnection -- read-only immutable
# ---------------------------------------------------------------------------

class SourceConnection:
    """Thread-safe read-only immutable connection to a source WhatsApp database.

    Opens the database in **immutable** mode (``?immutable=1`` URI flag) with
    optimized PRAGMAs for forensic read-only analysis of large (3 GB+)
    databases.  Immutable mode disables all file-locking, journal detection,
    and change counting, which yields significant speed-ups on read paths.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Must exist.
    mmap_size:
        Value for the ``mmap_size`` PRAGMA.  Defaults to 4 GiB which allows
        SQLite to memory-map the entire database on 64-bit systems.
    cache_mb:
        Page-cache size in MiB.  Converted to the negative-KiB form consumed
        by the ``cache_size`` PRAGMA.

    Raises
    ------
    FileNotFoundError
        If *db_path* does not exist on disk.
    apsw.Error
        If the database cannot be opened.
    """

    def __init__(
        self,
        db_path: Path,
        mmap_size: int = 4_294_967_296,
        cache_mb: int = 64,
    ) -> None:
        self._db_path = Path(db_path)
        if not self._db_path.exists():
            raise FileNotFoundError(f"Source database not found: {self._db_path}")

        # Build an immutable URI. APSW expects forward-slash paths inside
        # the URI even on Windows, so we normalise explicitly.
        uri_path = self._db_path.resolve().as_posix()
        uri = f"file:{uri_path}?immutable=1"
        logger.info("Opening source database (immutable): %s", self._db_path)

        self._conn: apsw.Connection = apsw.Connection(
            uri,
            flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
        )

        # Apply performance-oriented PRAGMAs.
        pragmas: dict[str, Any] = {
            "journal_mode": "OFF",
            "synchronous": "OFF",
            "temp_store": "MEMORY",
            "mmap_size": mmap_size,
            "cache_size": _cache_size_pages(cache_mb),
            "query_only": "ON",
        }
        _apply_pragmas(self.get_cursor(), pragmas)
        logger.debug("PRAGMAs applied for %s", self._db_path.name)

    # -- Accessors ----------------------------------------------------------

    @property
    def path(self) -> Path:
        """Return the filesystem path this connection was opened against."""
        return self._db_path

    @property
    def raw_connection(self) -> apsw.Connection:
        """Return the underlying ``apsw.Connection`` object."""
        return self._conn

    # -- Cursor / execution -------------------------------------------------

    def get_cursor(self) -> apsw.Cursor:
        """Create and return a new ``apsw.Cursor`` for this connection."""
        return self._conn.cursor()

    def execute(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> apsw.Cursor:
        """Execute *sql* and return the cursor.

        Parameters
        ----------
        sql:
            A single SQL statement.
        bindings:
            Optional sequence of parameter bindings (positional ``?`` style).

        Returns
        -------
        apsw.Cursor
            The cursor that holds the result set.
        """
        cursor = self.get_cursor()
        if bindings is not None:
            cursor.execute(sql, bindings)
        else:
            cursor.execute(sql)
        return cursor

    def fetchone(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> Optional[tuple[Any, ...]]:
        """Execute *sql* and return the first row, or ``None``."""
        cursor = self.execute(sql, bindings)
        return cursor.fetchone()  # type: ignore[return-value]

    def fetchall(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> list[tuple[Any, ...]]:
        """Execute *sql* and return every row as a list of tuples."""
        cursor = self.execute(sql, bindings)
        return list(cursor)

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying APSW connection.

        Safe to call multiple times.  After closing, any further operations
        on this object will raise ``apsw.ConnectionClosedError``.
        """
        try:
            self._conn.close()
            logger.debug("Closed source connection: %s", self._db_path.name)
        except apsw.ConnectionClosedError:
            pass

    # -- Context manager ----------------------------------------------------

    def __enter__(self) -> SourceConnection:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<SourceConnection path={self._db_path.name!r}>"


# ---------------------------------------------------------------------------
# AnalysisConnection -- read-write with WAL
# ---------------------------------------------------------------------------

class AnalysisConnection:
    """Thread-safe read-write connection to the ``analysis.db`` database.

    Uses WAL journal mode so that readers do not block during API serving.
    The connection is opened in standard (non-immutable) mode and enforces
    foreign-key constraints.

    Parameters
    ----------
    db_path:
        Path to the ``analysis.db`` file.  Created automatically if it does
        not yet exist.
    cache_mb:
        Page-cache size in MiB.
    """

    def __init__(self, db_path: Path, cache_mb: int = 64) -> None:
        self._db_path = Path(db_path)

        # Ensure the parent directory exists so APSW can create the file.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Opening analysis database (read-write): %s", self._db_path)
        self._conn: apsw.Connection = apsw.Connection(str(self._db_path.resolve()))

        pragmas: dict[str, Any] = {
            "journal_mode": "WAL",
            "synchronous": "OFF",        # Faster writes; WAL protects against corruption
            "temp_store": "MEMORY",
            "cache_size": _cache_size_pages(512),  # 512MB cache for large ingestion
            "mmap_size": 4_294_967_296,   # 4GB mmap for fast reads during ingestion
            "foreign_keys": "ON",
        }
        _apply_pragmas(self.get_cursor(), pragmas)
        logger.debug("PRAGMAs applied for analysis.db")

    # -- Accessors ----------------------------------------------------------

    @property
    def path(self) -> Path:
        """Return the filesystem path this connection was opened against."""
        return self._db_path

    @property
    def raw_connection(self) -> apsw.Connection:
        """Return the underlying ``apsw.Connection`` object."""
        return self._conn

    # -- Cursor / execution -------------------------------------------------

    def get_cursor(self) -> apsw.Cursor:
        """Create and return a new ``apsw.Cursor`` for this connection."""
        return self._conn.cursor()

    def execute(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> apsw.Cursor:
        """Execute a single SQL statement and return the cursor.

        Parameters
        ----------
        sql:
            SQL statement to execute.
        bindings:
            Optional positional bindings (``?`` placeholders).
        """
        cursor = self.get_cursor()
        if bindings is not None:
            cursor.execute(sql, bindings)
        else:
            cursor.execute(sql)
        return cursor

    def executemany(
        self,
        sql: str,
        seq_of_bindings: Iterator[Sequence[Any]] | Sequence[Sequence[Any]],
    ) -> None:
        """Execute *sql* once for every set of bindings in *seq_of_bindings*.

        Uses ``apsw.Cursor.executemany`` for efficient bulk inserts.
        """
        cursor = self.get_cursor()
        cursor.executemany(sql, seq_of_bindings)

    def fetchone(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> Optional[tuple[Any, ...]]:
        """Execute *sql* and return the first row, or ``None``."""
        cursor = self.execute(sql, bindings)
        return cursor.fetchone()  # type: ignore[return-value]

    def fetchall(self, sql: str, bindings: Optional[Sequence[Any]] = None) -> list[tuple[Any, ...]]:
        """Execute *sql* and return every row as a list of tuples."""
        cursor = self.execute(sql, bindings)
        return list(cursor)

    # -- Transaction helpers ------------------------------------------------

    def begin_transaction(self) -> None:
        """Begin an explicit transaction.

        Prefer using :meth:`transaction` as a context manager instead.
        Idempotent: if a transaction is already open, this is a no-op
        rather than letting APSW raise ``cannot start a transaction
        within a transaction``.
        """
        try:
            self.get_cursor().execute("BEGIN;")
        except Exception as exc:
            # APSW is strict: once a transaction is active, BEGIN
            # raises.  Treat that as harmless idempotency — the
            # caller's intent (have a writable transaction) is met.
            msg = str(exc).lower()
            if "within a transaction" in msg or "cannot start" in msg:
                return
            raise

    def commit(self) -> None:
        """Commit the current transaction.

        APSW raises ``cannot commit - no transaction is active`` when
        called outside an explicit BEGIN — for example after a DDL like
        ``CREATE TABLE`` that auto-commits.  We swallow that specific
        error so a no-op commit doesn't kill the surrounding stage;
        anything else still bubbles up.
        """
        try:
            self.get_cursor().execute("COMMIT;")
        except Exception as exc:
            msg = str(exc).lower()
            if "no transaction is active" in msg or "no transaction" in msg:
                return
            raise

    def rollback(self) -> None:
        """Roll back the current transaction.

        Same defensive treatment as :meth:`commit` — calling rollback
        when no transaction is active is harmless (nothing to undo).
        """
        try:
            self.get_cursor().execute("ROLLBACK;")
        except Exception as exc:
            msg = str(exc).lower()
            if "no transaction is active" in msg or "no transaction" in msg:
                return
            raise

    class _TransactionCtx:
        """Context manager returned by :meth:`AnalysisConnection.transaction`."""

        def __init__(self, conn: AnalysisConnection) -> None:
            self._conn = conn

        def __enter__(self) -> AnalysisConnection:
            self._conn.begin_transaction()
            return self._conn

        def __exit__(
            self,
            exc_type: Optional[type[BaseException]],
            exc_val: Optional[BaseException],
            exc_tb: Optional[TracebackType],
        ) -> None:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()

    def transaction(self) -> _TransactionCtx:
        """Return a context manager that wraps a BEGIN / COMMIT / ROLLBACK.

        Usage::

            with analysis_conn.transaction():
                analysis_conn.execute("INSERT INTO ...")
                analysis_conn.execute("INSERT INTO ...")
        """
        return self._TransactionCtx(self)

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying APSW connection.

        Safe to call multiple times.
        """
        try:
            self._conn.close()
            logger.debug("Closed analysis connection: %s", self._db_path.name)
        except apsw.ConnectionClosedError:
            pass

    # -- Context manager ----------------------------------------------------

    def __enter__(self) -> AnalysisConnection:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<AnalysisConnection path={self._db_path.name!r}>"


# ---------------------------------------------------------------------------
# DatabaseManager -- central registry
# ---------------------------------------------------------------------------

class DatabaseManager:
    """Central manager for all database connections.

    Provides named access to **source** databases (read-only, immutable) and
    the single **analysis.db** (read-write, WAL).  All public methods are
    thread-safe; the internal dictionary of connections is guarded by a
    :class:`threading.Lock`.

    Parameters
    ----------
    databases_path:
        Directory containing the extracted WhatsApp ``.db`` files
        (``msgstore.db``, ``wa.db``, etc.).
    analysis_db_path:
        Path to the ``analysis.db`` file (will be created if missing).
    mmap_size:
        Default ``mmap_size`` PRAGMA for source connections.
    cache_mb:
        Default page-cache size (MiB) for all connections.
    """

    def __init__(
        self,
        databases_path: Path,
        analysis_db_path: Path,
        mmap_size: int = 4_294_967_296,
        cache_mb: int = 64,
        extra_db_paths: dict[str, str] | None = None,
    ) -> None:
        self._databases_path = Path(databases_path)
        self._analysis_db_path = Path(analysis_db_path)
        self._mmap_size = mmap_size
        self._cache_mb = cache_mb
        self._lock = threading.Lock()
        self._source_connections: dict[str, SourceConnection] = {}
        self._analysis_conn: Optional[AnalysisConnection] = None
        # Explicit paths for individual databases (overrides databases_path lookup)
        self._extra_db_paths: dict[str, Path] = {}
        if extra_db_paths:
            for name, path in extra_db_paths.items():
                if path and os.path.isfile(path):
                    self._extra_db_paths[name] = Path(path)

    # -- Source accessors ----------------------------------------------------

    def get_source(self, db_name: str) -> SourceConnection:
        """Get or create a read-only :class:`SourceConnection` for *db_name*.

        The connection is cached so that subsequent calls for the same
        *db_name* return the same object.

        Parameters
        ----------
        db_name:
            Filename of the database (e.g. ``"msgstore.db"``).  Resolved
            relative to *databases_path*.

        Returns
        -------
        SourceConnection

        Raises
        ------
        FileNotFoundError
            If the requested database file does not exist.
        """
        with self._lock:
            if db_name in self._source_connections:
                return self._source_connections[db_name]
            # Check explicit path first, then default databases_path
            if db_name in self._extra_db_paths:
                db_path = self._extra_db_paths[db_name]
            else:
                db_path = self._databases_path / db_name
            conn = SourceConnection(
                db_path,
                mmap_size=self._mmap_size,
                cache_mb=self._cache_mb,
            )
            self._source_connections[db_name] = conn
            return conn

    def get_msgstore(self) -> SourceConnection:
        """Shorthand for ``get_source("msgstore.db")``."""
        return self.get_source("msgstore.db")

    def get_wa_db(self) -> SourceConnection:
        """Shorthand for ``get_source("wa.db")``."""
        return self.get_source("wa.db")

    def get_status_db(self) -> SourceConnection:
        """Shorthand for ``get_source("status.db")``."""
        return self.get_source("status.db")

    # -- Analysis accessor ---------------------------------------------------

    def get_analysis(self) -> AnalysisConnection:
        """Get or create the read-write :class:`AnalysisConnection`.

        Returns
        -------
        AnalysisConnection
        """
        with self._lock:
            if self._analysis_conn is not None:
                return self._analysis_conn
            self._analysis_conn = AnalysisConnection(
                self._analysis_db_path,
                cache_mb=self._cache_mb,
            )
            return self._analysis_conn

    # -- Introspection -------------------------------------------------------

    def list_open_sources(self) -> list[str]:
        """Return the names of all currently-open source connections."""
        with self._lock:
            return list(self._source_connections.keys())

    @property
    def databases_path(self) -> Path:
        """Root directory where source ``.db`` files reside."""
        return self._databases_path

    @property
    def analysis_db_path(self) -> Path:
        """Path to the analysis database."""
        return self._analysis_db_path

    # -- Lifecycle -----------------------------------------------------------

    def close_all(self) -> None:
        """Close **all** open connections (source *and* analysis).

        Thread-safe.  Safe to call multiple times.
        """
        with self._lock:
            for name, conn in self._source_connections.items():
                try:
                    conn.close()
                except Exception:
                    logger.exception("Error closing source connection: %s", name)
            self._source_connections.clear()

            if self._analysis_conn is not None:
                try:
                    self._analysis_conn.close()
                except Exception:
                    logger.exception("Error closing analysis connection")
                self._analysis_conn = None

        logger.info("All database connections closed.")

    def __repr__(self) -> str:
        n_src = len(self._source_connections)
        has_analysis = self._analysis_conn is not None
        return (
            f"<DatabaseManager sources={n_src} "
            f"analysis={'open' if has_analysis else 'closed'}>"
        )
