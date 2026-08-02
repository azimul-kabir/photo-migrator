"""Crash-safe, deterministic writers for reports and metadata sidecars."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Callable, TextIO


def _place(destination: Path, write: Callable[[TextIO], None], encoding: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".photo-migrator-{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist the rename where directory descriptors are supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(destination: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *destination* with complete text content."""

    def write(stream: TextIO) -> None:
        stream.write(content)

    _place(destination, write, encoding)


def atomic_write_csv(
    destination: Path,
    rows: Iterable[Sequence[object]],
    headers: Sequence[str],
) -> None:
    """Write a deterministic RFC-style CSV report using LF line endings."""

    def write(stream: TextIO) -> None:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)

    _place(destination, write, "utf-8")


def atomic_write_json(destination: Path, payload: object) -> None:
    """Write sorted, indented JSON with a terminating newline."""

    def write(stream: TextIO) -> None:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")

    _place(destination, write, "utf-8")
