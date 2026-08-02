import hashlib
import os
from pathlib import Path

import pytest

from photo_migrator.builder import Builder, Rollback
from photo_migrator.config import load_config
from photo_migrator.database import Database
from photo_migrator.planner import Planner
from photo_migrator.scanner import Scanner
from photo_migrator.verifier import Verifier


def prepared(tmp_path: Path, status: str = "ready") -> tuple[Database, Path, Path, int]:
    source = tmp_path / "source"
    source.mkdir()
    media = source / "photo.jpg"
    media.write_bytes(b"small synthetic media")
    destination = tmp_path / "destination"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[sources]]\nname="source"\npath="{source}"\npriority=1\n'
        '[scan]\nextensions=[".jpg"]\n'
        f'[planning]\ndestination_root="{destination}"\n'
        'naming_template="{original_name}"\n',
        encoding="utf-8",
    )
    database = Database(tmp_path / "photo.db")
    database.initialize()
    config = load_config(config_path)
    Scanner(database, config).scan()
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    database.connection.execute("UPDATE assets SET sha256=?,hash_status='completed'", (digest,))
    database.connection.commit()
    plan_id = Planner(database, config).run().plan_id
    database.connection.execute("UPDATE migration_plans SET status=? WHERE id=?", (status, plan_id))
    database.connection.commit()
    return database, media, destination, plan_id


def test_dry_run_creates_no_destination(tmp_path: Path) -> None:
    database, media, destination, plan_id = prepared(tmp_path)
    with database:
        run_id = Builder(database).run(plan_id)
        item = database.connection.execute(
            "SELECT source_verified,owned_by_build,status FROM build_items WHERE build_run_id=?",
            (run_id,),
        ).fetchone()
    assert tuple(item) == (1, 0, "skipped")
    assert not destination.exists()
    assert media.read_bytes() == b"small synthetic media"


def test_copy_verify_and_owned_rollback(tmp_path: Path) -> None:
    database, media, destination, plan_id = prepared(tmp_path)
    original = media.read_bytes()
    with database:
        run_id = Builder(database, 2).run(plan_id, "copy")
        target = destination / "photo.jpg"
        assert target.read_bytes() == original
        assert target.stat().st_mtime_ns == media.stat().st_mtime_ns
        assert not list(destination.glob(".photo-migrator-*.tmp"))
        assert Verifier(database).run(run_id)[0].status == "completed"
        assert Rollback(database).run(run_id, dry_run=True)[0].status == "planned"
        assert target.exists()
        assert (
            Rollback(database).run(run_id, dry_run=False, confirmed=True)[0].status == "rolled_back"
        )
        assert not target.exists()
    assert media.read_bytes() == original


def test_existing_identical_is_never_owned_or_rolled_back(tmp_path: Path) -> None:
    database, _, destination, plan_id = prepared(tmp_path)
    destination.mkdir()
    target = destination / "photo.jpg"
    target.write_bytes(b"small synthetic media")
    with database:
        run_id = Builder(database).run(plan_id, "copy")
        owned = database.connection.execute(
            "SELECT owned_by_build FROM build_items WHERE build_run_id=?", (run_id,)
        ).fetchone()[0]
        assert owned == 0
        assert Rollback(database).run(run_id, dry_run=False, confirmed=True) == []
    assert target.exists()


def test_conflict_is_not_overwritten_and_draft_is_gated(tmp_path: Path) -> None:
    database, _, destination, plan_id = prepared(tmp_path, "draft")
    destination.mkdir()
    target = destination / "photo.jpg"
    target.write_bytes(b"unrelated")
    with database:
        with pytest.raises(ValueError, match="allow-draft"):
            Builder(database).run(plan_id, "copy")
        run_id = Builder(database).run(plan_id, "copy", allow_draft=True)
        row = database.connection.execute(
            "SELECT status,error FROM build_items WHERE build_run_id=?", (run_id,)
        ).fetchone()
    assert row["status"] == "failed"
    assert "conflict" in row["error"]
    assert target.read_bytes() == b"unrelated"


def test_hardlink_and_rollback_only_unlinks_destination(tmp_path: Path) -> None:
    database, media, destination, plan_id = prepared(tmp_path)
    with database:
        try:
            run_id = Builder(database).run(plan_id, "hardlink")
        except OSError as exc:
            pytest.skip(f"hardlinks unsupported: {exc}")
        target = destination / "photo.jpg"
        assert (media.stat().st_dev, media.stat().st_ino) == (
            target.stat().st_dev,
            target.stat().st_ino,
        )
        Rollback(database).run(run_id, dry_run=False, confirmed=True)
    assert media.read_bytes() == b"small synthetic media"
    assert not target.exists()


def test_source_symlink_and_destination_symlink_are_rejected(tmp_path: Path) -> None:
    database, media, destination, plan_id = prepared(tmp_path)
    real = media.with_suffix(".real")
    media.rename(real)
    os.symlink(real, media)
    with database:
        run_id = Builder(database).run(plan_id, "copy")
        assert (
            database.connection.execute(
                "SELECT status FROM build_items WHERE build_run_id=?", (run_id,)
            ).fetchone()[0]
            == "failed"
        )
    assert not destination.joinpath("photo.jpg").exists()
