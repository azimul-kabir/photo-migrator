from __future__ import annotations

from pathlib import Path

from photo_migrator.apple_live_photos import AppleIdentifier, normalize_stem
from photo_migrator.motion_photos import MotionDetection, inspect_motion_photo
from photo_migrator.relationships import AssetIdentity, Inspection, build_relationships


def asset(asset_id: int, path: Path, source: str = "phone") -> AssetIdentity:
    kind = "image" if path.suffix.lower() == ".jpg" else "video"
    return AssetIdentity(asset_id, path, path.name, path.suffix.lower(), kind, None, source, 10, 1)


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
