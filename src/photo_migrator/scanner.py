"""Read-only, metadata-only filesystem scanner."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from photo_migrator.config import Config, SourceConfig
from photo_migrator.database import Database

LOGGER = logging.getLogger(__name__)


@dataclass
class ScanResult:
    discovered: int = 0
    indexed: int = 0
    errors: list[str] = field(default_factory=list)


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _media_type(extension: str) -> str:
    return "video" if extension in {".mov", ".mp4", ".m4v"} else "image"


class Scanner:
    def __init__(self, database: Database, config: Config) -> None:
        self.database = database
        self.config = config

    def scan(self) -> ScanResult:
        """Scan all roots, leaving a durable run record even when scanning fails."""
        run_id = self.database.start_run(len(self.config.sources))
        result = ScanResult()
        try:
            for source in self.config.sources:
                self._scan_source(source, result)
        except BaseException as exc:
            message = f"scan failed: {type(exc).__name__}: {exc}"
            LOGGER.exception(message)
            self.database.finish_run(
                run_id, "failed", result.discovered, result.indexed, len(result.errors) + 1, message
            )
            raise
        status = "completed_with_errors" if result.errors else "completed"
        summary = "\n".join(result.errors) if result.errors else None
        self.database.finish_run(
            run_id, status, result.discovered, result.indexed, len(result.errors), summary
        )
        return result

    def _scan_source(self, source: SourceConfig, result: ScanResult) -> None:
        seen: set[str] = set()
        source_had_error = False
        pending = [source.path]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                source_had_error = True
                self._record_error(result, Path(directory), "scandir", exc)
                continue
            child_directories: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in self.config.scan.exclude_directory_names:
                            child_directories.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if any(
                        entry.name.startswith(p) for p in self.config.scan.exclude_filename_prefixes
                    ):
                        continue
                    if any(
                        entry.name.endswith(s) for s in self.config.scan.exclude_filename_suffixes
                    ):
                        continue
                    extension = path.suffix.lower()
                    if extension not in self.config.scan.extensions:
                        continue
                    result.discovered += 1
                    resolved = path.resolve(strict=False)
                    if not _contained(resolved, source.path):
                        raise ValueError(f"path escapes source root: {path}")
                    metadata = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    absolute = str(resolved)
                    relative = resolved.relative_to(source.path).as_posix()
                    self.database.upsert_asset(
                        {
                            "source_name": source.name,
                            "source_priority": source.priority,
                            "absolute_path": absolute,
                            "relative_path": relative,
                            "filename": entry.name,
                            "extension": extension,
                            "size_bytes": metadata.st_size,
                            "mtime_ns": metadata.st_mtime_ns,
                            "device_id": metadata.st_dev,
                            "inode": metadata.st_ino,
                            "media_type": _media_type(extension),
                        }
                    )
                    seen.add(absolute)
                    result.indexed += 1
                except (OSError, ValueError) as exc:
                    source_had_error = True
                    self._record_error(result, path, "stat/containment", exc)
            pending.extend(reversed(child_directories))
        if not source_had_error:
            self.database.mark_missing(source.name, seen)

    @staticmethod
    def _record_error(result: ScanResult, path: Path, operation: str, exc: Exception) -> None:
        message = f"{operation} error for {path}: {type(exc).__name__}: {exc}"
        LOGGER.error(message)
        result.errors.append(message)
