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
    def redundant_bytes(self) -> int:
        """Bytes that could be freed by keeping only one copy."""
        return sum(f.size for f in self.files) - self.size


@dataclass(frozen=True, slots=True)
class RootStats:
    root: str
    files: int
    bytes: int
    scanned_at: float
    errors: int = 0
