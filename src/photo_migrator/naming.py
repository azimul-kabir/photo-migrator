"""Deterministic, source-read-only destination naming."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from photo_migrator.config import PlanningConfig
from photo_migrator.planning_models import PlanningAsset

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def sanitize_component(value: str, maximum: int, extension: str = "") -> str:
    """Sanitize one path component, preserving Unicode and the extension."""
    value = _SPACE.sub(" ", _CONTROL.sub("", value.replace("/", "_").replace("\\", "_"))).strip()
    if value in {"", ".", ".."}:
        value = "unnamed"
    if len(value) <= maximum:
        return value
    suffix = extension if extension and value.lower().endswith(extension.lower()) else ""
    return value[: max(1, maximum - len(suffix))] + suffix


def destination_path(asset: PlanningAsset, config: PlanningConfig) -> Path:
    captured: datetime | None = None
    if asset.captured_at:
        try:
            captured = datetime.fromisoformat(asset.captured_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            captured = None
    stem = Path(asset.filename).stem
    values = {
        "year": captured.strftime("%Y") if captured else config.date_fallback,
        "month": captured.strftime("%m") if captured else "00",
        "day": captured.strftime("%d") if captured else "00",
        "hour": captured.strftime("%H") if captured else "00",
        "minute": captured.strftime("%M") if captured else "00",
        "second": captured.strftime("%S") if captured else "00",
        "timestamp": captured.strftime("%Y%m%d_%H%M%S") if captured else "undated",
        "original_name": asset.filename,
        "stem": stem,
        "extension": asset.extension,
        "source_name": asset.source_name,
        "asset_id": str(asset.asset_id),
    }
    rendered = config.naming_template.format(**values)
    raw = Path(rendered)
    if raw.is_absolute() or not raw.parts or any(part in {"", ".", ".."} for part in raw.parts):
        raise ValueError("unsafe template output")
    parts = [
        sanitize_component(
            part, config.max_filename_length, asset.extension if index == len(raw.parts) - 1 else ""
        )
        for index, part in enumerate(raw.parts)
    ]
    result = Path(*parts)
    resolved = (config.destination_root / result).resolve(strict=False)
    if resolved == config.destination_root or not resolved.is_relative_to(config.destination_root):
        raise ValueError("destination path escapes destination root")
    return result


def collision_key(path: Path, case_insensitive: bool) -> str:
    normalized = unicodedata.normalize("NFC", path.as_posix())
    return normalized.casefold() if case_insensitive else normalized


def disambiguate(path: Path, asset_id: int, maximum: int) -> Path:
    suffix = f"__a{asset_id}"
    extension = path.suffix
    room = maximum - len(extension) - len(suffix)
    if room < 1:
        raise ValueError("filename too short for collision suffix")
    return path.with_name(path.stem[:room] + suffix + extension)
