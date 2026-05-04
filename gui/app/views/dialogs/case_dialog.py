"""
Case Dialog -- startup dialog for opening or creating a forensic case.

Shows recent cases, and provides buttons for:
  - Opening an existing .wfacase folder
  - Creating a new case + running ingestion pipeline
  - Opening a standalone analysis.db (no case folder)
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath, QIcon
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
    QWidget, QInputDialog, QStyledItemDelegate, QStyle, QSizePolicy,
)

from app.services.case_manager import CaseManager


def _themed(light: str, dark: str) -> str:
    """Return the correct color for current theme."""
    try:
        from app.services.theme_manager import ThemeManager
        return light if ThemeManager.get().is_light else dark
    except Exception:
        return light


class CaseDialog(QDialog):
    """Startup dialog for selecting or creating a forensic case."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WAInsight \u2014 WhatsApp Forensic Suite")
        self.setMinimumSize(640, 520)
        self.setModal(True)

        self._selected_db: str = ""
        self._case_path: str = ""

        # Theme colors
        bg = _themed("#f5f6f8", "#1a1d21")
        fg = _themed("#1a1d21", "#e8eaed")
        fg_sub = _themed("#5f6368", "rgba(255,255,255,0.5)")
        accent = "#00897b"
        card_bg = _themed("#ffffff", "rgba(255,255,255,0.03)")
        card_border = _themed("rgba(0,0,0,0.08)", "rgba(255,255,255,0.06)")
        btn_bg = _themed("rgba(0,137,123,0.08)", "rgba(0,188,212,0.15)")
        btn_border = _themed("#00897b", "#00bcd4")
        btn_fg = _themed("#00897b", "#00bcd4")
        btn_hover = _themed("rgba(0,137,123,0.15)", "rgba(0,188,212,0.25)")

        self.setStyleSheet(f"QDialog {{ background: {bg}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # Header
        header = QVBoxLayout()
        header.setSpacing(4)

        title = QLabel("\U0001F4F1 WAInsight")
        title.setFont(QFont("Segoe UI", 18, QFont.Bold))
        title.setStyleSheet(f"color: {fg};")
        header.addWidget(title)

        ver_label = QLabel("WhatsApp Forensic Suite for Android  \u2022  v2.2.0  \u2022  Select or create a case")
        ver_label.setStyleSheet(f"color: {fg_sub}; font-size: 12px;")
        header.addWidget(ver_label)

        layout.addLayout(header)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {card_border};")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        # ---- Action buttons (TOP, prominent) ----
        actions = QHBoxLayout()
        actions.setSpacing(10)

        btn_style = f"""
            QPushButton {{
                background: {btn_bg}; border: 1.5px solid {btn_border};
                border-radius: 8px; color: {btn_fg}; font-size: 12px;
                font-weight: bold; padding: 10px 16px;
            }}
            QPushButton:hover {{ background: {btn_hover}; }}
        """

        new_case_btn = QPushButton("\u2795  New Case + Ingest")
        new_case_btn.setFixedHeight(44)
        new_case_btn.setStyleSheet(f"""
            QPushButton {{
                background: {accent}; border: none; border-radius: 8px;
                color: white; font-size: 13px; font-weight: bold;
                padding: 10px 20px;
            }}
            QPushButton:hover {{ background: #00796b; }}
        """)
        new_case_btn.clicked.connect(self._new_case)
        actions.addWidget(new_case_btn, 1)

        open_case_btn = QPushButton("\U0001F4C2  Open Case Folder")
        open_case_btn.setFixedHeight(44)
        open_case_btn.setStyleSheet(btn_style)
        open_case_btn.clicked.connect(self._open_case)
        actions.addWidget(open_case_btn, 1)

        open_db_btn = QPushButton("\U0001F5C3  Open analysis.db")
        open_db_btn.setFixedHeight(44)
        open_db_btn.setStyleSheet(btn_style)
        open_db_btn.clicked.connect(self._open_db)
        actions.addWidget(open_db_btn, 1)

        layout.addLayout(actions)

        # ---- Recent cases ----
        recent_header = QHBoxLayout()
        recent_label = QLabel("Recent Cases")
        recent_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        recent_label.setStyleSheet(f"color: {fg};")
        recent_header.addWidget(recent_label)
        recent_header.addStretch()

        self._clear_recent_btn = QPushButton("Clear")
        self._clear_recent_btn.setFixedSize(60, 24)
        self._clear_recent_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none;
                          color: {fg_sub}; font-size: 10px; }}
            QPushButton:hover {{ color: {btn_fg}; }}
        """)
        self._clear_recent_btn.clicked.connect(self._clear_recent)
        recent_header.addWidget(self._clear_recent_btn)
        layout.addLayout(recent_header)

        self._recent_list = QListWidget()
        self._recent_list.setStyleSheet(f"""
            QListWidget {{
                background: {card_bg};
                border: 1px solid {card_border};
                border-radius: 8px;
            }}
            QListWidget::item {{
                padding: 10px 14px;
                border-bottom: 1px solid {card_border};
            }}
            QListWidget::item:last {{ border-bottom: none; }}
            QListWidget::item:selected {{
                background: {btn_bg};
            }}
            QListWidget::item:hover:!selected {{
                background: {_themed('rgba(0,0,0,0.02)', 'rgba(255,255,255,0.02)')};
            }}
        """)
        self._recent_list.setMinimumHeight(160)
        self._recent_list.setWordWrap(True)
        self._recent_list.setTextElideMode(Qt.ElideNone)
        self._recent_list.itemDoubleClicked.connect(self._open_recent)
        layout.addWidget(self._recent_list, 1)

        # Hint
        hint = QLabel("Double-click a recent case to open it, or use the buttons above.")
        hint.setStyleSheet(f"color: {fg_sub}; font-size: 10px;")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        # Load recent cases
        self._load_recent()

    def _load_recent(self):
        cm = CaseManager.get()
        recent = cm.recent_cases()

        fg = _themed("#1a1d21", "#e8eaed")
        fg_sub = _themed("#5f6368", "#adb5bd")
        green = _themed("#2e7d32", "#66bb6a")
        red = _themed("#c62828", "#ef5350")

        if not recent:
            item = QListWidgetItem("No recent cases \u2014 click 'New Case + Ingest' to get started")
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            item.setForeground(QColor(fg_sub))
            self._recent_list.addItem(item)
            self._clear_recent_btn.setVisible(False)
            return

        for case in recent:
            name = case["name"]
            case_id = case.get("case_id", "")
            examiner = case.get("examiner", "")
            created = case.get("created", "")[:10]
            has_db = case.get("has_db", False)
            path = case["path"]

            # Build rich display text
            line1 = f"\U0001F4C1 {name}"
            if case_id:
                line1 += f"  \u2022  {case_id}"

            parts = []
            if examiner:
                parts.append(f"Examiner: {examiner}")
            if created:
                parts.append(f"Created: {created}")
            parts.append(os.path.basename(path))
            line2 = "  |  ".join(parts)

            display = f"{line1}\n{line2}"

            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, path)
            item.setSizeHint(QSize(0, 58))

            if has_db:
                item.setForeground(QColor(fg))
                item.setToolTip(f"analysis.db present \u2714\n{path}")
            else:
                item.setForeground(QColor(red))
                item.setToolTip(f"analysis.db MISSING \u2718\n{path}")
                display += "  [\u26A0 no DB]"
                item.setText(display)

            self._recent_list.addItem(item)

    def _open_recent(self, item: QListWidgetItem):
        path = item.data(Qt.UserRole)
        if not path:
            return
        try:
            cm = CaseManager.get()
            db_path = cm.open_case(path)
            self._selected_db = str(db_path)
            self._case_path = path
            self.accept()
        except FileNotFoundError as e:
            # Case has no analysis.db — offer to run ingestion
            reply = QMessageBox.question(
                self, "No Database",
                f"This case has no analysis.db.\n\nWould you like to run the ingestion pipeline?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._run_ingestion_for_case(path)

    def _open_case(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Case Folder (.wfacase)"
        )
        if not folder:
            return
        try:
            cm = CaseManager.get()
            db_path = cm.open_case(folder)
            self._selected_db = str(db_path)
            self._case_path = folder
            self.accept()
        except FileNotFoundError:
            # Check if folder contains databases (user picked DB folder instead of case)
            p = Path(folder)
            has_msgstore = any("msgstore" in f.name.lower() for f in p.glob("*.db"))
            if has_msgstore:
                reply = QMessageBox.question(
                    self, "Database Folder Detected",
                    "This looks like a WhatsApp databases folder, not a case folder.\n\n"
                    "Would you like to create a new case and ingest these databases?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._new_case_with_db_path(folder)
            else:
                QMessageBox.warning(
                    self, "Invalid Folder",
                    f"No analysis.db found in:\n{folder}\n\n"
                    "Please select a .wfacase folder or use 'New Case + Ingest'.",
                )

    def _new_case(self):
        """Create new case folder, then show ingestion wizard."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Location for New Case Folder"
        )
        if not folder:
            return

        # Ask for case name
        name, ok = QInputDialog.getText(
            self, "New Case", "Case name:",
            text="WhatsApp_Case"
        )
        if not ok or not name.strip():
            return

        case_dir = str(Path(folder) / f"{name.strip()}.wfacase")
        cm = CaseManager.get()
        cm.create_case(case_dir)

        self._run_ingestion_for_case(case_dir)

    def _new_case_with_db_path(self, db_folder: str):
        """Create a case and pre-fill the ingestion wizard with the DB folder."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Location for New Case Folder"
        )
        if not folder:
            return

        name, ok = QInputDialog.getText(
            self, "New Case", "Case name:",
            text="WhatsApp_Case"
        )
        if not ok or not name.strip():
            return

        case_dir = str(Path(folder) / f"{name.strip()}.wfacase")
        cm = CaseManager.get()
        cm.create_case(case_dir)

        # Show wizard pre-filled with the DB folder
        from app.views.dialogs.ingestion_wizard import IngestionWizard
        wizard = IngestionWizard(
            parent=self,
            default_output=case_dir,
            default_db_path=db_folder,
        )
        result = wizard.exec()
        if result == QDialog.Accepted and wizard.analysis_db_path:
            self._selected_db = wizard.analysis_db_path
            self._case_path = case_dir
            cm.save_metadata(
                source_paths={"analysis_db": wizard.analysis_db_path}
            )
            self.accept()

    def _run_ingestion_for_case(self, case_dir: str):
        """Show ingestion wizard for a case folder."""
        from app.views.dialogs.ingestion_wizard import IngestionWizard
        wizard = IngestionWizard(
            parent=self,
            default_output=case_dir,
        )
        result = wizard.exec()
        if result == QDialog.Accepted and wizard.analysis_db_path:
            self._selected_db = wizard.analysis_db_path
            self._case_path = case_dir
            cm = CaseManager.get()
            cm.save_metadata(
                source_paths={"analysis_db": wizard.analysis_db_path}
            )
            self.accept()

    def _open_db(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Analysis Database",
            "", "SQLite Database (*.db);;All Files (*)",
        )
        if path:
            self._selected_db = path
            self._case_path = ""
            self.accept()

    def _clear_recent(self):
        """Clear the recent cases list."""
        from PySide6.QtCore import QSettings
        settings = QSettings()
        settings.setValue("recent_cases", [])
        self._recent_list.clear()
        item = QListWidgetItem("No recent cases \u2014 click 'New Case + Ingest' to get started")
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        self._recent_list.addItem(item)
        self._clear_recent_btn.setVisible(False)

    @property
    def selected_db(self) -> str:
        return self._selected_db

    @property
    def selected_case_path(self) -> str:
        return self._case_path
