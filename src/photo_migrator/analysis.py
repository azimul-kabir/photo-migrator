"""Concurrent, resumable metadata analysis with serialized SQLite writes."""

from __future__ import annotations

import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from photo_migrator.atomic_io import atomic_write_csv, atomic_write_text
from photo_migrator.database import Database, utc_now
from photo_migrator.image_metadata import ImageAnalyzer
from photo_migrator.metadata import AnalyzerResult
from photo_migrator.video_metadata import VideoAnalyzer


@dataclass(frozen=True)
class AnalysisCandidate:
    id: int
    path: Path
    media_type: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class AssetAnalysis:
    candidate: AnalysisCandidate
    result: AnalyzerResult
    size_bytes: int | None
    mtime_ns: int | None


def _analyze(candidate: AnalysisCandidate, ffprobe: str) -> AssetAnalysis:
    """Analyze and verify one source without writing to it."""
    try:
        before = candidate.path.stat()
    except OSError as exc:
        return AssetAnalysis(
            candidate, AnalyzerResult("failed", error=f"stat failed: {exc}"), None, None
        )
    analyzer = VideoAnalyzer(ffprobe) if candidate.media_type == "video" else ImageAnalyzer()
    result = analyzer.analyze(candidate.path)
    try:
        after = candidate.path.stat()
    except OSError as exc:
        return AssetAnalysis(
            candidate, AnalyzerResult("failed", error=f"stat failed: {exc}"), None, None
        )
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        result = AnalyzerResult("failed", error="file changed during analysis")
    return AssetAnalysis(candidate, result, after.st_size, after.st_mtime_ns)


