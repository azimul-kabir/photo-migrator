from __future__ import annotations

import csv
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from photo_migrator.config import MetadataDateRecoveryConfig
from photo_migrator.database import Database, utc_now
from photo_migrator.date_recovery import (
    DateRecovery,
    Evidence,
    filename_evidence,
    folder_evidence,
    neighboring_evidence,
    resolve_evidence,
)

POLICY = MetadataDateRecoveryConfig(reasonable_year_max=2030)


def test_strict_filename_patterns_and_calendar_validation() -> None:
    for name in (
        "IMG_20210517_123456.jpg",
        "2021-05-17 12.34.56.jpg",
        "Screenshot_2021-05-17-12-34-56.png",
        "PXL_20210517_123456789.jpg",
    ):
        evidence = filename_evidence(Path(name), POLICY)
        assert evidence and evidence.timestamp == "2021-05-17T12:34:56"
        assert evidence.confidence == 95
    assert filename_evidence(Path("invoice_123456789.jpg"), POLICY) is None
    assert filename_evidence(Path("IMG_20210230_123456.jpg"), POLICY) is None


def test_filename_date_only() -> None:
    evidence = filename_evidence(Path("holiday 2021-05-17.jpg"), POLICY)
    assert evidence and evidence.precision == "day" and evidence.confidence == 90


def test_folder_precision_and_estimation(tmp_path: Path) -> None:
    exact = folder_evidence(tmp_path / "2021-05-17" / "a.jpg", POLICY)
    month = folder_evidence(tmp_path / "2021-05" / "a.jpg", POLICY)
    year = folder_evidence(tmp_path / "2018 Wedding" / "a.jpg", POLICY)
    assert exact and (exact.precision, exact.estimated) == ("day", False)
    assert month and (month.precision, month.estimated) == ("month", True)
    assert year and year.timestamp == "2018-07-01T12:00:00" and year.estimated


def test_neighbor_interpolation_is_two_sided_and_bounded(tmp_path: Path) -> None:
    target = tmp_path / "IMG_5342.jpg"
    dates = {
        tmp_path / "IMG_5341.jpg": "2019-12-25T10:05:00",
        tmp_path / "IMG_5343.jpg": "2019-12-25T10:08:00",
    }
    evidence = neighboring_evidence(target, dates)
    assert evidence and evidence.timestamp == "2019-12-25T10:06:30"
    assert neighboring_evidence(tmp_path / "OTHER_5342.jpg", dates) is None
    assert neighboring_evidence(target, dates, max_sequence_gap=1) is None


def test_conflict_and_agreement_are_explainable() -> None:
    first = Evidence("filename", "2020-01-01T12:00:00", "second", 95, "a", "", "a")
    near = Evidence("folder", "2020-01-01T12:01:00", "day", 88, "b", "", "b")
    primary, confidence, conflict = resolve_evidence([near, first])
    assert primary == first and confidence == 97 and not conflict
    far = Evidence("sidecar", "2021-01-01T12:00:00", "second", 99, "c", "", "c")
    assert resolve_evidence([first, far])[2]


def test_csv_module_quotes_special_paths(tmp_path: Path) -> None:
    path = tmp_path / 'a, "quoted"\n雪.jpg'
    report = tmp_path / "report.csv"
    with report.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["path"])
        writer.writeheader()
        writer.writerow({"path": str(path)})
    with report.open(newline="", encoding="utf-8") as stream:
        assert next(iter(csv.DictReader(stream)))["path"] == str(path)


def test_dynamic_year_policy_can_be_explicit() -> None:
    assert POLICY.reasonable_year_max >= datetime.now().year


class FakeExifTool:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.executable = "fake-exiftool"

    def read(self, path: Path) -> dict[str, object]:
        value = "2020-01-02T03:04:05" if path.read_bytes().endswith(b"|dated") else None
        return {"EXIF:DateTimeOriginal": value} if value else {}

    def write(
        self, path: Path, timestamp: str, comment: str | None
    ) -> subprocess.CompletedProcess[str]:
        del timestamp, comment
        if self.fail:
            return subprocess.CompletedProcess([], 1, "", "synthetic failure")
        path.write_bytes(path.read_bytes() + b"|dated")
        return subprocess.CompletedProcess([], 0, "", "")


