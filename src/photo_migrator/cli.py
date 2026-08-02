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
from photo_migrator.planner import Planner
from photo_migrator.relationships import RelationshipEngine
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
    relate = subparsers.add_parser("relate", help="detect Live and Motion Photo relationships")
    relate.add_argument("--database", type=Path, required=True)
    relate.add_argument("--workers", type=int, default=1)
    relate.add_argument("--limit", type=int)
    relate.add_argument("--source")
    relate.add_argument("--resume", action="store_true")
    relate.add_argument("--retry-failed", action="store_true")
    relate.add_argument("--ffprobe", default="ffprobe")
    plan = subparsers.add_parser("plan", help="create a read-only migration plan")
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--source")
    plan.add_argument("--limit", type=int)
    plan.add_argument("--supersede-draft", action="store_true")
    plan.add_argument("--include-orphans", action="store_true")
    plan.add_argument("--minimum-fallback-confidence", type=float, default=0.75)
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
        if args.command == "relate":
            with Database(args.database) as database:
                database.initialize()
                return RelationshipEngine(database, args.workers, args.ffprobe).run(
                    args.limit, args.source, args.resume, args.retry_failed
                )
        if args.command == "plan":
            config = load_config(args.config)
            with Database(args.database) as database:
                database.initialize()
                plan_result = Planner(database, config).run(
                    args.source,
                    args.limit,
                    args.supersede_draft,
                    args.include_orphans,
                    args.minimum_fallback_confidence,
                )
            print(
                f"Plan {plan_result.plan_id}: status={plan_result.status} "
                f"candidates={plan_result.candidates} items={plan_result.items} "
                f"collisions={plan_result.collisions} blocked={plan_result.blocked}"
            )
            return 0 if plan_result.status != "blocked" else 2
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
        print(f"Active relationships: {stats['relationships_active']}")
        print(f"Apple Live Photos: {stats['apple_live_photos']}")
        print(f"Google Motion Photos: {stats['google_motion_photos']}")
        print(f"Samsung Motion Photos: {stats['samsung_motion_photos']}")
        print(f"Fallback filename pairs: {stats['filename_pairs']}")
        print(f"Orphan motion images: {stats['orphan_motion_images']}")
        print(f"Orphan motion videos: {stats['orphan_motion_videos']}")
        print(f"Ambiguous relationships: {stats['ambiguous_relationships']}")
        print(f"Invalid relationships: {stats['invalid_relationships']}")
        latest_relationship = stats["latest_relationship_run"]
        print(
            "Latest relationship-run status: "
            f"{latest_relationship['status'] if latest_relationship else 'none'}"
        )
        latest_plan = stats["latest_plan"]
        counts = stats["latest_plan_counts"]
        print(f"Latest plan ID: {latest_plan['id'] if latest_plan else 'none'}")
        print(f"Latest plan status: {latest_plan['status'] if latest_plan else 'none'}")
        print(
            "Latest plan candidate assets: "
            f"{latest_plan['source_asset_count'] if latest_plan else 0}"
        )
        for label, key in (
            ("keep actions", "keep_count"),
            ("duplicate skips", "duplicate_skips"),
            ("relationship members", "relationship_members"),
            ("review items", "review_items"),
            ("blocked items", "blocked_items"),
            ("collisions", "collisions"),
        ):
            print(f"Latest plan {label}: {counts[key] or 0 if counts else 0}")
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
