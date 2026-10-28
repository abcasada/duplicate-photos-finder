"""Media categories: personal photos/videos versus other media (films, downloads, …).

The pools file may hold a `[categories]` table:

    [categories]
    other = ["*/Plex*", "/run/media/adam/Storage 2A 8TB/_Torrents"]

An indexed media file is OTHER when its folder, or a folder above it, matches an `other`
pattern, and PERSONAL otherwise. Patterns use the pool pattern syntax (`*`, `?`, `[..]`,
matched one path segment at a time, so `*` never spans "/"), but ignore case, as NTFS
does. An absolute pattern is anchored at "/"; a relative one matches at any depth:
"*/Plex*" is any folder named Plex… below another folder, "_Torrents" any folder named
_Torrents.

Duplicates and coverage entries take the category of their folders: OTHER only when
every folder involved is OTHER, so a personal photo copied into a media folder still
counts as personal.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Protocol

from dpf.pools import PoolError, default_pools_path

PERSONAL = "personal"
OTHER = "other"
ALL = "all"
CATEGORIES = (PERSONAL, OTHER)
TITLES = {PERSONAL: "Personal photos/videos", OTHER: "Other media"}

Category = Callable[[str], str]


class Categories:
    """The `other` patterns, and the category of a folder (memoised per folder)."""

    def __init__(self, other: Iterable[str] = ()) -> None:
        self.other = tuple(other)
        self._patterns = [
            (p.startswith("/"), [s.casefold() for s in _segments(p)]) for p in self.other
        ]
        self._memo: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self.other)

    def __call__(self, dir: str) -> str:
        return self.of(dir)

    def of(self, dir: str) -> str:
        """PERSONAL or OTHER for the folder `dir` (absolute)."""
        dir = dir.rstrip("/") or "/"
        found = self._memo.get(dir)
        if found is not None:
            return found
        parent = os.path.dirname(dir)
        if parent != dir and self.of(parent) == OTHER:
            found = OTHER
        else:
            found = OTHER if self._matches_at(dir) else PERSONAL
        self._memo[dir] = found
        return found

    def _matches_at(self, dir: str) -> bool:
        """True if a pattern matches `dir` itself (its ancestors are checked separately)."""
        parts = [s.casefold() for s in _segments(dir)]
        for absolute, pattern in self._patterns:
            n = len(pattern)
            if not n or len(parts) < n or (absolute and len(parts) != n):
                continue
            if all(fnmatchcase(part, pat) for part, pat in zip(parts[-n:], pattern)):
                return True
        return False


def _segments(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def of_dirs(category: Category | None, dirs: Iterable[str]) -> str:
    """OTHER if every folder is OTHER, else PERSONAL (always PERSONAL without categories)."""
    if category is None:
        return PERSONAL
    return OTHER if all(category(d) == OTHER for d in dirs) else PERSONAL


def parse_categories(data: Mapping[str, object], path: str = "pools file") -> Categories:
    """The optional [categories] table of a parsed pools file."""
    table = data.get("categories")
    if table is None:
        return Categories()
    if not isinstance(table, dict):
        raise PoolError(f"{path}: [categories] must be a table, e.g. other = [\"*/Plex*\"]")
    unknown = sorted(set(table) - {OTHER})
    if unknown:
        raise PoolError(
            f"{path}: [categories] has unknown key {unknown[0]}; only `other` is supported "
            "(everything else is personal)"
        )
    value = table.get(OTHER, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip("/") for v in value):
        raise PoolError(f"{path}: [categories] other must be a list of folder paths or patterns")
    return Categories(os.path.expanduser(v).rstrip("/") for v in value)


def load_categories(path: str | None = None) -> Categories:
    """[categories] from the pools file; empty when the file (or the table) is absent."""
    path = path or default_pools_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return Categories()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PoolError(f"cannot read pools file {path}: {e}") from None
    return parse_categories(data, path)


class _DirTotals(Protocol):
    def dir_totals(self, roots: Sequence[str]) -> Iterable[tuple[str, int, int]]: ...


def tally(index: _DirTotals, roots: Sequence[str], category: Category) -> dict[str, list[int]]:
    """{PERSONAL: [files, bytes], OTHER: [files, bytes]} of the media indexed under `roots`."""
    out = {c: [0, 0] for c in CATEGORIES}
    for d, files, nbytes in index.dir_totals(roots):
        entry = out[category(d)]
        entry[0] += files
        entry[1] += nbytes
    return out
