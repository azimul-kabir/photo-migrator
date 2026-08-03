"""Incremental, copy-only importer for an existing canonical library."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path

from photo_migrator.atomic_io import atomic_write_csv, atomic_write_text
from photo_migrator.config import Config, ConfigError, SourceConfig
from photo_migrator.database import Database, utc_now
from photo_migrator.filesystem_safety import safe_destination, sha256_file
from photo_migrator.progress import ProgressTracker
from photo_migrator.scanner import Scanner, ScanResult

LOGGER = logging.getLogger(__name__)


def _require(config: Config) -> Path:
    if config.library is None:
        raise ConfigError("[library] root is required for incremental commands")
    return config.library.root


class IncrementalImporter:
    def __init__(self, database: Database, config: Config) -> None:
        self.database, self.config = database, config

    def library_index(self, workers: int = 1, resume: bool = False) -> ScanResult:
        del workers, resume  # hashing is deliberately serialized with SQLite writes
        root = _require(self.config)
        result = Scanner(self.database, self.config).scan(
            (SourceConfig("canonical-library", root, 0),), "canonical"
        )
        totals = self.database.connection.execute(
            """SELECT COUNT(*) AS total_assets, COALESCE(SUM(size_bytes), 0) AS total_bytes,
            COALESCE(SUM(CASE WHEN hash_status='completed' AND sha256 IS NOT NULL THEN 1 ELSE 0 END), 0)
                AS previously_hashed,
            COALESCE(SUM(CASE WHEN hash_status='completed' AND sha256 IS NOT NULL THEN size_bytes ELSE 0 END), 0)
                AS previously_hashed_bytes
            FROM assets WHERE asset_role='canonical' AND scan_status='available'"""
        ).fetchone()
        rows = self.database.connection.execute(
            """SELECT * FROM assets WHERE asset_role='canonical' AND scan_status='available'
            AND (hash_status!='completed' OR sha256 IS NULL) ORDER BY id"""
        ).fetchall()
        tracker = ProgressTracker(
            LOGGER,
            int(totals["total_assets"]),
            int(totals["total_bytes"]),
            int(totals["previously_hashed"]),
            int(totals["previously_hashed_bytes"]),
        )
        LOGGER.info("Canonical assets: %s", f"{tracker.total_assets:,}")
        LOGGER.info("Already hashed: %s", f"{tracker.previously_hashed:,}")
        LOGGER.info("Remaining: %s", f"{len(rows):,}")
        if not rows:
            LOGGER.info("Canonical library already fully indexed.")
            self._library_reports()
            return result
        if tracker.previously_hashed:
            LOGGER.info("Resuming previous index...")
        for row in rows:
            try:
                digest, size = sha256_file(Path(row["absolute_path"]))
                if size != row["size_bytes"]:
                    raise ValueError("canonical size changed while hashing")
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed',hash_completed_at=? WHERE id=?",
                        (digest, utc_now(), row["id"]),
                    )
                tracker.record_success(size)
            except (OSError, ValueError) as exc:
                result.errors.append(f"hash error for {row['absolute_path']}: {exc}")
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE assets SET hash_status='failed',hash_error=? WHERE id=?",
                        (str(exc), row["id"]),
                    )
                tracker.record_failure()
        self._library_reports()
        LOGGER.info(tracker.final_summary())
        return result

    def import_scan(self) -> ScanResult:
        _require(self.config)
        return Scanner(self.database, self.config).scan(self.config.sources, "candidate")

    def plan(self, source: str | None = None, limit: int | None = None) -> int:
        root = _require(self.config)
        if self.config.imports is None:
            raise ConfigError("[imports] is required for import-plan")
        sql = "SELECT * FROM assets WHERE asset_role='candidate' AND scan_status='available'"
        params: list[object] = []
        if source:
            sql += " AND source_name=?"
            params.append(source)
        sql += " ORDER BY source_name,relative_path,id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        candidates = self.database.connection.execute(sql, params).fetchall()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO import_plans(created_at,status,library_root,source_filter,candidate_count) VALUES (?,'planning',?,?,?)",
                (utc_now(), str(root), source, len(candidates)),
            )
            plan_id = int(cursor.lastrowid or 0)
        new = duplicates = collisions = avoided = 0
        for candidate in candidates:
            canon = self.database.connection.execute(
                "SELECT * FROM assets WHERE asset_role='canonical' AND scan_status='available' AND size_bytes=? ORDER BY absolute_path",
                (candidate["size_bytes"],),
            ).fetchall()
            digest = None
            match: sqlite3.Row | None = None
            if canon:
                digest, observed = sha256_file(Path(candidate["absolute_path"]))
                if observed != candidate["size_bytes"]:
                    raise ValueError(
                        f"candidate changed during planning: {candidate['absolute_path']}"
                    )
                match = next((row for row in canon if row["sha256"] == digest), None)
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed',hash_completed_at=? WHERE id=?",
                        (digest, utc_now(), candidate["id"]),
                    )
            if match is not None:
                action, relative, reason = (
                    "duplicate_existing",
                    None,
                    "same size and SHA-256 as canonical asset",
                )
                duplicates += 1
                avoided += candidate["size_bytes"]
            else:
                action, relative, reason = (
                    "new",
                    self._destination(candidate),
                    "content is not in canonical library",
                )
                relative, collided, reused = self._resolve_collision(
                    root, relative, candidate, digest
                )
                collisions += int(collided)
                if reused is not None:
                    action, match, reason = (
                        "reuse_destination",
                        reused,
                        "destination already contains identical content",
                    )
                    duplicates += 1
                    avoided += candidate["size_bytes"]
                else:
                    new += 1
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,destination_relative_path,
                    canonical_asset_id,matching_canonical_path,expected_size_bytes,expected_sha256,reason)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id,
                        candidate["id"],
                        action,
                        relative.as_posix() if relative else None,
                        match["id"] if match else None,
                        match["absolute_path"] if match else None,
                        candidate["size_bytes"],
                        digest,
                        reason,
                    ),
                )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE import_plans SET status='ready',new_count=?,duplicate_count=?,collision_count=?,bytes_avoided=? WHERE id=?",
                (new, duplicates, collisions, avoided, plan_id),
            )
        self._plan_reports(plan_id)
        return plan_id

    def run(self, plan_id: int, dry_run: bool, confirm: bool) -> int:
        if not dry_run and not confirm:
            raise ValueError("real imports require --confirm")
        root = _require(self.config)
        plan = self.database.connection.execute(
            "SELECT * FROM import_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None or Path(plan["library_root"]) != root:
            raise ValueError("unknown plan or configured library differs from plan snapshot")
        prior = self.database.connection.execute(
            "SELECT * FROM import_runs WHERE plan_id=? AND dry_run=? ORDER BY id DESC LIMIT 1",
            (plan_id, int(dry_run)),
        ).fetchone()
        if prior is not None and prior["status"] in {"running", "completed_with_errors"}:
            run_id = int(prior["id"])
        else:
            with self.database.transaction() as connection:
                cur = connection.execute(
                    "INSERT INTO import_runs(plan_id,started_at,status,dry_run) VALUES (?,?,'running',?)",
                    (plan_id, utc_now(), int(dry_run)),
                )
                run_id = int(cur.lastrowid or 0)
        rows = self.database.connection.execute(
            """SELECT i.*,a.absolute_path,a.filename,a.extension,a.mtime_ns,a.source_name,a.relative_path
            FROM import_plan_items i JOIN assets a ON a.id=i.candidate_asset_id
            WHERE i.plan_id=? AND i.action='new' AND NOT EXISTS
            (SELECT 1 FROM import_run_items r WHERE r.run_id=? AND r.plan_item_id=i.id AND r.status IN ('copied','dry_run'))
            ORDER BY i.id""",
            (plan_id, run_id),
        ).fetchall()
        copied = failed = written = 0
        for row in rows:
            destination = safe_destination(root, Path(row["destination_relative_path"]))
            try:
                if dry_run:
                    status, digest, size, owned = (
                        "dry_run",
                        row["expected_sha256"],
                        row["expected_size_bytes"],
                        0,
                    )
                else:
                    digest, size = self._copy(
                        Path(row["absolute_path"]), destination, root, run_id, row["id"]
                    )
                    status, owned = "copied", 1
                    copied += 1
                    written += size
                    self._index_created(destination, row, digest, size)
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO import_run_items(run_id,plan_item_id,status,destination_path,sha256,size_bytes,owned,error) VALUES (?,?,?,?,?,?,?,NULL)",
                        (run_id, row["id"], status, str(destination), digest, size, owned),
                    )
            except (OSError, ValueError) as exc:
                failed += 1
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO import_run_items(run_id,plan_item_id,status,destination_path,owned,error) VALUES (?,?,'failed',?,0,?)",
                        (run_id, row["id"], str(destination), str(exc)),
                    )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE import_runs SET finished_at=?,status=?,copied_count=copied_count+?,failed_count=failed_count+?,bytes_written=bytes_written+? WHERE id=?",
                (
                    utc_now(),
                    "completed_with_errors" if failed else "completed",
                    copied,
                    failed,
                    written,
                    run_id,
                ),
            )
        self._run_reports(run_id)
        return run_id

    def _destination(self, row: sqlite3.Row) -> Path:
        assert self.config.imports is not None
        mapping = dict(self.config.imports.source_destinations)
        base = mapping.get(row["source_name"], self.config.imports.default_directory)
        return base / (
            Path(row["relative_path"])
            if self.config.imports.preserve_source_subdirectories
            else Path(row["filename"])
        )

    def _resolve_collision(
        self, root: Path, relative: Path, row: sqlite3.Row, digest: str | None
    ) -> tuple[Path, bool, sqlite3.Row | None]:
        destination = safe_destination(root, relative)
        if not destination.exists():
            return relative, False, None
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(f"unsafe destination collision: {destination}")
        existing_hash, existing_size = sha256_file(destination)
        candidate_hash = digest or sha256_file(Path(row["absolute_path"]))[0]
        if existing_size == row["size_bytes"] and existing_hash == candidate_hash:
            canonical = self.database.connection.execute(
                "SELECT * FROM assets WHERE absolute_path=?", (str(destination),)
            ).fetchone()
            if canonical is None:
                self._index_path(destination, "canonical-library", "canonical", existing_hash)
                canonical = self.database.connection.execute(
                    "SELECT * FROM assets WHERE absolute_path=?", (str(destination),)
                ).fetchone()
            return relative, True, canonical
        suffix = f"__import_{int(row['id']):08d}"
        candidate = relative.with_name(f"{relative.stem}{suffix}{relative.suffix}")
        while safe_destination(root, candidate).exists():
            candidate = candidate.with_name(f"{candidate.stem}_1{candidate.suffix}")
        return candidate, True, None

    @staticmethod
    def _copy(
        source: Path, destination: Path, root: Path, run_id: int, item_id: int
    ) -> tuple[str, int]:
        safe_destination(root, destination.relative_to(root))
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Recheck resolved containment and every created parent before opening the temporary.
        safe_destination(root, destination.relative_to(root))
        temporary = destination.parent / f".photo-migrator-import-{run_id}-{item_id}.tmp"
        digest = hashlib.sha256()
        size = 0
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            check_hash, check_size = sha256_file(temporary)
            if (check_hash, check_size) != (digest.hexdigest(), size):
                raise ValueError("temporary copy verification failed")
            os.link(temporary, destination, follow_symlinks=False)
            final_hash, final_size = sha256_file(destination)
            if (final_hash, final_size) != (check_hash, check_size):
                destination.unlink()
                raise ValueError("post-placement verification failed")
            return final_hash, final_size
        finally:
            temporary.unlink(missing_ok=True)

    def _index_created(self, path: Path, row: object, digest: str, size: int) -> None:
        self._index_path(path, "canonical-library", "canonical", digest)

    def _index_path(self, path: Path, source: str, role: str, digest: str) -> None:
        stat_result = path.stat()
        root = _require(self.config)
        self.database.upsert_asset(
            {
                "source_name": source,
                "source_priority": 0,
                "absolute_path": str(path.resolve()),
                "relative_path": path.resolve().relative_to(root).as_posix(),
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "device_id": stat_result.st_dev,
                "inode": stat_result.st_ino,
                "media_type": "video"
                if path.suffix.lower() in {".mov", ".mp4", ".m4v"}
                else "image",
                "asset_role": role,
            }
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed',hash_completed_at=? WHERE absolute_path=?",
                (digest, utc_now(), str(path.resolve())),
            )

    def _library_reports(self) -> None:
        report = self.database.path.parent / "reports"
        summary = self.database.connection.execute(
            "SELECT COUNT(*) count,COALESCE(SUM(size_bytes),0) bytes,SUM(hash_status='completed') hashed FROM assets WHERE asset_role='canonical' AND scan_status='available'"
        ).fetchone()
        atomic_write_text(
            report / "library_summary.txt",
            f"canonical_file_count={summary['count']}\ncanonical_bytes={summary['bytes']}\ncanonical_hashed_count={summary['hashed'] or 0}\n",
        )
        rows = self.database.connection.execute(
            "SELECT sha256,size_bytes,COUNT(*) count,GROUP_CONCAT(absolute_path,' | ') paths FROM assets WHERE asset_role='canonical' AND sha256 IS NOT NULL GROUP BY sha256,size_bytes HAVING COUNT(*)>1 ORDER BY sha256"
        ).fetchall()
        atomic_write_csv(
            report / "canonical_duplicates.csv",
            ((r["sha256"], r["size_bytes"], r["count"], r["paths"]) for r in rows),
            ("sha256", "size_bytes", "count", "paths"),
        )

    def _plan_reports(self, plan_id: int) -> None:
        base = self.database.path.parent / "reports" / f"import_plan_{plan_id}"
        rows = self.database.connection.execute(
            "SELECT i.*,a.absolute_path FROM import_plan_items i JOIN assets a ON a.id=i.candidate_asset_id WHERE plan_id=? ORDER BY i.id",
            (plan_id,),
        ).fetchall()
        headers = (
            "candidate_path",
            "destination_relative_path",
            "matching_canonical_path",
            "size_bytes",
            "sha256",
            "reason",
        )
        for filename, actions in (
            ("new_files.csv", {"new"}),
            ("existing_duplicates.csv", {"duplicate_existing", "reuse_destination"}),
            ("name_collisions.csv", set()),
            ("review_items.csv", {"review"}),
        ):
            selected = (
                rows
                if filename == "name_collisions.csv"
                else [r for r in rows if r["action"] in actions]
            )
            if filename == "name_collisions.csv":
                selected = [r for r in selected if "destination" in r["reason"]]
            atomic_write_csv(
                base / filename,
                (
                    (
                        r["absolute_path"],
                        r["destination_relative_path"],
                        r["matching_canonical_path"],
                        r["expected_size_bytes"],
                        r["expected_sha256"],
                        r["reason"],
                    )
                    for r in selected
                ),
                headers,
            )

    def _run_reports(self, run_id: int) -> None:
        base = self.database.path.parent / "reports" / f"import_run_{run_id}"
        rows = self.database.connection.execute(
            "SELECT * FROM import_run_items WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        atomic_write_csv(
            base / "copied_files.csv",
            (
                (r["destination_path"], r["sha256"], r["size_bytes"], r["owned"])
                for r in rows
                if r["status"] in {"copied", "dry_run"}
            ),
            ("destination_path", "sha256", "size_bytes", "owned"),
        )
        atomic_write_csv(
            base / "failures.csv",
            ((r["destination_path"], r["error"]) for r in rows if r["status"] == "failed"),
            ("destination_path", "error"),
        )
        atomic_write_csv(
            base / "verification.csv",
            ((r["destination_path"], r["status"], r["sha256"], r["size_bytes"]) for r in rows),
            ("destination_path", "status", "sha256", "size_bytes"),
        )
