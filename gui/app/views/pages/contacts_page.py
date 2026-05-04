"""
Contacts page — full contact list with search, filters, and
sorting.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMenu, QPushButton,
    QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.theme_manager import ThemeManager


AVATAR_ROLE = Qt.UserRole + 300

AVATAR_COLORS = [
    "#00897b", "#6a1b9a", "#c62828", "#1565c0",
    "#ef6c00", "#2e7d32", "#ad1457", "#4527a0",
]


CONTACT_INFO_ROLE = Qt.UserRole + 301


class ContactsModel(BaseLazyTableModel):
    """Model with all 10 data columns. The view controls which are visible."""
    _columns = [
        ("display_name", "Name \u25bc"),
        ("phone_number", "Phone"),
        ("wa_name", "WA Name"),
        ("msg_count", "Messages"),
        ("personal_msg", "DM"),
        ("group_msg", "Grp"),
        ("conv_count", "Chats"),
        ("c.status_text", "Status"),
        ("platform_estimate", "OS"),
        ("linked_devices", "Dev"),
    ]
    _base_sql = """
        SELECT CASE WHEN c.is_saved = 1 THEN
                    COALESCE(NULLIF(c.display_name,''), NULLIF(c.resolved_name,''), 'Unknown')
               ELSE
                    COALESCE(
                        NULLIF(c.business_name, ''),
                        NULLIF(c.resolved_name, ''),
                        CASE WHEN c.wa_name IS NOT NULL AND c.wa_name != '' THEN '~' || c.wa_name END,
                        CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != '' THEN c.phone_number END,
                        NULLIF(c.phone_jid,''), NULLIF(c.lid_jid,''), 'Unknown'
                    )
               END AS display_name,
               c.phone_number, c.wa_name,
               c.message_count AS msg_count,
               c.personal_msg_count AS personal_msg,
               c.group_msg_count AS group_msg,
               c.conversation_count AS conv_count,
               COALESCE(c.status_text, '') AS status_text,
               c.platform_estimate,
               COALESCE(c.linked_device_count, 0) AS linked_devices,
               c.id, c.avatar_blob, c.is_blocked, c.is_saved, c.is_business,
               COALESCE(c.is_meta_verified, 0) AS is_meta_verified
        FROM contact c
    """
    _count_sql = "SELECT COUNT(*) FROM contact c"
    _default_order = "msg_count DESC, display_name ASC"

    # Row layout: name(0), phone(1), wa_name(2), msg_count(3), personal_msg(4),
    # group_msg(5), conv_count(6), status_text(7), platform(8), linked_devices(9),
    # id(10), avatar(11), is_blocked(12), is_saved(13), is_business(14)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == 0:  # Name
                return str(row_data[0]) if row_data[0] else ""
            if col == 1:  # Phone
                return str(row_data[1]) if row_data[1] else ""
            if col == 2:  # WhatsApp Name
                return str(row_data[2]) if row_data[2] else ""
            if col == 3:  # Total messages
                val = row_data[3] or 0
                return f"{val:,}" if val else "0"
            if col == 4:  # Personal msgs
                val = row_data[4] or 0
                return f"{val:,}" if val else "0"
            if col == 5:  # Group msgs
                val = row_data[5] or 0
                return f"{val:,}" if val else "0"
            if col == 6:  # Chats
                val = row_data[6] or 0
                return f"{val:,}" if val else "0"
            if col == 7:  # Status
                return str(row_data[7]) if row_data[7] else ""
            if col == 8 and row_data[8]:  # Platform
                return str(row_data[8]).title()
            if col == 9:  # Linked devices
                val = row_data[9] or 0
                return str(val) if val else ""
            return ""

        if role == Qt.UserRole:
            if col in (3, 4, 5, 6):
                return row_data[col] or 0
            if col == 9:  # linked devices sortable
                return row_data[9] or 0
            return row_data[10]  # contact id

        if role == AVATAR_ROLE:
            return row_data[11] if len(row_data) > 11 else None

        if role == CONTACT_INFO_ROLE:
            return {
                "is_blocked": bool(row_data[12]) if len(row_data) > 12 else False,
                "is_saved": bool(row_data[13]) if len(row_data) > 13 else True,
                "is_business": bool(row_data[14]) if len(row_data) > 14 else False,
                "is_meta_verified": bool(row_data[15]) if len(row_data) > 15 else False,
            }

        if role == Qt.TextAlignmentRole and col in (3, 4, 5, 6, 9):
            return Qt.AlignRight | Qt.AlignVCenter

        if role == Qt.ForegroundRole:
            tm = ThemeManager.get()
            # Dim entire row when message count is 0
            msg_count = row_data[3] or 0
            if msg_count == 0 and col not in (0,):
                return QColor("#b0b8bc") if tm.is_light else QColor(255, 255, 255, 50)

            if col == 3:  # Total msg_count color
                val = row_data[3] or 0
                if val > 1000:
                    return QColor("#c62828") if tm.is_light else QColor("#ef5350")
                elif val > 100:
                    return QColor("#e65100") if tm.is_light else QColor("#ffa726")
                elif val > 0:
                    return QColor("#00897b") if tm.is_light else QColor("#00bcd4")
                return QColor("#a0aab0") if tm.is_light else QColor(255, 255, 255, 60)
            if col in (4, 5):  # Personal/Group counts
                val = row_data[col] or 0
                if val > 0:
                    return QColor("#1565c0") if tm.is_light else QColor("#42a5f5") if col == 4 \
                        else QColor("#2e7d32") if tm.is_light else QColor("#66bb6a")
                return QColor("#a0aab0") if tm.is_light else QColor(255, 255, 255, 60)
            if col == 6:  # Chats
                val = row_data[6] or 0
                if val > 0:
                    return QColor("#00897b") if tm.is_light else QColor("#00bcd4")
                return QColor("#a0aab0") if tm.is_light else QColor(255, 255, 255, 60)
            if col == 7:  # status_text - dim
                return QColor("#808888") if tm.is_light else QColor(180, 195, 200, 160)
            if col == 8:  # platform
                platform = row_data[8]
                if platform == "android":
                    return QColor("#66bb6a")
                elif platform == "iphone":
                    return QColor("#42a5f5")
            if col == 9:  # linked devices
                val = row_data[9] or 0
                if val >= 3:
                    return QColor("#e53935") if tm.is_light else QColor("#ef5350")
                elif val >= 1:
                    return QColor("#f57c00") if tm.is_light else QColor("#ffa726")
                return QColor("#a0aab0") if tm.is_light else QColor(255, 255, 255, 60)
        return None


class ContactCardDelegate(QStyledItemDelegate):
    """Card-style delegate: avatar + name + phone + stats in a card layout."""

    CARD_W = 280
    CARD_H = 110
    AVATAR_SIZE = 40
    MARGIN = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: dict[int, QPixmap | None] = {}
        self._is_light = ThemeManager.get().is_light

    def sizeHint(self, option, index):
        return QSize(self.CARD_W, self.CARD_H)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        row_data = index.model()._data[index.row()] if index.row() < len(index.model()._data) else None
        if not row_data:
            painter.restore()
            return

        name = str(row_data[0]) if row_data[0] else "Unknown"
        phone = str(row_data[1]) if row_data[1] else ""
        msg_count = row_data[3] or 0
        personal = row_data[4] or 0
        group = row_data[5] or 0
        chats = row_data[6] or 0
        platform = str(row_data[8]).title() if row_data[8] else ""
        contact_id = row_data[10] if len(row_data) > 10 else 0
        avatar_blob = row_data[11] if len(row_data) > 11 else None
        is_blocked = bool(row_data[12]) if len(row_data) > 12 else False
        is_saved = bool(row_data[13]) if len(row_data) > 13 else True
        is_business = bool(row_data[14]) if len(row_data) > 14 else False
        is_meta_verified = bool(row_data[15]) if len(row_data) > 15 else False

        lt = self._is_light
        rect = option.rect.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)

        # Card background
        from PySide6.QtWidgets import QStyle
        selected = option.state & QStyle.StateFlag.State_Selected
        if selected:
            card_bg = QColor("#e0f2f1") if lt else QColor("#1a3a2e")
            border_color = QColor("#00897b") if lt else QColor("#00bcd4")
        elif is_blocked:
            card_bg = QColor("#fce4ec") if lt else QColor("#2a1520")
            border_color = QColor("#ef9a9a") if lt else QColor("#c62828")
        else:
            card_bg = QColor("#ffffff") if lt else QColor("#1f2c34")
            border_color = QColor("#e0e4e8") if lt else QColor("#2a3942")

        painter.setPen(QPen(border_color, 1))
        painter.setBrush(card_bg)
        painter.drawRoundedRect(rect, 8, 8)

        # Avatar
        av_x = rect.x() + 10
        av_y = rect.y() + 10
        self._draw_avatar(painter, av_x, av_y, contact_id, name, avatar_blob)

        # Name (bold, elided)
        text_x = av_x + self.AVATAR_SIZE + 10
        text_w = rect.width() - self.AVATAR_SIZE - 30
        painter.setFont(QFont("Segoe UI", 10, QFont.Bold))
        name_color = QColor("#c62828") if is_blocked else (QColor("#111b21") if lt else QColor("#e9edef"))
        painter.setPen(name_color)
        elided = painter.fontMetrics().elidedText(name, Qt.ElideRight, text_w)
        painter.drawText(QRect(text_x, av_y - 2, text_w, 20), Qt.AlignLeft | Qt.AlignVCenter, elided)

        # Phone
        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor("#667781") if lt else QColor("#8696a0"))
        painter.drawText(QRect(text_x, av_y + 16, text_w, 16), Qt.AlignLeft | Qt.AlignVCenter,
                         f"+{phone}" if phone and not phone.startswith("+") else phone)

        # Badges row (below avatar)
        badge_y = rect.y() + 58
        badge_x = rect.x() + 10

        # Compact number formatting
        def _fmt(n):
            if n >= 100_000: return f"{n/1000:.0f}K"
            if n >= 10_000: return f"{n/1000:.1f}K"
            if n >= 1_000: return f"{n/1000:.1f}K"
            return str(n)

        # Stats chips — clamp to available width
        chips = []
        if personal > 0:
            chips.append(("#1565c0" if lt else "#42a5f5", f"DM {_fmt(personal)}"))
        if group > 0:
            chips.append(("#2e7d32" if lt else "#66bb6a", f"Grp {_fmt(group)}"))
        if chats > 0:
            chips.append(("#00897b" if lt else "#00bcd4", f"{chats} chats"))

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        max_badge_x = rect.right() - 10
        for color, text in chips:
            tw = painter.fontMetrics().horizontalAdvance(text) + 10
            if badge_x + tw > max_badge_x:
                break  # Don't overflow
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color).lighter(lt and 180 or 60))
            painter.drawRoundedRect(badge_x, badge_y, tw, 16, 3, 3)
            painter.setPen(QColor(color))
            painter.drawText(QRect(badge_x, badge_y, tw, 16), Qt.AlignCenter, text)
            badge_x += tw + 3

        # Status badge (top-right corner)
        tag_x = rect.right() - 8
        tag_y = rect.y() + 8
        painter.setFont(QFont("Segoe UI", 7))
        if is_blocked:
            painter.setPen(QColor("#c62828"))
            painter.drawText(QRect(tag_x - 45, tag_y, 45, 12), Qt.AlignRight, "\u26d4 Blocked")
        elif is_meta_verified:
            painter.setPen(QColor("#1da1f2"))
            painter.drawText(QRect(tag_x - 55, tag_y, 55, 12), Qt.AlignRight, "\u2713 Verified")
        elif is_business:
            painter.setPen(QColor("#1565c0"))
            painter.drawText(QRect(tag_x - 30, tag_y, 30, 12), Qt.AlignRight, "\U0001f4bc Biz")
        elif not is_saved:
            painter.setPen(QColor("#e65100"))
            painter.drawText(QRect(tag_x - 50, tag_y, 50, 12), Qt.AlignRight, "~ Unsaved")

        # Platform + device info (bottom-right)
        linked = row_data[9] or 0 if len(row_data) > 9 else 0
        info_parts = []
        if platform and platform.lower() != "none":
            if platform.lower() == "multi_device":
                info_parts.append(f"\U0001f4bb Multi ({linked})" if linked > 0 else "\U0001f4bb Multi")
            else:
                info_parts.append(f"\U0001f4f1{platform}")
        elif linked > 0:
            info_parts.append(f"\U0001f4bb {linked} dev")
        if info_parts:
            painter.setFont(QFont("Segoe UI", 7))
            painter.setPen(QColor("#888"))
            painter.drawText(QRect(rect.right() - 80, badge_y + 2, 72, 12), Qt.AlignRight, " ".join(info_parts))

        painter.restore()

    def _draw_avatar(self, painter, x, y, contact_id, name, blob):
        sz = self.AVATAR_SIZE
        if blob and len(blob) > 100 and contact_id not in self._cache:
            pxm = QPixmap()
            pxm.loadFromData(blob)
            if not pxm.isNull():
                # Cache the SCALED version to avoid scaling on every paint
                self._cache[contact_id] = pxm.scaled(
                    sz, sz, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            else:
                self._cache[contact_id] = None

        path = QPainterPath()
        path.addEllipse(float(x), float(y), float(sz), float(sz))
        painter.setClipPath(path)

        cached = self._cache.get(contact_id)
        if cached:
            painter.drawPixmap(x, y, cached)
        else:
            bg = QColor(AVATAR_COLORS[contact_id % len(AVATAR_COLORS)])
            painter.setBrush(bg)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(x, y, sz, sz)
            initials = "".join(w[0] for w in name.split()[:2] if w and w[0].isalpha())[:2].upper() or "?"
            painter.setPen(QColor("white"))
            painter.setFont(QFont("Segoe UI", 13, QFont.Bold))
            painter.drawText(QRect(x, y, sz, sz), Qt.AlignCenter, initials)

        painter.setClipping(False)


class ContactsPage(QWidget):
    contact_selected = Signal(int)  # contact_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._advanced_visible = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title = QLabel("Contacts")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(self._tm.header_label_style())
        header.addWidget(self._count_label)
        header.addStretch()

        # Sort dropdown
        from PySide6.QtWidgets import QComboBox
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Most Messages", "Name A-Z", "Most DMs", "Most Group"])
        self._sort_combo.setFixedHeight(30)
        self._sort_combo.setStyleSheet("font-size: 11px; padding: 0 8px;")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        header.addWidget(self._sort_combo)

        # Export button
        self._export_btn = QPushButton("\u21e9 Export CSV")
        self._export_btn.setFixedHeight(30)
        self._export_btn.setCursor(Qt.PointingHandCursor)
        self._export_btn.setStyleSheet(self._tm.export_btn_style())
        self._export_btn.clicked.connect(self._export_contacts)
        header.addWidget(self._export_btn)
        layout.addLayout(header)

        # Summary bar
        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(self._summary_style())
        layout.addWidget(self._summary_label)

        # Toolbar: search + filter chips
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("\U0001f50d  Search contacts...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)

        self._filter_btns: dict[str, QPushButton] = {}
        for fid, label in [("all", "All"), ("active", "\u2709 Active"),
                           ("saved", "\u2714 Saved"), ("business", "\U0001f4bc Business"),
                           ("blocked", "\u26d4 Blocked"), ("unsaved", "~ Unsaved")]:
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
        layout.addLayout(toolbar)

        # Card grid view
        self._model = ContactsModel()
        self._card_delegate = ContactCardDelegate()
        self._list = QListView()
        self._list.setModel(self._model)
        self._list.setItemDelegate(self._card_delegate)
        self._list.setViewMode(QListView.IconMode)
        self._list.setFlow(QListView.LeftToRight)
        self._list.setWrapping(True)
        self._list.setResizeMode(QListView.Adjust)
        self._list.setMovement(QListView.Static)
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setSpacing(4)
        self._list.setUniformItemSizes(True)
        self._list.setGridSize(QSize(ContactCardDelegate.CARD_W + 10,
                                      ContactCardDelegate.CARD_H + 10))
        lt = self._tm.is_light
        self._list.setStyleSheet(f"""
            QListView {{
                background: {"#f5f7fa" if lt else "#0b141a"};
                border: none;
            }}
            QListView::item {{
                border: none;
            }}
            QListView::item:selected {{
                background: transparent;
            }}
        """)
        layout.addWidget(self._list, 1)

        # Also keep a hidden table reference for sorting/export (reuses same model)
        self._table = self._list  # For compatibility with context menu/double-click

        # Double-click to open contact detail
        self._list.doubleClicked.connect(self._on_double_click)
        # Right-click context menu
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)

        # Search debounce
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._search.returnPressed.connect(self._apply)
        self._current_filter = "all"
        QTimer.singleShot(50, self._apply)

    # ---- Styles ----

    def _summary_style(self) -> str:
        if self._tm.is_light:
            return """
                QLabel {
                    color: #607080; font-size: 12px; padding: 4px 8px;
                    background: #f0f2f5; border-radius: 4px;
                }
            """
        return """
            QLabel {
                color: #8899a6; font-size: 12px; padding: 4px 8px;
                background: #1a2530; border-radius: 4px;
            }
        """

    # ---- Actions ----

    def _on_sort_changed(self, idx: int):
        orders = [
            "msg_count DESC, display_name ASC",
            "display_name ASC, msg_count DESC",
            "personal_msg DESC, display_name ASC",
            "group_msg DESC, display_name ASC",
        ]
        if 0 <= idx < len(orders):
            self._model._default_order = orders[idx]
            self._apply()

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        for k, b in self._filter_btns.items():
            b.setChecked(k == fid)
        self._current_filter = fid
        self._apply()

    def _apply(self):
        parts, params = [], []
        if self._current_filter == "saved":
            parts.append("c.is_saved = 1")
        elif self._current_filter == "unsaved":
            parts.append("c.is_saved = 0 AND c.is_whatsapp_user = 1")
        elif self._current_filter == "active":
            parts.append("c.message_count > 0")
        elif self._current_filter == "business":
            parts.append("c.is_business = 1")
        elif self._current_filter == "blocked":
            parts.append("c.is_blocked = 1")

        text = self._search.text().strip()
        if text:
            parts.append("(c.resolved_name LIKE ? OR c.phone_number LIKE ? OR c.wa_name LIKE ?)")
            params.extend([f"%{text}%", f"%{text}%", f"%{text}%"])

        self._model.load(where=" AND ".join(parts), params=tuple(params))
        total = self._model.total_rows
        self._count_label.setText(f"{total:,} contacts")

        # Build summary stats from DB (not from loaded page — that's incomplete)
        from app.services.database import Database
        try:
            db = Database.get()
            with_msgs = db.scalar("SELECT COUNT(*) FROM contact WHERE message_count > 0") or 0
            blocked = db.scalar("SELECT COUNT(*) FROM contact WHERE is_blocked = 1") or 0
            business = db.scalar("SELECT COUNT(*) FROM contact WHERE is_business = 1") or 0
        except Exception:
            with_msgs = blocked = business = 0
        self._summary_label.setText(
            f"{total:,} contacts  \u00b7  {with_msgs:,} with messages  "
            f"\u00b7  {blocked:,} blocked  \u00b7  {business:,} business"
        )

    def _on_double_click(self, index: QModelIndex):
        """Open contact detail when double-clicking a row."""
        contact_id = self._model.data(index, Qt.UserRole)
        if contact_id:
            self.contact_selected.emit(contact_id)

    def _show_context_menu(self, pos):
        index = self._list.indexAt(pos)
        if not index.isValid():
            return
        row_data = self._model._data[index.row()] if index.row() < len(self._model._data) else None
        if not row_data:
            return

        menu = QMenu(self)
        menu.setStyleSheet(self._tm.context_menu_style())

        name = str(row_data[0]) if row_data[0] else ""
        phone = str(row_data[1]) if row_data[1] else ""

        # Copy name
        if name:
            copy_name = menu.addAction("Copy Name")
            copy_name.triggered.connect(lambda: QApplication.clipboard().setText(name))

        # Copy phone
        if phone:
            copy_phone = menu.addAction("Copy Phone")
            copy_phone.triggered.connect(lambda: QApplication.clipboard().setText(phone))

        # View profile picture
        avatar_blob = row_data[11] if len(row_data) > 11 else None
        if avatar_blob and len(avatar_blob) > 100:
            view_dp = menu.addAction("View Profile Picture")
            view_dp.triggered.connect(lambda: self._show_dp(name, avatar_blob))

        # View detail
        contact_id = row_data[10] if len(row_data) > 10 else None
        if contact_id:
            view_detail = menu.addAction("View Contact Detail")
            view_detail.triggered.connect(lambda: self.contact_selected.emit(contact_id))

        menu.exec(self._list.mapToGlobal(pos))

    def _show_dp(self, name: str, blob: bytes):
        """Show profile picture in a popup dialog."""
        pxm = QPixmap()
        pxm.loadFromData(blob)
        if pxm.isNull():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Profile Picture - {name}")
        dlg.setFixedSize(400, 440)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        # Name label
        lbl_name = QLabel(name)
        f = QFont(); f.setPointSize(12); f.setBold(True); lbl_name.setFont(f)
        lbl_name.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_name)
        # Image
        scaled = pxm.scaled(360, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img_label = QLabel()
        img_label.setPixmap(scaled)
        img_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(img_label)
        # Size info
        info = QLabel(f"{pxm.width()} x {pxm.height()} pixels  |  {len(blob) // 1024} KB")
        info.setAlignment(Qt.AlignCenter)
        info.setStyleSheet("color: #808888; font-size: 10px;")
        layout.addWidget(info)
        dlg.exec()

    def _export_contacts(self):
        """Export visible contacts to CSV file."""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Contacts", "contacts.csv", "CSV Files (*.csv)"
        )
        if not file_path:
            return

        import csv
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "Phone", "WhatsApp Name", "Messages",
                                 "Personal", "Group", "Chats", "Status",
                                 "Platform", "Saved", "Blocked", "ID"])
                for row in self._model._data:
                    writer.writerow([
                        row[0] or "",
                        row[1] or "",
                        row[2] or "",
                        row[3] or 0,
                        row[4] or 0,
                        row[5] or 0,
                        row[6] or 0,
                        row[7] or "",
                        str(row[8]).title() if row[8] else "",
                        "Yes" if (len(row) > 13 and row[13]) else "No",  # is_saved
                        "Yes" if (len(row) > 12 and row[12]) else "No",  # is_blocked
                        row[10] if len(row) > 10 else "",  # contact id
                    ])
            self._count_label.setText(
                f"Exported {len(self._model._data):,} contacts to {file_path}"
            )
        except Exception as e:
            self._count_label.setText(f"Export failed: {e}")
