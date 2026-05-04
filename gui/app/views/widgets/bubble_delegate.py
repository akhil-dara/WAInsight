"""
WhatsApp-style bubble delegate for chat message list.
Paints messages as left/right-aligned bubbles with sender names,
media thumbnails, timestamps, delivery ticks, quoted replies,
system messages, link previews, ghost indicators, and media display.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QImage, QImageReader,
    QPainter, QPainterPath, QPixmap,
)
from PySide6.QtWidgets import QApplication, QStyledItemDelegate

# Custom data role for full message dict
MSG_DATA_ROLE = Qt.UserRole + 100

# Regex for detecting URLs
_URL_RE = re.compile(r'https?://\S+')
# Regex for detecting @phone_number or @lid_number mentions in WhatsApp text
_MENTION_RE = re.compile(r'@(\d{10,20})')
# WhatsApp markdown: *bold*, _italic_, ~strikethrough~, ```monospace```
_WA_BOLD_RE = re.compile(r'(?<!\w)\*([^\*\n]+)\*(?!\w)')
_WA_ITALIC_RE = re.compile(r'(?<!\w)_([^_\n]+)_(?!\w)')
_WA_STRIKE_RE = re.compile(r'(?<!\w)~([^~\n]+)~(?!\w)')
_WA_MONO_RE = re.compile(r'```(.*?)```', re.DOTALL)
_WA_MONO_INLINE_RE = re.compile(r'(?<!\w)`([^`\n]+)`(?!\w)')
# Quick check: does text contain any WA markdown chars?
_WA_MD_CHARS = frozenset('*_~`')


def _wa_markdown_to_html(text_escaped: str) -> str:
    """Convert WhatsApp markdown to HTML. Input must be HTML-escaped already."""
    # Process in order: monospace block first (``` ... ```)
    s = _WA_MONO_RE.sub(
        r'<code style="background:rgba(128,128,128,0.15);padding:1px 3px;'
        r'border-radius:3px;font-family:Consolas,monospace;">\1</code>', text_escaped)
    # Inline monospace (` ... `)
    s = _WA_MONO_INLINE_RE.sub(
        r'<code style="background:rgba(128,128,128,0.15);padding:1px 3px;'
        r'border-radius:3px;font-family:Consolas,monospace;">\1</code>', s)
    # Bold *text*
    s = _WA_BOLD_RE.sub(r'<b>\1</b>', s)
    # Italic _text_
    s = _WA_ITALIC_RE.sub(r'<i>\1</i>', s)
    # Strikethrough ~text~
    s = _WA_STRIKE_RE.sub(r'<s>\1</s>', s)
    return s

# Clearer display names for message types without content
_FRIENDLY_TYPE_LABELS = {
    "button_message": "Button message",
    "list_message": "List message",
    "list_reply": "List reply",
    "interactive": "Interactive message",
    "interactive_cta": "Interactive link",
    "interactive_product": "Product message",
    "interactive_carousel": "Carousel",
    "carousel": "Carousel",
    "ai_message": "AI-generated message",
    "deleted": "Deleted message",
    "ephemeral_sync": "Disappearing message sync",
    "media_express_notify": "Express media",
    "status_update": "Shared a status update",
    "newsletter": "Newsletter post",
    "album": "Photo album",
    "vcard_list": "Shared contacts",
    "view_once_voice": "View-once voice note",
    "advanced_chat_privacy": "Advanced chat privacy",
    "scheduled_event": "Scheduled event",
    # Fallbacks for pre-re-ingestion DBs
    "unknown_14": "Shared contacts",
    "unknown_82": "View-once voice note",
    "unknown_112": "Advanced chat privacy",
}


def _extract_domain(url: str) -> str:
    """Extract clean domain from URL."""
    try:
        # Remove protocol
        u = url.split("://", 1)[1] if "://" in url else url
        # Get domain part
        domain = u.split("/", 1)[0].split("?", 1)[0]
        # Remove www.
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except (IndexError, ValueError):
        return ""

# Media root on disk (WhatsApp extracted media)
MEDIA_ROOT = r"C:\Users\owner\Desktop\extracted_backup\20260213_015115\files\Android\media\com.whatsapp\WhatsApp"

# Type label -> emoji icon
TYPE_EMOJI = {
    "image": "\U0001F4F7", "video": "\U0001F4F9", "audio": "\U0001F3B5",
    "voice": "\U0001F3A4", "document": "\U0001F4C4", "sticker": "\U0001F3AD",
    "gif": "\U0001F3AC", "animated_gif": "\U0001F3AC",
    "location": "\U0001F4CD", "live_location": "\U0001F4CD",
    "vcard": "\U0001F464", "poll": "\U0001F4CA", "poll_vote": "\U0001F4CA",
    "group_invite": "\U0001F517", "newsletter": "\U0001F4F0",
    "call_log": "\U0001F4DE",
    "interactive": "\U0001F4AC", "interactive_cta": "\U0001F517",
    "interactive_product": "\U0001F6D2", "product_catalog": "\U0001F6D2",
    "button_message": "\U0001F518", "list_message": "\U0001F4CB",
    "list_reply": "\U0001F4CB", "carousel": "\U0001F3A0",
    "ai_message": "\U0001F916", "album": "\U0001F5BC",
    "status_update": "\U0001F4F1", "ephemeral_sync": "\u23F3",
    "deleted": "\U0001F6AB",
    "vcard_list": "\U0001F465",       # 👥 multiple people
    "view_once_voice": "\U0001F3A4",  # 🎤 microphone
    "advanced_chat_privacy": "\U0001F512",  # 🔒 lock
    "scheduled_event": "\U0001F4C5",  # 📅 calendar
    # Fallbacks for pre-re-ingestion DBs
    "unknown_14": "\U0001F465", "unknown_82": "\U0001F3A4", "unknown_112": "\U0001F512",
}

# System event label -> icon mapping
SYSTEM_ICONS = {
    # ── Confirmed types ──
    "group_subject_changed": "\u270F\uFE0F",       # type 1 ✅
    "participant_added": "\u2795",                  # type 12 ✅
    "participant_left": "\u2796",                   # type 5, 13 ✅
    "participant_removed": "\u2796",                # type 14 ✅
    "security_code_changed": "\U0001F512",          # type 18 ✅
    "participant_joined_via_link": "\u2795",         # type 20 ✅
    "e2e_encrypted": "\U0001F512",                  # type 67 ✅
    "membership_approval_request": "\u2795",         # type 83 ✅
    "admin_promoted": "\U0001F451",                 # type 84 ✅
    "channel_created": "\U0001F4F0",                # type 134 ✅

    # ── Corrected types (from phone validation) ──
    "group_icon_changed": "\U0001F4F7",              # type 6: "X changed the group icon" (photo in message_system_photo_change)
    "group_name_changed": "\u270F\uFE0F",           # legacy: "X changed the group/community name"
    "number_changed": "\U0001F4F1",                 # type 10, 28: "X changed to Y"
    "community_or_group_created": "\U0001F4AC",     # type 11: "X created community/group Y"
    "you_are_admin": "\U0001F451",                  # type 15: "You're an admin"
    "group_link_reset": "\U0001F517",               # type 21, 189: "X reset the group link"
    "group_description_changed": "\U0001F4DD",      # type 27: "X changed the group description"
    "group_add_member_permission": "\u2699\uFE0F",  # type 29: "allow X to add others"
    "group_edit_permission": "\u2699\uFE0F",        # type 30: "X can edit group settings"
    "group_send_message_permission": "\u2699\uFE0F",# type 32: "allow all members to send messages"
    "disappearing_timer_updated": "\u23F0",         # type 56: "updated the message timer...N days"
    "contact_blocked": "\U0001F6AB",                # type 58: "You blocked this contact/business"
    "disappearing_timer_changed": "\u23F0",         # type 59: "message timer was updated"
    "business_meta_managed": "\U0001F3E2",          # type 63, 69, 158: "secure service from Meta"
    "disappearing_messages_changed": "\u23F0",      # type 68: "X changed disappearing messages"
    "participant_joined_community": "\u2795",        # type 79: "X joined from the community"
    "community_admin_changed": "\U0001F451",        # type 81: "X is now/not a community admin"
    "group_join_permission": "\u2699\uFE0F",        # type 85: "turned on/off admin approval to join"
    "group_add_permission_all": "\u2699\uFE0F",     # type 91: "allow all members to add others"
    "group_add_permission_admins": "\u2699\uFE0F",  # type 92: "allow only admins to add others"
    "community_joined": "\u2795",                   # type 99: "You joined from the community"
    "subgroup_removed": "\u2796",                   # type 109, 111, 138: "Group X was removed"
    "subgroup_added": "\u2795",                     # type 110: "X added the group Y"
    "subgroup_unlinked": "\U0001F4AC",              # type 116: "X removed this group from community"
    "message_pinned": "\U0001F4CC",                 # type 118: "X pinned a message"
    "participant_added_with_approval": "\u2795",     # type 120: "X added Y. Tap to change permissions"
    "community_group_joined": "\u2795",              # type 126: "You joined a group via invite"
    "community_description_changed": "\U0001F4DD",  # type 131: "X changed the community description"
    "channel_privacy_notice": "\U0001F512",          # type 132: "channel has added privacy"
    "channel_deleted": "\u2796",                     # type 133: "The channel X was deleted"
    "group_auto_admin_restriction": "\u2699\uFE0F",  # type 142: "group has 256+ members"
    "meta_ai_disclaimer": "\U0001F916",              # type 146: "Only @AI mentions readable by Meta"
    "community_linked_group_join": "\u2795",          # type 149: "Welcome...You joined this group"
    "community_created": "\U0001F4AC",               # type 167: "X created community Y"
    "event_updated": "\U0001F4C5",                   # type 169: "X updated EVENT_NAME"
    "community_owner_changed": "\U0001F451",          # type 173: "Community owner has changed"
    "group_invite_permission": "\u2699\uFE0F",        # type 188: "allow members to add others / link"

    # ── From screenshots + logical deduction ──
    "you_were_added": "\u2795",                      # type 4: "You were added"
    "default_disappearing_timer": "\u23F0",          # type 31: "X uses a default timer..."
    "community_add_permission": "\u2699\uFE0F",      # type 107: "X changed community add settings"
    "community_settings_changed": "\u2699\uFE0F",   # type 108: community settings changed
    "phone_number_privacy": "\U0001F512",            # type 115: "This chat has added privacy for your phone number"
    "community_welcome_joined": "\U0001F44B",        # type 123: "Welcome to the community! You joined"
    "community_group_invite_joined": "\u2795",       # type 125: "You joined a group via invite in the community"
    "participant_joined_from_community": "\u2795",   # type 128: "X joined from the community"
    "contact_card_shown": "",                        # type 129: contact intro card (not rendered)
    "community_auto_added": "\u2795",                # type 144: "You were added because your group was added"
    "ai_disclaimer": "\U0001F916",                   # type 156: "Messages may be generated by AI"
    "system_notification": "\u2139\uFE0F",           # type 2 ❓
}


def _resolve_media_path(relative_path: str, resolved_path: str | None = None,
                        _cache: dict[str, str | None] | None = None) -> str | None:
    """Resolve a media path to an absolute file path on disk.

    Uses the pre-resolved path from analysis.db if available (fastest),
    otherwise falls back to manual resolution from MEDIA_ROOT.
    Optional _cache dict avoids repeated os.path.isfile() calls.
    """
    # Build a cache key from both paths
    cache_key = f"{relative_path or ''}|{resolved_path or ''}"
    if _cache is not None and cache_key in _cache:
        return _cache[cache_key]

    result = _resolve_media_path_uncached(relative_path, resolved_path)

    if _cache is not None:
        _cache[cache_key] = result
        if len(_cache) > 5000:
            # Evict oldest 1000 entries
            keys = list(_cache.keys())[:1000]
            for k in keys:
                del _cache[k]
    return result


def _resolve_media_path_uncached(relative_path: str, resolved_path: str | None = None) -> str | None:
    """Core path resolution without caching."""
    # Use pre-resolved path from analysis.db if it exists on disk
    if resolved_path and os.path.isfile(resolved_path):
        return resolved_path
    if not relative_path:
        return None
    # DB stores paths like "Media/WhatsApp Images/IMG-xxx.jpg"
    # On disk: MEDIA_ROOT/media/WhatsApp Images/IMG-xxx.jpg
    if relative_path.startswith("Media/"):
        rel = relative_path[6:]  # strip "Media/"
    elif relative_path.startswith("/"):
        return None
    else:
        rel = relative_path
    full = os.path.join(MEDIA_ROOT, "media", rel)
    if os.path.isfile(full):
        return full
    full2 = os.path.join(MEDIA_ROOT, relative_path)
    if os.path.isfile(full2):
        return full2
    # Try just the filename in common WhatsApp media subdirectories
    basename = os.path.basename(rel)
    if basename:
        for subdir in ("WhatsApp Stickers", "WhatsApp Images",
                        "WhatsApp Video", "WhatsApp Audio",
                        "WhatsApp Documents", "WhatsApp Animated Gifs",
                        "WhatsApp Voice Notes"):
            subdir_path = os.path.join(MEDIA_ROOT, "media", subdir)
            # Direct file in subdir
            full3 = os.path.join(subdir_path, basename)
            if os.path.isfile(full3):
                return full3
            # Voice Notes have date-based subdirectories (e.g., 202412/)
            if subdir == "WhatsApp Voice Notes" and os.path.isdir(subdir_path):
                try:
                    for date_dir in os.listdir(subdir_path):
                        full4 = os.path.join(subdir_path, date_dir, basename)
                        if os.path.isfile(full4):
                            return full4
                except OSError:
                    pass
    return None


class BubbleDelegate(QStyledItemDelegate):
    """Paints WhatsApp-style chat bubbles with light/dark theme support."""

    # Signals (emitted from editorEvent)
    quote_clicked = Signal(str)      # reply_to_key_id
    sender_clicked = Signal(int)     # contact_id
    link_clicked = Signal(str)       # URL
    media_clicked = Signal(str)      # file_path (absolute)
    audio_play_requested = Signal(str, int)  # (file_path, message_id) for inline playback
    audio_seek_requested = Signal(float)    # seek fraction 0.0-1.0 for waveform click
    text_select_requested = Signal(dict, QPoint)  # msg dict, viewport position
    reaction_clicked = Signal(int)   # message_id (to show reaction detail)
    download_media_requested = Signal(dict)  # msg dict (for single media download)
    replies_clicked = Signal(str)    # source_key_id — show all replies to this message
    cross_chat_quote_clicked = Signal(int, int)  # (conv_id, msg_id) — jump to quoted msg in another chat
    edit_clicked = Signal(int)  # message_id — show edit history (original vs current text)

    # Layout constants - TIGHTER bubbles
    MAX_RATIO = 0.55  # narrower bubbles (was 0.62)
    PAD = 7           # less padding (was 10)
    RADIUS = 10
    V_SPACING = 4     # comfortable gap between bubbles
    THUMB_MAX_H = 300  # max thumbnail/image height in bubble
    SENDER_H = 16
    META_H = 18        # taller meta line for readable timestamps
    QUOTE_H_MAX = 40
    STICKER_MAX = 280  # sticker display size (no bubble) — matches WhatsApp
    ALBUM_GRID_MAX = 200  # max height per album cell

    # Colors -- set by apply_theme() (defaults to dark)
    SENT_BG = QColor(0, 92, 75)
    RECV_BG = QColor(32, 44, 51)
    SYSTEM_BG = QColor(18, 28, 33, 210)
    BOT_BG = QColor(45, 30, 60)
    GHOST_BG = QColor(80, 40, 40)
    TEXT_COL = QColor(233, 237, 239)
    TIME_COL = QColor(148, 171, 184, 220)
    TICK_GRAY = QColor(138, 161, 174)
    TICK_BLUE = QColor(83, 189, 237)
    STAR_COL = QColor(255, 203, 5)
    SYSTEM_COL = QColor(134, 150, 160)
    QUOTE_BAR = QColor(0, 188, 212)
    QUOTE_BG = QColor(0, 0, 0, 60)
    QUOTE_BORDER = QColor(255, 255, 255, 25)
    QUOTE_TEXT = QColor(195, 205, 210)
    REVOKE_COL = QColor(138, 161, 174, 150)
    CAPTION_COL = QColor(180, 190, 195)
    FWD_COL = QColor(138, 161, 174, 170)
    LINK_COL = QColor(83, 189, 237)
    MENTION_COL = QColor(0, 188, 212)
    GHOST_LABEL_COL = QColor(255, 120, 120)
    VCARD_BG = QColor(25, 35, 42, 200)
    VCARD_CIRCLE = QColor(0, 150, 136, 180)
    VCARD_SUB = QColor(148, 171, 184, 180)
    DATE_SEP_BG = QColor(18, 28, 33, 220)
    DATE_SEP_COL = QColor(180, 195, 205)
    STICKER_BG = QColor(255, 255, 255, 30)  # circle behind stickers for visibility
    AVATAR_SIZE = 32
    AVATAR_GAP = 6
    AVATAR_OFFSET = 38  # AVATAR_SIZE + AVATAR_GAP
    SENDER_COLORS = [
        QColor(255, 179, 186), QColor(186, 255, 201),
        QColor(186, 225, 255), QColor(255, 223, 186),
        QColor(218, 186, 255), QColor(255, 255, 186),
        QColor(186, 255, 255), QColor(255, 186, 243),
    ]

    # Fonts (default sizes, can be changed via set_font_size)
    _base_font_size = 10
    F_TEXT = QFont("Segoe UI", 10)
    F_SENDER = QFont("Segoe UI", 9, QFont.Bold)
    F_META = QFont("Segoe UI", 9)
    F_SYSTEM = QFont("Segoe UI", 8)
    F_QUOTE = QFont("Segoe UI", 8)
    F_TYPE = QFont("Segoe UI", 9)
    F_FWD = QFont("Segoe UI", 8)
    F_DATE_SEP = QFont("Segoe UI", 8, QFont.Bold)
    F_LINK = QFont("Segoe UI", 9)
    F_GHOST = QFont("Segoe UI", 7, QFont.Bold)

    def __init__(self, is_group: bool = False, parent=None):
        super().__init__(parent)
        self._is_group = is_group
        self._thumb_cache: dict[int, QPixmap] = {}
        self._media_cache: dict[str, QPixmap] = {}
        # Hit-test rectangles for interactive areas per row
        self._quote_rects: dict[int, QRect] = {}
        self._sender_rects: dict[int, tuple[QRect, int]] = {}  # row -> (rect, contact_id)
        self._link_rects: dict[int, list[tuple[QRect, str]]] = {}  # row -> [(rect, url)]
        self._media_rects: dict[int, tuple[QRect, str]] = {}  # row -> (rect, filepath)
        self._mention_docs: dict[int, tuple] = {}  # row -> (QTextDocument, cx, dy)
        self._reaction_rects: dict[int, tuple[QRect, int]] = {}  # row -> (rect, message_id)
        self._reply_count_rects: dict[int, QRect] = {}  # row -> rect for "N replies" badge
        self._cross_chat_rects: dict[int, tuple[QRect, int, int]] = {}  # row -> (rect, conv_id, msg_id)
        self._edit_rects: dict[int, tuple[QRect, int]] = {}  # row -> (rect, message_id)
        self._download_rects: dict[int, tuple[QRect, dict]] = {}  # row -> (rect, msg dict)
        self._waveform_rects: dict[int, tuple[QRect, int]] = {}  # row -> (rect, msg_id) for seek
        self._file_exists_cache: dict[int, bool] = {}  # msg_id -> file exists on disk
        self._avatar_cache: dict[int, QPixmap] = {}  # sender_id -> circular avatar pixmap
        # ── Performance caches ──
        self._resolve_cache: dict[str, str | None] = {}   # (file_path+resolved) -> absolute path or None
        self._size_hint_cache: dict[tuple[int, int], QSize] = {}  # (msg_id, width) -> cached QSize
        # Animated sticker support (Pillow-based frame extraction)
        self._sticker_anims: dict[str, dict] = {}       # file_path -> {frames, durations, idx, elapsed}
        self._animated_check: dict[str, bool] = {}      # file_path -> is_animated
        self._sticker_fail: set[str] = set()             # paths that failed to render
        self._anim_timer = QTimer()
        self._anim_timer.setInterval(100)  # ~10fps animation advance
        self._anim_timer.timeout.connect(self._flush_anim_repaint)
        self._scrolling = False                           # suppress anim during scroll
        # Inline audio playback state
        self._playing_msg_id: int = 0       # currently playing message id (0 = none)
        self._audio_progress: float = 0.0   # 0.0 to 1.0
        # Device owner info for forensic display
        self._owner_name: str = ""
        self._owner_phone: str = ""
        self._owner_contact_id: int = -1
        self._chat_type: str = ""
        self._conv_name: str = ""
        # ── Performance: cached QFontMetrics + text height cache + regex cache ──
        self._update_cached_metrics()
        self._text_h_cache: dict[int, int] = {}   # msg_id -> computed text height
        self._url_cache: dict[int, list] = {}      # msg_id -> list of URLs found
        self._has_url_cache: dict[int, bool] = {}  # msg_id -> has any URL
        # Reusable QPainterPath to avoid allocations in paint
        self._rpath = QPainterPath()
        # Auto-detect theme
        self._detect_theme()

    def set_group(self, is_group: bool, chat_type: str = ""):
        self._is_group = is_group
        self._chat_type = chat_type

    def set_conv_name(self, name: str):
        self._conv_name = name or ""

    def set_owner_info(self, name: str, phone: str, contact_id: int = -1):
        """Set device owner info for forensic labeling in system events."""
        self._owner_name = name or ""
        self._owner_phone = phone or ""
        self._owner_contact_id = contact_id

    def _detect_theme(self):
        """Auto-detect and apply the current theme colours."""
        try:
            from app.services.theme_manager import ThemeManager
            tm = ThemeManager.get()
            if tm.is_light:
                self.apply_light_theme()
        except Exception:
            pass  # Fall back to dark defaults

    def apply_light_theme(self):
        """Switch all colour constants to light-mode WhatsApp style."""
        self.SENT_BG = QColor(217, 253, 211)       # WhatsApp green
        self.RECV_BG = QColor(255, 255, 255)        # White
        self.SYSTEM_BG = QColor(252, 243, 207, 230) # Warm yellow
        self.BOT_BG = QColor(234, 228, 247)         # Soft purple
        self.GHOST_BG = QColor(255, 235, 235)       # Light red
        self.TEXT_COL = QColor(17, 27, 33)           # Near-black
        self.TIME_COL = QColor(102, 119, 129, 220)  # #667781
        self.TICK_GRAY = QColor(130, 147, 158)
        self.TICK_BLUE = QColor(83, 189, 237)
        self.STAR_COL = QColor(255, 193, 7)
        self.SYSTEM_COL = QColor(85, 96, 104)
        self.QUOTE_BAR = QColor(0, 137, 123)        # Teal
        self.QUOTE_BG = QColor(0, 0, 0, 18)
        self.QUOTE_BORDER = QColor(0, 0, 0, 15)
        self.QUOTE_TEXT = QColor(60, 72, 80)
        self.REVOKE_COL = QColor(130, 147, 158, 160)
        self.CAPTION_COL = QColor(80, 90, 95)
        self.FWD_COL = QColor(102, 119, 129, 180)
        self.LINK_COL = QColor(2, 126, 181)         # Blue
        self.MENTION_COL = QColor(0, 137, 123)
        self.GHOST_LABEL_COL = QColor(211, 47, 47)  # Red
        self.VCARD_BG = QColor(241, 243, 244, 220)
        self.VCARD_CIRCLE = QColor(0, 137, 123, 200)
        self.VCARD_SUB = QColor(102, 119, 129, 200)
        self.DATE_SEP_BG = QColor(255, 255, 255, 240)
        self.DATE_SEP_COL = QColor(85, 96, 104)
        self.STICKER_BG = QColor(0, 0, 0, 22)  # circle behind stickers in light mode
        self.SENDER_COLORS = [
            QColor(180, 60, 80), QColor(30, 130, 76),
            QColor(25, 100, 180), QColor(200, 120, 20),
            QColor(120, 50, 180), QColor(160, 140, 10),
            QColor(0, 140, 140), QColor(180, 50, 160),
        ]

    def apply_dark_theme(self):
        """Switch all colour constants back to dark-mode defaults."""
        self.SENT_BG = QColor(0, 92, 75)
        self.RECV_BG = QColor(32, 44, 51)
        self.SYSTEM_BG = QColor(18, 28, 33, 210)
        self.BOT_BG = QColor(45, 30, 60)
        self.GHOST_BG = QColor(80, 40, 40)
        self.TEXT_COL = QColor(233, 237, 239)
        self.TIME_COL = QColor(148, 171, 184, 220)
        self.TICK_GRAY = QColor(138, 161, 174)
        self.TICK_BLUE = QColor(83, 189, 237)
        self.STAR_COL = QColor(255, 203, 5)
        self.SYSTEM_COL = QColor(134, 150, 160)
        self.QUOTE_BAR = QColor(0, 188, 212)
        self.QUOTE_BG = QColor(0, 0, 0, 60)
        self.QUOTE_BORDER = QColor(255, 255, 255, 25)
        self.QUOTE_TEXT = QColor(195, 205, 210)
        self.REVOKE_COL = QColor(138, 161, 174, 150)
        self.CAPTION_COL = QColor(180, 190, 195)
        self.FWD_COL = QColor(138, 161, 174, 170)
        self.LINK_COL = QColor(83, 189, 237)
        self.MENTION_COL = QColor(0, 188, 212)
        self.GHOST_LABEL_COL = QColor(255, 120, 120)
        self.VCARD_BG = QColor(25, 35, 42, 200)
        self.VCARD_CIRCLE = QColor(0, 150, 136, 180)
        self.VCARD_SUB = QColor(148, 171, 184, 180)
        self.DATE_SEP_BG = QColor(18, 28, 33, 220)
        self.DATE_SEP_COL = QColor(180, 195, 205)
        self.SENDER_COLORS = [
            QColor(255, 179, 186), QColor(186, 255, 201),
            QColor(186, 225, 255), QColor(255, 223, 186),
            QColor(218, 186, 255), QColor(255, 255, 186),
            QColor(186, 255, 255), QColor(255, 186, 243),
        ]

    def set_font_size(self, size: int):
        """Update all fonts based on a new base size."""
        self._base_font_size = max(8, min(18, size))
        s = self._base_font_size
        self.F_TEXT = QFont("Segoe UI", s)
        self.F_SENDER = QFont("Segoe UI", s - 1, QFont.Bold)
        self.F_META = QFont("Segoe UI", s - 1)
        self.F_SYSTEM = QFont("Segoe UI", s - 2)
        self.F_QUOTE = QFont("Segoe UI", s - 2)
        self.F_TYPE = QFont("Segoe UI", s - 1)
        self.F_FWD = QFont("Segoe UI", s - 2)
        self.F_DATE_SEP = QFont("Segoe UI", s - 2, QFont.Bold)
        self.F_LINK = QFont("Segoe UI", s - 1)
        self.F_GHOST = QFont("Segoe UI", s - 3, QFont.Bold)
        # ── Cached QFontMetrics (avoid 8+ allocations per paint) ──
        self._update_cached_metrics()

    def _update_cached_metrics(self):
        """Pre-compute QFontMetrics for all fonts. Called on init and font size change."""
        self._fm_text = QFontMetrics(self.F_TEXT)
        self._fm_sender = QFontMetrics(self.F_SENDER)
        self._fm_meta = QFontMetrics(self.F_META)
        self._fm_system = QFontMetrics(self.F_SYSTEM)
        self._fm_quote = QFontMetrics(self.F_QUOTE)
        self._fm_type = QFontMetrics(self.F_TYPE)
        self._fm_fwd = QFontMetrics(self.F_FWD)
        self._fm_date = QFontMetrics(self.F_DATE_SEP)
        self._fm_link = QFontMetrics(self.F_LINK)
        self._fm_ghost = QFontMetrics(self.F_GHOST)

    # ---- editorEvent (click detection) ----

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease:
            pos = event.pos()
            row = index.row()
            msg = index.data(MSG_DATA_ROLE)
            if not msg:
                return False

            # Check download button click
            if row in self._download_rects:
                drect, dmsg = self._download_rects[row]
                if drect.contains(pos):
                    self.download_media_requested.emit(dmsg)
                    return True

            # Check reaction pill click
            if row in self._reaction_rects:
                rrect, rmid = self._reaction_rects[row]
                if rrect.contains(pos):
                    self.reaction_clicked.emit(rmid)
                    return True

            # Check waveform seek (click on waveform bars to seek playing audio)
            if row in self._waveform_rects:
                wrect, wmsg_id = self._waveform_rects[row]
                if wrect.contains(pos) and self._playing_msg_id == wmsg_id and wmsg_id != 0:
                    # Calculate seek position as fraction of waveform width
                    seek_frac = max(0.0, min(1.0, (pos.x() - wrect.x()) / wrect.width()))
                    self.audio_seek_requested.emit(seek_frac)
                    return True

            # Check media click (emit signal for in-app viewer)
            if row in self._media_rects:
                mrect, mpath = self._media_rects[row]
                if mrect.contains(pos) and mpath:
                    # Handle "not on disk" sentinel — trigger download
                    if mpath == "__no_file__":
                        self.download_media_requested.emit(msg)
                        return True
                    # Audio/voice files: play inline instead of opening viewer
                    tl = msg.get("type_label", "")
                    aext = os.path.splitext(mpath)[1].lower()
                    is_audio = (tl in ("voice", "audio")
                                or aext in (".opus", ".ogg", ".m4a", ".aac",
                                            ".mp3", ".wav", ".amr", ".3gp"))
                    if is_audio:
                        self.audio_play_requested.emit(mpath, msg.get("id", 0))
                        return True
                    self.media_clicked.emit(mpath)
                    return True

            # Check edit indicator click
            if row in self._edit_rects:
                _ed_rect, _ed_msg_id = self._edit_rects[row]
                if _ed_rect.contains(pos):
                    self.edit_clicked.emit(_ed_msg_id)
                    return True

            # Check reply count badge click
            if row in self._reply_count_rects and self._reply_count_rects[row].contains(pos):
                src_key = msg.get("source_key_id")
                if src_key:
                    self.replies_clicked.emit(src_key)
                    return True

            # Check cross-chat quote button click
            if row in self._cross_chat_rects:
                cc_rect, cc_conv_id, cc_msg_id = self._cross_chat_rects[row]
                if cc_rect.contains(pos):
                    self.cross_chat_quote_clicked.emit(cc_conv_id, cc_msg_id)
                    return True

            # Check quote click
            if row in self._quote_rects and self._quote_rects[row].contains(pos):
                key_id = msg.get("reply_to_key_id")
                if key_id:
                    self.quote_clicked.emit(key_id)
                    return True

            # Check sender click
            if row in self._sender_rects:
                rect, contact_id = self._sender_rects[row]
                if rect.contains(pos) and contact_id:
                    self.sender_clicked.emit(contact_id)
                    return True

            # Check link clicks (tag pills and URL cards)
            if row in self._link_rects:
                for link_rect, url in self._link_rects[row]:
                    if link_rect.contains(pos):
                        if url.startswith("mention://"):
                            try:
                                cid = int(url.replace("mention://", ""))
                                if cid > 0:
                                    self.sender_clicked.emit(cid)
                            except (ValueError, TypeError):
                                pass
                            return True
                        from PySide6.QtCore import QUrl
                        QDesktopServices.openUrl(QUrl(url))
                        return True

            # Check inline mention/URL clicks via cached QTextDocument
            if row in self._mention_docs:
                doc, doc_x, doc_y = self._mention_docs[row]
                doc_pt = QPointF(pos.x() - doc_x, pos.y() - doc_y)
                href = doc.documentLayout().anchorAt(doc_pt)
                if href:
                    if href.startswith("mention://"):
                        try:
                            cid = int(href.replace("mention://", ""))
                            if cid > 0:
                                self.sender_clicked.emit(cid)
                        except (ValueError, TypeError):
                            pass
                    elif href.startswith("http"):
                        from PySide6.QtCore import QUrl
                        QDesktopServices.openUrl(QUrl(href))
                    return True

        return super().editorEvent(event, model, option, index)

    def is_interactive_at(self, row: int, pos) -> bool:
        """Check if position is over a clickable element for the given row."""
        if row in self._download_rects:
            drect, _ = self._download_rects[row]
            if drect.contains(pos):
                return True
        if row in self._reaction_rects:
            rrect, _ = self._reaction_rects[row]
            if rrect.contains(pos):
                return True
        if row in self._media_rects:
            mrect, mpath = self._media_rects[row]
            if mrect.contains(pos) and mpath:
                return True
        if row in self._quote_rects and self._quote_rects[row].contains(pos):
            return True
        if row in self._sender_rects:
            rect, _ = self._sender_rects[row]
            if rect.contains(pos):
                return True
        if row in self._link_rects:
            for link_rect, url in self._link_rects[row]:
                if link_rect.contains(pos):
                    return True
        # Check inline mention anchors in cached QTextDocument
        if row in self._mention_docs:
            doc, doc_x, doc_y = self._mention_docs[row]
            doc_pt = QPointF(pos.x() - doc_x, pos.y() - doc_y)
            href = doc.documentLayout().anchorAt(doc_pt)
            if href:
                return True
        return False

    # ---- sizeHint ----

    def sizeHint(self, option: object, index: QModelIndex) -> QSize:
        msg = index.data(MSG_DATA_ROLE)
        if not msg:
            return QSize(option.rect.width(), 30)

        w = option.rect.width()

        # Check sizeHint cache (msg_id + width → size)
        msg_id = msg.get("id", 0)
        cache_key = (msg_id, w)
        if msg_id and cache_key in self._size_hint_cache:
            return self._size_hint_cache[cache_key]

        # Date separator
        if msg.get("message_type") == -1:
            return QSize(w, 32)

        # System messages (type 7) and system-like messages (type 112 / unknown_112 = advanced chat privacy)
        if msg.get("message_type") in (7, 112) or msg.get("type_label") in ("advanced_chat_privacy", "unknown_112"):
            sys_text = self._build_system_text(msg)
            fm = self._fm_system
            max_tw = w - 40
            text_w = fm.horizontalAdvance(sys_text) + 28
            # Multi-line if text is wider than available
            if text_w >= max_tw:
                # Use word wrap calculation for accurate height
                br = fm.boundingRect(0, 0, max_tw - 16, 400, Qt.TextWordWrap, sys_text)
                return QSize(w, br.height() + 12)
            return QSize(w, 28)

        # Sticker: no bubble, larger display, just image + timestamp
        type_label = msg.get("type_label", "")
        is_sticker = type_label == "sticker"
        has_media_file = self._has_media_on_disk(msg)
        if is_sticker and (has_media_file or msg.get("has_thumb")):
            return QSize(w, self.STICKER_MAX + self.META_H + self.V_SPACING + 6)

        max_bw = int(w * self.MAX_RATIO)
        cw = max_bw - 2 * self.PAD
        h = 2 * self.PAD

        # Ghost label
        if msg.get("is_ghost"):
            h += 14

        # Status reply label
        if msg.get("is_status_reply"):
            h += 14

        # Forwarded label
        if msg.get("is_forwarded"):
            h += 14

        # Sender name
        if self._is_group and not msg.get("from_me"):
            h += self.SENDER_H
            if msg.get("member_label"):
                h += 14

        # Quoted text (or media reply without text)
        _qt = msg.get("quoted_text") or ""
        if not _qt and msg.get("reply_to_key_id"):
            _qtype = msg.get("quoted_type")
            _qt = {1: "\U0001F4F7 Photo", 2: "\U0001F3B5 Audio", 3: "\U0001F3AC Video",
                   5: "\U0001F4CD Location", 8: "\U0001F4C4 Document", 9: "\U0001F4C4 Document",
                   13: "GIF", 15: "\U0001F4C4 Document", 20: "Sticker", 46: "\U0001F4CA Poll",
                   82: "\U0001F3A4 View-once voice"}.get(_qtype, "\u21A9 Message")
        if _qt:
            fm = self._fm_quote
            qr = fm.boundingRect(0, 0, cw - 14, 400, Qt.TextWordWrap, _qt[:100])
            h += min(qr.height() + 8, self.QUOTE_H_MAX) + 3

        # Thumbnail or actual media image
        if has_media_file and (type_label in ("image", "gif", "animated_gif")
                               or (msg.get("mime_type") or "").startswith("image/")):
            h += self._estimate_media_h(msg, max_bw) + 3
        elif has_media_file and type_label in ("video",):
            if msg.get("has_thumb"):
                h += self._estimate_media_h(msg, max_bw) + 3
            else:
                h += 56
        elif msg.get("has_thumb") and type_label not in ("voice", "audio"):
            # Skip large thumbnail for text-with-URL messages;
            # the thumbnail is shown inside the link card instead (60px).
            text_for_url = msg.get("display_text", "")
            if not (text_for_url and _URL_RE.search(text_for_url)
                    and type_label not in ("image", "video", "gif", "animated_gif", "document", "sticker")):
                if type_label == "document":
                    h += 160  # document card: ~120px thumb + 40px bar (paint clips to actual)
                else:
                    h += min(self.THUMB_MAX_H, 200) + 3
        # Voice/audio card (with or without file)
        if type_label in ("voice", "audio"):
            h += 48
        elif not msg.get("has_thumb") and not has_media_file:
            # Media placeholder card for missing media
            if type_label in ("image", "video", "document",
                              "gif", "animated_gif"):
                if msg.get("file_path") or msg.get("media_url"):
                    h += 52  # placeholder card height
            # Photo album grid
            elif type_label == "album":
                children = msg.get("album_children") or []
                if children:
                    n = min(len(children), 6)
                    cols = 2 if n <= 4 else 3
                    rows_grid = (n + cols - 1) // cols
                    cell_h = min(self.ALBUM_GRID_MAX, 140)
                    h += rows_grid * cell_h + (rows_grid - 1) * 2 + 20  # grid + gaps + caption
                else:
                    h += 56

        # Location card
        if type_label in ("location", "live_location"):
            loc_h = 48
            if msg.get("loc_place_name"):
                loc_h += 16
            if msg.get("loc_place_address"):
                loc_h += 14
            h += loc_h

        # Poll card with vote bars
        if type_label in ("poll", "poll_vote") or msg.get("poll_options"):
            poll_options = msg.get("poll_options") or ""
            opt_count = len([o for o in poll_options.split("\n") if o.strip()]) if poll_options else 0
            total_voters = msg.get("poll_total_voters") or 0
            poll_voters = msg.get("poll_voters") or ""
            # Question text height (drawn above poll card)
            poll_q = msg.get("display_text") or msg.get("text_content") or ""
            if poll_q:
                h += 20  # question text line
            # 32 header + 28 per option + 18 footer + 14 voter names
            h += 32 + opt_count * 28 + (18 if total_voters else 0)
            if poll_voters:
                h += 14  # voter names line

        # vCard contact card(s)
        if type_label in ("vcard", "vcard_list"):
            vcard_data = msg.get("vcard_data") or ""
            vcard_count = max(1, len(vcard_data.split(";;")) if vcard_data else 1)
            h += vcard_count * 54

        # Link preview card ABOVE text (WhatsApp-style positioning)
        text = msg.get("display_text", "")
        link_card_h = 0
        # Cache URL extraction to avoid regex per sizeHint + paint
        if msg_id and msg_id in self._url_cache:
            urls = self._url_cache[msg_id]
        else:
            urls = _URL_RE.findall(text) if text else []
            # Fallback: if no URL found in text but link_details exists, extract URL from there
            if not urls and msg.get("link_details"):
                ld_str = msg.get("link_details") or ""
                for entry in ld_str.split(";;"):
                    parts_ld = entry.split("||")
                    if len(parts_ld) >= 2 and parts_ld[1].strip():
                        urls = [parts_ld[1].strip()]
                        break
            if msg_id:
                self._url_cache[msg_id] = urls
        if urls:
            ld_str = msg.get("link_details") or ""
            has_titles = bool(ld_str and "||" in ld_str)
            has_link_thumb = not has_media_file and msg.get("has_thumb")
            if has_titles:
                link_card_h = 64 if has_link_thumb else 44
            else:
                link_card_h = 28
            h += link_card_h

        # Revoked messages with no text still need height for "deleted" label
        _has_poll_hint = type_label in ("poll", "poll_vote") or msg.get("poll_options")
        if msg.get("is_revoked") and not text and not _has_poll_hint:
            h += 20  # "X deleted this message" line
        # Text (skip for vcard - shown in contact card, skip for poll - question shown above card)
        elif text and type_label != "vcard" and not _has_poll_hint:
            # Cache text height + URL/mention detection to avoid redundant work in paint()
            _cached_th = self._text_h_cache.get(msg_id)
            if _cached_th is not None:
                th = _cached_th
            else:
                fm = self._fm_text
                tr = fm.boundingRect(0, 0, cw, 8000, Qt.TextWordWrap, text)
                th = tr.height() + 3
                mentions_str_hint = msg.get("mentions_str") or ""
                _has_mention = bool(mentions_str_hint and _MENTION_RE.search(text))
                _has_url = bool(_URL_RE.search(text))
                self._has_url_cache[msg_id] = _has_url
                if _has_mention:
                    th += 6
                elif _has_url:
                    th += 6
                if msg_id:
                    self._text_h_cache[msg_id] = th
            h += th
        elif not msg.get("has_thumb") and not has_media_file:
            # Show type label
            tl = type_label
            if tl and tl not in ("location", "live_location", "poll", "poll_vote",
                                  "vcard", "album"):
                h += 18

        # @Mentions tag row (only when not rendered inline in text)
        mentions_str = msg.get("mentions_str") or ""
        has_inline_mentions = bool(text and _MENTION_RE.search(text) and mentions_str)
        if mentions_str and not has_inline_mentions:
            h += 20

        # Call record card
        if type_label == "call_log" and msg.get("call_result_label"):
            h += 44
            if msg.get("call_participants"):
                h += 16  # extra line for participant names

        h += self.META_H

        # Second meta line for delivery/read timestamps
        from_me_sz = msg.get("from_me", False)
        if from_me_sz:
            _st_sz = msg.get("status", 0)
            _read_sz = msg.get("first_read_ts") or 0
            _del_sz = msg.get("first_delivered_ts") or 0
            _srv_sz = msg.get("receipt_server_timestamp") or 0
            if (_read_sz > 0 or _del_sz > 0
                    or (_st_sz >= 5 and _srv_sz > 0)):
                h += self.META_H
        else:
            recv_ts_sz = msg.get("received_timestamp")
            if recv_ts_sz and recv_ts_sz > 0 and msg.get("timestamp") and recv_ts_sz != msg.get("timestamp"):
                h += self.META_H

        # Reply count badge
        if msg.get("reply_count") and msg["reply_count"] > 0:
            h += 18

        # Reactions below bubble
        if msg.get("reactions_str"):
            h += 28

        h += self.V_SPACING + 2  # spacing between bubbles + safety buffer
        result = QSize(w, max(h, 34))
        # Cache the computed size
        if msg_id:
            self._size_hint_cache[cache_key] = result
            if len(self._size_hint_cache) > 8000:
                keys = list(self._size_hint_cache.keys())[:3000]
                for k in keys:
                    del self._size_hint_cache[k]
        return result

    # ---- paint ----

    def paint(self, painter: QPainter, option: object, index: QModelIndex):
        msg = index.data(MSG_DATA_ROLE)
        if not msg:
            return
        painter.save()
        if not self._scrolling:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
        else:
            # Fast path during scroll: skip anti-aliasing for performance
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

        row = index.row()
        if msg.get("message_type") == -1:
            self._paint_date_separator(painter, option.rect, msg)
        elif msg.get("message_type") in (7, 112) or msg.get("type_label") in ("advanced_chat_privacy", "unknown_112"):
            self._paint_system(painter, option.rect, msg)
        elif (msg.get("type_label") == "sticker"
              and (self._has_media_on_disk(msg) or msg.get("has_thumb"))):
            self._paint_sticker(painter, option.rect, msg, row)
        else:
            self._paint_bubble(painter, option.rect, msg, row)

        painter.restore()

    def _paint_date_separator(self, p: QPainter, rect: QRect, msg: dict):
        """Paint a centered date pill between message groups."""
        text = msg.get("display_text", "")
        fm = self._fm_date
        tw = fm.horizontalAdvance(text) + 20
        th = 20
        x = rect.x() + (rect.width() - tw) // 2
        y = rect.y() + (rect.height() - th) // 2

        path = QPainterPath()
        path.addRoundedRect(float(x), float(y), float(tw), float(th), 10, 10)
        p.fillPath(path, self.DATE_SEP_BG)

        p.setFont(self.F_DATE_SEP)
        p.setPen(self.DATE_SEP_COL)
        p.drawText(QRect(x, y, tw, th), Qt.AlignCenter, text)

    @staticmethod
    def _fmt_ephemeral_duration(secs: int) -> str:
        """Format ephemeral timer duration to human-readable string."""
        if secs == 0:
            return ""
        if secs < 3600:
            return f"{secs // 60} minutes"
        if secs == 86400:
            return "24 hours"
        if secs == 604800:
            return "7 days"
        if secs == 7776000:
            return "90 days"
        days = secs // 86400
        if days > 0:
            return f"{days} days"
        hours = secs // 3600
        return f"{hours} hours"

    @staticmethod
    def _fmt_phone(number: str) -> str:
        """Format a phone number with spaces for readability.
        For example a 12-digit Indian number is grouped as
        ``+91 NNNNN NNNNN`` and an 11-digit US number as
        ``+1 NNN NNN NNNN``.
        """
        if not number or not number.strip():
            return number or ""
        n = number.lstrip("+")
        if not n.isdigit():
            return number  # not a pure phone number
        # Indian numbers: +91 XXXXX XXXXX
        if n.startswith("91") and len(n) == 12:
            return f"+91 {n[2:7]} {n[7:]}"
        # US/CA: +1 XXX XXX XXXX
        if n.startswith("1") and len(n) == 11:
            return f"+1 {n[1:4]} {n[4:7]} {n[7:]}"
        # Generic: +CC XXXX XXXX...
        if len(n) > 6:
            cc_len = 1 if n[0] in "17" else (3 if n[:2] in ("20","21","22","23","24","25","26","27","28","29","30","31","33","34","35","36","37","38","39","40","41","42","43","44","45","46","47","48","49","50","51","52","53","54","55","56","57","58","59","60","61","62","63","64","65","66","67","68","69","70","71","72","73","74","75","76","77","78","79","80","81","82","84","86","90","91","92","93","94","95","98") else 2)
            rest = n[cc_len:]
            # Split rest into groups of 5
            parts = [rest[i:i+5] for i in range(0, len(rest), 5)]
            return f"+{n[:cc_len]} {' '.join(parts)}"
        return f"+{n}"

    def _build_system_text(self, msg: dict) -> str:
        """Build descriptive system event text matching WhatsApp's display.

        Delegates to shared.system_event_formatter.build_system_text() so that
        both the GUI and backend ingestion pipeline use identical formatting.
        """
        from shared.system_event_formatter import build_system_text
        return build_system_text(
            msg,
            owner_phone=self._owner_phone,
            owner_name=self._owner_name,
            owner_contact_id=self._owner_contact_id,
            conv_name=getattr(self, "_conv_name", ""),
            chat_type=getattr(self, "_chat_type", ""),
        )

    def _paint_system(self, p: QPainter, rect: QRect, msg: dict):
        from app.config import format_timestamp
        display = self._build_system_text(msg)
        ts = msg.get("timestamp")
        ts_str = format_timestamp(ts, "system")
        if ts_str:
            display = f"{display}  \u00b7  {ts_str}"
        fm = self._fm_system
        max_tw = rect.width() - 40
        text_w = fm.horizontalAdvance(display)

        # Multi-line for long system messages
        if text_w + 24 > max_tw:
            bw = max_tw
            bx = rect.x() + (rect.width() - bw) // 2
            by = rect.y() + 2
            # Calculate wrapped text height
            br = fm.boundingRect(0, 0, bw - 16, 400, Qt.TextWordWrap, display)
            th = br.height() + 8

            path = QPainterPath()
            path.addRoundedRect(float(bx), float(by), float(bw), float(th), 10, 10)
            p.fillPath(path, self.SYSTEM_BG)

            p.setFont(self.F_SYSTEM)
            p.setPen(self.SYSTEM_COL)
            p.drawText(QRect(bx + 8, by + 3, bw - 16, th - 6),
                       Qt.TextWordWrap | Qt.AlignCenter, display)
            return

        tw = text_w + 24
        th = 20
        x = rect.x() + (rect.width() - tw) // 2
        y = rect.y() + (rect.height() - th) // 2

        path = QPainterPath()
        path.addRoundedRect(float(x), float(y), float(tw), float(th), 10, 10)
        p.fillPath(path, self.SYSTEM_BG)

        p.setFont(self.F_SYSTEM)
        p.setPen(self.SYSTEM_COL)
        p.drawText(QRect(x, y, tw, th), Qt.AlignCenter, display)

    def _has_media_on_disk(self, msg: dict) -> bool:
        """Check if media file exists on disk (cached).
        During scroll: trust DB flag only (zero disk I/O).
        When idle: full resolve with os.path.isfile() caching."""
        msg_id = msg.get("id", 0)
        if msg_id in self._file_exists_cache:
            return self._file_exists_cache[msg_id]
        # During scroll: trust the DB file_exists flag to avoid disk I/O completely
        if self._scrolling:
            return bool(msg.get("media_file_exists"))
        # Full resolve when idle
        result = False
        if msg.get("media_file_exists"):
            rp = msg.get("resolved_file_path")
            if rp:
                cache_key = f"|{rp}"
                if cache_key in self._resolve_cache:
                    result = self._resolve_cache[cache_key] is not None
                elif os.path.isfile(rp):
                    self._resolve_cache[cache_key] = rp
                    result = True
                else:
                    self._resolve_cache[cache_key] = None
        if not result:
            fp = msg.get("file_path")
            if fp:
                resolved = _resolve_media_path(fp, msg.get("resolved_file_path"),
                                               self._resolve_cache)
                result = resolved is not None
        self._file_exists_cache[msg_id] = result
        if len(self._file_exists_cache) > 5000:
            keys = list(self._file_exists_cache.keys())[:2000]
            for k in keys:
                del self._file_exists_cache[k]
        return result

    def _estimate_media_h(self, msg: dict, max_bw: int) -> int:
        """Estimate rendered media height using DB dimensions or fallback."""
        cw = max_bw - 2 * self.PAD
        mw = msg.get("media_width") or 0
        mh = msg.get("media_height") or 0
        if mw > 0 and mh > 0:
            # Scale to fit content width, capped at THUMB_MAX_H
            scale = min(cw / mw, self.THUMB_MAX_H / mh, 1.0)
            return min(int(mh * scale), self.THUMB_MAX_H)
        return min(self.THUMB_MAX_H, 280)  # reasonable default

    def _estimate_media_wh(self, msg: dict, max_bw: int) -> tuple[int, int]:
        """Estimate rendered media (width, height) using DB dimensions."""
        cw = max_bw - 2 * self.PAD
        mw = msg.get("media_width") or 0
        mh = msg.get("media_height") or 0
        if mw > 0 and mh > 0:
            scale = min(cw / mw, self.THUMB_MAX_H / mh, 1.0)
            return (int(mw * scale), min(int(mh * scale), self.THUMB_MAX_H))
        return (cw, min(self.THUMB_MAX_H, 280))

    def _load_media_pixmap(self, file_path: str, max_w: int, max_h: int,
                           resolved_file_path: str | None = None) -> QPixmap | None:
        """Load a media file from disk, with caching.
        Supports WebP (including animated - renders first frame) via QImageReader fallback.
        During scroll, only returns already-cached pixmaps (no disk I/O)."""
        resolved = _resolve_media_path(file_path, resolved_file_path,
                                       self._resolve_cache)
        if not resolved:
            return None
        cache_key = f"{resolved}|{max_w}x{max_h}"
        if cache_key in self._media_cache:
            return self._media_cache[cache_key]
        # During scroll, skip disk-loading new images (return None → shows thumbnail/placeholder)
        if self._scrolling:
            return None
        pxm = QPixmap()
        if not pxm.load(resolved):
            # Fallback: use QImageReader which handles more formats including WebP
            reader = QImageReader(resolved)
            reader.setAutoTransform(True)
            img = reader.read()
            if img.isNull():
                return None
            pxm = QPixmap.fromImage(img)
        if pxm.isNull():
            return None
        # Scale to fit
        scaled = pxm.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._media_cache[cache_key] = scaled
        if len(self._media_cache) > 500:
            # Evict oldest 200 entries in bulk
            keys = list(self._media_cache.keys())[:200]
            for k in keys:
                del self._media_cache[k]
        return scaled

    def _is_animated_sticker(self, file_path: str) -> bool:
        """Check if a sticker file is animated (multi-frame WebP/GIF). Cached.
        During scroll, returns cached result only (no disk I/O)."""
        if file_path in self._animated_check:
            return self._animated_check[file_path]
        # During scroll, assume not animated to avoid disk I/O
        if self._scrolling:
            return False
        try:
            reader = QImageReader(file_path)
            animated = reader.imageCount() > 1
        except Exception:
            animated = False
        self._animated_check[file_path] = animated
        if len(self._animated_check) > 1000:
            keys = list(self._animated_check.keys())[:400]
            for k in keys:
                del self._animated_check[k]
        return animated

    def _extract_sticker_frames(self, file_path: str, max_size: int) -> list[tuple[QPixmap, int]] | None:
        """Extract all frames from an animated WebP/GIF using Pillow.
        Returns list of (QPixmap, duration_ms) tuples, or None on failure.
        Pillow correctly composites WebP frames (disposal, blending) unlike QMovie."""
        try:
            from PIL import Image
            img = Image.open(file_path)
            if not hasattr(img, 'n_frames') or img.n_frames <= 1:
                return None
            frames = []
            for i in range(min(img.n_frames, 60)):  # cap at 60 frames
                img.seek(i)
                frame = img.convert("RGBA")
                # Scale to max_size
                w, h = frame.size
                if w > max_size or h > max_size:
                    ratio = min(max_size / w, max_size / h)
                    new_w, new_h = int(w * ratio), int(h * ratio)
                    frame = frame.resize((new_w, new_h), Image.LANCZOS)
                # Convert PIL -> QPixmap
                data = frame.tobytes("raw", "RGBA")
                qimg = QImage(data, frame.width, frame.height,
                              frame.width * 4, QImage.Format_RGBA8888).copy()
                pxm = QPixmap.fromImage(qimg)
                if pxm.isNull():
                    continue
                dur = img.info.get("duration", 33)
                if dur < 10:
                    dur = 33
                frames.append((pxm, dur))
            return frames if frames else None
        except Exception:
            return None

    def _get_sticker_anim(self, file_path: str, max_size: int) -> dict | None:
        """Get or create a Pillow-based animation for an animated sticker. Capped at 12.
        During scroll, only returns already-cached animations."""
        if file_path in self._sticker_fail:
            return None
        if file_path in self._sticker_anims:
            return self._sticker_anims[file_path]
        # During scroll, skip expensive frame extraction
        if self._scrolling:
            return None

        frame_data = self._extract_sticker_frames(file_path, max_size)
        if not frame_data:
            self._sticker_fail.add(file_path)
            return None

        anim = {
            "frames": [f[0] for f in frame_data],
            "durations": [f[1] for f in frame_data],
            "idx": 0,
            "elapsed": 0,
        }
        self._sticker_anims[file_path] = anim

        # Start timer if not running
        if not self._anim_timer.isActive():
            self._anim_timer.start()

        # Cap active animations to prevent memory bloat
        while len(self._sticker_anims) > 12:
            oldest_key = next(iter(self._sticker_anims))
            del self._sticker_anims[oldest_key]

        return anim

    def _flush_anim_repaint(self):
        """Advance Pillow-based sticker animation frames and trigger repaint.
        Uses targeted row repainting instead of full viewport update."""
        if not self._sticker_anims:
            self._anim_timer.stop()
            return
        if self._scrolling:
            return  # skip during scroll, keep timer running

        interval = self._anim_timer.interval()
        changed = False
        for anim in self._sticker_anims.values():
            anim["elapsed"] += interval
            dur = anim["durations"][anim["idx"]]
            while dur > 0 and anim["elapsed"] >= dur:
                anim["elapsed"] -= dur
                anim["idx"] = (anim["idx"] + 1) % len(anim["frames"])
                changed = True
                dur = anim["durations"][anim["idx"]]

        if changed:
            view = self.parent()
            if view and hasattr(view, 'viewport'):
                vp = view.viewport()
                vp_rect = vp.rect()
                # Use view.update(idx) for targeted repaint of visible sticker rows
                seen = set()
                for y in range(0, vp_rect.height(), 40):
                    idx = view.indexAt(QPoint(vp_rect.width() // 2, y))
                    if idx.isValid() and idx.row() not in seen:
                        seen.add(idx.row())
                        msg = idx.data(MSG_DATA_ROLE)
                        if msg and msg.get("type_label") == "sticker":
                            view.update(idx)

    def set_audio_state(self, msg_id: int, progress: float):
        """Update inline audio playback state and force repaint."""
        self._playing_msg_id = msg_id
        self._audio_progress = max(0.0, min(1.0, progress))
        view = self.parent()
        if view and hasattr(view, 'viewport'):
            model = view.model()
            if model and hasattr(model, '_id_to_row') and msg_id in model._id_to_row:
                row = model._id_to_row[msg_id]
                idx = model.index(row, 0)
                # Emit dataChanged to force Qt to call delegate.paint()
                model.dataChanged.emit(idx, idx)
                return
            view.viewport().update()

    def on_scroll_start(self):
        """Call when the list view starts scrolling — defer all disk I/O."""
        self._scrolling = True

    def on_scroll_stop(self):
        """Call when scrolling stops — trigger repaint to load deferred media."""
        self._scrolling = False
        # Invalidate size hints that were computed with scroll-mode approximations
        # (only uncached items used DB flag during scroll — they need revalidation)
        view = self.parent()
        if view and hasattr(view, 'viewport'):
            view.viewport().update()

    def invalidate_size_cache(self, msg_id: int | None = None):
        """Invalidate sizeHint cache. Call after downloads or data changes.
        If msg_id is None, clears entire cache."""
        if msg_id is None:
            self._size_hint_cache.clear()
            self._text_h_cache.clear()
            self._url_cache.clear()
            self._has_url_cache.clear()
        else:
            keys_to_remove = [k for k in self._size_hint_cache if k[0] == msg_id]
            for k in keys_to_remove:
                del self._size_hint_cache[k]
            self._text_h_cache.pop(msg_id, None)
            self._url_cache.pop(msg_id, None)
            self._has_url_cache.pop(msg_id, None)

    def _is_pixmap_black(self, pxm: QPixmap) -> bool:
        """Quick check if a pixmap is all opaque-black (failed decode).
        Transparent pixels are VALID for stickers — only flag images where
        every sampled pixel is opaque black (R<10,G<10,B<10,A>200)."""
        if pxm.isNull() or pxm.width() < 2 or pxm.height() < 2:
            return True
        img = pxm.toImage()
        w, h = img.width(), img.height()
        # Sample 12 points across the image for better coverage
        points = [
            (w // 2, h // 2), # center
            (w // 4, h // 4), (3 * w // 4, h // 4), # upper quarters
            (w // 4, 3 * h // 4), (3 * w // 4, 3 * h // 4), # lower quarters
            (w // 3, h // 2), (2 * w // 3, h // 2), # mid-thirds
            (w // 2, h // 3), (w // 2, 2 * h // 3), # vertical thirds
            (w // 6, h // 2), (5 * w // 6, h // 2), # near-edges
            (w // 2, h // 6), # top-center
        ]
        has_any_visible = False
        all_opaque_black = True
        for px, py in points:
            c = img.pixelColor(px, py)
            if c.alpha() < 10:
                # Transparent pixel — perfectly valid for stickers, skip it
                all_opaque_black = False
                continue
            # Pixel has visible alpha — check if it has color
            if c.red() > 10 or c.green() > 10 or c.blue() > 10:
                has_any_visible = True
                all_opaque_black = False
                break  # found a good pixel, image is fine
            # Opaque black pixel (alpha>10, RGB~0) — still suspicious
        # Image is "black" only if ALL opaque pixels were black AND we found no color
        # If image is all-transparent, it's empty (also bad for non-stickers)
        return not has_any_visible and all_opaque_black

    def _paint_sticker(self, p: QPainter, rect: QRect, msg: dict, row: int = -1):
        """Paint a sticker without bubble background (transparent, like WhatsApp).
        Supports animated WebP/GIF stickers via Pillow."""
        from_me = msg.get("from_me", False)
        w = rect.width()
        sm = self.STICKER_MAX
        resolved_fp = msg.get("resolved_file_path")

        # Try to load actual file first, then thumbnail
        pxm = None
        has_media_file = self._has_media_on_disk(msg)
        resolved_path = None
        if has_media_file:
            fp = msg.get("file_path", "")
            resolved_path = _resolve_media_path(fp, resolved_fp, self._resolve_cache)

        # Skip known-bad files
        if resolved_path and resolved_path in self._sticker_fail:
            resolved_path = None

        # Check for animated sticker (Pillow-based rendering)
        if resolved_path and self._is_animated_sticker(resolved_path):
            anim = self._get_sticker_anim(resolved_path, sm)
            if anim and anim["frames"]:
                pxm = anim["frames"][anim["idx"]]
            if not pxm:
                # Pillow extraction failed — try static first frame via QImageReader
                pxm = self._load_media_pixmap(msg.get("file_path", ""), sm, sm, resolved_fp)
        elif resolved_path:
            pxm = self._load_media_pixmap(msg.get("file_path", ""), sm, sm, resolved_fp)

        # Fallback: thumbnail blob
        if not pxm and msg.get("has_thumb"):
            pxm = self._get_thumb(msg.get("id", 0), msg.get("thumbnail_blob"))
            if pxm and not pxm.isNull():
                pxm = pxm.scaled(sm, sm, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        show_avatar = self._is_group and not from_me
        avatar_x_offset = self.AVATAR_OFFSET if show_avatar else 0

        if pxm and not pxm.isNull():
            # Position: right-aligned for sent, left-aligned for received
            if from_me:
                ix = rect.x() + w - pxm.width() - 16
            else:
                ix = rect.x() + 16 + avatar_x_offset
            iy = rect.y() + self.V_SPACING

            # Paint sender avatar for group received stickers
            if show_avatar:
                self._paint_sender_avatar(p, rect.x() + 8, iy, msg)

            # Clear sticker area to viewport background to prevent animation artifacts
            bg_color = self.parent().palette().window().color() if self.parent() else QColor(245, 245, 245)
            p.fillRect(QRect(ix, iy, pxm.width(), pxm.height()), bg_color)
            p.drawPixmap(ix, iy, pxm)

            # Store clickable rect
            if has_media_file and row >= 0 and resolved_path:
                self._media_rects[row] = (QRect(ix, iy, pxm.width(), pxm.height()), resolved_path)

            # Timestamp below sticker
            from app.config import format_timestamp
            ts = msg.get("timestamp")
            ts_str = format_timestamp(ts, "bubble")
            if ts_str:
                p.setFont(self.F_META)
                p.setPen(self.TIME_COL)
                ty = iy + pxm.height() + 2
                p.drawText(QRect(ix, ty, pxm.width(), self.META_H),
                           Qt.AlignRight | Qt.AlignVCenter, ts_str)
        else:
            # Nothing loaded — draw a placeholder icon for the sticker
            if from_me:
                ix = rect.x() + w - sm - 16
            else:
                ix = rect.x() + 16 + avatar_x_offset
            iy = rect.y() + self.V_SPACING
            placeholder_rect = QRect(ix, iy, sm, sm)
            # Draw subtle circle
            cx = ix + sm / 2
            cy = iy + sm / 2
            bg_path = QPainterPath()
            bg_path.addEllipse(QPointF(cx, cy), sm / 2 + 4, sm / 2 + 4)
            p.fillPath(bg_path, self.STICKER_BG)
            # Draw placeholder text
            p.setFont(QFont("Segoe UI", 16))
            p.setPen(self.TIME_COL)
            p.drawText(placeholder_rect, Qt.AlignCenter, "\u25A3")

    def _paint_sender_avatar(self, p: QPainter, ax: int, ay: int, msg: dict):
        """Paint a 32px circular avatar to the left of a received group bubble."""
        sender_id = msg.get("sender_id") or 0
        sz = self.AVATAR_SIZE

        # Check cache first
        if sender_id not in self._avatar_cache:
            blob = msg.get("sender_avatar_blob")
            if blob and len(blob) > 100:
                pxm = QPixmap()
                pxm.loadFromData(blob)
                if not pxm.isNull():
                    # Scale to AVATAR_SIZE and make circular
                    pxm = pxm.scaled(sz, sz, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    # Crop to square
                    if pxm.width() != sz or pxm.height() != sz:
                        x0 = (pxm.width() - sz) // 2
                        y0 = (pxm.height() - sz) // 2
                        pxm = pxm.copy(x0, y0, sz, sz)
                    # Apply circular mask
                    masked = QPixmap(sz, sz)
                    masked.fill(Qt.transparent)
                    mp = QPainter(masked)
                    mp.setRenderHint(QPainter.Antialiasing)
                    clip = QPainterPath()
                    clip.addEllipse(0, 0, sz, sz)
                    mp.setClipPath(clip)
                    mp.drawPixmap(0, 0, pxm)
                    mp.end()
                    self._avatar_cache[sender_id] = masked
                else:
                    self._avatar_cache[sender_id] = QPixmap()  # empty = fallback
            else:
                self._avatar_cache[sender_id] = QPixmap()  # no blob

            # Evict oldest if cache too large
            if len(self._avatar_cache) > 500:
                oldest = next(iter(self._avatar_cache))
                del self._avatar_cache[oldest]

        cached = self._avatar_cache.get(sender_id)
        if cached and not cached.isNull():
            p.drawPixmap(ax, ay, cached)
        else:
            # Fallback: colored circle with first letter
            sender_name = msg.get("sender_name", "?")
            color_idx = sender_id % len(self.SENDER_COLORS)
            color = self.SENDER_COLORS[color_idx]
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(ax, ay, sz, sz)
            # Draw letter
            letter = sender_name[0].upper() if sender_name else "?"
            p.setPen(QColor(50, 50, 50))
            p.setFont(QFont("Segoe UI", 12, QFont.Bold))
            p.drawText(QRect(ax, ay, sz, sz), Qt.AlignCenter, letter)

    def _paint_bubble(self, p: QPainter, rect: QRect, msg: dict, row: int = -1):
        from_me = msg.get("from_me", False)
        is_bot = msg.get("is_bot_message", False)
        is_ghost = msg.get("is_ghost", False)
        msg_id = msg.get("id", 0)
        w = rect.width()
        max_bw = int(w * self.MAX_RATIO)
        cw = max_bw - 2 * self.PAD
        type_label = msg.get("type_label", "")

        # --- compute bubble height ---
        cy = 0
        if is_ghost:
            cy += 14
        if msg.get("is_forwarded"):
            cy += 14
        if self._is_group and not from_me:
            cy += self.SENDER_H
            # Member tag line (admin-assigned label like apartment number)
            if msg.get("member_label"):
                cy += 14
        quote_h = 0
        _qt_paint = msg.get("quoted_text") or ""
        if not _qt_paint and msg.get("reply_to_key_id"):
            _qtype_p = msg.get("quoted_type")
            _qt_paint = {1: "\U0001F4F7 Photo", 2: "\U0001F3B5 Audio", 3: "\U0001F3AC Video",
                         5: "\U0001F4CD Location", 8: "\U0001F4C4 Document", 9: "\U0001F4C4 Document",
                         13: "GIF", 15: "\U0001F4C4 Document", 20: "Sticker", 46: "\U0001F4CA Poll",
                         82: "\U0001F3A4 View-once voice"}.get(_qtype_p, "\u21A9 Message")
        if _qt_paint:
            fm = self._fm_quote
            qr = fm.boundingRect(0, 0, cw - 14, 400, Qt.TextWordWrap, _qt_paint[:100])
            quote_h = min(qr.height() + 8, self.QUOTE_H_MAX) + 3
            # Extra height for cross-chat quote indicator button
            if msg.get("quoted_cross_chat_conv_name"):
                quote_h += 18
            cy += quote_h

        # Media/thumbnail height
        thumb_h = 0
        has_media_file = self._has_media_on_disk(msg)
        if has_media_file and (type_label in ("image", "gif", "animated_gif")
                               or (msg.get("mime_type") or "").startswith("image/")):
            thumb_h = self._estimate_media_h(msg, max_bw) + 3
            cy += thumb_h
        elif has_media_file and type_label in ("video",):
            if msg.get("has_thumb"):
                thumb_h = self._estimate_media_h(msg, max_bw) + 3
                cy += thumb_h
            else:
                cy += 56  # video card without thumbnail
        elif msg.get("has_thumb") and type_label not in ("voice", "audio"):
            # Skip large thumbnail for text-with-URL messages;
            # the link card handles the thumbnail at 80px.
            text_for_url = msg.get("display_text", "")
            if not (text_for_url and _URL_RE.search(text_for_url)
                    and type_label not in ("image", "video", "gif", "animated_gif", "document", "sticker")):
                if type_label == "document":
                    thumb_h = 160  # sync with sizeHint document card height
                else:
                    thumb_h = min(self.THUMB_MAX_H, 200) + 3
                cy += thumb_h
        # Voice/audio card (with or without file)
        if type_label in ("voice", "audio"):
            cy += 48
        elif not msg.get("has_thumb") and not has_media_file:
            # Media placeholder card
            if type_label in ("image", "video", "document",
                              "gif", "animated_gif"):
                if msg.get("file_path") or msg.get("media_url"):
                    cy += 52  # placeholder card
            # Photo album grid
            elif type_label == "album":
                children = msg.get("album_children") or []
                if children:
                    n = min(len(children), 6)
                    cols = 2 if n <= 4 else 3
                    rows_grid = (n + cols - 1) // cols
                    cell_h = min(self.ALBUM_GRID_MAX, 140)
                    cy += rows_grid * cell_h + (rows_grid - 1) * 2 + 20
                else:
                    cy += 56

        # Location / Poll cards
        if type_label in ("location", "live_location"):
            cy += 48
        poll_options = msg.get("poll_options") or ""
        poll_opt_count = len([o for o in poll_options.split("\n") if o.strip()]) if poll_options else 0
        poll_total_voters = msg.get("poll_total_voters") or 0
        if type_label in ("poll", "poll_vote") or msg.get("poll_options"):
            poll_q_paint = msg.get("display_text") or msg.get("text_content") or ""
            if poll_q_paint:
                cy += 20  # question text above poll card
            cy += 32 + poll_opt_count * 28 + (18 if poll_total_voters else 0)

        # vCard contact card(s)
        if type_label in ("vcard", "vcard_list"):
            vcard_data_paint = msg.get("vcard_data") or ""
            vcard_count_paint = max(1, len(vcard_data_paint.split(";;")) if vcard_data_paint else 1)
            cy += vcard_count_paint * 54

        text = msg.get("display_text", "")

        # Link preview card ABOVE text (WhatsApp-style: card then text below)
        link_h = 0
        # Reuse cached URL extraction from sizeHint
        if msg_id and msg_id in self._url_cache:
            urls = self._url_cache[msg_id]
        else:
            urls = _URL_RE.findall(text) if text else []
            if not urls and msg.get("link_details"):
                ld_str = msg.get("link_details") or ""
                for entry in ld_str.split(";;"):
                    parts_ld = entry.split("||")
                    if len(parts_ld) >= 2 and parts_ld[1].strip():
                        urls = [parts_ld[1].strip()]
                        break
            if msg_id:
                self._url_cache[msg_id] = urls
        if urls:
            ld_str = msg.get("link_details") or ""
            has_titles = bool(ld_str and "||" in ld_str)
            has_link_thumb = not has_media_file and msg.get("has_thumb")
            if has_titles:
                link_h = 64 if has_link_thumb else 44
            else:
                link_h = 28
            cy += link_h

        text_h = 0
        _has_poll_p = type_label in ("poll", "poll_vote") or msg.get("poll_options")
        if text and type_label != "vcard" and not _has_poll_p:
            # Reuse cached text height from sizeHint when available
            _cached = self._text_h_cache.get(msg_id) if msg_id else None
            if _cached is not None:
                text_h = _cached
            else:
                fm = self._fm_text
                tr = fm.boundingRect(0, 0, cw, 8000, Qt.TextWordWrap, text)
                text_h = tr.height() + 3
                mentions_str_paint = msg.get("mentions_str") or ""
                if mentions_str_paint and _MENTION_RE.search(text):
                    text_h += 6
                elif self._has_url_cache.get(msg_id, _URL_RE.search(text) if text else False):
                    text_h += 6
            cy += text_h
        elif not msg.get("has_thumb") and not has_media_file:
            if type_label and type_label not in ("location", "live_location",
                                                  "poll", "poll_vote", "vcard",
                                                  "album", "call_log"):
                text_h = 18
                cy += text_h

        # Call record card
        call_card_h = 0
        if type_label == "call_log" and msg.get("call_result_label"):
            call_card_h = 44
            if msg.get("call_participants"):
                call_card_h += 16  # extra line for participants
            cy += call_card_h

        cy += self.META_H
        # Second meta line for delivery/read timestamps
        if from_me:
            _st_p = msg.get("status", 0)
            if (msg.get("first_read_ts") or msg.get("first_delivered_ts")
                    or (_st_p >= 5 and msg.get("receipt_server_timestamp"))):
                cy += self.META_H
        else:
            _recv_ts = msg.get("received_timestamp")
            if _recv_ts and msg.get("timestamp") and _recv_ts != msg.get("timestamp"):
                cy += self.META_H
        # Extra line for "edited" indicator with edit timestamp
        if msg.get("is_edited") and not msg.get("is_bot_message"):
            cy += self.META_H
        reactions_h = 28 if msg.get("reactions_str") else 0
        bh = cy + 2 * self.PAD
        bw = max_bw

        # Shrink bubble width to fit media for media-dominant messages
        # (no text or very short text that fits within the media width)
        _media_types = ("image", "gif", "animated_gif", "video")
        if type_label in _media_types and (has_media_file or msg.get("has_thumb")):
            ew, eh = self._estimate_media_wh(msg, max_bw)
            if ew > 0:
                media_bw = ew + 2 * self.PAD
                # Ensure minimum width for meta line
                fm_meta = self._fm_meta
                ts = msg.get("timestamp")
                from app.config import format_timestamp as _fmt_ts
                _ts_w = fm_meta.horizontalAdvance(_fmt_ts(ts, "full") or "") + 40
                media_bw = max(media_bw, _ts_w)
                # If text exists, check if it fits in media width
                if text:
                    fm_txt = self._fm_text
                    txt_w = fm_txt.horizontalAdvance(text)
                    if txt_w > media_bw - 2 * self.PAD:
                        media_bw = max_bw  # text needs full width
                if media_bw < max_bw:
                    bw = media_bw
                    cw = bw - 2 * self.PAD

        # Shrink bubble width for call log cards (no text content)
        if type_label == "call_log" and call_card_h > 0 and not text:
            fm_meta = self._fm_meta
            from app.config import format_timestamp as _fmt_ts2
            _ts_w2 = fm_meta.horizontalAdvance(_fmt_ts2(msg.get("timestamp"), "full") or "") + 40
            call_bw = max(280, _ts_w2) + 2 * self.PAD  # 280px for call card content
            if call_bw < max_bw:
                bw = call_bw
                cw = bw - 2 * self.PAD

        # Position
        show_avatar = self._is_group and not from_me
        if show_avatar:
            bx = rect.x() + 8 + self.AVATAR_OFFSET
        elif from_me:
            bx = rect.x() + w - bw - 8
        else:
            bx = rect.x() + 8
        by = rect.y() + self.V_SPACING

        # Background
        if is_ghost:
            bg = QColor(self.GHOST_BG)
        elif is_bot:
            bg = QColor(self.BOT_BG)
        elif from_me:
            bg = QColor(self.SENT_BG)
        else:
            bg = QColor(self.RECV_BG)
        if msg.get("is_revoked"):
            bg.setAlpha(100)
        path = QPainterPath()
        path.addRoundedRect(float(bx), float(by), float(bw), float(bh),
                            self.RADIUS, self.RADIUS)
        p.fillPath(path, bg)

        # ── Group chat sender avatar (painted BEFORE clip, outside bubble) ──
        if show_avatar:
            self._paint_sender_avatar(p, rect.x() + 8, by, msg)

        # Clip all content to bubble boundary (prevents out-of-bounds rendering)
        p.save()
        p.setClipPath(path)

        # Tagged message indicator — red left border + flag
        if msg.get("is_tagged"):
            tag_bar = QPainterPath()
            tag_bar.addRoundedRect(float(bx), float(by), 4.0, float(bh), 2, 2)
            p.fillPath(tag_bar, QColor(220, 50, 50))
            # Small flag icon in top-left corner
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor(220, 50, 50))
            p.drawText(QRect(bx + 6, by + 2, 14, 12), Qt.AlignCenter, "\u2691")

        # Album child indicator — subtle top-right badge
        if msg.get("album_parent_id"):
            _lt = self.RECV_BG.lightness() > 128
            p.setFont(QFont("Segoe UI", 7))
            p.setPen(QColor(100, 140, 180) if _lt else QColor(120, 160, 200))
            p.drawText(QRect(bx + bw - 60, by + 2, 56, 10),
                       Qt.AlignRight | Qt.AlignVCenter, "\U0001F5BC album")

        # --- draw content ---
        cx = bx + self.PAD
        dy = by + self.PAD

        # Ghost message label
        if is_ghost:
            p.setFont(self.F_GHOST)
            p.setPen(self.GHOST_LABEL_COL)
            p.drawText(QRect(cx, dy, cw, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       "\U0001F47B RECOVERED (deleted)")
            dy += 14

        # Status reply badge
        if msg.get("is_status_reply"):
            p.setFont(self.F_FWD)
            _sr_col = QColor("#e040fb") # purple accent for status
            p.setPen(_sr_col)
            p.drawText(QRect(cx, dy, cw, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       "\U0001F4F1 Reply to Status")
            dy += 14

        # Forwarded label
        if msg.get("is_forwarded"):
            fwd_score = msg.get("forward_score")
            fwd_label = "\u21AA\uFE0F Forwarded"
            if fwd_score and fwd_score > 4:
                fwd_label += " many times"
            p.setFont(self.F_FWD)
            p.setPen(self.FWD_COL)
            p.drawText(QRect(cx, dy, cw, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       fwd_label)
            dy += 14

        # Sender (with phone/wa_name detail for group chats)
        if self._is_group and not from_me:
            name = msg.get("sender_name", "Unknown")
            if is_bot:
                name = "\U0001F916 " + name
            sid = msg.get("sender_id") or 0
            col = self.SENDER_COLORS[sid % len(self.SENDER_COLORS)]
            p.setFont(self.F_SENDER)
            p.setPen(col)
            fm_sn = self._fm_sender
            name_w = fm_sn.horizontalAdvance(name)
            sender_rect = QRect(cx, dy, cw, self.SENDER_H)
            p.drawText(sender_rect,
                       Qt.AlignLeft | Qt.AlignVCenter,
                       fm_sn.elidedText(name, Qt.ElideRight, cw))
            # Show phone_jid or wa_name in dimmer text next to sender
            detail = msg.get("phone_jid") or msg.get("wa_name") or ""
            if detail and detail != name:
                detail_text = f"  (@{detail.split('@')[0]})" if "@" in detail else f"  ({detail})"
                p.setFont(self.F_QUOTE)
                _detail_col = QColor(120, 130, 140, 160) if self.RECV_BG.lightness() > 128 else QColor(148, 171, 184, 140)
                p.setPen(_detail_col)
                detail_x = cx + min(name_w + 4, int(cw * 0.7))
                p.drawText(QRect(detail_x, dy, cw - (detail_x - cx), self.SENDER_H),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_quote.elidedText(detail_text, Qt.ElideRight,
                                                                  cw - (detail_x - cx)))
            if row >= 0 and sid:
                self._sender_rects[row] = (sender_rect, sid)
            dy += self.SENDER_H

            # Member tag / admin label (e.g. apartment number "F-804")
            member_label = msg.get("member_label")
            if member_label:
                p.setFont(self.F_QUOTE)
                _tag_col = QColor(100, 115, 130, 200) if self.RECV_BG.lightness() > 128 else QColor(148, 170, 185, 160)
                p.setPen(_tag_col)
                fm_tag = self._fm_quote
                p.drawText(QRect(cx, dy, cw, 12),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           fm_tag.elidedText(member_label, Qt.ElideRight, cw))
                dy += 14

        # Quoted reply (clickable) - WhatsApp-style card with accent bar
        _qt_render = msg.get("quoted_text") or ""
        if not _qt_render and msg.get("reply_to_key_id"):
            _qtype_r = msg.get("quoted_type")
            _qt_render = {1: "\U0001F4F7 Photo", 2: "\U0001F3B5 Audio", 3: "\U0001F3AC Video",
                          5: "\U0001F4CD Location", 8: "\U0001F4C4 Document", 9: "\U0001F4C4 Document",
                          13: "GIF", 15: "\U0001F4C4 Document", 20: "Sticker", 46: "\U0001F4CA Poll",
                          82: "\U0001F3A4 View-once voice"}.get(_qtype_r, "\u21A9 Message")
        if _qt_render:
            qh = quote_h - 3
            quote_rect = QRect(cx, dy, cw, qh)
            # Rounded background - more visible
            qpath = QPainterPath()
            qpath.addRoundedRect(float(cx), float(dy), float(cw), float(qh), 8, 8)
            p.fillPath(qpath, self.QUOTE_BG)
            # Accent bar on left (WhatsApp-style thick bar)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(float(cx), float(dy), 4.0, float(qh), 2, 2)
            p.fillPath(bar_path, self.QUOTE_BAR)
            # Reply icon
            p.setFont(QFont("Segoe UI", self._base_font_size - 2))
            p.setPen(QColor(self.QUOTE_BAR.red(), self.QUOTE_BAR.green(),
                            self.QUOTE_BAR.blue(), 180))
            p.drawText(QRect(cx + cw - 50, dy + 1, 46, 13),
                       Qt.AlignRight | Qt.AlignVCenter, "\u21A9 Reply")
            # Quote text - brighter, more readable
            p.setFont(self.F_QUOTE)
            p.setPen(self.QUOTE_TEXT)
            _cc_name = msg.get("quoted_cross_chat_conv_name")
            _qt_text_h = qh - 4 - (18 if _cc_name else 0)
            p.drawText(QRect(cx + 9, dy + 2, cw - 60, _qt_text_h),
                       Qt.TextWordWrap | Qt.AlignLeft,
                       _qt_render[:120])
            # Cross-chat quote button — visible "Go to [ChatName]" pill
            if _cc_name:
                _cc_conv_id = msg.get("quoted_cross_chat_conv_id", 0)
                _cc_msg_id = msg.get("quoted_cross_chat_msg_id", 0)
                _cc_y = dy + qh - 20
                _cc_btn_text = f"\U0001F4AC {_cc_name}"
                _cc_fm = self._fm_quote
                _cc_tw = min(_cc_fm.horizontalAdvance(_cc_btn_text) + 32, cw - 16)
                _cc_rect = QRect(cx + 8, _cc_y, _cc_tw, 16)
                # Pill background
                _cc_path = QPainterPath()
                _cc_path.addRoundedRect(float(_cc_rect.x()), float(_cc_rect.y()),
                                        float(_cc_rect.width()), float(_cc_rect.height()), 8, 8)
                p.fillPath(_cc_path, QColor("#e040fb"))
                # Text
                p.setFont(self.F_QUOTE)
                p.setPen(QColor("white"))
                p.drawText(_cc_rect, Qt.AlignCenter,
                           _cc_fm.elidedText(_cc_btn_text + "  \u2192", Qt.ElideMiddle, _cc_tw - 8))
                if row >= 0 and _cc_conv_id:
                    self._cross_chat_rects[row] = (_cc_rect, _cc_conv_id, _cc_msg_id)
            # Clickable bottom hint line
            p.setPen(self.QUOTE_BORDER)
            p.drawLine(cx + 6, dy + qh - 1, cx + cw - 6, dy + qh - 1)
            if row >= 0 and msg.get("reply_to_key_id"):
                self._quote_rects[row] = quote_rect
            dy += quote_h

        # Media image (from disk or thumbnail)
        media_drawn = False
        resolved_fp = msg.get("resolved_file_path")
        if has_media_file and (type_label in ("image", "sticker", "gif", "animated_gif")
                               or (msg.get("mime_type") or "").startswith("image/")):
            fp = msg.get("file_path", "")
            pxm = self._load_media_pixmap(fp, cw, self.THUMB_MAX_H, resolved_fp)
            if pxm and not pxm.isNull():
                clip = QPainterPath()
                clip.addRoundedRect(float(cx), float(dy),
                                    float(pxm.width()), float(pxm.height()), 5, 5)
                p.setClipPath(clip)
                p.drawPixmap(cx, dy, pxm)
                p.setClipping(False)
                resolved = _resolve_media_path(fp, resolved_fp, self._resolve_cache)
                if row >= 0 and resolved:
                    self._media_rects[row] = (QRect(cx, dy, pxm.width(), pxm.height()), resolved)
                dy += pxm.height() + 3
                media_drawn = True

        # Skip big thumbnail for text/link-preview messages — those use 80x80 in the link card
        _is_link_msg = (
            type_label in ("", None, "text")
            and _URL_RE.search(msg.get("display_text", "") or msg.get("text_content", "") or "")
        )
        if not media_drawn and msg.get("has_thumb") and thumb_h != 0 and not _is_link_msg:
            pxm = self._get_thumb(msg.get("id", 0), msg.get("thumbnail_blob"))

            # Document thumbnails: large preview image + filename bar below
            if pxm and not pxm.isNull() and type_label == "document":
                _light_bg = self.RECV_BG.lightness() > 128
                # Scale thumbnail to fill width, up to 120px tall
                doc_thumb_max_h = 120
                t_scaled = pxm.scaled(cw, doc_thumb_max_h,
                                      Qt.KeepAspectRatio, Qt.SmoothTransformation)
                thumb_h = t_scaled.height()
                bar_h = 40  # filename bar below the preview
                total_h = thumb_h + bar_h

                # Background card
                dcp = QPainterPath()
                dcp.addRoundedRect(float(cx), float(dy), float(cw), float(total_h), 6, 6)
                doc_bg = QColor(225, 235, 245, 200) if _light_bg else QColor(20, 30, 40, 200)
                p.fillPath(dcp, doc_bg)

                # Thumbnail image (top portion, clipped to rounded top)
                clip_top = QPainterPath()
                clip_top.addRoundedRect(float(cx), float(dy), float(cw), float(thumb_h + 6), 6, 6)
                p.setClipPath(clip_top)
                tx = cx + (cw - t_scaled.width()) // 2
                p.drawPixmap(tx, dy, t_scaled)
                p.setClipping(False)

                # Filename bar (bottom portion)
                bar_y = dy + thumb_h
                fp = msg.get("file_path", "")
                fname = msg.get("media_name") or (os.path.basename(fp) if fp else "Document")
                fs = msg.get("file_size") or 0
                mime = msg.get("mime_type") or ""
                ext = mime.split("/")[-1].upper() if "/" in mime else "FILE"
                size_str = f"{fs / 1_048_576:.1f} MB" if fs >= 1_048_576 else f"{fs // 1024} KB" if fs > 1024 else ""

                p.setFont(self.F_TYPE)
                p.setPen(self.TEXT_COL)
                p.drawText(QRect(cx + 8, bar_y + 2, cw - 16, 18),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_type.elidedText(fname, Qt.ElideMiddle, cw - 16))
                info_str = f"\u25A3 {ext}  {size_str}" if size_str else f"\u25A3 {ext}"
                p.setFont(self.F_QUOTE)
                p.setPen(self.TIME_COL)
                p.drawText(QRect(cx + 8, bar_y + 20, cw - 16, 16),
                           Qt.AlignLeft | Qt.AlignVCenter, info_str)

                # Make document type obvious in the preview card, especially PDFs.
                if ext:
                    chip_w = 42 if len(ext) <= 4 else 52
                    chip_rect = QRect(cx + cw - chip_w - 8, dy + 8, chip_w, 18)
                    chip_bg = QColor(210, 50, 50, 220) if ext == "PDF" else QColor(100, 120, 150, 220)
                    chip_path = QPainterPath()
                    chip_path.addRoundedRect(float(chip_rect.x()), float(chip_rect.y()),
                                             float(chip_rect.width()), float(chip_rect.height()), 8, 8)
                    p.fillPath(chip_path, chip_bg)
                    p.setFont(QFont("Segoe UI", 8, QFont.Bold))
                    p.setPen(QColor("white"))
                    p.drawText(chip_rect, Qt.AlignCenter, ext)

                card_rect = QRect(cx, dy, cw, total_h)
                if has_media_file and row >= 0:
                    resolved = _resolve_media_path(msg.get("file_path", ""), resolved_fp, self._resolve_cache)
                    if resolved:
                        self._media_rects[row] = (card_rect, resolved)
                dy += total_h + 3

            # Image/video/gif thumbnails: render large
            elif pxm and not pxm.isNull():
                scaled = pxm.scaled(cw, self.THUMB_MAX_H,
                                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
                clip = QPainterPath()
                clip.addRoundedRect(float(cx), float(dy),
                                    float(scaled.width()), float(scaled.height()), 5, 5)
                p.setClipPath(clip)
                p.drawPixmap(cx, dy, scaled)
                p.setClipping(False)

                # Media type overlay badge
                if type_label == "video":
                    pcx = cx + scaled.width() // 2 - 20
                    pcy = dy + scaled.height() // 2 - 20
                    play_bg = QPainterPath()
                    play_bg.addEllipse(float(pcx), float(pcy), 40.0, 40.0)
                    p.fillPath(play_bg, QColor(0, 0, 0, 140))
                    p.setFont(QFont("Segoe UI", 16, QFont.Bold))
                    p.setPen(QColor(255, 255, 255))
                    p.drawText(QRect(pcx, pcy, 40, 40), Qt.AlignCenter, "\u25B6")
                elif type_label in ("gif", "animated_gif"):
                    badge = "GIF"
                    p.setFont(QFont("Segoe UI", 10, QFont.Bold))
                    bw2 = 34
                    badge_rect = QRect(cx + scaled.width() - bw2 - 4, dy + 4, bw2, 20)
                    bp = QPainterPath()
                    bp.addRoundedRect(float(badge_rect.x()), float(badge_rect.y()),
                                      float(bw2), 20.0, 4, 4)
                    p.fillPath(bp, QColor(0, 0, 0, 160))
                    p.setPen(QColor(255, 255, 255))
                    p.drawText(badge_rect, Qt.AlignCenter, badge)

                # If media file exists, store clickable rect
                if has_media_file and row >= 0:
                    resolved = _resolve_media_path(msg.get("file_path", ""), resolved_fp, self._resolve_cache)
                    if resolved:
                        self._media_rects[row] = (QRect(cx, dy, scaled.width(), scaled.height()), resolved)

                # Media status overlay
                if not has_media_file:
                    # ── THUMBNAIL ONLY — actual media file is MISSING ──
                    # Larger, more prominent overlay so user can clearly see the status
                    ov_h = 36
                    ov_y = dy + scaled.height() - ov_h
                    ov_path = QPainterPath()
                    ov_path.addRoundedRect(float(cx), float(ov_y),
                                           float(scaled.width()), float(ov_h), 0, 0)
                    p.fillPath(ov_path, QColor(0, 0, 0, 180))

                    has_dl = bool(msg.get("media_url") and msg.get("media_key"))
                    has_hash_copy = bool(msg.get("file_hash"))  # hash exists, other copies may exist

                    if has_dl:
                        # Downloadable from CDN
                        p.setFont(QFont(self.F_TEXT.family(), self._base_font_size - 2, QFont.Bold))
                        p.setPen(QColor(100, 200, 255))
                        p.drawText(QRect(cx + 4, ov_y, scaled.width() - 8, 18),
                                   Qt.AlignCenter, "\u2B07 THUMBNAIL ONLY")
                        p.setFont(self.F_QUOTE)
                        p.setPen(QColor(180, 220, 255))
                        p.drawText(QRect(cx + 4, ov_y + 17, scaled.width() - 8, 16),
                                   Qt.AlignCenter, "Tap to download from WhatsApp CDN")
                    else:
                        # Not downloadable
                        p.setFont(QFont(self.F_TEXT.family(), self._base_font_size - 2, QFont.Bold))
                        p.setPen(QColor(255, 180, 100))
                        p.drawText(QRect(cx + 4, ov_y, scaled.width() - 8, 18),
                                   Qt.AlignCenter, "\u26A0 THUMBNAIL ONLY")
                        p.setFont(self.F_QUOTE)
                        p.setPen(QColor(255, 200, 150))
                        label2 = "Media file missing \u2022 CDN link expired"
                        p.drawText(QRect(cx + 4, ov_y + 17, scaled.width() - 8, 16),
                                   Qt.AlignCenter, label2)

                    # Register click for download
                    if row >= 0 and has_dl:
                        self._media_rects[row] = (QRect(cx, dy, scaled.width(), scaled.height()), "__no_file__")

                else:
                    # ── MEDIA ON DISK — check if it was hash-reconstructed ──
                    orig_fp = msg.get("file_path", "")
                    res_fp = msg.get("resolved_file_path", "")
                    if (orig_fp and res_fp and orig_fp != res_fp
                            and os.path.basename(orig_fp) != os.path.basename(res_fp)):
                        # File was found via hash matching from a different file
                        ov_h = 18
                        ov_y = dy + scaled.height() - ov_h
                        ov_path = QPainterPath()
                        ov_path.addRoundedRect(float(cx), float(ov_y),
                                               float(scaled.width()), float(ov_h), 0, 0)
                        p.fillPath(ov_path, QColor(40, 80, 160, 180))
                        p.setFont(self.F_QUOTE)
                        p.setPen(QColor(180, 210, 255))
                        p.drawText(QRect(cx + 4, ov_y, scaled.width() - 8, ov_h),
                                   Qt.AlignCenter, "\U0001F517 Matched via file hash")

                dy += scaled.height() + 3
            else:
                dy += 3

        # Voice note / audio card with waveform visualization
        if not media_drawn and not msg.get("has_thumb") and type_label in ("voice", "audio"):
            fp = msg.get("file_path", "")
            resolved = _resolve_media_path(fp, resolved_fp, self._resolve_cache)
            fs = msg.get("file_size") or 0

            audio_h = 44
            apath = QPainterPath()
            apath.addRoundedRect(float(cx), float(dy), float(cw), float(audio_h), 8, 8)
            _audio_bg = QColor(220, 240, 225, 180) if self.RECV_BG.lightness() > 128 else QColor(20, 35, 30, 200)
            p.fillPath(apath, _audio_bg)

            # Is this audio currently playing?
            _is_playing = self._playing_msg_id == msg.get("id", 0) and self._playing_msg_id != 0
            _progress = self._audio_progress if _is_playing else 0.0

            # Play/pause button circle
            play_r = 32
            play_x, play_y = cx + 6, dy + 6
            pp = QPainterPath()
            pp.addEllipse(float(play_x), float(play_y), float(play_r), float(play_r))
            p.fillPath(pp, QColor(0, 168, 132))
            p.setFont(QFont("Segoe UI", 12))
            p.setPen(QColor(255, 255, 255))
            icon_char = "\u23F8" if _is_playing else "\u25B6"
            p.drawText(QRect(play_x + 2, play_y, play_r, play_r), Qt.AlignCenter, icon_char)

            # Waveform bars with progress coloring
            seed = hash(msg.get("id", 0))
            bar_x = cx + 44
            bar_w = cw - 54
            bar_count = min(int(bar_w / 4), 40)
            progress_x = bar_x + int(bar_w * _progress) if _is_playing else 0
            for i in range(bar_count):
                bh_val = 4 + ((seed * (i + 1) * 7) % 18)
                bx = bar_x + i * (bar_w // bar_count)
                by_bar = dy + 22 - bh_val // 2
                bp = QPainterPath()
                bp.addRoundedRect(float(bx), float(by_bar), 2.0, float(bh_val), 1, 1)
                if _is_playing and bx < progress_x:
                    p.fillPath(bp, QColor(0, 168, 132, 240))  # played portion
                else:
                    p.fillPath(bp, QColor(0, 168, 132, 100))  # unplayed

            # Store waveform rect for seek detection
            if row >= 0:
                self._waveform_rects[row] = (QRect(bar_x, dy, bar_w, 28), msg.get("id", 0))

            # Duration / elapsed time
            dur_str = ""
            dur_ms = msg.get("media_duration_ms") or 0
            if dur_ms > 0:
                if _is_playing:
                    elapsed = int(dur_ms * _progress / 1000)
                    total_secs = dur_ms // 1000
                    em, es = divmod(elapsed, 60)
                    tm, ts = divmod(total_secs, 60)
                    dur_str = f"{em}:{es:02d} / {tm}:{ts:02d}"
                else:
                    total_secs = dur_ms // 1000
                    mins, secs = divmod(total_secs, 60)
                    dur_str = f"{mins}:{secs:02d}"
            elif fs > 0:
                est_secs = fs // 2000
                mins, secs = divmod(est_secs, 60)
                dur_str = f"{mins}:{secs:02d}"
            _dur_col = QColor(100, 115, 125, 200) if self.RECV_BG.lightness() > 128 else QColor(148, 171, 184, 200)
            p.setFont(self.F_QUOTE)
            p.setPen(_dur_col)
            p.drawText(QRect(bar_x, dy + 28, bar_w, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, dur_str)

            # Always register click rect for audio (even if file not on disk)
            if row >= 0:
                if resolved:
                    self._media_rects[row] = (QRect(cx, dy, cw, audio_h), resolved)
                elif has_media_file:
                    resolved2 = _resolve_media_path(fp, resolved_fp, self._resolve_cache)
                    if resolved2:
                        self._media_rects[row] = (QRect(cx, dy, cw, audio_h), resolved2)
                    else:
                        self._media_rects[row] = (QRect(cx, dy, cw, audio_h), "__no_file__")
                else:
                    # File not on disk — register for download prompt
                    self._media_rects[row] = (QRect(cx, dy, cw, audio_h), "__no_file__")

            # Overlay download icon if file not on disk
            if not resolved and not has_media_file:
                p.setFont(QFont("Segoe UI", 8))
                _lt = self.RECV_BG.lightness() > 128
                p.setPen(QColor(180, 80, 60) if _lt else QColor(255, 130, 100))
                p.drawText(QRect(bar_x, dy + 28, bar_w, 14),
                           Qt.AlignRight | Qt.AlignVCenter, "\u21E9 Not on disk")
            dy += audio_h + 3
            media_drawn = True  # prevent duplicate cards

        # For media types without image but with file on disk, show card
        if not media_drawn and not msg.get("has_thumb") and has_media_file:
            if type_label == "video":
                # Video card with play button
                fp = msg.get("file_path", "")
                resolved = _resolve_media_path(fp, resolved_fp, self._resolve_cache)
                fname = os.path.basename(resolved) if resolved else fp.split("/")[-1]
                fs = msg.get("file_size") or 0
                _lt = self.RECV_BG.lightness() > 128
                vid_h = 52
                vpath = QPainterPath()
                vpath.addRoundedRect(float(cx), float(dy), float(cw), float(vid_h), 6, 6)
                p.fillPath(vpath, QColor(230, 235, 245, 200) if _lt else QColor(20, 25, 40, 200))
                p.setPen(QColor(0, 0, 0, 15) if _lt else QColor(255, 255, 255, 15))
                p.drawPath(vpath)
                # Play circle
                play_x, play_y = cx + 8, dy + 8
                pp = QPainterPath()
                pp.addEllipse(float(play_x), float(play_y), 36.0, 36.0)
                p.fillPath(pp, QColor(0, 137, 123) if _lt else QColor(0, 188, 212, 200))
                p.setFont(QFont("Segoe UI", 14, QFont.Bold))
                p.setPen(QColor(255, 255, 255))
                p.drawText(QRect(play_x + 2, play_y, 36, 36), Qt.AlignCenter, "\u25B6")
                # Filename + size
                p.setFont(self.F_SENDER)
                p.setPen(self.TEXT_COL)
                name_w = cw - 60
                p.drawText(QRect(cx + 52, dy + 6, name_w, 18),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_sender.elidedText(fname, Qt.ElideMiddle, name_w))
                size_str = f"{fs / 1_048_576:.1f} MB" if fs >= 1_048_576 else f"{fs // 1024} KB" if fs > 1024 else ""
                p.setFont(self.F_QUOTE)
                p.setPen(self.TIME_COL)
                p.drawText(QRect(cx + 52, dy + 26, name_w, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, f"Video  {size_str}")
                card_rect = QRect(cx, dy, cw, vid_h)
                if row >= 0 and resolved:
                    self._media_rects[row] = (card_rect, resolved)
                dy += vid_h + 3
                media_drawn = True
            elif type_label in ("audio", "document"):
                icon = TYPE_EMOJI.get(type_label, "\U0001F4C1")
                fp = msg.get("file_path", "")
                resolved = _resolve_media_path(fp, resolved_fp, self._resolve_cache)
                fname = os.path.basename(resolved) if resolved else fp.split("/")[-1]
                fs = msg.get("file_size") or 0
                size_str = f" ({fs // 1024} KB)" if fs > 1024 else ""
                label = f"{icon} {fname}{size_str}"
                p.setFont(self.F_TYPE)
                p.setPen(self.LINK_COL)
                label_rect = QRect(cx, dy, cw, 18)
                p.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_type.elidedText(label, Qt.ElideMiddle, cw))
                if row >= 0 and resolved:
                    self._media_rects[row] = (label_rect, resolved)
                dy += 18

        # Media placeholder card (no thumb AND no file on disk)
        if (not media_drawn and not msg.get("has_thumb") and not has_media_file
                and type_label in ("image", "video", "audio", "document",
                                   "gif", "animated_gif", "voice")
                and (msg.get("file_path") or msg.get("media_url"))):
            has_url = bool(msg.get("media_url"))
            has_key = bool(msg.get("media_key"))
            card_h = 48
            cpath = QPainterPath()
            cpath.addRoundedRect(float(cx), float(dy), float(cw), float(card_h), 6, 6)
            # Color-coded background based on downloadability
            _light_bg = self.RECV_BG.lightness() > 128
            if has_url and has_key:
                p.fillPath(cpath, QColor(225, 240, 250, 200) if _light_bg else QColor(15, 30, 45, 220))
                p.setPen(QColor(0, 120, 180, 40) if _light_bg else QColor(83, 189, 237, 50))
            else:
                p.fillPath(cpath, QColor(250, 230, 230, 200) if _light_bg else QColor(35, 25, 25, 200))
                p.setPen(QColor(200, 80, 80, 30) if _light_bg else QColor(255, 100, 100, 30))
            p.drawPath(cpath)
            # Type icon
            micon = TYPE_EMOJI.get(type_label, "\U0001F4C4")
            p.setFont(QFont("Segoe UI", 14))
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 8, dy + 4, 28, 28), Qt.AlignCenter, micon)
            # File name
            fp = msg.get("file_path", "")
            fname = os.path.basename(fp) if fp else type_label.replace("_", " ").title()
            p.setFont(self.F_TYPE)
            p.setPen(self.TEXT_COL)
            # Reserve space for download button on right
            btn_w = 80 if has_url and has_key else 0
            name_w = cw - 50 - btn_w
            p.drawText(QRect(cx + 40, dy + 4, name_w, 16),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       self._fm_type.elidedText(fname, Qt.ElideMiddle, name_w))
            # File size + status
            fs = msg.get("file_size") or 0
            size_str = f"{fs // 1024} KB" if fs > 1024 else ""
            if has_media_file:
                status_text, status_col = "\u2713 On disk", QColor(100, 220, 100)
            elif has_url and has_key:
                status_text, status_col = "\u21E9 Downloadable", QColor(83, 189, 237)
            elif has_url:
                status_text, status_col = "\u26A0 URL only (no key)", QColor(200, 180, 100)
            else:
                status_text, status_col = "\u2718 Not available", QColor(160, 120, 120)
            info = f"{size_str}  {status_text}" if size_str else status_text
            p.setFont(self.F_QUOTE)
            p.setPen(status_col)
            p.drawText(QRect(cx + 40, dy + 22, name_w, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, info)
            # Download button (only for downloadable media)
            if has_url and has_key and row >= 0:
                btn_x = cx + cw - btn_w - 4
                btn_y = dy + 8
                btn_h = 28
                btn_path = QPainterPath()
                btn_path.addRoundedRect(float(btn_x), float(btn_y),
                                         float(btn_w), float(btn_h), 6, 6)
                p.fillPath(btn_path, QColor(0, 150, 136, 60))
                p.setPen(QColor(0, 188, 212, 120))
                p.drawPath(btn_path)
                p.setFont(QFont("Segoe UI", 9, QFont.Bold))
                p.setPen(QColor(0, 188, 212))
                p.drawText(QRect(btn_x, btn_y, btn_w, btn_h),
                           Qt.AlignCenter, "\u21E9 Download")
                self._download_rects[row] = (
                    QRect(btn_x, btn_y, btn_w, btn_h), msg)
            dy += 52

        # Photo album grid (WhatsApp-style)
        if (not media_drawn and type_label == "album"):
            children = msg.get("album_children") or []
            if children:
                n = min(len(children), 6)
                cols = 2 if n <= 4 else 3
                rows_grid = (n + cols - 1) // cols
                cell_h = min(self.ALBUM_GRID_MAX, 140)
                gap = 2
                cell_w = (cw - gap * (cols - 1)) // cols

                for idx in range(n):
                    r_idx = idx // cols
                    c_idx = idx % cols
                    gx = cx + c_idx * (cell_w + gap)
                    gy = dy + r_idx * (cell_h + gap)

                    child = children[idx]
                    # Try to get thumbnail for this child
                    child_pxm = None
                    child_id = child.get("id", 0)
                    child_blob = child.get("thumbnail_blob")
                    if child_blob and len(child_blob) > 20:
                        child_pxm = self._get_thumb(child_id, child_blob)

                    if child_pxm and not child_pxm.isNull():
                        # Scale to fill cell (crop to fit)
                        scaled = child_pxm.scaled(
                            cell_w, cell_h, Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation)
                        # Center-crop
                        sx = (scaled.width() - cell_w) // 2
                        sy = (scaled.height() - cell_h) // 2
                        cropped = scaled.copy(sx, sy, min(cell_w, scaled.width()),
                                              min(cell_h, scaled.height()))
                        # Rounded corners clip
                        clip_path = QPainterPath()
                        clip_path.addRoundedRect(float(gx), float(gy),
                                                  float(cell_w), float(cell_h), 4, 4)
                        p.save()
                        p.setClipPath(clip_path)
                        p.drawPixmap(gx, gy, cropped)
                        p.restore()
                        # Video overlay icon
                        if child.get("type_label") == "video":
                            p.setPen(Qt.NoPen)
                            p.setBrush(QColor(0, 0, 0, 120))
                            p.drawEllipse(QPointF(gx + cell_w / 2, gy + cell_h / 2), 14, 14)
                            p.setFont(QFont("Segoe UI", 10))
                            p.setPen(QColor(255, 255, 255))
                            p.drawText(QRect(gx, gy, cell_w, cell_h), Qt.AlignCenter, "\u25B6")
                    else:
                        # Empty cell with placeholder
                        _lt_album = self.RECV_BG.lightness() > 128
                        cell_path = QPainterPath()
                        cell_path.addRoundedRect(float(gx), float(gy),
                                                  float(cell_w), float(cell_h), 4, 4)
                        p.fillPath(cell_path,
                                   QColor(220, 230, 240) if _lt_album else QColor(30, 40, 55))
                        p.setFont(QFont("Segoe UI", 14))
                        p.setPen(QColor(160, 170, 180) if _lt_album else QColor(80, 90, 100))
                        icon = "\u25B6" if child.get("type_label") == "video" else "\U0001F5BC"
                        p.drawText(QRect(gx, gy, cell_w, cell_h), Qt.AlignCenter, icon)

                # Count label if more than 6 children
                total_children = len(msg.get("album_children") or [])
                grid_h = rows_grid * cell_h + (rows_grid - 1) * gap
                if total_children > 6:
                    # "+N more" overlay on last cell
                    extra = total_children - 6
                    last_gx = cx + ((n - 1) % cols) * (cell_w + gap)
                    last_gy = dy + ((n - 1) // cols) * (cell_h + gap)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(0, 0, 0, 140))
                    ov_path = QPainterPath()
                    ov_path.addRoundedRect(float(last_gx), float(last_gy),
                                            float(cell_w), float(cell_h), 4, 4)
                    p.drawPath(ov_path)
                    p.setFont(QFont("Segoe UI", 14, QFont.Bold))
                    p.setPen(QColor(255, 255, 255))
                    p.drawText(QRect(last_gx, last_gy, cell_w, cell_h),
                               Qt.AlignCenter, f"+{extra}")

                # Caption below grid
                caption = msg.get("media_caption") or msg.get("text_content") or ""
                cap_y = dy + grid_h + 4
                p.setFont(self.F_QUOTE)
                p.setPen(self.TIME_COL)
                cap_text = f"\U0001F5BC {total_children} photos/videos"
                if caption and caption != "Photo album":
                    cap_text = caption
                p.drawText(QRect(cx, cap_y, cw, 14),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_quote.elidedText(cap_text, Qt.ElideRight, cw))
                dy += grid_h + 20
            else:
                # Fallback: simple placeholder (no children found)
                _lt_album = self.RECV_BG.lightness() > 128
                album_h = 52
                apath = QPainterPath()
                apath.addRoundedRect(float(cx), float(dy), float(cw), float(album_h), 6, 6)
                p.fillPath(apath, QColor(230, 240, 250, 200) if _lt_album else QColor(20, 30, 50, 200))
                p.setFont(QFont("Segoe UI", 16))
                p.setPen(self.TEXT_COL)
                p.drawText(QRect(cx + 8, dy + 8, 32, 32), Qt.AlignCenter, "\U0001F5BC")
                p.setFont(self.F_SENDER)
                p.drawText(QRect(cx + 46, dy + 6, cw - 56, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, "Photo Album")
                p.setFont(self.F_QUOTE)
                p.setPen(self.TIME_COL)
                p.drawText(QRect(cx + 46, dy + 26, cw - 56, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, "Multiple photos/videos")
                dy += album_h + 3

        # Location card
        if type_label in ("location", "live_location"):
            lat = msg.get("loc_latitude")
            lng = msg.get("loc_longitude")
            place = msg.get("loc_place_name") or ""
            addr = msg.get("loc_place_address") or ""
            is_live = msg.get("loc_is_live", False)
            live_dur = msg.get("loc_live_duration")

            loc_h = 48
            if place:
                loc_h += 16
            if addr:
                loc_h += 14

            lpath = QPainterPath()
            lpath.addRoundedRect(float(cx), float(dy), float(cw), float(loc_h), 6, 6)
            loc_bg = QColor(220, 240, 225, 200) if self.RECV_BG.lightness() > 128 else QColor(20, 45, 30, 180)
            p.fillPath(lpath, loc_bg)
            loc_border = QColor(60, 140, 80, 30) if self.RECV_BG.lightness() > 128 else QColor(60, 140, 80, 60)
            p.setPen(loc_border)
            p.drawPath(lpath)
            p.setFont(QFont("Segoe UI", 16))
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 6, dy + 4, 30, 30), Qt.AlignCenter, "\U0001F4CD")

            loc_label = "Live Location" if is_live else "Location"
            if is_live and live_dur:
                mins = live_dur // 60
                loc_label += f" ({mins}m)" if mins else ""
            p.setFont(self.F_SENDER)
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 38, dy + 4, cw - 48, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, loc_label)

            sub_y = dy + 22
            if place:
                p.setFont(self.F_TYPE)
                p.setPen(self.TEXT_COL)
                p.drawText(QRect(cx + 38, sub_y, cw - 48, 14),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_type.elidedText(place, Qt.ElideRight, cw - 48))
                sub_y += 16
            if addr:
                p.setFont(self.F_QUOTE)
                p.setPen(self.VCARD_SUB)
                p.drawText(QRect(cx + 38, sub_y, cw - 48, 12),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_quote.elidedText(addr, Qt.ElideRight, cw - 48))
                sub_y += 14

            # Coordinates + "Open in Maps" link
            if lat is not None and lng is not None:
                coord_str = f"{lat:.6f}, {lng:.6f}"
                p.setFont(self.F_QUOTE)
                p.setPen(QColor(148, 171, 184, 160))
                p.drawText(QRect(cx + 38, sub_y, cw // 2, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, coord_str)
                p.setPen(self.LINK_COL)
                link_txt = "Open in Maps"
                link_w = self._fm_quote.horizontalAdvance(link_txt) + 4
                link_x = cx + cw - link_w - 8
                link_rect = QRect(link_x, sub_y, link_w, 14)
                p.drawText(link_rect, Qt.AlignLeft | Qt.AlignVCenter, link_txt)
                if row >= 0:
                    maps_url = f"https://maps.google.com/?q={lat},{lng}"
                    if row not in self._link_rects:
                        self._link_rects[row] = []
                    self._link_rects[row].append((link_rect, maps_url))
            else:
                p.setFont(self.F_QUOTE)
                p.setPen(self.LINK_COL)
                p.drawText(QRect(cx + 38, sub_y, cw - 48, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, "View Location")

            dy += loc_h

        # Poll card with vote bars
        if type_label in ("poll", "poll_vote") or msg.get("poll_options"):
            poll_question = msg.get("display_text") or msg.get("text_content") or ""
            poll_options_str = msg.get("poll_options") or ""
            poll_label = "Poll"
            total_voters = msg.get("poll_total_voters") or 0
            is_light = self.RECV_BG.lightness() > 128

            # Draw question text ABOVE the poll card
            if poll_question:
                p.setFont(self.F_TEXT)
                p.setPen(self.TEXT_COL)
                fm_pq = self._fm_text
                elided_q = fm_pq.elidedText(poll_question, Qt.ElideRight, cw - 12)
                p.drawText(QRect(cx + 6, dy, cw - 12, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, elided_q)
                dy += 20

            # Parse options with vote counts (format: "option_name::vote_count")
            opt_entries = []
            for line in poll_options_str.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "::" in line:
                    parts = line.rsplit("::", 1)
                    name = parts[0]
                    try:
                        votes = int(parts[1])
                    except (ValueError, IndexError):
                        votes = 0
                else:
                    name, votes = line, 0
                opt_entries.append((name, votes))

            max_votes = max((v for _, v in opt_entries), default=0)
            poll_voters = msg.get("poll_voters") or ""
            poll_h = 32 + len(opt_entries) * 28 + (18 if total_voters else 0) + (14 if poll_voters else 0)

            ppath = QPainterPath()
            ppath.addRoundedRect(float(cx), float(dy), float(cw), float(poll_h), 6, 6)
            poll_bg = QColor(230, 235, 240, 200) if is_light else QColor(30, 30, 50, 180)
            p.fillPath(ppath, poll_bg)
            poll_border = QColor(0, 0, 0, 15) if is_light else QColor(100, 100, 180, 40)
            p.setPen(poll_border)
            p.drawPath(ppath)

            # Poll icon + label
            p.setFont(QFont("Segoe UI", 13))
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 6, dy + 2, 26, 26), Qt.AlignCenter, "\U0001F4CA")
            p.setFont(self.F_SENDER)
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 36, dy + 4, cw - 46, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, poll_label)

            # Options with vote bars
            oy = dy + 30
            opt_text_col = QColor(60, 72, 80) if is_light else QColor(180, 195, 210)
            bar_bg = QColor(0, 0, 0, 20) if is_light else QColor(255, 255, 255, 15)
            bar_fill = QColor(0, 137, 123, 120) if is_light else QColor(0, 188, 212, 100)
            vote_count_col = QColor(0, 137, 123) if is_light else QColor(0, 188, 212)
            bar_w = cw - 24

            for opt_name, opt_votes in opt_entries[:8]:
                # Vote bar background
                bar_rect = QRect(cx + 12, oy + 1, bar_w, 16)
                bar_path = QPainterPath()
                bar_path.addRoundedRect(float(bar_rect.x()), float(bar_rect.y()),
                                         float(bar_rect.width()), float(bar_rect.height()), 3, 3)
                p.fillPath(bar_path, bar_bg)

                # Vote bar fill (proportional)
                if max_votes > 0 and opt_votes > 0:
                    fill_w = max(4, int(bar_w * opt_votes / max_votes))
                    fill_path = QPainterPath()
                    fill_path.addRoundedRect(float(bar_rect.x()), float(bar_rect.y()),
                                              float(fill_w), float(bar_rect.height()), 3, 3)
                    p.fillPath(fill_path, bar_fill)

                # Option text
                p.setFont(self.F_TYPE)
                p.setPen(opt_text_col)
                elided = self._fm_type.elidedText(opt_name, Qt.ElideRight, bar_w - 40)
                p.drawText(QRect(cx + 16, oy, bar_w - 44, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, elided)

                # Vote count on the right
                p.setFont(self.F_QUOTE)
                p.setPen(vote_count_col)
                p.drawText(QRect(cx + bar_w - 24, oy, 32, 18),
                           Qt.AlignRight | Qt.AlignVCenter, str(opt_votes))
                oy += 28

            # Total voters footer
            if total_voters:
                p.setFont(self.F_QUOTE)
                p.setPen(QColor(130, 145, 155) if is_light else QColor(148, 171, 184, 180))
                p.drawText(QRect(cx + 12, oy, bar_w, 14),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           f"{total_voters} vote{'s' if total_voters != 1 else ''}")
                oy += 16

            # Voter names
            if poll_voters:
                p.setFont(self.F_QUOTE)
                _voter_col = QColor(100, 115, 130) if is_light else QColor(148, 171, 184, 160)
                p.setPen(_voter_col)
                fm_v = self._fm_quote
                voter_text = fm_v.elidedText(
                    f"\U0001F465 {poll_voters}", Qt.ElideRight, bar_w)
                p.drawText(QRect(cx + 12, oy, bar_w, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, voter_text)

            dy += poll_h

        # vCard contact card(s)
        if type_label in ("vcard", "vcard_list"):
            vcard_data_str = msg.get("vcard_data") or ""
            is_light_vc = self.RECV_BG.lightness() > 128

            # Parse vcard entries: "name||phones;;name||phones"
            vcard_entries = []
            if vcard_data_str:
                for entry in vcard_data_str.split(";;"):
                    parts = entry.split("||", 1)
                    vc_name = parts[0].strip() if parts else ""
                    vc_phones = parts[1].strip() if len(parts) > 1 else ""
                    if vc_name or vc_phones:
                        vcard_entries.append((vc_name, vc_phones))
            if not vcard_entries:
                # Fallback to text_content for name
                fallback_name = msg.get("display_text") or msg.get("text_content") or "Contact"
                vcard_entries = [(fallback_name, "")]

            for vc_name, vc_phones in vcard_entries:
                vcard_h = 50
                vpath = QPainterPath()
                vpath.addRoundedRect(float(cx), float(dy), float(cw), float(vcard_h), 6, 6)
                p.fillPath(vpath, self.VCARD_BG)
                p.setPen(QColor(0, 0, 0, 15) if is_light_vc else QColor(255, 255, 255, 20))
                p.drawPath(vpath)
                # Contact avatar circle
                av_x, av_y, av_r = cx + 10, dy + 9, 32
                avpath = QPainterPath()
                avpath.addEllipse(float(av_x), float(av_y), float(av_r), float(av_r))
                p.fillPath(avpath, self.VCARD_CIRCLE)
                p.setFont(QFont("Segoe UI", 14))
                p.setPen(QColor(255, 255, 255))
                p.drawText(QRect(av_x, av_y, av_r, av_r), Qt.AlignCenter, "\U0001F464")
                # Contact name
                p.setFont(self.F_SENDER)
                p.setPen(self.TEXT_COL)
                p.drawText(QRect(cx + 50, dy + 6, cw - 60, 18),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_sender.elidedText(vc_name or "Contact", Qt.ElideRight, cw - 60))
                # Phone number(s) or "Shared Contact"
                p.setFont(self.F_QUOTE)
                p.setPen(self.VCARD_SUB)
                subtitle = vc_phones if vc_phones else "Shared Contact"
                p.drawText(QRect(cx + 50, dy + 26, cw - 60, 14),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._fm_quote.elidedText(subtitle, Qt.ElideRight, cw - 60))
                # Divider line at bottom
                p.setPen(QColor(0, 0, 0, 8) if is_light_vc else QColor(255, 255, 255, 15))
                p.drawLine(cx + 4, dy + vcard_h - 1, cx + cw - 4, dy + vcard_h - 1)
                dy += 54

        # Clickable URL info cards ABOVE text (WhatsApp-style: card then text below)
        if urls and row >= 0:
            # Parse link details from DB if available
            link_meta = {}
            ld_str = msg.get("link_details") or ""
            if ld_str:
                for entry in ld_str.split(";;"):
                    parts_ld = entry.split("||")
                    if len(parts_ld) >= 4:
                        _title = parts_ld[0].strip()
                        _url = parts_ld[1].strip()
                        _desc = parts_ld[2].strip()
                        _dom = parts_ld[3].strip()
                        if _url:
                            link_meta[_url] = (_title, _desc, _dom)

            # Get thumbnail for first link (if has_thumb and no media image drawn above)
            link_thumb = None
            if not media_drawn and msg.get("has_thumb"):
                link_thumb = self._get_thumb(msg.get("id", 0), msg.get("thumbnail_blob"))
                if link_thumb and not link_thumb.isNull():
                    # Scale to a reasonable preview size for link cards
                    link_thumb = link_thumb.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    link_thumb = None

            link_rects = []
            for i, url in enumerate(urls[:1]):
                meta = link_meta.get(url)
                # Fallback: if exact URL match fails, try matching by domain or partial URL
                if not meta and link_meta:
                    url_domain = _extract_domain(url)
                    url_lower = url.lower()
                    for ld_url, ld_meta in link_meta.items():
                        # Match if one URL starts with the other, or same domain
                        if (url_lower.startswith(ld_url.lower().rstrip("/"))
                                or ld_url.lower().rstrip("/").startswith(url_lower.rstrip("/"))
                                or _extract_domain(ld_url) == url_domain):
                            meta = ld_meta
                            break
                    # If still no match but there's exactly 1 link_meta entry, use it
                    if not meta and len(link_meta) == 1:
                        meta = next(iter(link_meta.values()))
                title = meta[0] if meta else ""
                desc = meta[1] if meta else ""
                domain = meta[2] if meta else _extract_domain(url)

                # Show thumbnail only on first link card
                show_thumb = i == 0 and link_thumb is not None
                thumb_w = link_thumb.width() + 6 if show_thumb else 0

                # Taller card if we have title; even taller with thumbnail
                card_h = 26
                if title:
                    card_h = 62 if show_thumb else 42
                cpath_url = QPainterPath()
                cpath_url.addRoundedRect(float(cx), float(dy), float(cw), float(card_h), 6, 6)
                # Better link card background - more visible
                _lbg = QColor(235, 240, 250, 200) if self.RECV_BG.lightness() > 128 else QColor(20, 35, 45, 220)
                p.fillPath(cpath_url, _lbg)
                # Left accent bar (like WhatsApp link previews)
                bar_link = QPainterPath()
                bar_link.addRoundedRect(float(cx), float(dy), 3.0, float(card_h), 2, 2)
                p.fillPath(bar_link, self.LINK_COL)
                # Subtle border
                _lborder = QColor(0, 120, 180, 30) if self.RECV_BG.lightness() > 128 else QColor(83, 189, 237, 40)
                p.setPen(_lborder)
                p.drawPath(cpath_url)

                # Draw thumbnail on right side
                if show_thumb:
                    th_x = cx + cw - link_thumb.width() - 4
                    th_y = dy + (card_h - link_thumb.height()) // 2
                    clip_th = QPainterPath()
                    clip_th.addRoundedRect(float(th_x), float(th_y),
                                           float(link_thumb.width()), float(link_thumb.height()), 4, 4)
                    p.setClipPath(clip_th)
                    p.drawPixmap(th_x, th_y, link_thumb)
                    p.setClipping(False)

                text_w = cw - 20 - thumb_w  # extra left margin for accent bar
                text_left = cx + 10  # offset for accent bar
                fm_link = self._fm_quote
                _lt = self.RECV_BG.lightness() > 128
                _link_domain_col = QColor(100, 115, 125) if _lt else QColor(148, 171, 184, 200)
                _link_title_col = QColor(2, 100, 160) if _lt else QColor(130, 210, 255)
                _link_desc_col = QColor(80, 95, 105) if _lt else QColor(170, 185, 195, 210)
                if title:
                    # Domain + title + description
                    p.setFont(self.F_QUOTE)
                    p.setPen(_link_domain_col)
                    p.drawText(QRect(text_left, dy + 3, text_w, 12),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               f"\u26D3 {domain}" if domain else "\u26D3 Link")
                    p.setFont(self.F_SENDER)
                    p.setPen(_link_title_col)
                    p.drawText(QRect(text_left, dy + 16, text_w, 16),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               self._fm_sender.elidedText(title, Qt.ElideRight, text_w))
                    if desc:
                        p.setFont(self.F_QUOTE)
                        p.setPen(_link_desc_col)
                        desc_y = dy + 33
                        desc_lines = 2 if card_h > 46 else 1
                        desc_h = 13 * desc_lines
                        p.drawText(QRect(text_left, desc_y, text_w, desc_h),
                                   Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                                   fm_link.elidedText(desc, Qt.ElideRight, text_w * desc_lines))
                else:
                    # Fallback: domain + URL
                    p.setFont(self.F_SENDER)
                    p.setPen(_link_title_col)
                    p.drawText(QRect(text_left, dy + 1, text_w, 13),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               f"\u26D3 {domain}" if domain else "\u26D3 Link")
                    p.setFont(self.F_QUOTE)
                    p.setPen(_link_desc_col)
                    p.drawText(QRect(text_left, dy + 13, text_w, 12),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               fm_link.elidedText(url, Qt.ElideMiddle, text_w))

                lr = QRect(cx, dy, cw, card_h)
                link_rects.append((lr, url))
                dy += card_h + 2
            self._link_rects[row] = link_rects

        # Prefix text with icon for special types
        if text and type_label in ("vcard_list", "unknown_14"):
            text = f"\U0001F465 Shared {text}"
        elif text and type_label in ("view_once_voice", "unknown_82"):
            text = f"\U0001F3A4 View-once voice note"
        elif type_label == "scheduled_event":
            se_data = msg.get("scheduled_event_data") or ""
            if se_data:
                parts_se = se_data.split("||")
                se_name = parts_se[0] if len(parts_se) > 0 else ""
                se_desc = parts_se[1] if len(parts_se) > 1 else ""
                se_loc = parts_se[2] if len(parts_se) > 2 else ""
                se_link = parts_se[3] if len(parts_se) > 3 else ""
                se_lines = [f"\U0001F4C5 {se_name}" if se_name else "\U0001F4C5 Event"]
                if se_desc:
                    se_lines.append(se_desc)
                if se_loc:
                    se_lines.append(f"\U0001F4CD {se_loc}")
                text = "\n".join(se_lines)
            else:
                text = f"\U0001F4C5 {text}" if text else "\U0001F4C5 Event"

        # Revoked messages — always show "deleted" text even when text_content is NULL
        has_poll = type_label in ("poll", "poll_vote") or msg.get("poll_options")
        if msg.get("is_revoked") and not has_poll:
            p.setFont(self.F_TEXT)
            p.setPen(self.REVOKE_COL)
            revoke_label = "\U0001F6AB This message was deleted"
            admin_name = msg.get("revoked_by_admin_name")
            _is_newsletter = type_label == "newsletter" or msg.get("message_type") == 64
            if admin_name:
                revoke_label = f"\U0001F6AB {admin_name} deleted this message"
            elif _is_newsletter:
                revoke_label = "\U0001F6AB This message was deleted"
            elif msg.get("from_me"):
                _op = self._owner_phone
                _on = self._owner_name
                if _op:
                    _ofmt = self._fmt_phone(_op)
                    _olbl = f"You (Owner: {_on}, {_ofmt})" if _on else f"You ({_ofmt})"
                else:
                    _olbl = "You"
                revoke_label = f"\U0001F6AB {_olbl} deleted this message"
            else:
                sender = msg.get("sender_name", "")
                if sender and sender != "Unknown":
                    revoke_label = f"\U0001F6AB {sender} deleted this message"
            fm_rev = self._fm_text
            rev_h = fm_rev.boundingRect(0, 0, cw, 1000, Qt.TextWordWrap, revoke_label).height() + 3
            p.drawText(QRect(cx, dy, cw, rev_h),
                       Qt.TextWordWrap | Qt.AlignLeft,
                       revoke_label)
            dy += rev_h
        # Text content (skip for vcard - shown in card, skip for poll - shown above card)
        elif text and type_label != "vcard" and not has_poll:
                # Check for inline @mentions to render with colors
                mention_map = self._parse_mention_map(msg)
                if mention_map:
                    self._paint_text_with_mentions(
                        p, cx, dy, cw, text_h, text,
                        mention_map, row, msg)
                elif self._has_url_cache.get(msg_id, bool(_URL_RE.search(text))):
                    # Render with blue URLs + WA markdown using QTextDocument
                    self._paint_text_with_urls(p, cx, dy, cw, text_h, text, row)
                elif any(c in _WA_MD_CHARS for c in text):
                    # WhatsApp markdown (*bold*, _italic_, ~strike~, `code`)
                    self._paint_text_with_markdown(
                        p, cx, dy, cw, text_h, text, row)
                else:
                    p.setPen(self.TEXT_COL)
                    p.drawText(QRect(cx, dy, cw, text_h),
                               Qt.TextWordWrap | Qt.AlignLeft, text)
                dy += text_h
        elif (type_label and not media_drawn and not msg.get("has_thumb")
              and type_label not in ("location", "live_location", "poll", "poll_vote",
                                     "vcard", "album")):
            # Type label for messages without text (button_msg, interactive, etc.)
            tl = type_label
            icon = TYPE_EMOJI.get(tl, "\u2709")
            if is_bot and not self._is_group:
                icon = "\U0001F916"
            display_label = _FRIENDLY_TYPE_LABELS.get(tl, tl.replace("_", " ").title())
            display_label = f"{icon} {display_label}"
            _type_col = QColor(100, 115, 125, 200) if self.RECV_BG.lightness() > 128 else QColor(180, 190, 195, 200)
            p.setFont(self.F_TYPE)
            p.setPen(_type_col)
            p.drawText(QRect(cx, dy, cw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       display_label)
            dy += 18

        # @Mention tag pills (only shown if text had no @phone patterns for inline rendering)
        mentions_str = msg.get("mentions_str") or ""
        has_inline_mentions = bool(text and _MENTION_RE.search(text) and mentions_str)
        if mentions_str and row >= 0 and not has_inline_mentions:
            mentions = []
            for m_entry in mentions_str.split(";;"):
                parts_m = m_entry.split("::")
                if len(parts_m) >= 2:
                    m_name = parts_m[0].strip()
                    try:
                        m_cid = int(parts_m[1].strip())
                    except (ValueError, TypeError):
                        m_cid = 0
                    raw_dn = parts_m[4].strip() if len(parts_m) >= 5 else ""
                    if m_name == "Unknown" and raw_dn:
                        m_name = raw_dn
                    if m_name:
                        mentions.append((m_name, m_cid))
            if mentions:
                p.setFont(self.F_QUOTE)
                fm_m = self._fm_quote
                mx = cx
                for m_name, m_cid in mentions[:6]:
                    tag = f"@{m_name}"
                    tw = fm_m.horizontalAdvance(tag) + 10
                    if mx + tw > cx + cw:
                        break
                    tag_rect = QRect(mx, dy, tw, 16)
                    tp = QPainterPath()
                    tp.addRoundedRect(float(mx), float(dy), float(tw), 16.0, 8, 8)
                    p.fillPath(tp, QColor(0, 188, 212, 40))
                    p.setPen(self.MENTION_COL)
                    p.drawText(tag_rect, Qt.AlignCenter, tag)
                    if m_cid > 0:
                        if row not in self._link_rects:
                            self._link_rects[row] = []
                        self._link_rects[row].append((tag_rect, f"mention://{m_cid}"))
                    mx += tw + 4
                dy += 20

        # Call record card
        if call_card_h > 0:
            call_result = msg.get("call_result_label", "")
            is_video = msg.get("call_is_video", False)
            is_group_call = msg.get("call_is_group", False)
            duration = msg.get("call_duration")

            _lt_call = self.RECV_BG.lightness() > 128
            if _lt_call:
                call_bg = QColor(230, 245, 235, 200) if not is_video else QColor(230, 235, 250, 200)
            else:
                call_bg = QColor(20, 40, 35, 180) if not is_video else QColor(20, 30, 50, 180)
            cpath_call = QPainterPath()
            cpath_call.addRoundedRect(float(cx), float(dy), float(cw), float(call_card_h - 4), 6, 6)
            p.fillPath(cpath_call, call_bg)

            # Call icon
            call_icon = "\U0001F4F9" if is_video else "\U0001F4DE"
            if is_group_call:
                call_icon = "\U0001F465 " + call_icon
            p.setFont(QFont("Segoe UI", 14))
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 8, dy + 4, 30, 30), Qt.AlignCenter, call_icon)

            # Call type + result
            call_type = "Video call" if is_video else "Voice call"
            if is_group_call:
                call_type = "Group " + call_type.lower()
            p.setFont(self.F_SENDER)
            p.setPen(self.TEXT_COL)
            p.drawText(QRect(cx + 42, dy + 4, cw - 52, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, call_type)

            # Duration + result
            dur_parts = []
            if duration and duration > 0:
                mins, secs = divmod(duration, 60)
                if mins > 0:
                    dur_parts.append(f"{mins}m {secs}s")
                else:
                    dur_parts.append(f"{secs}s")
            if call_result:
                dur_parts.append(call_result)
            dur_text = "  \u2022  ".join(dur_parts) if dur_parts else ""
            result_col = QColor(100, 220, 100) if call_result == "answered" else QColor(255, 160, 100)
            p.setFont(self.F_QUOTE)
            p.setPen(result_col)
            p.drawText(QRect(cx + 42, dy + 22, cw - 52, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, dur_text)

            # Call participants
            participants = msg.get("call_participants")
            if participants:
                _lt_call = self.RECV_BG.lightness() > 128
                p.setFont(self.F_QUOTE)
                p.setPen(QColor(100, 115, 130) if _lt_call else QColor(148, 171, 184))
                fm_p = self._fm_quote
                p_text = fm_p.elidedText(
                    f"\U0001F465 {participants}", Qt.ElideRight, cw - 52)
                p.drawText(QRect(cx + 42, dy + 36, cw - 52, 14),
                           Qt.AlignLeft | Qt.AlignVCenter, p_text)
            dy += call_card_h

        # Meta line: starred | edited | time | delivery/read ticks
        # When two meta lines exist, the bubble is taller, so base position
        # is one META_H from the bottom for the second line
        _has_meta2 = False
        if from_me:
            _status = msg.get("status", 0)
            _read_m2 = msg.get("first_read_ts") or 0
            _del_m2 = msg.get("first_delivered_ts") or 0
            _srv_m2 = msg.get("receipt_server_timestamp") or 0
            if (_read_m2 > 0 or _del_m2 > 0
                    or (_status >= 5 and _srv_m2 > 0)):
                _has_meta2 = True
        else:
            _recv = msg.get("received_timestamp")
            _ts_check = msg.get("timestamp")
            if _recv and _recv > 0 and _ts_check and _recv != _ts_check:
                _has_meta2 = True
        meta_y = by + bh - self.PAD - self.META_H + 1
        parts = []
        if msg.get("is_starred"):
            parts.append("\u2B50")
        if msg.get("is_view_once"):
            parts.append("\U0001F441")

        from app.config import format_timestamp
        ts = msg.get("timestamp")
        ts_str = format_timestamp(ts, "full")  # forensic: 2025-11-27 14:30:05.123

        if ts_str:
            parts.append(ts_str)

        meta = "  ".join(parts)

        # Second line: delivery/read forensic timestamps (full ms precision)
        meta2 = ""
        if from_me and ts:
            read_ts_val = msg.get("first_read_ts")
            delivered_ts = msg.get("first_delivered_ts")
            status = msg.get("status", 0)
            server_ts = msg.get("receipt_server_timestamp")
            # Treat -1, 0, None as invalid timestamps
            if server_ts and server_ts <= 0:
                server_ts = None
            if read_ts_val and read_ts_val > 0:
                meta2 = f"Read: {format_timestamp(read_ts_val, 'full')}"
            elif status >= 6 and server_ts:
                # Status says read but no receipt row — use server ts
                meta2 = f"Read: {format_timestamp(server_ts, 'full')}"
            elif delivered_ts and delivered_ts > 0:
                meta2 = f"Delivered: {format_timestamp(delivered_ts, 'full')}"
            elif status >= 5 and server_ts:
                meta2 = f"Delivered: {format_timestamp(server_ts, 'full')}"
        else:
            recv_ts = msg.get("received_timestamp")
            if recv_ts and ts and recv_ts != ts:
                meta2 = f"Received: {format_timestamp(recv_ts, 'full')}"

        p.setFont(self.F_META)

        # "Edited" indicator — prominent pencil badge with edit timestamp, clickable
        _is_edited = msg.get("is_edited") and not msg.get("is_bot_message")
        _is_edited_and_deleted = _is_edited and msg.get("is_revoked")
        _edit_badge_w = 0
        if _is_edited:
            _edit_ts = msg.get("last_edit_timestamp")
            _edit_label = "\u270E edited"
            if _edit_ts and _edit_ts > 0:
                _edit_label += f" {format_timestamp(_edit_ts, 'full')}"
            _light_bg = self.RECV_BG.lightness() > 128
            _edit_col = QColor("#e65100") if _light_bg else QColor("#ffab40")

        # If we have a second line (delivery/read), shift primary meta up
        meta_h = self.META_H
        _extra_meta_lines = 0
        if meta2:
            _extra_meta_lines += 1
        if _is_edited:
            _extra_meta_lines += 1
        if _extra_meta_lines:
            meta_y -= meta_h * _extra_meta_lines  # shift up for extra lines

        if from_me:
            status = msg.get("status", 0)
            if status >= 13:
                tick, tc = " \u2713\u2713", self.TICK_BLUE      # read/played
            elif status >= 6:
                tick, tc = " \u2713\u2713", self.TICK_BLUE      # read
            elif status == 5:
                tick, tc = " \u2713\u2713", self.TICK_GRAY      # delivered
            elif status == 4:
                tick, tc = " \u2713", self.TICK_GRAY             # sent to server
            else:
                tick, tc = " \U0001F552", self.TICK_GRAY         # pending

            p.setPen(self.TIME_COL)
            p.drawText(QRect(cx, meta_y, cw - 26, meta_h),
                       Qt.AlignRight | Qt.AlignVCenter, meta)
            p.setPen(tc)
            p.drawText(QRect(cx + cw - 24, meta_y, 24, meta_h),
                       Qt.AlignRight | Qt.AlignVCenter, tick)
        else:
            p.setPen(self.TIME_COL)
            p.drawText(QRect(cx, meta_y, cw, meta_h),
                       Qt.AlignRight | Qt.AlignVCenter, meta)

        _next_meta_y = meta_y + meta_h

        # Second meta line: delivery/read timestamp
        if meta2:
            p.setPen(QColor(self.TIME_COL.red(), self.TIME_COL.green(),
                            self.TIME_COL.blue(), 180))
            p.drawText(QRect(cx, _next_meta_y, cw, meta_h),
                       Qt.AlignRight | Qt.AlignVCenter, meta2)
            _next_meta_y += meta_h

        # Edit indicator — clickable pill badge: "✎ Edited · tap for history"
        # If also deleted: "✎ Edited → 🚫 Deleted · tap"
        if _is_edited:
            p.setFont(self.F_FWD)
            _light_bg = self.RECV_BG.lightness() > 128
            if _is_edited_and_deleted:
                _pill_bg = QColor("#ffebee") if _light_bg else QColor(211, 47, 47, 50)
                _pill_border = QColor("#ef9a9a") if _light_bg else QColor("#ef5350")
                _pill_text_col = QColor("#c62828") if _light_bg else QColor("#ef5350")
                _pill_label = "\u270E Edited \u2192 \U0001F6AB Deleted \u00B7 tap"
            else:
                _pill_bg = QColor("#fff3e0") if _light_bg else QColor(230, 81, 0, 50)
                _pill_border = QColor("#ffcc80") if _light_bg else QColor("#ff9800")
                _pill_text_col = QColor("#e65100") if _light_bg else QColor("#ffab40")
                _pill_label = "\u270E Edited \u00B7 tap for history"
            _pill_fm = self._fm_fwd
            _pill_tw = _pill_fm.horizontalAdvance(_pill_label) + 16
            _pill_h = 16
            _pill_x = cx + cw - _pill_tw - 2
            _pill_y = _next_meta_y + 1
            _pill_rect = QRect(_pill_x, _pill_y, _pill_tw, _pill_h)
            # Draw pill background
            _pill_path = QPainterPath()
            _pill_path.addRoundedRect(float(_pill_x), float(_pill_y),
                                      float(_pill_tw), float(_pill_h), 8, 8)
            p.fillPath(_pill_path, _pill_bg)
            # Draw pill border
            p.setPen(QPen(_pill_border, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(_pill_rect, 8, 8)
            # Draw pill text
            p.setPen(_pill_text_col)
            p.drawText(_pill_rect, Qt.AlignCenter, _pill_label)
            if row >= 0:
                self._edit_rects[row] = (_pill_rect, msg.get("id", 0))

        # Reply count badge — "N replies ▸" below meta, clickable
        _rcount = msg.get("reply_count") or 0
        if _rcount > 0:
            _rc_y = meta_y + self.META_H + (self.META_H if meta2 else 0) + (self.META_H if _is_edited else 0) + 2
            _rc_text = f"\u21B3 {_rcount} {'reply' if _rcount == 1 else 'replies'}"
            p.setFont(self.F_FWD)
            _rc_col = QColor("#00897b") if self.RECV_BG.lightness() > 128 else QColor("#4dd0c8")
            p.setPen(_rc_col)
            _rc_rect = QRect(cx, _rc_y, cw, 14)
            p.drawText(_rc_rect, Qt.AlignLeft | Qt.AlignVCenter, _rc_text)
            if row >= 0:
                self._reply_count_rects[row] = _rc_rect

        # Restore painter (remove bubble clipping) before drawing reactions
        # which intentionally overflow below the bubble
        p.restore()

        # Reactions pills below bubble (WhatsApp mobile style)
        if reactions_h > 0:
            reactions = msg.get("reactions_str", "")
            count = msg.get("reaction_count", 0)
            if reactions:
                # Parse reactions_detail for grouped emoji pills
                # Format: "emoji:PersonName;;emoji:PersonName;;..."
                detail_str = msg.get("reactions_detail") or ""
                from collections import OrderedDict
                emoji_groups: OrderedDict[str, list[str]] = OrderedDict()
                if detail_str:
                    for entry in detail_str.split(";;"):
                        if ":" in entry:
                            emoji_part, name_part = entry.split(":", 1)
                            emoji_part = emoji_part.strip()
                            name_part = name_part.strip()
                            if emoji_part:
                                if emoji_part not in emoji_groups:
                                    emoji_groups[emoji_part] = []
                                emoji_groups[emoji_part].append(name_part)
                else:
                    # Fallback: just count unique emoji
                    emoji_list = list(reactions)
                    for e in emoji_list:
                        if e not in emoji_groups:
                            emoji_groups[e] = []
                        emoji_groups[e].append("")

                # Draw individual pills for each emoji type (WhatsApp mobile style)
                pill_h = 24
                pill_y = by + bh - 4  # overlap bottom of bubble
                pill_x = bx + bw - 6  # start from right side
                f_emoji = QFont("Segoe UI Emoji", 12)
                f_count = QFont("Segoe UI", 9)
                fm_count = QFontMetrics(f_count)

                # Calculate pill widths right-to-left
                pills = []
                for emoji, names in list(emoji_groups.items())[:5]:
                    n = len(names)
                    emoji_w = 22
                    count_w = fm_count.horizontalAdvance(str(n)) + 4 if n > 1 else 0
                    pw = emoji_w + count_w + 12
                    pills.append((emoji, n, pw, names))

                # Draw pills right-to-left
                total_w = sum(pw + 3 for _, _, pw, _ in pills) - 3
                pill_x = bx + bw - total_w - 6
                # Store full reaction area for click detection
                if row >= 0:
                    msg_id = msg.get("id", 0)
                    if msg_id:
                        self._reaction_rects[row] = (
                            QRect(int(pill_x), int(pill_y), int(total_w + 6), int(pill_h)),
                            msg_id)
                _lt_rxn = self.RECV_BG.lightness() > 128
                # Pill must contrast against BOTH sent and received bubble backgrounds
                _pill_bg = QColor(255, 255, 255, 248) if _lt_rxn else QColor(60, 75, 88, 250)
                _pill_border = QColor(0, 0, 0, 60) if _lt_rxn else QColor(255, 255, 255, 90)
                _pill_count_col = QColor(50, 60, 70) if _lt_rxn else QColor(225, 235, 240)
                for emoji, n, pw, names in pills:
                    rpath = QPainterPath()
                    rpath.addRoundedRect(float(pill_x), float(pill_y),
                                         float(pw), float(pill_h), 12, 12)
                    # Pill background with subtle border
                    p.fillPath(rpath, _pill_bg)
                    p.setPen(_pill_border)
                    p.drawPath(rpath)
                    # Emoji (larger, centered vertically)
                    p.setFont(f_emoji)
                    p.setPen(self.TEXT_COL)
                    p.drawText(QRect(pill_x + 4, pill_y + 1, 22, pill_h - 2),
                               Qt.AlignCenter, emoji)
                    # Count (if > 1)
                    if n > 1:
                        p.setFont(f_count)
                        p.setPen(_pill_count_col)
                        p.drawText(QRect(pill_x + 26, pill_y, pw - 30, pill_h),
                                   Qt.AlignLeft | Qt.AlignVCenter, str(n))
                    pill_x += pw + 3

    # ---- inline @mention helpers ----

    def _parse_mention_map(self, msg: dict) -> dict[str, tuple[str, int]]:
        """Parse mentions_str into lookup maps for @mention matching.
        Format: 'name::contact_id::phone_number::lid_number::display_name;;...'
        Returns dict mapping identifier -> (display_name, contact_id).
        Keys include phone numbers, LID numbers, and positional fallbacks."""
        mentions_str = msg.get("mentions_str") or ""
        if not mentions_str:
            return {}
        result: dict[str, tuple[str, int]] = {}
        ordered: list[tuple[str, int]] = []
        for entry in mentions_str.split(";;"):
            parts = entry.split("::")
            if len(parts) >= 2:
                name = parts[0].strip()
                try:
                    cid = int(parts[1].strip())
                except (ValueError, TypeError):
                    cid = 0
                phone = parts[2].strip() if len(parts) >= 3 else ""
                lid = parts[3].strip() if len(parts) >= 4 else ""
                raw_display = parts[4].strip() if len(parts) >= 5 else ""
                # Use raw display_name from mention table if name is 'Unknown'
                if name == "Unknown" and raw_display:
                    name = raw_display
                if name:
                    # Map phone, LID, and raw display_name for matching
                    if phone:
                        result[phone] = (name, cid)
                    if lid:
                        result[lid] = (name, cid)
                    # Also map the raw display_name digits (e.g. "15551234567")
                    if raw_display and raw_display not in result:
                        result[raw_display] = (name, cid)
                    ordered.append((name, cid))
        # Always include positional fallback for unmatched @numbers
        for i, (name, cid) in enumerate(ordered):
            result[f"__pos_{i}"] = (name, cid)
        return result

    def _paint_text_with_urls(self, p: QPainter, cx: int, dy: int,
                              cw: int, text_h: int, text: str, row: int):
        """Paint text with URLs highlighted in blue and clickable."""
        import html as _html
        from PySide6.QtGui import QTextDocument

        link_col_hex = self.LINK_COL.name()
        text_col_hex = self.TEXT_COL.name()

        # Build HTML: split text by URL patterns, replace with blue anchor tags
        # Also apply WhatsApp markdown (*bold*, _italic_, ~strike~, `code`)
        parts: list[str] = []
        last_end = 0
        for match in _URL_RE.finditer(text):
            if match.start() > last_end:
                seg = _html.escape(text[last_end:match.start()])
                seg = _wa_markdown_to_html(seg)
                parts.append(seg.replace("\n", "<br>"))
            url = match.group(0)
            esc_url = _html.escape(url)
            parts.append(
                f'<a href="{esc_url}" style="color:{link_col_hex};'
                f'text-decoration:none;">{esc_url}</a>')
            last_end = match.end()
        if last_end < len(text):
            seg = _html.escape(text[last_end:])
            seg = _wa_markdown_to_html(seg)
            parts.append(seg.replace("\n", "<br>"))

        processed = "".join(parts)

        doc = QTextDocument()
        doc.setDefaultFont(self.F_TEXT)
        doc.setTextWidth(cw)
        doc.setHtml(f'<div style="color:{text_col_hex};">{processed}</div>')

        # Cache for click detection (reuses _mention_docs infrastructure)
        self._mention_docs[row] = (doc, cx, dy)
        if len(self._mention_docs) > 300:
            oldest = next(iter(self._mention_docs))
            del self._mention_docs[oldest]

        p.save()
        p.translate(cx, dy)
        doc.drawContents(p, QRectF(0, 0, cw, text_h))
        p.restore()

    def _paint_text_with_markdown(self, p: QPainter, cx: int, dy: int,
                                   cw: int, text_h: int, text: str, row: int):
        """Paint text with WhatsApp markdown formatting (*bold*, _italic_, ~strike~, `code`)."""
        import html as _html
        from PySide6.QtGui import QTextDocument

        text_col_hex = self.TEXT_COL.name()
        escaped = _html.escape(text).replace("\n", "<br>")
        processed = _wa_markdown_to_html(escaped)

        doc = QTextDocument()
        doc.setDefaultFont(self.F_TEXT)
        doc.setTextWidth(cw)
        doc.setHtml(f'<div style="color:{text_col_hex};">{processed}</div>')

        p.save()
        p.translate(cx, dy)
        doc.drawContents(p, QRectF(0, 0, cw, text_h))
        p.restore()

    def _paint_text_with_mentions(self, p: QPainter, cx: int, dy: int,
                                   cw: int, text_h: int, text: str,
                                   mention_map: dict, row: int, msg: dict):
        """Paint text with inline @mentions highlighted and clickable.
        Uses QTextDocument for HTML rendering with <a> anchors for mentions.
        mention_map: phone -> (display_name, contact_id)"""
        import html as _html
        from PySide6.QtGui import QTextDocument

        mention_col_hex = self.MENTION_COL.name()
        text_col_hex = self.TEXT_COL.name()

        # Build HTML: split text by @number patterns, replace with styled anchor tags
        # mention_map has phone->contact, lid->contact, and __pos_N->contact entries
        parts: list[str] = []
        last_end = 0
        positional = [v for k, v in mention_map.items() if k.startswith("__pos_")]
        pos_idx = 0

        for match in _MENTION_RE.finditer(text):
            # Append escaped text before this mention (with WA markdown)
            if match.start() > last_end:
                seg = _html.escape(text[last_end:match.start()])
                seg = _wa_markdown_to_html(seg)
                parts.append(seg.replace("\n", "<br>"))

            num = match.group(1)  # The number after @
            matched_name = None
            matched_cid = 0
            if num in mention_map and not num.startswith("__"):
                # Direct match (phone or LID or display_name)
                matched_name, matched_cid = mention_map[num]
                pos_idx += 1  # Advance positional counter too
            elif pos_idx < len(positional):
                # Fallback: match by order of appearance
                matched_name, matched_cid = positional[pos_idx]
                pos_idx += 1

            if matched_name:
                # If name is still 'Unknown', use the number as display
                if matched_name == "Unknown":
                    matched_name = num
                esc = _html.escape(f"@{matched_name}")
                if matched_cid > 0:
                    # Resolved mention - clickable, colored
                    parts.append(
                        f'<a href="mention://{matched_cid}" style="color:{mention_col_hex};'
                        f'font-weight:bold;text-decoration:none;">{esc}</a>')
                else:
                    # Unresolved mention (cid=0) - styled but not clickable
                    parts.append(
                        f'<span style="color:{mention_col_hex};'
                        f'font-weight:bold;">{esc}</span>')
            else:
                # No match at all - show styled @number (still visible as mention)
                esc = _html.escape(f"@{num}")
                parts.append(
                    f'<span style="color:{mention_col_hex};'
                    f'font-style:italic;">{esc}</span>')

            last_end = match.end()

        # Append remaining text after last mention (with WA markdown)
        if last_end < len(text):
            seg = _html.escape(text[last_end:])
            seg = _wa_markdown_to_html(seg)
            parts.append(seg.replace("\n", "<br>"))

        processed = "".join(parts)

        # Create QTextDocument with rich HTML
        doc = QTextDocument()
        doc.setDefaultFont(self.F_TEXT)
        doc.setTextWidth(cw)
        doc.setHtml(f'<div style="color:{text_col_hex};">{processed}</div>')

        # Cache doc + offset for click detection in editorEvent
        self._mention_docs[row] = (doc, cx, dy)
        if len(self._mention_docs) > 300:
            oldest = next(iter(self._mention_docs))
            del self._mention_docs[oldest]

        # Paint the rich text
        p.save()
        p.translate(cx, dy)
        doc.drawContents(p, QRectF(0, 0, cw, text_h))
        p.restore()

    # ---- thumbnail cache ----

    def _get_thumb(self, msg_id: int, blob: bytes | None) -> QPixmap | None:
        if msg_id in self._thumb_cache:
            return self._thumb_cache[msg_id]
        if not blob:
            self._thumb_cache[msg_id] = None  # cache misses to avoid repeated parsing
            return None
        pxm = QPixmap()
        pxm.loadFromData(blob)
        if pxm.isNull():
            self._thumb_cache[msg_id] = None  # cache decode failures too
            return None
        self._thumb_cache[msg_id] = pxm
        if len(self._thumb_cache) > 800:
            keys = list(self._thumb_cache.keys())[:300]
            for k in keys:
                del self._thumb_cache[k]
        return pxm
