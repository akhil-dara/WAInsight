"""
Background worker for running the ingestion pipeline from the GUI.

Runs the backend pipeline as a subprocess to avoid Python module name
collisions (both gui/app and backend/app exist). Parses JSON progress
lines from stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal


class IngestionWorker(QThread):
    """Run the backend ingestion pipeline as a subprocess."""

    stage_started = Signal(str)         # stage name (stage just began)
    stage_finished = Signal(str, int)   # stage name, row count (stage done)
    progress_text = Signal(str)         # log line
    finished = Signal(bool, str)        # success, message

    def __init__(
        self,
        databases_path: str,
        output_path: str,
        media_path: Optional[str] = None,
        avatars_path: Optional[str] = None,
        case_id: str = "",
        examiner: str = "",
        extra_db_paths: dict[str, str] | None = None,
        prefs_dir: Optional[str] = None,
    ):
        super().__init__()
        self._databases_path = databases_path
        self._output_path = output_path
        self._media_path = media_path
        self._avatars_path = avatars_path
        self._case_id = case_id
        self._examiner = examiner
        self._extra_db_paths = extra_db_paths or {}
        # Optional staged ``shared_prefs`` folder (temp dir created by the GUI).
        # Passed through to the backend so it never has to write into the
        # read-only source databases folder.
        self._prefs_dir = prefs_dir

    def run(self):
        try:
            # Find the backend runner script
            backend_root = (
                Path(__file__).resolve().parent.parent.parent.parent / "backend"
            )
            runner = backend_root / "run_ingest.py"

            if not runner.exists():
                self.finished.emit(False, f"Backend runner not found: {runner}")
                return

            # Build command
            cmd = [
                sys.executable, str(runner),
                "--db-path", self._databases_path,
                "--output", self._output_path,
            ]
            if self._media_path:
                cmd += ["--media-path", self._media_path]
            if self._case_id:
                cmd += ["--case-id", self._case_id]
            if self._examiner:
                cmd += ["--examiner", self._examiner]
            if self._avatars_path:
                cmd += ["--avatars-path", self._avatars_path]
            if self._prefs_dir:
                cmd += ["--prefs-dir", self._prefs_dir]
            for db_name, db_path in self._extra_db_paths.items():
                cmd += ["--extra-db", f"{db_name}={db_path}"]

            self.progress_text.emit(f"Databases: {self._databases_path}")
            self.progress_text.emit(f"Output:    {self._output_path}")
            for db_name, db_path in self._extra_db_paths.items():
                self.progress_text.emit(f"{db_name}: {db_path}")
            if self._media_path:
                self.progress_text.emit(f"Media:     {self._media_path}")
            self.progress_text.emit("")
            self.progress_text.emit("Starting pipeline subprocess...")

            # Run as subprocess, read stdout line by line
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
                cwd=str(backend_root),
            )

            stage_count = 0
            total_rows = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = data.get("event", "")

                    if event == "start":
                        self.progress_text.emit("Pipeline started...")

                    elif event == "stage_start":
                        # Real-time: stage just started
                        name = data.get("name", "?")
                        label = data.get("label", "")
                        self.stage_started.emit(name)
                        self.progress_text.emit(
                            f"  ▶ {name}: {label}..."
                        )

                    elif event == "stage":
                        # Real-time: stage completed/failed/skipped
                        stage_count += 1
                        name = data.get("name", "?")
                        status = data.get("status", "?")
                        rows = data.get("rows", 0)
                        dur = data.get("duration", 0)
                        error = data.get("error", "")
                        total_rows += rows

                        if status == "completed":
                            self.stage_finished.emit(name, rows)
                            self.progress_text.emit(
                                f"  ✓ Stage {stage_count}: {name} — "
                                f"{rows:,} rows ({dur:.1f}s)"
                            )
                        elif status == "failed":
                            self.progress_text.emit(
                                f"  ✗ Stage {stage_count}: {name} — "
                                f"FAILED: {error}"
                            )
                        elif status == "skipped":
                            self.stage_finished.emit(name, 0)
                            self.progress_text.emit(
                                f"  ⊘ Stage {stage_count}: {name} — skipped"
                            )
                        else:
                            self.progress_text.emit(
                                f"  Stage {stage_count}: {name} — {status}"
                            )

                    elif event == "done":
                        elapsed = data.get("elapsed", 0)
                        summary = (
                            f"Pipeline complete in {elapsed:.1f}s\n"
                            f"Total rows processed: {total_rows:,}"
                        )
                        self.progress_text.emit(summary)

                    elif event == "error":
                        msg = data.get("message", "Unknown error")
                        self.progress_text.emit(f"ERROR: {msg}")

                except json.JSONDecodeError:
                    # Not JSON — just show as log text
                    self.progress_text.emit(line)

            proc.wait()

            # Read stderr for any errors
            stderr_text = proc.stderr.read().strip()
            if stderr_text:
                # Only show non-trivial stderr
                for err_line in stderr_text.split("\n")[-10:]:
                    self.progress_text.emit(f"[stderr] {err_line}")

            if proc.returncode == 0:
                summary = (
                    f"Pipeline complete\n"
                    f"Total rows processed: {total_rows:,}"
                )
                self.finished.emit(True, summary)
            else:
                self.finished.emit(
                    False,
                    f"Pipeline exited with code {proc.returncode}"
                    + (f"\n{stderr_text[-500:]}" if stderr_text else ""),
                )

        except Exception as e:
            self.progress_text.emit(f"Worker error: {type(e).__name__}: {e}")
            self.finished.emit(False, str(e))
