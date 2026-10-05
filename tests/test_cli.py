from pathlib import Path

import pytest

from photo_migrator.cli import COMMANDS, build_parser, main


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


def test_library_index_resume_and_rescan_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "library-index",
                "--database",
                str(tmp_path / "db"),
                "--config",
                str(tmp_path / "config"),
                "--resume",
                "--rescan",
            ]
        )
    assert error.value.code == 2


def test_every_subcommand_has_a_handler() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command" and action.choices
    )
    assert set(subparsers.choices) == set(COMMANDS)
