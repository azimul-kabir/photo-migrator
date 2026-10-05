"""Regression coverage for incremental import planning and execution safety."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from photo_migrator.config import SourceConfig, load_config
from photo_migrator.database import IMPORT_PLAN_ITEMS_TABLE, Database
from photo_migrator.incremental import IncrementalImporter
from photo_migrator.progress import ProgressSnapshot
from photo_migrator.scanner import Scanner


def _setup(
    tmp_path: Path,
    sources: dict[str, tuple[int, dict[str, bytes]]],
    canonical: dict[str, bytes] | None = None,
) -> tuple[Database, IncrementalImporter, Path]:
    library = tmp_path / "CleanLibrary"
    library.mkdir()
    for name, contents in (canonical or {}).items():
        (library / name).write_bytes(contents)
    config = (
        f'[library]\nroot="{library}"\n[scan]\nextensions=[".jpg"]\n'
        '[imports]\ndefault_directory="Imported"\npreserve_source_subdirectories=false\n'
    )
    for source_name, (priority, files) in sources.items():
        directory = tmp_path / source_name
        directory.mkdir()
        for name, contents in files.items():
            (directory / name).write_bytes(contents)
        config += f'[[sources]]\nname="{source_name}"\npath="{directory}"\npriority={priority}\n'
    (tmp_path / "config.toml").write_text(config)
    database = Database(tmp_path / "inventory.db")
    database.initialize()
    return database, IncrementalImporter(database, load_config(tmp_path / "config.toml")), library


def _items(database: Database, plan_id: int) -> list[tuple[str, str | None, str]]:
    return [
        (row["action"], row["destination_relative_path"], Path(row["absolute_path"]).name)
        for row in database.connection.execute(
            """SELECT i.action,i.destination_relative_path,a.absolute_path FROM import_plan_items i
            JOIN assets a ON a.id=i.candidate_asset_id WHERE plan_id=? ORDER BY i.id""",
            (plan_id,),
        )
    ]


def _digests(paths: list[Path]) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_identical_candidates_are_imported_once_from_the_highest_priority_source(
    tmp_path: Path,
) -> None:
    database, importer, library = _setup(
        tmp_path,
        {"alpha": (10, {"copy.jpg": b"same"}), "beta": (90, {"original.jpg": b"same"})},
    )
    sources = [tmp_path / "alpha" / "copy.jpg", tmp_path / "beta" / "original.jpg"]
    before = _digests(sources)
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        assert _items(database, plan_id) == [
            ("duplicate_candidate", None, "copy.jpg"),
            ("new", "Imported/original.jpg", "original.jpg"),
        ]
        plan = database.connection.execute(
            "SELECT * FROM import_plans WHERE id=?", (plan_id,)
        ).fetchone()
        assert (plan["new_count"], plan["internal_duplicate_count"], plan["bytes_avoided"]) == (
            1,
            1,
            4,
        )
        importer.run(plan_id, dry_run=False, confirm=True)

    assert sorted(path.name for path in library.rglob("*.jpg")) == ["original.jpg"]
    assert _digests(sources) == before
    report = tmp_path / "reports" / f"import_plan_{plan_id}" / "existing_duplicates.csv"
    rows = list(csv.DictReader(report.open()))
    assert [Path(row["candidate_path"]).name for row in rows] == ["copy.jpg"]
    assert rows[0]["matching_canonical_path"] == str(library / "Imported" / "original.jpg")


def test_same_name_candidates_get_distinct_destinations(tmp_path: Path) -> None:
    database, importer, library = _setup(
        tmp_path,
        {
            "a": (1, {"IMG_0001.jpg": b"first"}),
            "b": (1, {"IMG_0001.jpg": b"second!"}),
            "c": (1, {"img_0001.JPG.jpg": b"x", "IMG_0001.JPG.jpg": b"yy"}),
        },
    )
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        destinations = [item[1] for item in _items(database, plan_id)]
        assert len({str(path).casefold() for path in destinations}) == len(destinations)
        run_id = importer.run(plan_id, dry_run=False, confirm=True)
        statuses = {
            row[0]
            for row in database.connection.execute(
                "SELECT status FROM import_run_items WHERE run_id=?", (run_id,)
            )
        }
    assert statuses == {"copied"}
    assert len(list(library.rglob("*.jpg"))) == 4
    collisions = tmp_path / "reports" / f"import_plan_{plan_id}" / "name_collisions.csv"
    assert len(list(csv.DictReader(collisions.open()))) == 2


def test_unhashed_library_assets_send_same_size_candidates_to_review(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, importer, library = _setup(
        tmp_path, {"a": (1, {"dup.jpg": b"dup!", "other.jpg": b"unique"})}, {"orig.jpg": b"dup!"}
    )
    with database:
        # Scanned but never hashed, as after an interrupted library-index.
        Scanner(database, importer.config).scan(
            (SourceConfig("canonical-library", library, 0),), "canonical"
        )
        importer.import_scan()
        plan_id = importer.plan()
        assert _items(database, plan_id) == [
            ("review", None, "dup.jpg"),
            ("new", "Imported/other.jpg", "other.jpg"),
        ]
        assert "canonical assets are not hashed" in caplog.text
        review = tmp_path / "reports" / f"import_plan_{plan_id}" / "review_items.csv"
        assert "not hashed" in next(csv.DictReader(review.open()))["reason"]

        importer.library_index()
        assert _items(database, importer.plan())[0] == ("duplicate_existing", None, "dup.jpg")


def test_unreadable_candidate_is_recorded_for_review_without_aborting_the_plan(
    tmp_path: Path,
) -> None:
    database, importer, _ = _setup(
        tmp_path, {"a": (1, {"gone.jpg": b"four", "kept.jpg": b"FOUR"})}, {"c.jpg": b"4444"}
    )
    with database:
        importer.library_index()
        importer.import_scan()
        (tmp_path / "a" / "gone.jpg").unlink()
        plan_id = importer.plan()
        status = database.connection.execute(
            "SELECT status,review_count FROM import_plans WHERE id=?", (plan_id,)
        ).fetchone()
        reason = database.connection.execute(
            "SELECT reason FROM import_plan_items WHERE action='review'"
        ).fetchone()[0]
        actions = [item[0] for item in _items(database, plan_id)]
    assert tuple(status) == ("ready", 1)
    assert "could not be read" in reason
    assert actions == ["review", "new"]


def test_unexpected_planning_failure_marks_the_plan_failed(tmp_path: Path) -> None:
    database, importer, _ = _setup(tmp_path, {"a": (1, {"one.jpg": b"one"})})
    with database:
        importer.import_scan()
        failing = patch.object(importer, "_plan_items", side_effect=RuntimeError("boom"))
        with failing, pytest.raises(RuntimeError):
            importer.plan()
        status = database.connection.execute("SELECT status FROM import_plans").fetchone()[0]
        assert status == "failed"
        with pytest.raises(ValueError, match="not ready"):
            importer.run(1, dry_run=False, confirm=True)


def test_rerunning_a_completed_import_is_a_no_op(tmp_path: Path) -> None:
    database, importer, library = _setup(tmp_path, {"a": (1, {"one.jpg": b"one"})})
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        first = importer.run(plan_id, dry_run=False, confirm=True)
        assert importer.run(plan_id, dry_run=False, confirm=True) == first
        runs = database.connection.execute(
            "SELECT COUNT(*),SUM(failed_count) FROM import_runs"
        ).fetchone()
    assert tuple(runs) == (1, 0)
    assert [path.name for path in library.rglob("*.jpg")] == ["one.jpg"]


def test_placed_but_unrecorded_copy_is_verified_on_resume(tmp_path: Path) -> None:
    database, importer, library = _setup(
        tmp_path, {"a": (1, {"placed.jpg": b"placed", "clash.jpg": b"clash"})}
    )
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        # Simulate a crash after placement and an unrelated file appearing at a destination.
        (library / "Imported").mkdir()
        (library / "Imported" / "placed.jpg").write_bytes(b"placed")
        (library / "Imported" / "clash.jpg").write_bytes(b"something else")
        run_id = importer.run(plan_id, dry_run=False, confirm=True)
        rows = {
            Path(row["destination_path"]).name: (row["status"], row["owned"], row["error"])
            for row in database.connection.execute(
                "SELECT * FROM import_run_items WHERE run_id=?", (run_id,)
            )
        }
        run = database.connection.execute(
            "SELECT * FROM import_runs WHERE id=?", (run_id,)
        ).fetchone()
    assert rows["placed.jpg"] == ("verified_existing", 0, None)
    assert rows["clash.jpg"][0] == "failed"
    assert "different content" in rows["clash.jpg"][2]
    assert (library / "Imported" / "clash.jpg").read_bytes() == b"something else"
    assert (run["copied_count"], run["reused_count"], run["failed_count"]) == (0, 1, 1)


def test_stale_temporary_file_does_not_block_resume(tmp_path: Path) -> None:
    database, importer, library = _setup(tmp_path, {"a": (1, {"one.jpg": b"one"})})
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        (library / "Imported").mkdir()
        (library / "Imported" / ".photo-migrator-import-1-1.tmp").write_bytes(b"partial")
        importer.run(plan_id, dry_run=False, confirm=True)
    assert [path.name for path in (library / "Imported").iterdir()] == ["one.jpg"]


def test_unsafe_destination_fails_one_item_not_the_run(tmp_path: Path) -> None:
    database, importer, library = _setup(
        tmp_path, {"a": (1, {"one.jpg": b"one", "two.jpg": b"two!"})}
    )
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        with database.transaction() as connection:
            connection.execute(
                "UPDATE import_plan_items SET destination_relative_path='../escape.jpg' WHERE id=1"
            )
        run_id = importer.run(plan_id, dry_run=False, confirm=True)
        statuses = [
            row[0]
            for row in database.connection.execute(
                "SELECT status FROM import_run_items WHERE run_id=? ORDER BY plan_item_id",
                (run_id,),
            )
        ]
        run_status = database.connection.execute(
            "SELECT status FROM import_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
    assert statuses == ["failed", "copied"]
    assert run_status == "completed_with_errors"
    assert not (tmp_path / "escape.jpg").exists()
    assert (library / "Imported" / "two.jpg").exists()


def test_schema_upgrade_widens_import_plan_actions(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    with Database(path) as database:
        database.initialize()
        old_table = IMPORT_PLAN_ITEMS_TABLE.format(name="import_plan_items").replace(
            "'duplicate_candidate',", ""
        )
        connection = database.connection
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            "DROP TABLE import_plan_items;"
            + old_table
            + """INSERT INTO assets(id,source_name,source_priority,absolute_path,relative_path,
            filename,extension,media_type,scan_status,first_seen_at,last_seen_at,created_at,
            updated_at) VALUES (1,'a',1,'/a/x.jpg','x.jpg','x.jpg','.jpg','image','available',
            't','t','t','t');
            INSERT INTO import_plans(id,created_at,status,library_root) VALUES (1,'t','ready','/l');
            INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,expected_size_bytes,
            reason) VALUES (1,1,'new',1,'old row');"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,
                expected_size_bytes,reason) VALUES (1,1,'duplicate_candidate',1,'x')"""
            )

    with Database(path) as database:
        database.initialize()
        connection = database.connection
        assert connection.execute("SELECT reason FROM import_plan_items").fetchone()[0] == (
            "old row"
        )
        with database.transaction():
            connection.execute("DELETE FROM import_plan_items")
            connection.execute(
                """INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,
                expected_size_bytes,reason) VALUES (1,1,'duplicate_candidate',1,'x')"""
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_library_index_warns_that_workers_are_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, importer, _ = _setup(tmp_path, {"a": (1, {})})
    with database:
        importer.library_index(workers=4)
    assert "ignores --workers=4" in caplog.text


class _Stop(Exception):
    pass


def test_progress_listener_receives_snapshots_and_can_stop_a_resumable_import(
    tmp_path: Path,
) -> None:
    database, importer, library = _setup(
        tmp_path, {"a": (1, {"one.jpg": b"one", "two.jpg": b"two!", "three.jpg": b"three"})}
    )
    snapshots: list[ProgressSnapshot] = []
    stop_after_first_copy = False

    def listen(snapshot: ProgressSnapshot) -> None:
        snapshots.append(snapshot)
        importing = snapshot.phase_name == "Importing files"
        if stop_after_first_copy and importing and snapshot.completed_items >= 1:
            raise _Stop

    importer.progress_listener = listen
    with database:
        importer.library_index()
        importer.import_scan()
        plan_id = importer.plan()
        phases = {snapshot.phase_name for snapshot in snapshots}
        assert {"Scanning candidate sources", "Planning import"} <= phases
        planning = [s for s in snapshots if s.phase_name == "Planning import"]
        assert planning[-1].completed_items == planning[-1].total_items == 3

        stop_after_first_copy = True
        with pytest.raises(_Stop):
            importer.run(plan_id, dry_run=False, confirm=True)
        assert len(list(library.rglob("*.jpg"))) == 1
        stop_after_first_copy = False
        run_id = importer.run(plan_id, dry_run=False, confirm=True)
        statuses = [
            row[0]
            for row in database.connection.execute(
                "SELECT status FROM import_run_items WHERE run_id=?", (run_id,)
            )
        ]
    assert statuses == ["copied", "copied", "copied"]
    assert len(list(library.rglob("*.jpg"))) == 3
    final = [s for s in snapshots if s.phase_name == "Importing files"][-1]
    assert final.completed_items == final.total_items == 3
    assert final.total_bytes == 12
