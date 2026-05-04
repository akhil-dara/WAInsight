"""
Ingestion pipeline progress tracking and SSE broadcasting.

Tracks the state of each pipeline stage, supports real-time progress
updates via Server-Sent Events, and provides elapsed/estimated time
calculations for the frontend progress UI.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional
from asyncio import Queue

logger = logging.getLogger(__name__)


class StageStatus(StrEnum):
    """Status of an individual pipeline stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StageProgress:
    """Progress state for a single ingestion pipeline stage."""

    name: str
    """Machine-readable stage identifier (e.g. 'CONTACTS', 'MESSAGES')."""

    label: str
    """Human-readable label for display (e.g. 'Building unified contact registry')."""

    status: StageStatus = StageStatus.PENDING

    # Row-level progress within the stage
    processed: int = 0
    """Number of rows/items processed so far."""

    total: int = 0
    """Expected total rows/items for this stage (0 if unknown)."""

    # Timing
    started_at: float = 0.0
    """Unix timestamp when this stage started."""

    completed_at: float = 0.0
    """Unix timestamp when this stage finished."""

    error_message: str = ""
    """Error message if the stage failed."""

    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since stage started (or total duration if complete)."""
        if self.started_at == 0:
            return 0.0
        end = self.completed_at if self.completed_at > 0 else time.time()
        return end - self.started_at

    @property
    def progress_pct(self) -> float:
        """Completion percentage (0.0 to 100.0). Returns 0 if total unknown."""
        if self.total <= 0:
            return 0.0
        return min(100.0, (self.processed / self.total) * 100.0)

    @property
    def rows_per_second(self) -> float:
        """Processing throughput in rows/second."""
        elapsed = self.elapsed_seconds
        if elapsed <= 0 or self.processed <= 0:
            return 0.0
        return self.processed / elapsed

    @property
    def eta_seconds(self) -> Optional[float]:
        """Estimated seconds remaining based on current throughput."""
        if self.total <= 0 or self.processed <= 0:
            return None
        remaining = self.total - self.processed
        rps = self.rows_per_second
        if rps <= 0:
            return None
        return remaining / rps

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON/SSE output."""
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status.value,
            "processed": self.processed,
            "total": self.total,
            "progress_pct": round(self.progress_pct, 1),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "rows_per_second": round(self.rows_per_second, 0),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds else None,
            "error_message": self.error_message,
        }


# Pipeline stage definitions in execution order
PIPELINE_STAGES: list[tuple[str, str]] = [
    # Must stay in sync with the ``complete_stage()`` calls in
    # orchestrator.py.  Stages that don't emit progress events
    # (``_ingest_location_db``, ``_stage_mentions``) run silently
    # within their parent stages and are not listed here.
    ("VALIDATE", "Validating source databases and detecting schema version"),
    ("HASH", "Computing SHA-256 evidence hashes for all source databases"),
    ("CONTACTS", "Building unified contact registry from 5 identity sources"),
    ("CONVERSATIONS", "Normalizing conversations from chat table"),
    ("MESSAGES", "Normalizing messages (batches of 50K)"),
    ("ALBUMS", "Grouping multi-photo / multi-video albums"),
    ("MEDIA", "Normalizing media records and resolving file paths"),
    ("MEDIA_EVIDENCE", "Building media evidence audit trail"),
    ("ORPHANED_MEDIA", "Scanning for orphaned media files"),
    ("RECEIPTS", "Ingesting delivery/read/played receipts"),
    ("REACTIONS", "Ingesting emoji reactions"),
    ("SYSTEM_EVENTS", "Parsing system events (60+ action types)"),
    ("CALLS", "Normalizing call records"),
    ("SCHEDULED_EVENTS", "Ingesting scheduled events"),
    ("POLLS", "Ingesting polls and votes"),
    ("VCARDS", "Parsing shared contact vCards"),
    ("PINS", "Ingesting pinned messages"),
    ("EDITS", "Ingesting edit history records"),
    ("COMMENTS", "Ingesting comment threads"),
    ("REVOKES", "Processing revoked (deleted-for-everyone) messages"),
    ("LINKS", "Processing URL link records"),
    ("NEWSLETTERS", "Ingesting newsletter + location messages"),
    ("STATUS", "Ingesting WhatsApp Status data"),
    ("COMPANION", "Ingesting companion database data (chatsettings, stickers, media)"),
    ("AVATARS", "Extracting contact avatar images"),
    ("FTS_INDEX", "Building FTS5 full-text search index"),
    ("PRECOMPUTE", "Pre-computing analytics tables, resolving display names"),
    ("FINALIZE", "Verifying integrity and writing case metadata"),
]


