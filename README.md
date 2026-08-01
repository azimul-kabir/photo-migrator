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
