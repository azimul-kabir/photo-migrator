from __future__ import annotations

import os
from pathlib import Path

import pytest

from photo_migrator.config import Config, ConfigError, ScanConfig, SourceConfig, load_config
from photo_migrator.database import Database
from photo_migrator.media_types import media_type_for
from photo_migrator.scanner import Scanner, _contained


def config_for(root: Path) -> Config:
    return Config(
        sources=(SourceConfig("test", root.resolve(), 100),),
        scan=ScanConfig(
            extensions=frozenset({".jpg", ".mov"}),
            exclude_directory_names=frozenset({"@eaDir", "Photos Library.photoslibrary"}),
            exclude_filename_suffixes=("@SynoEAStream",),
            exclude_filename_prefixes=("SYNOINDEX_",),
        ),
    )


def scan(root: Path, database_path: Path) -> Database:
    database = Database(database_path)
    database.initialize()
    Scanner(database, config_for(root)).scan()
    return database


def test_normal_case_insensitive_scan_and_exclusions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.JPG").write_bytes(b"one")
    (source / "clip.MOV").write_bytes(b"video")
    (source / "ignore.txt").write_text("no")
    for excluded in ("@eaDir", "Photos Library.photoslibrary"):
        directory = source / excluded
        directory.mkdir()
        (directory / "hidden.jpg").write_bytes(b"hidden")
    (source / "bad.jpg@SynoEAStream").write_bytes(b"no")
    (source / "SYNOINDEX_bad.jpg").write_bytes(b"no")

    with scan(source, tmp_path / "inventory.db") as database:
        rows = database.connection.execute(
            "SELECT relative_path, extension, media_type FROM assets ORDER BY relative_path"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("clip.MOV", ".mov", "video"),
            ("one.JPG", ".jpg", "image"),
        ]


def test_does_not_follow_directory_symlink(tmp_path: Path) -> None:
    source, outside = tmp_path / "source", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"secret")
    try:
        (source / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with scan(source, tmp_path / "inventory.db") as database:
        assert database.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_resumable_upsert_and_missing_retention(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    asset = source / "one.jpg"
    asset.write_bytes(b"one")
    database_path = tmp_path / "inventory.db"
    with scan(source, database_path) as database:
        first_id = database.connection.execute("SELECT id FROM assets").fetchone()[0]
    with scan(source, database_path) as database:
        assert database.connection.execute("SELECT id FROM assets").fetchone()[0] == first_id
        assert database.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    asset.unlink()
    with scan(source, database_path) as database:
        row = database.connection.execute(
            "SELECT scan_status, missing_since FROM assets"
        ).fetchone()
        assert tuple(row) == ("missing", row[1])
        assert row[1] is not None


def test_scan_run_status_and_stats(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"123")
    with scan(source, tmp_path / "inventory.db") as database:
        stats = database.stats()
        assert (stats["total"], stats["available"], stats["bytes"]) == (1, 1, 3)
        assert stats["latest_run"]["status"] == "completed"
        assert stats["by_source"][0]["count"] == 1
        assert stats["by_extension"][0]["extension"] == ".jpg"


def test_containment() -> None:
    assert _contained(Path("/root/picture.jpg"), Path("/root"))
    assert not _contained(Path("/elsewhere/picture.jpg"), Path("/root"))


def write_config(path: Path, source: Path, extra_source: Path | None = None) -> None:
    second = ""
    if extra_source is not None:
        second = f'\n[[sources]]\nname="second"\npath="{extra_source}"\npriority=2\n'
    path.write_text(
        f'[[sources]]\nname="first"\npath="{source}"\npriority=1\n{second}'
        '[scan]\nextensions=[".JPG"]\n'
    )


def test_invalid_and_duplicate_source_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, tmp_path / "missing")
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(config_path)
    source = tmp_path / "source"
    source.mkdir()
    write_config(config_path, source, source)
    with pytest.raises(ConfigError, match="duplicate source path"):
        load_config(config_path)


def test_rejects_clean_library_root_and_file_root(tmp_path: Path) -> None:
    clean = tmp_path / "CleanLibrary"
    clean.mkdir()
    config_path = tmp_path / "config.toml"
    write_config(config_path, clean)
    with pytest.raises(ConfigError, match="reserved CleanLibrary"):
        load_config(config_path)
    file_path = tmp_path / "file"
    file_path.write_text("x")
    write_config(config_path, file_path)
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(config_path)


def test_scandir_error_is_visible_and_prevents_missing_marking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    asset = source / "one.jpg"
    asset.write_bytes(b"one")
    database_path = tmp_path / "inventory.db"
    with scan(source, database_path):
        pass
    real_scandir = os.scandir

    def denied(path: os.PathLike[str]) -> os.ScandirIterator[str]:
        if Path(path) == source:
            raise PermissionError("synthetic denial")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with scan(source, database_path) as database:
        row = database.connection.execute("SELECT scan_status FROM assets").fetchone()
        run = database.connection.execute(
            "SELECT status, error_count, message FROM scan_runs ORDER BY id DESC"
        ).fetchone()
        assert row[0] == "available"
        assert run[0] == "completed_with_errors"
        assert run[1] == 1
        assert "synthetic denial" in run[2]
        assert database.stats()["errors"] == 1


def test_media_type_classification_covers_common_video_containers() -> None:
    assert media_type_for(".MOV") == "video"
    assert all(media_type_for(ext) == "video" for ext in (".avi", ".mts", ".3gp", ".mkv"))
    assert all(media_type_for(ext) == "image" for ext in (".jpg", ".heic", ".dng", ".png"))
