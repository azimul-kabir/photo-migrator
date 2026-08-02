"""Production environment diagnostics; no repair is performed."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from photo_migrator import __version__
from photo_migrator.config import Config, load_config
from photo_migrator.database import SCHEMA_VERSION
from photo_migrator.db_tools import check_database, read_only


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    details: dict[str, Any] | None = None


def _probe(directory: Path) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".photo-migrator-write-probe-", dir=directory)
    os.close(descriptor)
    Path(name).unlink()


def _config_checks(config: Config, write: bool) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("configuration", "PASS", "configuration is valid"))
    for source in config.sources:
        checks.append(
            Check(
                f"source:{source.name}",
                "PASS" if os.access(source.path, os.R_OK) else "FAIL",
                f"{source.path} is {'readable' if os.access(source.path, os.R_OK) else 'not readable'}",
            )
        )
        checks.append(
            Check(
                f"source-symlink:{source.name}",
                "WARN" if source.path.is_symlink() else "PASS",
                "source root is a symlink"
                if source.path.is_symlink()
                else "source root is not a symlink",
            )
        )
    if config.planning is None:
        checks.append(Check("destination", "SKIP", "planning.destination_root is not configured"))
        return checks
    destination = config.planning.destination_root
    parent = destination if destination.exists() else destination.parent
    checks.append(Check("destination-parent", "PASS" if parent.exists() else "FAIL", str(parent)))
    hazard = any(part.is_symlink() for part in [destination, *destination.parents] if part.exists())
    checks.append(
        Check(
            "destination-symlinks",
            "FAIL" if hazard else "PASS",
            "symlink component detected" if hazard else "no symlink component detected",
        )
    )
    if write and parent.exists():
        try:
            _probe(parent)
            checks.append(
                Check("destination-write-access", "PASS", f"temporary probe succeeded in {parent}")
            )
        except OSError as exc:
            checks.append(Check("destination-write-access", "FAIL", str(exc)))
    else:
        checks.append(
            Check("destination-write-access", "SKIP", "use --check-write-access to probe")
        )
    usage = shutil.disk_usage(parent) if parent.exists() else None
    checks.append(
        Check(
            "destination-free-space",
            "PASS" if usage else "SKIP",
            f"{usage.free} bytes free" if usage else "destination parent unavailable",
        )
    )
    return checks


def run_doctor(
    database: Path,
    config_path: Path | None,
    plan_id: int | None,
    hardlinks: bool,
    write: bool,
    strict: bool,
) -> tuple[dict[str, Any], int]:
    checks = [
        Check(
            "python",
            "PASS" if (3, 9) <= sys.version_info[:2] <= (3, 12) else "FAIL",
            platform.python_version(),
        ),
        Check("package-version", "PASS", __version__),
        Check("platform", "PASS", f"{platform.system()} {platform.machine()}"),
        Check("sqlite", "PASS", sqlite3.sqlite_version),
        Check(
            "sqlite-foreign-keys",
            "PASS"
            if sqlite3.connect(":memory:")
            .execute("PRAGMA foreign_keys=ON")
            .execute("PRAGMA foreign_keys")
            .fetchone()[0]
            else "FAIL",
            "supported",
        ),
    ]
    if database.exists():
        try:
            result = check_database(database, strict)
            checks.extend(
                (
                    Check("database-exists", "PASS", str(database)),
                    Check(
                        "database-schema",
                        "PASS" if result["schema_version"] == SCHEMA_VERSION else "WARN",
                        f"schema {result['schema_version']}, current {SCHEMA_VERSION}",
                    ),
                    Check(
                        "database-integrity",
                        "PASS" if result["ok"] else "FAIL",
                        ", ".join(result["integrity"]),
                    ),
                    Check(
                        "running-runs",
                        "WARN" if any(result["running_runs"].values()) else "PASS",
                        str(result["running_runs"]),
                    ),
                    Check(
                        "build-consistency",
                        "FAIL" if result["orphaned_references"] else "PASS",
                        f"{result['orphaned_references']} orphaned references",
                    ),
                )
            )
            with read_only(database) as connection:
                mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            checks.append(
                Check("sqlite-wal", "PASS" if mode == "wal" else "WARN", f"journal mode is {mode}")
            )
        except sqlite3.DatabaseError as exc:
            checks.append(Check("database-integrity", "FAIL", str(exc)))
    else:
        checks.append(Check("database-exists", "FAIL", str(database)))
    if config_path:
        checks.extend(_config_checks(load_config(config_path), write))
    else:
        checks.append(Check("configuration", "SKIP", "no --config supplied"))
    ffprobe = shutil.which("ffprobe")
    version = "not found"
    if ffprobe:
        completed = subprocess.run(
            [ffprobe, "-version"], capture_output=True, text=True, check=False
        )
        version = completed.stdout.splitlines()[0] if completed.stdout else ffprobe
    checks.append(Check("ffprobe", "PASS" if ffprobe else "WARN", version))
    for module, label in (("PIL", "Pillow"), ("pillow_heif", "pillow-heif")):
        checks.append(
            Check(
                label,
                "PASS" if importlib.util.find_spec(module) else "WARN",
                "available" if importlib.util.find_spec(module) else "not available",
            )
        )
    checks.append(
        Check(
            "hardlinks",
            "SKIP" if not hardlinks else "WARN",
            "not requested"
            if not hardlinks
            else "verify same filesystem and immutability before use",
        )
    )
    checks.append(
        Check(
            "plan",
            "SKIP" if plan_id is None else "WARN",
            "no plan supplied" if plan_id is None else f"plan {plan_id} requires capacity review",
        )
    )
    checks.sort(key=lambda item: item.name)
    failures = any(item.status == "FAIL" for item in checks)
    warnings = any(item.status == "WARN" for item in checks)
    code = 2 if failures else (1 if strict and warnings else 0)
    return {"checks": [asdict(item) for item in checks], "exit_code": code}, code
