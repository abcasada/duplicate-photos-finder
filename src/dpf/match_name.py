"""Mode 1: duplicates by name, size and modification time.

Names match when equal up to case, copy marks and neutral text around a camera file
number (names.match_key), or with `exact_names` only up to case. Within one key, names
with different copy numbers never match, and a folder holding a renamed series
("X (1)", "X (2)") makes its key fall back to exact names (see names).
"""

from __future__ import annotations

from collections.abc import Iterator

from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord
from dpf.names import copy_numbers


def basis(
    *, exact_names: bool = False, ignore_size: bool = False, ignore_mtime: bool = False
) -> str:
    """How groups were matched, e.g. "similar name+size+mtime" or "name+size"."""
    parts = ["name" if exact_names else "similar name"]
    if not ignore_size:
        parts.append("size")
    if not ignore_mtime:
        parts.append("mtime")
    return "+".join(parts)


def _mtime_clusters(files: list[FileRecord], tolerance: float) -> Iterator[list[FileRecord]]:
    """Split by mtime so every file is within `tolerance` of its cluster's earliest file.

    Anchoring on the earliest file (rather than chaining neighbours) guarantees no two
    files in a cluster differ by more than the tolerance.
    """
    files = sorted(files, key=lambda f: f.mtime)
    cluster = [files[0]]
    for cur in files[1:]:
        if cur.mtime - cluster[0].mtime > tolerance:
            yield cluster
            cluster = []
        cluster.append(cur)
    yield cluster


def _one_name(files: list[FileRecord]) -> bool:
    first = files[0].name.casefold()
    return all(f.name.casefold() == first for f in files)


def is_series(bucket: list[FileRecord]) -> bool:
    """True if one folder holds two files of `bucket` with different copy numbers.

    "Vacation (1)" and "Vacation (2)" side by side are a renamed series (a Windows bulk
    rename of a burst), not copies. "X" beside "X (1)" or "X - Copy" is still a copy.
    """
    seen: dict[str, tuple[int, ...]] = {}
    for f in bucket:
        numbers = copy_numbers(f.name)
        if numbers and seen.setdefault(f.dir, numbers) != numbers:
            return True
    return False


def _series_split(bucket: list[FileRecord]) -> list[list[FileRecord]]:
    """`bucket`, or for a renamed series its files split by exact (case-insensitive) name."""
    if not is_series(bucket):
        return [bucket]
    parts: dict[str, list[FileRecord]] = {}
    for f in bucket:
        parts.setdefault(f.name.casefold(), []).append(f)
    return list(parts.values())


def _number_split(cluster: list[FileRecord]) -> list[list[FileRecord]]:
    """`cluster`, or split by copy number when its numbered names disagree.

    Different copy numbers never match: "X (1)" matches "X" but never "X (2)". With two
    or more different numbers, an unnumbered file would pair with several numbered ones
    that do not pair with each other, so each number (and "none") becomes its own group.
    """
    numbers = [copy_numbers(f.name) for f in cluster]
    if len({n for n in numbers if n}) < 2:
        return [cluster]
    parts: dict[tuple[int, ...], list[FileRecord]] = {}
    for f, n in zip(cluster, numbers):
        parts.setdefault(n, []).append(f)
    return list(parts.values())


def find_name_duplicates(
    index: Index,
    roots: list[str],
    *,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
) -> Iterator[DuplicateGroup]:
    """Yield groups of indexed files under `roots` that look like copies by metadata."""
    how = basis(exact_names=exact_names, ignore_size=ignore_size, ignore_mtime=ignore_mtime)
    for bucket in index.name_buckets(roots, by_size=not ignore_size, exact_names=exact_names):
        # Equal names (up to case) have equal copy numbers: nothing to split.
        uniform = exact_names or _one_name(bucket)
        for part in [bucket] if uniform else _series_split(bucket):
            if len(part) < 2:
                continue
            clusters = [part] if ignore_mtime else _mtime_clusters(part, mtime_tolerance)
            for cluster in clusters:
                for group in [cluster] if uniform else _number_split(cluster):
                    if len(group) >= 2:
                        yield DuplicateGroup(tuple(sorted(group, key=lambda f: f.path)), how)
