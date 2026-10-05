# Safe first run

Treat `CleanLibrary` as the permanent canonical source of truth and every `[[sources]]` folder as
read-only. Run `library-index`, `import-scan`, `import-plan`, and `import-run --dry-run` in that
order; review `reports/import_plan_<ID>/` before supplying `--confirm`.

Use `uv sync --frozen` (`--dev` on a validation workstation). Before configuring real sources,
run `uv run pytest tests/test_end_to_end.py` and confirm the Python 3.9–3.12 CI matrix is green for
the deployed commit. Create and verify a database backup immediately before the first full build
and another after successful verification; retain both backups and their JSON sidecars.

The numbered steps below are for the legacy full-migration workflow (see "Choosing a workflow" in
the README). For the incremental workflow, follow steps 1–4 and 7, then run `library-index`,
`import-scan`, `import-plan`, review the plan reports, `import-run --dry-run`, and finally
`import-run --confirm`.

1. Clone the repository, run `uv sync --frozen --dev`, then `uv run photo-migrator --version`.
2. Copy `config.synology.example.toml` to `config.toml`; review every source, priority, exclusion, and destination.
3. Run `uv run photo-migrator doctor --database /path/photo.db --config config.toml --strict`.
4. Initialize with `uv run photo-migrator init --database /path/photo.db`.
5. First change the config to a small synthetic or expendable test *copy* of a source.
6. Run `uv run photo-migrator scan --database /path/photo.db --config config.toml`, then `stats` and inspect errors.
7. Back up: `uv run photo-migrator db backup --database /path/photo.db --output /path/photo-pre-hash.db --verify`.
8. Run `hash`, `analyze --ffprobe ffprobe`, and `relate --ffprobe ffprobe`, each with `--workers 1`; inspect relationship CSV reports.
9. Run `plan --database /path/photo.db --config config.toml`; review **every** plan report.
10. Run build without `--mode` for its dry run; review all build reports.
11. Run a limited copy build (`build --database /path/photo.db --plan-id ID --mode copy --limit 10`), verify it (`verify --database /path/photo.db --build-run-id ID`), and manually inspect the destination.
12. Only after approval run the full copy build, verify it, and back up the database again.

Use copy mode for the first production migration; avoid hardlinks initially. A rollback must first be dry-run and can remove only unchanged, tool-owned destination files when explicitly confirmed. Never delete old libraries based on one successful build. Keep sources unchanged until the clean library has independent verification **and an independent backup**. RAID is not a backup and this tool cannot protect against disk failure.
