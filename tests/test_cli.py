from pathlib import Path

from photo_migrator.cli import main


def test_init_scan_and_stats(tmp_path: Path, capsys: object) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "picture.JPG").write_bytes(b"photo")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[[sources]]\nname="photos"\npath="{source}"\npriority=10\n'
        '[scan]\nextensions=[".jpg"]\nexclude_directory_names=[]\n'
    )
    database = tmp_path / "inventory.db"
    assert main(["init", "--database", str(database)]) == 0
    assert main(["scan", "--database", str(database), "--config", str(config)]) == 0
    assert main(["stats", "--database", str(database)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Total indexed assets: 1" in output
    assert "Latest scan-run status: completed" in output


def test_invalid_config_returns_error(tmp_path: Path) -> None:
    assert (
        main(["scan", "--database", str(tmp_path / "db"), "--config", str(tmp_path / "missing")])
        == 2
    )
