"""
WAInsight -- WhatsApp Forensic Suite for Android

Cross-platform forensic analysis viewer for normalized WhatsApp databases.
Connects directly to analysis.db (read-only) without an API layer.

Startup flow:
  1. CLI arg -> open that DB or case directly
  2. Otherwise -> ALWAYS show Case Dialog (recent cases, open, new + ingest)

Usage:
    python main.py [path/to/analysis.db | path/to/case.wfacase]
"""

from __future__ import annotations

import os
import sys
import time as _time
from pathlib import Path

# Chromium flags for QWebEngine: suppress noise + enable GPU acceleration
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
    "--log-level=3 "
    "--enable-gpu-rasterization "  # GPU-accelerated tile rasterization
    "--enable-zero-copy "          # Zero-copy GPU memory buffers
    "--enable-features=CanvasOopRasterization"  # Out-of-process canvas raster
)

# Suppress Qt font/DPI warnings
os.environ["QT_LOGGING_RULES"] = "qt.text.font.db=false;qt.qpa.fonts=false;qt.text.font=false;qt.qpa.window=false"

# Ensure project root is in sys.path so 'shared' module can be imported
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.config import APP_NAME, APP_SUBTITLE, APP_VERSION, ORG_NAME

_LOGO_PATH = str(Path(__file__).parent / "app" / "resources" / "logo.png")


def _apply_theme(app: QApplication) -> None:
    """Apply Material base theme and custom QSS overrides."""
    from app.services.theme_manager import ThemeManager
    tm = ThemeManager.get()

    try:
        from qt_material import apply_stylesheet
        extra = {
            "density_scale": "0",
            "font_family": "Segoe UI, SF Pro Display, sans-serif",
        }
        apply_stylesheet(app, theme=tm.qt_material_theme, extra=extra)
    except ImportError:
        pass

    qss_path = Path(__file__).parent / "app" / "resources" / "themes" / tm.qss_filename
    if qss_path.exists():
        with open(qss_path, "r") as f:
            app.setStyleSheet(app.styleSheet() + f.read())


def _resolve_cli_path() -> Path | None:
    """Check if a CLI argument points to a valid DB or case folder."""
    if len(sys.argv) > 1:
        arg = Path(sys.argv[1]).resolve()
        # Could be a .wfacase folder
        if arg.is_dir() and (arg / "analysis.db").exists():
            from app.services.case_manager import CaseManager
            cm = CaseManager.get()
            cm.open_case(str(arg))
            return arg / "analysis.db"
        # Could be an analysis.db file
        if arg.exists() and arg.suffix == ".db":
            return arg
    return None


def main() -> int:
    # Setup logging early — always UTC for forensic consistency
    import logging
    logging.Formatter.converter = _time.gmtime  # force UTC timestamps
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s UTC [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)
    if os.path.isfile(_LOGO_PATH):
        app.setWindowIcon(QIcon(_LOGO_PATH))

    # Migrate recent cases from old settings (ForensicTools/WhatsApp Forensic Analyzer)
    from PySide6.QtCore import QSettings
    new_settings = QSettings()
    if not new_settings.value("recent_cases"):
        old_settings = QSettings("ForensicTools", "WhatsApp Forensic Analyzer")
        old_recent = old_settings.value("recent_cases", [])
        if old_recent and isinstance(old_recent, list):
            new_settings.setValue("recent_cases", old_recent)
            logging.getLogger(__name__).info(
                "Migrated %d recent cases from old settings", len(old_recent))

    _apply_theme(app)

    # CLI argument takes priority — skip dialog
    db_path = _resolve_cli_path()

    # Otherwise ALWAYS show Case Dialog — never silently auto-open an old DB
    if db_path is None:
        from app.views.dialogs.case_dialog import CaseDialog

        dlg = CaseDialog()
        result = dlg.exec()
        from PySide6.QtWidgets import QDialog as _QD
        if result != _QD.Accepted or not dlg.selected_db:
            return 0  # User cancelled

        db_path = Path(dlg.selected_db)

    # Initialize database
    from app.services.database import Database
    try:
        Database.init(db_path)
    except Exception as e:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None, APP_NAME,
            f"Failed to open database:\n\n{e}",
        )
        return 1

    # Add file logging to case directory
    from app.services.case_manager import CaseManager
    cm = CaseManager.get()
    if cm.is_open and cm._case_path:
        log_file = cm._case_path / "wainsight.log"
        file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
        _fmt = logging.Formatter(
            "%(asctime)s UTC [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")
        _fmt.converter = _time.gmtime  # UTC timestamps
        file_handler.setFormatter(_fmt)
        logging.getLogger().addHandler(file_handler)
        logging.getLogger(__name__).info("Log file: %s", log_file)

    # Create and show main window
    from app.views.main_window import MainWindow

    window = MainWindow()
    if cm.is_open:
        window.setWindowTitle(f"{APP_NAME} \u2014 {cm.case_name}")
    else:
        window.setWindowTitle(f"{APP_NAME} \u2014 {APP_SUBTITLE}")
    window.showMaximized()

    ret = app.exec()

    # Cleanup
    Database.get().close()
    return ret


if __name__ == "__main__":
    sys.exit(main())
