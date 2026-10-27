"""Core data types shared across dpf modules."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One media file in the inventory. `dir` is absolute and has no trailing slash."""

    dir: str
    name: str
    size: int
    mtime: float
    kind: str  # media.IMAGE, media.VIDEO or media.OTHER

    @property
    def path(self) -> str:
        return os.path.join(self.dir, self.name)


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Files judged to be copies of each other. `basis` says how, e.g. "name+size+mtime"."""

    files: tuple[FileRecord, ...]
    basis: str

    @property
    def size(self) -> int:
        return max(f.size for f in self.files)

    @property
    def same_names(self) -> bool:
        """True if every file has exactly the same name (case included)."""
        return len({f.name for f in self.files}) == 1

    @property
    def redundant_bytes(self) -> int:
        """Bytes that could be freed by keeping only one copy."""
        return sum(f.size for f in self.files) - self.size


SPACE_STATVFS = "statvfs"
SPACE_MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class DriveSpace:
    """Used and total bytes of the drive holding a root.

    `source` is SPACE_STATVFS (measured with os.statvfs) or SPACE_MANUAL (entered by
    hand, the path untouched). `is_mount` is False when the root is a folder inside a
    drive, and None when not checked. `taken_at` None means "now", when stored.
    """

    used: int
    capacity: int
    is_mount: bool | None
    source: str = SPACE_STATVFS
    taken_at: float | None = None


@dataclass(frozen=True, slots=True)
class RootStats:
    """A scanned root. The space fields are None until recorded (schema v5)."""

    root: str
    files: int
    bytes: int
    scanned_at: float
    errors: int = 0
    used_bytes: int | None = None
    capacity_bytes: int | None = None
    is_mount: bool | None = None
    space_source: str | None = None
    space_at: float | None = None

    @property
    def space(self) -> DriveSpace | None:
        if self.used_bytes is None or self.capacity_bytes is None:
            return None
        return DriveSpace(
            self.used_bytes, self.capacity_bytes, self.is_mount,
            self.space_source or SPACE_STATVFS, self.space_at,
        )  # fmt: skip
