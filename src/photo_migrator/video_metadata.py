"""Read-only ffprobe video metadata extraction."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from photo_migrator.metadata import AnalyzerResult, MediaMetadata


def _float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def parse_frame_rate(value: object) -> float | None:
    raw = str(value or "")
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else None
        return float(raw)
    except ValueError:
        return None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def parse_ffprobe(data: dict[str, Any]) -> MediaMetadata:
    raw_streams = data.get("streams")
    streams: list[dict[str, Any]] = (
        [stream for stream in raw_streams if isinstance(stream, dict)]
        if isinstance(raw_streams, list)
        else []
    )
    raw_format = data.get("format")
    format_data: dict[str, Any] = raw_format if isinstance(raw_format, dict) else {}
    video: dict[str, Any] = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), {}
    )
    audio: dict[str, Any] = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"), {}
    )
    raw_format_tags = format_data.get("tags")
    format_tags: dict[str, Any] = raw_format_tags if isinstance(raw_format_tags, dict) else {}
    raw_video_tags = video.get("tags")
    video_tags: dict[str, Any] = raw_video_tags if isinstance(raw_video_tags, dict) else {}
    captured = _timestamp(format_tags.get("creation_time"))
    source = "video_format_creation_time" if captured else "none"
    if not captured:
        captured = _timestamp(video_tags.get("creation_time"))
        source = "video_stream_creation_time" if captured else "none"
    bitrate = format_data.get("bit_rate") or video.get("bit_rate")
    try:
        normalized_bitrate = int(bitrate) if bitrate is not None else None
    except (TypeError, ValueError):
        normalized_bitrate = None
    return MediaMetadata(
        captured_at=captured,
        captured_at_source=source,
        width=video.get("width"),
        height=video.get("height"),
        duration_seconds=_float(format_data.get("duration") or video.get("duration")),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
        container_format=format_data.get("format_name"),
        frame_rate=parse_frame_rate(video.get("avg_frame_rate")),
        bitrate=normalized_bitrate,
        color_space=video.get("color_space"),
    )


class VideoAnalyzer:
    def __init__(self, ffprobe: str = "ffprobe", timeout: float = 30.0) -> None:
        self.ffprobe = ffprobe
        self.timeout = timeout

    def analyze(self, path: Path) -> AnalyzerResult:
        if path.suffix.lower() not in {".mov", ".mp4", ".m4v"}:
            return AnalyzerResult("unsupported", error=f"unsupported video format: {path.suffix}")
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
        try:
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except FileNotFoundError:
            return AnalyzerResult("failed", error=f"ffprobe not found: {self.ffprobe}")
        except subprocess.TimeoutExpired:
            return AnalyzerResult(
                "failed", error=f"ffprobe timed out after {self.timeout:g} seconds"
            )
        except OSError as exc:
            return AnalyzerResult("failed", error=f"ffprobe error: {type(exc).__name__}: {exc}")
        if process.returncode:
            detail = process.stderr.strip() or f"exit status {process.returncode}"
            return AnalyzerResult("failed", error=f"ffprobe failed: {detail}")
        try:
            data = json.loads(process.stdout)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON value is not an object")
            metadata = parse_ffprobe(data)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return AnalyzerResult("failed", error=f"malformed ffprobe JSON: {exc}")
        if metadata.width is None and metadata.duration_seconds is None:
            return AnalyzerResult("failed", error="ffprobe returned no usable video metadata")
        return AnalyzerResult("completed", metadata)
