"""
System Events page -- group changes, security events, admin actions, calls.

Enhanced with target resolution, phone numbers, event_data display, search,
and copyable fields.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from app.config import format_timestamp_with_utc
from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

_GROUP_CHANGE_TYPES = {
    "group_subject_changed", "group_name_changed",
    "group_description_changed",
    "group_link_reset", "group_auto_admin_restriction",
    "group_add_member_permission", "group_edit_permission",
    "group_send_message_permission", "group_join_permission",
    "group_add_permission_all", "group_add_permission_admins",
    "group_invite_permission", "message_pinned",
    "event_updated",
    "subgroup_removed", "subgroup_added",
    "subgroup_unlinked",
    "community_add_permission", "community_settings_changed",
}
_SECURITY_TYPES = {
    "security_code_changed", "e2e_encrypted",
    "contact_blocked", "business_meta_managed",
    "channel_privacy_notice", "meta_ai_disclaimer",
    "phone_number_privacy", "ai_disclaimer",
    "contact_card_shown",
}
_ADMIN_TYPES = {
    "admin_promoted", "community_admin_changed",
    "you_are_admin", "community_owner_changed",
}
_MEMBER_TYPES = {
    "participant_added", "participant_left",
    "participant_removed", "participant_joined_via_link",
    "membership_approval_request", "participant_joined_community",
    "community_joined", "community_group_joined",
    "participant_added_with_approval", "community_linked_group_join",
    "number_changed", "participant_joined_from_community",
    "you_were_added", "community_welcome_joined",
    "community_group_invite_joined", "community_auto_added",
}
_DISAPPEARING_TYPES = {
    "disappearing_timer_updated", "disappearing_timer_changed",
    "disappearing_messages_changed", "default_disappearing_timer",
}
_COMMUNITY_TYPES = {
    "community_or_group_created", "community_created",
    "community_welcome_joined", "community_settings_changed",
    "community_description_changed", "community_owner_changed",
    "community_linked_group_join", "community_group_joined",
    "community_add_permission", "community_auto_added",
    "community_group_invite_joined",
}

_JOINS = """
    FROM system_event se
    LEFT JOIN contact ac ON ac.id = se.actor_id
    LEFT JOIN contact tc ON tc.id = se.target_id
    LEFT JOIN conversation conv ON conv.id = se.conversation_id
