"""Small explicit-SQL repository for inventory state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3


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
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(assets)").fetchall()
            }
            additions = {
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
                CREATE INDEX IF NOT EXISTS idx_assets_analysis_status
                    ON assets(analysis_status);
                CREATE INDEX IF NOT EXISTS idx_assets_captured_at ON assets(captured_at);
                CREATE INDEX IF NOT EXISTS idx_assets_camera_make ON assets(camera_make);
                CREATE INDEX IF NOT EXISTS idx_assets_camera_model ON assets(camera_model);
                """
            )

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
                    first_seen_at, last_seen_at, missing_since, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', NULL, ?, ?, NULL, ?, ?)
                ON CONFLICT(absolute_path) DO UPDATE SET
                    source_name=excluded.source_name, source_priority=excluded.source_priority,
                    relative_path=excluded.relative_path, filename=excluded.filename,
                    extension=excluded.extension, size_bytes=excluded.size_bytes,
                    mtime_ns=excluded.mtime_ns, device_id=excluded.device_id, inode=excluded.inode,
                    media_type=excluded.media_type, scan_status='available', error_message=NULL,
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
        }
