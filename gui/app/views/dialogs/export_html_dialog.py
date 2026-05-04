"""
HTML Export Dialog — pick conversations + options for the V2 viewer bundle.

Layout (top → bottom):

    ┌──────────────────────────────────────────────────────────────┐
    │ Export chats to HTML                                          │
    │ Produces a portable, browser-openable bundle…                 │
    │──────────────────────────────────────────────────────────────│
    │ 1  Choose conversations                                       │
    │    [filter______________] [All] [None] [Only groups] …        │
    │    ☐ conversation list …                                      │
    │    N selected (Y,YYY msgs · Z.Z GB media estimated)           │
    │──────────────────────────────────────────────────────────────│
    │ 2  Bundle options                                             │
    │    ☑ Include media (…)  ☑ Package as single ZIP               │
    │──────────────────────────────────────────────────────────────│
    │ 3  Output folder                                              │
    │    [<path>] [Browse…]                                         │
    │──────────────────────────────────────────────────────────────│
    │ progress bar (hidden until export)                            │
    │                                  [Cancel]  [⇩ Export bundle]  │
    └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from app.services.database import Database


def _is_light() -> bool:
    """Best-effort theme probe that doesn't crash if ThemeManager isn't importable."""
    try:
        from app.services.theme_manager import ThemeManager
        return ThemeManager.get().is_light
    except Exception:
        return False


class ExportHtmlDialog(QDialog):
    """Select conversations and export to a V2 viewer bundle (folder-in-ZIP)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Chats to HTML")
        self.setMinimumSize(680, 620)
        self.setModal(True)

        light = _is_light()
        self._c_text = "#111b21" if light else "#e9edef"
        self._c_muted = "#667781" if light else "rgba(255,255,255,0.55)"
        self._c_panel_bg = "#ffffff" if light else "rgba(255,255,255,0.03)"
        self._c_border = "#e0e3e7" if light else "rgba(255,255,255,0.08)"
        self._c_section_hdr = "#00897b" if light else "#00bcd4"
        self._c_accent = "#00897b" if light else "#00bcd4"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)

        # ---- Title + subtitle ---------------------------------------
        title = QLabel("\u21E9 Export chats to HTML")
        tf = QFont(); tf.setPointSize(15); tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(f"color: {self._c_text};")
        layout.addWidget(title)

        subtitle = QLabel(
            "Produces a portable bundle (<code>index.html</code> + <code>media/</code>)"
            " that opens from a double-click \u2014 no server, no internet. "
            "<b>Ctrl+K</b> searches across every included chat; "
            "<b>\u2139</b> on any bubble shows full forensic provenance "
            "(msgstore <code>_id</code>, <code>key_id</code>, JID, SHA-256, "
            "case ID, examiner)."
        )
        subtitle.setWordWrap(True)
        subtitle.setTextFormat(Qt.RichText)
        subtitle.setStyleSheet(
            f"color: {self._c_muted}; font-size: 11.5px; line-height: 1.35;"
        )
        layout.addWidget(subtitle)

        # ---- Section 1 : conversations ------------------------------
        sec1 = self._build_section("1", "Choose conversations", layout)
        self._build_conv_picker(sec1)

        # ---- Section 2 : options ------------------------------------
        sec2 = self._build_section("2", "Bundle options", layout)
        opts_row = QHBoxLayout()
        opts_row.setSpacing(18)

        self._include_media_cb = QCheckBox("Include media (images / videos / voice / docs)")
        self._include_media_cb.setChecked(True)
        self._include_media_cb.setToolTip(
            "Copies every available media file into bundle/media/ with its original "
            "WhatsApp filename (IMG-YYYYMMDD-WAnnnn.jpg, DOC-…, PTT-…, etc.).\n"
            "Uncheck for a lightweight metadata-only export."
        )
        opts_row.addWidget(self._include_media_cb)

        self._make_zip_cb = QCheckBox("Package as single ZIP")
        self._make_zip_cb.setChecked(True)
        self._make_zip_cb.setToolTip("Recommended \u2014 one file to share. "
                                      "Uncheck if you want the raw folder.")
        opts_row.addWidget(self._make_zip_cb)
        opts_row.addStretch()
        sec2.addLayout(opts_row)

        # ---- Section 3 : output folder ------------------------------
        sec3 = self._build_section("3", "Output folder", layout)
        out_row = QHBoxLayout()
        out_row.setSpacing(6)
        self._output_input = QLineEdit()
        self._output_input.setFixedHeight(30)
        self._output_input.setText(self._default_output_dir())
        self._output_input.setStyleSheet(
            f"QLineEdit {{ background: {self._c_panel_bg};"
            f" color: {self._c_text};"
            f" border: 1px solid {self._c_border}; border-radius: 5px;"
            f" padding: 4px 8px; }}"
        )
        out_row.addWidget(self._output_input, 1)
        browse_btn = QPushButton("Browse\u2026")
        browse_btn.setFixedHeight(30)
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        sec3.addLayout(out_row)

        # ---- Progress bar -------------------------------------------
        self._progress = QProgressBar()
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background: {self._c_panel_bg};"
            f" border: 1px solid {self._c_border}; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {self._c_accent}; border-radius: 2px; }}"
        )
        layout.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {self._c_muted}; font-size: 11px;"
        )
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        # ---- Action buttons -----------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedSize(100, 34)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._export_btn = QPushButton("\u21E9  Export bundle")
        self._export_btn.setFixedSize(180, 34)
        self._export_btn.setCursor(Qt.PointingHandCursor)
        self._export_btn.setStyleSheet(
            f"QPushButton {{ background: {self._c_accent}; color: #fff;"
            f" border: none; border-radius: 6px;"
            f" font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: #007a6e; }}"
            f"QPushButton:disabled {{ background: rgba(128,128,128,0.25);"
            f" color: rgba(255,255,255,0.4); }}"
        )
        self._export_btn.clicked.connect(self._start_export)
        btn_row.addWidget(self._export_btn)
        layout.addLayout(btn_row)

        self._worker = None
        self._total_msgs_selected = 0
        self._load_conversations()
        self._update_selection_count()

    # ------------------------------------------------------------------
    # Section builder
    # ------------------------------------------------------------------

    def _build_section(self, num: str, title: str, parent_layout: QVBoxLayout) -> QVBoxLayout:
        """Create a titled panel and return its inner layout."""
        wrap = QFrame()
        wrap.setStyleSheet(
            f"QFrame {{ background: {self._c_panel_bg};"
            f" border: 1px solid {self._c_border}; border-radius: 8px; }}"
        )
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(14, 10, 14, 12)
        outer.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        num_lbl = QLabel(num)
        num_lbl.setFixedSize(22, 22)
        num_lbl.setAlignment(Qt.AlignCenter)
        num_lbl.setStyleSheet(
            f"background: {self._c_accent}; color: #fff; border-radius: 11px;"
            f" font-size: 11px; font-weight: 700;"
        )
        hdr.addWidget(num_lbl)
        tlab = QLabel(title)
        hf = QFont(); hf.setPointSize(11); hf.setBold(True)
        tlab.setFont(hf)
        tlab.setStyleSheet(f"color: {self._c_section_hdr};")
        hdr.addWidget(tlab)
        hdr.addStretch()
        outer.addLayout(hdr)

        parent_layout.addWidget(wrap)
        return outer

    # ------------------------------------------------------------------
    # Conversation picker
    # ------------------------------------------------------------------

    def _build_conv_picker(self, outer: QVBoxLayout) -> None:
        # Top row: filter + quick toggles
        top = QHBoxLayout()
        top.setSpacing(6)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name\u2026")
        self._search.setFixedHeight(28)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_list)
        top.addWidget(self._search, 1)

        def _quick_btn(label, tooltip, cb):
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tooltip)
            b.clicked.connect(cb)
            return b

        top.addWidget(_quick_btn("All", "Select every visible conversation",
                                  self._select_all))
        top.addWidget(_quick_btn("None", "Clear all checkboxes",
                                  self._select_none))
        top.addWidget(_quick_btn("Groups", "Select only group chats that are currently visible",
                                  lambda: self._select_by_type("group")))
        top.addWidget(_quick_btn("Personal", "Select only 1:1 chats that are currently visible",
                                  lambda: self._select_by_type("personal")))
        outer.addLayout(top)

        # List
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background: {self._c_panel_bg};"
            f" border: 1px solid {self._c_border}; border-radius: 5px; }}"
            f"QListWidget::item {{ padding: 5px 8px;"
            f" border-bottom: 1px solid {self._c_border}; }}"
            f"QListWidget::item:selected {{ background: transparent;"
            f" color: {self._c_text}; }}"
        )
        outer.addWidget(self._list, 1)

        # Footer — selection count + estimated payload
        self._selection_label = QLabel("0 conversations selected")
        self._selection_label.setStyleSheet(
            f"color: {self._c_muted}; font-size: 11px;"
        )
        outer.addWidget(self._selection_label)

    def _load_conversations(self):
        db = Database.get()
        rows = db.fetchall(
            "SELECT id, COALESCE(display_name, jid_raw_string, '#'||id) AS title, "
            "       chat_type, message_count, last_message_ts "
            "FROM conversation "
            "WHERE message_count > 0 "
            "ORDER BY last_message_ts DESC, message_count DESC"
        )
        self._conv_data: list[dict] = []
        for row in rows:
            conv_id = row[0]
            name = row[1] or f"#{conv_id}"
            chat_type = (row[2] or "personal").lower()
            msg_count = row[3] or 0
            self._conv_data.append({
                "id": conv_id, "name": name,
                "chat_type": chat_type, "msg_count": msg_count,
            })
            type_badge = {
                "group": "[GROUP]", "personal": "[1:1]",
                "community": "[COMMUNITY]", "channel": "[CHANNEL]",
                "broadcast": "[BROADCAST]", "newsletter": "[NEWSLETTER]",
                "status": "[STATUS]",
            }.get(chat_type, f"[{chat_type.upper()}]")
            item = QListWidgetItem(f"{type_badge}  {name}   \u00B7  {msg_count:,} msgs")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, conv_id)
            item.setData(Qt.UserRole + 1, chat_type)
            item.setData(Qt.UserRole + 2, msg_count)
            self._list.addItem(item)
        self._list.itemChanged.connect(self._update_selection_count)

    def _filter_list(self, text: str):
        text = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(text not in item.text().lower())

    def _select_all(self):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.Checked)

    def _select_none(self):
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.Unchecked)

    def _select_by_type(self, ctype: str):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.isHidden():
                continue
            if (item.data(Qt.UserRole + 1) or "") == ctype:
                item.setCheckState(Qt.Checked)

    def _update_selection_count(self):
        count = 0
        total_msgs = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                count += 1
                total_msgs += item.data(Qt.UserRole + 2) or 0
        self._total_msgs_selected = total_msgs
        summary = f"{count} conversation{'s' if count != 1 else ''} selected"
        if count:
            summary += f"  \u00B7  {total_msgs:,} messages"
            if total_msgs > 20000:
                summary += "  \u00B7  heavy export \u2014 may take a minute"
        self._selection_label.setText(summary)
        self._export_btn.setEnabled(count > 0)

    # ------------------------------------------------------------------
    # Output + export
    # ------------------------------------------------------------------

    def _default_output_dir(self) -> str:
        """Prefer a sibling `exports/` next to the analysis.db, else the repo's
        backend/output/html_exports, else the user's Documents folder."""
        try:
            db = Database.get()
            case_dir = Path(db.path).parent
            if case_dir.is_dir():
                out = case_dir / "exports"
                out.mkdir(parents=True, exist_ok=True)
                return str(out)
        except Exception:
            pass
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / "backend" / "output" / "html_exports"
            if cand.parent.is_dir():
                cand.mkdir(parents=True, exist_ok=True)
                return str(cand)
        return str(Path.home() / "Documents")

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder",
            self._output_input.text() or self._default_output_dir(),
        )
        if folder:
            self._output_input.setText(folder)

    def _start_export(self):
        selected_ids = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                selected_ids.append(item.data(Qt.UserRole))

        if not selected_ids:
            QMessageBox.warning(self, "No Selection",
                                "Please select at least one conversation.")
            return

        output_dir = self._output_input.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "Output folder missing",
                                "Please specify an output folder.")
            return
        os.makedirs(output_dir, exist_ok=True)

        # Disable inputs + show progress
        self._export_btn.setEnabled(False)
        self._export_btn.setText("Exporting\u2026")
        self._progress.setRange(0, len(selected_ids))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._status_label.setText("Preparing\u2026")
        self._status_label.setVisible(True)

        db = Database.get()
        ViewerBundleExporter = self._load_bundle_exporter_cls()
        case_info = self._load_case_info(db)
        self._worker = ViewerBundleExporter(
            conversation_ids=selected_ids,
            db_path=str(db.path),
            output_dir=output_dir,
            include_media=self._include_media_cb.isChecked(),
            make_zip=self._make_zip_cb.isChecked(),
            title=case_info.get("case_id") or "WhatsApp Export",
            case_info=case_info,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @staticmethod
    def _load_bundle_exporter_cls():
        """Load ViewerBundleExporter from backend/ via importlib."""
        import importlib.util
        from pathlib import Path as _P
        here = _P(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "backend" / "app" / "export" / "viewer_bundle_exporter.py"
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location(
                    "wainsight_viewer_bundle_exporter", str(candidate))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.ViewerBundleExporter
        raise FileNotFoundError(
            "Could not locate backend/app/export/viewer_bundle_exporter.py")

    def _load_case_info(self, db) -> dict:
        info: dict = {}
        try:
            rows = db.fetchall(
                "SELECT key, value FROM case_metadata WHERE key IN "
                "('case_id','examiner','notes','analysis_db_sha256',"
                " 'source_msgstore_sha256','source_msgstore_path')"
            )
            for row in rows:
                info[row[0]] = row[1]
        except Exception:
            pass
        info["analysis_db"] = str(db.path)
        return info

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------

    def _on_progress(self, current: int, total: int, stage: str):
        self._progress.setMaximum(max(total, 1))
        self._progress.setValue(current)
        self._status_label.setText(stage or f"Exporting conversation {current}/{total}")

    def _on_finished(self, output_path: str, error_msg: str):
        self._progress.setVisible(False)
        self._status_label.setVisible(False)
        self._export_btn.setEnabled(True)
        self._export_btn.setText("\u21E9  Export bundle")

        if error_msg or not output_path:
            QMessageBox.warning(self, "Export failed",
                                error_msg or "Unknown error during export.")
            return

        QMessageBox.information(
            self, "Export complete",
            f"Bundle written to:\n\n{output_path}\n\n"
            "Unzip (if zipped) and double-click index.html to browse."
        )
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(output_path).parent)))
        self.accept()
