"""Mode 1b: duplicates by camera shot identity, else by name, size and date (mode 1).

A file's shot key (exif.ExifData.shot, cached by `dpf exif`) is its camera's make, model
and body serial, the shutter count and DateTimeOriginal, plus its format family and, for
non-RAW formats, its pixel size. Two files with the same shot key are the same photo in
the same format, whatever their names, sizes and dates: a DNG or ARW re-saved with new
metadata still matches its original. This proves the same capture, not the same bytes.

Rules:
- Same shot key: a match, even with different names, sizes and mtimes.
- RAW+JPG of one shot (or a resized JPG) have different keys: never duplicates.
- A conflicting identity vetoes a mode-1 match: a name/size/date group holding files of
  two shot keys is split by key (split_by_shot), and its files without a key are kept
  apart from both, since either side could be theirs.
- Files without a key (no identity, or no EXIF cached yet) match by mode 1 alone.

The final groups are the union of shot groups and mode-1 groups. Every mode-1 group
holds at most one shot key after the veto, so the union joins it to that key's shot
group only, and never chains two shots together.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dpf.index import Index
from dpf.match_name import basis as name_basis, bucket_groups, hour_offsets
from dpf.models import DuplicateGroup, FileRecord

SHOT_BASIS = "same shot (camera serial + shutter count)"
SHOT_WORDS = "the same camera shot (body serial and shutter count)"


def basis(name_rule: str) -> str:
    """E.g. "same shot (camera serial + shutter count), else similar name+size+mtime ±2s"."""
    return f"{SHOT_BASIS}, else {name_rule}"


def split_by_shot(
    group: Sequence[FileRecord], shot_of: Callable[[FileRecord], str | None]
) -> list[tuple[list[FileRecord], str | None]]:
    """A mode-1 group as parts holding at most one shot key each: (files, key or None).

    With one key (or none) the group stays whole. With several, each key's files form a
    part and the files without a key form one more (key None). Parts may be single files.
    """
    keys = {shot_of(f) for f in group} - {None}
    if len(keys) <= 1:
        return [(list(group), next(iter(keys), None))]
    parts: dict[str | None, list[FileRecord]] = {}
    for f in group:
        parts.setdefault(shot_of(f), []).append(f)
    return [(files, key) for key, files in parts.items()]


@dataclass(slots=True)
class ShotStats:
    """What mode 1b found beyond mode 1, for the report header."""

    files: int = 0  # compared photos/videos (non-empty)
    with_identity: int = 0  # cached, with a shot identity
    no_identity: int = 0  # cached, read without an identity
    errors: int = 0  # cached as unreadable
    no_exif: int = 0  # no valid cached row: run `dpf exif`
    shot_groups: int = 0  # groups that rely on a shot match
    resaved_groups: int = 0  # of those, holding files of different sizes
    vetoed: int = 0  # name/size/date groups split because their shots differ
    multi_format: int = 0  # shots held in two or more formats (e.g. RAW+JPG)

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def exif_counts(index: Index, roots: Sequence[str]) -> ShotStats:
    """ShotStats with the EXIF cache state of the media under `roots` filled in."""
    counts = index.exif_status(roots)
    stats = ShotStats(
        with_identity=counts["ok"], no_identity=counts["none"], errors=counts["error"],
        no_exif=counts["missing"],
    )  # fmt: skip
    stats.files = sum(counts.values())
    stats.multi_format = index.multi_format_shots(roots)
    return stats


def find_shot_duplicates(
    index: Index,
    roots: list[str],
    *,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> tuple[list[DuplicateGroup], ShotStats]:
    """Duplicate groups under `roots` by shot key, else by mode 1 (see module docstring).

    Groups that rely on a shot match have basis SHOT_BASIS and no hour offsets; groups
    found by mode 1 alone keep mode 1's basis and offsets.
    """
    stats = exif_counts(index, roots)
    how = name_basis(exact_names=exact_names, ignore_size=ignore_size, ignore_mtime=ignore_mtime)
    keyed: dict[str, list[list[FileRecord]]] = {}
    plain: list[list[FileRecord]] = []
    buckets = index.shot_name_buckets(roots, by_size=not ignore_size, exact_names=exact_names)
    for bucket in buckets:
        shots = {id(f): s for f, s in bucket}
        for group in bucket_groups(
            [f for f, _ in bucket], mtime_tolerance=mtime_tolerance,
            ignore_mtime=ignore_mtime, exact_names=exact_names, hour_shift=hour_shift,
        ):  # fmt: skip
            parts = split_by_shot(group, lambda f: shots[id(f)])
            stats.vetoed += len(parts) > 1
            for files, key in parts:
                if len(files) < 2:
                    continue
                (keyed.setdefault(key, []) if key else plain).append(files)

    def by_name(files: list[FileRecord]) -> DuplicateGroup:
        files = sorted(files, key=lambda f: f.path)
        return DuplicateGroup(tuple(files), how, () if ignore_mtime else hour_offsets(files))

    groups: list[DuplicateGroup] = []
    for key, files in index.shot_groups(roots):
        parts = keyed.pop(key, [])
        members = {f.path: f for f in files}
        for part in parts:
            members.update((f.path, f) for f in part)
        if len(parts) == 1 and len(members) == len(parts[0]):
            groups.append(by_name(parts[0]))  # the name match already holds every copy
            continue
        stats.shot_groups += 1
        stats.resaved_groups += len({f.size for f in members.values()}) > 1
        ordered = tuple(sorted(members.values(), key=lambda f: f.path))
        groups.append(DuplicateGroup(ordered, SHOT_BASIS))
    # A key left here has one file with an identity: its single name group is the match.
    groups += [by_name(part) for parts in keyed.values() for part in parts]
    groups += [by_name(part) for part in plain]
    groups.sort(key=lambda g: g.files[0].path)
    return groups, stats
