"""Backup coverage: the media files of one pool that have no copy anywhere in another.

This is the one comparison between pools (duplicates are never searched across pools).
A file "has a copy" in the other pool when mode 1, run over both pools together, would
group it with a file of the other pool: same match_key (or exact name), same size and
mtime within the tolerance (or whole hours apart, give or take the tolerance), under the
same copy-number and renamed-series rules (match_name.bucket_groups). Where the file
sits in the other pool does not matter.

Groups are built as in mode 1, so a file is only called backed up when a file of the
other pool matches it directly; in doubt it is reported missing. Contents are not
compared: a renamed-and-re-saved copy (different size) counts as missing. Only media
files are compared; other files (sidecars, edits, catalogs) are counted, never checked.

Missing files are reported per folder, in runs of consecutive missing files (natural
order), and a tree whose compared files are all missing becomes one line. Roll-up never
climbs above the scanned roots, nor across a category boundary (categories), so each
entry is wholly personal or wholly other media. 0-byte files are not compared and only
counted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from dpf.categories import ALL, CATEGORIES, OTHER, PERSONAL, Category
from dpf.grouping import Run, beside_sibling, natural_key
from dpf.index import Index
from dpf.match_name import basis, bucket_groups, hour_offsets, shifts_hours
from dpf.models import FileRecord

NOT_BACKED_UP = "not_backed_up"  # the other pool is the backup of this one
ONLY_IN_BACKUP = "only_in_backup"  # this pool is the backup of the other one
NOT_IN_OTHER = "not_in_other"  # pools not linked by [backups] (--coverage-against)


@dataclass(frozen=True, slots=True)
class MissingFolder:
    """A folder with files that have no copy in the other pool, or a whole missing tree.

    `missing`, `total` and `bytes` count compared (non-empty) files: directly in the
    folder, or for a whole tree everything under it. `runs` are consecutive missing files
    in natural order (Run with equal A and B names); a whole tree has none.
    """

    dir: str
    missing: int
    total: int
    bytes: int
    runs: tuple[Run, ...] = ()
    folders: int = 1
    whole_tree: bool = False
    unindexed: int = 0  # entries not indexed (non-media, unreadable) in the folder or tree
    similar: int = 0  # missing files with a similarly named file of another size/date there
    category: str = PERSONAL


@dataclass(slots=True)
class Coverage:
    """Files under `roots` (pool `pool`) with no copy under `other_roots` (pool `other`)."""

    pool: str | None
    other: str
    direction: str  # NOT_BACKED_UP, ONLY_IN_BACKUP or NOT_IN_OTHER
    roots: list[str]
    other_roots: list[str]
    basis: str  # e.g. "similar name+size+mtime ±2s"
    basis_words: str  # e.g. "similar name, size and date"
    files: int  # compared (non-empty) files under roots
    bytes: int
    missing: list[FileRecord]  # sorted by folder, then natural name order
    similar: set[str]  # paths of missing files whose name key occurs in the other pool
    missing_bytes: int
    empty: int  # 0-byte files under roots, not compared
    folders: list[MissingFolder]  # largest first
    other_status: list[dict] = field(default_factory=list)  # report.root_lines entries
    never_scanned: list[str] = field(default_factory=list)  # other-pool members not indexed
    elapsed: float = 0.0
    shifted: int = 0  # files counted as copied only through a whole-hour time shift
    unindexed: int = 0  # entries not indexed (non-media, unreadable) under roots: not compared
    unindexed_dirs: int = 0  # folders holding them
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)  # see check_coverage
    category: str = ALL  # the category shown (--category); ALL for both
    warnings: list[str] = field(default_factory=list)  # e.g. a stale index of the other pool

    @property
    def missing_dirs(self) -> int:
        """Folders directly holding missing files (each folder of a tree counted)."""
        return len({f.dir for f in self.missing})

    @property
    def label(self) -> str:
        """Short name, e.g. "A not in B" (or "Not in B" for a run on paths)."""
        return f"{self.pool} not in {self.other}" if self.pool else f"Not in {self.other}"

    @property
    def title(self) -> str:
        if self.direction == NOT_BACKED_UP:
            return f"Not backed up — no copy anywhere in pool {self.other}"
        if self.direction == ONLY_IN_BACKUP:
            return f"Only in the backup — no copy anywhere in pool {self.other}"
        return f"Not in pool {self.other} — no copy anywhere in pool {self.other}"


def basis_words(
    *,
    exact_names: bool = False,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    hour_shift: bool = False,
) -> str:
    """E.g. "similar name, size and date" for the caveat line."""
    parts = ["name" if exact_names else "similar name"]
    if not ignore_size:
        parts.append("size")
    if not ignore_mtime:
        parts.append("date (or a date whole hours apart)" if hour_shift else "date")
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def caveat(cov_basis_words: str) -> str:
    return (
        f"Matched by {cov_basis_words}, not by contents; a renamed-and-re-saved copy "
        "counts as missing."
    )


def find_missing(
    index: Index,
    roots: Sequence[str],
    other_roots: Sequence[str],
    *,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> tuple[list[FileRecord], Counter[str], set[str], Counter[str]]:
    """Non-empty files under `roots` with no copy under `other_roots`.

    Returns (missing files, compared files per dir, paths of the missing files whose
    name key does occur under `other_roots` with another size or date, and per dir the
    files whose only copies differ from them by whole hours). `roots` and `other_roots`
    must not overlap.
    """
    missing: list[FileRecord] = []
    similar: set[str] = set()
    totals: Counter[str] = Counter()
    shifted: Counter[str] = Counter()
    other_keys = index.name_keys(other_roots, exact_names=exact_names)
    buckets = index.coverage_buckets(
        roots, other_roots, by_size=not ignore_size, exact_names=exact_names
    )
    for key, bucket in buckets:
        mine = [f for f, other in bucket if not other]
        for f in mine:
            totals[f.dir] += 1
        if len(mine) == len(bucket):
            missing += mine
            if key in other_keys:
                similar.update(f.path for f in mine)
            continue
        others = {id(f) for f, other in bucket if other}
        covered: set[int] = set()
        for group in bucket_groups(
            [f for f, _ in bucket], mtime_tolerance=mtime_tolerance,
            ignore_mtime=ignore_mtime, exact_names=exact_names, hour_shift=hour_shift,
        ):  # fmt: skip
            if not any(id(f) in others for f in group):
                continue
            covered.update(id(f) for f in group)
            hours = () if ignore_mtime else hour_offsets(group)
            if hours:
                there = {h for f, h in zip(group, hours) if id(f) in others}
                for f, h in zip(group, hours):
                    if id(f) not in others and h not in there:
                        shifted[f.dir] += 1
        left = [f for f in mine if id(f) not in covered]
        missing += left
        similar.update(f.path for f in left)
    missing.sort(key=lambda f: (f.dir, natural_key(f.name), f.name))
    return missing, totals, similar, shifted


def roll_up(
    missing: Sequence[FileRecord],
    totals: Counter[str],
    roots: Sequence[str],
    list_dir: Callable[[str], list[FileRecord]],
    extras: dict[str, int] | None = None,
    similar: set[str] | None = None,
    category: Category | None = None,
) -> list[MissingFolder]:
    """Missing files as folder entries, largest first.

    A tree whose compared files are all missing and that holds files in two or more
    folders becomes one whole-tree entry, as high up as possible without leaving the
    scanned `roots`. Every other folder with missing files gets an entry with its runs.
    `totals` counts compared files per folder (as find_missing returns); `extras` maps
    folders to entries that were not indexed; `similar` holds the paths of missing files
    with a similarly named file in the other pool (counted per entry). With `category`,
    a whole tree holds folders of one category only, and each entry carries it.
    """
    extras = extras or {}
    similar = similar or set()
    chain = _Ancestry(roots)

    def cat(d: str) -> str:
        return category(d) if category is not None else PERSONAL

    miss: Counter[str] = Counter()
    miss_bytes: Counter[str] = Counter()
    near: Counter[str] = Counter()
    names: dict[str, set[str]] = {}
    for f in missing:
        miss[f.dir] += 1
        miss_bytes[f.dir] += f.size
        names.setdefault(f.dir, set()).add(f.name)
        if f.path in similar:
            near[f.dir] += 1

    sub_total: Counter[str] = Counter()
    sub_miss: Counter[str] = Counter()
    sub_bytes: Counter[str] = Counter()
    sub_folders: Counter[str] = Counter()
    sub_extra: Counter[str] = Counter()
    sub_near: Counter[str] = Counter()
    mixed: set[str] = set()  # trees holding compared files of another category than their top
    for d in set(totals) | set(extras):
        for a in chain(d):
            if totals[d]:
                sub_total[a] += totals[d]
                sub_miss[a] += miss[d]
                sub_bytes[a] += miss_bytes[d]
                sub_folders[a] += 1
                sub_near[a] += near[d]
                if category is not None and cat(d) != cat(a):
                    mixed.add(a)
            sub_extra[a] += extras.get(d, 0)

    def full(a: str) -> bool:
        return sub_total[a] > 0 and sub_miss[a] == sub_total[a] and a not in mixed

    tops = set()
    for a in sub_total:
        if not full(a) or sub_folders[a] < 2:
            continue
        up = chain(a)
        if len(up) == 1 or not full(up[1]):
            tops.add(a)

    entries = [
        MissingFolder(
            a, sub_miss[a], sub_total[a], sub_bytes[a], (), sub_folders[a], True, sub_extra[a],
            sub_near[a], cat(a),
        )  # fmt: skip
        for a in tops
    ]
    for d in miss:
        if any(a in tops for a in chain(d)):
            continue
        only = names[d] if miss[d] < totals[d] else None
        entries.append(
            MissingFolder(
                d, miss[d], totals[d], miss_bytes[d], _runs(list_dir(d), only),
                unindexed=extras.get(d, 0), similar=near[d], category=cat(d),
            )
        )  # fmt: skip
    entries.sort(key=lambda e: (-e.bytes, e.dir))
    return entries


def _runs(listing: list[FileRecord], missing: set[str] | None) -> tuple[Run, ...]:
    """Runs of consecutive missing files (all non-empty files when `missing` is None).

    As in grouping, a file with a copy that is another format of a missing neighbour's
    shot (the backed-up JPG of a missing RAW) is passed over and listed in Run.skipped
    rather than ending the run; `count` and `bytes` cover missing files only.
    """
    files = sorted(
        ((natural_key(f.name), f.name, f) for f in listing if f.size > 0), key=lambda e: e[:2]
    )
    if missing is None:
        missing = {name for _, name, _ in files}
    runs: list[Run] = []
    cur: list | None = None  # [first, last, count, bytes, skipped]
    passed: list[str] = []
    for i, (_, name, f) in enumerate(files):
        if name not in missing:
            if cur is None:
                continue
            if beside_sibling(files, missing, i):
                passed.append(name)
            else:
                runs.append(_run(cur))
                cur, passed = None, []
            continue
        if cur is None:
            cur = [name, name, 1, f.size, []]
        else:
            cur[1] = name
            cur[2] += 1
            cur[3] += f.size
            cur[4] += passed
        passed = []
    if cur is not None:
        runs.append(_run(cur))
    return tuple(runs)


def _run(cur: list) -> Run:
    first, last, count, nbytes, skipped = cur
    return Run(first, last, first, last, count, nbytes, True, tuple(skipped))


class _Ancestry:
    """Maps a folder to itself and every folder above it, up to its scanned root.

    () for a folder outside the roots. Results are memoised, so walking a whole tree
    costs one step per folder.
    """

    def __init__(self, roots: Sequence[str]) -> None:
        self.memo: dict[str, tuple[str, ...]] = {}
        for r in roots:
            r = r.rstrip("/") or "/"
            self.memo[r] = (r,)

    def __call__(self, d: str) -> tuple[str, ...]:
        found = self.memo.get(d)
        if found is not None:
            return found
        path: list[str] = []
        cur = d
        while True:
            known = self.memo.get(cur)
            if known is not None:
                break
            path.append(cur)
            cut = cur.rfind("/")
            parent = cur[:cut] or "/"
            if parent == cur:
                known = ()
                break
            cur = parent
        for p in reversed(path):
            known = (p, *known) if known else ()
            self.memo[p] = known
        return self.memo[d]


def check_coverage(
    index: Index,
    roots: Sequence[str],
    other_roots: Sequence[str],
    *,
    pool: str | None,
    other: str,
    direction: str,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
    category: Category | None = None,
    only: str = ALL,
) -> Coverage:
    """Find and roll up the files under `roots` that have no copy under `other_roots`.

    Raises ValueError when a root of one side equals or lies inside a root of the other.
    With `category`, `by_category` holds per category the compared, missing and shifted
    files and missing bytes; `only` (PERSONAL or OTHER) keeps the files of one category.
    """
    for r in roots:
        for o in other_roots:
            if _related(r, o):
                raise ValueError(f"coverage roots overlap: {r} and {o}")
    missing, totals, similar, shifted = find_missing(
        index, roots, other_roots, mtime_tolerance=mtime_tolerance, ignore_size=ignore_size,
        ignore_mtime=ignore_mtime, exact_names=exact_names, hour_shift=hour_shift,
    )  # fmt: skip
    extras = index.extras_under(roots)

    def cat(d: str) -> str:
        return category(d) if category is not None else PERSONAL

    by_category = {c: {"files": 0, "missing": 0, "missing_bytes": 0, "shifted": 0}
                   for c in CATEGORIES}  # fmt: skip
    for d, n in totals.items():
        by_category[cat(d)]["files"] += n
    for f in missing:
        by_category[cat(f.dir)]["missing"] += 1
        by_category[cat(f.dir)]["missing_bytes"] += f.size
    for d, n in shifted.items():
        by_category[cat(d)]["shifted"] += n
    if only != ALL:
        missing = [f for f in missing if cat(f.dir) == only]
        totals = Counter({d: n for d, n in totals.items() if cat(d) == only})
        shifted = Counter({d: n for d, n in shifted.items() if cat(d) == only})
        extras = {d: n for d, n in extras.items() if cat(d) == only}
        similar = {p for p in similar if cat(p.rsplit("/", 1)[0] or "/") == only}
    folders = roll_up(missing, totals, roots, index.list_dir, extras, similar, category)
    nbytes = index.totals(roots)[1]
    shift = not ignore_mtime and shifts_hours(mtime_tolerance, hour_shift)
    flags = {"exact_names": exact_names, "ignore_size": ignore_size, "ignore_mtime": ignore_mtime}
    how = basis(**flags, tolerance=mtime_tolerance, hour_shift=hour_shift)
    cov = Coverage(
        pool, other, direction, list(roots), list(other_roots), how,
        basis_words(**flags, hour_shift=shift), sum(totals.values()), nbytes, missing,
        similar, sum(f.size for f in missing), index.empty_files(roots), folders,
    )  # fmt: skip
    cov.shifted = sum(shifted.values())
    cov.unindexed = sum(extras.values())
    cov.unindexed_dirs = len(extras)
    cov.by_category = by_category if category is not None else {}
    cov.category = only
    return cov


def _related(x: str, y: str) -> bool:
    x, y = x.rstrip("/") or "/", y.rstrip("/") or "/"
    return x == y or "/" in (x, y) or x.startswith(y + "/") or y.startswith(x + "/")


STALE_AFTER = 86_400  # seconds between scans before the other pool is called out of date


def stale_warnings(target_status: Sequence[dict], cov: Coverage) -> list[str]:
    """Warnings for roots of the other pool scanned over a day before this pool's newest scan.

    Files deleted from them since still count as copies, so coverage may look better
    than it is. Entries are report.root_lines dicts (root, scanned_at).
    """
    times = [e["scanned_at"] for e in target_status if e.get("scanned_at") is not None]
    if not times:
        return []
    from dpf.report import scan_date

    newest = max(times)
    return [
        f"pool {cov.other}'s index may be out of date: {e['root']} last scanned "
        f"{scan_date(e['scanned_at'])}; files deleted since then still count as copies"
        for e in cov.other_status
        if e.get("scanned_at") is not None and newest - e["scanned_at"] > STALE_AFTER
    ]


_COPY_SUFFIX = re.compile(r" \(\d+\)$")


def drive_label(root: str) -> str:
    """A root's last path segment, casefolded, minus a trailing " (N)" (a re-mounted label)."""
    name = (root.rstrip("/") or "/").rsplit("/", 1)[-1]
    return _COPY_SUFFIX.sub("", name).casefold()


def same_drive_warnings(roots: Sequence[str], pool: str | None) -> list[str]:
    """Warnings for indexed roots of one pool that look like the same drive.

    "Storage 1B 4TB" and "Storage 1B 4TB (2)" are usually one drive mounted under two
    labels: both indexes count, so deleted files may linger in the older one.
    """
    seen: dict[str, str] = {}
    out = []
    what = f"pool {pool}" if pool else "these folders"
    for r in roots:
        label = drive_label(r)
        if label in seen and seen[label] != r:
            out.append(
                f"{what}: {seen[label]} and {r} look like the same drive (labels differ only "
                "by case or a trailing \" (N)\"); forget the stale one with `dpf drives --forget`"
            )
        else:
            seen.setdefault(label, r)
    return out
