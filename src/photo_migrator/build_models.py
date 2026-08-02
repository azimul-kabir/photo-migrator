"""Immutable values exchanged by build workers and the SQLite coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildPlanItem:
    plan_item_id: int
    asset_id: int
    source_path: Path
    source_root: Path
    destination_relative_path: Path
    expected_sha256: str
    expected_size_bytes: int
    expected_mtime_ns: int | None
    action: str
    status: str
    bundle_key: str
    owned_by_build: bool = False


@dataclass(frozen=True)
class BuildOperationResult:
    plan_item_id: int
    asset_id: int
    source_path: Path
    destination_path: Path
    operation: str
    status: str
    bytes_written: int = 0
    source_verified: bool = False
    destination_verified: bool = False
    owned_by_build: bool = False
    actual_sha256: str | None = None
    actual_size_bytes: int | None = None
    observed_source_mtime_ns: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    build_item_id: int
    destination_path: Path
    status: str
    expected_sha256: str
    actual_sha256: str | None
    expected_size_bytes: int
    actual_size_bytes: int | None
    error: str | None = None


@dataclass(frozen=True)
class RollbackResult:
    build_item_id: int
    destination_path: Path
    status: str
    error: str | None = None
