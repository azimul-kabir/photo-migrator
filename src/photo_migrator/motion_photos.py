"""Bounded detectors for self-contained Google and Samsung Motion Photos."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_READ_LIMIT = 2 * 1024 * 1024
_GOOGLE_FLAG = re.compile(rb"(?:GCamera:MotionPhoto|GCamera:MicroVideo)\s*=\s*[\"']1[\"']")
_GOOGLE_OFFSET = re.compile(rb"GCamera:MicroVideoOffset\s*=\s*[\"'](\d+)[\"']")
_ITEM_LENGTH = re.compile(rb"Item:Length\s*=\s*[\"'](\d+)[\"']")
_SAMSUNG_MARKERS = (b"MotionPhoto_Data", b"Samsung_Motion_Photo", b"SEF\x00")


@dataclass(frozen=True)
class MotionDetection:
    kind: str | None = None
    status: str | None = None
    offset: int | None = None
    evidence: str = ""
    error: str | None = None


def _regions(path: Path, size: int) -> bytes:
    with path.open("rb") as stream:
        head = stream.read(min(size, _READ_LIMIT))
        if size <= _READ_LIMIT:
            return head
        stream.seek(max(0, size - _READ_LIMIT))
        return head + stream.read(_READ_LIMIT)


def inspect_motion_photo(path: Path, expected_size: int) -> MotionDetection:
    """Inspect bounded head/tail regions and validate all offsets against file size."""
    try:
        before = path.stat()
        data = _regions(path, before.st_size)
        after = path.stat()
    except OSError as exc:
        return MotionDetection(error=f"motion metadata read failed: {exc}")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return MotionDetection(error="file changed during relationship analysis")
    size = after.st_size
    if size != expected_size:
        return MotionDetection(error="file changed since inventory scan")
    google = _GOOGLE_FLAG.search(data)
    offset_match = _GOOGLE_OFFSET.search(data) or _ITEM_LENGTH.search(data)
    if google or offset_match:
        if not offset_match:
            return MotionDetection(
                "google",
                "invalid",
                evidence="self_contained=true;google_motion_marker",
                error="motion photo offset is missing",
            )
        offset = int(offset_match.group(1))
        if offset <= 0 or offset >= size:
            return MotionDetection(
                "google",
                "invalid",
                offset,
                f"self_contained=true;offset={offset}",
                "motion photo offset outside file boundaries",
            )
        return MotionDetection(
            "google", "active", offset, f"self_contained=true;offset={offset};source=xmp"
        )
    for marker in _SAMSUNG_MARKERS:
        position = data.find(marker)
        if position >= 0:
            # Samsung's payload begins at or immediately after the explicit marker.  A marker
            # with no bytes after it is corrupt, not a relationship.
            absolute = max(0, size - _READ_LIMIT) + position if size > _READ_LIMIT else position
            offset = absolute + len(marker)
            if offset >= size:
                return MotionDetection(
                    "samsung",
                    "invalid",
                    offset,
                    f"self_contained=true;marker={marker.decode('ascii', 'replace')}",
                    "Samsung marker has no payload inside file boundaries",
                )
            return MotionDetection(
                "samsung",
                "active",
                offset,
                f"self_contained=true;marker={marker.decode('ascii', 'replace')};offset={offset}",
            )
    return MotionDetection()
