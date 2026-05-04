"""
Edit History page -- message edit history with search, sorting,
conversation navigation, and bot message filtering.

Displays edit_history records joined to message for current text.
Bot messages (Meta AI streaming edits) are tagged and filterable.
"""

from __future__ import annotations

from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

# Shared JOIN clause used by both _base_sql and _count_sql so that WHERE
# clauses referencing m.* / c.* / conv.* columns work for both queries.
_JOINS = """
    FROM edit_history eh
    JOIN message m ON m.id = eh.message_id
    LEFT JOIN contact c ON c.id = m.sender_id
    LEFT JOIN conversation conv ON conv.id = m.conversation_id
"""


class EditHistoryModel(BaseLazyTableModel):
    _columns = [
        ("text_content", "Current Text"),
        ("original_text", "Original Text (V1)"),
        ("sender_name", "Sender"),
        ("conv_name", "Conversation"),
        ("version", "Version"),
        ("edited_timestamp", "Edited At"),
        ("is_bot", "Bot"),
        ("type_label", "Type"),
    ]

    _base_sql = f"""
        SELECT COALESCE(m.text_content, '[media]') AS text_content,
               COALESCE(eh.original_text, '') AS original_text,
               CASE WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI'
                    WHEN m.from_me = 1 AND m.sender_id IS NULL THEN 'You (Owner)'
                    ELSE COALESCE(c.resolved_name, c.wa_name,
                         '+' || c.phone_number,
                         REPLACE(c.phone_jid, '@s.whatsapp.net', '+'),
                         'Unknown') END AS sender_name,
               COALESCE(conv.display_name, conv.jid_raw_string) AS conv_name,
               eh.version, eh.edited_timestamp,
               COALESCE(m.is_bot_message, 0) AS is_bot,
               CASE WHEN COALESCE(m.is_bot_message, 0) = 1
                    THEN 'Bot / Meta AI' ELSE 'User' END AS type_label,
               eh.id, m.conversation_id,
               COALESCE(conv.display_name, conv.jid_raw_string, '#' || m.conversation_id) AS nav_conv_name
    {_JOINS}"""

    _count_sql = f"SELECT COUNT(*) {_JOINS}"
    _default_order = "eh.edited_timestamp DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            # Edited At -- human-readable timestamp (col 5)
            if col == 5 and raw:
                try:
                    return format_timestamp(raw, "minute")
                except (ValueError, OSError):
                    pass
            # Version -- show as "v1", "v2", etc. (col 4)
            if col == 4 and raw is not None:
                return f"v{raw}"
            # Bot column -- robot icon (col 6)
            if col == 6:
                return "\U0001F916" if raw else ""
            # Type column (col 7)
            if col == 7:
                return str(raw) if raw is not None else "User"
            # Original text — truncate for display
            if col == 1 and raw:
                return raw[:120] + "..." if len(raw) > 120 else raw
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            # Colour the Original Text column (col 1) differently
            if col == 1:
                return QColor("#c62828") if row_data[1] else QColor("#999999")
            # Colour the Type column (col 7)
            if col == 7:
                is_bot = row_data[6]
                return QColor("#ffa726") if is_bot else QColor("#66bb6a")
            # Colour the Bot column icon (col 6)
            if col == 6 and row_data[6]:
                return QColor("#ffa726")

        if role == Qt.TextAlignmentRole:
            if col in (4, 6):
                return Qt.AlignCenter | Qt.AlignVCenter

        return None


