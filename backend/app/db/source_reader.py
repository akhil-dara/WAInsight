"""
High-level read operations for source WhatsApp databases.

Provides structured access to msgstore.db, wa.db, and companion databases
without exposing raw SQL to higher layers.  All operations are read-only.

This module is a convenience layer on top of :class:`SourceConnection`; it
translates common forensic queries into method calls that return typed
Python objects rather than raw tuples.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator, Optional, Sequence

import apsw

from .connection import SourceConnection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------


@dataclass
class TableInfo:
    """Summary information about a single database table."""

    name: str
    row_count: int
    column_names: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"<TableInfo {self.name!r} rows={self.row_count}>"


@dataclass
class DatabaseStats:
    """Aggregate statistics for a source database."""

    db_name: str
    file_size_bytes: int
    page_size: int
    page_count: int
    tables: list[TableInfo] = field(default_factory=list)

    @property
    def file_size_mb(self) -> float:
        """Return the file size in MiB, rounded to two decimal places."""
        return round(self.file_size_bytes / (1024 * 1024), 2)

    @property
    def total_rows(self) -> int:
        """Total row count across all tables."""
        return sum(t.row_count for t in self.tables)


# ---------------------------------------------------------------------------
# SQL identifier validation
# ---------------------------------------------------------------------------

_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate and return a SQL identifier (table or column name).

    Raises :class:`ValueError` if *name* contains characters that could
    enable SQL injection.  Only standard ASCII identifiers are allowed.
    """
    if not _VALID_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid SQL identifier: {name!r}.  Only alphanumerics and "
            f"underscores are allowed, and the name must start with a letter "
            f"or underscore."
        )
    return name


def _quote_identifier(name: str) -> str:
    """Validate *name* and wrap it in double-quotes for safe interpolation."""
    return f'"{_validate_identifier(name)}"'


# ---------------------------------------------------------------------------
# WhatsApp schema version detection
# ---------------------------------------------------------------------------

# Known sentinel tables that indicate schema epoch. WhatsApp periodically
# restructures its schema; the presence or absence of specific tables is the
# most reliable heuristic for version detection.
_SCHEMA_MARKERS: list[tuple[str, int]] = [
    # (table_name, minimum_version)
    ("message_add_on",        90),   # Added around schema version 90+
    ("message_add_on_orphan", 85),
    ("message_ephemeral",     80),
    ("message_quoted",        70),
    ("message_future",        65),
    ("message_system",        60),
    ("message_media",         45),
    ("message_vcard",         40),
    ("message_link",          35),
    ("message_location",      30),
    ("message_thumbnail",     20),
    ("message_forwarded",     15),
    ("messages_fts_content",  10),   # FTS5 virtual table content
    ("messages",               1),   # Always present
]


# ---------------------------------------------------------------------------
# SourceReader
# ---------------------------------------------------------------------------

