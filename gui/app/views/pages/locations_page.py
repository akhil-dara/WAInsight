"""
Locations page -- shared locations with sender, conversation, live/static filtering,
search, and "Go to Chat" navigation.
"""

from __future__ import annotations

from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.theme_manager import ThemeManager

_JOINS = """
    FROM location l
    JOIN message m ON m.id = l.message_id
    LEFT JOIN contact c ON c.id = m.sender_id
    LEFT JOIN conversation conv ON conv.id = m.conversation_id
"""


class LocationsModel(BaseLazyTableModel):
    _columns = [
        ("sender_name", "Sender"),
        ("conv_name", "Conversation"),
        ("place_name", "Place"),
        ("place_address", "Address"),
        ("latitude", "Lat"),
        ("longitude", "Lon"),
        ("is_live", "Live"),
        ("live_duration", "Duration"),
        ("timestamp", "Time"),
        ("route_pts", "Route Pts"),
    ]

    _base_sql = f"""
        SELECT CASE
                 WHEN m.from_me = 1 THEN
                   COALESCE(
                     (SELECT cm.value FROM case_metadata cm WHERE cm.key = 'device_owner_name'),
                     'Device Owner'
                   ) || COALESCE(
                     ' (+' || (SELECT cm2.value FROM case_metadata cm2 WHERE cm2.key = 'device_owner_phone') || ')',
                     ''
                   )
                 ELSE COALESCE(c.resolved_name, c.wa_name, 'Unknown')
                   || CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                           THEN ' (+' || c.phone_number || ')'
                           WHEN c.phone_jid IS NOT NULL
                           THEN ' (' || REPLACE(c.phone_jid, '@s.whatsapp.net', '') || ')'
                           ELSE '' END
               END AS sender_name,
               COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
               COALESCE(l.place_name, '') AS place_name,
               COALESCE(l.place_address, '') AS place_address,
               l.latitude, l.longitude, l.is_live, l.live_duration,
               m.timestamp,
               l.id,
               m.conversation_id,
               COALESCE(conv.display_name, conv.jid_raw_string, '') AS nav_conv_name,
               l.thumbnail_blob,
               (SELECT COUNT(*) FROM location_point lp WHERE lp.location_id = l.id) AS route_point_count,
               m.id AS message_id,
               m.source_key_id
    {_JOINS}"""
    _count_sql = f"SELECT COUNT(*) {_JOINS}"
    _default_order = "m.timestamp DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        # Row: sender(0), conv(1), place(2), addr(3), lat(4), lon(5),
        # live(6), duration(7), timestamp(8), id(9), conv_id(10), nav_name(11),
        # thumbnail_blob(12), route_point_count(13), message_id(14), source_key_id(15)

        if role == Qt.DisplayRole:
            if col == 0:  # Sender
                return str(row_data[0]) if row_data[0] else "Unknown"
            if col == 1:  # Conversation
                return str(row_data[1]) if row_data[1] else ""
            if col == 2:  # Place
                return str(row_data[2]) if row_data[2] else "\u2014"
            if col == 3:  # Address
                return str(row_data[3]) if row_data[3] else ""
            if col == 4:  # Lat
                lat = row_data[4]
                if lat is not None:
                    return f"{float(lat):.6f}"
                return ""
            if col == 5:  # Lon
                lon = row_data[5]
                if lon is not None:
                    return f"{float(lon):.6f}"
                return ""
            if col == 6:  # Live
                return "\U0001F534 Live" if row_data[6] else ""
            if col == 7:  # Duration
                dur = row_data[7]
                if dur and dur > 0:
                    mins = int(dur) // 60 if int(dur) >= 60 else 0
                    secs = int(dur) % 60
                    if mins:
                        return f"{mins}m {secs}s"
                    return f"{secs}s"
                return ""
            if col == 8:  # Timestamp
                ts = row_data[8]
                if ts:
                    try:
                        return format_timestamp(ts, "minute")
                    except (ValueError, OSError):
                        pass
                return ""
            if col == 9:  # Route Points
                rpc = row_data[13] if len(row_data) > 13 else 0
                return str(rpc) if rpc and rpc > 0 else ""
            return ""

        if role == Qt.DecorationRole:
            if col == 2:  # Show map thumbnail in the Place column
                blob = row_data[12] if len(row_data) > 12 else None
                if blob and len(blob) > 50:
                    img = QImage.fromData(bytes(blob))
                    if not img.isNull():
                        return QPixmap.fromImage(img).scaled(
                            60, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation
                        )

        if role == Qt.TextAlignmentRole:
            if col in (4, 5):
                return Qt.AlignRight | Qt.AlignVCenter
            if col == 8:
                return Qt.AlignRight | Qt.AlignVCenter

        if role == Qt.ForegroundRole:
            _light = ThemeManager.get().is_light
            if col == 0:  # Sender
                return QColor("#111b21") if _light else QColor("#e9edef")
            if col == 1:  # Conversation
                return QColor("#00796b") if _light else QColor("#80cbc4")
            if col == 6 and row_data[6]:
                return QColor("#ef5350")
            if col in (4, 5):
                return QColor("#00796b") if _light else QColor("#80cbc4")
            if col == 8:
                return QColor("#546e7a") if _light else QColor(148, 171, 184)

        # Expose conversation_id for Go to Chat
        if role == Qt.UserRole:
            return row_data  # full row tuple

        return None


