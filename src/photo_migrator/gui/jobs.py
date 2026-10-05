"""Run incremental-import steps one at a time on a background thread for the GUI."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from photo_migrator.config import load_config
from photo_migrator.database import Database, utc_now
from photo_migrator.incremental import IncrementalImporter
from photo_migrator.progress import ProgressSnapshot

LOGGER = logging.getLogger(__name__)

ACTIONS = (
    "library-index",
    "library-resume",
    "import-scan",
    "import-plan",
    "import-dry-run",
    "import-run",
)
PLAN_ACTIONS = frozenset({"import-dry-run", "import-run"})


class JobCancelled(Exception):
    """Raised from the progress listener to stop a job at its next checkpoint."""


class JobConflict(Exception):
    """Raised when a job is requested while another one is still running."""


@dataclass
class Job:
    id: int
    action: str
    plan_id: int | None
    started_at: str
    status: str = "running"
    finished_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    progress: ProgressSnapshot | None = None
    cancel_requested: bool = False
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=400))

    def to_json(self) -> dict[str, Any]:
        progress = self.progress
        return {
            "id": self.id,
            "action": self.action,
            "plan_id": self.plan_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "progress": None
            if progress is None
            else {
                "phase": progress.phase,
                "phase_name": progress.phase_name,
                "completed_items": progress.completed_items,
                "total_items": progress.total_items,
                "failed": progress.failed,
                "completed_bytes": progress.completed_bytes,
                "total_bytes": progress.total_bytes,
                "bytes_per_second": progress.bytes_per_second,
                "elapsed_seconds": progress.elapsed_seconds,
                "eta_seconds": progress.eta_seconds,
                "current_item": progress.current_item,
                "message": progress.message,
            },
            "logs": list(self.logs),
        }


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job) -> None:
        super().__init__(logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        for line in self.format(record).splitlines() or [""]:
            self.job.logs.append(line)


class JobRunner:
    """Own the single background job; every job opens its own SQLite connection."""

    def __init__(self, database_path: Path, config_path: Path) -> None:
        self.database_path, self.config_path = database_path, config_path
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None
        self._next_id = 1

    def current(self) -> dict[str, Any] | None:
        job = self._job
        return job.to_json() if job is not None else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, action: str, plan_id: int | None = None) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"unknown action: {action}")
        if action in PLAN_ACTIONS and plan_id is None:
            raise ValueError(f"{action} requires a plan_id")
        with self._lock:
            if self.running:
                raise JobConflict("another job is still running")
            self._preflight(action, plan_id)
            job = Job(self._next_id, action, plan_id, utc_now())
            self._next_id += 1
            self._cancel.clear()
            self._job = job
            self._thread = threading.Thread(
                target=self._run, args=(job,), name=f"photo-migrator-{action}", daemon=True
            )
            self._thread.start()
        return job.to_json()

    def cancel(self) -> bool:
        job = self._job
        if job is None or not self.running:
            return False
        job.cancel_requested = True
        self._cancel.set()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.running

    def _preflight(self, action: str, plan_id: int | None) -> None:
        """Refuse a real import unless the same plan has a finished dry run."""
        if action not in PLAN_ACTIONS:
            return
        connection = sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            plan = connection.execute(
                "SELECT status FROM import_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise ValueError(f"import plan {plan_id} does not exist")
            if plan[0] != "ready":
                raise ValueError(f"import plan {plan_id} is {plan[0]}; create a new plan")
            if action == "import-run":
                dry_run = connection.execute(
                    """SELECT 1 FROM import_runs WHERE plan_id=? AND dry_run=1
                    AND status IN ('completed','completed_with_errors')""",
                    (plan_id,),
                ).fetchone()
                if dry_run is None:
                    raise ValueError("run a dry run of this plan before importing")
        finally:
            connection.close()

    def _listener(self, job: Job) -> Any:
        def listen(snapshot: ProgressSnapshot) -> None:
            job.progress = snapshot
            if self._cancel.is_set():
                raise JobCancelled

        return listen

    def _run(self, job: Job) -> None:
        package_logger = logging.getLogger("photo_migrator")
        handler = _JobLogHandler(job)
        previous_level = package_logger.level
        if package_logger.getEffectiveLevel() > logging.INFO:
            package_logger.setLevel(logging.INFO)
        package_logger.addHandler(handler)
        try:
            config = load_config(self.config_path)
            with Database(self.database_path) as database:
                database.initialize()
                importer = IncrementalImporter(database, config, self._listener(job))
                job.result = self._execute(importer, job)
            job.status = "succeeded"
        except JobCancelled:
            job.status = "cancelled"
            LOGGER.warning(
                "%s cancelled; completed work is saved and the next run resumes it", job.action
            )
        except Exception as exc:
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            LOGGER.error("%s failed: %s", job.action, job.error)
        finally:
            job.finished_at = utc_now()
            package_logger.removeHandler(handler)
            package_logger.setLevel(previous_level)

    @staticmethod
    def _execute(importer: IncrementalImporter, job: Job) -> dict[str, Any]:
        if job.action in {"library-index", "library-resume"}:
            scan = importer.library_index(resume=job.action == "library-resume")
            return {"errors": len(scan.errors), "error_samples": scan.errors[:20]}
        if job.action == "import-scan":
            scan = importer.import_scan()
            return {
                "indexed": scan.indexed,
                "errors": len(scan.errors),
                "error_samples": scan.errors[:20],
            }
        if job.action == "import-plan":
            return {"plan_id": importer.plan()}
        assert job.plan_id is not None
        dry_run = job.action == "import-dry-run"
        return {"run_id": importer.run(job.plan_id, dry_run=dry_run, confirm=not dry_run)}
