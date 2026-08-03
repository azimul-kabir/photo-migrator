from __future__ import annotations

import logging
from pathlib import Path

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
    tracker = ProgressTracker(logging.getLogger("test"), 10, 4096, 5, 1024, clock=clock)
    clock.now = 2.0
    tracker.record_success(1024)

    assert format_bytes(4 * 1024**4) == "4.00 TiB"
    assert format_duration(3 * 3600 + 42 * 60) == "3h 42m"
    assert "6 / 10 canonical assets (50.0%)" in tracker.progress_message()
    assert "512 B/s" in tracker.progress_message()


def test_eta_calculation_and_periodic_logging(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    tracker = ProgressTracker(
        logging.getLogger("progress-test"), 4, 4000, 0, 0, clock=clock, file_interval=2
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
    tracker = ProgressTracker(logging.getLogger("test"), 3, 3072, 1, 1024, clock=clock)
    clock.now = 60.0
    tracker.record_success(1024)
    tracker.record_failure()

    assert tracker.final_summary() == (
        "Canonical indexing completed\n\n"
        "Assets:\n"
        "  Total ............ 3\n"
        "  Newly hashed ..... 1\n"
        "  Previously hashed  1\n"
        "  Failed ........... 1\n\n"
        "Data:\n"
        "  Processed ........ 2.00 KiB\n"
        "  Elapsed .......... 1m\n"
        "  Average speed .... 17 B/s"
    )


def test_empty_library_has_no_work(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    database, importer = _importer(tmp_path, {})
    with database, caplog.at_level(logging.INFO):
        result = importer.library_index()

    assert not result.errors
    assert "Canonical assets: 0" in caplog.messages
    assert "Canonical library already fully indexed." in caplog.messages


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
        assert "Already hashed: 1" in caplog.messages
        assert "Remaining: 1" in caplog.messages
        assert "Resuming previous index..." in caplog.messages
        assert any("Previously hashed  1" in message for message in caplog.messages)

        caplog.clear()
        with caplog.at_level(logging.INFO):
            importer.library_index(resume=True)
        assert "Already hashed: 2" in caplog.messages
        assert "Remaining: 0" in caplog.messages
        assert "Canonical library already fully indexed." in caplog.messages
