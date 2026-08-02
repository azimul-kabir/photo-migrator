from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from photo_migrator.apple_live_photos import AppleIdentifier, inspect_image, normalize_stem
from photo_migrator.database import Database, utc_now
from photo_migrator.motion_photos import MotionDetection, inspect_motion_photo
from photo_migrator.relationships import (
    AssetIdentity,
    Inspection,
    RelationshipEngine,
    RelationshipResult,
    build_relationships,
)


def asset(asset_id: int, path: Path, source: str = "phone") -> AssetIdentity:
    kind = "image" if path.suffix.lower() == ".jpg" else "video"
    return AssetIdentity(asset_id, path, path.name, path.suffix.lower(), kind, None, source, 10, 1)


def with_time(identity: AssetIdentity, captured_at: str) -> AssetIdentity:
    return AssetIdentity(
        identity.asset_id,
        identity.absolute_path,
        identity.filename,
        identity.extension,
        identity.media_type,
        captured_at,
        identity.source_name,
        identity.size_bytes,
        identity.mtime_ns,
    )


def test_one_sided_apple_identifier_pairs_same_directory_heic(tmp_path: Path) -> None:
    image = Inspection(asset(1, tmp_path / "IMG_1327.HEIC"), AppleIdentifier(), MotionDetection())
    video = Inspection(
        asset(2, tmp_path / "IMG_1327.MOV"),
        AppleIdentifier("apple-id", "video:test"),
        MotionDetection(),
    )
    result = build_relationships([image, video])
    assert [(row.status, row.relationship_type, row.confidence) for row in result] == [
        ("active", "apple_live_photo", 0.9)
    ]
    assert "fallback=one_sided_apple_identifier" in result[0].evidence
    assert "video_identifier=apple-id" in result[0].evidence
    assert "same_source=true;same_directory=true" in result[0].evidence
    assert not any(row.relationship_type == "orphan_motion_video" for row in result)


def test_one_sided_identifier_never_crosses_directories_or_sources(tmp_path: Path) -> None:
    video = Inspection(
        asset(3, tmp_path / "2024" / "IMG_1.mov"),
        AppleIdentifier("id", "video:test"),
        MotionDetection(),
    )
    images = [
        Inspection(
            asset(1, tmp_path / "2023" / "IMG_1.heic"), AppleIdentifier(), MotionDetection()
        ),
        Inspection(
            asset(2, tmp_path / "2024" / "IMG_1.jpg", "other"), AppleIdentifier(), MotionDetection()
        ),
    ]
    result = build_relationships([*images, video])
    assert [row.relationship_type for row in result] == ["orphan_motion_video"]


def test_one_sided_identifier_rejects_timestamp_mismatch(tmp_path: Path) -> None:
    image = Inspection(
        with_time(asset(1, tmp_path / "IMG_1.heic"), "2024-01-01T00:00:00"),
        AppleIdentifier(),
        MotionDetection(),
    )
    video = Inspection(
        with_time(asset(2, tmp_path / "IMG_1.mov"), "2024-01-01T00:00:04"),
        AppleIdentifier("id", "video:test"),
        MotionDetection(),
    )
    assert [row.relationship_type for row in build_relationships([image, video])] == [
        "orphan_motion_video"
    ]


def test_one_sided_identifier_multiple_images_is_ambiguous(tmp_path: Path) -> None:
    items = [
        Inspection(asset(1, tmp_path / "IMG_1.heic"), AppleIdentifier(), MotionDetection()),
        Inspection(asset(2, tmp_path / "IMG_1.jpg"), AppleIdentifier(), MotionDetection()),
        Inspection(
            asset(3, tmp_path / "IMG_1.mov"), AppleIdentifier("id", "video:test"), MotionDetection()
        ),
    ]
    result = build_relationships(items)
    assert len(result) == 1
    assert (result[0].status, result[0].relationship_type) == ("ambiguous", "apple_live_photo")


def test_heif_identifier_uses_metadata_blocks_without_changing_source(tmp_path: Path) -> None:
    path = tmp_path / "IMG_1.heic"
    original = b"synthetic unchanged heif"
    path.write_bytes(original)
    identifier = b"com.apple.quicktime.content.identifier 6c576f1e-5371-471a-858e-23405c9d8681"
    fake = type("FakeHeif", (), {"info": {"metadata": [{"type": "XMP", "data": identifier}]}})()
    with patch("pillow_heif.open_heif", return_value=fake) as opened:
        result = inspect_image(path)
    assert result.value == "6c576f1e-5371-471a-858e-23405c9d8681"
    assert result.source == "image:heif_metadata:xmp:com.apple.quicktime.content.identifier"
    opened.assert_called_once()
    assert path.read_bytes() == original


