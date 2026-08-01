"""Configuration loading and validation."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

tomllib: Any = importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")


class ConfigError(ValueError):
    """Raised when a configuration is unsafe or malformed."""


@dataclass(frozen=True)
class SourceConfig:
    name: str
    path: Path
    priority: int


@dataclass(frozen=True)
class ScanConfig:
    extensions: frozenset[str]
    exclude_directory_names: frozenset[str]
    exclude_filename_suffixes: tuple[str, ...]
    exclude_filename_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    sources: tuple[SourceConfig, ...]
    scan: ScanConfig


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field} must be a list of strings")
    return value


def load_config(path: Path) -> Config:
    """Load TOML and resolve and validate all source roots exactly once."""
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc

    raw_sources = raw.get("sources")
    raw_scan = raw.get("scan")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("sources must be a non-empty array of tables")
    if not isinstance(raw_scan, dict):
        raise ConfigError("scan must be a table")

    sources: list[SourceConfig] = []
    seen_paths: set[Path] = set()
    seen_names: set[str] = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            raise ConfigError("each source must be a table")
        name, source_path, priority = item.get("name"), item.get("path"), item.get("priority")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("source name must be a non-empty string")
        if name in seen_names:
            raise ConfigError(f"duplicate source name: {name}")
        if not isinstance(source_path, str) or not source_path:
            raise ConfigError(f"source {name} path must be a non-empty string")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ConfigError(f"source {name} priority must be an integer")
        candidate = Path(source_path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"source {name} does not exist: {candidate}") from exc
        if not resolved.is_dir():
            raise ConfigError(f"source {name} is not a directory: {resolved}")
        if "CleanLibrary" in resolved.parts:
            raise ConfigError(f"source {name} is under reserved CleanLibrary: {resolved}")
        if resolved in seen_paths:
            raise ConfigError(f"duplicate source path: {resolved}")
        seen_names.add(name)
        seen_paths.add(resolved)
        sources.append(SourceConfig(name=name, path=resolved, priority=priority))

    extensions = _string_list(raw_scan.get("extensions"), "scan.extensions")
    normalized_extensions: set[str] = set()
    for extension in extensions:
        if not extension.startswith(".") or extension == ".":
            raise ConfigError(f"invalid extension: {extension}")
        normalized_extensions.add(extension.lower())
    if not normalized_extensions:
        raise ConfigError("scan.extensions must not be empty")

    return Config(
        sources=tuple(sources),
        scan=ScanConfig(
            extensions=frozenset(normalized_extensions),
            exclude_directory_names=frozenset(
                _string_list(raw_scan.get("exclude_directory_names", []), "exclude_directory_names")
            ),
            exclude_filename_suffixes=tuple(
                _string_list(
                    raw_scan.get("exclude_filename_suffixes", []), "exclude_filename_suffixes"
                )
            ),
            exclude_filename_prefixes=tuple(
                _string_list(
                    raw_scan.get("exclude_filename_prefixes", []), "exclude_filename_prefixes"
                )
            ),
        ),
    )
