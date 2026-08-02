"""Filesystem primitives which never follow source or destination symlinks."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_relative(relative: Path) -> None:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"unsafe destination relative path: {relative}")


def safe_destination(root: Path, relative: Path, create_parents: bool = False) -> Path:
    """Lexically contain a path and lstat every existing parent component."""
    validate_relative(relative)
    root = root.absolute()
    destination = root.joinpath(*relative.parts)
    if not contained(destination, root):
        raise ValueError("destination escaped configured root")
    current = root
    if current.exists() and (stat.S_ISLNK(current.lstat().st_mode) or not current.is_dir()):
        raise ValueError(f"unsafe destination root: {root}")
    if create_parents and not current.exists():
        current.mkdir()
    for part in relative.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if create_parents:
                current.mkdir()
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"unsafe destination parent component: {current}")
    # Existing components have now proved that realpath cannot escape through a symlink.
    if root.exists() and not contained(destination.parent.resolve(), root.resolve()):
        raise ValueError("resolved destination escaped configured root")
    return destination


def verify_source(
    path: Path, root: Path, size: int, mtime_ns: int | None, digest: str
) -> tuple[int, str]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("source is not a regular non-symlink file")
    if not contained(path.absolute(), root.absolute()) or not contained(
        path.resolve(), root.resolve()
    ):
        raise ValueError("source escaped configured source root")
    if info.st_size != size:
        raise ValueError(f"source size changed: expected {size}, observed {info.st_size}")
    if mtime_ns is not None and info.st_mtime_ns != mtime_ns:
        raise ValueError(f"source mtime changed: expected {mtime_ns}, observed {info.st_mtime_ns}")
    actual, actual_size = sha256_file(path)
    if actual_size != size or actual != digest:
        raise ValueError(f"source SHA-256 mismatch: expected {digest}, observed {actual}")
    return info.st_mtime_ns, actual


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