class LocationsPage(QWidget):
    conversation_selected = Signal(int, str)  # (conv_id, display_name)
    go_to_message = Signal(int, int)  # (conv_id, message_id) — navigate + scroll to msg

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Locations")
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
            ("all", "All"), ("live", "\U0001F534 Live"), ("static", "Static"),
            ("has_route", "\U0001F4CD Route Pts"),
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
        self._search.setPlaceholderText("\U0001F50D  Search sender, place, conversation...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)
        layout.addLayout(toolbar)

        hint = QLabel(
            "Click headers to sort  |  "
            "Double-click or right-click to open conversation"
        )
        hint.setStyleSheet(self._tm.hint_label_style())
        layout.addWidget(hint)

        # Table
        self._model = LocationsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(56)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.resizeSection(0, 220)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.resizeSection(1, 180)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)  # Place
        for col, w in [(3, 180), (4, 90), (5, 90), (6, 60), (7, 80), (8, 130), (9, 80)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        layout.addWidget(self._table, 1)

        # Context menu
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.doubleClicked.connect(self._on_double_click)

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
        where_parts: list[str] = []
        params: list = []

        if self._current_filter == "live":
            where_parts.append("l.is_live = 1")
        elif self._current_filter == "static":
            where_parts.append("(l.is_live = 0 OR l.is_live IS NULL)")
        elif self._current_filter == "has_route":
            where_parts.append(
                "EXISTS (SELECT 1 FROM location_point lp WHERE lp.location_id = l.id)"
            )

        # Search filter
        text = self._search.text().strip()
        if text:
            where_parts.append(
                "(c.resolved_name LIKE ? OR c.wa_name LIKE ? OR c.phone_number LIKE ? "
                "OR l.place_name LIKE ? OR l.place_address LIKE ? "
                "OR conv.display_name LIKE ?)"
            )
            params.extend([f"%{text}%"] * 6)

        self._model.load(where=" AND ".join(where_parts), params=tuple(params))
        self._count_label.setText(f"{self._model.total_rows:,} locations")

    def _on_double_click(self, index: QModelIndex):
        row_data = index.data(Qt.UserRole)
        if row_data:
            conv_id = row_data[10]
            msg_id = row_data[14] if len(row_data) > 14 else None
            conv_name = row_data[11] or f"Chat #{conv_id}"
            if conv_id and msg_id:
                self.go_to_message.emit(conv_id, msg_id)
            elif conv_id:
                self.conversation_selected.emit(conv_id, conv_name)

    def _on_context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row_data = index.data(Qt.UserRole)
        if not row_data:
            return

        menu = QMenu(self)
        conv_id = row_data[10]
        conv_name = row_data[11] or f"Chat #{conv_id}"

        msg_id = row_data[14] if len(row_data) > 14 else None
        if conv_id:
            act_chat = menu.addAction(f"\u2192  Go to Message in: {conv_name}")
            if msg_id:
                act_chat.triggered.connect(
                    lambda checked=False, c=conv_id, m=msg_id: self.go_to_message.emit(c, m)
                )
            else:
                act_chat.triggered.connect(
                    lambda checked=False, c=conv_id, n=conv_name: self.conversation_selected.emit(c, n)
                )
            menu.addSeparator()

        lat = row_data[4]
        lon = row_data[5]
        if lat is not None and lon is not None:
            act_maps = menu.addAction("Open in Google Maps")
            maps_url = f"https://www.google.com/maps?q={lat},{lon}"
            act_maps.triggered.connect(
                lambda: __import__('PySide6.QtGui', fromlist=['QDesktopServices']).QDesktopServices.openUrl(
                    __import__('PySide6.QtCore', fromlist=['QUrl']).QUrl(maps_url)
                )
            )

        # Copy coordinates
        if lat is not None and lon is not None:
            act_copy = menu.addAction("Copy Coordinates")
            act_copy.triggered.connect(
                lambda: __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication.clipboard().setText(
                    f"{lat}, {lon}"
                )
            )

        # Copy sender
        sender = row_data[0]
        if sender:
            act_sender = menu.addAction(f"Copy Sender: {sender}")
            act_sender.triggered.connect(
                lambda: __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication.clipboard().setText(
                    sender
                )
            )

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def refresh_for_timezone_change(self) -> None:
        """Reload after a global timezone change so cached
        formatted timestamps re-render in the new tz."""
        try:
            if hasattr(self, "_apply") and callable(getattr(self, "_apply")):
                self._apply()
        except Exception:
            pass

