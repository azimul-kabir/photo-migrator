"""Read-only database diagnostics and safe online backups."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from photo_migrator.atomic_io import atomic_write_json

RUN_TABLES = (
    "scan_runs",
    "hash_runs",
    "analysis_runs",
    "relationship_runs",
    "planning_runs",
    "build_runs",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return None if row is None or row[0] is None else int(row[0])
    except sqlite3.DatabaseError:
        return None


def check_database(path: Path, full: bool = False) -> dict[str, Any]:
    with read_only(path) as connection:
        pragma = "integrity_check" if full else "quick_check"
        integrity = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        running: dict[str, int] = {}
        for table in RUN_TABLES:
            if table in tables:
                running[table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status='running'"
                    ).fetchone()[0]
                )
        orphaned = 0
        if {"build_runs", "migration_plans"} <= tables:
            orphaned += int(
                connection.execute(
                    "SELECT COUNT(*) FROM build_runs b LEFT JOIN migration_plans p ON p.id=b.plan_id WHERE p.id IS NULL"
                ).fetchone()[0]
            )
        if {"build_items", "build_runs"} <= tables:
            orphaned += int(
                connection.execute(
                    "SELECT COUNT(*) FROM build_items i LEFT JOIN build_runs b ON b.id=i.build_run_id WHERE b.id IS NULL"
                ).fetchone()[0]
            )
    return {
        "database": str(path.resolve()),
        "check": pragma,
        "integrity": integrity,
        "ok": integrity == ["ok"] and not foreign_keys and orphaned == 0,
        "schema_version": schema_version_from(path),
        "foreign_key_violations": foreign_keys,
        "running_runs": running,
        "orphaned_references": orphaned,
    }


def schema_version_from(path: Path) -> int | None:
    with read_only(path) as connection:
        return schema_version(connection)


def backup_database(source: Path, output: Path, overwrite: bool, verify: bool) -> dict[str, Any]:
    source = source.resolve(strict=True)
    output = output.resolve(strict=False)
    if source == output:
        raise ValueError("backup output must differ from source database")
    if output.exists() and not overwrite:
        raise FileExistsError(f"backup already exists: {output}; use --overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with read_only(source) as source_db, sqlite3.connect(temporary) as target_db:
            source_db.backup(target_db)
        status = "not_requested"
        if verify:
            status = "passed" if check_database(temporary)["ok"] else "failed"
            if status == "failed":
                raise ValueError("backup verification failed")
        os.replace(temporary, output)
        metadata = {
            "source_database": str(source),
            "backup_path": str(output),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": schema_version_from(output),
            "sqlite_version": sqlite3.sqlite_version,
            "source_size_bytes": source.stat().st_size,
            "backup_size_bytes": output.stat().st_size,
            "verification_status": status,
            "sha256": sha256_file(output),
        }
        atomic_write_json(Path(f"{output}.json"), metadata)
        return metadata
    finally:
        temporary.unlink(missing_ok=True)
