"""
Export page -- export conversations and data to CSV/JSON.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from app.services.database import Database


class ExportWorker(QThread):
    """Background worker for exporting data."""
    progress = Signal(int, int)  # current, total
    finished = Signal(str)  # result message
    log = Signal(str)  # log messages

    def __init__(self, export_type: str, format_type: str, output_path: str):
        super().__init__()
        self._export_type = export_type
        self._format = format_type
        self._output_path = output_path

    def run(self):
        try:
            db = Database.get()

            if self._export_type == "conversations":
                self._export_table(db, "conversation",
                    "SELECT * FROM conversation ORDER BY message_count DESC")
            elif self._export_type == "contacts":
                self._export_table(db, "contact",
                    "SELECT * FROM contact ORDER BY resolved_name")
            elif self._export_type == "messages":
                self._export_messages(db)
            elif self._export_type == "calls":
                self._export_table(db, "call_record",
                    "SELECT cr.*, c.resolved_name as contact_name "
                    "FROM call_record cr LEFT JOIN contact c ON c.id = cr.contact_id "
                    "ORDER BY cr.timestamp DESC")
            elif self._export_type == "ghost_messages":
                self._export_table(db, "ghost_message",
                    "SELECT gm.*, c.resolved_name as sender_name, "
                    "conv.display_name as conv_name "
                    "FROM ghost_message gm "
                    "LEFT JOIN contact c ON c.id = gm.original_sender_id "
                    "LEFT JOIN conversation conv ON conv.id = gm.conversation_id")
            elif self._export_type == "media_metadata":
                self._export_table(db, "media",
                    "SELECT id, message_id, file_path, file_size, mime_type, "
                    "width, height, duration_ms, media_caption, media_name "
                    "FROM media ORDER BY id")
            else:
                self.finished.emit(f"Unknown export type: {self._export_type}")
                return

            self.finished.emit(f"Export complete: {self._output_path}")
        except Exception as e:
            self.finished.emit(f"Export failed: {e}")

    def _export_table(self, db: Database, name: str, sql: str):
        self.log.emit(f"Querying {name}...")
        rows = db.fetchall(sql)
        total = len(rows)
        self.log.emit(f"Found {total:,} rows")

        if self._format == "csv":
            self._write_csv(rows, total)
        else:
            self._write_json(rows, total)

    def _export_messages(self, db: Database):
        """Export messages in batches to handle large message tables."""
        total = db.scalar("SELECT COUNT(*) FROM message WHERE message_type != 7") or 0
        self.log.emit(f"Exporting {total:,} messages...")

        batch_size = 10000
        offset = 0

        if self._format == "csv":
            first_batch = True
            with open(self._output_path, "w", newline="", encoding="utf-8") as f:
                while offset < total:
                    rows = db.fetchall(
                        "SELECT m.id, m.conversation_id, m.from_me, m.text_content, "
                        "m.type_label, m.timestamp, m.is_starred, m.is_forwarded, "
                        "m.is_edited, m.is_revoked, "
                        "COALESCE(c.resolved_name, 'Unknown') as sender "
                        "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
                        "WHERE m.message_type != 7 "
                        "ORDER BY m.timestamp LIMIT ? OFFSET ?",
                        (batch_size, offset)
                    )
                    if not rows:
                        break

                    writer = csv.writer(f)
                    if first_batch:
                        writer.writerow(rows[0].keys())
                        first_batch = False
                    for row in rows:
                        writer.writerow(tuple(row))

                    offset += len(rows)
                    self.progress.emit(offset, total)
                    self.log.emit(f"  Exported {offset:,} / {total:,}")
        else:
            all_data = []
            while offset < total:
                rows = db.fetchall(
                    "SELECT m.id, m.conversation_id, m.from_me, m.text_content, "
                    "m.type_label, m.timestamp, m.is_starred, m.is_forwarded, "
                    "m.is_edited, m.is_revoked, "
                    "COALESCE(c.resolved_name, 'Unknown') as sender "
                    "FROM message m LEFT JOIN contact c ON c.id = m.sender_id "
                    "WHERE m.message_type != 7 "
                    "ORDER BY m.timestamp LIMIT ? OFFSET ?",
                    (batch_size, offset)
                )
                if not rows:
                    break
                all_data.extend(dict(row) for row in rows)
                offset += len(rows)
                self.progress.emit(offset, total)
                self.log.emit(f"  Loaded {offset:,} / {total:,}")

            self.log.emit("Writing JSON...")
            with open(self._output_path, "w", encoding="utf-8") as f:
                json.dump(all_data, f, ensure_ascii=False, indent=2, default=str)

    def _write_csv(self, rows, total):
        with open(self._output_path, "w", newline="", encoding="utf-8") as f:
            if not rows:
                return
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for i, row in enumerate(rows):
                writer.writerow(tuple(row))
                if i % 1000 == 0:
                    self.progress.emit(i, total)
        self.progress.emit(total, total)

    def _write_json(self, rows, total):
        data = [dict(row) for row in rows]
        with open(self._output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        self.progress.emit(total, total)


class ExportPage(QWidget):
    """Export data to CSV or JSON files."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title = QLabel("\u21E9  Export Data")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        subtitle = QLabel(
            "Pick a destination. <b>HTML bundle</b> is the main flow: "
            "choose conversations and get a portable browser-openable viewer. "
            "CSV / JSON are for raw forensic tables (contacts, calls, ghosts\u2026)."
        )
        subtitle.setTextFormat(Qt.RichText)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #78909c; font-size: 12px;")
        layout.addWidget(subtitle)

        # ---- Primary card: Export HTML chat bundle ------------------
        html_frame = QFrame()
        html_frame.setStyleSheet("""
            QFrame { background: rgba(0,188,212,0.06);
                     border-radius: 10px; border: 1px solid rgba(0,188,212,0.25); }
        """)
        hfl = QVBoxLayout(html_frame)
        hfl.setContentsMargins(22, 18, 22, 18)
        hfl.setSpacing(8)

        htitle = QLabel("\U0001F4AC  Export Chats to HTML bundle")
        hf = QFont(); hf.setPointSize(13); hf.setBold(True)
        htitle.setFont(hf)
        htitle.setStyleSheet("color: #00bcd4;")
        hfl.addWidget(htitle)

        hdesc = QLabel(
            "Pick conversations and get a portable ZIP with "
            "<code>index.html</code> + <code>media/</code>. Opens in any "
            "browser, works offline. Ctrl+K searches every included chat; "
            "\u2139 on any bubble reveals full forensic provenance."
        )
        hdesc.setTextFormat(Qt.RichText)
        hdesc.setWordWrap(True)
        hdesc.setStyleSheet("color: #90a4ae; font-size: 12px;")
        hfl.addWidget(hdesc)

        self._html_export_btn = QPushButton("\U0001F4C2  Choose chats \u2192 Export HTML")
        self._html_export_btn.setFixedHeight(38)
        self._html_export_btn.setMinimumWidth(260)
        self._html_export_btn.setCursor(Qt.PointingHandCursor)
        self._html_export_btn.setStyleSheet("""
            QPushButton { background: #00bcd4; color: #ffffff;
                          border: none; border-radius: 6px;
                          font-size: 13px; font-weight: bold; padding: 4px 20px; }
            QPushButton:hover { background: #0097a7; }
        """)
        self._html_export_btn.clicked.connect(self._export_html)
        hrow = QHBoxLayout()
        hrow.addWidget(self._html_export_btn)
        hrow.addStretch()
        hfl.addLayout(hrow)
        layout.addWidget(html_frame)

        # ---- Secondary card: CSV / JSON raw-table exports -----------
        raw_frame = QFrame()
        raw_frame.setStyleSheet("""
            QFrame { background: rgba(128,128,128,0.06);
                     border-radius: 10px; border: 1px solid rgba(128,128,128,0.15); }
        """)
        rfl = QVBoxLayout(raw_frame)
        rfl.setContentsMargins(22, 18, 22, 18)
        rfl.setSpacing(10)

        rtitle = QLabel("\U0001F4CA  Forensic data tables (CSV / JSON)")
        rtf = QFont(); rtf.setPointSize(13); rtf.setBold(True)
        rtitle.setFont(rtf)
        rtitle.setStyleSheet("color: #90a4ae;")
        rfl.addWidget(rtitle)

        rdesc = QLabel("For when you need raw data in a spreadsheet: single-table dump. "
                       "Chat rendering, media, reactions etc. come with the HTML bundle above.")
        rdesc.setStyleSheet("color: #78909c; font-size: 11.5px;")
        rdesc.setWordWrap(True)
        rfl.addWidget(rdesc)

        type_row = QHBoxLayout()
        type_row.setSpacing(8)
        type_row.addWidget(QLabel("Table:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems([
            "Conversations", "Contacts", "Messages (all)",
            "Calls", "Ghost Messages", "Media Metadata",
        ])
        self._type_combo.setFixedHeight(30)
        self._type_combo.setMinimumWidth(200)
        type_row.addWidget(self._type_combo)

        type_row.addSpacing(14)
        type_row.addWidget(QLabel("Format:"))
        self._format_combo = QComboBox()
        self._format_combo.addItems(["CSV", "JSON"])
        self._format_combo.setFixedHeight(30)
        self._format_combo.setMinimumWidth(100)
        type_row.addWidget(self._format_combo)
        type_row.addStretch()

        self._export_btn = QPushButton("\u21E9  Export table")
        self._export_btn.setFixedHeight(30)
        self._export_btn.setCursor(Qt.PointingHandCursor)
        self._export_btn.setStyleSheet("""
            QPushButton { background: rgba(128,128,128,0.15);
                          border: 1px solid rgba(128,128,128,0.3); border-radius: 6px;
                          color: #b0bec5; font-size: 12px; font-weight: bold;
                          padding: 2px 16px; }
            QPushButton:hover { background: rgba(128,128,128,0.25); }
        """)
        self._export_btn.clicked.connect(self._start_export)
        type_row.addWidget(self._export_btn)
        rfl.addLayout(type_row)

        layout.addWidget(raw_frame)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setFixedHeight(8)
        self._progress.setVisible(False)
        self._progress.setStyleSheet("""
            QProgressBar { background: rgba(128,128,128,0.08);
                           border-radius: 4px; border: none; }
            QProgressBar::chunk { background: #00bcd4; border-radius: 4px; }
        """)
        layout.addWidget(self._progress)

        # Log output
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Export log will appear here...")
        self._log.setStyleSheet("""
            QTextEdit { background: rgba(128,128,128,0.06);
                        border: 1px solid rgba(128,128,128,0.12);
                        border-radius: 6px; padding: 8px;
                        font-family: Consolas, monospace; font-size: 11px; }
        """)
        layout.addWidget(self._log, 1)

        self._worker = None

    def _start_export(self):
        type_map = {
            0: "conversations", 1: "contacts", 2: "messages",
            3: "calls", 4: "ghost_messages", 5: "media_metadata",
        }
        export_type = type_map[self._type_combo.currentIndex()]
        fmt = "csv" if self._format_combo.currentIndex() == 0 else "json"
        ext = ".csv" if fmt == "csv" else ".json"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export File",
            f"whatsapp_{export_type}{ext}",
            f"{'CSV Files (*.csv)' if fmt == 'csv' else 'JSON Files (*.json)'};;All Files (*)",
        )
        if not path:
            return

        self._log.clear()
        self._log.append(f"Starting export: {export_type} -> {path}")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._export_btn.setEnabled(False)

        self._worker = ExportWorker(export_type, fmt, path)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(lambda msg: self._log.append(msg))
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, current, total):
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)

    def _on_finished(self, message):
        self._log.append(f"\n{message}")
        self._export_btn.setEnabled(True)
        self._progress.setVisible(False)

    def _export_html(self):
        """Open the HTML export dialog with conversation selection."""
        from app.views.dialogs.export_html_dialog import ExportHtmlDialog
        dlg = ExportHtmlDialog(self)
        dlg.exec()
