#!/usr/bin/env python3
"""
WAInsight — WhatsApp Forensic Suite for Android

Root entry point. Launches the GUI application.

Usage:
    python wainsight.py [path/to/analysis.db | path/to/case.wfacase]
"""
import os
import sys
from pathlib import Path

# Add gui/ to sys.path so app.* imports work
_GUI_DIR = str(Path(__file__).resolve().parent / "gui")
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

if __name__ == "__main__":
    from main import main
    sys.exit(main())
