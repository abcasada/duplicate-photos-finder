"""Mode 1: duplicates by name, size and modification time.

Names match when equal up to case, copy marks and neutral text around a camera file
number (names.match_key), or with `exact_names` only up to case. Within one key, names
with different copy numbers never match, and a folder holding a renamed series
("X (1)", "X (2)") makes its key fall back to exact names (see names).

Modification times match when they differ by at most the tolerance, or by a whole
number of hours (1 to MAX_HOURS) give or take the tolerance: copies made across time
zones or a DST change keep the photo but shift its mtime by whole hours (times_match).
Groups are built so that every two members match under that rule (_hour_merge), never
by chaining.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord
from dpf.names import copy_numbers


HOUR = 3600
MAX_HOURS = 14  # the largest whole-hour shift accepted (time zones span UTC-12 … UTC+14)


def basis(
    *,
    exact_names: bool = False,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    tolerance: float | None = None,
    hour_shift: bool = False,
) -> str:
    """How groups were matched, e.g. "similar name+size+mtime ±2s (or whole hours)".

    The tolerance and the hour-shift note are only added when `tolerance` is given.
    """
    parts = ["name" if exact_names else "similar name"]
    if not ignore_size:
        parts.append("size")
    if not ignore_mtime:
        parts.append("mtime")
    text = "+".join(parts)
    if ignore_mtime or tolerance is None:
        return text
    text += f" ±{tolerance:g}s"
    return text + " (or whole hours)" if shifts_hours(tolerance, hour_shift) else text


def shifts_hours(tolerance: float, hour_shift: bool = True) -> bool:
    """True if whole-hour shifts are matched: asked for, and the tolerance well below 30 min.

    With a tolerance of half an hour or more every residue would match, so the rule is off.
    """
    return hour_shift and tolerance < HOUR / 2


def times_match(t1: float, t2: float, tolerance: float, *, hour_shift: bool = True) -> bool:
    """The mtime rule: |t1 - t2| <= tol, or |t1 - t2 - k*3600| <= tol with 1 <= |k| <= 14."""
    delta = abs(t1 - t2)
    if delta <= tolerance:
        return True
    if not shifts_hours(tolerance, hour_shift):
        return False
    k = round(delta / HOUR)
    return 1 <= k <= MAX_HOURS and abs(delta - k * HOUR) <= tolerance


def hour_offsets(files: Sequence[FileRecord]) -> tuple[int, ...]:
    """Whole hours each file's mtime lies after the group's earliest, () if all are 0.

    Only meaningful for a group from bucket_groups with mtimes compared: there every two
    members differ by whole hours give or take the tolerance (under 30 minutes).
    """
    if not files:
        return ()
    t0 = min(f.mtime for f in files)
    hours = tuple(round((f.mtime - t0) / HOUR) for f in files)
    return hours if any(hours) else ()


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


class _Merged:
    """A candidate group of mtime clusters, all matching each other across whole hours.

    Every member's mtime residue (modulo one hour) lies on an arc no longer than the
    tolerance, measured as offsets from the first member's residue, and the whole group
    spans at most MAX_HOURS hours plus the tolerance. Then every two members differ by
    k hours (0 <= k <= MAX_HOURS) give or take the tolerance, so all pairs match.
    """

    __slots__ = ("files", "ref", "lo", "hi", "first", "last")

    def __init__(self, cluster: list[FileRecord]) -> None:
        self.files = list(cluster)
        self.ref = cluster[0].mtime
        offsets = [self._offset(f.mtime) for f in cluster]
        self.lo, self.hi = min(offsets), max(offsets)
        self.first = min(f.mtime for f in cluster)
        self.last = max(f.mtime for f in cluster)

    def _offset(self, t: float) -> float:
        """Signed residue distance from the reference file, in [-1800, 1800)."""
        return (t - self.ref + HOUR / 2) % HOUR - HOUR / 2

    def add(self, cluster: list[FileRecord], tolerance: float) -> bool:
        """Add `cluster` if every pair would still match; False (unchanged) otherwise."""
        offsets = [self._offset(f.mtime) for f in cluster]
        lo, hi = min(self.lo, *offsets), max(self.hi, *offsets)
        first = min(self.first, *(f.mtime for f in cluster))
        last = max(self.last, *(f.mtime for f in cluster))
        if hi - lo > tolerance or last - first > MAX_HOURS * HOUR + tolerance:
            return False
        self.files += cluster
        self.lo, self.hi, self.first, self.last = lo, hi, first, last
        return True


def _hour_merge(
    clusters: Iterable[list[FileRecord]], tolerance: float
) -> Iterator[list[FileRecord]]:
    """Merge mtime clusters whose files differ by whole hours (time zone or DST copies).

    Clusters (from _mtime_clusters, in time order) are never split, so every match found
    without hour shifts is kept. A cluster joins the first earlier group it fits in
    whole (_Merged.add), which keeps every pair in a group matching under times_match:
    t, t+1h and t+2h form one group (each pair is a whole-hour shift), but t, t+1h+1.5s
    and t+2h+3s do not chain (the residues spread over 3 s); the last one starts its own.
    """
    groups: list[_Merged] = []
    for cluster in clusters:
        if not any(g.add(cluster, tolerance) for g in groups):
            groups.append(_Merged(cluster))
    for g in groups:
        yield g.files


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


def bucket_groups(
    bucket: list[FileRecord],
    *,
    mtime_tolerance: float = 2.0,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> Iterator[list[FileRecord]]:
    """Split one name bucket (as from Index.name_buckets) into groups of copies.

    Applies the renamed-series fallback, mtime clustering (merged across whole-hour
    shifts unless `hour_shift` is False) and the copy-number split. Only groups of two
    or more files are yielded.
    """
    # Equal names (up to case) have equal copy numbers: nothing to split.
    uniform = exact_names or _one_name(bucket)
    for part in [bucket] if uniform else _series_split(bucket):
        if len(part) < 2:
            continue
        if ignore_mtime:
            clusters: Iterable[list[FileRecord]] = [part]
        else:
            clusters = _mtime_clusters(part, mtime_tolerance)
            if shifts_hours(mtime_tolerance, hour_shift):
                clusters = _hour_merge(clusters, mtime_tolerance)
        for cluster in clusters:
            for group in [cluster] if uniform else _number_split(cluster):
                if len(group) >= 2:
                    yield group


def find_name_duplicates(
    index: Index,
    roots: list[str],
    *,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> Iterator[DuplicateGroup]:
    """Yield groups of indexed files under `roots` that look like copies by metadata.

    A group whose files differ by whole hours carries their offsets (DuplicateGroup.hours).
    """
    how = basis(exact_names=exact_names, ignore_size=ignore_size, ignore_mtime=ignore_mtime)
    for bucket in index.name_buckets(roots, by_size=not ignore_size, exact_names=exact_names):
        for group in bucket_groups(
            bucket, mtime_tolerance=mtime_tolerance, ignore_mtime=ignore_mtime,
            exact_names=exact_names, hour_shift=hour_shift,
        ):  # fmt: skip
            files = tuple(sorted(group, key=lambda f: f.path))
            hours = () if ignore_mtime else hour_offsets(files)
            yield DuplicateGroup(files, how, hours)
