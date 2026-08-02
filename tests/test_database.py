from pathlib import Path

from photo_migrator.database import Database


def test_initialize_schema_and_pragmas(tmp_path: Path) -> None:
    with Database(tmp_path / "inventory.db") as database:
        database.initialize()
        tables = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"assets", "scan_runs", "schema_version"} <= tables
        assert {
            "planning_runs",
            "migration_plans",
            "migration_plan_items",
            "migration_plan_item_assets",
        } <= tables
        assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2


def test_run_history_is_retained(tmp_path: Path) -> None:
    with Database(tmp_path / "inventory.db") as database:
        database.initialize()
        first = database.start_run(1)
        database.finish_run(first, "completed", 0, 0, 0, None)
        database.start_run(1)
        statuses = database.connection.execute(
            "SELECT status FROM scan_runs ORDER BY id"
        ).fetchall()
        assert [row[0] for row in statuses] == ["completed", "running"]
