"""
Calls page -- call records with filtering, sorting, stats, and detail panel.

Enhanced with:
- Direction column (incoming/outgoing arrows)
- Bytes transferred column with human-readable formatting
- Right-side detail panel with full call info + participants
- Extended stats: longest call, total duration, outgoing vs incoming count
- Incoming / Outgoing filter buttons
"""

from __future__ import annotations

from PySide6.QtCore import QDate, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCalendarWidget, QDateEdit, QFrame, QHBoxLayout,
    QHeaderView, QLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStyledItemDelegate,
    QStyle, QStyleOptionViewItem, QTableView, QVBoxLayout, QWidget,
)

from app.config import CALL_RESULT_LABELS, format_timestamp, qdate_range_to_timestamps, timestamp_to_qdate
from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CallCountCalendar(QCalendarWidget):
    """``QCalendarWidget`` with a flight-fare-style per-day count badge.

    Each day cell renders the call count for that day directly underneath
    the day number, and the cell background is tinted in proportion to
    that count (so analysts can scan at-a-glance for high-activity
    periods).  Days with zero calls render normally so the calendar
    stays readable.

    The count map is supplied as ``{QDate: int}`` via ``set_counts(...)``
    — which the calls page recomputes once after every data load.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._counts: dict[QDate, int] = {}
        self._max_count: int = 1
        # Hide the year-/month-pick row's frame artefacts that bleed
        # under the QSS hover backgrounds.
        self.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setGridVisible(False)

    def set_counts(self, counts: dict[QDate, int]) -> None:
        """Replace the date→count map and trigger a repaint."""
        self._counts = counts or {}
        self._max_count = max(self._counts.values()) if self._counts else 1
        self.updateCells()

    # NOTE: Qt routes per-cell painting through ``paintCell`` for the
    # day grid view; we override it to draw the count badge + tint.
    def paintCell(self, painter: QPainter, rect: QRect, date: QDate) -> None:
        cnt = self._counts.get(date, 0)
        is_other_month = (date.month() != self.monthShown()
                          or date.year() != self.yearShown())

        # Background tint — five steps from "no activity" → "very high".
        # Done before super().paintCell so the framework still draws the
        # day number on top.
        if cnt > 0 and not is_other_month:
            ratio = cnt / max(1, self._max_count)
            if ratio >= 0.8:
                bg = QColor(0, 137, 123, 110)
            elif ratio >= 0.5:
                bg = QColor(0, 137, 123, 78)
            elif ratio >= 0.25:
                bg = QColor(0, 137, 123, 50)
            elif ratio >= 0.1:
                bg = QColor(0, 137, 123, 30)
            else:
                bg = QColor(0, 137, 123, 18)
            painter.save()
            painter.fillRect(rect.adjusted(1, 1, -1, -1), bg)
            painter.restore()

        # Let the framework paint the day number itself + selection state
        super().paintCell(painter, rect, date)

        # Now overlay the count badge (skip zero / other-month cells)
        if cnt <= 0 or is_other_month:
            return
        painter.save()
        try:
            font = painter.font()
            font.setPointSize(7)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#00695c"))
            text = (
                f"{cnt}" if cnt < 1000 else
                f"{cnt // 1000}.{(cnt % 1000) // 100}k" if cnt < 100_000 else
                f"{cnt // 1000}k"
            )
            metrics = QFontMetrics(font)
            text_w = metrics.horizontalAdvance(text)
            x = rect.right() - text_w - 4
            y = rect.bottom() - 3
            # Subtle pill background so the badge stays legible over
            # the cell tint and the day-number ink.
            pad = 2
            badge_rect = QRect(
                x - pad, y - metrics.ascent() + 1,
                text_w + 2 * pad, metrics.height() - 1
            )
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(QColor(255, 255, 255, 220))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(badge_rect, 5, 5)
            painter.setPen(QColor("#00695c"))
            painter.drawText(x, y, text)
        finally:
            painter.restore()


class WrapLayout(QLayout):
    """Simple flow layout so filter chips wrap instead of clipping."""

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 8, v_spacing: int = 8):
        super().__init__(parent)
        self._items = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if line_height > 0 and next_x - self._h_spacing > effective.right() and x > effective.x():
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        total = (y - rect.y()) + line_height + margins.bottom()
        return max(total, 0)


def _call_scope_label(call_category: str | None, is_group_call: bool | int | None = None) -> str:
    cat = (call_category or "").strip()
    if not cat:
        cat = "group_call" if is_group_call else "personal"
    return {
        "voice_chat": "Voice Chat",
        "group_call": "Group",
        "multi_person": "Multi-Person",
        "personal": "Personal",
    }.get(cat, cat.replace("_", " ").title())

def _fmt_duration(seconds: int | None) -> str:
    """Format seconds into a human-readable duration string."""
    if not seconds:
        return "0s"
    s = int(seconds)
    if s >= 3600:
        h, remainder = divmod(s, 3600)
        m, sec = divmod(remainder, 60)
        return f"{h}h {m}m {sec}s"
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s"
    return f"{s}s"


def _fmt_bytes(b: int | None) -> str:
    """Format bytes into a human-readable size string."""
    if not b:
        return ""
    val = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            if unit == "B":
                return f"{int(val)} {unit}"
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _fmt_timestamp(ts: int | None) -> str:
    """Format a millisecond timestamp to a readable date/time string."""
    if not ts:
        return ""
    return format_timestamp(ts, "datetime")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class CallsModel(BaseLazyTableModel):
    """Lazy-loading model for call_record table.

    Columns selected into each row tuple (by index):
        0  contact_name    (resolved from contact table)
        1  from_me         (0/1 - direction)
        2  is_video        (0/1 - voice/video)
        3  result_label    (text)
        4  duration_sec    (int)
        5  timestamp       (int, ms)
        6  bytes_transferred (int)
        7  is_group_call   (0/1)
        8  id              (call_record PK, hidden - used for detail lookup)
        9  contact_id      (hidden - for detail panel)
        10 conversation_id (hidden)
    """

    _columns = [
        ("contact_name", "Contact"),
        ("phone_number", "Phone"),
        ("cr.from_me", "Direction"),
        ("cr.is_video", "Type"),
        ("cr.result_label", "Result"),
        ("cr.duration_sec", "Duration"),
        ("cr.timestamp", "Date / Time"),
        ("participant_count", "Participants"),
        ("call_category", "Scope"),
    ]

    _base_sql = """
        SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') AS contact_name,
               COALESCE(c.phone_number,
                        REPLACE(c.phone_jid, '@s.whatsapp.net', ''),
                        REPLACE(c.lid_jid, '@lid', ''),
                        '') AS phone_number,
               cr.from_me, cr.is_video, cr.result_label, cr.duration_sec,
               cr.timestamp,
               (SELECT COUNT(*) FROM call_participant cp WHERE cp.call_id = cr.id) AS participant_count,
               cr.is_group_call,
               cr.id, cr.contact_id, cr.conversation_id, cr.bytes_transferred,
               COALESCE(conv.display_name, '') AS group_name,
               COALESCE(c.phone_jid, c.lid_jid, '') AS contact_jid,
               c.avatar_blob,
               COALESCE(cr.call_category, 'personal') AS call_category,
               cr.creator_jid,
               COALESCE(cc.resolved_name, cc.wa_name, cc.phone_number, cr.creator_jid) AS creator_name,
               cr.creator_device_type
        FROM call_record cr
        LEFT JOIN contact c ON c.id = cr.contact_id
        LEFT JOIN conversation conv ON conv.id = cr.group_conversation_id
        LEFT JOIN contact cc ON cc.id = cr.creator_contact_id
    """
    _count_sql = (
        "SELECT COUNT(*) FROM call_record cr "
        "LEFT JOIN contact c ON c.id = cr.contact_id"
    )
    _default_order = "cr.timestamp DESC"

    # ---- display data ----
    # Row indices: 0=contact_name, 1=phone_number, 2=from_me, 3=is_video,
    # 4=result_label, 5=duration_sec, 6=timestamp, 7=participant_count,
    # 8=is_group_call, 9=id, 10=contact_id, 11=conversation_id,
    # 12=bytes_transferred, 13=group_name, 14=contact_jid, 15=avatar_blob,
    # 16=call_category, 17=creator_jid, 18=creator_name, 19=creator_device_type

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 0:  # Contact name only (group name shown in detail panel)
                return str(raw) if raw else "Unknown"
            if col == 1:  # Phone number
                phone = str(raw) if raw else ""
                if phone and not phone.startswith("+"):
                    return f"+{phone}"
                return phone
            if col == 2:  # Direction
                return "Outgoing" if raw else "Incoming"
            if col == 3:  # Type
                category = row_data[16] if len(row_data) > 16 else "personal"
                if category == "voice_chat":
                    return "Voice Chat"
                return "Video" if raw else "Voice"
            if col == 4:  # Result
                label = CALL_RESULT_LABELS.get(raw, str(raw) if raw is not None else "")
                return label.title() if label else ""
            if col == 5:  # Duration
                return _fmt_duration(raw)
            if col == 6:  # Date
                return _fmt_timestamp(raw)
            if col == 7:  # Participant count
                cnt = raw or 0
                return str(cnt) if cnt > 0 else ""
            if col == 8:  # Scope
                return _call_scope_label(row_data[16] if len(row_data) > 16 else "", raw)
            return str(raw) if raw is not None else ""

        if role == Qt.ToolTipRole:
            if col == 0:  # Full name + group on hover
                name = str(row_data[0]) if row_data[0] else "Unknown"
                group = row_data[13] if len(row_data) > 13 and row_data[13] else ""
                return f"{name}\nGroup: {group}" if group else name
            return None

        if role == Qt.ForegroundRole:
            _light = ThemeManager.get().is_light
            if col == 0:  # Contact name -- strong readable color
                return QColor("#1a1a2e") if _light else QColor("#e6edf3")
            if col == 1:  # Phone -- teal monospace look
                return QColor("#01696f") if _light else QColor("#80cbc4")
            if col == 2:  # Direction
                if row_data[2]:  # outgoing
                    return QColor("#9a6700") if _light else QColor("#e3b341")
                return QColor("#1a7f37") if _light else QColor("#3fb950") # incoming
            if col == 3:  # Type
                if row_data[3]:  # video
                    return QColor("#8250df") if _light else QColor("#a371f7")
                return QColor("#0969da") if _light else QColor("#58a6ff") # voice
            if col == 4:  # Result
                result = row_data[4]
                if result == "missed":
                    return QColor("#cf222e") if _light else QColor("#f85149")
                if result in ("rejected", "cancelled"):
                    return QColor("#9a6700") if _light else QColor("#e3b341")
                if result in ("completed", "answered", "connected", "disconnected"):
                    return QColor("#1a7f37") if _light else QColor("#3fb950")
            if col == 5:  # Duration
                dur = row_data[5]
                if dur and dur > 3600:
                    return QColor("#9a6700") if _light else QColor("#e3b341")
                return QColor("#1a1a2e") if _light else QColor("#e6edf3")
            if col == 6:  # Date
                return QColor("#57606a") if _light else QColor("#8b949e")
            if col == 7:  # Participants
                cnt = row_data[7] or 0
                if cnt >= 10:
                    return QColor("#8250df") if _light else QColor("#a371f7")
                if cnt >= 3:
                    return QColor("#0969da") if _light else QColor("#58a6ff")
            if col == 8:  # Scope
                scope = _call_scope_label(row_data[16] if len(row_data) > 16 else "", row_data[8])
                if scope == "Voice Chat":
                    return QColor("#8250df") if _light else QColor("#a371f7")
                if scope == "Multi-Person":
                    return QColor("#0969da") if _light else QColor("#58a6ff")
                if scope == "Group":
                    return QColor("#00695c") if _light else QColor("#2dd4bf")

        if role == Qt.FontRole:
            font = QFont()
            if col == 0:  # Contact name bold
                font.setBold(True)
                font.setPointSize(10)
                return font
            if col == 1:  # Phone monospace
                font.setFamily("Consolas")
                font.setPointSize(9)
                return font
            if col in (2, 3, 4):  # Badges bold
                font.setBold(True)
                font.setPointSize(9)
                return font
            if col == 5:  # Duration monospace
                font.setFamily("Consolas")
                font.setPointSize(9)
                return font

        if role == Qt.TextAlignmentRole:
            if col in (5, 6):
                return Qt.AlignRight | Qt.AlignVCenter
            if col in (2, 3, 4, 7, 8):
                return Qt.AlignCenter | Qt.AlignVCenter

        # Expose raw row for detail panel
        if role == Qt.UserRole:
            return row_data

        return None

    # ---- helper for detail panel ----
    def get_call_row(self, row: int) -> tuple | None:
        """Return the full row tuple for the given model row."""
        if 0 <= row < len(self._data):
            return self._data[row]
        return None


# ---------------------------------------------------------------------------
# Custom delegate for polished cell rendering
# ---------------------------------------------------------------------------

# Avatar colors for contact initials
_AVATAR_COLORS = [
    "#25c2a0", "#58a6ff", "#a371f7", "#e3b341",
    "#f85149", "#3fb950", "#f78166", "#79c0ff",
]


def _initials(name: str) -> str:
    return "".join(w[0].upper() for w in (name or "?").split()[:2] if w)


def _avatar_color(name: str) -> str:
    h = 0
    for c in (name or ""):
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return _AVATAR_COLORS[h % len(_AVATAR_COLORS)]


class CallsDelegate(QStyledItemDelegate):
    """Custom cell painter for polished badge-style rendering."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw selection/hover background
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor("#25c2a0") if ThemeManager.get().is_dark else QColor("#e0f2f1"))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, QColor("#1c2333") if ThemeManager.get().is_dark else QColor("#f5f5f5"))

        col = index.column()
        row_data = index.data(Qt.UserRole)
        rect = option.rect
        _dark = ThemeManager.get().is_dark

        if col == 0 and row_data:
            # Contact: avatar circle + bold name
            self._paint_contact(painter, rect, row_data, _dark)
        elif col == 2 and row_data:
            # Direction: colored pill badge
            from_me = row_data[2]
            if from_me:
                self._paint_pill(painter, rect, "Outgoing",
                                 QColor("#e3b341") if _dark else QColor("#9a6700"),
                                 QColor(227, 179, 65, 36) if _dark else QColor(154, 103, 0, 25))
            else:
                self._paint_pill(painter, rect, "Incoming",
                                 QColor("#3fb950") if _dark else QColor("#1a7f37"),
                                 QColor(63, 185, 80, 36) if _dark else QColor(26, 127, 55, 25))
        elif col == 3 and row_data:
            # Type: colored pill -- show Voice Chat specially
            is_video = row_data[3]
            category = row_data[16] if len(row_data) > 16 else "personal"
            if category == "voice_chat":
                self._paint_pill(painter, rect, "Voice Chat",
                                 QColor("#a371f7") if _dark else QColor("#8250df"),
                                 QColor(163, 113, 247, 36) if _dark else QColor(130, 80, 223, 25))
            elif is_video:
                self._paint_pill(painter, rect, "Video",
                                 QColor("#a371f7") if _dark else QColor("#8250df"),
                                 QColor(163, 113, 247, 36) if _dark else QColor(130, 80, 223, 25))
            else:
                self._paint_pill(painter, rect, "Voice",
                                 QColor("#58a6ff") if _dark else QColor("#0969da"),
                                 QColor(88, 166, 255, 36) if _dark else QColor(9, 105, 218, 25))
        elif col == 4 and row_data:
            # Result: colored pill with dot
            result = row_data[4] or ""
            label = CALL_RESULT_LABELS.get(result, str(result)).title()
            if result == "missed":
                fg = QColor("#f85149") if _dark else QColor("#cf222e")
                bg = QColor(248, 81, 73, 36) if _dark else QColor(207, 34, 46, 25)
            elif result in ("completed", "answered", "connected", "disconnected"):
                fg = QColor("#3fb950") if _dark else QColor("#1a7f37")
                bg = QColor(63, 185, 80, 36) if _dark else QColor(26, 127, 55, 25)
            else:
                fg = QColor("#e3b341") if _dark else QColor("#9a6700")
                bg = QColor(227, 179, 65, 36) if _dark else QColor(154, 103, 0, 25)
            self._paint_pill(painter, rect, label, fg, bg)
        elif col == 5 and row_data:
            # Duration: monospace + thin bar
            self._paint_duration(painter, rect, row_data, _dark)
        elif col == 6:
            # Date/time: split into date + time
            self._paint_datetime(painter, rect, index.data(Qt.DisplayRole), _dark)
        elif col == 8 and row_data:
            scope = _call_scope_label(row_data[16] if len(row_data) > 16 else "", row_data[8])
            if scope == "Voice Chat":
                fg = QColor("#a371f7") if _dark else QColor("#8250df")
                bg = QColor(163, 113, 247, 36) if _dark else QColor(130, 80, 223, 25)
            elif scope == "Multi-Person":
                fg = QColor("#58a6ff") if _dark else QColor("#0969da")
                bg = QColor(88, 166, 255, 36) if _dark else QColor(9, 105, 218, 25)
            elif scope == "Group":
                fg = QColor("#2dd4bf") if _dark else QColor("#00695c")
                bg = QColor(45, 212, 191, 34) if _dark else QColor(0, 105, 92, 18)
            else:
                fg = QColor("#57606a") if _dark else QColor("#667781")
                bg = QColor(139, 148, 158, 28) if _dark else QColor(102, 119, 129, 16)
            self._paint_pill(painter, rect, scope, fg, bg)
        else:
            # Default: just draw text
            super().paint(painter, option, index)
            painter.restore()
            return

        painter.restore()

    def _paint_contact(self, painter: QPainter, rect: QRect, row_data, dark: bool):
        """Draw avatar (photo or initials) + bold contact name."""
        name = str(row_data[0]) if row_data[0] else "Unknown"
        avatar_blob = row_data[15] if len(row_data) > 15 else None
        x, y, h = rect.x() + 6, rect.y(), rect.height()

        av_size = 28
        av_y = y + (h - av_size) // 2

        if avatar_blob and len(avatar_blob) > 100:
            # Real photo avatar -- circular clip
            pix = QPixmap()
            pix.loadFromData(bytes(avatar_blob))
            if not pix.isNull():
                pix = pix.scaled(av_size, av_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                clip = QPainterPath()
                clip.addEllipse(float(x), float(av_y), float(av_size), float(av_size))
                painter.setClipPath(clip)
                # Center the scaled pixmap
                px = x + (av_size - pix.width()) // 2
                py = av_y + (av_size - pix.height()) // 2
                painter.drawPixmap(px, py, pix)
                painter.setClipping(False)
            else:
                self._paint_initials_avatar(painter, x, av_y, av_size, name)
        else:
            # Initials avatar
            self._paint_initials_avatar(painter, x, av_y, av_size, name)

        # Name text + group name below
        name_x = x + av_size + 8
        category = row_data[16] if len(row_data) > 16 else "personal"
        # Only show group name for actual group calls and voice chats, not multi-person
        group_name = ""
        if category in ("group_call", "voice_chat") and len(row_data) > 13:
            group_name = row_data[13] or ""
        available_w = rect.width() - av_size - 20

        if group_name:
            # Two lines: name on top, group below
            painter.setPen(QColor("#e6edf3") if dark else QColor("#1a1a2e"))
            name_font = QFont("Inter", 9)
            name_font.setBold(True)
            painter.setFont(name_font)
            name_rect = QRect(name_x, y + 4, available_w, h // 2 - 2)
            elided = QFontMetrics(name_font).elidedText(name, Qt.ElideRight, available_w)
            painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

            # Group name (smaller, muted)
            painter.setPen(QColor("#8b949e") if dark else QColor("#57606a"))
            grp_font = QFont("Inter", 8)
            painter.setFont(grp_font)
            grp_rect = QRect(name_x, y + h // 2, available_w, h // 2 - 4)
            grp_elided = QFontMetrics(grp_font).elidedText(f"- {group_name}", Qt.ElideRight, available_w)
            painter.drawText(grp_rect, Qt.AlignVCenter | Qt.AlignLeft, grp_elided)
        else:
            # Single line: name centered
            painter.setPen(QColor("#e6edf3") if dark else QColor("#1a1a2e"))
            name_font = QFont("Inter", 10)
            name_font.setBold(True)
            painter.setFont(name_font)
            text_rect = QRect(name_x, y, available_w, h)
            elided = QFontMetrics(name_font).elidedText(name, Qt.ElideRight, available_w)
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_initials_avatar(self, painter: QPainter, x, y, size, name):
        """Paint a colored circle with initials."""
        color = QColor(_avatar_color(name))
        bg_color = QColor(color)
        bg_color.setAlpha(40)

        path = QPainterPath()
        path.addEllipse(float(x), float(y), float(size), float(size))
        painter.fillPath(path, QBrush(bg_color))
        painter.setPen(QPen(color, 1))
        painter.drawPath(path)

        ini = _initials(name)
        painter.setPen(color)
        font = QFont("Inter", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRect(int(x), int(y), size, size), Qt.AlignCenter, ini)

    def _paint_pill(self, painter: QPainter, rect: QRect, text: str,
                    fg: QColor, bg: QColor):
        """Draw a rounded pill badge."""
        fm = QFontMetrics(QFont("Inter", 9))
        tw = fm.horizontalAdvance(text) + 16
        th = 22
        px = rect.x() + (rect.width() - tw) // 2
        py = rect.y() + (rect.height() - th) // 2

        pill = QPainterPath()
        pill.addRoundedRect(QRectF(px, py, tw, th), 11, 11)
        painter.fillPath(pill, QBrush(bg))

        painter.setPen(fg)
        font = QFont("Inter", 9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRect(px, py, tw, th), Qt.AlignCenter, text)

    def _paint_duration(self, painter: QPainter, rect: QRect, row_data, dark: bool):
        """Draw duration text + thin progress bar."""
        dur = row_data[5] or 0
        text = _fmt_duration(dur)

        # Text
        painter.setPen(QColor("#e6edf3") if dark else QColor("#1a1a2e"))
        font = QFont("Consolas", 9)
        painter.setFont(font)
        text_rect = QRect(rect.x() + 4, rect.y(), rect.width() - 8, rect.height() - 6)
        painter.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter, text)

        # Thin bar at bottom
        bar_w = rect.width() - 16
        bar_h = 3
        bar_x = rect.x() + 8
        bar_y = rect.y() + rect.height() - 7

        # Background bar
        bar_bg = QPainterPath()
        bar_bg.addRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 1.5, 1.5)
        painter.fillPath(bar_bg, QBrush(QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 15)))

        # Fill bar (proportional, max = 1 hour for scaling)
        max_dur = 3600  # 1 hour = 100%
        fill_pct = min(1.0, dur / max_dur) if dur else 0
        if fill_pct > 0:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(bar_x, bar_y, bar_w * fill_pct, bar_h), 1.5, 1.5)
            bar_color = QColor("#25c2a0") if dark else QColor("#01696f")
            bar_color.setAlpha(150)
            painter.fillPath(fill, QBrush(bar_color))

    def _paint_datetime(self, painter: QPainter, rect: QRect, text: str, dark: bool):
        """Draw date on top, time below in monospace."""
        if not text:
            return
        parts = text.split(" ", 1)
        date_text = parts[0] if parts else ""
        time_text = parts[1] if len(parts) > 1 else ""

        mid_y = rect.y() + rect.height() // 2

        # Date
        painter.setPen(QColor("#e6edf3") if dark else QColor("#24292f"))
        font = QFont("Inter", 9)
        painter.setFont(font)
        painter.drawText(QRect(rect.x() + 4, mid_y - 16, rect.width() - 8, 16),
                         Qt.AlignRight | Qt.AlignBottom, date_text)

        # Time (monospace, muted)
        painter.setPen(QColor("#8b949e") if dark else QColor("#57606a"))
        font2 = QFont("Consolas", 8)
        painter.setFont(font2)
        painter.drawText(QRect(rect.x() + 4, mid_y + 1, rect.width() - 8, 14),
                         Qt.AlignRight | Qt.AlignTop, time_text)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), 38))
        return hint


