from __future__ import annotations

import json
from pathlib import Path

import pytest

from photo_migrator.cli import main
from photo_migrator.config import ConfigError, load_config
from photo_migrator.database import Database
from photo_migrator.db_tools import backup_database, check_database, sha256_file
from photo_migrator.recovery import audit


def test_online_backup_check_and_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "photo.db"
    backup = tmp_path / "backup.db"
    with Database(source) as database:
        database.initialize()
    metadata = backup_database(source, backup, overwrite=False, verify=True)
    assert check_database(backup)["ok"] is True
    assert metadata["sha256"] == sha256_file(backup)
    assert json.loads(Path(f"{backup}.json").read_text())["verification_status"] == "passed"


def test_recovery_audit_does_not_mutate_without_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "photo.db"
    with Database(path) as database:
        database.initialize()
        database.connection.execute(
            "INSERT INTO scan_runs(started_at,status,source_count) VALUES(?,?,?)",
            ("2000-01-01T00:00:00+00:00", "running", 1),
        )
        database.connection.commit()
    result = audit(path, None, False)
    assert len(result["stale_runs"]) == 1
    with Database(path) as database:
        status = database.connection.execute("SELECT status FROM scan_runs").fetchone()[0]
    assert status == "running"


def test_cli_version_and_interrupt_exit_codes(capsys: object) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0


def test_library_inside_a_source_must_be_excluded(tmp_path: Path) -> None:
    library = tmp_path / "photo" / "CleanLibrary"
    library.mkdir(parents=True)
    config = tmp_path / "config.toml"
    base = (
        f'[[sources]]\nname="photo"\npath="{tmp_path / "photo"}"\n'
        f'[library]\nroot="{library}"\n[scan]\nextensions=[".jpg"]\n'
    )
    config.write_text(base)
    with pytest.raises(ConfigError, match="inside source photo"):
        load_config(config)
    config.write_text(base + 'exclude_directory_names=["CleanLibrary"]\n')
    assert load_config(config).library is not None
