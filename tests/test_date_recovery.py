import csv
from datetime import datetime
from pathlib import Path

from photo_migrator.config import MetadataDateRecoveryConfig
from photo_migrator.date_recovery import (
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
