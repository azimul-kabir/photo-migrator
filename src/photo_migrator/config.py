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
class LibraryConfig:
    root: Path


@dataclass(frozen=True)
class ImportsConfig:
    default_directory: Path
    preserve_source_subdirectories: bool
    source_destinations: tuple[tuple[str, Path], ...]


@dataclass(frozen=True)
class ScanConfig:
    extensions: frozenset[str]
    exclude_directory_names: frozenset[str]
    exclude_filename_suffixes: tuple[str, ...]
    exclude_filename_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class KeeperPreferences:
    prefer_higher_source_priority: bool = True
    prefer_human_readable_filename: bool = True
    prefer_non_uuid_filename: bool = True
    prefer_shorter_relative_path: bool = True
    prefer_complete_metadata: bool = True
    prefer_relationship_complete_asset: bool = True


@dataclass(frozen=True)
class PlanningConfig:
    destination_root: Path
    date_fallback: str
    naming_template: str
    preserve_source_subdirectories: bool
    case_insensitive_destination: bool
    max_filename_length: int
    keeper_preferences: KeeperPreferences
    source_overrides: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class MetadataDateRecoveryConfig:
    """Conservative capture-date recovery policy."""

    min_confidence: int = 95
    allow_partial_dates: bool = False
    allow_estimated_dates: bool = False
    preserve_filesystem_times: bool = True
    max_neighbor_sequence_gap: int = 5
    max_neighbor_time_gap_hours: int = 24
    reasonable_year_min: int = 1980
    reasonable_year_max: int | None = None
    workers: int = 1


@dataclass(frozen=True)
class Config:
    sources: tuple[SourceConfig, ...]
    scan: ScanConfig
    planning: PlanningConfig | None = None
    library: LibraryConfig | None = None
    imports: ImportsConfig | None = None
    metadata_date_recovery: MetadataDateRecoveryConfig = MetadataDateRecoveryConfig()


SUPPORTED_TEMPLATE_FIELDS = frozenset(
    {
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "timestamp",
        "original_name",
        "stem",
        "extension",
        "source_name",
        "asset_id",
    }
)


