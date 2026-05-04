"""
Image Similarity Search page — perceptual image matching across
the case's media files.

Uses pHash + dHash + edge-map hashing to find exact duplicates,
near-duplicates, and template matches (same app layout, different
data — e.g. payment-app screenshots, ID cards, etc.).
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.services.database import Database
from app.services.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier display config
# ---------------------------------------------------------------------------
TIER_LABELS = {
    1: "Exact / Near-Exact",
    2: "Near-Duplicate",
    3: "Template Match",
}
TIER_COLORS = {
    1: "#4caf50", # green
    2: "#ff9800", # orange
    3: "#2196f3", # blue
}
TIER_BG_COLORS = {
    1: "#1b3a1b",
    2: "#3a2e1b",
    3: "#1b2a3a",
}

# Accent colors for conversation name (theme-aware)
ACCENT_LIGHT = "#00897b"
ACCENT_DARK = "#00bcd4"


def _accent_color() -> str:
    """Return the accent color for the current theme."""
    try:
        return ACCENT_LIGHT if ThemeManager.get().is_light else ACCENT_DARK
    except Exception:
        return ACCENT_DARK


def _is_light() -> bool:
    try:
        return ThemeManager.get().is_light
    except Exception:
        return False


def _muted_color() -> str:
    """Return a muted text color appropriate for the theme."""
    return "#777" if _is_light() else "#aaa"


def _card_hover_bg() -> str:
    """A subtle hover background."""
    return "#f0f0f0" if _is_light() else "#2a2a2a"


# ---------------------------------------------------------------------------
# Background worker for index building
# ---------------------------------------------------------------------------

class IndexWorker(QThread):
    """Build the perceptual hash index in a background thread."""

    progress = Signal(int, int)       # current, total
    finished = Signal(int, str)       # count_indexed, error_msg (empty on success)

    _instance: IndexWorker | None = None
    _running: bool = False

    @classmethod
    def get_or_create(cls, parent=None) -> IndexWorker:
        """Singleton — reuse the same worker across pages."""
        if cls._instance is None or not cls._instance.isRunning():
            cls._instance = IndexWorker(parent)
        return cls._instance

    @classmethod
    def is_running(cls) -> bool:
        return cls._instance is not None and cls._instance.isRunning()

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            from app.services.image_similarity import ImageSimilarityEngine

            db = Database.get()
            engine = ImageSimilarityEngine(db)
            count = engine.build_index(progress_callback=self._on_progress)
            self.finished.emit(count, "")
        except ImportError as exc:
            self.finished.emit(0, str(exc))
        except Exception as exc:
            logger.exception("Index build failed")
            self.finished.emit(0, str(exc))

    def _on_progress(self, current: int, total: int):
        self.progress.emit(current, total)


class ExpandWorker(QThread):
    """Background flood-fill expansion."""

    finished = Signal(list, str)

    def __init__(self, seed_results, query_msg_id, top_k, parent=None):
        super().__init__(parent)
        self._seed = seed_results
        self._qid = query_msg_id
        self._top_k = top_k

    def run(self):
        try:
            from app.services.image_similarity import ImageSimilarityEngine

            db = Database.get()
            engine = ImageSimilarityEngine(db)
            results = engine.expand_search(
                self._seed,
                original_query_id=self._qid if self._qid else None,
                max_rounds=3,
                top_k=self._top_k,
            )
            self.finished.emit(results, "")
        except Exception as e:
            self.finished.emit([], str(e))


class SearchWorker(QThread):
    """Run similarity search in a background thread.

    mode = 'exact'  → SHA-256 file_hash match (forensically strongest; shows every
                      share of the same bytes, across all chats / forwards)
    mode = 'visual' → perceptual hash (pHash + dHash + edge) — catches resizes,
                      recompressions, template matches
    """

    finished = Signal(list, str)  # results, error_msg

    def __init__(self, message_id: int = 0, image_path: str = "",
                 top_k: int = 50, mode: str = "exact", parent=None):
        super().__init__(parent)
        self._message_id = message_id
        self._image_path = image_path
        self._top_k = top_k
        self._mode = mode

    def run(self):
        try:
            from app.services.image_similarity import ImageSimilarityEngine

            db = Database.get()
            engine = ImageSimilarityEngine(db)

            # Diagnostic logging — when visual search returns
            # zero results despite the file being indexed, this
            # log line reveals which branch ran and with what
            # inputs.  Useful when the image's phash sits in
            # ``image_hash`` at Hamming distance 0 from the file
            # on disk yet the search still misses.
            logger.info(
                "SearchWorker: mode=%s message_id=%s image_path=%s top_k=%s",
                self._mode, self._message_id,
                (self._image_path or "")[:160], self._top_k,
            )

            if self._mode == "exact":
                if self._message_id:
                    results = engine.find_exact_duplicates_by_msg_id(self._message_id)
                elif self._image_path:
                    results = engine.find_exact_duplicates_by_path(self._image_path)
                else:
                    results = []
            else:
                if self._message_id:
                    results = engine.find_similar(self._message_id, top_k=self._top_k)
                elif self._image_path:
                    results = engine.find_similar_by_path(self._image_path, top_k=self._top_k)
                else:
                    results = []

            logger.info("SearchWorker: returned %d result(s)", len(results))
            self.finished.emit(results, "")
        except ImportError as exc:
            logger.warning("SearchWorker import error: %s", exc)
            self.finished.emit([], str(exc))
        except Exception as exc:
            logger.exception("Search failed")
            self.finished.emit([], str(exc))


# ---------------------------------------------------------------------------
# Result thumbnail card widget
# ---------------------------------------------------------------------------

class ResultCard(QFrame):
    """Single result thumbnail with tier badge, conversation context, distances, and file path."""

    clicked = Signal(dict)                # result dict — single click
    double_clicked = Signal(int, int)     # conversation_id, message_id

    CARD_SIZE = QSize(180, 250)
    THUMB_SIZE = QSize(164, 120)

    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self._result = result
        self._conv_id = result.get("conversation_id", 0)
        self._msg_id = result.get("message_id", 0)
        self._selected = False
        self._setup_ui()

    def _setup_ui(self):
        """Layout, top→bottom, no overlap:
           ┌────────────────────────┐
           │ [thumbnail 164x120]    │
           │ ─────────────────────  │
           │ [TIER BADGE]  [Dir]    │
           │ Conversation name      │
           │ Sender · Date          │
           │ IMG-YYYYMMDD-WAnnn.jpg │
           └────────────────────────┘"""
        self.setFixedSize(self.CARD_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style(hovered=False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 1. Thumbnail
        thumb_label = QLabel()
        thumb_label.setFixedSize(self.THUMB_SIZE)
        thumb_label.setAlignment(Qt.AlignCenter)
        thumb_label.setStyleSheet(
            "background: palette(window); border-radius: 4px; border: none;"
        )
        pixmap = self._load_thumbnail()
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(self.THUMB_SIZE,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb_label.setPixmap(scaled)
        else:
            thumb_label.setText("No Preview")
            thumb_label.setStyleSheet(
                "background: palette(window); border-radius: 4px;"
                " color: palette(mid); font-size: 10px; border: none;"
            )
        layout.addWidget(thumb_label)

        r = self._result
        tier = r.get("tier", 0)
        tier_color = TIER_COLORS.get(tier, "#888")
        is_exact = r.get("match_mode") == "exact" or tier == 0

        # 2. Badge row (tier + direction + orphan flag)
        badge_row = QHBoxLayout()
        badge_row.setSpacing(4)
        badge_row.setContentsMargins(0, 0, 0, 0)
        is_orphan = bool(r.get("is_orphan"))
        # ORPHAN badge takes the slot normally used for tier \u2014 for an
        # orphan, the message is gone, so "EXACT" alone is misleading.
        # Clear amber colour matches the orphaned-media page palette.
        if is_orphan:
            tier_label = QLabel("ORPHAN")
            tier_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
            tier_label.setAlignment(Qt.AlignCenter)
            tier_label.setFixedHeight(18)
            tier_label.setStyleSheet(
                "background: #e65100; color: #fff;"
                " border-radius: 9px; padding: 1px 8px; border: none;"
            )
            tier_label.setToolTip(
                "File on disk has the same SHA-256 as the query, but its "
                "chat record is gone (cleared chat / reinstall / deleted "
                "conversation).  Forensically: this file existed on the "
                "device at some point."
            )
        else:
            badge_text = "EXACT" if is_exact else TIER_LABELS.get(tier, f"T{tier}")
            tier_label = QLabel(badge_text)
            tier_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
            tier_label.setAlignment(Qt.AlignCenter)
            tier_label.setFixedHeight(18)
            tier_label.setStyleSheet(
                f"background: {'#4caf50' if is_exact else tier_color}; color: #fff;"
                f" border-radius: 9px; padding: 1px 8px; border: none;"
            )
        badge_row.addWidget(tier_label)

        from_me = bool(r.get("from_me"))
        if not is_orphan and (r.get("match_mode") == "exact" or from_me):
            dir_lbl = QLabel("\u2191 Sent" if from_me else "\u2193 Recv")
            dir_lbl.setFont(QFont("Segoe UI", 7, QFont.Bold))
            dir_lbl.setFixedHeight(18)
            dir_lbl.setStyleSheet(
                f"color: {'#4caf50' if from_me else '#2196f3'}; border: none;"
                f" padding: 1px 4px;"
            )
            badge_row.addWidget(dir_lbl)
        badge_row.addStretch()
        layout.addLayout(badge_row)

        # 3. Conversation name (bold, 1 line, clipped)
        conv_name = r.get("conv_name", "")
        if conv_name:
            display_name = conv_name if len(conv_name) <= 24 else conv_name[:22] + "\u2026"
            conv_label = QLabel(display_name)
            conv_label.setFont(QFont("Segoe UI", 9, QFont.Bold))
            conv_label.setStyleSheet(f"color: {_accent_color()}; border: none;")
            conv_label.setToolTip(conv_name)
            conv_label.setFixedHeight(15)
            layout.addWidget(conv_label)

        # 4. Sender · Date (one line, muted)
        sender_name = r.get("sender_name", "")
        ts = r.get("timestamp", 0)
        date_str = ""
        if ts:
            try:
                date_str = format_timestamp(ts, '%d %b %Y')
            except Exception:
                pass
        sub_parts = []
        if sender_name:
            sub_parts.append(sender_name[:18] + ("\u2026" if len(sender_name) > 18 else ""))
        if date_str:
            sub_parts.append(date_str)
        if sub_parts:
            sub_label = QLabel(" \u00B7 ".join(sub_parts))
            sub_label.setFont(QFont("Segoe UI", 8))
            sub_label.setStyleSheet(f"color: {_muted_color()}; border: none;")
            sub_label.setToolTip(sender_name)
            sub_label.setFixedHeight(13)
            layout.addWidget(sub_label)

        # 5. Filename (small, clipped)
        fpath = r.get("file_path", "")
        if fpath:
            fname = os.path.basename(fpath)
            display_fname = fname if len(fname) <= 26 else fname[:23] + "\u2026"
            fname_label = QLabel(display_fname)
            fname_label.setFont(QFont("Consolas", 7))
            fname_label.setStyleSheet("color: palette(text); border: none; opacity: 0.85;")
            fname_label.setToolTip(fpath)
            fname_label.setFixedHeight(12)
            layout.addWidget(fname_label)

        # 6. Distances — only in visual (non-exact) mode
        if not is_exact:
            dist_text = (f"p:{r.get('phash_dist', '?')}  d:{r.get('dhash_dist', '?')}"
                         f"  e:{r.get('edge_dist', '?')}")
            dist_label = QLabel(dist_text)
            dist_label.setFont(QFont("Consolas", 7))
            dist_label.setStyleSheet("color: palette(mid); border: none;")
            dist_label.setFixedHeight(12)
            layout.addWidget(dist_label)

        layout.addStretch()

    def _apply_style(self, hovered: bool = False, selected: bool = False):
        tier = self._result.get("tier", 1)
        tier_color = TIER_COLORS.get(tier, "#888")
        if selected:
            border = f"2px solid {tier_color}"
        elif hovered:
            border = f"2px solid {tier_color}88"
        else:
            border = f"1px solid {tier_color}44"

        self.setStyleSheet(f"""
            ResultCard {{
                background: palette(base);
                border: {border};
                border-radius: 6px;
            }}
        """)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style(selected=selected)

    def enterEvent(self, event):
        if not self._selected:
            self._apply_style(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_style(selected=self._selected)
        super().leaveEvent(event)

    def _load_thumbnail(self) -> QPixmap | None:
        """Prefer the full disk file over the small msgstore
        ``thumbnail_blob`` so result cards for the same image
        always show a crisp preview — falling back to the
        embedded thumb only when the disk file is missing."""
        fpath = self._result.get("file_path", "")
        if fpath and os.path.isfile(fpath):
            pm = QPixmap(fpath)
            if not pm.isNull():
                return pm

        thumb_b64 = self._result.get("thumb", "")
        if thumb_b64:
            try:
                data = base64.b64decode(thumb_b64)
                pm = QPixmap()
                pm.loadFromData(data)
                if not pm.isNull():
                    return pm
            except Exception:
                pass
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._result)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # Double-click = open the full-size image in a lightbox.
        # Chat navigation is reached via the preview panel's
        # "Go to Message" button instead.
        self._open_lightbox()
        super().mouseDoubleClickEvent(event)

    def _open_lightbox(self):
        """Show the full-resolution image in a borderless dialog with
        zoom / pan / keyboard controls / download."""
        fpath = self._result.get("file_path", "")
        pm = None
        if fpath and os.path.isfile(fpath):
            pm = QPixmap(fpath)
        if (not pm or pm.isNull()) and self._result.get("thumb"):
            try:
                data = base64.b64decode(self._result["thumb"])
                pm = QPixmap(); pm.loadFromData(data)
            except Exception:
                pm = None
        if not pm or pm.isNull():
            return
        from app.views.widgets.image_lightbox import show_lightbox
        show_lightbox(self, pm, self._result)


# ---------------------------------------------------------------------------
# Conversation preview panel (right side)
# ---------------------------------------------------------------------------

class PreviewPanel(QFrame):
    """Right-side panel showing detailed info for the selected result.

    Uses explicit, high-contrast colours instead of palette()
    tokens so it remains readable on every theme — palette-driven
    text on light backgrounds renders too faint here.
    """

    navigate_requested = Signal(int, int)  # conv_id, msg_id

    PANEL_WIDTH = 360
    PREVIEW_SIZE = QSize(328, 220)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._light = _is_light()
        self._c_bg = "#ffffff" if self._light else "#1f2c34"
        self._c_border = "#e0e3e7" if self._light else "#2a3942"
        self._c_text = "#111b21" if self._light else "#e9edef"
        self._c_muted = "#667781" if self._light else "#aebac1"
        self._c_key = "#455a64" if self._light else "#cfd8dc"
        self._c_accent = "#00897b" if self._light else "#00bcd4"
        self._c_thumb_bg = "#f4f5f5" if self._light else "#111b21"

        self.setFixedWidth(self.PANEL_WIDTH)
        self.setMinimumWidth(self.PANEL_WIDTH)
        self.setStyleSheet(
            f"PreviewPanel {{ background: {self._c_bg};"
            f" border-left: 1px solid {self._c_border}; }}"
        )
        self._db = Database.get()
        self._setup_ui()

    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(8)

        self._placeholder = QLabel("Select a result\nto see details")
        self._placeholder.setFont(QFont("Segoe UI", 10))
        self._placeholder.setStyleSheet(f"color: {_muted_color()}; border: none;")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self._placeholder, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {self._c_bg}; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: {self._c_bg}; }}"
        )

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(10)

        # Image preview — click to open fullscreen lightbox
        self._image_label = QLabel()
        self._image_label.setFixedSize(self.PREVIEW_SIZE)
        self._image_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setCursor(Qt.PointingHandCursor)
        self._apply_image_style()
        self._image_label.setToolTip("Click for full-size preview")
        self._image_label.mousePressEvent = lambda _=None: self._on_thumb_clicked()
        self._content_layout.addWidget(self._image_label, 0, Qt.AlignHCenter)

        # Metadata section — high-contrast, forced colors
        self._meta_label = QLabel()
        self._meta_label.setFont(QFont("Segoe UI", 9))
        self._meta_label.setWordWrap(True)
        self._meta_label.setTextFormat(Qt.RichText)
        self._meta_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._meta_label.setStyleSheet(
            f"color: {self._c_text}; border: none; background: transparent;"
        )
        self._meta_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._meta_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._content_layout.addWidget(self._meta_label)
        self._content_layout.addStretch()

        self._scroll.setWidget(self._content)
        self._layout.addWidget(self._scroll, 1)

        # Go to message button — solid accent, plainly readable
        self._goto_btn = QPushButton("\u2192  Go to message in chat")
        self._goto_btn.setCursor(Qt.PointingHandCursor)
        self._goto_btn.setStyleSheet(
            f"QPushButton {{ background: {self._c_accent}; color: #ffffff;"
            f" border: none; border-radius: 6px; padding: 10px 16px;"
            f" font-weight: bold; font-size: 12px; }}"
            f"QPushButton:hover {{ background: #007a6e; }}"
            f"QPushButton:disabled {{ background: rgba(128,128,128,0.25);"
            f" color: rgba(255,255,255,0.4); }}"
        )
        self._goto_btn.setEnabled(False)
        self._goto_btn.clicked.connect(self._on_goto_clicked)
        self._layout.addWidget(self._goto_btn)

        # "Also appears in" cross-chat block intentionally
        # omitted — the results grid already enumerates every
        # matching chat, so a duplicate listing here would only
        # add visual noise.

        self._current_result = None
        self._show_placeholder(True)

    def _show_placeholder(self, show: bool):
        self._placeholder.setVisible(show)
        self._scroll.setVisible(not show)
        self._goto_btn.setVisible(not show)

    def _apply_image_style(self):
        self._image_label.setStyleSheet(
            f"background: {self._c_thumb_bg}; border-radius: 6px;"
            f" border: 1px solid {self._c_border}; color: {self._c_muted};"
            f" font-size: 11px;"
        )

    def show_result(self, result: dict, all_results: list[dict]):
        """Display detailed info for a result."""
        self._current_result = result
        self._show_placeholder(False)

        # Load image at larger size
        pixmap = self._load_image(result)
        self._image_label.clear()
        self._apply_image_style()
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                self.PREVIEW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self._image_label.setPixmap(scaled)
        else:
            self._image_label.setText("No Preview")

        # Build metadata
        msg_id = result.get("message_id", 0)
        conv_name = result.get("conv_name", "Unknown")
        sender = result.get("sender_name", "Unknown")
        fpath = result.get("file_path", "")
        tier = result.get("tier", 0)

        # Fetch extra metadata from DB
        extra = self._fetch_extra_metadata(msg_id)
        timestamp = extra.get("timestamp", result.get("timestamp", 0))
        file_size = extra.get("file_size", 0)
        width = extra.get("width", 0)
        height = extra.get("height", 0)
        mime_type = extra.get("mime_type", "")

        dt_str = ""
        if timestamp:
            try:
                # tz-aware (selected case TZ) — was using machine local
                from app.config import format_timestamp as _ft
                dt_str = _ft(timestamp, "datetime")
            except Exception:
                pass

        tier_label = TIER_LABELS.get(tier, f"Tier {tier}")
        tier_color = TIER_COLORS.get(tier, "#888")
        is_exact = result.get("match_mode") == "exact" or tier == 0
        if is_exact:
            tier_label = "EXACT (SHA-256)"
            tier_color = "#4caf50"

        resolution_str = f"{width} x {height}" if width and height else "—"
        size_str = self._format_file_size(file_size) if file_size else "—"

        # Pull extra forensic bits for the provenance row
        jid_bits = self._fetch_sender_jid_bits(msg_id)

        # Fetch conversation JID + chat type
        conv_jid = ""
        conv_type = ""
        try:
            row = self._db.fetchone(
                "SELECT jid_raw_string, chat_type FROM conversation "
                "WHERE id = ?", (result.get("conversation_id", 0),),
            )
            if row:
                conv_jid = row[0] or ""
                conv_type = row[1] or ""
        except Exception:
            pass

        # Row builder — forced high-contrast colors
        k = self._c_key
        t = self._c_text
        a = self._c_accent

        def _row(key: str, val: str) -> str:
            return (
                f'<p style="margin:0 0 8px 0;">'
                f'<span style="color:{k};font-weight:700;font-size:11px;">'
                f'{key}</span><br>'
                f'<span style="color:{t};font-size:12px;line-height:1.35;">'
                f'{val}</span></p>'
            )

        direction_bit = ("\u2191 Sent" if result.get("from_me")
                          else "\u2193 Received")
        direction_color = "#2e7d32" if result.get("from_me") else "#1976d2"

        meta_html = (
            '<div style="font-size:12px;">'
            + (
                f'<p style="margin:0 0 10px 0;">'
                f'<span style="background:{tier_color};color:#fff;'
                f' padding:2px 8px;border-radius:8px;'
                f' font-weight:700;font-size:11px">{tier_label}</span>'
                f'&nbsp;&nbsp;<span style="color:{direction_color};'
                f' font-weight:700;font-size:12px">{direction_bit}</span>'
                f'</p>'
            )
            + _row("Conversation",
                   f'<span style="color:{a};font-weight:700">'
                   f'{self._esc(conv_name)}</span>'
                   + (f'<br><span style="color:{k};font-size:10.5px">'
                      f'<code>{self._esc(conv_jid)}</code></span>'
                      if conv_jid else ""))
            + (_row("Chat type", self._esc(conv_type.title())) if conv_type else "")
            + _row("Sender", self._esc(sender or "—"))
            + (_row("Sender JID / Phone",
                    f'<span style="font-family:Consolas,monospace;font-size:11px">'
                    f'{self._esc(jid_bits)}</span>') if jid_bits else "")
            + _row("Date / Time", self._esc(dt_str or "—"))
            + _row("Message ID",
                   f'<code style="font-size:11px">{msg_id}</code>')
            + _row("Resolution", resolution_str)
            + _row("File size", size_str)
            + _row("MIME", self._esc(mime_type or "—"))
            + (_row("Distances",
                    f'<code style="font-size:11px">p:{result.get("phash_dist","?")}'
                    f'  d:{result.get("dhash_dist","?")}'
                    f'  e:{result.get("edge_dist","?")}</code>')
               if not is_exact else "")
            + _row("File",
                   f'<span style="font-family:Consolas,monospace;font-size:10.5px;'
                   f' word-break:break-all">'
                   f'{self._esc(os.path.basename(fpath) if fpath else "—")}</span>')
            + '</div>'
        )
        self._meta_label.setText(meta_html)
        self._meta_label.setToolTip(fpath)

        # Enable navigation button
        conv_id = result.get("conversation_id", 0)
        self._goto_btn.setEnabled(bool(conv_id and msg_id))

    def _show_crosschat(self, result: dict, all_results: list[dict]):
        """Deprecated — kept as no-op for any stale call sites."""
        return

    def _fetch_extra_metadata(self, message_id: int) -> dict:
        """Fetch additional metadata from the database for a message."""
        if not message_id:
            return {}
        try:
            row = self._db.fetchone(
                "SELECT m.timestamp, "
                "       me.file_size, me.width, me.height, me.mime_type "
                "FROM message m "
                "LEFT JOIN media me ON me.message_id = m.id "
                "WHERE m.id = ?",
                (message_id,),
            )
            if row:
                return {
                    "timestamp": row[0] or 0,
                    "file_size": row[1] or 0,
                    "width": row[2] or 0,
                    "height": row[3] or 0,
                    "mime_type": row[4] or "",
                }
        except Exception:
            logger.debug("Failed to fetch extra metadata for message %d", message_id, exc_info=True)
        return {}

    def _fetch_sender_jid_bits(self, message_id: int) -> str:
        """Return '+<phone> · <jid>' for the sender.

        For owner-sent messages (m.from_me=1, sender_id usually NULL), pulls
        the device-owner phone / JID from case_metadata so we never show a
        blank sender field — always 'You (Owner) · +<phone> · <jid>'.
        """
        if not message_id:
            return ""
        try:
            row = self._db.fetchone(
                "SELECT c.phone_number, c.phone_jid, c.lid_jid, m.from_me "
                "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
                "WHERE m.id = ?",
                (message_id,),
            )
            if not row:
                return ""
            phone, pjid, lid, from_me = row
            bits = []
            if phone: bits.append("+" + phone)
            if pjid:  bits.append(pjid)
            elif lid: bits.append(lid)

            if from_me:
                # Owner message — look up device-owner identity from case_metadata
                owner_phone, owner_jid = "", ""
                try:
                    for k, v in self._db.fetchall(
                        "SELECT key, value FROM case_metadata WHERE key IN "
                        "('device_owner_phone','device_owner_jid','device_owner_name')"
                    ):
                        if k == "device_owner_phone" and v: owner_phone = v
                        elif k == "device_owner_jid" and v: owner_jid = v
                except Exception:
                    pass
                if not bits and owner_phone:
                    bits.append("+" + owner_phone.lstrip("+"))
                if not any("@" in b for b in bits) and owner_jid:
                    bits.append(owner_jid)
                prefix = "You (Owner)"
                return prefix + (" · " + " · ".join(bits) if bits else "")

            return " · ".join(bits) if bits else ""
        except Exception:
            return ""

    def _on_thumb_clicked(self):
        """Click preview thumb → open the fullscreen lightbox."""
        if not self._current_result:
            return
        pm = self._load_image(self._current_result)
        if not pm or pm.isNull():
            return
        from app.views.widgets.image_lightbox import show_lightbox
        show_lightbox(self, pm, self._current_result)

    def _load_image(self, result: dict) -> QPixmap | None:
        # Prefer full-resolution disk file over the tiny msgstore thumbnail_blob
        fpath = result.get("file_path", "")
        if fpath and os.path.isfile(fpath):
            pm = QPixmap(fpath)
            if not pm.isNull():
                return pm
        thumb_b64 = result.get("thumb", "")
        if thumb_b64:
            try:
                data = base64.b64decode(thumb_b64)
                pm = QPixmap()
                pm.loadFromData(data)
                if not pm.isNull():
                    return pm
            except Exception:
                pass
        fpath2 = fpath  # legacy fallthrough for structure
        if fpath2 and os.path.isfile(fpath2):
            pm = QPixmap(fpath2)
            if not pm.isNull():
                return pm
        return None

    def _on_goto_clicked(self):
        if self._current_result:
            conv_id = self._current_result.get("conversation_id", 0)
            msg_id = self._current_result.get("message_id", 0)
            if conv_id and msg_id:
                self.navigate_requested.emit(conv_id, msg_id)

    @staticmethod
    def _esc(text: str) -> str:
        """Escape HTML entities."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    def clear_preview(self):
        self._current_result = None
        self._show_placeholder(True)


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

