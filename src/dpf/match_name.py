"""Mode 1: duplicates by case-insensitive name, size and modification time."""

from __future__ import annotations

from collections.abc import Iterator

from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord


def _basis(ignore_size: bool, ignore_mtime: bool) -> str:
    parts = ["name"]
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


def find_name_duplicates(
    index: Index,
    roots: list[str],
    *,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
) -> Iterator[DuplicateGroup]:
    """Yield groups of indexed files under `roots` that look like copies by metadata."""
    basis = _basis(ignore_size, ignore_mtime)
    for bucket in index.name_buckets(roots, by_size=not ignore_size):
        clusters = [bucket] if ignore_mtime else _mtime_clusters(bucket, mtime_tolerance)
        for cluster in clusters:
            if len(cluster) >= 2:
                yield DuplicateGroup(tuple(sorted(cluster, key=lambda f: f.path)), basis)
