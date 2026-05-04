"""Calendar Heatmap Widget — airline-style date picker showing message counts per day.

Usage:
    cal = CalendarHeatmapWidget()
    cal.load_data(conv_id=3)              # Per-chat counts
    cal.load_data(conv_id=None)           # Global counts
    cal.date_selected.connect(on_date)    # Single day click
    cal.range_selected.connect(on_range)  # Two-click range
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFrame
from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QMouseEvent

from app.services.database import Database


class _MonthGrid(QWidget):
    """Single month grid with day cells showing message counts."""

    day_clicked = Signal(object)  # date

    CELL_W = 38
    CELL_H = 40
    HEADER_H = 24
    DOW_H = 20

    def __init__(self, year: int, month: int, counts: dict[date, int], max_count: int, parent=None):
        super().__init__(parent)
        self._year = year
        self._month = month
        self._counts = counts
        self._max_count = max(max_count, 1)
        self._selected: date | None = None
        self._range_start: date | None = None
        self._range_end: date | None = None
        self._hover_day: date | None = None

        self._cal = calendar.Calendar(0)  # Monday first
        self._weeks = self._cal.monthdayscalendar(year, month)
        w = 7 * self.CELL_W
        h = self.HEADER_H + self.DOW_H + len(self._weeks) * self.CELL_H
        self.setFixedSize(w, h)
        self.setMouseTracking(True)

    def set_selection(self, start: date | None, end: date | None):
        self._range_start = start
        self._range_end = end
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()

        # Month header
        p.setFont(QFont("Segoe UI", 12, QFont.Bold))
        p.setPen(QColor("#111b21"))
        month_name = f"{calendar.month_name[self._month]} {self._year}"
        p.drawText(QRect(0, 0, w, self.HEADER_H), Qt.AlignCenter, month_name)

        # Day-of-week headers
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.setPen(QColor("#667781"))
        for i, dow in enumerate(["M", "T", "W", "T", "F", "S", "S"]):
            x = i * self.CELL_W
            p.drawText(QRect(x, self.HEADER_H, self.CELL_W, self.DOW_H), Qt.AlignCenter, dow)

        # Day cells
        today = date.today()
        p.setFont(QFont("Segoe UI", 10))
        count_font = QFont("Segoe UI", 7, QFont.Bold)

        for week_idx, week in enumerate(self._weeks):
            for dow_idx, day_num in enumerate(week):
                if day_num == 0:
                    continue
                x = dow_idx * self.CELL_W
                y = self.HEADER_H + self.DOW_H + week_idx * self.CELL_H
                d = date(self._year, self._month, day_num)
                count = self._counts.get(d, 0)
                cell = QRect(x + 1, y + 1, self.CELL_W - 2, self.CELL_H - 2)

                # Background: heatmap color based on count
                is_selected = (self._range_start and self._range_end
                               and self._range_start <= d <= self._range_end)
                is_today = d == today
                is_hover = d == self._hover_day

                if is_selected:
                    bg = QColor("#00897b")
                    text_color = QColor("white")
                    count_color = QColor(255, 255, 255, 200)
                elif is_today:
                    bg = QColor("#e8f5e9")
                    text_color = QColor("#00897b")
                    count_color = QColor("#00897b")
                elif count > 0:
                    # Heatmap: intensity based on count relative to max
                    intensity = min(count / self._max_count, 1.0)
                    if intensity > 0.7:
                        bg = QColor("#1565c0")
                        text_color = QColor("white")
                        count_color = QColor(255, 255, 255, 200)
                    elif intensity > 0.4:
                        bg = QColor("#bbdefb")
                        text_color = QColor("#0d47a1")
                        count_color = QColor("#1565c0")
                    elif intensity > 0.1:
                        bg = QColor("#e3f2fd")
                        text_color = QColor("#1565c0")
                        count_color = QColor("#1976d2")
                    else:
                        bg = QColor("#f5f5f5")
                        text_color = QColor("#333")
                        count_color = QColor("#888")
                else:
                    bg = QColor("white")
                    text_color = QColor("#bbb")
                    count_color = QColor("#ccc")

                if is_hover and not is_selected:
                    bg = bg.lighter(110)

                # Draw cell
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(cell, 4, 4)

                # Border for today
                if is_today:
                    p.setPen(QPen(QColor("#00897b"), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(cell, 4, 4)

                # Day number
                p.setPen(text_color)
                p.setFont(QFont("Segoe UI", 10, QFont.Bold if count > 0 else QFont.Normal))
                p.drawText(QRect(x, y + 2, self.CELL_W, 20), Qt.AlignCenter, str(day_num))

                # Count
                if count > 0:
                    p.setPen(count_color)
                    p.setFont(count_font)
                    count_str = f"{count:,}" if count < 10000 else f"{count/1000:.0f}K"
                    p.drawText(QRect(x, y + 20, self.CELL_W, 16), Qt.AlignCenter, count_str)

        p.end()

    def mouseMoveEvent(self, event: QMouseEvent):
        d = self._day_at(event.position().toPoint())
        if d != self._hover_day:
            self._hover_day = d
            self.update()

    def mousePressEvent(self, event: QMouseEvent):
        d = self._day_at(event.position().toPoint())
        if d:
            self.day_clicked.emit(d)

    def leaveEvent(self, event):
        self._hover_day = None
        self.update()

    def _day_at(self, pos) -> date | None:
        x, y = pos.x(), pos.y()
        top = self.HEADER_H + self.DOW_H
        if y < top:
            return None
        week_idx = int((y - top) / self.CELL_H)
        dow_idx = int(x / self.CELL_W)
        if 0 <= week_idx < len(self._weeks) and 0 <= dow_idx < 7:
            day_num = self._weeks[week_idx][dow_idx]
            if day_num > 0:
                return date(self._year, self._month, day_num)
        return None


class CalendarHeatmapWidget(QWidget):
    """Two-month calendar heatmap with message counts per day.

    Signals:
        date_selected(date): Single day clicked
        range_selected(date, date): Two days clicked = date range
        range_cleared(): Range cleared
    """

    date_selected = Signal(object)          # date (auto-fires on first click)
    range_selected = Signal(object, object)  # start_date, end_date
    range_cleared = Signal()
    apply_requested = Signal()  # user clicked "Apply" - host should hide the calendar
    close_requested = Signal()  # user clicked "Close" / dismiss without committing

    def __init__(self, parent=None):
        super().__init__(parent)
        self._counts: dict[date, int] = {}
        self._max_count = 0
        self._current_month = date.today().replace(day=1)
        self._range_start: date | None = None
        self._range_end: date | None = None
        self._click_count = 0
        self._grids: list[_MonthGrid] = []
        self._setup_ui()

    def _setup_ui(self):
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(8, 4, 8, 4)
        self._main_layout.setSpacing(4)

        # Nav row: < Month Year   Month Year >
        # Theme-aware colours — hard-coded greys would render
        # dark-on-dark in dark mode and disappear into the
        # background.
        try:
            from app.services.theme_manager import ThemeManager
            _tm = ThemeManager.get()
            _is_light = _tm.is_light
        except Exception:
            _is_light = True
        # Filled-pill buttons so the navigation arrows are
        # clearly visible in both light and dark themes (a
        # transparent background with bare Unicode glyphs reads
        # as "empty circles" on some platforms).
        if _is_light:
            _nav_fg, _nav_bg, _nav_bd, _nav_hv = "#ffffff", "#00897b", "#00695c", "#00695c"
            _clr_fg, _clr_bd, _clr_hv = "#333333", "#cccccc", "#f0f0f0"
        else:
            _nav_fg, _nav_bg, _nav_bd, _nav_hv = "#ffffff", "#00796b", "#009688", "#00897b"
            _clr_fg, _clr_bd, _clr_hv = "#e9edef", "#3b4a54", "rgba(134,150,160,0.18)"

        _nav_btn_css = (
            "QPushButton { border: 1px solid " + _nav_bd + "; border-radius: 6px; "
            "font-size: 16px; font-weight: 900; color: " + _nav_fg + "; "
            "background: " + _nav_bg + "; padding: 0 4px; } "
            "QPushButton:hover { background: " + _nav_hv + "; }"
        )

        nav = QHBoxLayout()
        # ASCII-equivalent labels so the buttons render
        # reliably across platforms / fonts.
        self._prev_btn = QPushButton("\u25C4 PREV")   # ◄ PREV
        self._prev_btn.setFixedSize(84, 28)
        self._prev_btn.setStyleSheet(_nav_btn_css)
        self._prev_btn.setToolTip("Previous month")
        self._prev_btn.clicked.connect(self._prev_month)
        nav.addWidget(self._prev_btn)

        nav.addStretch()

        self._clear_btn = QPushButton("Clear Selection")
        self._clear_btn.setStyleSheet(
            "QPushButton { border: 1px solid " + _clr_bd + "; padding: 3px 10px; "
            "border-radius: 4px; font-size: 10px; color: " + _clr_fg + "; "
            "background: transparent; } "
            "QPushButton:hover { background: " + _clr_hv + "; }"
        )
        self._clear_btn.clicked.connect(self._clear_range)
        self._clear_btn.setVisible(False)
        nav.addWidget(self._clear_btn)

        nav.addStretch()

        self._next_btn = QPushButton("NEXT \u25BA")   # NEXT ►
        self._next_btn.setFixedSize(84, 28)
        self._next_btn.setStyleSheet(_nav_btn_css)
        self._next_btn.setToolTip("Next month")
        self._next_btn.clicked.connect(self._next_month)
        nav.addWidget(self._next_btn)

        self._main_layout.addLayout(nav)

        # Selection label
        self._sel_label = QLabel("")
        self._sel_label.setStyleSheet("font-size: 10px; color: #00897b; font-weight: 600;")
        self._sel_label.setAlignment(Qt.AlignCenter)
        self._main_layout.addWidget(self._sel_label)

        # Grid container
        self._grid_container = QHBoxLayout()
        self._grid_container.setSpacing(12)
        self._main_layout.addLayout(self._grid_container)

        # Action bar at the bottom — Apply / Reset / Close.
        # ``date_selected`` already fires on click, so the
        # filter is applied implicitly; the explicit action bar
        # gives the user a clear way to confirm or dismiss the
        # picker rather than relying on the implicit signal.
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 6, 0, 0)
        action_row.setSpacing(8)
        action_row.addStretch()

        if _is_light:
            _apply_fg, _apply_bg, _apply_hv = "#ffffff", "#00897b", "#00695c"
            _reset_fg, _reset_bg, _reset_bd, _reset_hv = "#b71c1c", "transparent", "#ef9a9a", "rgba(244,67,54,0.08)"
            _close_fg, _close_bg, _close_bd, _close_hv = "#37474f", "transparent", "#cfd8dc", "rgba(96,125,139,0.10)"
        else:
            _apply_fg, _apply_bg, _apply_hv = "#ffffff", "#00796b", "#00897b"
            _reset_fg, _reset_bg, _reset_bd, _reset_hv = "#ef5350", "transparent", "#5a3a3a", "rgba(244,67,54,0.18)"
            _close_fg, _close_bg, _close_bd, _close_hv = "#cfd8dc", "transparent", "#3b4a54", "rgba(134,150,160,0.18)"

        self._reset_btn = QPushButton("↻ Reset")  # ↻ Reset
        self._reset_btn.setFixedHeight(30)
        self._reset_btn.setStyleSheet(
            "QPushButton { border: 1px solid " + _reset_bd + "; border-radius: 6px; "
            "padding: 4px 14px; font-size: 11px; font-weight: 600; "
            "color: " + _reset_fg + "; background: " + _reset_bg + "; } "
            "QPushButton:hover { background: " + _reset_hv + "; }"
        )
        self._reset_btn.setToolTip("Clear date selection (back to All Time)")
        self._reset_btn.clicked.connect(self._on_reset_clicked)
        action_row.addWidget(self._reset_btn)

        self._close_btn = QPushButton("✕ Close")  # ✕ Close
        self._close_btn.setFixedHeight(30)
        self._close_btn.setStyleSheet(
            "QPushButton { border: 1px solid " + _close_bd + "; border-radius: 6px; "
            "padding: 4px 14px; font-size: 11px; font-weight: 600; "
            "color: " + _close_fg + "; background: " + _close_bg + "; } "
            "QPushButton:hover { background: " + _close_hv + "; }"
        )
        self._close_btn.setToolTip("Dismiss the calendar without changing the filter")
        self._close_btn.clicked.connect(self.close_requested.emit)
        action_row.addWidget(self._close_btn)

        self._apply_btn = QPushButton("✓ Apply Filter")   # ✓ Apply
        self._apply_btn.setFixedHeight(30)
        self._apply_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; "
            "padding: 4px 18px; font-size: 11px; font-weight: 700; "
            "color: " + _apply_fg + "; background: " + _apply_bg + "; } "
            "QPushButton:hover { background: " + _apply_hv + "; } "
            "QPushButton:disabled { background: rgba(120,120,120,0.3); color: rgba(180,180,180,0.6); }"
        )
        self._apply_btn.setToolTip("Apply selected date range and close calendar")
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        self._apply_btn.setEnabled(False)
        action_row.addWidget(self._apply_btn)

        self._main_layout.addLayout(action_row)

    def load_data(self, conv_id: int | None = None):
        """Load message counts per day from DB."""
        try:
            db = Database.get()
        except RuntimeError:
            return

        where = "WHERE m.conversation_id = ?" if conv_id else ""
        params = (conv_id,) if conv_id else ()

        rows = db.fetchall(
            f"SELECT DATE(m.timestamp / 1000, 'unixepoch', 'localtime') AS d, COUNT(*) "
            f"FROM message m {where} "
            f"GROUP BY d ORDER BY d",
            params,
        )

        self._counts.clear()
        for r in rows:
            try:
                d_str = str(r[0]) if r[0] else None
                if d_str:
                    d = date.fromisoformat(d_str)
                    self._counts[d] = int(r[1])
            except (ValueError, TypeError):
                pass

        self._max_count = max(self._counts.values()) if self._counts else 0

        # Navigate to the most recent month with data
        if self._counts:
            last_date = max(self._counts.keys())
            self._current_month = last_date.replace(day=1)
            print(f"[Calendar] Loaded {len(self._counts)} days, last={last_date}, showing {self._current_month}")
        else:
            print(f"[Calendar] No data loaded (rows={len(rows)})")

        self._rebuild_grids()

    def load_counts(self, counts: dict[date, int], label: str = "items"):
        """Load pre-computed day→count dict (for media gallery, etc.).

        Args:
            counts: dict mapping date → int count
            label: word to use in selection label (e.g. "media files")
        """
        self._count_label_word = label
        self._counts.clear()
        self._counts.update(counts)
        self._max_count = max(self._counts.values()) if self._counts else 0

        # Navigate to the most recent month with data
        if self._counts:
            last_date = max(self._counts.keys())
            self._current_month = last_date.replace(day=1)
        self._rebuild_grids()

    def _rebuild_grids(self):
        # Clear old grids
        for g in self._grids:
            self._grid_container.removeWidget(g)
            g.deleteLater()
        self._grids.clear()

        # Build 2 months
        m1 = self._current_month
        if m1.month == 12:
            m2_year, m2_month = m1.year + 1, 1
        else:
            m2_year, m2_month = m1.year, m1.month + 1

        for y, m in [(m1.year, m1.month), (m2_year, m2_month)]:
            grid = _MonthGrid(y, m, self._counts, self._max_count)
            grid.day_clicked.connect(self._on_day_clicked)
            if self._range_start and self._range_end:
                grid.set_selection(self._range_start, self._range_end)
            self._grid_container.addWidget(grid)
            self._grids.append(grid)

    def _on_day_clicked(self, d: date):
        _word = getattr(self, "_count_label_word", "messages")
        if self._click_count == 0:
            # First click: set start
            self._range_start = d
            self._range_end = d
            self._click_count = 1
            day_count = self._counts.get(d, 0)
            self._sel_label.setText(
                f"Selected: {d.strftime('%b %d, %Y')} ({day_count:,} {_word})"
                f" — click another day for range"
            )
            self._clear_btn.setVisible(True)
            self._apply_btn.setEnabled(True)
            self.date_selected.emit(d)
        else:
            # Second click: set range
            if d < self._range_start:
                self._range_start, self._range_end = d, self._range_start
            else:
                self._range_end = d
            self._click_count = 0

            # Count items in range
            total = sum(c for dt, c in self._counts.items()
                        if self._range_start <= dt <= self._range_end)
            days = (self._range_end - self._range_start).days + 1
            self._sel_label.setText(
                f"{self._range_start.strftime('%b %d')} \u2014 {self._range_end.strftime('%b %d, %Y')} "
                f"({days} days, {total:,} {_word})"
            )
            self._clear_btn.setVisible(True)
            self._apply_btn.setEnabled(True)
            self.range_selected.emit(self._range_start, self._range_end)

        # Update grid selection highlight
        for g in self._grids:
            g.set_selection(self._range_start, self._range_end)

    def _clear_range(self):
        self._range_start = None
        self._range_end = None
        self._click_count = 0
        self._sel_label.setText("")
        self._clear_btn.setVisible(False)
        self._apply_btn.setEnabled(False)
        for g in self._grids:
            g.set_selection(None, None)
        self.range_cleared.emit()

    def _on_reset_clicked(self):
        """Reset = clear selection + reset host filter back to All Time."""
        self._clear_range()

    def _on_apply_clicked(self):
        """Apply = commit current selection (already emitted on click) and
        ask the host to dismiss the calendar.  date_selected /
        range_selected have already fired by now, so the filter is
        already in effect; this just closes the picker."""
        self.apply_requested.emit()

    def _prev_month(self):
        if self._current_month.month == 1:
            self._current_month = self._current_month.replace(year=self._current_month.year - 1, month=12)
        else:
            self._current_month = self._current_month.replace(month=self._current_month.month - 1)
        self._rebuild_grids()

    def _next_month(self):
        if self._current_month.month == 12:
            self._current_month = self._current_month.replace(year=self._current_month.year + 1, month=1)
        else:
            self._current_month = self._current_month.replace(month=self._current_month.month + 1)
        self._rebuild_grids()
