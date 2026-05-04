"""
Ingestion Wizard -- WhatsApp data ingestion with real-time progress.

Flow:
  1. User picks the extracted databases folder OR individual DB files
  2. Sets media folder, case metadata
  3. Clicks Start → 26-stage pipeline runs as subprocess
  4. Real-time progress bar, dynamic ETA, per-stage row counts
  5. On completion → opens the main GUI with the new analysis.db
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)


def _themed(light: str, dark: str) -> str:
    try:
        from app.services.theme_manager import ThemeManager
        return light if ThemeManager.get().is_light else dark
    except Exception:
        return light


# Total stages emitting progress events from the backend pipeline.
# Must match the count of progress.complete_stage() calls in orchestrator.py.
TOTAL_STAGES = 27

# Database files we know about
DB_FILES = [
    ("msgstore.db",          "Messages, chats, media (required)",  True),
    ("wa.db",                "Contacts (optional)",                 False),
    ("axolotl.db",           "Encryption keys (optional)",         False),
    ("location.db",          "Live location route points (optional)", False),
]

# Shared preferences files
PREFS_FILES = [
    ("startup_prefs.xml",    "Owner phone & push name (optional)", False),
]


class IngestionWizard(QDialog):
    """Wizard for ingesting WhatsApp data into analysis.db."""

    def __init__(self, parent=None, default_output: str = "",
                 default_db_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("WAInsight \u2014 Data Ingestion")
        self.setModal(True)

        # Size to 80% of screen, scrollable content
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            w = min(int(avail.width() * 0.8), 1000)
            h = min(int(avail.height() * 0.85), 800)
            self.resize(w, h)
            self.move(avail.x() + (avail.width() - w) // 2,
                      avail.y() + (avail.height() - h) // 2)
        else:
            self.resize(800, 600)

        self._analysis_db_path: str = ""
        self._staging_dir: str = ""   # temp dir if files from mixed paths

        # Timing
        self._start_time: float = 0.0
        self._stage_count: int = 0

        # Theme
        fg = _themed("#1a1d21", "#e8eaed")
        fg_sub = _themed("#5f6368", "#9aa0a6")
        accent = _themed("#00897b", "#00bcd4")
        card_bg = _themed("#ffffff", "rgba(255,255,255,0.03)")
        card_border = _themed("rgba(0,0,0,0.08)", "rgba(255,255,255,0.06)")
        bg = _themed("#f5f6f8", "#1a1d21")
        input_bg = _themed("#ffffff", "rgba(255,255,255,0.05)")
        input_border = _themed("rgba(0,0,0,0.12)", "rgba(255,255,255,0.1)")
        green = _themed("#2e7d32", "#66bb6a")
        red = _themed("#c62828", "#ef5350")

        self.setStyleSheet(f"QDialog {{ background: {bg}; }}")

        # Wrap everything in a scroll area for small screens
        from PySide6.QtWidgets import QScrollArea
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.NoFrame)
        _scroll_widget = QWidget()
        layout = QVBoxLayout(_scroll_widget)
        _scroll.setWidget(_scroll_widget)
        _outer.addWidget(_scroll)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)

        # ── Title ──
        title = QLabel("Data Ingestion Wizard")
        title.setFont(QFont("Segoe UI", 15, QFont.Bold))
        title.setStyleSheet(f"color: {fg};")
        layout.addWidget(title)

        subtitle = QLabel(
            "Import WhatsApp databases into a normalized analysis.db. "
            "Runtime scales with message and media volume — "
            "tens of minutes for a typical phone backup on an SSD."
        )
        subtitle.setStyleSheet(f"color: {fg_sub}; font-size: 11px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Common styles
        self._input_style = f"""
            QLineEdit {{
                background: {input_bg}; border: 1px solid {input_border};
                border-radius: 5px; padding: 4px 8px; color: {fg};
                font-size: 10px;
            }}
            QLineEdit:focus {{ border-color: {accent}; }}
        """
        self._browse_style = f"""
            QPushButton {{
                background: {_themed('rgba(0,137,123,0.08)', 'rgba(0,188,212,0.12)')};
                border: 1px solid {accent}; border-radius: 5px;
                color: {accent}; font-size: 10px; font-weight: bold;
                padding: 3px 10px;
            }}
            QPushButton:hover {{
                background: {_themed('rgba(0,137,123,0.15)', 'rgba(0,188,212,0.2)')};
            }}
        """
        label_style = f"color: {fg}; font-size: 10px; font-weight: bold;"
        self._label_style = label_style
        self._fg = fg
        self._fg_sub = fg_sub
        self._accent = accent
        self._green = green
        self._red = red
        self._card_bg = card_bg
        self._card_border = card_border

        # ════════════════════════════════════════════════
        # STEP 1 — Database Source
        # ════════════════════════════════════════════════
        layout.addWidget(self._section("Step 1 \u2014 WhatsApp Database Files"))

        # Source mode selection (top, clear choice)
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        mode_row = QHBoxLayout()
        mode_row.setSpacing(16)
        self._mode_group = QButtonGroup(self)
        self._mode_normal = QRadioButton("\U0001F4C2 Extracted Databases (msgstore.db)")
        self._mode_encrypted = QRadioButton("\U0001F512 Encrypted Backup (.crypt14)")
        self._mode_normal.setChecked(True)
        self._mode_normal.setStyleSheet(f"color: {fg}; font-size: 11px; font-weight: bold;")
        self._mode_encrypted.setStyleSheet(f"color: {fg}; font-size: 11px; font-weight: bold;")
        self._mode_group.addButton(self._mode_normal, 0)
        self._mode_group.addButton(self._mode_encrypted, 1)
        self._mode_group.idToggled.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_normal)
        mode_row.addWidget(self._mode_encrypted)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # ── Normal mode: databases folder ──
        self._normal_frame = QFrame()
        normal_layout = QVBoxLayout(self._normal_frame)
        normal_layout.setContentsMargins(0, 0, 0, 0)
        normal_layout.setSpacing(4)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        folder_hint = QLabel("Select the databases folder to auto-detect all files")
        folder_hint.setStyleSheet(f"color: {fg_sub}; font-size: 10px;")
        folder_row.addWidget(folder_hint)
        folder_row.addStretch()
        self._browse_folder_btn = QPushButton("Browse Databases Folder")
        self._browse_folder_btn.setFixedHeight(28)
        self._browse_folder_btn.setStyleSheet(self._browse_style)
        self._browse_folder_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self._browse_folder_btn)
        normal_layout.addLayout(folder_row)
        layout.addWidget(self._normal_frame)

        # ── Encrypted mode: crypt file + key file (hidden by default) ──
        self._encrypted_frame = QFrame()
        self._encrypted_frame.setVisible(False)
        enc_layout = QVBoxLayout(self._encrypted_frame)
        enc_layout.setContentsMargins(0, 0, 0, 0)
        enc_layout.setSpacing(6)

        # Crypt file picker
        crypt_row = QHBoxLayout()
        crypt_lbl = QLabel("Backup File:")
        crypt_lbl.setStyleSheet(f"color: {fg}; font-size: 10px; font-weight: bold;")
        crypt_lbl.setFixedWidth(90)
        crypt_row.addWidget(crypt_lbl)
        self._crypt_input = QLineEdit()
        self._crypt_input.setPlaceholderText("Select .crypt14 backup file")
        self._crypt_input.setFixedHeight(28)
        self._crypt_input.setStyleSheet(self._input_style)
        self._crypt_input.textChanged.connect(self._update_run_btn)
        crypt_row.addWidget(self._crypt_input, 1)
        crypt_browse = QPushButton("Browse")
        crypt_browse.setFixedHeight(28)
        crypt_browse.setStyleSheet(self._browse_style)
        crypt_browse.clicked.connect(self._browse_crypt_file)
        crypt_row.addWidget(crypt_browse)
        enc_layout.addLayout(crypt_row)

        # Key file picker
        key_row = QHBoxLayout()
        key_lbl = QLabel("Key File:")
        key_lbl.setStyleSheet(f"color: {fg}; font-size: 10px; font-weight: bold;")
        key_lbl.setFixedWidth(90)
        key_row.addWidget(key_lbl)
        self._crypt_key_input = QLineEdit()
        self._crypt_key_input.setPlaceholderText("Select the 'key' file from /data/data/com.whatsapp/files/")
        self._crypt_key_input.setFixedHeight(28)
        self._crypt_key_input.setStyleSheet(self._input_style)
        key_row.addWidget(self._crypt_key_input, 1)
        key_browse = QPushButton("Browse")
        key_browse.setFixedHeight(28)
        key_browse.setStyleSheet(self._browse_style)
        key_browse.clicked.connect(self._browse_crypt_key)
        key_row.addWidget(key_browse)
        enc_layout.addLayout(key_row)

        enc_note = QLabel(
            "Decrypted database saved in case folder. Requires: pip install wa-crypt-tools"
        )
        enc_note.setStyleSheet(f"color: {fg_sub}; font-size: 9px;")
        enc_layout.addWidget(enc_note)

        layout.addWidget(self._encrypted_frame)

        # ── Normal mode DB cards (inside normal_frame) ──
        # Individual DB file rows in a card
        db_card = QFrame()
        db_card.setStyleSheet(
            f"QFrame {{ background: {card_bg}; border: 1px solid {card_border}; "
            f"border-radius: 6px; }}"
        )
        db_grid = QGridLayout(db_card)
        db_grid.setContentsMargins(10, 8, 10, 8)
        db_grid.setSpacing(4)
        db_grid.setColumnStretch(1, 1)

        self._db_inputs: dict[str, QLineEdit] = {}
        self._db_status: dict[str, QLabel] = {}
        self._db_row_widgets: dict[str, list[QWidget]] = {}

        for row_idx, (name, desc, required) in enumerate(DB_FILES):
            # Name label
            req_mark = " *" if required else ""
            name_lbl = QLabel(f"{name}{req_mark}")
            name_lbl.setStyleSheet(
                f"color: {fg}; font-size: 10px; font-weight: bold; "
                f"border: none; padding: 0;"
            )
            name_lbl.setFixedWidth(130)
            db_grid.addWidget(name_lbl, row_idx, 0)
            row_widgets: list[QWidget] = [name_lbl]

            # Path input
            inp = QLineEdit()
            inp.setPlaceholderText(f"{desc}" + (" (required)" if required else " (optional)"))
            inp.setFixedHeight(26)
            inp.setStyleSheet(
                f"QLineEdit {{ background: {input_bg}; border: 1px solid {input_border}; "
                f"border-radius: 4px; padding: 2px 6px; color: {fg}; font-size: 10px; }}"
            )
            inp.textChanged.connect(lambda text, n=name: self._update_db_status(n))
            if name == "wa.db":
                inp.editingFinished.connect(self._detect_wa_siblings)
            db_grid.addWidget(inp, row_idx, 1)
            self._db_inputs[name] = inp
            row_widgets.append(inp)

            # Status icon
            status = QLabel("\u2014")
            status.setStyleSheet(f"color: {fg_sub}; font-size: 10px; border: none;")
            status.setFixedWidth(16)
            db_grid.addWidget(status, row_idx, 2)
            self._db_status[name] = status
            row_widgets.append(status)

            # Browse button
            btn = QPushButton("Browse")
            btn.setFixedSize(80, 26)
            btn.setToolTip(f"Browse for {name}")
            btn.setStyleSheet(
                f"QPushButton {{ background: {_themed('#e0f2f1', '#1a3a4a')}; "
                f"border: 1px solid {_themed('#00897b', '#00bcd4')}; border-radius: 4px; "
                f"color: {_themed('#00897b', '#00bcd4')}; font-weight: bold; font-size: 10px; }}"
                f"QPushButton:hover {{ background: {_themed('#b2dfdb', '#0d3d4d')}; }}"
            )
            btn.clicked.connect(lambda checked, n=name: self._browse_single_db(n))
            db_grid.addWidget(btn, row_idx, 3)
            row_widgets.append(btn)

            clear_btn = QPushButton("\u2715 Clear")
            clear_btn.setFixedSize(60, 26)
            clear_btn.setToolTip(f"Clear {name}")
            clear_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; "
                f"border: 1px solid {input_border}; border-radius: 4px; "
                f"color: {fg_sub}; font-weight: bold; font-size: 10px; }}"
                f"QPushButton:hover {{ color: {fg}; border-color: {accent}; }}"
            )
            clear_btn.clicked.connect(lambda checked, n=name: self._clear_db(n))
            db_grid.addWidget(clear_btn, row_idx, 4)
            row_widgets.append(clear_btn)
            self._db_row_widgets[name] = row_widgets

        layout.addWidget(db_card)

        # Shared Preferences section
        prefs_card = QFrame()
        prefs_card.setStyleSheet(
            f"QFrame {{ background: {card_bg}; border: 1px solid {card_border}; "
            f"border-radius: 6px; }}"
        )
        prefs_grid = QGridLayout(prefs_card)
        prefs_grid.setContentsMargins(10, 8, 10, 8)
        prefs_grid.setSpacing(4)
        prefs_grid.setColumnStretch(1, 1)

        prefs_header = QLabel("Shared Preferences")
        prefs_header.setStyleSheet(f"color: {fg}; font-size: 10px; font-weight: bold; border: none;")
        prefs_grid.addWidget(prefs_header, 0, 0, 1, 5)

        self._prefs_inputs: dict[str, QLineEdit] = {}
        self._prefs_status: dict[str, QLabel] = {}

        for row_idx, (name, desc, required) in enumerate(PREFS_FILES):
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(
                f"color: {fg}; font-size: 10px; font-weight: bold; border: none; padding: 0;"
            )
            name_lbl.setFixedWidth(130)
            prefs_grid.addWidget(name_lbl, row_idx + 1, 0)

            inp = QLineEdit()
            inp.setPlaceholderText(desc)
            inp.setFixedHeight(26)
            inp.setStyleSheet(
                f"QLineEdit {{ background: {input_bg}; border: 1px solid {input_border}; "
                f"border-radius: 4px; padding: 2px 6px; color: {fg}; font-size: 10px; }}"
            )
            inp.textChanged.connect(lambda text, n=name: self._update_pref_status(n))
            prefs_grid.addWidget(inp, row_idx + 1, 1)
            self._prefs_inputs[name] = inp

            status = QLabel("\u2014")
            status.setStyleSheet(f"color: {fg_sub}; font-size: 10px; border: none;")
            status.setFixedWidth(16)
            prefs_grid.addWidget(status, row_idx + 1, 2)
            self._prefs_status[name] = status

            btn = QPushButton("Browse")
            btn.setFixedSize(80, 26)
            btn.setToolTip(f"Browse for {name}")
            btn.setStyleSheet(
                f"QPushButton {{ background: {_themed('#e0f2f1', '#1a3a4a')}; "
                f"border: 1px solid {_themed('#00897b', '#00bcd4')}; border-radius: 4px; "
                f"color: {_themed('#00897b', '#00bcd4')}; font-weight: bold; font-size: 10px; }}"
                f"QPushButton:hover {{ background: {_themed('#b2dfdb', '#0d3d4d')}; }}"
            )
            btn.clicked.connect(lambda checked, n=name: self._browse_single_pref(n))
            prefs_grid.addWidget(btn, row_idx + 1, 3)

            clear_btn = QPushButton("\u2715 Clear")
            clear_btn.setFixedSize(60, 26)
            clear_btn.setToolTip(f"Clear {name}")
            clear_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; "
                f"border: 1px solid {input_border}; border-radius: 4px; "
                f"color: {fg_sub}; font-weight: bold; font-size: 10px; }}"
                f"QPushButton:hover {{ color: {fg}; border-color: {accent}; }}"
            )
            clear_btn.clicked.connect(lambda checked, n=name: self._clear_pref(n))
            prefs_grid.addWidget(clear_btn, row_idx + 1, 4)

        layout.addWidget(prefs_card)

        # ════════════════════════════════════════════════
        # STEP 2 — Media & Output
        # ════════════════════════════════════════════════
        layout.addWidget(self._section("Step 2 \u2014 Media & Output"))

        # Media path
        media_row = QHBoxLayout()
        media_row.setSpacing(6)
        mlbl = QLabel("Media:")
        mlbl.setStyleSheet(label_style)
        mlbl.setFixedWidth(50)
        media_row.addWidget(mlbl)
        self._media_input = QLineEdit()
        self._media_input.setPlaceholderText(
            "WhatsApp media root — contains WhatsApp Images/, Video/, Audio/ etc."
        )
        self._media_input.setFixedHeight(28)
        self._media_input.setStyleSheet(self._input_style)
        media_row.addWidget(self._media_input, 1)
        self._media_browse = QPushButton("Browse")
        self._media_browse.setFixedHeight(28)
        self._media_browse.setStyleSheet(self._browse_style)
        self._media_browse.clicked.connect(self._browse_media)
        media_row.addWidget(self._media_browse)
        layout.addLayout(media_row)

        # Avatars path
        avatar_row = QHBoxLayout()
        avatar_row.setSpacing(6)
        avlbl = QLabel("Avatars:")
        avlbl.setStyleSheet(label_style)
        avlbl.setFixedWidth(50)
        avatar_row.addWidget(avlbl)
        self._avatars_input = QLineEdit()
        self._avatars_input.setPlaceholderText(
            "Optional — /data/data/com.whatsapp/files/Avatars/"
        )
        self._avatars_input.setFixedHeight(28)
        self._avatars_input.setStyleSheet(self._input_style)
        avatar_row.addWidget(self._avatars_input, 1)
        self._avatars_browse = QPushButton("Browse")
        self._avatars_browse.setFixedHeight(28)
        self._avatars_browse.setStyleSheet(self._browse_style)
        self._avatars_browse.clicked.connect(self._browse_avatars)
        avatar_row.addWidget(self._avatars_browse)
        layout.addLayout(avatar_row)

        # Output path
        out_row = QHBoxLayout()
        out_row.setSpacing(6)
        olbl = QLabel("Output:")
        olbl.setStyleSheet(label_style)
        olbl.setFixedWidth(50)
        out_row.addWidget(olbl)
        self._output_input = QLineEdit()
        if default_output:
            self._output_input.setText(default_output)
        else:
            self._output_input.setText(
                str(Path(__file__).resolve().parent.parent.parent.parent
                    / "backend" / "output")
            )
        self._output_input.setFixedHeight(28)
        self._output_input.setStyleSheet(self._input_style)
        out_row.addWidget(self._output_input, 1)
        self._output_browse = QPushButton("Browse")
        self._output_browse.setFixedHeight(28)
        self._output_browse.setStyleSheet(self._browse_style)
        self._output_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self._output_browse)
        layout.addLayout(out_row)

        # Case metadata
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        cidlbl = QLabel("Case ID:")
        cidlbl.setStyleSheet(label_style)
        meta_row.addWidget(cidlbl)
        self._case_id_input = QLineEdit()
        self._case_id_input.setPlaceholderText("Optional")
        self._case_id_input.setFixedHeight(28)
        self._case_id_input.setStyleSheet(self._input_style)
        meta_row.addWidget(self._case_id_input)
        exlbl = QLabel("Examiner:")
        exlbl.setStyleSheet(label_style)
        meta_row.addWidget(exlbl)
        self._examiner_input = QLineEdit()
        self._examiner_input.setPlaceholderText("Your name")
        self._examiner_input.setFixedHeight(28)
        self._examiner_input.setStyleSheet(self._input_style)
        meta_row.addWidget(self._examiner_input)
        layout.addLayout(meta_row)

        # ════════════════════════════════════════════════
        # STEP 3 — Run
        # ════════════════════════════════════════════════
        layout.addWidget(self._section("Step 3 \u2014 Run Pipeline"))

        run_row = QHBoxLayout()
        run_row.setSpacing(12)
        self._run_btn = QPushButton("\u25B6  Start Ingestion")
        self._run_btn.setFixedHeight(40)
        self._run_btn.setFixedWidth(200)
        self._run_btn.setEnabled(False)
        self._run_btn.setStyleSheet(f"""
            QPushButton {{
                background: {accent}; border: none; border-radius: 8px;
                color: white; font-size: 13px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {_themed('#00796b', '#0097a7')}; }}
            QPushButton:disabled {{
                background: {_themed('rgba(0,0,0,0.06)', 'rgba(255,255,255,0.05)')};
                color: {_themed('rgba(0,0,0,0.25)', 'rgba(255,255,255,0.3)')};
            }}
        """)
        self._run_btn.clicked.connect(self._start_ingestion)
        run_row.addWidget(self._run_btn)

        status_col = QVBoxLayout()
        status_col.setSpacing(1)
        self._stage_label = QLabel("")
        self._stage_label.setStyleSheet(
            f"color: {accent}; font-size: 11px; font-weight: bold;"
        )
        status_col.addWidget(self._stage_label)
        self._eta_label = QLabel("")
        self._eta_label.setStyleSheet(f"color: {fg_sub}; font-size: 10px;")
        status_col.addWidget(self._eta_label)
        run_row.addLayout(status_col)
        run_row.addStretch()
        layout.addLayout(run_row)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setFixedHeight(14)
        self._progress.setRange(0, TOTAL_STAGES)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%v / %m stages")
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background: {_themed('rgba(0,0,0,0.04)', 'rgba(255,255,255,0.04)')};
                border: none; border-radius: 7px;
                font-size: 10px; color: {fg_sub}; text-align: center;
            }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 7px; }}
        """)
        layout.addWidget(self._progress)

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Pipeline log appears here after you click Start...")
        self._log.setStyleSheet(f"""
            QTextEdit {{
                background: {card_bg}; border: 1px solid {card_border};
                border-radius: 6px; padding: 6px;
                font-family: Consolas, "Courier New", monospace;
                font-size: 10px; color: {fg};
            }}
        """)
        layout.addWidget(self._log, 1)

        self._worker = None

        # Auto-detect pre-filled path
        if default_db_path:
            self._detect_folder(default_db_path)

    # ── Helpers ──

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        lbl.setStyleSheet(f"color: {self._fg}; margin-top: 4px;")
        return lbl

    def _update_run_btn(self):
        """Enable Start based on current mode."""
        if self._mode_encrypted.isChecked():
            has_crypt = bool(self._crypt_input.text().strip())
            self._run_btn.setEnabled(has_crypt)
        else:
            has_msgstore = bool(self._db_inputs["msgstore.db"].text())
            self._run_btn.setEnabled(has_msgstore)

    def _set_status(self, status: QLabel, ok: bool, optional: bool = True):
        status.setText("\u2713" if ok else ("\u2014" if optional else "\u2718"))
        status.setStyleSheet(
            f"color: {self._green if ok else (self._fg_sub if optional else self._red)}; "
            f"font-size: 10px; border: none;"
        )
        status.setToolTip("")

    def _update_db_status(self, db_name: str):
        val = self._db_inputs[db_name].text().strip()
        required = next((req for name, _, req in DB_FILES if name == db_name), False)
        optional = not required or (self._mode_encrypted.isChecked() and db_name == "msgstore.db")
        valid = bool(val) and Path(val).is_file()
        self._set_status(self._db_status[db_name], valid, optional=optional and not val)
        self._update_run_btn()

    def _update_pref_status(self, pref_name: str):
        val = self._prefs_inputs[pref_name].text().strip()
        valid = bool(val) and Path(val).is_file()
        self._set_status(self._prefs_status[pref_name], valid, optional=not val)

    def _clear_db(self, db_name: str):
        self._db_inputs[db_name].clear()
        self._update_db_status(db_name)

    def _clear_pref(self, pref_name: str):
        self._prefs_inputs[pref_name].clear()
        self._update_pref_status(pref_name)

    # ── Browse handlers ──

    def _detect_wa_siblings(self):
        """In encrypted mode, use wa.db's folder to fill companion DBs."""
        if not self._mode_encrypted.isChecked():
            return
        wa_path = self._db_inputs["wa.db"].text().strip()
        if wa_path and Path(wa_path).is_file():
            self._detect_folder(str(Path(wa_path).parent))

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select WhatsApp Databases Folder"
        )
        if folder:
            self._detect_folder(folder)

    def _browse_single_db(self, db_name: str):
        """Browse for an individual database file."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {db_name}",
            "",
            f"SQLite Database (*.db);;All Files (*)",
        )
        if not path:
            return
        self._db_inputs[db_name].setText(path)
        self._set_status(self._db_status[db_name], True)
        if self._mode_encrypted.isChecked() and db_name == "wa.db":
            self._detect_folder(str(Path(path).parent))
        self._update_run_btn()

    def _browse_single_pref(self, pref_name: str):
        """Browse for a shared_prefs XML file."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {pref_name}",
            "",
            f"XML Files (*.xml);;All Files (*)",
        )
        if not path:
            return
        self._prefs_inputs[pref_name].setText(path)
        self._set_status(self._prefs_status[pref_name], True)

    def _on_decrypt_finished(self, success: bool, message: str):
        """Called when background decryption completes."""
        if not success:
            self._log.append(f"\u2718 DECRYPTION FAILED: {message}")
            self._run_btn.setEnabled(True)
            self._run_btn.setText("\u25B6  Start Ingestion")
            QMessageBox.critical(
                self, "Decryption Failed",
                f"Failed to decrypt:\n\n{message}\n\n"
                f"Ensure the key file is correct.\n"
                f"Install: pip install wa-crypt-tools"
            )
            return

        decrypted_db = message
        size_mb = os.path.getsize(decrypted_db) / (1024 * 1024)
        self._log.append(f"\u2713 Decrypted: {size_mb:.1f} MB")
        self._log.append(f"  Saved: {decrypted_db}")

        # No copy needed — already decrypted as msgstore.db

        for db_name, db_path in self._pending_extra_db.items():
            self._log.append(f"  + {db_name}: {db_path}")

        self._log.append(f"\n\u25B6 Starting ingestion pipeline...\n")

        # Now start the pipeline
        output = self._pending_output
        media = self._pending_media
        extra_db_paths = self._pending_extra_db
        db_path = output  # case folder where decrypted msgstore.db lives

        self._run_btn.setText("\u23F3  Running...")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._stage_count = 0
        self._start_time = time.time()
        self._last_stage_time = self._start_time
        self._stage_durations = []
        self._stage_label.setText("Starting pipeline...")
        self._eta_label.setText("")

        avatars = self._avatars_input.text().strip() or None

        from app.services.ingestion_worker import IngestionWorker
        self._worker = IngestionWorker(
            databases_path=db_path,
            output_path=output,
            media_path=media,
            avatars_path=avatars,
            case_id=self._case_id_input.text().strip(),
            examiner=self._examiner_input.text().strip(),
            extra_db_paths=extra_db_paths if extra_db_paths else None,
            prefs_dir=getattr(self, "_pending_prefs_dir", None),
        )
        self._worker.progress_text.connect(self._on_progress_text)
        self._worker.stage_finished.connect(self._on_stage_finished)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_mode_changed(self, button_id: int, checked: bool):
        """Toggle between normal DB and encrypted backup mode."""
        if not checked:
            return
        encrypted = (button_id == 1)
        self._normal_frame.setVisible(not encrypted)
        self._encrypted_frame.setVisible(encrypted)
        for widget in self._db_row_widgets.get("msgstore.db", []):
            widget.setVisible(not encrypted)
        if encrypted:
            self._db_inputs["wa.db"].setPlaceholderText(
                "Select or paste wa.db; axolotl.db and location.db will be auto-picked from the same folder"
            )
        else:
            self._db_inputs["msgstore.db"].setPlaceholderText(
                "Messages, chats, media (required)"
            )
            self._db_inputs["wa.db"].setPlaceholderText("Contacts (optional)")
        self._update_run_btn()
        return

    def _browse_crypt_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Encrypted Backup (.crypt14)",
            "", "Crypt14 Files (*.crypt14);;Crypt15 Files (*.crypt15);;All Files (*)"
        )
        if path:
            self._crypt_input.setText(path)
            self._update_run_btn()

    def _browse_crypt_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Key File", "", "All Files (*)"
        )
        if path:
            self._crypt_key_input.setText(path)

    def _browse_key_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Key File", "", "All Files (*)"
        )
        if path:
            self._key_input.setText(path)

    def _browse_media(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select WhatsApp Media Root Folder"
        )
        if folder:
            self._media_input.setText(folder)

    def _browse_avatars(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Avatars Folder (contains .j files)"
        )
        if folder:
            self._avatars_input.setText(folder)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder"
        )
        if folder:
            self._output_input.setText(folder)

    # ── Input detection ──

    def _detect_folder(self, folder: str):
        """Scan a folder and populate individual DB fields."""
        p = Path(folder)
        found_any = False

        is_encrypted = self._mode_encrypted.isChecked()

        for db_name, desc, required in DB_FILES:
            # In encrypted mode, skip msgstore.db (comes from decrypted backup)
            if is_encrypted and db_name == "msgstore.db":
                continue

            db_path = p / db_name
            inp = self._db_inputs[db_name]
            status = self._db_status[db_name]

            if db_path.exists():
                size_mb = db_path.stat().st_size / (1024 * 1024)
                inp.setText(str(db_path))
                status.setText("\u2713")
                status.setStyleSheet(
                    f"color: {self._green}; font-size: 10px; border: none;"
                )
                status.setToolTip(f"{size_mb:.1f} MB")
                found_any = True
            else:
                # Check for variants (e.g. msgstore.db.crypt14)
                variants = list(p.glob(f"{db_name}*"))
                if variants and not inp.text():
                    # Don't overwrite if already set
                    pass
                if not inp.text():
                    status.setText("\u2014" if not required else "\u2718")
                    status.setStyleSheet(
                        f"color: {self._red if required else self._fg_sub}; "
                        f"font-size: 10px; border: none;"
                    )

        # Also scan for shared_prefs files
        # Check: folder/shared_prefs/, parent/shared_prefs/, sibling shared_prefs/
        for sp_dir in [p / "shared_prefs", p.parent / "shared_prefs",
                       p / ".." / "shared_prefs"]:
            if sp_dir.is_dir():
                for pref_name, desc, required in PREFS_FILES:
                    pref_path = sp_dir / pref_name
                    if pref_path.exists() and pref_name in self._prefs_inputs:
                        self._prefs_inputs[pref_name].setText(str(pref_path.resolve()))
                        self._prefs_status[pref_name].setText("\u2713")
                        self._prefs_status[pref_name].setStyleSheet(
                            f"color: {self._green}; font-size: 10px; border: none;"
                        )
                        found_any = True
                break  # found shared_prefs dir

        # If no msgstore.db found but crypt files exist, hint the user
        if not is_encrypted and not self._db_inputs["msgstore.db"].text():
            crypt_files = list(p.glob("*.crypt*"))
            if crypt_files:
                self._db_status["msgstore.db"].setText("\U0001F512")
                self._db_status["msgstore.db"].setToolTip(
                    "No msgstore.db found, but encrypted backups detected.\n"
                    "Switch to 'Encrypted Backup' mode above."
                )

        self._update_run_btn()

    # ── Prepare databases directory ──

    def _prepare_db_path(self) -> str | None:
        """
        Ensure all selected DBs are in one directory.
        If files come from different paths, copy them to a staging dir.
        Returns the path to use as databases_path.
        """
        paths: dict[str, str] = {}
        for db_name, _, _ in DB_FILES:
            val = self._db_inputs[db_name].text().strip()
            if val:
                paths[db_name] = val

        if "msgstore.db" not in paths:
            QMessageBox.warning(
                self, "Missing Required File",
                "msgstore.db is required. Please select it."
            )
            return None

        # Check if all files are in the same directory
        dirs = set()
        for p in paths.values():
            dirs.add(str(Path(p).parent))

        # Also collect prefs files
        prefs_paths: dict[str, str] = {}
        for pref_name, _, _ in PREFS_FILES:
            if pref_name in self._prefs_inputs:
                val = self._prefs_inputs[pref_name].text().strip()
                if val:
                    prefs_paths[pref_name] = val

        if len(dirs) == 1:
            # All in one folder — use directly, don't modify source
            return dirs.pop()
        else:
            # Mixed paths — stage into temp directory
            staging = tempfile.mkdtemp(prefix="wfa_staging_")
            self._staging_dir = staging
            for db_name, src_path in paths.items():
                src = Path(src_path)
                dst_name = db_name if src.suffix == ".db" else src.name
                shutil.copy2(src_path, os.path.join(staging, dst_name))
                self._log.append(f"  Staged: {db_name} -> staging dir")
            # Copy prefs files to staging/shared_prefs/
            if prefs_paths:
                sp_dir = os.path.join(staging, "shared_prefs")
                os.makedirs(sp_dir, exist_ok=True)
                for pref_name, src_path in prefs_paths.items():
                    shutil.copy2(src_path, os.path.join(sp_dir, pref_name))
                    self._log.append(f"  Staged: {pref_name} -> staging/shared_prefs/")
            return staging

    # ── Run pipeline ──

    def _start_ingestion(self):
        output = self._output_input.text().strip()
        if not output:
            QMessageBox.warning(self, "Error", "Please specify an output path.")
            return
        os.makedirs(output, exist_ok=True)

        # ── Encrypted backup flow ──
        if self._mode_encrypted.isChecked():
            crypt_file = self._crypt_input.text().strip()
            key_file = self._crypt_key_input.text().strip()
            if not crypt_file:
                QMessageBox.warning(self, "Error", "Please select the encrypted backup file.")
                return
            if not key_file:
                QMessageBox.warning(self, "Error", "Please select the key file.")
                return

            # Collect extra DB paths now (before background thread)
            extra_db_paths: dict[str, str] = {}
            for db_name, _, _ in DB_FILES:
                if db_name == "msgstore.db":
                    continue
                val = self._db_inputs[db_name].text().strip()
                if val and os.path.isfile(val):
                    extra_db_paths[db_name] = val

            # Stage any selected prefs files into a temp folder — NEVER into
            # the source (it's forensic evidence, must stay read-only).
            pending_prefs_dir: str | None = None
            prefs_selected_enc: list[tuple[str, str]] = []
            for pref_name, _, _ in PREFS_FILES:
                if pref_name in self._prefs_inputs:
                    val = self._prefs_inputs[pref_name].text().strip()
                    if val and os.path.isfile(val):
                        prefs_selected_enc.append((pref_name, val))
            if prefs_selected_enc:
                if not self._staging_dir or not os.path.isdir(self._staging_dir):
                    self._staging_dir = tempfile.mkdtemp(prefix="wfa_staging_")
                sp_dir = os.path.join(self._staging_dir, "shared_prefs")
                os.makedirs(sp_dir, exist_ok=True)
                for pref_name, src_path in prefs_selected_enc:
                    dst = os.path.join(sp_dir, pref_name)
                    if not os.path.exists(dst):
                        shutil.copy2(src_path, dst)
                        self._log.append(
                            f"  Staged: {pref_name} -> <tempdir>/shared_prefs/"
                        )
                pending_prefs_dir = sp_dir

            # Store for use after decryption completes
            self._pending_output = output
            self._pending_media = self._media_input.text().strip() or None
            self._pending_extra_db = extra_db_paths
            self._pending_prefs_dir = pending_prefs_dir

            # Lock UI
            self._run_btn.setEnabled(False)
            self._run_btn.setText("\U0001F512 Decrypting...")
            self._log.append(f"{'=' * 50}")
            self._log.append(f"  Encrypted Backup Decryption")
            self._log.append(f"  Backup: {Path(crypt_file).name}")
            self._log.append(f"  Key:    {Path(key_file).name}")
            self._log.append(f"{'=' * 50}")
            self._log.append(f"\n\U0001F512 Decrypting (this may take a few minutes for large files)...")

            # Run decryption in background thread
            from PySide6.QtCore import QThread, Signal as _Signal

            class _DecryptWorker(QThread):
                finished = _Signal(bool, str)  # (success, message_or_path)

                def __init__(self, crypt_f, key_f, out_path):
                    super().__init__()
                    self._crypt = crypt_f
                    self._key = key_f
                    self._out = out_path

                def run(self):
                    try:
                        from app.services.backup_decryptor import decrypt_backup
                        decrypt_backup(self._crypt, self._key, self._out)
                        self.finished.emit(True, self._out)
                    except Exception as e:
                        self.finished.emit(False, str(e))

            # Decrypt as msgstore.db (pipeline needs this name)
            # Store original backup filename in log for forensic traceability
            decrypted_db = os.path.join(output, "msgstore.db")
            self._log.append(f"  Source: {Path(crypt_file).name}")
            self._decrypt_worker = _DecryptWorker(crypt_file, key_file, decrypted_db)
            self._decrypt_worker.finished.connect(self._on_decrypt_finished)
            self._decrypt_worker.start()
            return  # Wait for decryption to complete
        else:
            # ── Normal flow ──
            # Collect all DB paths directly — no staging/copying
            extra_db_paths = {}
            msgstore_path = self._db_inputs["msgstore.db"].text().strip()
            if not msgstore_path:
                QMessageBox.warning(self, "Error", "msgstore.db is required.")
                return
            if not os.path.isfile(msgstore_path):
                QMessageBox.warning(self, "Error", f"File not found: {msgstore_path}")
                return

            # Use msgstore.db's directory as the base db_path
            db_path = str(Path(msgstore_path).parent)

            # Pass every selected DB as an explicit full path. The backend
            # still receives a base folder, but named paths win.
            for db_name, _, _ in DB_FILES:
                val = self._db_inputs[db_name].text().strip()
                if val and os.path.isfile(val):
                    extra_db_paths[db_name] = val

            # Prefs: stage to a TEMP directory — never into the source
            # databases folder.  Writing to the source would violate
            # forensic read-only integrity (the source extraction is
            # evidence; we must never mutate it).  The backend reads the
            # staged files via the ``--prefs-dir`` CLI arg.
            prefs_selected: list[tuple[str, str]] = []
            for pref_name, _, _ in PREFS_FILES:
                if pref_name in self._prefs_inputs:
                    val = self._prefs_inputs[pref_name].text().strip()
                    if val and os.path.isfile(val):
                        prefs_selected.append((pref_name, val))

            if prefs_selected:
                # Reuse self._staging_dir if one was already created by the
                # encrypted flow, else create a fresh one.  The existing
                # cleanup in _on_finished() will rmtree it when ingestion
                # ends (or on next launch if we crashed mid-run).
                if not self._staging_dir or not os.path.isdir(self._staging_dir):
                    self._staging_dir = tempfile.mkdtemp(prefix="wfa_staging_")
                sp_dir = os.path.join(self._staging_dir, "shared_prefs")
                os.makedirs(sp_dir, exist_ok=True)
                for pref_name, src_path in prefs_selected:
                    dst = os.path.join(sp_dir, pref_name)
                    if not os.path.exists(dst):
                        shutil.copy2(src_path, dst)
                        self._log.append(
                            f"  Staged: {pref_name} -> <tempdir>/shared_prefs/"
                        )
                prefs_dir_arg = sp_dir
            else:
                prefs_dir_arg = None

        media = self._media_input.text().strip() or None

        # Lock UI
        self._run_btn.setEnabled(False)
        self._run_btn.setText("\u23F3  Running...")
        self._browse_folder_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._stage_count = 0
        self._start_time = time.time()
        self._last_stage_time = self._start_time
        self._stage_durations: list[float] = []
        self._stage_label.setText("Starting pipeline...")
        self._eta_label.setText("")

        avatars = self._avatars_input.text().strip() or None

        from app.services.ingestion_worker import IngestionWorker

        self._worker = IngestionWorker(
            databases_path=db_path,
            output_path=output,
            media_path=media,
            avatars_path=avatars,
            case_id=self._case_id_input.text().strip(),
            examiner=self._examiner_input.text().strip(),
            extra_db_paths=extra_db_paths if extra_db_paths else None,
            prefs_dir=prefs_dir_arg,
        )
        self._worker.progress_text.connect(self._on_progress_text)
        self._worker.stage_finished.connect(self._on_stage_finished)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress_text(self, msg: str):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_stage_finished(self, name: str, rows: int):
        self._stage_count += 1
        now = time.time()
        stage_dur = now - self._last_stage_time
        self._last_stage_time = now
        self._stage_durations.append(stage_dur)
        if not hasattr(self, "_stage_names"):
            self._stage_names = []
        self._stage_names.append(name)

        self._progress.setValue(self._stage_count)
        dur_str = f"{stage_dur:.1f}s" if stage_dur < 60 else f"{stage_dur/60:.1f}m"
        self._stage_label.setText(
            f"Stage {self._stage_count}/{TOTAL_STAGES}: {name} ({dur_str})"
        )

        # Dynamic ETA based on elapsed-fraction method
        # Instead of median-per-stage (inaccurate because MESSAGES and PRECOMPUTE
        # take 5+ min each while most stages take <1s), use the fraction of known
        # heavy work completed to project remaining time.
        elapsed = now - self._start_time
        remaining_stages = TOTAL_STAGES - self._stage_count

        if self._stage_count > 0 and remaining_stages > 0:
            # Weight completed stages by actual duration for proportion-based ETA
            # Heavy stages (MESSAGES, PRECOMPUTE, MEDIA, LINKS, FTS_INDEX) dominate
            # runtime, so once they're done the remaining estimate drops sharply.
            _heavy = {"MESSAGES", "PRECOMPUTE", "MEDIA", "LINKS", "FTS_INDEX",
                      "SYSTEM_EVENTS", "RECEIPTS"}
            _completed_names = set(self._stage_names)
            _remaining_heavy = _heavy - _completed_names
            _completed_heavy = _heavy & _completed_names

            if _completed_heavy:
                # We've seen some heavy stages — project based on elapsed time
                # and fraction of total weight completed
                # Assume heavy stages = 90% of runtime, light = 10%
                heavy_weight = 0.90
                light_weight = 0.10
                n_heavy_total = len(_heavy)
                n_heavy_done = len(_completed_heavy)
                n_light_total = TOTAL_STAGES - n_heavy_total
                n_light_done = self._stage_count - n_heavy_done

                frac_done = (
                    heavy_weight * (n_heavy_done / max(n_heavy_total, 1))
                    + light_weight * (n_light_done / max(n_light_total, 1))
                )
                frac_done = max(frac_done, 0.01)  # avoid div/0
                estimated_total = elapsed / frac_done
                remaining = max(estimated_total - elapsed, 0)
            else:
                # Only light stages done so far — use average pace
                avg_dur = elapsed / self._stage_count
                remaining = avg_dur * remaining_stages

            if remaining > 120:
                eta_str = f"~{remaining / 60:.0f} min remaining"
            elif remaining > 10:
                eta_str = f"~{remaining:.0f}s remaining"
            else:
                eta_str = "Almost done..."
        elif remaining_stages == 0:
            eta_str = "Finishing..."
        else:
            eta_str = "Calculating..."

        elapsed_str = (
            f"{elapsed / 60:.1f} min" if elapsed > 60
            else f"{elapsed:.0f}s"
        )
        self._eta_label.setText(f"Elapsed: {elapsed_str}  |  {eta_str}")

    def _on_finished(self, success: bool, message: str):
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._run_btn.setText("\u25B6  Start Ingestion")
        self._browse_folder_btn.setEnabled(True)

        elapsed = time.time() - self._start_time
        elapsed_str = (
            f"{elapsed / 60:.1f} min" if elapsed > 60
            else f"{elapsed:.0f}s"
        )

        # Cleanup staging dir
        if self._staging_dir and os.path.exists(self._staging_dir):
            try:
                shutil.rmtree(self._staging_dir)
            except Exception:
                pass
            self._staging_dir = ""

        if success:
            output = self._output_input.text().strip()
            self._analysis_db_path = str(Path(output) / "analysis.db")

            self._stage_label.setText("\u2713  Pipeline Complete!")
            self._stage_label.setStyleSheet(
                f"color: {self._green}; font-size: 11px; font-weight: bold;"
            )
            self._eta_label.setText(f"Total time: {elapsed_str}")

            self._log.append(
                f"\n{'=' * 50}\n"
                f"  DONE \u2014 analysis.db created ({elapsed_str})\n"
                f"  {self._analysis_db_path}\n"
                f"{'=' * 50}"
            )

            # Save ingestion log to case folder
            self._save_ingestion_log(output, elapsed_str)

            QMessageBox.information(
                self, "Ingestion Complete",
                f"Pipeline completed in {elapsed_str}!\n\n"
                f"Click OK to open the case."
            )
            self.accept()
        else:
            self._stage_label.setText("\u2718  Pipeline Failed")
            self._stage_label.setStyleSheet(
                "color: #c62828; font-size: 11px; font-weight: bold;"
            )
            self._eta_label.setText(f"Failed after {elapsed_str}")
            self._log.append(f"\n\u2718 FAILED: {message}")
            QMessageBox.critical(
                self, "Ingestion Failed",
                f"Pipeline failed after {elapsed_str}:\n\n{message}"
            )

    def _save_ingestion_log(self, case_path: str, elapsed_str: str):
        """Save the ingestion log text to a file in the case folder."""
        try:
            from datetime import datetime
            log_path = os.path.join(case_path, "ingestion_log.txt")
            log_text = self._log.toPlainText()
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"WAInsight Ingestion Log\n")
                f.write(f"Generated: {datetime.now().isoformat()}\n")
                f.write(f"Duration: {elapsed_str}\n")
                f.write(f"{'=' * 60}\n\n")
                f.write(log_text)
            logger.info("Ingestion log saved: %s", log_path)
        except Exception as e:
            logger.warning("Failed to save ingestion log: %s", e)

    @property
    def analysis_db_path(self) -> str:
        return self._analysis_db_path

    # ── Backup decryption ──

    def _decrypt_backup(self, crypt_file: str, key_text: str,
                        output_dir: str) -> str:
        from app.services.backup_decryptor import decrypt_backup
        decrypt_dir = os.path.join(output_dir, "decrypted")
        os.makedirs(decrypt_dir, exist_ok=True)
        out_db = os.path.join(decrypt_dir, "msgstore.db")
        decrypt_backup(crypt_file, key_text, out_db)
        return decrypt_dir