class SourceReader:
    """High-level read-only access to a source WhatsApp database.

    Wraps a :class:`SourceConnection` and exposes useful forensic queries as
    simple method calls.  SQL is never leaked to the caller.

    Parameters
    ----------
    conn:
        An already-open :class:`SourceConnection`.
    """

    def __init__(self, conn: SourceConnection) -> None:
        self._conn = conn

    # -- Accessors -----------------------------------------------------------

    @property
    def connection(self) -> SourceConnection:
        """The underlying source connection."""
        return self._conn

    # -- Schema introspection ------------------------------------------------

    def get_schema_version(self) -> int:
        """Detect the WhatsApp schema version heuristically.

        Returns an estimated integer version based on the presence of known
        sentinel tables.  Higher values indicate newer schemas.

        Returns
        -------
        int
            Estimated schema version (1 = oldest known, 90+ = very recent).
        """
        tables = set(self.get_table_list())
        version = 0
        for marker_table, marker_version in _SCHEMA_MARKERS:
            if marker_table in tables and marker_version > version:
                version = marker_version
        if version == 0:
            logger.warning(
                "Could not detect schema version for %s; "
                "no known marker tables found.",
                self._conn.path.name,
            )
        return version

    def get_table_list(self) -> list[str]:
        """Return a sorted list of all table names (including virtual tables).

        Returns
        -------
        list[str]
        """
        rows = self._conn.fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "ORDER BY name;"
        )
        return [row[0] for row in rows]

    def get_column_names(self, table_name: str) -> list[str]:
        """Return an ordered list of column names for *table_name*.

        Parameters
        ----------
        table_name:
            Name of the table to inspect.  Validated for safe interpolation.

        Returns
        -------
        list[str]
        """
        safe_name = _quote_identifier(table_name)
        rows = self._conn.fetchall(f"PRAGMA table_info({safe_name});")
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        return [row[1] for row in rows]

    def table_exists(self, table_name: str) -> bool:
        """Return ``True`` if *table_name* exists in the database."""
        row = self._conn.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?;",
            (table_name,),
        )
        return row is not None

    def get_row_count(self, table_name: str) -> int:
        """Return the exact row count of *table_name*.

        Parameters
        ----------
        table_name:
            Must be a valid SQL identifier.

        Returns
        -------
        int
        """
        safe_name = _quote_identifier(table_name)
        row = self._conn.fetchone(f"SELECT COUNT(*) FROM {safe_name};")
        return row[0] if row else 0

    def get_database_stats(self) -> DatabaseStats:
        """Gather comprehensive statistics for the source database.

        Includes file size, page metrics, and per-table row counts plus
        column lists.

        Returns
        -------
        DatabaseStats
        """
        db_path = self._conn.path

        # File size on disk.
        try:
            file_size = db_path.stat().st_size
        except OSError:
            file_size = 0

        # Page metrics.
        page_size_row = self._conn.fetchone("PRAGMA page_size;")
        page_size = page_size_row[0] if page_size_row else 0

        page_count_row = self._conn.fetchone("PRAGMA page_count;")
        page_count = page_count_row[0] if page_count_row else 0

        # Per-table info.
        tables: list[TableInfo] = []
        for tbl_name in self.get_table_list():
            try:
                row_count = self.get_row_count(tbl_name)
            except (apsw.SQLError, ValueError):
                # Virtual tables or corrupt tables may fail on COUNT(*).
                row_count = -1
            try:
                cols = self.get_column_names(tbl_name)
            except (apsw.SQLError, ValueError):
                cols = []
            tables.append(TableInfo(name=tbl_name, row_count=row_count, column_names=cols))

        return DatabaseStats(
            db_name=db_path.name,
            file_size_bytes=file_size,
            page_size=page_size,
            page_count=page_count,
            tables=tables,
        )

    # -- Batched iteration ---------------------------------------------------

    def iter_batched(
        self,
        table: str,
        columns: Optional[Sequence[str]] = None,
        *,
        batch_size: int = 50_000,
        where: Optional[str] = None,
        order_by: Optional[str] = None,
        bindings: Optional[Sequence[Any]] = None,
    ) -> Generator[list[tuple[Any, ...]], None, None]:
        """Yield rows from *table* in fixed-size batches.

        This is the preferred way to stream large tables (millions of rows)
        without loading everything into memory at once.

        Parameters
        ----------
        table:
            Table name.  Validated for safe interpolation.
        columns:
            Column names to SELECT.  ``None`` means ``*`` (all columns).
            Each name is validated individually.
        batch_size:
            Maximum rows per yielded batch.
        where:
            Optional ``WHERE`` clause **without** the ``WHERE`` keyword.
            Parameter placeholders (``?``) may be used; supply matching
            *bindings*.
        order_by:
            Optional ``ORDER BY`` clause **without** the ``ORDER BY`` keyword.
            Column names are **not** validated here -- use literal column
            names only.
        bindings:
            Positional bindings for *where* placeholders.

        Yields
        ------
        list[tuple[Any, ...]]
            A list of up to *batch_size* rows.  The last batch may be
            shorter.
        """
        safe_table = _quote_identifier(table)
        if columns:
            cols_sql = ", ".join(_quote_identifier(c) for c in columns)
        else:
            cols_sql = "*"

        parts = [f"SELECT {cols_sql} FROM {safe_table}"]
        if where:
            parts.append(f"WHERE {where}")
        if order_by:
            parts.append(f"ORDER BY {order_by}")

        sql = " ".join(parts) + ";"
        cursor = self._conn.execute(sql, bindings)

        batch: list[tuple[Any, ...]] = []
        for row in cursor:
            batch.append(row)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    # -- Raw execution -------------------------------------------------------

    def execute_raw(
        self,
        sql: str,
        bindings: Optional[Sequence[Any]] = None,
    ) -> list[tuple[Any, ...]]:
        """Execute an arbitrary read-only SQL statement.

        This is an escape hatch for queries that do not fit the higher-level
        API.  The caller is responsible for ensuring the SQL is read-only
        (the underlying connection has ``query_only=ON``).

        Parameters
        ----------
        sql:
            Any valid SQL statement.
        bindings:
            Optional positional bindings.

        Returns
        -------
        list[tuple[Any, ...]]
        """
        return self._conn.fetchall(sql, bindings)

    # -- Cross-database ATTACH -----------------------------------------------

    @staticmethod
    def attach_database(
        conn: SourceConnection,
        db_path: Path,
        alias: str,
    ) -> None:
        """Attach another database file to *conn* for cross-DB joins.

        This is commonly used to attach ``wa.db`` (contacts) to a
        ``msgstore.db`` connection so that messages and contacts can be
        joined without copying data.

        Parameters
        ----------
        conn:
            The :class:`SourceConnection` to attach to.
        db_path:
            Filesystem path to the database to attach.
        alias:
            Schema alias used in SQL (e.g. ``wa`` so you can write
            ``wa.wa_contacts``).

        Raises
        ------
        FileNotFoundError
            If *db_path* does not exist.
        ValueError
            If *alias* is not a valid identifier.

        Example
        -------
        ::

            reader = SourceReader(db_manager.get_msgstore())
            SourceReader.attach_database(
                reader.connection,
                Path("/data/wa.db"),
                "wa",
            )
            rows = reader.execute_raw(
                "SELECT m._id, w.display_name "
                "FROM message m "
                "JOIN wa.wa_contacts w ON m.key_remote_jid = w.jid;"
            )
        """
        db_path = Path(db_path)
        if not db_path.exists():
            raise FileNotFoundError(f"Cannot attach -- file not found: {db_path}")
        safe_alias = _validate_identifier(alias)
        resolved = str(db_path.resolve())
        conn.execute(f"ATTACH DATABASE ? AS \"{safe_alias}\";", (resolved,))
        logger.info("Attached %s as '%s' on %s", db_path.name, alias, conn.path.name)

    @staticmethod
    def detach_database(conn: SourceConnection, alias: str) -> None:
        """Detach a previously attached database.

        Parameters
        ----------
        conn:
            The connection the database was attached to.
        alias:
            The schema alias used in the original :meth:`attach_database`
            call.
        """
        safe_alias = _validate_identifier(alias)
        conn.execute(f'DETACH DATABASE "{safe_alias}";')
        logger.info("Detached '%s' from %s", alias, conn.path.name)

    # -- Convenience forensic queries ----------------------------------------

    def get_pragma(self, pragma_name: str) -> Any:
        """Read a single PRAGMA value from the database.

        Parameters
        ----------
        pragma_name:
            Name of the PRAGMA (e.g. ``"user_version"``, ``"page_size"``).

        Returns
        -------
        Any
            The scalar value, or ``None`` if the PRAGMA returns no rows.
        """
        _validate_identifier(pragma_name)
        row = self._conn.fetchone(f"PRAGMA {pragma_name};")
        return row[0] if row else None

    def get_index_list(self, table_name: str) -> list[str]:
        """Return index names for *table_name*.

        Parameters
        ----------
        table_name:
            Table to inspect.

        Returns
        -------
        list[str]
            Sorted list of index names.
        """
        safe_name = _quote_identifier(table_name)
        rows = self._conn.fetchall(f"PRAGMA index_list({safe_name});")
        # PRAGMA index_list columns: seq, name, unique, origin, partial
        return sorted(row[1] for row in rows)

    def sample_rows(
        self,
        table_name: str,
        limit: int = 10,
    ) -> list[tuple[Any, ...]]:
        """Return a small sample of rows from *table_name*.

        Useful for quick inspection during forensic triage.

        Parameters
        ----------
        table_name:
            Table to sample from.
        limit:
            Maximum number of rows to return.

        Returns
        -------
        list[tuple[Any, ...]]
        """
        safe_name = _quote_identifier(table_name)
        return self._conn.fetchall(f"SELECT * FROM {safe_name} LIMIT ?;", (limit,))

    def __repr__(self) -> str:
        return f"<SourceReader db={self._conn.path.name!r}>"
