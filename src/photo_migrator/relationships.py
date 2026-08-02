"""Safe, deterministic relationship analysis with coordinator-only SQLite writes."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photo_migrator.apple_live_photos import (
    AppleIdentifier,
    inspect_image,
    inspect_video,
    normalize_stem,
)
from photo_migrator.atomic_io import atomic_write_csv, atomic_write_text
from photo_migrator.database import Database, utc_now
from photo_migrator.motion_photos import MotionDetection, inspect_motion_photo

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
TIMESTAMP_TOLERANCE_SECONDS = 3.0


@dataclass(frozen=True)
class AssetIdentity:
    asset_id: int
    absolute_path: Path
    filename: str
    extension: str
    media_type: str
    captured_at: str | None
    source_name: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class RelationshipCandidate:
    relationship_type: str
    primary_asset_id: int
    secondary_asset_id: int | None
    confidence: float
    evidence: str


@dataclass(frozen=True)
class RelationshipResult:
    status: str
    relationship_type: str
    primary_asset_id: int
    secondary_asset_id: int | None
    confidence: float
    evidence: str
    error: str | None = None


@dataclass(frozen=True)
class Inspection:
    asset: AssetIdentity
    apple: AppleIdentifier
    motion: MotionDetection


def _inspect(asset: AssetIdentity, ffprobe: str) -> Inspection:
    if asset.extension in IMAGE_EXTENSIONS:
        return Inspection(
            asset,
            inspect_image(asset.absolute_path),
            inspect_motion_photo(asset.absolute_path, asset.size_bytes),
        )
    if asset.extension in VIDEO_EXTENSIONS:
        return Inspection(asset, inspect_video(asset.absolute_path, ffprobe), MotionDetection())
    return Inspection(asset, AppleIdentifier(), MotionDetection())


def _timestamp_close(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    try:
        first = datetime.fromisoformat(left.replace("Z", "+00:00"))
        second = datetime.fromisoformat(right.replace("Z", "+00:00"))
        return abs((first - second).total_seconds()) <= TIMESTAMP_TOLERANCE_SECONDS
    except (ValueError, TypeError):
        return False


def _nearby(left: Path, right: Path) -> bool:
    return left.parent == right.parent or left.parent.parent == right.parent.parent


def build_relationships(inspections: list[Inspection]) -> list[RelationshipResult]:
    """Build normalized results without side effects; output is deterministically sorted."""
    images = [item for item in inspections if item.asset.extension in IMAGE_EXTENSIONS]
    videos = [item for item in inspections if item.asset.extension in VIDEO_EXTENSIONS]
    results: list[RelationshipResult] = []
    paired: set[int] = set()

    for item in images:
        motion = item.motion
        if motion.kind:
            relationship_type = f"{motion.kind}_motion_photo"
            results.append(
                RelationshipResult(
                    motion.status or "invalid",
                    relationship_type,
                    item.asset.asset_id,
                    None,
                    0.99 if motion.status == "active" else 0.0,
                    motion.evidence,
                    motion.error,
                )
            )
            paired.add(item.asset.asset_id)

    identifier_videos: dict[str, list[Inspection]] = {}
    for video in videos:
        if video.apple.value:
            identifier_videos.setdefault(video.apple.value, []).append(video)
    for image in images:
        if image.asset.asset_id in paired or not image.apple.value:
            continue
        matches = identifier_videos.get(image.apple.value, [])
        if len(matches) == 1:
            video = matches[0]
            results.append(
                RelationshipResult(
                    "active",
                    "apple_live_photo",
                    image.asset.asset_id,
                    video.asset.asset_id,
                    1.0,
                    "apple_content_identifier="
                    f"image:{image.apple.value};video:{video.apple.value};"
                    f"sources={image.apple.source},{video.apple.source}",
                )
            )
            paired.update((image.asset.asset_id, video.asset.asset_id))
        elif len(matches) > 1:
            ids = ",".join(str(match.asset.asset_id) for match in matches)
            paths = "|".join(str(match.asset.absolute_path) for match in matches)
            results.append(
                RelationshipResult(
                    "ambiguous",
                    "apple_live_photo",
                    image.asset.asset_id,
                    None,
                    1.0,
                    f"identifier_match;candidate_asset_ids={ids};candidate_paths={paths}",
                )
            )

    for image in images:
        if image.asset.asset_id in paired or image.apple.value:
            continue
        candidates = [
            video
            for video in videos
            if video.asset.asset_id not in paired
            and not video.apple.value
            and video.asset.source_name == image.asset.source_name
            and normalize_stem(video.asset.absolute_path.stem)
            == normalize_stem(image.asset.absolute_path.stem)
            and _nearby(image.asset.absolute_path, video.asset.absolute_path)
            and _timestamp_close(image.asset.captured_at, video.asset.captured_at)
        ]
        if len(candidates) == 1:
            video = candidates[0]
            results.append(
                RelationshipResult(
                    "active",
                    "filename_pair",
                    image.asset.asset_id,
                    video.asset.asset_id,
                    0.6,
                    "fallback=filename;normalized_stem="
                    f"{normalize_stem(image.asset.absolute_path.stem)};same_source=true;"
                    "directory=nearby;timestamp_tolerance_seconds=3",
                )
            )
            paired.update((image.asset.asset_id, video.asset.asset_id))
        elif len(candidates) > 1:
            ids = ",".join(str(value.asset.asset_id) for value in candidates)
            paths = "|".join(str(value.asset.absolute_path) for value in candidates)
            results.append(
                RelationshipResult(
                    "ambiguous",
                    "filename_pair",
                    image.asset.asset_id,
                    None,
                    0.6,
                    f"fallback=filename;candidate_asset_ids={ids};candidate_paths={paths}",
                )
            )

    for item in inspections:
        if item.asset.asset_id in paired:
            continue
        if item.apple.value:
            kind = (
                "orphan_motion_image"
                if item.asset.extension in IMAGE_EXTENSIONS
                else ("orphan_motion_video")
            )
            results.append(
                RelationshipResult(
                    "orphan",
                    kind,
                    item.asset.asset_id,
                    None,
                    0.9,
                    f"unmatched_apple_content_identifier={item.apple.value};source={item.apple.source}",
                    item.apple.error,
                )
            )
    return sorted(
        results,
        key=lambda value: (
            value.relationship_type,
            next(
                str(item.asset.absolute_path)
                for item in inspections
                if item.asset.asset_id == value.primary_asset_id
            ),
            value.secondary_asset_id or -1,
            value.evidence,
        ),
    )


class RelationshipEngine:
    def __init__(self, database: Database, workers: int = 1, ffprobe: str = "ffprobe") -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.database = database
        self.workers = workers
        self.ffprobe = ffprobe

    def run(
        self,
        limit: int | None = None,
        source: str | None = None,
        resume: bool = False,
        retry_failed: bool = False,
    ) -> int:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than 0")
        assets = self._assets(limit, source)
        run_id = self._start_run(len(assets))
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures: list[Future[Inspection]] = [
                    executor.submit(_inspect, asset, self.ffprobe) for asset in assets
                ]
                inspections = [future.result() for future in futures]
            results = build_relationships(inspections)
            failures = sum(1 for item in inspections if item.apple.error or item.motion.error)
            created, reused = self._store(inspections, results)
            ambiguous = sum(item.status == "ambiguous" for item in results)
            orphan = sum(item.status == "orphan" for item in results)
            invalid = sum(item.status == "invalid" for item in results)
            status = (
                "completed_with_errors"
                if failures or ambiguous or orphan or invalid
                else "completed"
            )
            self._finish(run_id, status, created, reused, ambiguous, orphan, failures)
            self.write_reports()
            return 2 if status == "completed_with_errors" else 0
        except BaseException as exc:
            self._fail(run_id, str(exc))
            raise

    def _assets(self, limit: int | None, source: str | None) -> list[AssetIdentity]:
        clauses = [
            "scan_status='available'",
            "size_bytes IS NOT NULL",
            "mtime_ns IS NOT NULL",
            "extension IN ('.jpg','.jpeg','.heic','.heif','.mov','.mp4','.m4v')",
        ]
        parameters: list[object] = []
        if source:
            clauses.append("source_name=?")
            parameters.append(source)
        sql = "SELECT * FROM assets WHERE " + " AND ".join(clauses) + " ORDER BY absolute_path"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return [
            AssetIdentity(
                row["id"],
                Path(row["absolute_path"]),
                row["filename"],
                row["extension"].lower(),
                row["media_type"],
                row["captured_at"],
                row["source_name"],
                row["size_bytes"],
                row["mtime_ns"],
            )
            for row in self.database.connection.execute(sql, parameters)
        ]

    def _start_run(self, candidates: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO relationship_runs(started_at,status,candidate_assets) "
                "VALUES (?,'running',?)",
                (utc_now(), candidates),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def _store(
        self, inspections: list[Inspection], results: list[RelationshipResult]
    ) -> tuple[int, int]:
        ids = {item.asset.asset_id for item in inspections}
        created = reused = 0
        keep: set[int] = set()
        now = utc_now()
        with self.database.transaction() as connection:
            for result in results:
                existing = connection.execute(
                    """SELECT id FROM asset_relationships WHERE relationship_type=?
                    AND primary_asset_id=? AND secondary_asset_id IS ? AND status=?
                    AND evidence=?""",
                    (
                        result.relationship_type,
                        result.primary_asset_id,
                        result.secondary_asset_id,
                        result.status,
                        result.evidence,
                    ),
                ).fetchone()
                if existing:
                    keep.add(int(existing["id"]))
                    reused += 1
                    connection.execute(
                        "UPDATE asset_relationships SET updated_at=? WHERE id=?",
                        (now, existing["id"]),
                    )
                else:
                    cursor = connection.execute(
                        """INSERT INTO asset_relationships(relationship_type,primary_asset_id,
                        secondary_asset_id,confidence,evidence,status,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            result.relationship_type,
                            result.primary_asset_id,
                            result.secondary_asset_id,
                            result.confidence,
                            result.evidence,
                            result.status,
                            now,
                            now,
                        ),
                    )
                    assert cursor.lastrowid is not None
                    keep.add(int(cursor.lastrowid))
                    created += 1
            if ids:
                placeholders = ",".join("?" for _ in ids)
                stale_sql = (
                    f"DELETE FROM asset_relationships WHERE primary_asset_id IN ({placeholders})"
                )
                parameters: list[object] = list(sorted(ids))
                if keep:
                    stale_sql += " AND id NOT IN (" + ",".join("?" for _ in keep) + ")"
                    parameters.extend(sorted(keep))
                connection.execute(stale_sql, parameters)
            by_id = {item.asset.asset_id: item for item in inspections}
            result_by_id = {item.primary_asset_id: item for item in results}
            for asset_id, inspection in by_id.items():
                asset_result = result_by_id.get(asset_id)
                error = (
                    inspection.apple.error
                    or inspection.motion.error
                    or (asset_result.error if asset_result else None)
                )
                state = (
                    "failed"
                    if error and asset_result is None
                    else (
                        "invalid"
                        if asset_result and asset_result.status == "invalid"
                        else "completed"
                    )
                )
                connection.execute(
                    """UPDATE assets SET apple_content_identifier=?,motion_photo_offset=?,
                    relationship_status=?,relationship_error=?,relationship_analyzed_size_bytes=?,
                    relationship_analyzed_mtime_ns=? WHERE id=?""",
                    (
                        inspection.apple.value,
                        inspection.motion.offset,
                        state,
                        error,
                        inspection.asset.size_bytes,
                        inspection.asset.mtime_ns,
                        asset_id,
                    ),
                )
        return created, reused

    def _finish(
        self,
        run_id: int,
        status: str,
        created: int,
        reused: int,
        ambiguous: int,
        orphan: int,
        failed: int,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE relationship_runs SET finished_at=?,status=?,relationships_created=?,
                relationships_reused=?,ambiguous_count=?,orphan_count=?,failed_count=?,message=?
                WHERE id=?""",
                (
                    utc_now(),
                    status,
                    created,
                    reused,
                    ambiguous,
                    orphan,
                    failed,
                    "relationship analysis completed",
                    run_id,
                ),
            )

    def _fail(self, run_id: int, message: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE relationship_runs SET finished_at=?,status='failed',message=? WHERE id=?",
                (utc_now(), message, run_id),
            )

    def write_reports(self) -> None:
        directory = self.database.path.parent / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        connection = self.database.connection
        rows = connection.execute(
            """SELECT r.*,p.absolute_path primary_path,s.absolute_path secondary_path,
            p.source_name,p.relationship_error FROM asset_relationships r
            JOIN assets p ON p.id=r.primary_asset_id LEFT JOIN assets s ON s.id=r.secondary_asset_id
            ORDER BY r.relationship_type,p.absolute_path,COALESCE(s.absolute_path,'')"""
        ).fetchall()
        atomic_write_csv(
            directory / "asset_relationships.csv",
            (
                tuple(
                    row[key]
                    for key in (
                        "relationship_type",
                        "status",
                        "confidence",
                        "primary_asset_id",
                        "primary_path",
                        "secondary_asset_id",
                        "secondary_path",
                        "evidence",
                    )
                )
                for row in rows
            ),
            (
                "relationship_type",
                "status",
                "confidence",
                "primary_asset_id",
                "primary_path",
                "secondary_asset_id",
                "secondary_path",
                "evidence",
            ),
        )
        atomic_write_csv(
            directory / "orphan_assets.csv",
            (
                (
                    row["relationship_type"],
                    row["primary_asset_id"],
                    row["primary_path"],
                    row["source_name"],
                    row["evidence"],
                    row["relationship_error"],
                )
                for row in rows
                if row["status"] == "orphan"
            ),
            (
                "relationship_type",
                "asset_id",
                "absolute_path",
                "source_name",
                "evidence",
                "relationship_error",
            ),
        )
        ambiguous_rows = []
        for row in rows:
            if row["status"] == "ambiguous":
                evidence = row["evidence"]
                ambiguous_rows.append(
                    (
                        row["primary_asset_id"],
                        row["primary_path"],
                        _evidence_value(evidence, "candidate_asset_ids"),
                        _evidence_value(evidence, "candidate_paths"),
                        evidence,
                    )
                )
        atomic_write_csv(
            directory / "ambiguous_relationships.csv",
            ambiguous_rows,
            (
                "primary_asset_id",
                "primary_path",
                "candidate_asset_ids",
                "candidate_paths",
                "evidence",
            ),
        )
        stats = self.database.stats()
        latest = stats["latest_relationship_run"]
        labels = (
            ("Candidate asset count", latest["candidate_assets"] if latest else 0),
            ("Active relationship count", stats["relationships_active"]),
            ("Apple Live Photo count", stats["apple_live_photos"]),
            ("Google Motion Photo count", stats["google_motion_photos"]),
            ("Samsung Motion Photo count", stats["samsung_motion_photos"]),
            ("Filename fallback pair count", stats["filename_pairs"]),
            ("Orphan image count", stats["orphan_motion_images"]),
            ("Orphan video count", stats["orphan_motion_videos"]),
            ("Ambiguous count", stats["ambiguous_relationships"]),
            ("Invalid count", stats["invalid_relationships"]),
            ("Failed count", latest["failed_count"] if latest else 0),
            ("Reused count", latest["relationships_reused"] if latest else 0),
            ("Latest run status", latest["status"] if latest else "none"),
        )
        atomic_write_text(
            directory / "relationship_summary.txt",
            "".join(f"{label}: {value}\n" for label, value in labels),
        )


def _evidence_value(evidence: str, key: str) -> str:
    prefix = f"{key}="
    return next(
        (part[len(prefix) :] for part in evidence.split(";") if part.startswith(prefix)), ""
    )
