# Photo Migrator

Photo Migrator is a safety-first utility for consolidating overlapping photo and video archives into one deduplicated, verified library suitable for Immich.

## Incremental canonical-library workflow

`CleanLibrary` is the permanent source of truth. Configure it as `[library].root`; configure only
outside, read-only folders as `[[sources]]`. For a macOS Photos package, list only its `originals`
directory—never the package root. Existing canonical media is indexed in place and is never
renamed or reorganized. Candidate media is never modified or deleted.

```bash
photo-migrator library-index --database photo.db --config config.toml
photo-migrator import-scan --database photo.db --config config.toml
photo-migrator import-plan --database photo.db --config config.toml
photo-migrator import-run --database photo.db --config config.toml --plan-id 1 --dry-run
photo-migrator import-run --database photo.db --config config.toml --plan-id 1 --confirm
```

Normal `library-index` performs a full reconciliation scan, marking new, changed, and missing
canonical files before hashing incomplete assets. After interrupted hashing, use
`library-index --resume` for fast hash-only recovery from the existing SQLite inventory; it skips
the canonical scan, validates each pending file's containment, type, size, and modification time,
and preserves completed hashes. Use normal `library-index` after adding, deleting, moving, or
modifying anything in `CleanLibrary` (`--rescan` explicitly requests the same full behavior).
Both scanning and hashing log progress every 500 assets or 30 seconds,
whichever comes first. Hashing percent complete and ETA are byte-based, which better represents
libraries containing a mix of photos and large videos. A production run looks like:

```text
INFO Canonical assets : 22,567
INFO Already hashed   : 17,726
INFO Remaining hashes : 4,841
INFO
INFO Phase 1/2: Scanning canonical library...
INFO Phase 1/2 | Scanned 5,000 / 22,567 (22.2%) | 34.7 GiB | 1m08s elapsed
INFO Phase 1/2 | Scanned 10,000 / 22,567 (44.3%) | 69.5 GiB | 2m17s elapsed
INFO Phase 1 complete.
INFO
INFO Phase 2/2: Hashing remaining canonical assets...
INFO Phase 2/2 | Indexed 18,226 / 22,567 canonical assets (79.1%) | 2.96 TiB / 3.74 TiB | 247 MiB/s | ETA 54m
...
INFO Library indexing complete.
```

Planning compares sizes first and hashes a candidate only when canonical files share its size.
Exact matches record the canonical path and avoid a copy. Real runs are copy-only and verify size
and SHA-256. Import execution reports progress every 500 files or 30 seconds, whichever comes
first, and resumes its counters from the latest interrupted run:

```text
INFO Import plan            : 7
INFO Files to import        : 36,563
INFO Existing duplicates    : 2
INFO Destination reuse      : 0
INFO Data to copy           : 207 GiB
INFO
INFO Destination            : /volume1/photo/CleanLibrary
INFO
INFO Phase 1/1: Importing files...
INFO Phase 1/1 | Imported 12,500 / 36,563 files (34.2%) | 73.8 GiB / 207 GiB (35.6%) | 42.7 MiB/s | ETA 1h 07m | Current Camera Imports/Mobile Backup/azimul/iPhone/2025/04/IMG_3220.HEIC
...
INFO Import Summary
==============

Files

Planned ............ 36,563
Copied ............. 36,561
Reused ............. 0
Skipped ............ 2
Failed ............. 0

Data

Copied ............. 207 GiB

Elapsed ............ 2h 13m
Average speed ...... 26.5 MiB/s

Destination ......... /volume1/photo/CleanLibrary
INFO Import complete.
```

The older commands remain the **legacy full-migration workflow**.

The project lockfile is the deployment contract for Python 3.9–3.12. Use
`uv sync --frozen --dev` for development or `uv sync --frozen` for runtime deployment. CI performs
the frozen install and all checks on each supported Python version. Reports and JSON sidecars are
written through same-directory, flushed atomic replacements, so a failed regeneration preserves
the previous complete report. The end-to-end smoke test uses temporary synthetic assets only.

