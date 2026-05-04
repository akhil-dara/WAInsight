"""
Scheduled Events page -- shows scheduled calls/events from WhatsApp.
Reads from the scheduled_event table in analysis.db.
Double-click to navigate to the conversation where the event was created.
"""

from __future__ import annotations

from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableView, QTextEdit, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database


class ScheduledEventsModel(BaseLazyTableModel):
    _columns = [
        ("se.name", "Event Name"),
        ("se.start_time", "Start Time"),
        ("se.end_time", "End Time"),
        ("c.display_name", "Conversation"),
        ("se.is_schedule_call", "Type"),
        ("se.is_canceled", "Status"),
        ("se.location_name", "Location"),
    ]
    _base_sql = """
        SELECT se.name, se.start_time, se.end_time,
               c.display_name, se.is_schedule_call, se.is_canceled,
               se.location_name, se.id, se.description,
               se.conversation_id, se.location_address,
               se.join_link, se.allow_extra_guests
        FROM scheduled_event se
        LEFT JOIN conversation c ON c.id = se.conversation_id
    """
    _count_sql = "SELECT COUNT(*) FROM scheduled_event se"
    _default_order = "se.start_time DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col in (1, 2) and raw:
                try:
                    return format_timestamp(raw, "minute")
                except (ValueError, OSError):
                    return str(raw)
            if col == 4:
                return "Scheduled Call" if raw else "Event"
            if col == 5:
                return "\u274C Canceled" if raw else "\u2705 Active"
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            if col == 4:
                return QColor("#42a5f5") if row_data[4] else QColor("#66bb6a")
            if col == 5:
                return QColor("#ef5350") if row_data[5] else QColor("#66bb6a")

        if role == Qt.UserRole:
            return row_data[7]  # id

        # Conversation ID for navigation
        if role == Qt.UserRole + 1:
            return row_data[9] if len(row_data) > 9 else None

        # Conversation display_name for navigation
        if role == Qt.UserRole + 2:
            return row_data[3] or ""

        if role == Qt.ToolTipRole:
            parts = []
            desc = row_data[8] if len(row_data) > 8 else None
            if desc:
                parts.append(f"Description: {desc}")
            loc = row_data[6]
            if loc:
                parts.append(f"Location: {loc}")
            addr = row_data[10] if len(row_data) > 10 else None
            if addr:
                parts.append(f"Address: {addr}")
            link = row_data[11] if len(row_data) > 11 else None
            if link:
                parts.append(f"Join: {link}")
            guests = row_data[12] if len(row_data) > 12 else None
            if guests:
                parts.append("Extra guests allowed")
            return "\n".join(parts) if parts else None

        return None

    def get_full_event(self, row: int) -> dict | None:
        """Get full event data for a given row."""
        if 0 <= row < len(self._data):
            r = self._data[row]
            return {
                "name": r[0],
                "start_time": r[1],
                "end_time": r[2],
                "conv_name": r[3],
                "is_call": bool(r[4]),
                "is_canceled": bool(r[5]),
                "location": r[6],
                "id": r[7],
                "description": r[8] if len(r) > 8 else None,
                "conversation_id": r[9] if len(r) > 9 else None,
                "address": r[10] if len(r) > 10 else None,
                "join_link": r[11] if len(r) > 11 else None,
                "allow_extra_guests": bool(r[12]) if len(r) > 12 else False,
            }
        return None


