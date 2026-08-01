"""Streaming, resumable SHA-256 hashing and exact-duplicate reports."""

from __future__ import annotations

import csv
import hashlib
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from photo_migrator.database import Database, utc_now

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    id: int
    path: Path
    size_bytes: int
    mtime_ns: int
    sha256: str | None
    status: str | None


@dataclass(frozen=True)
class HashResult:
    candidate: Candidate
    digest: str | None
    size_bytes: int | None
    mtime_ns: int | None
    error: str | None


def _hash(candidate: Candidate) -> HashResult:
    """Read one source in bounded chunks without writing to it."""
    try:
        before = candidate.path.stat()
        digest = hashlib.sha256()
        with candidate.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        after = candidate.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("file changed while it was being hashed")
        return HashResult(candidate, digest.hexdigest(), after.st_size, after.st_mtime_ns, None)
    except (OSError, ValueError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        LOGGER.error("hash error for %s: %s", candidate.path, message)
        return HashResult(candidate, None, None, None, message)


class HashEngine:
    def __init__(self, database: Database, workers: int = 1) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.database = database
        self.workers = workers

    def run(self, limit: int | None = None, source: str | None = None, resume: bool = False) -> int:
        candidates = self._candidates(limit, source, resume)
        run_id = self._start_run(len(candidates))
        completed = failed = 0
        errors: list[str] = []
        started = time.monotonic()
        try:
            work: list[Candidate] = []
            for candidate in candidates:
                try:
                    metadata = candidate.path.stat()
                except OSError:
                    work.append(candidate)
                    continue
                unchanged = (
                    candidate.status == "completed"
                    and candidate.sha256 is not None
                    and metadata.st_size == candidate.size_bytes
                    and metadata.st_mtime_ns == candidate.mtime_ns
                )
                if unchanged:
                    completed += 1
                else:
                    work.append(candidate)
                    self._mark_running(candidate.id)
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures: list[Future[HashResult]] = [executor.submit(_hash, item) for item in work]
                for future in futures:  # submission order keeps database updates deterministic
                    result = future.result()
                    if result.error:
                        failed += 1
                        errors.append(f"{result.candidate.path}: {result.error}")
                    else:
                        completed += 1
                    self._store_result(result)
                    self._progress(len(candidates), completed, failed, started)
        except BaseException as exc:
            self._finish_run(run_id, "failed", completed, failed, str(exc))
            raise
        status = "completed_with_errors" if failed else "completed"
        message = "\n".join(errors) if errors else "all candidates completed"
        self._finish_run(run_id, status, completed, failed, message)
        self.write_reports()
        return 2 if failed else 0

    def _candidates(self, limit: int | None, source: str | None, resume: bool) -> list[Candidate]:
        parameters: list[object] = []
        clauses = ["a.scan_status='available'", "a.size_bytes IS NOT NULL"]
        if source is not None:
            clauses.append("a.source_name=?")
            parameters.append(source)
        if not resume:
            clauses.append("COALESCE(a.hash_status, 'pending') != 'failed'")
        sql = f"""SELECT a.id, a.absolute_path, a.size_bytes, a.mtime_ns, a.sha256,
                   a.hash_status FROM assets a
                   JOIN (SELECT size_bytes FROM assets WHERE scan_status='available'
                         GROUP BY size_bytes HAVING COUNT(*) > 1) groups
                     ON groups.size_bytes=a.size_bytes
                   WHERE {" AND ".join(clauses)} ORDER BY a.absolute_path"""
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = self.database.connection.execute(sql, parameters)
        return [
            Candidate(
                row["id"],
                Path(row["absolute_path"]),
                row["size_bytes"],
                row["mtime_ns"],
                row["sha256"],
                row["hash_status"],
            )
            for row in rows
        ]

    def _start_run(self, count: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO hash_runs(started_at,status,candidate_files) VALUES (?,'running',?)",
                (utc_now(), count),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def _mark_running(self, asset_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE assets SET hash_status='running', hash_started_at=?,
                   hash_completed_at=NULL, hash_error=NULL WHERE id=?""",
                (utc_now(), asset_id),
            )

    def _store_result(self, result: HashResult) -> None:
        with self.database.transaction() as connection:
            if result.error:
                connection.execute(
                    """UPDATE assets SET hash_status='failed',hash_error=?,hash_completed_at=?
                       WHERE id=?""",
                    (result.error, utc_now(), result.candidate.id),
                )
            else:
                connection.execute(
                    """UPDATE assets SET sha256=?,hash_algorithm='sha256',hash_status='completed',
                       hash_completed_at=?,hash_error=NULL,size_bytes=?,mtime_ns=?,updated_at=?
                       WHERE id=?""",
                    (
                        result.digest,
                        utc_now(),
                        result.size_bytes,
                        result.mtime_ns,
                        utc_now(),
                        result.candidate.id,
                    ),
                )

    def _finish_run(
        self, run_id: int, status: str, completed: int, failed: int, message: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE hash_runs SET finished_at=?,status=?,hashed_files=?,failed_files=?,
                   message=?
                   WHERE id=?""",
                (utc_now(), status, completed, failed, message, run_id),
            )

    @staticmethod
    def _progress(total: int, completed: int, failed: int, started: float) -> None:
        elapsed = time.monotonic() - started
        done = completed + failed
        eta = (elapsed / done * (total - done)) if done else 0.0
        print(
            f"Candidates: {total} Completed: {completed} Failed: {failed} "
            f"Remaining: {total - done} Elapsed: {elapsed:.1f}s Estimated completion: {eta:.1f}s"
        )

    def write_reports(self) -> None:
        report_dir = self.database.path.parent / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        rows = self.database.connection.execute(
            """SELECT sha256,COUNT(*) copies,MAX(size_bytes) size_bytes,
               SUM(size_bytes) total_bytes FROM assets
               WHERE scan_status='available' AND hash_status='completed' AND sha256 IS NOT NULL
               GROUP BY sha256 HAVING COUNT(*) > 1 ORDER BY sha256"""
        ).fetchall()
        duplicate_files = self.database.connection.execute(
            """SELECT a.sha256,g.copies,a.size_bytes,g.total_bytes,a.absolute_path
               FROM assets a JOIN (SELECT sha256,COUNT(*) copies,SUM(size_bytes) total_bytes
               FROM assets WHERE scan_status='available' AND hash_status='completed'
               GROUP BY sha256 HAVING COUNT(*) > 1) g ON g.sha256=a.sha256
               WHERE a.scan_status='available' ORDER BY a.sha256,a.absolute_path"""
        ).fetchall()
        candidate_count = self.database.connection.execute(
            """SELECT COUNT(*) FROM assets WHERE scan_status='available' AND size_bytes IN
               (SELECT size_bytes FROM assets WHERE scan_status='available'
                GROUP BY size_bytes HAVING COUNT(*) > 1)"""
        ).fetchone()[0]
        hashed_count = self.database.connection.execute(
            "SELECT COUNT(*) FROM assets WHERE hash_status='completed'"
        ).fetchone()[0]
        failed_count = self.database.connection.execute(
            "SELECT COUNT(*) FROM assets WHERE hash_status='failed'"
        ).fetchone()[0]
        largest = max((row["copies"] for row in rows), default=0)
        (report_dir / "hash_summary.txt").write_text(
            f"candidate count: {candidate_count}\n"
            f"hashed count: {hashed_count}\nfailed count: {failed_count}\n"
            f"duplicate groups: {len(rows)}\nlargest duplicate group: {largest}\n",
            encoding="utf-8",
        )
        with (report_dir / "duplicate_groups.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["sha256", "copies", "size_bytes", "total_bytes", "paths"])
            for row in duplicate_files:
                writer.writerow(row)
