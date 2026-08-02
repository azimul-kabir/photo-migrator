"""Read-only destination verification with serialized SQLite updates."""

from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from photo_migrator.build_models import VerificationResult
from photo_migrator.database import Database, utc_now
from photo_migrator.filesystem_safety import safe_destination, sha256_file


class Verifier:
    def __init__(self, database: Database, workers: int = 1) -> None:
        if workers < 1:
            raise ValueError("workers must be at least one")
        self.database, self.workers = database, workers

    def run(
        self, build_run_id: int, limit: int | None = None, repair_metadata_only: bool = False
    ) -> list[VerificationResult]:
        del repair_metadata_only  # Verification state is always refreshed; media is never repaired.
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        run = self.database.connection.execute(
            "SELECT * FROM build_runs WHERE id=?", (build_run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"build run {build_run_id} does not exist")
        query = (
            "SELECT * FROM build_items WHERE build_run_id=? AND destination_verified=1 ORDER BY id"
        )
        parameters: list[object] = [build_run_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = list(self.database.connection.execute(query, parameters))
        root = Path(run["destination_root"])
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            results = list(executor.map(lambda row: self._verify(row, root), rows))
        with self.database.transaction() as connection:
            for result in results:
                good = result.status == "completed"
                connection.execute(
                    """UPDATE build_items SET status=?,destination_verified=?,actual_sha256=?,
                    actual_size_bytes=?,finished_at=?,error=? WHERE id=?""",
                    (
                        result.status,
                        int(good),
                        result.actual_sha256,
                        result.actual_size_bytes,
                        utc_now(),
                        result.error,
                        result.build_item_id,
                    ),
                )
            failed = sum(result.status == "verification_failed" for result in results)
            connection.execute(
                "UPDATE build_runs SET verified_items=?,verification_failed_items=? WHERE id=?",
                (len(results) - failed, failed, build_run_id),
            )
        from photo_migrator.builder import Builder

        Builder(self.database).reports(build_run_id)
        return results

    @staticmethod
    def _verify(row: object, root: Path) -> VerificationResult:
        build_item_id = int(row["id"])  # type: ignore[index]
        destination = Path(row["destination_absolute_path"])  # type: ignore[index]
        expected_hash = str(row["expected_sha256"])  # type: ignore[index]
        expected_size = int(row["expected_size_bytes"])  # type: ignore[index]
        try:
            checked = safe_destination(root, Path(row["destination_relative_path"]))  # type: ignore[index]
            if checked != destination:
                raise ValueError("recorded destination path mismatch")
            info = destination.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("destination path type is not a regular non-symlink file")
            digest, size = sha256_file(destination)
            if size != expected_size:
                raise ValueError(
                    f"destination size changed: expected {expected_size}, observed {size}"
                )
            if digest != expected_hash:
                raise ValueError(
                    f"destination SHA-256 mismatch: expected {expected_hash}, observed {digest}"
                )
            return VerificationResult(
                build_item_id, destination, "completed", expected_hash, digest, expected_size, size
            )
        except (OSError, ValueError) as exc:
            return VerificationResult(
                build_item_id,
                destination,
                "verification_failed",
                expected_hash,
                None,
                expected_size,
                None,
                str(exc),
            )
