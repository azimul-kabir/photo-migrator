"""Synthetic, no-network smoke coverage for the complete public workflow."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from photo_migrator.analysis import AnalysisEngine
from photo_migrator.builder import Builder, Rollback
from photo_migrator.cli import main
from photo_migrator.config import load_config
from photo_migrator.database import Database
from photo_migrator.db_tools import check_database
from photo_migrator.hashing import HashEngine
from photo_migrator.planner import Planner
from photo_migrator.relationships import RelationshipEngine
from photo_migrator.verifier import Verifier


def test_complete_synthetic_workflow(tmp_path: Path, monkeypatch: object) -> None:
    source_a, source_b = tmp_path / "a", tmp_path / "b"
    source_a.mkdir()
    source_b.mkdir()
    image_stream = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image_stream, format="JPEG")
    image = image_stream.getvalue()
    assets = [
        source_a / "dup.jpg",
        source_b / "dup.jpg",
        source_a / "live.jpg",
        source_a / "live.mov",
        source_a / "solo.jpg",
    ]
    assets[0].write_bytes(image)
    assets[1].write_bytes(image)
    assets[2].write_bytes(image + b"unique")
    assets[3].write_bytes(b"synthetic-video-metadata")
    assets[4].write_bytes(image + b"standalone")
    original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets}
    destination = tmp_path / "destination"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[sources]]\nname="a"\npath="{source_a}"\npriority=1\n'
        f'[[sources]]\nname="b"\npath="{source_b}"\npriority=2\n'
        '[scan]\nextensions=[".jpg", ".mov"]\n'
        f'[planning]\ndestination_root="{destination}"\nnaming_template="{{source_name}}/{{original_name}}"\n'
    )
    database_path = tmp_path / "photo.db"
    assert main(["init", "--database", str(database_path)]) == 0
    assert main(["scan", "--database", str(database_path), "--config", str(config_path)]) == 0

    probe = {
        "streams": [
            {
                "codec_type": "video",
                "width": 2,
                "height": 2,
                "codec_name": "h264",
                "avg_frame_rate": "30/1",
            }
        ],
        "format": {"duration": "1.0", "format_name": "mov"},
    }
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "photo_migrator.video_metadata.subprocess.run",
        Mock(return_value=Mock(returncode=0, stdout=json.dumps(probe), stderr="")),
    )
    config = load_config(config_path)
    with Database(database_path) as database:
        database.initialize()  # schema initialization/upgrades are idempotent
        assert database.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 5
        assert HashEngine(database).run() == 0
        # Unique files are intentionally outside size-based duplicate hashing; complete their
        # synthetic hashes so the planner can consume the entire analyzed inventory.
        for path in assets:
            database.connection.execute(
                "UPDATE assets SET sha256=?,hash_status='completed' WHERE absolute_path=?",
                (original_hashes[path], str(path.resolve())),
            )
        database.connection.commit()
        assert AnalysisEngine(database, ffprobe="mock-ffprobe").run() == 0
        assert RelationshipEngine(database, ffprobe="mock-ffprobe").run() == 0
        assert database.stats()["relationships_active"] >= 1
        plan_id = Planner(database, config).run().plan_id
        database.connection.execute(
            "UPDATE migration_plans SET status='ready' WHERE id=?", (plan_id,)
        )
        database.connection.commit()
        fingerprint = database.connection.execute(
            "SELECT fingerprint FROM migration_plans WHERE id=?", (plan_id,)
        ).fetchone()[0]
        assert not destination.exists()
        dry_run = Builder(database).run(plan_id)
        assert not destination.exists()
        # This identical destination is external and must never become build-owned.
        existing_relative = database.connection.execute(
            "SELECT destination_relative_path FROM migration_plan_items "
            "WHERE plan_id=? AND destination_relative_path LIKE '%/dup.jpg'",
            (plan_id,),
        ).fetchone()[0]
        existing = destination / existing_relative
        existing.parent.mkdir(parents=True)
        existing.write_bytes(image)
        build_run = Builder(database).run(plan_id, "copy")
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM build_items WHERE build_run_id=? AND owned_by_build=1",
                (build_run,),
            ).fetchone()[0]
            >= 1
        )
        assert (
            database.connection.execute(
                "SELECT owned_by_build FROM build_items "
                "WHERE build_run_id=? AND destination_absolute_path=?",
                (build_run, str(existing.resolve())),
            ).fetchone()[0]
            == 0
        )
        assert all(result.status == "completed" for result in Verifier(database).run(build_run))
        assert Rollback(database).run(build_run, dry_run=True)
        Rollback(database).run(build_run, dry_run=False, confirmed=True)
        assert existing.exists()
        assert (
            database.connection.execute(
                "SELECT fingerprint FROM migration_plans WHERE id=?", (plan_id,)
            ).fetchone()[0]
            == fingerprint
        )
        assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM build_runs WHERE status='running'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT status FROM build_runs WHERE id=?", (dry_run,)
            ).fetchone()[0]
            == "completed"
        )
    assert check_database(database_path, full=False)["ok"]
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets
    } == original_hashes
    assert list((tmp_path / "reports").glob("plan_*/*.csv"))
    assert list((tmp_path / "reports").glob("build_*/*.csv"))