def recovery_fixture(
    tmp_path: Path, tool: FakeExifTool
) -> tuple[Database, DateRecovery, Path, int]:
    path = tmp_path / "IMG_20200102_030405.jpg"
    path.write_bytes(b"synthetic image")
    database = Database(tmp_path / "inventory.db")
    database.initialize()
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    database.upsert_asset(
        {
            "source_name": "canonical-library",
            "source_priority": 0,
            "absolute_path": str(path),
            "relative_path": path.name,
            "filename": path.name,
            "extension": ".jpg",
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "device_id": stat.st_dev,
            "inode": stat.st_ino,
            "media_type": "image",
            "asset_role": "canonical",
        }
    )
    asset_id = int(database.connection.execute("SELECT id FROM assets").fetchone()[0])
    with database.transaction() as connection:
        connection.execute(
            "UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed' WHERE id=?",
            (digest, asset_id),
        )
        scan_id = connection.execute(
            """INSERT INTO metadata_date_scan_runs(started_at,root,configuration,status)
            VALUES(?,?,?,'completed')""",
            (utc_now(), str(tmp_path), "{}"),
        ).lastrowid
        connection.execute(
            """INSERT INTO metadata_date_evidence(scan_run_id,asset_id,evidence_type,
            candidate_timestamp,precision,derivation,confidence,explanation)
            VALUES(?,?,'exact_duplicate','2020-01-02T03:04:05','second','inferred',99,'test')""",
            (scan_id, asset_id),
        )
        plan_id = connection.execute(
            """INSERT INTO metadata_date_plans(scan_run_id,created_at,min_confidence,
            allow_estimated,status) VALUES(?,?,90,0,'ready')""",
            (scan_id, utc_now()),
        ).lastrowid
        connection.execute(
            """INSERT INTO metadata_date_plan_items(plan_id,asset_id,path,proposed_timestamp,
            precision,derivation,confidence,status,planned_size,planned_mtime_ns,planned_sha256,
            write_supported) VALUES(?,?,?,'2020-01-02T03:04:05','second','inferred',99,
            'eligible',?,?,?,1)""",
            (plan_id, asset_id, str(path), stat.st_size, stat.st_mtime_ns, digest),
        )
        import_plan_id = connection.execute(
            """INSERT INTO import_plans(created_at,status,library_root,candidate_count)
            VALUES(?,'ready',?,1)""",
            (utc_now(), str(tmp_path)),
        ).lastrowid
        connection.execute(
            """INSERT INTO import_plan_items(plan_id,candidate_asset_id,action,
            canonical_asset_id,matching_canonical_path,expected_size_bytes,expected_sha256,reason)
            VALUES(?,?,'duplicate_existing',?,?,?,?, 'exact test match')""",
            (import_plan_id, asset_id, asset_id, str(path), stat.st_size, digest),
        )
    recovery = DateRecovery(database, POLICY, tmp_path / "reports", tool)
    return database, recovery, path, int(plan_id or 0)


