"""
Chat Viewer page -- WhatsApp-style bubble view for conversation messages.
Supports thumbnails, delivery ticks, quoted replies, system messages,
media types, copy, search within chat, debug info panel, profile pictures,
date separators, date filtering, scroll indicator, font size, keyboard nav,
ghost message indicators, media file display, link handling, system event details.
"""

from __future__ import annotations

from datetime import datetime, date, timezone

from PySide6.QtCore import (
    QAbstractListModel, QDate, QEvent, QModelIndex, QPoint, QRect, QSize,
    Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDateEdit, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QScrollArea, QSizePolicy, QTextEdit,
    QVBoxLayout, QWidget,
)

import json as _json
import os as _os
import queue as _queue
import threading as _threading
import time as _time

from app.services.database import Database
from app.services.theme_manager import ThemeManager
from app.config import (
    date_range_to_timestamps,
    format_timestamp,
    format_timestamp_with_utc,
    timestamp_to_qdate,
)
from app.views.widgets.bubble_delegate import MSG_DATA_ROLE, BubbleDelegate
from app.views.widgets.chat_media_panel import ChatMediaGalleryPanel

# WebEngine imports (optional — falls back to QListView if not available)
try:
    from app.views.widgets.chat_web_view import ChatWebView
    _HAS_WEBENGINE = True
except Exception as _web_err:
    print(f"[WebEngine] Import FAILED: {type(_web_err).__name__}: {_web_err}")
    _HAS_WEBENGINE = False

INITIAL_BATCH = 100       # Must match TILE_SIZE in chat_renderer.js — smaller = less DOM churn
BATCH_SIZE = 100          # Tile size — must match JS TILE_SIZE
PREFETCH_THRESHOLD = 0.40  # Start loading older messages at 40% from top

# When a conversation has fewer messages than this, preload
# the entire conversation in a single Python pass and push it
# all to JS in one ``set_messages_at`` call.  Eliminates per-
# tile Python round-trips so scrolling feels like the exported
# viewer.  The threshold is set high enough that most chats
# fit, while keeping the JSON payload size and serialisation
# time bounded.
PRELOAD_ALL_THRESHOLD = 20000


class SearchResultsPanel(QFrame):
    """Right-side panel showing search results with inline filters and rich cards."""
    result_selected = Signal(int)  # msg_id
    search_options_changed = Signal()  # re-trigger search with new options

    _TYPE_ICONS = {
        "text": "\U0001F4AC", "image": "\U0001F4F7", "video": "\U0001F3AC",
        "gif": "\U0001F3AC", "animated_gif": "\U0001F3AC",
        "audio": "\U0001F3B5", "voice": "\U0001F3A4", "document": "\U0001F4C4",
        "sticker": "\U0001F4CC", "poll": "\U0001F4CA", "location": "\U0001F4CD",
        "live_location": "\U0001F4CD", "vcard": "\U0001F464",
        "call_log": "\U0001F4DE", "album": "\U0001F5BC",
    }

    _PAGE_SIZE = 80  # show this many results at a time

    def __init__(self, parent, tm):
        super().__init__(parent)
        self._tm = tm
        self._all_results: list[dict] = []
        self._filtered_results: list[dict] = []
        self._current_msg_id: int | None = None
        self._query = ""
        self._date_from_ms: int | None = None
        self._date_to_ms: int | None = None
        self._displayed_count: int = 0  # how many items currently in list
        self._filter_debounce = QTimer()
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(150)
        self._filter_debounce.timeout.connect(self._apply_sub_filters_now)

        _lt = tm.is_light
        self.setFixedWidth(380)
        self.setVisible(False)
        self.setStyleSheet(
            "QFrame#SearchPanel { background: #ffffff; border-left: 1px solid #d0d7de; }"
            if _lt else
            "QFrame#SearchPanel { background: #1a2026; border-left: 1px solid #2a3942; }"
        )
        self.setObjectName("SearchPanel")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header ----
        hdr = QFrame()
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(
            "QFrame { background: #f6f8fa; border-bottom: 1px solid #d0d7de; }"
            if _lt else
            "QFrame { background: #202c33; border-bottom: 1px solid #2a3942; }"
        )
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 8, 0)
        hl.setSpacing(8)
        self._hdr_label = QLabel("")
        self._hdr_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #1b1b1b;" if _lt else
            "font-size: 13px; font-weight: bold; color: #e9edef;"
        )
        hl.addWidget(self._hdr_label, 1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            "font-size: 11px; color: #57606a;" if _lt else
            "font-size: 11px; color: #8696a0;"
        )
        hl.addWidget(self._count_label)
        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #57606a; font-size: 14px; }"
            "QPushButton:hover { color: #1b1b1b; }" if _lt else
            "QPushButton { background: transparent; border: none; color: #8696a0; font-size: 14px; }"
            "QPushButton:hover { color: #e9edef; }"
        )
        close_btn.clicked.connect(lambda: self.setVisible(False))
        hl.addWidget(close_btn)
        root.addWidget(hdr)

        # ---- Active filter summary bar (always visible when filters active) ----
        self._filter_summary = QLabel("")
        self._filter_summary.setVisible(False)
        self._filter_summary.setWordWrap(True)
        self._filter_summary.setStyleSheet(
            "QLabel { background: rgba(0,137,123,0.08); border-bottom: 1px solid "
            f"{'#b2dfdb' if _lt else 'rgba(0,188,212,0.2)'}; padding: 4px 10px;"
            f" font-size: 10px; color: {'#00695c' if _lt else '#80deea'}; }}"
        )
        root.addWidget(self._filter_summary)

        # ---- Search options (exact match, case sensitive) ----
        opts_frame = QFrame()
        opts_frame.setStyleSheet(
            "QFrame { background: #f6f8fa; border-bottom: 1px solid #d0d7de; }"
            if _lt else
            "QFrame { background: #202c33; border-bottom: 1px solid #2a3942; }"
        )
        ol = QHBoxLayout(opts_frame)
        ol.setContentsMargins(8, 3, 8, 3)
        ol.setSpacing(8)
        _chk_style = "font-size: 10px; color: #57606a;" if _lt else "font-size: 10px; color: #8696a0;"
        from PySide6.QtWidgets import QCheckBox
        self._case_sensitive_chk = QCheckBox("Aa Case sensitive")
        self._case_sensitive_chk.setStyleSheet(_chk_style)
        self._case_sensitive_chk.toggled.connect(lambda: self.search_options_changed.emit())
        ol.addWidget(self._case_sensitive_chk)
        self._exact_match_chk = QCheckBox("\" \" Exact match")
        self._exact_match_chk.setStyleSheet(_chk_style)
        self._exact_match_chk.toggled.connect(lambda: self.search_options_changed.emit())
        ol.addWidget(self._exact_match_chk)
        ol.addStretch()
        root.addWidget(opts_frame)

        # ---- Inline Filters (type dropdown) ----
        filt_frame = QFrame()
        filt_frame.setStyleSheet(
            "QFrame { background: #f0f3f6; border-bottom: 1px solid #d0d7de; }"
            if _lt else
            "QFrame { background: #111b21; border-bottom: 1px solid #2a3942; }"
        )
        fl = QHBoxLayout(filt_frame)
        fl.setContentsMargins(8, 4, 8, 4)
        fl.setSpacing(4)

        _combo_style = (
            "QComboBox { background: #fff; border: 1px solid #d0d7de; border-radius: 4px;"
            " padding: 2px 6px; font-size: 11px; color: #1b1b1b; }"
            if _lt else
            "QComboBox { background: #2a3942; border: 1px solid #3b4a54; border-radius: 4px;"
            " padding: 2px 6px; font-size: 11px; color: #e9edef; }"
        )
        _input_style = (
            "QLineEdit { background: #fff; border: 1px solid #d0d7de; border-radius: 4px;"
            " padding: 2px 6px; font-size: 11px; color: #1b1b1b; }"
            if _lt else
            "QLineEdit { background: #2a3942; border: 1px solid #3b4a54; border-radius: 4px;"
            " padding: 2px 6px; font-size: 11px; color: #e9edef; }"
        )
        self._type_filter = QComboBox()
        self._type_filter.setFixedHeight(24)
        self._type_filter.setMaximumWidth(160)
        self._type_filter.setStyleSheet(_combo_style)
        self._type_filter.currentIndexChanged.connect(self._apply_sub_filters)
        _type_lbl = QLabel("Type:")
        _type_lbl.setStyleSheet(f"font-size: 10px; color: {'#57606a' if _lt else '#8696a0'};")
        fl.addWidget(_type_lbl)
        fl.addWidget(self._type_filter)
        fl.addStretch()
        root.addWidget(filt_frame)

        # ---- Sender filter: collapsible checkbox list with search ----
        self._sender_frame = QFrame()
        self._sender_frame.setStyleSheet(
            "QFrame { background: #f6f8fa; border-bottom: 1px solid #d0d7de; }"
            if _lt else
            "QFrame { background: #111b21; border-bottom: 1px solid #2a3942; }"
        )
        sf_layout = QVBoxLayout(self._sender_frame)
        sf_layout.setContentsMargins(8, 3, 8, 3)
        sf_layout.setSpacing(2)

        # Sender header row with toggle
        sf_hdr = QHBoxLayout()
        sf_hdr.setSpacing(4)
        self._sender_toggle = QPushButton("\u25B6 Senders")
        self._sender_toggle.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 10px;"
            f" color: {'#57606a' if _lt else '#8696a0'}; font-weight: bold; text-align: left; }}"
            f"QPushButton:hover {{ color: {'#1b1b1b' if _lt else '#e9edef'}; }}"
        )
        self._sender_toggle.clicked.connect(self._toggle_sender_list)
        sf_hdr.addWidget(self._sender_toggle)
        self._sender_count_label = QLabel("")
        self._sender_count_label.setStyleSheet(
            f"font-size: 9px; color: {'#57606a' if _lt else '#8696a0'};"
        )
        sf_hdr.addWidget(self._sender_count_label, 1)
        _sel_all = QPushButton("All")
        _sel_all.setFixedHeight(18)
        _sel_all.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {'#00897b' if _lt else '#00bcd4'}; font-size: 9px; font-weight: bold; }}"
            f"QPushButton:hover {{ text-decoration: underline; }}"
        )
        _sel_all.clicked.connect(self._select_all_senders)
        sf_hdr.addWidget(_sel_all)
        _sel_none = QPushButton("None")
        _sel_none.setFixedHeight(18)
        _sel_none.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {'#999' if _lt else '#666'}; font-size: 9px; }}"
            "QPushButton:hover { color: #ef5350; }"
        )
        _sel_none.clicked.connect(self._select_no_senders)
        sf_hdr.addWidget(_sel_none)
        sf_layout.addLayout(sf_hdr)

        # Search + checkbox list (hidden by default)
        self._sender_list_container = QWidget()
        self._sender_list_container.setVisible(False)
        sl_layout = QVBoxLayout(self._sender_list_container)
        sl_layout.setContentsMargins(0, 2, 0, 0)
        sl_layout.setSpacing(2)
        self._sender_search = QLineEdit()
        self._sender_search.setPlaceholderText("Search senders...")
        self._sender_search.setFixedHeight(22)
        self._sender_search.setStyleSheet(_input_style)
        self._sender_search.setClearButtonEnabled(True)
        self._sender_search.textChanged.connect(self._filter_sender_checkboxes)
        sl_layout.addWidget(self._sender_search)
        self._sender_list = QListWidget()
        self._sender_list.setMaximumHeight(140)
        self._sender_list.setStyleSheet(
            "QListWidget { background: transparent; border: 1px solid "
            f"{'#d0d7de' if _lt else '#3b4a54'}; border-radius: 4px; }}"
            "QListWidget::item { padding: 1px 4px; }"
        )
        sl_layout.addWidget(self._sender_list)
        sf_layout.addWidget(self._sender_list_container)
        root.addWidget(self._sender_frame)

        # Internal data for sender checkboxes
        self._sender_checkboxes: list[tuple[QCheckBox, str, str]] = []  # (checkbox, name, phone)
        self._selected_senders: set[str] | None = None  # None = all selected
        self._date_chip_buttons: dict[str, QPushButton] = {}  # date_str -> button
        self._date_chip_all_btn: QPushButton | None = None

        # ---- Date chips: show dates with result counts ----
        self._date_chips_frame = QFrame()
        self._date_chips_frame.setStyleSheet(
            "QFrame { background: #f6f8fa; border-bottom: 1px solid #d0d7de; }"
            if _lt else
            "QFrame { background: #111b21; border-bottom: 1px solid #2a3942; }"
        )
        date_chips_layout = QVBoxLayout(self._date_chips_frame)
        date_chips_layout.setContentsMargins(8, 4, 8, 4)
        date_chips_layout.setSpacing(2)
        # Header row with toggle
        dch_row = QHBoxLayout()
        dch_row.setSpacing(4)
        self._date_chips_toggle = QPushButton("\u25B6 Dates")
        self._date_chips_toggle.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 10px;"
            f" color: {'#57606a' if _lt else '#8696a0'}; font-weight: bold; text-align: left; }}"
            f"QPushButton:hover {{ color: {'#1b1b1b' if _lt else '#e9edef'}; }}"
        )
        self._date_chips_toggle.clicked.connect(self._toggle_date_chips)
        dch_row.addWidget(self._date_chips_toggle)
        self._date_range_label = QLabel("")
        self._date_range_label.setStyleSheet(
            f"font-size: 9px; color: {'#57606a' if _lt else '#8696a0'};"
        )
        dch_row.addWidget(self._date_range_label, 1)
        _date_clear = QPushButton("\u2715 Clear")
        _date_clear.setFixedHeight(18)
        _date_clear.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {'#999' if _lt else '#666'}; font-size: 9px; }}"
            "QPushButton:hover { color: #ef5350; }"
        )
        _date_clear.clicked.connect(self._clear_date_filter)
        dch_row.addWidget(_date_clear)
        date_chips_layout.addLayout(dch_row)

        # Scrollable chip area (hidden by default, expanded on toggle)
        self._date_chips_scroll = QScrollArea()
        self._date_chips_scroll.setWidgetResizable(True)
        self._date_chips_scroll.setMaximumHeight(120)
        self._date_chips_scroll.setVisible(False)
        self._date_chips_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._date_chips_container = QWidget()
        from PySide6.QtWidgets import QGridLayout
        self._date_chips_grid = QGridLayout(self._date_chips_container)
        self._date_chips_grid.setContentsMargins(0, 0, 0, 0)
        self._date_chips_grid.setSpacing(3)
        self._date_chips_scroll.setWidget(self._date_chips_container)
        date_chips_layout.addWidget(self._date_chips_scroll)

        root.addWidget(self._date_chips_frame)

        # ---- Results list ----
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border-bottom: 1px solid #eaeef2; padding: 0; margin: 0; }"
            "QListWidget::item:selected { background: rgba(0,137,123,0.08); }"
            "QListWidget::item:hover { background: rgba(0,0,0,0.03); }"
            if _lt else
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border-bottom: 1px solid #2a3942; padding: 0; margin: 0; }"
            "QListWidget::item:selected { background: rgba(0,188,212,0.08); }"
            "QListWidget::item:hover { background: rgba(255,255,255,0.03); }"
        )
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.setFocusPolicy(Qt.StrongFocus)
        # Lazy load on scroll near bottom
        self._list.verticalScrollBar().valueChanged.connect(self._on_list_scroll)
        root.addWidget(self._list, 1)

    @property
    def case_sensitive(self) -> bool:
        return self._case_sensitive_chk.isChecked()

    @property
    def exact_match(self) -> bool:
        return self._exact_match_chk.isChecked()

    def load_results(self, query: str, results: list[dict]):
        """Populate panel with search results."""
        self._query = query
        self._all_results = results
        self._hdr_label.setText(f'\U0001F50D "{query}"')

        # Populate type filter
        self._type_filter.blockSignals(True)
        self._type_filter.clear()
        self._type_filter.addItem("All Types", None)
        types = sorted({r["type_label"] for r in results})
        for t in types:
            icon = self._TYPE_ICONS.get(t, "\U0001F4AC")
            self._type_filter.addItem(f"{icon} {t}", t)
        self._type_filter.blockSignals(False)

        self._date_from_ms = None
        self._date_to_ms = None
        self._selected_senders = None  # all selected
        self._sender_search.clear()

        # Build sender checkbox list
        self._build_sender_list(results)

        # Build date chips showing counts per date
        self._build_date_chips(results)

        self._apply_sub_filters_now()  # immediate on initial load, no debounce
        self.setVisible(True)

    def _build_sender_list(self, results: list[dict]):
        """Build sender checkbox list with name + phone + result count."""
        from PySide6.QtWidgets import QCheckBox
        _lt = self._tm.is_light

        self._sender_list.clear()
        self._sender_checkboxes.clear()

        # Count results per sender and collect phone numbers
        sender_info: dict[str, dict] = {}  # name -> {count, phone}
        for r in results:
            name = r.get("sender_name", "Unknown")
            phone = r.get("phone", "") or ""
            if name not in sender_info:
                sender_info[name] = {"count": 0, "phone": phone}
            sender_info[name]["count"] += 1

        sorted_senders = sorted(sender_info.items(), key=lambda x: -x[1]["count"])
        self._sender_count_label.setText(f"{len(sorted_senders)} senders")

        for name, info in sorted_senders:
            phone = info["phone"]
            cnt = info["count"]
            label = f"{name} ({cnt})"
            if phone:
                label = f"{name} [{phone}] ({cnt})"

            item = QListWidgetItem()
            self._sender_list.addItem(item)
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setStyleSheet(
                f"QCheckBox {{ font-size: 10px; color: {'#24292f' if _lt else '#d1d7db'}; }}"
            )
            cb.toggled.connect(self._on_sender_checkbox_changed)
            self._sender_list.setItemWidget(item, cb)
            item.setSizeHint(cb.sizeHint())
            self._sender_checkboxes.append((cb, name, phone))

    def _toggle_sender_list(self):
        vis = not self._sender_list_container.isVisible()
        self._sender_list_container.setVisible(vis)
        self._sender_toggle.setText("\u25BC Senders" if vis else "\u25B6 Senders")

    def _filter_sender_checkboxes(self, text: str):
        """Show/hide sender checkboxes matching the search text."""
        text_lower = text.lower()
        for i, (cb, name, phone) in enumerate(self._sender_checkboxes):
            item = self._sender_list.item(i)
            if item:
                match = text_lower in name.lower() or text_lower in phone.lower()
                item.setHidden(not match)

    def _select_all_senders(self):
        for cb, _, _ in self._sender_checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self._selected_senders = None
        self._apply_sub_filters()

    def _select_no_senders(self):
        for cb, _, _ in self._sender_checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self._selected_senders = set()
        self._apply_sub_filters()

    def _on_sender_checkbox_changed(self):
        """Rebuild the set of selected senders — debounced."""
        checked = set()
        all_checked = True
        for cb, name, _ in self._sender_checkboxes:
            if cb.isChecked():
                checked.add(name)
            else:
                all_checked = False
        self._selected_senders = None if all_checked else checked
        self._filter_debounce.start()  # debounce instead of immediate apply

    def _build_date_chips(self, results: list[dict]):
        """Build date filter chips (called once on load_results, not on every filter)."""
        from datetime import datetime
        _lt = self._tm.is_light

        # Clear existing chips
        while self._date_chips_grid.count():
            item = self._date_chips_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._date_chip_buttons.clear()
        self._date_chip_all_btn = None

        # Count results per date
        date_counts: dict[str, int] = {}
        for r in results:
            ts = r.get("timestamp", 0)
            if ts:
                try:
                    d = format_timestamp(ts, "date")
                    date_counts[d] = date_counts.get(d, 0) + 1
                except Exception:
                    pass

        if not date_counts:
            self._date_range_label.setText("")
            return

        sorted_dates = sorted(date_counts.keys())
        self._date_range_label.setText(f"{sorted_dates[0]} \u2192 {sorted_dates[-1]}")

        # "All" chip
        all_btn = QPushButton(f"All ({len(results)})")
        all_btn.setFixedHeight(20)
        all_btn.setStyleSheet(
            "QPushButton { background: rgba(0,137,123,0.15); border: 1px solid #00897b;"
            " border-radius: 10px; padding: 1px 8px; font-size: 9px;"
            f" color: {'#00897b' if _lt else '#00bcd4'}; font-weight: bold; }}"
            "QPushButton:hover { background: rgba(0,137,123,0.25); }"
        )
        all_btn.clicked.connect(self._clear_date_filter)
        self._date_chips_grid.addWidget(all_btn, 0, 0)
        self._date_chip_all_btn = all_btn

        # Per-date chips in grid (4 columns)
        cols = 4
        for i, d in enumerate(sorted_dates):
            cnt = date_counts[d]
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                label = f"{dt.strftime('%b %d %y')} ({cnt})"
            except Exception:
                label = f"{d} ({cnt})"

            chip = QPushButton(label)
            chip.setFixedHeight(20)
            chip.setStyleSheet(
                "QPushButton {{ background: {bg}; border: 1px solid {brd};"
                " border-radius: 10px; padding: 1px 6px; font-size: 9px;"
                " color: {fg}; }}"
                "QPushButton:hover {{ background: {hov}; }}".format(
                    bg="rgba(0,0,0,0.04)" if _lt else "rgba(255,255,255,0.06)",
                    brd="#d0d7de" if _lt else "#3b4a54",
                    fg="#24292f" if _lt else "#d1d7db",
                    hov="rgba(0,0,0,0.08)" if _lt else "rgba(255,255,255,0.1)",
                )
            )
            chip.clicked.connect(lambda checked=False, date_str=d: self._on_date_chip_clicked(date_str))
            row = (i + 1) // cols
            col = (i + 1) % cols
            self._date_chips_grid.addWidget(chip, row, col)
            self._date_chip_buttons[d] = chip

    def _update_date_chip_counts(self, cascaded_results: list[dict]):
        """Update existing date chip labels with new counts (no widget recreation)."""
        from datetime import datetime
        from collections import Counter
        date_counts = Counter()
        for r in cascaded_results:
            ts = r.get("timestamp", 0)
            if ts:
                try:
                    d = format_timestamp(ts, "date")
                    date_counts[d] += 1
                except Exception:
                    pass
        # Update each chip's label — hide zero-count chips
        for d, btn in self._date_chip_buttons.items():
            cnt = date_counts.get(d, 0)
            if cnt > 0:
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d")
                    btn.setText(f"{dt.strftime('%b %d %y')} ({cnt})")
                except Exception:
                    btn.setText(f"{d} ({cnt})")
                btn.setVisible(True)
            else:
                btn.setVisible(False)
        # Update "All" chip
        if self._date_chip_all_btn:
            total = sum(date_counts.values())
            self._date_chip_all_btn.setText(f"All ({total})")

    def _toggle_date_chips(self):
        vis = not self._date_chips_scroll.isVisible()
        self._date_chips_scroll.setVisible(vis)
        self._date_chips_toggle.setText(
            ("\u25BC Dates" if vis else "\u25B6 Dates")
        )

    def _on_date_chip_clicked(self, date_str: str):
        """Filter results to a specific date."""
        from datetime import datetime, time
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        self._date_from_ms = int(datetime.combine(d, time.min).timestamp() * 1000)
        self._date_to_ms = int(datetime.combine(d, time.max).timestamp() * 1000)
        self._date_range_label.setText(f"Showing: {date_str}")
        self._apply_sub_filters()

    def _clear_date_filter(self):
        self._date_from_ms = None
        self._date_to_ms = None
        # Restore full range label
        if self._all_results:
            from datetime import datetime
            ts_list = [r["timestamp"] for r in self._all_results if r.get("timestamp")]
            if ts_list:
                mn = datetime.fromtimestamp(min(ts_list) / 1000).strftime("%Y-%m-%d")
                mx = datetime.fromtimestamp(max(ts_list) / 1000).strftime("%Y-%m-%d")
                self._date_range_label.setText(f"{mn} \u2192 {mx}")
        self._apply_sub_filters()

    def _apply_sub_filters(self):
        """Debounced entry point — schedules the real filter work."""
        self._filter_debounce.start()

    def _apply_sub_filters_now(self):
        """Actual filter work — runs after debounce settles."""
        type_val = self._type_filter.currentData()

        # Step 1: Apply all filters
        filtered = self._all_results
        if self._selected_senders is not None:
            filtered = [r for r in filtered if r.get("sender_name", "") in self._selected_senders]
        if type_val:
            filtered = [r for r in filtered if r["type_label"] == type_val]
        if self._date_from_ms is not None:
            filtered = [r for r in filtered if r.get("timestamp", 0) >= self._date_from_ms]
        if self._date_to_ms is not None:
            filtered = [r for r in filtered if r.get("timestamp", 0) <= self._date_to_ms]
        self._filtered_results = filtered

        # Step 2: Cascade sender counts (cheap — just update labels)
        sender_base = self._all_results
        if type_val:
            sender_base = [r for r in sender_base if r["type_label"] == type_val]
        if self._date_from_ms is not None:
            sender_base = [r for r in sender_base if r.get("timestamp", 0) >= self._date_from_ms]
        if self._date_to_ms is not None:
            sender_base = [r for r in sender_base if r.get("timestamp", 0) <= self._date_to_ms]
        self._update_sender_counts(sender_base)

        # Step 3: Cascade date chip counts (cheap — just update labels)
        date_base = self._all_results
        if self._selected_senders is not None:
            date_base = [r for r in date_base if r.get("sender_name", "") in self._selected_senders]
        if type_val:
            date_base = [r for r in date_base if r["type_label"] == type_val]
        self._update_date_chip_counts(date_base)

        # Step 4: Update summary + rebuild list (paginated)
        self._update_filter_summary()
        self._rebuild_list()

    def _update_sender_counts(self, filtered_for_senders: list[dict]):
        """Update sender checkbox labels with counts from cascaded filter."""
        from collections import Counter
        counts = Counter(r.get("sender_name", "Unknown") for r in filtered_for_senders)
        for cb, name, phone in self._sender_checkboxes:
            cnt = counts.get(name, 0)
            label = f"{name} [{phone}] ({cnt})" if phone else f"{name} ({cnt})"
            cb.setText(label)

    def _update_filter_summary(self):
        """Show active filter summary bar."""
        parts: list[str] = []
        type_val = self._type_filter.currentData()
        if type_val:
            icon = self._TYPE_ICONS.get(type_val, "")
            parts.append(f"{icon} {type_val}")
        if self._selected_senders is not None:
            n = len(self._selected_senders)
            total = len(self._sender_checkboxes)
            parts.append(f"{n}/{total} senders")
        if self._date_from_ms is not None:
            from datetime import datetime
            d = format_timestamp(self._date_from_ms, '%b %d %Y')
            parts.append(f"date: {d}")
        if parts:
            self._filter_summary.setText("\u2630 Filtered: " + " \u2022 ".join(parts)
                                         + f"  \u2192 {len(self._filtered_results)} results")
            self._filter_summary.setVisible(True)
        else:
            self._filter_summary.setVisible(False)

    def _rebuild_list(self):
        """Rebuild the list — shows first PAGE_SIZE items, lazy-loads rest on scroll."""
        self._list.clear()
        self._displayed_count = 0
        self._load_more_results()
        total = len(self._filtered_results)
        shown = self._displayed_count
        if shown < total:
            self._count_label.setText(f"{shown}/{total} results (\u2193 scroll for more)")
        else:
            self._count_label.setText(f"{total} results")

    def _load_more_results(self):
        """Append next page of results to the list widget."""
        _lt = self._tm.is_light
        query_lower = self._query if self.case_sensitive else self._query.lower()
        import html as _html
        from datetime import datetime

        start = self._displayed_count
        end = min(start + self._PAGE_SIZE, len(self._filtered_results))
        if start >= end:
            return

        sc = "#1b1b1b" if _lt else "#e9edef"
        tc = "#57606a" if _lt else "#8696a0"
        snc = "#24292f" if _lt else "#d1d7db"
        tyc = "#57606a" if _lt else "#8696a0"

        for i in range(start, end):
            r = self._filtered_results[i]
            snippet = r.get("snippet", "") or ""
            if len(snippet) > 120:
                snippet = snippet[:120] + "\u2026"

            snippet_safe = _html.escape(snippet)
            if query_lower:
                compare = snippet if self.case_sensitive else snippet.lower()
                idx = compare.find(query_lower)
                if idx >= 0:
                    qlen = len(self._query)
                    before = _html.escape(snippet[:idx])
                    match = _html.escape(snippet[idx:idx + qlen])
                    after = _html.escape(snippet[idx + qlen:])
                    snippet_safe = (
                        f'{before}<span style="background:#ffd54f;border-radius:2px;'
                        f'padding:0 1px;">{match}</span>{after}'
                    )

            type_icon = self._TYPE_ICONS.get(r.get("type_label", ""), "\U0001F4AC")
            type_label = r.get("type_label", "text")
            tag_html = ' <span style="color:#ef5350;">\U0001F6A9</span>' if r.get("is_tagged") else ""
            sender = _html.escape(r.get("sender_name", "Unknown"))
            ts = r.get("timestamp", 0)
            ts_str = ""
            if ts:
                try:
                    ts_str = format_timestamp(ts, "%b %d %Y, %H:%M")
                except Exception:
                    pass

            card_html = (
                f'<div style="padding:8px 10px;">'
                f'<div><span style="font-weight:600;font-size:12px;color:{sc};">{sender}</span>'
                f' <span style="font-size:10px;color:{tc};float:right;">{ts_str}</span></div>'
                f'<div style="font-size:11px;color:{snc};margin-top:3px;'
                f'overflow:hidden;max-height:34px;line-height:1.3;">{snippet_safe}</div>'
                f'<div style="font-size:10px;color:{tyc};margin-top:2px;">'
                f'{type_icon} {type_label}{tag_html}</div></div>'
            )

            item = QListWidgetItem()
            item.setData(Qt.UserRole, r["id"])
            self._list.addItem(item)
            card = QLabel(card_html)
            card.setTextFormat(Qt.RichText)
            card.setWordWrap(True)
            card.setStyleSheet("QLabel { background: transparent; }")
            item.setSizeHint(card.sizeHint() + QSize(0, 4))
            self._list.setItemWidget(item, card)

        self._displayed_count = end

    def _on_list_scroll(self, value: int):
        """Load more results when scrolled near bottom."""
        sb = self._list.verticalScrollBar()
        if sb.maximum() > 0 and value >= sb.maximum() - 50:
            if self._displayed_count < len(self._filtered_results):
                self._load_more_results()
                total = len(self._filtered_results)
                shown = self._displayed_count
                if shown < total:
                    self._count_label.setText(f"{shown}/{total} results (\u2193 scroll)")
                else:
                    self._count_label.setText(f"{total} results")

    def set_current(self, msg_id: int):
        self._current_msg_id = msg_id
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.UserRole) == msg_id:
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                break

    def _on_item_clicked(self, item: QListWidgetItem):
        msg_id = item.data(Qt.UserRole)
        if msg_id:
            self._current_msg_id = msg_id
            self.result_selected.emit(msg_id)

    def _on_row_changed(self, row: int):
        """Auto-navigate when user moves with keyboard arrows."""
        if row < 0:
            return
        item = self._list.item(row)
        if item:
            msg_id = item.data(Qt.UserRole)
            if msg_id and msg_id != self._current_msg_id:
                self._current_msg_id = msg_id
                self.result_selected.emit(msg_id)

    def navigate_next(self):
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            self._list.setCurrentRow(row + 1)
            item = self._list.currentItem()
            if item:
                self._on_item_clicked(item)

    def navigate_prev(self):
        row = self._list.currentRow()
        if row > 0:
            self._list.setCurrentRow(row - 1)
            item = self._list.currentItem()
            if item:
                self._on_item_clicked(item)

    def clear(self):
        self._list.clear()
        self._sender_list.clear()
        self._sender_checkboxes.clear()
        self._selected_senders = None
        self._all_results.clear()
        self._filtered_results.clear()
        self._current_msg_id = None
        self._query = ""
        self._date_from_ms = None
        self._date_to_ms = None
        self._hdr_label.setText("")
        self._count_label.setText("")
        self._sender_count_label.setText("")
        self._sender_list_container.setVisible(False)
        self._date_chips_scroll.setVisible(False)
        self.setVisible(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.setVisible(False)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self._list.currentItem()
            if item:
                self._on_item_clicked(item)
        else:
            super().keyPressEvent(event)


class GhostMessagesSidebarPanel(QFrame):
    """Right-side panel listing every recovered ghost message in the
    current conversation with preview, sender, timestamp and recovery
    method.  Clicking an entry scrolls the chat to the original
    revoked message location.

    Mirrors the structure of ``RepliesSidebarPanel`` /
    ``SearchResultsPanel`` so it slots into the same right-hand
    content area and respects the existing mutual-exclusion rules
    (open one closes the others).

    The sidebar is the user-facing replacement for the chat-level
    "ghost-only" filter — filtering hides every other message in the
    chat, which is too aggressive when an analyst wants ghosts in
    their conversation context.  The sidebar gives them an at-a-
    glance index they can click through without losing the chat.
    """

    ghost_selected = Signal(int)  # msg_id to navigate to in the chat

    def __init__(self, parent, tm):
        super().__init__(parent)
        self._tm = tm
        self._conv_id: int | None = None
        _lt = tm.is_light

        self.setFixedWidth(400)
        self.setVisible(False)
        self.setObjectName("GhostPanel")
        self.setStyleSheet(
            "QFrame#GhostPanel { background: #ffffff; border-left: 1px solid #d0d7de; }"
            if _lt else
            "QFrame#GhostPanel { background: #1a2026; border-left: 1px solid #2a3942; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header (matches RepliesSidebarPanel header layout) ----
        hdr = QFrame()
        hdr.setFixedHeight(44)
        _hdr_bg = "#f6f8fa" if _lt else "#111b21"
        _hdr_border = "#d0d7de" if _lt else "#2a3942"
        hdr.setStyleSheet(
            f"QFrame {{ background: {_hdr_bg}; border-bottom: 1px solid {_hdr_border}; }}"
        )
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 8, 0)
        self._hdr_label = QLabel("\U0001F47B Ghost messages")
        self._hdr_label.setStyleSheet(
            f"font-weight: bold; font-size: 12px; color: {'#1a1a1a' if _lt else '#e9edef'};"
        )
        hl.addWidget(self._hdr_label, 1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f"font-size: 11px; color: {'#666' if _lt else '#8696a0'};"
        )
        hl.addWidget(self._count_label)
        close_btn = QPushButton("✕ CLOSE")
        close_btn.setFixedHeight(28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Close ghost messages panel")
        _close_fg = "#1a1a1a" if _lt else "#e9edef"
        _close_bd = "#d0d7de" if _lt else "#3b4a54"
        _close_hv = "#ffebee" if _lt else "rgba(239,83,80,0.18)"
        close_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {_close_bd}; border-radius: 4px; "
            f"padding: 0 10px; font-size: 11px; font-weight: 600; "
            f"color: {_close_fg}; background: transparent; }}"
            f"QPushButton:hover {{ background: {_close_hv}; "
            f"border-color: {'#c62828' if _lt else '#ef5350'}; "
            f"color: {'#c62828' if _lt else '#ef5350'}; }}"
        )
        close_btn.clicked.connect(lambda: self.setVisible(False))
        hl.addWidget(close_btn)
        root.addWidget(hdr)

        # ---- Search box (filter the visible ghost list) ----
        search_frame = QFrame()
        search_frame.setStyleSheet(
            f"QFrame {{ background: {_hdr_bg}; border-bottom: 1px solid {_hdr_border}; }}"
        )
        sl = QHBoxLayout(search_frame)
        sl.setContentsMargins(8, 4, 8, 4)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter ghost messages by text or sender...")
        self._search_edit.setFixedHeight(26)
        _input_bg = "#fff" if _lt else "#2a3942"
        _input_bd = "#d0d7de" if _lt else "#3b4a54"
        _input_fg = "#1b1b1b" if _lt else "#e9edef"
        self._search_edit.setStyleSheet(
            f"QLineEdit {{ background: {_input_bg}; border: 1px solid {_input_bd}; "
            f"border-radius: 4px; padding: 2px 6px; font-size: 11px; color: {_input_fg}; }}"
        )
        self._search_edit.textChanged.connect(self._apply_filter)
        sl.addWidget(self._search_edit)
        root.addWidget(search_frame)

        # ---- Empty-state placeholder ----
        self._empty_label = QLabel(
            "\U0001F47B  No recovered ghost messages in this chat.\n\n"
            "Ghost messages are deleted-for-everyone messages whose original\n"
            "text was reconstructed from quoted replies, edit history, or WAL\n"
            "recovery during ingestion."
        )
        self._empty_label.setVisible(False)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet(
            f"color: {'#666' if _lt else '#8696a0'}; font-size: 11px; padding: 20px;"
        )
        root.addWidget(self._empty_label)

        # ---- Ghost list ----
        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _sel_bg = "rgba(0,137,123,0.12)" if _lt else "rgba(0,188,212,0.12)"
        _hover_bg = "#f5f6f6" if _lt else "rgba(255,255,255,0.04)"
        _item_border = "#eee" if _lt else "rgba(255,255,255,0.06)"
        self._list.setStyleSheet(f"""
            QListWidget {{ background: transparent; }}
            QListWidget::item {{ padding: 8px 10px; border-bottom: 1px solid {_item_border}; }}
            QListWidget::item:selected {{ background: {_sel_bg}; }}
            QListWidget::item:hover {{ background: {_hover_bg}; }}
        """)
        self._list.itemClicked.connect(self._on_item_clicked)
        root.addWidget(self._list, 1)

        # Theme colors stashed for card rendering
        self._c_text = "#1a1a1a" if _lt else "#e9edef"
        self._c_text2 = "#666" if _lt else "#8696a0"
        self._c_accent = "#00897b" if _lt else "#00bcd4"
        self._c_danger = "#c62828" if _lt else "#ef5350"

        self._all_rows: list[dict] = []

    def load_for_conversation(self, conv_id: int) -> int:
        """Pull every ghost_message row for ``conv_id`` and populate
        the list.  Returns the count so the caller can decide whether
        to show a "no ghosts" toast.
        """
        from app.services.database import Database
        from datetime import datetime
        db = Database.get()
        self._conv_id = conv_id
        self._list.clear()
        self._all_rows = []

        if not conv_id:
            self._empty_label.setVisible(True)
            self._count_label.setText("0")
            return 0

        # The revoked message is what we navigate to (the bubble that
        # WhatsApp now renders as "this message was deleted").  Fall
        # back to recovered_from_msg_id (the quoted-reply that
        # preserved the text) when the original revoked row is
        # missing — happens when the revoke and recovery rows were
        # ingested in different passes.
        rows = db.fetchall(
            "SELECT g.id,"
            "       g.revoked_msg_id,"
            "       g.recovered_from_msg_id,"
            "       g.original_text,"
            "       g.original_type,"
            "       g.revoke_timestamp,"
            "       g.recovery_method,"
            "       COALESCE("
            "         CASE WHEN m.from_me = 1 THEN 'You' END,"
            "         NULLIF(c.resolved_name, ''),"
            "         NULLIF(c.wa_name, ''),"
            "         '+' || NULLIF(c.phone_number, ''),"
            "         'Unknown'"
            "       ) AS sender_name,"
            "       COALESCE(m.timestamp, g.revoke_timestamp) AS msg_ts"
            "  FROM ghost_message g"
            "  LEFT JOIN message m ON m.id = g.revoked_msg_id"
            "  LEFT JOIN contact c ON c.id = g.original_sender_id"
            " WHERE g.conversation_id = ?"
            " ORDER BY COALESCE(m.timestamp, g.revoke_timestamp) DESC, g.id DESC",
            (conv_id,),
        )

        for r in rows:
            row = dict(r)
            target_id = row["revoked_msg_id"] or row["recovered_from_msg_id"] or 0
            if not target_id:
                continue
            row["target_msg_id"] = target_id
            self._all_rows.append(row)

        self._render_rows(self._all_rows)
        return len(self._all_rows)

    def _render_rows(self, rows: list[dict]) -> None:
        from datetime import datetime
        self._list.clear()
        if not rows:
            self._empty_label.setVisible(True)
            self._count_label.setText("0")
            return
        self._empty_label.setVisible(False)
        self._count_label.setText(f"{len(rows):,}")

        for row in rows:
            text = (row.get("original_text") or "[deleted media]").strip()
            if len(text) > 240:
                text = text[:240] + "…"
            sender = row.get("sender_name") or "Unknown"
            ts = row.get("msg_ts")
            try:
                when = (datetime.fromtimestamp(int(ts) / 1000)
                                .strftime("%Y-%m-%d %H:%M:%S")) if ts else "—"
            except (ValueError, OSError):
                when = "—"
            method = row.get("recovery_method") or "?"

            label = QLabel(
                f"<div style='line-height:1.45;'>"
                f"<div style='font-size:11px; font-weight:600; color:{self._c_accent};'>"
                f"{_html_escape(sender)}"
                f"<span style='color:{self._c_text2}; font-weight:400; font-size:10px;'> · {when}</span>"
                f"</div>"
                f"<div style='font-size:11.5px; color:{self._c_text}; margin-top:2px;'>"
                f"{_html_escape(text)}"
                f"</div>"
                f"<div style='font-size:9px; color:{self._c_text2}; margin-top:3px; "
                f"font-style:italic;'>recovered via {_html_escape(method)}</div>"
                f"</div>"
            )
            label.setTextFormat(Qt.RichText)
            label.setWordWrap(True)
            label.setMargin(0)

            item = QListWidgetItem()
            item.setData(Qt.UserRole, row["target_msg_id"])
            item.setSizeHint(label.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, label)

    def _apply_filter(self, query: str) -> None:
        q = (query or "").strip().lower()
        if not q:
            self._render_rows(self._all_rows)
            return
        filtered = [
            r for r in self._all_rows
            if q in (r.get("original_text") or "").lower()
            or q in (r.get("sender_name") or "").lower()
        ]
        self._render_rows(filtered)

    def _on_item_clicked(self, item) -> None:
        msg_id = item.data(Qt.UserRole)
        if msg_id:
            self.ghost_selected.emit(int(msg_id))


def _html_escape(s: object) -> str:
    """Tiny HTML escape for the ghost panel's QLabel rich-text."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


class RepliesSidebarPanel(QFrame):
    """Right-side panel showing replies/quotes to a specific message."""

    reply_selected = Signal(int)      # msg_id — navigate to this reply
    cross_conv_nav = Signal(int, int) # conv_id, msg_id — navigate to reply in other chat

    _MEDIA_ICONS = {
        "image": "\U0001F4F7", "video": "\U0001F3AC", "gif": "\U0001F3AC",
        "audio": "\U0001F3B5", "voice": "\U0001F399", "document": "\U0001F4C4",
        "sticker": "\U0001F36D", "location": "\U0001F4CD", "vcard": "\U0001F464",
        "view_once_image": "\U0001F441", "view_once_video": "\U0001F441",
    }

    def __init__(self, parent, tm):
        super().__init__(parent)
        self._tm = tm
        self._current_msg_id: int | None = None
        self._source_conv_id: int | None = None
        _lt = tm.is_light

        self.setFixedWidth(400)
        self.setVisible(False)
        self.setObjectName("RepliesPanel")
        self.setStyleSheet(
            "QFrame#RepliesPanel { background: #ffffff; border-left: 1px solid #d0d7de; }"
            if _lt else
            "QFrame#RepliesPanel { background: #1a2026; border-left: 1px solid #2a3942; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header ----
        hdr = QFrame()
        hdr.setFixedHeight(44)
        _hdr_bg = "#f6f8fa" if _lt else "#111b21"
        _hdr_border = "#d0d7de" if _lt else "#2a3942"
        hdr.setStyleSheet(f"QFrame {{ background: {_hdr_bg}; border-bottom: 1px solid {_hdr_border}; }}")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 8, 0)
        self._hdr_label = QLabel("\u21A9 Replies")
        self._hdr_label.setStyleSheet(
            f"font-weight: bold; font-size: 12px; color: {'#1a1a1a' if _lt else '#e9edef'};"
        )
        hl.addWidget(self._hdr_label, 1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"font-size: 11px; color: {'#666' if _lt else '#8696a0'};")
        hl.addWidget(self._count_label)
        # Close button — explicit text + bordered background so it's
        # always visible regardless of which unicode glyphs Qt picks.
        close_btn = QPushButton("\u2715 CLOSE")
        close_btn.setFixedHeight(28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Close replies panel")
        _close_fg = "#1a1a1a" if _lt else "#e9edef"
        _close_bd = "#d0d7de" if _lt else "#3b4a54"
        _close_hv = "#ffebee" if _lt else "rgba(239,83,80,0.18)"
        close_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {_close_bd}; border-radius: 4px; "
            f"padding: 0 10px; font-size: 11px; font-weight: 600; "
            f"color: {_close_fg}; background: transparent; }}"
            f"QPushButton:hover {{ background: {_close_hv}; "
            f"border-color: {'#c62828' if _lt else '#ef5350'}; "
            f"color: {'#c62828' if _lt else '#ef5350'}; }}"
        )
        close_btn.clicked.connect(lambda: self.setVisible(False))
        hl.addWidget(close_btn)
        root.addWidget(hdr)

        # ---- Original message preview ----
        self._orig_frame = QFrame()
        _orig_bg = "#f0f4f8" if _lt else "#0d1418"
        _orig_border = "#d0d7de" if _lt else "#2a3942"
        self._orig_frame.setStyleSheet(
            f"QFrame {{ background: {_orig_bg}; border-bottom: 1px solid {_orig_border}; padding: 8px 12px; }}"
        )
        ol = QVBoxLayout(self._orig_frame)
        ol.setContentsMargins(12, 8, 12, 8)
        ol.setSpacing(2)
        self._orig_sender = QLabel("")
        self._orig_sender.setStyleSheet(f"font-size: 10px; font-weight: bold; color: {'#00897b' if _lt else '#00bcd4'};")
        ol.addWidget(self._orig_sender)
        self._orig_text = QLabel("")
        self._orig_text.setWordWrap(True)
        self._orig_text.setStyleSheet(f"font-size: 10px; color: {'#555' if _lt else '#8696a0'};")
        ol.addWidget(self._orig_text)
        self._orig_edit_warn = QLabel("")
        self._orig_edit_warn.setVisible(False)
        self._orig_edit_warn.setStyleSheet("font-size: 9px; color: #ff8f00; font-style: italic;")
        ol.addWidget(self._orig_edit_warn)

        # "Go to original" navigation button — jumps to the replied-to
        # message in the conversation. Emits reply_selected with the
        # stored _current_msg_id so the parent chat viewer scrolls there.
        self._goto_orig_btn = QPushButton("\u21A9 Go to original message")
        self._goto_orig_btn.setCursor(Qt.PointingHandCursor)
        self._goto_orig_btn.setFixedHeight(26)
        _go_bg = "#00897b" if _lt else "#00796b"
        _go_hv = "#00796b" if _lt else "#00897b"
        self._goto_orig_btn.setStyleSheet(
            f"QPushButton {{ background: {_go_bg}; color: #ffffff; border: none; "
            f"border-radius: 4px; font-size: 11px; font-weight: 600; padding: 0 10px; "
            f"margin-top: 4px; }}"
            f"QPushButton:hover {{ background: {_go_hv}; }}"
        )
        self._goto_orig_btn.clicked.connect(self._on_goto_original)
        ol.addWidget(self._goto_orig_btn)
        root.addWidget(self._orig_frame)

        # ---- Section labels (in this chat / other chats) ----
        # These will be added dynamically when loading

        # ---- Results list ----
        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _sel_bg = "rgba(0,137,123,0.12)" if _lt else "rgba(0,188,212,0.12)"
        _hover_bg = "#f5f6f6" if _lt else "rgba(255,255,255,0.04)"
        _item_border = "#eee" if _lt else "rgba(255,255,255,0.06)"
        self._list.setStyleSheet(f"""
            QListWidget {{ background: transparent; }}
            QListWidget::item {{ padding: 6px 10px; border-bottom: 1px solid {_item_border}; }}
            QListWidget::item:selected {{ background: {_sel_bg}; }}
            QListWidget::item:hover {{ background: {_hover_bg}; }}
        """)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self._list, 1)

        # Theme colors for card rendering
        self._c_text = "#1a1a1a" if _lt else "#e9edef"
        self._c_text2 = "#666" if _lt else "#8696a0"
        self._c_accent = "#00897b" if _lt else "#00bcd4"
        self._c_warn = "#ff8f00"
        self._c_danger = "#c62828" if _lt else "#ef5350"

    def load_replies(self, msg_id: int, source_key: str, conv_id: int):
        """Populate panel with all replies to the given message."""
        from app.services.database import Database
        db = Database.get()
        self._source_conv_id = conv_id
        # Keep a handle on the original msg_id so the "Go to original"
        # button below the preview can scroll the chat back to it.
        self._orig_msg_id = msg_id
        self._list.clear()

        if not source_key:
            source_key = db.scalar("SELECT source_key_id FROM message WHERE id = ?", (msg_id,))
        if not source_key:
            return

        # Get original message info
        orig = db.fetchone(
            "SELECT COALESCE(m.text_content, '') AS text, m.type_label, m.is_edited, "
            "CASE WHEN m.from_me = 1 THEN 'You' ELSE "
            "  COALESCE(c.resolved_name, c.wa_name, c.phone_number, 'Unknown') END AS sender, "
            "c.phone_jid "
            "FROM message m LEFT JOIN contact c ON c.id = m.sender_id WHERE m.id = ?",
            (msg_id,),
        )
        if orig:
            orig = dict(orig)
            jid = orig.get("phone_jid") or ""
            self._orig_sender.setText(f"\u21A9 Original by {orig.get('sender', 'Unknown')}"
                                      + (f" ({jid})" if jid else ""))
            preview = (orig.get("text") or orig.get("type_label") or "[media]")[:120]
            self._orig_text.setText(preview + ("..." if len(orig.get("text") or "") > 120 else ""))
            if orig.get("is_edited"):
                self._orig_edit_warn.setText("\u26A0 This message was edited after some replies were sent")
                self._orig_edit_warn.setVisible(True)
            else:
                self._orig_edit_warn.setVisible(False)

        # Check for original pre-edit key
        orig_key = db.scalar(
            "SELECT original_key_id FROM edit_history WHERE message_id = ? LIMIT 1",
            (msg_id,),
        )
        keys = [source_key]
        if orig_key and orig_key != source_key:
            keys.append(orig_key)

        # Query ALL replies across ALL conversations (reply_to_key_id).
        # ``from_me = 1`` always renders as "You" — regardless of
        # whether ``sender_id`` is populated.  ``contact_resolver``
        # often creates a self-contact entry from wa.db, so a
        # narrower "from_me=1 AND sender_id IS NULL" branch would
        # leak the owner's real name + number for their own
        # replies in this panel.
        placeholders = ",".join("?" * len(keys))
        _sender_sql = (
            "CASE WHEN m.from_me = 1 THEN 'You' "
            "  WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI' "
            "  ELSE COALESCE("
            "    CASE WHEN c.resolved_name NOT LIKE '%@lid' AND c.resolved_name != '' "
            "         THEN c.resolved_name END, "
            "    NULLIF(c.wa_name, ''), "
            "    NULLIF(c.display_name, ''), "
            "    '+' || NULLIF(c.phone_number, ''), "
            "    CASE WHEN m.rendered_sender NOT LIKE 'LID:%' AND m.rendered_sender != '' "
            "         THEN m.rendered_sender END, "
            "    REPLACE(c.phone_jid, '@s.whatsapp.net', '+'), "
            "    c.lid_display_name, "
            "    REPLACE(c.lid_jid, '@lid', ' [LID]'), "
            "    'Unknown') END"
        )
        # JID column hidden for the owner's own replies —
        # showing the owner's own phone number next to "You" is
        # redundant and exposes PII the analyst didn't ask for.
        _jid_sql = (
            "CASE WHEN m.from_me = 1 THEN '' "
            "  ELSE COALESCE(c.phone_jid, c.lid_jid, '') END"
        )
        _base_cols = (
            f"m.id, m.conversation_id, m.timestamp, "
            f"COALESCE(m.text_content, '') AS text, "
            f"m.message_type, m.type_label, m.from_me, m.is_bot_message, "
            f"m.is_revoked, m.is_edited, "
            f"{_sender_sql} AS sender, "
            f"{_jid_sql} AS phone_jid, "
            f"me.mime_type, me.media_name, me.media_caption, "
            f"me.duration_ms, me.page_count, "
            f"conv.display_name AS conv_name, "
            f"c.avatar_blob, m.sender_id"
        )
        _base_joins = (
            "LEFT JOIN contact c ON c.id = m.sender_id "
            "LEFT JOIN media me ON me.message_id = m.id "
            "LEFT JOIN conversation conv ON conv.id = m.conversation_id"
        )

        replies = db.fetchall(
            f"SELECT {_base_cols} FROM message m {_base_joins} "
            f"WHERE m.reply_to_key_id IN ({placeholders}) "
            f"ORDER BY m.conversation_id = ? DESC, m.timestamp ASC",
            keys + [conv_id],
        )

        # Also check message_comment table (channel/community thread replies)
        try:
            comment_replies = db.fetchall(
                f"SELECT {_base_cols} FROM message_comment mc "
                f"JOIN message m ON m.id = mc.reply_message_id "
                f"{_base_joins} "
                f"WHERE mc.parent_message_id = ? "
                f"ORDER BY m.timestamp ASC",
                (msg_id,),
            )
            if comment_replies:
                replies = list(replies) + list(comment_replies)
        except Exception:
            pass  # message_comment may not exist

        if not replies:
            self._hdr_label.setText("\u21A9 Replies")
            self._count_label.setText("0 replies")
            self.setVisible(True)
            return

        replies = [dict(r) for r in replies]
        same_chat = [r for r in replies if r["conversation_id"] == conv_id]
        other_chat = [r for r in replies if r["conversation_id"] != conv_id]

        self._hdr_label.setText("\u21A9 Replies")
        total = len(replies)
        self._count_label.setText(f"{total} repl{'ies' if total > 1 else 'y'}")

        # Add "In this chat" section
        if same_chat:
            self._add_section_header(f"In this chat ({len(same_chat)})")
            for r in same_chat:
                self._add_reply_item(r, is_cross_conv=False)

        # Add "In other conversations" section
        if other_chat:
            self._add_section_header(f"In other conversations ({len(other_chat)})")
            for r in other_chat:
                self._add_reply_item(r, is_cross_conv=True)

        self.setVisible(True)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _add_section_header(self, text: str):
        """Add a non-selectable section header to the list."""
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        _lt = self._tm.is_light
        f = item.font()
        f.setBold(True)
        f.setPointSize(9)
        item.setFont(f)
        item.setForeground(QColor(self._c_accent))
        item.setBackground(QColor("#f0f9f7" if _lt else "#0d1a1f"))
        self._list.addItem(item)

    def _add_reply_item(self, r: dict, is_cross_conv: bool = False):
        """Add a rich reply card with avatar as a list item."""
        from datetime import datetime
        from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
        from PySide6.QtGui import QPixmap, QPainter, QPainterPath

        sender = r.get("sender") or "Unknown"
        jid = r.get("phone_jid") or ""
        ts = r.get("timestamp")
        text = r.get("text") or ""
        caption = r.get("media_caption") or ""
        tl = r.get("type_label") or ""
        mime = r.get("mime_type") or ""
        media_name = r.get("media_name") or ""
        is_revoked = r.get("is_revoked")
        is_edited = r.get("is_edited")
        is_bot = r.get("is_bot_message")
        from_me = r.get("from_me")
        conv_name = r.get("conv_name") or ""
        duration = r.get("duration_ms")
        pages = r.get("page_count")
        avatar_blob = r.get("avatar_blob")
        sender_id = r.get("sender_id")

        _lt = self._tm.is_light

        # Format timestamp
        ts_str = ""
        if ts:
            try:
                ts_str = format_timestamp(ts, '%b %d %Y, %H:%M')
            except (ValueError, OSError):
                pass

        # Build identity line
        identity = sender
        if jid and "@" in jid:
            phone = jid.split("@")[0]
            if phone.isdigit():
                identity += f" (+{phone})"

        # Badges HTML
        badges = ""
        if from_me:
            badges += " <span style='color:#e65100;font-size:8px;font-weight:bold;background:rgba(230,81,0,0.1);padding:1px 3px;border-radius:2px;'>Owner</span>"
        if is_bot:
            badges += " <span style='color:#7c4dff;font-size:8px;font-weight:bold;background:rgba(124,77,255,0.1);padding:1px 3px;border-radius:2px;'>\U0001F916 AI</span>"
        if is_revoked:
            badges += " <span style='color:#ef5350;font-size:8px;'>[deleted]</span>"
        if is_edited:
            badges += " <span style='color:#ffa726;font-size:8px;'>[edited]</span>"

        # Media line
        media_line = ""
        if is_revoked:
            media_line = "\U0001F6AB This message was deleted"
        else:
            icon = self._MEDIA_ICONS.get(tl, "")
            if tl in ("image", "view_once_image"):
                media_line = f"{icon} Photo"
            elif tl in ("video", "view_once_video"):
                dur_str = f" ({duration // 1000 // 60}:{duration // 1000 % 60:02d})" if duration else ""
                media_line = f"{icon} Video{dur_str}"
            elif tl == "voice" or (tl == "audio" and mime and "ogg" in mime):
                dur_str = f" ({duration // 1000 // 60}:{duration // 1000 % 60:02d})" if duration else ""
                media_line = f"\U0001F399 Voice{dur_str}"
            elif tl == "audio":
                media_line = "\U0001F3B5 Audio"
            elif tl == "document":
                ext = media_name.split(".")[-1].upper() if "." in media_name else "DOC"
                pg = f" \u00B7 {pages}p" if pages else ""
                media_line = f"\U0001F4C4 {ext}{pg}"
            elif tl == "sticker":
                media_line = "\U0001F36D Sticker"
            elif tl in ("location", "live_location"):
                media_line = "\U0001F4CD Location"

        # Text display
        display_text = caption or text
        truncated = display_text or media_line or ""

        # Cross-conv label
        cross_label = f"\u2197 in {conv_name}" if is_cross_conv and conv_name else ""

        # Build HTML for the card content
        html_parts = []
        html_parts.append(f"<b style='color:{self._c_accent};font-size:10px;'>{identity}</b>{badges}")
        if jid:
            html_parts.append(f"<span style='color:{self._c_text2};font-size:8px;font-family:monospace;'>{jid}</span>")
        if media_line and not is_revoked:
            html_parts.append(f"<span style='color:{'#7c4dff' if _lt else '#b388ff'};font-size:9px;'>{media_line}</span>")
        if truncated:
            _tc = f"color:{self._c_danger}" if is_revoked else f"color:{self._c_text}"
            # Show full text — replace newlines with <br> for proper display
            _full = truncated.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            html_parts.append(f"<span style='{_tc};font-size:10px;'>{_full}</span>")
        if cross_label:
            html_parts.append(f"<span style='color:#7c4dff;font-size:9px;font-weight:bold;'>{cross_label}</span>")
        if ts_str:
            html_parts.append(f"<span style='color:{self._c_text2};font-size:8px;'>{ts_str}</span>")

        card_html = "<br>".join(html_parts)

        # Create widget with avatar + content
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(widget)
        hl.setContentsMargins(4, 4, 4, 4)
        hl.setSpacing(8)

        # Avatar (32x32 circle)
        avatar_lbl = QLabel()
        avatar_lbl.setFixedSize(32, 32)
        avatar_lbl.setAlignment(Qt.AlignCenter)
        if avatar_blob and len(avatar_blob) > 100:
            pxm = QPixmap()
            pxm.loadFromData(avatar_blob)
            if not pxm.isNull():
                scaled = pxm.scaled(32, 32, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                # Circular crop
                result = QPixmap(32, 32)
                result.fill(Qt.transparent)
                painter = QPainter(result)
                painter.setRenderHint(QPainter.Antialiasing)
                path = QPainterPath()
                path.addEllipse(0, 0, 32, 32)
                painter.setClipPath(path)
                painter.drawPixmap(0, 0, scaled)
                painter.end()
                avatar_lbl.setPixmap(result)
            else:
                initial = sender[0].upper() if sender else "?"
                _colors = ["#00897b", "#6a1b9a", "#c62828", "#1565c0", "#ef6c00", "#2e7d32"]
                _bg = _colors[(sender_id or 0) % len(_colors)]
                avatar_lbl.setText(initial)
                avatar_lbl.setStyleSheet(
                    f"background: {_bg}; color: white; border-radius: 16px;"
                    " font-weight: bold; font-size: 14px;"
                )
        else:
            initial = sender[0].upper() if sender else "?"
            _colors = ["#00897b", "#6a1b9a", "#c62828", "#1565c0", "#ef6c00", "#2e7d32"]
            _bg = _colors[(sender_id or 0) % len(_colors)]
            avatar_lbl.setText(initial)
            avatar_lbl.setStyleSheet(
                f"background: {_bg}; color: white; border-radius: 16px;"
                " font-weight: bold; font-size: 14px;"
            )
        hl.addWidget(avatar_lbl)

        # Content label
        content_lbl = QLabel(card_html)
        content_lbl.setTextFormat(Qt.RichText)
        content_lbl.setWordWrap(True)
        content_lbl.setStyleSheet(f"color: {self._c_text};")
        hl.addWidget(content_lbl, 1)

        # Create list item and set widget
        item = QListWidgetItem()
        item.setData(Qt.UserRole, r.get("id"))
        item.setData(Qt.UserRole + 1, r.get("conversation_id"))
        item.setData(Qt.UserRole + 2, is_cross_conv)
        item.setSizeHint(widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)

    def _on_item_clicked(self, item: QListWidgetItem):
        msg_id = item.data(Qt.UserRole)
        conv_id = item.data(Qt.UserRole + 1)
        is_cross = item.data(Qt.UserRole + 2)
        if not msg_id:
            return
        self._current_msg_id = msg_id
        if is_cross and conv_id and conv_id != self._source_conv_id:
            self.cross_conv_nav.emit(conv_id, msg_id)
        else:
            self.reply_selected.emit(msg_id)

    def _on_goto_original(self):
        """Navigate to the ORIGINAL message that is being replied to."""
        orig_id = getattr(self, "_orig_msg_id", None)
        if orig_id:
            # Reuse the same reply_selected signal the parent chat already
            # wires — the handler doesn't care whether the target is the
            # original or a reply, just scrolls to msg_id in this chat.
            self.reply_selected.emit(int(orig_id))

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        item = self._list.item(row)
        if item:
            msg_id = item.data(Qt.UserRole)
            if msg_id and msg_id != self._current_msg_id:
                self._on_item_clicked(item)

    def navigate_next(self):
        row = self._list.currentRow()
        # Skip section headers
        for r in range(row + 1, self._list.count()):
            it = self._list.item(r)
            if it and (it.flags() & Qt.ItemIsSelectable):
                self._list.setCurrentRow(r)
                break

    def navigate_prev(self):
        row = self._list.currentRow()
        for r in range(row - 1, -1, -1):
            it = self._list.item(r)
            if it and (it.flags() & Qt.ItemIsSelectable):
                self._list.setCurrentRow(r)
                break

    def clear(self):
        self._list.clear()
        self._current_msg_id = None
        self.setVisible(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.setVisible(False)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self._list.currentItem()
            if item:
                self._on_item_clicked(item)
        else:
            super().keyPressEvent(event)


class _SqliteDbWrap:
    """Minimal wrapper around a raw sqlite3 connection for _fetch_auxiliary_batch."""
    def __init__(self, conn):
        self._conn = conn
    def fetchall(self, sql, params=()):
        return self._conn.execute(sql, params).fetchall()
    def fetchone(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return cur.fetchone()


class _PrefetchWorker(QThread):
    """Background worker to fetch older messages without blocking the UI."""
    done = Signal(list, list, int)  # new_items, new_raw, new_start

    def __init__(self, db_path: str, sql: str, params: tuple,
                 new_start: int, ghost_map: dict, tagged_ids: set,
                 is_group: bool, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._sql = sql
        self._params = params
        self._new_start = new_start
        self._ghost_map = ghost_map
        self._tagged_ids = tagged_ids
        self._is_group = is_group

    def run(self):
        import sqlite3 as _sqlite3
        try:
            conn = _sqlite3.connect(self._db_path, uri=True)
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(self._sql, self._params).fetchall()
        except Exception:
            self.done.emit([], [], self._new_start)
            return

        new_items: list[dict] = []
        new_raw: list[dict] = []
        last_date_str = ""

        for row in rows:
            msg = self._build_msg_dict_static(row, self._ghost_map,
                                              self._tagged_ids, self._is_group)
            if (msg["quoted_text"] and msg["display_text"]
                    and msg["quoted_text"].strip() == msg["display_text"].strip()):
                msg["quoted_text"] = None

            ts = msg.get("timestamp")
            if ts and msg.get("message_type") != 7:
                try:
                    msg_date_str = format_timestamp(ts, "date")
                    if msg_date_str != last_date_str:
                        last_date_str = msg_date_str
                        date_label = format_timestamp(ts, '%B %d, %Y')
                        sep = {"id": -1, "message_type": -1,
                               "display_text": date_label, "timestamp": ts}
                        new_items.append(sep)
                except (ValueError, OSError):
                    pass
            new_items.append(msg)
            new_raw.append(msg)

        # Fetch auxiliary data (reactions, polls, mentions, etc.) via batch queries
        try:
            db_wrap = _SqliteDbWrap(conn)
            ChatMessageModel._fetch_auxiliary_batch(db_wrap, new_raw)
        except Exception:
            pass
        conn.close()

        # Link album containers to child images
        self._link_album_children_static(new_items, conn)
        self.done.emit(new_items, new_raw, self._new_start)

    @staticmethod
    def _load_album_children_map(conn, parent_ids: list[int]) -> dict[int, list[int]]:
        """Return {parent_msg_id: [child_msg_id ordered by sort_order]} from
        the message_association table.  Returns {} when the table doesn't
        exist (older analysis.dbs without album-backfill applied) so the
        caller can fall back to the timestamp heuristic.
        """
        if not parent_ids:
            return {}
        out: dict[int, list[int]] = {}
        qmarks = ",".join("?" * len(parent_ids))
        sql = (f"SELECT parent_message_id, child_message_id "
               f"FROM message_association "
               f"WHERE association_type = 2 AND parent_message_id IN ({qmarks}) "
               f"ORDER BY parent_message_id, sort_order")
        try:
            # Support both raw sqlite3 connections and our wrapper
            if hasattr(conn, "fetchall"):
                rows = conn.fetchall(sql, tuple(parent_ids))
            else:
                rows = conn.execute(sql, parent_ids).fetchall()
        except Exception:
            return {}
        for r in rows:
            pid, cid = (r[0], r[1]) if not hasattr(r, "keys") else (r["parent_message_id"], r["child_message_id"])
            out.setdefault(pid, []).append(cid)
        return out

    @staticmethod
    def _load_album_meta_map(conn, parent_ids: list[int]) -> dict[int, dict]:
        """Return {parent_msg_id: {image_count, video_count, expected_*,
        missing_*, note}} from the message_album table.  This is the
        authoritative count per album (set by msgstore + album_ingester);
        the renderer uses it to show "N photos . M videos" in the album
        header even when individual children fail to link or render.
        """
        if not parent_ids:
            return {}
        out: dict[int, dict] = {}
        qmarks = ",".join("?" * len(parent_ids))
        sql = (f"SELECT message_id, image_count, video_count, "
               f"       expected_image_count, expected_video_count, "
               f"       missing_image_count, missing_video_count, "
               f"       actual_child_count, note "
               f"FROM message_album WHERE message_id IN ({qmarks})")
        try:
            if hasattr(conn, "fetchall"):
                rows = conn.fetchall(sql, tuple(parent_ids))
            else:
                rows = conn.execute(sql, parent_ids).fetchall()
        except Exception:
            return {}
        for r in rows:
            mid = r[0] if not hasattr(r, "keys") else r["message_id"]
            out[mid] = {
                "image_count":           (r[1] if not hasattr(r, "keys") else r["image_count"]) or 0,
                "video_count":           (r[2] if not hasattr(r, "keys") else r["video_count"]) or 0,
                "expected_image_count":   r[3] if not hasattr(r, "keys") else r["expected_image_count"],
                "expected_video_count":   r[4] if not hasattr(r, "keys") else r["expected_video_count"],
                "missing_image_count":   (r[5] if not hasattr(r, "keys") else r["missing_image_count"]) or 0,
                "missing_video_count":   (r[6] if not hasattr(r, "keys") else r["missing_video_count"]) or 0,
                "actual_child_count":    (r[7] if not hasattr(r, "keys") else r["actual_child_count"]) or 0,
                "note":                   r[8] if not hasattr(r, "keys") else r["note"],
            }
        return out

    @staticmethod
    def _link_album_children_static(items: list, conn=None):
        """Link album containers to child images.

        Preferred: query message_association (association_type=2) for the
        authoritative parent->children graph from msgstore.  Fallback:
        the legacy timestamp-proximity heuristic (10s same-sender window)
        kicks in when the table doesn't exist or returns no rows for an
        album - keeps things working on analysis.dbs that pre-date the
        album_ingester stage.
        """
        # Items by id - quick lookup for child rows in the SQL path.  Skip
        # date separators (message_type=-1) which carry id=-1.
        items_by_id = {
            item["id"]: item for item in items
            if item.get("id") is not None and item.get("message_type", -1) != -1
        }

        album_items = [it for it in items if it.get("type_label") == "album"]
        if not album_items:
            return

        children_map: dict[int, list[int]] = {}
        meta_map: dict[int, dict] = {}
        if conn is not None:
            parent_ids = [it["id"] for it in album_items if it.get("id")]
            children_map = _PrefetchWorker._load_album_children_map(conn, parent_ids)
            meta_map = _PrefetchWorker._load_album_meta_map(conn, parent_ids)
        # Stamp every album parent with its authoritative meta counts so
        # the renderer can always show "N photos . M videos" in the
        # header, even when album_children comes back empty.
        for it in album_items:
            meta = meta_map.get(it.get("id"))
            if meta:
                it["album_meta"] = meta

        # Build a set of ALL child IDs claimed by any SQL-mapped album.
        # When the heuristic fallback runs (only when SQL returned nothing
        # for that specific album), this prevents it from glomming a
        # child that already belongs to a NEIGHBOURING album's SQL list.
        sql_claimed_children: set[int] = set()
        for plist in children_map.values():
            for cid in plist:
                sql_claimed_children.add(cid)

        def _child_payload(child: dict) -> dict:
            return {
                "id": child.get("id"),
                "thumbnail_blob": child.get("thumbnail_blob"),
                "has_thumb": child.get("has_thumb"),
                "type_label": child.get("type_label"),
                "mime_type": child.get("mime_type"),
                "file_path": child.get("file_path"),
                "resolved_file_path": child.get("resolved_file_path"),
                "media_file_exists": child.get("media_file_exists"),
                "media_url": child.get("media_url"),
                "media_width": child.get("media_width"),
                "media_height": child.get("media_height"),
            }

        for i, item in enumerate(items):
            if item.get("type_label") != "album":
                continue
            children: list[dict] = []
            sql_child_ids = children_map.get(item.get("id"), [])

            if sql_child_ids:
                # SQL path - use the message_association mapping.  This is
                # the authoritative graph from msgstore.message_association
                # (association_type=2).  Children outside the current page
                # window are skipped here; they'll render as part of the
                # album when scrolled into view.
                for cid in sql_child_ids:
                    child = items_by_id.get(cid)
                    if not child:
                        continue
                    children.append(_child_payload(child))
                    child["album_parent_id"] = item.get("id")
            else:
                # Heuristic fallback - kicks in only for albums whose
                # parent_id has NO row in message_association (older
                # analysis.dbs without album-backfill applied).  Walk the
                # subsequent rows like the original logic, but skip any
                # children that another album's SQL row has already
                # claimed - otherwise back-to-back albums (parent A,
                # parent B, then A's children) would have B's heuristic
                # incorrectly grab A's children.
                album_ts = item.get("timestamp") or 0
                album_from_me = item.get("from_me")
                for j in range(i + 1, len(items)):
                    child = items[j]
                    if child.get("message_type") == -1:
                        continue
                    cid = child.get("id")
                    if cid in sql_claimed_children:
                        # Belongs to another album; don't steal it.
                        continue
                    c_ts = child.get("timestamp") or 0
                    if (child.get("type_label") in ("image", "video", "gif", "animated_gif")
                            and child.get("from_me") == album_from_me
                            and 0 <= (c_ts - album_ts) <= 10000):
                        children.append(_child_payload(child))
                        child["album_parent_id"] = item.get("id")
                    else:
                        break
            if children:
                item["album_children"] = children

    @staticmethod
    def _build_msg_dict_static(row, ghost_map, tagged_ids, is_group) -> dict:
        """Build msg dict from row — static version for background thread."""
        msg = {
            "id": row[0],
            "from_me": bool(row[1]),
            "text_content": row[2],
            "message_type": row[3],
            "type_label": row[4],
            "timestamp": row[5],
            "status": row[6] or 0,
            "is_starred": bool(row[7]),
            "is_forwarded": bool(row[8]),
            "is_edited": bool(row[9]),
            "is_revoked": bool(row[10]),
            "quoted_text": row[11],
            "sender_id": row[12],
            "sender_name": row[13],
            "thumbnail_blob": row[14],
            "has_thumb": row[14] is not None and len(row[14]) > 20 if row[14] else False,
            "mime_type": row[15],
            "media_caption": row[16],
            "source_msg_id": row[17],
            "source_key_id": row[18],
            "forward_score": row[19],
            "received_timestamp": row[20],
            "receipt_server_timestamp": row[21],
            "reply_to_key_id": row[22],
            "is_view_once": bool(row[23]) if row[23] else False,
            "is_ephemeral": bool(row[24]) if row[24] else False,
            "is_bot_message": bool(row[25]) if row[25] else False,
            "broadcast": bool(row[26]) if row[26] else False,
            "phone_jid": row[27],
            "lid_jid": row[28],
            "wa_name": row[29],
            "display_name": row[30],
            "file_path": row[31],
            "file_size": row[32],
            "resolved_file_path": row[33],
            "media_file_exists": bool(row[34]) if row[34] else False,
            "media_url": row[35],
            "media_key": row[36],
            "file_hash": row[37],
            "recovery_method": row[38] if len(row) > 38 else "",
            "media_status": row[39] if len(row) > 39 else "",
            "media_width": row[40] if len(row) > 40 else None,
            "media_height": row[41] if len(row) > 41 else None,
            "media_duration_ms": row[42] if len(row) > 42 else None,
            "media_name": row[43] if len(row) > 43 else "",
            "system_event_label": row[44] if len(row) > 44 else None,
            "system_event_data": row[45] if len(row) > 45 else None,
            "system_event_actor": row[46] if len(row) > 46 else None,
            "system_event_target": row[47] if len(row) > 47 else None,
            "reactions_str": row[48] if len(row) > 48 else None,
            "reaction_count": row[49] if len(row) > 49 else 0,
            "reactions_detail": row[50] if len(row) > 50 else None,
            "link_details": row[51] if len(row) > 51 else None,
            "call_duration": row[52] if len(row) > 52 else None,
            "call_is_video": bool(row[53]) if len(row) > 53 and row[53] else False,
            "call_result_label": row[54] if len(row) > 54 else None,
            "call_is_group": bool(row[55]) if len(row) > 55 and row[55] else False,
            "poll_options": row[56] if len(row) > 56 else None,
            "poll_total_voters": row[57] if len(row) > 57 else 0,
            "mentions_str": row[58] if len(row) > 58 else None,
            "loc_latitude": row[59] if len(row) > 59 else None,
            "loc_longitude": row[60] if len(row) > 60 else None,
            "loc_place_name": row[61] if len(row) > 61 else None,
            "loc_place_address": row[62] if len(row) > 62 else None,
            "loc_is_live": bool(row[63]) if len(row) > 63 and row[63] else False,
            "loc_live_duration": row[64] if len(row) > 64 else None,
            "first_delivered_ts": row[65] if len(row) > 65 else None,
            "first_read_ts": row[66] if len(row) > 66 else None,
            "sender_device_number": row[67] if len(row) > 67 else -1,
            "sender_is_primary": row[68] if len(row) > 68 else -1,
            "sender_platform_label": row[69] if len(row) > 69 else "",
            "origin": row[70] if len(row) > 70 else 0,
            "origination_flags": row[71] if len(row) > 71 else 0,
            "member_label": row[72] if len(row) > 72 else None,
            "nc_old_phone": row[73] if len(row) > 73 else None,
            "nc_new_phone": row[74] if len(row) > 74 else None,
            "revoked_by_admin_id": row[75] if len(row) > 75 else None,
            "revoked_by_admin_name": row[76] if len(row) > 76 else None,
            "ephemeral_duration": row[77] if len(row) > 77 else None,
            "community_name": row[78] if len(row) > 78 else None,
            "sender_avatar_blob": row[79] if len(row) > 79 else None,
            "se_actor_id": row[80] if len(row) > 80 else None,
            "se_target_id": row[81] if len(row) > 81 else None,
            "call_participants": row[82] if len(row) > 82 else None,
            "poll_voters": row[83] if len(row) > 83 else None,
            "vcard_data": row[84] if len(row) > 84 else None,
            "quoted_type": row[85] if len(row) > 85 else None,
            "scheduled_event_data": row[86] if len(row) > 86 else None,
            "rendered_system_text": row[87] if len(row) > 87 else None,
            "page_count": row[88] if len(row) > 88 else None,
            "view_once_state": row[89] if len(row) > 89 else None,
            "sender_is_meta_verified": bool(row[90]) if len(row) > 90 and row[90] else False,
            "sender_is_business": bool(row[91]) if len(row) > 91 and row[91] else False,
            "sender_jid_row_id": row[92] if len(row) > 92 else None,
            "source_chat_row_id": row[93] if len(row) > 93 else None,
            "source_media_row_id": row[94] if len(row) > 94 else None,
            "loc_thumbnail_blob": row[95] if len(row) > 95 else None,
            "loc_final_lat": row[96] if len(row) > 96 else None,
            "loc_final_lon": row[97] if len(row) > 97 else None,
            "loc_final_ts": row[98] if len(row) > 98 else None,
            "loc_map_url": row[99] if len(row) > 99 else None,
            "is_status_reply": bool(row[100]) if len(row) > 100 and row[100] else False,
            "reply_count": row[101] if len(row) > 101 else 0,
            "last_edit_timestamp": row[102] if len(row) > 102 else None,
            "edit_count": row[103] if len(row) > 103 else 0,
        }

        msg_id = msg["id"]
        is_ghost = False
        if msg_id in ghost_map:
            ghost = ghost_map[msg_id]
            is_ghost = True
            if ghost.get("original_text"):
                msg["text_content"] = ghost["original_text"]
                msg["is_revoked"] = False
        msg["is_ghost"] = is_ghost
        msg["is_tagged"] = msg_id in tagged_ids

        if msg["is_revoked"] and not is_ghost:
            msg["display_text"] = "This message was deleted"
        elif msg["media_caption"]:
            msg["display_text"] = msg["media_caption"]
        elif msg["text_content"]:
            msg["display_text"] = msg["text_content"]
        else:
            msg["display_text"] = ""
        msg["show_sender"] = is_group and not msg["from_me"]
        return msg


class _TileWorker(QThread):
    """Single persistent worker thread for on-demand tile loading.

    Architecture:
    - ONE long-lived thread with a task queue (not one thread per tile)
    - Tasks are (tile_idx, tile_start, count, gen) tuples
    - clear_pending() atomically drains the queue (stale scroll positions)
    - gen (generation counter) lets us silently discard results from old conversations
    - Reuses a single SQLite connection across all tile loads
    """
    tile_ready = Signal(int, list, int)  # tile_start, raw_msgs, gen

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: _queue.Queue = _queue.Queue()
        self._stop_flag = _threading.Event()
        self._db_path: str | None = None
        self._ghost_map: dict = {}
        self._tagged_ids: set = set()
        self._is_group: bool = False

    def configure(self, db_path: str, ghost_map: dict, tagged_ids: set, is_group: bool):
        """Update DB path and metadata for current conversation."""
        self._db_path = db_path
        self._ghost_map = ghost_map
        self._tagged_ids = tagged_ids
        self._is_group = is_group

    def enqueue(self, sql: str, params: tuple, tile_start: int, count: int, gen: int):
        """Add a tile load task to the queue."""
        self._queue.put((sql, params, tile_start, count, gen))

    def clear_pending(self):
        """Drain all pending tasks (user scrolled past them)."""
        while True:
            try:
                self._queue.get_nowait()
            except _queue.Empty:
                break

    def shutdown(self):
        """Stop the worker thread gracefully."""
        self._stop_flag.set()
        self._queue.put(None)  # unblock get()

    def run(self):
        import sqlite3 as _sqlite3
        conn = None
        current_db_path = None

        while not self._stop_flag.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except _queue.Empty:
                continue
            if task is None:
                break  # shutdown sentinel

            sql, params, tile_start, count, gen = task

            # Open/reopen connection if DB path changed
            if self._db_path != current_db_path:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                current_db_path = self._db_path
                try:
                    conn = _sqlite3.connect(f"file:{current_db_path}?mode=ro&immutable=1", uri=True)
                    conn.execute("PRAGMA query_only = ON")
                    conn.execute("PRAGMA cache_size = -200000")    # 200MB (match main connection)
                    conn.execute("PRAGMA mmap_size = 2000000000")  # 2GB memory-mapped I/O
                    conn.execute("PRAGMA temp_store = MEMORY")
                except Exception as e:
                    print(f"[TileWorker] DB connect error: {e}")
                    conn = None
                    continue

            if not conn:
                continue

            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception as e:
                print(f"[TileWorker] Query error: {e}")
                # Emit even on error so Python discards
                # pending_tile_requests and JS's pendingTileRequests
                # clears via the stall sweep — silently dropping
                # the result would leave the tile locked forever.
                self.tile_ready.emit(tile_start, [], gen)
                continue

            # Build message dicts
            raw_msgs = []
            try:
                for row in rows:
                    msg = _PrefetchWorker._build_msg_dict_static(
                        row, self._ghost_map, self._tagged_ids, self._is_group
                    )
                    if (msg["quoted_text"] and msg["display_text"]
                            and msg["quoted_text"].strip() == msg["display_text"].strip()):
                        msg["quoted_text"] = None
                    raw_msgs.append(msg)
            except Exception as e:
                print(f"[TileWorker] build error: {e}")
                self.tile_ready.emit(tile_start, [], gen)
                continue

            # Fetch auxiliary data (reactions, polls, mentions, etc.)
            try:
                db_wrap = _SqliteDbWrap(conn)
                ChatMessageModel._fetch_auxiliary_batch(db_wrap, raw_msgs)
            except Exception:
                pass

            # Link album containers to children
            _PrefetchWorker._link_album_children_static(raw_msgs, conn)

            # Emit result (checked against gen on the receiving end)
            self.tile_ready.emit(tile_start, raw_msgs, gen)

        if conn:
            try:
                conn.close()
            except Exception:
                pass


class ChatMessageModel(QAbstractListModel):
    """Single-column list model providing full message dicts via MSG_DATA_ROLE.
    Injects date separator items (message_type = -1) between day boundaries.
    Builds a key_id -> row index for quoted-message navigation.
    Supports ghost message overlay from ghost_message table.

    prefetch_done signal: emitted after async prefetch data is inserted into the model.
    Carries the count of new items prepended."""
    prefetch_done = Signal(int)  # count of items prepended

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[dict] = []
        self._raw_data: list[dict] = []  # messages without separators
        self._total: int = 0
        self._conv_id: int | None = None
        self._is_group: bool = False
        self._search_text: str = ""
        self._date_from: int | None = None  # ms timestamp
        self._date_to: int | None = None    # ms timestamp
        self._ghost_only: bool = False       # show only ghost messages
        self._missing_media_only: bool = False  # show only downloadable missing media
        self._sender_filter_id: int | None = None  # filter by sender contact id
        self._db = Database.get()
        # Index for quick lookup: source_key_id -> row in _data
        self._key_id_to_row: dict[str, int] = {}
        # Index for quick lookup: message_id -> row in _data
        self._id_to_row: dict[int, int] = {}
        # Ghost message lookup: revoked_msg_id -> ghost info
        self._ghost_map: dict[int, dict] = {}
        # Tagged message IDs (investigator tags)
        self._tagged_ids: set[int] = set()
        self._tagged_ids_loaded = False
        # Offset of the oldest loaded message in DB result order
        self._loaded_start: int = 0
        # Async prefetch state
        self._prefetch_worker: _PrefetchWorker | None = None
        self._prefetch_pending = False
        # Owner contact_id for sender filter merging
        self._owner_contact_id: int = 0
        try:
            _oc_row = self._db.execute(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_contact_id'"
            ).fetchone()
            if _oc_row and _oc_row[0]:
                self._owner_contact_id = int(_oc_row[0])
        except Exception:
            pass
        self._ensure_vcard_table()
        self._ensure_scheduled_event_table()
        self._migrate_system_event_labels()

    def _ensure_vcard_table(self):
        """Create message_vcard_data table if it doesn't exist (pre-re-ingestion compat)."""
        try:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_vcard_data'"
            ).fetchone()
            if exists:
                return
        except Exception:
            pass
        try:
            self._db.execute_write("""
                CREATE TABLE IF NOT EXISTS message_vcard_data (
                    id              INTEGER PRIMARY KEY,
                    message_id      INTEGER NOT NULL REFERENCES message(id),
                    display_name    TEXT,
                    phone_numbers   TEXT,
                    vcard_index     INTEGER DEFAULT 0
                )
            """)
            self._db.checkpoint_and_reconnect()
        except Exception as e:
            print(f"[ChatModel] vcard_data table creation error: {e}")

    def _ensure_scheduled_event_table(self):
        """Create scheduled_event table if it doesn't exist (pre-re-ingestion compat)."""
        try:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_event'"
            ).fetchone()
            if exists:
                return
        except Exception:
            pass
        try:
            self._db.execute_write("""
                CREATE TABLE IF NOT EXISTS scheduled_event (
                    id              INTEGER PRIMARY KEY,
                    source_msg_row_id INTEGER,
                    message_id      INTEGER REFERENCES message(id),
                    conversation_id INTEGER REFERENCES conversation(id),
                    name            TEXT,
                    description     TEXT,
                    start_time      INTEGER,
                    end_time        INTEGER,
                    location_latitude REAL,
                    location_longitude REAL,
                    location_name   TEXT,
                    location_address TEXT,
                    join_link       TEXT,
                    is_schedule_call INTEGER DEFAULT 0,
                    is_canceled     INTEGER DEFAULT 0,
                    event_state     INTEGER DEFAULT 0,
                    response_going  INTEGER DEFAULT 0,
                    response_not_going INTEGER DEFAULT 0,
                    duration_seconds INTEGER DEFAULT 0,
                    extra_data      TEXT,
                    timestamp       INTEGER
                )
            """)
            self._db.checkpoint_and_reconnect()
        except Exception as e:
            print(f"[ChatModel] scheduled_event table creation error: {e}")

    def _migrate_system_event_labels(self):
        """Runtime migration: fix stale ``event_label`` values in
        already-ingested analysis DBs.

        Earlier ingesters mapped ``action_type = 108`` to
        ``'community_settings_changed'``, but every observed
        event of that type carries a child group node and is
        actually a ``subgroup_added`` event paired with
        ``action_type = 110``.  Without this migration the
        viewer renders "You changed the community settings"
        instead of 'You added the group <name>'.

        Idempotent — safe to run on every load.
        """
        try:
            # Check how many rows still use the old label
            stale = self._db.scalar(
                "SELECT COUNT(*) FROM system_event "
                "WHERE event_type = 108 AND event_label = 'community_settings_changed'"
            ) or 0
            if stale == 0:
                return
            self._db.execute_write(
                "UPDATE system_event SET event_label = 'subgroup_added' "
                "WHERE event_type = 108 AND event_label = 'community_settings_changed'"
            )
            self._db.checkpoint_and_reconnect()
            print(f"[ChatModel] Migrated {stale} system_event rows: 108 → subgroup_added")
        except Exception as e:
            print(f"[ChatModel] system_event label migration skipped: {e}")

    @property
    def total_rows(self) -> int:
        return self._total

    @property
    def is_group(self) -> bool:
        return self._is_group

    def set_conversation(self, conv_id: int, is_group: bool = False) -> None:
        self._conv_id = conv_id
        self._is_group = is_group
        self._search_text = ""
        self._date_from = None
        self._date_to = None
        self._load_ghost_map(conv_id)
        self._reload()

    def _load_ghost_map(self, conv_id: int):
        """Load ghost messages for this conversation into a lookup dict."""
        self._ghost_map.clear()
        rows = self._db.fetchall(
            "SELECT revoked_msg_id, original_text, original_type, recovery_method "
            "FROM ghost_message WHERE conversation_id = ?",
            (conv_id,),
        )
        for r in rows:
            msg_id = r[0]
            if msg_id not in self._ghost_map:
                self._ghost_map[msg_id] = {
                    "original_text": r[1],
                    "original_type": r[2],
                    "recovery_method": r[3],
                }

    def search(self, text: str) -> None:
        self._search_text = text.strip()
        self._reload()

    def set_date_range(self, from_ms: int | None, to_ms: int | None) -> None:
        self._date_from = from_ms
        self._date_to = to_ms
        self._reload()

    def set_ghost_only(self, enabled: bool) -> None:
        self._ghost_only = enabled
        self._reload()

    def set_missing_media_only(self, enabled: bool) -> None:
        self._missing_media_only = enabled
        self._reload()

    def set_sender_filter(self, sender_id: int | None) -> None:
        self._sender_filter_id = sender_id
        self._reload()

    def find_row_by_msg_id(self, msg_id: int) -> int:
        """Find row index for a message with given id. Returns -1 if not found.
        Uses direct DB offset lookup instead of loading all messages sequentially."""
        if msg_id in self._id_to_row:
            return self._id_to_row[msg_id]

        if self._conv_id is None:
            return -1

        # Direct approach: find the message's position in the conversation
        # and reload data centered around it (avoids loading ALL messages)
        where, params = self._where_clause()
        offset = self._db.scalar(
            f"SELECT COUNT(*) FROM message m WHERE {where}"
            " AND (m.timestamp, m.sort_id) < (SELECT timestamp, sort_id FROM message WHERE id = ?)",
            tuple(params) + (msg_id,),
        )
        if offset is None:
            return -1

        # Reload data centered around the target message
        self.beginResetModel()
        self._data.clear()
        self._raw_data.clear()
        self._key_id_to_row.clear()
        self._id_to_row.clear()
        # Load a window around the target: some before + batch after
        self._loaded_start = max(0, offset - BATCH_SIZE // 4)
        self._fetch_batch(where, params)
        self.endResetModel()

        return self._id_to_row.get(msg_id, -1)

    def find_row_by_key_id(self, key_id: str) -> int:
        """Find row index for a message with given source_key_id. Returns -1 if not found.
        Uses direct DB offset lookup instead of loading all messages sequentially."""
        if key_id in self._key_id_to_row:
            return self._key_id_to_row[key_id]

        if self._conv_id is None:
            return -1

        # Find the message ID and its sort position directly
        row_info = self._db.fetchone(
            "SELECT id, sort_id FROM message WHERE source_key_id = ? AND conversation_id = ?",
            (key_id, self._conv_id),
        )
        if not row_info:
            return -1

        msg_id = row_info[0]
        # Delegate to the msg_id-based lookup (which does the smart jump)
        return self.find_row_by_msg_id(msg_id)

    def get_message_at(self, row: int) -> dict | None:
        if 0 <= row < len(self._data):
            return self._data[row]
        return None

    def _where_clause(self) -> tuple[str, list]:
        parts = ["m.conversation_id = ?"]
        params: list = [self._conv_id]
        # Hide *non-HD* association children only.  WhatsApp's
        # message_association table couples several distinct
        # patterns under one mechanism — each handled differently
        # in the chat:
        #
        #   * type 7 / 12 — DUAL-QUALITY video/image (assoc_kind
        #     = 'hd'): BOTH the SD parent and HD child are real
        #     messages forensically.  Show both.  The renderer
        #     adds a clickable "HD pair" pill on each bubble
        #     cross-referencing the twin's msg #.
        #   * type 11 — MOTION PHOTO clip (assoc_kind = 'motion')
        #   * type 4  — STATUS link-preview (assoc_kind = 'status')
        #   * type 6  — POLL option image (assoc_kind = 'poll')
        #     all three are scaffolding for the parent's bubble;
        #     hide them.
        #
        # The COALESCE handles older databases ingested before
        # ``assoc_kind`` existed: there ``assoc_kind`` is NULL
        # everywhere, so ``!= 'hd'`` is true for every row and
        # we hide them all (the safe legacy behaviour).  The
        # database.py migration backfills 'hd' / 'motion' on
        # existing DBs by inspecting parent-side pointers, so
        # most existing cases get the new behaviour without
        # re-ingestion; for everything else, re-ingest rebuilds
        # ``assoc_kind`` from scratch.
        #
        # Performance: this filter relies on
        # ``idx_media_is_hd_twin`` (partial index over the few %
        # of rows with is_hd_twin=1).  The COALESCE check is a
        # cheap sequential filter on the small subset returned
        # by the index — no correlated subquery, no full-table
        # scan.
        parts.append(
            "m.id NOT IN ("
            "  SELECT message_id FROM media "
            "   WHERE is_hd_twin = 1 "
            "     AND COALESCE(assoc_kind, '') != 'hd'"
            ")"
        )
        if self._search_text:
            parts.append(
                "(m.text_content LIKE ? OR m.id IN ("
                "SELECT ld.message_id FROM message_link_detail ld "
                "WHERE ld.page_title LIKE ? OR ld.description LIKE ?"
                "))"
            )
            like = f"%{self._search_text}%"
            params.extend([like, like, like])
        if self._date_from is not None:
            parts.append("m.timestamp >= ?")
            params.append(self._date_from)
        if self._date_to is not None:
            parts.append("m.timestamp <= ?")
            params.append(self._date_to)
        if self._ghost_only:
            parts.append(
                "m.id IN (SELECT gm.revoked_msg_id FROM ghost_message gm "
                "WHERE gm.conversation_id = m.conversation_id)"
            )
        if self._missing_media_only:
            parts.append(
                "m.id IN (SELECT me3.message_id FROM media me3 "
                "WHERE me3.media_url IS NOT NULL AND me3.media_key IS NOT NULL "
                "AND (me3.file_exists = 0 OR me3.file_exists IS NULL))"
            )
        if self._sender_filter_id is not None:
            if self._sender_filter_id == -1:
                # Owner filter: match both NULL sender (outgoing) and owner contact_id
                owner_cid = self._owner_contact_id
                if owner_cid and owner_cid > 0:
                    parts.append("(m.sender_id IS NULL OR m.sender_id = ?)")
                    params.append(owner_cid)
                else:
                    parts.append("m.sender_id IS NULL")
            else:
                parts.append("m.sender_id = ?")
                params.append(self._sender_filter_id)
        return " AND ".join(parts), params

    def _reload(self) -> None:
        t0 = _time.perf_counter()
        self.beginResetModel()
        self._data.clear()
        self._raw_data.clear()
        self._key_id_to_row.clear()
        self._id_to_row.clear()
        if self._conv_id is None:
            self._total = 0
            self._loaded_start = 0
            self.endResetModel()
            return
        where, params = self._where_clause()
        t1 = _time.perf_counter()
        self._total = self._db.scalar(
            f"SELECT COUNT(*) FROM message m WHERE {where}", tuple(params)
        ) or 0
        t2 = _time.perf_counter()

        # Build anchor table for keyset pagination — deferred to background
        # to avoid blocking initial load. Set empty now, populate async.
        self._anchors = []
        t_anchor = _time.perf_counter()

        # Always start from the LAST tile so the initial JS render
        # has real data at the bottom rather than tombstones.
        # Computing ``_loaded_start = (total - 1) // BATCH_SIZE *
        # BATCH_SIZE`` snaps to the start of the tile containing
        # the very last message, which guarantees the actual last
        # tile is the one we load first.  An offset based on
        # ``total - INITIAL_BATCH`` rounded to a tile boundary
        # would sometimes miss the trailing tile and produce a
        # transient mis-scroll on chat open.
        if self._total > 0:
            last_tile_start = ((self._total - 1) // BATCH_SIZE) * BATCH_SIZE
            self._loaded_start = last_tile_start
        else:
            self._loaded_start = 0
        self._fetch_batch(where, params, limit=INITIAL_BATCH)
        t3 = _time.perf_counter()
        self.endResetModel()
        print(f"[ChatModel] _reload: total={self._total}, loaded_start={self._loaded_start}, "
              f"raw_count={len(self._raw_data)}, anchors={len(self._anchors)} | "
              f"COUNT={t2-t1:.3f}s, ANCHOR={t_anchor-t2:.3f}s, FETCH={t3-t_anchor:.3f}s, TOTAL={t3-t0:.3f}s")

    def _base_sql(self, where: str) -> str:
        """Core message query — fast, no correlated subqueries.

        Returns 85 columns in the same positional order as before so
        _build_msg_dict_static works unchanged. Auxiliary data (reactions,
        polls, mentions, calls, links, receipts, device, vcards, scheduled)
        is filled in by _fetch_auxiliary_batch() AFTER the core query.
        """
        return f"""
            SELECT m.id, m.from_me, COALESCE(m.text_content, '') AS text_content,
                   m.message_type, COALESCE(m.type_label, '') AS type_label,
                   m.timestamp, m.status,
                   m.is_starred, m.is_forwarded, m.is_edited, m.is_revoked,
                   m.quoted_text, m.sender_id,
                   -- [13] sender_name: prefer pre-computed, fallback for old DBs
                   CASE
                       WHEN m.rendered_sender IS NOT NULL THEN m.rendered_sender
                       WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI'
                       ELSE COALESCE(
                         NULLIF(c.resolved_name, ''),
                         NULLIF(c.display_name, ''),
                         CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                              THEN '+' || c.phone_number END,
                         NULLIF(c.wa_name, ''),
                         CASE WHEN c.phone_jid IS NOT NULL AND c.phone_jid != ''
                              THEN REPLACE(c.phone_jid, '@s.whatsapp.net', '') END,
                         CASE WHEN m.is_bot_message = 1 THEN 'Meta AI' END,
                         'Unknown')
                   END AS sender_name,
                   me.thumbnail_blob, me.mime_type,
                   COALESCE(me.media_caption, '') AS media_caption,
                   m.source_msg_id, m.source_key_id, m.forward_score,
                   m.received_timestamp, m.receipt_server_timestamp,
                   m.reply_to_key_id, m.is_view_once, m.is_ephemeral,
                   m.is_bot_message, m.broadcast,
                   c.phone_jid, c.lid_jid, c.wa_name, c.display_name,
                   me.file_path, me.file_size,
                   me.resolved_file_path, me.file_exists,
                   me.media_url, me.media_key, me.file_hash,
                   COALESCE(me.recovery_method, '') AS recovery_method,
                   COALESCE(me.media_status, '') AS media_status,
                   me.width AS media_width, me.height AS media_height,
                   me.duration_ms AS media_duration_ms,
                   COALESCE(me.media_name, '') AS media_name,
                   se.event_label, se.event_data,
                   -- [44-45] actor/target name: NULL here, filled by _fetch_auxiliary_batch
                   NULL AS actor_name, NULL AS target_name,
                   -- [46-84] auxiliary slots: NULL here, filled by _fetch_auxiliary_batch
                   NULL AS reactions_str, 0 AS reaction_count, NULL AS reactions_detail,
                   NULL AS link_details,
                   NULL AS call_duration, 0 AS call_is_video,
                   NULL AS call_result_label, 0 AS call_is_group,
                   NULL AS poll_options, 0 AS poll_total_voters,
                   NULL AS mentions_str,
                   loc.latitude, loc.longitude, loc.place_name,
                   loc.place_address, loc.is_live, loc.live_duration,
                   NULL AS first_delivered_ts, NULL AS first_read_ts,
                   COALESCE(md.device_number, -1) AS sender_device_number,
                   COALESCE(md.is_primary, -1) AS sender_is_primary,
                   COALESCE(md.platform_label, '') AS sender_platform_label,
                   COALESCE(m.origin, 0) AS origin,
                   COALESCE(m.origination_flags, 0) AS origination_flags,
                   NULL AS member_label,
                   NULL AS nc_old_phone, NULL AS nc_new_phone,
                   m.revoked_by_admin_id, NULL AS revoked_by_admin_name,
                   m.ephemeral_duration,
                   COALESCE(se.community_name, '') AS community_name,
                   c.avatar_blob AS sender_avatar_blob,
                   se.actor_id AS se_actor_id, se.target_id AS se_target_id,
                   NULL AS call_participants, NULL AS poll_voters,
                   NULL AS vcard_data,
                   m.quoted_type,
                   NULL AS scheduled_event_data,
                   m.rendered_system_text,
                   me.page_count,
                   m.view_once_state,
                   COALESCE(c.is_meta_verified, 0) AS sender_is_meta_verified,
                   COALESCE(c.is_business, 0) AS sender_is_business,
                   m.sender_jid_row_id, m.source_chat_row_id,
                   me.source_media_row_id,
                   loc.thumbnail_blob AS loc_thumbnail_blob,
                   loc.final_latitude AS loc_final_lat,
                   loc.final_longitude AS loc_final_lon,
                   loc.final_timestamp AS loc_final_ts,
                   loc.map_preview_url AS loc_map_url,
                   m.is_status_reply,
                   0 AS reply_count,  -- Filled lazily by _fetch_auxiliary_batch
                   m.last_edit_timestamp,
                   m.edit_count
            FROM message m
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN media me ON me.message_id = m.id
            LEFT JOIN system_event se ON se.message_id = m.id
            LEFT JOIN location loc ON loc.message_id = m.id
            LEFT JOIN message_device md ON md.message_id = m.id
            WHERE {where}
            ORDER BY m.timestamp ASC, m.sort_id ASC
        """

    @staticmethod
    def _fetch_auxiliary_batch(db, msgs: list[dict]) -> None:
        """Fetch auxiliary data for a batch of messages via separate indexed queries.

        Instead of 20+ correlated subqueries per row (N×20 queries), this runs
        ~10 batch queries keyed by message_id range (10 queries total).
        Mutates msgs in-place.
        """
        if not msgs:
            return
        min_id = msgs[0]["id"]
        max_id = msgs[-1]["id"]
        msg_map = {m["id"]: m for m in msgs}

        # 0. was_transferred — added in a separate fetch so we don't have
        # to shift the dozens of hard-coded row[N] indices in the main
        # SELECT.  Forensically critical: msgstore's `transferred=1`
        # tells us this specific message DID download bytes when first
        # received, so file_exists=0 NOW means "deleted afterwards" —
        # qualitatively different from "never downloaded" (transferred=0).
        # The chat renderer uses this to render a clearer "Missing (was
        # downloaded)" vs "Not downloaded" status.
        try:
            for r in db.fetchall(
                "SELECT message_id, was_transferred FROM media "
                "WHERE message_id BETWEEN ? AND ?",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["was_transferred"] = r[1]
        except Exception:
            # Older schemas may not have was_transferred — silently skip
            pass

        # 0b. HD-twin info — for SD parents, look up the HD twin's
        # resolved_file_path and file_size so the renderer can show the
        # higher-quality bytes (and an "HD" badge).  WhatsApp's dual-
        # quality send leaves the parent (SD) as the user-visible
        # message but keeps the HD content in a sibling row marked
        # is_hd_twin=1.  Without this lookup the chat would always
        # render the lower-resolution SD bubble even when the HD file
        # is sitting on disk.
        try:
            # We also pull the HD twin's media_url / media_key /
            # cdn_expiry_ts / recovery_method / mime_type so the
            # chat bubble can:
            #   * Offer a "Download HD" button when the HD bytes
            #     aren't on disk but the CDN URL is still valid
            #     (separate URL/key from the SD parent — they're
            #     independent re-uploads in WhatsApp's protocol).
            #   * Distinguish "HD never received" from "HD received
            #     and on disk" from "HD downloadable from CDN".
            #   * Tell the analyst which provenance applies to the
            #     HD bytes (e.g. SD parent shows hash_linked but HD
            #     twin was a fresh download — both should be
            #     surfaced separately).
            for r in db.fetchall(
                "SELECT pme.message_id, "
                "       pme.hd_twin_msg_id, "
                "       cme.resolved_file_path, "
                "       cme.file_exists, "
                "       cme.file_size, "
                "       cme.width, cme.height, "
                "       cme.file_hash, "
                "       cme.media_url, "
                "       cme.media_key, "
                "       cme.media_status, "
                "       COALESCE(cme.recovery_method, ''), "
                "       cme.cdn_expiry_ts, "
                "       cme.mime_type "
                "  FROM media pme "
                "  JOIN media cme ON cme.message_id = pme.hd_twin_msg_id "
                " WHERE pme.message_id BETWEEN ? AND ? "
                "   AND pme.hd_twin_msg_id IS NOT NULL",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["hd_twin_msg_id"] = r[1]
                    m["hd_twin_path"] = r[2]
                    m["hd_twin_exists"] = bool(r[3])
                    m["hd_twin_size"] = r[4] or 0
                    m["hd_twin_width"] = r[5] or 0
                    m["hd_twin_height"] = r[6] or 0
                    m["hd_twin_hash"] = r[7] or ""
                    m["hd_twin_url"] = r[8] or ""
                    m["hd_twin_key"] = r[9]
                    m["hd_twin_status"] = r[10] or ""
                    m["hd_twin_recovery"] = r[11] or ""
                    m["hd_twin_cdn_expiry"] = r[12] or 0
                    m["hd_twin_mime"] = r[13] or ""
                    # Mark the SD parent for renderer logic (so
                    # the bubble's pill reads "SD pair · HD #N").
                    m["is_hd_pair_role"] = "sd"
                    m["hd_pair_twin_id"] = r[1]

            # Mirror lookup: for HD twin rows in the batch, populate
            # their SD parent's identity + on-disk info so the HD
            # bubble can label itself "HD pair · SD #M" and offer
            # a click-to-jump back to the SD parent.  Filtered by
            # ``assoc_kind = 'hd'`` so types 4/6/11 don't get
            # picked up.  The JOIN uses ``idx_media_message_id``
            # for ``pme.message_id``; ``assoc_kind = 'hd'`` is
            # cheap with ``idx_media_assoc_kind``.  No correlated
            # subqueries — runs as a single indexed lookup.
            for r in db.fetchall(
                "SELECT cme.message_id, "
                "       cme.assoc_parent_msg_id, "
                "       pme.resolved_file_path, "
                "       pme.file_exists, "
                "       pme.file_size, "
                "       pme.width, pme.height, "
                "       pme.file_hash, "
                "       COALESCE(pme.recovery_method, '') "
                "  FROM media cme "
                "  JOIN media pme ON pme.message_id = cme.assoc_parent_msg_id "
                " WHERE cme.message_id BETWEEN ? AND ? "
                "   AND cme.assoc_kind = 'hd'",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["is_hd_pair_role"] = "hd"
                    m["hd_pair_twin_id"] = r[1]
                    m["sd_parent_path"] = r[2] or ""
                    m["sd_parent_exists"] = bool(r[3])
                    m["sd_parent_size"] = r[4] or 0
                    m["sd_parent_width"] = r[5] or 0
                    m["sd_parent_height"] = r[6] or 0
                    m["sd_parent_hash"] = r[7] or ""
                    m["sd_parent_recovery"] = r[8] or ""
        except Exception:
            # Older schemas without is_hd_twin / hd_twin_msg_id columns
            pass

        # 0c. Motion-photo (type-11) twin: parent = still image, child
        # = 1-2s video clip.  Look up the motion clip's path so the
        # renderer can show a "▶ Live" badge that plays the clip on
        # click.  Same JOIN-self pattern as HD twins.
        try:
            for r in db.fetchall(
                "SELECT pme.message_id, "
                "       pme.motion_video_msg_id, "
                "       cme.resolved_file_path, "
                "       cme.file_exists, "
                "       cme.duration_ms "
                "  FROM media pme "
                "  JOIN media cme ON cme.message_id = pme.motion_video_msg_id "
                " WHERE pme.message_id BETWEEN ? AND ? "
                "   AND pme.motion_video_msg_id IS NOT NULL",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["motion_video_msg_id"] = r[1]
                    m["motion_video_path"] = r[2]
                    m["motion_video_exists"] = bool(r[3])
                    m["motion_video_duration_ms"] = r[4] or 0
        except Exception:
            # Older schemas without motion_video_msg_id column
            pass

        # 1. Reactions
        try:
            for r in db.fetchall(
                "SELECT r.message_id, GROUP_CONCAT(r.emoji, ''), COUNT(*),"
                " GROUP_CONCAT(r.emoji || ':' || "
                "   COALESCE("
                "     NULLIF(rc.resolved_name, ''),"
                "     NULLIF(rc.display_name, ''),"
                "     CASE WHEN rc.phone_number IS NOT NULL AND rc.phone_number != ''"
                "          THEN '+' || rc.phone_number END,"
                "     NULLIF(rc.wa_name, ''),"
                "     CASE WHEN r.from_me = 1 THEN 'You' END,"
                "     NULLIF(rc.business_name, ''),"
                "     CASE WHEN rc.phone_jid LIKE '1313555%' THEN 'Meta AI' END,"
                "     'Unknown')"
                " || ':' || COALESCE(rc.phone_number, '')"
                " || ':' || COALESCE(r.timestamp, 0), ';;')"
                " FROM reaction r LEFT JOIN contact rc ON rc.id = r.reactor_id"
                " WHERE r.message_id BETWEEN ? AND ? GROUP BY r.message_id",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["reactions_str"] = r[1]
                    m["reaction_count"] = r[2]
                    m["reactions_detail"] = r[3]
        except Exception:
            pass

        # 2. Links
        try:
            for r in db.fetchall(
                "SELECT ld.message_id,"
                " GROUP_CONCAT(ld.page_title || '||' || ld.url || '||' || COALESCE(ld.description,'') || '||' || COALESCE(ld.domain,''), ';;')"
                " FROM message_link_detail ld"
                " WHERE ld.message_id BETWEEN ? AND ? GROUP BY ld.message_id",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["link_details"] = r[1]
        except Exception:
            pass

        # 2b. Link preview thumbnails (only for the first URL in
        # each msg — WhatsApp only previews that one).  The
        # ``thumbnail_blob`` column was added in a later schema
        # revision; the try/except keeps older analysis DBs
        # working with no thumbnails (the original behaviour).
        try:
            for r in db.fetchall(
                "SELECT ld.message_id, ld.thumbnail_blob "
                " FROM message_link_detail ld "
                " WHERE ld.message_id BETWEEN ? AND ? AND ld.link_index = 0 "
                "   AND ld.thumbnail_blob IS NOT NULL",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["link_thumb_blob"] = r[1]
        except Exception:
            pass  # column not in old schema

        # 3. Mentions
        try:
            for r in db.fetchall(
                "SELECT mn.message_id, GROUP_CONCAT("
                " COALESCE("
                "   NULLIF(mc.resolved_name, ''),"
                "   NULLIF(mc.display_name, ''),"
                "   CASE WHEN mc.phone_number IS NOT NULL AND mc.phone_number != ''"
                "        THEN '+' || mc.phone_number END,"
                "   NULLIF(mc.wa_name, ''),"
                "   mn.display_name,"
                "   'Unknown')"
                " || '::' || CAST(COALESCE(mn.mentioned_id, 0) AS TEXT)"
                " || '::' || COALESCE(mc.phone_number, '')"
                " || '::' || COALESCE(REPLACE(mc.lid_jid, '@lid', ''), '')"
                " || '::' || COALESCE(mn.display_name, ''), ';;')"
                " FROM mention mn LEFT JOIN contact mc ON mc.id = mn.mentioned_id"
                " WHERE mn.message_id BETWEEN ? AND ? GROUP BY mn.message_id",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["mentions_str"] = r[1]
        except Exception:
            pass

        # 4. Polls (options + voter count + per-option voter names)
        try:
            for r in db.fetchall(
                "SELECT p.message_id,"
                " GROUP_CONCAT(po.option_name || '::' || COALESCE(po.vote_total, 0)"
                "   || '::' || COALESCE(po.voter_names, ''), CHAR(10)),"
                " (SELECT COUNT(DISTINCT pv.voter_id) FROM poll_vote pv WHERE pv.poll_id = p.id)"
                " FROM poll p JOIN poll_option po ON po.poll_id = p.id"
                " WHERE p.message_id BETWEEN ? AND ?"
                " GROUP BY p.message_id ORDER BY po.option_index",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["poll_options"] = r[1]
                    m["poll_total_voters"] = r[2] or 0
        except Exception as e:
            print(f"[ChatViewer] Poll query error: {e}")

        # 4b. Channel-poll image options (msgstore.message_association
        # association_type=6).  Each poll option may have an attached
        # image stored as a separate `message` row whose text_data
        # matches the option_name.  We marked those rows is_hd_twin=1
        # and back-pointer assoc_parent_msg_id = poll_msg_id during
        # ingestion — now we look them up so the poll bubble can
        # render each option WITH its image inline.
        try:
            from collections import defaultdict
            opt_by_poll: dict[int, list[dict]] = defaultdict(list)
            for r in db.fetchall(
                "SELECT me.assoc_parent_msg_id, "
                "       m.text_content, "
                "       me.resolved_file_path, "
                "       me.thumbnail_blob, "
                "       me.file_exists, "
                "       me.width, me.height "
                "  FROM media me "
                "  JOIN message m ON m.id = me.message_id "
                " WHERE me.assoc_parent_msg_id BETWEEN ? AND ? "
                "   AND me.is_hd_twin = 1 "
                "   AND m.message_type = 1",   # only image children
                (min_id, max_id),
            ):
                poll_msg_id = r[0]
                opt_name = (r[1] or "").strip()
                if not opt_name:
                    continue
                # Embed thumbnail BLOB as base64 data URL so the
                # rendered HTML works without disk-file dependency
                # (channel-poll image options usually arrive as
                # CDN thumbnails — file_path is often NULL).
                thumb_b64 = ""
                if r[3]:
                    try:
                        import base64
                        thumb_b64 = base64.b64encode(r[3]).decode("ascii")
                    except Exception:
                        pass
                opt_by_poll[poll_msg_id].append({
                    "name":   opt_name,
                    "path":   r[2] or "",
                    "thumb":  thumb_b64,
                    "exists": bool(r[4]),
                    "width":  r[5] or 0,
                    "height": r[6] or 0,
                })
            for poll_msg_id, opts in opt_by_poll.items():
                m = msg_map.get(poll_msg_id)
                if m:
                    m["poll_option_images"] = opts
        except Exception as e:
            # Older schema without assoc_parent_msg_id — silently skip
            print(f"[ChatViewer] Poll image-options lookup skipped: {e}")

        # 5. Calls (by timestamp proximity)
        try:
            call_msgs = [m for m in msgs if m.get("type_label") == "call_log" or m.get("message_type") in (10, 16, 90)]
            # Resolve the conversation we're viewing once per batch.
            # _fetch_auxiliary_batch is a @staticmethod (no self), so
            # we look up the conversation_id from the first message
            # row.  All msgs in a batch share the same conversation,
            # so a single probe is enough.
            _cur_conv = None
            if call_msgs:
                _r = db.fetchone(
                    "SELECT conversation_id FROM message WHERE id = ?",
                    (call_msgs[0]["id"],),
                )
                _cur_conv = _r[0] if _r else None
            for cm in call_msgs:
                ts = cm.get("timestamp")
                if ts is None:
                    continue
                # Core call data — kept as the original SELECT shape
                # so older case schemas (pre-call_origin) still work.
                # Creator name follows the same WhatsApp UI rule as
                # the participant list: prefix with "~" when the
                # contact is unsaved (only their WhatsApp profile name
                # is known, not a name from the device address book).
                cr = db.fetchone(
                    "SELECT cr.duration_sec, cr.is_video, cr.result_label, cr.is_group_call,"
                    " cr.call_category,"
                    " COALESCE("
                    "   CASE WHEN cc.resolved_name IS NOT NULL AND cc.resolved_name != ''"
                    "             AND cc.resolved_name != 'Unknown'"
                    "        THEN ("
                    "             CASE WHEN COALESCE(cc.is_saved, 0) = 0"
                    "                       AND cc.resolved_name NOT LIKE '~%'"
                    "                  THEN '~' ELSE '' END"
                    "        )"
                    "        || cc.resolved_name"
                    "   END,"
                    "   CASE WHEN cc.wa_name IS NOT NULL AND cc.wa_name != ''"
                    "        THEN '~' || cc.wa_name END,"
                    "   cc.phone_number,"
                    "   ''"
                    " ) AS creator_name"
                    " FROM call_record cr"
                    " LEFT JOIN contact cc ON cc.id = cr.creator_contact_id"
                    " WHERE ABS(cr.timestamp - ?) < 2000 LIMIT 1",
                    (ts,),
                )
                if cr:
                    cm["call_duration"] = cr[0]
                    cm["call_is_video"] = bool(cr[1])
                    cm["call_result_label"] = cr[2]
                    cm["call_is_group"] = bool(cr[3])
                    cm["call_category"] = cr[4] or "personal"
                    cm["call_creator_name"] = cr[5] or ""
                    # Origin lookup — surfaces a "from <Group>" pill +
                    # Go-to-original button on per-participant synthetic
                    # call echoes.  Two viable signals (try both):
                    #
                    #   (1) original (non-synthetic) message in another
                    #       conversation that shares this call's
                    #       source_key_id (with the "::p<cid>" suffix
                    #       stripped if present);
                    #   (2) call_record's own home conversation
                    #       (group_conversation_id or conversation_id),
                    #       used as a fallback when the original
                    #       message itself wasn't ingested.
                    #
                    # Important: this method is a @staticmethod, so
                    # there is no `self` — derive the message's own
                    # conversation_id directly from the message dict
                    # (it equals the chat being viewed because the
                    # core SELECT filters by conv_id = current).
                    try:
                        cur_conv = _cur_conv
                        src_key = (cm.get("source_key_id") or "").strip()
                        if "::p" in src_key:
                            base_key = src_key.split("::p", 1)[0]
                        else:
                            base_key = src_key

                        # Pull call_record's home conversation context
                        # — set at ingest time directly from msgstore's
                        # chat_row_id linkage.
                        _cr_ctx = db.fetchone(
                            "SELECT cr.group_conversation_id, cr.conversation_id,"
                            " cr.call_id_text"
                            " FROM call_record cr"
                            " WHERE ABS(cr.timestamp - ?) < 2000 LIMIT 1",
                            (ts,),
                        )

                        _origin_conv_id = None
                        _origin_msg_id = 0

                        if _cr_ctx:
                            _cr_home = _cr_ctx[0] or _cr_ctx[1]
                            _cr_call_text = (_cr_ctx[2] or "").replace("call:", "").strip()
                            _lookup_key = base_key or _cr_call_text

                            if _cr_home and (cur_conv is None or _cr_home != cur_conv):
                                _origin_conv_id = _cr_home

                            # Find the canonical original message in
                            # another chat that shares this call's key.
                            # Prefer rows with a positive source_msg_id
                            # (real msgstore row) over synthetic siblings.
                            if _lookup_key and cur_conv is not None:
                                _orig = db.fetchone(
                                    "SELECT m.id, m.conversation_id,"
                                    " conv.display_name, conv.chat_type"
                                    " FROM message m"
                                    " LEFT JOIN conversation conv ON conv.id = m.conversation_id"
                                    " WHERE m.source_key_id = ?"
                                    "   AND m.message_type = 90"
                                    "   AND m.conversation_id != ?"
                                    " ORDER BY"
                                    "   CASE WHEN COALESCE(m.source_msg_id, -1) > 0 THEN 0 ELSE 1 END,"
                                    "   m.id"
                                    " LIMIT 1",
                                    (_lookup_key, cur_conv),
                                )
                                if _orig:
                                    _origin_conv_id = _orig[1]
                                    _origin_msg_id = _orig[0]
                                    cm["call_origin_msg_id"] = _orig[0]
                                    cm["call_origin_conv_id"] = _orig[1]
                                    cm["call_origin_conv_name"] = _orig[2] or ""
                                    cm["call_origin_chat_type"] = _orig[3] or ""

                            # Pill-only case: origin identified via
                            # call_record but no concrete message found.
                            if (_origin_conv_id and _origin_msg_id == 0
                                    and not cm.get("call_origin_conv_id")):
                                _origin = db.fetchone(
                                    "SELECT display_name, chat_type"
                                    " FROM conversation WHERE id = ?",
                                    (_origin_conv_id,),
                                )
                                if _origin:
                                    cm["call_origin_conv_id"] = _origin_conv_id
                                    cm["call_origin_conv_name"] = _origin[0] or ""
                                    cm["call_origin_chat_type"] = _origin[1] or ""
                    except Exception as e:
                        print(f"[ChatViewer] call origin lookup failed: {e}")
                # Call participants — include a leading "~" on
                # unsaved contacts whose name comes from their
                # WhatsApp profile (not from the device address
                # book), matching WhatsApp's own UI convention.
                # Guards: don't double-tilde a name that already
                # starts with "~" (some resolvers pre-prefix); skip
                # the tilde for the device owner since "~" implies
                # an unverified WA name.
                cp = db.fetchall(
                    "SELECT GROUP_CONCAT("
                    " COALESCE("
                    "   CASE WHEN cpc.resolved_name IS NOT NULL AND cpc.resolved_name != ''"
                    "             AND cpc.resolved_name != 'Unknown'"
                    "        THEN ("
                    "             CASE WHEN COALESCE(cpc.is_saved, 0) = 0"
                    "                       AND cpc.resolved_name NOT LIKE '~%'"
                    "                  THEN '~' ELSE '' END"
                    "        )"
                    "        || cpc.resolved_name"
                    "        || CASE WHEN cpc.phone_number IS NOT NULL AND cpc.phone_number != ''"
                    "                    AND INSTR(cpc.resolved_name, cpc.phone_number) = 0"
                    "               THEN ' (+' || cpc.phone_number || ')'"
                    "               ELSE '' END"
                    "   END,"
                    "   CASE WHEN cpc.phone_number IS NOT NULL AND cpc.phone_number != ''"
                    "        THEN '+' || cpc.phone_number END,"
                    "   'Unknown')"
                    " || '|' || COALESCE(cap.call_result, -1), ', ')"
                    " FROM call_participant cap"
                    " INNER JOIN call_record cr2 ON cr2.id = cap.call_id"
                    " LEFT JOIN contact cpc ON cpc.id = cap.contact_id"
                    " WHERE ABS(cr2.timestamp - ?) < 2000",
                    (ts,),
                )
                if cp and cp[0] and cp[0][0]:
                    cm["call_participants"] = cp[0][0]
        except Exception:
            pass

        # 6. vCards
        try:
            for r in db.fetchall(
                "SELECT vcd.message_id, GROUP_CONCAT("
                " vcd.display_name || '||' || COALESCE(vcd.phone_numbers, ''), ';;')"
                " FROM message_vcard_data vcd"
                " WHERE vcd.message_id BETWEEN ? AND ?"
                " GROUP BY vcd.message_id ORDER BY vcd.vcard_index",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["vcard_data"] = r[1]
        except Exception:
            pass

        # 7. Receipts (delivered/read)
        try:
            for r in db.fetchall(
                "SELECT rr.message_id, MIN(rr.delivered_ts), MIN(rr.read_ts)"
                " FROM receipt rr"
                " WHERE rr.message_id BETWEEN ? AND ?"
                " GROUP BY rr.message_id",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["first_delivered_ts"] = r[1]
                    m["first_read_ts"] = r[2]
        except Exception:
            pass

        # 8. Scheduled events
        try:
            for r in db.fetchall(
                "SELECT sev.message_id,"
                " sev.name || '||' || COALESCE(sev.description,'') || '||' || COALESCE(sev.location_name,'')"
                " || '||' || COALESCE(sev.join_link,'') || '||' || COALESCE(sev.start_time,'')"
                " || '||' || COALESCE(sev.is_canceled,0)"
                " || '||' || COALESCE(sev.end_time,'') || '||' || COALESCE(sev.is_schedule_call,0)"
                " FROM scheduled_event sev"
                " WHERE sev.message_id BETWEEN ? AND ?",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["scheduled_event_data"] = r[1]
        except Exception:
            pass

        # 9. Group member labels
        try:
            conv_id = msgs[0].get("conversation_id") if msgs else None
            if conv_id is None:
                # Try to get from DB
                r = db.fetchone(
                    "SELECT conversation_id FROM message WHERE id = ?", (min_id,)
                )
                conv_id = r[0] if r else None
            if conv_id:
                for r in db.fetchall(
                    "SELECT gm.contact_id, gm.label FROM group_member gm"
                    " WHERE gm.conversation_id = ?",
                    (conv_id,),
                ):
                    for m in msgs:
                        if m.get("sender_id") == r[0]:
                            m["member_label"] = r[1]
        except Exception:
            pass

        # 10. Poll voters
        try:
            for r in db.fetchall(
                "SELECT p.message_id, GROUP_CONCAT("
                " COALESCE("
                "   NULLIF(pvc.resolved_name, ''),"
                "   NULLIF(pvc.display_name, ''),"
                "   CASE WHEN pvc.phone_number IS NOT NULL AND pvc.phone_number != ''"
                "        THEN '+' || pvc.phone_number END,"
                "   NULLIF(pvc.wa_name, ''),"
                "   'Unknown')"
                " || CASE WHEN pvc.phone_number IS NOT NULL AND pvc.phone_number != ''"
                "         AND (pvc.resolved_name IS NOT NULL AND pvc.resolved_name != ''"
                "              OR pvc.display_name IS NOT NULL AND pvc.display_name != ''"
                "              OR pvc.wa_name IS NOT NULL AND pvc.wa_name != '')"
                "         THEN ' (+' || pvc.phone_number || ')' ELSE '' END, ', ')"
                " FROM poll_vote pv2"
                " INNER JOIN poll p ON p.id = pv2.poll_id"
                " LEFT JOIN contact pvc ON pvc.id = pv2.voter_id"
                " WHERE p.message_id BETWEEN ? AND ?"
                " GROUP BY p.message_id",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    m["poll_voters"] = r[1]
        except Exception:
            pass

        # 11. Live location final coords (enrichment from new schema columns)
        try:
            live_msgs = [m for m in msgs if m.get("loc_is_live")]
            if live_msgs:
                live_ids = [m["id"] for m in live_msgs]
                placeholders = ",".join("?" * len(live_ids))
                for r in db.fetchall(
                    f"SELECT message_id, final_latitude, final_longitude, final_timestamp"
                    f" FROM location WHERE message_id IN ({placeholders})"
                    f" AND final_latitude IS NOT NULL",
                    live_ids,
                ):
                    m = msg_map.get(r[0])
                    if m:
                        m["loc_final_lat"] = r[1]
                        m["loc_final_lon"] = r[2]
                        m["loc_final_ts"] = r[3]
        except Exception:
            pass  # Columns may not exist yet (pre-re-ingestion)

        # 12. System event actor/target names + number changes
        try:
            se_msgs = [m for m in msgs if m.get("se_actor_id") or m.get("se_target_id")]
            if se_msgs:
                actor_ids = {m["se_actor_id"] for m in se_msgs if m.get("se_actor_id")}
                target_ids = {m["se_target_id"] for m in se_msgs if m.get("se_target_id")}
                all_ids = actor_ids | target_ids
                if all_ids:
                    placeholders = ",".join("?" * len(all_ids))
                    contact_names = {}
                    contact_jids = {}
                    for cr in db.fetchall(
                        f"SELECT id,"
                        f" CASE WHEN is_saved = 1 AND display_name IS NOT NULL AND display_name != ''"
                        f"           AND phone_number IS NOT NULL AND phone_number != ''"
                        f"      THEN display_name || ' (+' || phone_number || ')'"
                        f"      WHEN is_saved = 1 AND display_name IS NOT NULL AND display_name != ''"
                        f"      THEN display_name"
                        f"      WHEN wa_name IS NOT NULL AND wa_name != ''"
                        f"           AND phone_number IS NOT NULL AND phone_number != ''"
                        f"      THEN '~' || wa_name || ' (+' || phone_number || ')'"
                        f"      WHEN wa_name IS NOT NULL AND wa_name != '' THEN '~' || wa_name"
                        f"      WHEN phone_number IS NOT NULL AND phone_number != '' THEN '+' || phone_number"
                        f"      WHEN phone_jid IS NOT NULL THEN '+' || REPLACE(phone_jid, '@s.whatsapp.net', '')"
                        f"      ELSE 'Unknown' END,"
                        f" phone_jid, lid_jid"
                        f" FROM contact WHERE id IN ({placeholders})",
                        tuple(all_ids),
                    ):
                        contact_names[cr[0]] = cr[1]
                        contact_jids[cr[0]] = (cr[2] or "", cr[3] or "")
                    for m in se_msgs:
                        if m.get("se_actor_id") and m["se_actor_id"] in contact_names:
                            m["system_event_actor"] = contact_names[m["se_actor_id"]]
                            jids = contact_jids.get(m["se_actor_id"], ("", ""))
                            m["se_actor_phone_jid"] = jids[0]
                            m["se_actor_lid_jid"] = jids[1]
                        if m.get("se_target_id") and m["se_target_id"] in contact_names:
                            m["system_event_target"] = contact_names[m["se_target_id"]]
                            jids = contact_jids.get(m["se_target_id"], ("", ""))
                            m["se_target_phone_jid"] = jids[0]
                            m["se_target_lid_jid"] = jids[1]
        except Exception:
            pass

        # 12. Revoked-by admin names
        try:
            revoked = [m for m in msgs if m.get("revoked_by_admin_id")]
            if revoked:
                admin_ids = {m["revoked_by_admin_id"] for m in revoked}
                placeholders = ",".join("?" * len(admin_ids))
                admin_names = {}
                for cr in db.fetchall(
                    f"SELECT id,"
                    f" CASE WHEN is_saved = 1 AND display_name IS NOT NULL AND display_name != ''"
                    f"           AND phone_number IS NOT NULL AND phone_number != ''"
                    f"      THEN display_name || ' (+' || phone_number || ')'"
                    f"      WHEN is_saved = 1 AND display_name IS NOT NULL AND display_name != ''"
                    f"      THEN display_name"
                    f"      WHEN wa_name IS NOT NULL AND wa_name != ''"
                    f"           AND phone_number IS NOT NULL AND phone_number != ''"
                    f"      THEN '~' || wa_name || ' (+' || phone_number || ')'"
                    f"      WHEN wa_name IS NOT NULL AND wa_name != '' THEN '~' || wa_name"
                    f"      WHEN phone_number IS NOT NULL AND phone_number != '' THEN '+' || phone_number"
                    f"      WHEN phone_jid IS NOT NULL THEN '+' || REPLACE(phone_jid, '@s.whatsapp.net', '')"
                    f"      ELSE 'Unknown' END"
                    f" FROM contact WHERE id IN ({placeholders})",
                    tuple(admin_ids),
                ):
                    admin_names[cr[0]] = cr[1]
                for m in revoked:
                    aid = m.get("revoked_by_admin_id")
                    if aid and aid in admin_names:
                        m["revoked_by_admin_name"] = admin_names[aid]
        except Exception:
            pass

        # 13. Number changes — resolve old/new JID row IDs to phone numbers
        try:
            for r in db.fetchall(
                "SELECT se.message_id,"
                "  COALESCE(j_old.jid_raw_string, '') AS old_jid,"
                "  COALESCE(j_new.jid_raw_string, '') AS new_jid,"
                "  COALESCE(c_old.resolved_name, c_old.wa_name, '') AS old_name,"
                "  COALESCE(c_new.resolved_name, c_new.wa_name, '') AS new_name"
                " FROM number_change nc"
                " JOIN system_event se ON se.id = nc.system_event_id"
                " LEFT JOIN jid_to_contact j_old ON j_old.jid_row_id = nc.old_jid_row_id"
                " LEFT JOIN jid_to_contact j_new ON j_new.jid_row_id = nc.new_jid_row_id"
                " LEFT JOIN contact c_old ON c_old.id = nc.old_contact_id"
                " LEFT JOIN contact c_new ON c_new.id = nc.new_contact_id"
                " WHERE se.message_id BETWEEN ? AND ?",
                (min_id, max_id),
            ):
                m = msg_map.get(r[0])
                if m:
                    old_jid = r[1].replace("@s.whatsapp.net", "") if r[1] else ""
                    new_jid = r[2].replace("@s.whatsapp.net", "") if r[2] else ""
                    old_name = r[3] or ""
                    new_name = r[4] or ""
                    m["nc_old_phone"] = f"+{old_jid}" if old_jid else ""
                    m["nc_new_phone"] = f"+{new_jid}" if new_jid else ""
                    m["nc_old_name"] = old_name
                    m["nc_new_name"] = new_name
        except Exception:
            pass

        # 14. Quoted sender names (resolve reply_to_key_id → sender display name)
        try:
            reply_keys = [m["reply_to_key_id"] for m in msgs
                          if m.get("reply_to_key_id")]
            if reply_keys:
                unique_keys = list(set(reply_keys))
                placeholders = ",".join("?" * len(unique_keys))
                key_to_sender = {}
                _qo_name = getattr(self, '_owner_name', '') or ''
                _qo_phone = getattr(self, '_owner_phone', '') or ''
                _you_label = f"{_qo_name} (+{_qo_phone})" if _qo_name and _qo_phone else _qo_name or 'You'
                _you_sql = _you_label.replace("'", "''")
                for pr in db.fetchall(
                    f"SELECT m.source_key_id,"
                    f" CASE WHEN m.from_me = 1 THEN '{_you_sql}'"
                    f"      ELSE COALESCE("
                    f"        NULLIF(c.resolved_name, ''),"
                    f"        NULLIF(c.display_name, ''),"
                    f"        CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                    f"             THEN '+' || c.phone_number END,"
                    f"        NULLIF(c.wa_name, ''),"
                    f"        'Unknown') END"
                    f" FROM message m LEFT JOIN contact c ON c.id = m.sender_id"
                    f" WHERE m.source_key_id IN ({placeholders})",
                    tuple(unique_keys),
                ):
                    key_to_sender[pr[0]] = pr[1]
                for m in msgs:
                    rk = m.get("reply_to_key_id")
                    if rk and rk in key_to_sender:
                        m["quoted_sender"] = key_to_sender[rk]
        except Exception:
            pass

        # 14a-ii. Cross-chat quote detection (quoted msg is in another conversation)
        try:
            reply_keys_cc = [m["reply_to_key_id"] for m in msgs
                             if m.get("reply_to_key_id")]
            if reply_keys_cc and self._conv_id is not None:
                unique_keys_cc = list(set(reply_keys_cc))
                ph_cc = ",".join("?" * len(unique_keys_cc))
                key_to_cross: dict[str, tuple[int, int, str]] = {}
                for cr in db.fetchall(
                    f"SELECT m.source_key_id, m.id, m.conversation_id,"
                    f" COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name"
                    f" FROM message m"
                    f" LEFT JOIN conversation conv ON conv.id = m.conversation_id"
                    f" WHERE m.source_key_id IN ({ph_cc})"
                    f" AND m.conversation_id != ?",
                    tuple(unique_keys_cc) + (self._conv_id,),
                ):
                    key_to_cross[cr[0]] = (cr[2], cr[1], cr[3])  # conv_id, msg_id, conv_name
                for m in msgs:
                    rk = m.get("reply_to_key_id")
                    if rk and rk in key_to_cross:
                        cc_conv_id, cc_msg_id, cc_conv_name = key_to_cross[rk]
                        m["quoted_cross_chat_conv_id"] = cc_conv_id
                        m["quoted_cross_chat_msg_id"] = cc_msg_id
                        m["quoted_cross_chat_conv_name"] = cc_conv_name
        except Exception:
            pass

        # 14b. Quoted message thumbnails (for media reply preview)
        try:
            reply_keys2 = [m["reply_to_key_id"] for m in msgs
                           if m.get("reply_to_key_id") and m.get("quoted_type") in (1, 2, 3, 9, 13, 20, 42, 43)]
            if reply_keys2:
                unique_keys2 = list(set(reply_keys2))
                ph2 = ",".join("?" * len(unique_keys2))
                key_to_thumb: dict[str, bytes] = {}
                for tr in db.fetchall(
                    f"SELECT m.source_key_id, me.thumbnail_blob"
                    f" FROM message m JOIN media me ON me.message_id = m.id"
                    f" WHERE m.source_key_id IN ({ph2})"
                    f" AND me.thumbnail_blob IS NOT NULL AND LENGTH(me.thumbnail_blob) > 50",
                    tuple(unique_keys2),
                ):
                    key_to_thumb[tr[0]] = tr[1]
                for m in msgs:
                    rk = m.get("reply_to_key_id")
                    if rk and rk in key_to_thumb:
                        m["quoted_thumb_blob"] = key_to_thumb[rk]
        except Exception:
            pass

        # 15. Comment thread counts (channel reply threads)
        try:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_comment'"
            ).fetchone()
            if exists:
                for r in db.fetchall(
                    "SELECT parent_message_id, COUNT(*) FROM message_comment"
                    " WHERE parent_message_id BETWEEN ? AND ?"
                    " GROUP BY parent_message_id",
                    (min_id, max_id),
                ):
                    m = msg_map.get(r[0])
                    if m:
                        m["comment_count"] = r[1]
        except Exception:
            pass

        # 16. Reply counts — batch lookup instead of correlated subquery per row
        # Also check original_key_id from edit_history for edited messages,
        # because replies to pre-edit versions point to the original key.
        try:
            key_map = {}  # source_key_id → msg dict
            for m in msgs:
                sk = m.get("source_key_id")
                if sk:
                    key_map[sk] = m
            if key_map:
                # Collect original (pre-edit) keys too
                msg_ids = [m["id"] for m in msgs if m.get("is_edited")]
                orig_key_map = {}  # original_key_id → msg dict
                if msg_ids:
                    ph = ",".join("?" * len(msg_ids))
                    for r in db.fetchall(
                        f"SELECT message_id, original_key_id FROM edit_history"
                        f" WHERE message_id IN ({ph}) AND original_key_id IS NOT NULL",
                        msg_ids,
                    ):
                        mid, orig_key = r[0], r[1]
                        for m in msgs:
                            if m["id"] == mid:
                                orig_key_map[orig_key] = m
                                break

                # Search for replies to both current AND original keys
                all_keys = list(key_map.keys()) + list(orig_key_map.keys())
                if all_keys:
                    placeholders = ",".join("?" * len(all_keys))
                    for r in db.fetchall(
                        f"SELECT reply_to_key_id, COUNT(*) FROM message"
                        f" WHERE reply_to_key_id IN ({placeholders})"
                        f" GROUP BY reply_to_key_id",
                        all_keys,
                    ):
                        m = key_map.get(r[0]) or orig_key_map.get(r[0])
                        if m:
                            m["reply_count"] = (m.get("reply_count") or 0) + r[1]
        except Exception:
            pass

    def _fetch_batch(self, where: str | None = None,
                     params: list | None = None,
                     limit: int | None = None) -> None:
        if self._conv_id is None:
            return
        if where is None:
            where, params = self._where_clause()
        if params is None:
            params = []
        batch_limit = limit if limit is not None else BATCH_SIZE
        offset = self._loaded_start + len(self._raw_data)

        # Optimization: for the LAST tile (initial load), use
        # DESC + reverse to avoid the expensive O(n) OFFSET scan
        # on large conversations.  An OFFSET near the end of a
        # large message table is dominated by the row scan; a
        # ``DESC LIMIT N`` with the index reads only the last
        # rows directly.
        remaining = self._total - offset
        if remaining <= batch_limit and offset > 1000:
            # Fetch last N messages using DESC (index-friendly, no OFFSET)
            desc_where = where.replace(
                "ORDER BY m.timestamp ASC, m.sort_id ASC",
                "ORDER BY m.timestamp DESC, m.sort_id DESC"
            ) if "ORDER BY" in where else where
            sql = self._base_sql(where)
            # Replace the ORDER BY in _base_sql output
            sql = sql.replace(
                "ORDER BY m.timestamp ASC, m.sort_id ASC",
                "ORDER BY m.timestamp DESC, m.sort_id DESC"
            )
            sql += " LIMIT ?"
            rows = self._db.fetchall(sql, tuple(params) + (batch_limit,))
            rows = list(reversed(rows))  # Reverse back to ASC order
        else:
            sql = self._base_sql(where) + " LIMIT ? OFFSET ?"
            rows = self._db.fetchall(sql, tuple(params) + (batch_limit, offset))

        last_date_str = ""
        if self._data:
            for item in reversed(self._data):
                if item.get("message_type") != -1 and item.get("timestamp"):
                    try:
                        last_date_str = format_timestamp(item["timestamp"], "date")
                    except (ValueError, OSError):
                        pass
                    break

        new_items = []
        for row in rows:
            msg = self._build_msg_dict(row)

            # Fix duplicate quoted messages: if quoted_text == display_text, clear quote
            if (msg["quoted_text"] and msg["display_text"]
                    and msg["quoted_text"].strip() == msg["display_text"].strip()):
                msg["quoted_text"] = None

            # Date separator injection
            ts = msg.get("timestamp")
            if ts and msg.get("message_type") != 7:
                try:
                    msg_date_str = format_timestamp(ts, "date")
                    if msg_date_str != last_date_str:
                        last_date_str = msg_date_str
                        date_label = format_timestamp(ts, '%B %d, %Y')
                        sep = {
                            "id": -1,
                            "message_type": -1,
                            "display_text": date_label,
                            "timestamp": ts,
                        }
                        new_items.append(sep)
                except (ValueError, OSError):
                    pass

            new_items.append(msg)
            self._raw_data.append(msg)

        # Fetch auxiliary data (reactions, polls, mentions, etc.) via batch queries
        raw_msgs_in_batch = [m for m in new_items if m.get("message_type", -1) != -1]
        if raw_msgs_in_batch:
            self._fetch_auxiliary_batch(self._db, raw_msgs_in_batch)

        # Link album containers to their child images
        self._link_album_children(new_items)

        # Add items and build key_id + id index
        for item in new_items:
            row_idx = len(self._data)
            self._data.append(item)
            key_id = item.get("source_key_id")
            if key_id:
                self._key_id_to_row[key_id] = row_idx
            msg_id = item.get("id")
            if msg_id and msg_id > 0:
                self._id_to_row[msg_id] = row_idx

    def _link_album_children(self, items: list):
        """Link album container messages to their child images.

        Source of truth (preferred): the message_association table
        populated by album_ingester.  Authoritatively reflects msgstore
        message_album / message_association semantics including videos
        mixed with photos and out-of-window children.

        Fallback (when the table is missing on older analysis.dbs): a
        timestamp-proximity heuristic - an album (type_label='album') is
        followed by consecutive image/video messages from the same sender
        within ~10 seconds.

        Either way, child rows are marked with ``album_parent_id`` so the
        delegate can render them with a subtle 'part of album' indicator
        (or skip them when the parent grid renders them as cells).
        """
        # Use the same SQL-then-heuristic logic as the worker version,
        # but read from self._db so we get the analysis.db connection.
        _PrefetchWorker._link_album_children_static(items, self._db)

    def _get_tagged_ids(self) -> set:
        """Load tagged message IDs from the message_tag table (lazy, cached)."""
        if not self._tagged_ids_loaded:
            try:
                rows = self._db.fetchall(
                    "SELECT message_id FROM message_tag"
                )
                self._tagged_ids = {r[0] for r in rows}
            except Exception:
                self._tagged_ids = set()
            self._tagged_ids_loaded = True
        return self._tagged_ids

    def _build_msg_dict(self, row) -> dict:
        msg = {
            "id": row[0],
            "from_me": bool(row[1]),
            "text_content": row[2],
            "message_type": row[3],
            "type_label": row[4],
            "timestamp": row[5],
            "status": row[6] or 0,
            "is_starred": bool(row[7]),
            "is_forwarded": bool(row[8]),
            "is_edited": bool(row[9]),
            "is_revoked": bool(row[10]),
            "quoted_text": row[11],
            "sender_id": row[12],
            "sender_name": row[13],
            "thumbnail_blob": row[14],
            "has_thumb": row[14] is not None and len(row[14]) > 20 if row[14] else False,
            "mime_type": row[15],
            "media_caption": row[16],
            # Extended debug fields
            "source_msg_id": row[17],
            "source_key_id": row[18],
            "forward_score": row[19],
            "received_timestamp": row[20],
            "receipt_server_timestamp": row[21],
            "reply_to_key_id": row[22],
            "is_view_once": bool(row[23]) if row[23] else False,
            "is_ephemeral": bool(row[24]) if row[24] else False,
            "is_bot_message": bool(row[25]) if row[25] else False,
            "broadcast": bool(row[26]) if row[26] else False,
            # Contact details
            "phone_jid": row[27],
            "lid_jid": row[28],
            "wa_name": row[29],
            "display_name": row[30],
            # Media details
            "file_path": row[31],
            "file_size": row[32],
            "resolved_file_path": row[33],
            "media_file_exists": bool(row[34]) if row[34] else False,
            "media_url": row[35],
            "media_key": row[36],
            "file_hash": row[37],
            "recovery_method": row[38] if len(row) > 38 else "",
            "media_status": row[39] if len(row) > 39 else "",
            # Media dimensions
            "media_width": row[40] if len(row) > 40 else None,
            "media_height": row[41] if len(row) > 41 else None,
            "media_duration_ms": row[42] if len(row) > 42 else None,
            "media_name": row[43] if len(row) > 43 else "",
            # System event details
            "system_event_label": row[44] if len(row) > 44 else None,
            "system_event_data": row[45] if len(row) > 45 else None,
            "system_event_actor": row[46] if len(row) > 46 else None,
            "system_event_target": row[47] if len(row) > 47 else None,
            # Reactions
            "reactions_str": row[48] if len(row) > 48 else None,
            "reaction_count": row[49] if len(row) > 49 else 0,
            "reactions_detail": row[50] if len(row) > 50 else None,
            # Link details (title||url||desc||domain;;...)
            "link_details": row[51] if len(row) > 51 else None,
            # Call details
            "call_duration": row[52] if len(row) > 52 else None,
            "call_is_video": bool(row[53]) if len(row) > 53 and row[53] else False,
            "call_result_label": row[54] if len(row) > 54 else None,
            "call_is_group": bool(row[55]) if len(row) > 55 and row[55] else False,
            # Poll options (name::vote_count, newline separated)
            "poll_options": row[56] if len(row) > 56 else None,
            "poll_total_voters": row[57] if len(row) > 57 else 0,
            # Mentions (name::contact_id;;...)
            "mentions_str": row[58] if len(row) > 58 else None,
            # Location details
            "loc_latitude": row[59] if len(row) > 59 else None,
            "loc_longitude": row[60] if len(row) > 60 else None,
            "loc_place_name": row[61] if len(row) > 61 else None,
            "loc_place_address": row[62] if len(row) > 62 else None,
            "loc_is_live": bool(row[63]) if len(row) > 63 and row[63] else False,
            "loc_live_duration": row[64] if len(row) > 64 else None,
            # Receipt timestamps (for forensic display)
            "first_delivered_ts": row[65] if len(row) > 65 else None,
            "first_read_ts": row[66] if len(row) > 66 else None,
            # Multi-device tracking
            "sender_device_number": row[67] if len(row) > 67 else -1,
            "sender_is_primary": row[68] if len(row) > 68 else -1,
            "sender_platform_label": row[69] if len(row) > 69 else "",
            "origin": row[70] if len(row) > 70 else 0,
            "origination_flags": row[71] if len(row) > 71 else 0,
            # Group member tag (admin-assigned label like apartment number)
            "member_label": row[72] if len(row) > 72 else None,
            "nc_old_phone": row[73] if len(row) > 73 else None,
            "nc_new_phone": row[74] if len(row) > 74 else None,
            "revoked_by_admin_id": row[75] if len(row) > 75 else None,
            "revoked_by_admin_name": row[76] if len(row) > 76 else None,
            "ephemeral_duration": row[77] if len(row) > 77 else None,
            "community_name": row[78] if len(row) > 78 else None,
            "sender_avatar_blob": row[79] if len(row) > 79 else None,
            "se_actor_id": row[80] if len(row) > 80 else None,
            "se_target_id": row[81] if len(row) > 81 else None,
            "call_participants": row[82] if len(row) > 82 else None,
            "poll_voters": row[83] if len(row) > 83 else None,
            "vcard_data": row[84] if len(row) > 84 else None,
            "quoted_type": row[85] if len(row) > 85 else None,
            "scheduled_event_data": row[86] if len(row) > 86 else None,
            "rendered_system_text": row[87] if len(row) > 87 else None,
            "page_count": row[88] if len(row) > 88 else None,
            "view_once_state": row[89] if len(row) > 89 else None,
            "sender_is_meta_verified": bool(row[90]) if len(row) > 90 and row[90] else False,
            "sender_is_business": bool(row[91]) if len(row) > 91 and row[91] else False,
            "sender_jid_row_id": row[92] if len(row) > 92 else None,
            "source_chat_row_id": row[93] if len(row) > 93 else None,
            "source_media_row_id": row[94] if len(row) > 94 else None,
            "loc_thumbnail_blob": row[95] if len(row) > 95 else None,
            "loc_final_lat": row[96] if len(row) > 96 else None,
            "loc_final_lon": row[97] if len(row) > 97 else None,
            "loc_final_ts": row[98] if len(row) > 98 else None,
            "loc_map_url": row[99] if len(row) > 99 else None,
            "is_status_reply": bool(row[100]) if len(row) > 100 and row[100] else False,
            "reply_count": row[101] if len(row) > 101 else 0,
            "last_edit_timestamp": row[102] if len(row) > 102 else None,
            "edit_count": row[103] if len(row) > 103 else 0,
        }

        # Ghost message detection - overlay recovered text for revoked messages
        msg_id = msg["id"]
        is_ghost = False
        if msg_id in self._ghost_map:
            ghost = self._ghost_map[msg_id]
            is_ghost = True
            # Show the original (recovered) text instead of "deleted"
            if ghost.get("original_text"):
                msg["text_content"] = ghost["original_text"]
                msg["is_revoked"] = False  # Don't grey out - show recovered text

        msg["is_ghost"] = is_ghost

        # Check if message is tagged by investigator
        msg["is_tagged"] = msg_id in self._get_tagged_ids()

        if msg["is_revoked"] and not is_ghost:
            msg["display_text"] = "This message was deleted"
        elif msg["media_caption"]:
            msg["display_text"] = msg["media_caption"]
        elif msg["text_content"]:
            msg["display_text"] = msg["text_content"]
        else:
            msg["display_text"] = ""
        msg["show_sender"] = self._is_group and not msg["from_me"]
        return msg

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._data)

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        # Don't auto-fetch — we use explicit fetch_older() for scroll-up loading
        return False

    def has_older(self) -> bool:
        """Check if there are older messages to load above current data."""
        return self._loaded_start > 0

    def fetch_older(self) -> int:
        """Load older messages (prepend to top). Returns count of items added."""
        if self._loaded_start <= 0 or self._conv_id is None:
            return 0

        t0 = _time.perf_counter()
        where, params = self._where_clause()
        new_start = max(0, self._loaded_start - BATCH_SIZE)
        count_to_load = self._loaded_start - new_start

        sql = self._base_sql(where) + " LIMIT ? OFFSET ?"
        rows = self._db.fetchall(sql, tuple(params) + (count_to_load, new_start))
        t1 = _time.perf_counter()
        print(f"[ChatModel] fetch_older: SQL {count_to_load} rows from offset {new_start} -> {len(rows)} rows in {t1-t0:.3f}s")

        new_items: list[dict] = []
        new_raw: list[dict] = []
        last_date_str = ""

        for row in rows:
            msg = self._build_msg_dict(row)
            if (msg["quoted_text"] and msg["display_text"]
                    and msg["quoted_text"].strip() == msg["display_text"].strip()):
                msg["quoted_text"] = None

            # Date separator injection
            ts = msg.get("timestamp")
            if ts and msg.get("message_type") != 7:
                try:
                    msg_date_str = format_timestamp(ts, "date")
                    if msg_date_str != last_date_str:
                        last_date_str = msg_date_str
                        date_label = format_timestamp(ts, '%B %d, %Y')
                        sep = {"id": -1, "message_type": -1,
                               "display_text": date_label, "timestamp": ts}
                        new_items.append(sep)
                except (ValueError, OSError):
                    pass

            new_items.append(msg)
            new_raw.append(msg)

        if not new_items:
            return 0

        # Remove duplicate date separator at boundary with existing data
        if self._data and self._data[0].get("message_type") == -1:
            existing_sep_text = self._data[0].get("display_text", "")
            # Check if the new batch's last date matches the existing separator
            for item in reversed(new_items):
                if item.get("message_type") != -1 and item.get("timestamp"):
                    try:
                        last_new_date = format_timestamp(item["timestamp"], '%B %d, %Y')
                        if last_new_date == existing_sep_text:
                            self._data.pop(0)
                    except (ValueError, OSError):
                        pass
                    break

        count = len(new_items)
        self.beginInsertRows(QModelIndex(), 0, count - 1)
        self._data = new_items + self._data
        self._raw_data = new_raw + self._raw_data
        self._loaded_start = new_start
        # Rebuild key_id + id index (all indices shifted)
        self._key_id_to_row.clear()
        self._id_to_row.clear()
        for i, item in enumerate(self._data):
            key_id = item.get("source_key_id")
            if key_id:
                self._key_id_to_row[key_id] = i
            msg_id = item.get("id")
            if msg_id and msg_id > 0:
                self._id_to_row[msg_id] = i
        self.endInsertRows()
        return count

    def start_prefetch(self) -> bool:
        """Start async prefetch of older messages in a background thread.
        Returns True if prefetch was started, False if not needed or already running."""
        if self._loaded_start <= 0 or self._conv_id is None:
            return False
        if self._prefetch_worker is not None and self._prefetch_worker.isRunning():
            return False  # Already fetching

        where, params = self._where_clause()
        new_start = max(0, self._loaded_start - BATCH_SIZE)
        count_to_load = self._loaded_start - new_start

        sql = self._base_sql(where) + " LIMIT ? OFFSET ?"
        full_params = tuple(params) + (count_to_load, new_start)

        # Get DB path for background connection (immutable URI for concurrent reads)
        raw_path = self._db._db_path if hasattr(self._db, '_db_path') else None
        if not raw_path:
            # Fallback to sync fetch
            self.fetch_older()
            return True
        db_path = f"file:{raw_path}?mode=ro&immutable=1"

        self._prefetch_pending = True
        self._prefetch_worker = _PrefetchWorker(
            db_path, sql, full_params, new_start,
            self._ghost_map, self._tagged_ids, self._is_group,
            parent=self,
        )
        self._prefetch_worker.done.connect(self._on_prefetch_done)
        self._prefetch_worker.start()
        return True

    def _on_prefetch_done(self, new_items: list, new_raw: list, new_start: int):
        """Receive prefetched data and insert into model on the main thread."""
        self._prefetch_pending = False
        if self._prefetch_worker:
            self._prefetch_worker.deleteLater()
            self._prefetch_worker = None

        if not new_items:
            return

        # Remove duplicate date separator at boundary
        if self._data and self._data[0].get("message_type") == -1:
            existing_sep_text = self._data[0].get("display_text", "")
            for item in reversed(new_items):
                if item.get("message_type") != -1 and item.get("timestamp"):
                    try:
                        last_new_date = format_timestamp(item["timestamp"], '%B %d, %Y')
                        if last_new_date == existing_sep_text:
                            self._data.pop(0)
                    except (ValueError, OSError):
                        pass
                    break

        count = len(new_items)
        self.beginInsertRows(QModelIndex(), 0, count - 1)
        self._data = new_items + self._data
        self._raw_data = new_raw + self._raw_data
        self._loaded_start = new_start
        # Rebuild indices (all indices shifted by count)
        self._key_id_to_row.clear()
        self._id_to_row.clear()
        for i, item in enumerate(self._data):
            key_id = item.get("source_key_id")
            if key_id:
                self._key_id_to_row[key_id] = i
            msg_id = item.get("id")
            if msg_id and msg_id > 0:
                self._id_to_row[msg_id] = i
        self.endInsertRows()
        self.prefetch_done.emit(count)

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:
        pass  # No-op; use fetch_older() or start_prefetch()

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._data):
            return None
        if role == MSG_DATA_ROLE:
            return self._data[index.row()]
        if role == Qt.DisplayRole:
            return self._data[index.row()].get("display_text", "")
        return None


class BulkMediaDownloadWorker(QThread):
    """Background worker for downloading all missing media in a conversation."""
    progress = Signal(int, int, str)  # current, total, status_message
    finished = Signal(int, int, int)  # downloaded, failed, skipped
    media_saved = Signal(int, str)  # media_id, save_path (for re-mapping)

    def __init__(self, conv_id: int, db_path: str, sender_mode: str = "all", sender_filter: str = ""):
        super().__init__()
        self._conv_id = conv_id
        self._db_path = db_path
        self._sender_mode = (sender_mode or "all").strip().lower()
        self._sender_filter = (sender_filter or "").strip().lower()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        import os
        import sqlite3 as _sqlite3

        # Open a separate read connection for this thread
        conn = _sqlite3.connect(f"file:{self._db_path}?immutable=1",
                                uri=True, check_same_thread=False)
        conn.row_factory = _sqlite3.Row

        # Open a writable connection for updating media records
        write_conn = _sqlite3.connect(self._db_path, check_same_thread=False)

        sender_where = ""
        params: list[object] = [self._conv_id]
        if self._sender_mode == "mine":
            sender_where += "AND m.from_me = 1 "
        elif self._sender_mode == "others":
            sender_where += "AND m.from_me = 0 "

        if self._sender_filter:
            sender_where += (
                "AND LOWER(COALESCE("
                "CASE WHEN m.from_me = 1 THEN 'You' END, "
                "snd.resolved_name, snd.wa_name, snd.phone_number, m.rendered_sender, ''"
                ")) LIKE ? "
            )
            params.append(f"%{self._sender_filter}%")

        rows = conn.execute(
            "SELECT me.id, me.media_url, me.media_key, me.mime_type, "
            "me.file_hash, m.id as message_id, "
            "COALESCE(m.type_label, '') as type_label, "
            "COALESCE(m.source_key_id, '') as source_key_id "
            "FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "LEFT JOIN contact snd ON snd.id = m.sender_id "
            "WHERE m.conversation_id = ? "
            f"{sender_where}"
            "AND (me.file_exists = 0 OR me.file_exists IS NULL) "
            "AND me.media_url IS NOT NULL AND TRIM(me.media_url) != '' "
            "AND me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32 "
            "AND (me.cdn_expiry_ts IS NULL OR me.cdn_expiry_ts > CAST(strftime('%s','now') AS INTEGER)) "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM media mx "
            "    WHERE mx.file_hash = me.file_hash "
            "    AND mx.id != me.id "
            "    AND mx.file_hash IS NOT NULL AND TRIM(mx.file_hash) != '' "
            "    AND mx.file_exists = 1"
            ")",
            tuple(params),
        ).fetchall()

        total = len(rows)
        if total == 0:
            self.finished.emit(0, 0, 0)
            conn.close()
            write_conn.close()
            return

        import time as _time_mod
        from app.services.media_crypto import (
            download_and_decrypt, get_media_type, get_extension_for_mime,
        )

        # Prefer case recovered_media dir
        from app.services.case_manager import CaseManager
        cm = CaseManager.get()
        if cm.is_open and cm.recovered_media_dir:
            save_dir = str(cm.recovered_media_dir)
        else:
            # Fallback: save next to the analysis.db file
            save_dir = os.path.join(os.path.dirname(str(self._db.path)), "recovered_media")
        os.makedirs(save_dir, exist_ok=True)

        downloaded = 0
        failed = 0
        skipped = 0

        # Initialize evidence DB for audit logging
        try:
            from app.db.media_evidence_db import MediaEvidenceDB
        except ImportError:
            import importlib.util
            from pathlib import Path as _P
            for _p in _P(__file__).resolve().parents:
                _c = _p / "backend" / "app" / "db" / "media_evidence_db.py"
                if _c.is_file():
                    _s = importlib.util.spec_from_file_location("media_evidence_db", str(_c))
                    _m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)
                    MediaEvidenceDB = _m.MediaEvidenceDB; break
            else:
                class MediaEvidenceDB:
                    _instance = None
                    @classmethod
                    def get(cls): return cls()
                    is_open = False
                    def log_download(self, *a, **kw): pass
                    def update_registry_status(self, *a, **kw): pass
        evidence_db = MediaEvidenceDB.get()
        _tool_ver = "2.2.0"
        _case_id = ""
        try:
            _ci = conn.execute("SELECT value FROM case_metadata WHERE key='case_id'").fetchone()
            if _ci:
                _case_id = _ci[0]
        except Exception:
            pass

        for i, row in enumerate(rows):
            if self._cancelled:
                break

            media_id = row[0]
            url = row[1]
            key = row[2]
            mime_type = row[3] or ""
            file_hash = row[4]
            message_id = row[5]
            type_label = row[6]
            source_key_id = row[7] if len(row) > 7 else ""

            ext = get_extension_for_mime(mime_type) if mime_type else ".bin"
            _dl_ts = int(_time_mod.time())
            save_path = os.path.join(save_dir, f"Recovered_msg_media_{message_id}_{_dl_ts}{ext}")

            # Skip if already downloaded on disk
            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                # Already on disk — update DB mapping
                import time as _time_mod
                recovery_ts = int(_time_mod.time() * 1000)
                write_conn.execute(
                    "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                    "recovery_method = 'downloaded', recovery_timestamp = ? WHERE id = ?",
                    (save_path, recovery_ts, media_id),
                )
                # Link all copies with same file_hash to this file
                if file_hash:
                    write_conn.execute(
                        "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                        "recovery_method = 'hash_linked', recovery_timestamp = ? "
                        "WHERE file_hash = ? AND id != ? AND (file_exists = 0 OR file_exists IS NULL)",
                        (save_path, recovery_ts, file_hash, media_id),
                    )
                write_conn.commit()
                skipped += 1
                self.media_saved.emit(media_id, save_path)
                self.progress.emit(i + 1, total, f"Skipped (exists): msg_{message_id}{ext}")
                continue

            media_type = get_media_type(type_label, mime_type)
            self.progress.emit(i + 1, total, f"Downloading: msg_{message_id}{ext}")

            # Audit: log download start
            if evidence_db.is_open:
                evidence_db.log_download(
                    source_key_id, "download_start", success=True,
                    media_url=url, expected_hash=file_hash,
                    tool_version=_tool_ver, case_id=_case_id,
                    notes=f"mime={mime_type}, media_id={media_id}, msg_id={message_id}")

            try:
                import time as _time_mod
                recovery_ts = int(_time_mod.time() * 1000)
                download_and_decrypt(
                    url=url, media_key=key, media_type=media_type,
                    file_hash=file_hash, save_path=save_path, timeout=20,
                )

                # Audit: log download success
                _saved_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
                if evidence_db.is_open:
                    evidence_db.log_download(
                        source_key_id, "download_success", success=True,
                        media_url=url, expected_hash=file_hash,
                        saved_path=save_path, saved_size=_saved_size,
                        tool_version=_tool_ver, case_id=_case_id)
                    evidence_db.update_registry_status(
                        source_key_id, recovery_method="downloaded",
                        recovery_timestamp=recovery_ts,
                        recovered_file_path=save_path, is_on_disk=1)

                # Update DB: map the resolved path and mark as exists
                write_conn.execute(
                    "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                    "recovery_method = 'downloaded', recovery_timestamp = ? WHERE id = ?",
                    (save_path, recovery_ts, media_id),
                )
                # Link all copies with same file_hash to this file
                if file_hash:
                    write_conn.execute(
                        "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                        "recovery_method = 'hash_linked', recovery_timestamp = ? "
                        "WHERE file_hash = ? AND id != ? AND (file_exists = 0 OR file_exists IS NULL)",
                        (save_path, recovery_ts, file_hash, media_id),
                    )
                write_conn.commit()
                downloaded += 1
                self.media_saved.emit(media_id, save_path)
            except Exception as e:
                failed += 1
                err_msg = f"{type(e).__name__}: {e}"
                self.progress.emit(i + 1, total, f"Failed: {err_msg}")
                # Audit: log download failure with full error
                if evidence_db.is_open:
                    _http_code = None
                    if "410" in str(e):
                        _http_code = 410
                    elif "404" in str(e):
                        _http_code = 404
                    elif "403" in str(e):
                        _http_code = 403
                    evidence_db.log_download(
                        source_key_id, "download_fail", success=False,
                        error_message=err_msg, media_url=url,
                        http_status_code=_http_code,
                        expected_hash=file_hash,
                        tool_version=_tool_ver, case_id=_case_id)
                # Mark as download_failed so it doesn't keep showing as downloadable
                try:
                    write_conn.execute(
                        "UPDATE media SET media_status = 'download_failed' WHERE id = ?",
                        (media_id,),
                    )
                    write_conn.commit()
                except Exception:
                    pass

        # Checkpoint WAL so immutable read connections can see the writes
        try:
            write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
        write_conn.close()
        # Reconnect the global Database singleton to pick up changes
        try:
            Database.get().reconnect_read()
        except Exception:
            pass
        self.finished.emit(downloaded, failed, skipped)


class ChatViewerPage(QWidget):
    """WhatsApp-style chat viewer with bubble messages."""

    back_requested = Signal()
    group_info_requested = Signal(int, str)  # conv_id, display_name
    contact_requested = Signal(int)  # contact_id
    conversation_switch_requested = Signal(int, int)  # conv_id, message_id
    find_similar_requested = Signal(int)  # message_id — navigate to similarity page

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._conv_id: int | None = None
        self._conv_name: str = ""
        self._is_group: bool = False
        self._debug_visible: bool = False
        self._font_size: int = 10

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Header bar ----
        header = QFrame()
        header.setObjectName("chatHeader")
        header.setFixedHeight(50)
        header.setStyleSheet(self._tm.chat_header_style())
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(6)

        self._back_btn = QPushButton("\u25C0")  # ◀ solid left triangle
        self._back_btn.setFixedSize(36, 36)
        self._back_btn.setStyleSheet(self._tm.chat_back_btn_style())
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.setToolTip("Back to conversations")
        self._back_btn.clicked.connect(self.back_requested.emit)
        hl.addWidget(self._back_btn)

        # Avatar circle (clickable for group info)
        self._avatar = QLabel("?")
        self._avatar.setFixedSize(36, 36)
        self._avatar.setAlignment(Qt.AlignCenter)
        self._avatar.setStyleSheet("""
            QLabel { background: #00897b; border-radius: 18px;
                     color: white; font-size: 14px; font-weight: bold; }
        """)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._avatar.mousePressEvent = self._on_avatar_click
        hl.addWidget(self._avatar)

        # Name + info (clickable for group info)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        self._title_label = QLabel("Select a conversation")
        f = QFont(); f.setPointSize(12); f.setBold(True)
        self._title_label.setFont(f)
        self._title_label.setStyleSheet(self._tm.chat_title_style())
        self._title_label.setCursor(Qt.PointingHandCursor)
        self._title_label.mousePressEvent = self._on_avatar_click
        name_col.addWidget(self._title_label)
        self._info_label = QLabel("")
        self._info_label.setStyleSheet(self._tm.chat_info_label_style())
        name_col.addWidget(self._info_label)
        hl.addLayout(name_col, 1)

        # Header action buttons
        _hdr_btn_style = self._tm.chat_hdr_btn_style()

        self._ghost_filter_active = False
        self._missing_media_filter_active = False
        self._saved_filter_state: dict | None = None
        self._search_results: list[int] = []
        self._search_idx: int = -1

        # ============================================================
        # CHAT HEADER TOOLBAR
        # Each button uses an unambiguous emoji or short-text label
        # (bare Unicode glyphs render unreliably on some platforms),
        # with thin separators between functional groups so the
        # strip reads left-to-right as
        #   nav \u00B7 font \u00B7 filters \u00B7 download \u00B7 views \u00B7 debug
        # ============================================================
        _hdr_btn_style_v2 = (
            _hdr_btn_style
            + " QPushButton { font-size: 13px; padding: 0; }"
        )
        _toolbar_groups = [
            # nav
            [
                ("\u23EB",       "Scroll to first message",  lambda: self._scroll_to_first(), None),
                ("\u23EC",       "Scroll to last message",   lambda: self._scroll_to_last(),  None),
                ("\U0001F4CC",   "Jump to first unread message (if any)",
                                                              lambda: self._scroll_to_first_unread(), None),
            ],
            # font
            [
                ("A\u2212", "Decrease font size", lambda: self._font_decrease(), None),
                ("A+",      "Increase font size", lambda: self._font_increase(), None),
            ],
            # filters
            [
                ("\U0001F4C5", "Filter by date range",
                                                          lambda: self._toggle_date_filter(),  None),
                ("\U0001F47B", "Browse recovered ghost messages (opens a sidebar with previews + jump-to)",
                                                          lambda: self._toggle_ghost_sidebar(), "_ghost_btn"),
                ("\U0001F4F2", "Show only messages with missing downloadable media",
                                                          lambda: self._toggle_missing_media_filter(),
                                                                                                 "_missing_media_btn"),
                ("\U0001F465", "Filter by sender",        lambda: self._toggle_sender_filter(), None),
                ("\U0001F50D", "Search in chat",          lambda: self._toggle_search(),       None),
            ],
            # download / recovery
            [
                ("\u2B07\uFE0F", "Download all missing media in this chat",
                                                          lambda: self._start_bulk_download(),  "_bulk_dl_btn"),
                ("\u2601\uFE0F", "Media recovery & download stats",
                                                          lambda: self._toggle_download_panel(), None),
            ],
            # views
            [
                ("\U0001F5BC\uFE0F", "Media gallery",
                                                       lambda: self._toggle_media_gallery(),  "_gallery_btn"),
                ("\U0001F41E",       "Toggle debug info panel",
                                                       lambda: self._toggle_debug(),         "_debug_btn"),
            ],
        ]
        for _gi, _grp in enumerate(_toolbar_groups):
            if _gi > 0:
                # Visual separator between groups
                _sep = QWidget()
                _sep.setFixedSize(8, 22)
                _sep.setStyleSheet(
                    "background: transparent; "
                    "border-left: 1px solid rgba(127,127,127,0.25); margin: 2px 3px;"
                )
                hl.addWidget(_sep)
            for label, tooltip, handler, attr_name in _grp:
                btn = QPushButton(label)
                btn.setFixedSize(30, 28)
                btn.setToolTip(tooltip)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setStyleSheet(_hdr_btn_style_v2)
                btn.clicked.connect(handler)
                hl.addWidget(btn)
                if attr_name:
                    setattr(self, attr_name, btn)

        # "Clear all filters" button (hidden by default, shown when any filter active)
        self._clear_all_filters_btn = QPushButton("\u2716")
        self._clear_all_filters_btn.setFixedSize(28, 28)
        self._clear_all_filters_btn.setToolTip("Clear all filters")
        self._clear_all_filters_btn.setVisible(False)
        self._clear_all_filters_btn.setStyleSheet(
            "QPushButton { background: rgba(239,83,80,0.15); border: 1px solid #ef5350;"
            " border-radius: 14px; color: #ef5350; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(239,83,80,0.3); }"
        )
        self._clear_all_filters_btn.clicked.connect(self._clear_all_filters)
        hl.addWidget(self._clear_all_filters_btn)

        layout.addWidget(header)

        # ---- Search bar (hidden by default) ----
        self._search_bar = QFrame()
        self._search_bar.setFixedHeight(38)
        self._search_bar.setVisible(False)
        self._search_bar.setStyleSheet(self._tm.chat_search_bar_style())
        sbl = QHBoxLayout(self._search_bar)
        sbl.setContentsMargins(12, 3, 12, 3)
        self._chat_search = QLineEdit()
        self._chat_search.setPlaceholderText("Search in this chat...")
        self._chat_search.setFixedHeight(28)
        self._chat_search.setClearButtonEnabled(True)
        self._chat_search.setStyleSheet(self._tm.chat_search_input_style())
        sbl.addWidget(self._chat_search)
        layout.addWidget(self._search_bar)

        # ---- Date filter bar (hidden by default) ----
        self._date_bar = QFrame()
        self._date_bar.setFixedHeight(38)
        self._date_bar.setVisible(False)
        self._date_bar.setStyleSheet(self._tm.chat_search_bar_style())
        dbl2 = QHBoxLayout(self._date_bar)
        dbl2.setContentsMargins(12, 3, 12, 3)
        dbl2.setSpacing(6)
        dbl2.addWidget(QLabel("From:"))
        self._date_from_edit = QDateEdit()
        self._date_from_edit.setCalendarPopup(True)
        self._date_from_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_from_edit.setFixedHeight(26)
        self._date_from_edit.setStyleSheet(self._tm.chat_date_edit_style())
        dbl2.addWidget(self._date_from_edit)
        dbl2.addWidget(QLabel("To:"))
        self._date_to_edit = QDateEdit()
        self._date_to_edit.setCalendarPopup(True)
        self._date_to_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_to_edit.setFixedHeight(26)
        self._date_to_edit.setStyleSheet(self._tm.chat_date_edit_style())
        dbl2.addWidget(self._date_to_edit)
        self._date_apply_btn = QPushButton("Apply")
        self._date_apply_btn.setFixedSize(52, 26)
        self._date_apply_btn.setStyleSheet(self._tm.chat_date_apply_btn_style())
        self._date_apply_btn.clicked.connect(self._apply_date_filter)
        dbl2.addWidget(self._date_apply_btn)
        self._date_clear_btn = QPushButton("Clear")
        self._date_clear_btn.setFixedSize(44, 26)
        self._date_clear_btn.setStyleSheet(self._tm.chat_date_clear_btn_style())
        self._date_clear_btn.clicked.connect(self._clear_date_filter)
        dbl2.addWidget(self._date_clear_btn)
        dbl2.addStretch()
        layout.addWidget(self._date_bar)

        # ---- Calendar heatmap (hidden by default, toggled with date filter) ----
        from app.views.widgets.calendar_heatmap import CalendarHeatmapWidget
        self._calendar_heatmap = CalendarHeatmapWidget()
        self._calendar_heatmap.setVisible(False)
        self._calendar_heatmap.range_selected.connect(self._on_calendar_range)
        self._calendar_heatmap.date_selected.connect(self._on_calendar_date)
        self._calendar_heatmap.range_cleared.connect(self._clear_date_filter)
        layout.addWidget(self._calendar_heatmap)

        # ---- Sender filter bar (hidden by default) ----
        # ---- Sender filter bar (hidden by default) ----
        # An editable QComboBox + substring-matching QCompleter gives a
        # search-as-you-type UX without needing a separate popup widget.
        # The combo's view shows the full label; the completer matches
        # against any substring (saved name, phone, raw JID, LID).  The
        # secondary-text "JID hint" label below the combo surfaces the
        # currently-highlighted entry's full identifier so analysts can
        # cross-reference msgstore.jid lookups directly.
        from PySide6.QtWidgets import QCompleter
        self._sender_bar = QFrame()
        self._sender_bar.setFixedHeight(64)
        self._sender_bar.setVisible(False)
        self._sender_bar.setStyleSheet(self._tm.chat_search_bar_style())
        sfb_outer = QVBoxLayout(self._sender_bar)
        sfb_outer.setContentsMargins(12, 3, 12, 3)
        sfb_outer.setSpacing(2)
        sfbl = QHBoxLayout()
        sfbl.setSpacing(6)
        sfbl.addWidget(QLabel("Sender:"))
        self._sender_combo = QComboBox()
        self._sender_combo.setEditable(True)
        self._sender_combo.setInsertPolicy(QComboBox.NoInsert)
        self._sender_combo.lineEdit().setPlaceholderText(
            "Type to search by name, phone, JID, or LID…"
        )
        self._sender_combo.setFixedHeight(26)
        self._sender_combo.setMinimumWidth(420)
        self._sender_combo.setMaxVisibleItems(15)
        # Substring search across the visible label — typing "lid" filters
        # to LID-only entries, typing "+91" filters to Indian phones, etc.
        self._sender_completer = QCompleter(self._sender_combo)
        self._sender_completer.setFilterMode(Qt.MatchContains)
        self._sender_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._sender_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._sender_combo.setCompleter(self._sender_completer)
        _lt = self._tm.is_light
        self._sender_combo.setStyleSheet(
            "QComboBox { background: #f5f5f5; border: 1px solid #d0d0d0; "
            "border-radius: 4px; color: #1b1b1b; padding: 2px 8px; font-size: 12px; }"
            "QComboBox QAbstractItemView { font-size: 12px; }"
            if _lt else
            "QComboBox { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); "
            "border-radius: 4px; color: #e9edef; padding: 2px 8px; font-size: 12px; }"
            "QComboBox QAbstractItemView { background: #1a2026; color: #e9edef; font-size: 12px; }"
        )
        # When the user picks via mouse OR confirms the completer, apply
        # the filter.  ``activated`` fires for both whereas
        # ``currentIndexChanged`` would also fire when we programmatically
        # rebuild the list, causing a stray reload.
        self._sender_combo.activated.connect(self._apply_sender_filter)
        sfbl.addWidget(self._sender_combo, 1)
        sender_clear = QPushButton("Clear")
        sender_clear.setFixedSize(52, 26)
        sender_clear.setStyleSheet(self._tm.chat_date_clear_btn_style())
        sender_clear.clicked.connect(self._clear_sender_filter)
        sfbl.addWidget(sender_clear)
        sfbl.addStretch()
        sfb_outer.addLayout(sfbl)

        # JID hint row — shows the full forensic identifier for whoever
        # is currently selected so the analyst can copy-paste it without
        # opening Contact Detail.
        self._sender_jid_hint = QLabel("")
        self._sender_jid_hint.setStyleSheet(
            f"font-size: 10px; color: {'#666' if _lt else '#8696a0'};"
            " font-family: 'Cascadia Mono', 'Consolas', monospace;"
        )
        self._sender_jid_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sfb_outer.addWidget(self._sender_jid_hint)
        layout.addWidget(self._sender_bar)

        # ---- Filter context bar (shown when viewing msg in full context) ----
        self._filter_context_bar = QFrame()
        self._filter_context_bar.setFixedHeight(38)
        self._filter_context_bar.setVisible(False)
        self._filter_context_bar.setStyleSheet(
            "QFrame { background: #e0f2f1; border-bottom: 1px solid #00897b; }"
            if _lt else
            "QFrame { background: #1a3a4a; border-bottom: 1px solid #00bcd4; }"
        )
        fcbl = QHBoxLayout(self._filter_context_bar)
        fcbl.setContentsMargins(12, 3, 12, 3)
        fcbl.setSpacing(10)
        _fc_icon = QLabel("\u2630")
        _fc_icon.setStyleSheet(
            "color: #00897b; font-size: 14px;" if _lt else
            "color: #00bcd4; font-size: 14px;"
        )
        fcbl.addWidget(_fc_icon)
        self._filter_context_label = QLabel("Viewing message in full context")
        self._filter_context_label.setStyleSheet(
            "color: #1b1b1b; font-size: 12px;" if _lt else
            "color: #e9edef; font-size: 12px;"
        )
        fcbl.addWidget(self._filter_context_label)
        fcbl.addStretch()
        _back_btn = QPushButton("\u2190 Back to filtered view")
        _back_btn.setFixedHeight(26)
        _back_btn.setStyleSheet(self._tm.chat_date_apply_btn_style())
        _back_btn.clicked.connect(self._restore_saved_filters)
        fcbl.addWidget(_back_btn)
        _exit_btn = QPushButton("Browse normally")
        _exit_btn.setFixedHeight(26)
        _exit_btn.setStyleSheet(self._tm.chat_date_clear_btn_style())
        _exit_btn.clicked.connect(self._discard_saved_filters)
        fcbl.addWidget(_exit_btn)
        layout.addWidget(self._filter_context_bar)

        # ---- Group analytics bar (hidden by default, shown for groups) ----
        self._analytics_bar = QFrame()
        self._analytics_bar.setVisible(False)
        self._analytics_bar.setStyleSheet("""
            QFrame { background: rgba(0, 188, 212, 0.04);
                     border-bottom: 1px solid rgba(0, 188, 212, 0.15); }
        """)
        abl = QHBoxLayout(self._analytics_bar)
        abl.setContentsMargins(16, 6, 16, 6)
        abl.setSpacing(20)
        self._analytics_labels: list[QLabel] = []
        for _ in range(5):
            lbl = QLabel("")
            lbl.setStyleSheet("color: #607d8b; font-size: 10px;")
            abl.addWidget(lbl)
            self._analytics_labels.append(lbl)
        abl.addStretch()
        close_analytics = QPushButton("\u2715")
        close_analytics.setFixedSize(20, 20)
        close_analytics.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #90a4ae; font-size: 11px; }"
            "QPushButton:hover { color: #37474f; }"
            if _lt else
            "QPushButton { background: transparent; border: none; color: rgba(255,255,255,0.3); font-size: 11px; }"
            "QPushButton:hover { color: #e9edef; }"
        )
        close_analytics.clicked.connect(lambda: self._analytics_bar.setVisible(False))
        abl.addWidget(close_analytics)
        layout.addWidget(self._analytics_bar)

        # ---- Pinned messages bar (hidden by default, shown when pins exist) ----
        self._pin_bar = QFrame()
        self._pin_bar.setFixedHeight(44)
        self._pin_bar.setVisible(False)
        self._pin_bar.setStyleSheet(
            "QFrame { background: #e8f5e9; border-bottom: 1px solid #c8e6c9; }"
            if _lt else
            "QFrame { background: rgba(76, 175, 80, 0.08); "
            "border-bottom: 1px solid rgba(76, 175, 80, 0.15); }"
        )
        pbl = QHBoxLayout(self._pin_bar)
        pbl.setContentsMargins(10, 4, 6, 4)
        pbl.setSpacing(8)

        # Vertical green accent bar (like WhatsApp)
        _pin_accent = QFrame()
        _pin_accent.setFixedWidth(3)
        _pin_accent.setStyleSheet(
            "background: #25D366; border-radius: 1px;"
        )
        pbl.addWidget(_pin_accent)

        # Counter "1/3" label
        self._pin_counter_label = QLabel("")
        self._pin_counter_label.setFixedWidth(36)
        self._pin_counter_label.setAlignment(Qt.AlignCenter)
        self._pin_counter_label.setStyleSheet(
            "color: #25D366; font-size: 11px; font-weight: bold;"
        )
        pbl.addWidget(self._pin_counter_label)

        # Middle: sender + preview (stacked)
        _pin_text_area = QVBoxLayout()
        _pin_text_area.setContentsMargins(0, 0, 0, 0)
        _pin_text_area.setSpacing(0)
        self._pin_sender_label = QLabel("")
        self._pin_sender_label.setStyleSheet(
            "color: #2e7d32; font-size: 10px; font-weight: bold; padding: 0; margin: 0;"
            if _lt else
            "color: #81c784; font-size: 10px; font-weight: bold; padding: 0; margin: 0;"
        )
        _pin_text_area.addWidget(self._pin_sender_label)
        self._pin_label = QLabel("")
        self._pin_label.setStyleSheet(
            "color: #546e7a; font-size: 11px; padding: 0; margin: 0;"
            if _lt else
            "color: rgba(255,255,255,0.6); font-size: 11px; padding: 0; margin: 0;"
        )
        self._pin_label.setWordWrap(False)
        _pin_text_area.addWidget(self._pin_label)
        pbl.addLayout(_pin_text_area, 1)

        # Navigate left button (previous pin)
        _pin_left_btn = QPushButton("Prev")
        _pin_left_btn.setFixedSize(44, 28)
        _pin_left_btn.setCursor(Qt.PointingHandCursor)
        _pin_left_btn.setStyleSheet(
            "QPushButton { background: rgba(37,211,102,0.08); color: #25D366; "
            "font-family: 'Segoe UI'; font-size: 11px; font-weight: 600; border: 1px solid rgba(37,211,102,0.2); "
            "border-radius: 14px; padding: 0 6px; } "
            "QPushButton:hover { background: rgba(37,211,102,0.2); }"
        )
        _pin_left_btn.clicked.connect(self._on_pin_prev)
        pbl.addWidget(_pin_left_btn)

        # Navigate right button (next pin)
        _pin_right_btn = QPushButton("Next")
        _pin_right_btn.setFixedSize(44, 28)
        _pin_right_btn.setCursor(Qt.PointingHandCursor)
        _pin_right_btn.setStyleSheet(
            "QPushButton { background: rgba(37,211,102,0.08); color: #25D366; "
            "font-family: 'Segoe UI'; font-size: 11px; font-weight: 600; border: 1px solid rgba(37,211,102,0.2); "
            "border-radius: 14px; padding: 0 6px; } "
            "QPushButton:hover { background: rgba(37,211,102,0.2); }"
        )
        _pin_right_btn.clicked.connect(self._on_pin_next)
        pbl.addWidget(_pin_right_btn)

        # Close button
        _pin_close = QPushButton("\u2715")
        _pin_close.setFixedSize(24, 24)
        _pin_close.setCursor(Qt.PointingHandCursor)
        _pin_close.setStyleSheet(
            "QPushButton { background: transparent; color: #999; "
            "font-size: 12px; border: none; } "
            "QPushButton:hover { color: #c62828; }"
        )
        _pin_close.clicked.connect(lambda: self._pin_bar.setVisible(False))
        pbl.addWidget(_pin_close)

        # Make text labels clickable to navigate to current pin
        self._pin_sender_label.setCursor(Qt.PointingHandCursor)
        self._pin_sender_label.mousePressEvent = self._on_pin_bar_click
        self._pin_label.setCursor(Qt.PointingHandCursor)
        self._pin_label.mousePressEvent = self._on_pin_bar_click
        self._pin_counter_label.setCursor(Qt.PointingHandCursor)
        self._pin_counter_label.mousePressEvent = self._on_pin_bar_click
        self._pinned_msg_ids: list[int] = []
        self._pin_previews: list[tuple[str, str]] = []  # (sender, preview)
        self._pin_index: int = 0
        layout.addWidget(self._pin_bar)

        # ---- Admins-only-send banner (shown for announcement groups) ----
        # Populated by _update_chat_policy_banner() when a conversation
        # loads; hidden when policy is normal or not applicable.
        self._policy_banner = QLabel("")
        self._policy_banner.setWordWrap(True)
        self._policy_banner.setTextFormat(Qt.RichText)
        self._policy_banner.setVisible(False)
        layout.addWidget(self._policy_banner)

        # ---- Content area (message list + optional debug panel) ----
        content_area = QHBoxLayout()
        content_area.setContentsMargins(0, 0, 0, 0)
        content_area.setSpacing(0)

        # Message list wrapper
        list_container = QWidget()
        list_layout = QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)

        # Message list (WhatsApp bubble view)
        self._model = ChatMessageModel()
        self._delegate = BubbleDelegate()
        self._use_webengine = _HAS_WEBENGINE

        if self._use_webengine:
            # --- QWebEngineView (Chromium-based, smooth 60fps) ---
            self._web_view = ChatWebView()
            self._list = None  # Not used in WebEngine mode
            list_layout.addWidget(self._web_view)
        else:
            # --- Fallback: QListView + BubbleDelegate (QPainter) ---
            self._web_view = None
            self._list = QListView()
            self._list.setModel(self._model)
            self._list.setItemDelegate(self._delegate)
            self._list.setSelectionMode(QAbstractItemView.SingleSelection)
            self._list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._list.setUniformItemSizes(False)
            self._list.setLayoutMode(QListView.Batched)
            self._list.setBatchSize(40)
            self._list.setSpacing(0)
            self._list.setStyleSheet(self._tm.chat_list_style())
            self._list.setContextMenuPolicy(Qt.CustomContextMenu)
            self._list.customContextMenuRequested.connect(self._show_context_menu)
            self._list.setMouseTracking(True)
            self._list.viewport().installEventFilter(self)
            list_layout.addWidget(self._list)

        # Floating date overlay (only for QListView mode)
        self._date_overlay = QLabel("")
        if self._list:
            self._date_overlay.setParent(self._list)
        else:
            self._date_overlay.setParent(self)
        self._date_overlay.setAlignment(Qt.AlignCenter)
        self._date_overlay.setFixedHeight(22)
        self._date_overlay.setStyleSheet(self._tm.chat_date_overlay_style())
        self._date_overlay.setVisible(False)

        content_area.addWidget(list_container, 1)

        # Debug info panel (hidden by default, right side)
        self._debug_panel = QFrame()
        self._debug_panel.setFixedWidth(360)
        self._debug_panel.setVisible(False)
        self._debug_panel.setStyleSheet(self._tm.chat_debug_panel_style())
        dp_layout = QVBoxLayout(self._debug_panel)
        dp_layout.setContentsMargins(10, 6, 10, 6)
        dp_layout.setSpacing(3)

        dp_header = QLabel("\u2261  Message Debug Info")
        dp_header.setStyleSheet(self._tm.chat_debug_header_style())
        dp_layout.addWidget(dp_header)

        self._debug_text = QTextEdit()
        self._debug_text.setReadOnly(True)
        self._debug_text.setStyleSheet(self._tm.chat_debug_text_style())
        dp_layout.addWidget(self._debug_text, 1)

        content_area.addWidget(self._debug_panel)

        # Media gallery panel (hidden by default, right side)
        self._media_panel = ChatMediaGalleryPanel()
        self._media_panel.setVisible(False)
        self._media_panel.navigate_to_message.connect(self._navigate_to_message_id)
        self._media_panel.close_requested.connect(self._toggle_media_gallery)
        if hasattr(self._media_panel, "navigate_to_chat"):
            self._media_panel.navigate_to_chat.connect(self._on_navigate_to_chat)
        content_area.addWidget(self._media_panel)

        # Media download/recovery panel (hidden by default, right side)
        from app.views.widgets.media_download_panel import MediaDownloadPanel
        self._download_panel = MediaDownloadPanel()
        self._download_panel.setVisible(False)
        self._download_panel.close_requested.connect(self._toggle_download_panel)
        self._download_panel.download_chat_requested.connect(self._start_bulk_download)
        self._download_panel.navigate_to_chat.connect(
            lambda cid: self._on_navigate_to_chat(cid, 0)
        )
        content_area.addWidget(self._download_panel)

        # Search results panel (hidden by default, right side)
        self._search_results_panel = SearchResultsPanel(self, self._tm)
        self._search_results_panel.setVisible(False)
        self._search_results_panel.result_selected.connect(self._on_search_result_selected)
        self._search_results_panel.search_options_changed.connect(self._do_search)
        content_area.addWidget(self._search_results_panel)

        # Replies sidebar panel (hidden by default, right side)
        self._replies_sidebar = RepliesSidebarPanel(self, self._tm)
        self._replies_sidebar.setVisible(False)
        self._replies_sidebar.reply_selected.connect(self._on_search_result_selected)  # reuse nav
        self._replies_sidebar.cross_conv_nav.connect(self._on_reply_cross_conv_nav)
        content_area.addWidget(self._replies_sidebar)

        # Ghost messages sidebar panel (hidden by default, right side)
        # Lists every ghost-recovered message in the current chat with
        # preview + jump-to-message — designed to replace the chat-
        # level "ghost-only" filter with a non-destructive sidebar.
        self._ghost_sidebar = GhostMessagesSidebarPanel(self, self._tm)
        self._ghost_sidebar.setVisible(False)
        self._ghost_sidebar.ghost_selected.connect(self._on_search_result_selected)
        content_area.addWidget(self._ghost_sidebar)

        content_widget = QWidget()
        content_widget.setLayout(content_area)
        layout.addWidget(content_widget, 1)

        # ---- Bottom detail bar ----
        self._detail_bar = QFrame()
        self._detail_bar.setFixedHeight(28)
        self._detail_bar.setStyleSheet(self._tm.chat_detail_bar_style())
        dbl = QHBoxLayout(self._detail_bar)
        dbl.setContentsMargins(12, 0, 12, 0)
        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet(self._tm.chat_detail_label_style())
        dbl.addWidget(self._detail_label)
        layout.addWidget(self._detail_bar)

        # ---- Signals ----
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(400)
        self._search_timer.timeout.connect(self._do_search)
        self._chat_search.textChanged.connect(lambda: self._search_timer.start())
        self._chat_search.returnPressed.connect(self._on_search_enter)

        self._loading_older = False
        self._prefetch_anchor_row = 0
        self._model.prefetch_done.connect(self._on_async_prefetch_done)

        # Date overlay / scroll idle timers (needed in both modes for guard safety)
        self._date_overlay_timer = QTimer()
        self._date_overlay_timer.setSingleShot(True)
        self._date_overlay_timer.setInterval(1500)
        self._date_overlay_timer.timeout.connect(
            lambda: self._date_overlay.setVisible(False)
        )
        self._scroll_idle_timer = QTimer()
        self._scroll_idle_timer.setSingleShot(True)
        self._scroll_idle_timer.setInterval(120)
        self._scroll_idle_timer.timeout.connect(self._on_scroll_idle)

        if self._use_webengine:
            # --- WebEngine signal wiring ---
            self._web_view.quote_clicked.connect(self._navigate_to_quoted)
            self._web_view.sender_clicked.connect(
                lambda cid: self.contact_requested.emit(
                    getattr(self, '_owner_cid', cid) if cid == -1 else cid
                )
            )
            self._web_view.media_clicked.connect(self._on_webengine_media_click)
            self._web_view.audio_play_requested.connect(self._toggle_audio_play)
            self._web_view.bridge.audio_seek.connect(self._seek_audio)
            self._web_view.url_clicked.connect(self._on_webengine_url_click)
            self._web_view.mention_clicked.connect(
                lambda cid: self.contact_requested.emit(cid)
            )
            self._web_view.context_menu_requested.connect(self._on_webengine_context_menu)
            self._web_view.load_older_requested.connect(self._on_webengine_load_older)
            self._web_view.load_range_requested.connect(self._on_webengine_load_range)
            self._web_view.cancel_pending_requested.connect(self._on_cancel_pending)
            self._web_view.download_requested.connect(self._on_webengine_download)
            self._web_view.reaction_clicked.connect(self._show_reactions_detail)
            self._web_view.forensic_info_requested.connect(self._on_forensic_info_request)
            self._web_view.vcard_download_requested.connect(self._on_vcard_download)
            self._web_view.scroll_to_unloaded_requested.connect(self._on_scroll_to_unloaded)
            self._web_view.scroll_to_key_unloaded_requested.connect(self._on_scroll_to_key_unloaded)
            self._web_view.comments_requested.connect(self._on_comments_click)
            self._web_view.receipt_detail_requested.connect(self._show_receipt_detail)
            self._web_view.edit_history_requested.connect(self._show_edit_history_popup)
            self._web_view.replies_requested.connect(self._show_replies_panel)
            self._web_view.call_origin_nav_requested.connect(self._on_call_origin_nav)
        else:
            # --- QListView signal wiring ---
            self._list.clicked.connect(self._on_item_clicked)

            self._list.verticalScrollBar().valueChanged.connect(self._on_scroll_combined)

            # Connect delegate signals
            self._delegate.quote_clicked.connect(self._navigate_to_quoted)
            self._delegate.sender_clicked.connect(
                lambda cid: self.contact_requested.emit(cid)
            )
            self._delegate.media_clicked.connect(self._open_media_viewer)
            self._delegate.audio_play_requested.connect(self._toggle_audio_play)
            self._delegate.audio_seek_requested.connect(self._seek_audio)
            self._delegate.reaction_clicked.connect(self._show_reactions_detail)
            self._delegate.download_media_requested.connect(self._download_decrypt_media)
            self._delegate.replies_clicked.connect(self._show_replies_panel)
            self._delegate.cross_chat_quote_clicked.connect(
                lambda conv_id, msg_id: self.conversation_switch_requested.emit(conv_id, msg_id)
            )
            self._delegate.edit_clicked.connect(self._show_edit_history)

        # Inline audio player
        self._audio_player = None
        self._audio_output = None
        self._audio_msg_id: int = 0

    # ---- WebEngine-specific handlers ----

    def _on_webengine_media_click(self, json_str: str):
        """Handle media click from WebEngine bridge."""
        try:
            data = _json.loads(json_str)
            path = data.get("path", "")
            if path:
                self._open_media_viewer(path)
        except Exception as e:
            print(f"[WebEngine] media click error: {e}")

    def _on_webengine_url_click(self, url: str):
        """Handle URL click from WebEngine bridge."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    def _on_webengine_context_menu(self, msg_id_str: str, screen_x: int, screen_y: int):
        """Handle right-click context menu from WebEngine bridge."""
        try:
            msg_id = int(msg_id_str)
        except (ValueError, TypeError):
            return
        # Look up message dict from model first (fast)
        row = self._model.find_row_by_msg_id(msg_id)
        msg = self._model.get_message_at(row) if row >= 0 else None
        if not msg:
            # Direct DB fetch — message is in a tile not in the model
            try:
                db = Database.get()
                sql = self._model._base_sql("m.id = ?") + " LIMIT 1"
                db_row = db.fetchone(sql, (msg_id,))
                if db_row:
                    msg = self._model._build_msg_dict(db_row)
                    ChatMessageModel._fetch_auxiliary_batch(db, [msg])
            except Exception:
                pass
        if not msg:
            return
        self._show_context_menu_for_msg(msg, QPoint(screen_x, screen_y))

    def _on_webengine_load_older(self):
        """No-op — tile-based loading handles scroll-to-top automatically.
        JS requests tiles via onLoadRange as user scrolls."""
        pass

    def _ensure_tile_worker(self):
        """Create or reconfigure the single persistent tile worker thread."""
        worker = getattr(self, '_tile_worker', None)
        if worker is None or not worker.isRunning():
            if worker:
                worker.shutdown()
                worker.wait(200)
                worker.deleteLater()
            worker = _TileWorker(parent=self)
            worker.tile_ready.connect(self._on_tile_ready)
            self._tile_worker = worker
            worker.start()
        # (Re)configure for current conversation
        raw_path = getattr(self._model._db, '_db_path', None) or ""
        worker.configure(
            raw_path, self._model._ghost_map,
            self._model._tagged_ids, self._model._is_group,
        )
        return worker

    def _on_cancel_pending(self):
        """JS called bridge.onCancelPending() — drain the tile worker queue."""
        worker = getattr(self, '_tile_worker', None)
        if worker:
            worker.clear_pending()
        self._pending_tile_requests = set()

    def _on_tile_ready(self, tile_start: int, raw_msgs: list, gen: int):
        """Tile worker finished loading a tile — push to JS if still relevant."""
        if getattr(self, '_prog_gen', 0) != gen:
            return  # conversation switched, discard
        tile_size = BATCH_SIZE
        tile_key = f"tile_{tile_start // tile_size}"
        pending = getattr(self, '_pending_tile_requests', set())
        pending.discard(tile_key)
        # Mark as loaded — prevents re-fetching the same tile
        loaded = getattr(self, '_loaded_tiles', set())
        loaded.add(tile_key)
        self._loaded_tiles = loaded
        if raw_msgs and self._web_view:
            self._enrich_system_events(raw_msgs)
            # Cache the enriched tile data for instant re-serve
            cache = getattr(self, '_tile_cache', {})
            cache[tile_key] = raw_msgs
            # LRU eviction: keep max 200 tiles (~20K messages)
            if len(cache) > 200:
                oldest = next(iter(cache))
                del cache[oldest]
            self._web_view.set_messages_at(tile_start, raw_msgs)

    def _on_webengine_load_range(self, global_idx: int):
        """On-demand tile load: JS scrolled to an unloaded area.

        Uses a single persistent _TileWorker thread with a task queue.
        No thread-per-tile — enqueue and let the worker handle it serially.
        """
        if not self._model or not self._web_view:
            return

        tile_size = BATCH_SIZE  # 500 — must match JS TILE_SIZE
        tile_idx = global_idx // tile_size
        tile_start = tile_idx * tile_size

        tile_key = f"tile_{tile_idx}"

        # ALREADY LOADED — never reload a tile for the same conversation
        loaded = getattr(self, '_loaded_tiles', set())
        self._loaded_tiles = loaded
        if tile_key in loaded:
            return

        pending = getattr(self, '_pending_tile_requests', set())
        if tile_key in pending:
            return  # already enqueued
        self._pending_tile_requests = pending
        pending.add(tile_key)

        raw_path = getattr(self._model._db, '_db_path', None)
        if not raw_path:
            self._fetch_tile_sync(tile_start, tile_size, tile_key)
            return

        count_to_load = min(tile_size, self._model._total - tile_start)
        if count_to_load <= 0:
            pending.discard(tile_key)
            return

        # Keyset pagination: use anchor table for O(1) seeks instead of O(n) OFFSET
        anchors = getattr(self._model, '_anchors', [])
        if anchors and tile_idx < len(anchors):
            ts, sid = anchors[tile_idx]
            keyset_where = (
                "m.conversation_id = ? AND (m.timestamp > ? OR (m.timestamp = ? AND m.sort_id >= ?))"
            )
            keyset_params = [self._model._conv_id, ts, ts, sid]
            # Add date range filters if active
            if self._model._date_from is not None:
                keyset_where += " AND m.timestamp >= ?"
                keyset_params.append(self._model._date_from)
            if self._model._date_to is not None:
                keyset_where += " AND m.timestamp <= ?"
                keyset_params.append(self._model._date_to)
            sql = self._model._base_sql(keyset_where) + " LIMIT ?"
            full_params = tuple(keyset_params) + (count_to_load,)
        else:
            # Fallback to OFFSET (filters active or anchors not built)
            where, params = self._model._where_clause()
            sql = self._model._base_sql(where) + " LIMIT ? OFFSET ?"
            full_params = tuple(params) + (count_to_load, tile_start)
        gen = getattr(self, '_prog_gen', 0)

        worker = self._ensure_tile_worker()
        worker.enqueue(sql, full_params, tile_start, count_to_load, gen)

    def _fetch_tile_sync(self, tile_start: int, tile_size: int, tile_key: str):
        """Synchronous fallback tile fetch (main thread). Uses cache if available."""
        pending = getattr(self, '_pending_tile_requests', set())

        # Check tile cache first — instant serve, no SQL
        cache = getattr(self, '_tile_cache', {})
        if tile_key in cache:
            cached_msgs = cache[tile_key]
            if cached_msgs and self._web_view:
                self._web_view.set_messages_at(tile_start, cached_msgs)
                loaded = getattr(self, '_loaded_tiles', set())
                loaded.add(tile_key)
                pending.discard(tile_key)
                return

        try:
            # Keyset pagination if anchors available
            anchors = getattr(self._model, '_anchors', [])
            tile_idx = tile_start // tile_size
            if anchors and tile_idx < len(anchors) and not self._model._search_text:
                ts, sid = anchors[tile_idx]
                keyset_where = (
                    "m.conversation_id = ? AND (m.timestamp > ? OR (m.timestamp = ? AND m.sort_id >= ?))"
                )
                keyset_params = [self._model._conv_id, ts, ts, sid]
                count_to_load = min(tile_size, self._model._total - tile_start)
                if count_to_load <= 0:
                    return
                sql = self._model._base_sql(keyset_where) + " LIMIT ?"
                rows = self._model._db.fetchall(sql, tuple(keyset_params) + (count_to_load,))
            else:
                where, params = self._model._where_clause()
                sql = self._model._base_sql(where) + " LIMIT ? OFFSET ?"
                count_to_load = min(tile_size, self._model._total - tile_start)
                if count_to_load <= 0:
                    return
                rows = self._model._db.fetchall(sql, tuple(params) + (count_to_load, tile_start))
            raw_msgs = []
            for row in rows:
                msg = self._model._build_msg_dict(row)
                if (msg["quoted_text"] and msg["display_text"]
                        and msg["quoted_text"].strip() == msg["display_text"].strip()):
                    msg["quoted_text"] = None
                raw_msgs.append(msg)
            if raw_msgs:
                self._model._fetch_auxiliary_batch(self._model._db, raw_msgs)
                self._enrich_system_events(raw_msgs)
                # Cache for instant re-serve
                cache[tile_key] = raw_msgs
                if len(cache) > 200:
                    oldest = next(iter(cache))
                    del cache[oldest]
                if self._web_view:
                    self._web_view.set_messages_at(tile_start, raw_msgs)
        except Exception as e:
            print(f"[WebView] Sync tile fetch error: {e}")
        finally:
            pending.discard(tile_key)

    def _on_webengine_download(self, msg_id_str: str):
        """Handle download request from WebEngine bridge."""
        try:
            msg_id = int(msg_id_str)
        except (ValueError, TypeError):
            return
        row = self._model.find_row_by_msg_id(msg_id)
        if row < 0:
            return
        msg = self._model.get_message_at(row)
        if msg:
            self._download_decrypt_media(msg)

    def _on_vcard_download(self, msg_id_str: str, contact_name: str):
        """Export vCard data as a .vcf file for the given message."""
        import os
        try:
            msg_id = int(msg_id_str)
        except (ValueError, TypeError):
            return
        db = Database.get()
        if not db:
            return
        try:
            rows = db.fetchall(
                "SELECT display_name, phone_numbers FROM message_vcard_data"
                " WHERE message_id = ? ORDER BY vcard_index",
                (msg_id,),
            )
        except Exception:
            rows = []
        if not rows:
            return
        # Build VCF content
        vcf_lines = []
        for display_name, phones in rows:
            vcf_lines.append("BEGIN:VCARD")
            vcf_lines.append("VERSION:3.0")
            vcf_lines.append(f"FN:{display_name or 'Unknown'}")
            if phones:
                for ph in phones.split(","):
                    ph = ph.strip()
                    if ph:
                        vcf_lines.append(f"TEL;TYPE=CELL:{ph}")
            vcf_lines.append("END:VCARD")
        vcf_text = "\r\n".join(vcf_lines) + "\r\n"
        # Save to temp file and open
        import tempfile
        safe_name = "".join(c for c in (contact_name or "contact") if c.isalnum() or c in " _-").strip()[:50]
        tmp_dir = tempfile.gettempdir()
        vcf_path = os.path.join(tmp_dir, f"{safe_name or 'contact'}_{msg_id}.vcf")
        try:
            with open(vcf_path, "w", encoding="utf-8") as f:
                f.write(vcf_text)
            # Open with default handler (Contacts app)
            os.startfile(vcf_path)
        except Exception as e:
            print(f"[ChatViewerPage] VCF export error: {e}")

    def _enrich_system_events(self, messages: list) -> None:
        """Re-compute system event display_text with owner info for forensic labeling."""
        from shared.system_event_formatter import build_system_text
        _o_phone = getattr(self, '_owner_phone', '') or ''
        _o_name = getattr(self, '_owner_name', '') or ''
        _o_cid = getattr(self, '_owner_cid', -1) or -1
        _conv_name = getattr(self, '_conv_name', '') or ''
        _chat_type = 'group' if getattr(self, '_is_group', False) else 'personal'
        for msg in messages:
            mt = msg.get("message_type")
            if mt == 7 or mt == 112:
                try:
                    msg["display_text"] = build_system_text(
                        msg,
                        owner_phone=_o_phone,
                        owner_name=_o_name,
                        owner_contact_id=_o_cid,
                        conv_name=_conv_name,
                        chat_type=_chat_type,
                    )
                except Exception:
                    # Fallback to pre-rendered text
                    rst = msg.get("rendered_system_text")
                    if rst:
                        msg["display_text"] = rst

    def _send_messages_to_webview(self):
        """Send current model data to WebEngine for rendering (tile-based virtual scroll).

        Architecture: Only sends the LAST tile (most recent messages).
        JS renders them immediately, then requests more tiles on-demand as
        user scrolls. No progressive loading — purely on-demand.
        """
        print(f"[WebView] _send_messages_to_webview called: web_view={self._web_view is not None}, "
              f"total={self._model._total}, raw_count={len(self._model._raw_data)}, "
              f"loaded_start={self._model._loaded_start}")
        if not self._web_view:
            return
        t0 = _time.perf_counter()
        # New renderer generation first: any queued runJavaScript payloads
        # from the previous chat will be ignored by JS after this point.
        self._prog_gen = getattr(self, '_prog_gen', 0) + 1
        _send_gen = self._prog_gen
        self._web_view.set_generation(_send_gen)
        self._web_view.clear()
        _on = getattr(self, '_owner_name', '') or ''
        _op = getattr(self, '_owner_phone', '') or ''
        _ol = f"{_on} (+{_op})" if _on and _op else _on or ''
        self._web_view.set_config(self._is_group, owner_label=_ol)
        # Push the analyst's selected timezone into the JS
        # renderer so day dividers + bubble timestamps +
        # forensic-info timestamps all format in the case
        # timezone instead of the host machine's local timezone.
        try:
            from app.config import get_timezone_name
            self._web_view.set_timezone(get_timezone_name() or "")
        except Exception:
            pass

        # ============================================================
        # PHASE 1 — resolve the target message we want to land on.
        #
        # Three possible sources, in priority order:
        #   1. An explicit caller-set ``_target_msg_id`` (search hit, quote
        #      jump, "scroll to message" from another page).
        #   2. The first-unread message, when this chat has unread messages
        #      and no explicit target was supplied.  Matches WhatsApp's
        #      native UX: opening a chat with N unread lands on the divider.
        #   3. None — fall back to "scroll to bottom of last tile".
        #
        # This block MUST run BEFORE ``set_total_count`` because
        # the JS init render checks ``_pendingScrollMsgId``; if
        # we announce total count first JS launches
        # ``requestVisibleTiles`` for tiles 0..2 from the top
        # and the user briefly sees an empty conversation start.
        # ============================================================
        _tmid = 0
        _target_tile_start = -1
        first_unread_id = 0

        # 1. Explicit caller-set target wins.
        # Album-child redirect: if the user clicked "Go to Chat"
        # on an individual album photo (these messages are
        # hidden from the chat stream because they're rendered
        # inside the parent bubble's grid), scrolling to the
        # hidden row would land on a 0-height ghost.  Instead,
        # redirect to the album parent and tell the renderer
        # which child cell to pulse so the user can identify
        # which photo within the grid they came from.
        _album_highlight = None  # (parent_msg_id, child_msg_id, position-1based)
        if self._target_msg_id and self._target_msg_id > 0:
            try:
                _db_albk = Database.get()
                _albk_row = _db_albk.fetchone(
                    "SELECT a.parent_message_id, a.sort_order "
                    "FROM message_association a "
                    "WHERE a.child_message_id = ? AND a.association_type = 2",
                    (self._target_msg_id,),
                )
                if _albk_row:
                    _parent_id = int(_albk_row[0])
                    _sort_ord = int(_albk_row[1] or 0)
                    _album_highlight = (_parent_id, int(self._target_msg_id), _sort_ord + 1)
                    print(f"[WebView] go-to-chat: msg {self._target_msg_id} is album child of "
                          f"parent {_parent_id} at position {_sort_ord + 1}; redirecting scroll target")
                    self._target_msg_id = _parent_id
            except Exception as e:
                print(f"[WebView] album-child redirect lookup failed: {e}")
        # Stash for the post-load JS notify
        self._pending_album_highlight = _album_highlight

        if self._target_msg_id and self._target_msg_id > 0:
            _tmid = self._target_msg_id
            self._target_msg_id = 0
            try:
                db = Database.get()
                where, params = self._model._where_clause()
                target_gi = db.scalar(
                    f"SELECT COUNT(*) FROM message m WHERE {where}"
                    " AND (m.timestamp, m.sort_id) < (SELECT timestamp, sort_id FROM message WHERE id = ?)",
                    tuple(params) + (_tmid,),
                )
                print(f"[WebView] scroll-to-msg: id={_tmid}, global_idx={target_gi}, total={self._model._total}")
                if target_gi is not None and target_gi >= 0:
                    target_tile_idx = target_gi // BATCH_SIZE
                    _target_tile_start = target_tile_idx * BATCH_SIZE
            except Exception as e:
                print(f"[WebView] target tile index lookup failed: {e}")

        # 2. Look up first-unread regardless (JS needs it for the divider),
        #    and use it as the scroll target when no explicit target is set.
        #    Source: WhatsApp's own ``chat.unseen_message_count`` (mirrored
        #    on ``conversation.source_unseen_count``) — that's the
        #    authoritative counter, exactly what the native app shows.
        try:
            db_unread = Database.get()
            _unseen_row = db_unread.fetchone(
                "SELECT source_unseen_count FROM conversation WHERE id = ?",
                (self._conv_id,),
            )
            unseen_count = None
            if _unseen_row and _unseen_row[0] is not None:
                try:
                    unseen_count = int(_unseen_row[0])
                except (TypeError, ValueError):
                    unseen_count = None
            if unseen_count and unseen_count > 0:
                # Exclude system events (type 7 / 112), date separators
                # (-1), call logs (90), and ephemeral notices — none of
                # those count towards WhatsApp's own unseen_message_count.
                _fu_row = db_unread.fetchone(
                    "SELECT id FROM message "
                    "WHERE conversation_id = ? AND from_me = 0 "
                    "  AND message_type NOT IN (-1, 7, 90, 112) "
                    "ORDER BY timestamp DESC, sort_id DESC "
                    "LIMIT 1 OFFSET ?",
                    (self._conv_id, unseen_count - 1),
                )
                if _fu_row and _fu_row[0]:
                    first_unread_id = int(_fu_row[0])

            # Auto-scroll target = first unread (only if no
            # explicit target AND the first-unread won't already
            # be visible in the default scroll-to-bottom view).
            # If the first-unread is within the initial render
            # window (the last ~BATCH_SIZE messages), the natural
            # scroll-to-end behaviour already places the divider
            # on screen — auto-jumping with placement='start'
            # would instead pin the divider to the very top,
            # leaving an empty area below.
            #
            # Placement is 'start' for the first-unread auto-jump
            # so the divider lands at the top of the viewport.
            # For an explicit target (search click / quote jump)
            # it stays 'center'.
            _scroll_placement = "center"
            # Suppress the first-unread auto-jump when any chat-level
            # filter is active — the unread message id comes from the
            # un-filtered conversation, so it almost certainly isn't in
            # the filtered tile set.  The renderer would then receive
            # a scroll target it can't resolve, sit pending, and paint
            # an empty chat (the "blank when filter on" symptom).
            _filters_active = bool(
                self._model._search_text
                or self._model._date_from is not None
                or self._model._date_to is not None
                or self._model._ghost_only
                or self._model._missing_media_only
                or self._model._sender_filter_id is not None
            )
            if first_unread_id and not _tmid and not _filters_active:
                # Skip auto-jump if the first-unread is already inside the
                # default initial render window.  ``loaded_start`` is the
                # global index where the default fetch began — the initial
                # tile renders [loaded_start .. total-1], so anything at or
                # past loaded_start is already visible without scrolling.
                _default_loaded_start = self._model._loaded_start
                try:
                    where2, params2 = self._model._where_clause()
                    target_gi = db_unread.scalar(
                        f"SELECT COUNT(*) FROM message m WHERE {where2}"
                        " AND (m.timestamp, m.sort_id) < "
                        "(SELECT timestamp, sort_id FROM message WHERE id = ?)",
                        tuple(params2) + (_tmid_check_id := first_unread_id,),
                    )
                except Exception as _e:
                    target_gi = None
                    print(f"[WebView] first-unread tile lookup failed: {_e}")

                if (target_gi is not None
                        and target_gi >= _default_loaded_start
                        and target_gi < self._model._total):
                    # First-unread is already in the default-loaded window —
                    # let scroll-to-bottom render it naturally.  The unread
                    # divider will still draw above its message because
                    # set_first_unread_msg_id was already pushed to JS.
                    print(f"[WebView] First-unread (gi={target_gi}) already in "
                          f"default render window (start={_default_loaded_start}, "
                          f"total={self._model._total}); skipping auto-jump")
                else:
                    _tmid = first_unread_id
                    _scroll_placement = "start"
                    if target_gi is not None and target_gi >= 0:
                        target_tile_idx = target_gi // BATCH_SIZE
                        _target_tile_start = target_tile_idx * BATCH_SIZE
                        print(f"[WebView] Auto-jump to first unread: id={_tmid}, "
                              f"global_idx={target_gi}, tile_start={_target_tile_start}")
        except Exception as e:
            print(f"[WebView] first-unread lookup failed: {e}")

        # ============================================================
        # PHASE 2 — announce target + count to JS, in the required order.
        #
        # ORDER MATTERS:
        #   set_pending_scroll(...) → set_total_count(...) → set_first_unread(...)
        #
        # JS's renderVisible / requestVisibleTiles both check
        # _pendingScrollMsgId and bail out when it's set, so flagging the
        # pending scroll BEFORE total_count suppresses the scrollTop=0
        # render that would otherwise paint empty tiles starting from the
        # top of the conversation.
        # ============================================================
        if _tmid:
            self._web_view.set_pending_scroll(_tmid)

        # Set total count — creates spacer for full scrollbar range.
        self._web_view.set_total_count(self._model._total)

        # Tell JS which message gets the "Unread messages" divider above
        # it.  Safe to call after total_count: this only annotates a
        # bubble at render time, it doesn't affect the initial scroll.
        self._web_view.set_first_unread_msg_id(first_unread_id)

        # Cancel previous tile requests + drain worker queue
        self._pending_tile_requests = set()
        self._loaded_tiles = set()  # reset for new conversation
        self._tile_cache = {}       # tile_key → raw_msgs list (LRU, max 200 tiles = 20K msgs)
        worker = getattr(self, '_tile_worker', None)
        if worker:
            worker.clear_pending()

        # ---- FAST PATH: preload the entire conversation when it's small enough ----
        # Eliminates per-tile Python round-trips so scroll feels instant (matches
        # the exported-bundle viewer's behaviour).
        if (not _tmid
                and self._model._total > 0
                and self._model._total <= PRELOAD_ALL_THRESHOLD
                and not self._model._search_text
                and not self._model._ghost_only
                and not self._model._missing_media_only
                and self._model._sender_filter_id is None):
            t_pre = _time.perf_counter()
            ok = self._preload_all_and_push()
            if ok:
                t_done = _time.perf_counter()
                print(f"[WebView] PRELOAD-ALL mode: {self._model._total:,} msgs "
                      f"pushed in {t_done-t_pre:.2f}s \u2014 no tile loads needed")
                return
            # Preload failed (OOM, SQL error) — fall through to tile-based load.

        if _tmid and _target_tile_start >= 0:
            # ---- Target-message path: load target tile as the FIRST tile ----
            target_tile_idx = _target_tile_start // BATCH_SIZE
            target_tile_key = f"tile_{target_tile_idx}"
            self._fetch_tile_sync(_target_tile_start, BATCH_SIZE, target_tile_key)
            self._loaded_tiles.add(target_tile_key)
            t1 = _time.perf_counter()
            print(f"[WebView] Target tile loaded: tile={target_tile_key}, "
                  f"start={_target_tile_start} | setup={t1-t0:.3f}s")

            # Also send the default last tile if it's different, so scrollbar
            # range is accurate and user can scroll to bottom later
            if self._model._raw_data:
                initial_tile_idx = self._model._loaded_start // BATCH_SIZE
                initial_tile_key = f"tile_{initial_tile_idx}"
                if initial_tile_key != target_tile_key:
                    self._enrich_system_events(self._model._raw_data)
                    self._loaded_tiles.add(initial_tile_key)
                    self._web_view.set_messages_at(
                        self._model._loaded_start, self._model._raw_data
                    )

            # Load adjacent tiles for scroll correction context
            for adj in [target_tile_idx - 1, target_tile_idx + 1]:
                adj_key = f"tile_{adj}"
                adj_start = adj * BATCH_SIZE
                if adj >= 0 and adj_start < self._model._total and adj_key not in self._loaded_tiles:
                    self._fetch_tile_sync(adj_start, BATCH_SIZE, adj_key)
                    self._loaded_tiles.add(adj_key)

            # Scroll after a short delay to let JS process the tile data.
            # Use a longer delay for large conversations to allow JS rendering.
            _scroll_delay = 300 if self._model._total > 10000 else 200
            _place = _scroll_placement
            QTimer.singleShot(
                _scroll_delay,
                lambda gen=_send_gen, msg_id=_tmid, p=_place: (
                    self._web_view.scroll_to_message(msg_id, placement=p)
                    if getattr(self, '_prog_gen', 0) == gen else None
                ),
            )
            # Retry scroll in case the first attempt arrives before JS is ready
            QTimer.singleShot(
                _scroll_delay + 400,
                lambda gen=_send_gen, msg_id=_tmid, p=_place: (
                    self._web_view.scroll_to_message(msg_id, placement=p)
                    if getattr(self, '_prog_gen', 0) == gen else None
                ),
            )
            # If the user came from a media-gallery click on an ALBUM CHILD,
            # the scroll target above is the album PARENT.  Pulse the
            # specific child cell inside the rendered grid so the user
            # sees which photo (e.g. "1/2", "47/100") was the source.
            _albk = getattr(self, '_pending_album_highlight', None)
            if _albk:
                _alb_pid, _alb_cid, _alb_pos = _albk
                self._pending_album_highlight = None
                QTimer.singleShot(
                    _scroll_delay + 600,
                    lambda gen=_send_gen, p=_alb_pid, c=_alb_cid, pos=_alb_pos: (
                        self._web_view.highlight_album_child(p, c, pos)
                        if getattr(self, '_prog_gen', 0) == gen else None
                    ),
                )
        elif self._model._raw_data:
            # ---- Default path: load last tile (most recent messages) ----
            self._enrich_system_events(self._model._raw_data)
            t1 = _time.perf_counter()
            initial_tile_idx = self._model._loaded_start // BATCH_SIZE
            self._loaded_tiles.add(f"tile_{initial_tile_idx}")
            self._web_view.set_messages_at(
                self._model._loaded_start, self._model._raw_data
            )
            t2 = _time.perf_counter()
            print(f"[WebView] Initial tile: {len(self._model._raw_data)} msgs at offset "
                  f"{self._model._loaded_start}, total={self._model._total} | "
                  f"set_messages_at={t2-t1:.3f}s, setup={t1-t0:.3f}s")

        print(f"[WebView] Tile-based on-demand mode: total={self._model._total}, "
              f"tiles will load as user scrolls")

    def _preload_all_and_push(self) -> bool:
        """Load every message of the current conversation in one pass and push to JS.

        Used for conversations <= PRELOAD_ALL_THRESHOLD messages. Avoids the
        tile-based round-trip loop entirely, so scrolling is instant after the
        initial load — no 'waiting' feel on mid-chat navigation.

        Returns True on success; False if the load errored (caller falls back
        to tile-based mode).
        """
        try:
            t0 = _time.perf_counter()
            where, params = self._model._where_clause()
            sql = self._model._base_sql(where) + " LIMIT ?"
            rows = self._model._db.fetchall(sql, tuple(params) + (self._model._total + 10,))
            t1 = _time.perf_counter()

            raw_msgs = []
            for row in rows:
                msg = self._model._build_msg_dict(row)
                if (msg["quoted_text"] and msg["display_text"]
                        and msg["quoted_text"].strip() == msg["display_text"].strip()):
                    msg["quoted_text"] = None
                raw_msgs.append(msg)
            t2 = _time.perf_counter()

            if not raw_msgs:
                return False

            # Enrich auxiliary data (reactions, polls, mentions, etc.) in one batch
            try:
                self._model._fetch_auxiliary_batch(self._model._db, raw_msgs)
            except Exception as e:
                print(f"[WebView] preload-all aux enrichment warning: {e}")
            self._enrich_system_events(raw_msgs)
            t3 = _time.perf_counter()

            # Push in ONE call so JS sees all content at once.
            # Mark every tile as "loaded" so the tile-fetch machinery never fires.
            self._web_view.set_messages_at(0, raw_msgs)
            last_tile = (self._model._total - 1) // BATCH_SIZE if self._model._total else 0
            for ti in range(last_tile + 1):
                self._loaded_tiles.add(f"tile_{ti}")
            # Also populate the per-tile cache so re-queries / navigation don't
            # rebuild from scratch.
            cache = getattr(self, '_tile_cache', {})
            for ti in range(last_tile + 1):
                tile_start = ti * BATCH_SIZE
                tile_end = min(tile_start + BATCH_SIZE, len(raw_msgs))
                if tile_start < len(raw_msgs):
                    cache[f"tile_{ti}"] = raw_msgs[tile_start:tile_end]
            # IMPORTANT: do NOT mutate self._model._raw_data or
            # self._model._loaded_start here. The rest of the app (media
            # gallery, replies sidebar, pinned panel, find_row_by_msg_id,
            # filters) assumes those point at the last-tile snapshot. Bulk-
            # replacing them broke quote navigation, calls-to-msg, and the
            # in-chat media side-panel. The preload-all optimisation is
            # strictly a JS-side delivery shortcut.
            t4 = _time.perf_counter()
            print(f"[WebView] preload-all breakdown: SQL={t1-t0:.2f}s, "
                  f"build_dicts={t2-t1:.2f}s, aux+enrich={t3-t2:.2f}s, "
                  f"push={t4-t3:.2f}s, TOTAL={t4-t0:.2f}s")
            return True
        except Exception as e:
            print(f"[WebView] preload-all failed, falling back to tiles: {e}")
            import traceback; traceback.print_exc()
            return False

    def _start_next_bg_batch(self, gen=None):
        """No-op — progressive loading is DISABLED.

        Tile-based on-demand loading handles everything via
        _on_webengine_load_range(). This method is kept as a stub
        so existing call sites don't crash.
        """
        pass

    # ---- Cursor feedback for interactive bubble regions ----

    def eventFilter(self, obj, event):
        if not self._list:
            return super().eventFilter(obj, event)
        if obj is self._list.viewport():
            if event.type() == QEvent.Type.MouseMove:
                # Skip expensive hit-testing during scroll
                if self._delegate._scrolling:
                    return super().eventFilter(obj, event)
                pos = event.pos()
                index = self._list.indexAt(pos)
                if index.isValid():
                    row = index.row()
                    if self._delegate.is_interactive_at(row, pos):
                        self._list.viewport().setCursor(Qt.PointingHandCursor)
                    else:
                        self._list.viewport().setCursor(Qt.IBeamCursor)
                else:
                    self._list.viewport().setCursor(Qt.ArrowCursor)
        return super().eventFilter(obj, event)

    # ---- Keyboard shortcuts ----

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key_Escape:
            # If we have a quote navigation stack, go back there first
            if hasattr(self, "_nav_back_stack") and self._nav_back_stack:
                self._navigate_back()
                return
            self.back_requested.emit()
            return
        if key == Qt.Key_Left and mods & Qt.AltModifier:
            self._navigate_back()
            return
        if key == Qt.Key_C and mods & Qt.ControlModifier:
            self._copy_selected_message()
            return
        if key == Qt.Key_F and mods & Qt.ControlModifier:
            self._toggle_search()
            return
        if key == Qt.Key_Home and mods & Qt.ControlModifier:
            self._scroll_to_first()
            return
        if key == Qt.Key_End and mods & Qt.ControlModifier:
            self._scroll_to_last()
            return
        if key == Qt.Key_Plus and mods & Qt.ControlModifier:
            self._font_increase()
            return
        if key == Qt.Key_Minus and mods & Qt.ControlModifier:
            self._font_decrease()
            return
        super().keyPressEvent(event)

    def _copy_selected_message(self):
        """Copy the currently selected message text to clipboard (Ctrl+C)."""
        if self._use_webengine:
            # WebEngine: browser handles Ctrl+C natively via text selection
            return
        if not self._list:
            return
        idx = self._list.currentIndex()
        if not idx.isValid():
            return
        msg = idx.data(MSG_DATA_ROLE)
        if not msg or msg.get("message_type") == -1:
            return
        text = msg.get("display_text", "")
        if text:
            QApplication.clipboard().setText(text)
            self._show_copy_toast("Message copied!")

    # ---- Font size ----

    def _font_increase(self):
        self._font_size = min(18, self._font_size + 1)
        if self._use_webengine:
            self._web_view.set_font_size(self._font_size)
        else:
            self._delegate.set_font_size(self._font_size)
            self._list.viewport().update()

    def _font_decrease(self):
        self._font_size = max(8, self._font_size - 1)
        if self._use_webengine:
            self._web_view.set_font_size(self._font_size)
        else:
            self._delegate.set_font_size(self._font_size)
            self._list.viewport().update()

    # ---- Toast overlay ----

    def _show_copy_toast(self, text: str):
        """Show a brief floating toast message near the bottom of the chat."""
        toast = QLabel(text, self)
        toast.setAlignment(Qt.AlignCenter)
        _lt = self._tm.is_light
        toast.setStyleSheet(
            "QLabel { background: rgba(0,0,0,0.75); color: white; "
            "border-radius: 12px; padding: 6px 16px; font-size: 11px; }"
            if _lt else
            "QLabel { background: rgba(255,255,255,0.15); color: #e9edef; "
            "border-radius: 12px; padding: 6px 16px; font-size: 11px; }"
        )
        toast.adjustSize()
        toast.move((self.width() - toast.width()) // 2, self.height() - 80)
        toast.show()
        toast.raise_()
        QTimer.singleShot(1500, toast.deleteLater)

    # ---- Scroll navigation ----

    def _scroll_to_first(self):
        """Load all older messages and scroll to the very first message."""
        if self._use_webengine:
            # With virtual scroll, just scroll to top — spacer already has full height
            self._web_view.page().runJavaScript("chatArea.scrollTop = 0;")
            return
        while self._model.has_older():
            self._model.fetch_older()
        if self._model.rowCount() > 0:
            self._list.scrollToTop()

    def _scroll_to_last(self):
        if self._use_webengine:
            self._web_view.page().runJavaScript(
                "chatArea.scrollTop = chatArea.scrollHeight;"
            )
            return
        if self._model.rowCount() > 0:
            self._list.scrollToBottom()

    def _scroll_to_first_unread(self):
        """Jump to the first unread message in this conversation.

        Uses WhatsApp's authoritative ``conversation.source_unseen_count``
        (mirrored from ``chat.unseen_message_count``) — that's exactly the
        blue "N unread messages" pill the native app shows.  If unseen
        count is N, the first-unread is the Nth-from-end received chat
        message, excluding non-chat types (date separators, system
        events, call logs, privacy notifications) which the native app
        also doesn't include in its unread tally.

        Note: ``status < 6`` is *not* a safe unread filter — values
        of 0/4/5 also appear on stale receipts of older messages,
        so a status-based query would land on the very first
        received message rather than the start of the unread
        stack.  We query against ``unseen_message_count`` instead.
        """
        if not getattr(self, "_conv_id", None):
            return
        from app.services.database import Database
        db = Database.get()
        unseen_row = db.fetchone(
            "SELECT source_unseen_count FROM conversation WHERE id = ?",
            (self._conv_id,),
        )
        unseen_count = None
        if unseen_row and unseen_row[0] is not None:
            try:
                unseen_count = int(unseen_row[0])
            except (TypeError, ValueError):
                unseen_count = None
        if not unseen_count or unseen_count <= 0:
            try:
                from PySide6.QtWidgets import QToolTip
                from PySide6.QtGui import QCursor
                QToolTip.showText(QCursor.pos(),
                                  "No unread messages in this conversation")
            except Exception:
                pass
            return
        row = db.fetchone(
            "SELECT id FROM message "
            "WHERE conversation_id = ? AND from_me = 0 "
            "  AND message_type NOT IN (-1, 7, 90, 112) "
            "ORDER BY timestamp DESC, sort_id DESC "
            "LIMIT 1 OFFSET ?",
            (self._conv_id, unseen_count - 1),
        )
        if not row or not row[0]:
            # Nothing unread — show a brief status-bar-style hint
            try:
                from PySide6.QtWidgets import QToolTip
                from PySide6.QtGui import QCursor
                QToolTip.showText(QCursor.pos(),
                                  "No unread messages in this conversation")
            except Exception:
                pass
            return
        target_msg_id = int(row[0])
        if self._use_webengine:
            # Use the same scrollToMessage path search results use — the
            # scroll-settle watchdog will keep the target centered as tiles
            # continue to arrive, and the target gets the pulse highlight.
            self._web_view.scroll_to_message(target_msg_id)
            return
        # QPainter fallback
        try:
            gi = self._model.find_row_by_msg_id(target_msg_id)
            if gi is not None and gi >= 0:
                idx = self._model.index(gi, 0)
                self._list.scrollTo(idx, self._list.PositionAtCenter)
        except Exception:
            pass

    def _on_scroll_combined(self, value):
        """Single handler for all scroll events — reduces signal overhead."""
        if self._use_webengine:
            return  # WebEngine handles scroll internally via JS
        self._on_scroll(value)
        self._check_load_older(value)
        self._on_scroll_activity()

    def _check_load_older(self, value):
        """When user scrolls near the top, start async prefetch of older messages."""
        if self._use_webengine or not self._list:
            return  # WebEngine handles scroll-top via JS IntersectionObserver
        if self._loading_older or not self._model.has_older():
            return
        scrollbar = self._list.verticalScrollBar()
        # Trigger when within 30% of the top (earlier = smoother scrolling)
        if scrollbar.maximum() > 0 and value < scrollbar.maximum() * PREFETCH_THRESHOLD:
            self._loading_older = True
            # Remember scroll anchor and position
            first_visible = self._list.indexAt(QPoint(0, 0))
            self._prefetch_anchor_row = first_visible.row() if first_visible.isValid() else 0
            self._prefetch_scroll_pos = self._list.verticalScrollBar().value()

            # Try async prefetch first
            if not self._model.start_prefetch():
                # Fallback to sync
                prepended = self._model.fetch_older()
                if prepended > 0:
                    new_row = self._prefetch_anchor_row + prepended
                    idx = self._model.index(new_row, 0)
                    self._list.scrollTo(idx, QAbstractItemView.PositionAtTop)
                self._loading_older = False

    def _on_async_prefetch_done(self, count: int):
        """Handle completion of async prefetch — restore scroll position."""
        if self._use_webengine:
            # Tile-based on-demand loading handles WebEngine.
            # This callback is only used for the QListView fallback path.
            self._loading_older = False
            return
        if count > 0 and hasattr(self, '_prefetch_anchor_row') and self._list:
            # Only restore position if user hasn't scrolled significantly since prefetch started
            current_pos = self._list.verticalScrollBar().value()
            anchor_pos = getattr(self, '_prefetch_scroll_pos', current_pos)
            if abs(current_pos - anchor_pos) < 200:  # user hasn't moved much
                new_row = self._prefetch_anchor_row + count
                if 0 <= new_row < len(self._model._data):
                    idx = self._model.index(new_row, 0)
                    # Use scrollTo to position the anchor row at the top
                    self._list.scrollTo(idx, QAbstractItemView.PositionAtTop)
        self._loading_older = False

    # ---- Scroll date overlay ----

    def _on_scroll(self, value):
        if self._use_webengine or not self._list:
            return
        # Throttle: skip if last overlay update was <150ms ago (was 80ms, too frequent)
        now = datetime.now().timestamp()
        if hasattr(self, '_last_date_overlay_ts') and now - self._last_date_overlay_ts < 0.15:
            self._date_overlay_timer.start()
            return
        self._last_date_overlay_ts = now

        vp = self._list.viewport()
        center = QPoint(vp.width() // 2, vp.height() // 3)
        idx = self._list.indexAt(center)
        if idx.isValid():
            msg = idx.data(MSG_DATA_ROLE)
            if msg and msg.get("timestamp"):
                try:
                    ts = msg["timestamp"]
                    dt_str = format_timestamp(ts, '%B %d, %Y')
                    self._date_overlay.setText(f"  {dt_str}  ")
                    self._date_overlay.adjustSize()
                    ox = (vp.width() - self._date_overlay.width()) // 2
                    self._date_overlay.move(ox, 6)
                    self._date_overlay.setVisible(True)
                    self._date_overlay.raise_()
                    self._date_overlay_timer.start()
                except (ValueError, OSError):
                    pass

    def _on_scroll_activity(self, _value=None):
        """Called on every scroll event — pause sticker animations."""
        if self._use_webengine:
            return
        if not self._delegate._scrolling:
            self._delegate.on_scroll_start()
        self._scroll_idle_timer.start()  # restart 200ms idle timer

    def _on_scroll_idle(self):
        """Called 200ms after scrolling stops — resume sticker animations."""
        if self._use_webengine:
            return
        self._delegate.on_scroll_stop()

    # ---- Quote navigation ----

    def _navigate_to_quoted(self, key_id: str):
        if self._use_webengine:
            # WebEngine: scroll via JS
            self._web_view.scroll_to_key(key_id)
            return
        row = self._model.find_row_by_key_id(key_id)
        if row >= 0:
            idx = self._model.index(row, 0)
            # Push current position to back-stack before jumping
            cur = self._list.currentIndex()
            if cur.isValid() and cur.row() != row:
                if not hasattr(self, "_nav_back_stack"):
                    self._nav_back_stack = []
                self._nav_back_stack.append(cur.row())
            self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
            self._list.setCurrentIndex(idx)
            self._on_item_clicked(idx)
        else:
            # Cross-chat: quoted message is in another conversation
            self._try_cross_chat_quote(key_id)

    def _navigate_back(self):
        """Go back to the message user was viewing before jumping to a quote."""
        if not hasattr(self, "_nav_back_stack") or not self._nav_back_stack:
            return
        prev_row = self._nav_back_stack.pop()
        if 0 <= prev_row < self._model.rowCount():
            idx = self._model.index(prev_row, 0)
            self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
            self._list.setCurrentIndex(idx)
            self._on_item_clicked(idx)

    def _try_cross_chat_quote(self, key_id: str):
        """Try to find a quoted message in a different conversation."""
        try:
            row = self._db.fetchone(
                "SELECT m.id, m.conversation_id, m.text_content, m.type_label, m.from_me,"
                " COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name"
                " FROM message m"
                " LEFT JOIN conversation conv ON conv.id = m.conversation_id"
                " WHERE m.source_key_id = ? LIMIT 1",
                (key_id,),
            )
            if row:
                conv_id = row["conversation_id"]
                msg_id = row["id"]
                conv_name = row["conv_name"]
                text = (row["text_content"] or "")[:60]
                tl = row["type_label"] or "message"
                direction = "Sent" if row["from_me"] else "Received"
                from PySide6.QtWidgets import QMessageBox
                reply = QMessageBox.question(
                    self, "Quoted Message in Another Chat",
                    f"This quote points to a {tl} in:\n"
                    f"\U0001F4AC {conv_name}\n"
                    f"{direction}: {text}{'…' if len(row['text_content'] or '') > 60 else ''}\n\n"
                    f"Jump to that conversation?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self.conversation_switch_requested.emit(conv_id, msg_id)
        except Exception:
            pass

    def _show_replies_panel(self, source_key_id: str):
        """Show a popup listing all replies to the given message, with navigation."""
        try:
            replies = self._db.fetchall(
                "SELECT m.id, m.text_content, m.type_label, m.from_me, m.timestamp,"
                " m.source_key_id, m.conversation_id,"
                " COALESCE(c.resolved_name, c.display_name, c.phone_number, c.wa_name, 'Unknown') AS sender,"
                " COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name"
                " FROM message m"
                " LEFT JOIN contact c ON c.id = m.sender_id"
                " LEFT JOIN conversation conv ON conv.id = m.conversation_id"
                " WHERE m.reply_to_key_id = ?"
                " ORDER BY m.timestamp ASC",
                (source_key_id,),
            )
            if not replies:
                return

            from PySide6.QtWidgets import QDialog, QListWidget, QListWidgetItem
            from PySide6.QtGui import QFont as _QFont
            from app.config import format_timestamp

            dlg = QDialog(self)
            dlg.setWindowTitle(f"Replies ({len(replies)})")
            dlg.setMinimumSize(480, 340)
            dl = QVBoxLayout(dlg)
            dl.setContentsMargins(8, 8, 8, 8)
            dl.setSpacing(6)

            info = QLabel(f"\u21B3 {len(replies)} replies to this message")
            info.setStyleSheet("font-size: 12px; font-weight: bold; color: #00897b;")
            dl.addWidget(info)

            lst = QListWidget()
            lst.setStyleSheet(
                "QListWidget { border: 1px solid rgba(128,128,128,0.2); border-radius: 6px; }"
                " QListWidget::item { padding: 6px; border-bottom: 1px solid rgba(128,128,128,0.1); }"
                " QListWidget::item:selected { background: rgba(0,137,123,0.12); }"
            )

            for r in replies:
                ts_str = format_timestamp(r["timestamp"], "short") if r["timestamp"] else ""
                sender = "You" if r["from_me"] else r["sender"]
                text = (r["text_content"] or "").replace("\n", " ")[:80]
                tl = r["type_label"] or ""
                if not text and tl:
                    text = f"[{tl.replace('_', ' ').title()}]"
                conv_name = r["conv_name"] or ""
                same_chat = r["conversation_id"] == self._conv_id

                display = f"{sender}  •  {ts_str}\n{text}"
                if not same_chat:
                    display += f"\n\U0001F4AC {conv_name}"

                item = QListWidgetItem(display)
                item.setData(Qt.UserRole, {
                    "msg_id": r["id"],
                    "conv_id": r["conversation_id"],
                    "source_key_id": r["source_key_id"],
                    "same_chat": same_chat,
                })
                f = _QFont()
                f.setPointSize(9)
                item.setFont(f)
                if not same_chat:
                    item.setForeground(QColor("#e040fb")) # purple for cross-chat
                lst.addItem(item)

            dl.addWidget(lst, 1)

            # Back button
            back_btn = QPushButton("\u2190 Back to original message")
            back_btn.setStyleSheet(
                "QPushButton { padding: 6px 14px; border-radius: 6px;"
                " border: 1px solid rgba(128,128,128,0.2); font-size: 10px; }"
                " QPushButton:hover { background: rgba(0,137,123,0.1); }"
            )
            back_btn.clicked.connect(lambda: (dlg.close(), self._navigate_back()))
            dl.addWidget(back_btn)

            def on_reply_clicked(item):
                data = item.data(Qt.UserRole)
                if not data:
                    return
                if data["same_chat"]:
                    # Same chat: scroll to the reply
                    row = self._model.find_row_by_key_id(data["source_key_id"])
                    if row >= 0:
                        idx = self._model.index(row, 0)
                        self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                        self._list.setCurrentIndex(idx)
                        self._on_item_clicked(idx)
                else:
                    # Cross-chat: navigate to other conversation
                    dlg.close()
                    self.go_to_chat.emit(data["conv_id"], data["msg_id"])

            lst.itemDoubleClicked.connect(on_reply_clicked)
            dlg.show()

        except Exception as e:
            print(f"[ChatViewer] Show replies error: {e}")

    def _show_edit_history(self, message_id: int):
        """Show full edit forensic comparison: original text (FTS), intermediate
        versions (from quotes), current text, deletion state, dual receipt
        timelines, and forensic metadata."""
        try:
            from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                           QLabel, QTextEdit, QFrame, QPushButton,
                                           QScrollArea, QWidget)
            from PySide6.QtGui import QFont as _QFont
            from app.config import format_timestamp

            db = self._db

            # ── Fetch message data ──
            msg_row = db.fetchone(
                "SELECT m.text_content, m.timestamp, m.from_me, m.last_edit_timestamp,"
                " m.edit_count, m.source_key_id, m.original_key_id,"
                " m.received_timestamp, m.receipt_server_timestamp,"
                " m.first_delivered_ts, m.first_read_ts,"
                " m.is_revoked, m.revoke_timestamp,"
                " COALESCE(c.resolved_name, c.display_name, c.wa_name, 'Unknown') AS sender"
                " FROM message m LEFT JOIN contact c ON c.id = m.sender_id"
                " WHERE m.id = ?",
                (message_id,),
            )
            if not msg_row:
                return

            current_text = msg_row["text_content"] or ""
            sent_ts = msg_row["timestamp"]
            edit_ts = msg_row["last_edit_timestamp"]
            from_me = msg_row["from_me"]
            sender = msg_row["sender"] if not from_me else "You"
            current_key = msg_row["source_key_id"] or ""
            original_key = msg_row["original_key_id"] or ""
            is_revoked = msg_row["is_revoked"]
            revoke_ts = msg_row["revoke_timestamp"]

            # Get edit history (FTS original text)
            edit_row = db.fetchone(
                "SELECT original_key_id, edited_timestamp, sender_timestamp, original_text"
                " FROM edit_history WHERE message_id = ? ORDER BY edited_timestamp LIMIT 1",
                (message_id,),
            )

            # Get intermediate versions from quoted replies
            quote_versions = db.fetchall(
                "SELECT captured_text, captured_timestamp, is_pre_edit,"
                " quote_source_msg_id"
                " FROM edit_version WHERE message_id = ?"
                " ORDER BY captured_timestamp",
                (message_id,),
            )

            # Get dual receipt timelines (original delivery + edit delivery)
            addon_receipts = db.fetchall(
                "SELECT original_receipt_ts, edit_receipt_ts, addon_key_id"
                " FROM edit_addon_receipt WHERE message_id = ?"
                " ORDER BY original_receipt_ts",
                (message_id,),
            )

            # Timestamps
            sent_str = format_timestamp(sent_ts, "full") if sent_ts else "Unknown"
            edit_str = format_timestamp(edit_ts, "full") if edit_ts else None
            sender_edit_str = None
            if edit_row and edit_row["sender_timestamp"]:
                sender_edit_str = format_timestamp(edit_row["sender_timestamp"], "full")
            if not edit_str and edit_row:
                edit_str = format_timestamp(edit_row["edited_timestamp"], "full")
            revoke_str = format_timestamp(revoke_ts, "full") if revoke_ts else None

            # Receipt timestamps
            recv_ts = msg_row["received_timestamp"]
            recv_str = format_timestamp(recv_ts, "full") if recv_ts and recv_ts > 0 else None
            delivered_str = format_timestamp(msg_row["first_delivered_ts"], "full") if msg_row["first_delivered_ts"] else None
            read_str = format_timestamp(msg_row["first_read_ts"], "full") if msg_row["first_read_ts"] else None

            # ── Build dialog ──
            dlg = QDialog(self)
            title = "\u270E Edit History"
            if is_revoked:
                title += " + \U0001F6AB Deletion"
            title += " — Forensic Comparison"
            dlg.setWindowTitle(title)
            dlg.setMinimumSize(540, 480)
            dlg.resize(580, 700)
            dlg.setStyleSheet(
                "QDialog { background: #fafafa; }"
                " QLabel { font-family: 'Segoe UI'; }"
            )

            # Scrollable content
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            dl = QVBoxLayout(content)
            dl.setContentsMargins(20, 16, 20, 16)
            dl.setSpacing(8)
            scroll.setWidget(content)

            outer = QVBoxLayout(dlg)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(scroll)

            # ── Header ──
            hdr_text = f"\u270E Message edited by {sender}"
            if is_revoked:
                hdr_text += "  \u2192  \U0001F6AB then deleted"
            hdr = QLabel(hdr_text)
            hdr_font = _QFont("Segoe UI", 13)
            hdr_font.setBold(True)
            hdr.setFont(hdr_font)
            hdr.setStyleSheet("color: #e65100; margin-bottom: 2px;")
            hdr.setWordWrap(True)
            dl.addWidget(hdr)

            # ── Timeline card ──
            time_frame = QFrame()
            time_frame.setStyleSheet(
                "QFrame { background: white; border: 1px solid #e0e0e0;"
                " border-radius: 8px; padding: 10px; }"
            )
            tl = QVBoxLayout(time_frame)
            tl.setContentsMargins(12, 8, 12, 8)
            tl.setSpacing(4)

            def _ts_row(icon, label, value, color="#333"):
                row = QHBoxLayout()
                lbl = QLabel(f"{icon} <b>{label}:</b>")
                lbl.setStyleSheet("font-size: 11px; color: #667781;")
                row.addWidget(lbl)
                val = QLabel(f"<b>{value}</b>")
                val.setStyleSheet(f"font-size: 11px; color: {color};")
                row.addWidget(val)
                row.addStretch()
                return row

            tl.addLayout(_ts_row("\U0001F4E4", "Original sent", sent_str, "#1565c0"))

            # ── Dual receipt timelines ──
            if from_me and addon_receipts:
                # We have the ORIGINAL delivery receipt from message_add_on_receipt_device
                first_addon = addon_receipts[0]
                orig_recv_ts = first_addon["original_receipt_ts"]
                edit_recv_ts = first_addon["edit_receipt_ts"]

                if orig_recv_ts:
                    tl.addLayout(_ts_row(
                        "\u2713\u2713", "Original delivered",
                        format_timestamp(orig_recv_ts, "full"), "#4caf50"
                    ))
                if delivered_str:
                    tl.addLayout(_ts_row(
                        "\u2713\u2713", "Edit delivered (overwrote original)",
                        delivered_str, "#ff9800"
                    ))
                if read_str:
                    tl.addLayout(_ts_row("\U0001F440", "Read", read_str, "#2196f3"))

                if orig_recv_ts and edit_recv_ts:
                    diff_s = abs(edit_recv_ts - orig_recv_ts) / 1000
                    if diff_s < 120:
                        diff_str = f"{diff_s:.0f}s"
                    elif diff_s < 7200:
                        diff_str = f"{diff_s / 60:.1f}min"
                    else:
                        diff_str = f"{diff_s / 3600:.1f}hr"
                    tl.addLayout(_ts_row(
                        "\u23F1", "Gap (original \u2192 edit delivery)",
                        diff_str, "#9c27b0"
                    ))
            elif from_me:
                if delivered_str:
                    tl.addLayout(_ts_row("\u2713\u2713", "Delivered (edit-overwritten)", delivered_str, "#ff9800"))
                if read_str:
                    tl.addLayout(_ts_row("\U0001F440", "Read", read_str, "#2196f3"))
            else:
                if recv_str:
                    tl.addLayout(_ts_row("\U0001F4E5", "Received on device", recv_str, "#4caf50"))

            # Edit timestamps
            sep1 = QLabel("\u2500" * 30)
            sep1.setStyleSheet("color: #e0e0e0; font-size: 8px;")
            tl.addWidget(sep1)

            if sender_edit_str:
                tl.addLayout(_ts_row("\u270E", "Edit sent by sender", sender_edit_str, "#e65100"))
            if edit_str:
                tl.addLayout(_ts_row("\U0001F4DD", "Edit recorded (server)", edit_str, "#c62828"))

            # Deletion timestamps
            if is_revoked and revoke_str:
                sep2 = QLabel("\u2500" * 30)
                sep2.setStyleSheet("color: #e0e0e0; font-size: 8px;")
                tl.addWidget(sep2)

                tl.addLayout(_ts_row(
                    "\U0001F6AB", "Deleted for everyone",
                    revoke_str, "#d32f2f"
                ))
                if edit_ts and revoke_ts:
                    gap_s = (revoke_ts - edit_ts) / 1000
                    if gap_s < 120:
                        gap_str = f"{gap_s:.0f}s after edit"
                    elif gap_s < 7200:
                        gap_str = f"{gap_s / 60:.1f}min after edit"
                    else:
                        gap_str = f"{gap_s / 3600:.1f}hr after edit"
                    tl.addLayout(_ts_row(
                        "\u23F1", "Edit \u2192 Delete gap",
                        gap_str, "#9c27b0"
                    ))

            if from_me:
                note_parts = []
                note_parts.append(
                    "After editing, WhatsApp overwrites delivery/read receipt "
                    "timestamps with edit delivery times."
                )
                if addon_receipts:
                    note_parts.append(
                        "Original receipt recovered from message_add_on_receipt_device."
                    )
                if is_revoked:
                    note_parts.append(
                        "After deletion, receipt_device timestamps reflect the "
                        "delete delivery time, not the original or edit delivery."
                    )
                note_lbl = QLabel(" ".join(note_parts))
                note_lbl.setStyleSheet("color: #999; font-size: 8px; font-style: italic; margin-top: 4px;")
                note_lbl.setWordWrap(True)
                tl.addWidget(note_lbl)

            dl.addWidget(time_frame)

            # ══════ TEXT VERSIONS (chronological) ══════

            version_num = 0

            # ── V0: Original text (BEFORE) from FTS ──
            if edit_row and edit_row["original_text"]:
                version_num += 1
                orig_hdr = QHBoxLayout()
                orig_icon = QLabel("\U0001F534")
                orig_icon.setFixedWidth(20)
                orig_label = QLabel(f"V{version_num}: ORIGINAL (from FTS index — pre-edit)")
                orig_label.setStyleSheet(
                    "font-weight: bold; font-size: 11px; color: #c62828;"
                )
                orig_hdr.addWidget(orig_icon)
                orig_hdr.addWidget(orig_label)
                orig_hdr.addStretch()
                dl.addLayout(orig_hdr)

                orig_te = QTextEdit()
                orig_te.setPlainText(edit_row["original_text"])
                orig_te.setReadOnly(True)
                orig_te.setMaximumHeight(100)
                orig_te.setStyleSheet(
                    "QTextEdit { background: #fff3e0; border: 2px solid #ffcc80;"
                    " border-left: 4px solid #e65100;"
                    " border-radius: 6px; padding: 8px; font-size: 12px;"
                    " color: #bf360c; font-family: 'Segoe UI'; }"
                )
                dl.addWidget(orig_te)

                fts_note = QLabel(
                    "FTS index stores text in lowercase with tokenized spacing"
                )
                fts_note.setStyleSheet("color: #999; font-size: 9px; font-style: italic;")
                dl.addWidget(fts_note)
            else:
                no_orig = QLabel(
                    "\u26A0 Original text not available — "
                    "FTS index (message_ftsv2_content) was not recovered for this message."
                )
                no_orig.setStyleSheet(
                    "color: #ff6f00; font-size: 10px; padding: 8px;"
                    " background: #fff8e1; border: 1px solid #ffe082;"
                    " border-radius: 6px;"
                )
                no_orig.setWordWrap(True)
                dl.addWidget(no_orig)

            # ── Intermediate versions from quoted replies ──
            if quote_versions:
                for qv in quote_versions:
                    version_num += 1
                    is_pre = qv["is_pre_edit"]
                    cap_ts = qv["captured_timestamp"]
                    cap_str = format_timestamp(cap_ts, "full") if cap_ts else "?"

                    arrow = QLabel("\u2B07")
                    arrow.setAlignment(Qt.AlignCenter)
                    arrow.setStyleSheet("font-size: 14px; color: #bbb; margin: 2px 0;")
                    dl.addWidget(arrow)

                    qv_hdr = QHBoxLayout()
                    qv_icon = QLabel("\U0001F7E0")  # orange circle
                    qv_icon.setFixedWidth(20)
                    timing = "before edit" if is_pre else "after edit"
                    qv_label = QLabel(
                        f"V{version_num}: INTERMEDIATE (quoted reply — {timing})"
                    )
                    qv_label.setStyleSheet(
                        "font-weight: bold; font-size: 11px; color: #e65100;"
                    )
                    qv_hdr.addWidget(qv_icon)
                    qv_hdr.addWidget(qv_label)
                    qv_hdr.addStretch()
                    dl.addLayout(qv_hdr)

                    qv_ts_lbl = QLabel(f"Captured at: {cap_str}")
                    qv_ts_lbl.setStyleSheet("color: #888; font-size: 9px; margin-left: 20px;")
                    dl.addWidget(qv_ts_lbl)

                    qv_te = QTextEdit()
                    qv_te.setPlainText(qv["captured_text"])
                    qv_te.setReadOnly(True)
                    qv_te.setMaximumHeight(80)
                    qv_te.setStyleSheet(
                        "QTextEdit { background: #fff8e1; border: 2px solid #ffe082;"
                        " border-left: 4px solid #ff9800;"
                        " border-radius: 6px; padding: 8px; font-size: 12px;"
                        " color: #e65100; font-family: 'Segoe UI'; }"
                    )
                    dl.addWidget(qv_te)

            # Arrow to final version
            arrow2 = QLabel("\u2B07")
            arrow2.setAlignment(Qt.AlignCenter)
            arrow2.setStyleSheet("font-size: 18px; color: #bbb; margin: 2px 0;")
            dl.addWidget(arrow2)

            # ── Current/Final text (AFTER) ──
            version_num += 1
            cur_hdr = QHBoxLayout()
            if is_revoked:
                cur_icon = QLabel("\U0001F6AB")
                cur_icon.setFixedWidth(20)
                status = "DELETED" if not current_text else "DELETED (text preserved)"
                cur_label = QLabel(f"V{version_num}: FINAL \u2192 {status}")
                cur_label.setStyleSheet(
                    "font-weight: bold; font-size: 11px; color: #d32f2f;"
                )
            else:
                cur_icon = QLabel("\U0001F7E2")
                cur_icon.setFixedWidth(20)
                cur_label = QLabel(f"V{version_num}: CURRENT (text in message table)")
                cur_label.setStyleSheet(
                    "font-weight: bold; font-size: 11px; color: #1b5e20;"
                )
            cur_hdr.addWidget(cur_icon)
            cur_hdr.addWidget(cur_label)
            cur_hdr.addStretch()
            dl.addLayout(cur_hdr)

            cur_te = QTextEdit()
            if is_revoked and not current_text:
                cur_te.setPlainText("(message content wiped after deletion)")
                cur_te.setStyleSheet(
                    "QTextEdit { background: #ffebee; border: 2px solid #ef9a9a;"
                    " border-left: 4px solid #d32f2f;"
                    " border-radius: 6px; padding: 8px; font-size: 12px;"
                    " color: #b71c1c; font-family: 'Segoe UI'; font-style: italic; }"
                )
            elif is_revoked:
                cur_te.setPlainText(current_text)
                cur_te.setStyleSheet(
                    "QTextEdit { background: #ffebee; border: 2px solid #ef9a9a;"
                    " border-left: 4px solid #d32f2f;"
                    " border-radius: 6px; padding: 8px; font-size: 12px;"
                    " color: #b71c1c; font-family: 'Segoe UI'; }"
                )
            else:
                cur_te.setPlainText(current_text)
                cur_te.setStyleSheet(
                    "QTextEdit { background: #e8f5e9; border: 2px solid #a5d6a7;"
                    " border-left: 4px solid #2e7d32;"
                    " border-radius: 6px; padding: 8px; font-size: 12px;"
                    " color: #1b5e20; font-family: 'Segoe UI'; }"
                )
            cur_te.setReadOnly(True)
            cur_te.setMaximumHeight(100)
            dl.addWidget(cur_te)

            # ── Forensic metadata ──
            meta_frame = QFrame()
            meta_frame.setStyleSheet(
                "QFrame { background: #f5f5f5; border: 1px solid #e0e0e0;"
                " border-radius: 6px; padding: 8px; }"
            )
            ml = QVBoxLayout(meta_frame)
            ml.setContentsMargins(10, 6, 10, 6)
            ml.setSpacing(2)

            orig_key_val = (edit_row["original_key_id"] if edit_row else original_key) or "N/A"
            edit_count_val = msg_row["edit_count"] or 1
            meta_lines = [
                f"Original Key ID: {orig_key_val}",
                f"Current Key ID:  {current_key}",
            ]
            if orig_key_val != "N/A" and current_key and orig_key_val != current_key:
                meta_lines.append(
                    "\u2192 Key ID changed after edit (WhatsApp overwrites key_id on edit)"
                )
            meta_lines.append(f"Edit count (from ingestion): {edit_count_val}")
            if quote_versions:
                meta_lines.append(
                    f"Intermediate versions recovered from quotes: {len(quote_versions)}"
                )
            if addon_receipts:
                meta_lines.append(
                    f"Original delivery receipts recovered: {len(addon_receipts)}"
                )
            if is_revoked:
                meta_lines.append(
                    f"Message was deleted for everyone after editing"
                )
            meta_lines.append("")
            meta_lines.append(
                "Per WhatsApp forensic analysis:\n"
                "Only the initial and final text are stored in msgstore.db.\n"
                "Original text recovered from FTS index (c0content).\n"
                "Intermediate versions recovered from quoted replies.\n"
                "WhatsApp overwrites receipt timestamps on edit/delete."
            )
            meta_label = QLabel("\n".join(meta_lines))
            meta_label.setStyleSheet("color: #888; font-size: 9px;")
            meta_label.setWordWrap(True)
            ml.addWidget(meta_label)
            dl.addWidget(meta_frame)

            dl.addStretch()
            dlg.show()

        except Exception as e:
            print(f"[ChatViewer] Show edit history error: {e}")

    # ---- Date filter ----

    def _toggle_date_filter(self):
        visible = not self._calendar_heatmap.isVisible()
        self._date_bar.setVisible(visible)
        self._calendar_heatmap.setVisible(visible)
        if visible and self._conv_id:
            self._calendar_heatmap.load_data(self._conv_id)

    def _apply_date_filter(self):
        from_date = self._date_from_edit.date()
        to_date = self._date_to_edit.date()
        from_ms, to_ms = date_range_to_timestamps(from_date.toPython(), to_date.toPython())
        self._model.set_date_range(from_ms, to_ms)
        if self._use_webengine:
            self._send_messages_to_webview()
        total = self._model.total_rows
        from_str = from_date.toString("MMM dd, yyyy")
        to_str = to_date.toString("MMM dd, yyyy")
        self._info_label.setText(
            f"Showing {from_str} \u2013 {to_str}  ({total:,} messages)"
        )
        self._clear_all_filters_btn.setVisible(True)

    def _on_calendar_date(self, d):
        """Single day clicked on calendar heatmap."""
        from_ms, to_ms = date_range_to_timestamps(d, d)
        self._model.set_date_range(from_ms, to_ms)
        if self._use_webengine:
            self._send_messages_to_webview()
        total = self._model.total_rows
        self._info_label.setText(f"{d.strftime('%b %d, %Y')} ({total:,} messages)")
        self._clear_all_filters_btn.setVisible(True)

    def _on_calendar_range(self, start, end):
        """Date range selected on calendar heatmap."""
        from_ms, to_ms = date_range_to_timestamps(start, end)
        self._model.set_date_range(from_ms, to_ms)
        if self._use_webengine:
            self._send_messages_to_webview()
        total = self._model.total_rows
        self._info_label.setText(
            f"{start.strftime('%b %d')} \u2013 {end.strftime('%b %d, %Y')} ({total:,} messages)"
        )
        self._clear_all_filters_btn.setVisible(True)

    def _clear_date_filter(self):
        self._model.set_date_range(None, None)
        if self._use_webengine:
            self._send_messages_to_webview()
        count = self._model.total_rows
        self._info_label.setText(f"{count:,} messages")
        self._clear_all_filters_btn.setVisible(self._has_active_filters())

    # ---- Ghost message filter ----

    def _toggle_ghost_filter(self):
        self._ghost_filter_active = not self._ghost_filter_active
        self._model.set_ghost_only(self._ghost_filter_active)
        if self._use_webengine:
            self._send_messages_to_webview()
        count = self._model.total_rows
        _active_style = """
            QPushButton { background: rgba(255,120,120,0.2); border: 1px solid #ff7878;
                          border-radius: 14px; font-size: 13px; color: #ff7878; }
            QPushButton:hover { background: rgba(255,120,120,0.3); }
        """
        _normal_style = """
            QPushButton { background: transparent; border: none;
                          font-size: 13px; color: #aebac1; padding: 2px; }
            QPushButton:hover { color: #e9edef; }
        """
        if self._ghost_filter_active:
            self._ghost_btn.setStyleSheet(_active_style)
            self._info_label.setText(f"\u2718 {count:,} ghost messages")
        else:
            self._ghost_btn.setStyleSheet(_normal_style)
            self._info_label.setText(f"{count:,} messages")
        self._clear_all_filters_btn.setVisible(self._has_active_filters())

    def _toggle_ghost_sidebar(self):
        """Open / close the ghost-messages sidebar.

        Mutually exclusive with the search-results and replies
        sidebars \u2014 opening this one closes the others so the
        right-hand pane only ever shows one panel at a time.

        Also clears the legacy ghost-only chat filter on open so the
        chat body renders normally underneath the sidebar.  The
        sidebar is the new browse-and-jump UX; the destructive
        chat-level filter is no longer wired to a button.
        """
        was_visible = self._ghost_sidebar.isVisible()
        # Close peer sidebars first
        if hasattr(self, "_search_results_panel"):
            self._search_results_panel.setVisible(False)
        if hasattr(self, "_replies_sidebar"):
            self._replies_sidebar.setVisible(False)

        if was_visible:
            self._ghost_sidebar.setVisible(False)
            return

        # If the legacy ghost-only filter happens to be on (carried
        # over from a previous session / state), clear it so the
        # chat shows in full while the sidebar is open.
        if getattr(self, "_ghost_filter_active", False):
            try:
                self._ghost_filter_active = False
                self._model.set_ghost_only(False)
                if self._use_webengine:
                    self._send_messages_to_webview()
                self._info_label.setText(f"{self._model.total_rows:,} messages")
                if hasattr(self, "_clear_all_filters_btn"):
                    self._clear_all_filters_btn.setVisible(self._has_active_filters())
            except Exception as e:
                print(f"[ChatViewer] could not clear legacy ghost filter: {e}")

        # Populate from the current conversation, then show.
        try:
            count = self._ghost_sidebar.load_for_conversation(self._conv_id or 0)
        except Exception as e:
            print(f"[ChatViewer] ghost sidebar load failed: {e}")
            count = 0
        self._ghost_sidebar.setVisible(True)
        # Tiny status hint so a chat with zero ghosts isn't silent
        if count == 0 and self._conv_id:
            self._info_label.setText("No recovered ghost messages in this chat")
        else:
            self._info_label.setText(f"{count:,} ghost message" + ("s" if count != 1 else ""))

    # ---- Missing media filter ----

    def _toggle_missing_media_filter(self):
        self._missing_media_filter_active = not self._missing_media_filter_active
        self._model.set_missing_media_only(self._missing_media_filter_active)
        if self._use_webengine:
            self._send_messages_to_webview()
        count = self._model.total_rows
        _active_style = """
            QPushButton { background: rgba(255,165,0,0.2); border: 1px solid #ffa726;
                          border-radius: 14px; font-size: 13px; color: #ffa726; }
            QPushButton:hover { background: rgba(255,165,0,0.3); }
        """
        _normal_style = """
            QPushButton { background: transparent; border: none;
                          font-size: 13px; color: #aebac1; padding: 2px; }
            QPushButton:hover { color: #e9edef; }
        """
        if self._missing_media_filter_active:
            self._missing_media_btn.setStyleSheet(_active_style)
            self._info_label.setText(f"\u2205 {count:,} messages with missing media")
        else:
            self._missing_media_btn.setStyleSheet(_normal_style)
            self._info_label.setText(f"{count:,} messages")
        self._clear_all_filters_btn.setVisible(self._has_active_filters())

    # ---- Filter state helpers ----

    def _has_active_filters(self) -> bool:
        """Check if any message filter is currently active."""
        m = self._model
        return bool(
            m._search_text or m._date_from is not None or m._date_to is not None
            or m._ghost_only or m._missing_media_only
            or m._sender_filter_id is not None
        )

    def _save_filter_state(self) -> dict | None:
        """Capture current filter state. Returns None if no filters active."""
        if not self._has_active_filters():
            return None
        m = self._model
        return {
            "search_text": m._search_text,
            "date_from": m._date_from,
            "date_to": m._date_to,
            "ghost_only": m._ghost_only,
            "missing_media_only": m._missing_media_only,
            "sender_filter_id": m._sender_filter_id,
            "ghost_filter_active": self._ghost_filter_active,
            "missing_media_active": self._missing_media_filter_active,
            "search_bar_visible": self._search_bar.isVisible(),
            "search_text_ui": self._chat_search.text(),
        }

    def _restore_saved_filters(self):
        """Restore previously saved filter state and hide context bar."""
        state = self._saved_filter_state
        if not state:
            return
        m = self._model
        m._search_text = state["search_text"]
        m._date_from = state["date_from"]
        m._date_to = state["date_to"]
        m._ghost_only = state["ghost_only"]
        m._missing_media_only = state["missing_media_only"]
        m._sender_filter_id = state["sender_filter_id"]
        self._ghost_filter_active = state["ghost_filter_active"]
        self._missing_media_filter_active = state["missing_media_active"]
        if state["search_bar_visible"]:
            self._search_bar.setVisible(True)
            self._chat_search.setText(state["search_text_ui"])
        m._reload()
        self._update_info_label_for_filters()
        # Scroll back to the message the user was viewing before jumping
        scroll_msg = state.get("scroll_to_msg_id")
        if scroll_msg and self._use_webengine:
            self._web_view.scroll_to_message(scroll_msg)
            self._web_view.set_search_target(scroll_msg)
        self._saved_filter_state = None
        self._filter_context_bar.setVisible(False)

    def _discard_saved_filters(self):
        """Discard saved filters and stay in unfiltered view."""
        self._saved_filter_state = None
        self._filter_context_bar.setVisible(False)

    def _update_info_label_for_filters(self):
        """Update info label with active filter description."""
        m = self._model
        count = m.total_rows
        parts: list[str] = []
        if m._search_text:
            parts.append(f'search: "{m._search_text}"')
        if m._ghost_only:
            parts.append("ghost messages")
        if m._missing_media_only:
            parts.append("missing media")
        if m._sender_filter_id is not None:
            parts.append("sender filter")
        if m._date_from is not None or m._date_to is not None:
            parts.append("date range")
        if parts:
            self._info_label.setText(f"{count:,} messages ({', '.join(parts)})")
            self._clear_all_filters_btn.setVisible(True)
        else:
            self._info_label.setText(f"{count:,} messages")
            self._clear_all_filters_btn.setVisible(False)

    def _clear_all_filters(self):
        """Clear all active filters and show full chat."""
        _normal_style = """
            QPushButton { background: transparent; border: none;
                          font-size: 13px; color: #aebac1; padding: 2px; }
            QPushButton:hover { color: #e9edef; }
        """
        self._model._ghost_only = False
        self._model._missing_media_only = False
        self._model._search_text = ""
        self._model._sender_filter_id = None
        self._model._date_from = None
        self._model._date_to = None
        self._ghost_filter_active = False
        self._missing_media_filter_active = False
        if hasattr(self, '_ghost_btn'):
            self._ghost_btn.setStyleSheet(_normal_style)
        if hasattr(self, '_missing_media_btn'):
            self._missing_media_btn.setStyleSheet(_normal_style)
        self._chat_search.clear()
        self._search_bar.setVisible(False)
        self._date_bar.setVisible(False)
        self._sender_bar.setVisible(False)
        if hasattr(self, '_calendar_heatmap'):
            self._calendar_heatmap.setVisible(False)
        self._model._reload()
        self._info_label.setText(f"{self._model.total_rows:,} messages")
        self._clear_all_filters_btn.setVisible(False)
        self._filter_context_bar.setVisible(False)
        self._saved_filter_state = None

    # ---- Navigate to message in full chat ----

    def _goto_message_in_full_chat(self, msg_id: int):
        """Save filter state, clear all filters, and scroll to a specific message."""
        # Save current filter state so user can return
        self._saved_filter_state = self._save_filter_state()
        if self._saved_filter_state:
            self._saved_filter_state["scroll_to_msg_id"] = msg_id

        _normal_style = """
            QPushButton { background: transparent; border: none;
                          font-size: 13px; color: #aebac1; padding: 2px; }
            QPushButton:hover { color: #e9edef; }
        """
        # Clear all active filters (set internal state without triggering reload)
        if self._ghost_filter_active:
            self._ghost_filter_active = False
            self._ghost_btn.setStyleSheet(_normal_style)

        if self._missing_media_filter_active:
            self._missing_media_filter_active = False
            self._missing_media_btn.setStyleSheet(_normal_style)

        if self._model._search_text:
            self._chat_search.clear()
            self._search_bar.setVisible(False)

        # Reset model filter state directly (avoid multiple reloads)
        self._model._ghost_only = False
        self._model._missing_media_only = False
        self._model._search_text = ""
        self._model._sender_filter_id = None
        self._model._date_from = None
        self._model._date_to = None

        # Hide other filter bars
        self._date_bar.setVisible(False)
        self._sender_bar.setVisible(False)
        if hasattr(self, '_calendar_heatmap'):
            self._calendar_heatmap.setVisible(False)
        self._clear_all_filters_btn.setVisible(False)

        # Show filter context bar if we had active filters
        if self._saved_filter_state:
            self._filter_context_bar.setVisible(True)

        # Single reload with all filters cleared
        self._model._reload()
        count = self._model.total_rows
        self._info_label.setText(f"{count:,} messages")

        # Find and scroll to the message with persistent highlight
        if self._use_webengine:
            self._web_view.scroll_to_message(msg_id)
            self._web_view.set_search_target(msg_id)
            self._show_copy_toast("Jumped to message")
            return
        row = self._model.find_row_by_msg_id(msg_id)
        if row >= 0:
            idx = self._model.index(row, 0)
            self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
            self._list.setCurrentIndex(idx)
            self._on_item_clicked(idx)
            self._show_copy_toast("Jumped to message")
        else:
            self._show_copy_toast("Message not found")

    # ---- Sender filter ----

    def _toggle_sender_filter(self):
        vis = not self._sender_bar.isVisible()
        self._sender_bar.setVisible(vis)
        if vis and self._conv_id:
            self._load_sender_list()

    def _load_sender_list(self):
        """Populate the sender filter dropdown with every sender that
        appears in the current conversation, plus their full forensic
        identity (saved name, WA name, phone, phone JID, LID JID,
        message count).  The visible label is searchable by typing
        any substring (resolved name, phone digits, JID, "LID", "+91").
        Per-entry forensic IDs are stored in ``_sender_meta`` keyed
        by combo index so the JID hint label can update on selection.
        """
        self._sender_combo.blockSignals(True)
        self._sender_combo.clear()
        self._sender_meta: dict[int, dict] = {}
        # Index 0 \u2014 show everything
        self._sender_combo.addItem("All senders", None)
        self._sender_meta[0] = {"jid_hint": ""}

        db = Database.get()
        senders = db.fetchall(
            "SELECT m.sender_id,"
            " m.is_bot_message,"
            " c.is_saved, c.display_name, c.wa_name, c.phone_number,"
            " c.phone_jid, c.lid_jid,"
            " COUNT(*) AS cnt"
            " FROM message m LEFT JOIN contact c ON c.id = m.sender_id"
            " WHERE m.conversation_id = ? AND m.message_type != 7"
            " GROUP BY m.sender_id ORDER BY cnt DESC",
            (self._conv_id,),
        )

        # Resolve owner identity (case_metadata) so the merged
        # "You (Owner)" row can show the full owner phone + JID.
        _owner_name_row = db.fetchone(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_name'"
        )
        _owner_phone_row = db.fetchone(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'"
        )
        _owner_cid_row = db.fetchone(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_contact_id'"
        )
        _owner_name = _owner_name_row[0] if _owner_name_row else ""
        _owner_phone_full = (_owner_phone_row[0] or "") if _owner_phone_row else ""
        _owner_phone_digits = _owner_phone_full.replace("@s.whatsapp.net", "")
        _owner_cid = int(_owner_cid_row[0]) if _owner_cid_row and _owner_cid_row[0] else 0

        def _label_for(sid, is_bot, is_saved, display_name, wa_name,
                       phone_number, phone_jid, lid_jid, cnt) -> tuple[str, str]:
            """Return (visible_label, jid_hint) for one sender row.

            visible_label encodes everything searchable in plain text so
            QCompleter can substring-match. jid_hint is the full
            forensic identifier shown beneath the combo on selection.
            """
            # Outgoing / owner
            if sid is None and is_bot:
                return ("Meta AI", "Meta AI bot \u2014 no JID")
            if sid is None:
                hint_parts = []
                if _owner_name:
                    hint_parts.append(f"Owner: {_owner_name}")
                if _owner_phone_digits:
                    hint_parts.append(f"+{_owner_phone_digits}@s.whatsapp.net")
                hint = "  \u00b7  ".join(hint_parts) or "Outgoing message (sender_id IS NULL)"
                return ("You (outgoing)", hint)

            # Resolved entries
            primary = ""
            if is_saved and display_name:
                primary = display_name
            elif wa_name:
                primary = f"~{wa_name}"
            elif phone_number:
                primary = f"+{phone_number}"
            elif phone_jid:
                primary = phone_jid.replace("@s.whatsapp.net", "")
            elif lid_jid:
                primary = f"LID:{lid_jid.split('@')[0]}"
            else:
                primary = f"Unknown (cid={sid})"

            # Append phone (if not already in primary)
            id_bits = []
            if phone_number and phone_number not in primary:
                id_bits.append(f"+{phone_number}")
            if phone_jid:
                id_bits.append(phone_jid)
            if lid_jid:
                id_bits.append(lid_jid)

            visible = primary
            if id_bits:
                # Show only the first ID inline to keep the row tight,
                # but include them all in the searchable string.
                visible = f"{primary}  \u2014  {id_bits[0]}"
                if len(id_bits) > 1:
                    visible += f"  +{len(id_bits) - 1}"

            jid_hint = "  \u00b7  ".join(id_bits) if id_bits else f"contact_id={sid}, no JID resolved"
            return (visible, jid_hint)

        # Merge owner sender_id rows with the implicit "You (outgoing)"
        # bucket \u2014 both refer to the device owner.
        you_count = 0
        owner_hint = ""
        rows_visible: list[tuple] = []
        for r in senders:
            sid, is_bot, is_saved, dn, wn, ph, pj, lj, cnt = r
            if sid is None or (sid is not None and sid == _owner_cid):
                you_count += cnt or 0
                if owner_hint == "":
                    owner_hint = _label_for(None, is_bot, 0, "", "", "", "", "", cnt)[1]
            else:
                rows_visible.append(r)

        if you_count > 0:
            you_label = f"You (Owner: {_owner_name})" if _owner_name else "You (outgoing)"
            you_visible = f"{you_label}  ({you_count:,} msgs)"
            idx = self._sender_combo.count()
            self._sender_combo.addItem(you_visible, -1)
            self._sender_meta[idx] = {"jid_hint": owner_hint}

        for r in rows_visible:
            sid, is_bot, is_saved, dn, wn, ph, pj, lj, cnt = r
            visible, hint = _label_for(sid, is_bot, is_saved, dn, wn, ph, pj, lj, cnt)
            label = f"{visible}  ({cnt:,} msgs)"
            idx = self._sender_combo.count()
            self._sender_combo.addItem(label, sid)
            self._sender_meta[idx] = {"jid_hint": hint}

        # Re-arm the completer with the freshly-rebuilt model
        self._sender_completer.setModel(self._sender_combo.model())
        self._sender_combo.setCurrentIndex(0)
        self._sender_jid_hint.setText("")
        self._sender_combo.blockSignals(False)

    def _apply_sender_filter(self, idx):
        if idx < 0:
            return
        sid = self._sender_combo.itemData(idx)
        meta = getattr(self, "_sender_meta", {}).get(idx, {})
        self._sender_jid_hint.setText(meta.get("jid_hint", ""))

        self._model.set_sender_filter(sid)
        if self._use_webengine:
            self._send_messages_to_webview()
        count = self._model.total_rows
        if sid is not None:
            name = self._sender_combo.currentText()
            self._info_label.setText(f"\u2630 {count:,} messages from {name}")
        else:
            self._info_label.setText(f"{count:,} messages")
        self._clear_all_filters_btn.setVisible(self._has_active_filters())

    def _clear_sender_filter(self):
        self._sender_combo.setCurrentIndex(0)
        self._sender_jid_hint.setText("")
        self._sender_bar.setVisible(False)

    # ---- Bulk media download ----

    def _start_bulk_download(self):
        if self._conv_id is None:
            return

        db = Database.get()
        missing_count = db.scalar(
            "SELECT COUNT(*) FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "WHERE m.conversation_id = ? "
            "AND me.media_url IS NOT NULL AND me.media_key IS NOT NULL "
            "AND (me.file_exists = 0 OR me.file_exists IS NULL)",
            (self._conv_id,),
        ) or 0

        if missing_count == 0:
            self._show_copy_toast("No missing media to download")
            return

        reply = QMessageBox.question(
            self, "Download Missing Media",
            f"Download and decrypt {missing_count:,} missing media files "
            f"for this conversation?\n\n"
            f"Files will be saved to the case's recovered_media folder.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Set up progress dialog
        self._dl_progress = QProgressDialog(
            "Preparing download...", "Cancel", 0, missing_count, self
        )
        self._dl_progress.setWindowTitle("Downloading Media")
        self._dl_progress.setMinimumWidth(450)
        self._dl_progress.setAutoClose(False)
        self._dl_progress.setAutoReset(False)

        self._bulk_dl_btn.setEnabled(False)

        self._dl_worker = BulkMediaDownloadWorker(
            self._conv_id, str(db.path)
        )
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.media_saved.connect(self._on_single_media_saved)
        self._dl_worker.finished.connect(self._on_dl_finished)
        self._dl_progress.canceled.connect(self._dl_worker.cancel)
        self._dl_worker.start()

    def _on_dl_progress(self, current: int, total: int, status: str):
        if hasattr(self, "_dl_progress") and self._dl_progress:
            self._dl_progress.setMaximum(total)
            self._dl_progress.setValue(current)
            self._dl_progress.setLabelText(f"[{current}/{total}] {status}")
        # Also update download panel if visible
        if hasattr(self, "_download_panel") and self._download_panel.isVisible():
            self._download_panel.set_progress(current, total, status)
        # Update global status bar progress
        main_win = self.window()
        if hasattr(main_win, 'show_download_progress'):
            main_win.show_download_progress(current, total, status)

    def _on_single_media_saved(self, media_id: int, save_path: str):
        """Refresh a single bubble after its media was downloaded."""
        # Find message row by querying which message has this media_id
        db = Database.get()
        row_data = db.scalar(
            "SELECT message_id FROM media WHERE id = ?", (media_id,)
        )
        if not row_data:
            return
        msg_id = row_data

        # Update in-memory data for this message
        for i, msg in enumerate(self._model._data):
            if msg and msg.get("id") == msg_id:
                msg["resolved_file_path"] = save_path
                msg["media_file_exists"] = True
                if self._use_webengine:
                    # Re-send updated messages to WebView
                    self._send_messages_to_webview()
                else:
                    # Clear delegate caches for this specific message
                    delegate = self._list.itemDelegate()
                    if hasattr(delegate, '_file_exists_cache'):
                        delegate._file_exists_cache.pop(msg_id, None)
                    if hasattr(delegate, '_size_hint_cache'):
                        # Clear all size hints for this msg (any width)
                        keys_to_del = [k for k in delegate._size_hint_cache if k[0] == msg_id]
                        for k in keys_to_del:
                            del delegate._size_hint_cache[k]
                    # Trigger repaint for this row
                    idx = self._model.index(i, 0)
                    self._model.dataChanged.emit(idx, idx)
                break

    def _on_dl_finished(self, downloaded: int, failed: int, skipped: int):
        if hasattr(self, "_dl_progress") and self._dl_progress:
            self._dl_progress.close()
            self._dl_progress = None

        self._bulk_dl_btn.setEnabled(True)

        # Update global status bar
        main_win = self.window()
        if hasattr(main_win, 'hide_download_progress'):
            summary = f"{downloaded} downloaded"
            if failed:
                summary += f", {failed} failed"
            if skipped:
                summary += f", {skipped} skipped"
            main_win.hide_download_progress(summary)

        # Reconnect the read connection to pick up writes
        db = Database.get()
        db.reconnect_read()

        # Clear bubble delegate caches so downloaded media renders
        if not self._use_webengine and self._list:
            delegate = self._list.itemDelegate()
            if hasattr(delegate, '_file_exists_cache'):
                delegate._file_exists_cache.clear()
            if hasattr(delegate, '_resolve_cache'):
                delegate._resolve_cache.clear()
            if hasattr(delegate, '_size_hint_cache'):
                delegate._size_hint_cache.clear()

        # Reload the chat to show newly available media
        if self._conv_id is not None:
            self._model.set_conversation(self._conv_id, self._is_group)
            count = self._model.total_rows
            self._info_label.setText(f"{count:,} messages")
            if count > 0:
                if self._use_webengine:
                    _reload_gen = getattr(self, '_prog_gen', 0)
                    QTimer.singleShot(
                        100,
                        lambda gen=_reload_gen: (
                            self._send_messages_to_webview()
                            if getattr(self, '_prog_gen', 0) == gen else None
                        ),
                    )
                else:
                    QTimer.singleShot(150, self._list.scrollToBottom)

        self._forensic_log("bulk_download_complete", {
            "conversation_id": self._conv_id,
            "downloaded": downloaded, "failed": failed, "skipped": skipped,
        })

        summary = (
            f"Download complete!\n\n"
            f"Downloaded: {downloaded:,}\n"
            f"Failed: {failed:,}\n"
            f"Skipped (already on disk): {skipped:,}"
        )
        QMessageBox.information(self, "Bulk Download Complete", summary)

        # Refresh download panel stats
        if hasattr(self, "_download_panel") and self._download_panel.isVisible():
            self._download_panel.on_download_finished(downloaded, failed, skipped)

    # ---- Avatar + group info ----

    def _on_avatar_click(self, event):
        if self._is_group and self._conv_id is not None:
            self.group_info_requested.emit(self._conv_id, self._conv_name)
        elif not self._is_group and self._conv_id is not None:
            # Personal chat: resolve the other party's contact_id
            from app.services.database import Database
            db = Database.get()
            row = db.fetchone(
                "SELECT c.id FROM contact c "
                "JOIN conversation cv ON (c.phone_jid = cv.jid_raw_string "
                "  OR c.lid_jid = cv.jid_raw_string) "
                "WHERE cv.id = ? LIMIT 1",
                (self._conv_id,),
            )
            if row:
                self.contact_requested.emit(row[0])

    def load_conversation(self, conv_id: int, display_name: str,
                          target_msg_id: int = 0, search_keyword: str = "") -> None:
        # ---- Instant feedback: spinner up BEFORE we touch the DB ----
        # The setup work below is mostly synchronous; without this
        # call the user sees no response between double-clicking a
        # chat and the first tile arriving.  ``processEvents`` is
        # what actually flushes the ``runJavaScript('showLoading
        # (true)')`` call into the Chromium renderer — otherwise it
        # sits queued behind the synchronous setup and the spinner
        # only appears once everything has finished.
        if getattr(self, '_use_webengine', False) and getattr(self, '_web_view', None):
            try:
                self._web_view.show_loading(True)
                from PySide6.QtCore import QCoreApplication
                QCoreApplication.processEvents()
            except Exception:
                pass
        # ---- Instant cancel: stop any in-flight work from previous chat ----
        self._prog_gen = getattr(self, '_prog_gen', 0) + 1
        # Clear pending tile requests — tile workers check _prog_gen on completion
        self._pending_tile_requests = set()
        tile_w = getattr(self, '_tile_worker', None)
        if tile_w:
            tile_w.clear_pending()
        _worker = getattr(self._model, '_prefetch_worker', None)
        if _worker and _worker.isRunning():
            _worker.quit()
            _worker.wait(100)
        # ---- Clear the JS DOM IMMEDIATELY, BEFORE we change the header.
        # Otherwise the user sees the previous conversation's messages
        # underneath the new chat's title for the ~1-2 seconds it takes for
        # the first tile to arrive. Clearing up-front gives a clean "new
        # chat is opening" feel instead of a mismatched header-vs-body.
        if getattr(self, '_use_webengine', False) and getattr(self, '_web_view', None):
            try:
                self._web_view.set_generation(self._prog_gen)  # invalidate stale JS payloads
                self._web_view.clear()
            except Exception:
                pass
        self._target_msg_id = target_msg_id
        self._conv_id = conv_id
        self._conv_name = display_name or f"#{conv_id}"
        # Check for Meta Verified / Business status for header badge
        from app.services.database import Database
        _hdr_db = Database.get()
        _hdr_row = _hdr_db.fetchone(
            "SELECT c.is_meta_verified, c.is_business "
            "FROM contact c "
            "JOIN conversation cv ON (c.phone_jid = cv.jid_raw_string "
            "  OR c.lid_jid = cv.jid_raw_string) "
            "WHERE cv.id = ? LIMIT 1",
            (conv_id,),
        )
        _title = self._conv_name
        if _hdr_row:
            try:
                if _hdr_row[0]:  # is_meta_verified
                    _title += " \u2713\uFE0F"  # checkmark
                elif _hdr_row[1]:  # is_business
                    _title += " \U0001F4BC"  # briefcase
            except (IndexError, KeyError):
                pass
        self._title_label.setText(_title)
        # Refresh the admin-only-send / owner-permission banner
        self._update_chat_policy_banner(_hdr_db, conv_id)
        self._chat_search.clear()
        self._search_bar.setVisible(False)
        self._date_bar.setVisible(False)
        # Hide calendar heatmap on chat switch — will reload if user opens it again
        if hasattr(self, '_calendar_heatmap'):
            self._calendar_heatmap.setVisible(False)
        self._analytics_bar.setVisible(False)

        # Reset filters and filter navigation state
        self._ghost_filter_active = False
        self._missing_media_filter_active = False
        self._saved_filter_state = None
        self._filter_context_bar.setVisible(False)
        self._clear_all_filters_btn.setVisible(False)
        self._search_results_panel.clear()
        self._ghost_btn.setStyleSheet(self._tm.chat_hdr_btn_style())
        self._missing_media_btn.setStyleSheet(self._tm.chat_hdr_btn_style())
        self._sender_bar.setVisible(False)
        self._sender_combo.clear()
        self._model.set_sender_filter(None)

        db = Database.get()

        self._load_avatar(db, conv_id, display_name)

        chat_type = db.scalar(
            "SELECT chat_type FROM conversation WHERE id = ?", (conv_id,)
        )
        self._is_group = chat_type in ("group", "community")
        if not self._use_webengine:
            self._delegate.set_group(self._is_group, chat_type or "")
            self._delegate.set_conv_name(display_name or "")
        # Load owner info for forensic labeling (both modes)
        if not getattr(self, '_owner_phone', ''):
            owner_name = db.scalar("SELECT value FROM case_metadata WHERE key = 'device_owner_name'") or ""
            owner_phone = db.scalar("SELECT value FROM case_metadata WHERE key = 'device_owner_phone'") or ""
            owner_cid = -1
            if owner_phone:
                owner_cid = db.scalar(
                    "SELECT id FROM contact WHERE phone_number = ?", (owner_phone,)
                ) or -1
            # Store on page for WebEngine system event enrichment
            self._owner_name = owner_name
            self._owner_phone = owner_phone
            self._owner_cid = owner_cid
            if not self._use_webengine:
                self._delegate.set_owner_info(owner_name, owner_phone, owner_cid)
        # Clear performance caches for the new conversation (QPainter mode only)
        if not self._use_webengine:
            self._delegate._file_exists_cache.clear()
            self._delegate._size_hint_cache.clear()
            self._delegate._resolve_cache.clear()
            self._delegate._thumb_cache.clear()
            self._delegate._media_cache.clear()
            self._delegate._mention_docs.clear()
            self._delegate._quote_rects.clear()
            self._delegate._sender_rects.clear()
            self._delegate._link_rects.clear()
            self._delegate._media_rects.clear()
            self._delegate._reaction_rects.clear()
            self._delegate._download_rects.clear()

        # Date range pickers
        first_ts = db.scalar(
            "SELECT MIN(timestamp) FROM message "
            "WHERE conversation_id = ? AND timestamp > 0", (conv_id,)
        )
        last_ts = db.scalar(
            "SELECT MAX(timestamp) FROM message WHERE conversation_id = ?", (conv_id,)
        )
        if first_ts and last_ts:
            try:
                self._date_from_edit.setDate(timestamp_to_qdate(first_ts))
                self._date_to_edit.setDate(timestamp_to_qdate(last_ts))
            except (ValueError, OSError):
                pass

        # Pump events to keep UI responsive during load
        from PySide6.QtWidgets import QApplication as _QApp
        _QApp.processEvents()

        # Load messages
        self._model.set_conversation(conv_id, self._is_group)
        count = self._model.total_rows

        _QApp.processEvents()

        # Ghost count — indexed, fast
        ghost_count = db.scalar(
            "SELECT COUNT(*) FROM ghost_message WHERE conversation_id = ?",
            (conv_id,),
        ) or 0

        # Missing downloadable media count — defer heavy JOIN to avoid blocking
        missing_media = 0

        # Enable/disable bulk download button based on availability
        self._bulk_dl_btn.setEnabled(missing_media > 0)
        if missing_media > 0:
            self._bulk_dl_btn.setToolTip(
                f"Download {missing_media:,} missing media files in this chat"
            )
        else:
            self._bulk_dl_btn.setToolTip("No missing downloadable media")

        if self._is_group:
            participants = db.scalar(
                "SELECT participant_count FROM conversation WHERE id = ?", (conv_id,)
            ) or 0
            info_parts = [f"{count:,} messages", f"{participants} participants"]
            if ghost_count:
                info_parts.append(f"\u2718 {ghost_count} ghost")
            if missing_media:
                info_parts.append(f"\u21E9 {missing_media:,} downloadable")
            self._info_label.setText("  |  ".join(info_parts))
        else:
            # Look up contact info for 1-on-1 chat
            contact_sub_parts: list[str] = []
            try:
                contact_info = db.fetchone(
                    "SELECT ct.wa_name, ct.phone_jid, ct.phone_number "
                    "FROM conversation c "
                    "JOIN jid_to_contact jtc ON jtc.jid_raw_string = c.jid_raw_string "
                    "JOIN contact ct ON ct.id = jtc.contact_id "
                    "WHERE c.id = ? LIMIT 1",
                    (conv_id,),
                )
                if contact_info:
                    wa_name = contact_info[0] or ""
                    phone_jid = contact_info[1] or ""
                    phone_num = contact_info[2] or ""
                    if phone_jid and "@" in phone_jid:
                        phone_display = phone_jid.split("@")[0]
                        if phone_display.startswith("+") or phone_display.isdigit():
                            contact_sub_parts.append(phone_display)
                    elif phone_num:
                        contact_sub_parts.append(phone_num)
                    if wa_name and wa_name != display_name:
                        contact_sub_parts.append(f"~{wa_name}")
            except Exception:
                pass  # jid_to_contact table may not exist

            info_parts = list(contact_sub_parts)
            info_parts.append(f"{count:,} messages")
            if ghost_count:
                info_parts.append(f"\u2718 {ghost_count} ghost")
            if missing_media:
                info_parts.append(f"\u21E9 {missing_media:,} downloadable")
            self._info_label.setText("  |  ".join(info_parts))

        # Detail bar
        if first_ts and last_ts:
            try:
                first = format_timestamp(first_ts, '%b %d, %Y')
                last = format_timestamp(last_ts, '%b %d, %Y')
                self._detail_label.setText(
                    f"{first}  \u2014  {last}  |  {count:,} messages"
                )
            except (ValueError, OSError):
                self._detail_label.setText(f"{count:,} messages")
        else:
            self._detail_label.setText(f"{count:,} messages")

        print(f"[ChatViewer] load_conversation done: conv_id={conv_id}, count={count}, "
              f"use_webengine={self._use_webengine}, web_view={self._web_view is not None}")
        if count > 0:
            if self._use_webengine:
                # Send messages to WebEngine for rendering
                _load_gen = getattr(self, '_prog_gen', 0)
                QTimer.singleShot(
                    100,
                    lambda gen=_load_gen: (
                        self._send_messages_to_webview()
                        if getattr(self, '_prog_gen', 0) == gen else None
                    ),
                )
            elif self._target_msg_id > 0:
                # Navigate to specific message instead of bottom
                QTimer.singleShot(200, lambda: self._navigate_to_message_id(self._target_msg_id))
            else:
                QTimer.singleShot(150, self._list.scrollToBottom)

        # Hide pinned bar immediately (will be shown again by deferred load if new conv has pins)
        self._pin_bar.setVisible(False)
        self._pinned_msg_ids.clear()
        self._pin_previews.clear()
        self._pin_index = 0

        # Defer heavy queries so conversation renders first
        QTimer.singleShot(150, lambda: self._load_pinned_messages(Database.get(), conv_id))
        QTimer.singleShot(250, lambda: self._deferred_missing_media(conv_id))
        QTimer.singleShot(400, lambda: self._build_anchors_deferred(conv_id))
        if self._is_group:
            QTimer.singleShot(350, lambda: self._load_group_analytics(Database.get(), conv_id))

        if search_keyword:
            _kw = search_keyword
            _target_conv = conv_id
            def _trigger_kw_search(kw=_kw, target=_target_conv):
                if self._conv_id != target:
                    return  # user navigated to a different chat; abort
                self._search_bar.setVisible(True)
                self._chat_search.blockSignals(True)
                self._chat_search.setText(kw)
                self._chat_search.blockSignals(False)
                self._do_search()
            QTimer.singleShot(400, _trigger_kw_search)

    def _build_anchors_deferred(self, conv_id: int) -> None:
        """Build keyset pagination anchors in background (deferred from load)."""
        if self._conv_id != conv_id:
            return  # user switched away
        if self._model._search_text or self._model._ghost_only \
                or self._model._missing_media_only or self._model._sender_filter_id is not None:
            return  # filters active, keyset not used
        try:
            db = Database.get()
            t0 = _time.perf_counter()
            rows = db.fetchall(
                "SELECT m.timestamp, m.sort_id FROM message m "
                "WHERE m.conversation_id = ? "
                "ORDER BY m.timestamp ASC, m.sort_id ASC",
                (conv_id,),
            )
            self._model._anchors = [
                (rows[i][0], rows[i][1])
                for i in range(0, len(rows), BATCH_SIZE)
            ]
            t1 = _time.perf_counter()
            print(f"[ChatViewer] Anchors built: {len(self._model._anchors)} "
                  f"for {len(rows)} msgs in {t1-t0:.3f}s")
        except Exception:
            self._model._anchors = []

    def _deferred_missing_media(self, conv_id: int) -> None:
        """Compute missing media count in background (deferred from load_conversation)."""
        if self._conv_id != conv_id:
            return  # user switched away
        try:
            db = Database.get()
            missing_media = db.scalar(
                "SELECT COUNT(*) FROM media me "
                "JOIN message m ON m.id = me.message_id "
                "WHERE m.conversation_id = ? "
                "AND me.media_url IS NOT NULL AND me.media_key IS NOT NULL "
                "AND (me.file_exists = 0 OR me.file_exists IS NULL)",
                (conv_id,),
            ) or 0
            self._bulk_dl_btn.setEnabled(missing_media > 0)
            if missing_media > 0:
                self._bulk_dl_btn.setToolTip(
                    f"Download {missing_media:,} missing media files in this chat"
                )
                # Update info label if it doesn't already show downloadable count
                cur = self._info_label.text()
                if "downloadable" not in cur:
                    self._info_label.setText(
                        f"{cur}  |  \u21E9 {missing_media:,} downloadable"
                    )
        except Exception:
            pass

    def _load_group_analytics(self, db: Database, conv_id: int) -> None:
        """Load quick analytics for group chat header.

        Top-N sender list:
          * Filters out non-chat message types: date separators
            (-1), system events (7), call logs (90), privacy /
            setting notifications (112).  Without this filter, an
            ``advanced_chat_privacy`` notification would appear
            as a message from "Unknown".
          * Inner-joins the contact table so unresolved senders
            never collapse into a single misleading top-N entry.

        Unresolved senders (NULL sender_id, or contact rows whose
        name fields are all NULL) are bucketed and surfaced
        separately so the analyst can see they exist.  Typical
        contributors to this bucket are bot JIDs
        (``server = 'bot'`` in msgstore — Meta AI for example)
        and removed members whose JIDs never made it into any of
        the contact-resolution sources.
        """
        try:
            # Non-chat message types we exclude from sender stats.
            # (Same set the first-unread query uses — keep them in sync.)
            non_chat_types = "(-1, 7, 90, 112)"

            top_senders = db.fetchall(
                "SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name, "
                "       COUNT(*) AS cnt "
                "  FROM message m "
                "  JOIN contact c ON c.id = m.sender_id "
                " WHERE m.conversation_id = ? "
                f"   AND m.message_type NOT IN {non_chat_types} "
                "   AND m.from_me = 0 "
                "   AND m.sender_id IS NOT NULL "
                "   AND COALESCE(c.resolved_name, c.wa_name, c.phone_number) IS NOT NULL "
                " GROUP BY m.sender_id "
                " ORDER BY cnt DESC LIMIT 3",
                (conv_id,),
            )
            # Bucket of from-others CHAT messages whose sender wasn't
            # resolved.  Typically: bot JIDs + removed members.
            other_count = db.scalar(
                "SELECT COUNT(*) FROM message m "
                "LEFT JOIN contact c ON c.id = m.sender_id "
                "WHERE m.conversation_id = ? "
                f"  AND m.message_type NOT IN {non_chat_types} "
                "  AND m.from_me = 0 "
                "  AND (m.sender_id IS NULL "
                "       OR COALESCE(c.resolved_name, c.wa_name, c.phone_number) IS NULL)",
                (conv_id,),
            ) or 0
            # Owner-side message count
            my_count = db.scalar(
                "SELECT COUNT(*) FROM message WHERE conversation_id = ? "
                f"AND message_type NOT IN {non_chat_types} AND from_me = 1",
                (conv_id,),
            ) or 0
            # Media count
            media_count = db.scalar(
                "SELECT COUNT(*) FROM media me JOIN message m ON m.id = me.message_id "
                "WHERE m.conversation_id = ?",
                (conv_id,),
            ) or 0
            # Active days
            active_days = db.scalar(
                "SELECT COUNT(DISTINCT DATE(timestamp/1000, 'unixepoch')) "
                f"FROM message WHERE conversation_id = ? AND message_type NOT IN {non_chat_types}",
                (conv_id,),
            ) or 0

            # Set labels
            labels = self._analytics_labels
            sender_parts = []
            for r in top_senders:
                if not r[0]:
                    continue
                short = r[0].split()[0] if " " in r[0] else r[0]
                sender_parts.append(f"{short}: {r[1]:,}")
            top_text = "Top: " + ", ".join(sender_parts) if sender_parts else ""
            if other_count > 0:
                # Non-committal label — could be a bot, an ex-member, or a
                # JID that simply didn't merge.  Don't claim "past members"
                # since that's only true for some of them.
                if top_text:
                    top_text += f"  |  Other: {other_count:,}"
                else:
                    top_text = f"Other: {other_count:,}"
            labels[0].setText(top_text)
            labels[1].setText(f"You: {my_count:,}")
            labels[2].setText(f"Media: {media_count:,}")
            labels[3].setText(f"Active: {active_days:,} days")
            labels[4].setText("")
            self._analytics_bar.setVisible(True)
        except Exception:
            self._analytics_bar.setVisible(False)

    def _load_pinned_messages(self, db: Database, conv_id: int) -> None:
        """Load pinned messages for this conversation and show pin bar."""
        self._pinned_msg_ids.clear()
        self._pin_previews.clear()
        self._pin_index = 0
        try:
            # Check if message_pin table exists
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_pin'"
            ).fetchone()
            if not exists:
                self._pin_bar.setVisible(False)
                return

            pins = db.fetchall(
                "SELECT mp.message_id, "
                "SUBSTR(COALESCE(m.text_content, ''), 1, 80), "
                "CASE WHEN m.is_revoked = 1 THEN 'revoked' "
                "     WHEN m.type_label IS NOT NULL THEN m.type_label "
                "     ELSE '' END, "
                "COALESCE("
                "  NULLIF(c.resolved_name, ''), "
                "  NULLIF(c.display_name, ''), "
                "  CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != '' "
                "       THEN '+' || c.phone_number END, "
                "  NULLIF(c.wa_name, ''), "
                "  ''), "
                "m.from_me, "
                "(SELECT me.mime_type FROM media me WHERE me.message_id = m.id LIMIT 1) "
                "FROM message_pin mp "
                "JOIN message m ON m.id = mp.message_id "
                "LEFT JOIN contact c ON c.id = m.sender_id "
                "WHERE mp.conversation_id = ? AND mp.pin_state = 1 "
                "ORDER BY mp.pin_timestamp DESC",
                (conv_id,),
            )
            if pins:
                self._pinned_msg_ids = [p[0] for p in pins]
                for _p in pins:
                    _txt = _p[1] or ""
                    _tl = _p[2] or ""
                    _mime = (_p[5] or "") if len(_p) > 5 else ""
                    if _tl == "document" and _mime:
                        if _mime.startswith("image/"): _tl = "image"
                        elif _mime.startswith("video/"): _tl = "video"
                        elif _mime.startswith("audio/"): _tl = "audio"
                    _sender = _p[3] or ""
                    _from_me = _p[4] if len(_p) > 4 else False
                    if _from_me:
                        _on = getattr(self, '_owner_name', '') or ''
                        _op = getattr(self, '_owner_phone', '') or ''
                        if _on and _op:
                            _sender = f"{_on} (+{_op})"
                        elif _on:
                            _sender = _on
                        elif _op:
                            _sender = f"+{_op}"
                        else:
                            _sender = "You"
                    # Content preview — map type_label to descriptive text
                    _pin_type_map = {
                        "image": "\U0001F4F7 Photo", "gif": "\U0001F4F7 GIF",
                        "animated_gif": "\U0001F4F7 GIF",
                        "video": "\U0001F3AC Video",
                        "voice": "\U0001F3B5 Voice note", "audio": "\U0001F3B5 Audio",
                        "view_once_voice": "\U0001F441 View once voice note",
                        "view_once_image": "\U0001F441 View once photo",
                        "view_once_video": "\U0001F441 View once video",
                        "document": "\U0001F4C4 Document",
                        "sticker": "\U0001F36D Sticker",
                        "poll": "\U0001F4CA Poll", "poll_vote": "\U0001F4CA Poll",
                        "location": "\U0001F4CD Location",
                        "live_location": "\U0001F4CD Live Location",
                        "vcard": "\U0001F4C7 Contact", "vcard_list": "\U0001F4C7 Contacts",
                        "call_log": "\U0001F4DE Call",
                        "newsletter": "\U0001F4F0 Channel post",
                        "revoked": "\U0001F6AB Deleted message",
                    }
                    # Show the ACTUAL pinned text in the pin bar preview
                    # rather than a generic label. Strip newlines/whitespace
                    # so multi-line messages collapse cleanly into one row.
                    if _txt:
                        _prev = " ".join(_txt.split())[:100]
                    elif _tl in _pin_type_map:
                        _prev = _pin_type_map[_tl]
                    else:
                        # Empty text_content for type=text, or an unknown
                        # type_label — don't call it "text message" (which
                        # is noise); just show a clean pinned marker.
                        _prev = "\U0001F4CC Pinned message"
                    self._pin_previews.append((_sender, _prev))

                self._update_pin_display()
                self._pin_bar.setVisible(True)
            else:
                self._pin_bar.setVisible(False)
        except Exception:
            self._pin_bar.setVisible(False)

    def _update_pin_display(self) -> None:
        """Update pin bar labels to show current pin."""
        total = len(self._pinned_msg_ids)
        if total == 0:
            return
        idx = self._pin_index
        sender, preview = self._pin_previews[idx]
        self._pin_counter_label.setText(f"{idx + 1}/{total}")
        self._pin_sender_label.setText(sender or "Pinned message")
        self._pin_label.setText(preview)

    def _on_pin_bar_click(self, event) -> None:
        """Navigate to the current pinned message (click on text area)."""
        if not self._pinned_msg_ids:
            return
        msg_id = self._pinned_msg_ids[self._pin_index]
        self._navigate_to_message_id(msg_id)

    def _on_pin_next(self) -> None:
        """Cycle to next pinned message and navigate."""
        if len(self._pinned_msg_ids) < 2:
            return
        self._pin_index = (self._pin_index + 1) % len(self._pinned_msg_ids)
        self._update_pin_display()
        msg_id = self._pinned_msg_ids[self._pin_index]
        self._navigate_to_message_id(msg_id)

    def _on_pin_prev(self) -> None:
        """Cycle to previous pinned message and navigate."""
        if len(self._pinned_msg_ids) < 2:
            return
        self._pin_index = (self._pin_index - 1) % len(self._pinned_msg_ids)
        self._update_pin_display()
        msg_id = self._pinned_msg_ids[self._pin_index]
        self._navigate_to_message_id(msg_id)

    def _load_avatar(self, db: Database, conv_id: int, display_name: str) -> None:
        avatar_blob = db.scalar(
            "SELECT avatar_blob FROM conversation WHERE id = ?", (conv_id,)
        )

        if not avatar_blob:
            jid = db.scalar(
                "SELECT jid_raw_string FROM conversation WHERE id = ?", (conv_id,)
            )
            if jid:
                avatar_blob = db.scalar(
                    "SELECT avatar_blob FROM contact WHERE phone_jid = ? OR lid_jid = ?",
                    (jid, jid),
                )

        if avatar_blob and len(avatar_blob) > 100:
            pxm = QPixmap()
            pxm.loadFromData(avatar_blob)
            if not pxm.isNull():
                scaled = pxm.scaled(36, 36, Qt.KeepAspectRatioByExpanding,
                                    Qt.SmoothTransformation)
                self._avatar.setPixmap(scaled)
                self._avatar.setStyleSheet(
                    "QLabel { border-radius: 18px; }"
                )
                self._avatar.setText("")
                return

        initials = "".join(
            w[0] for w in display_name.split()[:2]
            if w and w[0].isalpha()
        )
        if not initials:
            initials = "#"
        colors = ["#00897b", "#6a1b9a", "#c62828", "#1565c0",
                  "#ef6c00", "#2e7d32", "#ad1457", "#4527a0"]
        avatar_bg = colors[conv_id % len(colors)]
        self._avatar.setPixmap(QPixmap())
        self._avatar.setText(initials[:2].upper())
        self._avatar.setStyleSheet(
            f"QLabel {{ background: {avatar_bg}; border-radius: 18px; "
            f"color: white; font-size: 14px; font-weight: bold; }}"
        )

    # ---- Search ----

    def _toggle_search(self):
        visible = not self._search_bar.isVisible()
        self._search_bar.setVisible(visible)
        if visible:
            self._chat_search.setFocus()
        else:
            self._chat_search.clear()
            self._search_results_panel.clear()
            if self._use_webengine:
                self._web_view.highlight_search("")
                self._web_view.set_search_target(None)
            if self._conv_id:
                self._model.search("")
                self._info_label.setText(f"{self._model.total_rows:,} messages")
            self._clear_all_filters_btn.setVisible(self._has_active_filters())

    def _toggle_debug(self):
        self._debug_visible = not self._debug_visible
        self._debug_panel.setVisible(self._debug_visible)
        if self._debug_visible:
            lt = self._tm.is_light
            accent = "#009688" if lt else "#00bcd4"
            self._debug_btn.setStyleSheet(f"""
                QPushButton {{ background: {'rgba(0,150,136,0.15)' if lt else 'rgba(0,188,212,0.15)'}; border: none;
                              border-radius: 14px; font-size: 13px; color: {accent}; }}
                QPushButton:hover {{ background: {'rgba(0,150,136,0.25)' if lt else 'rgba(0,188,212,0.25)'}; }}
            """)
        else:
            self._debug_btn.setStyleSheet(self._tm.chat_hdr_btn_style())

    def _toggle_download_panel(self):
        visible = not self._download_panel.isVisible()
        self._download_panel.setVisible(visible)
        if visible:
            conv_name = getattr(self, '_conv_name', '') or ''
            self._download_panel.load_conversation_stats(self._conv_id, conv_name)
            self._download_panel.load_global_stats()

    def _toggle_media_gallery(self):
        visible = not self._media_panel.isVisible()
        self._media_panel.setVisible(visible)
        if visible and self._conv_id is not None:
            self._media_panel.load_conversation(self._conv_id, self._is_group)
            lt = self._tm.is_light
            accent = "#009688" if lt else "#00bcd4"
            self._gallery_btn.setStyleSheet(f"""
                QPushButton {{ background: {'rgba(0,150,136,0.15)' if lt else 'rgba(0,188,212,0.15)'}; border: none;
                              border-radius: 14px; font-size: 13px; color: {accent}; }}
                QPushButton:hover {{ background: {'rgba(0,150,136,0.25)' if lt else 'rgba(0,188,212,0.25)'}; }}
            """)
        else:
            self._gallery_btn.setStyleSheet(self._tm.chat_hdr_btn_style())

    def _navigate_to_message_id(self, message_id: int):
        """Navigate to a message by its database ID (from media gallery click)."""
        if self._use_webengine:
            # Always go through tile-loading path which handles both loaded
            # and unloaded cases, then scrolls after ensuring tile is ready.
            self._on_scroll_to_unloaded(str(message_id))
            return
        # O(1) lookup via id_to_row map
        row = self._model.find_row_by_msg_id(message_id)
        if row >= 0:
            idx = self._model.index(row, 0)
            self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
            self._list.setCurrentIndex(idx)
            self._on_item_clicked(idx)
            return

        # Fallback: try key_id approach
        db = Database.get()
        key_id = db.scalar(
            "SELECT source_key_id FROM message WHERE id = ?", (message_id,)
        )
        if key_id:
            row = self._model.find_row_by_key_id(key_id)
            if row >= 0:
                idx = self._model.index(row, 0)
                self._list.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                self._list.setCurrentIndex(idx)
                self._on_item_clicked(idx)

    # ------------------------------------------------------------------
    # Admin-only-send / owner-permission banner
    # ------------------------------------------------------------------

    def _resolve_owner_phone(self, db) -> str | None:
        """Best-effort resolve of the device-owner phone number for display."""
        try:
            row = db.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'")
            if row and row[0]:
                return str(row[0])
            row = db.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_jid'")
            if row and row[0]:
                val = str(row[0])
                return val.split("@", 1)[0] if "@" in val else val
            # Fallback: pick the contact most frequently from_me=1
            row = db.fetchone(
                "SELECT c.phone_number FROM message m "
                "JOIN contact c ON c.id = m.sender_id "
                "WHERE m.from_me = 1 AND m.message_type != 7 "
                "GROUP BY m.sender_id ORDER BY COUNT(*) DESC LIMIT 1")
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _get_admin_send_policy(db, conv_id: int) -> dict:
        """Return admins-only-send policy for a group, per.

        PRIMARY source   : conversation.announcement_group (mirrored from
                           wa.db.wa_group_admin_settings.announcement_group)
                           — 1 = only admins can send; 0 = all members.
        TIMELINE source  : group_metadata_change rows where
                           change_type IN ('admin_only_send_on',
                                           'admin_only_send_off').
                           The LATEST one's timestamp tells us when the
                           current policy took effect.

        Returns dict with keys:
            announcement_only   : bool | None (None when unknown/not group)
            owner_ps            : participation_status (NULL/0/1/2/3/4)
            owner_can_send      : bool | None
            enforced_since_ts   : int | None  (ms when ON was last toggled)
            changed_at_ts       : int | None  (ms of LAST toggle, either way)
            last_change_type    : 'admin_only_send_on' | 'admin_only_send_off' | None
            changed_by_name     : str | None
            restrict_mode       : bool | None  — admins-only-edit group info
            member_add_mode     : bool | None  — admins-only-add-people
            source              : 'wa_db' | 'inferred_timeline' | 'none'
        """
        out = {
            "announcement_only": None, "owner_ps": None, "owner_can_send": None,
            "enforced_since_ts": None, "changed_at_ts": None,
            "last_change_type": None, "changed_by_name": None,
            "restrict_mode": None, "member_add_mode": None,
            "source": "none",
        }
        if not conv_id:
            return out
        try:
            conv = db.fetchone(
                "SELECT chat_type, announcement_group, restrict_mode, "
                "       member_add_mode, participation_status "
                "FROM conversation WHERE id = ?",
                (conv_id,),
            )
        except Exception:
            conv = None
        if not conv:
            return out
        ctype = conv["chat_type"]
        if ctype not in ("group", "community"):
            return out

        try:
            out["owner_ps"] = conv["participation_status"]
        except (IndexError, KeyError):
            pass

        # PRIMARY: wa_group_admin_settings mirror
        ann = None
        try:
            ann = conv["announcement_group"]
        except (IndexError, KeyError):
            ann = None
        if ann is not None:
            out["announcement_only"] = bool(ann)
            out["source"] = "wa_db"
            try:
                out["restrict_mode"] = bool(conv["restrict_mode"]) if conv["restrict_mode"] is not None else None
                out["member_add_mode"] = bool(conv["member_add_mode"]) if conv["member_add_mode"] is not None else None
            except (IndexError, KeyError):
                pass

        # TIMELINE: LATEST admin_only_send_{on,off} toggle
        try:
            row = db.fetchone(
                "SELECT gmc.change_type, gmc.timestamp, "
                "       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS changed_by "
                "FROM group_metadata_change gmc "
                "LEFT JOIN contact c ON c.id = gmc.changed_by_id "
                "WHERE gmc.conversation_id = ? "
                "  AND gmc.change_type IN ('admin_only_send_on','admin_only_send_off') "
                "ORDER BY gmc.timestamp DESC LIMIT 1",
                (conv_id,),
            )
            if row:
                out["last_change_type"] = row["change_type"]
                out["changed_at_ts"] = row["timestamp"]
                out["changed_by_name"] = row["changed_by"]
                # If wa.db didn't give us a value, infer from timeline
                if out["announcement_only"] is None:
                    out["announcement_only"] = (row["change_type"] == "admin_only_send_on")
                    out["source"] = "inferred_timeline"
                # enforced_since is the latest time we KNOW it turned ON
                if row["change_type"] == "admin_only_send_on":
                    out["enforced_since_ts"] = row["timestamp"]
                else:
                    # Search for the most recent ON before this OFF
                    prev = db.fetchone(
                        "SELECT timestamp FROM group_metadata_change "
                        "WHERE conversation_id = ? AND change_type = 'admin_only_send_on' "
                        "ORDER BY timestamp DESC LIMIT 1",
                        (conv_id,),
                    )
                    if prev:
                        out["enforced_since_ts"] = prev["timestamp"]
        except Exception:
            pass

        # Derive "can the owner send?"
        # participation_status ∈ {3,4}  → admin/creator → can always send
        # participation_status == 2     → regular member → blocked iff ann=1
        # participation_status == 1     → former member → cannot send
        # None                           → unknown → infer from ann only
        ps = out["owner_ps"]
        ann_only = out["announcement_only"]
        if ann_only is None:
            out["owner_can_send"] = None
        elif ps == 1:
            out["owner_can_send"] = False
        elif ps in (3, 4):
            out["owner_can_send"] = True
        elif ps == 2:
            out["owner_can_send"] = not ann_only
        else:  # ps is None — participation_status not ingested
            out["owner_can_send"] = not ann_only  # assume member
        return out

    def _update_chat_policy_banner(self, db, conv_id: int) -> None:
        """Show/hide the admins-only-send banner for group chats."""
        if not hasattr(self, "_policy_banner"):
            return
        info = self._get_admin_send_policy(db, conv_id)
        ann = info.get("announcement_only")
        if not ann:
            self._policy_banner.setVisible(False)
            return

        owner_phone = self._resolve_owner_phone(db) or "device owner"
        can_send = info.get("owner_can_send")

        def _fmt_ts(ts):
            if not ts:
                return ""
            try:
                from datetime import datetime as _dt
                return _dt.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                return ""

        enforced_str = _fmt_ts(info.get("enforced_since_ts"))
        changer = info.get("changed_by_name") or "an admin"

        # Compact single-line banner — keeps vertical space free
        # for messages instead of stacking onto multiple rows.
        bits: list[str] = ["\U0001F4E2 <b>Admins-only group</b>"]
        if owner_phone and can_send is False:
            bits.append(
                f"<b>{owner_phone}</b> "
                f"<span style='color:#ffcdd2'>can't send</span>"
            )
        elif owner_phone and can_send is True:
            bits.append(
                f"<b>{owner_phone}</b> "
                f"<span style='color:#c8e6c9'>is admin</span>"
            )
        if enforced_str:
            bits.append(
                f"<span style='opacity:0.80'>since {enforced_str}"
                + (f" by {changer}" if changer and changer != 'an admin' else "")
                + "</span>"
            )

        self._policy_banner.setText(
            "<div style='font-size:11px; line-height:1.3;'>"
            + " \u00B7 ".join(bits)   # middle-dot separators
            + "</div>"
        )
        self._policy_banner.setStyleSheet(
            "QLabel { background: #b71c1c; color: #ffffff; "
            "border-left: 3px solid #c62828; "
            "padding: 3px 10px; margin: 0; font-weight: 500; }"
        )
        self._policy_banner.setVisible(True)

    def _on_scroll_to_unloaded(self, msg_id_str: str):
        """JS reports msg_id not in tile map — load its tile then scroll."""
        try:
            msg_id = int(msg_id_str)
        except (ValueError, TypeError):
            return
        db = Database.get()
        if not db or not self._conv_id:
            return
        # Find message's global index using timestamp ordering (matches base query)
        try:
            where, params = self._model._where_clause()
            global_idx = db.scalar(
                f"SELECT COUNT(*) FROM message m WHERE {where}"
                " AND (m.timestamp, m.sort_id) < (SELECT timestamp, sort_id FROM message WHERE id = ?)",
                tuple(params) + (msg_id,),
            )
            if global_idx is not None and global_idx >= 0:
                tile_idx = global_idx // BATCH_SIZE
                tile_start = tile_idx * BATCH_SIZE
                tile_key = f"tile_{tile_idx}"
                loaded = getattr(self, '_loaded_tiles', set())
                if tile_key not in loaded:
                    # Load synchronously so data is available immediately
                    self._fetch_tile_sync(tile_start, BATCH_SIZE, tile_key)
                    self._loaded_tiles.add(tile_key)
                    # Also load adjacent tiles for scroll correction
                    for adj in [tile_idx - 1, tile_idx + 1]:
                        adj_key = f"tile_{adj}"
                        adj_start = adj * BATCH_SIZE
                        if adj >= 0 and adj_start < self._model._total and adj_key not in loaded:
                            self._fetch_tile_sync(adj_start, BATCH_SIZE, adj_key)
                            self._loaded_tiles.add(adj_key)
                # Tile data is now delivered — scroll after a
                # short delay so JS has processed setMessagesAt
                # and populated idToGlobal.  A single retry only:
                # multiple timers cause the target bubble to
                # "dance" because each scroll rebuilds the DOM
                # and fights the preceding one mid-animation.
                # JS's own pending-retry in loadMessages handles
                # the race where the tile arrives slightly after
                # this signal.
                from PySide6.QtCore import QTimer
                _nav_gen = getattr(self, '_prog_gen', 0)
                QTimer.singleShot(
                    80,
                    lambda gen=_nav_gen, mid=msg_id: (
                        self._web_view.scroll_to_message(mid)
                        if getattr(self, '_prog_gen', 0) == gen else None
                    ),
                )
        except Exception:
            pass

    def _on_scroll_to_key_unloaded(self, key_id: str):
        """JS reports key_id not in tile map — resolve to msg_id and load."""
        db = Database.get()
        if not db:
            return
        try:
            msg_id = db.scalar(
                "SELECT id FROM message WHERE source_key_id = ? AND conversation_id = ?",
                (key_id, self._conv_id),
            )
            if msg_id:
                self._on_scroll_to_unloaded(str(msg_id))
        except Exception:
            pass

    def _on_comments_click(self, msg_id_str: str):
        """Show comment thread for a channel message."""
        try:
            msg_id = int(msg_id_str)
        except (ValueError, TypeError):
            return
        db = Database.get()
        if not db:
            return
        try:
            # Fetch comment replies with sender info
            rows = db.fetchall(
                "SELECT m.id, m.text_content, m.timestamp, m.from_me,"
                " CASE WHEN c.is_saved = 1 AND c.display_name IS NOT NULL AND c.display_name != ''"
                "      THEN c.display_name"
                "      WHEN c.wa_name IS NOT NULL AND c.wa_name != '' THEN '~' || c.wa_name"
                "      WHEN c.phone_number IS NOT NULL AND c.phone_number != '' THEN '+' || c.phone_number"
                "      ELSE 'Unknown' END,"
                " c.phone_number"
                " FROM message_comment mc"
                " JOIN message m ON m.id = mc.reply_message_id"
                " LEFT JOIN contact c ON c.id = m.sender_id"
                " WHERE mc.parent_message_id = ?"
                " ORDER BY m.timestamp ASC",
                (msg_id,),
            )
            if not rows:
                return

            from PySide6.QtWidgets import QDialog, QVBoxLayout, QScrollArea, QLabel, QFrame
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Replies ({len(rows)})")
            dlg.setFixedSize(420, 500)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(0, 0, 0, 0)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            container = QWidget()
            cl = QVBoxLayout(container)
            cl.setContentsMargins(12, 8, 12, 8)
            cl.setSpacing(6)

            _on = getattr(self, '_owner_name', '') or ''
            _op = getattr(self, '_owner_phone', '') or ''

            for row in rows:
                rid, text, ts, from_me, sender, phone = row
                if from_me:
                    sender = f"{_on} (+{_op})" if _on and _op else _on or "You"
                    phone = _op
                frame = QFrame()
                frame.setStyleSheet(
                    "QFrame { background: #f5f5f5; border-radius: 8px; padding: 8px; }"
                )
                fl = QVBoxLayout(frame)
                fl.setContentsMargins(8, 6, 8, 6)
                fl.setSpacing(2)

                header = QLabel(f"<b style='color:#00796b;'>{sender or 'Unknown'}</b>"
                               + (f"  <span style='color:#999;font-size:10px;'>+{phone}</span>" if phone else ""))
                header.setTextFormat(Qt.RichText)
                fl.addWidget(header)

                if text:
                    body = QLabel(text)
                    body.setWordWrap(True)
                    body.setStyleSheet("font-size: 13px; color: #333;")
                    fl.addWidget(body)

                if ts:
                    ts_label = QLabel(self._format_ts(ts))
                    ts_label.setStyleSheet("font-size: 10px; color: #999;")
                    fl.addWidget(ts_label)

                cl.addWidget(frame)

            cl.addStretch()
            scroll.setWidget(container)
            layout.addWidget(scroll)
            dlg.exec()
        except Exception as e:
            print(f"[ChatViewerPage] Comments error: {e}")

    def _on_navigate_to_chat(self, conv_id: int, msg_id: int):
        """Handle navigate_to_chat from media panel (e.g. shared chats dialog)."""
        if conv_id == self._conv_id:
            # Same conversation -- just scroll to the message
            self._navigate_to_message_id(msg_id)
        else:
            # Different conversation -- emit signal for main_window to handle
            self.conversation_switch_requested.emit(conv_id, msg_id)

    def _do_search(self):
        query = self._chat_search.text().strip()
        if self._conv_id is None:
            return
        # Mutual exclusion: close replies sidebar when search is active
        if query and hasattr(self, '_replies_sidebar'):
            self._replies_sidebar.setVisible(False)
        if self._use_webengine:
            self._web_view.highlight_search(query)
            self._search_results = []
            self._search_idx = -1
            if query and len(query) >= 2:
                try:
                    db = Database.get()
                    # Get search options from panel
                    case_sens = self._search_results_panel.case_sensitive
                    exact = self._search_results_panel.exact_match
                    pat = query if exact else f"%{query}%"

                    # Build LIKE/GLOB clause based on options
                    if case_sens:
                        # SQLite GLOB is case-sensitive
                        glob_pat = f"*{query}*" if not exact else query
                        match_op = "GLOB"
                        match_pat = glob_pat
                    else:
                        match_op = "LIKE"
                        match_pat = pat

                    rows = db.fetchall(
                        f"SELECT m.id,"
                        f" COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),"
                        f"   NULLIF(c.wa_name,''),"
                        f"   CASE WHEN m.from_me=1 THEN 'Me' ELSE 'Unknown' END) AS sender_name,"
                        f" m.text_content,"
                        f" COALESCE(me.media_caption, '') AS media_caption,"
                        f" m.timestamp, m.type_label, m.message_type,"
                        f" m.from_me, m.sender_id,"
                        f" COALESCE(c.phone_number, '') AS phone_number,"
                        f" (SELECT ld.page_title FROM message_link_detail ld"
                        f"  WHERE ld.message_id = m.id LIMIT 1) AS link_title,"
                        f" (SELECT ld.description FROM message_link_detail ld"
                        f"  WHERE ld.message_id = m.id LIMIT 1) AS link_desc"
                        f" FROM message m"
                        f" LEFT JOIN contact c ON c.id = m.sender_id"
                        f" LEFT JOIN media me ON me.message_id = m.id"
                        f" WHERE m.conversation_id = ?"
                        f"   AND (m.text_content {match_op} ? OR me.media_caption {match_op} ?"
                        f"     OR m.id IN (SELECT ld.message_id FROM message_link_detail ld"
                        f"       WHERE ld.page_title {match_op} ? OR ld.description {match_op} ?))"
                        f" ORDER BY m.timestamp DESC LIMIT 5000",
                        (self._conv_id, match_pat, match_pat, match_pat, match_pat),
                    )
                    tagged_ids = self._model._get_tagged_ids() if hasattr(self._model, '_get_tagged_ids') else set()
                    results = []
                    q_lower = query.lower()
                    for r in rows:
                        msg_id = r[0]
                        text_content = r[2] or ""
                        media_caption = r[3] or ""
                        phone_number = r[9] or ""
                        link_title = r[10] or ""
                        link_desc = r[11] or ""

                        # Pick best snippet: prefer the field that actually matched
                        snippet = text_content
                        check = text_content if case_sens else text_content.lower()
                        check_q = query if case_sens else q_lower
                        if check_q not in check:
                            cap_check = media_caption if case_sens else media_caption.lower()
                            if check_q in cap_check:
                                snippet = media_caption
                            elif link_title or link_desc:
                                lt_check = link_title if case_sens else link_title.lower()
                                ld_check = link_desc if case_sens else link_desc.lower()
                                if check_q in lt_check:
                                    snippet = link_title
                                elif check_q in ld_check:
                                    snippet = link_desc
                                else:
                                    snippet = link_title or link_desc
                            else:
                                snippet = text_content or media_caption

                        results.append({
                            "id": msg_id,
                            "sender_name": r[1] or "Unknown",
                            "snippet": snippet,
                            "timestamp": r[4],
                            "type_label": r[5] or "text",
                            "message_type": r[6],
                            "from_me": bool(r[7]),
                            "sender_id": r[8],
                            "phone": phone_number,
                            "is_tagged": msg_id in tagged_ids,
                        })
                    self._search_results = [r["id"] for r in results]
                    count = len(results)
                    self._info_label.setText(f"{count:,} matches for \"{query}\"")
                    self._search_results_panel.load_results(query, results)
                    if results:
                        self._search_idx = 0
                        first_id = results[0]["id"]
                        self._web_view.scroll_to_message(first_id)
                        self._web_view.set_search_target(first_id)
                        self._search_results_panel.set_current(first_id)
                except Exception as e:
                    print(f"[Search] Error: {e}")
                    import traceback; traceback.print_exc()
            elif not query:
                self._info_label.setText(f"{self._model._total:,} messages")
                self._search_results_panel.clear()
                self._web_view.set_search_target(None)
            return
        # Non-webengine fallback (filter-based search)
        self._model.search(query)
        count = self._model.total_rows
        if query:
            self._info_label.setText(f"{count:,} matches for \"{query}\"")
        else:
            self._info_label.setText(f"{count:,} messages")
        self._clear_all_filters_btn.setVisible(self._has_active_filters())

    def _on_search_result_selected(self, msg_id: int):
        """Handle click on a search result / replies-sidebar original-message link.

        Reused by the replies sidebar so when you click "Go to original message"
        there IS no active search. In that case we must NOT fake a
        "0 matches for ''" label — just show the normal message count.
        """
        if self._use_webengine and msg_id:
            self._info_label.setText("Navigating to message...")
            QApplication.processEvents()
            self._web_view.scroll_to_message(msg_id)
            self._web_view.set_search_target(msg_id)
            query = self._chat_search.text().strip()
            if query:
                self._web_view.highlight_search(query)
                # Position-in-results label only when we're actually in a search
                idx = -1
                for i, rid in enumerate(self._search_results):
                    if rid == msg_id:
                        idx = i + 1
                        break
                total = len(self._search_results)
                if idx > 0:
                    self._info_label.setText(f"Result {idx} of {total} for \"{query}\"")
                else:
                    self._info_label.setText(f"{total} matches for \"{query}\"")
            else:
                # No active search — restore the plain message count
                self._info_label.setText(f"{self._model._total:,} messages")

    def _on_search_enter(self):
        """Handle Enter key in search box — navigate to next result or trigger search."""
        if self._search_results:
            self._search_results_panel.navigate_next()
        else:
            self._do_search()

    def _format_ts(self, ts) -> str:
        if not ts:
            return "N/A"
        return format_timestamp_with_utc(ts, "full") or str(ts)

    def _format_ts_full(self, ts) -> str:
        """Full timestamp with milliseconds + timezone for forensic display."""
        if not ts:
            return "--"
        return format_timestamp_with_utc(ts, "full") or str(ts)

    # ---- Item click / detail bar ----

    def _on_item_clicked(self, index: QModelIndex):
        msg = index.data(MSG_DATA_ROLE)
        if not msg or msg.get("message_type") == -1:
            return

        ts = msg.get("timestamp")
        ts_str = format_timestamp_with_utc(ts, "full") if ts else ""

        parts = [f"ID: {msg.get('id')}"]
        if msg.get("sender_name"):
            parts.append(f"From: {msg['sender_name']}")
        if ts_str:
            parts.append(ts_str)
        if msg.get("type_label"):
            parts.append(f"Type: {msg['type_label']}")
        status = msg.get("status", 0)
        if msg.get("from_me"):
            status_map = {0: "pending", 4: "sent", 5: "delivered",
                          6: "read", 13: "read/played"}
            parts.append(f"Status: {status_map.get(status, f'code {status}')}")
        flags = []
        if msg.get("is_starred"): flags.append("starred")
        if msg.get("is_forwarded"): flags.append(f"fwd(x{msg.get('forward_score', '?')})")
        if msg.get("is_edited"): flags.append("edited")
        if msg.get("is_revoked"): flags.append("revoked")
        if msg.get("is_ghost"): flags.append("\u2718 ghost")
        if msg.get("is_view_once"): flags.append("view-once")
        if msg.get("is_ephemeral"): flags.append("disappearing")
        if msg.get("is_bot_message"): flags.append("bot")
        if flags:
            parts.append(", ".join(flags))
        self._detail_label.setText("  |  ".join(parts))

        if self._debug_visible:
            self._update_debug_panel(msg)

    # ---- Debug panel ----

    def _update_debug_panel(self, msg: dict) -> None:
        lines = []

        # ===== TIMESTAMPS (most important — always at top) =====
        ts = msg.get("timestamp")
        first_delivered = msg.get("first_delivered_ts")
        first_read = msg.get("first_read_ts")

        lines.append("SENT          " + self._format_ts_full(ts))
        if first_delivered:
            delay_d = f"  (+{(first_delivered - ts) / 1000:.3f}s)" if ts else ""
            lines.append(f"DELIVERED  \u2713\u2713  {self._format_ts_full(first_delivered)}{delay_d}")
        else:
            lines.append("DELIVERED  \u2713\u2713  --")
        if first_read:
            delay_r = f"  (+{(first_read - ts) / 1000:.3f}s)" if ts else ""
            lines.append(f"READ/SEEN  \u2713\u2713  {self._format_ts_full(first_read)}{delay_r}")
        else:
            lines.append("READ/SEEN  \u2713\u2713  --")
        lines.append("")

        # ===== IDENTITY =====
        lines.append(f"ID  {msg.get('id')}  |  src_id {msg.get('source_msg_id')}  |  key {msg.get('source_key_id')}")
        sender = msg.get("sender_name") or "Me"
        jid = msg.get("phone_jid") or msg.get("lid_jid") or ""
        lines.append(f"{'OUTGOING' if msg.get('from_me') else 'INCOMING'}  {sender}  {jid}")

        # Device
        dev_num = msg.get("sender_device_number", -1)
        origin = msg.get("origin", 0)
        if msg.get("from_me"):
            dev_str = f"origin={origin}" if origin else "phone"
        elif dev_num is not None and dev_num >= 0:
            dev_str = "Phone" if dev_num == 0 else f"Web/Desktop #{dev_num}"
        else:
            dev_str = ""
        if dev_str:
            lines.append(f"Device: {dev_str}")

        # Origination flags — pure bitmask.  Each bit is
        # independent; the composite integer values seen in
        # msgstore are OR-combinations of the flags below.
        oflags = msg.get("origination_flags", 0)
        if oflags:
            bits = []
            if oflags & 1:           bits.append("Forwarded")
            if oflags & 64:          bits.append("Multi-contact image")
            if oflags & 256:         bits.append("Ephemeral")
            if oflags & 512:         bits.append("System message")
            if oflags & 2048:        bits.append("Document/URL/Status video")
            if oflags & 32768:       bits.append("Voice note")
            if oflags & 131072:      bits.append("Edited/Meta AI")
            if oflags & 67108864:    bits.append("Multimedia album")
            if oflags & 536870912:   bits.append("Scheduled event")
            known_mask = 1 | 64 | 256 | 512 | 2048 | 32768 | 131072 | 67108864 | 536870912
            residual = oflags & ~known_mask
            if residual:
                bits.append(f"unknown bits 0x{residual:x}")
            if bits:
                lines.append(f"Flags: {', '.join(bits)} (raw: {oflags} = 0x{oflags:x})")

        # Type & status
        status = msg.get("status", 0)
        status_map = {0: "recv", 4: "server", 5: "delivered", 6: "read", 13: "played"}
        tl = msg.get("type_label") or ""
        lines.append(f"Type: {tl}  |  Status: {status_map.get(status, str(status))}")

        # Active flags (only show True ones)
        active = []
        for flag in ("is_starred", "is_forwarded", "is_edited", "is_revoked",
                     "is_ghost", "is_view_once", "is_ephemeral", "is_bot_message"):
            if msg.get(flag):
                active.append(flag.replace("is_", ""))
        if active:
            lines.append(f"[{', '.join(active)}]")

        # Media info (compact)
        if msg.get("has_thumb") or msg.get("file_path"):
            lines.append("")
            mime = msg.get("mime_type") or ""
            fs = msg.get("file_size") or 0
            size_str = f"{fs/1048576:.1f}MB" if fs > 1048576 else f"{fs//1024}KB" if fs > 1024 else f"{fs}B" if fs else ""
            lines.append(f"Media: {mime}  {size_str}")
            import os as _dbg_os
            # Use resolved_file_path first (absolute), fall back to file_path resolution
            resolved = msg.get("resolved_file_path") or ""
            if resolved and _dbg_os.path.exists(resolved):
                lines.append(f"Disk: {resolved}")
            else:
                from app.views.widgets.bubble_delegate import _resolve_media_path
                fallback = _resolve_media_path(msg.get("file_path", ""))
                lines.append(f"Disk: {fallback or 'NOT FOUND'}")

        self._debug_text.setPlainText("\n".join(lines))

    # ---- Context menu ----

    def _show_context_menu_for_msg(self, msg: dict, global_pos: QPoint):
        """Show context menu for a message dict at a screen position (used by WebEngine)."""
        if not msg or msg.get("message_type") == -1:
            return
        self._build_and_show_context_menu(msg, global_pos)

    def _show_context_menu(self, pos):
        if not self._list:
            return
        index = self._list.indexAt(pos)
        msg = index.data(MSG_DATA_ROLE) if index.isValid() else None
        if not msg or msg.get("message_type") == -1:
            return
        global_pos = self._list.viewport().mapToGlobal(pos)
        self._build_and_show_context_menu(msg, global_pos)

    def _build_and_show_context_menu(self, msg: dict, global_pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet(self._tm.context_menu_style())

        text = msg.get("display_text", "")

        # Copy message
        copy_act = QAction("Copy Message Text  (Ctrl+C)", self)
        copy_act.triggered.connect(
            lambda: (QApplication.clipboard().setText(text), self._show_copy_toast("Text copied!"))
        )
        menu.addAction(copy_act)

        # Select & Copy Text (dialog with selectable QTextEdit)
        if text:
            select_act = QAction("Select & Copy Text...", self)
            select_act.triggered.connect(lambda checked=False, m=msg: self._show_text_dialog(m))
            menu.addAction(select_act)

        # Copy with timestamp
        ts = msg.get("timestamp")
        ts_str = format_timestamp_with_utc(ts, "full") if ts else ""
        detail_text = f"[{ts_str}] {msg.get('sender_name', '')}: {text}"
        copy_detail = QAction("Copy with Timestamp", self)
        copy_detail.triggered.connect(
            lambda: QApplication.clipboard().setText(detail_text)
        )
        menu.addAction(copy_detail)

        # Copy Message Info (structured format)
        sender_name = msg.get("sender_name", "You" if msg.get("from_me") else "Unknown")
        info_text = f"Sender: {sender_name}\nTime: {ts_str}\nText: {text}"
        copy_info = QAction("Copy Message Info", self)
        copy_info.triggered.connect(
            lambda: (QApplication.clipboard().setText(info_text),
                     self._show_copy_toast("Message info copied!"))
        )
        menu.addAction(copy_info)

        # Copy message ID
        msg_id = msg.get("id")
        copy_id_act = QAction("Copy Message ID", self)
        copy_id_act.triggered.connect(
            lambda: QApplication.clipboard().setText(str(msg_id))
        )
        menu.addAction(copy_id_act)

        # Copy key ID
        key_id = msg.get("source_key_id")
        if key_id:
            copy_key_act = QAction("Copy Key ID", self)
            copy_key_act.triggered.connect(
                lambda: QApplication.clipboard().setText(str(key_id))
            )
            menu.addAction(copy_key_act)

        # Copy links from message
        import re
        urls = re.findall(r'https?://\S+', text)
        if urls:
            menu.addSeparator()
            for url in urls[:5]:
                short = url[:50] + "..." if len(url) > 50 else url
                link_act = QAction(f"Copy: {short}", self)
                link_act.triggered.connect(
                    lambda checked=False, u=url: QApplication.clipboard().setText(u)
                )
                menu.addAction(link_act)

        menu.addSeparator()

        # Open media file
        file_path = msg.get("file_path")
        resolved_fp = msg.get("resolved_file_path")
        if file_path:
            from app.views.widgets.bubble_delegate import _resolve_media_path
            resolved = _resolve_media_path(file_path, resolved_fp)
            if resolved:
                open_media = QAction("Open Media File", self)
                open_media.triggered.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(resolved))
                )
                menu.addAction(open_media)

                copy_path = QAction("Copy File Path", self)
                copy_path.triggered.connect(
                    lambda: QApplication.clipboard().setText(resolved)
                )
                menu.addAction(copy_path)
            elif msg.get("media_url") and msg.get("media_key"):
                # Media not on disk but has download URL + key
                dl_act = QAction("\u21E9  Download & Decrypt Media", self)
                dl_act.triggered.connect(
                    lambda checked=False, m=msg: self._download_decrypt_media(m)
                )
                menu.addAction(dl_act)

        # Find Copies (exact SHA-256 match) — opens a dialog with every row that
        # shares the same file_hash OR enc_file_hash. Shows JID, phone, and
        # Go-to-Chat buttons. Available for ANY media-bearing message (including
        # owner-sent ones whose file_hash is sometimes NULL — the dialog also
        # tries enc_file_hash and reports clearly if neither is populated).
        has_media = bool(
            msg.get("file_hash") or msg.get("mime_type") or msg.get("file_path")
            or (msg.get("type_label") in (
                "image", "video", "gif", "sticker", "voice", "ptt",
                "audio", "document", "view_once_image", "view_once_video"))
        )
        if has_media:
            copies_act = QAction("\U0001F517 Find Copies (exact SHA-256)", self)
            copies_act.setToolTip(
                "Dialog list of every chat/sender/timestamp that shared this exact file "
                "(matched by SHA-256 or enc_file_hash). Go-to-Chat buttons on each row."
            )
            copies_act.triggered.connect(
                lambda checked=False, mid=msg_id: self._show_hash_matches(mid)
            )
            menu.addAction(copies_act)

        # Find Similar Images (perceptual pHash/dHash) — opens Image Similarity page.
        # Different tool: catches resizes, recompressions, template matches.
        tl = msg.get("type_label", "")
        show_similar = (
            tl in ("image", "sticker", "gif", "animated_gif", "view_once_image")
            or (msg.get("mime_type") or "").startswith("image/")
        )
        if show_similar:
            sim_act = QAction("\U0001F50D Find Similar Images (perceptual)", self)
            sim_act.setToolTip("Open the Image Similarity page for near-duplicates, "
                               "resizes, and template matches (pHash + dHash).")
            sim_act.triggered.connect(
                lambda checked=False, mid=msg_id: self.find_similar_requested.emit(mid)
            )
            menu.addAction(sim_act)

        # Go to quoted message
        reply_key = msg.get("reply_to_key_id")
        if reply_key:
            goto_quote = QAction("Go to Quoted Message", self)
            goto_quote.triggered.connect(
                lambda: self._navigate_to_quoted(reply_key)
            )
            menu.addAction(goto_quote)

        # View sender profile
        sender_id = msg.get("sender_id")
        if sender_id:
            view_sender = QAction("View Sender Profile", self)
            view_sender.triggered.connect(
                lambda: self.contact_requested.emit(sender_id)
            )
            menu.addAction(view_sender)

        # Reactions detail
        reaction_count = msg.get("reaction_count", 0)
        if reaction_count and reaction_count > 0:
            react_act = QAction(f"View Reactions ({reaction_count})", self)
            react_act.triggered.connect(
                lambda checked=False, mid=msg_id: self._show_reactions_detail(mid)
            )
            menu.addAction(react_act)

        # Message tagging for investigator
        menu.addSeparator()
        is_tagged = self._is_message_tagged(msg_id)
        if is_tagged:
            untag_act = QAction("\u2691  Remove Tag", self)
            untag_act.triggered.connect(
                lambda checked=False, mid=msg_id: self._untag_message(mid)
            )
            menu.addAction(untag_act)
        else:
            tag_act = QAction("\u2691  Tag Message", self)
            tag_act.triggered.connect(
                lambda checked=False, mid=msg_id: self._tag_message(mid)
            )
            menu.addAction(tag_act)

        # "View in full context" - only when a filter is active
        if self._has_active_filters() and msg_id and msg_id > 0:
            goto_act = QAction("\u2192  View in Full Context", self)
            goto_act.triggered.connect(
                lambda checked=False, mid=msg_id: self._goto_message_in_full_chat(mid)
            )
            menu.addAction(goto_act)

        menu.addSeparator()

        # Forensic Info panel (opens right-side panel with full provenance)
        forensic_act = QAction("\U0001F50D  Forensic Info", self)
        forensic_act.triggered.connect(
            lambda checked=False, mid=msg_id: self._on_forensic_info_request(mid)
        )
        menu.addAction(forensic_act)

        # Copy debug info
        copy_debug = QAction("Copy Debug Info", self)
        copy_debug.triggered.connect(
            lambda: self._copy_debug_info(msg)
        )
        menu.addAction(copy_debug)

        # Copy all visible
        copy_all = QAction("Copy All Visible", self)
        copy_all.triggered.connect(self._copy_all_visible)
        menu.addAction(copy_all)

        # Need QUrl/QDesktopServices import for open media
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        menu.exec(global_pos)

    def _copy_debug_info(self, msg: dict):
        self._update_debug_panel(msg)
        QApplication.clipboard().setText(self._debug_text.toPlainText())

    def _show_text_dialog(self, msg: dict):
        """Open a dialog with selectable text for a single message."""
        from PySide6.QtWidgets import QDialog
        text = msg.get("display_text", "") or msg.get("text_content", "")
        if not text:
            return
        sender = msg.get("sender_name", "You" if msg.get("from_me") else "Unknown")
        ts = msg.get("timestamp")
        ts_str = format_timestamp_with_utc(ts, "full") if ts else ""

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Select Text — {sender}")
        dlg.resize(550, 350)
        _lt = self._tm.is_light
        dlg.setStyleSheet(
            "QDialog { background: #ffffff; }" if _lt else
            "QDialog { background: #111b21; }"
        )
        lay = QVBoxLayout(dlg)

        header = QLabel(f"{sender}  •  {ts_str}")
        header.setStyleSheet(
            f"color: {'#667781' if _lt else '#8696a0'}; font-size: 10px; padding: 2px;"
        )
        lay.addWidget(header)

        te = QTextEdit()
        te.setReadOnly(True)
        te.setPlainText(text)
        te.setFont(QFont("Segoe UI", self._font_size))
        te.setStyleSheet(
            "QTextEdit { background: #ffffff; color: #111b21; border: 1px solid #e0e3e7; "
            "border-radius: 4px; padding: 6px; }"
            if _lt else
            "QTextEdit { background: #202c33; color: #e9edef; border: 1px solid #2a3942; "
            "border-radius: 4px; padding: 6px; }"
        )
        lay.addWidget(te, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        copy_btn = QPushButton("Copy All")
        copy_btn.setStyleSheet(self._tm.chat_date_apply_btn_style())
        copy_btn.clicked.connect(
            lambda: (QApplication.clipboard().setText(text),
                     self._show_copy_toast("Copied!"))
        )
        btn_row.addWidget(copy_btn)
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(self._tm.chat_date_clear_btn_style())
        close_btn.clicked.connect(dlg.close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    # ---- Message tagging for investigators ----

    def _ensure_tag_table(self):
        """Create message_tag table if it doesn't exist."""
        if getattr(self, "_tag_table_checked", False):
            return
        try:
            db = Database.get()
            db.execute_write("""
                CREATE TABLE IF NOT EXISTS message_tag (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  INTEGER NOT NULL UNIQUE REFERENCES message(id),
                    tag_label   TEXT DEFAULT 'flagged',
                    note        TEXT DEFAULT '',
                    tagged_at   TEXT DEFAULT (datetime('now')),
                    tagged_by   TEXT DEFAULT 'investigator'
                )
            """)
            db.execute_write("""
                CREATE INDEX IF NOT EXISTS idx_message_tag_mid
                ON message_tag(message_id)
            """)
            db.checkpoint_and_reconnect()
            self._tag_table_checked = True
        except Exception as e:
            print(f"[Tag] Table creation error: {e}")

    def _is_message_tagged(self, msg_id: int) -> bool:
        if not msg_id:
            return False
        return msg_id in self._model._get_tagged_ids()

    def _tag_message(self, msg_id: int):
        self._ensure_tag_table()
        try:
            db = Database.get()
            db.execute_write(
                "INSERT OR IGNORE INTO message_tag (message_id) VALUES (?)",
                (msg_id,)
            )
            self._model._tagged_ids.add(msg_id)
            # Update the msg dict in the model for immediate visual feedback
            if msg_id in self._model._id_to_row:
                row = self._model._id_to_row[msg_id]
                if 0 <= row < len(self._model._data):
                    self._model._data[row]["is_tagged"] = True
            self._show_copy_toast("\u2691 Message tagged")
            if self._use_webengine:
                self._web_view.update_tagged_messages(self._model._tagged_ids)
            elif self._list:
                self._list.viewport().update()
        except Exception as e:
            QMessageBox.warning(self, "Tag Error", str(e))

    def _untag_message(self, msg_id: int):
        self._ensure_tag_table()
        try:
            db = Database.get()
            db.execute_write(
                "DELETE FROM message_tag WHERE message_id = ?",
                (msg_id,)
            )
            self._model._tagged_ids.discard(msg_id)
            if msg_id in self._model._id_to_row:
                row = self._model._id_to_row[msg_id]
                if 0 <= row < len(self._model._data):
                    self._model._data[row]["is_tagged"] = False
            self._show_copy_toast("Tag removed")
            if self._use_webengine:
                self._web_view.update_tagged_messages(self._model._tagged_ids)
            elif self._list:
                self._list.viewport().update()
        except Exception as e:
            QMessageBox.warning(self, "Tag Error", str(e))

    def _on_viewer_tag(self, item: dict):
        """Handle tag request from MediaViewerDialog."""
        msg_id = item.get("message_id")
        if msg_id:
            self._tag_message(msg_id)

    def _on_viewer_goto(self, item: dict):
        """Handle go-to-chat request from MediaViewerDialog — scroll to message."""
        msg_id = item.get("message_id")
        if not msg_id or not hasattr(self._model, '_id_to_row'):
            return
        if self._use_webengine:
            self._web_view.scroll_to_message(msg_id)
            self._on_scroll_to_unloaded(str(msg_id))
            return
        row = self._model._id_to_row.get(msg_id)
        if row is not None:
            idx = self._model.index(row, 0)
            self._list.scrollTo(idx, self._list.ScrollHint.PositionAtCenter)
            self._list.setCurrentIndex(idx)

    def _copy_all_visible(self):
        lines = []
        for i in range(min(self._model.rowCount(), 500)):
            msg = self._model.data(self._model.index(i), MSG_DATA_ROLE)
            if msg and msg.get("message_type") != -1:
                ts = msg.get("timestamp")
                ts_str = ""
                if ts:
                    try:
                        ts_str = format_timestamp(ts, "minute")
                    except (ValueError, OSError):
                        pass
                sender = msg.get("sender_name", "You" if msg.get("from_me") else "?")
                text = msg.get("display_text", msg.get("type_label", ""))
                ghost_tag = " [GHOST]" if msg.get("is_ghost") else ""
                lines.append(f"[{ts_str}] {sender}: {text}{ghost_tag}")
        QApplication.clipboard().setText("\n".join(lines))

    def _forensic_log(self, action: str, details: dict) -> None:
        """Append a timestamped entry to the chain of custody log."""
        try:
            from app.services.chain_of_custody import ChainOfCustody
            coc = ChainOfCustody.get()
            if not coc._log_path:
                # Initialize if not already
                from app.services.case_manager import CaseManager
                cm = CaseManager.get()
                if cm.is_open and cm.case_path:
                    coc.initialize(cm.case_path)
            coc.log(action, details)
        except Exception:
            pass

    def _download_decrypt_media(self, msg: dict):
        """Download and decrypt a single media file from WhatsApp CDN."""
        import os, hashlib

        msg_id = msg.get("id", 0)
        url = msg.get("media_url")
        key = msg.get("media_key")
        if not url or not key:
            QMessageBox.warning(self, "Error", "No URL or key available for this media.")
            self._forensic_log("download_rejected", {
                "message_id": msg_id, "reason": "no_url_or_key",
                "conversation_id": self._conv_id,
            })
            return

        self._forensic_log("download_started", {
            "message_id": msg_id,
            "conversation_id": self._conv_id,
            "type_label": msg.get("type_label", ""),
            "mime_type": msg.get("mime_type", ""),
            "file_hash_expected": msg.get("file_hash", ""),
            "url_domain": url.split("/")[2] if url and "/" in url else "",
        })

        try:
            from app.services.media_crypto import (
                download_and_decrypt, get_media_type, get_extension_for_mime,
            )

            media_type = get_media_type(msg.get("type_label", ""), msg.get("mime_type", ""))
            ext = get_extension_for_mime(msg.get("mime_type", "")) if msg.get("mime_type") else ".bin"

            # Prefer case recovered_media dir, fallback to backend output
            from app.services.case_manager import CaseManager
            cm = CaseManager.get()
            if cm.is_open and cm.recovered_media_dir:
                save_dir = str(cm.recovered_media_dir)
            else:
                save_dir = os.path.join(os.path.dirname(str(self._db.path)), "recovered_media")
            os.makedirs(save_dir, exist_ok=True)
            import time as _time_mod
            _dl_ts = int(_time_mod.time())
            save_path = os.path.join(save_dir, f"Recovered_msg_media_{msg_id}_{_dl_ts}{ext}")

            plaintext = download_and_decrypt(
                url=url, media_key=key, media_type=media_type,
                file_hash=msg.get("file_hash"), save_path=save_path, timeout=15,
            )

            # Compute SHA-256 of the decrypted file for chain of custody
            sha256 = hashlib.sha256(plaintext).hexdigest()

            self._forensic_log("download_success", {
                "message_id": msg_id,
                "conversation_id": self._conv_id,
                "save_path": save_path,
                "size_bytes": len(plaintext),
                "sha256": sha256,
                "file_hash_expected": msg.get("file_hash", ""),
                "media_type": media_type,
                "mime_type": msg.get("mime_type", ""),
            })

            # Update DB: map the resolved path and mark as exists
            db = Database.get()
            media_id = db.scalar(
                "SELECT id FROM media WHERE message_id = ? LIMIT 1",
                (msg.get("id"),),
            )
            if media_id:
                import time as _time_mod
                recovery_ts = int(_time_mod.time() * 1000)
                db.execute_write(
                    "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                    "recovery_method = 'downloaded', recovery_timestamp = ? WHERE id = ?",
                    (save_path, recovery_ts, media_id),
                )
                # Link all copies with same file_hash
                file_hash = msg.get("file_hash")
                if file_hash:
                    linked = db.scalar(
                        "SELECT COUNT(*) FROM media WHERE file_hash = ? AND id != ? "
                        "AND (file_exists = 0 OR file_exists IS NULL)",
                        (file_hash, media_id),
                    ) or 0
                    db.execute_write(
                        "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                        "recovery_method = 'hash_linked', recovery_timestamp = ? "
                        "WHERE file_hash = ? AND id != ? AND (file_exists = 0 OR file_exists IS NULL)",
                        (save_path, recovery_ts, file_hash, media_id),
                    )
                    if linked > 0:
                        self._forensic_log("hash_linked", {
                            "message_id": msg_id, "file_hash": file_hash,
                            "linked_count": linked, "save_path": save_path,
                        })
                db.checkpoint_and_reconnect()

                # Clear caches so bubble re-renders with media
                if not self._use_webengine and self._list:
                    delegate = self._list.itemDelegate()
                    if hasattr(delegate, '_file_exists_cache'):
                        delegate._file_exists_cache.clear()
                    if hasattr(delegate, '_resolve_cache'):
                        delegate._resolve_cache.clear()
                    if hasattr(delegate, '_size_hint_cache'):
                        delegate._size_hint_cache.clear()

                # Update WebEngine view with new file path
                if self._use_webengine and self._web_view:
                    import json as _json
                    from pathlib import Path as _Path
                    try:
                        file_url = _Path(save_path).as_uri() if os.path.exists(save_path) else ""
                        _fname = os.path.basename(save_path)
                        update_fields = {
                            "file_exists": True,
                            "file_url": file_url,
                            "file_path": save_path,
                            "media_name": _fname,
                            "is_recovered_download": True,
                        }
                        # Don't set thumb to video file (browsers can't display .mp4 as img)
                        _mime = msg.get("mime_type", "")
                        if not (_mime and _mime.startswith("video/")):
                            update_fields["thumb"] = file_url
                        self._web_view.page().runJavaScript(
                            f"if(typeof updateMessageMedia==='function')updateMessageMedia({msg_id},{_json.dumps(update_fields)})"
                        )
                    except Exception as _e:
                        print(f"[ChatViewer] WebEngine media update error: {_e}")

            # Show download result with view options
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            size_str = f"{len(plaintext):,} bytes"
            fname = os.path.basename(save_path)
            result = QMessageBox(self)
            result.setWindowTitle("Download Complete")
            result.setText(f"Downloaded: {fname}\nSize: {size_str}\nSHA-256: {sha256[:16]}...")
            result.setInformativeText(save_path)
            view_btn = result.addButton("Open in Viewer", QMessageBox.AcceptRole)
            ext_btn = result.addButton("Open Externally", QMessageBox.ActionRole)
            result.addButton("Close", QMessageBox.RejectRole)
            result.exec()
            clicked = result.clickedButton()
            if clicked == view_btn:
                try:
                    from app.views.dialogs.media_viewer_dialog import MediaViewerDialog
                    dlg = MediaViewerDialog(save_path, parent=self)
                    dlg.exec()
                except Exception:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(save_path))
            elif clicked == ext_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(save_path))

            # Update in-memory message dict so re-render picks up changes
            msg["resolved_file_path"] = save_path
            msg["file_path"] = save_path
            msg["media_file_exists"] = True
            msg["recovery_method"] = "downloaded"

            if self._use_webengine:
                # Re-serialize just this message and push the update to JS
                serialized = self._web_view._serialize_messages([msg])
                self._web_view._run_js(
                    f"updateSingleMessage({msg_id}, {serialized})"
                )
            elif self._list:
                self._list.viewport().update()

        except ImportError as e:
            self._forensic_log("download_error", {
                "message_id": msg_id, "error": f"ImportError: {e}",
            })
            QMessageBox.warning(self, "Missing Dependency", str(e))
        except ValueError as e:
            self._forensic_log("download_error", {
                "message_id": msg_id, "error": f"ValueError: {e}",
            })
            QMessageBox.warning(self, "Download Failed", str(e))
        except Exception as e:
            self._forensic_log("download_error", {
                "message_id": msg_id, "error": f"{type(e).__name__}: {e}",
            })
            QMessageBox.warning(self, "Error", f"{type(e).__name__}: {e}")

    # ---- Inline audio playback ----

    def _toggle_audio_play(self, file_path: str, msg_id: int):
        """Toggle inline audio playback for a voice/audio message."""
        import os
        from PySide6.QtCore import QUrl

        if not file_path or not os.path.isfile(file_path):
            self._show_copy_toast("Audio file not on disk")
            return

        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        except ImportError:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
            return

        # If same audio is playing, toggle pause/resume
        if self._audio_player and self._audio_msg_id == msg_id:
            if self._audio_player.playbackState() == QMediaPlayer.PlayingState:
                self._audio_player.pause()
                return
            else:
                self._audio_player.play()
                return

        # Stop any existing playback
        self._stop_audio()

        # Create new player
        player = QMediaPlayer(self)
        output = QAudioOutput(self)
        output.setVolume(0.8)
        player.setAudioOutput(output)
        self._audio_player = player
        self._audio_output = output
        self._audio_msg_id = msg_id

        # Connect signals BEFORE setting source
        player.mediaStatusChanged.connect(self._on_audio_status)
        player.errorOccurred.connect(self._on_audio_error)
        # Real-time position updates (fires at native rate ~30fps)
        player.positionChanged.connect(self._on_audio_position_changed)

        # Normalize path for Windows and store for fallback
        self._audio_file_path = os.path.normpath(file_path)
        player.setSource(QUrl.fromLocalFile(self._audio_file_path))

        if self._use_webengine:
            self._web_view.update_audio_progress(msg_id, 0.0)
        else:
            self._delegate.set_audio_state(msg_id, 0.0)

        # Play after a short delay to let the source buffer
        QTimer.singleShot(100, player.play)

        # Safety: if no playback after 2s, fallback to system player
        QTimer.singleShot(2000, lambda: self._check_audio_started(msg_id))

    def _seek_audio(self, fraction: float):
        """Seek the currently playing audio to a position (0.0 to 1.0)."""
        if not self._audio_player:
            return
        dur = self._audio_player.duration()
        if dur > 0:
            target_pos = int(dur * fraction)
            self._audio_player.setPosition(target_pos)
            # Update progress immediately
            if self._use_webengine:
                self._web_view.update_audio_progress(self._audio_msg_id, fraction)
            else:
                self._delegate._audio_progress = fraction
                self._list.viewport().update()

    def _on_audio_position_changed(self, position: int):
        """Real-time audio progress update via positionChanged signal."""
        if not self._audio_player:
            return
        dur = self._audio_player.duration()
        progress = position / max(dur, 1)
        if self._use_webengine and self._web_view:
            self._web_view.update_audio_progress(self._audio_msg_id, progress)
        else:
            self._delegate.set_audio_state(self._audio_msg_id, progress)

    def _check_audio_started(self, expected_msg_id: int):
        """Safety check: if QMediaPlayer hasn't started after 2s, use system player."""
        if self._audio_msg_id != expected_msg_id:
            return  # Different audio or stopped
        if not self._audio_player:
            return
        # Check if player has made any progress
        pos = self._audio_player.position()
        dur = self._audio_player.duration()
        if pos <= 0 and dur <= 0:
            # QMediaPlayer couldn't play this format — fallback
            fp = getattr(self, "_audio_file_path", "")
            self._stop_audio()
            if fp:
                import os
                if os.path.isfile(fp):
                    from PySide6.QtGui import QDesktopServices
                    from PySide6.QtCore import QUrl
                    QDesktopServices.openUrl(QUrl.fromLocalFile(fp))
                    self._show_copy_toast("Opening in system player")

    def _on_audio_error(self, error, error_string=""):
        """Handle audio playback errors — fallback to system player."""
        import os
        print(f"[Audio Error] {error}: {error_string}")
        fp = getattr(self, "_audio_file_path", "")
        self._stop_audio()
        # Fallback: open with system default player
        if fp and os.path.isfile(fp):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(fp))
            self._show_copy_toast("Opening in system player")

    def _on_audio_status(self, status):
        """Handle audio playback completion."""
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._stop_audio()

    def _stop_audio(self):
        """Stop inline audio playback and reset state."""
        if self._audio_player:
            try:
                self._audio_player.stop()
            except Exception:
                pass
            self._audio_player.deleteLater()
            self._audio_player = None
        if self._audio_output:
            self._audio_output.deleteLater()
            self._audio_output = None
        if self._use_webengine and self._web_view:
            self._web_view.update_audio_stopped(self._audio_msg_id)
        else:
            self._delegate.set_audio_state(0, 0.0)
        self._audio_msg_id = 0

    def _open_media_viewer(self, file_path: str):
        """Open media in lightbox viewer dialog with prev/next navigation."""
        import os
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        if not file_path or not os.path.isfile(file_path):
            return

        ext = os.path.splitext(file_path)[1].lower()
        VIEWABLE_IMG = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
        VIEWABLE_VID = (".mp4", ".3gp", ".avi", ".mkv", ".mov")
        if ext in VIEWABLE_IMG + VIEWABLE_VID:
            from app.views.dialogs.media_viewer_dialog import MediaViewerDialog

            # Build media list from DB (fast) — only files that exist on disk
            media_list = []
            current_index = 0
            if self._conv_id is not None:
                try:
                    db = Database.get()
                    rows = db.fetchall("""
                        SELECT me.resolved_file_path, me.file_size,
                               COALESCE(c.resolved_name, c.wa_name, ''),
                               m.timestamp, m.type_label, m.id
                        FROM media me
                        JOIN message m ON m.id = me.message_id
                        LEFT JOIN contact c ON c.id = m.sender_id
                        WHERE m.conversation_id = ? AND me.file_exists = 1
                              AND me.resolved_file_path IS NOT NULL
                              AND me.resolved_file_path != ''
                        ORDER BY m.timestamp ASC
                    """, (self._conv_id,))
                    norm_target = os.path.normcase(os.path.abspath(file_path))
                    for r in rows:
                        rp = r[0]
                        if not rp:
                            continue
                        fext = os.path.splitext(rp)[1].lower()
                        if fext not in VIEWABLE_IMG + VIEWABLE_VID:
                            continue
                        mtype = "video" if fext in VIEWABLE_VID else "image"
                        entry = {
                            "file_path": rp,
                            "sender_name": r[2] or "",
                            "timestamp": r[3],
                            "file_size": r[1] or 0,
                            "media_type": mtype,
                            "message_id": r[5],
                            "conversation_id": self._conv_id,
                        }
                        if os.path.normcase(os.path.abspath(rp)) == norm_target:
                            current_index = len(media_list)
                        media_list.append(entry)
                except Exception:
                    pass  # Fallback: just open the single file

            media_type = "video" if ext in VIEWABLE_VID else "image"
            file_size = 0
            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                pass
            dlg = MediaViewerDialog(
                file_path, parent=self,
                media_type=media_type,
                file_size=file_size,
                media_list=media_list,
                current_index=current_index,
                conversation_id=self._conv_id,
            )
            # Connect tag signal to existing tag handler
            dlg.tag_requested.connect(self._on_viewer_tag)
            dlg.go_to_chat_requested.connect(self._on_viewer_goto)
            # Re-route the viewer's "Find Similar" button into the
            # same signal that the right-click context menu uses,
            # so a single handler in the parent (image_similarity
            # page launch) covers both entry points.
            dlg.find_similar_requested.connect(self.find_similar_requested)
            dlg.exec()
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))

    def _on_forensic_info_request(self, msg_id: int):
        """Compute and send forensic provenance data on-demand for a single message."""
        # Open the JS forensic panel first (context menu path doesn't go through JS showForensicInfo)
        if self._use_webengine and self._web_view:
            self._web_view._run_js(f"showForensicInfo({msg_id});")
        try:
            from shared.forensic_provenance import build_provenance
            # Try model first (fast), then fall back to direct DB query
            row_idx = self._model._id_to_row.get(msg_id, -1)
            msg = self._model.get_message_at(row_idx) if row_idx >= 0 else None
            if not msg:
                msg = next((m for m in self._model._raw_data if m.get("id") == msg_id), None)
            if not msg:
                # Direct DB fetch — message is in a tile not currently in the model
                db = Database.get()
                where = f"m.id = ?"
                sql = self._model._base_sql(where) + " LIMIT 1"
                row = db.fetchone(sql, (msg_id,))
                if row:
                    msg = self._model._build_msg_dict(row)
                    # Fetch auxiliary data (reactions, calls, etc.)
                    ChatMessageModel._fetch_auxiliary_batch(db, [msg])
            if not msg:
                print(f"[Forensic] Message {msg_id} not found")
                self._web_view.send_provenance(msg_id, "{}")
                return
            prov = build_provenance(msg)
            import json as _json_prov
            prov_json = _json_prov.dumps(prov, separators=(",", ":"), ensure_ascii=False)
            self._web_view.send_provenance(msg_id, prov_json)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[Forensic] Error building provenance for msg {msg_id}: {e}")
            self._web_view.send_provenance(msg_id, "{}")

    def _show_edit_history_popup(self, msg_id: int):
        """Show popup with edit version timeline for an edited message."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QFrame, QScrollArea, QWidget,
            QSizePolicy, QTextEdit,
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextOption
        from datetime import datetime

        db = Database.get()
        _lt = self._tm.is_light

        # Get current message text
        msg = db.fetchone(
            "SELECT m.text_content, m.timestamp, m.source_key_id, m.from_me, m.is_bot_message, "
            "CASE WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI' "
            "  WHEN m.from_me = 1 AND m.sender_id IS NULL THEN "
            "    COALESCE((SELECT value FROM case_metadata WHERE key='device_owner_name'), 'You') "
            "  ELSE COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
            "    conv.display_name, 'Unknown') END AS sender "
            "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
            "LEFT JOIN conversation conv ON conv.id = m.conversation_id "
            "WHERE m.id = ?", (msg_id,)
        )
        if not msg:
            return
        current_text = msg["text_content"] or "[media]"
        sender = msg["sender"]
        msg_ts = msg["timestamp"]

        # Get edit_history records (FTS-recovered original)
        edits = db.fetchall(
            "SELECT original_key_id, edited_timestamp, sender_timestamp, "
            "version, original_text, recovery_method "
            "FROM edit_history WHERE message_id = ? ORDER BY edited_timestamp ASC",
            (msg_id,),
        )

        # Get edit_version records (intermediate from quoted replies)
        versions = db.fetchall(
            "SELECT captured_text, captured_timestamp, recovery_method "
            "FROM edit_version WHERE message_id = ? ORDER BY captured_timestamp ASC",
            (msg_id,),
        )

        if not edits and not versions:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Edit History", "No edit history found for this message.")
            return

        # Build popup
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit History — {sender}")
        dlg.setMinimumSize(620, 420)
        # Default to a generous size so multi-version edit chains
        # with long text fit without forcing the user to drag the
        # dialog corner.  Capped at ~85% of the parent window so it
        # never spills off-screen on smaller laptops.
        try:
            _parent_geom = self.window().geometry() if self.window() else None
        except Exception:
            _parent_geom = None
        if _parent_geom:
            _dw = min(960, int(_parent_geom.width() * 0.7))
            _dh = min(760, int(_parent_geom.height() * 0.85))
            dlg.resize(max(620, _dw), max(460, _dh))
        else:
            dlg.resize(840, 660)
        dlg.setSizeGripEnabled(True)

        _bg = "#ffffff" if _lt else "#0b141a"
        _bg2 = "#f5f6f6" if _lt else "#111b21"
        _bg3 = "#edf0f2" if _lt else "#1a2630"
        _text = "#111b21" if _lt else "#e9edef"
        _text2 = "#667781" if _lt else "#8696a0"
        _accent = "#00897b" if _lt else "#00bcd4"
        _danger = "#c62828" if _lt else "#ef5350"
        _warn = "#e65100" if _lt else "#ffa726"
        _success = "#2e7d32" if _lt else "#66bb6a"
        _border = "#e0e3e7" if _lt else "rgba(128,128,128,0.12)"

        dlg.setStyleSheet(f"QDialog {{ background: {_bg}; }}")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header — fixed at the top
        hdr = QLabel(f"✎ Edit History — <b>{sender}</b>")
        hdr.setTextFormat(Qt.RichText)
        hdr.setStyleSheet(
            f"background: {_bg2}; color: {_text}; font-size: 13px; "
            f"padding: 12px 16px; border-bottom: 1px solid {_border};"
        )
        layout.addWidget(hdr)

        # Scrollable body — every version frame goes inside.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: {_bg}; border: none; }}")
        content = QWidget()
        content.setStyleSheet(f"background: {_bg};")
        # MinimumExpanding height policy so as frames grow with
        # their wrapped text, the scrollbar appears instead of
        # frames overlapping each other.
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        cl = QVBoxLayout(content)
        cl.setContentsMargins(16, 12, 16, 16)
        cl.setSpacing(12)

        def _fmt_ts(ts):
            if not ts:
                return "—"
            try:
                return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError):
                return "—"

        def _make_text_block(text, fg, bg, italic=False):
            """Build a read-only, word-wrapping text block sized so
            it grows to its full content height inside the scroll
            area.

            We use ``QTextEdit`` (read-only) instead of QLabel here
            because QLabel.setWordWrap inside a nested-layout
            QScrollArea sometimes negotiates a height shorter than
            the wrapped text actually needs — producing sibling
            frames that visually overlap each other when the
            current-text bubble is very tall.  QTextEdit's
            document-driven sizing is reliable: we measure the
            laid-out document height after the widget receives its
            real width and lock the widget to exactly that
            height.  Each frame ends up claiming exactly the
            vertical space it needs, and the outer scroll area
            handles overflow.
            """
            te = QTextEdit()
            te.setReadOnly(True)
            te.setText(text or "")
            te.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            te.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            te.setFrameShape(QFrame.NoFrame)
            te.setLineWrapMode(QTextEdit.WidgetWidth)
            te.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            style = (
                f"QTextEdit {{ color: {fg}; font-size: 12px; padding: 8px;"
                f" background: {bg}; border-radius: 4px; border: none;"
            )
            if italic:
                style += " font-style: italic;"
            style += " }"
            te.setStyleSheet(style)
            te.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            def _resize_to_doc():
                doc = te.document()
                doc.setTextWidth(max(50, te.viewport().width()))
                h = int(doc.size().height()) + 18
                te.setMinimumHeight(h)
                te.setMaximumHeight(h)

            te.document().contentsChanged.connect(_resize_to_doc)
            _orig_resize = te.resizeEvent

            def _on_resize(ev, _orig=_orig_resize, _refit=_resize_to_doc):
                _orig(ev)
                _refit()

            te.resizeEvent = _on_resize
            _resize_to_doc()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, _resize_to_doc)
            return te

        # Render every record in edit_history (ordered ASC by
        # edited_ts).  Always render a frame per record — even
        # when WhatsApp's FTS-index recovery couldn't reconstruct
        # the text bytes — so the analyst sees that the edit
        # event exists in the source data.
        if edits:
            n_edits = len(edits)
            for i, eh_row in enumerate(edits):
                eh = dict(eh_row)
                orig_text = (eh.get("original_text") or "").strip()
                recovery = eh.get("recovery_method") or "unknown"
                v_label = f"V{i+1}" if n_edits > 1 else "V1"
                v_total = f" of {n_edits}" if n_edits > 1 else ""

                v_frame = QFrame()
                v_frame.setStyleSheet(
                    f"QFrame {{ background: {_bg3}; border: 1px solid {_border}; "
                    f"border-left: 4px solid {_danger}; border-radius: 6px; }}"
                )
                v_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                vl = QVBoxLayout(v_frame)
                vl.setContentsMargins(12, 8, 12, 12)
                vl.setSpacing(6)

                v_hdr = QLabel(
                    f"<b style='color:{_danger}'>{v_label}{v_total} — Original Text</b>"
                    f" <span style='color:{_text2};font-size:10px;'>"
                    f"recovery: {recovery} · "
                    f"edit applied at {_fmt_ts(eh.get('edited_timestamp'))}"
                    f"</span>"
                )
                v_hdr.setTextFormat(Qt.RichText)
                v_hdr.setWordWrap(True)
                v_hdr.setStyleSheet(f"color: {_text}; font-size: 11px;")
                vl.addWidget(v_hdr)

                if orig_text:
                    v_text = _make_text_block(
                        orig_text,
                        '#b71c1c' if _lt else '#ef9a9a',
                        '#ffebee' if _lt else 'rgba(198,40,40,0.08)',
                    )
                else:
                    v_text = _make_text_block(
                        "(original text could not be recovered for this edit — "
                        "no FTS-index hit and no quoted-reply capture)",
                        _text2,
                        '#fff8e1' if _lt else 'rgba(255,167,38,0.08)',
                        italic=True,
                    )
                vl.addWidget(v_text)
                cl.addWidget(v_frame)

        # Intermediate versions captured from quoted replies
        for i, v in enumerate(versions):
            v = dict(v)
            vi_frame = QFrame()
            vi_frame.setStyleSheet(
                f"QFrame {{ background: {_bg3}; border: 1px solid {_border}; "
                f"border-left: 4px solid {_warn}; border-radius: 6px; }}"
            )
            vi_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            vil = QVBoxLayout(vi_frame)
            vil.setContentsMargins(12, 8, 12, 12)
            vil.setSpacing(6)

            vi_hdr = QLabel(
                f"<b style='color:{_warn}'>Intermediate Version {i+1}</b>"
                f" <span style='color:{_text2};font-size:10px;'>"
                f"captured via {v.get('recovery_method', 'quoted reply')}"
                f" at {_fmt_ts(v.get('captured_timestamp'))}</span>"
            )
            vi_hdr.setTextFormat(Qt.RichText)
            vi_hdr.setWordWrap(True)
            vi_hdr.setStyleSheet(f"color: {_text}; font-size: 11px;")
            vil.addWidget(vi_hdr)

            vi_text = _make_text_block(
                v.get("captured_text", "") or "(empty)",
                _text,
                '#fff8e1' if _lt else 'rgba(255,167,38,0.08)',
            )
            vil.addWidget(vi_text)
            cl.addWidget(vi_frame)

        # Current (final) version
        cur_frame = QFrame()
        cur_frame.setStyleSheet(
            f"QFrame {{ background: {_bg3}; border: 1px solid {_border}; "
            f"border-left: 4px solid {_success}; border-radius: 6px; }}"
        )
        cur_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        curl = QVBoxLayout(cur_frame)
        curl.setContentsMargins(12, 8, 12, 12)
        curl.setSpacing(6)

        edit_ts = edits[-1]["edited_timestamp"] if edits else None
        cur_hdr = QLabel(
            f"<b style='color:{_success}'>Current Text (Final Version)</b>"
            f" <span style='color:{_text2};font-size:10px;'>edited at {_fmt_ts(edit_ts)}</span>"
        )
        cur_hdr.setTextFormat(Qt.RichText)
        cur_hdr.setWordWrap(True)
        cur_hdr.setStyleSheet(f"color: {_text}; font-size: 11px;")
        curl.addWidget(cur_hdr)

        cur_text = _make_text_block(
            current_text,
            '#1b5e20' if _lt else '#a5d6a7',
            '#e8f5e9' if _lt else 'rgba(46,125,50,0.08)',
        )
        curl.addWidget(cur_text)
        cl.addWidget(cur_frame)

        if edits:
            eh = dict(edits[0])
            info = QLabel(
                f"<span style='color:{_text2};font-size:9px;font-style:italic;'>"
                f"message.id (analysis.db): {msg_id} · "
                f"original_key_id: {eh.get('original_key_id', '?')} · "
                f"recovery: {eh.get('recovery_method', '?')} · "
                f"edit_history records: {len(edits)} · "
                f"intermediate versions: {len(versions)}"
                f"</span>"
            )
            info.setTextFormat(Qt.RichText)
            info.setWordWrap(True)
            cl.addWidget(info)

        cl.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        dlg.exec()

    def _show_replies_panel(self, msg_id: int, source_key: str):
        """Open the replies sidebar panel for the given message."""
        # Close search panel (mutual exclusion)
        self._search_results_panel.setVisible(False)
        self._replies_sidebar.load_replies(msg_id, source_key, self._conv_id)

    def _on_reply_cross_conv_nav(self, conv_id: int, msg_id: int):
        """Handle clicking a reply that's in a different conversation."""
        from app.views.main_window import MainWindow
        main = self.window()
        if isinstance(main, MainWindow):
            main._switch_to_conversation(conv_id, msg_id)

    def _on_call_origin_nav(self, conv_id: int, msg_id: int):
        """Jump to the original group / multi-person call from a
        reconstructed per-participant call echo.

        Synthetic per-participant call messages live in each
        participant's 1-on-1 conversation, but the real call message
        sits in the originating group / community / multi-person
        conversation.  The renderer surfaces a "Go to group call"
        pill on the synthetic bubble; clicking it routes here, which
        switches the chat to the origin conversation and scrolls to
        the original message.  Falls back to opening just the
        conversation if the original message_id wasn't resolvable
        at lookup time.
        """
        from app.views.main_window import MainWindow
        main = self.window()
        if not isinstance(main, MainWindow):
            return
        try:
            if msg_id and msg_id > 0:
                main._switch_to_conversation(conv_id, msg_id)
            else:
                main._switch_to_conversation(conv_id, 0)
        except Exception as e:
            print(f"[ChatViewer] call origin nav failed: {e}")

    def _show_replies_panel_popup_DISABLED(self, msg_id: int, source_key: str):
        """DISABLED — old popup version, replaced by sidebar panel."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QFrame, QScrollArea, QWidget,
            QPushButton, QHBoxLayout,
        )
        from PySide6.QtCore import Qt
        from datetime import datetime

        db = Database.get()
        _lt = self._tm.is_light

        if not source_key:
            source_key = db.scalar(
                "SELECT source_key_id FROM message WHERE id = ?", (msg_id,)
            )
        if not source_key:
            return

        # Find all replies — also check original_key_id for edited messages
        orig_key = db.scalar(
            "SELECT original_key_id FROM edit_history WHERE message_id = ? LIMIT 1",
            (msg_id,),
        )
        if orig_key and orig_key != source_key:
            key_clause = "m.reply_to_key_id IN (?, ?)"
            key_params = [source_key, orig_key, self._conv_id]
        else:
            key_clause = "m.reply_to_key_id = ?"
            key_params = [source_key, self._conv_id]

        # Get owner info for from_me resolution
        _owner_name = db.scalar("SELECT value FROM case_metadata WHERE key = 'device_owner_name'") or "You"
        _owner_phone = db.scalar("SELECT value FROM case_metadata WHERE key = 'device_owner_phone'") or ""
        _owner_jid = db.scalar("SELECT value FROM case_metadata WHERE key = 'device_owner_jid'") or ""

        replies = db.fetchall(
            f"SELECT m.id, m.timestamp, m.from_me, "
            "COALESCE(m.text_content, '[media]') AS text, "
            "CASE WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI' "
            f"  WHEN m.from_me = 1 AND m.sender_id IS NULL THEN '{_owner_name}' "
            "  ELSE COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
            "    REPLACE(c.phone_jid, '@s.whatsapp.net', ''), 'Unknown') END AS sender, "
            "CASE WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN '13135550002@s.whatsapp.net' "
            f"  WHEN m.from_me = 1 AND m.sender_id IS NULL THEN '{_owner_jid}' "
            "  ELSE COALESCE(c.phone_jid, '') END AS phone_jid, "
            "m.message_type, m.type_label, "
            "m.is_revoked, m.is_edited, m.is_bot_message "
            "FROM message m "
            "LEFT JOIN contact c ON c.id = m.sender_id "
            f"WHERE {key_clause} AND m.conversation_id = ? "
            "ORDER BY m.timestamp ASC",
            key_params,
        )

        if not replies:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Replies", "No replies found for this message.")
            return

        # Get original message text for header
        orig = db.fetchone(
            "SELECT COALESCE(m.text_content, '[media]'), "
            "COALESCE(c.resolved_name, c.wa_name, 'Unknown') "
            "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
            "WHERE m.id = ?", (msg_id,)
        )
        orig_text = (orig[0] or "")[:80] if orig else ""
        orig_sender = orig[1] if orig else "Unknown"

        # Build popup
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Replies to {orig_sender}'s message")
        dlg.setMinimumSize(500, 300)
        dlg.resize(600, 420)
        _bg = "#ffffff" if _lt else "#0b141a"
        _bg2 = "#f5f6f6" if _lt else "#111b21"
        _bg3 = "#edf0f2" if _lt else "#1a2630"
        _text = "#111b21" if _lt else "#e9edef"
        _text2 = "#667781" if _lt else "#8696a0"
        _accent = "#00897b" if _lt else "#00bcd4"
        _border = "#e0e3e7" if _lt else "rgba(128,128,128,0.12)"

        dlg.setStyleSheet(f"QDialog {{ background: {_bg}; }}")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header with original message preview
        hdr = QLabel(
            f"\u21A9 <b>{len(replies)} repl{'ies' if len(replies) > 1 else 'y'}</b> to "
            f"<b>{orig_sender}</b>: <i>{orig_text[:60]}{'...' if len(orig_text) > 60 else ''}</i>"
        )
        hdr.setTextFormat(Qt.RichText)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f"background: {_bg2}; color: {_text}; font-size: 12px; "
            f"padding: 10px 14px; border-bottom: 1px solid {_border};"
        )
        layout.addWidget(hdr)

        # Scrollable replies list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background: {_bg}; border: none; }}")
        content = QWidget()
        content.setStyleSheet(f"background: {_bg};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 8, 12, 12)
        cl.setSpacing(6)

        def _fmt_ts(ts):
            if not ts:
                return "—"
            try:
                return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError):
                return "—"

        for r in replies:
            r = dict(r) if hasattr(r, 'keys') else {
                "id": r[0], "timestamp": r[1], "from_me": r[2], "text": r[3],
                "sender": r[4], "phone_jid": r[5], "message_type": r[6],
                "type_label": r[7], "is_revoked": r[8], "is_edited": r[9],
            }

            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background: {_bg3}; border: 1px solid {_border}; "
                f"border-radius: 6px; }}"
            )
            card_l = QVBoxLayout(card)
            card_l.setContentsMargins(10, 6, 10, 6)
            card_l.setSpacing(3)

            # Sender + timestamp + full identity
            sender = r.get("sender") or "Unknown"
            jid = r.get("phone_jid") or ""
            ts_str = _fmt_ts(r.get("timestamp"))
            badges = ""
            if r.get("from_me"):
                badges += f" <span style='color:#e65100;font-size:9px;font-weight:bold;background:rgba(230,81,0,0.1);padding:1px 4px;border-radius:3px;'>Owner</span>"
            if r.get("is_bot_message"):
                badges += " <span style='color:#7c4dff;font-size:9px;font-weight:bold;background:rgba(124,77,255,0.1);padding:1px 4px;border-radius:3px;'>\U0001F916 AI</span>"
            if r.get("is_revoked"):
                badges += " <span style='color:#ef5350;font-size:9px;'>[deleted]</span>"
            if r.get("is_edited"):
                badges += " <span style='color:#ffa726;font-size:9px;'>[edited]</span>"

            # Phone from JID
            phone_display = ""
            if jid and "@" in jid:
                phone_num = jid.split("@")[0]
                if phone_num.isdigit():
                    phone_display = f" (+{phone_num})"

            hdr_html = (
                f"<b style='color:{_accent};'>{sender}</b>"
                f"<span style='color:{_text2};font-size:10px;'>{phone_display}</span>"
                f"{badges}"
                f"<br><span style='color:{_text2};font-size:9px;font-family:monospace;'>JID: {jid}</span>"
                f" <span style='color:{_text2};font-size:9px;float:right;'>{ts_str}</span>"
            )
            hdr_lbl = QLabel(hdr_html)
            hdr_lbl.setTextFormat(Qt.RichText)
            hdr_lbl.setStyleSheet(f"color: {_text}; font-size: 11px;")
            card_l.addWidget(hdr_lbl)

            # Reply text
            reply_text = r.get("text") or ""
            if r.get("is_revoked"):
                reply_text = "\U0001F6AB This message was deleted"
            text_lbl = QLabel(reply_text[:300] + ("..." if len(reply_text) > 300 else ""))
            text_lbl.setWordWrap(True)
            text_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            text_lbl.setStyleSheet(f"color: {_text}; font-size: 11px; padding: 2px 0;")
            card_l.addWidget(text_lbl)

            # "Go to" button
            reply_msg_id = r.get("id")
            go_btn = QPushButton("Go to \u25B6")
            go_btn.setFixedSize(70, 22)
            go_btn.setCursor(Qt.PointingHandCursor)
            go_btn.setStyleSheet(
                f"QPushButton {{ background: {_accent}; color: white; border: none; "
                f"border-radius: 4px; font-size: 9px; font-weight: bold; }}"
                f"QPushButton:hover {{ opacity: 0.8; }}"
            )
            go_btn.clicked.connect(
                lambda checked=False, mid=reply_msg_id: (
                    dlg.accept(),
                    self._navigate_to_message_id(mid),
                )
            )
            card_l.addWidget(go_btn, alignment=Qt.AlignRight)

            cl.addWidget(card)

        cl.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        dlg.exec()

    def _show_receipt_detail(self, msg_id: int):
        """Show popup with per-user delivery/read/played timestamps for a message."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
            QHeaderView, QLabel, QLineEdit, QPushButton,
        )
        from PySide6.QtCore import Qt
        from datetime import datetime

        db = Database.get()
        # Get message info
        msg_row = db.fetchone(
            "SELECT timestamp, text_content, from_me FROM message WHERE id = ?", (msg_id,)
        )
        if not msg_row:
            return
        msg_ts = msg_row[0]
        msg_text = (msg_row[1] or "")[:80]

        # Get all receipts with contact info
        rows = db.fetchall(
            "SELECT r.delivered_ts, r.read_ts, r.played_ts,"
            "  r.delivery_delay_ms, r.read_delay_ms,"
            "  COALESCE(c.resolved_name, c.display_name, c.wa_name, 'Unknown') AS name,"
            "  COALESCE(c.phone_number, '') AS phone,"
            "  c.id AS contact_id"
            " FROM receipt r"
            " LEFT JOIN contact c ON c.id = r.recipient_id"
            " WHERE r.message_id = ?"
            " ORDER BY r.delivered_ts",
            (msg_id,),
        )

        if not rows:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Receipt Info", "No per-user receipt data for this message.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Message Receipt Details — ID {msg_id}")
        dlg.setMinimumSize(1100, 550)
        layout = QVBoxLayout(dlg)

        # Header
        sent_str = format_timestamp_with_utc(msg_ts, "full") if msg_ts else "?"
        hdr = QLabel(f"<b>Message sent:</b> {sent_str}<br><b>Text:</b> {msg_text}<br>"
                      f"<b>Recipients:</b> {len(rows)}")
        hdr.setWordWrap(True)
        layout.addWidget(hdr)

        # Filter
        filter_row = QHBoxLayout()
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("Filter by name or phone...")
        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(filter_input)

        # Stats
        delivered_count = sum(1 for r in rows if r[0] and r[0] > 0)
        read_count = sum(1 for r in rows if r[1] and r[1] > 0)
        played_count = sum(1 for r in rows if r[2] and r[2] > 0)
        stats = QLabel(f"<span style='color:#1565c0'>✓✓ Delivered: {delivered_count}</span> &nbsp; "
                        f"<span style='color:#2e7d32'>✓✓ Read: {read_count}</span> &nbsp; "
                        f"<span style='color:#6a1b9a'>▶ Played: {played_count}</span>")
        filter_row.addWidget(stats)
        layout.addLayout(filter_row)

        # Table
        headers = ["Name", "Phone", "Delivered", "Delivery Lag",
                    "Read/Seen", "Read Lag", "Played", "Status"]
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)

        def _fmt_ts(ts):
            if not ts or ts <= 0:
                return "—"
            try:
                # tz-aware (selected case TZ); the 'full' format
                # already carries millisecond precision.
                return format_timestamp(ts, "full")
            except Exception:
                return format_timestamp_with_utc(ts, "full")

        def _fmt_lag(ms):
            if ms is None or ms <= 0:
                return "—"
            if ms < 1000:
                return f"{ms}ms"
            if ms < 60000:
                return f"{ms / 1000:.1f}s"
            if ms < 3600000:
                return f"{ms / 60000:.1f}m"
            return f"{ms / 3600000:.1f}h"

        for i, r in enumerate(rows):
            delivered_ts, read_ts, played_ts, dlv_delay, read_delay, name, phone, cid = r

            # Status
            if played_ts and played_ts > 0:
                status = "▶ Played"
            elif read_ts and read_ts > 0:
                status = "✓✓ Read"
            elif delivered_ts and delivered_ts > 0:
                status = "✓ Delivered"
            else:
                status = "⏳ Pending"

            items = [
                name, f"+{phone}" if phone else "",
                _fmt_ts(delivered_ts), _fmt_lag(dlv_delay),
                _fmt_ts(read_ts), _fmt_lag(read_delay),
                _fmt_ts(played_ts), status,
            ]
            for j, val in enumerate(items):
                item = QTableWidgetItem(str(val))
                if j in (2, 4, 6):  # Timestamp columns — right-align
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(i, j, item)

        # Filter logic
        def _apply_filter():
            text = filter_input.text().lower()
            for row_idx in range(table.rowCount()):
                name_item = table.item(row_idx, 0)
                phone_item = table.item(row_idx, 1)
                match = (not text
                         or text in (name_item.text().lower() if name_item else "")
                         or text in (phone_item.text().lower() if phone_item else ""))
                table.setRowHidden(row_idx, not match)

        filter_input.textChanged.connect(_apply_filter)
        layout.addWidget(table)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

        dlg.exec()

    def _show_reactions_detail(self, message_id: int):
        """Show popup with detailed reactions: who reacted, their phone, timestamp."""
        from PySide6.QtWidgets import QDialog, QTableWidget, QTableWidgetItem, QHeaderView

        db = Database.get()
        rows = db.fetchall("""
            SELECT r.emoji,
                   COALESCE(
                     NULLIF(c.resolved_name, ''),
                     NULLIF(c.display_name, ''),
                     CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                          THEN '+' || c.phone_number END,
                     NULLIF(c.wa_name, ''),
                     'Unknown') AS reactor_name,
                   c.phone_number, r.timestamp
            FROM reaction r
            LEFT JOIN contact c ON c.id = r.reactor_id
            WHERE r.message_id = ?
            ORDER BY r.timestamp ASC
        """, (message_id,))

        if not rows:
            return

        is_light = self._tm.is_light
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Reactions ({len(rows)})")
        dlg.resize(680, 360)
        dlg_bg = "#ffffff" if is_light else "#111b21"
        dlg.setStyleSheet(f"QDialog {{ background: {dlg_bg}; }}")
        lay = QVBoxLayout(dlg)

        tbl = QTableWidget(len(rows), 4)
        tbl.setHorizontalHeaderLabels(["Emoji", "Name", "Phone", "Time"])
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # Emoji
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)            # Name
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)   # Phone
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # Time
        if is_light:
            tbl.setStyleSheet(
                "QTableWidget { background: #ffffff; color: #111b21; border: none; gridline-color: #e0e3e7; }"
                "QHeaderView::section { background: #f0f2f5; color: #667781; border: 1px solid #e0e3e7; padding: 4px; }"
            )
        else:
            tbl.setStyleSheet(
                "QTableWidget { background: #0b141a; color: #e9edef; border: none; gridline-color: #1f2c34; }"
                "QHeaderView::section { background: #1f2c34; color: #aebac1; border: 1px solid #0b141a; padding: 4px; }"
            )
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)

        for i, r in enumerate(rows):
            tbl.setItem(i, 0, QTableWidgetItem(r[0] or ""))
            tbl.setItem(i, 1, QTableWidgetItem(r[1] or ""))
            phone = r[2] or ""
            tbl.setItem(i, 2, QTableWidgetItem(f"+{phone}" if phone and not phone.startswith("+") else phone))
            ts = r[3]
            ts_str = ""
            if ts:
                try:
                    ts_str = format_timestamp(ts, "minute")
                except (ValueError, OSError):
                    pass
            tbl.setItem(i, 3, QTableWidgetItem(ts_str))

        lay.addWidget(tbl)
        dlg.exec()

    def _show_hash_matches(self, msg_id_or_hash):
        """Dialog — every row whose media.file_hash OR enc_file_hash matches
        the query message's hash. Shows sender name + JID + phone, direction,
        timestamp, on-disk flag, and a Go-to-chat button per row.

        Works when the query msg was shared by owner (sender_id NULL) OR
        when only enc_file_hash is populated (CDN-only media).
        """
        from PySide6.QtWidgets import (
            QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
        )

        db = Database.get()
        file_hash = None
        enc_hash = None
        query_msg_id = None

        if isinstance(msg_id_or_hash, (int,)) or (isinstance(msg_id_or_hash, str)
                                                     and msg_id_or_hash.isdigit()):
            query_msg_id = int(msg_id_or_hash)
            row = db.fetchone(
                "SELECT file_hash, enc_file_hash FROM media WHERE message_id = ?",
                (query_msg_id,),
            )
            if row:
                file_hash = row[0] or None
                enc_hash = row[1] or None
        else:
            # Legacy: a raw file_hash was passed
            file_hash = str(msg_id_or_hash) or None

        if not file_hash and not enc_hash:
            QMessageBox.information(self, "Find Copies",
                "This message has no stored SHA-256 (file_hash) or enc_file_hash.\n"
                "WhatsApp sometimes omits the hash for older outbound messages; "
                "re-ingesting can recover it.")
            return

        # Build WHERE: match either hash so owner-shared files still surface.
        conditions, params = [], []
        if file_hash:
            conditions.append("me.file_hash = ?")
            params.append(file_hash)
        if enc_hash:
            conditions.append("me.enc_file_hash = ?")
            params.append(enc_hash)
        where_clause = " OR ".join(conditions)

        rows = db.fetchall(f"""
            SELECT m.id AS msg_id, m.conversation_id,
                   COALESCE(cv.display_name, cv.jid_raw_string, '') AS conv_name,
                   cv.chat_type,
                   cv.jid_raw_string AS conv_jid,
                   COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),
                            NULLIF(c.wa_name,''),
                            CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                                 THEN '+'||c.phone_number END,
                            CASE WHEN m.from_me = 1 THEN 'You (Owner)' ELSE 'Unknown' END
                   ) AS sender,
                   c.phone_number AS sender_phone,
                   c.phone_jid AS sender_jid,
                   m.from_me, m.timestamp, me.file_exists, me.file_size,
                   me.file_hash, me.enc_file_hash
            FROM media me
            JOIN message m ON m.id = me.message_id
            JOIN conversation cv ON cv.id = m.conversation_id
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE {where_clause}
            ORDER BY m.timestamp ASC
        """, tuple(params))

        if not rows:
            QMessageBox.information(self, "Find Copies",
                "No other messages share this file's hash.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Find Copies — {len(rows)} match{'es' if len(rows) != 1 else ''}")
        dlg.resize(960, 520)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        # Forensic header
        hash_preview = (file_hash or enc_hash or "")[:48]
        hdr = QLabel(
            f"<b>SHA-256:</b> <code>{hash_preview}\u2026</code>&nbsp;"
            f"&middot;&nbsp; <b>{len(rows)}</b> copies across "
            f"<b>{len({r[1] for r in rows})}</b> chats, "
            f"<b>{len({r[6] for r in rows if r[6]})}</b> distinct senders"
        )
        hdr.setTextFormat(Qt.RichText)
        hdr.setStyleSheet("font-size: 12px; padding: 2px 0;")
        lay.addWidget(hdr)

        tbl = QTableWidget(len(rows), 7)
        tbl.setHorizontalHeaderLabels([
            "Dir", "Conversation", "Sender", "JID / Phone",
            "Timestamp", "On disk", "",
        ])
        tbl.setAlternatingRowColors(True)
        tbl.verticalHeader().setVisible(False)
        hh = tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Fixed)
        tbl.setColumnWidth(6, 120)

        def _go(cid: int, mid: int):
            dlg.accept()
            if cid and mid:
                # Use the existing nav helper — it already routes through
                # scroll_to_message after tile preload.
                self._navigate_to_message_id(int(mid))

        for i, r in enumerate(rows):
            msg_id_x = r[0]
            conv_id_x = r[1]
            conv_nm = r[2] or ""
            conv_jid = r[4] or ""
            sender = r[5] or "Unknown"
            phone = r[6] or ""
            jid = r[7] or ""
            from_me = r[8]
            ts = r[9]
            on_disk = bool(r[10])

            dir_item = QTableWidgetItem("\u2191 Sent" if from_me else "\u2193 Recv")
            dir_item.setForeground(QColor("#2e7d32" if from_me else "#1976d2"))
            tbl.setItem(i, 0, dir_item)

            # Conversation: show name + JID subtext
            conv_text = conv_nm + ("\n" + conv_jid if conv_jid else "")
            conv_item = QTableWidgetItem(conv_text)
            conv_item.setToolTip(conv_jid)
            tbl.setItem(i, 1, conv_item)

            # Sender — mark current msg / owner specially
            sender_display = sender
            if msg_id_x == query_msg_id:
                sender_display += "  \u25CF (this one)"
            tbl.setItem(i, 2, QTableWidgetItem(sender_display))

            # JID / Phone column
            jid_bits = []
            if phone: jid_bits.append("+" + phone)
            if jid:   jid_bits.append(jid)
            tbl.setItem(i, 3, QTableWidgetItem("\n".join(jid_bits) or "\u2014"))

            # Timestamp
            ts_str = format_timestamp_with_utc(ts, "datetime") if ts else ""
            tbl.setItem(i, 4, QTableWidgetItem(ts_str))

            # On disk
            od_item = QTableWidgetItem("Yes" if on_disk else "No")
            od_item.setForeground(QColor("#2e7d32" if on_disk else "#b0b0b0"))
            tbl.setItem(i, 5, od_item)

            # Go to chat button
            go_btn = QPushButton("\u2192 Go to chat")
            go_btn.setCursor(Qt.PointingHandCursor)
            go_btn.setStyleSheet(
                "QPushButton { background: rgba(0,137,123,0.15); color: #00897b;"
                " border: 1px solid rgba(0,137,123,0.3); border-radius: 4px;"
                " padding: 3px 8px; font-size: 11px; }"
                "QPushButton:hover { background: rgba(0,137,123,0.28); }"
            )
            go_btn.clicked.connect(lambda _=False, c=conv_id_x, m=msg_id_x: _go(c, m))
            tbl.setCellWidget(i, 6, go_btn)

        # Row height big enough for 2-line cells
        tbl.verticalHeader().setDefaultSectionSize(46)
        lay.addWidget(tbl, 1)

        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(dlg.reject)
        bbox = QHBoxLayout()
        bbox.addStretch()
        bbox.addWidget(close_btn)
        lay.addLayout(bbox)

        dlg.exec()

    def _shutdown_background_workers(self) -> None:
        tile_w = getattr(self, '_tile_worker', None)
        if tile_w and tile_w.isRunning():
            tile_w.shutdown()
            tile_w.wait(1500)
            tile_w.deleteLater()
            self._tile_worker = None

        prefetch_w = getattr(self._model, '_prefetch_worker', None)
        if prefetch_w and prefetch_w.isRunning():
            prefetch_w.quit()
            prefetch_w.wait(1500)
            prefetch_w.deleteLater()
            self._model._prefetch_worker = None

    def closeEvent(self, event) -> None:
        self._shutdown_background_workers()
        super().closeEvent(event)

    def refresh_for_timezone_change(self) -> None:
        # Lightweight path for the WebEngine renderer: just push the new
        # IANA timezone to JS, which clears its formatter caches and
        # re-renders the visible window.  No SQL re-fetch, no DOM
        # rebuild, no scroll loss.
        if self._use_webengine and self._web_view is not None:
            try:
                from app.config import get_timezone_name
                self._web_view.set_timezone(get_timezone_name() or "")
                return
            except Exception:
                pass
        # Native (non-WebEngine) path: full reload is required because
        # the delegate caches rendered text per row and there's no
        # simple flush hook.
        if self._conv_id is not None:
            self.load_conversation(
                self._conv_id,
                self._conv_name,
                target_msg_id=self._target_msg_id or 0,
            )
