# AGENTS.md

## Mission
Photo Migrator consolidates overlapping photo and video archives into one verified, deduplicated, Immich-ready library while keeping all source files unchanged.

## Non-negotiable safety rules
1. Never modify, rename, move, link over, truncate, or delete source files.
2. Destructive operations must not exist in the default CLI path.
3. Every write operation must support dry-run mode.
4. Build actions must write only inside the configured destination directory.
5. A destination path must be containment-checked after resolution.
6. Every copied or hardlinked asset must be auditable in SQLite and CSV reports.
7. Exact duplicates require size plus content hash equality.
8. Live Photo image and video components are separate assets and must never be collapsed into one file.
9. Errors must be recorded and surfaced; never silently skip unreadable files.
10. Tests must use temporary directories and synthetic files only.

## Initial scope
- Python 3.9+
- SQLite-backed inventory
- Read-only scanning
- Exact duplicate candidate detection using file size, followed by SHA-256
- Resumable hashing
- Source-priority-based keeper planning
- Dry-run reports
- Copy and hardlink build modes added only after planning and verification are complete

## Out of scope for the first milestone
- Deleting originals
- Near-duplicate or perceptual matching
- Editing EXIF metadata, except opt-in capture-date recovery (`metadata-date-apply --apply`),
  which writes only canonical-library files and first keeps a verified byte backup of each
  original for rollback
- Importing through the Immich API
- Parsing macOS Photos library databases

## Engineering standards
- Use a `src/` package layout.
- Prefer the Python standard library unless a dependency clearly improves correctness.
- Use explicit SQL through a small repository layer; do not add an ORM without justification.
- Keep modules focused and typed.
- Use `pathlib.Path` for filesystem paths.
- Use transactions for database mutations.
- Commands must be idempotent where practical.
- Long-running operations must support restart/resume.
- Logging must include enough context to identify the source path and operation.

## Required checks before committing
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Review checklist
- Can this change alter a source file?
- Can a path escape the configured destination?
- Is interrupted execution recoverable?
- Are errors visible and persisted?
- Is the result deterministic?
- Are tests covering failure and safety cases?