class EventsPage(QWidget):
    conversation_selected = Signal(int, str)  # conv_id, display_name

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Scheduled Events")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #78909c; font-size: 12px;")
        header.addWidget(self._count_label)
        header.addStretch()
        layout.addLayout(header)

        hint = QLabel("Double-click an event to open its conversation  |  "
                       "Click to see details below")
        hint.setStyleSheet("color: #90a4ae; font-size: 10px;")
        layout.addWidget(hint)

        # Toolbar with filters
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._filter_btns: dict[str, QPushButton] = {}
        for fid, label in [("all", "All"), ("events", "Events"),
                           ("calls", "Scheduled Calls"), ("active", "Active"),
                           ("canceled", "Canceled")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet("""
                QPushButton { padding: 4px 14px; border-radius: 14px;
                              border: 1px solid rgba(128,128,128,0.18); font-size: 11px; }
                QPushButton:checked { background: rgba(0,188,212,0.2);
                                      border-color: #00bcd4; color: #00bcd4; }
                QPushButton:hover:!checked { background: rgba(128,128,128,0.08); }
            """)
            btn.clicked.connect(self._on_filter)
            if fid == "all":
                btn.setChecked(True)
            toolbar.addWidget(btn)
            self._filter_btns[fid] = btn
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Table
        self._model = ScheduledEventsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(34)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 140), (2, 140), (3, 180), (4, 120), (5, 100), (6, 150)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        layout.addWidget(self._table, 1)

        # Detail panel
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFixedHeight(120)
        self._detail.setStyleSheet("""
            QTextEdit { background: rgba(128,128,128,0.06);
                        border: 1px solid rgba(128,128,128,0.12);
                        border-radius: 6px; padding: 8px;
                        font-family: Consolas, monospace; font-size: 10px;
                        color: #546e7a; }
        """)
        self._detail.setPlaceholderText("Click an event to see details...")
        layout.addWidget(self._detail)

        # Signals
        self._table.clicked.connect(self._on_click)
        self._table.doubleClicked.connect(self._on_double_click)

        self._current_filter = "all"
        QTimer.singleShot(50, self._apply)

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        for k, b in self._filter_btns.items():
            b.setChecked(k == fid)
        self._current_filter = fid
        self._apply()

    def _apply(self):
        parts, params = [], []
        if self._current_filter == "events":
            parts.append("se.is_schedule_call = 0")
        elif self._current_filter == "calls":
            parts.append("se.is_schedule_call = 1")
        elif self._current_filter == "active":
            parts.append("se.is_canceled = 0")
        elif self._current_filter == "canceled":
            parts.append("se.is_canceled = 1")
        self._model.load(where=" AND ".join(parts), params=tuple(params))
        self._count_label.setText(f"{self._model.total_rows:,} events")

    def _on_click(self, index: QModelIndex):
        """Show event details in the detail panel."""
        event = self._model.get_full_event(index.row())
        if not event:
            return

        lines = []
        lines.append(f"Name:         {event['name']}")
        lines.append(f"Type:         {'Scheduled Call' if event['is_call'] else 'Event'}")
        lines.append(f"Status:       {'Canceled' if event['is_canceled'] else 'Active'}")
        lines.append(f"Conversation: {event['conv_name'] or 'N/A'}")

        if event.get("start_time"):
            try:
                lines.append(f"Start:        {format_timestamp(event['start_time'], "minute")}")
            except (ValueError, OSError):
                pass
        if event.get("end_time"):
            try:
                lines.append(f"End:          {format_timestamp(event['end_time'], "minute")}")
            except (ValueError, OSError):
                pass

        if event.get("description"):
            lines.append(f"Description:  {event['description']}")
        if event.get("location"):
            lines.append(f"Location:     {event['location']}")
        if event.get("address"):
            lines.append(f"Address:      {event['address']}")
        if event.get("join_link"):
            lines.append(f"Join Link:    {event['join_link']}")
        if event.get("allow_extra_guests"):
            lines.append("Extra Guests: Allowed")

        # Show participants from group_event_detail
        db = Database.get()
        participants = db.fetchall(
            "SELECT ged.participant_contact_id, ged.is_me_joined, "
            "COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') AS name "
            "FROM group_event_detail ged "
            "LEFT JOIN system_event se2 ON se2.id = ged.system_event_id "
            "LEFT JOIN contact c ON c.id = ged.participant_contact_id "
            "WHERE se2.message_id = (SELECT message_id FROM scheduled_event WHERE id = ?) "
            "LIMIT 20",
            (event["id"],),
        )
        if participants:
            names = [p[2] + (" (you)" if p[1] else "") for p in participants]
            lines.append(f"Participants: {', '.join(names)}")

        self._detail.setPlainText("\n".join(lines))

    def _on_double_click(self, index: QModelIndex):
        """Navigate to the conversation where this event belongs."""
        conv_id = self._model.data(index, Qt.UserRole + 1)
        conv_name = self._model.data(index, Qt.UserRole + 2)
        if conv_id:
            self.conversation_selected.emit(conv_id, conv_name or "")

    def refresh_for_timezone_change(self) -> None:
        """Reload after a global timezone change so cached
        formatted timestamps re-render in the new tz."""
        try:
            if hasattr(self, "_apply") and callable(getattr(self, "_apply")):
                self._apply()
        except Exception:
            pass