def test_relationship_candidate_build_scales_to_thousands(tmp_path: Path) -> None:
    items = []
    for index in range(2_500):
        directory = tmp_path / str(index)
        items.extend(
            (
                Inspection(
                    asset(index * 2 + 1, directory / "IMG_1.heic"),
                    AppleIdentifier(),
                    MotionDetection(),
                ),
                Inspection(
                    asset(index * 2 + 2, directory / "IMG_1.mov"),
                    AppleIdentifier(f"id-{index}", "video:test"),
                    MotionDetection(),
                ),
            )
        )
    result = build_relationships(items)
    assert len(result) == 2_500
    assert all(row.relationship_type == "apple_live_photo" for row in result)


def test_identifier_pair_precedes_filename_fallback(tmp_path: Path) -> None:
    image = Inspection(
        asset(1, tmp_path / "IMG_1.jpg"),
        AppleIdentifier("shared", "image:test"),
        MotionDetection(),
    )
    correct = Inspection(
        asset(2, tmp_path / "OTHER.mov"),
        AppleIdentifier("shared", "video:test"),
        MotionDetection(),
    )
    same_name = Inspection(asset(3, tmp_path / "IMG_1.mov"), AppleIdentifier(), MotionDetection())
    results = build_relationships([image, correct, same_name])
    assert [(row.relationship_type, row.secondary_asset_id) for row in results] == [
        ("apple_live_photo", 2)
    ]


def test_edited_apple_stem_and_ambiguous_fallback(tmp_path: Path) -> None:
    assert normalize_stem("IMG_E1234") == "img_1234"
    items = [
        Inspection(asset(1, tmp_path / "IMG_E1234.jpg"), AppleIdentifier(), MotionDetection()),
        Inspection(asset(2, tmp_path / "IMG_1234.mov"), AppleIdentifier(), MotionDetection()),
        Inspection(asset(3, tmp_path / "IMG_1234.mp4"), AppleIdentifier(), MotionDetection()),
    ]
    result = build_relationships(items)
    assert len(result) == 1
    assert result[0].status == "ambiguous"


def test_google_offset_validation_and_ordinary_trailing_data(tmp_path: Path) -> None:
    valid = tmp_path / "motion.jpg"
    valid.write_bytes(b'<x GCamera:MotionPhoto="1" GCamera:MicroVideoOffset="5"/>' + b"video")
    detection = inspect_motion_photo(valid, valid.stat().st_size)
    assert (detection.kind, detection.status, detection.offset) == ("google", "active", 5)

    invalid = tmp_path / "invalid.jpg"
    invalid.write_bytes(b'<x GCamera:MotionPhoto="1" GCamera:MicroVideoOffset="999"/>')
    assert inspect_motion_photo(invalid, invalid.stat().st_size).status == "invalid"

    ordinary = tmp_path / "ordinary.jpg"
    ordinary.write_bytes(b"jpeg bytes with harmless trailing bytes")
    assert inspect_motion_photo(ordinary, ordinary.stat().st_size).kind is None


def test_samsung_marker_is_self_contained(tmp_path: Path) -> None:
    path = tmp_path / "samsung.jpg"
    path.write_bytes(b"jpeg MotionPhoto_Data" + b"mp4")
    detection = inspect_motion_photo(path, path.stat().st_size)
    assert (detection.kind, detection.status) == ("samsung", "active")


def _insert_assets(database: Database, paths: list[Path]) -> None:
    now = utc_now()
    database.connection.executemany(
        """INSERT INTO assets(source_name,source_priority,absolute_path,relative_path,filename,
        extension,size_bytes,mtime_ns,media_type,scan_status,first_seen_at,last_seen_at,
        created_at,updated_at) VALUES ('source',1,?,?,?,?,1,1,'image','available',?,?,?,?)""",
        ((str(path), path.name, path.name, ".jpg", now, now, now, now) for path in paths),
    )
    database.connection.commit()


