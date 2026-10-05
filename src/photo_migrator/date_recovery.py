"""Auditable, dry-run-first recovery of missing still-image capture dates."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from photo_migrator.config import MetadataDateRecoveryConfig
from photo_migrator.database import Database, utc_now
from photo_migrator.filesystem_safety import fsync_directory, sha256_file
from photo_migrator.image_metadata import ImageAnalyzer, normalize_timestamp

LOGGER = logging.getLogger(__name__)

WRITE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
STILL_EXTENSIONS = WRITE_EXTENSIONS | {".heic", ".heif", ".nef", ".cr2", ".cr3", ".arw", ".dng"}
RAW_EXTENSIONS = {".nef", ".cr2", ".cr3", ".arw", ".dng"}
FULL_PATTERNS = (
    (
        "compact",
        re.compile(r"(?:^|[_-])(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?:\d{3})?(?:$|[_-])"),
    ),
    (
        "separated",
        re.compile(
            r"(?:^|[_ -])(\d{4})-(\d{2})-(\d{2})[ T._-](\d{2})[.:-](\d{2})[.:-](\d{2})(?:$|[_ -])"
        ),
    ),
)
DATE_PATTERN = re.compile(
    r"(?:^|[_ -])(\d{4})[-_]?((?:0[1-9])|(?:1[0-2]))[-_]?((?:0[1-9])|(?:[12]\d)|(?:3[01]))(?:$|[_ -])"
)
SEQUENCE_PATTERN = re.compile(r"^(.*?)(\d{3,8})$")


@dataclass(frozen=True)
class Evidence:
    kind: str
    timestamp: str
    precision: str
    confidence: int
    source: str
    raw: str
    explanation: str
    derivation: str = "parsed"
    estimated: bool = False


def _valid(value: object, policy: MetadataDateRecoveryConfig) -> str | None:
    normalized = normalize_timestamp(value)
    if normalized is None:
        return None
    parsed = datetime.fromisoformat(normalized)
    maximum = policy.reasonable_year_max or datetime.now().year
    return normalized if policy.reasonable_year_min <= parsed.year <= maximum else None


def filename_evidence(path: Path, policy: MetadataDateRecoveryConfig) -> Evidence | None:
    """Strictly parse only recognized, calendar-valid filename layouts."""
    stem = path.stem
    for name, pattern in FULL_PATTERNS:
        match = pattern.search(stem)
        if match:
            try:
                parts = [int(part) for part in match.groups()]
                value = datetime(
                    parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
                ).isoformat()
            except ValueError:
                return None
            if _valid(value, policy):
                return Evidence(
                    "filename_datetime",
                    value,
                    "second",
                    95,
                    str(path),
                    match.group(0),
                    f"strict filename pattern: {name}",
                )
    match = DATE_PATTERN.search(stem)
    if match:
        try:
            parts = [int(part) for part in match.groups()]
            value = datetime(parts[0], parts[1], parts[2]).isoformat()
        except ValueError:
            return None
        if _valid(value, policy):
            return Evidence(
                "filename_date",
                value,
                "day",
                90,
                str(path),
                match.group(0),
                "strict date-only filename",
            )
    return None


def folder_evidence(path: Path, policy: MetadataDateRecoveryConfig) -> Evidence | None:
    for parent in path.parents:
        name = parent.name
        exact = re.fullmatch(r"(\d{4})[-_](\d{2})[-_](\d{2})", name)
        if exact:
            try:
                parts = [int(x) for x in exact.groups()]
                value = datetime(parts[0], parts[1], parts[2]).isoformat()
            except ValueError:
                continue
            if _valid(value, policy):
                return Evidence(
                    "folder_date", value, "day", 88, str(parent), name, "exact-date folder"
                )
        month = re.fullmatch(r"(\d{4})[-_](\d{2})", name)
        if month:
            try:
                value = datetime(int(month[1]), int(month[2]), 15, 12).isoformat()
            except ValueError:
                continue
            return Evidence(
                "folder_month",
                value,
                "month",
                70,
                str(parent),
                name,
                "month folder; placeholder day/time",
                "estimated",
                True,
            )
        year = re.match(r"^(\d{4})(?:$|\s+\D)", name)
        if year:
            value = datetime(int(year[1]), 7, 1, 12).isoformat()
            if _valid(value, policy):
                return Evidence(
                    "folder_year",
                    value,
                    "year",
                    60,
                    str(parent),
                    name,
                    "year folder; placeholder day/time",
                    "estimated",
                    True,
                )
    return None


class ExifTool:
    """Safe argument-array adapter; ExifTool is optional for read-only scans."""

    def __init__(self, executable: str = "exiftool") -> None:
        self.executable = executable

    def version(self) -> str | None:
        if shutil.which(self.executable) is None:
            return None
        result = subprocess.run(
            [self.executable, "-ver"], capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def read(self, path: Path) -> dict[str, Any]:
        if self.version() is None:
            analyzed = ImageAnalyzer().analyze(path)
            if analyzed.metadata and analyzed.metadata.captured_at:
                return {"DateTimeOriginal": analyzed.metadata.captured_at}
            return {}
        args = [self.executable, "-j", "-G1", "-a", "-s", "--", str(path)]
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise OSError(
                f"ExifTool read failed ({completed.returncode}): {completed.stderr.strip()}"
            )
        values = json.loads(completed.stdout)
        return values[0] if values else {}

    def write(
        self, path: Path, timestamp: str, comment: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        dt = datetime.fromisoformat(timestamp)
        value = dt.strftime("%Y:%m:%d %H:%M:%S")
        args = [
            self.executable,
            "-overwrite_original_in_place",
            f"-EXIF:DateTimeOriginal={value}",
            f"-EXIF:CreateDate={value}",
            f"-EXIF:ModifyDate={value}",
            f"-XMP:CreateDate={timestamp}",
            f"-XMP:DateCreated={timestamp}",
        ]
        if comment:
            args.append(f"-XMP-dc:Description={comment}")
        args.extend(["--", str(path)])
        return subprocess.run(args, capture_output=True, text=True, check=False)


CAPTURE_KEYS = (
    "EXIF:DateTimeOriginal",
    "DateTimeOriginal",
    "EXIF:CreateDate",
    "CreateDate",
    "XMP:DateCreated",
    "XMP:CreateDate",
)


def capture_value(
    metadata: dict[str, Any], policy: MetadataDateRecoveryConfig
) -> tuple[str | None, str | None, str | None]:
    for key in CAPTURE_KEYS:
        if key in metadata:
            value = _valid(metadata[key], policy)
            if value:
                return value, key, str(metadata[key])
    return None, None, None


def sidecar_evidence(path: Path, policy: MetadataDateRecoveryConfig) -> list[Evidence]:
    found: list[Evidence] = []
    xmp = path.with_suffix(path.suffix + ".xmp")
    if not xmp.exists():
        xmp = path.with_suffix(".xmp")
    if xmp.is_file():
        text = xmp.read_text("utf-8", errors="replace")
        match = re.search(r"(?:DateTimeOriginal|DateCreated|CreateDate)=[\"']([^\"']+)", text)
        if match and (value := _valid(match[1], policy)):
            found.append(
                Evidence(
                    "xmp_sidecar",
                    value,
                    "second",
                    99,
                    str(xmp),
                    match[1],
                    "trusted associated XMP field",
                    "copied",
                )
            )
    candidates = (Path(str(path) + ".json"), path.with_suffix(".json"))
    for sidecar in candidates:
        if not sidecar.is_file():
            continue
        try:
            raw = json.loads(sidecar.read_text("utf-8"))
            stamp = raw.get("photoTakenTime", {}).get("timestamp")
            if stamp is not None:
                value = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
                found.append(
                    Evidence(
                        "google_json",
                        value,
                        "second",
                        99,
                        str(sidecar),
                        str(stamp),
                        "Google Photos photoTakenTime",
                        "copied",
                    )
                )
                break
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return found


def resolve_evidence(
    items: list[Evidence], tolerance_seconds: int = 120
) -> tuple[Evidence | None, int, bool]:
    if not items:
        return None, 0, False
    ordered = sorted(items, key=lambda item: (-item.confidence, item.kind, item.source))
    primary = ordered[0]
    primary_time = datetime.fromisoformat(primary.timestamp)
    strong_conflict = any(
        abs((datetime.fromisoformat(item.timestamp) - primary_time).total_seconds())
        > tolerance_seconds
        and item.confidence >= 88
        for item in ordered[1:]
    )
    agreeing = sum(
        abs((datetime.fromisoformat(item.timestamp) - primary_time).total_seconds())
        <= tolerance_seconds
        for item in ordered[1:]
    )
    return primary, min(99, primary.confidence + min(agreeing * 2, 4)), strong_conflict


def neighboring_evidence(
    target: Path,
    dated: dict[Path, str],
    max_sequence_gap: int = 5,
    max_time_gap_hours: int = 24,
) -> Evidence | None:
    """Interpolate only between unambiguous same-directory sequence neighbors."""
    target_match = SEQUENCE_PATTERN.fullmatch(target.stem)
    if target_match is None:
        return None
    prefix, number_text = target_match.groups()
    number = int(number_text)
    candidates: list[tuple[int, Path, datetime]] = []
    for path, date_text in dated.items():
        match = SEQUENCE_PATTERN.fullmatch(path.stem)
        if path.parent != target.parent or match is None or match[1] != prefix:
            continue
        try:
            candidates.append((int(match[2]), path, datetime.fromisoformat(date_text)))
        except ValueError:
            continue
    before = sorted((item for item in candidates if item[0] < number), reverse=True)
    after = sorted(item for item in candidates if item[0] > number)
    if not before or not after:
        return None
    left, right = before[0], after[0]
    sequence_gap = right[0] - left[0]
    time_gap = right[2] - left[2]
    if (
        sequence_gap > max_sequence_gap
        or time_gap.total_seconds() < 0
        or time_gap.total_seconds() > max_time_gap_hours * 3600
    ):
        return None
    fraction = (number - left[0]) / sequence_gap
    interpolated = left[2] + time_gap * fraction
    return Evidence(
        "neighbor_interpolation",
        interpolated.isoformat(),
        "second",
        93,
        f"{left[1]} | {right[1]}",
        f"({number}-{left[0]})/({right[0]}-{left[0]})={fraction:.6f}",
        "two-sided chronological sequence interpolation",
        "interpolated",
    )


def _copy_verified(source: Path, destination: Path) -> str:
    """Copy through a same-directory temporary, fsync, re-read, and return the SHA-256."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                outgoing.write(chunk)
                digest.update(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if sha256_file(temporary)[0] != digest.hexdigest():
            raise RuntimeError(f"copy of {source} did not verify")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    fsync_directory(destination.parent)
    return digest.hexdigest()


class DateRecovery:
    def __init__(
        self,
        database: Database,
        policy: MetadataDateRecoveryConfig,
        report_dir: Path,
        tool: ExifTool | None = None,
    ) -> None:
        self.database, self.policy, self.report_dir = database, policy, report_dir
        self.tool = tool or ExifTool()
        report_dir.mkdir(parents=True, exist_ok=True)

    def scan(self, root: Path, limit: int | None = None) -> int:
        root = root.resolve(strict=True)
        now = utc_now()
        with self.database.transaction() as c:
            run = c.execute(
                "INSERT INTO metadata_date_scan_runs(started_at,root,configuration,tool_version,status) VALUES(?,?,?,?, 'running')",
                (
                    now,
                    str(root),
                    json.dumps(self.policy.__dict__, sort_keys=True),
                    self.tool.version(),
                ),
            ).lastrowid
        assert run is not None
        rows = self.database.connection.execute(
            "SELECT * FROM assets WHERE asset_role='canonical' AND scan_status='available' AND media_type='image' AND absolute_path LIKE ? ORDER BY absolute_path"
            + (" LIMIT ?" if limit else ""),
            ((str(root) + os.sep + "%", limit) if limit else (str(root) + os.sep + "%",)),
        ).fetchall()
        report: list[dict[str, object]] = []
        counts = {"scanned": 0, "already_dated": 0, "missing": 0, "conflicts": 0, "errors": 0}
        dated_by_hash: dict[tuple[int, str], tuple[str, int, str]] = {}
        dated_by_pair: dict[tuple[str, str], tuple[str, int, str]] = {}
        dated_paths: dict[Path, str] = {}
        pending: list[tuple[Any, Path, list[Evidence], os.stat_result]] = []
        for row in rows:
            path = Path(row["absolute_path"])
            counts["scanned"] += 1
            try:
                stat = path.stat()
                metadata = self.tool.read(path)
                existing, source, _raw = capture_value(metadata, self.policy)
                if existing:
                    counts["already_dated"] += 1
                    if row["sha256"]:
                        dated_by_hash[(row["size_bytes"], row["sha256"])] = (
                            existing,
                            row["id"],
                            str(path),
                        )
                    dated_by_pair[(str(path.parent), path.stem.casefold())] = (
                        existing,
                        row["id"],
                        str(path),
                    )
                    dated_paths[path] = existing
                    report.append(self._report(row, existing, source, [], None, 0, False, stat))
                    continue
                counts["missing"] += 1
                evidence = sidecar_evidence(path, self.policy)
                for item in (
                    filename_evidence(path, self.policy),
                    folder_evidence(path, self.policy),
                ):
                    if item:
                        evidence.append(item)
                birth = getattr(stat, "st_birthtime", None)
                if birth:
                    evidence.append(
                        Evidence(
                            "filesystem_birthtime",
                            datetime.fromtimestamp(birth, timezone.utc).isoformat(),
                            "second",
                            55,
                            str(path),
                            str(birth),
                            "weak filesystem birth time",
                            "copied",
                        )
                    )
                evidence.append(
                    Evidence(
                        "filesystem_mtime",
                        datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        "second",
                        40,
                        str(path),
                        str(stat.st_mtime_ns),
                        "weak filesystem modification time",
                        "copied",
                    )
                )
                pending.append((row, path, evidence, stat))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                counts["errors"] += 1
                report.append(self._report(row, None, None, [], None, 0, False, None, str(exc)))
        for row, path, evidence, stat in pending:
            neighbor = neighboring_evidence(
                path,
                dated_paths,
                self.policy.max_neighbor_sequence_gap,
                self.policy.max_neighbor_time_gap_hours,
            )
            if neighbor:
                evidence.append(neighbor)
            if row["sha256"] and (match := dated_by_hash.get((row["size_bytes"], row["sha256"]))):
                evidence.append(
                    Evidence(
                        "exact_duplicate",
                        match[0],
                        "second",
                        99,
                        match[2],
                        str(match[1]),
                        "same size and SHA-256 counterpart",
                        "copied",
                    )
                )
            pair = dated_by_pair.get((str(path.parent), path.stem.casefold()))
            if pair and (
                path.suffix.lower() in RAW_EXTENSIONS
                or Path(pair[2]).suffix.lower() in RAW_EXTENSIONS
            ):
                evidence.append(
                    Evidence(
                        "raw_jpeg_pair",
                        pair[0],
                        "second",
                        98,
                        pair[2],
                        str(pair[1]),
                        "same-directory RAW/JPEG stem pair",
                        "copied",
                    )
                )
            primary, confidence, conflict = resolve_evidence(evidence)
            counts["conflicts"] += int(conflict)
            with self.database.transaction() as c:
                for item in evidence:
                    c.execute(
                        "INSERT INTO metadata_date_evidence(scan_run_id,asset_id,evidence_type,source,candidate_timestamp,precision,derivation,confidence,raw_value,explanation) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            run,
                            row["id"],
                            item.kind,
                            item.source,
                            item.timestamp,
                            item.precision,
                            item.derivation,
                            item.confidence,
                            item.raw,
                            item.explanation,
                        ),
                    )
            report.append(
                self._report(row, None, None, evidence, primary, confidence, conflict, stat)
            )
        self._csv(f"metadata-date-scan-{run}.csv", report)
        self._csv(
            f"metadata-date-summary-{run}.csv",
            [
                {
                    "files_scanned": counts["scanned"],
                    "files_already_dated": counts["already_dated"],
                    "files_missing_dates": counts["missing"],
                    "exact_high_confidence_candidates": sum(
                        isinstance(r["confidence"], int)
                        and r["confidence"] >= self.policy.min_confidence
                        and not r["conflict"]
                        for r in report
                    ),
                    "estimated_candidates": sum(
                        '"estimated": true' in str(r["all_evidence"]) for r in report
                    ),
                    "conflicts": counts["conflicts"],
                    "unsupported_files": sum(not bool(r["write_supported"]) for r in report),
                    "files_with_no_evidence": sum(not bool(r["candidate_date"]) for r in report),
                    "planned_updates": 0,
                    "successfully_updated": 0,
                    "verification_failures": 0,
                    "skipped_changed_since_scan": 0,
                }
            ],
        )
        with self.database.transaction() as c:
            c.execute(
                "UPDATE metadata_date_scan_runs SET finished_at=?,status=?,scanned=?,already_dated=?,missing=?,conflicts=?,errors=? WHERE id=?",
                (
                    utc_now(),
                    "completed_with_errors" if counts["errors"] else "completed",
                    counts["scanned"],
                    counts["already_dated"],
                    counts["missing"],
                    counts["conflicts"],
                    counts["errors"],
                    run,
                ),
            )
        print(
            f"Scanned: {counts['scanned']}\nMissing dates: {counts['missing']}\n"
            f"High-confidence candidates: {sum(isinstance(r['confidence'], int) and r['confidence'] >= self.policy.min_confidence and not r['conflict'] for r in report)}\n"
            f"Conflicts: {counts['conflicts']}\nErrors: {counts['errors']}"
        )
        return int(run)

    def plan(
        self,
        scan_run_id: int | None = None,
        min_confidence: int | None = None,
        allow_estimated: bool | None = None,
    ) -> int:
        if scan_run_id is None:
            row = self.database.connection.execute(
                "SELECT max(id) id FROM metadata_date_scan_runs WHERE status LIKE 'completed%'"
            ).fetchone()
            scan_run_id = row["id"]
        if scan_run_id is None:
            raise ValueError("no completed metadata date scan")
        threshold = min_confidence or self.policy.min_confidence
        estimated_allowed = (
            self.policy.allow_estimated_dates if allow_estimated is None else allow_estimated
        )
        with self.database.transaction() as c:
            plan_id = c.execute(
                "INSERT INTO metadata_date_plans(scan_run_id,created_at,min_confidence,allow_estimated,status) VALUES(?,?,?,?, 'ready')",
                (scan_run_id, utc_now(), threshold, int(estimated_allowed)),
            ).lastrowid
            assets = c.execute(
                "SELECT a.* FROM assets a JOIN metadata_date_evidence e ON e.asset_id=a.id WHERE e.scan_run_id=? GROUP BY a.id ORDER BY a.absolute_path",
                (scan_run_id,),
            ).fetchall()
            output = []
            for asset in assets:
                evidence_rows = c.execute(
                    "SELECT * FROM metadata_date_evidence WHERE scan_run_id=? AND asset_id=? ORDER BY confidence DESC,evidence_type,source",
                    (scan_run_id, asset["id"]),
                ).fetchall()
                evidence = [
                    Evidence(
                        r["evidence_type"],
                        r["candidate_timestamp"],
                        r["precision"],
                        r["confidence"],
                        r["source"] or "",
                        r["raw_value"] or "",
                        r["explanation"],
                        r["derivation"],
                        r["derivation"] == "estimated",
                    )
                    for r in evidence_rows
                ]
                primary, confidence, conflict = resolve_evidence(evidence)
                supported = (
                    Path(asset["absolute_path"]).suffix.lower() in WRITE_EXTENSIONS
                    and self.tool.version() is not None
                )
                eligible = bool(
                    primary
                    and confidence >= threshold
                    and not conflict
                    and supported
                    and (not primary.estimated or estimated_allowed)
                    and primary.kind not in {"filesystem_mtime", "filesystem_birthtime"}
                )
                status = "eligible" if eligible else "manual_review"
                item_id = c.execute(
                    "INSERT INTO metadata_date_plan_items(plan_id,asset_id,path,proposed_timestamp,precision,derivation,confidence,status,selected_evidence,conflicts,planned_size,planned_mtime_ns,planned_sha256,write_supported) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        asset["id"],
                        asset["absolute_path"],
                        primary.timestamp if primary else None,
                        primary.precision if primary else None,
                        primary.derivation if primary else None,
                        confidence,
                        status,
                        json.dumps(primary.__dict__, sort_keys=True) if primary else None,
                        "strong evidence disagrees" if conflict else None,
                        asset["size_bytes"],
                        asset["mtime_ns"],
                        asset["sha256"],
                        int(supported),
                    ),
                ).lastrowid
                output.append(
                    {
                        "plan_item_id": item_id,
                        "path": asset["absolute_path"],
                        "candidate_date": primary.timestamp if primary else "",
                        "precision": primary.precision if primary else "",
                        "confidence": confidence,
                        "status": status,
                        "conflict": conflict,
                        "write_supported": supported,
                    }
                )
        assert plan_id is not None
        self._csv(
            f"metadata-date-plan-{plan_id}.csv",
            [r for r in output if r["status"] == "eligible"],
        )
        self._csv(
            f"metadata-date-manual-review-{plan_id}.csv",
            [r for r in output if r["status"] != "eligible"],
        )
        return int(plan_id)

    def apply(
        self,
        plan_id: int,
        apply: bool = False,
        preserve_times: bool = True,
        limit: int | None = None,
    ) -> int:
        plan = self.database.connection.execute(
            "SELECT status FROM metadata_date_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None or plan["status"] != "ready":
            raise ValueError("metadata date plan is not ready")
        with self.database.transaction() as c:
            run_id = c.execute(
                "INSERT INTO metadata_date_apply_runs(plan_id,started_at,dry_run,status) VALUES(?,?,?,'running')",
                (plan_id, utc_now(), int(not apply)),
            ).lastrowid
        assert run_id is not None
        query = (
            "SELECT * FROM metadata_date_plan_items WHERE plan_id=? AND status='eligible' ORDER BY path"
            + (" LIMIT ?" if limit else "")
        )
        rows = self.database.connection.execute(
            query, (plan_id, limit) if limit else (plan_id,)
        ).fetchall()
        report = []
        for item in rows:
            path = Path(item["path"])
            before: dict[str, Any] = {}
            status = "dry_run"
            error = ""
            after = None
            backup: tuple[Path, str] | None = None
            after_digest: str | None = None
            try:
                stat = path.stat()
                if (
                    stat.st_size != item["planned_size"]
                    or stat.st_mtime_ns != item["planned_mtime_ns"]
                ):
                    raise RuntimeError("file changed since plan")
                before = self.tool.read(path)
                existing, _, _ = capture_value(before, self.policy)
                if existing:
                    raise RuntimeError("valid capture metadata appeared since plan")
                if apply:
                    backup = self._backup_original(int(run_id), item, path)
                    comment = None
                    if item["derivation"] == "estimated":
                        comment = f"Estimated capture date by photo-migrator. Original precision: {item['precision']}. Recovery run: {run_id}."
                    result = self.tool.write(path, item["proposed_timestamp"], comment)
                    if result.returncode:
                        raise RuntimeError(
                            f"ExifTool write failed ({result.returncode}): {result.stderr.strip()}"
                        )
                    if preserve_times:
                        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    after = self.tool.read(path)
                    verified, _, _ = capture_value(after, self.policy)
                    expected_dt = datetime.fromisoformat(item["proposed_timestamp"]).replace(
                        tzinfo=None, microsecond=0
                    )
                    verified_dt = (
                        datetime.fromisoformat(verified).replace(tzinfo=None, microsecond=0)
                        if verified
                        else None
                    )
                    if verified_dt != expected_dt:
                        raise RuntimeError(f"verification failed: observed {verified!r}")
                    self._refresh_asset_identity(int(item["asset_id"]), path)
                    after_digest = sha256_file(path)[0]
                    status = "completed"
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                status, error = "skipped", str(exc)
                if backup is not None:
                    status, after_digest = self._classify_failed_write(item, path, backup[1])
            if status == "write_unverified":
                LOGGER.error(
                    "metadata write to %s could not be verified; original kept at %s (%s)",
                    path,
                    backup[0] if backup else "",
                    error,
                )
            current = path.stat() if path.exists() else None
            with self.database.transaction() as c:
                c.execute(
                    "INSERT INTO metadata_date_apply_items(apply_run_id,plan_item_id,path,before_values,intended_values,after_values,before_size,before_mtime_ns,after_size,after_mtime_ns,status,verification,error,backup,backup_path,backup_sha256,after_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        item["id"],
                        str(path),
                        json.dumps(before, default=str, sort_keys=True),
                        json.dumps({"capture_date": item["proposed_timestamp"]}),
                        json.dumps(after, default=str, sort_keys=True) if after else None,
                        item["planned_size"],
                        item["planned_mtime_ns"],
                        current.st_size if current else None,
                        current.st_mtime_ns if current else None,
                        status,
                        "verified" if status == "completed" else "not_written",
                        error or None,
                        json.dumps(
                            {k: before.get(k) for k in CAPTURE_KEYS if k in before},
                            default=str,
                            sort_keys=True,
                        ),
                        str(backup[0]) if backup else None,
                        backup[1] if backup else None,
                        after_digest,
                    ),
                )
            report.append(
                {
                    "path": str(path),
                    "status": status,
                    "before_date": capture_value(before, self.policy)[0],
                    "after_date": item["proposed_timestamp"] if status == "completed" else "",
                    "confidence": item["confidence"],
                    "evidence": item["selected_evidence"],
                    "verification": "verified" if status == "completed" else "",
                    "error": error,
                    "run_id": run_id,
                }
            )
        with self.database.transaction() as c:
            c.execute(
                "UPDATE metadata_date_apply_runs SET finished_at=?,status='completed' WHERE id=?",
                (utc_now(), run_id),
            )
        self._csv(f"metadata-date-apply-{run_id}.csv", report)
        return int(run_id)

    def rollback(self, apply_run_id: int, apply: bool = False) -> int:
        original = self.database.connection.execute(
            "SELECT * FROM metadata_date_apply_runs WHERE id=? AND dry_run=0", (apply_run_id,)
        ).fetchone()
        if original is None:
            raise ValueError("apply run does not exist or made no changes")
        with self.database.transaction() as c:
            run = c.execute(
                "INSERT INTO metadata_date_apply_runs(plan_id,started_at,dry_run,rollback_of,status) VALUES(?,?,?,?, 'running')",
                (original["plan_id"], utc_now(), int(not apply), apply_run_id),
            ).lastrowid
        assert run is not None
        items = self.database.connection.execute(
            "SELECT i.*,p.proposed_timestamp FROM metadata_date_apply_items i JOIN metadata_date_plan_items p ON p.id=i.plan_item_id WHERE i.apply_run_id=? AND i.status IN ('completed','write_unverified') ORDER BY i.path",
            (apply_run_id,),
        ).fetchall()
        report = []
        for item in items:
            path = Path(item["path"])
            status = "dry_run"
            error = ""
            try:
                backup = json.loads(item["backup"])
                if item["backup_path"]:
                    # Byte-level restore: exact original content and mtime.
                    self._check_restorable(item, path)
                    if apply:
                        self._restore_original(item, path)
                        self._refresh_asset_identity(self._asset_id(item), path)
                        status = "completed"
                    report.append(
                        {
                            "path": str(path),
                            "status": status,
                            "restored": f"original bytes from {item['backup_path']}",
                            "error": "",
                            "run_id": run,
                        }
                    )
                    continue
                if item["status"] != "completed":
                    raise RuntimeError("unverified write has no byte backup to restore")
                current = self.tool.read(path)
                value, _, _ = capture_value(current, self.policy)
                if value != item["proposed_timestamp"]:
                    raise RuntimeError("current capture date differs from value written by run")
                if apply:
                    # This first version only writes previously-empty fields, so rollback removes exactly them.
                    args = [
                        self.tool.executable,
                        "-overwrite_original_in_place",
                        "-EXIF:DateTimeOriginal=",
                        "-EXIF:CreateDate=",
                        "-EXIF:ModifyDate=",
                        "-XMP:CreateDate=",
                        "-XMP:DateCreated=",
                        "--",
                        str(path),
                    ]
                    result = subprocess.run(args, capture_output=True, text=True, check=False)
                    if result.returncode:
                        raise RuntimeError(result.stderr.strip())
                    restored = self.tool.read(path)
                    restored_value, _, _ = capture_value(restored, self.policy)
                    if restored_value is not None:
                        raise RuntimeError(
                            f"rollback verification failed: observed {restored_value!r}"
                        )
                    self._refresh_asset_identity(self._asset_id(item), path)
                    status = "completed"
                report.append(
                    {
                        "path": str(path),
                        "status": status,
                        "restored": json.dumps(backup, sort_keys=True),
                        "error": "",
                        "run_id": run,
                    }
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                report.append(
                    {
                        "path": str(path),
                        "status": "skipped",
                        "restored": "",
                        "error": error,
                        "run_id": run,
                    }
                )
        with self.database.transaction() as c:
            c.execute(
                "UPDATE metadata_date_apply_runs SET finished_at=?,status='completed' WHERE id=?",
                (utc_now(), run),
            )
        self._csv(f"metadata-date-rollback-{run}.csv", report)
        return int(run)

    def _asset_id(self, item: Any) -> int:
        plan_item = self.database.connection.execute(
            "SELECT asset_id FROM metadata_date_plan_items WHERE id=?", (item["plan_item_id"],)
        ).fetchone()
        if plan_item is None:
            raise RuntimeError("rollback plan item no longer exists")
        return int(plan_item["asset_id"])

    def _backup_original(self, run_id: int, item: Any, path: Path) -> tuple[Path, str]:
        """Keep a verified byte copy of *path* before ExifTool rewrites it in place."""
        directory = self.report_dir / "metadata-backups" / f"apply-{run_id}"
        backup = directory / f"{int(item['asset_id']):08d}{path.suffix.lower()}"
        digest = _copy_verified(path, backup)
        if item["planned_sha256"] and digest != item["planned_sha256"]:
            backup.unlink()
            raise RuntimeError("file content changed since plan")
        return backup, digest

    def _classify_failed_write(
        self, item: Any, path: Path, original_digest: str
    ) -> tuple[str, str | None]:
        """After an error, tell an untouched file apart from one ExifTool already changed."""
        try:
            current = sha256_file(path)[0]
        except OSError:
            return "write_unverified", None
        if current == original_digest:
            return "skipped", None
        try:
            self._refresh_asset_identity(int(item["asset_id"]), path)
        except (OSError, RuntimeError) as exc:
            LOGGER.error("could not refresh inventory for %s: %s", path, exc)
        return "write_unverified", current

    @staticmethod
    def _check_restorable(item: Any, path: Path) -> None:
        backup = Path(item["backup_path"])
        if sha256_file(backup)[0] != item["backup_sha256"]:
            raise RuntimeError(f"backup {backup} no longer matches its recorded SHA-256")
        if item["after_sha256"] is None or sha256_file(path)[0] != item["after_sha256"]:
            raise RuntimeError("file changed after the apply run; not restoring")

    @staticmethod
    def _restore_original(item: Any, path: Path) -> None:
        current = path.stat()
        temporary = path.parent / f".photo-migrator-restore-{int(item['id'])}.tmp"
        digest = _copy_verified(Path(item["backup_path"]), temporary)
        try:
            if digest != item["backup_sha256"]:
                raise RuntimeError("backup changed while restoring")
            os.utime(temporary, ns=(current.st_atime_ns, int(item["before_mtime_ns"])))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        fsync_directory(path.parent)
        if sha256_file(path)[0] != item["backup_sha256"]:
            raise RuntimeError("restored file does not match backup")

    def _refresh_asset_identity(self, asset_id: int, path: Path) -> None:
        """Atomically publish a verified metadata write's new on-disk identity."""
        before = path.stat()
        digest, size = sha256_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("file changed while refreshing canonical identity")
        if size != after.st_size:
            raise RuntimeError("file size changed while refreshing canonical identity")

        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE assets SET size_bytes=?,mtime_ns=?,sha256=?,
                hash_algorithm='sha256',hash_status='completed',hash_started_at=NULL,
                hash_completed_at=?,hash_error=NULL,updated_at=?,analysis_status='pending',
                analysis_started_at=NULL,analysis_completed_at=NULL,analysis_error=NULL,
                analyzed_size_bytes=NULL,analyzed_mtime_ns=NULL,relationship_status='pending',
                relationship_error=NULL,relationship_analyzed_size_bytes=NULL,
                relationship_analyzed_mtime_ns=NULL WHERE id=? AND absolute_path=?""",
                (size, after.st_mtime_ns, digest, now, now, asset_id, str(path)),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("canonical asset no longer matches metadata plan")
            connection.execute(
                """UPDATE import_plans SET status='stale' WHERE status='ready' AND id IN
                (SELECT plan_id FROM import_plan_items WHERE canonical_asset_id=?)""",
                (asset_id,),
            )
            connection.execute(
                """UPDATE migration_plans SET status='superseded',updated_at=?
                WHERE status!='superseded' AND id IN
                (SELECT i.plan_id FROM migration_plan_items i
                 JOIN migration_plan_item_assets a ON a.plan_item_id=i.id
                 WHERE a.asset_id=?)""",
                (now, asset_id),
            )
            connection.execute(
                """UPDATE metadata_date_plans SET status='stale' WHERE status='ready' AND scan_run_id IN
                (SELECT scan_run_id FROM metadata_date_evidence WHERE asset_id=?)""",
                (asset_id,),
            )

    def _report(
        self,
        row: Any,
        existing: str | None,
        source: str | None,
        evidence: list[Evidence],
        primary: Evidence | None,
        confidence: int,
        conflict: bool,
        stat: os.stat_result | None,
        error: str = "",
    ) -> dict[str, object]:
        return {
            "path": row["absolute_path"],
            "extension": row["extension"],
            "file_size": row["size_bytes"],
            "existing_capture_date": existing or "",
            "existing_date_source": source or "",
            "missing_date": not bool(existing),
            "candidate_date": primary.timestamp if primary else "",
            "candidate_timezone": "present"
            if primary and datetime.fromisoformat(primary.timestamp).tzinfo
            else "unavailable",
            "candidate_precision": primary.precision if primary else "",
            "confidence": confidence,
            "recommended_action": "manual_review"
            if conflict or confidence < self.policy.min_confidence
            else "plan",
            "primary_reason": primary.explanation if primary else error or "no evidence",
            "all_evidence": json.dumps([e.__dict__ for e in evidence], sort_keys=True),
            "conflict": conflict,
            "write_supported": Path(row["absolute_path"]).suffix.lower() in WRITE_EXTENSIONS
            and self.tool.version() is not None,
            "filesystem_mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            if stat
            else "",
            "filesystem_birthtime": datetime.fromtimestamp(
                stat.__getattribute__("st_birthtime"), timezone.utc
            ).isoformat()
            if stat and hasattr(stat, "st_birthtime")
            else "",
        }

    def _csv(self, name: str, rows: list[dict[str, object]]) -> None:
        path = self.report_dir / name
        fields = list(rows[0]) if rows else ["path", "status"]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