class AnalysisEngine:
    def __init__(self, database: Database, workers: int = 1, ffprobe: str = "ffprobe") -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.database = database
        self.workers = workers
        self.ffprobe = ffprobe

    def run(
        self,
        limit: int | None = None,
        source: str | None = None,
        resume: bool = False,
        retry_failed: bool = False,
    ) -> int:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than 0")
        candidates = self._candidates(limit, source, resume, retry_failed)
        run_id = self._start_run(len(candidates))
        analyzed = failed = unsupported = 0
        reused = self._reused_count(source)
        errors: list[str] = []
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures: list[tuple[AnalysisCandidate, Future[AssetAnalysis]]] = []
                for candidate in candidates:
                    self._mark_running(candidate.id)
                    futures.append((candidate, executor.submit(_analyze, candidate, self.ffprobe)))
                # Consume in absolute-path submission order for deterministic persistence.
                for candidate, future in futures:
                    try:
                        item = future.result()
                    except Exception as exc:
                        item = AssetAnalysis(
                            candidate,
                            AnalyzerResult(
                                "failed",
                                error=f"analysis worker error: {type(exc).__name__}: {exc}",
                            ),
                            None,
                            None,
                        )
                    self._store(item)
                    if item.result.status == "completed":
                        analyzed += 1
                    elif item.result.status == "unsupported":
                        unsupported += 1
                        errors.append(f"{item.candidate.path}: {item.result.error}")
                    else:
                        failed += 1
                        errors.append(f"{item.candidate.path}: {item.result.error}")
        except BaseException as exc:
            self._finish(run_id, "failed", analyzed, failed, unsupported, reused, str(exc))
            raise
        status = "completed_with_errors" if failed or unsupported else "completed"
        self._finish(
            run_id,
            status,
            analyzed,
            failed,
            unsupported,
            reused,
            "\n".join(errors) if errors else "analysis completed",
        )
        self.write_reports()
        return 2 if failed or unsupported else 0

    def _candidates(
        self, limit: int | None, source: str | None, resume: bool, retry_failed: bool
    ) -> list[AnalysisCandidate]:
        # A running marker is never a completed result. Reclaim it on every invocation so a
        # prior process crash cannot strand the asset indefinitely. ``resume`` remains accepted
        # for CLI compatibility and documents the caller's intent.
        statuses = ["pending", "running"]
        if retry_failed:
            statuses.extend(("failed", "unsupported"))
        placeholders = ",".join("?" for _ in statuses)
        clauses = [
            "scan_status='available'",
            "size_bytes IS NOT NULL",
            "mtime_ns IS NOT NULL",
            f"(COALESCE(analysis_status,'pending') IN ({placeholders}) OR "
            "(analysis_status='completed' AND (analyzed_size_bytes != size_bytes OR "
            "analyzed_mtime_ns != mtime_ns OR analyzed_size_bytes IS NULL OR "
            "analyzed_mtime_ns IS NULL)))",
        ]
        parameters: list[object] = list(statuses)
        if source:
            clauses.append("source_name=?")
            parameters.append(source)
        sql = (
            "SELECT id,absolute_path,media_type,size_bytes,mtime_ns FROM assets WHERE "
            + " AND ".join(clauses)
            + " ORDER BY absolute_path"
        )
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        # fetchmany avoids materializing the whole asset table; only candidates live here.
        cursor = self.database.connection.execute(sql, parameters)
        result: list[AnalysisCandidate] = []
        while rows := cursor.fetchmany(256):
            result.extend(
                AnalysisCandidate(
                    row["id"],
                    Path(row["absolute_path"]),
                    row["media_type"],
                    row["size_bytes"],
                    row["mtime_ns"],
                )
                for row in rows
            )
        return result

    def _reused_count(self, source: str | None) -> int:
        clauses = [
            "scan_status='available'",
            "analysis_status='completed'",
            "analyzed_size_bytes=size_bytes",
            "analyzed_mtime_ns=mtime_ns",
        ]
        parameters: list[object] = []
        if source:
            clauses.append("source_name=?")
            parameters.append(source)
        row = self.database.connection.execute(
            "SELECT COUNT(*) FROM assets WHERE " + " AND ".join(clauses), parameters
        ).fetchone()
        return int(row[0])

    def _start_run(self, candidates: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO analysis_runs(started_at,status,candidate_files)
                   VALUES (?,'running',?)""",
                (utc_now(), candidates),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def _mark_running(self, asset_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE assets SET analysis_status='running',analysis_started_at=?,
                   analysis_completed_at=NULL,analysis_error=NULL WHERE id=?""",
                (utc_now(), asset_id),
            )

    def _store(self, item: AssetAnalysis) -> None:
        result = item.result
        with self.database.transaction() as connection:
            if result.status != "completed" or result.metadata is None:
                connection.execute(
                    """UPDATE assets SET analysis_status=?,analysis_error=?,
                       analysis_completed_at=? WHERE id=?""",
                    (result.status, result.error, utc_now(), item.candidate.id),
                )
                return
            metadata = result.metadata
            connection.execute(
                """UPDATE assets SET analysis_status='completed',analysis_completed_at=?,
                   analysis_error=NULL,analyzed_size_bytes=?,analyzed_mtime_ns=?,captured_at=?,
                   captured_at_source=?,width=?,height=?,orientation=?,camera_make=?,camera_model=?,
                   lens_model=?,gps_latitude=?,gps_longitude=?,gps_altitude=?,duration_seconds=?,
                   video_codec=?,audio_codec=?,container_format=?,frame_rate=?,bitrate=?,color_space=?
                   WHERE id=?""",
                (
                    utc_now(),
                    item.size_bytes,
                    item.mtime_ns,
                    metadata.captured_at,
                    metadata.captured_at_source,
                    metadata.width,
                    metadata.height,
                    metadata.orientation,
                    metadata.camera_make,
                    metadata.camera_model,
                    metadata.lens_model,
                    metadata.latitude,
                    metadata.longitude,
                    metadata.altitude,
                    metadata.duration_seconds,
                    metadata.video_codec,
                    metadata.audio_codec,
                    metadata.container_format,
                    metadata.frame_rate,
                    metadata.bitrate,
                    metadata.color_space,
                    item.candidate.id,
                ),
            )

    def _finish(
        self,
        run_id: int,
        status: str,
        analyzed: int,
        failed: int,
        unsupported: int,
        reused: int,
        message: str,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE analysis_runs SET finished_at=?,status=?,analyzed_files=?,failed_files=?,
                   unsupported_files=?,reused_files=?,message=? WHERE id=?""",
                (utc_now(), status, analyzed, failed, unsupported, reused, message, run_id),
            )

    def write_reports(self) -> None:
        report_dir = self.database.path.parent / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        connection = self.database.connection
        latest = connection.execute(
            "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        counts = connection.execute(
            """SELECT
               SUM(CASE WHEN analysis_status='completed' AND media_type='image'
                   THEN 1 ELSE 0 END) images,
               SUM(CASE WHEN analysis_status='completed' AND media_type='video'
                   THEN 1 ELSE 0 END) videos,
               SUM(CASE WHEN captured_at IS NOT NULL THEN 1 ELSE 0 END) captured,
               SUM(CASE WHEN analysis_status='completed' AND captured_at IS NULL
                   THEN 1 ELSE 0 END) no_capture,
               SUM(CASE WHEN gps_latitude IS NOT NULL AND gps_longitude IS NOT NULL
                   THEN 1 ELSE 0 END) gps,
               SUM(CASE WHEN camera_make IS NOT NULL OR camera_model IS NOT NULL
                   THEN 1 ELSE 0 END) camera
               FROM assets WHERE scan_status='available'"""
        ).fetchone()
        assert latest is not None and counts is not None
        summary = (
            f"candidate count: {latest['candidate_files']}\n"
            f"newly analyzed count: {latest['analyzed_files']}\n"
            f"reused count: {latest['reused_files']}\n"
            f"failed count: {latest['failed_files']}\n"
            f"unsupported count: {latest['unsupported_files']}\n"
            f"images analyzed: {counts['images'] or 0}\n"
            f"videos analyzed: {counts['videos'] or 0}\n"
            f"assets with captured_at: {counts['captured'] or 0}\n"
            f"assets without captured_at: {counts['no_capture'] or 0}\n"
            f"assets with GPS: {counts['gps'] or 0}\n"
            f"assets with camera make/model: {counts['camera'] or 0}\n"
            f"ffprobe path used: {self.ffprobe}\nlatest run status: {latest['status']}\n"
        )
        atomic_write_text(report_dir / "metadata_summary.txt", summary)
        camera_rows = connection.execute(
            """SELECT COALESCE(camera_make,'') camera_make,COALESCE(camera_model,'') camera_model,
               COUNT(*) asset_count FROM assets WHERE scan_status='available'
               AND (camera_make IS NOT NULL OR camera_model IS NOT NULL)
               GROUP BY camera_make,camera_model ORDER BY camera_make,camera_model"""
        ).fetchall()
        atomic_write_csv(
            report_dir / "camera_statistics.csv",
            camera_rows,
            ["camera_make", "camera_model", "asset_count"],
        )
        rows = connection.execute(
            """SELECT absolute_path,source_name,media_type,analysis_status,analysis_error,
               captured_at,
               width,height,camera_make,camera_model,gps_latitude,gps_longitude,duration_seconds,
               video_codec FROM assets WHERE scan_status='available' ORDER BY absolute_path"""
        )
        missing_rows = []
        for row in rows:
            missing = self._missing_fields(row)
            if missing or row["analysis_error"]:
                missing_rows.append(
                    [
                        row["absolute_path"],
                        row["source_name"],
                        row["media_type"],
                        row["analysis_status"],
                        ";".join(missing),
                        row["analysis_error"] or "",
                    ]
                )
        atomic_write_csv(
            report_dir / "missing_metadata.csv",
            missing_rows,
            [
                "absolute_path",
                "source_name",
                "media_type",
                "analysis_status",
                "missing_fields",
                "analysis_error",
            ],
        )

    @staticmethod
    def _missing_fields(values: sqlite3.Row) -> list[str]:
        missing: list[str] = []
        if values["captured_at"] is None:
            missing.append("captured_at")
        if values["width"] is None or values["height"] is None:
            missing.append("dimensions")
        if values["media_type"] == "image":
            if values["camera_make"] is None and values["camera_model"] is None:
                missing.append("camera")
            if values["gps_latitude"] is None or values["gps_longitude"] is None:
                missing.append("gps")
        else:
            if values["duration_seconds"] is None:
                missing.append("duration")
            if values["video_codec"] is None:
                missing.append("video_codec")
        return missing
