"""
Conversations page — full conversation list with search,
filters, and sorting.  Each row shows the last message preview
plus an avatar (image or initials) in WhatsApp style.  Double-
click a row to open the chat viewer.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListView, QMenu, QPushButton, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from app.config import CHAT_TYPE_LABELS, format_timestamp, timestamp_to_local_datetime
from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

# Custom data role for full conversation dict
CONV_DATA_ROLE = Qt.UserRole + 200
# Custom data role for message search snippet text
SEARCH_SNIPPET_ROLE = Qt.UserRole + 201

# Avatar colors
AVATAR_COLORS = [
    "#00897b", "#6a1b9a", "#c62828", "#1565c0",
    "#ef6c00", "#2e7d32", "#ad1457", "#4527a0",
    "#00838f", "#827717", "#4e342e", "#37474f",
]


class ConversationsModel(BaseLazyTableModel):
    """Conversations model with last message preview data."""

    _columns = [
        ("c.display_name", "Name"),
        ("c.chat_type", "Type"),
        ("c.message_count", "Messages"),
        ("c.media_count", "Media"),
        ("c.participant_count", "Members"),
        ("c.last_message_ts", "Last Message"),
        ("c.first_message_ts", "First Message"),
    ]
    _base_sql = None  # Built dynamically on first use

    @classmethod
    def _build_base_sql(cls):
        """Build base SQL, detecting which pre-computed columns exist."""
        if cls._base_sql is not None:
            return cls._base_sql
        from app.services.database import Database
        db = Database.get()
        try:
            conv_cols = [r[1] for r in db.fetchall("PRAGMA table_info(conversation)")]
        except Exception:
            conv_cols = []
        has_last_msg = "last_msg_text" in conv_cols
        has_ghost = "ghost_count" in conv_cols
        has_unread = "unread_count" in conv_cols

        cls._base_sql = f"""
        SELECT c.display_name, c.chat_type, c.message_count, c.media_count,
               c.participant_count, c.last_message_ts, c.first_message_ts,
               c.id, c.jid_raw_string,
               COALESCE(c.avatar_blob, ct.avatar_blob) AS avatar_blob,
               {"c.last_msg_status" if has_last_msg else "NULL"} AS last_msg_status,
               {"c.last_msg_from_me" if has_last_msg else "NULL"} AS last_msg_from_me,
               ct.is_business,
               {"c.ghost_count" if has_ghost else "0"} AS ghost_count,
               {"c.last_msg_text" if has_last_msg else "NULL"} AS last_msg_text,
               {"c.last_msg_sender" if has_last_msg else "NULL"} AS last_msg_sender,
               c.community_parent_id,
               c.is_archived, c.is_pinned, c.is_locked, c.is_muted,
               ct.is_blocked,
               (SELECT c2.display_name FROM conversation c2
                WHERE c2.id = c.community_parent_id LIMIT 1) AS community_name,
               (SELECT COUNT(*) FROM call_record cr
                WHERE cr.conversation_id = c.id) AS call_count,
               -- Prefer WhatsApp's own unseen_message_count (source_unseen_count)
               -- when available: it's the AUTHORITATIVE count the native app
               -- uses for the blue "N unread messages" pill, and it matches
               -- exactly with available_message_view's understanding of
               -- "available chat msgs".  Fall back to our status<6 heuristic
               -- only if source_unseen_count wasn't ingested (old DBs).
               COALESCE(c.source_unseen_count,
                        {"c.unread_count" if has_unread else "0"}) AS unread_count,
               COALESCE(ct.is_saved, 0) AS contact_is_saved
        FROM conversation c
        LEFT JOIN contact ct ON ct.id = (
            SELECT jtc.contact_id FROM jid_to_contact jtc
            WHERE jtc.jid_raw_string = c.jid_raw_string LIMIT 1
        )
        """
        return cls._base_sql
    _count_sql = "SELECT COUNT(*) FROM conversation c"
    _default_order = "c.last_message_ts DESC"

    def __init__(self, parent=None):
        super().__init__(parent)
        # conv_id -> snippet text for message search mode
        self._search_snippets: dict[int, str] = {}

    def set_search_snippets(self, snippets: dict[int, str]) -> None:
        """Set the message search snippet map (conv_id -> matching text)."""
        self._search_snippets = snippets

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 1:
                return CHAT_TYPE_LABELS.get(raw, raw or "")
            if col in (5, 6) and raw:
                return format_timestamp(raw, "datetime")
            if col in (2, 3, 4) and raw is not None:
                return f"{raw:,}"
            return str(raw) if raw is not None else ""

        if role == Qt.UserRole:
            return row_data[7]  # id
        if role == Qt.UserRole + 1:
            return row_data[0]  # display_name

        if role == CONV_DATA_ROLE:
            # Pre-format timestamp string to avoid datetime work in every paint()
            last_ts = row_data[5]
            time_str = ""
            if last_ts:
                try:
                    dt = timestamp_to_local_datetime(last_ts)
                    now = datetime.now(tz=dt.tzinfo)
                    if dt.date() == now.date():
                        time_str = dt.strftime("%H:%M")
                    elif (now - dt).days < 7:
                        time_str = dt.strftime("%a")
                    else:
                        time_str = dt.strftime("%m/%d/%y")
                except (ValueError, OSError):
                    pass
            return {
                "display_name": row_data[0] or "",
                "chat_type": row_data[1] or "personal",
                "message_count": row_data[2] or 0,
                "media_count": row_data[3] or 0,
                "participant_count": row_data[4] or 0,
                "last_message_ts": row_data[5],
                "first_message_ts": row_data[6],
                "id": row_data[7],
                "jid": row_data[8] or "",
                "avatar_blob": row_data[9] if len(row_data) > 9 else None,
                "last_msg_status": row_data[10] if len(row_data) > 10 else None,
                "last_msg_from_me": bool(row_data[11]) if len(row_data) > 11 and row_data[11] else False,
                "is_business": bool(row_data[12]) if len(row_data) > 12 and row_data[12] else False,
                "ghost_count": row_data[13] if len(row_data) > 13 else 0,
                "last_msg_text": row_data[14] if len(row_data) > 14 else "",
                "last_msg_sender": row_data[15] if len(row_data) > 15 else "",
                "community_parent_id": row_data[16] if len(row_data) > 16 else None,
                "is_archived": bool(row_data[17]) if len(row_data) > 17 and row_data[17] else False,
                "is_pinned": bool(row_data[18]) if len(row_data) > 18 and row_data[18] else False,
                "is_locked": bool(row_data[19]) if len(row_data) > 19 and row_data[19] else False,
                "is_muted": bool(row_data[20]) if len(row_data) > 20 and row_data[20] else False,
                "is_blocked": bool(row_data[21]) if len(row_data) > 21 and row_data[21] else False,
                "community_name": row_data[22] if len(row_data) > 22 else None,
                "call_count": row_data[23] if len(row_data) > 23 else 0,
                "unread_count": row_data[24] if len(row_data) > 24 else 0,
                "is_saved": bool(row_data[25]) if len(row_data) > 25 and row_data[25] else False,
                "time_str": time_str,
            }

        if role == SEARCH_SNIPPET_ROLE:
            conv_id = row_data[7]
            return self._search_snippets.get(conv_id, "")

        if role == Qt.ForegroundRole and col == 2:
            cnt = row_data[2]
            if cnt and cnt > 10000:
                return QColor("#ef5350")
            if cnt and cnt > 1000:
                return QColor("#ffa726")
        if role == Qt.TextAlignmentRole and col in (2, 3, 4):
            return Qt.AlignRight | Qt.AlignVCenter
        return None


class ConversationDelegate(QStyledItemDelegate):
    """WhatsApp-style conversation list item with avatar, name, preview, time."""

    ROW_HEIGHT = 72
    AVATAR_SIZE = 48
    PAD = 12

    F_NAME = QFont("Segoe UI", 12)
    F_PREVIEW = QFont("Segoe UI", 10)
    F_TIME = QFont("Segoe UI", 9)
    F_BADGE = QFont("Segoe UI", 8, QFont.Bold)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._avatar_cache: dict[int, QPixmap | None] = {}
        self._is_light = ThemeManager.get().is_light
        # Pre-create QFontMetrics to avoid allocations in every paint() call
        self._fm_name = QFontMetrics(self.F_NAME)
        self._fm_preview = QFontMetrics(self.F_PREVIEW)
        self._fm_time = QFontMetrics(self.F_TIME)
        self._fm_badge = QFontMetrics(self.F_BADGE)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        conv = index.data(CONV_DATA_ROLE)
        if not conv:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = option.rect
        x = rect.x() + self.PAD
        y = rect.y()
        w = rect.width() - 2 * self.PAD

        # Selection highlight
        from PySide6.QtWidgets import QStyle
        if option.state & QStyle.StateFlag.State_Selected:
            if self._is_light:
                painter.fillRect(rect, QColor(0, 137, 123, 20))
            else:
                painter.fillRect(rect, QColor(0, 188, 212, 20))

        # Bottom separator
        if self._is_light:
            painter.setPen(QColor(0, 0, 0, 12))
        else:
            painter.setPen(QColor(255, 255, 255, 15))
        painter.drawLine(x + self.AVATAR_SIZE + 12, rect.bottom(),
                         rect.right() - self.PAD, rect.bottom())

        # Avatar circle
        av_x = x
        av_y = y + (self.ROW_HEIGHT - self.AVATAR_SIZE) // 2
        cid = conv["id"]

        # Try profile picture first
        avatar_drawn = False
        avatar_blob = conv.get("avatar_blob")
        if avatar_blob and len(avatar_blob) > 100:
            pxm = self._get_avatar(cid, avatar_blob)
            if pxm and not pxm.isNull():
                clip = QPainterPath()
                clip.addEllipse(float(av_x), float(av_y),
                                float(self.AVATAR_SIZE), float(self.AVATAR_SIZE))
                painter.setClipPath(clip)
                scaled = pxm.scaled(self.AVATAR_SIZE, self.AVATAR_SIZE,
                                    Qt.KeepAspectRatioByExpanding,
                                    Qt.SmoothTransformation)
                # Center the scaled image in the avatar area
                dx = (scaled.width() - self.AVATAR_SIZE) // 2
                dy_img = (scaled.height() - self.AVATAR_SIZE) // 2
                painter.drawPixmap(av_x - dx, av_y - dy_img, scaled)
                painter.setClipping(False)
                avatar_drawn = True

        if not avatar_drawn:
            # Fallback to colored circle with initials
            name = conv["display_name"] or conv["jid"]
            initials = "".join(
                ch[0] for ch in name.split()[:2] if ch and ch[0].isalpha()
            )
            if not initials:
                initials = "#"
            bg = QColor(AVATAR_COLORS[cid % len(AVATAR_COLORS)])
            path = QPainterPath()
            path.addEllipse(float(av_x), float(av_y),
                            float(self.AVATAR_SIZE), float(self.AVATAR_SIZE))
            painter.fillPath(path, bg)
            painter.setFont(QFont("Segoe UI", 14, QFont.Bold))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(av_x, av_y, self.AVATAR_SIZE, self.AVATAR_SIZE),
                             Qt.AlignCenter, initials[:2].upper())

        # Text area
        tx = av_x + self.AVATAR_SIZE + 12
        tw = w - self.AVATAR_SIZE - 12

        # Chat type badge
        chat_type = conv["chat_type"]
        type_indicator = ""
        if chat_type == "group":
            type_indicator = "\u2630 "
        elif chat_type == "community":
            type_indicator = "\u2302 "
        elif chat_type == "broadcast":
            type_indicator = "\u25B7 "
        elif chat_type == "newsletter":
            type_indicator = "\u2637 "

        # Name — prefix ~ for unsaved contacts in personal chats
        raw_name = conv["display_name"] or conv["jid"]
        if chat_type == "personal" and not conv.get("is_saved") and raw_name and not raw_name.startswith("~"):
            raw_name = "~" + raw_name
        display_name = type_indicator + raw_name
        # State indicators after name
        if conv.get("is_blocked"):
            display_name += " \u26D4"  # no-entry sign
        if conv.get("is_locked"):
            display_name += " \U0001F512"  # lock
        if conv.get("is_archived"):
            display_name += " \u2193"  # down arrow for archived
        painter.setFont(self.F_NAME)
        painter.setPen(QColor(17, 27, 33) if self._is_light else QColor(233, 237, 239))
        fm = self._fm_name
        # Reserve extra space for BIZ badge if business
        name_max_w = tw - 80 - (36 if conv.get("is_business") else 0)
        elided = fm.elidedText(display_name, Qt.ElideRight, name_max_w)
        painter.drawText(QRect(tx, y + 12, name_max_w, 22),
                         Qt.AlignLeft | Qt.AlignVCenter, elided)

        # Business "BIZ" badge (drawn as small rounded rect after name)
        if conv.get("is_business"):
            name_drawn_w = fm.horizontalAdvance(elided)
            biz_x = tx + name_drawn_w + 6
            biz_y = y + 14
            biz_w, biz_h = 30, 16
            biz_path = QPainterPath()
            biz_path.addRoundedRect(float(biz_x), float(biz_y), float(biz_w), float(biz_h), 4, 4)
            if self._is_light:
                painter.fillPath(biz_path, QColor(0, 137, 123, 30))
                painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                painter.setPen(QColor(0, 105, 92))
            else:
                painter.fillPath(biz_path, QColor(0, 188, 212, 50))
                painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                painter.setPen(QColor(0, 188, 212))
            painter.drawText(QRect(biz_x, biz_y, biz_w, biz_h),
                             Qt.AlignCenter, "BIZ")

        # Time (pre-formatted in model to avoid datetime work per paint)
        time_str = conv.get("time_str", "")

        # Time + delivery ticks
        tick_str = ""
        gray_tick_color = QColor(100, 115, 130) if self._is_light else QColor(138, 161, 174)
        if conv.get("last_msg_from_me"):
            status = conv.get("last_msg_status") or 0
            if status >= 6:
                tick_str = "\u2713\u2713 "
                painter.setFont(self.F_TIME)
                painter.setPen(QColor(83, 189, 237))  # blue ticks
                painter.drawText(QRect(tx + tw - 80, y + 14, 24, 18),
                                 Qt.AlignRight | Qt.AlignVCenter, "\u2713\u2713")
            elif status == 5:
                tick_str = "\u2713\u2713 "
                painter.setFont(self.F_TIME)
                painter.setPen(gray_tick_color)
                painter.drawText(QRect(tx + tw - 80, y + 14, 24, 18),
                                 Qt.AlignRight | Qt.AlignVCenter, "\u2713\u2713")
            elif status == 4:
                tick_str = "\u2713 "
                painter.setFont(self.F_TIME)
                painter.setPen(gray_tick_color)
                painter.drawText(QRect(tx + tw - 80, y + 14, 16, 18),
                                 Qt.AlignRight | Qt.AlignVCenter, "\u2713")

        tick_w = 26 if tick_str else 0
        painter.setFont(self.F_TIME)
        painter.setPen(QColor(100, 115, 130) if self._is_light else QColor(138, 161, 174))
        painter.drawText(QRect(tx + tw - 80 + tick_w, y + 14, 80 - tick_w, 18),
                         Qt.AlignRight | Qt.AlignVCenter, time_str)

        # Community badge (shown on preview line before message text)
        community_badge_w = 0
        community_name = conv.get("community_name")
        if community_name and conv.get("community_parent_id"):
            comm_label = f"\u2302 {community_name}"
            painter.setFont(self.F_BADGE)
            fm_badge = self._fm_badge
            comm_w = fm_badge.horizontalAdvance(comm_label) + 12
            comm_x = tx
            comm_y = y + 38
            comm_path = QPainterPath()
            comm_path.addRoundedRect(float(comm_x), float(comm_y), float(comm_w), 16.0, 8, 8)
            if self._is_light:
                painter.fillPath(comm_path, QColor(103, 58, 183, 25))
                painter.setPen(QColor(103, 58, 183))
            else:
                painter.fillPath(comm_path, QColor(149, 117, 205, 50))
                painter.setPen(QColor(179, 157, 219))
            painter.drawText(QRect(comm_x + 6, comm_y, comm_w - 6, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, comm_label)
            community_badge_w = comm_w + 4

        # Preview line: message search snippet (if present) or last message text
        search_snippet = index.data(SEARCH_SNIPPET_ROLE) if index else ""
        msg_count = conv.get("message_count", 0)
        last_text = conv.get("last_msg_text") or ""
        last_sender = conv.get("last_msg_sender") or ""
        chat_type = conv["chat_type"]

        sender_prefix = ""
        if search_snippet:
            # Show the matching message snippet with a search icon
            snippet_clean = search_snippet.replace("\n", " ").strip()[:120]
            preview = "\U0001F50D " + snippet_clean
        elif last_text:
            # Clean up: single line, no newlines
            last_text = last_text.replace("\n", " ").strip()[:120]
            # Map type_label to readable labels
            type_labels = {
                "image": "\U0001F4F7 Photo", "video": "\U0001F4F9 Video",
                "audio": "\U0001F3B5 Audio", "voice": "\U0001F3A4 Voice message",
                "document": "\U0001F4C4 Document", "sticker": "Sticker",
                "gif": "GIF", "location": "\U0001F4CD Location",
                "vcard": "\U0001F464 Contact", "poll": "\U0001F4CA Poll",
            }
            preview = type_labels.get(last_text, last_text)
            if conv.get("last_msg_from_me"):
                sender_prefix = "You: "
            elif chat_type in ("group", "community") and last_sender:
                short = last_sender.split()[0] if " " in last_sender else last_sender
                sender_prefix = short + ": "
        elif msg_count:
            preview = f"{msg_count:,} messages"
        else:
            preview = "No messages"

        painter.setFont(self.F_PREVIEW)
        fm2 = self._fm_preview
        preview_x = tx + community_badge_w
        preview_w = tw - community_badge_w

        if search_snippet:
            # Highlight snippet in a slightly different color to stand out
            painter.setPen(QColor(0, 137, 123, 220) if self._is_light else QColor(0, 188, 212, 200))
            elided2 = fm2.elidedText(preview, Qt.ElideRight, preview_w)
            painter.drawText(QRect(preview_x, y + 38, preview_w, 20),
                             Qt.AlignLeft | Qt.AlignVCenter, elided2)
        elif sender_prefix:
            # Draw sender prefix in teal, then message text in gray
            teal = QColor(0, 137, 123) if self._is_light else QColor(0, 188, 212)
            gray = QColor(100, 115, 130, 200) if self._is_light else QColor(138, 161, 174, 180)
            prefix_w = fm2.horizontalAdvance(sender_prefix)
            painter.setPen(teal)
            painter.drawText(QRect(preview_x, y + 38, prefix_w, 20),
                             Qt.AlignLeft | Qt.AlignVCenter, sender_prefix)
            remaining_w = preview_w - prefix_w
            elided2 = fm2.elidedText(preview, Qt.ElideRight, max(remaining_w, 20))
            painter.setPen(gray)
            painter.drawText(QRect(preview_x + prefix_w, y + 38, remaining_w, 20),
                             Qt.AlignLeft | Qt.AlignVCenter, elided2)
        else:
            painter.setPen(QColor(100, 115, 130, 200) if self._is_light else QColor(138, 161, 174, 180))
            elided2 = fm2.elidedText(preview, Qt.ElideRight, preview_w)
            painter.drawText(QRect(preview_x, y + 38, preview_w, 20),
                             Qt.AlignLeft | Qt.AlignVCenter, elided2)

        # Badges row (right-aligned, y+40) — track x offset so they don't overlap
        badge_right = tx + tw  # right edge for badge placement

        # Unread count badge (green pill with exact count, forensic-grade).
        # The full number is rendered with no "1K+" truncation —
        # forensic context means one-off large counts can themselves
        # be meaningful evidence.  Pill widens to fit up to 6 digits.
        unread = conv.get("unread_count", 0) or 0
        if unread > 0:
            unread_text = f"{unread:,}"  # 1234 → "1,234"
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            # Recompute FM for this font to get accurate width
            from PySide6.QtGui import QFontMetrics as _QFM
            _ufm = _QFM(painter.font())
            uw = max(22, _ufm.horizontalAdvance(unread_text) + 14)
            ux = badge_right - uw
            unread_y = y + 40
            unread_path = QPainterPath()
            unread_path.addRoundedRect(float(ux), float(unread_y), float(uw), 18.0, 9, 9)
            painter.fillPath(unread_path, QColor(37, 211, 102))  # WhatsApp green
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(ux, unread_y, uw, 18), Qt.AlignCenter, unread_text)
            badge_right = ux - 2

        # Pinned badge
        if conv.get("is_pinned"):
            pin_text = "\U0001F4CC"
            painter.setFont(self.F_BADGE)
            pw = 20
            px_ = badge_right - pw
            painter.setPen(QColor(0, 137, 123) if self._is_light else QColor(0, 188, 212))
            painter.drawText(QRect(px_, y + 40, pw, 18), Qt.AlignCenter, pin_text)
            badge_right = px_ - 2

        # Muted badge
        if conv.get("is_muted"):
            mute_text = "\U0001F515"
            painter.setFont(self.F_BADGE)
            mw = 20
            mx = badge_right - mw
            painter.setPen(QColor(160, 170, 175) if self._is_light else QColor(120, 130, 140))
            painter.drawText(QRect(mx, y + 40, mw, 18), Qt.AlignCenter, mute_text)
            badge_right = mx - 2

        # Message count badge (for high-volume chats)
        if msg_count > 5000:
            badge_text = f"{msg_count // 1000}K"
            bw = max(28, fm.horizontalAdvance(badge_text) + 12)
            bx = badge_right - bw
            badge_y = y + 40
            badge_path = QPainterPath()
            badge_path.addRoundedRect(float(bx), float(badge_y), float(bw), 18.0, 9, 9)
            if self._is_light:
                painter.fillPath(badge_path, QColor(0, 137, 123, 30))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(0, 105, 92))
            else:
                painter.fillPath(badge_path, QColor(0, 188, 212, 50))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(0, 188, 212))
            painter.drawText(QRect(bx, badge_y, bw, 18), Qt.AlignCenter, badge_text)
            badge_right = bx - 4  # move left for next badge

        # Ghost message badge
        ghost_count = conv.get("ghost_count", 0)
        if ghost_count:
            ghost_text = f"\u2718 {ghost_count}"
            gw = max(36, fm.horizontalAdvance(ghost_text) + 10)
            gx = badge_right - gw
            ghost_y = y + 40
            ghost_path = QPainterPath()
            ghost_path.addRoundedRect(float(gx), float(ghost_y), float(gw), 18.0, 9, 9)
            if self._is_light:
                painter.fillPath(ghost_path, QColor(200, 60, 60, 25))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(180, 60, 60))
            else:
                painter.fillPath(ghost_path, QColor(80, 40, 40, 120))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(255, 120, 120))
            painter.drawText(QRect(gx, ghost_y, gw, 18), Qt.AlignCenter, ghost_text)
            badge_right = gx - 4

        # Call history badge
        call_count = conv.get("call_count", 0)
        if call_count:
            call_text = f"\u260E {call_count}"
            painter.setFont(self.F_BADGE)
            cw = max(36, fm.horizontalAdvance(call_text) + 10)
            cx = badge_right - cw
            call_y = y + 40
            call_path = QPainterPath()
            call_path.addRoundedRect(float(cx), float(call_y), float(cw), 18.0, 9, 9)
            if self._is_light:
                painter.fillPath(call_path, QColor(33, 150, 243, 25))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(21, 101, 192))
            else:
                painter.fillPath(call_path, QColor(33, 150, 243, 50))
                painter.setFont(self.F_BADGE)
                painter.setPen(QColor(100, 181, 246))
            painter.drawText(QRect(cx, call_y, cw, 18), Qt.AlignCenter, call_text)

        painter.restore()

    def _get_avatar(self, conv_id: int, blob: bytes) -> QPixmap | None:
        """Convert avatar BLOB to QPixmap with caching."""
        if conv_id in self._avatar_cache:
            return self._avatar_cache[conv_id]
        pxm = QPixmap()
        pxm.loadFromData(blob)
        if pxm.isNull():
            self._avatar_cache[conv_id] = None
            return None
        self._avatar_cache[conv_id] = pxm
        # Evict old entries to limit memory
        if len(self._avatar_cache) > 2000:
            oldest = next(iter(self._avatar_cache))
            del self._avatar_cache[oldest]
        return pxm


class ConversationsPage(QWidget):
    conversation_selected = Signal(int, str, str)  # conv_id, name, search_keyword

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Conversations")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(self._tm.header_label_style())
        header.addWidget(self._count_label)
        header.addStretch()
        layout.addLayout(header)

        # Toolbar: search bar + search mode combo
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("\U0001F50D  Search conversations...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)

        # Search mode toggle: Chat Name vs Messages
        self._search_mode = QComboBox()
        self._search_mode.addItem("Chat Name")
        self._search_mode.addItem("Messages")
        self._search_mode.setFixedHeight(36)
        self._search_mode.setFixedWidth(120)
        self._search_mode.setToolTip("Search by conversation name or message content")
        self._search_mode.currentIndexChanged.connect(self._on_search_mode_changed)
        toolbar.addWidget(self._search_mode)

        # Tab bar (Row 2): type tabs + More dropdown + view toggle icons
        tab_row = QHBoxLayout()
        tab_row.setSpacing(0)
        tab_row.setContentsMargins(0, 0, 0, 0)
        self._filter_btns: dict[str, QPushButton] = {}
        self._tab_counts: dict[str, int] = {}
        for fid, label in [("home", "Home"), ("all", "All"),
                           ("personal", "Personal"),
                           ("group", "Groups"), ("community", "Communities"),
                           ("newsletter", "Channels"),
                           ("business", "Business")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(34)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet(self._tm.tab_bar_style())
            btn.clicked.connect(self._on_filter)
            if fid == "home":
                btn.setChecked(True)
            tab_row.addWidget(btn)
            self._filter_btns[fid] = btn

        # "More" dropdown combining state filters + quick filters
        self._more_btn = QPushButton("More \u25BC")
        self._more_btn.setFixedHeight(34)
        self._more_btn.setStyleSheet(self._tm.tab_bar_style())
        more_menu = QMenu(self._more_btn)

        # State filters as checkable menu actions
        self._state_filter_btns: dict[str, QPushButton] = {}  # compat stub (unused)
        self._state_actions: dict[str, object] = {}
        for fid, label in [("archived", "Archived"), ("locked", "Locked"),
                           ("blocked", "Blocked"), ("muted", "Muted")]:
            act = more_menu.addAction(label)
            act.setCheckable(True)
            act.setProperty("filter_id", fid)
            act.triggered.connect(self._on_state_filter_action)
            self._state_actions[fid] = act

        more_menu.addSeparator()

        # Quick filters as checkable menu actions
        self._quick_filter_btns: dict[str, QPushButton] = {}  # compat stub (unused)
        self._quick_actions: dict[str, object] = {}
        for qid, qlabel in [("has_links", "\U0001F517 Has Links"),
                             ("has_documents", "\U0001F4C4 Has Documents"),
                             ("has_media", "\U0001F4F7 Has Media"),
                             ("unread", "\u2709 Unread")]:
            act = more_menu.addAction(qlabel)
            act.setCheckable(True)
            act.setProperty("quick_filter_id", qid)
            act.triggered.connect(self._on_quick_filter)
            self._quick_actions[qid] = act

        self._more_btn.setMenu(more_menu)
        tab_row.addWidget(self._more_btn)

        tab_row.addStretch()

        # View toggle icon buttons (right end of Row 2)
        self._view_list_btn = QPushButton("\u2261")
        self._view_list_btn.setCheckable(True)
        self._view_list_btn.setChecked(True)
        self._view_list_btn.setFixedSize(34, 34)
        self._view_list_btn.setToolTip("List View")
        self._view_list_btn.setStyleSheet(self._tm.view_toggle_btn_style())
        self._view_list_btn.clicked.connect(lambda: self._set_view("list"))
        tab_row.addWidget(self._view_list_btn)

        self._view_table_btn = QPushButton("\u2637")
        self._view_table_btn.setCheckable(True)
        self._view_table_btn.setFixedSize(34, 34)
        self._view_table_btn.setToolTip("Table View")
        self._view_table_btn.setStyleSheet(self._tm.view_toggle_btn_style())
        self._view_table_btn.clicked.connect(lambda: self._set_view("table"))
        tab_row.addWidget(self._view_table_btn)

        # Calendar date filter toggle
        self._cal_btn = QPushButton("\U0001F4C5")
        self._cal_btn.setCheckable(True)
        self._cal_btn.setFixedSize(34, 34)
        self._cal_btn.setToolTip("Calendar date filter")
        self._cal_btn.setStyleSheet(self._tm.view_toggle_btn_style())
        self._cal_btn.clicked.connect(self._toggle_calendar)
        tab_row.addWidget(self._cal_btn)

        layout.addLayout(toolbar)
        layout.addLayout(tab_row)

        # Calendar heatmap (hidden by default)
        from app.views.widgets.calendar_heatmap import CalendarHeatmapWidget
        self._calendar = CalendarHeatmapWidget()
        self._calendar.setVisible(False)
        self._calendar.range_selected.connect(self._on_calendar_range)
        self._calendar.date_selected.connect(self._on_calendar_date)
        self._calendar.range_cleared.connect(self._clear_calendar_filter)
        layout.addWidget(self._calendar)

        # Results count label (shows "N conversations matching 'query'")
        self._results_label = QLabel("")
        self._results_label.setStyleSheet(self._tm.hint_label_style())
        self._results_label.setVisible(False)
        layout.addWidget(self._results_label)

        # Model (shared between list and table view)
        self._model = ConversationsModel()

        # List view (WhatsApp-style with avatars)
        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_delegate = ConversationDelegate()
        self._list_view.setItemDelegate(self._list_delegate)
        self._list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSpacing(0)
        self._list_view.setStyleSheet(self._tm.list_view_style())
        self._list_view.verticalScrollBar().setSingleStep(24)
        self._list_view.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list_view, 1)

        # Table view (hidden by default)
        from PySide6.QtWidgets import QTableView
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.doubleClicked.connect(self._on_double_click)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 90), (2, 90), (3, 70), (4, 80), (5, 140), (6, 140)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        self._table.setVisible(False)
        layout.addWidget(self._table, 1)

        # Search debounce
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._search.returnPressed.connect(self._apply)
        self._current_filter = "home"
        self._current_view = "list"
        self._current_search_mode = "name"  # "name" or "messages"
        self._has_fts5: bool | None = None  # cached FTS5 availability check
        QTimer.singleShot(50, self._apply)

    def _set_view(self, mode: str):
        self._current_view = mode
        self._view_list_btn.setChecked(mode == "list")
        self._view_table_btn.setChecked(mode == "table")
        self._list_view.setVisible(mode == "list")
        self._table.setVisible(mode == "table")

    def _toggle_calendar(self):
        visible = not self._calendar.isVisible()
        self._calendar.setVisible(visible)
        self._cal_btn.setChecked(visible)
        if visible:
            self._calendar.load_data(None)  # Global — all conversations

    def _on_calendar_date(self, d):
        """Single day on global calendar."""
        from datetime import datetime
        from_ms = int(datetime(d.year, d.month, d.day).timestamp() * 1000)
        to_ms = int(datetime(d.year, d.month, d.day, 23, 59, 59).timestamp() * 1000)
        self._apply_calendar_date_range(from_ms, to_ms, d.strftime("%b %d, %Y"))

    def _on_calendar_range(self, start, end):
        """Date range on global calendar."""
        from datetime import datetime
        from_ms = int(datetime(start.year, start.month, start.day).timestamp() * 1000)
        to_ms = int(datetime(end.year, end.month, end.day, 23, 59, 59).timestamp() * 1000)
        label = f"{start.strftime('%b %d')} \u2013 {end.strftime('%b %d, %Y')}"
        self._apply_calendar_date_range(from_ms, to_ms, label)

    def _apply_calendar_date_range(self, from_ms: int, to_ms: int, label: str):
        """Filter conversation list to only show chats with messages in date range."""
        where = (
            "EXISTS (SELECT 1 FROM message m2 WHERE m2.conversation_id = c.id "
            f"AND m2.timestamp >= {from_ms} AND m2.timestamp <= {to_ms})"
        )
        self._model.load(where=where, order=self._model._current_order)
        count = self._model.rowCount()
        self._results_label.setText(f"{count:,} conversations with messages in {label}")
        self._results_label.setVisible(True)

    def _clear_calendar_filter(self):
        self._model.load(order=self._model._current_order)
        self._results_label.setVisible(False)

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        for k, b in self._filter_btns.items():
            b.setChecked(k == fid)
        # Uncheck all state filters when a type tab is selected
        for act in self._state_actions.values():
            act.setChecked(False)
        self._current_filter = fid
        self._apply()

    def _on_state_filter_action(self):
        """Handle state filter toggle from More dropdown menu."""
        fid = self.sender().property("filter_id")
        # Toggle: uncheck others in state group
        for k, act in self._state_actions.items():
            act.setChecked(k == fid and act.isChecked())
        # Uncheck type tabs -- state filters are independent
        for k, b in self._filter_btns.items():
            b.setChecked(False)
        self._current_filter = fid if self._state_actions[fid].isChecked() else "home"
        if self._current_filter == "home":
            self._filter_btns["home"].setChecked(True)
        self._apply()

    def _on_quick_filter(self):
        """Handle quick filter toggle from More dropdown menu."""
        self._apply()

    def _on_search_mode_changed(self, idx: int):
        self._current_search_mode = "messages" if idx == 1 else "name"
        placeholder = (
            "\U0001F50D  Search message content..."
            if self._current_search_mode == "messages"
            else "\U0001F50D  Search conversations..."
        )
        self._search.setPlaceholderText(placeholder)
        self._apply()

    def _check_fts5(self) -> bool:
        """Check if message_fts FTS5 table exists (cached)."""
        if self._has_fts5 is None:
            db = Database.get()
            try:
                row = db.fetchone(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_fts'"
                )
                self._has_fts5 = row is not None
            except Exception:
                self._has_fts5 = False
        return self._has_fts5

    def _search_messages(self, text: str) -> dict[int, str]:
        """Search messages by text content. Returns {conv_id: snippet}.

        Uses FTS5 if the message_fts table exists, otherwise falls back to LIKE.
        """
        db = Database.get()
        snippets: dict[int, str] = {}
        if not text:
            return snippets

        if self._check_fts5():
            # FTS5 search -- fast full-text match
            try:
                # Escape FTS5 special chars and use prefix matching
                safe = text.replace('"', '""')
                rows = db.fetchall(
                    "SELECT m.conversation_id, "
                    "snippet(message_fts, 0, '>>>', '<<<', '...', 48) AS snip "
                    "FROM message_fts "
                    "JOIN message m ON m.rowid = message_fts.rowid "
                    f'WHERE message_fts MATCH ? '
                    "GROUP BY m.conversation_id "
                    "LIMIT 5000",
                    (f'"{safe}"',),
                )
                for r in rows:
                    snippets[r[0]] = r[1] if r[1] else ""
                return snippets
            except Exception:
                pass  # FTS5 query failed, fall back to LIKE

        # Fallback: LIKE search (slower but always works)
        rows = db.fetchall(
            "SELECT m.conversation_id, "
            "SUBSTR(m.text_content, MAX(1, INSTR(LOWER(m.text_content), LOWER(?)) - 20), 100) AS snip "
            "FROM message m "
            "WHERE m.text_content LIKE ? AND m.message_type != 7 "
            "GROUP BY m.conversation_id "
            "LIMIT 5000",
            (text, f"%{text}%"),
        )
        for r in rows:
            snippets[r[0]] = r[1] if r[1] else ""
        return snippets

    def _apply(self):
        # Ensure base SQL is built (detects column existence on first call)
        ConversationsModel._build_base_sql()
        self._model._base_sql = ConversationsModel._base_sql

        parts, params = [], []

        # Only show conversations with messages
        parts.append("c.message_count > 0")

        # "home" = WhatsApp home screen (not hidden, not archived)
        # "all" = everything with messages
        if self._current_filter == "home":
            parts.append("COALESCE(c.is_hidden, 0) = 0")
            parts.append("c.is_archived = 0")

        if self._current_filter == "business":
            parts.append("""EXISTS (
                SELECT 1 FROM jid_to_contact jtc
                JOIN contact ct2 ON ct2.id = jtc.contact_id
                WHERE jtc.jid_raw_string = c.jid_raw_string AND ct2.is_business = 1
            )""")
        elif self._current_filter == "archived":
            parts.append("c.is_archived = 1")
        elif self._current_filter == "locked":
            parts.append("c.is_locked = 1")
        elif self._current_filter == "muted":
            parts.append("c.is_muted = 1")
        elif self._current_filter == "blocked":
            parts.append("""EXISTS (
                SELECT 1 FROM jid_to_contact jtc
                JOIN contact ct2 ON ct2.id = jtc.contact_id
                WHERE jtc.jid_raw_string = c.jid_raw_string AND ct2.is_blocked = 1
            )""")
        elif self._current_filter == "group":
            # Groups tab: only standalone groups (not community sub-groups)
            parts.append("c.chat_type = 'group' AND c.community_parent_id IS NULL")
        elif self._current_filter == "community":
            # Communities tab: parent communities + their sub-groups
            parts.append("(c.chat_type = 'community' OR c.community_parent_id IS NOT NULL)")
        elif self._current_filter not in ("all", "home"):
            parts.append("c.chat_type = ?")
            params.append(self._current_filter)

        # Quick filter actions (from More dropdown)
        qf = self._quick_actions
        if qf["has_links"].isChecked():
            parts.append("""EXISTS (
                SELECT 1 FROM message_link_detail mld
                JOIN message ml ON ml.id = mld.message_id
                WHERE ml.conversation_id = c.id
            )""")
        if qf["has_documents"].isChecked():
            parts.append("""EXISTS (
                SELECT 1 FROM message md
                WHERE md.conversation_id = c.id AND md.type_label = 'document'
            )""")
        if qf["has_media"].isChecked():
            parts.append("""EXISTS (
                SELECT 1 FROM message mm
                WHERE mm.conversation_id = c.id
                AND mm.type_label IN ('image', 'video', 'gif', 'sticker')
            )""")
        if qf["unread"].isChecked():
            # Conversations where the last message is not from me (i.e. received)
            parts.append("""(SELECT mr.from_me FROM message mr
                WHERE mr.conversation_id = c.id
                ORDER BY mr.timestamp DESC, mr.sort_id DESC LIMIT 1) = 0""")

        # Text search
        text = self._search.text().strip()
        snippets: dict[int, str] = {}

        if text and self._current_search_mode == "messages":
            # Message content search -- get matching conv IDs + snippets
            snippets = self._search_messages(text)
            if snippets:
                # Filter to only conversations that matched
                placeholders = ",".join("?" * len(snippets))
                parts.append(f"c.id IN ({placeholders})")
                params.extend(list(snippets.keys()))
            else:
                # No matches: force empty result
                parts.append("0")
        elif text:
            # Chat name search -- also matches last message preview text
            parts.append("(c.display_name LIKE ? OR c.jid_raw_string LIKE ? OR c.last_msg_text LIKE ?)")
            params.extend([f"%{text}%", f"%{text}%", f"%{text}%"])

        # Set snippets on model before load
        self._model.set_search_snippets(snippets)

        # For home tab, use WhatsApp's sort_timestamp for authentic ordering.
        # The Community tab still groups its results by parent so the
        # community + its sub-groups stay together (that's intrinsic to
        # the Community filter, not a toggle).
        order = None
        if self._current_filter == "home":
            order = "COALESCE(c.sort_timestamp, c.last_message_ts) DESC"
        elif self._current_filter == "community":
            order = (
                "CASE WHEN c.community_parent_id IS NOT NULL THEN c.community_parent_id "
                "     WHEN c.chat_type = 'community' THEN c.id "
                "     ELSE 999999999 END, "
                "CASE WHEN c.chat_type = 'community' THEN 0 ELSE 1 END, "
                "c.last_message_ts DESC"
            )
        self._model.load(where=" AND ".join(parts), params=tuple(params),
                         order=order if order else "")
        self._count_label.setText(f"{self._model.total_rows:,} conversations")

        # Compute tab counts (run once, lightweight)
        self._update_tab_counts()

        # Update results count label
        has_active_search = bool(text) or any(a.isChecked() for a in self._quick_actions.values())
        if has_active_search:
            if text:
                mode_label = "messages" if self._current_search_mode == "messages" else "names"
                self._results_label.setText(
                    f"{self._model.total_rows:,} conversations matching '{text}' in {mode_label}"
                )
            else:
                self._results_label.setText(
                    f"{self._model.total_rows:,} conversations matching filters"
                )
            self._results_label.setVisible(True)
        else:
            self._results_label.setVisible(False)

    def _update_tab_counts(self):
        """Compute conversation counts per type and update tab labels."""
        db = Database.get()
        base = "COALESCE(c.group_type, 0) != 3 AND c.message_count > 0"
        try:
            rows = db.fetchall(
                f"SELECT c.chat_type, c.community_parent_id, "
                f"COUNT(*) AS cnt "
                f"FROM conversation c WHERE {base} "
                f"GROUP BY c.chat_type, CASE WHEN c.community_parent_id IS NOT NULL THEN 1 ELSE 0 END"
            )
        except Exception:
            return

        counts: dict[str, int] = {}
        total = 0
        community_count = 0
        group_standalone = 0
        for row in rows:
            chat_type = row[0]
            has_parent = row[1] is not None
            cnt = row[2] or 0
            total += cnt

            if chat_type == "community":
                community_count += cnt
            elif chat_type == "group" and has_parent:
                # Sub-group of a community
                community_count += cnt
            elif chat_type == "group" and not has_parent:
                group_standalone += cnt
            else:
                counts[chat_type] = counts.get(chat_type, 0) + cnt

        counts["group"] = group_standalone
        counts["community"] = community_count
        counts["all"] = total

        # Business count (separate query since it crosses types)
        try:
            biz_row = db.fetchone(
                f"SELECT COUNT(*) FROM conversation c "
                f"LEFT JOIN jid_to_contact jtc ON jtc.jid_raw_string = c.jid_raw_string "
                f"LEFT JOIN contact ct2 ON ct2.id = jtc.contact_id "
                f"WHERE {base} AND ct2.is_business = 1"
            )
            counts["business"] = biz_row[0] if biz_row else 0
        except Exception:
            counts["business"] = 0

        # Update button labels
        label_map = {
            "all": "All", "personal": "Personal", "group": "Groups",
            "community": "Communities", "newsletter": "Channels",
            "status": "Status", "business": "Business",
        }
        for fid, btn in self._filter_btns.items():
            n = counts.get(fid, 0)
            base_label = label_map.get(fid, fid.title())
            btn.setText(f"{base_label} ({n:,})" if n else base_label)
        self._tab_counts = counts

    def _on_double_click(self, index):
        conv_id = self._model.data(index, Qt.UserRole)
        name = self._model.data(self._model.index(index.row(), 0), Qt.UserRole + 1)
        if conv_id:
            keyword = (
                self._search.text().strip()
                if self._current_search_mode == "messages"
                else ""
            )
            self.conversation_selected.emit(conv_id, name or "", keyword)

    def refresh_for_timezone_change(self) -> None:
        self._table.viewport().update()
        self._list_view.viewport().update()
