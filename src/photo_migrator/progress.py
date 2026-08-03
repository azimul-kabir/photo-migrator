"""Low-overhead progress reporting for long-running operations."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def format_bytes(value: int) -> str:
    """Format a byte count with an IEC unit and useful precision."""
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    for index, unit in enumerate(units):
        if abs(amount) < 1024 or index == len(units) - 1:
            if index == 0:
                return f"{int(amount):,} {unit}"
            precision = 1 if amount >= 10 else 2
            return f"{amount:,.{precision}f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def format_duration(seconds: float) -> str:
    """Format a duration compactly, without implying second-level ETA accuracy."""
    total_minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if total_minutes:
        return f"{total_minutes}m"
    return "<1m"


@dataclass
class ProgressTracker:
    """Maintain indexing counters in memory and periodically log a snapshot."""

    logger: logging.Logger
    total_assets: int
    total_bytes: int
    previously_hashed: int
    previously_hashed_bytes: int
    clock: Callable[[], float] = time.monotonic
    file_interval: int = 500
    time_interval: float = 30.0
    newly_hashed: int = 0
    failed: int = 0
    bytes_processed: int = 0
    _started_at: float = field(init=False)
    _last_logged_at: float = field(init=False)
    _last_logged_files: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()
        self._last_logged_at = self._started_at

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    @property
    def attempted(self) -> int:
        return self.newly_hashed + self.failed

    @property
    def completed_assets(self) -> int:
        return self.previously_hashed + self.newly_hashed

    @property
    def completed_bytes(self) -> int:
        return min(self.total_bytes, self.previously_hashed_bytes + self.bytes_processed)

    @property
    def average_bytes_per_second(self) -> float:
        elapsed = self.elapsed
        return self.bytes_processed / elapsed if elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        speed = self.average_bytes_per_second
        if speed <= 0:
            return None
        return max(0.0, self.total_bytes - self.completed_bytes) / speed

    def record_success(self, size: int) -> None:
        self.newly_hashed += 1
        self.bytes_processed += size
        self._maybe_log()

    def record_failure(self) -> None:
        self.failed += 1
        self._maybe_log()

    def _maybe_log(self) -> None:
        now = self.clock()
        if (
            self.attempted - self._last_logged_files < self.file_interval
            and now - self._last_logged_at < self.time_interval
        ):
            return
        self.logger.info(self.progress_message())
        self._last_logged_files = self.attempted
        self._last_logged_at = now

    def progress_message(self) -> str:
        percent = 100.0 * self.completed_bytes / self.total_bytes if self.total_bytes else 100.0
        eta = self.eta_seconds
        eta_text = format_duration(eta) if eta is not None else "calculating"
        return (
            f"Indexed {self.completed_assets:,} / {self.total_assets:,} canonical assets "
            f"({percent:.1f}%) | {format_bytes(self.completed_bytes)} / "
            f"{format_bytes(self.total_bytes)} | "
            f"{format_bytes(round(self.average_bytes_per_second))}/s | ETA {eta_text}"
        )

    def final_summary(self) -> str:
        return (
            "Canonical indexing completed\n\n"
            "Assets:\n"
            f"  Total ............ {self.total_assets:,}\n"
            f"  Newly hashed ..... {self.newly_hashed:,}\n"
            f"  Previously hashed  {self.previously_hashed:,}\n"
            f"  Failed ........... {self.failed:,}\n\n"
            "Data:\n"
            f"  Processed ........ {format_bytes(self.completed_bytes)}\n"
            f"  Elapsed .......... {format_duration(self.elapsed)}\n"
            f"  Average speed .... {format_bytes(round(self.average_bytes_per_second))}/s"
        )