def test_successful_apply_refreshes_identity_and_stales_exact_evidence(tmp_path: Path) -> None:
    database, recovery, path, plan_id = recovery_fixture(tmp_path, FakeExifTool())
    old = database.connection.execute("SELECT * FROM assets").fetchone()
    recovery.apply(plan_id, apply=True, preserve_times=False)
    current = database.connection.execute("SELECT * FROM assets").fetchone()
    assert current["size_bytes"] == path.stat().st_size > old["size_bytes"]
    assert current["mtime_ns"] == path.stat().st_mtime_ns
    assert current["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert current["sha256"] != old["sha256"] and current["hash_status"] == "completed"
    assert current["analyzed_size_bytes"] is None
    assert current["relationship_analyzed_size_bytes"] is None
    assert (
        database.connection.execute(
            "SELECT status FROM metadata_date_plans WHERE id=?", (plan_id,)
        ).fetchone()[0]
        == "stale"
    )
    assert database.connection.execute("SELECT status FROM import_plans").fetchone()[0] == "stale"
    database.close()


def test_failed_apply_leaves_canonical_identity_unchanged(tmp_path: Path) -> None:
    database, recovery, _path, plan_id = recovery_fixture(tmp_path, FakeExifTool(fail=True))
    before = tuple(
        database.connection.execute("SELECT size_bytes,mtime_ns,sha256 FROM assets").fetchone()
    )
    recovery.apply(plan_id, apply=True)
    after = tuple(
        database.connection.execute("SELECT size_bytes,mtime_ns,sha256 FROM assets").fetchone()
    )
    assert after == before
    assert (
        database.connection.execute(
            "SELECT status FROM metadata_date_plans WHERE id=?", (plan_id,)
        ).fetchone()[0]
        == "ready"
    )
    database.close()


def test_rollback_refreshes_identity_and_index_never_reuses_apply_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, recovery, path, plan_id = recovery_fixture(tmp_path, FakeExifTool())
    apply_run = recovery.apply(plan_id, apply=True, preserve_times=False)
    applied_hash = database.connection.execute("SELECT sha256 FROM assets").fetchone()[0]
    # Runs recorded before byte backups existed fall back to removing the written tags.
    with database.transaction() as connection:
        connection.execute("UPDATE metadata_date_apply_items SET backup_path=NULL")

    def rollback_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        target = Path(args[-1])
        target.write_bytes(target.read_bytes().removesuffix(b"|dated"))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", rollback_run)
    recovery.rollback(apply_run, apply=True)
    row = database.connection.execute(
        "SELECT size_bytes,mtime_ns,sha256,hash_status FROM assets"
    ).fetchone()
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["sha256"] != applied_hash and row["hash_status"] == "completed"
    assert row["size_bytes"] == path.stat().st_size
    database.close()


class CorruptingExifTool(FakeExifTool):
    """Reports success but leaves the file changed without the requested date."""

    def write(
        self, path: Path, timestamp: str, comment: str | None
    ) -> subprocess.CompletedProcess[str]:
        del timestamp, comment
        path.write_bytes(path.read_bytes() + b"|mangled")
        return subprocess.CompletedProcess([], 0, "", "")


def _no_exiftool(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("byte restore must not invoke ExifTool")


def test_apply_keeps_verified_byte_backup_and_rollback_restores_it_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, recovery, path, plan_id = recovery_fixture(tmp_path, FakeExifTool())
    original_bytes, original_mtime = path.read_bytes(), path.stat().st_mtime_ns
    apply_run = recovery.apply(plan_id, apply=True, preserve_times=False)
    item = database.connection.execute("SELECT * FROM metadata_date_apply_items").fetchone()
    backup = Path(item["backup_path"])
    assert backup.read_bytes() == original_bytes
    assert item["backup_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert item["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(subprocess, "run", _no_exiftool)
    recovery.rollback(apply_run, apply=True)
    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime_ns == original_mtime
    row = database.connection.execute("SELECT size_bytes,mtime_ns,sha256 FROM assets").fetchone()
    assert tuple(row) == (
        len(original_bytes),
        original_mtime,
        hashlib.sha256(original_bytes).hexdigest(),
    )
    assert list(path.parent.glob(".photo-migrator-restore-*")) == []
    database.close()


def test_unverified_write_is_recorded_and_can_be_rolled_back(tmp_path: Path) -> None:
    database, recovery, path, plan_id = recovery_fixture(tmp_path, CorruptingExifTool())
    original_bytes = path.read_bytes()
    apply_run = recovery.apply(plan_id, apply=True, preserve_times=False)
    item = database.connection.execute("SELECT * FROM metadata_date_apply_items").fetchone()
    assert item["status"] == "write_unverified"
    assert "verification failed" in item["error"]
    asset = database.connection.execute("SELECT sha256 FROM assets").fetchone()
    assert asset["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    recovery.rollback(apply_run, apply=True)
    assert path.read_bytes() == original_bytes
    database.close()


def test_rollback_refuses_to_restore_over_later_changes(tmp_path: Path) -> None:
    database, recovery, path, plan_id = recovery_fixture(tmp_path, FakeExifTool())
    apply_run = recovery.apply(plan_id, apply=True, preserve_times=False)
    path.write_bytes(path.read_bytes() + b"|edited later")
    edited = path.read_bytes()
    rollback_run = recovery.rollback(apply_run, apply=True)
    assert path.read_bytes() == edited
    report = tmp_path / "reports" / f"metadata-date-rollback-{rollback_run}.csv"
    row = next(csv.DictReader(report.open()))
    assert row["status"] == "skipped"
    assert "changed after the apply run" in row["error"]
    database.close()


def test_failed_write_without_changes_is_skipped_not_unverified(tmp_path: Path) -> None:
    database, recovery, _path, plan_id = recovery_fixture(tmp_path, FakeExifTool(fail=True))
    recovery.apply(plan_id, apply=True)
    status = database.connection.execute("SELECT status FROM metadata_date_apply_items").fetchone()[
        0
    ]
    assert status == "skipped"
    database.close()
