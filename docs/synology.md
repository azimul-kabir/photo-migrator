# Synology DSM deployment

Configure `/volume1/photo/CleanLibrary` under `[library]`; it is the permanent source of truth.
Candidate folders remain read-only. For Apple Photos configure only
`/volume1/photo/Photos Library.photoslibrary/originals`, not the package root.

Use Python 3.11 or newer where DSM makes it available (the supported range is 3.9–3.13). If the NAS
Python is older than 3.9, install a supported interpreter from a trusted package or container; do
not bypass the project requirement. Deploy only the committed lock with `uv sync --frozen` and do
not run an unlocked dependency upgrade on the NAS. Upgrade with:

```sh
git pull
uv sync --frozen
uv run photo-migrator doctor --database /path/photo.db --config config.toml
uv run photo-migrator db check --database /path/photo.db
```

Photo Migrator supports DSM's Linux environment when Python 3.9–3.13 is available. Install Git and a current Python from Package Center, or a trusted SynoCommunity package where appropriate for your DSM/model. Install an ffmpeg package that includes `ffprobe`; confirm with `command -v ffprobe` and `ffprobe -version`. Package availability differs by DSM release, so verify publisher and architecture rather than pasting unreviewed root commands.

Use an ordinary dedicated DSM user, **not root**, and grant read-only Synology ACL access to sources plus write access only to the destination and data share. Clone to `/volume1/docker/photo-migrator` and keep state at `/volume1/docker/photo-migrator-data/photo.db`. Install `uv` into that user's home or a directory on the shared data volume, then:

```sh
cd /volume1/docker/photo-migrator
uv sync --frozen --dev
uv run photo-migrator --version
```

Reports are created beside the database under `reports/`. Review ACL inheritance. Exclude `@eaDir`, `#recycle`, `Photos Library.photoslibrary`, and the destination. Never treat Synology-generated metadata as source media.

For a DS220+, use one process/worker for scan, hash, analyze, relate, copy/hardlink build. Long commands may be run in `tmux`; if using `nohup`, redirect both output streams and retain logs. Do not configure a daemon or scheduled task. Check free space and database backups often.

Before major work:

```sh
uv run photo-migrator doctor --database /volume1/docker/photo-migrator-data/photo.db --config config.toml --strict
uv run photo-migrator db backup --database /volume1/docker/photo-migrator-data/photo.db --output /volume1/docker/photo-migrator-data/photo-before-run.db --verify
```

Run `library-index` normally to fully scan and reconcile `CleanLibrary`, then hash new or changed
assets. If hashing is interrupted (for example, by a power failure), restart with
`library-index --resume`: this skips the slow NAS directory scan and validates and hashes only
pending inventory rows. Use normal `library-index` again after any file is added, deleted, moved,
or modified in `CleanLibrary`; `--rescan` is an explicit spelling of that default behavior. Fast
resume review items are written to `reports/library_resume_review.csv`.

For safe upgrades, stop active work, back up the database, run `git pull`, `uv sync --frozen`, and doctor again. Recovery and rollback only act on database status or verified files owned by a build; inspect dry-run reports first. Hardlinks require source and destination on the same filesystem and make both names refer to the same data: never use hardlink mode if destination files might be edited. Begin with copy mode and a small source. Follow [the first-run runbook](first-run.md).

To use the web interface on the NAS, run it inside `tmux` and tunnel to it from your computer:

```sh
uv run photo-migrator gui --database /volume1/docker/photo-migrator-data/photo.db --config config.toml --no-browser
# on your computer:
ssh -L 8765:127.0.0.1:8765 youruser@nas
```

Then open the `http://127.0.0.1:8765/#token=…` link the NAS printed. Stopping the server with
Ctrl+C stops the current step at a safe point; starting the same step again resumes it.
