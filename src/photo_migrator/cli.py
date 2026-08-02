"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from photo_migrator import __version__
from photo_migrator.analysis import AnalysisEngine
from photo_migrator.builder import Builder, Rollback
from photo_migrator.config import ConfigError, load_config
from photo_migrator.database import Database
from photo_migrator.db_tools import backup_database, check_database
from photo_migrator.doctor import run_doctor
from photo_migrator.hashing import HashEngine
from photo_migrator.logging_config import configure_logging
from photo_migrator.planner import Planner
from photo_migrator.recovery import audit
from photo_migrator.relationships import RelationshipEngine
from photo_migrator.scanner import Scanner
from photo_migrator.verifier import Verifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-migrator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    parser.add_argument("--log-format", choices=("text", "json"), default="text")
    parser.add_argument("--log-file", type=Path)
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
    build = subparsers.add_parser(
        "build", help="execute a reviewed migration plan (dry-run by default)"
    )
    build.add_argument("--database", type=Path, required=True)
    build.add_argument("--plan-id", type=int, required=True)
    build.add_argument("--mode", choices=("copy", "hardlink"))
    build.add_argument("--workers", type=int, default=1)
    build.add_argument("--allow-draft", action="store_true")
    build.add_argument("--resume", action="store_true")
    build.add_argument("--verify-only", action="store_true")
    build.add_argument("--limit", type=int)
    verify = subparsers.add_parser("verify", help="verify one build run's recorded destinations")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--build-run-id", type=int, required=True)
    verify.add_argument("--workers", type=int, default=1)
    verify.add_argument("--limit", type=int)
    verify.add_argument("--repair-metadata-only", action="store_true")
    rollback = subparsers.add_parser(
        "rollback", help="remove unchanged files owned by one build run"
    )
    rollback.add_argument("--database", type=Path, required=True)
    rollback.add_argument("--build-run-id", type=int, required=True)
    rollback.add_argument("--dry-run", action="store_true")
    rollback.add_argument("--confirm-owned-files-only", action="store_true")
    doctor = subparsers.add_parser("doctor", help="audit environment and database safety")
    doctor.add_argument("--database", type=Path, required=True)
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--plan-id", type=int)
    doctor.add_argument("--check-hardlinks", action="store_true")
    doctor.add_argument("--check-write-access", action="store_true")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--strict", action="store_true")
    db = subparsers.add_parser("db", help="database safety utilities")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    backup = db_commands.add_parser("backup", help="create an atomic SQLite online backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--overwrite", action="store_true")
    backup.add_argument("--verify", action="store_true")
    check = db_commands.add_parser("check", help="check database integrity without repair")
    check.add_argument("--database", type=Path, required=True)
    check.add_argument("--full", action="store_true")
    check.add_argument("--json", action="store_true")
    recover = subparsers.add_parser("recover", help="audit interrupted runs without file changes")
    recover.add_argument("--database", type=Path, required=True)
    recover.add_argument("--json", action="store_true")
    recover.add_argument("--mark-stale-failed", action="store_true")
    recover.add_argument("--older-than-minutes", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format, args.log_file)
    try:
        if args.command == "doctor":
            diagnostic, code = run_doctor(
                args.database,
                args.config,
                args.plan_id,
                args.check_hardlinks,
                args.check_write_access,
                args.strict,
            )
            print(
                json.dumps(diagnostic, indent=2, sort_keys=True)
                if args.json
                else "\n".join(
                    f"{item['status']:4} {item['name']}: {item['message']}"
                    for item in diagnostic["checks"]
                )
            )
            return code
        if args.command == "db":
            if args.db_command == "backup":
                print(
                    json.dumps(
                        backup_database(args.database, args.output, args.overwrite, args.verify),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            db_result = check_database(args.database, args.full)
            print(
                json.dumps(db_result, indent=2, sort_keys=True)
                if args.json
                else f"{db_result['check']}: {', '.join(db_result['integrity'])}; schema={db_result['schema_version']}"
            )
            return 0 if db_result["ok"] else 2
        if args.command == "recover":
            recovery_result = audit(args.database, args.older_than_minutes, args.mark_stale_failed)
            print(
                json.dumps(recovery_result, indent=2, sort_keys=True)
                if args.json
                else f"Stale runs: {len(recovery_result['stale_runs'])}; records updated: {recovery_result['records_updated']}"
            )
            return 1 if recovery_result["stale_runs"] and not args.mark_stale_failed else 0
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
        if args.command == "build":
            with Database(args.database) as database:
                database.initialize()
                run_id = Builder(database, args.workers).run(
                    args.plan_id,
                    args.mode,
                    args.allow_draft,
                    args.resume,
                    args.verify_only,
                    args.limit,
                )
            print(f"Build run {run_id} complete")
            return 0
        if args.command == "verify":
            with Database(args.database) as database:
                database.initialize()
                results = Verifier(database, args.workers).run(
                    args.build_run_id, args.limit, args.repair_metadata_only
                )
            failed = sum(result.status == "verification_failed" for result in results)
            print(f"Verification complete: checked={len(results)} failed={failed}")
            return 2 if failed else 0
        if args.command == "rollback":
            dry_run = args.dry_run or not args.confirm_owned_files_only
            with Database(args.database) as database:
                database.initialize()
                rollback_results = Rollback(database).run(
                    args.build_run_id, dry_run, args.confirm_owned_files_only
                )
            print(f"Rollback {'dry-run' if dry_run else 'complete'}: items={len(rollback_results)}")
            return 0
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
        latest_build = stats["latest_build_run"]
        print(f"Latest build run ID: {latest_build['id'] if latest_build else 'none'}")
        print(f"Latest build run plan ID: {latest_build['plan_id'] if latest_build else 'none'}")
        for label, key in (
            ("mode", "mode"),
            ("status", "status"),
            ("completed items", "completed_items"),
            ("failed items", "failed_items"),
            ("verified items", "verified_items"),
            ("verification failures", "verification_failed_items"),
            ("bytes written", "bytes_written"),
            ("rollback status", "rollback_status"),
        ):
            default = "none" if key in {"mode", "status", "rollback_status"} else 0
            print(f"Latest build {label}: {latest_build[key] if latest_build else default}")
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.error(
            "Interrupted by user; completed work is preserved. Run 'photo-migrator recover --database PATH' before resuming."
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
