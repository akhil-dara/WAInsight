"""
Image lightbox — fullscreen-ish dialog that shows a crisp image with
zoom / pan / keyboard shortcuts / download.

Used by:
    - Image Similarity result card (double-click)
    - Any page that wants a quick "open full image" action
"""
from __future__ import annotations

import os
from datetime import datetime
from app.config import format_timestamp  # tz-aware fmt
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QEvent, QPoint
from PySide6.QtGui import QFont, QKeyEvent, QPixmap, QWheelEvent, QMouseEvent
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


def show_lightbox(parent, pixmap: QPixmap, result: dict | None = None) -> None:
    """Convenience entry point."""
    dlg = ImageLightbox(parent, pixmap, result or {})
    dlg.exec()


class ImageLightbox(QDialog):
    """Pan-zoom image viewer.

    Keyboard:
        +/=  zoom in          -/_  zoom out
        0    100%             F    fit to window
        D    download as…     Esc  close
    Mouse:
        wheel  = zoom around cursor
        drag   = pan when zoomed in
        dbl-click = toggle 1× ↔ 2×
    """

    def __init__(self, parent, pixmap: QPixmap, result: dict):
        super().__init__(parent)
        self.setWindowTitle("Image preview")
        self.setModal(True)
        self.resize(1050, 740)
        self.setStyleSheet(
            "QDialog { background: #0b141a; }"
        )

        self._pixmap = pixmap
        self._result = result or {}
        self._zoom = 1.0
        self._fit_mode = True      # True = fit to window on resize
        self._panning = False
        self._pan_origin = QPoint()
        self._scroll_start = QPoint()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Top bar ----
        top = QHBoxLayout()
        top.setContentsMargins(12, 8, 12, 8)
        top.setSpacing(10)
        info = self._build_info_label()
        top.addWidget(info, 1)

        # Toolbar buttons
        self._lbl_zoom = QLabel("100%")
        self._lbl_zoom.setStyleSheet("color: #b0bec5; font-size: 11px;")
        top.addWidget(self._lbl_zoom)

        def _tb(label, tip, cb):
            b = QPushButton(label)
            b.setFixedSize(36, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(self._btn_style())
            b.clicked.connect(cb)
            return b

        top.addWidget(_tb("\u2212", "Zoom out (\u2212)", lambda: self._zoom_by(0.8)))
        top.addWidget(_tb("+", "Zoom in (+)", lambda: self._zoom_by(1.25)))
        top.addWidget(_tb("100%", "Actual size (0)", lambda: self._set_zoom(1.0)))
        top.addWidget(_tb("Fit", "Fit to window (F)", self._fit))
        top.addWidget(_tb("\u21E9", "Download\u2026 (D)", self._download))
        top.addWidget(_tb("\u2715", "Close (Esc)", self.reject))
        root.addLayout(top)

        # ---- Image viewport ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._scroll.setStyleSheet(
            "QScrollArea { background: #0b141a; border: none; }"
            "QScrollBar:vertical, QScrollBar:horizontal {"
            "  background: transparent; width: 12px; height: 12px; }"
            "QScrollBar::handle:vertical, QScrollBar::handle:horizontal {"
            "  background: rgba(255,255,255,0.22); border-radius: 6px; }"
        )
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setStyleSheet("background: #0b141a;")
        self._scroll.setWidget(self._image_label)
        root.addWidget(self._scroll, 1)

        self._scroll.viewport().installEventFilter(self)
        self._image_label.installEventFilter(self)

        # Initial display
        self._render_pixmap()
        # Defer fit-on-open until the widget actually has a size
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._fit)

    # ------------------------------------------------------------------
    # Info bar
    # ------------------------------------------------------------------

    def _build_info_label(self) -> QLabel:
        r = self._result
        conv = r.get("conv_name") or ""
        sender = r.get("sender_name") or ""
        ts = r.get("timestamp", 0)
        ts_str = ""
        if ts:
            try:
                ts_str = format_timestamp(ts, "minute")
            except Exception:
                pass
        fp = r.get("file_path") or ""
        fname = os.path.basename(fp) if fp else ""
        w = self._pixmap.width()
        h = self._pixmap.height()

        html = f'<span style="color:#eceff1;font-size:12.5px;font-weight:600">'
        if fname:
            html += fname
        else:
            html += "Image preview"
        html += "</span>"
        bits = []
        if w and h:
            bits.append(f"{w}\u00D7{h}")
        if conv:
            bits.append(f'\U0001F4AC {conv}')
        if sender and sender != "You":
            bits.append(f'\U0001F464 {sender}')
        if ts_str:
            bits.append(f'\U0001F550 {ts_str}')
        if bits:
            html += f'<br><span style="color:#90a4ae;font-size:11px">{" \u00B7 ".join(bits)}</span>'
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet("color: #eceff1;")
        return lbl

    @staticmethod
    def _btn_style() -> str:
        return (
            "QPushButton { background: rgba(255,255,255,0.08);"
            " color: #eceff1; border: 1px solid rgba(255,255,255,0.12);"
            " border-radius: 5px; font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }"
        )

    # ------------------------------------------------------------------
    # Zoom / pan
    # ------------------------------------------------------------------

    def _render_pixmap(self):
        if self._pixmap.isNull():
            return
        w = int(self._pixmap.width() * self._zoom)
        h = int(self._pixmap.height() * self._zoom)
        if w < 1 or h < 1:
            return
        scaled = self._pixmap.scaled(
            QSize(w, h), Qt.KeepAspectRatio,
            Qt.SmoothTransformation if self._zoom <= 2.0 else Qt.FastTransformation,
        )
        self._image_label.setPixmap(scaled)
        self._image_label.resize(scaled.size())
        self._lbl_zoom.setText(f"{int(round(self._zoom * 100))}%")

    def _fit(self):
        if self._pixmap.isNull():
            return
        vp = self._scroll.viewport().size()
        if vp.width() < 10 or vp.height() < 10:
            return
        zw = vp.width() / self._pixmap.width()
        zh = vp.height() / self._pixmap.height()
        self._zoom = max(0.05, min(zw, zh))
        self._fit_mode = True
        self._render_pixmap()

    def _set_zoom(self, z: float):
        self._zoom = max(0.1, min(10.0, z))
        self._fit_mode = False
        self._render_pixmap()

    def _zoom_by(self, factor: float):
        self._set_zoom(self._zoom * factor)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._fit_mode:
            self._fit()

    def eventFilter(self, obj, ev) -> bool:
        et = ev.type()
        # Wheel = zoom at cursor
        if et == QEvent.Type.Wheel and isinstance(ev, QWheelEvent):
            factor = 1.25 if ev.angleDelta().y() > 0 else 0.8
            self._zoom_by(factor)
            return True
        # Drag-to-pan
        if et == QEvent.Type.MouseButtonPress and isinstance(ev, QMouseEvent):
            if ev.button() == Qt.LeftButton and self._image_is_larger_than_viewport():
                self._panning = True
                self._pan_origin = ev.globalPos()
                self._scroll_start = QPoint(
                    self._scroll.horizontalScrollBar().value(),
                    self._scroll.verticalScrollBar().value(),
                )
                self._image_label.setCursor(Qt.ClosedHandCursor)
                return True
        if et == QEvent.Type.MouseButtonRelease and self._panning:
            self._panning = False
            self._image_label.setCursor(Qt.ArrowCursor)
            return True
        if et == QEvent.Type.MouseMove and self._panning and isinstance(ev, QMouseEvent):
            delta = ev.globalPos() - self._pan_origin
            self._scroll.horizontalScrollBar().setValue(self._scroll_start.x() - delta.x())
            self._scroll.verticalScrollBar().setValue(self._scroll_start.y() - delta.y())
            return True
        if et == QEvent.Type.MouseButtonDblClick:
            self._set_zoom(2.0 if self._zoom < 1.5 else 1.0)
            return True
        return super().eventFilter(obj, ev)

    def _image_is_larger_than_viewport(self) -> bool:
        vp = self._scroll.viewport().size()
        return (self._image_label.width() > vp.width()
                or self._image_label.height() > vp.height())

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def keyPressEvent(self, ev: QKeyEvent):
        k = ev.key()
        mods = ev.modifiers()
        if k == Qt.Key_Escape:
            self.reject(); return
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_by(1.25); return
        if k in (Qt.Key_Minus, Qt.Key_Underscore):
            self._zoom_by(0.8); return
        if k == Qt.Key_0:
            self._set_zoom(1.0); return
        if k == Qt.Key_F:
            self._fit(); return
        if k == Qt.Key_D:
            self._download(); return
        super().keyPressEvent(ev)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _download(self):
        src = self._result.get("file_path", "")
        default_name = os.path.basename(src) if src else "image.jpg"
        target, _ = QFileDialog.getSaveFileName(
            self, "Save image as",
            str(Path.home() / "Downloads" / default_name),
            "Images (*.jpg *.jpeg *.png *.webp);;All Files (*)",
        )
        if not target:
            return
        if src and os.path.isfile(src):
            import shutil
            try:
                shutil.copy2(src, target)
            except Exception:
                self._pixmap.save(target)
        else:
            self._pixmap.save(target)