## Core guarantees

- Source files are read-only.
- Nothing is deleted automatically.
- Exact duplicates require content verification.
- Long-running scans are resumable.
- Every planned and completed action is auditable.

## Planned workflow

1. Scan configured source folders into SQLite.
2. Group duplicate candidates by size.
3. Hash only candidate groups.
4. Plan one keeper for every exact-duplicate group.
5. Generate reports for review.
6. Build a clean library using copy or hardlink mode.
7. Verify destination files before Immich indexing.

## Installation

Python 3.9 or newer is required. Install the project with `uv`:

```bash
uv sync
```

Copy `config.example.toml`, set each source path, and then run:

```bash
uv run photo-migrator init --database inventory.db
uv run photo-migrator scan --database inventory.db --config config.toml
uv run photo-migrator stats --database inventory.db
uv run photo-migrator hash --database inventory.db
uv run photo-migrator analyze --database inventory.db
```

Scanning is resumable: assets are upserted by absolute path, prior scan runs remain in the
database, and files absent after a successful source scan are retained as missing.

## Hashing and exact-duplicate reports

`hash` uses SQL size grouping to select only plausible duplicate candidates, then streams each
candidate through SHA-256 in 1 MiB chunks. It never modifies a source. Hashing defaults to one
worker; `--workers 4` enables parallel reads while keeping SQLite writes serialized. Use `--limit
500` to bound a batch, `--source NAME` to restrict it, and `--resume` to retry failed work. Completed
hashes are reused when the indexed size and modification time still match.

Every invocation is retained in `hash_runs`. The command writes deterministic reports beside the
database in `reports/hash_summary.txt` and `reports/duplicate_groups.csv`. The CSV has one row per
duplicate file with its SHA-256, copy count, size, group byte total, and path.

Limitations: detection is exact-content only. It does not inspect EXIF, identify Live Photos,
choose keepers, delete duplicates, create a migration plan, or copy/link/import files. A failed
file remains failed until explicitly retried with `--resume`.

## Safety

Milestone 1 performs **metadata-only filesystem scanning**. It reads directory entries and file
stat metadata, but does not open or hash file contents. It does not copy, rename, move, hardlink,
or delete media. Directory symlinks are not followed, and all discovered paths are checked for
containment within their configured source root.

Library building, deletion, and Immich integration are intentionally outside the current milestone.

## Media metadata analysis

Install dependencies with `uv sync`. Pillow reads conventional images and `pillow-heif` registers
HEIC/HEIF decoding where the platform decoder supports the file. Video analysis also requires the
external `ffprobe` executable (normally supplied by FFmpeg) on `PATH`; select another executable
with `--ffprobe PATH`.

```bash
uv run photo-migrator analyze --database inventory.db
uv run photo-migrator analyze --database inventory.db --workers 2
uv run photo-migrator analyze --database inventory.db --source MobileBackup --limit 500
uv run photo-migrator analyze --database inventory.db --retry-failed
```

Analysis supports JPEG, HEIC/HEIF, PNG, TIFF, WebP, and DNG images where the installed decoder
permits, plus MOV, MP4, and M4V video. It normalizes dimensions, orientation, camera and lens,
GPS, capture time, duration, codecs, container, frame rate, bitrate, and color space into SQLite.
Image capture time prefers `DateTimeOriginal`, then `DateTimeDigitized`, then EXIF `DateTime`.
Video capture time prefers format-level `creation_time`, then video-stream `creation_time`.
Filesystem modification time is never treated as capture time. EXIF dates without timezone data
remain naive local ISO-8601 timestamps; the tool does not invent an offset.

