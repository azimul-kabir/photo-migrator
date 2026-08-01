"""Read-only image metadata extraction with Pillow."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from photo_migrator.metadata import AnalyzerResult, MediaMetadata

Image: Any
UnidentifiedImageError: Any
try:
    Image = importlib.import_module("PIL.Image")
    UnidentifiedImageError = importlib.import_module("PIL").UnidentifiedImageError
except ImportError:
    Image = None
    UnidentifiedImageError = OSError

pillow_heif: Any
try:
    pillow_heif = importlib.import_module("pillow_heif")
except ImportError:  # optional decoder capability is reported per asset
    pillow_heif = None
else:
    pillow_heif.register_heif_opener()

HEIF_EXTENSIONS = {".heic", ".heif"}
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".dng"}


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    result = str(value).strip().strip("\x00")
    return result or None


def normalize_timestamp(value: object) -> str | None:
    """Normalize EXIF or ISO timestamps without inventing timezone information."""
    raw = _text(value)
    if not raw:
        return None
    candidates = (raw, raw.replace("Z", "+00:00"))
    for candidate in candidates:
        try:
            if len(candidate) >= 10 and candidate[4] == ":" and candidate[7] == ":":
                parsed = datetime.strptime(candidate, "%Y:%m:%d %H:%M:%S")
            else:
                parsed = datetime.fromisoformat(candidate)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _number(value: object) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        return float(Fraction(int(value[0]), int(value[1])))
    return float(cast(Any, value))  # Pillow IFDRational and ordinary numeric values


def gps_coordinate(value: object, reference: object, latitude: bool) -> float | None:
    """Convert a complete rational DMS coordinate, respecting its hemisphere."""
    try:
        parts = list(cast(Iterable[object], value))
        ref = (_text(reference) or "").upper()
        valid_refs = {"N", "S"} if latitude else {"E", "W"}
        if len(parts) != 3 or ref not in valid_refs:
            return None
        degrees, minutes, seconds = (_number(part) for part in parts)
        if degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
            return None
        result = degrees + minutes / 60 + seconds / 3600
        maximum = 90 if latitude else 180
        if result > maximum:
            return None
        return -result if ref in {"S", "W"} else result
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


class ImageAnalyzer:
    def analyze(self, path: Path) -> AnalyzerResult:
        suffix = path.suffix.lower()
        if suffix in HEIF_EXTENSIONS and pillow_heif is None:
            return AnalyzerResult("unsupported", error="HEIC/HEIF decoder unavailable")
        if suffix not in SUPPORTED_EXTENSIONS | HEIF_EXTENSIONS:
            return AnalyzerResult("unsupported", error=f"unsupported image format: {suffix}")
        if Image is None:
            return AnalyzerResult("unsupported", error="Pillow image decoder unavailable")
        try:
            with Image.open(path, mode="r") as image:
                width, height = image.size
                exif = image.getexif()
                # Nested GPS IFD is the reliable Pillow API; malformed IFDs remain per-file errors.
                gps: dict[int, Any] = {}
                if exif and 34853 in exif:
                    gps = dict(exif.get_ifd(34853))
                captured_at = None
                captured_source = "none"
                for tag, source in (
                    (36867, "exif_datetime_original"),
                    (36868, "exif_datetime_digitized"),
                    (306, "exif_datetime"),
                ):
                    captured_at = normalize_timestamp(exif.get(tag))
                    if captured_at:
                        captured_source = source
                        break
                latitude = gps_coordinate(gps.get(2), gps.get(1), True)
                longitude = gps_coordinate(gps.get(4), gps.get(3), False)
                # Coordinates are only useful as a valid pair.
                if latitude is None or longitude is None:
                    latitude = longitude = None
                altitude = None
                if gps.get(6) is not None:
                    try:
                        altitude = _number(gps[6]) * (-1 if gps.get(5) == 1 else 1)
                    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                        altitude = None
                metadata = MediaMetadata(
                    captured_at=captured_at,
                    captured_at_source=captured_source,
                    width=width,
                    height=height,
                    orientation=exif.get(274),
                    camera_make=_text(exif.get(271)),
                    camera_model=_text(exif.get(272)),
                    lens_model=_text(exif.get(42036)),
                    latitude=latitude,
                    longitude=longitude,
                    altitude=altitude,
                )
                return AnalyzerResult("completed", metadata)
        except UnidentifiedImageError as exc:
            return AnalyzerResult("failed", error=f"unreadable image: {exc}")
        except (OSError, ValueError, TypeError, SyntaxError) as exc:
            return AnalyzerResult(
                "failed", error=f"image metadata error: {type(exc).__name__}: {exc}"
            )
