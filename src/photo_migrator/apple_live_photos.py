"""Bounded, read-only Apple Live Photo identifier inspection."""

from __future__ import annotations

import importlib
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
_MAX_METADATA_BLOCKS = 64
_MAX_METADATA_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AppleIdentifier:
    value: str | None = None
    source: str | None = None
    error: str | None = None


def normalize_identifier(value: object) -> str | None:
    text = str(value).strip().strip("\x00{}")
    return text.casefold() or None


def _identifier_in_bytes(data: bytes, source: str) -> AppleIdentifier | None:
    lowered = data.lower()
    for key in IDENTIFIER_KEYS:
        position = lowered.find(key.encode())
        if position >= 0:
            match = _UUID.search(data, position, min(len(data), position + 512))
            if match:
                return AppleIdentifier(
                    normalize_identifier(match.group().decode("ascii", "replace")),
                    f"{source}:{key}",
                )
    return None


def _inspect_heif_metadata(path: Path) -> AppleIdentifier | None:
    """Inspect libheif metadata blocks without requesting decoded pixels."""
    try:
        pillow_heif = importlib.import_module("pillow_heif")
    except ImportError:
        return None
    try:
        heif = pillow_heif.open_heif(path, convert_hdr_to_8bit=False)
        info = getattr(heif, "info", {})
        blocks = info.get("metadata", ()) if isinstance(info, dict) else ()
        for index, block in enumerate(blocks):
            if index >= _MAX_METADATA_BLOCKS or not isinstance(block, dict):
                break
            payload = block.get("data")
            if not isinstance(payload, bytes):
                continue
            kind = str(block.get("type") or "unknown").casefold()
            found = _identifier_in_bytes(
                payload[:_MAX_METADATA_BLOCK_BYTES], f"image:heif_metadata:{kind}"
            )
            if found:
                return found
    except (OSError, ValueError, TypeError, SyntaxError):
        # Metadata extraction is opportunistic; the bounded byte scan remains available.
        return None
    return None


def inspect_image(path: Path, maximum_bytes: int = 1024 * 1024) -> AppleIdentifier:
    """Find common identifier keys without decoding or loading the whole image."""
    if path.suffix.casefold() in {".heic", ".heif"}:
        identifier = _inspect_heif_metadata(path)
        if identifier:
            return identifier
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum_bytes)
    except OSError as exc:
        return AppleIdentifier(error=f"image metadata read failed: {exc}")
    return _identifier_in_bytes(data, "image:bounded_file_metadata") or AppleIdentifier()


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
