"""Controlled execution of an immutable migration-plan snapshot."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from photo_migrator.atomic_io import atomic_write_csv, atomic_write_text
from photo_migrator.build_models import BuildOperationResult, BuildPlanItem, RollbackResult
from photo_migrator.database import Database, utc_now
from photo_migrator.filesystem_safety import (
    fsync_directory,
    safe_destination,
    sha256_file,
    verify_source,
)

ELIGIBLE = ("keep", "include_relationship_member")


def _source_root(row: sqlite3.Row) -> Path:
    path, relative = Path(row["absolute_path"]), Path(row["relative_path"])
    root = path
    for _ in relative.parts:
        root = root.parent
    return root


class Builder:
    def __init__(self, database: Database, workers: int = 1) -> None:
        if workers < 1:
            raise ValueError("workers must be at least one")
        self.database, self.workers = database, workers

    def run(
        self,
        plan_id: int,
        mode: str | None = None,
        allow_draft: bool = False,
        resume: bool = False,
        verify_only: bool = False,
        limit: int | None = None,
    ) -> int:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        if verify_only and mode is not None:
            raise ValueError("--verify-only cannot be combined with --mode")
        plan = self.database.connection.execute(
            "SELECT * FROM migration_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise ValueError(f"migration plan {plan_id} does not exist")
        if plan["status"] in {"blocked", "superseded"}:
            raise ValueError(f"cannot execute {plan['status']} migration plan")
        if plan["status"] == "draft" and not allow_draft:
            raise ValueError("draft migration plans require --allow-draft")
        destination_root = Path(plan["destination_root"]).absolute()
        self._validate_roots(destination_root)
        selected_mode = mode or "dry_run"
        if resume:
            run = self.database.connection.execute(
                "SELECT * FROM build_runs WHERE plan_id=? AND mode=? ORDER BY id DESC LIMIT 1",
                (plan_id, selected_mode),
            ).fetchone()
            if run is None:
                raise ValueError("no compatible prior build run to resume")
            if (
                run["plan_fingerprint"] != plan["fingerprint"]
                or Path(run["destination_root"]) != destination_root
            ):
                raise ValueError("prior build snapshot is incompatible")
            run_id = int(run["id"])
            self.database.connection.execute(
                "UPDATE build_runs SET status='running',finished_at=NULL WHERE id=?", (run_id,)
            )
            self.database.connection.commit()
            items = self._snapshot_items(run_id)
        else:
            items = self._plan_items(plan_id, limit)
            run_id = self._start_run(plan, selected_mode, destination_root, items)
        if verify_only:
            from photo_migrator.verifier import Verifier

            Verifier(self.database, self.workers).run(run_id, limit)
            return run_id
        if mode is not None:
            destination_root.mkdir(parents=False, exist_ok=True)
        operation = selected_mode
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            results = list(
                executor.map(
                    lambda item: self._execute(item, destination_root, operation, run_id), items
                )
            )
        self._persist_results(run_id, results)
        self._finish(run_id)
        self.reports(run_id)
        return run_id

    def _validate_roots(self, destination: Path) -> None:
        for row in self.database.connection.execute(
            "SELECT absolute_path,relative_path FROM assets"
        ):
            root = _source_root(row).resolve()
            target = destination.resolve(strict=False)
            if target == root or target.is_relative_to(root) or root.is_relative_to(target):
                raise ValueError(f"destination root overlaps source root: {root}")

    def _plan_items(self, plan_id: int, limit: int | None) -> list[BuildPlanItem]:
        query = """SELECT p.id plan_item_id,p.primary_asset_id asset_id,p.destination_relative_path,
        p.action,p.status,p.bundle_key,a.* FROM migration_plan_items p
        LEFT JOIN assets a ON a.id=p.primary_asset_id WHERE p.plan_id=? AND p.action IN (?,?)
        ORDER BY p.id,a.id"""
        parameters: list[object] = [plan_id, *ELIGIBLE]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        result = []
        for row in self.database.connection.execute(query, parameters):
            if (
                row["absolute_path"] is None
                or row["destination_relative_path"] is None
                or row["sha256"] is None
            ):
                continue
            result.append(
                BuildPlanItem(
                    int(row["plan_item_id"]),
                    int(row["asset_id"]),
                    Path(row["absolute_path"]),
                    _source_root(row),
                    Path(row["destination_relative_path"]),
                    str(row["sha256"]),
                    int(row["size_bytes"]),
                    row["mtime_ns"],
                    str(row["action"]),
                    str(row["status"]),
                    str(row["bundle_key"]),
                    False,
                )
            )
        return result

    def _start_run(
        self, plan: sqlite3.Row, mode: str, root: Path, items: list[BuildPlanItem]
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO build_runs(plan_id,started_at,status,mode,dry_run,requested_items,
                plan_fingerprint,plan_status,destination_root) VALUES (? ,?,'running',?,?,?,?,?,?)""",
                (
                    plan["id"],
                    utc_now(),
                    mode,
                    int(mode == "dry_run"),
                    len(items),
                    plan["fingerprint"],
                    plan["status"],
                    str(root),
                ),
            )
            assert cursor.lastrowid is not None
            run_id = int(cursor.lastrowid)
            for item in items:
                destination = root / item.destination_relative_path
                connection.execute(
                    """INSERT INTO build_items(build_run_id,plan_item_id,asset_id,source_path,source_root,
                    destination_relative_path,destination_absolute_path,operation,status,expected_sha256,
                    expected_size_bytes,expected_source_mtime_ns,bundle_key) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?,?)""",
                    (
                        run_id,
                        item.plan_item_id,
                        item.asset_id,
                        str(item.source_path),
                        str(item.source_root),
                        str(item.destination_relative_path),
                        str(destination),
                        mode,
                        item.expected_sha256,
                        item.expected_size_bytes,
                        item.expected_mtime_ns,
                        item.bundle_key,
                    ),
                )
        return run_id

    def _snapshot_items(self, run_id: int) -> list[BuildPlanItem]:
        rows = self.database.connection.execute(
            "SELECT * FROM build_items WHERE build_run_id=? ORDER BY id", (run_id,)
        )
        return [
            BuildPlanItem(
                int(r["plan_item_id"]),
                int(r["asset_id"]),
                Path(r["source_path"]),
                Path(r["source_root"]),
                Path(r["destination_relative_path"]),
                str(r["expected_sha256"]),
                int(r["expected_size_bytes"]),
                r["expected_source_mtime_ns"],
                "keep",
                str(r["status"]),
                str(r["bundle_key"]),
                bool(r["owned_by_build"]),
            )
            for r in rows
        ]

    def _execute(
        self, item: BuildPlanItem, root: Path, operation: str, run_id: int
    ) -> BuildOperationResult:
        destination = root / item.destination_relative_path
        try:
            observed, _ = verify_source(
                item.source_path,
                item.source_root,
                item.expected_size_bytes,
                item.expected_mtime_ns,
                item.expected_sha256,
            )
            if operation == "dry_run":
                safe_destination(root, item.destination_relative_path)
                return BuildOperationResult(
                    item.plan_item_id,
                    item.asset_id,
                    item.source_path,
                    destination,
                    operation,
                    "skipped",
                    source_verified=True,
                    observed_source_mtime_ns=observed,
                )
            destination = safe_destination(
                root, item.destination_relative_path, create_parents=True
            )
            if destination.exists() or destination.is_symlink():
                info = destination.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("destination exists and is not a regular non-symlink file")
                digest, size = sha256_file(destination)
                if digest != item.expected_sha256 or size != item.expected_size_bytes:
                    raise ValueError("destination conflict: existing content differs")
                if item.owned_by_build:
                    return BuildOperationResult(
                        item.plan_item_id,
                        item.asset_id,
                        item.source_path,
                        destination,
                        operation,
                        "completed",
                        source_verified=True,
                        destination_verified=True,
                        owned_by_build=True,
                        actual_sha256=digest,
                        actual_size_bytes=size,
                        observed_source_mtime_ns=observed,
                    )
                return BuildOperationResult(
                    item.plan_item_id,
                    item.asset_id,
                    item.source_path,
                    destination,
                    "skip",
                    "skipped",
                    source_verified=True,
                    destination_verified=True,
                    actual_sha256=digest,
                    actual_size_bytes=size,
                    observed_source_mtime_ns=observed,
                )
            if operation == "copy":
                written = self._copy(item, destination, run_id)
            else:
                self._hardlink(item, destination)
                written = 0
            digest, size = sha256_file(destination)
            if digest != item.expected_sha256 or size != item.expected_size_bytes:
                raise ValueError("destination post-write verification failed")
            return BuildOperationResult(
                item.plan_item_id,
                item.asset_id,
                item.source_path,
                destination,
                operation,
                "completed",
                written,
                True,
                True,
                True,
                digest,
                size,
                observed,
            )
        except (OSError, ValueError) as exc:
            return BuildOperationResult(
                item.plan_item_id,
                item.asset_id,
                item.source_path,
                destination,
                operation,
                "failed",
                error=str(exc),
            )

    def _copy(self, item: BuildPlanItem, destination: Path, run_id: int) -> int:
        temporary = destination.parent / f".photo-migrator-{run_id}-{item.plan_item_id}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        written = 0
        try:
            with item.source_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    written += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            digest, size = sha256_file(temporary)
            if digest != item.expected_sha256 or size != item.expected_size_bytes:
                raise ValueError("temporary-file verification failed")
            os.utime(
                temporary,
                ns=(item.source_path.lstat().st_atime_ns, item.source_path.lstat().st_mtime_ns),
                follow_symlinks=False,
            )
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
            fsync_directory(destination.parent)
            return written
        except BaseException:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
            raise

    def _hardlink(self, item: BuildPlanItem, destination: Path) -> None:
        if item.source_path.lstat().st_dev != destination.parent.stat().st_dev:
            raise ValueError("hardlink requires source and destination on the same filesystem")
        os.link(item.source_path, destination, follow_symlinks=False)
        source_info, destination_info = item.source_path.lstat(), destination.lstat()
        if (source_info.st_dev, source_info.st_ino) != (
            destination_info.st_dev,
            destination_info.st_ino,
        ):
            destination.unlink()
            raise ValueError("hardlink inode verification failed")
        fsync_directory(destination.parent)

    def _persist_results(self, run_id: int, results: list[BuildOperationResult]) -> None:
        with self.database.transaction() as connection:
            for result in results:
                connection.execute(
                    """UPDATE build_items SET operation=?,status=?,bytes_written=?,source_verified=?,
                    destination_verified=?,owned_by_build=?,actual_sha256=?,actual_size_bytes=?,observed_source_mtime_ns=?,
                    started_at=COALESCE(started_at,?),finished_at=?,error=? WHERE build_run_id=? AND plan_item_id=? AND asset_id=?""",
                    (
                        result.operation,
                        result.status,
                        result.bytes_written,
                        int(result.source_verified),
                        int(result.destination_verified),
                        int(result.owned_by_build),
                        result.actual_sha256,
                        result.actual_size_bytes,
                        result.observed_source_mtime_ns,
                        utc_now(),
                        utc_now(),
                        result.error,
                        run_id,
                        result.plan_item_id,
                        result.asset_id,
                    ),
                )

    def _finish(self, run_id: int) -> None:
        counts = self.database.connection.execute(
            """SELECT SUM(status='completed') completed,SUM(status='skipped') skipped,
            SUM(status IN ('failed','verification_failed')) failed,SUM(destination_verified) verified,
            SUM(status='verification_failed') verification_failed,COALESCE(SUM(bytes_written),0) bytes FROM build_items WHERE build_run_id=?""",
            (run_id,),
        ).fetchone()
        status = "completed_with_errors" if counts["failed"] else "completed"
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE build_runs SET finished_at=?,status=?,completed_items=?,skipped_items=?,failed_items=?,
                verified_items=?,verification_failed_items=?,bytes_written=? WHERE id=?""",
                (
                    utc_now(),
                    status,
                    counts["completed"] or 0,
                    counts["skipped"] or 0,
                    counts["failed"] or 0,
                    counts["verified"] or 0,
                    counts["verification_failed"] or 0,
                    counts["bytes"],
                    run_id,
                ),
            )

    def reports(self, run_id: int) -> None:
        directory = self.database.path.parent / "reports" / f"build_{run_id}"
        directory.mkdir(parents=True, exist_ok=True)
        run = self.database.connection.execute(
            "SELECT * FROM build_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run
        rows = list(
            self.database.connection.execute(
                "SELECT * FROM build_items WHERE build_run_id=? ORDER BY id", (run_id,)
            )
        )
        summary = {
            "build run ID": run_id,
            "plan ID": run["plan_id"],
            "plan fingerprint": run["plan_fingerprint"],
            "plan status": run["plan_status"],
            "build mode": run["mode"],
            "dry-run state": bool(run["dry_run"]),
            "destination root": run["destination_root"],
            "requested items": run["requested_items"],
            "completed items": run["completed_items"],
            "skipped items": run["skipped_items"],
            "failed items": run["failed_items"],
            "verified items": run["verified_items"],
            "verification failures": run["verification_failed_items"],
            "owned destination files": sum(r["owned_by_build"] for r in rows),
            "pre-existing identical files": sum(
                r["status"] == "skipped" and r["destination_verified"] for r in rows
            ),
            "bytes written": run["bytes_written"],
            "relationship bundles complete": 0,
            "relationship bundles partial": 0,
            "elapsed time": "recorded timestamps",
            "final build status": run["status"],
        }
        atomic_write_text(
            directory / "build_summary.txt", "".join(f"{k}: {v}\n" for k, v in summary.items())
        )
        fields = [
            "id",
            "plan_item_id",
            "asset_id",
            "operation",
            "status",
            "source_path",
            "destination_absolute_path",
            "expected_sha256",
            "actual_sha256",
            "expected_size_bytes",
            "actual_size_bytes",
            "bytes_written",
            "source_verified",
            "destination_verified",
            "owned_by_build",
            "error",
        ]
        self._csv(directory / "build_items.csv", rows, fields)
        self._csv(
            directory / "failed_items.csv",
            [r for r in rows if r["error"]],
            [
                "id",
                "plan_item_id",
                "asset_id",
                "source_path",
                "destination_absolute_path",
                "status",
                "error",
            ],
        )
        self._csv(
            directory / "verification_results.csv",
            rows,
            [
                "id",
                "destination_absolute_path",
                "status",
                "expected_sha256",
                "actual_sha256",
                "expected_size_bytes",
                "actual_size_bytes",
                "error",
            ],
        )
        bundles = []
        for key in sorted({str(r["bundle_key"]) for r in rows}):
            members = [r for r in rows if r["bundle_key"] == key]
            complete = [str(r["asset_id"]) for r in members if r["destination_verified"]]
            failed = [
                str(r["asset_id"])
                for r in members
                if r["status"] in {"failed", "verification_failed"}
            ]
            bundles.append(
                {
                    "bundle_key": key,
                    "relationship_type": key.split(":", 1)[0],
                    "required_asset_ids": ";".join(str(r["asset_id"]) for r in members),
                    "completed_asset_ids": ";".join(complete),
                    "failed_asset_ids": ";".join(failed),
                    "status": "complete" if len(complete) == len(members) else "partial",
                }
            )
        self._csv(
            directory / "bundle_results.csv",
            bundles,
            [
                "bundle_key",
                "relationship_type",
                "required_asset_ids",
                "completed_asset_ids",
                "failed_asset_ids",
                "status",
            ],
        )
        existing = [
            {
                "id": r["id"],
                "destination_absolute_path": r["destination_absolute_path"],
                "status": r["status"],
                "identical": r["destination_verified"],
                "owned_by_build": r["owned_by_build"],
                "reason": "pre-existing identical",
            }
            for r in rows
            if r["status"] == "skipped" and r["destination_verified"]
        ]
        self._csv(
            directory / "existing_destination_items.csv",
            existing,
            ["id", "destination_absolute_path", "status", "identical", "owned_by_build", "reason"],
        )
        for name, fields2 in (
            (
                "rollback_plan.csv",
                [
                    "build_item_id",
                    "destination_path",
                    "owned_by_build",
                    "current_hash_matches",
                    "planned_action",
                    "reason",
                ],
            ),
            ("rollback_results.csv", ["build_item_id", "destination_path", "status", "error"]),
        ):
            path = directory / name
            if not path.exists():
                self._csv(path, [], fields2)

    @staticmethod
    def _csv(path: Path, rows: Sequence[object], fields: list[str]) -> None:
        atomic_write_csv(
            path,
            (
                [
                    row[field] if isinstance(row, (dict, sqlite3.Row)) and field in row else ""
                    for field in fields
                ]
                for row in rows
            ),
            fields,
        )


class Rollback:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(
        self, run_id: int, dry_run: bool = True, confirmed: bool = False
    ) -> list[RollbackResult]:
        if not dry_run and not confirmed:
            raise ValueError("real rollback requires --confirm-owned-files-only")
        run = self.database.connection.execute(
            "SELECT * FROM build_runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"build run {run_id} does not exist")
        root = Path(run["destination_root"])
        results = []
        rows = list(
            self.database.connection.execute(
                "SELECT * FROM build_items WHERE build_run_id=? AND owned_by_build=1 ORDER BY id",
                (run_id,),
            )
        )
        for row in rows:
            path = Path(row["destination_absolute_path"])
            status, error = "skipped", None
            try:
                safe_destination(root, Path(row["destination_relative_path"]))
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("rollback target is not a regular non-symlink file")
                digest, _ = sha256_file(path)
                if digest != row["actual_sha256"]:
                    raise ValueError("rollback target content changed; manual review required")
                status = "planned" if dry_run else "rolled_back"
                if not dry_run:
                    path.unlink()
            except (OSError, ValueError) as exc:
                status = "rollback_failed"
                error = str(exc)
            results.append(RollbackResult(int(row["id"]), path, status, error))
        if not dry_run:
            final = (
                "completed"
                if all(r.status == "rolled_back" for r in results)
                else "completed_with_errors"
            )
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE build_runs SET rollback_status=?,status=? WHERE id=?",
                    (final, "rolled_back" if final == "completed" else "rollback_failed", run_id),
                )
                for result in results:
                    connection.execute(
                        "UPDATE build_items SET status=?,finished_at=?,error=? WHERE id=?",
                        (result.status, utc_now(), result.error, result.build_item_id),
                    )
        directory = self.database.path.parent / "reports" / f"build_{run_id}"
        directory.mkdir(parents=True, exist_ok=True)
        Builder._csv(
            directory / ("rollback_plan.csv" if dry_run else "rollback_results.csv"),
            [
                {
                    "build_item_id": r.build_item_id,
                    "destination_path": str(r.destination_path),
                    "owned_by_build": 1,
                    "current_hash_matches": r.error is None,
                    "planned_action": "delete" if r.error is None else "none",
                    "reason": r.error or "owned unchanged file",
                    "status": r.status,
                    "error": r.error,
                }
                for r in results
            ],
            [
                "build_item_id",
                "destination_path",
                "owned_by_build",
                "current_hash_matches",
                "planned_action",
                "reason",
            ]
            if dry_run
            else ["build_item_id", "destination_path", "status", "error"],
        )
        return results
