"""Normalized media metadata and analyzer result types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class MediaMetadata:
    """Analyzer-independent, immutable metadata representation."""

    captured_at: str | None = None
    captured_at_source: str = "none"
    width: int | None = None
    height: int | None = None
    orientation: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    lens_model: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    duration_seconds: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    container_format: str | None = None
    frame_rate: float | None = None
    bitrate: int | None = None
    color_space: str | None = None


@dataclass(frozen=True)
class AnalyzerResult:
    """A structured result which contains no persistence behavior."""

    status: str
    metadata: MediaMetadata | None = None
    error: str | None = None


class MediaAnalyzer(Protocol):
    def analyze(self, path: Path) -> AnalyzerResult:
        """Read metadata from *path* without modifying it."""
        ...
