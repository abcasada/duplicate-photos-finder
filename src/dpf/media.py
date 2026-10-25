"""Media file classification by extension."""

from __future__ import annotations

import os

IMAGE_EXTS = frozenset(
    {
        # common
        "jpg", "jpeg", "jpe", "png", "gif", "bmp", "tif", "tiff", "webp",
        # modern
        "heic", "heif", "avif", "jxl",
        # raw
        "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf", "orf",
        "rw2", "pef", "srw", "x3f", "3fr", "iiq", "mrw", "erf", "kdc", "dcr", "mos",
    }
)  # fmt: skip

VIDEO_EXTS = frozenset(
    {
        "mp4", "m4v", "mov", "qt", "avi", "mkv", "webm", "wmv", "asf", "flv",
        "mts", "m2ts", "ts", "mpg", "mpeg", "m2v", "vob", "3gp", "3g2", "mod", "tod",
        "insv", "lrv",
    }
)  # fmt: skip

IMAGE = "image"
VIDEO = "video"
OTHER = "other"

# Folders that never contain user media on NTFS/Windows/macOS-touched drives.
SKIP_DIRS = frozenset(
    {
        "$RECYCLE.BIN",
        "System Volume Information",
        "$Extend",
        ".Trash-1000",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        "@eaDir",  # Synology thumbnails
        ".thumbnails",
    }
)

# OS-generated files that say nothing about a folder's content; never indexed or counted.
JUNK_FILES = frozenset({"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store", "icon\r"})


def is_junk(name: str) -> bool:
    """AppleDouble "._*" files and OS clutter such as Thumbs.db and .DS_Store."""
    return name.startswith("._") or name.casefold() in JUNK_FILES


def classify(name: str) -> str | None:
    """Return IMAGE, VIDEO, or None for a filename, based on its extension."""
    ext = os.path.splitext(name)[1][1:].lower()
    if ext in IMAGE_EXTS:
        return IMAGE
    if ext in VIDEO_EXTS:
        return VIDEO
    return None
