from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from photo_migrator.cli import main
from photo_migrator.database import Database
from photo_migrator.hashing import HashEngine


def add_asset(database: Database, path: Path, source: str = "camera") -> None:
    metadata = path.stat()
    database.upsert_asset(
        {
            "source_name": source,
            "source_priority": 1,
            "absolute_path": str(path.resolve()),
            "relative_path": path.name,
            "filename": path.name,
            "extension": path.suffix,
            "size_bytes": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "device_id": metadata.st_dev,
            "inode": metadata.st_ino,
            "media_type": "image",
        }
    )


def test_candidates_duplicates_reports_resume_and_rehash(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("one.jpg", "two.jpg", "other.jpg", "single.jpg")]
    paths[0].write_bytes(b"duplicate")
    paths[1].write_bytes(b"duplicate")
    paths[2].write_bytes(b"different")  # same size, different content
    paths[3].write_bytes(b"alone")
    database_path = tmp_path / "inventory.db"
    with Database(database_path) as database:
        database.initialize()
        for path in paths:
            add_asset(database, path)
        engine = HashEngine(database, workers=2)
        assert engine.run() == 0
        rows = database.connection.execute(
            "SELECT filename,sha256,hash_status FROM assets ORDER BY filename"
        ).fetchall()
        assert next(row[1] for row in rows if row[0] == "single.jpg") is None
        assert sum(row[2] == "completed" for row in rows) == 3
        first_hash = next(row[1] for row in rows if row[0] == "one.jpg")
        assert engine.run() == 0  # completed hashes resume without rereading
        paths[0].write_bytes(b"different")
        os.utime(paths[0], ns=(paths[0].stat().st_atime_ns, paths[0].stat().st_mtime_ns + 1))
        assert engine.run() == 0
        assert (
            database.connection.execute(
                "SELECT sha256 FROM assets WHERE filename='one.jpg'"
            ).fetchone()[0]
            != first_hash
        )
    summary = (tmp_path / "reports/hash_summary.txt").read_text()
    assert "duplicate groups: 1" in summary
    with (tmp_path / "reports/duplicate_groups.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 2


def test_empty_sparse_limit_source_and_failure_retry(tmp_path: Path) -> None:
    empty1, empty2 = tmp_path / "empty1.jpg", tmp_path / "empty2.jpg"
    empty1.touch()
    empty2.touch()
    sparse1, sparse2 = tmp_path / "sparse1.jpg", tmp_path / "sparse2.jpg"
    for path in (sparse1, sparse2):
        with path.open("wb") as stream:
            stream.seek(2 * 1024 * 1024)
            stream.write(b"x")
    with Database(tmp_path / "db.sqlite") as database:
        database.initialize()
        for path in (empty1, empty2, sparse1, sparse2):
            add_asset(database, path)
        empty2.unlink()
        assert HashEngine(database).run(limit=2, source="camera") == 2
        failed = database.connection.execute(
            "SELECT hash_status,hash_error FROM assets WHERE filename='empty2.jpg'"
        ).fetchone()
        assert failed[0] == "failed" and "FileNotFoundError" in failed[1]
        empty2.touch()
        assert HashEngine(database).run(resume=True) == 0


def test_cli_hash_command(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jpg", tmp_path / "b.jpg"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    database_path = tmp_path / "cli.db"
    with Database(database_path) as database:
        database.initialize()
        add_asset(database, first)
        add_asset(database, second)
    assert main(["hash", "--database", str(database_path), "--workers", "2"]) == 0


def test_permission_error_is_persisted(tmp_path: Path, monkeypatch: Any) -> None:
    first, second = tmp_path / "denied.jpg", tmp_path / "peer.jpg"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    original_open = Path.open

    def denied_open(path: Path, *args: object, **kwargs: object) -> Any:
        if path == first:
            raise PermissionError("synthetic denial")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    with Database(tmp_path / "denied.db") as database:
        database.initialize()
        add_asset(database, first)
        add_asset(database, second)
        assert HashEngine(database).run() == 2
        row = database.connection.execute(
            "SELECT hash_status,hash_error FROM assets WHERE filename='denied.jpg'"
        ).fetchone()
        assert row[0] == "failed"
        assert "PermissionError" in row[1]
