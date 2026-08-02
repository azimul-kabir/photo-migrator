"""Interrupted-run audit and narrowly scoped stale-status updates."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from photo_migrator.db_tools import RUN_TABLES


def audit(database: Path, older_than_minutes: int | None, mark_failed: bool) -> dict[str, Any]:
    if mark_failed and older_than_minutes is None:
        raise ValueError("--older-than-minutes is required with --mark-stale-failed")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes or 0)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    stale: list[dict[str, Any]] = []
    updated = 0
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in RUN_TABLES:
            if table not in tables:
                continue
            for row in connection.execute(
                f"SELECT id, started_at FROM {table} WHERE status='running' ORDER BY id"
            ):
                record = {"table": table, "id": row["id"], "started_at": row["started_at"]}
                stale.append(record)
                if mark_failed and str(row["started_at"]) < cutoff.isoformat():
                    connection.execute(
                        f"UPDATE {table} SET status='failed', finished_at=?, message=? WHERE id=? AND status='running'",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            "Marked failed by explicit recovery audit",
                            row["id"],
                        ),
                    )
                    updated += 1
        interrupted = []
        temporary = []
        if "build_items" in tables:
            interrupted = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, build_run_id, status, destination_absolute_path, temp_path FROM build_items WHERE status IN ('pending','running','failed') ORDER BY id"
                )
            ]
            temporary = sorted({str(row["temp_path"]) for row in interrupted if row["temp_path"]})
        if mark_failed:
            connection.commit()
    finally:
        connection.close()
    return {
        "stale_runs": stale,
        "records_updated": updated,
        "interrupted_build_items": interrupted,
        "owned_temporary_files": temporary,
        "unknown_temporary_files": [],
        "recommendations": [
            "Run doctor --strict before resuming.",
            "Inspect temporary files manually; recovery never deletes them.",
        ],
    }
