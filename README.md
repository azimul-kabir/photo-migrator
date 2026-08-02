# Photo Migrator

Photo Migrator is a safety-first utility for consolidating overlapping photo and video archives into one deduplicated, verified library suitable for Immich.

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
match. `--resume` retries interrupted `running` records, while `--retry-failed` explicitly retries
failed and unsupported records. Decoder, file, and ffprobe failures are stored per asset and do not
stop other assets. SQLite writes are serialized even when `--workers` enables concurrent reads.

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

When identifiers are absent, the engine conservatively considers only supported image/video
extensions with the same normalized stem and source, the same or a nearby directory, and capture
times within three seconds when both exist. Apple's `IMG_E1234` edit name normalizes to
`IMG_1234`. Conflicting identifiers are never overridden. Equal candidates are reported as
ambiguous rather than selected; fallback pairing may require user review. Identifier-bearing
unmatched components are reported as orphans, while ordinary standalone videos are not.

Relationship state is stored in SQLite and analysis size/mtime snapshots make results auditable.
Runs are retained and repeated results are reused without duplicate rows. Deterministic files in
`reports/` are:

* `relationship_summary.txt` — counts and latest run status;
* `asset_relationships.csv` — normalized relationships and evidence;
* `orphan_assets.csv` — unmatched supported components and errors;
* `ambiguous_relationships.csv` — all equally plausible candidates.

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
