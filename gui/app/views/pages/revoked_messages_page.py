"""
Revoked Messages page -- messages deleted by sender (is_revoked = 1)
with search, phone numbers, ghost recovery indicator, and conversation navigation.
"""

from __future__ import annotations

from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

_JOINS = """
    FROM message m
    LEFT JOIN contact c ON c.id = m.sender_id
    LEFT JOIN conversation conv ON conv.id = m.conversation_id
"""


class RevokedMessagesModel(BaseLazyTableModel):
    _columns = [
        ("text_content", "Message"),
        ("sender_name", "Sender"),
        ("phone_number", "Phone"),
        ("conv_name", "Conversation"),
        ("type_label", "Type"),
        ("recovered", "Recovered"),
        ("timestamp", "Time"),
    ]

    _base_sql = f"""
        SELECT COALESCE(m.text_content, '[revoked]') AS text_content,
               CASE WHEN m.from_me = 1 THEN 'You'
                    ELSE COALESCE(c.resolved_name, c.wa_name, c.phone_number,
                                  REPLACE(c.phone_jid, '@s.whatsapp.net', ''), 'Unknown')
               END AS sender_name,
               CASE WHEN m.from_me = 1 THEN ''
                    ELSE COALESCE(c.phone_number,
                                  REPLACE(c.phone_jid, '@s.whatsapp.net', ''), '')
               END AS phone_number,
               COALESCE(conv.display_name, conv.jid_raw_string) AS conv_name,
               m.type_label,
               (SELECT COUNT(*) FROM ghost_message gm2
                WHERE gm2.revoked_msg_id = m.id) AS recovered,
               m.timestamp,
               m.id, m.conversation_id,
               COALESCE(conv.display_name, conv.jid_raw_string, '#' || m.conversation_id) AS nav_conv_name
    {_JOINS}"""

    _count_sql = f"SELECT COUNT(*) {_JOINS}"
    _default_order = "m.timestamp DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            # Time -- human-readable timestamp
            if col == 6 and raw:
                try:
                    return format_timestamp(raw, "minute")
                except (ValueError, OSError):
                    pass
            # Phone -- add + prefix
            if col == 2 and raw:
                s = str(raw)
                if s and s[0].isdigit():
                    return f"+{s}"
                return s
            # Recovered -- ghost indicator
            if col == 5:
                return "\u2718 Yes" if raw else ""
            # Type -- prettify
            if col == 4 and raw:
                return str(raw).replace("_", " ").title()
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            if col == 5:  # Recovered
                return QColor("#66bb6a") if row_data[5] else QColor(100, 100, 100)
            if col == 2:  # Phone
                return QColor(148, 171, 184, 180)
            if col == 4:  # Type
                return QColor(148, 171, 184)

        if role == Qt.TextAlignmentRole:
            if col in (5, 6):
                return Qt.AlignRight | Qt.AlignVCenter

        return None


class RevokedMessagesPage(QWidget):
    conversation_selected = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Revoked Messages")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(self._tm.header_label_style())
        header.addWidget(self._count_label)
        header.addStretch()
        layout.addLayout(header)

        # Filter toolbar + search
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._filter_btns: dict[str, QPushButton] = {}
        for fid, label in [
            ("all", "All"), ("sent", "Sent by Me"), ("received", "Sent by Others"),
            ("recovered", "\u2718 Recovered"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet(self._tm.filter_btn_style())
            btn.clicked.connect(self._on_filter)
            if fid == "all":
                btn.setChecked(True)
            toolbar.addWidget(btn)
            self._filter_btns[fid] = btn
        toolbar.addStretch()

        # Search box
        self._search = QLineEdit()
        self._search.setPlaceholderText("\U0001F50D  Search revoked messages...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)
        layout.addLayout(toolbar)

        hint = QLabel(
            "Double-click to open chat  |  "
            "Right-click for options  |  \u2718 = original text recovered"
        )
        hint.setStyleSheet(self._tm.hint_label_style())
        layout.addWidget(hint)

        # Table
        self._model = RevokedMessagesModel()
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
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 130), (2, 110), (3, 170), (4, 90), (5, 80), (6, 130)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        # Hide internal columns
        for hcol in (7, 8, 9):
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
        parts = ["m.is_revoked = 1"]
        params = []

        # Search
        text = self._search.text().strip()
        if text:
            parts.append(
                "(m.text_content LIKE ? OR c.resolved_name LIKE ? "
                "OR c.wa_name LIKE ? OR c.phone_number LIKE ? "
                "OR conv.display_name LIKE ?)"
            )
            params.extend([f"%{text}%"] * 5)

        # Direction filter
        if self._current_filter == "sent":
            parts.append("m.from_me = 1")
        elif self._current_filter == "received":
            parts.append("m.from_me = 0")
        elif self._current_filter == "recovered":
            parts.append(
                "m.id IN (SELECT gm3.revoked_msg_id FROM ghost_message gm3)"
            )

        self._model.load(where=" AND ".join(parts), params=tuple(params))
        self._count_label.setText(f"{self._model.total_rows:,} revoked messages")

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
            conv_id = row_data[8]
            conv_name = row_data[9] or f"#{conv_id}"
            if conv_id:
                self.conversation_selected.emit(conv_id, conv_name)

    def _show_context_menu(self, pos):
        index = self._table.indexAt(pos)
        row_data = self._get_row_data(index)
        if not row_data:
            return
        menu = QMenu(self)
        menu.setStyleSheet(self._tm.context_menu_style())
        conv_id = row_data[8]
        conv_name = row_data[9] or f"#{conv_id}"
        if conv_id:
            go_chat = menu.addAction("\u2192  Go to Chat")
            go_chat.triggered.connect(
                lambda: self.conversation_selected.emit(conv_id, conv_name)
            )
            menu.addSeparator()

        msg_text = row_data[0] or ""
        if msg_text and msg_text != "[revoked]":
            copy_msg = menu.addAction("Copy Message")
            copy_msg.triggered.connect(
                lambda: QApplication.clipboard().setText(msg_text)
            )

        phone = row_data[2] or ""
        if phone:
            copy_phone = menu.addAction("Copy Phone")
            copy_phone.triggered.connect(
                lambda: QApplication.clipboard().setText(f"+{phone}" if phone[0].isdigit() else phone)
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