def test_large_relationship_store_preserves_retained_and_deletes_stale(tmp_path: Path) -> None:
    paths = [tmp_path / f"asset-{index}.jpg" for index in range(1_205)]
    with Database(tmp_path / "inventory.db") as database:
        database.initialize()
        _insert_assets(database, paths)
        rows = database.connection.execute(
            "SELECT id,absolute_path FROM assets ORDER BY id"
        ).fetchall()
        inspections = [
            Inspection(
                asset(row["id"], Path(row["absolute_path"])), AppleIdentifier(), MotionDetection()
            )
            for row in rows
        ]
        now = utc_now()
        database.connection.executemany(
            """INSERT INTO asset_relationships(relationship_type,primary_asset_id,
            secondary_asset_id,confidence,evidence,status,created_at,updated_at)
            VALUES ('orphan_motion_image',?,NULL,0.9,?,'orphan',?,?)""",
            ((row["id"], f"keep-{row['id']}", now, now) for row in rows[:-1]),
        )
        stale_id = database.connection.execute(
            """INSERT INTO asset_relationships(relationship_type,primary_asset_id,
            secondary_asset_id,confidence,evidence,status,created_at,updated_at)
            VALUES ('orphan_motion_image',?,NULL,0.9,'stale','orphan',?,?) RETURNING id""",
            (rows[-1]["id"], now, now),
        ).fetchone()[0]
        database.connection.commit()
        results = [
            RelationshipResult(
                "orphan", "orphan_motion_image", row["id"], None, 0.9, f"keep-{row['id']}"
            )
            for row in rows[:-1]
        ]
        results.append(
            RelationshipResult("orphan", "orphan_motion_image", rows[-1]["id"], None, 0.9, "new")
        )

        assert RelationshipEngine(database)._store(inspections, results) == (1, 1_204)
        stored = database.connection.execute(
            "SELECT id,evidence FROM asset_relationships ORDER BY id"
        ).fetchall()
        assert stale_id not in {row["id"] for row in stored}
        assert "new" in {row["evidence"] for row in stored}
        assert not database.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'relationship_%_ids'"
        ).fetchall()
        assert all(not path.exists() for path in paths)


def test_relationship_store_rolls_back_and_cleans_temp_tables(tmp_path: Path) -> None:
    path = tmp_path / "source.jpg"
    with Database(tmp_path / "inventory.db") as database:
        database.initialize()
        _insert_assets(database, [path])
        row = database.connection.execute("SELECT id,absolute_path FROM assets").fetchone()
        inspection = Inspection(
            asset(row["id"], Path(row["absolute_path"])), AppleIdentifier(), MotionDetection()
        )
        database.connection.execute(
            """CREATE TRIGGER force_relationship_failure BEFORE UPDATE ON assets
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
        )
        with pytest.raises(Exception, match="forced failure"):
            RelationshipEngine(database)._store(
                [inspection],
                [RelationshipResult("orphan", "orphan_motion_image", row["id"], None, 0.9, "new")],
            )
        assert (
            database.connection.execute("SELECT COUNT(*) FROM asset_relationships").fetchone()[0]
            == 0
        )
        assert not database.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'relationship_%_ids'"
        ).fetchall()
        assert not path.exists()


def test_unexpected_worker_failure_is_persisted_per_asset(tmp_path: Path) -> None:
    paths = [tmp_path / "bad.jpg", tmp_path / "good.jpg"]
    for path in paths:
        path.write_bytes(b"synthetic")
    before = [path.read_bytes() for path in paths]
    with Database(tmp_path / "inventory.db") as database:
        database.initialize()
        _insert_assets(database, paths)

        def inspect_one(identity: AssetIdentity, _ffprobe: str) -> Inspection:
            if identity.filename == "bad.jpg":
                raise RuntimeError("detector crashed")
            return Inspection(identity, AppleIdentifier(), MotionDetection())

        with patch("photo_migrator.relationships._inspect", side_effect=inspect_one):
            assert RelationshipEngine(database, workers=2).run() == 2
        statuses = database.connection.execute(
            "SELECT filename,relationship_status,relationship_error FROM assets ORDER BY filename"
        ).fetchall()
        assert statuses[0][1] == "failed"
        assert "detector crashed" in statuses[0][2]
        assert statuses[1][1] == "completed"
        assert (
            database.connection.execute(
                "SELECT status FROM relationship_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            == "completed_with_errors"
        )
    assert [path.read_bytes() for path in paths] == before
