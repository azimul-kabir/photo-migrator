"""Deterministic planning coordinator; it never writes media or the destination."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from photo_migrator.config import Config, PlanningConfig
from photo_migrator.database import Database, utc_now
from photo_migrator.naming import collision_key, destination_path, disambiguate
from photo_migrator.planning_models import AssetBundle, KeeperDecision, PlannedItem, PlanningAsset


@dataclass(frozen=True)
class PlanResult:
    plan_id: int
    status: str
    candidates: int
    items: int
    collisions: int
    blocked: int
    ambiguous: int
    fingerprint: str


def _asset(row: sqlite3.Row) -> PlanningAsset:
    return PlanningAsset(
        int(row["id"]),
        str(row["source_name"]),
        int(row["source_priority"]),
        Path(row["absolute_path"]),
        Path(row["relative_path"]),
        str(row["filename"]),
        str(row["extension"]),
        str(row["media_type"]),
        int(row["size_bytes"] or 0),
        row["sha256"],
        row["captured_at"],
        row["camera_make"],
        row["camera_model"],
    )


def keeper_score(
    asset: PlanningAsset, relationship_complete: bool, config: PlanningConfig
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    """Return an ordered score (lower wins), plus an auditable explanation."""
    prefs = config.keeper_preferences
    override = dict(config.source_overrides).get(asset.source_name, asset.source_priority)
    uuid_name = bool(
        __import__("re").fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            Path(asset.filename).stem,
        )
    )
    human = any(character.isalpha() for character in Path(asset.filename).stem) and not uuid_name
    metadata = sum(
        value is not None for value in (asset.captured_at, asset.camera_make, asset.camera_model)
    )
    score: tuple[object, ...] = (
        -int(relationship_complete) if prefs.prefer_relationship_complete_asset else 0,
        -asset.source_priority if prefs.prefer_higher_source_priority else 0,
        -override,
        -int(human) if prefs.prefer_human_readable_filename else 0,
        int(uuid_name) if prefs.prefer_non_uuid_filename else 0,
        -metadata if prefs.prefer_complete_metadata else 0,
        len(asset.relative_path.as_posix()) if prefs.prefer_shorter_relative_path else 0,
        asset.absolute_path.as_posix(),
        asset.asset_id,
    )
    reasons = (
        f"relationship_complete={relationship_complete}",
        f"source_priority={asset.source_priority}",
        f"source_override={override}",
        f"human_readable={human}",
        f"uuid_filename={uuid_name}",
        f"metadata_fields={metadata}",
        f"relative_path_length={len(asset.relative_path.as_posix())}",
        f"lexical_path={asset.absolute_path.as_posix()}",
    )
    return score, reasons


class Planner:
    def __init__(self, database: Database, config: Config) -> None:
        if config.planning is None:
            raise ValueError("configuration requires a [planning] section")
        self.database, self.config, self.planning = database, config, config.planning

    def run(
        self,
        source: str | None = None,
        limit: int | None = None,
        supersede_draft: bool = False,
        include_orphans: bool = False,
        minimum_fallback_confidence: float = 0.75,
    ) -> PlanResult:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        if not 0 <= minimum_fallback_confidence <= 1:
            raise ValueError("minimum fallback confidence must be between 0 and 1")
        query = "SELECT * FROM assets WHERE scan_status='available'"
        parameters: list[object] = []
        if source:
            query += " AND source_name=?"
            parameters.append(source)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        assets = [_asset(row) for row in self.database.connection.execute(query, parameters)]
        return self._create(assets, supersede_draft, include_orphans, minimum_fallback_confidence)

    def _create(
        self, assets: list[PlanningAsset], supersede: bool, include_orphans: bool, confidence: float
    ) -> PlanResult:
        by_id = {asset.asset_id: asset for asset in assets}
        relationships = list(
            self.database.connection.execute(
                "SELECT * FROM asset_relationships ORDER BY relationship_type,primary_asset_id,id"
            )
        )
        bundles, related, conflicts = self._bundles(relationships, by_id, confidence)
        complete = {
            asset_id
            for bundle in bundles
            if bundle.status == "active"
            for asset_id in bundle.member_asset_ids
        }
        groups: dict[str, list[PlanningAsset]] = defaultdict(list)
        missing_hash = []
        for asset in assets:
            if asset.sha256 and self._hash_complete(asset.asset_id):
                groups[asset.sha256].append(asset)
            else:
                missing_hash.append(asset)
        decisions: list[KeeperDecision] = []
        keepers: set[int] = set()
        skipped: dict[int, int] = {}
        for digest in sorted(groups):
            members = groups[digest]
            ranked = sorted(
                (keeper_score(item, item.asset_id in complete, self.planning), item)
                for item in members
            )
            score, keeper = ranked[0]
            keepers.add(keeper.asset_id)
            skipped_ids = tuple(sorted(item.asset_id for item in members if item != keeper))
            skipped.update({item: keeper.asset_id for item in skipped_ids})
            decisions.append(
                KeeperDecision(digest, keeper.asset_id, skipped_ids, score[0], score[1])
            )
        items: list[PlannedItem] = []
        for asset in missing_hash:
            items.append(
                PlannedItem(
                    f"asset:{asset.asset_id}",
                    asset.asset_id,
                    (asset.asset_id,),
                    "blocked",
                    None,
                    "blocked",
                    "missing completed SHA-256 hash",
                )
            )
        bundle_by_asset = {
            member: bundle for bundle in bundles for member in bundle.member_asset_ids
        }
        for asset in assets:
            if asset in missing_hash or asset.asset_id in conflicts:
                continue
            bundle = bundle_by_asset.get(asset.asset_id)
            if asset.asset_id in skipped:
                items.append(
                    PlannedItem(
                        bundle.bundle_key if bundle else f"asset:{asset.asset_id}",
                        asset.asset_id,
                        (asset.asset_id,),
                        "skip_exact_duplicate",
                        None,
                        "planned",
                        f"exact SHA-256 duplicate of asset {skipped[asset.asset_id]}",
                        "duplicate_skipped",
                    )
                )
                continue
            if asset.asset_id not in keepers and asset.asset_id not in related:
                continue
            action = "keep" if asset.asset_id in keepers else "include_relationship_member"
            status, reason = "planned", "selected exact-content keeper"
            if bundle and bundle.status in {"ambiguous", "review", "orphan"}:
                action, status, reason = (
                    "review",
                    "ambiguous",
                    f"{bundle.bundle_type}: {bundle.status}",
                )
                if bundle.status == "orphan" and include_orphans:
                    action, status, reason = (
                        "keep",
                        "planned",
                        "standalone orphan explicitly included",
                    )
            try:
                path = destination_path(asset, self.planning).as_posix()
            except ValueError as exc:
                path = None
                action, status, reason = "blocked", "blocked", str(exc)
            items.append(
                PlannedItem(
                    bundle.bundle_key if bundle else f"asset:{asset.asset_id}",
                    asset.asset_id,
                    (asset.asset_id,),
                    action,
                    path,
                    status,
                    reason,
                    "relationship_member" if action == "include_relationship_member" else "primary",
                )
            )
        for asset_id in sorted(conflicts):
            asset = by_id[asset_id]
            items.append(
                PlannedItem(
                    f"conflict:{asset_id}",
                    asset_id,
                    (asset_id,),
                    "blocked",
                    None,
                    "blocked",
                    "asset belongs to conflicting active relationships",
                )
            )
        items, collisions = self._collisions(items)
        fingerprint = self._fingerprint(assets, relationships)
        ambiguous = sum(item.status == "ambiguous" for item in items)
        blocked = sum(item.status == "blocked" for item in items)
        status = "blocked" if blocked else ("draft" if ambiguous else "ready")
        return self._persist(
            status,
            assets,
            groups,
            bundles,
            decisions,
            items,
            collisions,
            fingerprint,
            supersede,
            by_id,
        )

    def _hash_complete(self, asset_id: int) -> bool:
        row = self.database.connection.execute(
            "SELECT hash_status,hash_error FROM assets WHERE id=?", (asset_id,)
        ).fetchone()
        return bool(row and row["hash_status"] == "completed" and not row["hash_error"])

    def _bundles(
        self, rows: list[sqlite3.Row], assets: dict[int, PlanningAsset], minimum: float
    ) -> tuple[list[AssetBundle], set[int], set[int]]:
        bundles, memberships = [], defaultdict(list)
        for row in rows:
            primary, secondary = int(row["primary_asset_id"]), row["secondary_asset_id"]
            members = tuple(
                value
                for value in (primary, int(secondary) if secondary else None)
                if value is not None and value in assets
            )
            if not members:
                continue
            status = str(row["status"])
            if (
                status == "active"
                and row["relationship_type"] == "filename_pair"
                and row["confidence"] < minimum
            ):
                status = "review"
            if status == "orphan":
                status = "orphan"
            if secondary is not None and int(secondary) not in assets:
                status = "review"
            bundle = AssetBundle(
                f"relationship:{row['id']}",
                members,
                str(row["relationship_type"]),
                primary,
                float(row["confidence"]),
                str(row["evidence"]),
                status,
            )
            bundles.append(bundle)
            if status == "active":
                for member in members:
                    memberships[member].append(bundle.bundle_key)
        conflicts = {key for key, value in memberships.items() if len(value) > 1}
        related = {member for bundle in bundles for member in bundle.member_asset_ids}
        return bundles, related, conflicts

    def _collisions(
        self, items: list[PlannedItem]
    ) -> tuple[list[PlannedItem], list[dict[str, str]]]:
        used: dict[str, int] = {}
        output = []
        records = []
        for item in sorted(items, key=lambda value: value.primary_asset_id):
            if not item.destination_relative_path:
                output.append(item)
                continue
            original = Path(item.destination_relative_path)
            final = original
            key = collision_key(final, self.planning.case_insensitive_destination)
            if key in used:
                try:
                    final = disambiguate(
                        original, item.primary_asset_id, self.planning.max_filename_length
                    )
                    key = collision_key(final, self.planning.case_insensitive_destination)
                    if key in used:
                        raise ValueError("collision suffix is not unique")
                    records.append(
                        {
                            "asset_id": str(item.primary_asset_id),
                            "original": original.as_posix(),
                            "resolved": final.as_posix(),
                            "status": "resolved",
                        }
                    )
                except ValueError as exc:
                    output.append(
                        PlannedItem(
                            item.bundle_key,
                            item.primary_asset_id,
                            item.member_asset_ids,
                            "blocked",
                            None,
                            "blocked",
                            str(exc),
                            item.role,
                            original.as_posix(),
                        )
                    )
                    records.append(
                        {
                            "asset_id": str(item.primary_asset_id),
                            "original": original.as_posix(),
                            "resolved": "",
                            "status": "blocked",
                        }
                    )
                    continue
            used[key] = item.primary_asset_id
            output.append(
                PlannedItem(
                    item.bundle_key,
                    item.primary_asset_id,
                    item.member_asset_ids,
                    item.action,
                    final.as_posix(),
                    "collision" if final != original else item.status,
                    item.reason,
                    item.role,
                    original.as_posix() if final != original else None,
                )
            )
        return output, records

    def _fingerprint(self, assets: list[PlanningAsset], relationships: list[sqlite3.Row]) -> str:
        value = {
            "assets": [(a.asset_id, a.sha256, a.source_priority) for a in assets],
            "relationships": [tuple(row) for row in relationships],
            "planning": asdict(self.planning),
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()

    def _persist(
        self,
        status: str,
        assets: list[PlanningAsset],
        groups: dict[str, list[PlanningAsset]],
        bundles: list[AssetBundle],
        decisions: list[KeeperDecision],
        items: list[PlannedItem],
        collisions: list[dict[str, str]],
        fingerprint: str,
        supersede: bool,
        by_id: dict[int, PlanningAsset],
    ) -> PlanResult:
        now = utc_now()
        duplicate_groups = sum(len(value) > 1 for value in groups.values())
        with self.database.transaction() as connection:
            if supersede:
                prior = connection.execute(
                    "SELECT id FROM migration_plans WHERE status='draft' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if prior:
                    connection.execute(
                        "UPDATE migration_plans SET status='superseded',updated_at=? WHERE id=?",
                        (now, prior["id"]),
                    )
            run = connection.execute(
                "INSERT INTO planning_runs(started_at,status,candidate_assets) VALUES (?,'running',?)",
                (now, len(assets)),
            )
            cursor = connection.execute(
                """INSERT INTO migration_plans(created_at,updated_at,status,destination_root,naming_template,config_snapshot,fingerprint,source_asset_count,unique_content_count,duplicate_group_count,bundle_count,planned_item_count,collision_count,ambiguous_count,blocked_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now,
                    now,
                    status,
                    str(self.planning.destination_root),
                    self.planning.naming_template,
                    json.dumps(asdict(self.planning), sort_keys=True, default=str),
                    fingerprint,
                    len(assets),
                    len(groups),
                    duplicate_groups,
                    len(bundles),
                    len(items),
                    len(collisions),
                    sum(i.status == "ambiguous" for i in items),
                    sum(i.status == "blocked" for i in items),
                ),
            )
            assert cursor.lastrowid is not None
            plan_id = int(cursor.lastrowid)
            item_ids: dict[int, int] = {}
            for item in items:
                row = connection.execute(
                    """INSERT INTO migration_plan_items(plan_id,bundle_key,primary_asset_id,action,status,destination_relative_path,original_destination_relative_path,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id,
                        item.bundle_key,
                        item.primary_asset_id,
                        item.action,
                        item.status,
                        item.destination_relative_path,
                        item.original_destination_relative_path,
                        item.reason,
                        now,
                        now,
                    ),
                )
                assert row.lastrowid is not None
                item_ids[item.primary_asset_id] = int(row.lastrowid)
                connection.execute(
                    "INSERT INTO migration_plan_item_assets VALUES (?,?,?)",
                    (row.lastrowid, item.primary_asset_id, item.role),
                )
            connection.execute(
                """UPDATE planning_runs SET finished_at=?,status=?,exact_duplicate_groups=?,bundles_created=?,keepers_selected=?,items_planned=?,collisions=?,ambiguous_items=?,blocked_items=? WHERE id=?""",
                (
                    now,
                    "completed_with_errors" if status == "blocked" else "completed",
                    duplicate_groups,
                    len(bundles),
                    len(decisions),
                    len(items),
                    len(collisions),
                    sum(i.status == "ambiguous" for i in items),
                    sum(i.status == "blocked" for i in items),
                    run.lastrowid,
                ),
            )
        self._reports(
            plan_id,
            status,
            assets,
            groups,
            bundles,
            decisions,
            items,
            collisions,
            by_id,
            fingerprint,
        )
        return PlanResult(
            plan_id,
            status,
            len(assets),
            len(items),
            len(collisions),
            sum(i.status == "blocked" for i in items),
            sum(i.status == "ambiguous" for i in items),
            fingerprint,
        )

    def _reports(
        self,
        plan_id: int,
        status: str,
        assets: list[PlanningAsset],
        groups: dict[str, list[PlanningAsset]],
        bundles: list[AssetBundle],
        decisions: list[KeeperDecision],
        items: list[PlannedItem],
        collisions: list[dict[str, str]],
        by_id: dict[int, PlanningAsset],
        fingerprint: str,
    ) -> None:
        directory = self.database.path.parent / "reports" / f"plan_{plan_id}"
        directory.mkdir(parents=True, exist_ok=False)
        duplicate_bytes = sum(
            by_id[item].size_bytes for decision in decisions for item in decision.skipped_asset_ids
        )
        lines = {
            "plan ID": plan_id,
            "plan status": status,
            "destination root": self.planning.destination_root,
            "candidate assets": len(assets),
            "unique-content count": len(groups),
            "duplicate groups": sum(len(v) > 1 for v in groups.values()),
            "redundant copies skipped": sum(len(d.skipped_asset_ids) for d in decisions),
            "bundles created": len(bundles),
            "Apple Live Photo bundles": sum(b.bundle_type == "apple_live_photo" for b in bundles),
            "Google Motion Photo bundles": sum(
                b.bundle_type == "google_motion_photo" for b in bundles
            ),
            "Samsung Motion Photo bundles": sum(
                b.bundle_type == "samsung_motion_photo" for b in bundles
            ),
            "filename fallback bundles": sum(b.bundle_type == "filename_pair" for b in bundles),
            "orphan review items": sum(b.status == "orphan" for b in bundles),
            "ambiguous items": sum(i.status == "ambiguous" for i in items),
            "collisions detected": len(collisions),
            "collisions resolved": sum(c["status"] == "resolved" for c in collisions),
            "blocked items": sum(i.status == "blocked" for i in items),
            "total planned files": sum(i.destination_relative_path is not None for i in items),
            "total planned bytes": sum(
                by_id[i.primary_asset_id].size_bytes for i in items if i.destination_relative_path
            ),
            "estimated duplicate bytes avoided": duplicate_bytes,
            "naming template": self.planning.naming_template,
            "configuration fingerprint": fingerprint,
        }
        (directory / "plan_summary.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in lines.items()), encoding="utf-8"
        )

        def write(name: str, header: list[str], rows: Iterable[Iterable[object]]) -> None:
            with (directory / name).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(header)
                writer.writerows(rows)

        write(
            "keeper_decisions.csv",
            [
                "duplicate_group_sha256",
                "copies",
                "keeper_asset_id",
                "keeper_path",
                "keeper_source",
                "keeper_score",
                "keeper_reasons",
                "skipped_asset_ids",
                "skipped_paths",
            ],
            (
                (
                    d.duplicate_group_key,
                    len(groups[d.duplicate_group_key]),
                    d.keeper_asset_id,
                    by_id[d.keeper_asset_id].absolute_path,
                    by_id[d.keeper_asset_id].source_name,
                    repr(d.score),
                    ";".join(d.reasons),
                    ";".join(map(str, d.skipped_asset_ids)),
                    ";".join(str(by_id[x].absolute_path) for x in d.skipped_asset_ids),
                )
                for d in decisions
            ),
        )
        write(
            "migration_plan.csv",
            [
                "plan_item_id",
                "action",
                "status",
                "bundle_key",
                "asset_id",
                "source_path",
                "source_name",
                "sha256",
                "relationship_type",
                "destination_relative_path",
                "reason",
            ],
            (
                (
                    n,
                    i.action,
                    i.status,
                    i.bundle_key,
                    i.primary_asset_id,
                    by_id[i.primary_asset_id].absolute_path,
                    by_id[i.primary_asset_id].source_name,
                    by_id[i.primary_asset_id].sha256,
                    i.bundle_key.split(":")[0],
                    i.destination_relative_path or "",
                    i.reason,
                )
                for n, i in enumerate(items, 1)
                if i.destination_relative_path
            ),
        )
        write(
            "duplicate_skips.csv",
            [
                "sha256",
                "keeper_asset_id",
                "keeper_path",
                "skipped_asset_id",
                "skipped_path",
                "size_bytes",
                "reason",
            ],
            (
                (
                    d.duplicate_group_key,
                    d.keeper_asset_id,
                    by_id[d.keeper_asset_id].absolute_path,
                    s,
                    by_id[s].absolute_path,
                    by_id[s].size_bytes,
                    "exact SHA-256 equality",
                )
                for d in decisions
                for s in d.skipped_asset_ids
            ),
        )
        write(
            "relationship_bundles.csv",
            [
                "bundle_key",
                "relationship_type",
                "status",
                "confidence",
                "primary_asset_id",
                "primary_path",
                "member_asset_ids",
                "member_paths",
                "evidence",
                "planning_action",
            ],
            (
                (
                    b.bundle_key,
                    b.bundle_type,
                    b.status,
                    b.confidence,
                    b.primary_asset_id,
                    by_id[b.primary_asset_id].absolute_path if b.primary_asset_id in by_id else "",
                    ";".join(map(str, b.member_asset_ids)),
                    ";".join(str(by_id[x].absolute_path) for x in b.member_asset_ids),
                    b.evidence,
                    "review" if b.status != "active" else "keep",
                )
                for b in bundles
            ),
        )
        write(
            "collisions.csv",
            [
                "asset_id",
                "source_path",
                "original_destination",
                "resolved_destination",
                "collision_type",
                "resolution",
                "status",
            ],
            (
                (
                    c["asset_id"],
                    by_id[int(c["asset_id"])].absolute_path,
                    c["original"],
                    c["resolved"],
                    "normalized destination path",
                    "asset-id suffix",
                    c["status"],
                )
                for c in collisions
            ),
        )
        write(
            "review_items.csv",
            [
                "plan_item_id",
                "asset_ids",
                "source_paths",
                "review_reason",
                "relationship_type",
                "evidence",
            ],
            (
                (
                    n,
                    i.primary_asset_id,
                    by_id[i.primary_asset_id].absolute_path,
                    i.reason,
                    i.bundle_key.split(":")[0],
                    "",
                )
                for n, i in enumerate(items, 1)
                if i.status == "ambiguous"
            ),
        )
        write(
            "blocked_items.csv",
            ["plan_item_id", "asset_ids", "source_paths", "blocked_reason", "proposed_destination"],
            (
                (
                    n,
                    i.primary_asset_id,
                    by_id[i.primary_asset_id].absolute_path,
                    i.reason,
                    i.original_destination_relative_path or "",
                )
                for n, i in enumerate(items, 1)
                if i.status == "blocked"
            ),
        )
