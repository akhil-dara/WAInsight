"""Section-picker dialog for the contact / group report generators.

Lets the investigator:
  * tick which sections they want included in a report,
  * pick the output format (HTML or PDF),
  * pick / browse to a save location.

The choice is persisted in ``QSettings`` (as a JSON string — PySide6's
``QSettings.value`` does not support ``type=dict``) so the defaults
match the user's last selection.

Used by:
    - ``contact_detail_page._generate_contact_report``
    - ``group_info_page._generate_group_report`` (future)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt

logger = logging.getLogger(__name__)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


# Group sections into logical clusters so the dialog reads top-to-bottom
# rather than as a flat 10-row list.  Each tuple is
#   (group_label, [(section_key, display_label), ...]).
SECTION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Identity",
        [
            ("identity",
             "Contact identity (name, phone, JID, LID, business info)"),
        ],
    ),
    (
        "Activity",
        [
            ("overall_stats",
             "Overall messaging totals"),
            ("activity_patterns",
             "Activity patterns (hourly / daily heatmap)"),
            ("group_activity",
             "Per-group activity breakdown"),
        ],
    ),
    (
        "Communication",
        [
            ("direct_conversation",
             "1-on-1 conversation summary"),
            ("calls",
             "Call statistics & per-call detail"),
            ("groups_in_common",
             "Groups in common"),
        ],
    ),
    (
        "Network & content",
        [
            ("mentions",
             "Mentions network (given & received)"),
            ("reactions",
             "Reactions given & received"),
            ("media_links",
             "Media types & top link domains"),
        ],
    ),
]


class ReportSectionsDialog(QDialog):
    """Modal dialog where the investigator picks report sections.

    Usage
    -----
        dlg = ReportSectionsDialog("Alice Smith", parent=self)
        if dlg.exec() == QDialog.Accepted:
            sections = dlg.get_selection()  # dict[str, bool]
            generate_contact_report(..., sections=sections)
    """

    # Keys under which the last selection / format / dir are persisted.
    _SETTINGS_KEY = "wainsight/contact_report_sections"
    _FORMAT_KEY   = "wainsight/contact_report_format"
    _LAST_DIR_KEY = "wainsight/contact_report_last_dir"

    FORMAT_HTML = "html"
    FORMAT_PDF  = "pdf"

    def __init__(self, contact_name: str, parent=None,
                 default_dir: Optional[Path] = None,
                 default_filename_stem: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Report sections — {contact_name}")
        self.setMinimumSize(640, 720)
        self._contact_name = contact_name
        self._checkboxes: dict[str, QCheckBox] = {}
        self._selected: Optional[dict[str, bool]] = None
        self._chosen_format: str = self.FORMAT_HTML
        self._chosen_path: Optional[Path] = None
        self._default_dir = Path(default_dir) if default_dir else Path.home()
        self._default_stem = default_filename_stem or "contact_report"
        self._build_ui()
        self._load_persisted_selection()
        self._refresh_default_path()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)

        # Header
        header = QLabel(
            "<b>Choose which sections to include in this report.</b><br>"
            "<span style='color:#666;'>Skipping a section also skips its "
            "underlying SQL queries — useful when one part of the data is "
            "slow or not relevant for this case.</span>"
        )
        header.setWordWrap(True)
        root.addWidget(header)

        # Toolbar — Select all / Clear all / Reset to defaults
        bar = QHBoxLayout()
        for label, handler in (
            ("Select all", lambda: self._set_all(True)),
            ("Clear all", lambda: self._set_all(False)),
            ("Reset to defaults", self._reset_defaults),
        ):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(handler)
            bar.addWidget(btn)
        bar.addStretch()
        root.addLayout(bar)

        # Scrollable section list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_l = QVBoxLayout(inner)
        inner_l.setSpacing(14)

        for group_label, items in SECTION_GROUPS:
            heading = QLabel(f"<b>{group_label}</b>")
            heading.setStyleSheet("color:#444;margin-top:6px;")
            inner_l.addWidget(heading)
            for key, display in items:
                cb = QCheckBox(display)
                cb.setChecked(True)  # actual default loaded later
                inner_l.addWidget(cb)
                self._checkboxes[key] = cb

        inner_l.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # ---- Output format + save location ----
        # Two fixed groups:
        #   • Format: HTML (rich, interactive, fast) vs PDF (printable,
        #     forensic-grade hand-off).  Same dataset; PDF is rendered
        #     by QWebEngineView.printToPdf so colours / charts survive.
        #   • Save location: pre-filled to <case>/reports/<safe_name>_<ts>
        #     with the right extension, but the user can browse anywhere.
        format_box = QGroupBox("Output format")
        fl = QHBoxLayout(format_box)
        fl.setContentsMargins(10, 12, 10, 10)
        fl.setSpacing(20)
        self._format_group = QButtonGroup(self)
        self._fmt_html = QRadioButton("HTML  (interactive, opens in browser)")
        self._fmt_pdf  = QRadioButton("PDF  (printable, single file)")
        self._format_group.addButton(self._fmt_html)
        self._format_group.addButton(self._fmt_pdf)
        # Restore last-used format
        last_fmt = QSettings().value(
            self._FORMAT_KEY, self.FORMAT_HTML, type=str
        ) or self.FORMAT_HTML
        if last_fmt == self.FORMAT_PDF:
            self._fmt_pdf.setChecked(True)
        else:
            self._fmt_html.setChecked(True)
        self._fmt_html.toggled.connect(self._refresh_default_path)
        self._fmt_pdf.toggled.connect(self._refresh_default_path)
        fl.addWidget(self._fmt_html)
        fl.addWidget(self._fmt_pdf)
        fl.addStretch()
        root.addWidget(format_box)

        save_box = QGroupBox("Save location")
        sl = QHBoxLayout(save_box)
        sl.setContentsMargins(10, 12, 10, 10)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Pick where to save the report…")
        sl.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        sl.addWidget(browse_btn)
        root.addWidget(save_box)

        # OK / Cancel
        bb = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        ok_btn = bb.button(QDialogButtonBox.Ok)
        ok_btn.setText("Generate report")
        ok_btn.setDefault(True)
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load_persisted_selection(self) -> None:
        """Restore the last selection from QSettings, falling back to defaults.

        Stored as a JSON-encoded string because PySide6's
        ``QSettings.value(... type=dict)`` raises ``TypeError`` — only
        the primitive Qt-known types are accepted there.
        """
        settings = QSettings()
        raw_str = settings.value(self._SETTINGS_KEY, "", type=str) or ""
        if not raw_str:
            return  # First run — checkboxes keep their initial state.
        try:
            raw = json.loads(raw_str)
            if not isinstance(raw, dict):
                return
        except (ValueError, TypeError):
            logger.warning("Corrupt report-sections setting; ignoring")
            return
        for key, cb in self._checkboxes.items():
            if key in raw:
                cb.setChecked(bool(raw[key]))

    def _persist_selection(self, selection: dict[str, bool]) -> None:
        """Persist as a JSON string (Qt's dict-typed value support is unreliable)."""
        QSettings().setValue(self._SETTINGS_KEY, json.dumps(selection))

    # ------------------------------------------------------------------ #
    # Toolbar handlers
    # ------------------------------------------------------------------ #

    def _set_all(self, checked: bool) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(checked)

    def _reset_defaults(self) -> None:
        # All sections default to ON.
        self._set_all(True)

    # ------------------------------------------------------------------ #
    # Accept / reject
    # ------------------------------------------------------------------ #

    def _refresh_default_path(self) -> None:
        """Pre-fill the save-location field with a sensible default
        (last-used directory or the case's ``reports/`` subfolder),
        the safe-encoded contact name, and a timestamp + the right
        extension for the currently-selected format.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = self.fmt_extension
        last_dir_str = QSettings().value(self._LAST_DIR_KEY, "", type=str) or ""
        try:
            base = (
                Path(last_dir_str) if last_dir_str and Path(last_dir_str).exists()
                else self._default_dir
            )
        except Exception:
            base = self._default_dir
        safe = "".join(
            c if c.isalnum() or c in " _-" else "_"
            for c in (self._contact_name or self._default_stem)
        )[:60].strip()
        if not safe:
            safe = self._default_stem
        path = base / f"{self._default_stem}_{safe}_{ts}.{ext}"
        self._path_edit.setText(str(path))

    def _on_browse(self) -> None:
        suggested = self._path_edit.text().strip() or str(self._default_dir)
        ext = self.fmt_extension
        filt = "HTML page (*.html)" if ext == "html" else "PDF document (*.pdf)"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report as…", suggested, filt
        )
        if not path:
            return
        # Force the right extension if the user typed without one
        if not path.lower().endswith(f".{ext}"):
            path += f".{ext}"
        self._path_edit.setText(path)

    def _on_accept(self) -> None:
        selection = {k: cb.isChecked() for k, cb in self._checkboxes.items()}
        if not any(selection.values()):
            QMessageBox.warning(
                self,
                "No sections selected",
                "Please tick at least one section before generating the report.",
            )
            return
        path_str = self._path_edit.text().strip()
        if not path_str:
            QMessageBox.warning(
                self,
                "Save location",
                "Pick a save location for the report.",
            )
            return
        path = Path(path_str)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(
                self, "Cannot create folder",
                f"{path.parent}\n\n{e}"
            )
            return

        self._selected = selection
        self._chosen_format = (
            self.FORMAT_PDF if self._fmt_pdf.isChecked() else self.FORMAT_HTML
        )
        self._chosen_path = path
        self._persist_selection(selection)
        QSettings().setValue(self._FORMAT_KEY, self._chosen_format)
        QSettings().setValue(self._LAST_DIR_KEY, str(path.parent))
        self.accept()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_selection(self) -> Optional[dict[str, bool]]:
        """Return the chosen sections, or ``None`` if dialog was cancelled."""
        return self._selected

    @property
    def output_format(self) -> str:
        """``"html"`` or ``"pdf"`` — set on accept, dialog default
        is HTML so direct callers that don't read this still work."""
        return self._chosen_format

    @property
    def fmt_extension(self) -> str:
        """File extension for the currently-selected format
        (live; reflects the radio buttons before accept too)."""
        if hasattr(self, "_fmt_pdf") and self._fmt_pdf.isChecked():
            return "pdf"
        return "html"

    @property
    def output_path(self) -> Optional[Path]:
        """Where the analyst chose to save the report."""
        return self._chosen_path