"""


class SystemEventsModel(BaseLazyTableModel):
    _columns = [
        ("event_label", "Event"),
        ("actor_name", "Actor"),
        ("actor_phone", "Actor Phone"),
        ("target_name", "Target"),
        ("target_phone", "Target Phone"),
        ("conv_name", "Conversation"),
        ("event_data", "Data"),
        ("timestamp", "Time"),
    ]

    _base_sql = f"""
        SELECT se.event_label,
               COALESCE(ac.resolved_name, ac.wa_name, 'Unknown') AS actor_name,
               COALESCE(ac.phone_number,
                        REPLACE(ac.phone_jid, '@s.whatsapp.net', ''),
                        '') AS actor_phone,
               COALESCE(tc.resolved_name, tc.wa_name, '') AS target_name,
               COALESCE(tc.phone_number,
                        REPLACE(tc.phone_jid, '@s.whatsapp.net', ''),
                        '') AS target_phone,
               COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
               COALESCE(se.event_data, '') AS event_data,
               se.timestamp,
               se.id, se.conversation_id, se.message_id
    {_JOINS}"""
    _count_sql = f"SELECT COUNT(*) {_JOINS}"
    _default_order = "se.timestamp DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        # Row: event_label(0), actor_name(1), actor_phone(2), target_name(3),
        # target_phone(4), conv_name(5), event_data(6), timestamp(7),
        # id(8), conversation_id(9)

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 0:  # Event label - prettify
                label = str(raw) if raw else ""
                return label.replace("_", " ").title()
            if col == 7 and raw:  # Timestamp
                return format_timestamp_with_utc(raw, "datetime")
            if col == 1:  # Actor name
                name = str(raw) if raw else "Unknown"
                phone = row_data[2]
                if name == "Unknown" and not phone:
                    # Phone-owner events: blocked/unblocked, personal disappearing changes
                    event_label = row_data[0] or ""
                    if event_label in ("contact_blocked", "disappearing_timer_changed",
                                       "community_joined", "community_group_joined"):
                        return "You (phone owner)"
                    # System notices: no actor needed
                    if event_label in ("e2e_encrypted", "chat_system_notice",
                                       "meta_ai_disclaimer", "channel_privacy_notice",
                                       "phone_number_privacy", "business_meta_managed",
                                       "channel_created", "channel_deleted",
                                       "contact_profile_shared"):
                        return "System"
                if phone and name == "Unknown":
                    return f"+{phone}" if not phone.startswith("+") else phone
                return name
            if col == 3:  # Target name
                name = str(raw) if raw else ""
                phone = row_data[4]
                if not name and phone:
                    return f"+{phone}" if not phone.startswith("+") else phone
                return name
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            _light = ThemeManager.get().is_light
            if col == 0:  # Event label coloring
                event_label = row_data[0]
                if event_label in _GROUP_CHANGE_TYPES:
                    return QColor("#66bb6a")
                if event_label in _SECURITY_TYPES:
                    return QColor("#ef5350")
                if event_label in _ADMIN_TYPES:
                    return QColor("#42a5f5")
                if event_label in _MEMBER_TYPES:
                    return QColor("#ab47bc")
                if event_label in _DISAPPEARING_TYPES:
                    return QColor("#ff9800")
                if event_label in _COMMUNITY_TYPES:
                    return QColor("#26a69a")
            if col in (1, 3):  # Actor/target names
                return QColor("#111b21") if _light else QColor("#e9edef")
            if col in (2, 4):  # Phone numbers
                return QColor("#00796b") if _light else QColor("#80cbc4")
            if col == 5:  # Conversation
                return QColor("#00796b") if _light else QColor("#80cbc4")
            if col == 6:  # Event data
                return QColor("#667781") if _light else QColor(148, 171, 184, 180)
            if col == 7:  # Timestamp
                return QColor("#546e7a") if _light else None

        if role == Qt.TextAlignmentRole:
            if col == 7:
                return Qt.AlignRight | Qt.AlignVCenter

        if role == Qt.UserRole:
            return row_data

        return None


class EventDetailPanel(QFrame):
    """Right-side detail panel for selected system event."""

    go_to_chat_requested = Signal(int, int)  # conv_id, message_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self.setObjectName("eventDetailPanel")
        self.setStyleSheet(self._tm.detail_panel_style("eventDetailPanel"))
        self.setMinimumWidth(260)
        self.setMaximumWidth(340)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(4)

        title = QLabel("Event Details")
        tf = QFont(); tf.setPointSize(13); tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(self._tm.detail_title_style())
        self._layout.addWidget(title)

        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet(self._tm.detail_info_text_style())
        self._info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._layout.addWidget(self._info_label)

        # Go to Chat button
        self._go_btn = QPushButton("\u2192  Go to Chat")
        self._go_btn.setFixedHeight(32)
        self._go_btn.setCursor(Qt.PointingHandCursor)
        self._go_btn.setStyleSheet(self._tm.export_btn_style())
        self._go_btn.clicked.connect(self._emit_go_to_chat)
        self._go_btn.hide()
        self._layout.addWidget(self._go_btn)

        self._layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

        # Placeholder
        self._placeholder = QLabel("Select an event to view details")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(self._tm.detail_placeholder_style())
        outer.addWidget(self._placeholder)
        scroll.hide()
        self._scroll = scroll
        self._current_conv_id: int | None = None
        self._current_conv_name: str = ""
        self._current_msg_id: int = 0

    def _emit_go_to_chat(self):
        if self._current_conv_id:
            self.go_to_chat_requested.emit(self._current_conv_id, self._current_msg_id)

    def show_event(self, row_data):
        self._placeholder.hide()
        self._scroll.show()

        event_label = (row_data[0] or "").replace("_", " ").title()
        actor_name = row_data[1] or "Unknown"
        actor_phone = row_data[2] or ""
        target_name = row_data[3] or ""
        target_phone = row_data[4] or ""
        conv_name = row_data[5] or ""
        event_data = row_data[6] or ""
        timestamp = row_data[7]
        conv_id = row_data[9] if len(row_data) > 9 else None
        msg_id = row_data[10] if len(row_data) > 10 else 0

        # Resolve "Unknown" actor to phone owner or "System" for known types
        _PHONE_OWNER_EVENTS = {
            "contact_blocked", "disappearing_timer_changed",
            "community_joined", "community_group_joined",
            "you_were_added", "community_welcome_joined",
            "community_group_invite_joined", "community_auto_added",
        }
        _SYSTEM_EVENTS = {
            "e2e_encrypted", "contact_card_shown", "meta_ai_disclaimer",
            "channel_privacy_notice", "phone_number_privacy",
            "business_meta_managed", "channel_created", "channel_deleted",
            "ai_disclaimer", "community_settings_changed",
        }
        if actor_name == "Unknown" and not actor_phone:
            if event_label in _PHONE_OWNER_EVENTS:
                actor_name = "You (phone owner)"
            elif event_label in _SYSTEM_EVENTS:
                actor_name = "System"

        self._current_conv_id = conv_id
        self._current_conv_name = conv_name or f"Chat #{conv_id}"
        self._current_msg_id = msg_id or 0
        self._go_btn.setVisible(bool(conv_id))

        ts_str = ""
        if timestamp:
            ts_str = format_timestamp_with_utc(timestamp, "full")

        lines = []
        lines.append(f"<b style='color:{self._tm.accent_highlight_color()}; font-size:14px'>{event_label}</b>")
        lines.append(f"<br/><b>Time:</b> {ts_str}")
        lines.append(f"<b>Conversation:</b> {conv_name}")
        lines.append("")
        lines.append(f"<b>Actor:</b> {actor_name}")
        if actor_phone:
            lines.append(f"<b>Actor Phone:</b> +{actor_phone}" if not actor_phone.startswith("+") else f"<b>Actor Phone:</b> {actor_phone}")
        if target_name:
            lines.append(f"<b>Target:</b> {target_name}")
        if target_phone:
            lines.append(f"<b>Target Phone:</b> +{target_phone}" if not target_phone.startswith("+") else f"<b>Target Phone:</b> {target_phone}")
        if event_data:
            lines.append(f"<br/><b>Event Data:</b><br/><span style='color: {self._tm.accent_highlight_color()}'>{event_data}</span>")

        self._info_label.setText("<br/>".join(lines))

    def clear(self):
        self._placeholder.show()
        self._scroll.hide()
        self._go_btn.hide()


class SystemEventsPage(QWidget):
    conversation_selected = Signal(int, str)      # legacy: conv_id, name
    navigate_to_message = Signal(int, int)         # conv_id, message_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("\u26A0\uFE0F  System Events")
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
            ("all", "All"), ("group", "Group Changes"), ("security", "\U0001F512 Security"),
            ("admin", "Admin"), ("members", "Members"), ("calls", "Calls"),
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
        self._search.setPlaceholderText("\U0001F50D  Search events, actors, targets...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)
        layout.addLayout(toolbar)

        hint = QLabel(
            "Double-click to open chat  |  "
            "Right-click for options  |  Click row for event details"
        )
        hint.setStyleSheet(self._tm.hint_label_style())
        layout.addWidget(hint)

        # Splitter: Table + Detail panel
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        # Table
        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)

        self._model = SystemEventsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(30)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.resizeSection(0, 160)
        for col, w in [(1, 130), (2, 110), (3, 120), (4, 110), (5, 150), (6, 140), (7, 130)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)  # Conversation

        tc_layout.addWidget(self._table)
        splitter.addWidget(table_container)

        # Detail panel
        self._detail_panel = EventDetailPanel()
        splitter.addWidget(self._detail_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, 1)

        # Context menu + navigation
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.clicked.connect(self._on_row_click)
        self._table.doubleClicked.connect(self._on_double_click)
        self._detail_panel.go_to_chat_requested.connect(
            lambda cid, mid: self.navigate_to_message.emit(cid, mid)
        )

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

        if self._current_filter == "group":
            labels = "','".join(_GROUP_CHANGE_TYPES)
            parts.append(f"se.event_label IN ('{labels}')")
        elif self._current_filter == "security":
            labels = "','".join(_SECURITY_TYPES)
            parts.append(f"se.event_label IN ('{labels}')")
        elif self._current_filter == "admin":
            labels = "','".join(_ADMIN_TYPES)
            parts.append(f"se.event_label IN ('{labels}')")
        elif self._current_filter == "members":
            labels = "','".join(_MEMBER_TYPES)
            parts.append(f"se.event_label IN ('{labels}')")
        elif self._current_filter == "calls":
            parts.append("se.event_label LIKE '%call%'")

        # Search
        text = self._search.text().strip()
        if text:
            parts.append(
                "(se.event_label LIKE ? OR ac.resolved_name LIKE ? OR ac.phone_number LIKE ? "
                "OR tc.resolved_name LIKE ? OR tc.phone_number LIKE ? "
                "OR conv.display_name LIKE ? OR se.event_data LIKE ?)"
            )
            params.extend([f"%{text}%"] * 7)

        self._model.load(where=" AND ".join(parts), params=tuple(params))
        self._count_label.setText(f"{self._model.total_rows:,} system events")
        self._detail_panel.clear()

    def _on_row_click(self, index: QModelIndex):
        row_data = index.data(Qt.UserRole)
        if row_data:
            self._detail_panel.show_event(row_data)

    def _on_double_click(self, index: QModelIndex):
        row_data = index.data(Qt.UserRole)
        if not row_data:
            return
        conv_id = row_data[9]
        msg_id = row_data[10] if len(row_data) > 10 else 0
        if conv_id:
            self.navigate_to_message.emit(conv_id, msg_id or 0)

    def _on_context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row_data = index.data(Qt.UserRole)
        if not row_data:
            return

        menu = QMenu(self)
        conv_id = row_data[9]
        conv_name = row_data[5] or f"Chat #{conv_id}"

        msg_id = row_data[10] if len(row_data) > 10 else 0
        if conv_id:
            act_chat = menu.addAction(f"\u2192  Go to Message in Chat: {conv_name}")
            act_chat.triggered.connect(
                lambda: self.navigate_to_message.emit(conv_id, msg_id or 0)
            )
            menu.addSeparator()

        # Copy actor info
        actor = row_data[1] or "Unknown"
        actor_phone = row_data[2] or ""
        act_actor = menu.addAction(f"Copy Actor: {actor} {actor_phone}")
        act_actor.triggered.connect(
            lambda: QApplication.clipboard().setText(f"{actor} {actor_phone}".strip())
        )

        # Copy target info
        target = row_data[3] or ""
        target_phone = row_data[4] or ""
        if target or target_phone:
            act_target = menu.addAction(f"Copy Target: {target} {target_phone}")
            act_target.triggered.connect(
                lambda: QApplication.clipboard().setText(f"{target} {target_phone}".strip())
            )

        # Copy event data
        event_data = row_data[6] or ""
        if event_data:
            act_data = menu.addAction("Copy Event Data")
            act_data.triggered.connect(
                lambda: QApplication.clipboard().setText(event_data)
            )

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def refresh_for_timezone_change(self) -> None:
        self._table.viewport().update()
        idx = self._table.currentIndex()
        if idx.isValid():
            row_data = idx.data(Qt.UserRole)
            if row_data:
                self._detail_panel.show_event(row_data)
