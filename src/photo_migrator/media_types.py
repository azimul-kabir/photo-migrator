"""Single source of truth for classifying inventory files as images or videos."""

from __future__ import annotations

# Containers that hold video. Anything not listed is inventoried as an image, so this list errs
# wide: an unlisted video would be offered to image-only steps such as capture-date recovery.
VIDEO_EXTENSIONS = frozenset(
    {
        ".3g2",
        ".3gp",
        ".avi",
        ".flv",
        ".m2t",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".mxf",
        ".ogv",
        ".qt",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)


def media_type_for(extension: str) -> str:
    """Return 'video' or 'image' for a file extension such as '.MOV'."""
    return "video" if extension.lower() in VIDEO_EXTENSIONS else "image"
