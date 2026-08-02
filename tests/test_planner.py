from pathlib import Path

from photo_migrator.config import load_config
from photo_migrator.database import Database
from photo_migrator.planner import Planner
from photo_migrator.scanner import Scanner


def test_unique_asset_plan_is_ready_and_writes_reports_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    media = source / "holiday.jpg"
    media.write_bytes(b"synthetic")
    destination = tmp_path / "clean-library"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[sources]]\nname="photos"\npath="{source}"\npriority=10\n'
        '[scan]\nextensions=[".jpg"]\n'
        f'[planning]\ndestination_root="{destination}"\n'
        'naming_template="{year}/{original_name}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    database_path = tmp_path / "photo.db"
    with Database(database_path) as database:
        database.initialize()
        Scanner(database, config).scan()
        database.connection.execute(
            "UPDATE assets SET sha256=?,hash_status='completed',hash_completed_at=?",
            ("a" * 64, "2026-01-01T00:00:00Z"),
        )
        database.connection.commit()
        result = Planner(database, config).run()
        item = database.connection.execute(
            "SELECT action,destination_relative_path FROM migration_plan_items"
        ).fetchone()
    assert result.status == "ready"
    assert tuple(item) == ("keep", "undated/holiday.jpg")
    assert not destination.exists()
    assert (tmp_path / "reports" / "plan_1" / "migration_plan.csv").is_file()
    assert media.read_bytes() == b"synthetic"


def test_duplicate_keeper_uses_priority_and_collision_is_disambiguated(tmp_path: Path) -> None:
    sources = []
    config_text = ""
    for name, priority in (("low", 1), ("high", 20)):
        source = tmp_path / name
        source.mkdir()
        (source / "same.jpg").write_bytes(b"same")
        sources.append(source)
        config_text += f'[[sources]]\nname="{name}"\npath="{source}"\npriority={priority}\n'
    destination = tmp_path / "destination"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        config_text + '[scan]\nextensions=[".jpg"]\n'
        f'[planning]\ndestination_root="{destination}"\n'
        'naming_template="undated/{original_name}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    with Database(tmp_path / "photo.db") as database:
        database.initialize()
        Scanner(database, config).scan()
        database.connection.execute(
            "UPDATE assets SET sha256=?,hash_status='completed'", ("b" * 64,)
        )
        database.connection.commit()
        result = Planner(database, config).run()
        rows = database.connection.execute(
            "SELECT action,primary_asset_id FROM migration_plan_items ORDER BY action"
        ).fetchall()
        high_id = database.connection.execute(
            "SELECT id FROM assets WHERE source_name='high'"
        ).fetchone()[0]
    assert result.status == "ready"
    assert ("keep", high_id) in [tuple(row) for row in rows]
    assert sum(row["action"] == "skip_exact_duplicate" for row in rows) == 1
