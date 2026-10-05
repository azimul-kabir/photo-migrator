"""Small explicit-SQL repository for inventory state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 8

# Shared by fresh databases and the schema 8 rebuild, which widened the action CHECK.
IMPORT_PLAN_ITEMS_TABLE = """
                CREATE TABLE IF NOT EXISTS {name} (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES import_plans(id),
                    candidate_asset_id INTEGER NOT NULL REFERENCES assets(id),
                    action TEXT NOT NULL CHECK(action IN ('new','duplicate_existing',
                        'duplicate_candidate','reuse_destination','review')),
                    destination_relative_path TEXT, canonical_asset_id INTEGER REFERENCES assets(id),
                    matching_canonical_path TEXT, expected_size_bytes INTEGER NOT NULL,
                    expected_sha256 TEXT, reason TEXT NOT NULL, UNIQUE(plan_id,candidate_asset_id)
                );
"""


def utc_now() -> str:
    """Return an unambiguous UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Database:
    """SQLite connection owner and inventory repository."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def initialize(self) -> None:
        """Create the versioned schema idempotently."""
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    source_priority INTEGER NOT NULL,
                    absolute_path TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER,
                    mtime_ns INTEGER,
                    device_id INTEGER,
                    inode INTEGER,
                    media_type TEXT NOT NULL,
                    scan_status TEXT NOT NULL CHECK (
                        scan_status IN ('available', 'error', 'missing')
                    ),
                    error_message TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing_since TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sha256 TEXT,
                    hash_algorithm TEXT,
                    hash_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                        hash_status IN ('pending', 'running', 'completed', 'failed')
                    ),
                    hash_started_at TEXT,
                    hash_completed_at TEXT,
                    hash_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_assets_size ON assets(size_bytes);
                CREATE INDEX IF NOT EXISTS idx_assets_extension ON assets(extension);
                CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source_name);
                CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(scan_status);
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('running', 'completed', 'completed_with_errors', 'failed')
                    ),
                    source_count INTEGER NOT NULL,
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    indexed_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS hash_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('running', 'completed', 'completed_with_errors', 'failed')
                    ),
                    candidate_files INTEGER NOT NULL DEFAULT 0,
                    hashed_files INTEGER NOT NULL DEFAULT 0,
                    failed_files INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('running','completed','completed_with_errors','failed')
                    ),
                    candidate_files INTEGER NOT NULL DEFAULT 0,
                    analyzed_files INTEGER NOT NULL DEFAULT 0,
                    failed_files INTEGER NOT NULL DEFAULT 0,
                    unsupported_files INTEGER NOT NULL DEFAULT 0,
                    reused_files INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS asset_relationships (
                    id INTEGER PRIMARY KEY,
                    relationship_type TEXT NOT NULL CHECK (relationship_type IN (
                        'apple_live_photo','google_motion_photo','samsung_motion_photo',
                        'filename_pair','orphan_motion_image','orphan_motion_video')),
                    primary_asset_id INTEGER NOT NULL REFERENCES assets(id),
                    secondary_asset_id INTEGER REFERENCES assets(id),
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    evidence TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active','orphan','ambiguous','invalid')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (secondary_asset_id IS NULL OR primary_asset_id != secondary_asset_id)
                );
                CREATE TABLE IF NOT EXISTS relationship_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN
                        ('running','completed','completed_with_errors','failed')),
                    candidate_assets INTEGER NOT NULL DEFAULT 0,
                    relationships_created INTEGER NOT NULL DEFAULT 0,
                    relationships_reused INTEGER NOT NULL DEFAULT 0,
                    ambiguous_count INTEGER NOT NULL DEFAULT 0,
                    orphan_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS planning_runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','completed_with_errors','failed')),
                    candidate_assets INTEGER NOT NULL DEFAULT 0,
                    exact_duplicate_groups INTEGER NOT NULL DEFAULT 0,
                    bundles_created INTEGER NOT NULL DEFAULT 0, keepers_selected INTEGER NOT NULL DEFAULT 0,
                    items_planned INTEGER NOT NULL DEFAULT 0, collisions INTEGER NOT NULL DEFAULT 0,
                    ambiguous_items INTEGER NOT NULL DEFAULT 0, blocked_items INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0, message TEXT
                );
                CREATE TABLE IF NOT EXISTS migration_plans (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','ready','blocked','superseded')),
                    destination_root TEXT NOT NULL, naming_template TEXT NOT NULL,
                    config_snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    source_asset_count INTEGER NOT NULL, unique_content_count INTEGER NOT NULL,
                    duplicate_group_count INTEGER NOT NULL, bundle_count INTEGER NOT NULL,
                    planned_item_count INTEGER NOT NULL, collision_count INTEGER NOT NULL,
                    ambiguous_count INTEGER NOT NULL, blocked_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS migration_plan_items (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES migration_plans(id),
                    bundle_key TEXT NOT NULL, primary_asset_id INTEGER NOT NULL REFERENCES assets(id),
                    action TEXT NOT NULL CHECK(action IN ('keep','skip_exact_duplicate','include_relationship_member','review','blocked')),
                    status TEXT NOT NULL CHECK(status IN ('planned','collision','ambiguous','blocked','superseded')),
                    destination_relative_path TEXT,
                    original_destination_relative_path TEXT, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(destination_relative_path IS NULL OR (destination_relative_path NOT LIKE '/%' AND destination_relative_path NOT LIKE '../%' AND destination_relative_path NOT LIKE '%/../%' AND destination_relative_path != '..'))
                );
                CREATE TABLE IF NOT EXISTS migration_plan_item_assets (
                    plan_item_id INTEGER NOT NULL REFERENCES migration_plan_items(id),
                    asset_id INTEGER NOT NULL REFERENCES assets(id),
                    role TEXT NOT NULL CHECK(role IN ('primary','relationship_member','duplicate_skipped')),
                    PRIMARY KEY(plan_item_id, asset_id, role)
                );
                CREATE TABLE IF NOT EXISTS build_runs (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES migration_plans(id),
                    started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','completed_with_errors','failed','rolled_back','rollback_failed')),
                    mode TEXT NOT NULL CHECK(mode IN ('dry_run','copy','hardlink')),
                    dry_run INTEGER NOT NULL CHECK(dry_run IN (0,1)),
                    requested_items INTEGER NOT NULL DEFAULT 0, completed_items INTEGER NOT NULL DEFAULT 0,
                    skipped_items INTEGER NOT NULL DEFAULT 0, failed_items INTEGER NOT NULL DEFAULT 0,
                    verified_items INTEGER NOT NULL DEFAULT 0, verification_failed_items INTEGER NOT NULL DEFAULT 0,
                    bytes_written INTEGER NOT NULL DEFAULT 0,
                    rollback_status TEXT NOT NULL DEFAULT 'not_requested' CHECK(rollback_status IN ('not_requested','running','completed','completed_with_errors','failed')),
                    message TEXT, plan_fingerprint TEXT NOT NULL, plan_status TEXT NOT NULL,
                    destination_root TEXT NOT NULL, resumed_from_build_run_id INTEGER REFERENCES build_runs(id)
                );
                CREATE TABLE IF NOT EXISTS build_items (
                    id INTEGER PRIMARY KEY, build_run_id INTEGER NOT NULL REFERENCES build_runs(id),
                    plan_item_id INTEGER NOT NULL REFERENCES migration_plan_items(id),
                    asset_id INTEGER NOT NULL REFERENCES assets(id), source_path TEXT NOT NULL,
                    source_root TEXT NOT NULL, destination_relative_path TEXT NOT NULL CHECK(destination_relative_path NOT LIKE '/%' AND destination_relative_path NOT LIKE '../%' AND destination_relative_path NOT LIKE '%/../%' AND destination_relative_path != '..'),
                    destination_absolute_path TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('dry_run','copy','hardlink','skip','verify','rollback_delete')),
                    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','skipped','failed','verification_failed','rolled_back','rollback_failed')),
                    expected_sha256 TEXT NOT NULL, actual_sha256 TEXT, expected_size_bytes INTEGER NOT NULL,
                    actual_size_bytes INTEGER, expected_source_mtime_ns INTEGER, observed_source_mtime_ns INTEGER,
                    bytes_written INTEGER NOT NULL DEFAULT 0, source_verified INTEGER NOT NULL DEFAULT 0 CHECK(source_verified IN (0,1)),
                    destination_verified INTEGER NOT NULL DEFAULT 0 CHECK(destination_verified IN (0,1)),
                    owned_by_build INTEGER NOT NULL DEFAULT 0 CHECK(owned_by_build IN (0,1)),
                    started_at TEXT, finished_at TEXT, error TEXT, bundle_key TEXT NOT NULL,
                    temp_path TEXT,
                    UNIQUE(build_run_id,plan_item_id,asset_id),
                    CHECK(status != 'completed' OR operation NOT IN ('copy','hardlink') OR owned_by_build=1)
                );
                CREATE INDEX IF NOT EXISTS idx_build_items_run ON build_items(build_run_id);
                CREATE INDEX IF NOT EXISTS idx_build_items_plan_item ON build_items(plan_item_id);
                CREATE INDEX IF NOT EXISTS idx_build_items_asset ON build_items(asset_id);
                CREATE INDEX IF NOT EXISTS idx_build_items_status ON build_items(status);
                CREATE INDEX IF NOT EXISTS idx_build_items_destination ON build_items(destination_absolute_path);
                CREATE INDEX IF NOT EXISTS idx_build_items_owned ON build_items(owned_by_build);
                CREATE TABLE IF NOT EXISTS metadata_date_scan_runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    root TEXT NOT NULL, configuration TEXT NOT NULL, tool_version TEXT,
                    status TEXT NOT NULL, scanned INTEGER NOT NULL DEFAULT 0,
                    already_dated INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0,
                    conflicts INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS metadata_date_evidence (
                    id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES metadata_date_scan_runs(id),
                    asset_id INTEGER NOT NULL REFERENCES assets(id), evidence_type TEXT NOT NULL,
                    source TEXT, candidate_timestamp TEXT NOT NULL, timezone TEXT,
                    precision TEXT NOT NULL, derivation TEXT NOT NULL, confidence INTEGER NOT NULL,
                    raw_value TEXT, explanation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_metadata_evidence_asset ON metadata_date_evidence(scan_run_id,asset_id);
                CREATE TABLE IF NOT EXISTS metadata_date_plans (
                    id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES metadata_date_scan_runs(id),
                    created_at TEXT NOT NULL, min_confidence INTEGER NOT NULL,
                    allow_estimated INTEGER NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_date_plan_items (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES metadata_date_plans(id),
                    asset_id INTEGER NOT NULL REFERENCES assets(id), path TEXT NOT NULL,
                    proposed_timestamp TEXT, precision TEXT, derivation TEXT, confidence INTEGER,
                    status TEXT NOT NULL, selected_evidence TEXT, conflicts TEXT,
                    planned_size INTEGER NOT NULL, planned_mtime_ns INTEGER NOT NULL,
                    planned_sha256 TEXT, write_supported INTEGER NOT NULL,
                    UNIQUE(plan_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS metadata_date_apply_runs (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES metadata_date_plans(id),
                    started_at TEXT NOT NULL, finished_at TEXT, dry_run INTEGER NOT NULL,
                    rollback_of INTEGER REFERENCES metadata_date_apply_runs(id), status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_date_apply_items (
                    id INTEGER PRIMARY KEY, apply_run_id INTEGER NOT NULL REFERENCES metadata_date_apply_runs(id),
                    plan_item_id INTEGER NOT NULL REFERENCES metadata_date_plan_items(id), path TEXT NOT NULL,
                    before_values TEXT NOT NULL, intended_values TEXT NOT NULL, after_values TEXT,
                    before_size INTEGER, before_mtime_ns INTEGER, after_size INTEGER, after_mtime_ns INTEGER,
                    status TEXT NOT NULL, verification TEXT, error TEXT, backup TEXT NOT NULL,
                    UNIQUE(apply_run_id,plan_item_id)
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(assets)").fetchall()
            }
            additions = {
                "asset_role": "TEXT NOT NULL DEFAULT 'candidate' CHECK (asset_role IN ('candidate','canonical'))",
                "sha256": "TEXT",
                "hash_algorithm": "TEXT",
                "hash_status": (
                    "TEXT NOT NULL DEFAULT 'pending' CHECK "
                    "(hash_status IN ('pending','running','completed','failed'))"
                ),
                "hash_started_at": "TEXT",
                "hash_completed_at": "TEXT",
                "hash_error": "TEXT",
                "analysis_status": (
                    "TEXT DEFAULT 'pending' CHECK (analysis_status IN "
                    "('pending','running','completed','failed','unsupported'))"
                ),
                "analysis_started_at": "TEXT",
                "analysis_completed_at": "TEXT",
                "analysis_error": "TEXT",
                "analyzed_size_bytes": "INTEGER",
                "analyzed_mtime_ns": "INTEGER",
                "captured_at": "TEXT",
                "captured_at_source": "TEXT",
                "width": "INTEGER",
                "height": "INTEGER",
                "orientation": "INTEGER",
                "camera_make": "TEXT",
                "camera_model": "TEXT",
                "lens_model": "TEXT",
                "gps_latitude": "REAL",
                "gps_longitude": "REAL",
                "gps_altitude": "REAL",
                "duration_seconds": "REAL",
                "video_codec": "TEXT",
                "audio_codec": "TEXT",
                "container_format": "TEXT",
                "frame_rate": "REAL",
                "bitrate": "INTEGER",
                "color_space": "TEXT",
                "apple_content_identifier": "TEXT",
                "motion_photo_offset": "INTEGER",
                "relationship_status": "TEXT CHECK (relationship_status IN "
                "('pending','completed','failed','invalid'))",
                "relationship_error": "TEXT",
                "relationship_analyzed_size_bytes": "INTEGER",
                "relationship_analyzed_mtime_ns": "INTEGER",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_assets_role_size ON assets(asset_role,size_bytes);
                CREATE TABLE IF NOT EXISTS import_plans (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    library_root TEXT NOT NULL, source_filter TEXT, candidate_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                    collision_count INTEGER NOT NULL DEFAULT 0, bytes_avoided INTEGER NOT NULL DEFAULT 0,
                    internal_duplicate_count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0
                );
                """
                + IMPORT_PLAN_ITEMS_TABLE.format(name="import_plan_items")
                + """
                CREATE TABLE IF NOT EXISTS import_runs (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES import_plans(id),
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL, copied_count INTEGER NOT NULL DEFAULT 0,
                    reused_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
                    bytes_written INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS import_run_items (
                    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES import_runs(id),
                    plan_item_id INTEGER NOT NULL REFERENCES import_plan_items(id), status TEXT NOT NULL,
                    destination_path TEXT, sha256 TEXT, size_bytes INTEGER, owned INTEGER NOT NULL DEFAULT 0,
                    error TEXT, UNIQUE(run_id,plan_item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_assets_analysis_status
                    ON assets(analysis_status);
                CREATE INDEX IF NOT EXISTS idx_assets_captured_at ON assets(captured_at);
                CREATE INDEX IF NOT EXISTS idx_assets_camera_make ON assets(camera_make);
                CREATE INDEX IF NOT EXISTS idx_assets_camera_model ON assets(camera_model);
                CREATE INDEX IF NOT EXISTS idx_relationship_type
                    ON asset_relationships(relationship_type);
                CREATE INDEX IF NOT EXISTS idx_relationship_primary
                    ON asset_relationships(primary_asset_id);
                CREATE INDEX IF NOT EXISTS idx_relationship_secondary
                    ON asset_relationships(secondary_asset_id);
                CREATE INDEX IF NOT EXISTS idx_relationship_status
                    ON asset_relationships(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_active_unique
                    ON asset_relationships(relationship_type,primary_asset_id,
                        COALESCE(secondary_asset_id,-1)) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON migration_plan_items(plan_id);
                CREATE INDEX IF NOT EXISTS idx_plan_items_primary ON migration_plan_items(primary_asset_id);
                CREATE INDEX IF NOT EXISTS idx_plan_items_status ON migration_plan_items(status);
                CREATE INDEX IF NOT EXISTS idx_plan_items_action ON migration_plan_items(action);
                CREATE INDEX IF NOT EXISTS idx_plan_items_destination ON migration_plan_items(destination_relative_path);
                CREATE INDEX IF NOT EXISTS idx_plan_items_bundle ON migration_plan_items(bundle_key);
                """
            )
            # Schema 8 additions to tables created by earlier versions.
            for table, name, definition in (
                ("import_plans", "internal_duplicate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("import_plans", "review_count", "INTEGER NOT NULL DEFAULT 0"),
                ("metadata_date_apply_items", "backup_path", "TEXT"),
                ("metadata_date_apply_items", "backup_sha256", "TEXT"),
                ("metadata_date_apply_items", "after_sha256", "TEXT"),
            ):
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self._widen_import_plan_actions()

    def _widen_import_plan_actions(self) -> None:
        """Rebuild import_plan_items once so its CHECK accepts duplicate_candidate (schema 8)."""
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='import_plan_items'"
        ).fetchone()
        if row is None or "duplicate_candidate" in row["sql"]:
            return
        # SQLite cannot alter a CHECK constraint; use its documented table-rebuild procedure.
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    IMPORT_PLAN_ITEMS_TABLE.format(name="import_plan_items_rebuild")
                )
                self.connection.execute(
                    "INSERT INTO import_plan_items_rebuild SELECT * FROM import_plan_items"
                )
                self.connection.execute("DROP TABLE import_plan_items")
                self.connection.execute(
                    "ALTER TABLE import_plan_items_rebuild RENAME TO import_plan_items"
                )
                if self.connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError("import_plan_items rebuild broke foreign keys")
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def start_run(self, source_count: int) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs(started_at, status, source_count) VALUES (?, 'running', ?)",
                (utc_now(), source_count),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        discovered: int,
        indexed: int,
        errors: int,
        message: str | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE scan_runs SET finished_at=?, status=?,
                   discovered_count=?, indexed_count=?, error_count=?, message=? WHERE id=?""",
                (utc_now(), status, discovered, indexed, errors, message, run_id),
            )

    def upsert_asset(self, asset: dict[str, Any]) -> None:
        now = utc_now()
        values = (
            asset["source_name"],
            asset["source_priority"],
            asset["absolute_path"],
            asset["relative_path"],
            asset["filename"],
            asset["extension"],
            asset["size_bytes"],
            asset["mtime_ns"],
            asset["device_id"],
            asset["inode"],
            asset["media_type"],
            asset.get("asset_role", "candidate"),
            now,
            now,
            now,
            now,
        )
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO assets(
                    source_name, source_priority, absolute_path, relative_path, filename, extension,
                    size_bytes, mtime_ns, device_id, inode, media_type, scan_status, error_message,
                    asset_role, first_seen_at, last_seen_at, missing_since, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', NULL, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(absolute_path) DO UPDATE SET
                    source_name=excluded.source_name, source_priority=excluded.source_priority,
                    relative_path=excluded.relative_path, filename=excluded.filename,
                    extension=excluded.extension, size_bytes=excluded.size_bytes,
                    mtime_ns=excluded.mtime_ns, device_id=excluded.device_id, inode=excluded.inode,
                    media_type=excluded.media_type, scan_status='available', error_message=NULL,
                    asset_role=excluded.asset_role,
                    last_seen_at=excluded.last_seen_at, missing_since=NULL,
                    updated_at=excluded.updated_at,
                    hash_status=CASE WHEN assets.size_bytes != excluded.size_bytes
                        OR assets.mtime_ns != excluded.mtime_ns THEN 'pending'
                        ELSE assets.hash_status END,
                    sha256=CASE WHEN assets.size_bytes != excluded.size_bytes
                        OR assets.mtime_ns != excluded.mtime_ns THEN NULL ELSE assets.sha256 END,
                    hash_algorithm=CASE WHEN assets.size_bytes != excluded.size_bytes
                        OR assets.mtime_ns != excluded.mtime_ns THEN NULL
                        ELSE assets.hash_algorithm END,
                    hash_completed_at=CASE WHEN assets.size_bytes != excluded.size_bytes
                        OR assets.mtime_ns != excluded.mtime_ns THEN NULL
                        ELSE assets.hash_completed_at END,
                    hash_error=CASE WHEN assets.size_bytes != excluded.size_bytes
                        OR assets.mtime_ns != excluded.mtime_ns THEN NULL ELSE assets.hash_error END
                """,
                values,
            )

    def mark_missing(self, source_name: str, seen_paths: set[str]) -> None:
        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT absolute_path FROM assets WHERE source_name=? AND scan_status != 'missing'",
                (source_name,),
            ).fetchall()
            missing = [
                (now, now, row["absolute_path"])
                for row in rows
                if row["absolute_path"] not in seen_paths
            ]
            connection.executemany(
                """UPDATE assets SET scan_status='missing', missing_since=?, updated_at=?
                   WHERE absolute_path=?""",
                missing,
            )

    def stats(self) -> dict[str, Any]:
        connection = self.connection
        scalar = connection.execute(
            """SELECT COUNT(*) AS total,
               SUM(CASE WHEN scan_status='available' THEN 1 ELSE 0 END) AS available,
               SUM(CASE WHEN scan_status='missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN scan_status='error' THEN 1 ELSE 0 END) AS errors,
               COALESCE(SUM(CASE WHEN scan_status='available' THEN size_bytes ELSE 0 END), 0)
               AS bytes,
               SUM(CASE WHEN analysis_status='completed' THEN 1 ELSE 0 END) analyses_completed,
               SUM(CASE WHEN analysis_status='failed' THEN 1 ELSE 0 END) analyses_failed,
               SUM(CASE WHEN analysis_status='unsupported' THEN 1 ELSE 0 END)
                   analyses_unsupported,
               SUM(CASE WHEN captured_at IS NOT NULL THEN 1 ELSE 0 END) with_captured_at,
               SUM(CASE WHEN gps_latitude IS NOT NULL AND gps_longitude IS NOT NULL
                   THEN 1 ELSE 0 END)
                   with_gps,
               SUM(CASE WHEN media_type='image' THEN 1 ELSE 0 END) images,
               SUM(CASE WHEN media_type='video' THEN 1 ELSE 0 END) videos
               FROM assets"""
        ).fetchone()
        assert scalar is not None
        latest = connection.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        latest_analysis = connection.execute(
            "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_relationship = connection.execute(
            "SELECT * FROM relationship_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        relationship_counts = connection.execute(
            """SELECT
            SUM(status='active') active,
            SUM(relationship_type='apple_live_photo' AND status='active') apple,
            SUM(relationship_type='apple_live_photo' AND status='active' AND confidence=1.0)
                apple_exact,
            SUM(relationship_type='apple_live_photo' AND status='active' AND
                evidence LIKE 'fallback=one_sided_apple_identifier;%') apple_one_sided,
            SUM(relationship_type='google_motion_photo' AND status='active') google,
            SUM(relationship_type='samsung_motion_photo' AND status='active') samsung,
            SUM(relationship_type='filename_pair' AND status='active') filename,
            SUM(relationship_type='orphan_motion_image' AND status='orphan') orphan_image,
            SUM(relationship_type='orphan_motion_video' AND status='orphan') orphan_video,
            SUM(status='ambiguous') ambiguous,
            SUM(status='invalid') invalid FROM asset_relationships"""
        ).fetchone()
        assert relationship_counts is not None
        latest_plan = connection.execute(
            "SELECT * FROM migration_plans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_build = connection.execute(
            "SELECT * FROM build_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        incremental = connection.execute(
            """SELECT
            SUM(asset_role='canonical' AND scan_status='available') canonical_count,
            COALESCE(SUM(CASE WHEN asset_role='canonical' AND scan_status='available'
                THEN size_bytes ELSE 0 END),0) canonical_bytes,
            SUM(asset_role='canonical' AND scan_status='available' AND hash_status='completed')
                canonical_hashed,
            SUM(asset_role='candidate' AND scan_status='available') candidate_count
            FROM assets"""
        ).fetchone()
        latest_import_plan = connection.execute(
            "SELECT * FROM import_plans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_import_run = connection.execute(
            "SELECT * FROM import_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        plan_counts = None
        if latest_plan is not None:
            plan_counts = connection.execute(
                """SELECT SUM(action='keep') keep_count,
                SUM(action='skip_exact_duplicate') duplicate_skips,
                SUM(action='include_relationship_member') relationship_members,
                SUM(action='review') review_items, SUM(action='blocked') blocked_items,
                SUM(status='collision') collisions FROM migration_plan_items WHERE plan_id=?""",
                (latest_plan["id"],),
            ).fetchone()
        return {
            "total": scalar["total"],
            "available": scalar["available"] or 0,
            "missing": scalar["missing"] or 0,
            # A traversal error may not correspond to an asset row (for example, an unreadable
            # directory), so the latest run is the authoritative scan-error count.
            "errors": latest["error_count"] if latest is not None else scalar["errors"] or 0,
            "bytes": scalar["bytes"],
            "by_source": connection.execute(
                """SELECT source_name, COUNT(*) AS count FROM assets
                   GROUP BY source_name ORDER BY source_name"""
            ).fetchall(),
            "by_extension": connection.execute(
                """SELECT extension, COUNT(*) AS count FROM assets
                   GROUP BY extension ORDER BY extension"""
            ).fetchall(),
            "latest_run": latest,
            "analyses_completed": scalar["analyses_completed"] or 0,
            "analyses_failed": scalar["analyses_failed"] or 0,
            "analyses_unsupported": scalar["analyses_unsupported"] or 0,
            "with_captured_at": scalar["with_captured_at"] or 0,
            "with_gps": scalar["with_gps"] or 0,
            "images": scalar["images"] or 0,
            "videos": scalar["videos"] or 0,
            "latest_analysis_run": latest_analysis,
            "relationships_active": relationship_counts["active"] or 0,
            "apple_live_photos": relationship_counts["apple"] or 0,
            "apple_live_photos_exact": relationship_counts["apple_exact"] or 0,
            "apple_live_photos_one_sided": relationship_counts["apple_one_sided"] or 0,
            "google_motion_photos": relationship_counts["google"] or 0,
            "samsung_motion_photos": relationship_counts["samsung"] or 0,
            "filename_pairs": relationship_counts["filename"] or 0,
            "orphan_motion_images": relationship_counts["orphan_image"] or 0,
            "orphan_motion_videos": relationship_counts["orphan_video"] or 0,
            "ambiguous_relationships": relationship_counts["ambiguous"] or 0,
            "invalid_relationships": relationship_counts["invalid"] or 0,
            "latest_relationship_run": latest_relationship,
            "latest_plan": latest_plan,
            "latest_plan_counts": plan_counts,
            "latest_build_run": latest_build,
            "canonical_count": incremental["canonical_count"] or 0,
            "canonical_bytes": incremental["canonical_bytes"] or 0,
            "canonical_hashed": incremental["canonical_hashed"] or 0,
            "candidate_count": incremental["candidate_count"] or 0,
            "latest_import_plan": latest_import_plan,
            "latest_import_run": latest_import_run,
        }
