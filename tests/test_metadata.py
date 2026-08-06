import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from photo_migrator.analysis import AnalysisEngine
from photo_migrator.database import Database, utc_now
from photo_migrator.image_metadata import ImageAnalyzer, gps_coordinate, normalize_timestamp
from photo_migrator.metadata import AnalyzerResult, MediaMetadata
from photo_migrator.video_metadata import VideoAnalyzer, parse_ffprobe, parse_frame_rate


def _asset(database: Database, path: Path, media_type: str = "video") -> None:
    stat = path.stat()
    now = utc_now()
    database.connection.execute(
        """INSERT INTO assets(source_name,source_priority,absolute_path,relative_path,filename,
           extension,size_bytes,mtime_ns,media_type,scan_status,first_seen_at,last_seen_at,
           created_at,updated_at) VALUES ('source',1,?,?,?,?,?,? ,?,'available',?,?,?,?)""",
        (
            str(path),
            path.name,
            path.name,
            path.suffix,
            stat.st_size,
            stat.st_mtime_ns,
            media_type,
            now,
            now,
            now,
            now,
        ),
    )
    database.connection.commit()


def test_timestamp_and_gps_normalization() -> None:
    assert normalize_timestamp("2024:01:02 03:04:05") == "2024-01-02T03:04:05"
    assert "+" not in normalize_timestamp("2024:01:02 03:04:05")
    assert gps_coordinate(((40, 1), (30, 1), (0, 1)), "N", True) == 40.5
    assert gps_coordinate(((73, 1), (59, 1), (0, 1)), "W", False) < 0
    assert gps_coordinate(((40, 1), (30, 1)), "N", True) is None


