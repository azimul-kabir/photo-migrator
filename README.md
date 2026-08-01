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

## Status

The repository is in its initial scaffolding phase. The first milestone is a read-only scanner and SQLite inventory.