Completed results are reused only while indexed and analyzed size and nanosecond modification time
still match the source. Assets left in `running` state by an interrupted process are safely queued
again on the next analysis run. Pillow's decompression-bomb protection remains enabled: images that
exceed its safety limit are recorded as per-asset failures, including the reported pixel count and
safety limit, while unrelated assets continue. Review these entries separately in the metadata
reports rather than disabling the protection.
`--resume` may be used to make this recovery intent explicit, while `--retry-failed` explicitly
retries failed and unsupported records. Decoder, file, and ffprobe failures are stored per asset and
do not stop other assets. SQLite writes are serialized even when `--workers` enables concurrent
reads.

Each run writes deterministic `reports/metadata_summary.txt`, `reports/camera_statistics.csv`, and
`reports/missing_metadata.csv` files. The latter is informational: camera and GPS are not presumed
to be required for every image.

### Metadata safety and current limitations

Source files are opened read-only. No EXIF or media data is written, and no file is copied, moved,
renamed, linked, transcoded, or deleted. Analysis only writes the configured inventory database and
its adjacent `reports/` directory. Live Photo and Motion Photo pairing are not yet implemented.
# Live and Motion Photo relationships

Milestone 3B adds read-only relationship analysis after scanning (and preferably metadata
analysis):

```bash
photo-migrator relate --database photo.db
photo-migrator relate --database photo.db --workers 2
photo-migrator relate --database photo.db --source MobileBackup --limit 500
```

`--resume`, `--retry-failed`, and `--ffprobe PATH` are available for interrupted/error
recovery and video metadata inspection. Apple Live Photos are paired primarily with embedded
content identifiers from image metadata and QuickTime tags. Google Pixel XMP motion flags and
validated offsets, and explicit Samsung motion-photo markers, identify self-contained Motion
Photos; the embedded motion video is recorded but **not extracted**.

Exact image-to-video Apple identifiers are the highest-confidence pairs. When an identifier-bearing
video's image identifier is missing (commonly after an export strips image metadata), a distinct
one-sided Apple fallback requires the same source, exact parent directory, normalized stem, and
exactly one eligible image. Timestamps are classified as `exact_within_3_seconds`,
`same_wall_clock_within_3_seconds`, `timezone_offset_compatible`, `unavailable`, or `incompatible`.
The first three classifications produce a 0.90-confidence pair; missing or unparseable timestamps
produce a 0.85-confidence pair because the exact directory/stem match remains strong. Clearly
incompatible timestamps produce an ambiguous review record rather than an active pair. Multiple
eligible images are also ambiguous, and reused `IMG_####` names in other folders are never
considered by this Apple fallback.

When identifiers are absent on both components, the engine conservatively considers only supported image/video
extensions with the same normalized stem and source, the same or a nearby directory, and capture
times within three seconds when both exist. Apple's `IMG_E1234` edit name normalizes to
`IMG_1234`. Conflicting identifiers are never overridden. Equal candidates are reported as
ambiguous rather than selected; fallback pairing may require user review. Identifier-bearing
unmatched components are reported as orphans, while ordinary standalone videos are not.

Relationship state is stored in SQLite and analysis size/mtime snapshots make results auditable.
Runs are retained and repeated results are reused without duplicate rows. Large-library
persistence stages inspected asset and retained relationship IDs in connection-local SQLite
temporary tables, so cleanup does not depend on SQLite's host-parameter limit. The staging data
is recreated for each run and relationship inserts, stale-row cleanup, and asset status updates
commit atomically. An unexpected detector failure is recorded against that asset while unrelated
inspections continue; the run then finishes `completed_with_errors`. Deterministic files in
`reports/` are:

* `relationship_summary.txt` — separate counts for exact Apple pairs, one-sided Apple fallbacks,
  generic filename fallbacks, genuine orphans, and the latest run status;
* `asset_relationships.csv` — normalized relationships, timestamp classification, and evidence;
* `orphan_assets.csv` — unmatched supported components and errors;
* `ambiguous_relationships.csv` — all review candidates and timestamp classifications.

## Relationship safety and limitations

