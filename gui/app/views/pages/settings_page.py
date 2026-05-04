"""
Settings page -- database info, app info, theme controls, and keyboard shortcuts.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from app.config import APP_NAME, APP_VERSION
from app.services.database import Database
from app.services.theme_manager import ThemeManager


class SettingsPage(QScrollArea):
    """Settings and database information page."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)

        self._tm = ThemeManager.get()

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(24, 20, 24, 24)
        self._layout.setSpacing(20)
        self.setWidget(container)

        self._build_header()
        self._build_theme_section()
        self._build_timezone_section()
        self._build_app_info()
        self._build_db_info()
        self._build_table_stats()
        self._build_keyboard_shortcuts()
        self._layout.addStretch()

    def _build_header(self) -> None:
        header = QHBoxLayout()
        title = QLabel("\u2699\uFE0F  Settings")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch()
        self._layout.addLayout(header)

    def _build_theme_section(self) -> None:
        section = self._make_section("Theme")
        sl = section.layout()

        desc = QLabel("Choose a visual theme for the application. Changes take effect on restart.")
        desc.setWordWrap(True)
        desc.setStyleSheet(self._tm.hint_label_style())
        sl.addWidget(desc)

        row = QHBoxLayout()
        row.setSpacing(12)

        # Light mode button
        self._light_btn = QPushButton("\u2600\uFE0F  Light Mode")
        self._light_btn.setCheckable(True)
        self._light_btn.setFixedHeight(40)
        self._light_btn.setMinimumWidth(160)
        self._light_btn.setChecked(self._tm.is_light)
        self._light_btn.clicked.connect(lambda: self._set_theme("light"))

        # Dark mode button
        self._dark_btn = QPushButton("Dark Mode")
        self._dark_btn.setCheckable(True)
        self._dark_btn.setFixedHeight(40)
        self._dark_btn.setMinimumWidth(160)
        self._dark_btn.setChecked(self._tm.is_dark)
        self._dark_btn.clicked.connect(lambda: self._set_theme("dark"))

        row.addWidget(self._light_btn)
        row.addWidget(self._dark_btn)
        row.addStretch()
        sl.addLayout(row)

        # Restart hint
        self._restart_hint = QLabel("")
        self._restart_hint.setStyleSheet("color: #ef5350; font-size: 11px; font-weight: bold;")
        self._restart_hint.setVisible(False)
        sl.addWidget(self._restart_hint)

        self._layout.addWidget(section)

    def _set_theme(self, theme: str) -> None:
        self._tm.theme = theme
        self._light_btn.setChecked(theme == "light")
        self._dark_btn.setChecked(theme == "dark")
        self._restart_hint.setText(
            "\u26A0\uFE0F  Please restart the app for the theme change to take full effect."
        )
        self._restart_hint.setVisible(True)

    def _build_timezone_section(self) -> None:
        """Global timezone/timestamp settings for forensic analysis (IANA-based)."""
        from PySide6.QtWidgets import QComboBox
        from app.config import (
            get_timezone_name, set_timezone, get_timezone_display,
            get_timezone_notifier,
            IANA_TIMEZONES,
        )

        section = self._make_section("Timestamp & Timezone")
        sl = section.layout()

        desc = QLabel(
            "Configure the timezone for all timestamp display. "
            "All timestamps use full forensic format (YYYY-MM-DD HH:MM:SS.mmm). "
            "Type to search by city or abbreviation."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(self._tm.hint_label_style())
        sl.addWidget(desc)

        row = QHBoxLayout()
        row.setSpacing(12)

        row.addWidget(QLabel("Timezone:"))

        self._tz_combo = tz_combo = QComboBox()
        tz_combo.setEditable(True)
        tz_combo.setInsertPolicy(QComboBox.NoInsert)
        from PySide6.QtWidgets import QCompleter
        tz_combo.completer().setFilterMode(Qt.MatchContains)
        tz_combo.completer().setCompletionMode(QCompleter.PopupCompletion)

        current_iana = get_timezone_name()
        selected_idx = 0
        for i, (iana_name, _abbr) in enumerate(IANA_TIMEZONES):
            display = get_timezone_display(iana_name)
            tz_combo.addItem(display, iana_name)
            if iana_name == current_iana:
                selected_idx = i

        # If system-detected timezone is not in our list, add it at the top
        if current_iana not in [name for name, _ in IANA_TIMEZONES]:
            display = get_timezone_display(current_iana)
            tz_combo.insertItem(0, display, current_iana)
            selected_idx = 0

        tz_combo.setCurrentIndex(selected_idx)
        tz_combo.setFixedHeight(32)
        tz_combo.setMinimumWidth(340)

        def _on_tz_changed(idx: int) -> None:
            iana = tz_combo.itemData(idx)
            if iana is not None:
                set_timezone(str(iana))
                self._tz_status.setText(
                    f"\u2713 Timezone set to {get_timezone_display(str(iana))}"
                )
                self._tz_status.setVisible(True)

        tz_combo.currentIndexChanged.connect(_on_tz_changed)
        get_timezone_notifier().timezone_changed.connect(self._sync_timezone_combo)
        row.addWidget(tz_combo)
        row.addStretch()
        sl.addLayout(row)

        self._tz_status = QLabel("")
        self._tz_status.setStyleSheet("color: #50c850; font-size: 11px; font-weight: bold;")
        self._tz_status.setVisible(False)
        sl.addWidget(self._tz_status)

        self._layout.addWidget(section)

    def _sync_timezone_combo(self, iana_name: str) -> None:
        if not hasattr(self, "_tz_combo"):
            return
        for idx in range(self._tz_combo.count()):
            if self._tz_combo.itemData(idx) == iana_name:
                if self._tz_combo.currentIndex() != idx:
                    self._tz_combo.blockSignals(True)
                    self._tz_combo.setCurrentIndex(idx)
                    self._tz_combo.blockSignals(False)
                break

    def _build_app_info(self) -> None:
        section = self._make_section("Application")
        sl = section.layout()

        current_theme = "Light" if self._tm.is_light else "Dark"
        self._add_info_row(sl, "Application", APP_NAME)
        self._add_info_row(sl, "Version", APP_VERSION)
        self._add_info_row(sl, "Framework", "PySide6 (Qt for Python)")
        self._add_info_row(sl, "Active Theme", f"Material {current_theme} Teal")
        self._add_info_row(sl, "Database Mode", "Read-Only (immutable)")
        self._layout.addWidget(section)

    def _build_db_info(self) -> None:
        section = self._make_section("Database Info")
        sl = section.layout()

        db = Database.get()
        self._add_info_row(sl, "Path", str(db.path))
        self._add_info_row(sl, "Size", f"{db.size_mb:.1f} MB")

        # Get SQLite version
        version = db.scalar("SELECT sqlite_version()")
        self._add_info_row(sl, "SQLite Version", str(version))

        # Page size and cache
        page_size = db.scalar("PRAGMA page_size")
        cache_size = db.scalar("PRAGMA cache_size")
        self._add_info_row(sl, "Page Size", f"{page_size:,} bytes" if page_size else "N/A")
        self._add_info_row(sl, "Cache Size", f"{abs(cache_size):,} KB" if cache_size else "N/A")

        self._layout.addWidget(section)

    def _build_table_stats(self) -> None:
        section = self._make_section("Table Statistics")
        sl = section.layout()

        db = Database.get()
        tables = [
            ("message", "Messages"),
            ("conversation", "Conversations"),
            ("contact", "Contacts"),
            ("media", "Media"),
            ("call_record", "Calls"),
            ("reaction", "Reactions"),
            ("mention", "Mentions"),
            ("system_event", "System Events"),
            ("ghost_message", "Ghost Messages"),
            ("edit_history", "Edit History"),
            ("location", "Locations"),
            ("message_link_detail", "Links"),
            ("poll", "Polls"),
            ("poll_option", "Poll Options"),
            ("poll_vote", "Poll Votes"),
            ("scheduled_event", "Scheduled Events"),
            ("stats_daily_activity", "Daily Stats"),
            ("stats_contact_activity", "Contact Stats"),
            ("stats_hourly_heatmap", "Heatmap Entries"),
        ]

        for table, label in tables:
            try:
                count = db.scalar(f"SELECT COUNT(*) FROM {table}")
                self._add_info_row(sl, label, f"{count:,}" if count else "0")
            except Exception:
                self._add_info_row(sl, label, "N/A")

        self._layout.addWidget(section)

    def _build_keyboard_shortcuts(self) -> None:
        section = self._make_section("\u2328\uFE0F  Keyboard Shortcuts")
        sl = section.layout()

        shortcuts = [
            ("Ctrl + D", "Go to Dashboard"),
            ("Ctrl + F", "Go to Search"),
            ("Escape", "Go to Conversations"),
        ]
        is_light = self._tm.is_light
        for key, desc in shortcuts:
            row = QHBoxLayout()
            key_label = QLabel(key)
            key_label.setFixedWidth(120)
            if is_light:
                key_label.setStyleSheet(
                    "background: #f0f2f5; "
                    "border-radius: 4px; padding: 4px 8px; "
                    "color: #00695c; font-family: monospace; font-size: 11px; "
                    "border: 1px solid #e0e0e0;"
                )
            else:
                key_label.setStyleSheet(
                    "background: rgba(128,128,128,0.08); "
                    "border-radius: 4px; padding: 4px 8px; "
                    "color: #00bcd4; font-family: monospace; font-size: 11px;"
                )
            key_label.setAlignment(Qt.AlignCenter)
            desc_label = QLabel(desc)
            if is_light:
                desc_label.setStyleSheet("color: #667781; font-size: 11px;")
            else:
                desc_label.setStyleSheet("color: #546e7a; font-size: 11px;")
            row.addWidget(key_label)
            row.addWidget(desc_label)
            row.addStretch()
            sl.addLayout(row)

        self._layout.addWidget(section)

    def _make_section(self, title: str) -> QFrame:
        section = QFrame()
        is_light = self._tm.is_light
        if is_light:
            section.setStyleSheet("""
                QFrame { background: #ffffff;
                         border-radius: 8px; border: 1px solid #e8eaed; }
            """)
        else:
            section.setStyleSheet("""
                QFrame { background: rgba(128,128,128,0.06);
                         border-radius: 8px; border: 1px solid rgba(128,128,128,0.12); }
            """)
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(6)

        label = QLabel(title)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            "background: #e8eaed;" if is_light else "background: rgba(128,128,128,0.12);"
        )
        sl.addWidget(sep)

        return section

    def _add_info_row(self, layout: QVBoxLayout, label: str, value: str) -> None:
        row = QHBoxLayout()
        is_light = self._tm.is_light
        lbl = QLabel(label)
        lbl.setFixedWidth(160)
        lbl.setStyleSheet(
            "color: #667781; font-size: 11px;" if is_light
            else "color: #78909c; font-size: 11px;"
        )
        val = QLabel(value)
        val.setStyleSheet(
            "color: #1b1b1b; font-size: 11px;" if is_light
            else "color: #cfd8dc; font-size: 11px;"
        )
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(lbl)
        row.addWidget(val, 1)
        layout.addLayout(row)
