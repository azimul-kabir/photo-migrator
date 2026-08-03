from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from photo_migrator.config import load_config
from photo_migrator.database import Database
from photo_migrator.incremental import IncrementalImporter
from photo_migrator.progress import ProgressTracker, format_bytes, format_duration


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _importer(tmp_path: Path, files: dict[str, bytes]) -> tuple[Database, IncrementalImporter]:
    library = tmp_path / "library"
    library.mkdir()
    for name, contents in files.items():
        (library / name).write_bytes(contents)
    source = tmp_path / "source"
    source.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[sources]]\nname="source"\npath="{source}"\npriority=1\n'
        f'[library]\nroot="{library}"\n[scan]\nextensions=[".jpg"]\n'
    )
    database = Database(tmp_path / "inventory.db")
    database.initialize()
    return database, IncrementalImporter(database, load_config(config_path))


def test_progress_formatting_and_byte_percentage() -> None:
    clock = Clock()
    tracker = ProgressTracker(
        logging.getLogger("test"),
        "Hashing canonical assets",
        10,
        "Phase 2/2",
        "Indexed",
        total_bytes=4096,
        initial_items=5,
        initial_bytes=1024,
        byte_based=True,
        clock=clock,
    )
    clock.now = 2.0
    tracker.record_success(1024)

    assert format_bytes(4 * 1024**4) == "4.00 TiB"
    assert format_duration(3 * 3600 + 42 * 60) == "3h 42m"
    assert "Phase 2/2 | Indexed 6 / 10 canonical assets (50.0%)" in tracker.progress_message()
    assert "512 B/s" in tracker.progress_message()


def test_eta_calculation_and_periodic_logging(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    tracker = ProgressTracker(
        logging.getLogger("progress-test"),
        "Hashing",
        4,
        "Phase 2/2",
        "Indexed",
        total_bytes=4000,
        byte_based=True,
        clock=clock,
        file_interval=2,
    )
    with caplog.at_level(logging.INFO, logger="progress-test"):
        clock.now = 1.0
        tracker.record_success(1000)
        assert not caplog.messages
        clock.now = 2.0
        tracker.record_success(1000)

    assert tracker.eta_seconds == pytest.approx(2.0)
    assert len(caplog.messages) == 1
    assert "50.0%" in caplog.messages[0]


def test_final_summary() -> None:
    clock = Clock()
    tracker = ProgressTracker(
        logging.getLogger("test"),
        "Scanning",
        3,
        "Phase 1/2",
        "Scanned",
        total_bytes=3072,
        initial_items=1,
        initial_bytes=1024,
        clock=clock,
    )
    clock.now = 60.0
    tracker.record_success(1024)
    tracker.record_failure()

    assert "Phase 1/2 | Scanned 2 / 3 (66.7%)" in tracker.progress_message()
    assert "1.00 KiB" in tracker.progress_message()


def test_scan_progress_logs_by_item_and_time(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    tracker = ProgressTracker(
        logging.getLogger("scan-progress"),
        "Scanning",
        1000,
        "Phase 1/2",
        "Scanned",
        clock=clock,
        file_interval=500,
    )
    with caplog.at_level(logging.INFO, logger="scan-progress"):
        for _ in range(500):
            tracker.record_success(10)
        clock.now = 30
        tracker.record_success(10)

    assert "Scanned 500 / 1,000 (50.0%)" in caplog.messages[0]
    assert "Scanned 501 / 1,000 (50.1%)" in caplog.messages[1]


def test_empty_library_has_no_work(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    database, importer = _importer(tmp_path, {})
    with database, caplog.at_level(logging.INFO):
        result = importer.library_index()

    assert not result.errors
    assert "Canonical assets : 0" in caplog.messages
    assert "Phase 1/2: Scanning canonical library..." in caplog.messages
    assert "Phase 1 complete." in caplog.messages
    assert "Phase 2/2: Hashing remaining canonical assets..." in caplog.messages
    assert "Canonical library already fully indexed." in caplog.messages
    assert "Library indexing complete." in caplog.messages


def test_partial_and_fully_indexed_library(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, importer = _importer(tmp_path, {"one.jpg": b"one", "two.jpg": b"two"})
    with database:
        importer.library_index()
        database.connection.execute(
            "UPDATE assets SET sha256=NULL, hash_status='pending' WHERE filename='two.jpg'"
        )
        database.connection.commit()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            importer.library_index(resume=True)
        assert "Already hashed   : 1" in caplog.messages
        assert "Remaining hashes : 1" in caplog.messages
        assert "Resuming previous index..." in caplog.messages
        assert any("Previously hashed  1" in message for message in caplog.messages)

        caplog.clear()
        with caplog.at_level(logging.INFO):
            importer.library_index(resume=True)
        assert "Already hashed   : 2" in caplog.messages
        assert "Remaining hashes : 0" in caplog.messages
        assert "Canonical library already fully indexed." in caplog.messages


def test_fast_resume_skips_scan_and_reviews_changed_and_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, importer = _importer(
        tmp_path, {"done.jpg": b"done", "pending.jpg": b"pending", "missing.jpg": b"gone"}
    )
    with database:
        importer.library_index()
        database.connection.execute(
            "UPDATE assets SET sha256=NULL,hash_status='pending' WHERE filename!='done.jpg'"
        )
        database.connection.commit()
        library = tmp_path / "library"
        (library / "pending.jpg").write_bytes(b"changed-size")
        (library / "missing.jpg").unlink()
        caplog.clear()
        with (
            patch("photo_migrator.incremental.Scanner.scan") as scan,
            caplog.at_level(logging.INFO),
        ):
            result = importer.library_index(resume=True)

        scan.assert_not_called()
        assert len(result.errors) == 2
        done = database.connection.execute(
            "SELECT sha256,hash_status FROM assets WHERE filename='done.jpg'"
        ).fetchone()
        assert done["sha256"] is not None and done["hash_status"] == "completed"
        changed = database.connection.execute(
            "SELECT hash_status,hash_error FROM assets WHERE filename='pending.jpg'"
        ).fetchone()
        assert changed["hash_status"] == "failed"
        assert changed["hash_error"] == "refresh_required: size_changed"
        assert "Fast resume requested." in caplog.messages
        assert "Canonical rescan skipped." in caplog.messages
        assert not any("Phase 1" in message for message in caplog.messages)
        report = (tmp_path / "reports" / "library_resume_review.csv").read_text()
        assert "size_changed" in report and "missing" in report


def test_fast_resume_hashes_unchanged_pending_then_is_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, importer = _importer(tmp_path, {"one.jpg": b"one", "two.jpg": b"two"})
    with database:
        importer.library_index()
        original = database.connection.execute(
            "SELECT sha256 FROM assets WHERE filename='one.jpg'"
        ).fetchone()[0]
        database.connection.execute(
            "UPDATE assets SET sha256=NULL,hash_status='running' WHERE filename='two.jpg'"
        )
        database.connection.commit()
        assert not importer.library_index(resume=True).errors
        assert (
            database.connection.execute(
                "SELECT sha256 FROM assets WHERE filename='one.jpg'"
            ).fetchone()[0]
            == original
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert not importer.library_index(resume=True).errors
        assert "Canonical library already fully indexed." in caplog.messages
