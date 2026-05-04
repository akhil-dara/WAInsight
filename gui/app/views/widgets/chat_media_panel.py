"""
In-chat media gallery panel -- shows all media for the current conversation,
split by sender. Supports type filtering, thumbnail grid, click-to-navigate,
media preview popup, and download status indicators.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex, QObject, QPoint, QPointF, QRect, QRectF, QRunnable,
    QSize, QSizeF, Qt, QThread, QThreadPool, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QImage,
    QImageReader, QKeySequence, QMovie, QPainter, QPainterPath,
    QPixmap, QShortcut, QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFrame, QLineEdit,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListView, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSplitter, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from app.services.database import Database
from app.services.theme_manager import ThemeManager

logger = logging.getLogger(__name__)


class _MediaItem:
    """Lightweight container for a media record in the gallery.
    thumbnail_blob, media_url and media_key are NOT stored here — loaded lazily."""
    __slots__ = (
        "media_id", "message_id", "has_thumb", "mime_type", "file_size",
        "has_url", "file_exists", "has_key", "file_hash",
        "resolved_path", "from_me", "timestamp", "type_label",
        "sender_name", "sender_id", "media_name", "_status",
    )

    def __init__(self, row):
        self.media_id = row[0]
        self.has_thumb = bool(row[1])  # flag only, blob loaded lazily
        self.mime_type = row[2] or ""
        self.file_size = row[3] or 0
        self.has_url = bool(row[4])     # flag only, URL fetched on demand
        self.file_exists = bool(row[5]) if row[5] else False
        self.has_key = bool(row[6])     # flag only, key fetched on demand
        self.file_hash = row[7] or ""
        self.resolved_path = row[8] or ""
        self.message_id = row[9]
        self.from_me = bool(row[10])
        self.timestamp = row[11]
        self.type_label = row[12] or ""
        self.sender_name = row[13] or "Unknown"
        self.sender_id = row[14]
        self.media_name = row[15] or ""
        # Pre-compute status
        if self.file_exists:
            self._status = "on_disk"
        elif self.has_url and self.has_key:
            self._status = "downloadable"
        elif self.has_url:
            self._status = "expired"
        else:
            self._status = "missing"

    @property
    def status(self) -> str:
        # Re-check actual file existence at access time
        if self._status == "on_disk":
            if not (self.resolved_path and os.path.isfile(self.resolved_path)):
                # DB says on_disk but file not found — downgrade
                if self.has_url and self.has_key:
                    return "downloadable"
                elif self.has_url:
                    return "expired"
                return "missing"
        return self._status

    @property
    def media_url(self) -> str:
        """Lazy-load media_url from DB."""
        return _load_media_field(self.media_id, "media_url") or ""

    @property
    def media_key(self) -> bytes | None:
        """Lazy-load media_key from DB."""
        return _load_media_field(self.media_id, "media_key")

    @property
    def type_category(self) -> str:
        if self.type_label in ("image", "sticker"):
            return "image"
        if self.type_label in ("video", "gif", "animated_gif"):
            return "video"
        if self.type_label in ("audio", "voice", "ptt"):
            return "audio"
        if self.type_label == "document":
            return "document"
        # Fallback to MIME
        if self.mime_type.startswith("image/"):
            return "image"
        if self.mime_type.startswith("video/"):
            return "video"
        if self.mime_type.startswith("audio/"):
            return "audio"
        return "document"

    @property
    def display_name(self) -> str:
        if self.media_name:
            return self.media_name
        if self.resolved_path:
            return os.path.basename(self.resolved_path)
        return ""


def _load_thumb_blob(media_id: int) -> bytes | None:
    """Lazy-load a single thumbnail blob by media ID."""
    try:
        db = Database.get()
        return db.scalar("SELECT thumbnail_blob FROM media WHERE id = ?", (media_id,))
    except Exception:
        return None


def _load_media_field(media_id: int, field: str):
    """Lazy-load a single field (media_url or media_key) from media table."""
    try:
        db = Database.get()
        return db.scalar(f"SELECT {field} FROM media WHERE id = ?", (media_id,))
    except Exception:
        return None


def _is_light() -> bool:
    """Quick check if using light theme."""
    try:
        return ThemeManager.get().is_light
    except Exception:
        return False


def _theme_colors():
    """Return a dict of themed color strings for dialogs."""
    lt = _is_light()
    return {
        "bg": "#fafafa" if lt else "#111b21",
        "text": "#111b21" if lt else "#e9edef",
        "dim": "#667781" if lt else "rgba(255,255,255,0.5)",
        "dim2": "#8696a0" if lt else "rgba(255,255,255,0.35)",
        "accent": "#009688" if lt else "#00bcd4",
        "accent_bg": "rgba(0,150,136,0.15)" if lt else "rgba(0,188,212,0.2)",
        "accent_border": "#009688" if lt else "#00bcd4",
        "panel_bg": "#f0f2f5" if lt else "rgba(255,255,255,0.03)",
        "card_bg": "#ffffff" if lt else "#1a2730",
        "border": "#e0e3e7" if lt else "rgba(255,255,255,0.08)",
        "hover": "rgba(0,0,0,0.04)" if lt else "rgba(255,255,255,0.06)",
        "hover2": "rgba(0,0,0,0.08)" if lt else "rgba(255,255,255,0.12)",
        "bar_bg": "#0b141a" if not lt else "rgba(0,0,0,0.65)",
        "selected": "rgba(0,150,136,0.12)" if lt else "rgba(0,188,212,0.15)",
        "grid_line": "rgba(0,0,0,0.06)" if lt else "rgba(255,255,255,0.05)",
    }


class _GalleryLoadWorker(QThread):
    """Loads media items for a conversation in a background thread."""
    finished = Signal(list)  # list of _MediaItem

    def __init__(self, conv_id: int, parent=None):
        super().__init__(parent)
        self._conv_id = conv_id

    def run(self):
        try:
            db = Database.get()
            rows = db.fetchall("""
                SELECT me.id,
                       CASE WHEN me.thumbnail_blob IS NOT NULL AND LENGTH(me.thumbnail_blob) > 50
                            THEN 1 ELSE NULL END,
                       me.mime_type, me.file_size,
                       CASE WHEN me.media_url IS NOT NULL AND me.media_url != ''
                            THEN 1 ELSE NULL END,
                       me.file_exists,
                       CASE WHEN me.media_key IS NOT NULL
                            THEN 1 ELSE NULL END,
                       me.file_hash,
                       me.resolved_file_path,
                       m.id, m.from_me, m.timestamp, m.type_label,
                       COALESCE(
                           CASE WHEN m.from_me = 1 THEN 'You' END,
                           c.resolved_name, c.wa_name, c.phone_number, m.rendered_sender, 'Unknown'
                       ) AS sender_name,
                       m.sender_id,
                       me.media_name
                FROM media me
                JOIN message m ON m.id = me.message_id
                LEFT JOIN contact c ON c.id = m.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.timestamp DESC
            """, (self._conv_id,))
            items = [_MediaItem(r) for r in rows]
        except Exception as exc:
            logger.exception("Gallery load failed: %s", exc)
            items = []
        self.finished.emit(items)


def _fmt_size(b) -> str:
    if not b or b <= 0:
        return ""
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


class _GalleryTileDelegate(QStyledItemDelegate):
    """Paints small thumbnails in the chat media panel grid.

    Thumbnail priority: disk file (images) > thumbnail_blob > emoji.
    """

    TILE = 116

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: dict[int, QPixmap | None] = {}
        try:
            from app.services.theme_manager import ThemeManager
            self._lt = ThemeManager.get().is_light
        except Exception:
            self._lt = False
        self._bg = QColor(235, 238, 242) if self._lt else QColor(18, 28, 33)
        self._icon_col = QColor(155, 165, 175) if self._lt else QColor(100, 120, 130)
        self._bar_bg = QColor(18, 24, 30, 165) if self._lt else QColor(0, 0, 0, 170)
        self._bar_text = QColor(235, 240, 245) if self._lt else QColor(200, 210, 215)

    def sizeHint(self, option, index):
        return QSize(self.TILE, self.TILE)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = option.rect
        item: _MediaItem | None = index.data(Qt.UserRole + 10)
        if not item:
            painter.restore()
            return

        from PySide6.QtWidgets import QStyle
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(0, 188, 212, 50))
        else:
            painter.fillRect(rect.adjusted(1, 1, -1, -1), self._bg)

        outer = rect.adjusted(2, 2, -2, -2)
        card = QPainterPath()
        card.addRoundedRect(QRectF(outer), 8, 8)
        painter.fillPath(card, QColor(255, 255, 255, 235) if self._lt else QColor(24, 35, 42, 235))
        painter.setPen(QColor(0, 150, 136, 120) if option.state & QStyle.StateFlag.State_Selected else QColor(0, 0, 0, 18 if self._lt else 0))
        painter.drawPath(card)

        img_area = rect.adjusted(6, 6, -6, -34)

        # Thumbnail: priority disk file (images) > blob > emoji
        drawn = False

        # 1. Try actual file from disk for image types
        if not drawn and item.file_exists and item.resolved_path and item.type_category == "image":
            pxm = self._get_file_thumb(item.media_id, item.resolved_path)
            if pxm and not pxm.isNull():
                drawn = self._draw_thumb(painter, pxm, img_area)

        # 2. Try thumbnail blob (lazy-loaded from DB)
        if not drawn and item.has_thumb:
            blob = self._fetch_thumb_blob(item.media_id)
            if blob:
                pxm = self._get_thumb(item.media_id, blob)
                if pxm and not pxm.isNull():
                    drawn = self._draw_thumb(painter, pxm, img_area)

        if not drawn:
            icons = {
                "image": "\u25A3", "video": "\u25B6",
                "audio": "\u266B", "document": "\U0001F4C4",
            }
            icon = icons.get(item.type_category, "\U0001F4C4")
            painter.setFont(QFont("Segoe UI", 18))
            painter.setPen(self._icon_col)
            icon_rect = QRect(rect.x(), rect.y(), rect.width(), rect.height() - 18)
            painter.drawText(icon_rect, Qt.AlignCenter, icon)

            # Document subtype badge for clearer PDF/doc identification.
            if item.type_category == "document":
                subtype = ""
                if item.mime_type == "application/pdf":
                    subtype = "PDF"
                elif item.mime_type:
                    subtype = item.mime_type.split("/")[-1].upper()[:4]
                elif item.display_name:
                    ext = os.path.splitext(item.display_name)[1].lower()
                    subtype = ext[1:].upper()[:4] if ext else ""
                if subtype:
                    badge = QRect(rect.x() + 6, rect.y() + 6, 34, 14)
                    painter.setBrush(QColor(210, 50, 50, 220) if subtype == "PDF" else QColor(90, 110, 140, 220))
                    painter.setPen(Qt.NoPen)
                    painter.drawRoundedRect(badge, 6, 6)
                    painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                    painter.setPen(QColor("white"))
                    painter.drawText(badge, Qt.AlignCenter, subtype)

        # Video play overlay
        if item.type_category == "video":
            painter.setFont(QFont("Segoe UI", 14, QFont.Bold))
            painter.setPen(QColor(255, 255, 255, 180))
            painter.drawText(
                QRect(rect.x(), rect.y(), rect.width(), rect.height() - 18),
                Qt.AlignCenter, "\u25B6",
            )

        # Bottom bar
        bar_h = 28
        bar_y = rect.bottom() - bar_h - 2
        bar_rect = QRect(rect.x() + 3, bar_y, rect.width() - 6, bar_h)
        painter.fillRect(bar_rect, self._bar_bg)

        # Status dot
        status = item.status
        colors = {
            "on_disk": QColor(80, 200, 80),
            "downloadable": QColor(80, 180, 255),
            "expired": QColor(200, 100, 50),
            "missing": QColor(150, 60, 60),
        }
        painter.setBrush(colors.get(status, QColor(100, 100, 100)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect.x() + 8, bar_y + 6, 6, 6)

        # Primary line: date + direction
        painter.setFont(QFont("Segoe UI", 7))
        painter.setPen(self._bar_text)
        arrow = "You" if item.from_me else "From"
        ts_str = ""
        if item.timestamp:
            try:
                ts_str = format_timestamp(item.timestamp, '%d %b')
            except (ValueError, OSError):
                pass
        text = f"{arrow}  {ts_str}".strip()
        painter.drawText(
            QRect(rect.x() + 18, bar_y + 1, rect.width() - 24, 12),
            Qt.AlignLeft | Qt.AlignVCenter, text,
        )

        # Secondary line: sender/file snippet
        detail = item.sender_name if not item.from_me else item.display_name or "Outgoing media"
        if item.type_category == "document" and item.display_name:
            detail = item.display_name
        painter.setFont(QFont("Segoe UI", 7, QFont.Medium))
        painter.drawText(
            QRect(rect.x() + 8, bar_y + 13, rect.width() - 16, 12),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(QFont("Segoe UI", 7, QFont.Medium)).elidedText(detail, Qt.ElideRight, rect.width() - 18),
        )

        painter.restore()

    def _draw_thumb(self, painter: QPainter, pxm: QPixmap, img_area: QRect) -> bool:
        """Scale and draw a pixmap centered in img_area. Caches scaled result."""
        rw, rh = img_area.width(), img_area.height()
        scale_key = ("scaled", pxm.cacheKey(), rw, rh)
        cached = self._cache.get(scale_key)
        if cached is None:
            cached = pxm.scaled(rw, rh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._cache[scale_key] = cached
            self._evict_cache()
        px = img_area.x() + (rw - cached.width()) // 2
        py = img_area.y() + (rh - cached.height()) // 2
        clip = QPainterPath()
        clip.addRoundedRect(float(px), float(py),
                            float(cached.width()), float(cached.height()), 3, 3)
        painter.setClipPath(clip)
        painter.drawPixmap(px, py, cached)
        painter.setClipping(False)
        return True

    def _fetch_thumb_blob(self, media_id: int) -> bytes | None:
        """Lazy-load thumbnail blob from DB on demand (avoids loading all blobs upfront)."""
        cache_key = ("blob", media_id)
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            db = Database.get()
            blob = db.scalar(
                "SELECT thumbnail_blob FROM media WHERE id = ?", (media_id,)
            )
        except Exception:
            blob = None
        self._cache[cache_key] = blob
        self._evict_cache()
        return blob

    def _get_thumb(self, media_id: int, blob: bytes) -> QPixmap | None:
        if media_id in self._cache:
            return self._cache[media_id]
        pxm = QPixmap()
        pxm.loadFromData(blob)
        if pxm.isNull():
            self._cache[media_id] = None
            return None
        self._cache[media_id] = pxm
        self._evict_cache()
        return pxm

    def _get_file_thumb(self, media_id: int, path: str) -> QPixmap | None:
        """Load thumbnail from actual image file on disk."""
        cache_key = media_id + 1_000_000_000
        if cache_key in self._cache:
            return self._cache[cache_key]
        if not path or not os.path.isfile(path):
            self._cache[cache_key] = None
            return None
        pxm = QPixmap()
        try:
            pxm.load(path)
        except Exception:
            self._cache[cache_key] = None
            return None
        if pxm.isNull():
            self._cache[cache_key] = None
            return None
        self._cache[cache_key] = pxm
        self._evict_cache()
        return pxm

    def _evict_cache(self):
        while len(self._cache) > 4000:
            oldest = next(iter(self._cache))
            del self._cache[oldest]

    def clear_cache(self):
        self._cache.clear()


class _GalleryListModel:
    """Not a QAbstractModel — we use a simpler approach with QListView + manual item management."""
    pass


# ---------------------------------------------------------------------------
# Zoomable Image Viewer Widget
# ---------------------------------------------------------------------------

class _ZoomableImageWidget(QWidget):
    """Custom widget supporting mouse-wheel zoom, click-drag pan, and
    double-click reset-to-fit for image viewing."""

    MIN_ZOOM = 0.2
    MAX_ZOOM = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom: float = 1.0
        self._offset = QPointF(0, 0)
        self._dragging = False
        self._drag_start = QPointF()
        self._offset_start = QPointF()
        self._lt = _is_light()
        self._bg_color = QColor("#f0f2f5") if self._lt else QColor(11, 20, 26)
        self._text_color = QColor("#667781") if self._lt else QColor(170, 170, 170)
        self._zoom_text_color = QColor(0, 0, 0, 140) if self._lt else QColor(255, 255, 255, 140)
        self.setMinimumSize(200, 200)
        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)

    # -- public API --

    def set_pixmap(self, pxm: QPixmap | None):
        self._pixmap = pxm
        self._fit_zoom()
        self.update()

    def pixmap(self) -> QPixmap | None:
        return self._pixmap

    def zoom_factor(self) -> float:
        return self._zoom

    def clear(self):
        self._pixmap = None
        self.update()

    # -- internal helpers --

    def _fit_zoom(self):
        """Compute zoom factor so the image fits entirely within the widget."""
        if not self._pixmap or self._pixmap.isNull():
            self._zoom = 1.0
            self._offset = QPointF(0, 0)
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        ww, wh = self.width() - 8, self.height() - 8
        if ww < 1 or wh < 1:
            ww, wh = 600, 400
        zx = ww / max(pw, 1)
        zy = wh / max(ph, 1)
        self._zoom = min(zx, zy, 1.0)  # don't upscale past 100%
        self._zoom = max(self._zoom, self.MIN_ZOOM)
        self._offset = QPointF(0, 0)

    def _clamp_offset(self):
        if not self._pixmap:
            return
        sw = self._pixmap.width() * self._zoom
        sh = self._pixmap.height() * self._zoom
        max_ox = max((sw - self.width()) / 2, 0)
        max_oy = max((sh - self.height()) / 2, 0)
        ox = max(-max_ox, min(self._offset.x(), max_ox))
        oy = max(-max_oy, min(self._offset.y(), max_oy))
        self._offset = QPointF(ox, oy)

    # -- Qt events --

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), self._bg_color)

        if not self._pixmap or self._pixmap.isNull():
            painter.setPen(self._text_color)
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignCenter, "No preview available")
            painter.end()
            return

        pw = self._pixmap.width() * self._zoom
        ph = self._pixmap.height() * self._zoom
        cx = (self.width() - pw) / 2 + self._offset.x()
        cy = (self.height() - ph) / 2 + self._offset.y()

        target = QRectF(cx, cy, pw, ph)
        source = QRectF(0, 0, self._pixmap.width(), self._pixmap.height())
        painter.drawPixmap(target, self._pixmap, source)

        # Zoom indicator
        painter.setPen(self._zoom_text_color)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRect(self.width() - 70, 6, 64, 18),
            Qt.AlignRight | Qt.AlignTop,
            f"{int(self._zoom * 100)}%",
        )
        painter.end()

    def wheelEvent(self, event: QWheelEvent):
        if not self._pixmap:
            return
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_zoom = self._zoom * factor
        new_zoom = max(self.MIN_ZOOM, min(new_zoom, self.MAX_ZOOM))
        if new_zoom != self._zoom:
            self._zoom = new_zoom
            self._clamp_offset()
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start = event.position()
            self._offset_start = QPointF(self._offset)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging:
            delta = event.position() - self._drag_start
            self._offset = self._offset_start + delta
            self._clamp_offset()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, event):
        self._fit_zoom()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap and not self._pixmap.isNull():
            # Re-fit only if not user-zoomed significantly
            self._clamp_offset()
            self.update()


# ---------------------------------------------------------------------------
# EXIF metadata extraction helpers
# ---------------------------------------------------------------------------

def _extract_exif(file_path: str) -> dict[str, str]:
    """Try to extract EXIF metadata using Pillow. Falls back to basic info."""
    info: dict[str, str] = {}
    if not file_path or not os.path.isfile(file_path):
        return info

    try:
        from PIL import Image as PILImage
        from PIL.ExifTags import TAGS, GPSTAGS

        pil_img = PILImage.open(file_path)
        info["Resolution"] = f"{pil_img.width} x {pil_img.height}"
        info["Format"] = pil_img.format or "Unknown"
        info["Mode"] = pil_img.mode

        exif_data = pil_img.getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                if tag_name == "Make":
                    info["Camera Make"] = str(value).strip()
                elif tag_name == "Model":
                    info["Camera Model"] = str(value).strip()
                elif tag_name == "DateTime":
                    info["Date Taken"] = str(value)
                elif tag_name == "DateTimeOriginal":
                    info["Date Taken (Original)"] = str(value)
                elif tag_name == "ExposureTime":
                    info["Exposure Time"] = str(value)
                elif tag_name == "FNumber":
                    info["F-Number"] = str(value)
                elif tag_name == "ISOSpeedRatings":
                    info["ISO"] = str(value)
                elif tag_name == "FocalLength":
                    info["Focal Length"] = str(value)
                elif tag_name == "ImageWidth":
                    info["EXIF Width"] = str(value)
                elif tag_name == "ImageLength":
                    info["EXIF Height"] = str(value)
                elif tag_name == "Software":
                    info["Software"] = str(value)

            # GPS
            gps_info = exif_data.get_ifd(0x8825)
            if gps_info:
                def _dms_to_dd(dms, ref):
                    d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
                    dd = d + m / 60 + s / 3600
                    if ref in ("S", "W"):
                        dd = -dd
                    return dd

                try:
                    lat_dms = gps_info.get(2)
                    lat_ref = gps_info.get(1, "N")
                    lon_dms = gps_info.get(4)
                    lon_ref = gps_info.get(3, "E")
                    if lat_dms and lon_dms:
                        lat = _dms_to_dd(lat_dms, lat_ref)
                        lon = _dms_to_dd(lon_dms, lon_ref)
                        info["GPS Coordinates"] = f"{lat:.6f}, {lon:.6f}"
                except Exception:
                    pass
    except ImportError:
        # Pillow not available -- basic QImage fallback
        qimg = QImage(file_path)
        if not qimg.isNull():
            info["Resolution"] = f"{qimg.width()} x {qimg.height()}"
            info["Bit Depth"] = str(qimg.depth())
    except Exception as exc:
        info["EXIF Error"] = str(exc)

    # File-level metadata
    try:
        st = os.stat(file_path)
        info["File Size"] = _fmt_size(st.st_size) or f"{st.st_size} B"
    except OSError:
        pass
    return info


# ---------------------------------------------------------------------------
# SharedChatsDialog -- shows all conversations sharing a media file_hash
# ---------------------------------------------------------------------------

class SharedChatsDialog(QDialog):
    """Dialog listing all conversations where a media with a given file_hash
    appears. Each row has a 'Go' button that emits navigate_to_chat."""

    navigate_to_chat = Signal(int, int)  # (conversation_id, message_id)

    def __init__(self, file_hash: str, parent=None):
        super().__init__(parent)
        self._file_hash = file_hash
        tc = _theme_colors()
        self.setWindowTitle("Shared Chats -- Media Hash Lookup")
        self.resize(700, 420)
        self.setStyleSheet(f"""
            QDialog {{ background: {tc['bg']}; }}
            QLabel {{ color: {tc['text']}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Header
        hdr = QLabel(f"Conversations containing media with hash: {file_hash[:16]}...")
        hdr.setStyleSheet(f"color: {tc['accent']}; font-size: 12px; font-weight: bold;")
        hdr.setWordWrap(True)
        layout.addWidget(hdr)

        # Query — use COUNT first, then LIMIT to prevent UI freeze on popular hashes
        db = Database.get()
        MAX_RESULTS = 200
        total_count = db.scalar(
            "SELECT COUNT(*) FROM media WHERE file_hash = ?", (file_hash,)
        ) or 0
        rows = db.fetchall(f"""
            SELECT cv.display_name,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Me'),
                   CASE WHEN m.from_me = 1 THEN 'Sent' ELSE 'Received' END,
                   m.timestamp,
                   m.id,
                   m.conversation_id
            FROM media me
            JOIN message m ON m.id = me.message_id
            JOIN conversation cv ON cv.id = m.conversation_id
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE me.file_hash = ?
            ORDER BY m.timestamp ASC
            LIMIT {MAX_RESULTS}
        """, (file_hash,))

        count_text = f"{total_count} occurrence(s) found across conversations"
        if total_count > MAX_RESULTS:
            count_text += f" (showing first {MAX_RESULTS})"
        count_label = QLabel(count_text)
        count_label.setStyleSheet(f"color: {tc['dim']}; font-size: 10px;")
        layout.addWidget(count_label)

        lt = _is_light()
        hdr_bg = "#e8eaed" if lt else "#1a2a32"
        hdr_col = "#3c4043" if lt else "#aebac1"
        hdr_border = "rgba(0,0,0,0.1)" if lt else "rgba(255,255,255,0.06)"

        # Table
        self._table = QTableWidget(len(rows), 5)
        self._table.setHorizontalHeaderLabels([
            "Conversation", "Sender", "Direction", "Timestamp", "",
        ])
        self._table.horizontalHeader().setStyleSheet(
            f"QHeaderView::section {{ background: {hdr_bg}; color: {hdr_col}; "
            f"border: 1px solid {hdr_border}; font-size: 10px; padding: 4px; }}"
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setStyleSheet(f"""
            QTableWidget {{
                background: {tc['bg']}; color: {tc['text']};
                border: 1px solid {tc['border']};
                gridline-color: {tc['grid_line']}; font-size: 10px;
            }}
            QTableWidget::item {{ padding: 3px 6px; }}
            QTableWidget::item:selected {{ background: {tc['selected']}; }}
        """)

        go_btn_style = f"""
            QPushButton {{ background: {tc['accent_bg']}; border: 1px solid {tc['accent']};
                          border-radius: 3px; padding: 2px 10px;
                          color: {tc['accent']}; font-size: 9px; font-weight: bold; }}
            QPushButton:hover {{ background: {tc['accent_bg'].replace('0.15', '0.35').replace('0.2', '0.4')}; }}
        """

        for r, row in enumerate(rows):
            conv_name = row[0] or "Unknown"
            sender = row[1] or "Unknown"
            direction = row[2]
            ts_val = row[3]
            msg_id = row[4]
            conv_id = row[5]

            self._table.setItem(r, 0, QTableWidgetItem(conv_name))
            self._table.setItem(r, 1, QTableWidgetItem(sender))
            self._table.setItem(r, 2, QTableWidgetItem(direction))

            ts_str = ""
            if ts_val:
                try:
                    ts_str = format_timestamp(ts_val, '%b %d, %Y %H:%M')
                except (ValueError, OSError):
                    ts_str = str(ts_val)
            self._table.setItem(r, 3, QTableWidgetItem(ts_str))

            go_btn = QPushButton("Go")
            go_btn.setStyleSheet(go_btn_style)
            go_btn.setFixedHeight(22)
            go_btn.clicked.connect(
                lambda checked=False, c=conv_id, m=msg_id: self._go_to_chat(c, m)
            )
            self._table.setCellWidget(r, 4, go_btn)

        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.setColumnWidth(4, 50)
        layout.addWidget(self._table, 1)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(28)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: {tc['hover']}; border: none;
                          border-radius: 4px; padding: 4px 20px;
                          color: {tc['text']}; font-size: 11px; }}
            QPushButton:hover {{ background: {tc['hover2']}; }}
        """)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignRight)

    def _go_to_chat(self, conv_id: int, msg_id: int):
        self.navigate_to_chat.emit(conv_id, msg_id)
        self.accept()


# ---------------------------------------------------------------------------
# MediaPreviewDialog -- full-featured media preview with zoom, EXIF, video,
# download/decrypt, shared chats, and navigation.
# ---------------------------------------------------------------------------

class MediaPreviewDialog(QDialog):
    """Full-featured popup for viewing a media item with zoom, EXIF metadata,
    video playback, download & decrypt, shared-chats lookup, and gallery
    navigation."""

    navigate_to_message = Signal(int)       # message_id
    navigate_to_chat = Signal(int, int)     # (conversation_id, message_id)

    _SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

    def __init__(self, items: list[_MediaItem], current_idx: int, parent=None):
        super().__init__(parent)
        self._items = items
        self._idx = current_idx
        self._current_pixmap: QPixmap | None = None
        self._current_movie: QMovie | None = None
        self._video_player = None
        self._video_widget = None
        self._audio_output = None
        self._exif_visible = True
        self._seeking = False
        self._speed_idx = 2  # 1.0x
        tc = _theme_colors()
        lt = _is_light()
        self._tc = tc
        self._lt = lt
        accent = tc['accent']

        self._BTN_STYLE = f"""
            QPushButton {{ background: {tc['hover']}; border: none;
                          border-radius: 6px; padding: 7px 16px;
                          color: {tc['text']}; font-size: 12px; }}
            QPushButton:hover {{ background: {tc['hover2']}; }}
            QPushButton:disabled {{ color: {tc['dim']}; }}
        """
        self._ACCENT_BTN_STYLE = f"""
            QPushButton {{ background: {tc['accent_bg']}; border: 1px solid {accent};
                          border-radius: 6px; padding: 7px 16px;
                          color: {accent}; font-size: 12px; }}
            QPushButton:hover {{ background: {'rgba(0,150,136,0.3)' if lt else 'rgba(0,188,212,0.35)'}; }}
            QPushButton:disabled {{ color: {'rgba(0,150,136,0.3)' if lt else 'rgba(0,188,212,0.3)'}; }}
        """
        arrow_bg = "rgba(0,0,0,0.03)" if lt else "rgba(255,255,255,0.03)"
        arrow_hover = "rgba(0,0,0,0.08)" if lt else "rgba(255,255,255,0.08)"
        arrow_text = "#aaa" if lt else "#555"
        arrow_hover_text = "#333" if lt else "#ccc"
        self._ARROW_STYLE = f"""
            QPushButton {{ background: {arrow_bg}; border: none;
                          color: {arrow_text}; font-size: 32px; font-weight: 300;
                          min-width: 40px; max-width: 40px; }}
            QPushButton:hover {{ background: {arrow_hover}; color: {arrow_hover_text}; }}
            QPushButton:disabled {{ color: transparent; background: transparent; }}
        """

        self.setWindowTitle("Media Preview")
        self.resize(1100, 750)
        self.setMinimumSize(800, 550)
        viewer_bg = "#f0f2f5" if lt else "#0b141a"
        self.setStyleSheet(f"""
            QDialog {{ background: {tc['bg']}; }}
            QLabel {{ color: {tc['text']}; }}
            QSplitter::handle {{ background: {tc['border']}; width: 3px; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ──
        hdr_w = QWidget()
        hdr_w.setStyleSheet(
            f"background: {tc['panel_bg']}; border-bottom: 1px solid {tc['border']};"
        )
        hdr = QHBoxLayout(hdr_w)
        hdr.setContentsMargins(14, 8, 14, 8)
        hdr.setSpacing(10)

        self._filename_label = QLabel()
        self._filename_label.setStyleSheet(
            f"color: {tc['text']}; font-size: 14px; font-weight: 600; border: none;"
        )
        self._filename_label.setWordWrap(False)
        self._filename_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        hdr.addWidget(self._filename_label, 1)

        self._status_label = QLabel()
        self._status_label.setStyleSheet(f"font-size: 11px; border: none;")
        hdr.addWidget(self._status_label)

        self._counter_label = QLabel()
        self._counter_label.setAlignment(Qt.AlignCenter)
        self._counter_label.setStyleSheet(
            f"color: {tc['dim']}; font-size: 12px; font-weight: 500; border: none;"
        )
        self._counter_label.setFixedWidth(80)
        hdr.addWidget(self._counter_label)

        root.addWidget(hdr_w)

        # ── Main splitter: [prev_arrow | viewer | next_arrow] | exif ──
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setChildrenCollapsible(True)

        viewer_outer = QWidget()
        viewer_outer.setStyleSheet(f"background: {viewer_bg};")
        vol = QHBoxLayout(viewer_outer)
        vol.setContentsMargins(0, 0, 0, 0)
        vol.setSpacing(0)

        # Prev arrow strip
        self._prev_btn = QPushButton("\u2039")  # single left angle quote
        self._prev_btn.setStyleSheet(self._ARROW_STYLE)
        self._prev_btn.clicked.connect(self._prev)
        self._prev_btn.setFocusPolicy(Qt.NoFocus)
        vol.addWidget(self._prev_btn)

        # Center viewer area
        viewer_center = QWidget()
        viewer_center.setStyleSheet(f"background: {viewer_bg};")
        vcl = QVBoxLayout(viewer_center)
        vcl.setContentsMargins(0, 0, 0, 0)
        vcl.setSpacing(0)

        self._zoom_widget = _ZoomableImageWidget()
        vcl.addWidget(self._zoom_widget, 1)

        # Video container (hidden by default)
        self._video_container = QWidget()
        self._video_container.setVisible(False)
        self._video_container.setStyleSheet(f"background: {viewer_bg};")
        video_vbox = QVBoxLayout(self._video_container)
        video_vbox.setContentsMargins(0, 0, 0, 0)
        video_vbox.setSpacing(0)

        self._video_placeholder = QLabel("\u25B6  Click Play or press Space")
        self._video_placeholder.setAlignment(Qt.AlignCenter)
        self._video_placeholder.setStyleSheet(
            f"background: {viewer_bg}; color: {tc['dim']}; font-size: 14px;"
        )
        video_vbox.addWidget(self._video_placeholder, 1)

        # Video controls bar
        self._video_bar = QWidget()
        self._video_bar.setStyleSheet(
            f"background: {tc['panel_bg']}; border-top: 1px solid {tc['border']};"
        )
        vbl = QHBoxLayout(self._video_bar)
        vbl.setContentsMargins(10, 5, 10, 5)
        vbl.setSpacing(8)

        self._play_btn = QPushButton("\u25B6")
        self._play_btn.setFixedSize(34, 28)
        play_style = self._ACCENT_BTN_STYLE.replace("padding: 7px 16px", "padding: 2px 6px")
        self._play_btn.setStyleSheet(play_style)
        self._play_btn.clicked.connect(self._toggle_video_play)
        vbl.addWidget(self._play_btn)

        self._time_label = QLabel("0:00")
        self._time_label.setFixedWidth(50)
        self._time_label.setStyleSheet(
            f"color: {tc['dim']}; font-size: 10px; font-family: 'Consolas','monospace';"
        )
        vbl.addWidget(self._time_label)

        slider_groove = "rgba(0,0,0,0.12)" if lt else "rgba(255,255,255,0.12)"
        slider_style = f"""
            QSlider::groove:horizontal {{ height: 4px; background: {slider_groove}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0;
                                         background: {accent}; border-radius: 6px; }}
            QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
        """
        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._seek_slider.setStyleSheet(slider_style)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._seek_slider.sliderMoved.connect(self._on_seek_moved)
        vbl.addWidget(self._seek_slider, 1)

        self._duration_label = QLabel("0:00")
        self._duration_label.setFixedWidth(50)
        self._duration_label.setStyleSheet(self._time_label.styleSheet())
        vbl.addWidget(self._duration_label)

        vol_icon = QLabel("\u266B")
        vol_icon.setStyleSheet(f"color: {tc['dim']}; font-size: 12px;")
        vbl.addWidget(vol_icon)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.setFixedWidth(70)
        self._vol_slider.setStyleSheet(slider_style)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        vbl.addWidget(self._vol_slider)

        self._speed_btn = QPushButton("1x")
        self._speed_btn.setFixedSize(36, 24)
        self._speed_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: 1px solid {tc['border']};
                          border-radius: 4px; color: {tc['dim']}; font-size: 10px; }}
            QPushButton:hover {{ background: {tc['hover']}; }}
        """)
        self._speed_btn.clicked.connect(self._cycle_speed)
        vbl.addWidget(self._speed_btn)

        video_vbox.addWidget(self._video_bar)
        vcl.addWidget(self._video_container)

        # Animated sticker label (hidden by default)
        self._anim_label = QLabel()
        self._anim_label.setAlignment(Qt.AlignCenter)
        self._anim_label.setStyleSheet(f"background: {viewer_bg};")
        self._anim_label.setVisible(False)
        vcl.addWidget(self._anim_label)

        vol.addWidget(viewer_center, 1)

        # Next arrow strip
        self._next_btn = QPushButton("\u203A")  # single right angle quote
        self._next_btn.setStyleSheet(self._ARROW_STYLE)
        self._next_btn.clicked.connect(self._next)
        self._next_btn.setFocusPolicy(Qt.NoFocus)
        vol.addWidget(self._next_btn)

        self._splitter.addWidget(viewer_outer)

        # Right: EXIF metadata panel (hidden by default, toggle with Info button)
        self._exif_panel = QScrollArea()
        self._exif_panel.setWidgetResizable(True)
        self._exif_panel.setMinimumWidth(220)
        self._exif_panel.setMaximumWidth(300)
        self._exif_panel.setStyleSheet(f"""
            QScrollArea {{ background: {tc['panel_bg']};
                          border-left: 1px solid {tc['border']};
                          border: none; }}
        """)
        exif_inner = QWidget()
        self._exif_layout = QVBoxLayout(exif_inner)
        self._exif_layout.setContentsMargins(12, 10, 12, 10)
        self._exif_layout.setSpacing(4)
        exif_title = QLabel("Metadata")
        exif_title.setStyleSheet(
            f"color: {accent}; font-size: 12px; font-weight: bold; padding-bottom: 4px;"
        )
        self._exif_layout.addWidget(exif_title)
        self._exif_content = QLabel()
        self._exif_content.setWordWrap(True)
        self._exif_content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        exif_text_col = "#3c4043" if lt else "rgba(255,255,255,0.7)"
        self._exif_content.setStyleSheet(
            f"color: {exif_text_col}; font-size: 10px; line-height: 1.5;"
        )
        self._exif_layout.addWidget(self._exif_content)
        self._exif_layout.addStretch()
        self._exif_panel.setWidget(exif_inner)
        self._splitter.addWidget(self._exif_panel)

        self._splitter.setSizes([850, 250])
        # Start with EXIF panel hidden — user can toggle with Info button
        self._exif_panel.setVisible(False)
        self._exif_visible = False
        root.addWidget(self._splitter, 1)

        # ── Info bar ──
        self._info_label = QLabel()
        self._info_label.setStyleSheet(
            f"color: {tc['dim']}; font-size: 10px; padding: 4px 14px;"
        )
        self._info_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self._info_label)

        # ── Action toolbar (icon buttons with tooltips) ──
        tb_w = QWidget()
        tb_w.setStyleSheet(
            f"background: {tc['panel_bg']}; border-top: 1px solid {tc['border']};"
        )
        toolbar = QHBoxLayout(tb_w)
        toolbar.setContentsMargins(10, 5, 10, 7)
        toolbar.setSpacing(4)

        _icon_style = f"""
            QPushButton {{ background: {tc['hover']}; border: none;
                          border-radius: 6px; padding: 6px 12px;
                          color: {tc['text']}; font-size: 16px; min-width: 36px; }}
            QPushButton:hover {{ background: {tc['hover2']}; }}
            QPushButton:disabled {{ color: {tc['dim']}; }}
        """
        _accent_icon_style = f"""
            QPushButton {{ background: {tc['accent_bg']}; border: 1px solid {accent};
                          border-radius: 6px; padding: 6px 12px;
                          color: {accent}; font-size: 16px; min-width: 36px; }}
            QPushButton:hover {{ background: {'rgba(0,150,136,0.3)' if lt else 'rgba(0,188,212,0.35)'}; }}
            QPushButton:disabled {{ color: {tc['dim']}; }}
        """

        goto_btn = QPushButton("\u2192")
        goto_btn.setStyleSheet(_accent_icon_style)
        goto_btn.clicked.connect(self._goto_message)
        goto_btn.setToolTip("Go to message in chat")
        toolbar.addWidget(goto_btn)

        self._open_btn = QPushButton("\u2197")
        self._open_btn.setStyleSheet(_icon_style)
        self._open_btn.clicked.connect(self._open_file)
        self._open_btn.setToolTip("Open in default app")
        toolbar.addWidget(self._open_btn)

        self._copy_img_btn = QPushButton("\u2398")
        self._copy_img_btn.setStyleSheet(_icon_style)
        self._copy_img_btn.clicked.connect(self._copy_image)
        self._copy_img_btn.setToolTip("Copy image to clipboard")
        toolbar.addWidget(self._copy_img_btn)

        self._download_btn = QPushButton("\u21E9")
        self._download_btn.setStyleSheet(_accent_icon_style)
        self._download_btn.clicked.connect(self._download_decrypt)
        self._download_btn.setToolTip("Download and decrypt from CDN")
        toolbar.addWidget(self._download_btn)

        self._download_status = QLabel()
        self._download_status.setStyleSheet(f"font-size: 10px; padding: 0 4px;")
        toolbar.addWidget(self._download_status)

        toolbar.addStretch()

        self._shared_btn = QPushButton("\u2637")
        self._shared_btn.setStyleSheet(_icon_style)
        self._shared_btn.clicked.connect(self._show_shared_chats)
        self._shared_btn.setToolTip("Find this media in other chats")
        toolbar.addWidget(self._shared_btn)

        exif_toggle = QPushButton("\u2139")
        exif_toggle.setStyleSheet(_icon_style)
        exif_toggle.clicked.connect(self._toggle_exif)
        exif_toggle.setToolTip("Toggle metadata panel")
        toolbar.addWidget(exif_toggle)

        root.addWidget(tb_w)

        # ── Keyboard shortcuts (work even when child widgets have focus) ──
        QShortcut(QKeySequence(Qt.Key_Left), self, self._prev)
        QShortcut(QKeySequence(Qt.Key_Right), self, self._next)
        QShortcut(QKeySequence(Qt.Key_Space), self, self._toggle_video_play)

        # ── Multimedia check ──
        self._has_multimedia = False
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
            self._has_multimedia = True
        except ImportError:
            pass

        self._show_current()

    # -- Display current item -----------------------------------------------

    def _show_current(self):
        if not self._items or self._idx < 0 or self._idx >= len(self._items):
            return
        item = self._items[self._idx]
        self._stop_video()
        self._stop_animation()

        # Reset visibility
        self._zoom_widget.setVisible(False)
        self._video_container.setVisible(False)
        self._anim_label.setVisible(False)

        # Determine what to show
        is_video = item.type_category == "video"
        is_animated_sticker = (
            item.mime_type == "image/webp"
            or item.type_label in ("sticker", "animated_gif")
        )
        has_file = bool(item.resolved_path and os.path.isfile(item.resolved_path))

        # -- Filename header (elide to fit) --
        if item.resolved_path:
            fname = os.path.basename(item.resolved_path)
        elif item.mime_type:
            from app.services.media_crypto import get_extension_for_mime
            fname = f"media_{item.media_id}{get_extension_for_mime(item.mime_type)}"
        else:
            fname = f"media_{item.media_id}"
        fm = QFontMetrics(self._filename_label.font())
        avail = max(self._filename_label.width() - 10, 200)
        elided = fm.elidedText(fname, Qt.ElideMiddle, avail)
        self._filename_label.setText(elided)
        self._filename_label.setToolTip(fname)

        # -- Status indicator --
        status = item.status
        status_colors = {
            "on_disk": "#50c850",
            "downloadable": "#50b4ff",
            "expired": "#c86432",
            "missing": "#963c3c",
        }
        status_texts = {
            "on_disk": "On disk",
            "downloadable": "Downloadable from CDN",
            "expired": "URL expired",
            "missing": "Missing",
        }
        color = status_colors.get(status, "#888")
        text = status_texts.get(status, status)
        self._status_label.setText(
            f'<span style="color:{color};">\u25CF</span> '
            f'<span style="color:{color};">{text}</span>'
        )

        # -- Video --
        if is_video and has_file and self._has_multimedia:
            self._show_video(item)
        # -- Animated sticker --
        elif is_animated_sticker and has_file:
            self._show_animated(item)
        else:
            # -- Image / thumbnail fallback --
            self._show_image(item)

        # -- Info bar --
        self._update_info_bar(item)

        # -- EXIF panel --
        self._update_exif(item)

        # -- Counter & button states --
        self._counter_label.setText(f"{self._idx + 1} / {len(self._items)}")
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(self._items) - 1)
        self._open_btn.setEnabled(has_file)
        self._copy_img_btn.setEnabled(self._current_pixmap is not None)
        self._shared_btn.setEnabled(bool(item.file_hash))

        # Download button state
        if status == "downloadable":
            self._download_btn.setEnabled(True)
            self._download_status.setText("")
        elif status == "on_disk":
            self._download_btn.setEnabled(False)
            self._download_status.setText(
                '<span style="color:#50c850;">Already on disk</span>'
            )
        else:
            self._download_btn.setEnabled(False)
            self._download_status.setText(
                '<span style="color:#963c3c;">Not available for download</span>'
            )

    def _show_image(self, item: _MediaItem):
        pxm = QPixmap()
        if item.resolved_path and os.path.isfile(item.resolved_path):
            pxm.load(item.resolved_path)
        elif item.has_thumb:
            blob = _load_thumb_blob(item.media_id)
            if blob:
                pxm.loadFromData(blob)

        self._current_pixmap = pxm if not pxm.isNull() else None
        self._zoom_widget.set_pixmap(self._current_pixmap)
        self._zoom_widget.setVisible(True)

    def _show_video(self, item: _MediaItem):
        """Show video using QMediaPlayer + QVideoWidget with seek/volume."""
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self._video_container.setVisible(True)

            # Create video widget on first use
            if self._video_widget is None:
                self._video_widget = QVideoWidget()
                vbg = "#f0f2f5" if self._lt else "#0b141a"
                self._video_widget.setStyleSheet(f"background: {vbg};")
                layout = self._video_container.layout()
                layout.insertWidget(0, self._video_widget, 1)
                self._video_placeholder.setVisible(False)
            else:
                self._video_widget.setVisible(True)
                self._video_placeholder.setVisible(False)

            if self._video_player is None:
                self._video_player = QMediaPlayer(self)
                self._audio_output = QAudioOutput(self)
                self._audio_output.setVolume(self._vol_slider.value() / 100.0)
                self._video_player.setAudioOutput(self._audio_output)
                self._video_player.setVideoOutput(self._video_widget)
                self._video_player.positionChanged.connect(self._on_position_changed)
                self._video_player.durationChanged.connect(self._on_duration_changed)

            self._video_player.setSource(QUrl.fromLocalFile(item.resolved_path))
            self._play_btn.setText("\u25B6")
            self._seek_slider.setValue(0)
            self._time_label.setText("0:00")
            self._duration_label.setText("0:00")

            # Load thumbnail for copy-image
            pxm = QPixmap()
            if item.has_thumb:
                blob = _load_thumb_blob(item.media_id)
                if blob:
                    pxm.loadFromData(blob)
            self._current_pixmap = pxm if not pxm.isNull() else None

        except Exception as exc:
            logger.warning("Video playback failed: %s", exc)
            self._show_image(item)

    def _show_animated(self, item: _MediaItem):
        """Show animated sticker as high-quality static image.
        QMovie produces heavily pixelated artifacts for animated WebP;
        QImageReader decodes the first frame at native resolution."""
        path = item.resolved_path
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        img = reader.read()
        if not img.isNull():
            pxm = QPixmap.fromImage(img)
            self._current_pixmap = pxm
            self._zoom_widget.set_pixmap(pxm)
            self._zoom_widget.setVisible(True)
        else:
            self._show_image(item)

    def _stop_video(self):
        if self._video_player is not None:
            try:
                self._video_player.stop()
            except Exception:
                pass

    def _stop_animation(self):
        if self._current_movie is not None:
            try:
                self._current_movie.stop()
            except Exception:
                pass
            self._current_movie = None

    def _toggle_video_play(self):
        if self._video_player is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self._video_player.playbackState() == QMediaPlayer.PlayingState:
            self._video_player.pause()
            self._play_btn.setText("\u25B6")
        else:
            self._video_player.play()
            self._play_btn.setText("\u23F8")

    def _on_position_changed(self, pos_ms: int):
        if not self._seeking:
            dur = max(self._video_player.duration(), 1)
            self._seek_slider.setValue(int(pos_ms * 1000 / dur))
            self._time_label.setText(self._fmt_time(pos_ms))

    def _on_duration_changed(self, dur_ms: int):
        self._duration_label.setText(self._fmt_time(dur_ms))

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_released(self):
        self._seeking = False
        if self._video_player:
            dur = self._video_player.duration()
            pos = int(self._seek_slider.value() * dur / 1000)
            self._video_player.setPosition(pos)

    def _on_seek_moved(self, val: int):
        if self._video_player:
            dur = self._video_player.duration()
            pos = int(val * dur / 1000)
            self._time_label.setText(self._fmt_time(pos))

    def _on_volume_changed(self, val: int):
        if self._audio_output:
            self._audio_output.setVolume(val / 100.0)

    def _cycle_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(self._SPEEDS)
        speed = self._SPEEDS[self._speed_idx]
        self._speed_btn.setText(f"{speed:g}x")
        if self._video_player:
            self._video_player.setPlaybackRate(speed)

    @staticmethod
    def _fmt_time(ms: int) -> str:
        s = max(ms, 0) // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # -- Info bar -----------------------------------------------------------

    def _update_info_bar(self, item: _MediaItem):
        ts_str = ""
        if item.timestamp:
            try:
                ts_str = format_timestamp(item.timestamp, '%b %d, %Y %H:%M')
            except (ValueError, OSError):
                pass
        direction = "Sent" if item.from_me else f"From: {item.sender_name}"
        size = _fmt_size(item.file_size)
        parts = [p for p in [direction, ts_str] if p]
        if item.type_label:
            parts.append(item.type_label.replace("_", " ").title())
        if size:
            parts.append(size)
        status = item.status
        status_labels = {
            "on_disk": '<span style="color:#50c850;">\u25CF</span> On disk',
            "downloadable": '<span style="color:#50b4ff;">\u25CF</span> Downloadable',
            "expired": '<span style="color:#c86432;">\u25CF</span> URL expired',
            "missing": '<span style="color:#963c3c;">\u25CF</span> Missing',
        }
        parts.append(status_labels.get(status, status))
        self._info_label.setText("  |  ".join(parts))

    # -- EXIF panel ---------------------------------------------------------

    def _update_exif(self, item: _MediaItem):
        lines: list[str] = []

        # Always show basic media info
        if item.mime_type:
            lines.append(f"<b>MIME:</b> {item.mime_type}")
        if item.file_size:
            lines.append(f"<b>File Size:</b> {_fmt_size(item.file_size)}")
        if item.file_hash:
            short_hash = item.file_hash[:24] + ("..." if len(item.file_hash) > 24 else "")
            lines.append(f"<b>File Hash:</b> {short_hash}")
        if item.media_id:
            lines.append(f"<b>Media ID:</b> {item.media_id}")

        lines.append("")  # separator

        # EXIF from file
        if item.resolved_path and os.path.isfile(item.resolved_path):
            exif = _extract_exif(item.resolved_path)
            for key, val in exif.items():
                lines.append(f"<b>{key}:</b> {val}")
        else:
            # QImage from thumbnail
            if item.has_thumb:
                blob = _load_thumb_blob(item.media_id)
                if blob:
                    qimg = QImage()
                    qimg.loadFromData(blob)
                    if not qimg.isNull():
                        lines.append(f"<b>Thumb Size:</b> {qimg.width()} x {qimg.height()}")

        if item.resolved_path:
            lines.append("")
            lines.append(f"<b>Path:</b> <span style='font-size:8px;'>{item.resolved_path}</span>")

        self._exif_content.setText("<br>".join(lines))

    def _toggle_exif(self):
        self._exif_visible = not self._exif_visible
        self._exif_panel.setVisible(self._exif_visible)

    # -- Navigation ---------------------------------------------------------

    def _prev(self):
        if self._idx > 0:
            self._idx -= 1
            self._show_current()

    def _next(self):
        if self._idx < len(self._items) - 1:
            self._idx += 1
            self._show_current()

    def _goto_message(self):
        if 0 <= self._idx < len(self._items):
            self.navigate_to_message.emit(self._items[self._idx].message_id)
            self.accept()

    def _open_file(self):
        if 0 <= self._idx < len(self._items):
            item = self._items[self._idx]
            if item.resolved_path and os.path.isfile(item.resolved_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(item.resolved_path))

    def keyPressEvent(self, event):
        # Escape still handled here (QShortcut handles Left/Right/Space)
        if event.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    # -- Copy image ---------------------------------------------------------

    def _copy_image(self):
        if self._current_pixmap and not self._current_pixmap.isNull():
            QApplication.clipboard().setPixmap(self._current_pixmap)
            self._download_status.setText(
                '<span style="color:#50c850;">Copied to clipboard</span>'
            )
            QTimer.singleShot(2000, lambda: self._download_status.setText(""))

    # -- Download & Decrypt -------------------------------------------------

    def _download_decrypt(self):
        if not (0 <= self._idx < len(self._items)):
            return
        item = self._items[self._idx]
        if item.status != "downloadable":
            return

        self._download_btn.setEnabled(False)
        self._download_status.setText(
            '<span style="color:#50b4ff;">Downloading...</span>'
        )
        QApplication.processEvents()

        try:
            from app.services.media_crypto import (
                download_and_decrypt, get_media_type, get_extension_for_mime,
            )

            media_type = get_media_type(item.type_label, item.mime_type)
            ext = get_extension_for_mime(item.mime_type)

            # Build save path — use case dir or analysis DB dir
            from app.services.case_manager import CaseManager
            _cm = CaseManager.get()
            if _cm.is_open and _cm.recovered_media_dir:
                base_dir = str(_cm.recovered_media_dir)
            else:
                from app.services.database import Database
                base_dir = os.path.join(os.path.dirname(str(Database.get().path)), "recovered_media")
            os.makedirs(base_dir, exist_ok=True)
            fname = f"{item.media_id}_{item.file_hash[:8] if item.file_hash else 'nohs'}{ext}"
            save_path = os.path.join(base_dir, fname)

            plaintext = download_and_decrypt(
                url=item.media_url,
                media_key=item.media_key,
                media_type=media_type,
                file_hash=item.file_hash or None,
                save_path=save_path,
            )

            self._download_status.setText(
                f'<span style="color:#50c850;">Saved ({_fmt_size(len(plaintext))})</span>'
            )
            # Update item so preview can show the file now
            item.resolved_path = save_path
            item.file_exists = True
            # Refresh display
            self._show_current()

        except Exception as exc:
            logger.exception("Download & decrypt failed")
            self._download_status.setText(
                f'<span style="color:#ff4444;">Error: {str(exc)[:60]}</span>'
            )
            self._download_btn.setEnabled(True)

    # -- Shared chats -------------------------------------------------------

    def _show_shared_chats(self):
        if not (0 <= self._idx < len(self._items)):
            return
        item = self._items[self._idx]
        if not item.file_hash:
            return

        dlg = SharedChatsDialog(item.file_hash, self)
        dlg.navigate_to_chat.connect(self._on_navigate_to_chat)
        dlg.exec()

    def _on_navigate_to_chat(self, conv_id: int, msg_id: int):
        self.navigate_to_chat.emit(conv_id, msg_id)
        self.accept()

    # -- Cleanup ------------------------------------------------------------

    def closeEvent(self, event):
        self._stop_video()
        self._stop_animation()
        super().closeEvent(event)

    def reject(self):
        self._stop_video()
        self._stop_animation()
        super().reject()


class ChatMediaGalleryPanel(QFrame):
    """Right-side panel showing media gallery for the current conversation."""

    navigate_to_message = Signal(int)  # message_id
    navigate_to_chat = Signal(int, int)  # (conversation_id, message_id)
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(420)
        lt = _is_light()
        tc = _theme_colors()
        self._lt = lt
        self._tc = tc
        self._load_worker: _GalleryLoadWorker | None = None
        bg = "#f5f7fa" if lt else "#111b21"
        border = "#e0e3e7" if lt else "rgba(255,255,255,0.08)"
        self.setStyleSheet(f"""
            QFrame {{ background: {bg};
                     border-left: 1px solid {border}; }}
        """)

        self._conv_id: int | None = None
        self._is_group: bool = False
        self._all_items: list[_MediaItem] = []
        self._filtered_items: list[_MediaItem] = []
        self._current_sender: str = "all"
        self._current_type: str = "all"
        self._current_date_filter: str = "all"
        self._current_sort: str = "newest"
        self._search_text: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title = QLabel("Chat Media")
        title.setStyleSheet(f"color: {tc['accent']}; font-size: 16px; font-weight: 700;")
        hdr.addWidget(title)
        hdr.addStretch()
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet(f"color: {tc['dim']}; font-size: 11px;")
        hdr.addWidget(self._stats_label)
        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(24, 24)
        dim_text = "#667781" if lt else "#aebac1"
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none;
                          color: {dim_text}; font-size: 14px; }}
            QPushButton:hover {{ color: {tc['text']}; }}
        """)
        close_btn.clicked.connect(self.close_requested.emit)
        hdr.addWidget(close_btn)
        layout.addLayout(hdr)

        self._selection_label = QLabel("Select media to preview or open its message in chat.")
        self._selection_label.setWordWrap(True)
        self._selection_label.setStyleSheet(
            f"background: {tc['panel_bg']}; border: 1px solid {tc['border']}; "
            f"border-radius: 8px; color: {tc['dim']}; font-size: 10px; padding: 8px;"
        )
        layout.addWidget(self._selection_label)

        # Sender filter
        sender_row = QHBoxLayout()
        sender_row.setSpacing(4)
        lbl = QLabel("Sender:")
        lbl.setStyleSheet(f"color: {tc['dim']}; font-size: 9px;")
        sender_row.addWidget(lbl)
        self._sender_combo = QComboBox()
        self._sender_combo.setFixedHeight(24)
        combo_bg = "rgba(0,0,0,0.04)" if lt else "rgba(255,255,255,0.06)"
        combo_border = "rgba(0,0,0,0.12)" if lt else "rgba(255,255,255,0.1)"
        combo_drop = "#ffffff" if lt else "#233138"
        self._sender_combo.setStyleSheet(f"""
            QComboBox {{ background: {combo_bg}; border: 1px solid {combo_border};
                        border-radius: 4px; padding: 0 6px; color: {tc['text']}; font-size: 9px; }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{ background: {combo_drop}; color: {tc['text']};
                                          selection-background-color: {tc['selected']}; }}
        """)
        self._sender_combo.currentIndexChanged.connect(self._on_sender_changed)
        sender_row.addWidget(self._sender_combo, 1)
        layout.addLayout(sender_row)

        # Search + date + sort controls
        refine_row = QHBoxLayout()
        refine_row.setSpacing(4)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search file or sender")
        self._search_edit.setFixedHeight(24)
        self._search_edit.setStyleSheet(f"""
            QLineEdit {{ background: {combo_bg}; border: 1px solid {combo_border};
                        border-radius: 4px; padding: 0 6px; color: {tc['text']}; font-size: 9px; }}
        """)
        self._search_edit.textChanged.connect(self._on_search_changed)
        refine_row.addWidget(self._search_edit, 2)

        self._date_combo = QComboBox()
        self._date_combo.setFixedHeight(24)
        self._date_combo.addItems(["All dates", "7d", "30d", "90d", "This year"])
        self._date_combo.setStyleSheet(self._sender_combo.styleSheet())
        self._date_combo.currentIndexChanged.connect(self._on_date_changed)
        refine_row.addWidget(self._date_combo, 1)

        self._sort_combo = QComboBox()
        self._sort_combo.setFixedHeight(24)
        self._sort_combo.addItems(["Newest", "Oldest", "Largest", "Name"])
        self._sort_combo.setStyleSheet(self._sender_combo.styleSheet())
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        refine_row.addWidget(self._sort_combo, 1)

        layout.addLayout(refine_row)

        # Type filter buttons
        type_row = QHBoxLayout()
        type_row.setSpacing(3)
        self._type_btns: dict[str, QPushButton] = {}
        pill_border = "rgba(0,0,0,0.12)" if lt else "rgba(255,255,255,0.1)"
        pill_text = "#667781" if lt else "#aaa"
        pill_hover = "rgba(0,0,0,0.04)" if lt else "rgba(255,255,255,0.05)"
        btn_style = f"""
            QPushButton {{ padding: 2px 6px; border-radius: 10px;
                          border: 1px solid {pill_border}; font-size: 8px; color: {pill_text}; }}
            QPushButton:checked {{ background: {tc['accent_bg']};
                                  border-color: {tc['accent']}; color: {tc['accent']}; }}
            QPushButton:hover:!checked {{ background: {pill_hover}; }}
        """
        for fid, label in [
            ("all", "All"), ("image", "\u25A3"), ("video", "\u25B6"),
            ("audio", "\u266B"), ("document", "\u2637"),
            ("sticker", "\u2B50"), ("no_sticker", "No Stickers"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet(btn_style)
            btn.setProperty("type_id", fid)
            btn.clicked.connect(self._on_type_filter)
            if fid == "all":
                btn.setChecked(True)
            type_row.addWidget(btn)
            self._type_btns[fid] = btn
        type_row.addStretch()
        layout.addLayout(type_row)

        # Sender stats
        self._sender_stats = QLabel("")
        self._sender_stats.setStyleSheet(f"color: {tc['dim2']}; font-size: 10px;")
        self._sender_stats.setWordWrap(True)
        layout.addWidget(self._sender_stats)

        # Loading indicator
        self._loading_label = QLabel("")
        self._loading_label.setAlignment(Qt.AlignCenter)
        self._loading_label.setStyleSheet(f"color: {tc['dim']}; font-size: 10px; padding: 20px;")
        self._loading_label.setVisible(False)
        layout.addWidget(self._loading_label)

        # Thumbnail grid
        from PySide6.QtCore import QAbstractListModel

        class _SimpleMediaModel(QAbstractListModel):
            def __init__(self):
                super().__init__()
                self.items: list[_MediaItem] = []

            def rowCount(self, parent=QModelIndex()):
                return len(self.items)

            def data(self, index, role=Qt.DisplayRole):
                if not index.isValid() or index.row() >= len(self.items):
                    return None
                if role == Qt.UserRole + 10:
                    return self.items[index.row()]
                return None

            def set_items(self, items: list[_MediaItem]):
                self.beginResetModel()
                self.items = items
                self.endResetModel()

        self._grid_model = _SimpleMediaModel()
        self._grid = QListView()
        self._grid.setModel(self._grid_model)
        self._grid_delegate = _GalleryTileDelegate()
        self._grid.setItemDelegate(self._grid_delegate)
        self._grid.setViewMode(QListView.IconMode)
        self._grid.setResizeMode(QListView.Adjust)
        self._grid.setGridSize(QSize(120, 120))
        self._grid.setWrapping(True)
        self._grid.setSpacing(8)
        self._grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self._grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._grid.setUniformItemSizes(True)
        self._grid.clicked.connect(self._on_grid_clicked)
        item_border = "rgba(0,0,0,0.05)" if lt else "rgba(255,255,255,0.04)"
        self._grid.setStyleSheet(f"""
            QListView {{ background: transparent; border: none; }}
            QListView::item {{ border: 1px solid {item_border}; border-radius: 8px; padding: 2px; }}
            QListView::item:selected {{ border: 2px solid {tc['accent']}; background: {tc['selected']}; }}
        """)
        self._grid.doubleClicked.connect(self._on_double_click)
        self._grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self._grid.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._grid, 1)

        # Legend
        legend = QLabel(
            '<span style="color:#50c850;">\u25CF</span> On disk '
            '<span style="color:#50b4ff;">\u25CF</span> Downloadable '
            '<span style="color:#c86432;">\u25CF</span> Expired '
            '<span style="color:#963c3c;">\u25CF</span> Missing'
        )
        legend.setStyleSheet(f"font-size: 9px; color: {tc['dim']};")
        layout.addWidget(legend)

        # Bottom: "Go to Message" button for selected item
        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self._goto_btn = QPushButton("Open Message In Chat")
        self._goto_btn.setFixedHeight(34)
        self._goto_btn.setStyleSheet(f"""
            QPushButton {{ background: {tc['accent_bg']}; border: 1px solid {tc['accent_border']};
                          border-radius: 8px; color: {tc['accent']}; font-size: 11px; font-weight: 600; }}
            QPushButton:hover {{ background: {'rgba(0,150,136,0.3)' if lt else 'rgba(0,188,212,0.3)'}; }}
        """)
        self._goto_btn.clicked.connect(self._goto_selected)
        bottom.addWidget(self._goto_btn)

        self._preview_btn = QPushButton("Preview Selected")
        self._preview_btn.setFixedHeight(34)
        self._preview_btn.setStyleSheet(f"""
            QPushButton {{ background: {tc['hover']}; border: none;
                          border-radius: 8px; color: {dim_text}; font-size: 11px; font-weight: 600; }}
            QPushButton:hover {{ background: {tc['hover2']}; }}
        """)
        self._preview_btn.clicked.connect(self._open_preview)
        bottom.addWidget(self._preview_btn)
        layout.addLayout(bottom)

    def load_conversation(self, conv_id: int, is_group: bool):
        """Load all media for a conversation (in background thread)."""
        self._conv_id = conv_id
        self._is_group = is_group
        self._all_items.clear()
        self._filtered_items.clear()
        self._grid_delegate.clear_cache()
        self._grid_model.set_items([])

        # Show loading indicator
        self._loading_label.setText("Loading media...")
        self._loading_label.setVisible(True)
        self._grid.setVisible(False)
        self._selection_label.setText("Loading media for this chat...")

        # Cancel previous worker if running
        if self._load_worker is not None:
            try:
                self._load_worker.finished.disconnect()
                self._load_worker.quit()
                self._load_worker.wait(200)
            except Exception:
                pass
            self._load_worker = None

        # Start background worker
        worker = _GalleryLoadWorker(conv_id, self)
        worker.finished.connect(lambda items: self._on_items_loaded(items, is_group))
        self._load_worker = worker
        worker.start()

    def _on_items_loaded(self, items: list[_MediaItem], is_group: bool):
        """Callback when background loading completes."""
        self._load_worker = None
        self._loading_label.setVisible(False)
        self._grid.setVisible(True)

        self._all_items = items

        # Build sender list
        self._sender_combo.blockSignals(True)
        self._sender_combo.clear()
        self._sender_combo.addItem(f"All ({len(self._all_items)})", "all")

        if is_group:
            # Group: list each sender
            sender_counts: dict[str, tuple[int, int]] = {}  # sender_id -> (count, sender_id_int)
            sender_names: dict[str, str] = {}  # sender_id key -> display name
            sent_count = 0
            for item in self._all_items:
                if item.from_me:
                    sent_count += 1
                else:
                    key = str(item.sender_id) if item.sender_id else item.sender_name
                    if key not in sender_counts:
                        sender_counts[key] = 0
                        sender_names[key] = item.sender_name
                    sender_counts[key] = sender_counts[key] + 1

            if sent_count > 0:
                self._sender_combo.addItem(f"You ({sent_count})", "me")
            # Sort by count descending
            for key, count in sorted(sender_counts.items(),
                                     key=lambda x: x[1], reverse=True):
                name = sender_names[key]
                display = name if len(name) <= 20 else name[:18] + ".."
                self._sender_combo.addItem(f"{display} ({count})", f"sender:{key}")
        else:
            # 1-on-1: You / Them
            sent = sum(1 for i in self._all_items if i.from_me)
            received = len(self._all_items) - sent
            if sent > 0:
                self._sender_combo.addItem(f"You sent ({sent})", "me")
            if received > 0:
                # Get the other person's name
                other_name = "Them"
                for item in self._all_items:
                    if not item.from_me and item.sender_name != "Unknown":
                        other_name = item.sender_name
                        break
                display = other_name if len(other_name) <= 18 else other_name[:16] + ".."
                self._sender_combo.addItem(f"{display} ({received})", "them")

        self._sender_combo.blockSignals(False)

        # Type stats
        type_counts = {"image": 0, "video": 0, "audio": 0, "document": 0}
        for item in self._all_items:
            cat = item.type_category
            type_counts[cat] = type_counts.get(cat, 0) + 1
        parts = []
        if type_counts["image"]:
            parts.append(f"\u25A3 {type_counts['image']}")
        if type_counts["video"]:
            parts.append(f"\u25B6 {type_counts['video']}")
        if type_counts["audio"]:
            parts.append(f"\u266B {type_counts['audio']}")
        if type_counts["document"]:
            parts.append(f"\u2637 {type_counts['document']}")
        self._stats_label.setText(f"{len(self._all_items)} media")
        self._sender_stats.setText("  ".join(parts))

        self._current_sender = "all"
        self._current_type = "all"
        self._current_date_filter = "All dates"
        self._current_sort = "newest"
        self._search_text = ""
        self._search_edit.clear()
        self._date_combo.setCurrentIndex(0)
        self._sort_combo.setCurrentIndex(0)
        self._apply_filters()
        self._selection_label.setText("Select media to preview or open its message in chat.")

    def _on_sender_changed(self, index):
        data = self._sender_combo.itemData(index)
        self._current_sender = data or "all"
        self._apply_filters()

    def _on_search_changed(self, text: str):
        self._search_text = (text or "").strip().lower()
        self._apply_filters()

    def _on_date_changed(self, index: int):
        self._current_date_filter = self._date_combo.itemText(index)
        self._apply_filters()

    def _on_sort_changed(self, index: int):
        mapping = {0: "newest", 1: "oldest", 2: "largest", 3: "name"}
        self._current_sort = mapping.get(index, "newest")
        self._apply_filters()

    def _on_type_filter(self):
        fid = self.sender().property("type_id")
        for k, b in self._type_btns.items():
            b.setChecked(k == fid)
        self._current_type = fid
        self._apply_filters()

    def _apply_filters(self):
        items = self._all_items

        # Sender filter
        if self._current_sender == "me":
            items = [i for i in items if i.from_me]
        elif self._current_sender == "them":
            items = [i for i in items if not i.from_me]
        elif self._current_sender.startswith("sender:"):
            key = self._current_sender[7:]
            items = [
                i for i in items
                if (str(i.sender_id) == key or i.sender_name == key) and not i.from_me
            ]

        # Type filter
        if self._current_type == "sticker":
            items = [i for i in items if i.type_label == "sticker"]
        elif self._current_type == "no_sticker":
            items = [i for i in items if i.type_label != "sticker"]
        elif self._current_type != "all":
            items = [i for i in items if i.type_category == self._current_type]

        # Date filter
        if self._current_date_filter and self._current_date_filter != "All dates":
            now_ms = int(datetime.now().timestamp() * 1000)
            if self._current_date_filter == "7d":
                cutoff = now_ms - 7 * 24 * 60 * 60 * 1000
                items = [i for i in items if i.timestamp and i.timestamp >= cutoff]
            elif self._current_date_filter == "30d":
                cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
                items = [i for i in items if i.timestamp and i.timestamp >= cutoff]
            elif self._current_date_filter == "90d":
                cutoff = now_ms - 90 * 24 * 60 * 60 * 1000
                items = [i for i in items if i.timestamp and i.timestamp >= cutoff]
            elif self._current_date_filter == "This year":
                year_start = datetime(datetime.now().year, 1, 1)
                cutoff = int(year_start.timestamp() * 1000)
                items = [i for i in items if i.timestamp and i.timestamp >= cutoff]

        # Search
        if self._search_text:
            needle = self._search_text
            items = [
                i for i in items
                if needle in (i.sender_name or "").lower()
                or needle in (i.display_name or "").lower()
                or needle in (i.mime_type or "").lower()
                or needle in (i.type_label or "").lower()
            ]

        # Sort
        if self._current_sort == "oldest":
            items = sorted(items, key=lambda i: (i.timestamp or 0))
        elif self._current_sort == "largest":
            items = sorted(items, key=lambda i: (i.file_size or 0), reverse=True)
        elif self._current_sort == "name":
            items = sorted(items, key=lambda i: (i.display_name or "").lower())
        else:
            items = sorted(items, key=lambda i: (i.timestamp or 0), reverse=True)

        self._filtered_items = items
        self._grid_model.set_items(items)
        self._stats_label.setText(f"{len(items)} shown")
        if not items:
            self._selection_label.setText("No media matches the current filters.")

    def _on_double_click(self, index: QModelIndex):
        """Open preview dialog on double-click."""
        if not index.isValid():
            return
        self._open_preview_at(index.row())

    def _on_grid_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        item: _MediaItem = index.data(Qt.UserRole + 10)
        if not item:
            return
        who = "You" if item.from_me else (item.sender_name or "Unknown")
        when = ""
        if item.timestamp:
            try:
                when = format_timestamp(item.timestamp, '%d %b %Y %H:%M')
            except (ValueError, OSError):
                when = ""
        name = item.display_name or item.type_label.replace("_", " ").title()
        status = item.status.replace("_", " ").title()
        size = _fmt_size(item.file_size)
        summary = "  |  ".join(part for part in [who, when, size, status] if part)
        self._selection_label.setText(f"<b>{name}</b><br>{summary}")

    def _on_context_menu(self, pos):
        idx = self._grid.indexAt(pos)
        if not idx.isValid():
            return
        item: _MediaItem = idx.data(Qt.UserRole + 10)
        if not item:
            return

        tc = self._tc
        lt = self._lt
        menu_bg = "#ffffff" if lt else "#233138"
        menu_border = "rgba(0,0,0,0.12)" if lt else "rgba(255,255,255,0.1)"
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background: {menu_bg}; border: 1px solid {menu_border};
                    border-radius: 6px; padding: 4px; }}
            QMenu::item {{ padding: 5px 14px; color: {tc['text']}; font-size: 10px; }}
            QMenu::item:selected {{ background: {tc['selected']}; }}
        """)

        goto_act = menu.addAction("\u2192  Open Message In Chat")
        goto_act.triggered.connect(lambda: self.navigate_to_message.emit(item.message_id))

        preview_act = menu.addAction("\u2315  Preview Media")
        preview_act.triggered.connect(lambda: self._open_preview_at(idx.row()))

        if item.resolved_path and os.path.isfile(item.resolved_path):
            open_act = menu.addAction("\u2197  Open File")
            open_act.triggered.connect(lambda: self._open_file(item.resolved_path))
            copy_act = menu.addAction("\u2398  Copy Path")
            copy_act.triggered.connect(
                lambda: QApplication.clipboard().setText(item.resolved_path)
            )

        if item.file_hash:
            hash_act = menu.addAction("\u2315  Find All Copies")
            hash_act.triggered.connect(
                lambda: QApplication.clipboard().setText(item.file_hash)
            )

        menu.exec(self._grid.mapToGlobal(pos))

    def _goto_selected(self):
        idx = self._grid.currentIndex()
        if idx.isValid():
            item: _MediaItem = idx.data(Qt.UserRole + 10)
            if item:
                self.navigate_to_message.emit(item.message_id)

    def _open_preview(self):
        idx = self._grid.currentIndex()
        if idx.isValid():
            self._open_preview_at(idx.row())

    def _open_preview_at(self, row: int):
        if not (0 <= row < len(self._filtered_items)):
            return
        item = self._filtered_items[row]
        if not (item.resolved_path and os.path.isfile(item.resolved_path)):
            return  # Cannot preview files not on disk

        from app.views.dialogs.media_viewer_dialog import MediaViewerDialog

        # Build media_list from filtered items that exist on disk
        media_list = []
        current_index = 0
        VIEWABLE_EXT = (
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
            ".mp4", ".3gp", ".avi", ".mkv", ".mov", ".webm", ".m4v",
            ".pdf", ".doc", ".docx", ".txt", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".md", ".rtf",
        )
        VIDEO_EXT = (".mp4", ".3gp", ".avi", ".mkv", ".mov", ".webm", ".m4v")
        for i, it in enumerate(self._filtered_items):
            if not (it.resolved_path and os.path.isfile(it.resolved_path)):
                continue
            ext = os.path.splitext(it.resolved_path)[1].lower()
            if ext not in VIEWABLE_EXT:
                continue
            mtype = "video" if ext in VIDEO_EXT else ("document" if it.type_category == "document" or ext == ".pdf" else "image")
            entry = {
                "file_path": it.resolved_path,
                "sender_name": ("You" if it.from_me else (it.sender_name or "")),
                "timestamp": it.timestamp,
                "file_size": it.file_size or 0,
                "media_type": mtype,
                "message_id": it.message_id,
                "conversation_id": self._conv_id,
            }
            if i == row:
                current_index = len(media_list)
            media_list.append(entry)

        ext = os.path.splitext(item.resolved_path)[1].lower()
        dlg = MediaViewerDialog(
            item.resolved_path, parent=self,
            media_type="video" if ext in VIDEO_EXT else ("document" if item.type_category == "document" or ext == ".pdf" else "image"),
            file_size=item.file_size or 0,
            sender_name=("You" if item.from_me else (item.sender_name or "")),
            timestamp=item.timestamp,
            media_list=media_list if media_list else None,
            current_index=current_index,
            message_id=item.message_id,
            conversation_id=self._conv_id,
        )
        dlg.exec()

    def _open_file(self, path: str):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
