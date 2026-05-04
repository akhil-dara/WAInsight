"""Group report customisation dialog.

Lets the investigator pick:
  * Output format (HTML or PDF).
  * Optional from/to date range to focus the report on a specific
    timeline.  Roster-level data (members, group identity, edit
    history) is always shown for context — only message-derived
    statistics are restricted.
  * Which sections to include.  Picks persist across runs via QSettings.
  * Save location (file dialog with format-aware extension).

Used by ``group_info_page._generate_group_report`` to drive the report
flow without hard-coding output paths or formats.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDate, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


# Section catalog — keys mirror the keys in ``generate_group_report``'s
# section order so toggling them off can simply skip the corresponding
# ``_section_*`` call.
SECTION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Group identity", [
        ("identity",       "Group identity (name, JID, creator, settings)"),
        ("owner_policy",   "Device-owner posting permission & group policy"),
        ("edit_history",   "Group edit history (subject / icon / description)"),
    ]),
    ("Members & contributors", [
        ("summary",        "Top-line stats summary"),
        ("members",        "Full member roster"),
        ("contributors",   "Top contributors (by message count)"),
        ("forwarders",     "Top forwarders"),
        ("devices",        "Sender device platforms (Android / iPhone / Web / companion)"),
        ("past_members",   "Former members"),
    ]),
    ("Network & content", [
        ("mentions",       "Mention network (who @-mentions whom — humans only)"),
        ("activity",       "Activity patterns (hourly / daily heatmap)"),
        ("calls",          "Call history (voice / video / group calls)"),
        ("locations",      "Shared locations"),
        ("admin_audit",    "Admin audit trail (promotions / demotions / settings)"),
        ("media_links",    "Media types + top link domains"),
    ]),
    ("Bots", [
        ("bot_activity",   "Meta AI / bot activity (replies + top human summoners)"),
    ]),
]

ALL_SECTION_KEYS = [k for _, rows in SECTION_GROUPS for k, _ in rows]
DEFAULT_SECTIONS = {k: True for k in ALL_SECTION_KEYS}

_SETTINGS_KEY = "GroupReportDialog/sections"
_SETTINGS_FORMAT = "GroupReportDialog/format"
_SETTINGS_TOP_N = "GroupReportDialog/top_n"
_SETTINGS_LAST_DIR = "GroupReportDialog/last_dir"


class GroupReportDialog(QDialog):
    """Customisable group-report build dialog.

    Returns from ``exec()`` with ``result.is_ok = True`` when the user
    confirms; the parent then reads the chosen ``output_path``,
    ``output_format``, ``date_from_ms``, ``date_to_ms``, ``sections``,
    and ``top_n`` to drive the actual report generation.
    """

    def __init__(self, parent, group_name: str, default_dir: str | os.PathLike,
                 conversation_id: int) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Generate Group Report — {group_name}")
        self.setMinimumWidth(640)
        self.setMinimumHeight(640)

        self._group_name = group_name
        self._conv_id = conversation_id
        self._default_dir = Path(default_dir)
        self._settings = QSettings()
        self.is_ok = False

        # ----- restore last picks ----- #
        try:
            last_sections_json = self._settings.value(_SETTINGS_KEY, "")
            self._initial_sections = (
                json.loads(last_sections_json)
                if last_sections_json else dict(DEFAULT_SECTIONS)
            )
        except Exception:
            self._initial_sections = dict(DEFAULT_SECTIONS)
        self._initial_format = (
            self._settings.value(_SETTINGS_FORMAT, "html") or "html"
        ).lower()
        try:
            self._initial_top_n = int(self._settings.value(_SETTINGS_TOP_N, 20) or 20)
        except (TypeError, ValueError):
            self._initial_top_n = 20

        # ----- build UI ----- #
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel(
            f"<b style='font-size:14px;'>{self._h(group_name)}</b>"
            f"<br><span style='color:#666;font-size:11px;'>"
            f"Customise what the forensic report includes, then choose "
            f"a save location.</span>"
        )
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        root.addWidget(title)

        # Two-column body: format/date on the left, sections on the right.
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addLayout(self._build_left_pane(), 0)
        body.addWidget(self._build_sections_pane(), 1)
        root.addLayout(body)

        # ----- save bar ----- #
        save_box = QGroupBox("Save location")
        sl = QHBoxLayout(save_box)
        sl.setContentsMargins(10, 12, 10, 10)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Click Browse…")
        self._refresh_default_path()
        sl.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        sl.addWidget(browse_btn)
        root.addWidget(save_box)

        # ----- footer buttons ----- #
        bb = QDialogButtonBox()
        gen_btn = bb.addButton("Generate Report", QDialogButtonBox.AcceptRole)
        gen_btn.setDefault(True)
        gen_btn.clicked.connect(self._on_accept)
        cancel_btn = bb.addButton(QDialogButtonBox.Cancel)
        cancel_btn.clicked.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #
    # UI construction helpers
    # ------------------------------------------------------------------ #

    def _build_left_pane(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(12)

        # Format
        fmt_box = QGroupBox("Output format")
        fl = QVBoxLayout(fmt_box)
        fl.setContentsMargins(10, 12, 10, 10)
        fl.setSpacing(4)
        self._fmt_group = QButtonGroup(self)
        self._html_radio = QRadioButton("HTML  (interactive, best for browser viewing)")
        self._pdf_radio = QRadioButton("PDF   (court-friendly, paginated, single file)")
        self._fmt_group.addButton(self._html_radio)
        self._fmt_group.addButton(self._pdf_radio)
        if self._initial_format == "pdf":
            self._pdf_radio.setChecked(True)
        else:
            self._html_radio.setChecked(True)
        self._html_radio.toggled.connect(self._refresh_default_path)
        fl.addWidget(self._html_radio)
        fl.addWidget(self._pdf_radio)
        col.addWidget(fmt_box)

        # Date range
        date_box = QGroupBox("Timeline filter (optional)")
        dl = QGridLayout(date_box)
        dl.setContentsMargins(10, 12, 10, 10)
        dl.setHorizontalSpacing(8)
        dl.setVerticalSpacing(6)

        self._date_enable = QCheckBox(
            "Restrict report to a specific date range"
        )
        self._date_enable.setToolTip(
            "When enabled, message-derived statistics (top contributors, "
            "calls, locations, mentions, activity, media, links) are "
            "restricted to messages within the selected window. The group "
            "roster, identity and edit history always cover the full "
            "history for context."
        )
        self._date_enable.toggled.connect(self._on_date_toggle)
        dl.addWidget(self._date_enable, 0, 0, 1, 2)

        dl.addWidget(QLabel("From:"), 1, 0)
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_from.setEnabled(False)
        dl.addWidget(self._date_from, 1, 1)
        dl.addWidget(QLabel("To:"), 2, 0)
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._date_to.setEnabled(False)
        dl.addWidget(self._date_to, 2, 1)

        # Default to "last 30 days" when enabled
        today = QDate.currentDate()
        self._date_from.setDate(today.addDays(-30))
        self._date_to.setDate(today)
        col.addWidget(date_box)

        # Top-N control
        topn_box = QGroupBox("Limits")
        tl = QHBoxLayout(topn_box)
        tl.setContentsMargins(10, 12, 10, 10)
        tl.addWidget(QLabel("Top-N for ranked tables:"))
        self._top_n_spin = QSpinBox()
        self._top_n_spin.setRange(5, 100)
        self._top_n_spin.setSingleStep(5)
        self._top_n_spin.setValue(self._initial_top_n)
        self._top_n_spin.setToolTip(
            "Cap for Top Contributors / Top Forwarders / Top Mentioners / "
            "Top Mentioned / Top Link Domains.  Each table's header will "
            "show the resolved cap, e.g. 'Top 20 Contributors'."
        )
        tl.addWidget(self._top_n_spin)
        tl.addStretch()
        col.addWidget(topn_box)

        col.addStretch()
        return col

    def _build_sections_pane(self) -> QWidget:
        wrap = QGroupBox("Sections to include")
        ol = QVBoxLayout(wrap)
        ol.setContentsMargins(10, 12, 10, 10)
        ol.setSpacing(6)

        # Quick all / none row
        qr = QHBoxLayout()
        qr.setSpacing(6)
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(self._select_all)
        none_btn = QPushButton("Clear all")
        none_btn.clicked.connect(self._select_none)
        qr.addWidget(all_btn)
        qr.addWidget(none_btn)
        qr.addStretch()
        ol.addLayout(qr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(10)

        self._checkboxes: dict[str, QCheckBox] = {}
        for group_name, items in SECTION_GROUPS:
            grp = QGroupBox(group_name)
            grp_layout = QVBoxLayout(grp)
            grp_layout.setContentsMargins(8, 8, 8, 8)
            grp_layout.setSpacing(2)
            for key, label in items:
                cb = QCheckBox(label)
                cb.setChecked(self._initial_sections.get(key, True))
                self._checkboxes[key] = cb
                grp_layout.addWidget(cb)
            il.addWidget(grp)
        il.addStretch()

        scroll.setWidget(inner)
        ol.addWidget(scroll, 1)
        return wrap

    # ------------------------------------------------------------------ #
    # Slots
    # ------------------------------------------------------------------ #

    def _on_date_toggle(self, on: bool) -> None:
        self._date_from.setEnabled(on)
        self._date_to.setEnabled(on)

    def _select_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _select_none(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _refresh_default_path(self) -> None:
        """Update the path field to use the chosen format's extension."""
        fmt = "pdf" if (hasattr(self, "_pdf_radio") and self._pdf_radio.isChecked()) else "html"
        safe_name = "".join(
            c if c.isalnum() or c in " _-" else "_" for c in self._group_name
        )[:50].strip().replace(" ", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Honour the directory of any existing path the user picked
        try:
            current = Path(self._path_edit.text()).expanduser()
            base_dir = current.parent if current.parent.exists() else self._default_dir
        except Exception:
            base_dir = self._default_dir
        last_dir = self._settings.value(_SETTINGS_LAST_DIR, "")
        if last_dir and Path(last_dir).exists():
            base_dir = Path(last_dir)
        if not base_dir.exists():
            base_dir = self._default_dir
        path = base_dir / f"group_report_{safe_name}_{self._conv_id}_{ts}.{fmt}"
        self._path_edit.setText(str(path))

    def _on_browse(self) -> None:
        fmt = "pdf" if self._pdf_radio.isChecked() else "html"
        ext_filter = (
            "PDF document (*.pdf);;HTML page (*.html)" if fmt == "pdf"
            else "HTML page (*.html);;PDF document (*.pdf)"
        )
        suggested = self._path_edit.text().strip() or str(
            self._default_dir / f"group_report.{fmt}"
        )
        path, chosen_filter = QFileDialog.getSaveFileName(
            self, "Save report as…", suggested, ext_filter,
        )
        if not path:
            return
        # If the user typed a name without extension, append the picked
        # filter's extension so the format stays in sync with the radio.
        if not path.lower().endswith((".pdf", ".html")):
            path += ".pdf" if "pdf" in chosen_filter.lower() else ".html"
        self._path_edit.setText(path)
        # Sync the format radio with whatever extension was picked.
        if path.lower().endswith(".pdf") and not self._pdf_radio.isChecked():
            self._pdf_radio.setChecked(True)
        elif path.lower().endswith(".html") and not self._html_radio.isChecked():
            self._html_radio.setChecked(True)

    def _on_accept(self) -> None:
        path_text = self._path_edit.text().strip()
        if not path_text:
            QMessageBox.warning(self, "Missing save location",
                                "Please pick where to save the report.")
            return
        out_path = Path(path_text)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Cannot create folder",
                                f"Could not create the folder:\n  {out_path.parent}\n\n{e}")
            return

        # Persist user's picks
        self._settings.setValue(
            _SETTINGS_KEY,
            json.dumps(self.sections, separators=(",", ":")),
        )
        self._settings.setValue(_SETTINGS_FORMAT, self.output_format)
        self._settings.setValue(_SETTINGS_TOP_N, self.top_n)
        self._settings.setValue(_SETTINGS_LAST_DIR, str(out_path.parent))

        if not any(self.sections.values()):
            QMessageBox.warning(self, "No sections selected",
                                "Pick at least one section to include.")
            return

        self.is_ok = True
        self.accept()

    # ------------------------------------------------------------------ #
    # Result accessors
    # ------------------------------------------------------------------ #

    @property
    def output_path(self) -> Path:
        return Path(self._path_edit.text().strip())

    @property
    def output_format(self) -> str:
        return "pdf" if self._pdf_radio.isChecked() else "html"

    @property
    def sections(self) -> dict[str, bool]:
        return {key: cb.isChecked() for key, cb in self._checkboxes.items()}

    @property
    def top_n(self) -> int:
        return int(self._top_n_spin.value())

    @property
    def date_from_ms(self) -> Optional[int]:
        if not self._date_enable.isChecked():
            return None
        d = self._date_from.date()
        dt = datetime(d.year(), d.month(), d.day(), 0, 0, 0).astimezone()
        return int(dt.timestamp() * 1000)

    @property
    def date_to_ms(self) -> Optional[int]:
        if not self._date_enable.isChecked():
            return None
        d = self._date_to.date()
        dt = datetime(d.year(), d.month(), d.day(), 23, 59, 59).astimezone()
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _h(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
