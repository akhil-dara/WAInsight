"""
Base lazy-loading table model for large SQLite datasets.

Implements QAbstractTableModel with fetchMore/canFetchMore pattern.
Sorting and filtering are done server-side via SQL.
Supports click-to-sort via sort() override.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from app.services.database import Database

BATCH_SIZE = 500


class BaseLazyTableModel(QAbstractTableModel):
    """Table model that loads data in batches from SQLite.

    Subclasses should set:
        _columns: list of (db_column, display_header) tuples
        _base_sql: base SELECT query (without LIMIT/OFFSET/ORDER BY)
        _count_sql: COUNT query matching _base_sql
        _default_order: default ORDER BY clause
    """

    _columns: list[tuple[str, str]] = []
    _base_sql: str = ""
    _count_sql: str = ""
    _default_order: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[tuple] = []
        self._total_rows: int = 0
        self._current_params: tuple = ()
        self._current_where: str = ""
        self._current_order: str = ""
        self._sort_column: int = -1
        self._sort_order: Qt.SortOrder = Qt.AscendingOrder
        self._db = Database.get()

    def load(self, where: str = "", params: tuple = (), order: str = "") -> None:
        """Load data with optional WHERE clause and ORDER BY."""
        self.beginResetModel()
        self._data.clear()
        self._current_where = where
        self._current_params = params
        self._current_order = order or self._default_order

        # Get total count
        count_sql = self._count_sql
        if where:
            count_sql += f" WHERE {where}"
        self._total_rows = self._db.scalar(count_sql, params) or 0

        # Fetch first batch
        self._fetch_batch()
        self.endResetModel()

    def clear(self) -> None:
        """Release loaded rows while keeping the last query settings."""
        self.beginResetModel()
        self._data.clear()
        self._total_rows = 0
        self.endResetModel()

    def _build_query(self) -> str:
        sql = self._base_sql
        if self._current_where:
            sql += f" WHERE {self._current_where}"
        if self._current_order:
            sql += f" ORDER BY {self._current_order}"
        return sql

    def _fetch_batch(self) -> None:
        if not self._base_sql:
            return  # No SQL set yet
        offset = len(self._data)
        sql = self._build_query() + " LIMIT ? OFFSET ?"
        batch_size = getattr(self, "_batch_size", BATCH_SIZE)
        params = self._current_params + (batch_size, offset)
        rows = self._db.fetchall(sql, params)
        self._data.extend(tuple(row) for row in rows)

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        return len(self._data) < self._total_rows

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:
        remainder = self._total_rows - len(self._data)
        batch_size = getattr(self, "_batch_size", BATCH_SIZE)
        to_fetch = min(batch_size, remainder)
        self.beginInsertRows(QModelIndex(), len(self._data),
                             len(self._data) + to_fetch - 1)
        self._fetch_batch()
        self.endInsertRows()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._data)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            val = self._data[index.row()][index.column()]
            return str(val) if val is not None else ""
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if 0 <= section < len(self._columns):
                header = self._columns[section][1]
                # Show sort indicator
                if section == self._sort_column:
                    arrow = " \u25B2" if self._sort_order == Qt.AscendingOrder else " \u25BC"
                    return header + arrow
                return header
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        """Sort by column via SQL ORDER BY (server-side sort)."""
        if not self._base_sql or not (0 <= column < len(self._columns)):
            return  # No SQL set yet or invalid column
        self._sort_column = column
        self._sort_order = order
        db_col = self._columns[column][0]
        direction = "ASC" if order == Qt.AscendingOrder else "DESC"
        self._current_order = f"{db_col} {direction}"

        self.beginResetModel()
        self._data.clear()
        self._fetch_batch()
        self.endResetModel()

    @property
    def total_rows(self) -> int:
        return self._total_rows