# ---------------------------------------------------------------------------
# Detail panel widget (right side)
# ---------------------------------------------------------------------------


class _DetailRow(QWidget):
    """A single label: value row in the detail panel."""

    def __init__(self, label_text: str, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(8)
        self._lbl = QLabel(label_text)
        self._lbl.setFixedWidth(95)
        self._lbl.setStyleSheet(self._tm.detail_label_style())
        self._lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self._val = QLabel("")
        self._val.setStyleSheet(self._tm.detail_value_style())
        self._val.setWordWrap(True)
        self._val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._lbl)
        lay.addWidget(self._val, 1)

    def set_value(self, text: str, accent: bool = False):
        self._val.setText(text)
        self._val.setStyleSheet(
            self._tm.detail_value_accent_style() if accent else self._tm.detail_value_style()
        )


class CallDetailPanel(QFrame):
    """Right-side panel showing full call details and participants."""

    # Signals for navigation
    go_to_chat = Signal(int, str)       # conv_id, display_name
    go_to_message = Signal(int, int)    # conv_id, msg_id
    contact_requested = Signal(int)     # contact_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self.setObjectName("callDetailPanel")
        self._all_participants: list[tuple] = []

        # Overlay styling with shadow border
        _dark = self._tm.is_dark
        self.setStyleSheet(
            f"CallDetailPanel {{ background: {'#1a1a2e' if _dark else '#ffffff'}; "
            f"border-left: 2px solid {'#25c2a0' if _dark else '#00897b'}; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header with title + close button
        hdr = QHBoxLayout()
        hdr.setContentsMargins(14, 8, 8, 4)
        self._title = QLabel("Call Details")
        tf = QFont()
        tf.setPointSize(12)
        tf.setBold(True)
        self._title.setFont(tf)
        self._title.setStyleSheet(self._tm.detail_title_style())
        hdr.addWidget(self._title)
        hdr.addStretch()
        close_btn = QPushButton("\u2715 Close")
        close_btn.setFixedHeight(24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { border: 1px solid #ccc; font-size: 11px; color: #666; "
            "border-radius: 4px; padding: 0 8px; background: rgba(0,0,0,0.03); }"
            "QPushButton:hover { background: rgba(255,0,0,0.08); color: red; border-color: red; }"
        )
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        outer.addLayout(hdr)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(14, 4, 14, 10)
        self._layout.setSpacing(2)

        # Section: Basic Info
        sec1 = QLabel("CALL INFO")
        sec1.setStyleSheet(self._tm.detail_section_header_style())
        self._layout.addWidget(sec1)

        # Voice-chat "reconstructed" banner — WhatsApp does NOT retain full
        # voice-chat session data the way regular calls are recorded, so the
        # per-call detail we display is pieced together from related signals
        # (group_call_creator_event + participant joins). Surface this to
        # the examiner so they know the record is reconstructed, not raw.
        self._reconstructed_banner = QLabel()
        self._reconstructed_banner.setWordWrap(True)
        self._reconstructed_banner.setTextFormat(Qt.RichText)
        self._reconstructed_banner.setStyleSheet(
            "QLabel { background: #fff3e0; color: #bf360c; "
            "border: 1px solid #ff9800; border-left: 4px solid #e65100; "
            "border-radius: 4px; padding: 8px 10px; font-size: 11px; "
            "margin-top: 4px; margin-bottom: 6px; }"
        )
        self._reconstructed_banner.setVisible(False)
        self._layout.addWidget(self._reconstructed_banner)

        self._contact_row = _DetailRow("Contact")
        self._jid_row = _DetailRow("JID")
        self._phone_row = _DetailRow("Phone")
        self._direction_row = _DetailRow("Direction")
        self._type_row = _DetailRow("Type")
        self._result_row = _DetailRow("Result")
        self._duration_row = _DetailRow("Duration")
        self._start_time_row = _DetailRow("Call Start")
        self._end_time_row = _DetailRow("Call End")
        self._data_row = _DetailRow("Data Sent")
        self._group_row = _DetailRow("Group Call")
        self._group_name_row = _DetailRow("Group")
        self._call_id_row = _DetailRow("Call ID")
        self._creator_row = _DetailRow("Creator")
        self._creator_device_row = _DetailRow("Device")

        for w in [self._contact_row, self._jid_row, self._phone_row,
                  self._direction_row, self._type_row, self._result_row,
                  self._duration_row, self._start_time_row, self._end_time_row,
                  self._data_row, self._group_row, self._group_name_row,
                  self._call_id_row, self._creator_row, self._creator_device_row]:
            self._layout.addWidget(w)

        # Navigation buttons
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        nav_row.setContentsMargins(0, 8, 0, 4)

        self._goto_chat_btn = QPushButton("\u2192  Open Chat")
        self._goto_chat_btn.setCursor(Qt.PointingHandCursor)
        self._goto_chat_btn.setFixedHeight(30)
        self._goto_chat_btn.setStyleSheet(
            "QPushButton { background: #01696f; color: white; border: none; "
            "border-radius: 6px; font-size: 11px; font-weight: bold; padding: 0 12px; } "
            "QPushButton:hover { background: #018786; } "
            "QPushButton:disabled { background: #555; color: #888; }"
        )
        self._goto_chat_btn.clicked.connect(self._on_goto_chat)
        nav_row.addWidget(self._goto_chat_btn)

        self._goto_msg_btn = QPushButton("\u2192  Go to Message")
        self._goto_msg_btn.setCursor(Qt.PointingHandCursor)
        self._goto_msg_btn.setFixedHeight(30)
        self._goto_msg_btn.setStyleSheet(
            "QPushButton { background: #0969da; color: white; border: none; "
            "border-radius: 6px; font-size: 11px; font-weight: bold; padding: 0 12px; } "
            "QPushButton:hover { background: #0550ae; } "
            "QPushButton:disabled { background: #555; color: #888; }"
        )
        self._goto_msg_btn.clicked.connect(self._on_goto_msg)
        nav_row.addWidget(self._goto_msg_btn)

        self._layout.addLayout(nav_row)

        # Section: Participants
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(self._tm.detail_separator_style())
        self._layout.addWidget(sep2)

        # Participants header with count badge
        p_header_row = QHBoxLayout()
        self._participants_header = QLabel("PARTICIPANTS")
        self._participants_header.setStyleSheet(self._tm.detail_section_header_style())
        p_header_row.addWidget(self._participants_header)
        p_header_row.addStretch()
        self._p_count_badge = QLabel("")
        self._p_count_badge.setStyleSheet(
            "background: rgba(37,194,160,0.15); color: #25c2a0; "
            "font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 10px;"
        )
        p_header_row.addWidget(self._p_count_badge)
        self._layout.addLayout(p_header_row)

        # Participant search
        self._p_search = QLineEdit()
        self._p_search.setPlaceholderText("Filter participants...")
        self._p_search.setFixedHeight(28)
        self._p_search.setStyleSheet(self._tm.search_box_style())
        self._p_search.textChanged.connect(self._filter_participants)
        self._layout.addWidget(self._p_search)

        self._participants_list = QListWidget()
        self._participants_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; }"
            "QListWidget::item { padding: 4px 6px; border-radius: 4px; }"
            "QListWidget::item:hover { background: rgba(37,194,160,0.1); }"
        )
        self._participants_list.setMinimumHeight(100)
        self._participants_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._participants_list.itemDoubleClicked.connect(self._on_participant_clicked)
        self._layout.addWidget(self._participants_list)

        self._layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        # Navigation state
        self._current_conv_id = None
        self._current_msg_id = None
        self._current_conv_name = ""

    def _on_goto_chat(self):
        if self._current_conv_id:
            self.go_to_chat.emit(self._current_conv_id, self._current_conv_name)

    def _on_goto_msg(self):
        if self._current_conv_id and self._current_msg_id:
            self.go_to_message.emit(self._current_conv_id, self._current_msg_id)

    def _filter_participants(self, text: str):
        """Filter participant list by search text."""
        self._render_participants(text.strip().lower())

    def _on_participant_clicked(self, item: QListWidgetItem):
        """Navigate to contact profile on double-click."""
        contact_id = item.data(Qt.UserRole)
        if contact_id:
            self.contact_requested.emit(contact_id)

    def _render_participants(self, filter_text: str = ""):
        """Render participants list, optionally filtered."""
        self._participants_list.clear()
        participants = self._all_participants
        if filter_text:
            participants = [
                p for p in participants
                if filter_text in (p[2] or "").lower()
                or filter_text in (p[3] or "").lower()
                or filter_text in (p[4] or "").lower()
            ]

        if not participants:
            item = QListWidgetItem("No matches" if filter_text else "No participant records")
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(QColor("#8b949e"))
            self._participants_list.addItem(item)
            self._participants_list.show()
            return

        # Participant call_result codes (call_log_participant_v2):
        # 0=joined/connected, 2=rejected/no_answer, 5=initiated/participated
        # IMPORTANT: code 5 is ambiguous — it means "initiated" for the caller
        # or "participated" for others. Cross-reference with main call result.
        main_result = getattr(self, '_current_call_result', '') or ''
        main_from_me = getattr(self, '_current_call_from_me', False)
        main_category = getattr(self, '_current_call_category', 'personal')
        call_was_missed = main_result in ('missed', 'rejected', 'unavailable', 'busy')

        for p in participants:
            contact_id = p[0]
            name = p[2] or "Unknown"
            result_code = p[1]
            jid = p[3] or ""
            p_phone = p[4] or ""

            # Determine badge based on result code + context
            is_you = name.startswith("You (") or name == "You"
            if result_code == 0:
                badge = "joined"
            elif result_code == 2:
                badge = "no answer" if not is_you else "not joined"
            elif result_code == 5:
                # Code 5: "initiated or disconnected" — context-dependent
                if is_you and call_was_missed:
                    badge = "not joined"
                elif call_was_missed and main_from_me:
                    badge = "no answer"
                elif call_was_missed:
                    badge = "invited"
                else:
                    badge = "participated"
            elif result_code == 3:
                badge = "unavailable"
            elif result_code is not None:
                badge = f"code {result_code}"
            else:
                badge = ""

            if p_phone and not p_phone.startswith("+"):
                p_phone = f"+{p_phone}"

            # Build display text with clear formatting
            line1 = name
            if p_phone:
                line1 += f"  ({p_phone})"
            line2 = ""
            if jid:
                line2 += f"  {jid}"
            if badge:
                line2 += f"  [{badge}]"
            text = line1 + ("\n" + line2 if line2 else "")

            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, contact_id)
            item.setToolTip(f"Double-click to view {name}'s profile")

            # Avatar icon — use real DP if available, else initials circle
            from PySide6.QtGui import QPixmap, QPainter, QFont as _QFont, QIcon
            avatar_blob = p[5] if len(p) > 5 else None
            pm = QPixmap(32, 32)

            if avatar_blob and len(avatar_blob) > 100:
                pm.loadFromData(bytes(avatar_blob))
                pm = pm.scaled(32, 32, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            else:
                _avatar_colors = [
                    "#00897b", "#6a1b9a", "#c62828", "#1565c0",
                    "#ef6c00", "#2e7d32", "#ad1457", "#4527a0",
                ]
                initials = ""
                clean_name = name.replace("You (", "").replace(")", "").replace("~", "").strip()
                for word in clean_name.split()[:2]:
                    if word and word[0].isalpha():
                        initials += word[0].upper()
                if not initials:
                    initials = "?"
                avatar_bg = _avatar_colors[(contact_id or 0) % len(_avatar_colors)]
                pm.fill(QColor(0, 0, 0, 0))
                qp = QPainter(pm)
                qp.setRenderHint(QPainter.Antialiasing)
                qp.setBrush(QColor(avatar_bg))
                qp.setPen(Qt.NoPen)
                qp.drawEllipse(0, 0, 32, 32)
                qp.setPen(QColor(255, 255, 255))
                qp.setFont(_QFont("Segoe UI", 11, _QFont.Bold))
                qp.drawText(QRect(0, 0, 32, 32), Qt.AlignCenter, initials[:2])
                qp.end()
            item.setIcon(QIcon(pm))

            # Color based on result
            if badge in ("no answer", "not joined", "missed"):
                item.setForeground(QColor("#f85149"))
            elif badge in ("declined", "unavailable"):
                item.setForeground(QColor("#e3b341"))
            elif badge in ("joined", "participated"):
                _light = ThemeManager.get().is_light
                item.setForeground(QColor("#1a7f37") if _light else QColor("#3fb950"))

            self._participants_list.addItem(item)

        self._participants_list.show()

    def show_call(self, row_data: tuple):
        """Populate the detail panel from a model row tuple.

        Row indices:
            0=contact_name, 1=phone_number, 2=from_me, 3=is_video,
            4=result_label, 5=duration_sec, 6=timestamp, 7=participant_count,
            8=is_group_call, 9=id (PK), 10=contact_id, 11=conversation_id,
            12=bytes_transferred, 13=group_name, 14=contact_jid
        """
        contact_name = row_data[0] or "Unknown"
        phone = row_data[1] or ""
        from_me = row_data[2]
        is_video = row_data[3]
        result_label = row_data[4]
        self._current_call_result = result_label
        self._current_call_from_me = from_me
        duration_sec = row_data[5]
        timestamp = row_data[6]
        bytes_transferred = row_data[12] if len(row_data) > 12 else None
        is_group = row_data[8]
        call_id = row_data[9]
        contact_id = row_data[10]
        group_name = row_data[13] if len(row_data) > 13 else ""
        contact_jid = row_data[14] if len(row_data) > 14 else ""

        self._contact_row.set_value(contact_name, accent=True)

        # JID
        self._jid_row.set_value(contact_jid or "N/A")

        # Show phone number
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"
        self._phone_row.set_value(phone or "N/A")

        if from_me:
            self._direction_row.set_value("Outgoing")
        else:
            self._direction_row.set_value("Incoming")

        self._type_row.set_value(
            "Video Call" if is_video else "Voice Call"
        )

        result_display = CALL_RESULT_LABELS.get(result_label, str(result_label) if result_label else "")
        self._result_row.set_value(result_display)

        self._duration_row.set_value(_fmt_duration(duration_sec), accent=(duration_sec and duration_sec > 600))

        # Call start and end times
        self._start_time_row.set_value(_fmt_timestamp(timestamp) if timestamp else "N/A")
        if timestamp and duration_sec:
            end_ts = timestamp + duration_sec * 1000
            self._end_time_row.set_value(_fmt_timestamp(end_ts))
        else:
            self._end_time_row.set_value("N/A")

        self._data_row.set_value(_fmt_bytes(bytes_transferred) or "N/A")

        self._group_row.set_value("Yes" if is_group else "No")

        # Call category
        call_category = row_data[16] if len(row_data) > 16 else "personal"
        self._current_call_category = call_category
        cat_labels = {
            "voice_chat": "Voice Chat",
            "group_call": "Group Call",
            "multi_person": "Multi-Person Call",
            "personal": "Personal Call",
        }
        self._type_row.set_value(cat_labels.get(call_category, call_category))

        # Voice-chat reconstructed banner — shown ONLY for Voice Chat
        # category (msgstore.db does not store the full session like a
        # normal call, so this record is synthesized from related events).
        if call_category == "voice_chat":
            _group_line = ""
            if group_name:
                _group_line = f" for group <b>{group_name}</b>"
            self._reconstructed_banner.setText(
                "\u26A0\uFE0F <b>RECONSTRUCTED VOICE CHAT RECORD</b><br>"
                "WhatsApp does not store voice-chat sessions the same way "
                "as regular calls. This entry is reassembled from "
                "<code>group_call_creator_event</code> + participant-join "
                f"records{_group_line} \u2014 timings, duration and "
                "participants reflect observed signals, not a raw call log."
            )
            self._reconstructed_banner.setVisible(True)
        else:
            self._reconstructed_banner.setVisible(False)

        # Group name (only for actual group calls/voice chats, not multi-person)
        if is_group and group_name and call_category in ("group_call", "voice_chat"):
            self._group_name_row.set_value(group_name)
            self._group_name_row.show()
        else:
            self._group_name_row.hide()

        # Call ID text
        db = Database.get()
        cid_row = db.fetchone(
            "SELECT call_id_text FROM call_record WHERE id = ?", (int(call_id),)
        )
        call_id_text = (cid_row[0] if cid_row and cid_row[0] else "N/A")
        if len(call_id_text) > 40:
            call_id_text = call_id_text[:37] + "..."
        self._call_id_row.set_value(call_id_text)

        # Call creator info
        creator_jid = row_data[17] if len(row_data) > 17 else ""
        creator_name = row_data[18] if len(row_data) > 18 else ""
        creator_device_type = row_data[19] if len(row_data) > 19 else ""
        if creator_name:
            self._creator_row.set_value(creator_name)
            self._creator_row.show()
        elif creator_jid:
            self._creator_row.set_value(creator_jid)
            self._creator_row.show()
        else:
            self._creator_row.hide()
        if creator_device_type:
            if creator_device_type == "primary_phone":
                dev_label = "Primary Phone"
            elif creator_device_type.startswith("companion"):
                dev_num = creator_device_type.replace("companion_", "")
                dev_label = f"Companion Device #{dev_num}"
            else:
                dev_label = creator_device_type
            self._creator_device_row.set_value(dev_label)
            self._creator_device_row.show()
        else:
            self._creator_device_row.hide()

        # Navigation state
        self._current_conv_id = None
        self._current_conv_name = group_name or contact_name
        self._current_msg_id = None

        # Find linked message and conversation via call_record
        if call_id:
            cr_row = db.fetchone(
                "SELECT call_id_text, conversation_id, group_conversation_id "
                "FROM call_record WHERE id = ?",
                (int(call_id),),
            )
            if cr_row:
                call_id_text_full = cr_row[0] or ""

                # For group calls: prefer group_conversation_id
                if is_group and cr_row[2]:
                    self._current_conv_id = cr_row[2]
                    if not group_name:
                        gn = db.scalar(
                            "SELECT COALESCE(display_name, jid_raw_string) FROM conversation WHERE id = ?",
                            (cr_row[2],),
                        )
                        if gn:
                            self._group_name_row.set_value(gn)
                            self._group_name_row.show()
                            self._current_conv_name = gn
                elif cr_row[1]:
                    self._current_conv_id = cr_row[1]

                # Find the actual message by key_id exact match
                # call_id_text is "call:ABC123..." -- source_key_id is "ABC123..."
                if call_id_text_full:
                    clean_id = call_id_text_full.replace("call:", "")
                    if clean_id:
                        msg_row = db.fetchone(
                            "SELECT id, conversation_id FROM message "
                            "WHERE source_key_id = ?",
                            (clean_id,),
                        )
                        if msg_row:
                            self._current_msg_id = msg_row[0]
                            self._current_conv_id = msg_row[1]
                            cn = db.scalar(
                                "SELECT COALESCE(display_name, jid_raw_string) FROM conversation WHERE id = ?",
                                (msg_row[1],),
                            )
                            if cn:
                                self._current_conv_name = cn

                # Fallback: no linked message, find nearest message by timestamp
                if not self._current_msg_id and self._current_conv_id and timestamp:
                    nearest = db.fetchone(
                        "SELECT id FROM message "
                        "WHERE conversation_id = ? AND message_type != 7 "
                        "ORDER BY ABS(timestamp - ?) LIMIT 1",
                        (self._current_conv_id, timestamp),
                    )
                    if nearest:
                        self._current_msg_id = nearest[0]

        # Fallback: use conversation_id from SQL join
        if not self._current_conv_id:
            conv_id = row_data[11] if len(row_data) > 11 else None
            self._current_conv_id = conv_id

        self._goto_chat_btn.setEnabled(bool(self._current_conv_id))
        self._goto_msg_btn.setEnabled(bool(self._current_msg_id))

        # Participants -- load and render with search
        # Include device owner as a participant (not in call_log_participant_v2)
        self._p_search.clear()
        self._all_participants = list(db.fetchall(
            "SELECT cp.contact_id, cp.call_result, "
            "COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') AS name, "
            "COALESCE(c.phone_jid, c.lid_jid, '') AS jid, "
            "COALESCE(c.phone_number, '') AS phone, "
            "c.avatar_blob "
            "FROM call_participant cp "
            "LEFT JOIN contact c ON c.id = cp.contact_id "
            "WHERE cp.call_id = ? "
            "ORDER BY name",
            (call_id,),
        ))

        # Add device owner to participant list (if not already present)
        owner_row = db.fetchone(
            "SELECT c.id, c.resolved_name, c.phone_jid, c.phone_number, c.avatar_blob "
            "FROM contact c JOIN case_metadata cm ON cm.key = 'device_owner_jid' "
            "WHERE c.phone_jid = cm.value"
        )
        if owner_row:
            owner_id = owner_row[0]
            # Check if owner is already in the list
            already_in = any(p[0] == owner_id for p in self._all_participants)
            if not already_in:
                # Determine owner's participation based on the MAIN call result
                # NOT based on duration (voice chats can have duration even if owner didn't join)
                call_answered = result_label in ("completed", "answered", "connected", "disconnected")
                # For voice chats where owner didn't join, don't add them
                # People will think owner was invited and not joined — misleading
                if call_category == "voice_chat" and not call_answered:
                    pass  # Skip owner — they weren't part of this voice chat
                else:
                    if from_me:
                        owner_result = 0 if call_answered else 5
                    else:
                        owner_result = 0 if call_answered else 2
                    owner_name = f"You ({owner_row[1]})" if owner_row[1] else "You"
                    owner_entry = (owner_row[0], owner_result, owner_name,
                                   owner_row[2] or "", owner_row[3] or "",
                                   owner_row[4])  # avatar_blob
                    self._all_participants.insert(0, owner_entry)

        # For 1-on-1 calls with no participants, add both parties
        if not self._all_participants and not is_group:
            # Add the other contact
            other_result = 0 if (result_label in ("completed", "answered", "connected", "disconnected")) else 2
            # Get contact avatar
            other_avatar = db.scalar(
                "SELECT avatar_blob FROM contact WHERE id = ?", (contact_id,)
            ) if contact_id else None
            other_entry = (contact_id, other_result, contact_name,
                           contact_jid, phone.lstrip("+"), other_avatar)
            self._all_participants.append(other_entry)

        if self._all_participants:
            self._participants_header.show()
            if call_category == "voice_chat":
                _cr = getattr(self, '_current_call_result', '') or ''
                _missed = _cr in ('missed', 'rejected', 'unavailable')
                joined_cnt = sum(1 for p in self._all_participants if p[1] == 0 or (p[1] == 5 and not _missed))
                self._p_count_badge.setText(f"{joined_cnt} joined")
            else:
                self._p_count_badge.setText(f"{len(self._all_participants)} total")
            self._p_count_badge.show()
            self._p_search.show() if len(self._all_participants) > 5 else self._p_search.hide()
            self._render_participants()
        else:
            self._participants_header.show()
            self._p_count_badge.setText("0")
            self._p_count_badge.show()
            self._p_search.hide()
            from PySide6.QtWidgets import QListWidgetItem
            self._participants_list.clear()
            item = QListWidgetItem("No participant records")
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(QColor("#8b949e"))
            self._participants_list.addItem(item)
            self._participants_list.show()

    def clear(self):
        self.hide()


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

class CallsPage(QWidget):
    conversation_selected = Signal(int, str)   # conv_id, display_name
    navigate_to_message = Signal(int, int)     # conv_id, msg_id
    contact_requested = Signal(int)            # contact_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 0)
        root.setSpacing(6)

        # ---- Header bar: title + count + stats badges ----
        hdr_frame = QFrame()
        hdr_frame.setStyleSheet(self._tm.stat_frame_style())
        hdr_layout = QHBoxLayout(hdr_frame)
        hdr_layout.setContentsMargins(16, 0, 16, 0)
        hdr_layout.setSpacing(12)

        title = QLabel("Call Records")
        tf = QFont()
        tf.setPointSize(14)
        tf.setBold(True)
        title.setFont(tf)
        hdr_layout.addWidget(title)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: gray; font-size: 12px;")
        hdr_layout.addWidget(self._count_label)
        hdr_layout.addStretch()

        # Inline stat badges (compact, no separate rows)
        self._stat_labels: dict[str, QLabel] = {}
        for key, label_text, color in [
            ("total", "Total", "#00897b"), ("voice", "Voice", "#0969da"),
            ("video", "Video", "#8250df"), ("missed", "Missed", "#cf222e"),
            ("outgoing", "Outgoing", "#9a6700"), ("incoming", "Incoming", "#1a7f37"),
            ("voice_chats", "Voice Chat", "#a371f7"),
        ]:
            badge = QLabel(f"{label_text}: ...")
            badge.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: bold; padding: 0 4px;")
            hdr_layout.addWidget(badge)
            self._stat_labels[key] = badge

        root.addWidget(hdr_frame)

        controls_frame = QFrame()
        controls_frame.setStyleSheet(self._tm.stat_frame_style())
        controls_layout = QVBoxLayout(controls_frame)
        controls_layout.setContentsMargins(12, 12, 12, 12)
        controls_layout.setSpacing(10)

        chips_header = QHBoxLayout()
        chips_header.setContentsMargins(0, 0, 0, 0)
        chips_header.setSpacing(8)
        chips_title = QLabel("Call Filters")
        chips_title.setStyleSheet(self._tm.header_label_style() + " font-weight: bold;")
        chips_header.addWidget(chips_title)
        chips_header.addStretch()
        controls_layout.addLayout(chips_header)

        chips_host = QWidget()
        chips_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._filter_layout = WrapLayout(chips_host, h_spacing=8, v_spacing=8)
        chips_host.setLayout(self._filter_layout)

        self._filter_btns: dict[str, QPushButton] = {}
        for fid, label in [
            ("all", "All"), ("incoming", "Incoming"), ("outgoing", "Outgoing"),
            ("voice", "Voice"), ("video", "Video"), ("group", "Group"),
            ("multi_person", "Multi-Person"), ("voice_chat", "Voice Chat"),
            ("missed", "Missed"), ("completed", "Answered"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setMinimumWidth(max(70, btn.sizeHint().width() + 8))
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet(self._tm.filter_btn_style())
            btn.clicked.connect(self._on_filter)
            if fid == "all":
                btn.setChecked(True)
            self._filter_layout.addWidget(btn)
            self._filter_btns[fid] = btn

        controls_layout.addWidget(chips_host)

        tools_row = QHBoxLayout()
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.setSpacing(10)

        search_label = QLabel("Search")
        search_label.setStyleSheet(self._tm.header_label_style())
        tools_row.addWidget(search_label)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search contact, phone, or participant...")
        self._search_box.setMinimumWidth(280)
        self._search_box.setFixedHeight(34)
        self._search_box.setStyleSheet(self._tm.search_box_style())
        self._search_box.setClearButtonEnabled(True)
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._apply)
        self._search_box.textChanged.connect(lambda: self._search_timer.start(300))
        tools_row.addWidget(self._search_box, 1)

        date_label = QLabel("Date Range")
        date_label.setStyleSheet(self._tm.header_label_style())
        tools_row.addWidget(date_label)

        # Two custom calendars (one per QDateEdit) so each picker
        # independently shows the per-day call count badges.  Sharing
        # a single calendar across both QDateEdits is unsafe — Qt
        # reparents the widget on popup, so the second picker would
        # silently lose its calendar.
        self._cal_from = CallCountCalendar()
        self._cal_to = CallCountCalendar()

        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setCalendarWidget(self._cal_from)
        self._date_from.setDate(QDate(2020, 1, 1))
        self._date_from.setFixedHeight(34)
        self._date_from.setFixedWidth(145)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_from.setMinimumDate(QDate(2009, 1, 1))
        self._date_from.setMaximumDate(QDate.currentDate())
        self._date_from.dateChanged.connect(self._apply)
        self._date_from.dateChanged.connect(lambda d: self._date_to.setMinimumDate(d))
        tools_row.addWidget(self._date_from)

        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setCalendarWidget(self._cal_to)
        self._date_to.setDate(QDate.currentDate())
        self._date_to.setFixedHeight(34)
        self._date_to.setFixedWidth(145)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._date_to.setMinimumDate(QDate(2009, 1, 1))
        self._date_to.setMaximumDate(QDate.currentDate())
        self._date_to.dateChanged.connect(self._apply)
        self._date_to.dateChanged.connect(lambda d: self._date_from.setMaximumDate(d))
        tools_row.addWidget(self._date_to)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setFixedHeight(34)
        self._reset_btn.setMinimumWidth(86)
        self._reset_btn.setStyleSheet(self._tm.export_btn_style())
        self._reset_btn.clicked.connect(self._reset_filters)
        tools_row.addWidget(self._reset_btn)

        # Export buttons
        _accent = "#00897b" if ThemeManager.get().is_light else "#00bcd4"
        _export_style = (
            f"QPushButton {{ background: {_accent}; color: white; border: none; "
            f"border-radius: 6px; padding: 6px 12px; font-weight: bold; font-size: 10px; }}"
            f"QPushButton:hover {{ background: #00695c; }}"
        )
        self._export_csv_btn = QPushButton("\u2913 Export CSV")
        self._export_csv_btn.setFixedHeight(34)
        self._export_csv_btn.setStyleSheet(_export_style)
        self._export_csv_btn.clicked.connect(self._export_csv)
        tools_row.addWidget(self._export_csv_btn)

        self._export_html_btn = QPushButton("\u2913 Export HTML")
        self._export_html_btn.setFixedHeight(34)
        self._export_html_btn.setStyleSheet(_export_style)
        self._export_html_btn.clicked.connect(self._export_html)
        tools_row.addWidget(self._export_html_btn)

        controls_layout.addLayout(tools_row)
        root.addWidget(controls_frame)

        # ---- Table (full width) ----
        table_container = QWidget()
        table_container.setObjectName("callsTableContainer")
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.setSpacing(0)

        self._model = CallsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.ElideRight)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setMouseTracking(True)
        self._table.setItemDelegate(CallsDelegate(self._table))

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionsMovable(True)
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setMinimumSectionSize(30)

        # Contact stretches to fill; others fixed but sensible
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 146), (2, 118), (3, 126), (4, 112),
                       (5, 112), (6, 156), (7, 110), (8, 126)]:
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)
            hdr.resizeSection(col, w)
        hdr.setStretchLastSection(True)  # Last column stretches to prevent empty space

        tc_layout.addWidget(self._table)
        root.addWidget(table_container, 1)

        # ---- Detail panel (overlay on right side) ----
        self._detail_panel = CallDetailPanel(table_container)
        self._detail_panel.go_to_chat.connect(self.conversation_selected.emit)
        self._detail_panel.go_to_message.connect(self.navigate_to_message.emit)
        self._detail_panel.contact_requested.connect(self.contact_requested.emit)
        self._detail_panel.hide()  # Hidden until a row is clicked

        # ---- Signals ----
        self._table.clicked.connect(self._on_row_click)

        # ---- Initial load ----
        self._current_filter = "all"
        self._sync_date_bounds()
        QTimer.singleShot(50, self._apply)
        QTimer.singleShot(100, self._load_stats)

    def resizeEvent(self, event):
        """Position the overlay detail panel on the right edge."""
        super().resizeEvent(event)
        self._position_detail_panel()

    def _position_detail_panel(self):
        """Position detail panel as overlay on right side of table."""
        if not hasattr(self, '_detail_panel'):
            return
        parent = self._detail_panel.parent()
        if not parent:
            return
        pw = parent.width()
        ph = parent.height()
        panel_w = min(420, pw // 3)
        self._detail_panel.setFixedWidth(panel_w)
        self._detail_panel.setFixedHeight(ph)
        self._detail_panel.move(pw - panel_w, 0)
        self._detail_panel.raise_()

    # ---- filter ----

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        for k, b in self._filter_btns.items():
            b.setChecked(k == fid)
        self._current_filter = fid
        self._apply()

    def _apply(self):
        where_parts: list[str] = []
        params: list = []

        if self._current_filter == "voice":
            where_parts.append("cr.is_video = 0")
        elif self._current_filter == "video":
            where_parts.append("cr.is_video = 1")
        elif self._current_filter == "group":
            where_parts.append("cr.call_category = 'group_call'")
        elif self._current_filter == "voice_chat":
            where_parts.append("cr.call_category = 'voice_chat'")
        elif self._current_filter == "multi_person":
            where_parts.append("cr.call_category = 'multi_person'")
        elif self._current_filter == "missed":
            where_parts.append("cr.result_label = 'missed'")
        elif self._current_filter == "completed":
            where_parts.append("cr.result_label IN ('completed', 'answered', 'connected', 'disconnected')")
        elif self._current_filter == "incoming":
            where_parts.append("cr.from_me = 0")
        elif self._current_filter == "outgoing":
            where_parts.append("cr.from_me = 1")

        # Search filter
        search_text = self._search_box.text().strip() if hasattr(self, '_search_box') else ""
        if search_text:
            pat = f"%{search_text}%"
            where_parts.append(
                "(c.resolved_name LIKE ? OR c.wa_name LIKE ? "
                "OR c.phone_number LIKE ? OR c.phone_jid LIKE ?"
                " OR cr.id IN ("
                "   SELECT cp2.call_id FROM call_participant cp2"
                "   JOIN contact pc ON pc.id = cp2.contact_id"
                "   WHERE pc.resolved_name LIKE ? OR pc.wa_name LIKE ?"
                "   OR pc.phone_number LIKE ? OR pc.phone_jid LIKE ?))"
            )
            params.extend([pat] * 8)

        # Date range filter
        if hasattr(self, '_date_from') and hasattr(self, '_date_to'):
            from_date = self._date_from.date()
            to_date = self._date_to.date()
            # Convert QDate to Unix-ms (start of from_date, end of to_date)
            from_ms, to_ms = qdate_range_to_timestamps(from_date, to_date)
            where_parts.append("cr.timestamp >= ? AND cr.timestamp <= ?")
            params.extend([from_ms, to_ms])

        self._model.load(where=" AND ".join(where_parts), params=tuple(params))
        total = self._model.total_rows
        # Show filtered context when filters are active
        has_filter = self._current_filter != "all"
        has_search = bool(search_text)
        if has_filter or has_search:
            self._count_label.setText(f"{total:,} records (filtered)")
        else:
            self._count_label.setText(f"{total:,} records")
        self._detail_panel.clear()
        # Keep the calendar badges in sync with the active filter
        # (search + date range deliberately excluded — see the helper).
        self._refresh_calendar_counts()

    def _sync_date_bounds(self) -> None:
        db = Database.get()
        first_ts = db.scalar("SELECT MIN(timestamp) FROM call_record WHERE timestamp > 0")
        last_ts = db.scalar("SELECT MAX(timestamp) FROM call_record WHERE timestamp > 0")
        self._date_from.blockSignals(True)
        self._date_to.blockSignals(True)
        if first_ts:
            self._date_from.setDate(timestamp_to_qdate(first_ts))
        else:
            self._date_from.setDate(QDate(2020, 1, 1))
        if last_ts:
            self._date_to.setDate(timestamp_to_qdate(last_ts))
        else:
            self._date_to.setDate(QDate.currentDate())
        self._date_from.blockSignals(False)
        self._date_to.blockSignals(False)
        # Pre-load the per-day count badges into both calendars.  This
        # is the data the picker shows over each day-cell ("flight-fare
        # style") so the analyst can spot busy days at a glance.
        self._refresh_calendar_counts()

    def _refresh_calendar_counts(self) -> None:
        """Recompute calls-per-day for both calendar pickers.

        Called once on initial date-bounds sync, and again after every
        ``_apply`` so the badge counts reflect the currently-active
        category / search / direction filter set.  The DATE-RANGE filter
        itself is intentionally excluded — otherwise the calendar would
        only ever show counts inside its own selected window, defeating
        the picker's purpose.
        """
        db = Database.get()
        where_parts: list[str] = ["timestamp > 0"]
        params: list = []

        # Replicate the category / direction / completion filter so the
        # badge counts match what the analyst is looking at, but
        # DELIBERATELY skip the date-range filter (the picker is what
        # picks the date — it must show counts outside the current
        # range so the analyst can see context).
        if self._current_filter == "voice":
            where_parts.append("is_video = 0")
        elif self._current_filter == "video":
            where_parts.append("is_video = 1")
        elif self._current_filter == "group":
            where_parts.append("call_category = 'group_call'")
        elif self._current_filter == "voice_chat":
            where_parts.append("call_category = 'voice_chat'")
        elif self._current_filter == "multi_person":
            where_parts.append("call_category = 'multi_person'")
        elif self._current_filter == "missed":
            where_parts.append("result_label = 'missed'")
        elif self._current_filter == "completed":
            where_parts.append(
                "result_label IN ('completed','answered','connected','disconnected')"
            )
        elif self._current_filter == "incoming":
            where_parts.append("from_me = 0")
        elif self._current_filter == "outgoing":
            where_parts.append("from_me = 1")

        where_sql = " AND ".join(where_parts)
        try:
            rows = db.fetchall(
                f"SELECT date(timestamp/1000, 'unixepoch', 'localtime') AS d, "
                f"       COUNT(*) AS n "
                f"FROM call_record WHERE {where_sql} "
                f"GROUP BY d",
                params
            )
        except Exception:
            rows = []
        counts: dict[QDate, int] = {}
        for r in rows:
            ds = r["d"] if "d" in r.keys() else r[0]
            n = r["n"] if "n" in r.keys() else r[1]
            if not ds:
                continue
            try:
                y, m, d = ds.split("-")
                counts[QDate(int(y), int(m), int(d))] = int(n)
            except Exception:
                continue
        self._cal_from.set_counts(counts)
        self._cal_to.set_counts(counts)

    def _reset_filters(self) -> None:
        self._current_filter = "all"
        for fid, btn in self._filter_btns.items():
            btn.setChecked(fid == "all")
        self._search_box.clear()
        self._sync_date_bounds()
        self._apply()

    def _export_csv(self) -> None:
        """Export current filtered calls to CSV."""
        from PySide6.QtWidgets import QFileDialog
        import csv
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Calls as CSV", "calls_export.csv",
            "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        db = Database.get()
        where, params = self._build_where()
        rows = db.fetchall(
            f"SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') AS contact, "
            f"COALESCE(c.phone_number, '') AS phone, "
            f"CASE WHEN cr.from_me = 1 THEN 'Outgoing' ELSE 'Incoming' END AS direction, "
            f"CASE WHEN cr.is_video = 1 THEN 'Video' ELSE 'Voice' END AS type, "
            f"cr.result_label, cr.duration_sec, cr.timestamp, cr.call_category, "
            f"cr.call_id_text, cr.is_group_call, cr.bytes_transferred "
            f"FROM call_record cr LEFT JOIN contact c ON c.id = cr.contact_id "
            f"{'WHERE ' + where if where else ''} "
            f"ORDER BY cr.timestamp DESC",
            tuple(params) if params else (),
        )
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Contact", "Phone", "Direction", "Type", "Result",
                                 "Duration (s)", "Timestamp", "Category", "Call ID",
                                 "Group Call", "Bytes Transferred"])
                for r in rows:
                    from datetime import datetime
                    ts = r[6]
                    ts_str = ""
                    if ts:
                        try:
                            ts_str = format_timestamp(ts, "datetime")
                        except Exception:
                            ts_str = str(ts)
                    writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5] or 0,
                                     ts_str, r[7] or "", r[8] or "", r[9], r[10] or 0])
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Export Complete",
                                    f"Exported {len(rows):,} calls to:\n{path}")
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export Failed", str(e))

    def _export_html(self) -> None:
        """Export current filtered calls to HTML report."""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Calls as HTML", "calls_report.html",
            "HTML Files (*.html);;All Files (*)"
        )
        if not path:
            return
        db = Database.get()
        where, params = self._build_where()
        rows = db.fetchall(
            f"SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') AS contact, "
            f"COALESCE(c.phone_number, '') AS phone, "
            f"CASE WHEN cr.from_me = 1 THEN 'Outgoing' ELSE 'Incoming' END AS direction, "
            f"CASE WHEN cr.is_video = 1 THEN 'Video' ELSE 'Voice' END AS type, "
            f"cr.result_label, cr.duration_sec, cr.timestamp, cr.call_category "
            f"FROM call_record cr LEFT JOIN contact c ON c.id = cr.contact_id "
            f"{'WHERE ' + where if where else ''} "
            f"ORDER BY cr.timestamp DESC",
            tuple(params) if params else (),
        )
        try:
            from datetime import datetime
            html = (
                '<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<title>WAInsight Call Report</title>'
                '<style>body{font-family:Segoe UI,sans-serif;margin:20px}'
                'h1{color:#00897b}table{border-collapse:collapse;width:100%}'
                'th{background:#00897b;color:white;padding:8px 12px;text-align:left}'
                'td{padding:6px 12px;border-bottom:1px solid #e0e0e0}'
                'tr:hover{background:#f5f5f5}.missed{color:#e53935}'
                '.answered{color:#2e7d32}.out{color:#1565c0}.in{color:#6a1b9a}'
                '</style></head><body>'
                f'<h1>WAInsight \u2014 Call Log Report</h1>'
                f'<p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} '
                f'| {len(rows):,} calls</p>'
                '<table><tr><th>Contact</th><th>Phone</th><th>Direction</th>'
                '<th>Type</th><th>Result</th><th>Duration</th><th>Date/Time</th>'
                '<th>Category</th></tr>'
            )
            for r in rows:
                ts = r[6]
                ts_str = ""
                if ts:
                    try:
                        ts_str = format_timestamp(ts, "datetime")
                    except Exception:
                        ts_str = str(ts)
                result = r[4] or ""
                rcls = "missed" if "miss" in result else ("answered" if result in ("answered", "connected", "disconnected") else "")
                dcls = "out" if r[2] == "Outgoing" else "in"
                dur = r[5] or 0
                dur_str = f"{dur // 60}m {dur % 60}s" if dur > 0 else "0s"
                html += (
                    f'<tr><td>{r[0]}</td><td>{r[1]}</td>'
                    f'<td class="{dcls}">{r[2]}</td><td>{r[3]}</td>'
                    f'<td class="{rcls}">{result}</td><td>{dur_str}</td>'
                    f'<td>{ts_str}</td><td>{r[7] or ""}</td></tr>'
                )
            html += '</table></body></html>'
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Export Complete",
                                    f"Exported {len(rows):,} calls to:\n{path}")
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export Failed", str(e))

    def _build_where(self) -> tuple[str, list]:
        """Build WHERE clause from current filters (reused for export)."""
        where_parts: list[str] = []
        params: list = []
        if self._current_filter == "incoming":
            where_parts.append("cr.from_me = 0")
        elif self._current_filter == "outgoing":
            where_parts.append("cr.from_me = 1")
        elif self._current_filter == "voice":
            where_parts.append("cr.is_video = 0")
        elif self._current_filter == "video":
            where_parts.append("cr.is_video = 1")
        elif self._current_filter == "group":
            where_parts.append("cr.call_category = 'group_call'")
        elif self._current_filter == "voice_chat":
            where_parts.append("cr.call_category = 'voice_chat'")
        elif self._current_filter == "multi_person":
            where_parts.append("cr.call_category = 'multi_person'")
        elif self._current_filter == "missed":
            where_parts.append("cr.result_label = 'missed'")
        elif self._current_filter == "completed":
            where_parts.append("cr.result_label IN ('completed', 'answered', 'connected', 'disconnected')")
        search = self._search_box.text().strip()
        if search:
            where_parts.append(
                "(c.resolved_name LIKE ? OR c.wa_name LIKE ? OR c.phone_number LIKE ?)"
            )
            params.extend([f"%{search}%"] * 3)
        return " AND ".join(where_parts), params

    def refresh_for_timezone_change(self) -> None:
        self._sync_date_bounds()
        self._apply()

    # ---- row click -> detail ----

    def _on_row_click(self, index: QModelIndex):
        row_data = self._model.get_call_row(index.row())
        if row_data:
            self._detail_panel.show_call(row_data)
            self._position_detail_panel()
            self._detail_panel.show()
            self._detail_panel.raise_()

    # ---- stats ----

    def _load_stats(self):
        db = Database.get()
        total = db.scalar("SELECT COUNT(*) FROM call_record") or 0
        voice = db.scalar("SELECT COUNT(*) FROM call_record WHERE is_video = 0") or 0
        video = db.scalar("SELECT COUNT(*) FROM call_record WHERE is_video = 1") or 0
        missed = db.scalar("SELECT COUNT(*) FROM call_record WHERE result_label = 'missed'") or 0
        outgoing = db.scalar("SELECT COUNT(*) FROM call_record WHERE from_me = 1") or 0
        incoming = db.scalar("SELECT COUNT(*) FROM call_record WHERE from_me = 0") or 0
        avg_dur = db.scalar(
            "SELECT AVG(duration_sec) FROM call_record WHERE duration_sec > 0"
        )
        longest = db.scalar(
            "SELECT MAX(duration_sec) FROM call_record"
        )
        total_dur = db.scalar(
            "SELECT SUM(duration_sec) FROM call_record WHERE duration_sec > 0"
        )
        total_data = db.scalar(
            "SELECT SUM(bytes_transferred) FROM call_record WHERE bytes_transferred > 0"
        )

        voice_chats = db.scalar("SELECT COUNT(*) FROM call_record WHERE call_category = 'voice_chat'") or 0
        multi = db.scalar("SELECT COUNT(*) FROM call_record WHERE call_category = 'multi_person'") or 0
        group_calls = db.scalar("SELECT COUNT(*) FROM call_record WHERE call_category = 'group_call'") or 0
        answered = db.scalar("SELECT COUNT(*) FROM call_record WHERE result_label IN ('completed', 'answered', 'connected', 'disconnected')") or 0

        self._stat_labels["total"].setText(f"Total: {total:,}")
        self._stat_labels["voice"].setText(f"Voice: {voice:,}")
        self._stat_labels["video"].setText(f"Video: {video:,}")
        self._stat_labels["missed"].setText(f"Missed: {missed:,}")
        self._stat_labels["outgoing"].setText(f"Outgoing: {outgoing:,}")
        self._stat_labels["incoming"].setText(f"Incoming: {incoming:,}")
        self._stat_labels["voice_chats"].setText(f"Voice Chat: {voice_chats:,}")

        # Update filter button labels with counts
        btn_counts = {
            "all": total, "incoming": incoming, "outgoing": outgoing,
            "voice": voice, "video": video, "group": group_calls,
            "multi_person": multi, "voice_chat": voice_chats,
            "missed": missed, "completed": answered,
        }
        btn_labels = {
            "all": "All", "incoming": "Incoming", "outgoing": "Outgoing",
            "voice": "Voice", "video": "Video", "group": "Group",
            "multi_person": "Multi-Person", "voice_chat": "Voice Chat",
            "missed": "Missed", "completed": "Answered",
        }
        for fid, btn in self._filter_btns.items():
            cnt = btn_counts.get(fid, 0)
            label = btn_labels.get(fid, fid)
            btn.setText(f"{label} ({cnt:,})" if cnt else label)
            btn.setMinimumWidth(max(100, btn.sizeHint().width() + 6))