def test_ffprobe_parsing_and_precedence() -> None:
    metadata = parse_ffprobe(
        {
            "format": {
                "duration": "2.5",
                "format_name": "mov,mp4",
                "bit_rate": "1000",
                "tags": {"creation_time": "2020-01-02T03:04:05Z"},
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 10,
                    "height": 20,
                    "avg_frame_rate": "30000/1001",
                    "color_space": "bt709",
                    "tags": {"creation_time": "2021-01-02T03:04:05Z"},
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )
    assert metadata.captured_at == "2020-01-02T03:04:05+00:00"
    assert metadata.captured_at_source == "video_format_creation_time"
    assert metadata.audio_codec == "aac"
    assert metadata.frame_rate == pytest.approx(29.97003)
    assert parse_frame_rate("0/0") is None


def test_video_analyzer_missing_timeout_and_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "tiny.mp4"
    path.write_bytes(b"synthetic")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert VideoAnalyzer().analyze(path).error == "ffprobe not found: ffprobe"
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
        assert "timed out" in (VideoAnalyzer().analyze(path).error or "")
    process = Mock(returncode=0, stdout="not-json", stderr="")
    with patch("subprocess.run", return_value=process):
        assert "malformed ffprobe JSON" in (VideoAnalyzer().analyze(path).error or "")


def test_decompression_bomb_is_a_structured_failure_without_disabling_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oversized.jpg"
    path.write_bytes(b"synthetic")
    original_limit = Image.MAX_IMAGE_PIXELS
    error = Image.DecompressionBombError(
        "Image size (199756800 pixels) exceeds limit of 178956970 pixels."
    )

    with patch("photo_migrator.image_metadata.Image.open", side_effect=error):
        result = ImageAnalyzer().analyze(path)

    assert result.status == "failed"
    assert result.metadata is None
    assert result.error is not None
    assert "DecompressionBombError" in result.error
    assert "199756800 pixels" in result.error
    assert f"MAX_IMAGE_PIXELS={original_limit}" in result.error
    assert original_limit == Image.MAX_IMAGE_PIXELS


def test_worker_exception_isolated_and_other_assets_continue(tmp_path: Path) -> None:
    broken = tmp_path / "a.jpg"
    healthy = tmp_path / "b.jpg"
    broken.write_bytes(b"broken")
    healthy.write_bytes(b"healthy")
    database_path = tmp_path / "inventory.db"

    with Database(database_path) as database:
        database.initialize()
        _asset(database, broken, "image")
        _asset(database, healthy, "image")
        successful = AnalyzerResult("completed", MediaMetadata(width=2, height=3))

        def analyze(path: Path) -> AnalyzerResult:
            if path == broken:
                raise RuntimeError("worker exploded")
            return successful

        with patch.object(ImageAnalyzer, "analyze", side_effect=analyze):
            assert AnalysisEngine(database, workers=2).run() == 2

        assets = database.connection.execute(
            "SELECT filename,analysis_status,analysis_error FROM assets ORDER BY filename"
        ).fetchall()
        assert tuple(assets[0]) == (
            "a.jpg",
            "failed",
            "analysis worker error: RuntimeError: worker exploded",
        )
        assert tuple(assets[1]) == ("b.jpg", "completed", None)
        run = database.connection.execute(
            "SELECT status,analyzed_files,failed_files FROM analysis_runs"
        ).fetchone()
        assert tuple(run) == ("completed_with_errors", 1, 1)


def test_stale_running_is_requeued_while_completed_asset_is_reused(tmp_path: Path) -> None:
    stale = tmp_path / "stale.jpg"
    completed = tmp_path / "completed.jpg"
    stale.write_bytes(b"stale")
    completed.write_bytes(b"completed")
    database_path = tmp_path / "inventory.db"

    with Database(database_path) as database:
        database.initialize()
        _asset(database, stale, "image")
        _asset(database, completed, "image")
        completed_stat = completed.stat()
        database.connection.execute(
            "UPDATE assets SET analysis_status='running' WHERE filename='stale.jpg'"
        )
        database.connection.execute(
            """UPDATE assets SET analysis_status='completed',analyzed_size_bytes=?,
               analyzed_mtime_ns=? WHERE filename='completed.jpg'""",
            (completed_stat.st_size, completed_stat.st_mtime_ns),
        )
        database.connection.commit()
        successful = AnalyzerResult("completed", MediaMetadata(width=4, height=5))
        with patch.object(ImageAnalyzer, "analyze", return_value=successful) as analyze:
            assert AnalysisEngine(database).run() == 0

        assert analyze.call_count == 1
        assert analyze.call_args.args == (stale,)
        statuses = database.connection.execute(
            "SELECT filename,analysis_status FROM assets ORDER BY filename"
        ).fetchall()
        assert [tuple(row) for row in statuses] == [
            ("completed.jpg", "completed"),
            ("stale.jpg", "completed"),
        ]
        run = database.connection.execute(
            "SELECT analyzed_files,reused_files,status FROM analysis_runs"
        ).fetchone()
        assert tuple(run) == (1, 1, "completed")


def test_incremental_analysis_and_reports(tmp_path: Path) -> None:
    path = tmp_path / "tiny.mp4"
    path.write_bytes(b"synthetic")
    database_path = tmp_path / "inventory.db"
    probe = {
        "format": {"duration": "1", "format_name": "mp4"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 2, "height": 3}],
    }
    process = Mock(returncode=0, stdout=json.dumps(probe), stderr="")
    with Database(database_path) as database:
        database.initialize()
        _asset(database, path)
        with patch("subprocess.run", return_value=process) as run:
            assert AnalysisEngine(database, workers=2).run() == 0
            assert AnalysisEngine(database).run() == 0
        assert run.call_count == 1
        runs = database.connection.execute(
            "SELECT status,analyzed_files,reused_files FROM analysis_runs ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in runs] == [("completed", 1, 0), ("completed", 0, 1)]
    assert (tmp_path / "reports" / "metadata_summary.txt").exists()
    assert (
        (tmp_path / "reports" / "camera_statistics.csv")
        .read_text()
        .startswith("camera_make,camera_model,asset_count")
    )


def test_schema_upgrade_from_version_two(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (2, 'old');
        CREATE TABLE assets(id INTEGER PRIMARY KEY, source_name TEXT NOT NULL,
        source_priority INTEGER NOT NULL, absolute_path TEXT NOT NULL UNIQUE,
        relative_path TEXT NOT NULL, filename TEXT NOT NULL, extension TEXT NOT NULL,
        size_bytes INTEGER,mtime_ns INTEGER,device_id INTEGER,inode INTEGER,
        media_type TEXT NOT NULL,scan_status TEXT NOT NULL,error_message TEXT,
        first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,missing_since TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        """
    )
    connection.close()
    with Database(path) as database:
        database.initialize()
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(assets)")}
        assert {"analysis_status", "captured_at", "video_codec"} <= columns
        assert (
            database.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            == 7
        )
