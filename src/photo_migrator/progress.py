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


def format_duration(seconds: float, *, precise: bool = False) -> str:
    """Format a duration compactly, without implying second-level ETA accuracy."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s" if precise else f"{minutes}m"
    return f"{seconds}s" if precise else "<1m"


def _flush_logger(logger: logging.Logger) -> None:
    """Flush the handlers that can receive a record from *logger*."""
    current: logging.Logger | None = logger
    while current is not None:
        for handler in current.handlers:
            handler.flush()
        if not current.propagate:
            break
        current = current.parent


@dataclass
class ProgressTracker:
    """Keep arbitrary phase counters in memory and periodically log progress."""

    logger: logging.Logger
    phase_name: str
    total_items: int
    phase: str = ""
    verb: str = "Processed"
    total_bytes: int | None = None
    initial_items: int = 0
    initial_bytes: int = 0
    byte_based: bool = False
    clock: Callable[[], float] = time.monotonic
    file_interval: int = 500
    time_interval: float = 30.0
    succeeded: int = 0
    failed: int = 0
    bytes_processed: int = 0
    _started_at: float = field(init=False)
    _last_logged_at: float = field(init=False)
    _last_logged_files: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.phase:
            self.phase = self.phase_name
        self._started_at = self.clock()
        self._last_logged_at = self._started_at

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    @property
    def attempted(self) -> int:
        return self.succeeded + self.failed

    @property
    def completed_items(self) -> int:
        return self.initial_items + self.succeeded

    @property
    def completed_bytes(self) -> int:
        value = self.initial_bytes + self.bytes_processed
        return min(self.total_bytes, value) if self.total_bytes is not None else value

    @property
    def average_bytes_per_second(self) -> float:
        elapsed = self.elapsed
        return self.bytes_processed / elapsed if elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        if self.total_bytes is None:
            return None
        speed = self.average_bytes_per_second
        if speed <= 0:
            return None
        return max(0.0, self.total_bytes - self.completed_bytes) / speed

    def record_success(self, size: int = 0) -> None:
        self.succeeded += 1
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
        _flush_logger(self.logger)
        self._last_logged_files = self.attempted
        self._last_logged_at = now

    def progress_message(self) -> str:
        if self.byte_based:
            total_bytes = self.total_bytes or 0
            percent = 100.0 * self.completed_bytes / total_bytes if total_bytes else 100.0
            eta = self.eta_seconds
            eta_text = format_duration(eta) if eta is not None else "calculating"
            return (
                f"{self.phase} | {self.verb} {self.completed_items:,} / {self.total_items:,} "
                f"canonical assets ({percent:.1f}%) | {format_bytes(self.completed_bytes)} / "
                f"{format_bytes(total_bytes)} | "
                f"{format_bytes(round(self.average_bytes_per_second))}/s | ETA {eta_text}"
            )
        percent = 100.0 * self.attempted / self.total_items if self.total_items else 100.0
        return (
            f"{self.phase} | {self.verb} {self.attempted:,} / {self.total_items:,} "
            f"({percent:.1f}%) | {format_bytes(self.bytes_processed)} | "
            f"{format_duration(self.elapsed, precise=True)} elapsed"
        )