Media files are opened read-only. No file is copied, moved, renamed, linked, rewritten,
transcoded, or deleted, and no sidecar is edited. Metadata parsing is deliberately bounded and
supports common XMP, QuickTime, and Samsung markers rather than every vendor-specific layout.
Malformed metadata and offsets are surfaced as errors or invalid records. Separate-component
Google/Samsung pairing requires explicit evidence; embedded video is never materialized. Keeper
selection and migration planning remain unimplemented, as do conversion, perceptual matching,
HTML output, and Immich integration.
# Migration planning (Milestone 4)

Create an immutable, reviewable dry-run snapshot after scanning, hashing, metadata analysis,
and relationship analysis:

```bash
photo-migrator plan --database photo.db --config config.toml
photo-migrator plan --database photo.db --config config.toml --source MobileBackup --limit 500
photo-migrator plan --database photo.db --config config.toml --include-orphans
```

Planning is a strict safety boundary: **no destination directory is created and no media file
is copied, linked, moved, renamed, rewritten, or deleted**. The only writes are SQLite planning
history and deterministic CSV/text files below the database-adjacent `reports/` directory.
Plan output must be reviewed before a future build milestone.

Exact duplicates are matched by completed SHA-256 only. Within each byte-identical group the
ordered keeper precedence is relationship completeness, source priority, configured source
override, human-readable name, non-UUID name, metadata completeness, shorter relative path,
and finally lexical path (plus asset ID). This makes ties explicit and deterministic.

Active Apple Live Photo image/video pairs are relationship-aware bundles; self-contained Google
and Samsung Motion Photos remain single-file bundles. Active filename fallback pairs below
`--minimum-fallback-confidence` and ambiguous or orphan relationships require manual review.
`--include-orphans` explicitly permits standalone orphan components. Conflicting active bundles
are blocked.

The `[planning]` configuration selects an absolute, non-overlapping `destination_root`, a naming
template, `date_fallback`, collision case rules, and a filename limit. Supported fields are
`year`, `month`, `day`, `hour`, `minute`, `second`, `timestamp`, `original_name`, `stem`,
`extension`, `source_name`, and `asset_id`. Capture metadata drives date fields; missing or invalid
dates use the configured undated directory, never filesystem mtime. Unicode is preserved,
controls and separators are removed, and truncation preserves extensions.

The complete normalized namespace is collision checked (including case and Unicode normalization).
Collisions receive a deterministic `__a<asset-id>` suffix and are rechecked; unsafe or impossible
paths are blocked. A plan is `ready` with no review/blocked items, `draft` with review items,
`blocked` with blocked items, or `superseded` when explicitly replaced with `--supersede-draft`.

Each `reports/plan_<id>/` contains `plan_summary.txt`, `keeper_decisions.csv`,
`migration_plan.csv`, `duplicate_skips.csv`, `relationship_bundles.csv`, `collisions.csv`,
`review_items.csv`, and `blocked_items.csv`. Reports include source paths solely for auditability.
Current limitations are planning-only: no build, conversion, EXIF writing, embedded-video
extraction, perceptual matching, HTML, or Immich integration is performed.

## Controlled library builds, verification, and rollback

Milestone 5 can execute a reviewed plan while remaining read-only toward every source. A `ready`
plan can be built directly; a `draft` plan requires `--allow-draft`, and `blocked` or `superseded`
plans are rejected. The destination root and plan fingerprint are snapshotted in SQLite when the
build starts, so later configuration cannot redirect an existing run. The builder rechecks that the
stored destination does not overlap any recorded source root.

`photo-migrator build --database photo.db --plan-id 12` is a **dry-run by default**. It verifies
source path type, containment, size, mtime, and SHA-256, records results, and writes reports, but it
does not create the destination root, directories, or media. Real writes require either
`--mode copy` or `--mode hardlink`; `--workers` controls read/copy/hash workers and SQLite updates
remain coordinated on one connection. Only `keep` and `include_relationship_member` items execute.
Review, blocked, and exact-duplicate-skip actions never become destination files. Use `--limit` for a
deterministic prefix and explicit `--resume` to reassess a compatible prior run snapshot.