@dataclass
class PipelineProgress:
    """Overall progress tracker for the ingestion pipeline.

    Provides methods to update individual stage progress and
    broadcast updates to SSE subscribers.
    """

    stages: dict[str, StageProgress] = field(default_factory=dict)
    """All pipeline stages keyed by stage name."""

    pipeline_started_at: float = 0.0
    """Unix timestamp when the entire pipeline started."""

    pipeline_completed_at: float = 0.0
    """Unix timestamp when the entire pipeline finished."""

    is_running: bool = False
    """True if the pipeline is currently executing."""

    is_complete: bool = False
    """True if the pipeline finished successfully."""

    error: str = ""
    """Fatal error message if the pipeline crashed."""

    _subscribers: list[Queue] = field(default_factory=list)
    """SSE subscriber queues for real-time progress broadcasting."""

    _stage_callback: Any = None
    """Optional callback(event_type, stage_dict) for subprocess progress reporting."""

    def __post_init__(self) -> None:
        """Initialize all pipeline stages from the definition list."""
        if not self.stages:
            for name, label in PIPELINE_STAGES:
                self.stages[name] = StageProgress(name=name, label=label)

    # -- Stage lifecycle methods --

    def start_pipeline(self) -> None:
        """Mark the pipeline as started."""
        self.pipeline_started_at = time.time()
        self.is_running = True
        self.is_complete = False
        self.error = ""
        logger.info("Ingestion pipeline started")
        self._broadcast("pipeline_started", {"timestamp": self.pipeline_started_at})

    def complete_pipeline(self) -> None:
        """Mark the pipeline as successfully completed."""
        self.pipeline_completed_at = time.time()
        self.is_running = False
        self.is_complete = True
        elapsed = self.pipeline_completed_at - self.pipeline_started_at
        logger.info("Ingestion pipeline completed in %.1f seconds", elapsed)
        self._broadcast("pipeline_completed", {
            "elapsed_seconds": round(elapsed, 1),
            "timestamp": self.pipeline_completed_at,
        })

    def fail_pipeline(self, error: str) -> None:
        """Mark the pipeline as failed."""
        self.pipeline_completed_at = time.time()
        self.is_running = False
        self.is_complete = False
        self.error = error
        logger.error("Ingestion pipeline FAILED: %s", error)
        self._broadcast("pipeline_failed", {"error": error})

    def start_stage(self, name: str, total: int = 0) -> None:
        """Mark a stage as running.

        Args:
            name: Stage identifier (e.g. 'MESSAGES').
            total: Expected total rows for progress tracking.
        """
        stage = self.stages.get(name)
        if not stage:
            logger.warning("Unknown pipeline stage: %s", name)
            return
        stage.status = StageStatus.RUNNING
        stage.started_at = time.time()
        stage.total = total
        stage.processed = 0
        logger.info("[%s] Starting: %s (total: %s)", name, stage.label, total or "unknown")
        self._broadcast("stage_started", stage.to_dict())

    def update_stage(self, name: str, processed: int, total: int = 0) -> None:
        """Update row-level progress within a stage.

        Args:
            name: Stage identifier.
            processed: Number of rows processed so far.
            total: Updated total if the initial estimate changed.
        """
        stage = self.stages.get(name)
        if not stage:
            return
        stage.processed = processed
        if total > 0:
            stage.total = total
        # Broadcast progress updates at reasonable intervals (every 10K rows or 2 seconds)
        self._broadcast("stage_progress", stage.to_dict())

    def complete_stage(self, name: str, processed: int = 0) -> None:
        """Mark a stage as completed.

        Args:
            name: Stage identifier.
            processed: Final processed count (updates the stage if non-zero).
        """
        stage = self.stages.get(name)
        if not stage:
            return
        stage.status = StageStatus.COMPLETED
        stage.completed_at = time.time()
        if processed > 0:
            stage.processed = processed
        if stage.total == 0:
            stage.total = stage.processed
        logger.info(
            "[%s] Completed: %d rows in %.1fs (%.0f rows/s)",
            name, stage.processed, stage.elapsed_seconds, stage.rows_per_second,
        )
        self._broadcast("stage_completed", stage.to_dict())

    def fail_stage(self, name: str, error: str) -> None:
        """Mark a stage as failed.

        Args:
            name: Stage identifier.
            error: Error message describing the failure.
        """
        stage = self.stages.get(name)
        if not stage:
            return
        stage.status = StageStatus.FAILED
        stage.completed_at = time.time()
        stage.error_message = error
        logger.error("[%s] FAILED: %s", name, error)
        self._broadcast("stage_failed", stage.to_dict())

    def skip_stage(self, name: str, reason: str = "") -> None:
        """Mark a stage as skipped.

        Args:
            name: Stage identifier.
            reason: Why the stage was skipped.
        """
        stage = self.stages.get(name)
        if not stage:
            return
        stage.status = StageStatus.SKIPPED
        stage.error_message = reason
        logger.info("[%s] Skipped: %s", name, reason or "not applicable")
        self._broadcast("stage_skipped", stage.to_dict())

    # -- Query methods --

    @property
    def current_stage(self) -> Optional[StageProgress]:
        """Return the currently running stage, or None."""
        for stage in self.stages.values():
            if stage.status == StageStatus.RUNNING:
                return stage
        return None

    @property
    def completed_count(self) -> int:
        """Number of stages that have completed."""
        return sum(1 for s in self.stages.values() if s.status == StageStatus.COMPLETED)

    @property
    def total_stages(self) -> int:
        """Total number of pipeline stages."""
        return len(self.stages)

    @property
    def overall_progress_pct(self) -> float:
        """Overall pipeline completion percentage."""
        if self.total_stages == 0:
            return 0.0
        done = sum(
            1 for s in self.stages.values()
            if s.status in (StageStatus.COMPLETED, StageStatus.SKIPPED)
        )
        return (done / self.total_stages) * 100.0

    @property
    def pipeline_elapsed_seconds(self) -> float:
        """Total elapsed time for the pipeline."""
        if self.pipeline_started_at == 0:
            return 0.0
        end = self.pipeline_completed_at if self.pipeline_completed_at > 0 else time.time()
        return end - self.pipeline_started_at

    def to_dict(self) -> dict[str, Any]:
        """Full pipeline state as a dictionary."""
        return {
            "is_running": self.is_running,
            "is_complete": self.is_complete,
            "error": self.error,
            "overall_progress_pct": round(self.overall_progress_pct, 1),
            "completed_stages": self.completed_count,
            "total_stages": self.total_stages,
            "elapsed_seconds": round(self.pipeline_elapsed_seconds, 1),
            "current_stage": self.current_stage.to_dict() if self.current_stage else None,
            "stages": [s.to_dict() for s in self.stages.values()],
        }

    # -- SSE broadcasting --

    def subscribe(self) -> Queue:
        """Subscribe to real-time progress updates.

        Returns:
            An asyncio Queue that will receive SSE event dictionaries.
        """
        queue: Queue = Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        """Remove an SSE subscriber."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Send an event to all SSE subscribers and optional callback.

        Args:
            event_type: SSE event name (e.g. 'stage_progress').
            data: Event payload dictionary.
        """
        # Fire optional callback (used by run_ingest.py for real-time JSON output)
        if self._stage_callback is not None:
            try:
                self._stage_callback(event_type, data)
            except Exception:
                pass

        message = {"event": event_type, "data": data}
        dead_queues: list[Queue] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
            except Exception:
                dead_queues.append(queue)
        for q in dead_queues:
            self._subscribers.remove(q)
