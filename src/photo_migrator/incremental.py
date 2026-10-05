"""Incremental, copy-only importer for an existing canonical library."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from photo_migrator.atomic_io import atomic_write_csv, atomic_write_text
from photo_migrator.config import Config, ConfigError, SourceConfig
from photo_migrator.database import Database, utc_now
from photo_migrator.filesystem_safety import contained, safe_destination, sha256_file
from photo_migrator.media_types import media_type_for
from photo_migrator.progress import (
    ProgressListener,
    ProgressTracker,
    flush_logger,
    format_bytes,
    format_duration,
)
from photo_migrator.scanner import Scanner, ScanResult

LOGGER = logging.getLogger(__name__)


def _claim_key(relative: Path) -> str:
    """Compare planned destinations case-insensitively, as macOS and SMB shares do."""
    return relative.as_posix().casefold()


@dataclass
class _PlanItem:
    candidate: sqlite3.Row
    action: str
    reason: str
    digest: str | None = None
    relative: Path | None = None
    match_id: int | None = None
    match_path: str | None = None
    collided: bool = False
    unreadable: bool = False
    keeper: _PlanItem | None = None


def _require(config: Config) -> Path:
    if config.library is None:
        raise ConfigError("[library] root is required for incremental commands")
    return config.library.root


class IncrementalImporter:
    def __init__(
        self,
        database: Database,
        config: Config,
        progress_listener: ProgressListener | None = None,
    ) -> None:
        self.database, self.config = database, config
        self.progress_listener = progress_listener

    def _tracker(self, **settings: Any) -> ProgressTracker:
        return ProgressTracker(LOGGER, listener=self.progress_listener, **settings)

    def library_index(self, workers: int = 1, resume: bool = False) -> ScanResult:
        if workers != 1:
            # Hashing is deliberately serialized with SQLite writes.
            LOGGER.warning("library-index ignores --workers=%s and hashes serially", workers)
        root = _require(self.config)
        before = self.database.connection.execute(
            """SELECT COUNT(*) AS total_assets, COALESCE(SUM(size_bytes), 0) AS total_bytes,
            COALESCE(SUM(CASE WHEN hash_status='completed' AND sha256 IS NOT NULL THEN 1 ELSE 0 END), 0)
                AS previously_hashed
            FROM assets WHERE asset_role='canonical' AND scan_status='available'"""
        ).fetchone()
        expected_assets = int(before["total_assets"])
        already_hashed = int(before["previously_hashed"])
        if resume:
            LOGGER.info("Fast resume requested.")
            LOGGER.info("Canonical rescan skipped.")
        LOGGER.info(
            "%sCanonical assets : %s", "Existing " if resume else "", f"{expected_assets:,}"
        )
        LOGGER.info("Already hashed   : %s", f"{already_hashed:,}")
        LOGGER.info("Remaining hashes : %s", f"{expected_assets - already_hashed:,}")
        LOGGER.info("")
        result = ScanResult()
        if not resume:
            LOGGER.info("Phase 1/2: Scanning canonical library...")
            scan_tracker = self._tracker(
                phase_name="Scanning canonical library",
                total_items=expected_assets,
                total_bytes=int(before["total_bytes"]),
                phase="Phase 1/2",
                verb="Scanned",
            )

            def record_scan(size: int | None) -> None:
                if size is None:
                    scan_tracker.record_failure()
                else:
                    scan_tracker.record_success(size)

            result = Scanner(self.database, self.config).scan(
                (SourceConfig("canonical-library", root, 0),), "canonical", record_scan
            )
            LOGGER.info("Phase 1 complete.")
            LOGGER.info("")
        LOGGER.info("Phase 2/2: Hashing remaining canonical assets...")
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
        tracker = self._tracker(
            phase_name="Hashing remaining canonical assets",
            total_items=int(totals["total_assets"]),
            total_bytes=int(totals["total_bytes"]),
            initial_items=int(totals["previously_hashed"]),
            initial_bytes=int(totals["previously_hashed_bytes"]),
            phase="Phase 2/2",
            verb="Indexed",
            byte_based=True,
        )
        review: list[tuple[object, ...]] = []
        if not rows:
            LOGGER.info("Canonical library already fully indexed.")
            if resume:
                self._resume_report(review)
            self._library_reports()
            LOGGER.info(self._index_summary(tracker))
            LOGGER.info("Library indexing complete.")
            return result
        if tracker.initial_items:
            LOGGER.info("Resuming previous index...")
        for row in rows:
            try:
                path = Path(row["absolute_path"])
                if resume:
                    reason, observed_size, observed_mtime = self._resume_validation(path, root, row)
                    if reason is not None:
                        review.append(
                            (
                                row["id"],
                                row["absolute_path"],
                                reason,
                                row["size_bytes"],
                                observed_size,
                                row["mtime_ns"],
                                observed_mtime,
                            )
                        )
                        self._mark_resume_review(row, reason)
                        result.errors.append(f"fast resume skipped {path}: {reason}")
                        tracker.record_failure()
                        continue
                digest, size = sha256_file(path)
                info = path.lstat()
                if size != row["size_bytes"] or info.st_size != row["size_bytes"]:
                    raise ValueError("canonical size changed while hashing")
                if info.st_mtime_ns != row["mtime_ns"]:
                    raise ValueError("canonical mtime changed while hashing")
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
        if resume:
            self._resume_report(review)
        self._library_reports()
        remaining = self.database.connection.execute(
            """SELECT COUNT(*) FROM assets WHERE asset_role='canonical'
            AND scan_status='available' AND (hash_status!='completed' OR sha256 IS NULL)"""
        ).fetchone()[0]
        missing = sum(item[2] == "missing" for item in review)
        LOGGER.info(self._index_summary(tracker, len(review) - missing, missing, int(remaining)))
        LOGGER.info("Library indexing complete.")
        return result

    @staticmethod
    def _index_summary(
        tracker: ProgressTracker,
        refresh_required: int = 0,
        missing: int = 0,
        remaining: int = 0,
    ) -> str:
        return (
            "Canonical indexing completed\n\n"
            "Assets:\n"
            f"  Total ............ {tracker.total_items:,}\n"
            f"  Newly hashed ..... {tracker.succeeded:,}\n"
            f"  Previously hashed  {tracker.initial_items:,}\n"
            f"  Failed ........... {tracker.failed:,}\n\n"
            f"  Missing .......... {missing:,}\n"
            f"  Refresh required . {refresh_required:,}\n"
            f"  Remaining ........ {remaining:,}\n\n"
            "Data:\n"
            f"  Processed ........ {format_bytes(tracker.completed_bytes)}\n"
            f"  Elapsed .......... {format_duration(tracker.elapsed)}\n"
            f"  Average speed .... {format_bytes(round(tracker.average_bytes_per_second))}/s"
        )

    @staticmethod
    def _resume_validation(
        path: Path, root: Path, row: sqlite3.Row
    ) -> tuple[str | None, int | None, int | None]:
        """Validate an inventory entry without following a final-component symlink."""
        if not contained(path.absolute(), root.absolute()):
            return "unsafe_path", None, None
        try:
            info = path.lstat()
        except FileNotFoundError:
            return "missing", None, None
        except OSError:
            return "unsafe_path", None, None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return "non_regular_file", info.st_size, info.st_mtime_ns
        try:
            if not contained(path.resolve(strict=True), root.resolve(strict=True)):
                return "unsafe_path", info.st_size, info.st_mtime_ns
        except OSError:
            return "unsafe_path", info.st_size, info.st_mtime_ns
        if info.st_size != row["size_bytes"]:
            return "size_changed", info.st_size, info.st_mtime_ns
        if info.st_mtime_ns != row["mtime_ns"]:
            return "mtime_changed", info.st_size, info.st_mtime_ns
        return None, info.st_size, info.st_mtime_ns

    def _mark_resume_review(self, row: sqlite3.Row, reason: str) -> None:
        with self.database.transaction() as connection:
            if reason == "missing":
                connection.execute(
                    "UPDATE assets SET scan_status='missing',missing_since=?,hash_error=? WHERE id=?",
                    (utc_now(), "refresh_required: missing", row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE assets SET hash_status='failed',hash_error=? WHERE id=?",
                    (f"refresh_required: {reason}", row["id"]),
                )

    def _resume_report(self, rows: list[tuple[object, ...]]) -> None:
        atomic_write_csv(
            self.database.path.parent / "reports" / "library_resume_review.csv",
            rows,
            (
                "asset_id",
                "absolute_path",
                "reason",
                "stored_size",
                "observed_size",
                "stored_mtime_ns",
                "observed_mtime_ns",
            ),
        )

    def import_scan(self) -> ScanResult:
        _require(self.config)
        known = self.database.connection.execute(
            """SELECT COUNT(*) AS total_assets, COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM assets WHERE asset_role='candidate' AND scan_status='available'"""
        ).fetchone()
        tracker = self._tracker(
            phase_name="Scanning candidate sources",
            total_items=int(known["total_assets"]),
            total_bytes=int(known["total_bytes"]),
            verb="Scanned",
        )

        def record(size: int | None) -> None:
            if size is None:
                tracker.record_failure()
            else:
                tracker.record_success(size)

        return Scanner(self.database, self.config).scan(self.config.sources, "candidate", record)

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
        unhashed = self.database.connection.execute(
            """SELECT COUNT(*) FROM assets WHERE asset_role='canonical' AND scan_status='available'
            AND (hash_status!='completed' OR sha256 IS NULL)"""
        ).fetchone()[0]
        if unhashed:
            LOGGER.warning(
                "%s canonical assets are not hashed; candidates of the same size are marked for "
                "review. Run library-index (or library-index --resume) and re-plan to resolve them.",
                f"{unhashed:,}",
            )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO import_plans(created_at,status,library_root,source_filter,candidate_count) VALUES (?,'planning',?,?,?)",
                (utc_now(), str(root), source, len(candidates)),
            )
            plan_id = int(cursor.lastrowid or 0)
        try:
            items = self._plan_items(root, candidates)
            counts = Counter(item.action for item in items)
            avoided = sum(
                int(item.candidate["size_bytes"])
                for item in items
                if item.action in {"duplicate_existing", "duplicate_candidate", "reuse_destination"}
            )
            with self.database.transaction() as connection:
                connection.executemany(
                    """INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,destination_relative_path,
                    canonical_asset_id,matching_canonical_path,expected_size_bytes,expected_sha256,reason)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            plan_id,
                            item.candidate["id"],
                            item.action,
                            item.relative.as_posix() if item.relative else None,
                            item.match_id,
                            item.match_path,
                            item.candidate["size_bytes"],
                            item.digest,
                            item.reason,
                        )
                        for item in items
                    ),
                )
                connection.execute(
                    """UPDATE import_plans SET status='ready',new_count=?,duplicate_count=?,
                    internal_duplicate_count=?,review_count=?,collision_count=?,bytes_avoided=?
                    WHERE id=?""",
                    (
                        counts["new"],
                        counts["duplicate_existing"] + counts["reuse_destination"],
                        counts["duplicate_candidate"],
                        counts["review"],
                        sum(item.collided for item in items),
                        avoided,
                        plan_id,
                    ),
                )
        except BaseException:
            with self.database.transaction() as connection:
                connection.execute("UPDATE import_plans SET status='failed' WHERE id=?", (plan_id,))
            raise
        self._plan_reports(plan_id)
        LOGGER.info(
            "Import plan %s: new=%s already_in_library=%s duplicates_within_sources=%s review=%s",
            plan_id,
            f"{counts['new']:,}",
            f"{counts['duplicate_existing'] + counts['reuse_destination']:,}",
            f"{counts['duplicate_candidate']:,}",
            f"{counts['review']:,}",
        )
        return plan_id

    def _plan_items(self, root: Path, candidates: list[sqlite3.Row]) -> list[_PlanItem]:
        sizes = Counter(int(candidate["size_bytes"]) for candidate in candidates)
        tracker = self._tracker(
            phase_name="Planning import", total_items=len(candidates), verb="Compared"
        )
        # Pass 1: compare each candidate with the canonical library.
        items: list[_PlanItem] = []
        for candidate in candidates:
            item = self._classify(candidate, sizes[int(candidate["size_bytes"])] > 1)
            items.append(item)
            if item.unreadable:
                tracker.record_failure()
            else:
                tracker.record_success(int(candidate["size_bytes"]) if item.digest else 0)
        # Pass 2: import one candidate per identical-content group, preferring source priority.
        groups: dict[tuple[int, str], list[_PlanItem]] = {}
        for item in items:
            if item.action == "new" and item.digest is not None:
                key = (int(item.candidate["size_bytes"]), item.digest)
                groups.setdefault(key, []).append(item)
        for members in groups.values():
            keeper = min(members, key=lambda member: -int(member.candidate["source_priority"]))
            for member in members:
                if member is not keeper:
                    member.action, member.keeper = "duplicate_candidate", keeper
        # Pass 3: assign destinations in candidate order, reserving each one for this plan.
        claimed: set[str] = set()
        for item in items:
            if item.action != "new":
                continue
            try:
                relative, item.collided, reused, item.digest = self._resolve_collision(
                    root, self._destination(item.candidate), item.candidate, item.digest, claimed
                )
            except (OSError, ValueError) as exc:
                LOGGER.error(
                    "import-plan could not plan a destination for %s: %s",
                    item.candidate["absolute_path"],
                    exc,
                )
                item.action, item.reason = "review", f"destination could not be planned: {exc}"
                continue
            if reused is not None:
                item.action, item.reason = (
                    "reuse_destination",
                    "destination already contains identical content",
                )
                item.match_id, item.match_path = int(reused["id"]), str(reused["absolute_path"])
            else:
                item.relative = relative
                claimed.add(_claim_key(relative))
        # Identical candidates follow the outcome of the one being imported.
        for item in items:
            if item.keeper is None:
                continue
            keeper_path = item.keeper.candidate["absolute_path"]
            if item.keeper.action == "new" and item.keeper.relative is not None:
                item.match_path = str(root / item.keeper.relative)
                item.reason = (
                    f"same size and SHA-256 as candidate {keeper_path}, which this plan imports"
                )
            elif item.keeper.action == "reuse_destination":
                item.match_id, item.match_path = item.keeper.match_id, item.keeper.match_path
                item.reason = (
                    f"same size and SHA-256 as candidate {keeper_path}, already in the library"
                )
            else:
                item.action = "review"
                item.reason = f"identical candidate {keeper_path} needs review"
        return items

    def _classify(self, candidate: sqlite3.Row, shares_size: bool) -> _PlanItem:
        canon = self.database.connection.execute(
            "SELECT * FROM assets WHERE asset_role='canonical' AND scan_status='available' AND size_bytes=? ORDER BY absolute_path",
            (candidate["size_bytes"],),
        ).fetchall()
        if not canon and not shares_size:
            return _PlanItem(candidate, "new", "content is not in canonical library")
        try:
            digest = self._hash_candidate(candidate)
        except (OSError, ValueError) as exc:
            LOGGER.error(
                "import-plan could not hash candidate %s: %s", candidate["absolute_path"], exc
            )
            return _PlanItem(
                candidate,
                "review",
                f"candidate could not be read during planning: {exc}",
                unreadable=True,
            )
        match = next(
            (row for row in canon if row["hash_status"] == "completed" and row["sha256"] == digest),
            None,
        )
        if match is not None:
            return _PlanItem(
                candidate,
                "duplicate_existing",
                "same size and SHA-256 as canonical asset",
                digest=digest,
                match_id=int(match["id"]),
                match_path=str(match["absolute_path"]),
            )
        if any(row["hash_status"] != "completed" or row["sha256"] is None for row in canon):
            return _PlanItem(
                candidate,
                "review",
                "a canonical asset of the same size is not hashed; run library-index and re-plan",
                digest=digest,
            )
        return _PlanItem(candidate, "new", "content is not in canonical library", digest=digest)

    def _hash_candidate(self, candidate: sqlite3.Row) -> str:
        path = Path(candidate["absolute_path"])
        digest, observed = sha256_file(path)
        info = path.lstat()
        if observed != candidate["size_bytes"] or info.st_size != candidate["size_bytes"]:
            raise ValueError("candidate size changed since import-scan; run import-scan again")
        if candidate["mtime_ns"] is not None and info.st_mtime_ns != candidate["mtime_ns"]:
            raise ValueError("candidate mtime changed since import-scan; run import-scan again")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed',hash_completed_at=? WHERE id=?",
                (digest, utc_now(), candidate["id"]),
            )
        return digest

    def run(self, plan_id: int, dry_run: bool, confirm: bool) -> int:
        if not dry_run and not confirm:
            raise ValueError("real imports require --confirm")
        root = _require(self.config)
        plan = self.database.connection.execute(
            "SELECT * FROM import_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None or Path(plan["library_root"]) != root:
            raise ValueError("unknown plan or configured library differs from plan snapshot")
        if plan["status"] != "ready":
            raise ValueError("import plan is not ready; regenerate it from current inventory")
        if not dry_run:
            latest_real = self.database.connection.execute(
                "SELECT id,status FROM import_runs WHERE plan_id=? AND dry_run=0 ORDER BY id DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
            if latest_real is not None and latest_real["status"] == "completed":
                LOGGER.info(
                    "Import plan %s was already completed by run %s; nothing to import.",
                    plan_id,
                    latest_real["id"],
                )
                return int(latest_real["id"])
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
        action_totals = self.database.connection.execute(
            """SELECT COUNT(*) AS planned,
            COALESCE(SUM(CASE WHEN action IN ('duplicate_existing','duplicate_candidate') THEN 1 ELSE 0 END),0) AS skipped,
            COALESCE(SUM(CASE WHEN action='review' THEN 1 ELSE 0 END),0) AS review,
            COALESCE(SUM(CASE WHEN action='reuse_destination' THEN 1 ELSE 0 END),0) AS reused,
            COALESCE(SUM(CASE WHEN action='new' THEN expected_size_bytes ELSE 0 END),0) AS bytes
            FROM import_plan_items WHERE plan_id=?""",
            (plan_id,),
        ).fetchone()
        # Item rows, not run counters, survive a hard interruption; use the same rule as below.
        done = self.database.connection.execute(
            """SELECT COUNT(*) AS items,
            COALESCE(SUM(CASE WHEN r.status='copied' THEN r.size_bytes ELSE 0 END),0) AS written
            FROM import_run_items r JOIN import_runs u ON u.id=r.run_id
            JOIN import_plan_items i ON i.id=r.plan_item_id
            WHERE i.plan_id=? AND ((r.run_id=? AND r.status='dry_run')
                OR (u.dry_run=0 AND r.status IN ('copied','verified_existing')))""",
            (plan_id, run_id),
        ).fetchone()
        already_copied = int(done["items"])
        already_written = int(done["written"])
        skipped = int(action_totals["skipped"])
        reused = int(action_totals["reused"])
        review = int(action_totals["review"])
        total_planned = int(action_totals["planned"])
        total_bytes = int(action_totals["bytes"])
        LOGGER.info("Import plan            : %s", plan_id)
        LOGGER.info("Files to import        : %s", f"{total_planned:,}")
        LOGGER.info("Existing duplicates    : %s", f"{skipped:,}")
        LOGGER.info("Destination reuse      : %s", f"{reused:,}")
        LOGGER.info("Needs review           : %s", f"{review:,}")
        LOGGER.info("Data to copy           : %s", format_bytes(total_bytes))
        LOGGER.info("")
        LOGGER.info("Destination            : %s", root)
        LOGGER.info("")
        if already_copied:
            LOGGER.info("Resuming import")
            LOGGER.info("")
            LOGGER.info("Already copied ...... %s", f"{already_copied:,}")
            LOGGER.info(
                "Remaining ........... %s",
                f"{max(0, int(plan['new_count']) - already_copied):,}",
            )
            LOGGER.info("")
        LOGGER.info("Phase 1/1: Importing files...")
        flush_logger(LOGGER)
        rows = self.database.connection.execute(
            """SELECT i.*,a.absolute_path,a.filename,a.extension,a.mtime_ns,a.source_name,a.relative_path
            FROM import_plan_items i JOIN assets a ON a.id=i.candidate_asset_id
            WHERE i.plan_id=? AND i.action='new' AND NOT EXISTS
            (SELECT 1 FROM import_run_items r JOIN import_runs u ON u.id=r.run_id WHERE r.plan_item_id=i.id
                AND ((r.run_id=? AND r.status='dry_run')
                    OR (u.dry_run=0 AND r.status IN ('copied','verified_existing'))))
            ORDER BY i.id""",
            (plan_id, run_id),
        ).fetchall()
        tracker = self._tracker(
            phase_name="Importing files",
            total_items=total_planned,
            total_bytes=total_bytes,
            initial_items=skipped + reused + review + already_copied,
            initial_bytes=already_written,
            phase="Phase 1/1",
            verb="Imported",
            byte_based=True,
            item_based_percentage=True,
            item_noun="files",
            count_failures_as_completed=True,
        )
        for row in rows:
            destination = root / str(row["destination_relative_path"])
            tracker.current_item = str(row["destination_relative_path"])
            try:
                destination = safe_destination(root, Path(row["destination_relative_path"]))
                existing = None if dry_run else self._existing_copy(row, destination)
                if existing is not None:
                    # A previous attempt placed this file but stopped before recording it.
                    digest, size = existing
                    status, owned = "verified_existing", 0
                    self._index_created(destination, row, digest, size)
                elif dry_run:
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
                    self._index_created(destination, row, digest, size)
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO import_run_items(run_id,plan_item_id,status,destination_path,sha256,size_bytes,owned,error) VALUES (?,?,?,?,?,?,?,NULL)",
                        (run_id, row["id"], status, str(destination), digest, size, owned),
                    )
                tracker.record_success(size if status == "copied" else 0)
            except (OSError, ValueError) as exc:
                LOGGER.error("import failed for %s: %s", row["absolute_path"], exc)
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO import_run_items(run_id,plan_item_id,status,destination_path,owned,error) VALUES (?,?,'failed',?,0,?)",
                        (run_id, row["id"], str(destination), str(exc)),
                    )
                tracker.record_failure()
        # Totals are recomputed from item rows so resumed runs never double count retries.
        final = self.database.connection.execute(
            """SELECT COALESCE(SUM(status='copied'),0) AS copied,
            COALESCE(SUM(status='verified_existing'),0) AS reused,
            COALESCE(SUM(status='failed'),0) AS failed,
            COALESCE(SUM(CASE WHEN status='copied' THEN size_bytes ELSE 0 END),0) AS written
            FROM import_run_items WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        failed = int(final["failed"])
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE import_runs SET finished_at=?,status=?,copied_count=?,reused_count=?,failed_count=?,bytes_written=? WHERE id=?",
                (
                    utc_now(),
                    "completed_with_errors" if failed else "completed",
                    final["copied"],
                    final["reused"],
                    failed,
                    final["written"],
                    run_id,
                ),
            )
        self._run_reports(run_id)
        LOGGER.info(
            self._import_summary(
                tracker,
                total_planned,
                int(final["copied"]),
                reused + int(final["reused"]),
                skipped,
                review,
                failed,
                root,
            )
        )
        LOGGER.info("Import complete.")
        flush_logger(LOGGER)
        return run_id

    @staticmethod
    def _import_summary(
        tracker: ProgressTracker,
        planned: int,
        copied: int,
        reused: int,
        skipped: int,
        review: int,
        failed: int,
        destination: Path,
    ) -> str:
        return (
            "Import Summary\n"
            "==============\n\n"
            "Files\n\n"
            f"Planned ............ {planned:,}\n"
            f"Copied ............. {copied:,}\n"
            f"Reused ............. {reused:,}\n"
            f"Skipped ............ {skipped:,}\n"
            f"Needs review ....... {review:,}\n"
            f"Failed ............. {failed:,}\n\n"
            "Data\n\n"
            f"Copied ............. {format_bytes(tracker.completed_bytes)}\n\n"
            f"Elapsed ............ {format_duration(tracker.elapsed)}\n"
            f"Average speed ...... {format_bytes(round(tracker.average_bytes_per_second))}/s\n\n"
            f"Destination ......... {destination}"
        )

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
        self,
        root: Path,
        relative: Path,
        row: sqlite3.Row,
        digest: str | None,
        claimed: set[str],
    ) -> tuple[Path, bool, sqlite3.Row | None, str | None]:
        """Pick a destination free on disk and among paths this plan already reserved."""
        destination = safe_destination(root, relative)
        if os.path.lexists(destination):
            if destination.is_symlink() or not destination.is_file():
                raise ValueError(f"unsafe destination collision: {destination}")
            existing_hash, existing_size = sha256_file(destination)
            digest = digest or self._hash_candidate(row)
            if existing_size == row["size_bytes"] and existing_hash == digest:
                # Inventory paths are stored resolved; look the reused file up the same way.
                resolved = str(destination.resolve())
                canonical = self.database.connection.execute(
                    "SELECT * FROM assets WHERE absolute_path=?", (resolved,)
                ).fetchone()
                if canonical is None:
                    self._index_path(destination, "canonical-library", "canonical", existing_hash)
                    canonical = self.database.connection.execute(
                        "SELECT * FROM assets WHERE absolute_path=?", (resolved,)
                    ).fetchone()
                return relative, True, canonical, digest
        elif _claim_key(relative) not in claimed:
            return relative, False, None, digest
        suffix = f"__import_{int(row['id']):08d}"
        candidate = relative.with_name(f"{relative.stem}{suffix}{relative.suffix}")
        while (
            os.path.lexists(safe_destination(root, candidate)) or _claim_key(candidate) in claimed
        ):
            candidate = candidate.with_name(f"{candidate.stem}_1{candidate.suffix}")
        return candidate, True, None, digest

    @staticmethod
    def _copy(
        source: Path, destination: Path, root: Path, run_id: int, item_id: int
    ) -> tuple[str, int]:
        safe_destination(root, destination.relative_to(root))
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Recheck resolved containment and every created parent before opening the temporary.
        safe_destination(root, destination.relative_to(root))
        temporary = destination.parent / f".photo-migrator-import-{run_id}-{item_id}.tmp"
        if os.path.lexists(temporary):
            # Only a hard kill leaves this behind; the name is unique to this run item.
            if not stat.S_ISREG(temporary.lstat().st_mode):
                raise ValueError(f"unexpected non-regular file at temporary path: {temporary}")
            LOGGER.warning(
                "Removing stale temporary file from an interrupted import: %s", temporary
            )
            temporary.unlink()
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

    @staticmethod
    def _existing_copy(row: sqlite3.Row, destination: Path) -> tuple[str, int] | None:
        """Return the digest of an identical file already at *destination*, if any."""
        if not os.path.lexists(destination):
            return None
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"destination exists and is not a regular file: {destination}")
        existing = sha256_file(destination)
        if existing != sha256_file(Path(row["absolute_path"])):
            raise ValueError(
                f"destination already exists with different content: {destination}; re-plan the import"
            )
        return existing

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
                "media_type": media_type_for(path.suffix),
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
            (
                "existing_duplicates.csv",
                {"duplicate_existing", "duplicate_candidate", "reuse_destination"},
            ),
            ("name_collisions.csv", set()),
            ("review_items.csv", {"review"}),
        ):
            selected = (
                rows
                if filename == "name_collisions.csv"
                else [r for r in rows if r["action"] in actions]
            )
            if filename == "name_collisions.csv":
                selected = [
                    r
                    for r in selected
                    if "destination" in r["reason"]
                    or "__import_" in (r["destination_relative_path"] or "")
                ]
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