class EditHistoryPage(QWidget):
    conversation_selected = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("\u270F\uFE0F  Edit History")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(self._tm.header_label_style())
        header.addWidget(self._count_label)
        header.addStretch()
        layout.addLayout(header)

        # Filter toolbar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)
        self._filter_btns: dict[str, QPushButton] = {}
        for fid, label in [
            ("all", "All"),
            ("user", "User Edits Only"),
            ("bot", "\U0001F916 Bot Edits Only"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet(self._tm.filter_btn_style())
            btn.clicked.connect(self._on_filter)
            if fid == "all":
                btn.setChecked(True)
            filter_bar.addWidget(btn)
            self._filter_btns[fid] = btn
        filter_bar.addStretch()

        # Search box
        self._search = QLineEdit()
        self._search.setPlaceholderText("\U0001F50D  Search edit history...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        filter_bar.addWidget(self._search, 1)
        layout.addLayout(filter_bar)

        hint = QLabel(
            "Double-click to open chat  |  "
            "\"Original Text (V1)\" is recovered from WhatsApp FTS index (lowercase/tokenized)"
        )
        hint.setStyleSheet(self._tm.hint_label_style())
        layout.addWidget(hint)

        # Table
        self._model = EditHistoryModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(self._on_double_click)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)  # Current Text
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)  # Original Text
        for col, w in [(2, 140), (3, 180), (4, 65), (5, 140), (6, 45), (7, 110)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        # Hide internal columns (eh.id, conversation_id, nav_conv_name)
        for hcol in (8, 9, 10):
            self._table.setColumnHidden(hcol, True)
        layout.addWidget(self._table, 1)

        # Search debounce
        self._current_filter = "all"
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._search.returnPressed.connect(self._apply)
        QTimer.singleShot(50, self._apply)

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        for k, b in self._filter_btns.items():
            b.setChecked(k == fid)
        self._current_filter = fid
        self._apply()

    def _apply(self):
        parts, params = [], []

        # Text search
        text = self._search.text().strip()
        if text:
            parts.append(
                "(m.text_content LIKE ? OR c.resolved_name LIKE ? "
                "OR c.wa_name LIKE ? OR conv.display_name LIKE ?)"
            )
            params.extend([f"%{text}%"] * 4)

        # Bot / user filter
        if self._current_filter == "user":
            parts.append("COALESCE(m.is_bot_message, 0) = 0")
        elif self._current_filter == "bot":
            parts.append("COALESCE(m.is_bot_message, 0) = 1")

        self._model.load(where=" AND ".join(parts), params=tuple(params))
        self._count_label.setText(f"{self._model.total_rows:,} edit records")

    def _get_row_data(self, index: QModelIndex) -> tuple | None:
        if not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._model._data):
            return self._model._data[row]
        return None

    def _on_double_click(self, index: QModelIndex):
        row_data = self._get_row_data(index)
        if row_data:
            # Hidden cols: [8]=eh.id, [9]=conv_id, [10]=nav_conv_name
            conv_id = row_data[9]
            conv_name = row_data[10] or f"#{conv_id}"
            if conv_id:
                self.conversation_selected.emit(conv_id, conv_name)

    def _show_context_menu(self, pos):
        index = self._table.indexAt(pos)
        row_data = self._get_row_data(index)
        if not row_data:
            return
        menu = QMenu(self)
        menu.setStyleSheet(self._tm.context_menu_style())
        # Hidden cols: [8]=eh.id, [9]=conv_id, [10]=nav_conv_name
        conv_id = row_data[9]
        conv_name = row_data[10] or f"#{conv_id}"
        if conv_id:
            go_chat = menu.addAction("\u2192  Go to Chat")
            go_chat.triggered.connect(
                lambda: self.conversation_selected.emit(conv_id, conv_name)
            )
            menu.addSeparator()

        msg_text = row_data[0] or ""
        orig_text = row_data[1] or ""
        if msg_text:
            copy_msg = menu.addAction("Copy Current Text")
            copy_msg.triggered.connect(
                lambda: QApplication.clipboard().setText(msg_text)
            )
        if orig_text:
            copy_orig = menu.addAction("Copy Original Text (V1)")
            copy_orig.triggered.connect(
                lambda: QApplication.clipboard().setText(orig_text)
            )

        menu.exec(QCursor.pos())

    def refresh_for_timezone_change(self) -> None:
        """Reload after a global timezone change so cached
        formatted timestamps re-render in the new tz."""
        try:
            if hasattr(self, "_apply") and callable(getattr(self, "_apply")):
                self._apply()
        except Exception:
            pass

