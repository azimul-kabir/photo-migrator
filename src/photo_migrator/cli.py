"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from photo_migrator.analysis import AnalysisEngine
from photo_migrator.config import ConfigError, load_config
from photo_migrator.database import Database
from photo_migrator.hashing import HashEngine
from photo_migrator.scanner import Scanner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-migrator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="initialize an inventory database")
    init.add_argument("--database", type=Path, required=True)
    scan = subparsers.add_parser("scan", help="scan configured sources")
    scan.add_argument("--database", type=Path, required=True)
    scan.add_argument("--config", type=Path, required=True)
    stats = subparsers.add_parser("stats", help="show inventory statistics")
    stats.add_argument("--database", type=Path, required=True)
    hash_command = subparsers.add_parser("hash", help="hash exact-duplicate candidates")
    hash_command.add_argument("--database", type=Path, required=True)
    hash_command.add_argument("--workers", type=int, default=1)
    hash_command.add_argument("--limit", type=int)
    hash_command.add_argument("--source")
    hash_command.add_argument("--resume", action="store_true")
    analyze = subparsers.add_parser("analyze", help="extract normalized media metadata")
    analyze.add_argument("--database", type=Path, required=True)
    analyze.add_argument("--workers", type=int, default=1)
    analyze.add_argument("--limit", type=int)
    analyze.add_argument("--source")
    analyze.add_argument("--resume", action="store_true")
    analyze.add_argument("--retry-failed", action="store_true")
    analyze.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            args.database.parent.mkdir(parents=True, exist_ok=True)
            with Database(args.database) as database:
                database.initialize()
            print(f"Initialized database: {args.database}")
            return 0
        if args.command == "scan":
            config = load_config(args.config)
            with Database(args.database) as database:
                database.initialize()
                result = Scanner(database, config).scan()
            print(
                f"Scan complete: discovered={result.discovered} indexed={result.indexed} "
                f"errors={len(result.errors)}"
            )
            return 0 if not result.errors else 2
        if args.command == "hash":
            with Database(args.database) as database:
                database.initialize()
                return HashEngine(database, args.workers).run(args.limit, args.source, args.resume)
        if args.command == "analyze":
            with Database(args.database) as database:
                database.initialize()
                return AnalysisEngine(database, args.workers, args.ffprobe).run(
                    args.limit, args.source, args.resume, args.retry_failed
                )
        with Database(args.database) as database:
            database.initialize()
            stats = database.stats()
        print(f"Total indexed assets: {stats['total']}")
        print(f"Currently available assets: {stats['available']}")
        print(f"Missing assets: {stats['missing']}")
        print(f"Scan errors: {stats['errors']}")
        print(f"Total bytes: {stats['bytes']}")
        print("Totals by source:")
        for row in stats["by_source"]:
            print(f"  {row['source_name']}: {row['count']}")
        print("Totals by extension:")
        for row in stats["by_extension"]:
            print(f"  {row['extension']}: {row['count']}")
        latest = stats["latest_run"]
        print(f"Latest scan-run status: {latest['status'] if latest else 'none'}")
        print(f"Completed analyses: {stats['analyses_completed']}")
        print(f"Failed analyses: {stats['analyses_failed']}")
        print(f"Unsupported analyses: {stats['analyses_unsupported']}")
        print(f"Assets with captured dates: {stats['with_captured_at']}")
        print(f"Assets with GPS: {stats['with_gps']}")
        print(f"Image count: {stats['images']}")
        print(f"Video count: {stats['videos']}")
        latest_analysis = stats["latest_analysis_run"]
        print(
            "Latest analysis-run status: "
            f"{latest_analysis['status'] if latest_analysis else 'none'}"
        )
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
