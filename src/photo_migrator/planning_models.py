"""Immutable values exchanged by the planning coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlanningAsset:
    asset_id: int
    source_name: str
    source_priority: int
    absolute_path: Path
    relative_path: Path
    filename: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str | None
    captured_at: str | None
    camera_make: str | None
    camera_model: str | None
    relationship_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AssetBundle:
    bundle_key: str
    member_asset_ids: tuple[int, ...]
    bundle_type: str
    primary_asset_id: int
    confidence: float = 1.0
    evidence: str = ""
    status: str = "active"


@dataclass(frozen=True)
class KeeperDecision:
    duplicate_group_key: str
    keeper_asset_id: int
    skipped_asset_ids: tuple[int, ...]
    score: tuple[object, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlannedItem:
    bundle_key: str
    primary_asset_id: int
    member_asset_ids: tuple[int, ...]
    action: str
    destination_relative_path: str | None
    status: str
    reason: str
    role: str = "primary"
    original_destination_relative_path: str | None = None
