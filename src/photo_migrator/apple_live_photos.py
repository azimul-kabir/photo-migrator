"""Bounded, read-only Apple Live Photo identifier inspection."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDENTIFIER_KEYS = (
    "com.apple.quicktime.content.identifier",
    "content.identifier",
    "assetidentifier",
    "asset identifier",
)
_UUID = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,40}")


@dataclass(frozen=True)
class AppleIdentifier:
    value: str | None = None
    source: str | None = None
    error: str | None = None


def normalize_identifier(value: object) -> str | None:
    text = str(value).strip().strip("\x00{}")
    return text.casefold() or None


def inspect_image(path: Path, maximum_bytes: int = 1024 * 1024) -> AppleIdentifier:
    """Find common identifier keys without decoding or loading the whole image."""
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum_bytes)
    except OSError as exc:
        return AppleIdentifier(error=f"image metadata read failed: {exc}")
    lowered = data.lower()
    for key in IDENTIFIER_KEYS:
        position = lowered.find(key.encode())
        if position >= 0:
            match = _UUID.search(data, position, min(len(data), position + 512))
            if match:
                return AppleIdentifier(
                    normalize_identifier(match.group().decode("ascii", "replace")),
                    f"image:{key}",
                )
    return AppleIdentifier()


def inspect_video(path: Path, ffprobe: str, timeout: float = 15.0) -> AppleIdentifier:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream_tags",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return AppleIdentifier(error=f"ffprobe not found: {ffprobe}")
    except subprocess.TimeoutExpired:
        return AppleIdentifier(error="ffprobe timed out")
    except OSError as exc:
        return AppleIdentifier(error=f"ffprobe failed: {exc}")
    if completed.returncode:
        return AppleIdentifier(error=f"ffprobe failed: {completed.stderr.strip()}")
    try:
        payload: Any = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        return AppleIdentifier(error=f"malformed ffprobe output: {exc}")
    tag_sets = [payload.get("format", {}).get("tags", {})]
    tag_sets.extend(stream.get("tags", {}) for stream in payload.get("streams", []))
    for tags in tag_sets:
        for key, value in sorted(tags.items()):
            lowered = key.casefold()
            if any(name in lowered for name in IDENTIFIER_KEYS):
                return AppleIdentifier(normalize_identifier(value), f"video:{key}")
    return AppleIdentifier()


def normalize_stem(stem: str) -> str:
    """Normalize only Apple's documented edited IMG_E#### naming convention."""
    normalized = stem.casefold()
    match = re.fullmatch(r"img_e(\d+)", normalized)
    return f"img_{match.group(1)}" if match else normalized
