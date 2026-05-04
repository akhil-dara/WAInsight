"""
Subprocess entry point for running the ingestion pipeline.

Called by the GUI's IngestionWorker via subprocess. Prints JSON progress
lines to stdout for the GUI to parse — in real-time as each stage
starts and completes.

Usage:
    python run_ingest.py --db-path <path> --output <path> [--media-path <path>]
                         [--case-id <id>] [--examiner <name>]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# Ensure backend is on path
sys.path.insert(0, os.path.dirname(__file__))
# Ensure project root is on path so 'shared' module can be imported
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.config import build_config
from app.ingestion.orchestrator import IngestionOrchestrator


def _emit(event: str, **kwargs):
    """Print a JSON event line for the GUI to parse."""
    data = {"event": event, **kwargs}
    print(json.dumps(data), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--media-path", default=None)
    parser.add_argument("--case-id", default="")
    parser.add_argument("--examiner", default="")
    parser.add_argument("--avatars-path", default=None,
                        help="Path to WhatsApp Avatars folder (contains .j files)")
    parser.add_argument("--extra-db", action="append", default=[],
                        help="Extra DB path in name=path format (e.g. wa.db=/path/to/wa.db)")
    parser.add_argument("--prefs-dir", default=None,
                        help=("Optional directory holding WhatsApp shared_prefs "
                              "XML files (typically a GUI-staged temp folder). "
                              "When omitted, the legacy '../shared_prefs' "
                              "lookup relative to msgstore.db is used."))
    args = parser.parse_args()

    # Suppress logging to stdout — we use JSON events instead. Always UTC.
    import time as _time
    logging.Formatter.converter = _time.gmtime
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s UTC %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    _emit("start", databases=args.db_path, output=args.output,
          media=args.media_path or "")

    # Parse extra DB paths: ["wa.db=/path/to/wa.db", ...]
    extra_db_paths = {}
    for item in args.extra_db:
        if "=" in item:
            name, path = item.split("=", 1)
            extra_db_paths[name.strip()] = path.strip()

    try:
        config = build_config(
            databases_path=args.db_path,
            output_path=args.output,
            media_path=args.media_path,
            avatars_path=args.avatars_path,
            case_id=args.case_id,
            examiner=args.examiner,
            extra_db_paths=extra_db_paths or None,
            prefs_dir=args.prefs_dir,
        )

        orchestrator = IngestionOrchestrator(config)

        # Hook into progress system for real-time stage reporting
        def _on_progress(event_type: str, data: dict):
            """Called by PipelineProgress._broadcast for every event."""
            if event_type == "stage_completed":
                _emit("stage",
                      name=data.get("name", "?"),
                      status="completed",
                      rows=data.get("processed", 0),
                      duration=data.get("elapsed_seconds", 0),
                      error="")
            elif event_type == "stage_started":
                _emit("stage_start",
                      name=data.get("name", "?"),
                      label=data.get("label", ""))
            elif event_type == "stage_failed":
                _emit("stage",
                      name=data.get("name", "?"),
                      status="failed",
                      rows=data.get("processed", 0),
                      duration=data.get("elapsed_seconds", 0),
                      error=data.get("error_message", ""))
            elif event_type == "stage_skipped":
                _emit("stage",
                      name=data.get("name", "?"),
                      status="skipped",
                      rows=0, duration=0, error="")

        orchestrator.progress._stage_callback = _on_progress

        start = time.time()
        orchestrator.run(force_recreate=True)
        elapsed = time.time() - start

        # Final summary
        total_rows = sum(s.processed for s in orchestrator.progress.stages.values())
        _emit("done", elapsed=round(elapsed, 1), total_rows=total_rows)

    except Exception as e:
        _emit("error", message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
