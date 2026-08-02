from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from photo_migrator import atomic_io


def test_atomic_text_csv_and_json(tmp_path: Path) -> None:
    text = tmp_path / "nested" / "report.txt"
    atomic_io.atomic_write_text(text, "héllo\nsecond\n")
    assert text.read_text() == "héllo\nsecond\n"

    report = tmp_path / "report.csv"
    atomic_io.atomic_write_csv(report, [["β", "line\nbreak"], [1, 2]], ["a", "b"])
    assert list(csv.reader(report.open(newline=""))) == [
        ["a", "b"],
        ["β", "line\nbreak"],
        ["1", "2"],
    ]
    assert report.read_bytes().startswith(b"a,b\n")

    sidecar = tmp_path / "report.json"
    atomic_io.atomic_write_json(sidecar, {"z": 1, "a": "é"})
    assert json.loads(sidecar.read_text()) == {"a": "é", "z": 1}
    assert sidecar.read_text().endswith("\n")


def test_failure_preserves_final_and_only_cleans_own_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "report.txt"
    destination.write_text("complete")
    unknown = tmp_path / ".photo-migrator-report.txt.unknown.tmp"
    unknown.write_text("unrelated")

    def fail_replace(source: Path, target: Path) -> None:
        assert source.parent == target.parent == tmp_path
        raise OSError("simulated placement failure")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_io.atomic_write_text(destination, "partial replacement")
    assert destination.read_text() == "complete"
    assert unknown.read_text() == "unrelated"
    assert sorted(tmp_path.iterdir()) == sorted([destination, unknown])


def test_directory_fsync_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atomic_io.os, "open", lambda *args: (_ for _ in ()).throw(OSError()))
    atomic_io._fsync_directory(tmp_path)
