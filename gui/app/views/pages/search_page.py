"""
Search page — FTS5 full-text search across all messages.
Double-click a result to navigate to its conversation.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QTableView, QVBoxLayout, QWidget,
)

from app.config import format_timestamp
from app.models.base_table_model import BaseLazyTableModel


def _is_light() -> bool:
    try:
        from app.services.theme_manager import ThemeManager
        return ThemeManager.get().is_light
    except Exception:
        return False


class SearchResultsModel(BaseLazyTableModel):
    _columns = [
        ("snippet", "Match"),
        ("sender_name", "Sender"),
        ("conv_name", "Conversation"),
        ("timestamp", "Date"),
    ]

    _base_sql = ""
    _count_sql = ""
    _default_order = "m.timestamp DESC"

    def load_search(self, query: str) -> None:
        if not query.strip():
            self.beginResetModel()
            self._data.clear()
            self._total_rows = 0
            self.endResetModel()
            return

        # Escape FTS5 special chars
        safe_query = query.replace('"', '""')

        # Sender-resolution priority:
        # 1. Pre-computed m.rendered_sender (ingester fills this for owner /
        # bots / anonymous community messages)
        # 2. If from_me=1 AND no contact linked → "You" (owner-sent message
        # where sender_id is NULL — previously rendered 'Unknown')
        # 3. Meta AI phones (13135555xxxxx@... or similar) → "Meta AI"
        # 4. Regular contact resolution chain
        # 5. Fallback: 'Unknown'
        self._base_sql = f"""
            SELECT
                snippet(message_fts, 0, '>>>', '<<<', '...', 48) AS snippet,
                CASE
                    WHEN m.rendered_sender IS NOT NULL AND m.rendered_sender != ''
                        THEN m.rendered_sender
                    WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI'
                    WHEN m.from_me = 1 AND
                         (c.id IS NULL OR c.resolved_name IS NULL OR c.resolved_name = '')
                        THEN 'You'
                    ELSE COALESCE(
                        NULLIF(c.resolved_name, ''),
                        NULLIF(c.display_name, ''),
                        CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                             THEN '+' || c.phone_number END,
                        NULLIF(c.wa_name, ''),
                        CASE WHEN c.phone_jid IS NOT NULL AND c.phone_jid != ''
                             THEN REPLACE(c.phone_jid, '@s.whatsapp.net', '') END,
                        CASE WHEN m.is_bot_message = 1 THEN 'Meta AI' END,
                        CASE WHEN m.from_me = 1 THEN 'You' END,
                        'Unknown'
                    )
                END AS sender_name,
                COALESCE(conv.display_name, conv.jid_raw_string) AS conv_name,
                m.timestamp,
                m.id AS msg_id,
                m.conversation_id
            FROM message_fts fts
            JOIN message m ON m.id = fts.rowid
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN conversation conv ON conv.id = m.conversation_id
            WHERE message_fts MATCH '"{safe_query}"'
        """
        self._count_sql = f"""
            SELECT COUNT(*) FROM message_fts
            WHERE message_fts MATCH '"{safe_query}"'
        """

        self.beginResetModel()
        self._data.clear()
        self._current_where = ""
        self._current_params = ()
        self._current_order = self._default_order

        db = self._db
        self._total_rows = db.scalar(self._count_sql) or 0

        if self._total_rows > 0:
            sql = self._base_sql + f" ORDER BY {self._default_order} LIMIT 500"
            rows = db.fetchall(sql)
            self._data = [tuple(row) for row in rows]

        self.endResetModel()

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 0 and raw:
                # Format snippet: replace >>> <<< with markers
                text = str(raw)
                text = text.replace(">>>", "\u00BB ").replace("<<<", " \u00AB")
                return text
            if col == 3 and raw:
                return format_timestamp(raw, "datetime")
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            if col == 0:
                return QColor("#e0e0e0")
            if col == 2:
                return QColor("#00bcd4")

        if role == Qt.UserRole:
            return row_data[5]  # conversation_id
        if role == Qt.UserRole + 1:
            return row_data[4]  # message_id

        return None


class SearchPage(QWidget):
    conversation_requested = Signal(int, str)  # conv_id, name (legacy, unused)
    navigate_to_message = Signal(int, int)  # conv_id, msg_id

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Search Messages")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        # Search box
        search_frame = QFrame()
        _lt = _is_light()
        search_frame.setStyleSheet(
            "QFrame { background: #ffffff; border-radius: 8px; border: 1px solid #e0e0e0; }"
            if _lt else
            "QFrame { background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.08); }"
        )
        sf_layout = QVBoxLayout(search_frame)
        sf_layout.setContentsMargins(16, 12, 16, 12)
        sf_layout.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search across all messages (FTS5 full-text search)...")
        self._search.setFixedHeight(44)
        self._search.setClearButtonEnabled(True)
        search_font = QFont()
        search_font.setPointSize(13)
        self._search.setFont(search_font)
        sf_layout.addWidget(self._search)

        tip = QLabel("Full-text search powered by SQLite FTS5  |  Press Enter or wait to search")
        tip.setStyleSheet("color: #78909c; font-size: 10px;")
        sf_layout.addWidget(tip)
        layout.addWidget(search_frame)

        # Results count
        self._count_label = QLabel("Enter a search term")
        self._count_label.setStyleSheet("color: #78909c; font-size: 12px;")
        layout.addWidget(self._count_label)

        # Results table
        self._model = SearchResultsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(False)  # FTS5 results have custom ordering
        self._table.verticalHeader().setDefaultSectionSize(32)
        self._table.setWordWrap(True)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 140), (2, 180), (3, 140)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        layout.addWidget(self._table, 1)

        # Hint at bottom
        bottom_hint = QLabel("Double-click a result to open the conversation")
        bottom_hint.setStyleSheet("color: #78909c; font-size: 10px;")
        layout.addWidget(bottom_hint)

        # Connect signals
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._do_search)
        self._search.textChanged.connect(lambda: self._timer.start())
        self._search.returnPressed.connect(self._do_search)

        self._table.doubleClicked.connect(self._on_double_click)

    def _do_search(self):
        query = self._search.text().strip()
        if not query:
            self._count_label.setText("Enter a search term")
            self._model.load_search("")
            return

        self._count_label.setText("\u23F3 Searching...")
        self._model.load_search(query)
        count = self._model.total_rows
        if count > 0:
            self._count_label.setText(
                f"\u2705 {count:,} results for \"{query}\""
            )
        else:
            self._count_label.setText(
                f"\u274C No results for \"{query}\""
            )

    def _on_double_click(self, index):
        conv_id = self._model.data(index, Qt.UserRole)
        msg_id = self._model.data(index, Qt.UserRole + 1)
        if conv_id and msg_id:
            self.navigate_to_message.emit(conv_id, msg_id)
        elif conv_id:
            conv_name = self._model.data(
                self._model.index(index.row(), 2), Qt.DisplayRole
            )
            self.conversation_requested.emit(conv_id, conv_name or "")

    def refresh_for_timezone_change(self) -> None:
        self._table.viewport().update()
