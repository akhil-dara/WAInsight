"""
Helper to load report generators from backend/app/reports/ using importlib,
avoiding the app/ package name conflict between gui/ and backend/.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

_THIS_DIR = Path(__file__).resolve().parent

def _find_backend_reports_dir() -> Path:
    """Walk up from this file to find backend/app/reports/."""
    for parent in _THIS_DIR.parents:
        candidate = parent / "backend" / "app" / "reports"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Cannot find backend/app/reports/ directory. "
        "Ensure the project structure is intact."
    )


def _load_module(module_name: str, file_path: Path):
    """Load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_group_report() -> Callable:
    """Return the generate_group_report function from backend."""
    reports_dir = _find_backend_reports_dir()
    mod = _load_module("group_report", reports_dir / "group_report.py")
    return mod.generate_group_report


def load_contact_report() -> Callable:
    """Return the generate_contact_report function from backend."""
    reports_dir = _find_backend_reports_dir()
    mod = _load_module("contact_report", reports_dir / "contact_report.py")
    return mod.generate_contact_report