class ImageSimilarityPage(QWidget):
    """Image Similarity Search page for the forensic analyzer."""

    navigate_to_message = Signal(int, int)  # conv_id, msg_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._db = Database.get()
        self._index_worker: IndexWorker | None = None
        self._search_worker: SearchWorker | None = None
        self._query_message_id: int = 0
        self._query_image_path: str = ""
        self._last_results: list[dict] = []
        self._result_cards: list[ResultCard] = []
        self._selected_card: ResultCard | None = None
        self._group_by_conv: bool = False
        self._setup_ui()
        # Default mode = exact (SHA-256 duplicates) — no perceptual index needed
        self._set_match_mode("exact")
        self._refresh_status()

    def showEvent(self, event):
        """Refresh index status every time the page becomes visible."""
        super().showEvent(event)
        self._refresh_status()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 8)
        root_layout.setSpacing(6)
        _accent = _accent_color()

        # ---- Compact title row (title + mode toggle + index strip on one row) ----
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        header = QLabel("Image Similarity \u00B7 Duplicate Finder")
        header.setFont(QFont("Segoe UI", 13, QFont.Bold))
        header.setStyleSheet("border: none;")
        title_row.addWidget(header)
        title_row.addStretch()

        # Match-mode segmented toggle
        mode_label = QLabel("Match:")
        mode_label.setStyleSheet(f"color: {_muted_color()}; border: none; font-size: 10px;")
        title_row.addWidget(mode_label)
        self._mode_exact_btn = QPushButton("Exact (SHA-256)")
        self._mode_exact_btn.setCheckable(True)
        self._mode_exact_btn.setChecked(True)
        self._mode_visual_btn = QPushButton("Visual (pHash)")
        self._mode_visual_btn.setCheckable(True)
        _mode_css = f"""
            QPushButton {{
                background: palette(base); color: palette(text);
                border: 1px solid palette(mid); padding: 3px 10px;
                font-size: 10px; font-weight: 600;
            }}
            QPushButton:checked {{ background: {_accent}; color: #fff; border-color: {_accent}; }}
            QPushButton:hover {{ border-color: {_accent}; }}
        """
        self._mode_exact_btn.setStyleSheet(_mode_css + "QPushButton { border-top-left-radius: 4px; border-bottom-left-radius: 4px; border-right: 0; }")
        self._mode_visual_btn.setStyleSheet(_mode_css + "QPushButton { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }")
        self._mode_exact_btn.clicked.connect(lambda: self._set_match_mode("exact"))
        self._mode_visual_btn.clicked.connect(lambda: self._set_match_mode("visual"))
        self._match_mode = "exact"
        title_row.addWidget(self._mode_exact_btn)
        title_row.addWidget(self._mode_visual_btn)
        root_layout.addLayout(title_row)

        # ---- Slim index strip (one row: status + progress + build btn) ----
        # Visible only in visual mode (exact mode doesn't need an index)
        self._index_strip = QFrame()
        self._index_strip.setFixedHeight(38)
        self._index_strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._index_strip.setStyleSheet(
            "QFrame { background: palette(base); border: 1px solid palette(mid); border-radius: 6px; }"
        )
        idx_row = QHBoxLayout(self._index_strip)
        idx_row.setContentsMargins(10, 4, 8, 4)
        idx_row.setSpacing(8)
        self._index_status = QLabel("Index: checking…")
        self._index_status.setFont(QFont("Segoe UI", 9))
        self._index_status.setStyleSheet("color: palette(text); border: none;")
        self._index_status.setTextFormat(Qt.RichText)
        idx_row.addWidget(self._index_status)
        self._index_progress = QProgressBar()
        self._index_progress.setFixedHeight(10)
        self._index_progress.setMinimumWidth(160)
        self._index_progress.setTextVisible(False)
        self._index_progress.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid palette(mid); border-radius: 3px;
                background: palette(window);
            }}
            QProgressBar::chunk {{ background: {_accent}; border-radius: 2px; }}
        """)
        idx_row.addWidget(self._index_progress, 1)
        self._index_detail = QLabel("")
        self._index_detail.setFont(QFont("Segoe UI", 9))
        self._index_detail.setStyleSheet(f"color: {_muted_color()}; border: none;")
        idx_row.addWidget(self._index_detail)
        self._build_btn = QPushButton("Build Index")
        self._build_btn.setFixedHeight(24)
        self._build_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_accent}; color: #fff; border: none;
                border-radius: 4px; padding: 2px 12px; font-weight: 600; font-size: 10px;
            }}
            QPushButton:hover {{ background: #00695c; }}
            QPushButton:disabled {{ background: palette(mid); color: palette(midlight); }}
        """)
        self._build_btn.clicked.connect(self._start_build_index)
        idx_row.addWidget(self._build_btn)
        root_layout.addWidget(self._index_strip)

        # ---- Compact query strip (drop zone + buttons + preview, one row) ----
        query_frame = QFrame()
        query_frame.setFixedHeight(92)
        query_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        query_frame.setStyleSheet(
            "QFrame { background: palette(base); border: 1px solid palette(mid); border-radius: 6px; }"
        )
        query_layout = QHBoxLayout(query_frame)
        query_layout.setContentsMargins(10, 8, 10, 8)
        query_layout.setSpacing(10)

        # Query thumb (left, 52x52)
        self._query_thumb = QLabel()
        self._query_thumb.setFixedSize(QSize(52, 52))
        self._query_thumb.setAlignment(Qt.AlignCenter)
        self._query_thumb.setStyleSheet(
            "background: palette(window); border-radius: 4px;"
            " color: palette(mid); font-size: 9px; border: 1px dashed palette(mid);"
        )
        self._query_thumb.setText("No\nimage")
        query_layout.addWidget(self._query_thumb)

        # Drop zone + filename (flexible middle)
        mid_col = QVBoxLayout()
        mid_col.setSpacing(2)
        mid_col.setContentsMargins(0, 0, 0, 0)
        self._drop_zone = DropZone()
        self._drop_zone.file_dropped.connect(self._on_file_selected)
        mid_col.addWidget(self._drop_zone)
        self._query_preview = QLabel("Drop an image, browse, or paste from clipboard.")
        self._query_preview.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._query_preview.setStyleSheet(f"color: {_muted_color()}; border: none; font-size: 9px;")
        self._query_preview.setWordWrap(True)
        self._query_preview.setMaximumHeight(18)
        mid_col.addWidget(self._query_preview)
        query_layout.addLayout(mid_col, 1)

        # Buttons column (right)
        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        btn_col.setContentsMargins(0, 0, 0, 0)
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(4)
        browse_btn = QPushButton("\U0001F4C2 Browse")
        browse_btn.setFixedHeight(26)
        browse_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_accent}; color: #fff; border: none;
                border-radius: 4px; padding: 2px 10px; font-weight: 600; font-size: 10px;
            }}
            QPushButton:hover {{ background: #00695c; }}
        """)
        browse_btn.clicked.connect(self._browse_file)
        btn_row1.addWidget(browse_btn)

        paste_btn = QPushButton("\U0001F4CB Paste")
        paste_btn.setFixedHeight(26)
        paste_btn.setStyleSheet("""
            QPushButton {
                background: #1565c0; color: #fff; border: none;
                border-radius: 4px; padding: 2px 10px; font-weight: 600; font-size: 10px;
            }
            QPushButton:hover { background: #0d47a1; }
        """)
        paste_btn.clicked.connect(self._paste_from_clipboard)
        btn_row1.addWidget(paste_btn)
        btn_col.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(4)
        btn_row2.addWidget(QLabel("Top:"))
        self._top_k_spin = QSpinBox()
        self._top_k_spin.setRange(10, 5000)
        self._top_k_spin.setValue(500)
        self._top_k_spin.setSingleStep(50)
        self._top_k_spin.setFixedWidth(70)
        self._top_k_spin.setFixedHeight(26)
        self._top_k_spin.setToolTip("Max results (visual mode only; exact returns every share)")
        self._top_k_spin.setStyleSheet(
            "background: palette(base); color: palette(text);"
            " border: 1px solid palette(mid); border-radius: 3px; padding: 2px 4px;"
        )
        btn_row2.addWidget(self._top_k_spin)

        self._search_btn = QPushButton("\u25B6 Find")
        self._search_btn.setFixedHeight(26)
        self._search_btn.setEnabled(False)
        self._search_btn.setStyleSheet("""
            QPushButton {
                background: #ff9800; color: #fff; border: none;
                border-radius: 4px; padding: 2px 16px; font-weight: 700; font-size: 10px;
            }
            QPushButton:hover { background: #ffa726; }
            QPushButton:disabled { background: palette(mid); color: palette(midlight); }
        """)
        self._search_btn.clicked.connect(self._start_search)
        btn_row2.addWidget(self._search_btn)
        btn_col.addLayout(btn_row2)
        query_layout.addLayout(btn_col)

        root_layout.addWidget(query_frame)

        # ---- Results header row (tight, single-line rich text) ----
        _res_row = QHBoxLayout()
        _res_row.setSpacing(6)
        self._results_header = QLabel("Results")
        self._results_header.setFont(QFont("Segoe UI", 10))
        self._results_header.setTextFormat(Qt.RichText)
        self._results_header.setWordWrap(True)
        self._results_header.setVisible(False)
        _res_row.addWidget(self._results_header, 1)

        self._group_toggle = QPushButton("Group by Conversation")
        self._group_toggle.setCheckable(True)
        self._group_toggle.setVisible(False)
        self._group_toggle.setStyleSheet("""
            QPushButton {
                background: palette(base); color: palette(text);
                border: 1px solid palette(mid); border-radius: 4px;
                padding: 4px 12px; font-size: 9px;
            }
            QPushButton:checked {
                background: palette(highlight); color: palette(highlighted-text);
                border: 1px solid palette(highlight);
            }
            QPushButton:hover { border-color: palette(highlight); }
        """)
        self._group_toggle.clicked.connect(self._on_group_toggle)
        _res_row.addWidget(self._group_toggle)

        self._table_toggle = QPushButton("Table View")
        self._table_toggle.setCheckable(True)
        self._table_toggle.setVisible(False)
        self._table_toggle.setStyleSheet(self._group_toggle.styleSheet())
        self._table_toggle.clicked.connect(self._on_table_toggle)
        _res_row.addWidget(self._table_toggle)

        self._expand_btn = QPushButton("Expand Search (find more)")
        self._expand_btn.setVisible(False)
        self._expand_btn.setStyleSheet("""
            QPushButton {
                background: #7c4dff; color: #fff; border: none;
                border-radius: 4px; padding: 6px 14px; font-weight: bold;
            }
            QPushButton:hover { background: #9c6fff; }
        """)
        self._expand_btn.clicked.connect(self._expand_search)
        _res_row.addWidget(self._expand_btn)

        # Tag all results as forensic evidence
        self._tag_all_btn = QPushButton("\U0001F3F7  Tag all as evidence")
        self._tag_all_btn.setVisible(False)
        self._tag_all_btn.setToolTip(
            "Add a named tag to every matched message so you can "
            "review, filter, and include them in reports later."
        )
        self._tag_all_btn.setStyleSheet("""
            QPushButton {
                background: #ff9800; color: #fff; border: none;
                border-radius: 4px; padding: 6px 14px; font-weight: bold;
            }
            QPushButton:hover { background: #fb8c00; }
        """)
        self._tag_all_btn.clicked.connect(self._tag_all_results)
        _res_row.addWidget(self._tag_all_btn)

        # Export a forensic HTML report of every match
        self._report_btn = QPushButton("\U0001F4DD  Export report")
        self._report_btn.setVisible(False)
        self._report_btn.setToolTip(
            "Generate a standalone HTML report listing every match "
            "(thumbnail, conversation, sender, JID, timestamp, hash)."
        )
        self._report_btn.setStyleSheet("""
            QPushButton {
                background: #2e7d32; color: #fff; border: none;
                border-radius: 4px; padding: 6px 14px; font-weight: bold;
            }
            QPushButton:hover { background: #388e3c; }
        """)
        self._report_btn.clicked.connect(self._export_report)
        _res_row.addWidget(self._report_btn)
        root_layout.addLayout(_res_row)

        # ---- Results area ----
        # The results region is wrapped in a QStackedWidget so
        # the available vertical space is always claimed by
        # either the empty-state hint or the results splitter —
        # never left as a blank gap below the input strips.
        self._results_stack = QStackedWidget()
        self._results_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Page 0: empty-state hint (no results yet)
        self._empty_state = self._build_empty_state()
        self._results_stack.addWidget(self._empty_state)

        # Page 1: the actual results splitter
        self._splitter = QSplitter(Qt.Horizontal)

        # Left: scrollable grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setSpacing(8)
        self._results_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        # The grid will be added inside _results_layout
        self._results_grid = QGridLayout()
        self._results_grid.setSpacing(8)
        self._results_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._results_layout.addLayout(self._results_grid)
        self._results_layout.addStretch()

        self._scroll.setWidget(self._results_container)

        # Table view (hidden by default) — configured dynamically per search mode
        from PySide6.QtWidgets import QTableWidget, QHeaderView
        self._results_table = QTableWidget()
        self._results_table.setColumnCount(7)  # exact-mode default
        self._results_table.setHorizontalHeaderLabels([
            "", "Dir", "Conversation", "Sender", "Date", "Filename", "Size"
        ])
        self._results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._results_table.setAlternatingRowColors(True)
        self._results_table.verticalHeader().setDefaultSectionSize(52)
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.horizontalHeader().setStretchLastSection(False)
        self._results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self._results_table.setColumnWidth(0, 54)
        self._results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._results_table.setIconSize(QSize(48, 48))
        self._results_table.setStyleSheet(
            "QTableWidget { border: none; background: palette(base); gridline-color: palette(mid); }"
            " QHeaderView::section { background: palette(window); border: none;"
            "  padding: 4px 8px; font-weight: 600; }"
        )
        self._results_table.cellClicked.connect(self._on_table_cell_clicked)
        self._results_table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self._results_table.setVisible(False)

        # Stack: show either grid scroll or table
        self._view_stack = QWidget()
        _stack_layout = QVBoxLayout(self._view_stack)
        _stack_layout.setContentsMargins(0, 0, 0, 0)
        _stack_layout.addWidget(self._scroll)
        _stack_layout.addWidget(self._results_table)
        self._splitter.addWidget(self._view_stack)

        # Right: preview panel
        self._preview_panel = PreviewPanel()
        self._preview_panel.navigate_requested.connect(self._on_card_double_clicked)
        self._splitter.addWidget(self._preview_panel)

        # Set splitter proportions
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)

        self._results_stack.addWidget(self._splitter)
        # Start on the empty-state page (index 0)
        self._results_stack.setCurrentIndex(0)
        root_layout.addWidget(self._results_stack, 1)

        # Accept drag-and-drop on the whole page
        self.setAcceptDrops(True)

    def _build_empty_state(self) -> QWidget:
        """Friendly placeholder shown when no search has run yet.

        Centered card with a big icon, headline, and three concrete
        usage tips.  Replaces the empty bottom area that used to make
        the page feel half-empty.  Theme-aware (palette-driven) so
        light- and dark-mode both look right.
        """
        wrap = QWidget()
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()

        # Card itself — soft background, rounded, centered
        card = QFrame()
        card.setObjectName("emptyStateCard")
        card.setMaximumWidth(640)
        card.setStyleSheet("""
            QFrame#emptyStateCard {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 12px;
            }
            QFrame#emptyStateCard QLabel { border: none; background: transparent; }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(10)

        icon = QLabel("\U0001F50D")  # magnifying-glass
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("font-size: 40px; color: palette(mid);")
        card_layout.addWidget(icon)

        title = QLabel("Find duplicate or visually-similar images")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Segoe UI", 12, QFont.DemiBold))
        card_layout.addWidget(title)

        sub = QLabel(
            "Drop a screenshot, browse for an image, or paste from your clipboard. "
            "Then pick a match mode and hit <b>Find</b>."
        )
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {_muted_color()};")
        card_layout.addWidget(sub)

        # Match-mode legend
        legend = QLabel(
            "<table cellpadding='4' style='margin: 0 auto;'>"
            "<tr><td style='color:#4caf50; font-weight:600'>Exact (SHA-256)</td>"
            "<td>byte-identical copies of the same file across all chats &mdash; "
            "every share, forward, and re-upload</td></tr>"
            "<tr><td style='color:#00bcd4; font-weight:600'>Visual (pHash)</td>"
            "<td>visually similar images &mdash; resized, recompressed, "
            "or template-matched (e.g. UPI screenshots)</td></tr>"
            "</table>"
        )
        legend.setTextFormat(Qt.RichText)
        legend.setAlignment(Qt.AlignCenter)
        legend.setWordWrap(True)
        legend.setStyleSheet(f"color: {_muted_color()}; font-size: 10px;")
        card_layout.addWidget(legend)

        # Center the card horizontally
        h = QHBoxLayout()
        h.addStretch()
        h.addWidget(card)
        h.addStretch()
        outer.addLayout(h)

        outer.addStretch()
        return wrap

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _paste_from_clipboard(self):
        """Paste an image from the system clipboard and use it as query."""
        clipboard = QApplication.clipboard()
        img = clipboard.image()
        if img and not img.isNull():
            # Save clipboard image to temp file
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            pxm = QPixmap.fromImage(img)
            pxm.save(tmp.name, "PNG")
            self._on_file_selected(tmp.name)
        else:
            # Try to get file path from clipboard
            mime = clipboard.mimeData()
            if mime and mime.hasUrls():
                for url in mime.urls():
                    if url.isLocalFile():
                        path = url.toLocalFile()
                        if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
                            self._on_file_selected(path)
                            return
            QMessageBox.information(
                self, "Paste", "No image found in clipboard.\n"
                "Copy an image or screenshot first, then click Paste.")

    def _refresh_status(self):
        # If indexing is running in background, show that
        if IndexWorker.is_running():
            self._build_btn.setEnabled(False)
            self._build_btn.setText("Indexing...")
            return

        try:
            from app.services.image_similarity import ImageSimilarityEngine
            engine = ImageSimilarityEngine(self._db)
            stats = engine.count_index_status()
            indexed = stats["indexed"]
            total = stats["total"]
            new = stats["new"]
            pct = stats["percentage"]
            unique_paths = stats.get("unique_paths", 0)

            # Forensic clarity: many message rows share the same file
            # (hash-linked siblings, forwards).  Surface the unique-file
            # count so it's clear we don't waste work re-hashing the
            # same bytes once per message_id.
            unique_note = ""
            if unique_paths and unique_paths < total:
                shared = total - unique_paths
                unique_note = (
                    f"  <span style='color:{_muted_color()};font-weight:400'>"
                    f"({unique_paths:,} unique files; {shared:,} are shared "
                    f"copies hashed once and reused)</span>"
                )

            if indexed > 0:
                self._index_status.setText(
                    f"<b>Indexed: {indexed:,} / {total:,} images</b>{unique_note}")
                self._index_status.setStyleSheet("color: #4caf50; border: none;")
                self._index_progress.setRange(0, max(total, 1))
                self._index_progress.setValue(indexed)
                self._index_progress.setFormat(f"{pct}%")
                self._index_progress.setVisible(True)
                if new > 0:
                    self._index_detail.setText(
                        f"\U0001F7E2 {indexed:,} indexed  \u2022  "
                        f"\U0001F535 {new:,} new (not yet indexed)")
                    self._build_btn.setText("Update Index")
                else:
                    self._index_detail.setText(
                        f"\u2705 All {indexed:,} eligible images are indexed")
                    self._build_btn.setText("Rebuild Index")
            else:
                self._index_status.setText(
                    f"<b>Index not built</b> \u2014 {total:,} images available"
                    f"{unique_note}")
                self._index_status.setStyleSheet("color: #ff9800; border: none;")
                self._index_progress.setRange(0, 1)
                self._index_progress.setValue(0)
                self._index_progress.setFormat("0%")
                self._index_progress.setVisible(True)
                self._index_detail.setText(
                    "Click 'Build Index' to compute perceptual hashes")
                self._build_btn.setText("Build Index")
        except ImportError as exc:
            self._index_status.setText(f"Missing libraries: {exc}")
            self._index_status.setStyleSheet("color: #f44336; border: none;")
            self._index_detail.setText("Install: pip install imagehash Pillow")
            self._build_btn.setEnabled(False)
        except Exception:
            self._index_status.setText("Index: error checking status")
            self._index_status.setStyleSheet("color: #ff9800; border: none;")

    def _start_build_index(self):
        if IndexWorker.is_running():
            return
        self._build_btn.setEnabled(False)
        self._build_btn.setText("Indexing...")

        # Try to use MainWindow's global indexing (shows progress in status bar)
        main_win = self.window()
        if hasattr(main_win, 'start_image_indexing'):
            main_win.start_image_indexing()
            # Also connect to local UI updates
            worker = IndexWorker.get_or_create(main_win)
            worker.progress.connect(self._on_build_progress)
            worker.finished.connect(self._on_build_finished)
        else:
            # Fallback: run locally
            self._index_worker = IndexWorker.get_or_create(self)
            self._index_worker.progress.connect(self._on_build_progress)
            self._index_worker.finished.connect(self._on_build_finished)
            self._index_worker.start()

    def _on_build_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self._index_progress.setMaximum(100)
            self._index_progress.setValue(pct)
            self._index_progress.setFormat(f"{current:,}/{total:,} ({pct}%)")

    def _on_build_finished(self, count: int, error: str):
        self._index_progress.setVisible(False)
        self._build_btn.setEnabled(True)
        self._build_btn.setText("Rebuild Index")

        if error:
            QMessageBox.warning(self, "Index Build Failed", error)
            self._index_status.setText(f"Index build failed: {error[:80]}")
            self._index_status.setStyleSheet("color: #f44336; border: none;")
        else:
            self._index_status.setText(f"Index: {count:,} images hashed")
            self._index_status.setStyleSheet("color: #4caf50; border: none;")

        self._refresh_status()
        self._index_worker = None

    # ------------------------------------------------------------------
    # Query image selection
    # ------------------------------------------------------------------

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Query Image",
            "",
            "Images (*.jpg *.jpeg *.png *.webp *.gif *.bmp);;All Files (*)",
        )
        if path:
            self._on_file_selected(path)

    def _on_file_selected(self, path: str):
        self._query_image_path = path
        self._query_message_id = 0
        self._search_btn.setEnabled(True)

        pm = QPixmap(path)
        if not pm.isNull():
            scaled = pm.scaled(QSize(76, 76), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._query_thumb.setPixmap(scaled)
        fname = os.path.basename(path)
        self._query_preview.setText(f"<b>{fname}</b><br><span style='color:gray'>{path}</span>")
        self._query_preview.setToolTip(path)
        self._drop_zone.set_file_name(fname)

    def set_query_message_id(self, message_id: int):
        """Set query from a message_id (e.g. right-click in media gallery)."""
        self._query_message_id = message_id
        self._query_image_path = ""
        self._search_btn.setEnabled(True)

        row = self._db.fetchone(
            "SELECT me.resolved_file_path, me.thumbnail_blob "
            "FROM media me WHERE me.message_id = ?",
            (message_id,),
        )
        if row:
            fpath = row[0] or ""
            thumb = row[1]
            if fpath and os.path.isfile(fpath):
                pm = QPixmap(fpath)
                if not pm.isNull():
                    scaled = pm.scaled(
                        QSize(56, 56), Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    self._query_preview.setPixmap(scaled)
                self._drop_zone.set_file_name(os.path.basename(fpath))
            elif thumb:
                pm = QPixmap()
                data = thumb if isinstance(thumb, bytes) else base64.b64decode(thumb)
                pm.loadFromData(data)
                if not pm.isNull():
                    scaled = pm.scaled(
                        QSize(56, 56), Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    self._query_preview.setPixmap(scaled)
                self._drop_zone.set_file_name(f"Message #{message_id}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _set_match_mode(self, mode: str):
        """Toggle between 'exact' (SHA-256) and 'visual' (perceptual) modes."""
        self._match_mode = mode
        self._mode_exact_btn.setChecked(mode == "exact")
        self._mode_visual_btn.setChecked(mode == "visual")
        # Index strip + Top-K + Expand only matter in visual mode
        self._index_strip.setVisible(mode == "visual")
        self._top_k_spin.setEnabled(mode == "visual")
        # Auto-switch view: table is the right default for exact (sharing history)
        if hasattr(self, "_table_toggle"):
            self._table_toggle.setChecked(mode == "exact")

    def _start_search(self):
        if not self._query_message_id and not self._query_image_path:
            return

        self._search_btn.setEnabled(False)
        self._search_btn.setText("Searching\u2026")
        self._clear_results()

        self._search_worker = SearchWorker(
            message_id=self._query_message_id,
            image_path=self._query_image_path,
            top_k=self._top_k_spin.value(),
            mode=self._match_mode,
            parent=self,
        )
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_worker.start()

    def _on_search_finished(self, results: list, error: str):
        self._search_btn.setEnabled(True)
        self._search_btn.setText("\u25B6 Find")

        if error:
            QMessageBox.warning(self, "Search Failed", error)
            self._search_worker = None
            return

        self._last_results = results
        # Default to table view for exact mode — sharing history reads better as a list
        if self._match_mode == "exact" and hasattr(self, "_table_toggle"):
            self._table_toggle.setChecked(True)
        self._display_results(results)
        self._search_worker = None

    def _expand_search(self):
        """Flood-fill expansion in background thread."""
        if not self._last_results:
            return
        self._expand_btn.setEnabled(False)
        self._expand_btn.setText("Expanding... (scanning 75K images)")

        self._expand_worker = ExpandWorker(
            self._last_results,
            self._query_message_id or 0,
            self._top_k_spin.value(),
            self,
        )
        self._expand_worker.finished.connect(self._on_expand_finished)
        self._expand_worker.start()

    def _on_expand_finished(self, results: list, error: str):
        self._expand_btn.setEnabled(True)
        self._expand_btn.setText("Expand Search (find more)")
        if error:
            QMessageBox.warning(self, "Expand Failed", error)
            return
        self._last_results = results
        self._display_results(results)

    # ------------------------------------------------------------------
    # Group toggle
    # ------------------------------------------------------------------

    def _on_group_toggle(self):
        self._group_by_conv = self._group_toggle.isChecked()
        if self._last_results:
            self._display_results(self._last_results)

    def _on_table_toggle(self):
        is_table = self._table_toggle.isChecked()
        self._scroll.setVisible(not is_table)
        self._results_table.setVisible(is_table)
        if is_table and self._last_results:
            self._fill_table(self._last_results)

    def _fill_table(self, results):
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtGui import QIcon
        is_exact = self._match_mode == "exact" or all(
            r.get("match_mode") == "exact" for r in results
        )
        self._results_table.setRowCount(0)
        self._results_table.setRowCount(len(results))
        for i, r in enumerate(results):
            # Thumbnail
            pm = self._load_result_pixmap(r)
            if pm and not pm.isNull():
                icon = QIcon(pm.scaled(QSize(48, 48), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                thumb_item = QTableWidgetItem()
                thumb_item.setIcon(icon)
                self._results_table.setItem(i, 0, thumb_item)

            if is_exact:
                # Direction column — color-coded sent/received
                is_me = bool(r.get("from_me"))
                dir_item = QTableWidgetItem("\u2191 Sent" if is_me else "\u2193 Recv")
                dir_item.setForeground(QColor("#4caf50" if is_me else "#2196f3"))
                dir_item.setData(Qt.UserRole, r)
                self._results_table.setItem(i, 1, dir_item)
            else:
                tier = r.get("tier", 0)
                tier_item = QTableWidgetItem(TIER_LABELS.get(tier, f"Tier {tier}"))
                tier_item.setForeground(QColor(TIER_COLORS.get(tier, "#888")))
                tier_item.setData(Qt.UserRole, r)
                self._results_table.setItem(i, 1, tier_item)

            # Conversation (with emoji prefix for group chats for quick scanning)
            cname = r.get("conv_name", "")
            self._results_table.setItem(i, 2, QTableWidgetItem(cname))
            # Sender
            self._results_table.setItem(i, 3, QTableWidgetItem(r.get("sender_name", "")))
            # Date
            ts = r.get("timestamp", 0)
            date_str = ""
            if ts:
                try:
                    date_str = format_timestamp(ts, "minute")
                except Exception:
                    pass
            self._results_table.setItem(i, 4, QTableWidgetItem(date_str))
            # Filename
            fp = r.get("file_path", "")
            self._results_table.setItem(i, 5, QTableWidgetItem(os.path.basename(fp) if fp else ""))

            if is_exact:
                # File size (readable)
                fs = r.get("file_size", 0) or 0
                self._results_table.setItem(i, 6, QTableWidgetItem(
                    PreviewPanel._format_file_size(fs) if fs else "\u2014"))
            else:
                self._results_table.setItem(i, 6, QTableWidgetItem(str(r.get("phash_dist", ""))))
                self._results_table.setItem(i, 7, QTableWidgetItem(str(r.get("dhash_dist", ""))))

    def _on_table_cell_clicked(self, row, col):
        item = self._results_table.item(row, 1)
        if item:
            result = item.data(Qt.UserRole)
            if result:
                self._preview_panel.show_result(result, self._last_results)

    def _on_table_cell_double_clicked(self, row, col):
        item = self._results_table.item(row, 1)
        if item:
            result = item.data(Qt.UserRole)
            if result:
                cid = result.get("conversation_id", 0)
                mid = result.get("message_id", 0)
                if cid and mid:
                    self.navigate_to_message.emit(cid, mid)

    @staticmethod
    def _load_result_pixmap(result):
        thumb_b64 = result.get("thumb", "")
        if thumb_b64:
            try:
                data = base64.b64decode(thumb_b64)
                pm = QPixmap()
                pm.loadFromData(data)
                if not pm.isNull():
                    return pm
            except Exception:
                pass
        fpath = result.get("file_path", "")
        if fpath and os.path.isfile(fpath):
            pm = QPixmap(fpath)
            if not pm.isNull():
                return pm
        return None

    # ------------------------------------------------------------------
    # Results display
    # ------------------------------------------------------------------

    def _clear_results(self):
        # Remove all items from the grid
        while self._results_grid.count():
            item = self._results_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        # Also remove group headers from the outer layout (index 0 = grid layout, last = stretch)
        # We need to remove QLabel group headers that were inserted
        while self._results_layout.count() > 2:
            # Keep the grid layout (index 0) and the stretch (last)
            item = self._results_layout.takeAt(1)
            if item:
                w = item.widget()
                if w:
                    w.deleteLater()
                inner = item.layout()
                if inner:
                    while inner.count():
                        sub = inner.takeAt(0)
                        if sub and sub.widget():
                            sub.widget().deleteLater()

        self._result_cards.clear()
        self._selected_card = None
        self._results_header.setVisible(False)
        # Show the empty-state hint card instead of leaving blank
        # vertical space below the input strip.
        if hasattr(self, "_results_stack"):
            self._results_stack.setCurrentIndex(0)
        self._preview_panel.clear_preview()

    def _build_no_visual_match_message(self) -> str:
        """Compose a forensic-grade 'why no visual match' explanation.

        Reads ``engine.last_search_diagnostics`` written during the last
        ``_search`` and explains in plain English how far the nearest
        candidate is and why it didn't pass the tier thresholds.  Also
        gently steers the user toward Exact (SHA-256) mode, which is
        the right tool when the question is "is this exact file
        anywhere else in the case".
        """
        try:
            from app.services.image_similarity import ImageSimilarityEngine
            engine = ImageSimilarityEngine(self._db)
            diag = getattr(engine, "last_search_diagnostics", None)
        except Exception:
            diag = None

        if not diag:
            return (
                "<b>No visually-similar images found.</b><br>"
                "<span style='color:#888'>Try Exact (SHA-256) mode if "
                "you're looking for byte-identical copies.</span>"
            )

        scanned = diag.get("scanned", 0)
        np_ = diag.get("nearest_phash") or {}
        np_p = np_.get("phash_dist", 999)
        np_d = np_.get("dhash_dist", 999)
        np_e = np_.get("edge_dist", 999)
        th = diag.get("thresholds") or {}
        t3p = th.get("tier3_phash", 65)
        t3d = th.get("tier3_dhash", 30)
        t3e = th.get("tier3_edge", 50)
        return (
            f"<b>No visually-similar images found.</b> "
            f"Scanned <b>{scanned:,}</b> indexed images. "
            f"<br><span style='color:#888'>Nearest candidate is far "
            f"outside even the loosest <i>template</i> threshold "
            f"(phash <b>{np_p}</b> vs ≤{t3p}, dhash <b>{np_d}</b> vs "
            f"≤{t3d}, edge <b>{np_e}</b> vs ≤{t3e}). "
            f"This image is effectively unique in your case &mdash; "
            f"no resize, recompression, or template-share of it "
            f"exists elsewhere.</span>"
            f"<br><span style='color:#1565c0'>Tip: switch to "
            f"<b>Exact (SHA-256)</b> mode to find byte-identical "
            f"copies (forwards / re-uploads of the very same file).</span>"
        )

    def _display_results(self, results: list[dict]):
        self._clear_results()

        if not results:
            if self._match_mode == "exact":
                self._results_header.setText(
                    "No exact duplicates found — this file was not "
                    "shared on this device."
                )
            else:
                # Visual mode with zero matches: surface the diagnostic
                # info from the engine so the user understands WHY
                # (e.g., "image is unique — nearest neighbour is 86
                # hash units away, well outside our loosest threshold").
                # Drives them toward Exact mode for byte-identical
                # copies, since visual mode mathematically cannot find
                # a sibling that shares no perceptual features.
                self._results_header.setText(
                    self._build_no_visual_match_message()
                )
            self._results_header.setVisible(True)
            return

        is_exact = self._match_mode == "exact" or all(r.get("match_mode") == "exact" for r in results)

        if is_exact:
            # Forensic sharing-history summary
            # Split message-bound matches from orphaned ones \u2014 the
            # latter are byte-identical files on disk whose chat
            # record is gone (cleared chats, reinstalls).  Counting
            # them separately keeps the chat / sender stats clean.
            msg_results = [r for r in results if not r.get("is_orphan")]
            orphan_cnt = sum(1 for r in results if r.get("is_orphan"))
            convs = {r.get("conversation_id", 0) for r in msg_results if r.get("conversation_id")}
            senders = {r.get("sender_name", "") for r in msg_results if r.get("sender_name")}
            fh = next((r.get("file_hash", "") for r in results if r.get("file_hash")), "")
            from_me_cnt = sum(1 for r in msg_results if r.get("from_me"))
            recv_cnt = len(msg_results) - from_me_cnt
            hash_tail = ("\u2026" + fh[-12:]) if len(fh) > 12 else fh
            header_text = (
                f"<b>{len(results)}</b> share{'s' if len(results) != 1 else ''} of this exact file "
                f"(SHA-256 <code style='color:{_muted_color()}'>{hash_tail}</code>) "
                f"\u2014 across <b>{len(convs)}</b> chat{'s' if len(convs) != 1 else ''}, "
                f"<b>{len(senders)}</b> sender{'s' if len(senders) != 1 else ''} "
                f"\u2022 <span style='color:#4caf50'>{from_me_cnt} sent</span> / "
                f"<span style='color:#2196f3'>{recv_cnt} received</span>"
            )
            if orphan_cnt:
                header_text += (
                    f" \u2022 <span style='color:#e65100;font-weight:600'>"
                    f"{orphan_cnt} orphaned</span> "
                    f"<span style='color:{_muted_color()};font-size:9px'>"
                    f"(on disk, no chat record)</span>"
                )
        else:
            # Perceptual-mode tier summary
            tier_counts: dict[int, int] = {}
            for r in results:
                t = r["tier"]
                tier_counts[t] = tier_counts.get(t, 0) + 1
            summary_parts = [f"{tier_counts[t]} {TIER_LABELS.get(t, f'Tier {t}')}"
                             for t in sorted(tier_counts)]
            round_counts: dict[int, int] = {}
            for r in results:
                rd = r.get("expansion_round", 0)
                round_counts[rd] = round_counts.get(rd, 0) + 1
            header_text = f"Found {len(results)} matches: " + ", ".join(summary_parts)
            if len(round_counts) > 1:
                round_parts = [
                    f"{round_counts[rd]} from {'initial' if rd == 0 else f'round {rd}'}"
                    for rd in sorted(round_counts)
                ]
                header_text += f"<br>({', '.join(round_parts)})"

        self._results_header.setTextFormat(Qt.RichText)
        self._results_header.setText(header_text)
        self._results_header.setVisible(True)
        self._expand_btn.setVisible(not is_exact)  # expansion only meaningful in visual mode
        self._group_toggle.setVisible(True)
        self._table_toggle.setVisible(True)
        self._tag_all_btn.setVisible(True)
        self._report_btn.setVisible(True)
        # Switch from empty-state placeholder to the actual results splitter
        self._results_stack.setCurrentIndex(1)

        # For exact mode: table view by default (the sharing history reads as a list)
        if is_exact and not self._table_toggle.isChecked():
            self._table_toggle.setChecked(True)
        self._scroll.setVisible(not self._table_toggle.isChecked())
        self._results_table.setVisible(self._table_toggle.isChecked())
        # Reconfigure table columns for exact vs visual
        self._configure_table_columns(is_exact)
        if self._table_toggle.isChecked():
            self._fill_table(results)
            return

        cols = max(1, (self._scroll.viewport().width() - 20) // (ResultCard.CARD_SIZE.width() + 8))
        if self._group_by_conv:
            self._display_grouped(results, cols)
        else:
            self._display_flat(results, cols)

    def _configure_table_columns(self, is_exact: bool):
        """Set table columns based on match mode.

        Exact: Thumb | Direction | Conversation | Sender | Date | Filename | Size
        Visual: Thumb | Tier | Conversation | Sender | Date | Filename | pHash | dHash
        """
        from PySide6.QtWidgets import QHeaderView
        if is_exact:
            self._results_table.setColumnCount(7)
            self._results_table.setHorizontalHeaderLabels([
                "", "Dir", "Conversation", "Sender", "Date", "Filename", "Size"
            ])
        else:
            self._results_table.setColumnCount(8)
            self._results_table.setHorizontalHeaderLabels([
                "", "Tier", "Conversation", "Sender", "Date", "Filename", "pHash", "dHash"
            ])
        hdr = self._results_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self._results_table.setColumnWidth(0, 54)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)

    def _display_flat(self, results: list[dict], cols: int):
        for i, result in enumerate(results):
            card = self._make_card(result)
            row_idx = i // cols
            col_idx = i % cols
            self._results_grid.addWidget(card, row_idx, col_idx)

    def _display_grouped(self, results: list[dict], cols: int):
        """Display results grouped by conversation name."""
        groups: dict[str, list[dict]] = {}
        for r in results:
            key = r.get("conv_name", "") or "Unknown"
            groups.setdefault(key, []).append(r)

        # We insert group headers and grids into _results_layout
        # First, hide the main grid (it won't be used in grouped mode)
        grid_row = 0
        for conv_name, group_results in sorted(groups.items(), key=lambda x: -len(x[1])):
            # Group header label
            header = QLabel(f"{conv_name}  ({len(group_results)} matches)")
            header.setFont(QFont("Segoe UI", 10, QFont.Bold))
            header.setStyleSheet(f"color: {_accent_color()}; padding: 4px 0;")
            # Insert before the stretch
            insert_pos = self._results_layout.count() - 1
            self._results_layout.insertWidget(insert_pos, header)

            # Grid for this group
            group_grid = QGridLayout()
            group_grid.setSpacing(8)
            group_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            for i, result in enumerate(group_results):
                card = self._make_card(result)
                row_idx = i // cols
                col_idx = i % cols
                group_grid.addWidget(card, row_idx, col_idx)

            group_widget = QWidget()
            group_widget.setLayout(group_grid)
            insert_pos = self._results_layout.count() - 1
            self._results_layout.insertWidget(insert_pos, group_widget)

    def _make_card(self, result: dict) -> ResultCard:
        card = ResultCard(result, parent=self._results_container)
        card.clicked.connect(self._on_card_clicked)
        card.double_clicked.connect(self._on_card_double_clicked)
        self._result_cards.append(card)
        return card

    def _on_card_clicked(self, result: dict):
        """Single click: show preview panel."""
        # Deselect previous
        if self._selected_card:
            self._selected_card.set_selected(False)
        # Find and select the new card
        msg_id = result.get("message_id", 0)
        for card in self._result_cards:
            if card._msg_id == msg_id:
                card.set_selected(True)
                self._selected_card = card
                break
        self._preview_panel.show_result(result, self._last_results)

    def _on_card_double_clicked(self, conv_id: int, msg_id: int):
        """Navigate to the message in chat viewer."""
        if conv_id and msg_id:
            self.navigate_to_message.emit(conv_id, msg_id)

    # ------------------------------------------------------------------
    # Tag all results as forensic evidence
    # ------------------------------------------------------------------

    def _tag_all_results(self):
        if not self._last_results:
            return
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        default_name = f"similar_{datetime.now().strftime('%Y%m%d_%H%M')}"
        name, ok = QInputDialog.getText(
            self, "Tag all matches",
            "Tag name (will be created if new):",
            text=default_name,
        )
        if not ok or not name.strip():
            return
        tag_name = name.strip()

        try:
            from app.services.message_tag_service import MessageTagService
            svc = MessageTagService.instance()
        except Exception as e:
            QMessageBox.warning(self, "Tag service unavailable", str(e))
            return

        msg_ids = [r.get("message_id") for r in self._last_results if r.get("message_id")]
        try:
            tag_id = svc.ensure_tag(tag_name)
            added = svc.bulk_tag(tag_id, msg_ids)
        except Exception as e:
            QMessageBox.warning(self, "Tagging failed", str(e))
            return

        QMessageBox.information(
            self, "Tagged",
            f"Added tag '<b>{tag_name}</b>' to <b>{added}</b> of {len(msg_ids)} "
            f"matches. Open the <b>Tagged Messages</b> page to review.",
        )

    # ------------------------------------------------------------------
    # Export HTML report of all matches
    # ------------------------------------------------------------------

    def _export_report(self):
        """Pick an output FOLDER. Generates:
           <folder>/report.html      — forensic HTML with msgstore IDs
           <folder>/report.csv       — same data as spreadsheet
           <folder>/originals/       — all matched image files with WhatsApp-
                                       native filenames (IMG-YYYYMMDD-WAnnnn.jpg)
        Only msgstore/source IDs are exposed — no analysis.db internal ids.
        """
        if not self._last_results:
            return
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from pathlib import Path as _P
        default_dir = str(_P(self._db.path).parent / "reports") \
            if getattr(self, "_db", None) and self._db.path else str(_P.home())
        try:
            _P(default_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            default_dir = str(_P.home())

        out_folder = QFileDialog.getExistingDirectory(
            self, "Select an output folder for the report bundle",
            default_dir,
        )
        if not out_folder:
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bundle_dir = _P(out_folder) / f"image_report_{stamp}"
        try:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "originals").mkdir(exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Report failed",
                                f"Could not create folder:\n{e}")
            return

        try:
            enriched, n_copied = self._copy_originals(bundle_dir / "originals")
            self._write_similarity_report(
                str(bundle_dir / "report.html"), enriched, n_copied, bundle_dir,
            )
            self._write_similarity_csv(
                str(bundle_dir / "report.csv"), enriched,
            )
        except Exception as e:
            QMessageBox.warning(self, "Report failed", str(e))
            return

        QMessageBox.information(
            self, "Report saved",
            f"Report bundle written to:\n\n{bundle_dir}\n\n"
            f"\u2022 report.html  \u2014 forensic HTML report\n"
            f"\u2022 report.csv   \u2014 spreadsheet of every match\n"
            f"\u2022 originals/   \u2014 {n_copied} original image(s) "
            f"with WhatsApp filenames"
        )
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(bundle_dir)))

    def _copy_originals(self, target_dir):
        """Copy every match's image into target_dir using its WhatsApp-native
        filename (file_path basename). Returns (enriched_results, copied_count).

        Also enriches each result in memory with:
            source_msg_id, source_key_id, source_chat_id  (from msgstore)
            forensic_name  (bundled filename relative to bundle)
        """
        from pathlib import Path as _P
        import shutil
        db = self._db

        # Fetch msgstore IDs in one query to avoid N+1
        msg_ids = [r.get("message_id") for r in self._last_results if r.get("message_id")]
        source_map: dict[int, dict] = {}
        if msg_ids:
            try:
                placeholders = ",".join("?" * len(msg_ids))
                rows = db.fetchall(
                    f"SELECT m.id, m.source_msg_id, m.source_key_id, "
                    f"       c.source_chat_id "
                    f"FROM message m JOIN conversation c ON c.id = m.conversation_id "
                    f"WHERE m.id IN ({placeholders})",
                    tuple(msg_ids),
                )
                for r in rows:
                    source_map[r[0]] = {
                        "source_msg_id": r[1],
                        "source_key_id": r[2] or "",
                        "source_chat_id": r[3],
                    }
            except Exception:
                pass

        enriched = []
        used_names: dict[str, int] = {}   # de-dup suffix when same name reoccurs
        copied = 0
        for r in self._last_results:
            src = source_map.get(r.get("message_id"), {})
            data = dict(r)
            data["source_msg_id"] = src.get("source_msg_id")
            data["source_key_id"] = src.get("source_key_id", "")
            data["source_chat_id"] = src.get("source_chat_id")
            # Copy original file if available
            fp = data.get("file_path", "")
            if fp and os.path.isfile(fp):
                name = os.path.basename(fp)
                if not name:
                    name = f"msg_{src.get('source_msg_id') or 'x'}.bin"
                # Disambiguate
                base = name
                n = used_names.get(base, 0) + 1
                used_names[base] = n
                if n > 1:
                    stem, ext = os.path.splitext(base)
                    name = f"{stem}_{n-1}{ext}"
                dest = target_dir / name
                try:
                    if not dest.exists():
                        shutil.copy2(fp, dest)
                    data["forensic_name"] = f"originals/{name}"
                    copied += 1
                except Exception:
                    data["forensic_name"] = ""
            else:
                data["forensic_name"] = ""
            enriched.append(data)
        return enriched, copied

    def _write_similarity_csv(self, out_path, enriched):
        """CSV dump of the match set — one row per share instance.

        Two timestamp columns: ``timestamp_local`` in the
        analyst's selected timezone (header includes the IANA
        name + abbreviation), plus ``timestamp_utc`` for the
        canonical court-friendly value.  Including both removes
        any ambiguity when the CSV is shared between analysts in
        different timezones.
        """
        import csv
        from app.config import (
            format_timestamp, get_timezone_abbreviation,
            get_timezone_name, timestamp_to_utc_datetime,
        )
        tz_abbr = get_timezone_abbreviation() or "LOCAL"
        tz_name = get_timezone_name() or ""
        local_col_header = f"timestamp_local ({tz_abbr} · {tz_name})" if tz_name else f"timestamp_local ({tz_abbr})"

        # Resolve device-owner identity once so from_me=1 rows show the
        # actual saved owner name instead of an empty ``sender_name``.
        owner_name, owner_phone, owner_jid = "", "", ""
        try:
            info = {r[0]: r[1] for r in self._db.fetchall(
                "SELECT key, value FROM case_metadata WHERE key IN "
                "('device_owner_name','device_owner_phone','device_owner_jid')"
            )}
            owner_name = info.get("device_owner_name", "") or ""
            owner_phone = info.get("device_owner_phone", "") or ""
            owner_jid = info.get("device_owner_jid", "") or ""
        except Exception:
            pass
        owner_label = (
            owner_name + " (Owner)" if owner_name
            else ("You (Owner)" + (f" · +{owner_phone}" if owner_phone else ""))
        )

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "source_msg_id", "source_key_id", "source_chat_id",
                "conversation", "conversation_jid", "chat_type",
                "direction", "sender_name", "sender_phone", "sender_jid",
                local_col_header, "timestamp_utc",
                "match_tier", "phash_dist", "dhash_dist",
                "edge_dist", "file_size_bytes", "mime_type", "width", "height",
                "original_filename", "bundle_path",
            ])
            # Pre-cache conversation + contact detail
            conv_cache: dict[int, tuple] = {}
            contact_cache: dict[int, tuple] = {}
            db = self._db
            for r in enriched:
                cid = r.get("conversation_id")
                if cid and cid not in conv_cache:
                    try:
                        cr = db.fetchone(
                            "SELECT jid_raw_string, chat_type FROM conversation "
                            "WHERE id = ?", (cid,),
                        )
                        conv_cache[cid] = (cr[0] or "", cr[1] or "") if cr else ("", "")
                    except Exception:
                        conv_cache[cid] = ("", "")
                mid = r.get("message_id")
                if mid and mid not in contact_cache:
                    try:
                        cr = db.fetchone(
                            "SELECT c.phone_number, c.phone_jid, c.lid_jid, "
                            "       me.file_size, me.mime_type, me.width, me.height "
                            "FROM message m "
                            "LEFT JOIN contact c ON c.id = m.sender_id "
                            "LEFT JOIN media me ON me.message_id = m.id "
                            "WHERE m.id = ?", (mid,),
                        )
                        contact_cache[mid] = cr if cr else ("",)*7
                    except Exception:
                        contact_cache[mid] = ("",)*7

                conv_jid, ctype = conv_cache.get(cid, ("", ""))
                c = contact_cache.get(mid, ("",)*7)
                phone, pjid, lid, fsize, mime, cw, ch = c
                tier = r.get("tier", 0)
                tier_label = ("EXACT (SHA-256)" if r.get("match_mode") == "exact" or tier == 0
                              else TIER_LABELS.get(tier, f"Tier {tier}"))
                ts_local = ""
                ts_utc = ""
                if r.get("timestamp"):
                    try:
                        ts_local = format_timestamp(r["timestamp"], "datetime")
                        utc_dt = timestamp_to_utc_datetime(r["timestamp"])
                        ts_utc = utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                    except Exception:
                        pass

                # sender_name / phone / jid fallbacks for owner-sent
                # rows (the engine returns empty for from_me=1 because
                # WhatsApp leaves message.sender_id NULL on outgoing
                # messages).  Same for groups where the sender contact
                # row didn't resolve.
                from_me = bool(r.get("from_me"))
                eng_sender = r.get("sender_name", "")
                if from_me and not eng_sender:
                    eng_sender = owner_label
                # Same for orphan rows that don't have a real msg-side
                # contact; the engine sets a generic label for orphans
                # already, but if it's empty, leave blank.
                out_phone = "+" + phone if phone else ""
                out_jid = pjid or lid or ""
                if from_me:
                    if not out_phone and owner_phone:
                        out_phone = "+" + owner_phone.lstrip("+")
                    if not out_jid and owner_jid:
                        out_jid = owner_jid
                w.writerow([
                    r.get("source_msg_id", ""), r.get("source_key_id", ""),
                    r.get("source_chat_id", ""),
                    r.get("conv_name", ""), conv_jid, ctype,
                    "Sent" if from_me else "Received",
                    eng_sender, out_phone, out_jid,
                    ts_local, ts_utc, tier_label,
                    r.get("phash_dist", ""), r.get("dhash_dist", ""),
                    r.get("edge_dist", ""),
                    fsize or "", mime or "", cw or "", ch or "",
                    os.path.basename(r.get("file_path", "")) if r.get("file_path") else "",
                    r.get("forensic_name", ""),
                ])

    def _write_similarity_report(self, out_path, results=None, n_copied=0, bundle_dir=None):
        """Emit a standalone HTML report.

        Forensic IDs are ONLY msgstore-derived (source_msg_id, source_key_id,
        source_chat_id) — no analysis.db internal ids surface in the output.
        """
        import html as _h
        from pathlib import Path as _P
        from app.config import (
            format_timestamp, get_timezone_abbreviation,
            get_timezone_name, timestamp_to_utc_datetime,
        )
        _tz_abbr = get_timezone_abbreviation() or "LOCAL"
        _tz_name = get_timezone_name() or ""
        _tz_caption = f"{_tz_abbr} ({_tz_name})" if _tz_name else _tz_abbr

        db = self._db
        exporter_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        case_id, examiner, owner_name, owner_phone, owner_jid = "", "", "", "", ""
        try:
            info = {r[0]: r[1] for r in db.fetchall(
                "SELECT key, value FROM case_metadata WHERE key IN "
                "('case_id','examiner','device_owner_name','device_owner_phone','device_owner_jid')"
            )}
            case_id = info.get("case_id", "") or ""
            examiner = info.get("examiner", "") or ""
            owner_name = info.get("device_owner_name", "") or ""
            owner_phone = info.get("device_owner_phone", "") or ""
            owner_jid = info.get("device_owner_jid", "") or ""
        except Exception:
            pass

        if results is None:
            results = self._last_results
        is_exact = all(r.get("match_mode") == "exact" for r in results)

        # Group by conversation
        by_conv: dict[int, list] = {}
        for r in results:
            cid = r.get("conversation_id", 0)
            by_conv.setdefault(cid, []).append(r)

        def _thumb_ref(r: dict) -> str:
            """Prefer a relative path to the copied original (portable bundle),
            fall back to embedded base64 thumb."""
            if r.get("forensic_name"):
                return r["forensic_name"]
            fp = r.get("file_path", "")
            if fp and os.path.isfile(fp):
                try:
                    data = _P(fp).read_bytes()
                    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                except Exception:
                    pass
            if r.get("thumb"):
                return "data:image/jpeg;base64," + r["thumb"]
            return ""

        def _row_details(r: dict) -> str:
            msg_id = r.get("message_id", 0)
            sender = r.get("sender_name", "")
            from_me = bool(r.get("from_me"))
            ts = r.get("timestamp", 0)
            ts_local_str = ""
            ts_utc_str = ""
            if ts:
                try:
                    ts_local_str = format_timestamp(ts, "datetime")
                    utc_dt = timestamp_to_utc_datetime(ts)
                    ts_utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    pass
            # Sender JID / phone (includes owner fallback)
            jid_bits = ""
            try:
                jr = db.fetchone(
                    "SELECT c.phone_number, c.phone_jid, c.lid_jid, m.from_me "
                    "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
                    "WHERE m.id = ?", (msg_id,)
                )
                if jr:
                    phone, pjid, lid, _from_me_db = jr
                    # Engine result's from_me is authoritative (already
                    # had it before this query).  Use it for orphan
                    # rows where m.id might not exist.
                    fm_eff = from_me if from_me is not None else bool(_from_me_db)
                    bits = []
                    if phone: bits.append("+" + phone)
                    if pjid:  bits.append(pjid)
                    elif lid: bits.append(lid)
                    if fm_eff:
                        if not bits and owner_phone:
                            bits.append("+" + owner_phone.lstrip("+"))
                        if not any("@" in b for b in bits) and owner_jid:
                            bits.append(owner_jid)
                        owner_label = (owner_name + " (Owner)") if owner_name else "You (Owner)"
                        jid_bits = owner_label + (" · " + " · ".join(bits) if bits else "")
                    else:
                        jid_bits = " · ".join(bits)
            except Exception:
                pass

            # Owner fallback for the visible Sender line — when from_me=1
            # and the engine left sender_name empty (sender_id is NULL on
            # outgoing rows), show the saved owner name so the analyst
            # doesn't see a blank cell.
            display_sender = sender
            if from_me and not display_sender:
                display_sender = (
                    owner_name + " (Owner)" if owner_name
                    else "You (Owner)" + (f" · +{owner_phone}" if owner_phone else "")
                )

            tier_bit = "EXACT (SHA-256)" if (r.get("match_mode") == "exact" or r.get("tier") == 0) \
                else TIER_LABELS.get(r.get("tier", 0), f"Tier {r.get('tier',0)}")
            # Orphan rows render an extra hint
            orphan_pill = ""
            if r.get("is_orphan"):
                orphan_pill = (' <span class="badge" style="background:#e65100">'
                               'ORPHAN (no chat record)</span>')
            dir_bit = "↑ Sent" if from_me else "↓ Recv"
            fp = r.get("file_path", "") or ""

            # Use ONLY msgstore IDs in the visible forensic record — no
            # analysis.db internal message_id leaks into the report.
            src_mid = r.get("source_msg_id")
            src_key = r.get("source_key_id", "")
            src_cid = r.get("source_chat_id")
            id_parts = []
            if src_mid is not None:
                id_parts.append(f'msgstore._id: <code>{src_mid}</code>')
            if src_key:
                id_parts.append(f'key_id: <code>{_h.escape(src_key)}</code>')
            if src_cid is not None:
                id_parts.append(f'chat_row_id: <code>{src_cid}</code>')

            return (
                f'<div class="meta">'
                f'<span class="badge">{_h.escape(tier_bit)}</span>{orphan_pill} '
                f'<span class="dir {"sent" if from_me else "recv"}">{dir_bit}</span> '
                f'<span class="ts">{_h.escape(ts_local_str)} {_h.escape(_tz_abbr)}</span>'
                + (f' <span class="iso">[{_h.escape(ts_utc_str)}]</span>' if ts_utc_str else "")
                + f'<br><b>Sender:</b> {_h.escape(display_sender or "—")}'
                + (f' &middot; <code>{_h.escape(jid_bits)}</code>' if jid_bits else "")
                + (f'<br><b>Identifiers:</b> {" &middot; ".join(id_parts)}' if id_parts else "")
                + (f'<br><b>Original filename:</b> <code>'
                   f'{_h.escape(os.path.basename(fp))}</code>' if fp else "")
                + (f'<br><b>Saved as:</b> <code>{_h.escape(r.get("forensic_name",""))}</code>'
                   if r.get("forensic_name") else "")
                + "</div>"
            )

        cards_html = []
        for cid, grp in sorted(by_conv.items(), key=lambda x: -len(x[1])):
            conv_name = grp[0].get("conv_name", f"Conv #{cid}")
            conv_jid = ""
            try:
                row = db.fetchone(
                    "SELECT jid_raw_string FROM conversation WHERE id = ?", (cid,)
                )
                if row:
                    conv_jid = row[0] or ""
            except Exception:
                pass
            cards_html.append(
                f'<h2 class="conv-header">\U0001F4AC {_h.escape(conv_name)}'
                + (f' <small><code>{_h.escape(conv_jid)}</code></small>' if conv_jid else "")
                + f' &middot; {len(grp)} match{"es" if len(grp) != 1 else ""}</h2>'
            )
            for r in grp:
                ref = _thumb_ref(r)
                if ref:
                    img_html = (
                        '<div class="imgwrap"><a href="' + ref +
                        '" target="_blank" title="Open full-size in new tab">'
                        '<img src="' + ref + '" alt="evidence"></a></div>'
                    )
                else:
                    img_html = '<div class="noimg">No preview</div>'
                cards_html.append(
                    f'<div class="card">{img_html}{_row_details(r)}</div>'
                )

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Image Similarity Report</title>
<style>
  body {{ background:#f6f7f8; color:#111; font-family:-apple-system,'Segoe UI',
         Roboto,Arial,sans-serif; margin:0; padding:20px 30px; font-size:13px; }}
  h1 {{ color:#00695c; margin:0 0 4px 0; font-size:22px; }}
  .sub {{ color:#667781; font-size:12px; margin-bottom:18px; }}
  .case {{ background:#fff; border:1px solid #dee2e6; border-radius:6px;
          padding:10px 14px; margin-bottom:14px; font-size:12px; }}
  .case b {{ color:#00695c; }}
  h2.conv-header {{ color:#00695c; border-bottom:1px solid #cfd8dc;
          padding:8px 0 4px; margin:18px 0 10px; font-size:15px; }}
  h2 small {{ color:#90a4ae; font-weight:400; font-size:11px; }}
  .card {{ background:#fff; border:1px solid #e0e3e7; border-radius:8px;
           padding:10px; margin:8px 0; display:flex; gap:14px; align-items:flex-start; }}
  /* Image cell: bigger fixed box, contained (NOT cropped).  Earlier
     this used object-fit:cover at 160x140 which trimmed tall
     screenshots to a square crop — useless for evidence.  Now the
     full image is visible, letterboxed inside a 220x200 box; click
     to open in a new tab at full resolution.                       */
  .card .imgwrap {{ width:220px; min-width:220px; height:200px;
                    background:#f4f6f8; border:1px solid #cfd8dc;
                    border-radius:6px; display:flex; align-items:center;
                    justify-content:center; overflow:hidden; padding:4px; }}
  .card .imgwrap a {{ display:flex; width:100%; height:100%;
                      align-items:center; justify-content:center; }}
  .card img {{ max-width:100%; max-height:100%; width:auto; height:auto;
               object-fit:contain; border-radius:4px; }}
  .card .noimg {{ width:220px; height:200px; background:#eceff1; color:#90a4ae;
          display:flex;align-items:center;justify-content:center;border-radius:6px; }}
  .card .meta {{ flex:1; line-height:1.6; word-break:break-word; }}
  .badge {{ background:#4caf50; color:#fff; padding:2px 8px; border-radius:10px;
           font-weight:700; font-size:11px; }}
  .dir.sent {{ color:#2e7d32; font-weight:700; }}
  .dir.recv {{ color:#1976d2; font-weight:700; }}
  .ts {{ color:#455a64; margin-left:6px; font-weight:600; }}
  .iso {{ color:#90a4ae; font-size:10.5px; margin-left:6px; font-family:Consolas,monospace; }}
  code {{ background:#eceff1; padding:1px 5px; border-radius:3px;
          font-family:Consolas,monospace; font-size:11px; }}
  .tz-note {{ color:#1565c0; font-size:11px; }}
</style></head><body>
<h1>Image Similarity Report</h1>
<div class="sub">Generated {exporter_date}
  &middot; {len(results)} match{"es" if len(results) != 1 else ""} across {len(by_conv)} conversation(s)
  &middot; {'Exact SHA-256' if is_exact else 'Perceptual'}
  <br><span class="tz-note">All timestamps shown in <b>{_h.escape(_tz_caption)}</b>; UTC equivalent in [brackets].</span>
</div>
<div class="case">
  <b>Case ID:</b> {_h.escape(case_id or '—')} &nbsp;
  <b>Examiner:</b> {_h.escape(examiner or '—')} &nbsp;
  <b>Owner:</b> {_h.escape(owner_name or '—')}{(' &middot; +' + _h.escape(owner_phone)) if owner_phone else ''}<br>
  <b>Analysis DB:</b> <code>{_h.escape(str(db.path) if db else '')}</code>
</div>
{''.join(cards_html)}
<div style="margin-top:24px;color:#90a4ae;font-size:10px;text-align:center">
  Generated by WAInsight \u2014 WhatsApp Forensic Suite
</div>
</body></html>"""
        _P(out_path).write_text(html, encoding="utf-8")

    # ------------------------------------------------------------------
    # Drag-and-drop support
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path and os.path.isfile(path):
                self._on_file_selected(path)


# ---------------------------------------------------------------------------
# Drop zone widget
# ---------------------------------------------------------------------------

class DropZone(QFrame):
    """A styled drop-target area for selecting a query image."""

    file_dropped = Signal(str)  # file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(50)
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
            DropZone:hover {
                border-color: #2196f3;
                background: palette(window);
            }
        """)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        self._label = QLabel("Drag & drop an image here, or click Browse")
        self._label.setFont(QFont("Segoe UI", 9))
        self._label.setStyleSheet("color: palette(mid); border: none;")
        self._label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._label)

    def set_file_name(self, name: str):
        self._label.setText(f"Selected: {name}")
        self._label.setStyleSheet("color: #4caf50; border: none;")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("""
                DropZone {
                    border: 2px dashed #4caf50;
                    border-radius: 8px;
                    background: palette(window);
                }
            """)

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
            DropZone:hover {
                border-color: #2196f3;
                background: palette(window);
            }
        """)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed palette(mid);
                border-radius: 8px;
                background: palette(base);
            }
        """)
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path and os.path.isfile(path):
                self.file_dropped.emit(path)

    def refresh_for_timezone_change(self) -> None:
        """Reload after a global timezone change so cached
        formatted timestamps re-render in the new tz."""
        try:
            if hasattr(self, "_refresh_status") and callable(getattr(self, "_refresh_status")):
                self._refresh_status()
        except Exception:
            pass

