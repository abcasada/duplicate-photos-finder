"""Folder overview for sorting media by hand: one CSV row per folder, two levels deep.

`dpf folders` writes it from the index alone, so it never touches the drives. Each drive
gets a row per top-level folder and per folder inside that, with media counts, size,
file dates and the main file types, plus blank "Personal?" and "Notes" columns for the
owner to fill in (e.g. to decide the `[categories]` patterns of the pools file).

Counts include everything below a folder. Files lying directly on a drive, or directly
in a top-level folder that also has subfolders, get their own "(files directly here)"
row so that every file belongs to exactly one deepest row. Folders without any media
get no row (their non-media files still count in "Other files" of the rows above).
"""

from __future__ import annotations

import csv
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from dpf import media
from dpf.grouping import natural_key
from dpf.index import Index

HERE = "(files directly here)"

FIELDS = (
    "Pool", "Drive", "Folder", "Subfolder", "Personal?", "Notes",
    "Media files", "Photos", "Videos", "Media size (GB)", "Other files",
    "Folders inside", "Oldest file", "Newest file", "Main types", "Path",
)  # fmt: skip

TYPES_SHOWN = 4


@dataclass(slots=True)
class FolderStats:
    """Totals for one row: every indexed file at or below its folder."""

    photos: int = 0
    videos: int = 0
    bytes: int = 0
    other: int = 0
    oldest: float | None = None
    newest: float | None = None
    dirs: set[str] = field(default_factory=set)
    types: Counter[str] = field(default_factory=Counter)

    @property
    def files(self) -> int:
        return self.photos + self.videos

    def add_file(self, dir: str, name: str, size: int, mtime: float, kind: str) -> None:
        if kind == media.VIDEO:
            self.videos += 1
        elif kind == media.IMAGE:
            self.photos += 1
        else:  # indexed with --all-files
            self.other += 1
            return
        self.bytes += size
        self.oldest = mtime if self.oldest is None else min(self.oldest, mtime)
        self.newest = mtime if self.newest is None else max(self.newest, mtime)
        self.dirs.add(dir)
        ext = os.path.splitext(name)[1].lstrip(".").upper()
        self.types[ext or "(none)"] += 1

    def add_other(self, dir: str, count: int) -> None:
        self.other += count
        self.dirs.add(dir)


# (folder, subfolder): "" for a drive's or top-level folder's own row.
Key = tuple[str, str]


def _keys(root: str, dir: str) -> list[Key]:
    """The rows a file in `dir` counts towards."""
    rel = os.path.relpath(dir, root)
    if rel == ".":
        return [(HERE, "")]
    parts = rel.split(os.sep)
    if len(parts) == 1:
        return [(parts[0], ""), (parts[0], HERE)]
    return [(parts[0], ""), (parts[0], parts[1])]


def summarize(idx: Index, root: str) -> dict[Key, FolderStats]:
    """Row totals for one indexed root, two folder levels below it."""
    rows: dict[Key, FolderStats] = {}

    def stats(key: Key) -> FolderStats:
        found = rows.get(key)
        if found is None:
            found = rows[key] = FolderStats()
        return found

    for dir, name, size, mtime, kind in idx.file_rows([root]):
        for key in _keys(root, dir):
            stats(key).add_file(dir, name, size, mtime, kind)
    for dir, count in idx.extras_under([root]).items():
        for key in _keys(root, dir):
            stats(key).add_other(dir, count)
    # Folders holding no media need no decision; and a top-level folder without media
    # subfolders needs no separate "files directly here" row.
    rows = {k: s for k, s in rows.items() if s.files}
    parents_with_children = {f for f, sub in rows if sub and sub != HERE}
    return {k: s for k, s in rows.items() if k[1] != HERE or k[0] in parents_with_children}


def _row_order(key: Key) -> tuple:
    folder, sub = key
    # Loose files first, then folders in natural order, each followed by its subfolders.
    return (folder != HERE, natural_key(folder), folder, sub != "", sub != HERE,
            natural_key(sub), sub)  # fmt: skip


def _date(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts is not None else ""


def _row(pool: str, root: str, key: Key, s: FolderStats) -> list[str | int]:
    folder, sub = key
    path = root if folder == HERE else os.path.join(root, folder)
    if sub and sub != HERE:
        path = os.path.join(path, sub)
    inside = len(s.dirs - {path})
    types = ", ".join(f"{ext} {n:,}" for ext, n in s.types.most_common(TYPES_SHOWN))
    return [
        pool, os.path.basename(root) or root, folder, sub, "", "",
        s.files, s.photos, s.videos, f"{s.bytes / 1e9:.2f}", s.other,
        inside, _date(s.oldest), _date(s.newest), types, path,
    ]  # fmt: skip


def write_folders_csv(
    idx: Index, roots: Sequence[str], path: str, pool_of: Callable[[str], str]
) -> int:
    """Write the overview of `roots` (in the given order) to `path`; returns the row count.

    UTF-8 with a byte-order mark, so Excel shows non-ASCII folder names correctly.
    """
    n = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for root in roots:
            rows = summarize(idx, root)
            for key in sorted(rows, key=_row_order):
                w.writerow(_row(pool_of(root), root, key, rows[key]))
                n += 1
    return n


def sort_roots(roots: Iterable[str], pool_of: Callable[[str], str]) -> list[str]:
    """Roots grouped by pool (pool-less last), each pool's drives in natural order."""
    return sorted(roots, key=lambda r: (pool_of(r) == "", pool_of(r), natural_key(r), r))