Copy mode streams to a uniquely created `.photo-migrator-<run>-<item>.tmp` file in the final
 directory, fsyncs and SHA-256 verifies that temporary file, preserves atime/mtime, then atomically
links it into the unused final name and verifies the final content. It never writes directly to or
overwrites a final name. Filesystem support for directory fsync varies, so unsupported directory
fsync is best-effort; file fsync and content verification remain mandatory.

Hardlink mode re-verifies the source, requires the source and destination directory to be on the
same filesystem, creates a non-following hardlink, and checks device/inode identity and destination
SHA-256. A hardlink shares the source inode: destination content and metadata must therefore never
be modified. The builder does not call chmod, chown, utime, or extended-attribute operations in
hardlink mode. Unlinking an owned destination link during rollback does not remove source content
while the source link remains.

Every destination component is checked with `lstat`; symlink components and non-directory parents
are rejected, and lexical plus resolved containment is checked immediately before use. There is no
overwrite option. Existing identical regular files are verified and skipped without being claimed;
conflicting files or any symlink/non-regular destination fail visibly and remain untouched.

Run `photo-migrator verify --database photo.db --build-run-id 7` to recheck only that run's recorded
paths. Verification never scans unrelated paths and never repairs media; `--repair-metadata-only`
only permits refreshing SQLite verification state. Missing files, changed sizes/hashes, and invalid
path types are recorded as verification failures.

`photo-migrator rollback --database photo.db --build-run-id 7` is a dry-run. Real deletion requires
`--confirm-owned-files-only`. Rollback removes only an exact recorded, regular, non-symlink file
owned by that build run whose current SHA-256 still matches the successful build record.
Pre-existing identical files are never claimed, included in the rollback plan, or removed. Changed
owned files require manual review. Directories are left intact because directory ownership is not
tracked, and rollback is idempotent. Source deletion, moving, renaming, metadata editing, automatic
cleanup, content repair, transcoding, embedded-video extraction, and Immich API integration remain
unimplemented.

Each run writes deterministic files under `reports/build_<BUILD_RUN_ID>/`:
`build_summary.txt`, `build_items.csv`, `failed_items.csv`, `verification_results.csv`,
`bundle_results.csv`, `existing_destination_items.csv`, `rollback_plan.csv`, and
`rollback_results.csv`. SQLite permanently retains build snapshots, item-level source/destination
hashes, ownership, verification state, byte counts, errors, and rollback state. `photo-migrator
stats` also shows the latest build and rollback counters.

## Production hardening and support

Release **0.1.0** supports Python 3.9–3.12 on Linux (including compatible Synology DSM environments) and macOS; Windows is not claimed. The package metadata is the authoritative version and follows semantic versioning while the project is pre-1.0. CI tests every supported Python version.

```sh
uv sync
uv sync --dev
uv run photo-migrator --version
uv run photo-migrator doctor --database photo.db --config config.toml --strict
uv run photo-migrator db check --database photo.db --full
uv run photo-migrator db backup --database photo.db --output photo.backup.db --verify
uv run photo-migrator recover --database photo.db --json
```

Global `--log-level DEBUG|INFO|WARNING|ERROR`, `--log-format text|json`, and `--log-file PATH` options provide diagnostics. JSON records contain timestamps, severity, logger and message, plus operation context when available. Exit codes are 0 success, 1 warnings/partial audit findings, 2 validation or safety failure, and 130 user interruption.

Read the [Synology guide](docs/synology.md) and [safe first-run runbook](docs/first-run.md). Production use must begin with a small test source and copy mode. Do not delete sources until independent verification and backup exist. Photo Migrator cannot protect against disk failure, RAID is not a backup, and Photo Migrator does not manage Immich or its API yet. This is a pre-1.0 release: retain sources and independently inspect all reports.