def _planning(raw: Any, sources: list[SourceConfig]) -> PlanningConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("planning must be a table")
    destination_value = raw.get("destination_root")
    if (
        not isinstance(destination_value, str)
        or not Path(destination_value).expanduser().is_absolute()
    ):
        raise ConfigError("planning.destination_root must be absolute")
    destination = Path(destination_value).expanduser().resolve(strict=False)
    for source in sources:
        if destination == source.path or destination.is_relative_to(source.path):
            raise ConfigError("destination_root must not equal or be inside a source root")
        if source.path.is_relative_to(destination):
            raise ConfigError("source root must not be inside destination_root")
    template = raw.get("naming_template", "{year}/{year}-{month}/{timestamp}_{original_name}")
    if not isinstance(template, str) or not template:
        raise ConfigError("planning.naming_template must be a non-empty string")
    import string

    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    except ValueError as exc:
        raise ConfigError(f"invalid naming template: {exc}") from exc
    unsupported = fields - SUPPORTED_TEMPLATE_FIELDS
    if unsupported:
        raise ConfigError(f"unsupported naming template fields: {', '.join(sorted(unsupported))}")
    maximum = raw.get("max_filename_length", 180)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 16 <= maximum <= 255:
        raise ConfigError("planning.max_filename_length must be between 16 and 255")
    fallback = raw.get("date_fallback", "undated")
    if (
        not isinstance(fallback, str)
        or not fallback.strip()
        or fallback in {".", ".."}
        or "/" in fallback
        or "\\" in fallback
    ):
        raise ConfigError("planning.date_fallback must be a safe directory name")
    preferences_raw = raw.get("keeper_preferences", {})
    overrides_raw = raw.get("source_overrides", {})
    if not isinstance(preferences_raw, dict) or not isinstance(overrides_raw, dict):
        raise ConfigError("planning preference and override values must be tables")
    for key, value in overrides_raw.items():
        if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError("planning.source_overrides must map names to integers")
    defaults = KeeperPreferences()
    values: dict[str, bool] = {}
    for name in defaults.__dataclass_fields__:
        value = preferences_raw.get(name, getattr(defaults, name))
        if not isinstance(value, bool):
            raise ConfigError(f"planning.keeper_preferences.{name} must be boolean")
        values[name] = value
    return PlanningConfig(
        destination,
        fallback,
        template,
        bool(raw.get("preserve_source_subdirectories", False)),
        bool(raw.get("case_insensitive_destination", True)),
        maximum,
        KeeperPreferences(**values),
        tuple(sorted(overrides_raw.items())),
    )


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
        name, source_path, priority = item.get("name"), item.get("path"), item.get("priority", 0)
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
        if raw.get("library") is None and "CleanLibrary" in resolved.parts:
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

    library = None
    raw_library = raw.get("library")
    if raw_library is not None:
        if not isinstance(raw_library, dict) or not isinstance(raw_library.get("root"), str):
            raise ConfigError("library.root must be an absolute existing directory")
        library_path = Path(raw_library["root"]).expanduser()
        if not library_path.is_absolute():
            raise ConfigError("library.root must be absolute")
        try:
            library_root = library_path.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"library root does not exist: {library_path}") from exc
        if not library_root.is_dir():
            raise ConfigError(f"library root is not a directory: {library_root}")
        if any(s.path == library_root or s.path.is_relative_to(library_root) for s in sources):
            raise ConfigError("candidate sources must be outside library.root")
        library = LibraryConfig(library_root)

    imports = None
    raw_imports = raw.get("imports")
    if raw_imports is not None:
        if not isinstance(raw_imports, dict):
            raise ConfigError("imports must be a table")
        default = _safe_relative_directory(
            raw_imports.get("default_directory", "Camera Imports/Unsorted"),
            "imports.default_directory",
        )
        destinations = raw_imports.get("source_destinations", {})
        if not isinstance(destinations, dict):
            raise ConfigError("imports.source_destinations must be a table")
        mapped = tuple(
            sorted(
                (name, _safe_relative_directory(value, f"imports.source_destinations.{name}"))
                for name, value in destinations.items()
            )
        )
        imports = ImportsConfig(
            default, bool(raw_imports.get("preserve_source_subdirectories", False)), mapped
        )

    recovery_raw = raw.get("metadata_date_recovery", {})
    if not isinstance(recovery_raw, dict):
        raise ConfigError("metadata_date_recovery must be a table")
    defaults = MetadataDateRecoveryConfig()
    recovery_values: dict[str, Any] = {}
    for field in defaults.__dataclass_fields__:
        recovery_values[field] = recovery_raw.get(field, getattr(defaults, field))
    for field in ("allow_partial_dates", "allow_estimated_dates", "preserve_filesystem_times"):
        if not isinstance(recovery_values[field], bool):
            raise ConfigError(f"metadata_date_recovery.{field} must be boolean")
    for field in (
        "min_confidence",
        "max_neighbor_sequence_gap",
        "max_neighbor_time_gap_hours",
        "reasonable_year_min",
        "workers",
    ):
        if (
            not isinstance(recovery_values[field], int)
            or isinstance(recovery_values[field], bool)
            or recovery_values[field] < 1
        ):
            raise ConfigError(f"metadata_date_recovery.{field} must be a positive integer")
    year_max = recovery_values["reasonable_year_max"]
    if year_max is not None and (not isinstance(year_max, int) or isinstance(year_max, bool)):
        raise ConfigError("metadata_date_recovery.reasonable_year_max must be an integer")
    if recovery_values["min_confidence"] > 100:
        raise ConfigError("metadata_date_recovery.min_confidence must be at most 100")

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
        planning=_planning(raw.get("planning"), sources),
        library=library,
        imports=imports,
        metadata_date_recovery=MetadataDateRecoveryConfig(**recovery_values),
    )


def _safe_relative_directory(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ConfigError(f"{field} must be a contained relative path")
    return path
