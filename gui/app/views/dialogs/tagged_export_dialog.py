"""Customisable export dialog for tagged messages.

Three export modes:

  A. **Full conversations** — every conversation that contains at
     least one tagged message is exported in its entirety as an HTML
     viewer bundle.  Lossless context, biggest output.

  B. **Tagged messages only (+ media)** — only the tagged messages
     themselves are emitted, with their attached media files.
     Smallest output, no surrounding chat.

  C. **Tagged messages with ±N day buffer** — every conversation
     that contains tagged messages is included, but only messages
     within ±N days of any tagged message in that conversation.
     Gaps between included messages are marked with a "compaction
     marker" banner showing how many messages were dropped, so the
     analyst always sees that the export is compacted.

The dialog returns ``mode``, ``buffer_days``, ``output_dir``,
``include_media``, ``make_zip`` and ``title`` for the
``ViewerBundleExporter`` to consume.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt
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
    QSpinBox,
    QVBoxLayout,
)


_S_MODE = "TaggedExportDialog/mode"
_S_DAYS = "TaggedExportDialog/days"
_S_LAST_DIR = "TaggedExportDialog/last_dir"
_S_INC_MEDIA = "TaggedExportDialog/include_media"
_S_MAKE_ZIP = "TaggedExportDialog/make_zip"


class TaggedExportDialog(QDialog):
    """Pick mode + buffer + save location for a tagged-message export."""

    MODE_FULL = "full"
    MODE_TAGGED_ONLY = "tagged_only"
    MODE_BUFFER = "buffer"

    def __init__(self, parent, default_dir: str | os.PathLike,
                 tagged_count: int, conv_count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export tagged messages")
        self.setMinimumWidth(620)

        self._default_dir = Path(default_dir)
        self._settings = QSettings()
        self.is_ok = False

        # --- restore picks ---
        self._initial_mode = (self._settings.value(_S_MODE, self.MODE_BUFFER)
                              or self.MODE_BUFFER)
        try:
            self._initial_days = int(self._settings.value(_S_DAYS, 3) or 3)
        except (TypeError, ValueError):
            self._initial_days = 3
        try:
            self._initial_inc_media = (
                self._settings.value(_S_INC_MEDIA, "true") not in ("false", False, 0, "0")
            )
        except Exception:
            self._initial_inc_media = True
        try:
            self._initial_zip = (
                self._settings.value(_S_MAKE_ZIP, "true") not in ("false", False, 0, "0")
            )
        except Exception:
            self._initial_zip = True

        # --- UI ---
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel(
            f"<b>Export {tagged_count:,} tagged message"
            f"{'s' if tagged_count != 1 else ''}</b>"
            f"<br><span style='color:#666;font-size:11px;'>"
            f"Spanning <b>{conv_count}</b> conversation"
            f"{'s' if conv_count != 1 else ''}.  Pick how much surrounding "
            f"context to include — the export becomes a self-contained HTML "
            f"viewer (or ZIP).</span>"
        )
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        root.addWidget(title)

        # --- Mode picker ---
        mode_box = QGroupBox("Export mode")
        ml = QVBoxLayout(mode_box)
        ml.setContentsMargins(10, 12, 10, 10)
        ml.setSpacing(6)

        self._mode_group = QButtonGroup(self)
        self._mode_full = QRadioButton(
            "Full conversations  —  every chat that holds a tagged message is "
            "exported in its entirety (lossless context, largest output)"
        )
        self._mode_tagged = QRadioButton(
            "Tagged messages only  —  emit just the tagged messages themselves, "
            "plus their attached media (smallest output, no surrounding chat)"
        )
        self._mode_buffer = QRadioButton(
            "Tagged messages with ±N day buffer (recommended)  —  per chat, "
            "include the tagged messages plus everything sent within N days of "
            "each tagged message; gaps show as compaction markers"
        )
        self._mode_group.addButton(self._mode_full)
        self._mode_group.addButton(self._mode_tagged)
        self._mode_group.addButton(self._mode_buffer)

        for cb in (self._mode_full, self._mode_tagged, self._mode_buffer):
            cb.setStyleSheet("padding: 2px 0;")
            ml.addWidget(cb)

        # Default
        if self._initial_mode == self.MODE_FULL:
            self._mode_full.setChecked(True)
        elif self._initial_mode == self.MODE_TAGGED_ONLY:
            self._mode_tagged.setChecked(True)
        else:
            self._mode_buffer.setChecked(True)

        # Buffer-days control (visible only with the buffer mode)
        days_row = QHBoxLayout()
        days_row.setContentsMargins(28, 0, 0, 0)
        days_row.setSpacing(6)
        days_row.addWidget(QLabel("Buffer:"))
        self._days_spin = QSpinBox()
        self._days_spin.setRange(0, 60)
        self._days_spin.setSuffix(" day(s) before / after each tagged message")
        self._days_spin.setValue(self._initial_days)
        self._days_spin.setMinimumWidth(280)
        days_row.addWidget(self._days_spin)
        days_row.addStretch()
        self._days_widget = QFrame()
        self._days_widget.setLayout(days_row)
        ml.addWidget(self._days_widget)

        for rb in (self._mode_full, self._mode_tagged, self._mode_buffer):
            rb.toggled.connect(self._refresh_visibility)
        self._refresh_visibility()

        root.addWidget(mode_box)

        # --- Bundle options ---
        opts_box = QGroupBox("Bundle options")
        ol = QVBoxLayout(opts_box)
        ol.setContentsMargins(10, 12, 10, 10)
        ol.setSpacing(4)
        self._inc_media_cb = QCheckBox(
            "Include media files (images, video, voice, documents, …)"
        )
        self._inc_media_cb.setChecked(self._initial_inc_media)
        ol.addWidget(self._inc_media_cb)
        self._zip_cb = QCheckBox(
            "Package as a single .zip file (uncheck to keep an unpacked folder)"
        )
        self._zip_cb.setChecked(self._initial_zip)
        ol.addWidget(self._zip_cb)
        root.addWidget(opts_box)

        # --- Save location ---
        save_box = QGroupBox("Save location")
        sl = QHBoxLayout(save_box)
        sl.setContentsMargins(10, 12, 10, 10)
        self._path_edit = QLineEdit()
        last_dir = self._settings.value(_S_LAST_DIR, "") or str(self._default_dir)
        if not Path(last_dir).exists():
            last_dir = str(self._default_dir)
        self._path_edit.setText(last_dir)
        sl.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        sl.addWidget(browse_btn)
        root.addWidget(save_box)

        # --- Footer ---
        bb = QDialogButtonBox()
        gen = bb.addButton("Generate Export", QDialogButtonBox.AcceptRole)
        gen.setDefault(True)
        gen.clicked.connect(self._on_accept)
        cancel = bb.addButton(QDialogButtonBox.Cancel)
        cancel.clicked.connect(self.reject)
        root.addWidget(bb)

    # ---------------------------------------------------------------- #
    # Slots
    # ---------------------------------------------------------------- #

    def _refresh_visibility(self) -> None:
        self._days_widget.setVisible(self._mode_buffer.isChecked())

    def _on_browse(self) -> None:
        suggested = self._path_edit.text().strip() or str(self._default_dir)
        path = QFileDialog.getExistingDirectory(
            self, "Choose save folder", suggested
        )
        if path:
            self._path_edit.setText(path)

    def _on_accept(self) -> None:
        out_dir = self._path_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "Missing save location",
                                "Please pick a folder to save the export.")
            return
        out_path = Path(out_dir)
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Cannot create folder",
                                f"Could not create folder:\n  {out_path}\n\n{e}")
            return

        self._settings.setValue(_S_MODE, self.mode)
        self._settings.setValue(_S_DAYS, self.buffer_days)
        self._settings.setValue(_S_LAST_DIR, str(out_path))
        self._settings.setValue(_S_INC_MEDIA, self.include_media)
        self._settings.setValue(_S_MAKE_ZIP, self.make_zip)

        self.is_ok = True
        self.accept()

    # ---------------------------------------------------------------- #
    # Result accessors
    # ---------------------------------------------------------------- #

    @property
    def mode(self) -> str:
        if self._mode_full.isChecked():
            return self.MODE_FULL
        if self._mode_tagged.isChecked():
            return self.MODE_TAGGED_ONLY
        return self.MODE_BUFFER

    @property
    def buffer_days(self) -> int:
        return int(self._days_spin.value())

    @property
    def output_dir(self) -> Path:
        return Path(self._path_edit.text().strip())

    @property
    def include_media(self) -> bool:
        return self._inc_media_cb.isChecked()

    @property
    def make_zip(self) -> bool:
        return self._zip_cb.isChecked()
