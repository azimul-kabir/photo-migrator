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
```

Scanning is resumable: assets are upserted by absolute path, prior scan runs remain in the
database, and files absent after a successful source scan are retained as missing.

## Milestone 1 safety

Milestone 1 performs **metadata-only filesystem scanning**. It reads directory entries and file
stat metadata, but does not open or hash file contents. It does not copy, rename, move, hardlink,
or delete media. Directory symlinks are not followed, and all discovered paths are checked for
containment within their configured source root.

Hashing, duplicate detection, EXIF extraction, library building, deletion, and Immich integration
are intentionally outside this milestone.
