"""
Full-screen media viewer dialog (lightbox) for images, videos, and documents.
Supports:
- Full-resolution image display with zoom (scroll wheel + buttons)
- Animated WebP/GIF playback via Pillow (correct frame compositing)
- Inline video playback via QMediaPlayer + QVideoWidget
- Document preview (PDF, Word, text files)
- Navigation between media in the same conversation
- File info overlay (filename, size, sender, timestamp)
- Play/pause, seek, volume, speed controls for video
- Tag message, go to chat, save copy actions
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QFont, QImage, QImageReader, QKeySequence,
    QPainter, QPainterPath, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from app.services.theme_manager import ThemeManager

# Video extensions we handle inline
_VIDEO_EXTS = (".mp4", ".3gp", ".avi", ".mkv", ".mov", ".webm", ".m4v")
_AUDIO_EXTS = (".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".amr", ".flac")

# Document extensions
_DOC_EXTS = (
    ".pdf", ".doc", ".docx", ".txt", ".csv", ".json",
    ".xml", ".html", ".htm", ".log", ".md", ".rtf",
    ".xls", ".xlsx", ".pptx", ".ppt", ".odt", ".ods",
)


def _has_qt_multimedia() -> bool:
    """Check if QtMultimedia is available."""
    try:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401
        from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: F401
        return True
    except ImportError:
        return False


class MediaViewerDialog(QDialog):
    """Lightbox-style media viewer dialog with prev/next navigation and actions."""

    navigated = Signal(int)                 # emits current index when navigating
    tag_requested = Signal(dict)            # emits current item dict (with message_id)
    go_to_chat_requested = Signal(dict)     # emits current item dict
    download_requested = Signal(dict)       # emits current item dict
    find_similar_requested = Signal(int)    # emits message_id — opens Image Similarity page

    def __init__(self, file_path: str, parent=None, *,
                 sender_name: str = "", timestamp: int | None = None,
                 file_size: int = 0, media_type: str = "image",
                 media_list: list[dict] | None = None,
                 current_index: int = 0,
                 message_id: int | None = None,
                 conversation_id: int | None = None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._file_path = file_path
        self._sender = sender_name
        self._timestamp = timestamp
        self._file_size = file_size
        self._media_type = media_type
        self._zoom = 1.0
        self._pxm: QPixmap | None = None
        self._message_id = message_id
        self._conversation_id = conversation_id

        # Pillow animation state (replaces QMovie for correct WebP compositing)
        self._anim_frames: list[QPixmap] = []
        self._anim_durations: list[int] = []
        self._anim_idx: int = 0
        self._anim_elapsed: int = 0
        self._anim_timer: QTimer | None = None

        # Video player state
        self._player = None       # QMediaPlayer
        self._audio_out = None    # QAudioOutput
        self._video_widget = None  # QVideoWidget
        self._is_video_mode = False
        self._is_audio_mode = False
        self._seeking = False     # user is dragging seek slider

        # Navigation state
        self._media_list = media_list or []
        self._current_idx = current_index

        self.setWindowTitle(os.path.basename(file_path))
        self.setMinimumSize(600, 400)
        self.resize(1000, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)

        is_light = self._tm.is_light
        bg = "#fafafa" if is_light else "#0b141a"
        text_col = "#111b21" if is_light else "#e9edef"
        dim_col = "#667781" if is_light else "rgba(255,255,255,0.5)"
        bar_bg = "#ffffff" if is_light else "#1a2730"
        border = "#e0e3e7" if is_light else "rgba(255,255,255,0.06)"
        accent = "#00897b" if is_light else "#00bcd4"
        self._bg = bg
        self._text_col = text_col
        self._dim_col = dim_col
        self._accent = accent
        self._is_light = is_light
        self._bar_bg = bar_bg
        self._border = border

        self.setStyleSheet(f"""
            QDialog {{ background: {bg}; }}
            QLabel {{ color: {text_col}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Top info bar ----
        info_bar = QWidget()
        info_bar.setFixedHeight(44)
        info_bar.setStyleSheet(f"background: {bar_bg}; border-bottom: 1px solid {border};")
        bar_layout = QHBoxLayout(info_bar)
        bar_layout.setContentsMargins(12, 0, 12, 0)
        bar_layout.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background: {"#f0f2f5" if is_light else "#2a3942"};
                color: {text_col};
                border: 1px solid {border};
                border-radius: 4px;
                font-size: 12px;
                padding: 2px 4px;
            }}
            QPushButton:hover {{
                background: {"#e0e3e7" if is_light else "#3a4952"};
            }}
            QPushButton:disabled {{
                background: {"#f7f8fa" if is_light else "#1a2730"};
                color: {"#c0c0c0" if is_light else "#555"};
            }}
        """
        self._btn_style = btn_style

        def _make_btn(text: str, tooltip: str, width: int = 30, callback=None) -> QPushButton:
            btn = QPushButton(text)
            btn.setFixedSize(width, 30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setStyleSheet(btn_style)
            if callback:
                btn.clicked.connect(callback)
            return btn

        # Navigation: prev
        self._btn_prev = _make_btn("Prev", "Previous (Left arrow)", width=42, callback=self._go_prev)
        bar_layout.addWidget(self._btn_prev)

        # File name + metadata
        self._fname_label = QLabel(os.path.basename(file_path))
        self._fname_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._fname_label.setStyleSheet(f"color: {text_col};")
        bar_layout.addWidget(self._fname_label)

        self._meta_label = QLabel()
        self._meta_label.setStyleSheet(f"color: {dim_col}; font-size: 10px;")
        bar_layout.addWidget(self._meta_label)
        self._update_meta_label()

        # Counter label
        self._counter_label = QLabel()
        self._counter_label.setStyleSheet(f"color: {dim_col}; font-size: 10px; font-weight: bold;")
        self._counter_label.setFixedWidth(70)
        self._counter_label.setAlignment(Qt.AlignCenter)
        bar_layout.addWidget(self._counter_label)

        # Navigation: next
        self._btn_next = _make_btn("Next", "Next (Right arrow)", width=42, callback=self._go_next)
        bar_layout.addWidget(self._btn_next)

        bar_layout.addStretch()

        # Zoom controls (hidden for video/doc)
        self._zoom_out = _make_btn("\u2212", "Zoom out (-)", callback=lambda: self._set_zoom(self._zoom * 0.8))
        bar_layout.addWidget(self._zoom_out)

        self._zoom_label = QLabel("100%")
        self._zoom_label.setFixedWidth(50)
        self._zoom_label.setAlignment(Qt.AlignCenter)
        self._zoom_label.setStyleSheet(f"color: {dim_col}; font-size: 10px;")
        bar_layout.addWidget(self._zoom_label)

        self._zoom_in = _make_btn("+", "Zoom in (+)", callback=lambda: self._set_zoom(self._zoom * 1.25))
        bar_layout.addWidget(self._zoom_in)

        self._zoom_fit = _make_btn("Fit", "Fit to window (0)", width=36, callback=lambda: self._set_zoom(0))
        bar_layout.addWidget(self._zoom_fit)

        # ---- Action buttons (icon-only, 30px each) ----
        self._btn_open = _make_btn("\u2197", "Open in default app", callback=self._open_external)
        bar_layout.addWidget(self._btn_open)

        self._btn_save = _make_btn("\u2B07", "Save a copy", callback=self._save_copy)
        bar_layout.addWidget(self._btn_save)

        self._btn_tag = _make_btn("\u2691", "Tag this message", callback=self._on_tag)
        bar_layout.addWidget(self._btn_tag)

        self._btn_goto = _make_btn("Chat", "Open this message in chat", width=40, callback=self._on_goto_chat)
        bar_layout.addWidget(self._btn_goto)

        # "Find Similar Images" — perceptual-hash based search for
        # near-duplicates (resizes, recompressions, template
        # matches).  Image-only feature: hidden for video / audio
        # / document because pHash/dHash don't apply.  Visibility
        # is reconciled in ``_apply_to_image_or_video`` whenever
        # the user navigates to a different item via prev / next.
        self._btn_similar = _make_btn(
            "\U0001F50D",
            "Find similar images (perceptual pHash / dHash)",
            width=30,
            callback=self._on_find_similar,
        )
        bar_layout.addWidget(self._btn_similar)

        layout.addWidget(info_bar)

        # ---- Content area: stacked widget for image/video/doc ----
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        # Page 0: Image display area with scroll
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {bg}; }}")
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._scroll.setWidget(self._image_label)
        self._stack.addWidget(self._scroll)  # index 0

        # Page 1: Video player (created on demand)
        self._video_container = QWidget()
        self._video_container.setStyleSheet(f"background: {bg};")
        video_layout = QVBoxLayout(self._video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        if _has_qt_multimedia():
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self._video_widget = QVideoWidget()
            self._video_widget.setStyleSheet("background: black;")
            video_layout.addWidget(self._video_widget, 1)

            # Video controls bar
            ctrl_bar = QWidget()
            ctrl_bar.setFixedHeight(48)
            ctrl_bar.setStyleSheet(
                f"background: {bar_bg}; border-top: 1px solid {border};"
            )
            ctrl_layout = QHBoxLayout(ctrl_bar)
            ctrl_layout.setContentsMargins(12, 0, 12, 0)

            self._btn_play = QPushButton("\u25B6")
            self._btn_play.setFixedSize(36, 36)
            self._btn_play.setCursor(Qt.PointingHandCursor)
            self._btn_play.setStyleSheet(f"""
                QPushButton {{
                    background: {accent};
                    color: white;
                    border: none;
                    border-radius: 18px;
                    font-size: 16px;
                }}
                QPushButton:hover {{ background: {"#00796b" if is_light else "#0097a7"}; }}
            """)
            self._btn_play.clicked.connect(self._toggle_play)
            ctrl_layout.addWidget(self._btn_play)

            self._time_label = QLabel("0:00")
            self._time_label.setFixedWidth(48)
            self._time_label.setStyleSheet(f"color: {dim_col}; font-size: 10px;")
            ctrl_layout.addWidget(self._time_label)

            slider_style = f"""
                QSlider::groove:horizontal {{
                    height: 4px;
                    background: {"#d0d4d8" if is_light else "#3a4952"};
                    border-radius: 2px;
                }}
                QSlider::handle:horizontal {{
                    background: {accent};
                    width: 14px;
                    height: 14px;
                    margin: -5px 0;
                    border-radius: 7px;
                }}
                QSlider::sub-page:horizontal {{
                    background: {accent};
                    border-radius: 2px;
                }}
            """

            self._seek_slider = QSlider(Qt.Horizontal)
            self._seek_slider.setRange(0, 1000)
            self._seek_slider.setStyleSheet(slider_style)
            self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
            self._seek_slider.sliderReleased.connect(self._on_seek_released)
            self._seek_slider.sliderMoved.connect(self._on_seek_moved)
            ctrl_layout.addWidget(self._seek_slider, 1)

            self._duration_label = QLabel("0:00")
            self._duration_label.setFixedWidth(48)
            self._duration_label.setStyleSheet(f"color: {dim_col}; font-size: 10px;")
            ctrl_layout.addWidget(self._duration_label)

            # Volume
            vol_icon = QLabel("\u266A")
            vol_icon.setStyleSheet(f"color: {dim_col}; font-size: 12px;")
            ctrl_layout.addWidget(vol_icon)

            self._vol_slider = QSlider(Qt.Horizontal)
            self._vol_slider.setRange(0, 100)
            # Persisted volume preference: read / written to a
            # QSettings key so the slider opens at whatever level
            # the user last selected, instead of snapping back to
            # a hard-coded default on every dialog open.
            try:
                _settings = QSettings("WAInsight", "MediaViewer")
                _saved_vol = int(_settings.value("video_volume", 100))
            except Exception:
                _saved_vol = 100
            _saved_vol = max(0, min(100, _saved_vol))
            self._vol_slider.setValue(_saved_vol)
            self._vol_slider.setFixedWidth(80)
            self._vol_slider.setStyleSheet(slider_style)
            self._vol_slider.valueChanged.connect(self._on_volume_changed)
            ctrl_layout.addWidget(self._vol_slider)

            # Speed
            self._speed_btn = QPushButton("1x")
            self._speed_btn.setFixedSize(36, 28)
            self._speed_btn.setCursor(Qt.PointingHandCursor)
            self._speed_btn.setStyleSheet(btn_style)
            self._speed_btn.setToolTip("Playback speed")
            self._speed_btn.clicked.connect(self._cycle_speed)
            ctrl_layout.addWidget(self._speed_btn)
            self._speed_idx = 2
            self._speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

            video_layout.addWidget(ctrl_bar)

            # Create media player.  Initial volume comes from the
            # QSettings-backed slider value above so playback starts
            # at the level the user actually wants.
            self._audio_out = QAudioOutput()
            self._audio_out.setVolume(_saved_vol / 100.0)
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio_out)
            self._player.setVideoOutput(self._video_widget)
            self._player.positionChanged.connect(self._on_position_changed)
            self._player.durationChanged.connect(self._on_duration_changed)
            self._player.playbackStateChanged.connect(self._on_playback_state_changed)
            self._player.errorOccurred.connect(self._on_player_error)
        else:
            fallback_label = QLabel("Video playback not available\nQt Multimedia not installed")
            fallback_label.setAlignment(Qt.AlignCenter)
            fallback_label.setStyleSheet(f"color: {dim_col}; font-size: 14px;")
            video_layout.addWidget(fallback_label)

        self._stack.addWidget(self._video_container)  # index 1

        # Page 2: Document viewer
        self._doc_container = QWidget()
        self._doc_container.setStyleSheet(f"background: {bg};")
        doc_layout = QVBoxLayout(self._doc_container)
        doc_layout.setContentsMargins(0, 0, 0, 0)
        doc_layout.setSpacing(0)

        from PySide6.QtWidgets import QTextBrowser
        self._doc_browser = QTextBrowser()
        self._doc_browser.setOpenExternalLinks(False)
        self._doc_browser.setReadOnly(True)
        self._doc_browser.setStyleSheet(
            f"QTextBrowser {{ background: {'#ffffff' if is_light else '#1a2730'}; "
            f"color: {text_col}; border: none; padding: 16px; "
            f"font-family: 'Segoe UI', 'Consolas', monospace; font-size: 11pt; }}"
        )
        doc_layout.addWidget(self._doc_browser, 1)

        self._doc_info = QLabel()
        self._doc_info.setFixedHeight(32)
        self._doc_info.setAlignment(Qt.AlignCenter)
        self._doc_info.setStyleSheet(
            f"background: {bar_bg}; border-top: 1px solid {border}; "
            f"color: {dim_col}; font-size: 10px; padding: 4px;"
        )
        doc_layout.addWidget(self._doc_info)
        self._stack.addWidget(self._doc_container)  # index 2

        # ---- Keyboard shortcuts ----
        for key, fn in [
            ("Escape", self.close),
            ("+", lambda: self._set_zoom(self._zoom * 1.25)),
            ("-", lambda: self._set_zoom(self._zoom * 0.8)),
            ("0", lambda: self._set_zoom(0)),
        ]:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)

        sc_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        sc_space.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_space.activated.connect(self._toggle_play)

        sc_left = QShortcut(QKeySequence(Qt.Key_Left), self)
        sc_left.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_left.activated.connect(self._go_prev)
        sc_right = QShortcut(QKeySequence(Qt.Key_Right), self)
        sc_right.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_right.activated.connect(self._go_next)

        self._scroll.setFocusPolicy(Qt.NoFocus)

        # Update nav state & load
        self._update_nav_buttons()
        self._update_similar_button_visibility()
        self._load_media()

    # ---- Helpers ----

    def _current_item_dict(self) -> dict:
        """Build a dict describing the currently displayed media item."""
        d = {
            "file_path": self._file_path,
            "sender_name": self._sender,
            "timestamp": self._timestamp,
            "file_size": self._file_size,
            "media_type": self._media_type,
            "message_id": self._message_id,
            "conversation_id": self._conversation_id,
        }
        if self._media_list and 0 <= self._current_idx < len(self._media_list):
            item = self._media_list[self._current_idx]
            d["message_id"] = item.get("message_id", d["message_id"])
            d["conversation_id"] = item.get("conversation_id", d["conversation_id"])
        return d

    # ---- Navigation ----

    def _go_prev(self):
        if not self._media_list or self._current_idx <= 0:
            return
        self._current_idx -= 1
        self._navigate_to(self._current_idx)

    def _go_next(self):
        if not self._media_list or self._current_idx >= len(self._media_list) - 1:
            return
        self._current_idx += 1
        self._navigate_to(self._current_idx)

    def _navigate_to(self, idx: int):
        item = self._media_list[idx]
        self._file_path = item.get("file_path", "")
        self._sender = item.get("sender_name", "")
        self._timestamp = item.get("timestamp")
        self._file_size = item.get("file_size", 0)
        self._media_type = item.get("media_type", "image")
        self._message_id = item.get("message_id")
        self._conversation_id = item.get("conversation_id")
        self._zoom = 1.0
        self._pxm = None

        # Stop any animation
        self._stop_animation()
        # Stop any video
        self._stop_video()

        self.setWindowTitle(os.path.basename(self._file_path) if self._file_path else "Media")
        self._fname_label.setText(os.path.basename(self._file_path) if self._file_path else "")
        self._update_meta_label()
        self._update_nav_buttons()
        self._update_similar_button_visibility()
        self._load_media()
        self.navigated.emit(idx)

    def _update_nav_buttons(self):
        has_list = len(self._media_list) > 1
        self._btn_prev.setVisible(has_list)
        self._btn_next.setVisible(has_list)
        self._counter_label.setVisible(has_list)
        if has_list:
            self._btn_prev.setEnabled(self._current_idx > 0)
            self._btn_next.setEnabled(self._current_idx < len(self._media_list) - 1)
            self._counter_label.setText(f"{self._current_idx + 1} / {len(self._media_list)}")

    def _update_meta_label(self):
        meta_parts = []
        if self._sender:
            meta_parts.append(self._sender)
        if self._timestamp:
            try:
                meta_parts.append(format_timestamp(self._timestamp, '%b %d, %Y %H:%M'))
            except (ValueError, OSError):
                pass
        if self._file_size and self._file_size > 0:
            if self._file_size >= 1_048_576:
                meta_parts.append(f"{self._file_size / 1_048_576:.1f} MB")
            else:
                meta_parts.append(f"{self._file_size // 1024} KB")
        self._meta_label.setText("  \u2022  ".join(meta_parts) if meta_parts else "")

    # ---- Media loading ----

    def _is_video(self) -> bool:
        if self._media_type == "video":
            return True
        if self._file_path:
            return self._file_path.lower().endswith(_VIDEO_EXTS)
        return False

    def _is_audio(self) -> bool:
        if self._media_type in ("audio", "voice", "ptt"):
            return True
        if self._file_path:
            return self._file_path.lower().endswith(_AUDIO_EXTS)
        return False

    def _is_document(self) -> bool:
        if self._file_path:
            ext = os.path.splitext(self._file_path)[1].lower()
            return ext in _DOC_EXTS
        return False

    def _load_media(self):
        """Load the current media file into the appropriate viewer."""
        # Stop previous animation
        self._stop_animation()
        self._image_label.setCursor(Qt.ArrowCursor)
        try:
            self._image_label.mousePressEvent = lambda e: None
        except Exception:
            pass

        if self._is_video():
            self._load_video()
            return

        if self._is_audio():
            self._load_audio()
            return

        if self._is_document():
            self._load_document()
            return

        # Image mode
        self._is_video_mode = False
        self._stack.setCurrentIndex(0)
        self._zoom_out.setVisible(True)
        self._zoom_in.setVisible(True)
        self._zoom_fit.setVisible(True)
        self._zoom_label.setVisible(True)

        # Check for animated WebP/GIF — use Pillow for correct compositing
        ext = os.path.splitext(self._file_path)[1].lower() if self._file_path else ""
        if ext in (".webp", ".gif") and self._file_path and os.path.isfile(self._file_path):
            if self._try_pillow_animation():
                return

        # Load static image with EXIF auto-rotation.
        # IMPORTANT: ``QPixmap.load()`` does NOT honour EXIF
        # orientation tags, so portrait JPEGs taken by phone cameras
        # (which store the sensor's native landscape pixels + an
        # orientation=6 tag) ended up displayed sideways.  Always
        # try ``QImageReader.setAutoTransform(True)`` first so the
        # tag is applied; only fall through to QPixmap.load if the
        # reader can't decode the file at all.
        pxm = QPixmap()
        loaded = False
        if self._file_path and os.path.isfile(self._file_path):
            try:
                reader = QImageReader(self._file_path)
                reader.setAutoTransform(True)
                img = reader.read()
                if not img.isNull():
                    pxm = QPixmap.fromImage(img)
                    loaded = True
            except Exception:
                loaded = False
            if not loaded:
                # Fallback for formats QImageReader can't handle
                pxm = QPixmap()
                if pxm.load(self._file_path):
                    loaded = True

        if not loaded or pxm.isNull():
            self._image_label.setText("Unable to load media file")
            return

        self._pxm = pxm
        self._set_zoom(0)

    # ---- Pillow-based animation (replaces QMovie) ----

    def _try_pillow_animation(self) -> bool:
        """Try to load animated WebP/GIF using Pillow. Returns True on success."""
        try:
            from PIL import Image
            img = Image.open(self._file_path)
            if not hasattr(img, 'n_frames') or img.n_frames <= 1:
                return False

            frames = []
            durations = []
            for i in range(min(img.n_frames, 120)):  # cap at 120 frames
                img.seek(i)
                frame = img.convert("RGBA")
                data = frame.tobytes("raw", "RGBA")
                qimg = QImage(data, frame.width, frame.height,
                              frame.width * 4, QImage.Format_RGBA8888).copy()
                pxm = QPixmap.fromImage(qimg)
                if pxm.isNull():
                    continue
                frames.append(pxm)
                dur = img.info.get("duration", 33)
                if dur < 10:
                    dur = 33
                durations.append(dur)

            if not frames:
                return False

            self._anim_frames = frames
            self._anim_durations = durations
            self._anim_idx = 0
            self._anim_elapsed = 0
            self._pxm = frames[0]  # store for zoom reference

            # Show first frame scaled to viewport
            self._show_anim_frame()

            # Start timer
            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(50)  # 20fps check
            self._anim_timer.timeout.connect(self._advance_anim_frame)
            self._anim_timer.start()

            self._zoom_label.setText("Anim")
            return True
        except Exception:
            return False

    def _show_anim_frame(self):
        """Display the current animation frame, scaled to fit viewport."""
        if not self._anim_frames:
            return
        pxm = self._anim_frames[self._anim_idx]
        avail = self._scroll.size()
        sw, sh = avail.width() - 20, avail.height() - 20
        scaled = pxm.scaled(sw, sh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._image_label.setPixmap(scaled)

    def _advance_anim_frame(self):
        """Timer callback to advance the animation frame index."""
        if not self._anim_frames:
            return
        self._anim_elapsed += 50  # timer interval
        dur = self._anim_durations[self._anim_idx]
        changed = False
        while dur > 0 and self._anim_elapsed >= dur:
            self._anim_elapsed -= dur
            self._anim_idx = (self._anim_idx + 1) % len(self._anim_frames)
            changed = True
            dur = self._anim_durations[self._anim_idx]
        if changed:
            self._show_anim_frame()

    def _stop_animation(self):
        """Stop Pillow-based animation and clean up."""
        if self._anim_timer:
            self._anim_timer.stop()
            self._anim_timer = None
        self._anim_frames = []
        self._anim_durations = []
        self._anim_idx = 0
        self._anim_elapsed = 0

    # ---- Document loading ----

    def _load_document(self):
        """Load and display a document (PDF, text, Word, etc.)."""
        self._is_video_mode = False
        self._stack.setCurrentIndex(2)
        self._zoom_out.setVisible(False)
        self._zoom_in.setVisible(False)
        self._zoom_fit.setVisible(False)
        self._zoom_label.setVisible(False)

        if not self._file_path or not os.path.isfile(self._file_path):
            self._doc_browser.setPlainText("File not found.")
            return

        ext = os.path.splitext(self._file_path)[1].lower()
        size_bytes = os.path.getsize(self._file_path)

        if size_bytes > 10_000_000:
            self._doc_browser.setPlainText(
                f"File too large for inline preview ({size_bytes / 1_048_576:.1f} MB).\n\n"
                "Use '\u2197' button to view in external application."
            )
            self._doc_info.setText(f"{ext.upper()[1:]} document  |  {size_bytes / 1_048_576:.1f} MB")
            return

        if ext == ".pdf":
            self._load_pdf()
            return

        if ext in (".txt", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".md", ".rtf"):
            try:
                with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)
                if ext in (".html", ".htm"):
                    self._doc_browser.setHtml(content)
                else:
                    self._doc_browser.setPlainText(content)
                self._doc_info.setText(
                    f"{ext.upper()[1:]} file  |  {size_bytes:,} bytes  |  "
                    f"{content.count(chr(10)) + 1} lines"
                )
            except Exception as e:
                self._doc_browser.setPlainText(f"Error reading file: {e}")
            return

        if ext == ".docx":
            try:
                import docx
                doc = docx.Document(self._file_path)
                paragraphs = [p.text for p in doc.paragraphs]
                self._doc_browser.setPlainText("\n".join(paragraphs))
                self._doc_info.setText(
                    f"Word Document  |  {size_bytes:,} bytes  |  {len(paragraphs)} paragraphs"
                )
            except ImportError:
                self._doc_browser.setPlainText(
                    "Word document (.docx)\n\n"
                    "python-docx not installed. Use '\u2197' button to view.\n\n"
                    "Install: pip install python-docx"
                )
                self._doc_info.setText("Word Document  |  python-docx not installed")
            except Exception as e:
                self._doc_browser.setPlainText(f"Error reading Word document: {e}")
            return

        # Fallback
        self._doc_browser.setPlainText(
            f"Document: {os.path.basename(self._file_path)}\n"
            f"Type: {ext.upper()[1:]}\n"
            f"Size: {size_bytes:,} bytes\n\n"
            "Use '\u2197' button to view in external application."
        )
        self._doc_info.setText(f"{ext.upper()[1:]} document  |  {size_bytes:,} bytes")

    def _load_pdf(self):
        """Load PDF using PyMuPDF or pypdf."""
        size_bytes = os.path.getsize(self._file_path)

        # Try PyMuPDF
        try:
            import fitz
            doc = fitz.open(self._file_path)
            total_pages = len(doc)
            text_parts = []
            for i, page in enumerate(doc):
                if i >= 50:
                    text_parts.append(f"\n--- (Showing first 50 of {total_pages} pages) ---")
                    break
                text_parts.append(f"--- Page {i + 1} ---\n")
                text_parts.append(page.get_text())
            doc.close()
            self._doc_browser.setPlainText("\n".join(text_parts))
            self._doc_info.setText(f"PDF  |  {total_pages} pages  |  {size_bytes:,} bytes")
            return
        except ImportError:
            pass

        # Try pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(self._file_path)
            total_pages = len(reader.pages)
            text_parts = []
            for i, page in enumerate(reader.pages):
                if i >= 50:
                    text_parts.append(f"\n--- (Showing first 50 of {total_pages} pages) ---")
                    break
                text_parts.append(f"--- Page {i + 1} ---\n")
                text_parts.append(page.extract_text() or "")
            self._doc_browser.setPlainText("\n".join(text_parts))
            self._doc_info.setText(f"PDF  |  {total_pages} pages  |  {size_bytes:,} bytes")
            return
        except ImportError:
            pass

        self._doc_browser.setPlainText(
            f"PDF Document: {os.path.basename(self._file_path)}\n"
            f"Size: {size_bytes:,} bytes\n\n"
            "No PDF reader library installed.\n"
            "Install one: pip install PyMuPDF  or  pip install pypdf\n\n"
            "Use '\u2197' button to view in external application."
        )
        self._doc_info.setText(f"PDF  |  {size_bytes:,} bytes  |  No PDF library")

    # ---- Video playback ----

    def _load_video(self):
        if not self._player or not self._video_widget:
            self._show_video_fallback()
            return

        self._is_video_mode = True
        self._is_audio_mode = False
        self._video_widget.setVisible(True)
        self._stack.setCurrentIndex(1)
        self._zoom_out.setVisible(False)
        self._zoom_in.setVisible(False)
        self._zoom_fit.setVisible(False)
        self._zoom_label.setVisible(False)

        if not self._file_path or not os.path.isfile(self._file_path):
            return

        url = QUrl.fromLocalFile(os.path.abspath(self._file_path))
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(url)
        self._player.play()

    def _load_audio(self):
        if not self._player or not self._video_widget:
            self._show_audio_fallback()
            return

        self._is_video_mode = False
        self._is_audio_mode = True
        self._stack.setCurrentIndex(1)
        self._video_widget.setVisible(False)
        self._zoom_out.setVisible(False)
        self._zoom_in.setVisible(False)
        self._zoom_fit.setVisible(False)
        self._zoom_label.setVisible(False)

        if not self._file_path or not os.path.isfile(self._file_path):
            return

        url = QUrl.fromLocalFile(os.path.abspath(self._file_path))
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(url)
        self._player.play()

    def _stop_video(self):
        self._is_audio_mode = False
        if self._player:
            self._player.stop()
            self._player.setSource(QUrl())

    def _toggle_play(self):
        if not self._player or not (self._is_video_mode or self._is_audio_mode):
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_position_changed(self, position: int):
        if not self._seeking:
            duration = self._player.duration() if self._player else 1
            if duration > 0:
                self._seek_slider.setValue(int(position * 1000 / duration))
        self._time_label.setText(self._fmt_time(position))

    def _on_duration_changed(self, duration: int):
        self._duration_label.setText(self._fmt_time(duration))

    def _on_playback_state_changed(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        if state == QMediaPlayer.PlayingState:
            self._btn_play.setText("\u275A\u275A")
        else:
            self._btn_play.setText("\u25B6")

    def _on_player_error(self, error, msg=""):
        self._show_video_fallback()

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_released(self):
        self._seeking = False
        if self._player and self._player.duration() > 0:
            pos = int(self._seek_slider.value() * self._player.duration() / 1000)
            self._player.setPosition(pos)

    def _on_seek_moved(self, value: int):
        if self._player and self._player.duration() > 0:
            pos = int(value * self._player.duration() / 1000)
            self._player.setPosition(pos)

    def _on_volume_changed(self, value: int):
        if self._audio_out:
            self._audio_out.setVolume(value / 100.0)
        # Persist the user's selection so the next MediaViewerDialog
        # opens at the same level (rather than snapping back to a
        # hard-coded default).
        try:
            QSettings("WAInsight", "MediaViewer").setValue("video_volume", int(value))
        except Exception:
            pass

    def _cycle_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(self._speeds)
        speed = self._speeds[self._speed_idx]
        if self._player:
            self._player.setPlaybackRate(speed)
        label = f"{speed}x" if speed != int(speed) else f"{int(speed)}x"
        self._speed_btn.setText(label)

    def _show_video_fallback(self):
        self._is_video_mode = False
        self._is_audio_mode = False
        self._stack.setCurrentIndex(0)
        self._zoom_out.setVisible(False)
        self._zoom_in.setVisible(False)
        self._zoom_fit.setVisible(False)
        self._zoom_label.setVisible(False)

        is_light = self._is_light
        w, h = 400, 300
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor("#f0f2f5" if is_light else "#1a2730"))

        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = w // 2 - 40, h // 2 - 40
        circle = QPainterPath()
        circle.addEllipse(float(cx), float(cy), 80.0, 80.0)
        p.fillPath(circle, QColor(0, 137, 123) if is_light else QColor(0, 188, 212))
        p.setFont(QFont("Segoe UI", 30, QFont.Bold))
        p.setPen(QColor(255, 255, 255))
        p.drawText(cx, cy, 80, 80, Qt.AlignCenter, "\u25B6")
        p.setFont(QFont("Segoe UI", 12))
        p.setPen(QColor("#667781" if is_light else "#8696a0"))
        p.drawText(0, h - 50, w, 30, Qt.AlignCenter, "Click to open in external player")
        p.end()

        self._pxm = QPixmap.fromImage(img)
        self._image_label.setPixmap(self._pxm)
        self._image_label.setCursor(Qt.PointingHandCursor)
        self._image_label.mousePressEvent = lambda e: self._open_external()

    def _show_audio_fallback(self):
        self._show_video_fallback()

    @staticmethod
    def _fmt_time(ms: int) -> str:
        s = max(0, ms // 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    # ---- Image zoom ----

    def _set_zoom(self, level: float):
        if not self._pxm:
            return
        # Stop animation if zooming manually
        if self._anim_frames and level != 0:
            pass  # keep animation running, but allow zoom
        if level <= 0:
            avail = self._scroll.size()
            sw = avail.width() - 20
            sh = avail.height() - 20
            scaled = self._pxm.scaled(sw, sh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._image_label.setPixmap(scaled)
            self._zoom = scaled.width() / self._pxm.width() if self._pxm.width() > 0 else 1.0
        else:
            level = max(0.1, min(level, 5.0))
            self._zoom = level
            w = int(self._pxm.width() * level)
            h = int(self._pxm.height() * level)
            scaled = self._pxm.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._image_label.setPixmap(scaled)
            self._image_label.resize(scaled.size())
        self._zoom_label.setText(f"{int(self._zoom * 100)}%")

    # ---- Action buttons ----

    def _open_external(self):
        if not self._file_path or not os.path.isfile(self._file_path):
            return
        if sys.platform == "win32":
            os.startfile(self._file_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self._file_path])
        else:
            subprocess.Popen(["xdg-open", self._file_path])

    def _save_copy(self):
        """Save a copy of the current media file to a user-chosen location."""
        if not self._file_path or not os.path.isfile(self._file_path):
            return
        basename = os.path.basename(self._file_path)
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save Media Copy", basename,
            "All Files (*.*)"
        )
        if dest:
            try:
                shutil.copy2(self._file_path, dest)
            except Exception:
                pass

    def _on_tag(self):
        """Emit tag request for the current media item."""
        self.tag_requested.emit(self._current_item_dict())

    def _on_find_similar(self):
        """Emit a find-similar request for the current item.  The
        chat viewer page wires this to its existing
        ``find_similar_requested`` handler, which opens the Image
        Similarity page seeded with this message's pHash / dHash.
        We close the dialog first so the similarity page isn't
        hidden behind the lightbox."""
        item = self._current_item_dict()
        mid = item.get("message_id") or self._message_id or 0
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            mid = 0
        if not mid:
            return
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:
            pass
        self.find_similar_requested.emit(mid)
        self.accept()

    def _update_similar_button_visibility(self):
        """Show the Find Similar button only for image-like
        media.  Perceptual hashing is image-only (pHash + dHash
        operate on a 2-D pixel grid); videos and audio aren't
        meaningful inputs."""
        try:
            ext = os.path.splitext(self._file_path)[1].lower()
        except Exception:
            ext = ""
        is_image = (
            self._media_type in ("image", "sticker", "gif", "animated_gif")
            or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
                       ".heic", ".heif")
        )
        try:
            self._btn_similar.setVisible(is_image)
        except Exception:
            pass

    def _on_goto_chat(self):
        """Emit a go-to-chat request and close the dialog so the
        chat scroll is actually visible — leaving the lightbox
        open after emitting would cover the very message we just
        scrolled to.  Stops any video playback before closing.
        """
        # Stop the player first so audio doesn't keep playing in the
        # background after the dialog closes.
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:
            pass
        self.go_to_chat_requested.emit(self._current_item_dict())
        self.accept()

    # ---- Events ----

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._anim_frames:
            self._show_anim_frame()
        elif self._pxm and self._zoom <= 0:
            self._set_zoom(0)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self._go_prev()
            event.accept()
            return
        if event.key() == Qt.Key_Right:
            self._go_next()
            event.accept()
            return
        if event.key() == Qt.Key_Space:
            self._toggle_play()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._is_video() or self._is_audio():
                self._toggle_play()
                return
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._stop_animation()
        if self._player:
            self._player.stop()
            self._player.setSource(QUrl())
        super().closeEvent(event)
