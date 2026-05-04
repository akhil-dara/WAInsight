"""Customisable Media Forensics Dashboard dialog.

The dashboard is a *folder-shaped* offline artifact (not a single .html
file).  The dialog therefore picks an **output directory**, then writes
``index.html`` plus ``data/``, ``vendor/``, and ``thumbs/`` underneath.
The folder can be zipped / handed off / opened by double-clicking
``index.html`` in any modern browser — no launcher required.

Choices the analyst makes:
  * **Scope** — whole case OR a single conversation (search-as-you-type).
  * **Hide stickers** — sticker rows excluded everywhere (default ON).
  * **Include thumbnails** — emit the ``thumbs/`` tree (default ON).
  * **Thumbnail quality** — Low / Medium / High; trades disk vs detail.
  * **Include orphan files** — extra sidebar tab for media on disk with
    no surviving message (whole-case scope only).
  * **Output folder** — created if missing.

Picks persist via QSettings so the next invocation defaults to the
analyst's previous choice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


_S_HIDE_STICK = "MediaReportDialog/hide_stickers"
_S_THUMBS     = "MediaReportDialog/include_thumbs"
_S_QUALITY    = "MediaReportDialog/thumb_quality"
_S_ORPHANS    = "MediaReportDialog/include_orphans"
_S_LAST_DIR   = "MediaReportDialog/last_dir"


class MediaReportDialog(QDialog):
    """Pick scope / quality / output folder for a Media Dashboard build."""

    QUALITY_LOW    = "low"
    QUALITY_MEDIUM = "medium"
    QUALITY_HIGH   = "high"

    # Kept so legacy callers that read .layout_mode / .top_n / .sections
    # don't break — the new dashboard ignores them but the old wire-up
    # still reads them in some paths.
    LAYOUT_DASHBOARD = "dashboard"
    LAYOUT_BARE      = "bare"

    def __init__(self, parent, default_dir: str | os.PathLike,
                 conversations: list[dict],
                 default_conv_id: Optional[int] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Media forensics dashboard")
        self.setMinimumWidth(700)
        self.setMinimumHeight(560)

        self._convs = list(conversations or [])
        self._default_dir = Path(default_dir)
        self._settings = QSettings()
        self.is_ok = False

        # ---- Restore picks ----
        try:
            self._initial_hide = (
                self._settings.value(_S_HIDE_STICK, "true")
                not in ("false", False, 0, "0")
            )
        except Exception:
            self._initial_hide = True
        try:
            self._initial_thumbs = (
                self._settings.value(_S_THUMBS, "true")
                not in ("false", False, 0, "0")
            )
        except Exception:
            self._initial_thumbs = True
        try:
            self._initial_orphans = (
                self._settings.value(_S_ORPHANS, "true")
                not in ("false", False, 0, "0")
            )
        except Exception:
            self._initial_orphans = True
        self._initial_quality = (
            self._settings.value(_S_QUALITY, self.QUALITY_MEDIUM)
            or self.QUALITY_MEDIUM
        ).lower()

        # ---- Build UI ----
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel(
            "<b>Generate the offline Media Forensics Dashboard</b><br>"
            "<span style='color:#666;font-size:11px;'>"
            "Folder-shaped artifact — open <code>index.html</code> in any "
            "browser, no launcher required.  Cascading filters by "
            "conversation / sender / status / MIME / extension / date, "
            "in-browser export to CSV / XLSX / HTML, "
            "scales to ~200k media rows."
            "</span>"
        )
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        root.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addLayout(self._build_scope_pane(default_conv_id), 1)
        body.addLayout(self._build_options_pane(), 1)
        root.addLayout(body)

        # ---- Output folder ----
        out_box = QGroupBox("Output folder")
        ol = QVBoxLayout(out_box)
        ol.setContentsMargins(10, 12, 10, 10)
        hint = QLabel(
            "<span style='color:#666;font-size:11px;'>"
            "The dashboard writes <b>index.html + data/ + vendor/ + thumbs/</b> "
            "into this folder.  An existing folder is reused (re-runs are "
            "cheap; thumbnails dedup by hash).</span>"
        )
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        ol.addWidget(hint)

        rl = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._refresh_default_path()
        rl.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        rl.addWidget(browse_btn)
        ol.addLayout(rl)

        # Show what'll happen at this folder
        self._open_after = QCheckBox(
            "Open dashboard in default browser when build finishes"
        )
        self._open_after.setChecked(True)
        ol.addWidget(self._open_after)
        root.addWidget(out_box)

        # ---- Buttons ----
        bb = QDialogButtonBox()
        gen = bb.addButton("Build Dashboard", QDialogButtonBox.AcceptRole)
        gen.setDefault(True)
        gen.clicked.connect(self._on_accept)
        cancel = bb.addButton(QDialogButtonBox.Cancel)
        cancel.clicked.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------ #

    def _build_scope_pane(self, default_conv_id: Optional[int]) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(12)

        scope_box = QGroupBox("Scope")
        sl = QVBoxLayout(scope_box)
        sl.setContentsMargins(10, 12, 10, 10)
        sl.setSpacing(6)

        self._scope_group = QButtonGroup(self)
        self._scope_all = QRadioButton(
            "Whole case  —  every conversation that has media"
        )
        self._scope_pick = QRadioButton(
            "Specific conversations  —  tick one or more from the list below"
        )
        self._scope_group.addButton(self._scope_all)
        self._scope_group.addButton(self._scope_pick)
        sl.addWidget(self._scope_all)
        sl.addWidget(self._scope_pick)

        # Filter input above the list — type-as-you-search
        self._conv_filter = QLineEdit()
        self._conv_filter.setPlaceholderText(
            "Filter conversations by name, JID, or LID…"
        )
        self._conv_filter.textChanged.connect(self._on_conv_filter_changed)
        sl.addWidget(self._conv_filter)

        # Multi-select checklist (one row per conversation, tickable)
        self._conv_list = QListWidget()
        self._conv_list.setSelectionMode(QAbstractItemView.NoSelection)
        self._conv_list.setMinimumHeight(220)
        self._conv_list.setUniformItemSizes(True)
        for c in self._convs:
            label = (
                f"{c.get('display_name') or '?'}   "
                f"[{c.get('chat_type') or 'personal'}]   "
                f"{c.get('jid_raw_string') or ''}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, c.get("id"))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._conv_list.addItem(item)
        self._conv_list.itemChanged.connect(self._on_conv_check_changed)
        sl.addWidget(self._conv_list, 1)

        # Selection summary + bulk-action row
        bulk = QHBoxLayout()
        bulk.setSpacing(6)
        all_btn = QPushButton("Select all visible")
        none_btn = QPushButton("Clear selection")
        invert_btn = QPushButton("Invert visible")
        all_btn.clicked.connect(lambda: self._bulk_check(True))
        none_btn.clicked.connect(lambda: self._bulk_check(False))
        invert_btn.clicked.connect(self._bulk_invert)
        bulk.addWidget(all_btn)
        bulk.addWidget(none_btn)
        bulk.addWidget(invert_btn)
        bulk.addStretch()
        self._sel_summary = QLabel("0 selected")
        self._sel_summary.setStyleSheet("color:#666; font-size:11px;")
        bulk.addWidget(self._sel_summary)
        sl.addLayout(bulk)

        # Default selection
        if default_conv_id is not None:
            self._scope_pick.setChecked(True)
            self._set_list_enabled(True)
            for i in range(self._conv_list.count()):
                it = self._conv_list.item(i)
                if it.data(Qt.UserRole) == default_conv_id:
                    it.setCheckState(Qt.Checked)
                    self._conv_list.scrollToItem(it)
                    break
        else:
            self._scope_all.setChecked(True)
            self._set_list_enabled(False)

        self._scope_pick.toggled.connect(self._set_list_enabled)
        self._scope_pick.toggled.connect(lambda _: self._refresh_default_path())
        col.addWidget(scope_box, 1)
        return col

    # ---- multi-conv helpers ---------------------------------------- #

    def _set_list_enabled(self, on: bool) -> None:
        self._conv_list.setEnabled(on)
        self._conv_filter.setEnabled(on)

    def _on_conv_filter_changed(self, text: str) -> None:
        q = (text or "").strip().lower()
        for i in range(self._conv_list.count()):
            it = self._conv_list.item(i)
            it.setHidden(bool(q) and q not in it.text().lower())

    def _on_conv_check_changed(self, _item) -> None:
        self._refresh_sel_summary()
        self._refresh_default_path()

    def _bulk_check(self, on: bool) -> None:
        for i in range(self._conv_list.count()):
            it = self._conv_list.item(i)
            if not it.isHidden():
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._refresh_sel_summary()
        self._refresh_default_path()

    def _bulk_invert(self) -> None:
        for i in range(self._conv_list.count()):
            it = self._conv_list.item(i)
            if not it.isHidden():
                it.setCheckState(
                    Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked
                )
        self._refresh_sel_summary()
        self._refresh_default_path()

    def _refresh_sel_summary(self) -> None:
        n = sum(
            1 for i in range(self._conv_list.count())
            if self._conv_list.item(i).checkState() == Qt.Checked
        )
        self._sel_summary.setText(f"{n} selected")

    def _build_options_pane(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(12)

        opt_box = QGroupBox("Options")
        ol = QVBoxLayout(opt_box)
        ol.setContentsMargins(10, 12, 10, 10)
        ol.setSpacing(8)

        self._hide_stickers = QCheckBox("Hide stickers everywhere")
        self._hide_stickers.setChecked(self._initial_hide)
        self._hide_stickers.setToolTip(
            "Excludes sticker rows from the dashboard, the histogram, "
            "and the cross-chat sharing index.  Recommended for triage."
        )
        ol.addWidget(self._hide_stickers)

        self._inc_thumbs = QCheckBox(
            "Include thumbnails (sharded by hash prefix)"
        )
        self._inc_thumbs.setChecked(self._initial_thumbs)
        self._inc_thumbs.setToolTip(
            "When off, the dashboard shows generic file-type icons "
            "instead of thumbnails.  Disable for very large cases."
        )
        ol.addWidget(self._inc_thumbs)

        # Thumbnail quality
        q_box = QFrame()
        ql = QHBoxLayout(q_box)
        ql.setContentsMargins(20, 0, 0, 0)
        ql.setSpacing(6)
        ql.addWidget(QLabel("Quality:"))
        self._quality_group = QButtonGroup(self)
        self._q_low = QRadioButton("Low (≈80 px, q60)")
        self._q_med = QRadioButton("Medium (≈160 px, q72)")
        self._q_high = QRadioButton("High (≈320 px, q82)")
        for r in (self._q_low, self._q_med, self._q_high):
            self._quality_group.addButton(r)
            ql.addWidget(r)
        if self._initial_quality == self.QUALITY_LOW:
            self._q_low.setChecked(True)
        elif self._initial_quality == self.QUALITY_HIGH:
            self._q_high.setChecked(True)
        else:
            self._q_med.setChecked(True)
        ql.addStretch()
        ol.addWidget(q_box)
        # Disable quality row if thumbs off
        def _sync_q(): q_box.setEnabled(self._inc_thumbs.isChecked())
        self._inc_thumbs.toggled.connect(lambda _: _sync_q())
        _sync_q()

        self._inc_orphans = QCheckBox(
            "Include orphan files (whole-case scope only)"
        )
        self._inc_orphans.setChecked(self._initial_orphans)
        self._inc_orphans.setToolTip(
            "Adds a sidebar tab listing media files on disk that have no "
            "surviving message (cleared chats, reinstall, etc.)."
        )
        ol.addWidget(self._inc_orphans)

        col.addWidget(opt_box)
        col.addStretch()
        return col

    # ------------------------------------------------------------------ #
    # Path + accept
    # ------------------------------------------------------------------ #

    def _refresh_default_path(self) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        last_dir = self._settings.value(_S_LAST_DIR, "") or str(self._default_dir)
        try:
            base = Path(last_dir) if Path(last_dir).exists() else self._default_dir
        except Exception:
            base = self._default_dir
        scope_tag = "case"
        if hasattr(self, "_scope_pick") and self._scope_pick.isChecked():
            ids = self.selected_conv_ids
            if len(ids) == 1:
                scope_tag = f"conv{ids[0]}"
            elif len(ids) > 1:
                scope_tag = f"conv{len(ids)}sel"
        path = base / f"media_dashboard_{scope_tag}_{ts}"
        self._path_edit.setText(str(path))

    def _on_browse(self) -> None:
        suggested = self._path_edit.text().strip() or str(self._default_dir)
        path = QFileDialog.getExistingDirectory(
            self, "Pick output folder for the dashboard", suggested,
            QFileDialog.ShowDirsOnly,
        )
        if not path:
            return
        self._path_edit.setText(path)

    def _on_accept(self) -> None:
        if self._scope_pick.isChecked() and not self.selected_conv_ids:
            QMessageBox.warning(self, "Pick conversations",
                                "Tick at least one conversation in the list, "
                                "or switch to whole-case scope.")
            return
        path = self._path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Output folder", "Pick an output folder.")
            return
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Cannot create folder",
                                 f"{path}\n\n{e}")
            return

        self._settings.setValue(_S_HIDE_STICK, self.hide_stickers)
        self._settings.setValue(_S_THUMBS, self.include_thumbnails)
        self._settings.setValue(_S_QUALITY, self.thumb_quality)
        self._settings.setValue(_S_ORPHANS, self.include_orphans)
        self._settings.setValue(_S_LAST_DIR, str(Path(path).parent))

        self.is_ok = True
        self.accept()

    # ------------------------------------------------------------------ #
    # Result accessors
    # ------------------------------------------------------------------ #

    @property
    def output_path(self) -> Path:
        """The output **directory** (not a file).  The generator writes
        ``index.html`` + ``data/`` + ``vendor/`` + ``thumbs/`` here.
        """
        return Path(self._path_edit.text().strip())

    @property
    def selected_conv_ids(self) -> list[int]:
        """Returns the picked conversation IDs.  Empty list means
        whole-case scope.  When the pick mode is on, returns every
        ticked conversation (length 1+).  Same value drives the file
        naming + the backend WHERE clause.
        """
        if not self._scope_pick.isChecked():
            return []
        ids: list[int] = []
        for i in range(self._conv_list.count()):
            it = self._conv_list.item(i)
            if it.checkState() == Qt.Checked:
                cid = it.data(Qt.UserRole)
                if cid is not None:
                    ids.append(int(cid))
        return ids

    @property
    def selected_conv_id(self) -> Optional[int]:
        """Backwards-compat alias: returns the first picked conv id, or
        ``None`` for whole-case / no-selection.  Kept so legacy callers
        that haven't migrated to ``selected_conv_ids`` still work.
        """
        ids = self.selected_conv_ids
        return ids[0] if len(ids) == 1 else None

    @property
    def hide_stickers(self) -> bool:
        return self._hide_stickers.isChecked()

    @property
    def include_thumbnails(self) -> bool:
        return self._inc_thumbs.isChecked()

    @property
    def include_orphans(self) -> bool:
        return self._inc_orphans.isChecked()

    @property
    def thumb_quality(self) -> str:
        if self._q_low.isChecked():
            return self.QUALITY_LOW
        if self._q_high.isChecked():
            return self.QUALITY_HIGH
        return self.QUALITY_MEDIUM

    @property
    def open_after(self) -> bool:
        return self._open_after.isChecked()

    # Legacy compat — old wire-up may still read these
    @property
    def sections(self) -> dict[str, bool]:
        return {
            "orphans": self.include_orphans,
            "sharing": True,   # always built into dashboard
        }

    @property
    def layout_mode(self) -> str:
        return self.LAYOUT_DASHBOARD

    @property
    def top_n(self) -> int:
        return 0
