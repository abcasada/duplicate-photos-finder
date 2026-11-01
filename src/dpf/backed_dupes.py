"""Backed-up sequences with extra copies: "Duplicated in A and B".

For a pool and its backup pool, the sequences that exist at least once in each (backed
up, as they should be) but more than once in at least one of them. Cleaning up keeps one
copy per pool, so the space saved is (copies in A - 1) + (copies in B - 1) times the
size of one copy.

Unlike the per-pool duplicate search, the files of both pools are matched together here
(same rules: mode 1, or mode 1b with --mode 1b). A row is a run of consecutive files in
one folder (natural order) whose matches sit in exactly the same set of folders, with
the same number of copies in each; every folder holding the run is listed on the row.
A run that only partly overlaps another folder is split, so each row is exact.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from dpf.grouping import natural_key
from dpf.index import Index
from dpf.models import DuplicateGroup
from dpf.report import human_bytes, plural

BOTH = "both"


@dataclass(frozen=True, slots=True)
class Spot:
    """One folder holding the sequence, with how many copies of each file it has."""

    dir: str
    pool: str
    copies: int


@dataclass(frozen=True, slots=True)
class BackedRun:
    first: str  # first and last file name, in the run's first folder
    last: str
    files: int
    bytes: int  # one copy of the run
    spots: tuple[Spot, ...]  # every folder holding it: pool A first, then B
    copies_a: int
    copies_b: int

    @property
    def saved(self) -> int:
        return self.bytes * (self.copies_a - 1 + self.copies_b - 1)

    @property
    def duplicated_in(self) -> str:
        if self.copies_a > 1 and self.copies_b > 1:
            return BOTH
        return "A" if self.copies_a > 1 else "B"


@dataclass(slots=True)
class BackedDupes:
    pool: str  # the primary pool (A)
    backup: str  # its backup pool (B)
    basis: str
    runs: list[BackedRun]

    def label_of(self, where: str) -> str:
        return {"A": self.pool, "B": self.backup}.get(where, where)

    @property
    def title(self) -> str:
        return f"Duplicated in {self.pool} and {self.backup}"

    def totals(self, where: str | None = None) -> tuple[int, int, int]:
        """(rows, files, bytes saved), for one "Duplicated in" value or all."""
        rows = [r for r in self.runs if where is None or r.duplicated_in == where]
        return len(rows), sum(r.files for r in rows), sum(r.saved for r in rows)


def find_backed_dupes(
    index: Index,
    groups: Iterable[DuplicateGroup],
    pool_of: Callable[[str], str],
    pool: str,
    backup: str,
    basis: str,
) -> BackedDupes:
    """Rows from `groups`, matched over both pools' roots together."""
    # Each kept group: its signature (folders and copies per folder) and size per copy.
    sig_of: dict[tuple[str, str], tuple] = {}  # (dir, name) -> signature, for every member
    size_of: dict[tuple, dict[tuple[str, str], int]] = {}
    for g in groups:
        per_dir = Counter(f.dir for f in g.files)
        pools = Counter()
        for d, n in per_dir.items():
            pools[pool_of(d)] += n
        a, b = pools.get(pool, 0), pools.get(backup, 0)
        if a < 1 or b < 1 or (a < 2 and b < 2):
            continue
        sig = tuple(sorted(per_dir.items()))
        anchor = min(g.files, key=lambda f: (f.dir, natural_key(f.name)))
        for f in g.files:
            sig_of[(f.dir, f.name)] = sig
        size_of.setdefault(sig, {})[(anchor.dir, anchor.name)] = anchor.size

    runs: list[BackedRun] = []
    for sig, anchors in size_of.items():
        first_dir = sig[0][0]
        names = sorted(
            (f.name for f in index.list_dir(first_dir)), key=lambda n: (natural_key(n), n)
        )
        run: list[str] = []
        for name in names + [None]:
            if name is not None and sig_of.get((first_dir, name)) == sig:
                if (first_dir, name) in anchors:
                    run.append(name)
                continue  # a second copy of a member in this folder: neither in nor out
            if run:
                runs.append(_row(sig, run, anchors, first_dir, pool_of, pool, backup))
                run = []
    runs.sort(key=lambda r: (-r.saved, r.spots[0].dir, natural_key(r.first)))
    return BackedDupes(pool, backup, basis, runs)


def _row(sig, run, anchors, first_dir, pool_of, pool, backup) -> BackedRun:
    spots = [Spot(d, pool_of(d), n) for d, n in sig]
    spots.sort(key=lambda s: (s.pool != pool, s.pool, natural_key(s.dir), s.dir))
    return BackedRun(
        first=run[0], last=run[-1], files=len(run),
        bytes=sum(anchors[(first_dir, n)] for n in run), spots=tuple(spots),
        copies_a=sum(s.copies for s in spots if s.pool == pool),
        copies_b=sum(s.copies for s in spots if s.pool == backup),
    )  # fmt: skip


def format_backed(found: BackedDupes, *, top: int) -> str:
    """The text section: totals by where the extra copies are, then the largest rows."""
    a, b = found.pool, found.backup
    lines = [
        f"Sequences backed up (in pool {a} and in pool {b}) that also have extra copies "
        f"inside {a}, {b} or both. Space saved keeps one copy in each pool.",
        f"Matched on {found.basis}, both pools together; not contents.",
        "",
    ]
    for where, label in ((BOTH, "both"), ("A", f"{a} only"), ("B", f"{b} only")):
        rows, files, saved = found.totals(where)
        lines.append(f"Extra copies in {label + ':':<10} {plural(rows, 'sequence')}, "
                     f"{plural(files, 'file')}, {human_bytes(saved)} to save")  # fmt: skip
    rows, files, saved = found.totals()
    lines.append(f"{'All:':<25} {plural(rows, 'sequence')}, {plural(files, 'file')}, "
                 f"{human_bytes(saved)} to save")  # fmt: skip
    width = len(str(min(len(found.runs), top)))
    for n, r in enumerate(found.runs[:top], 1):
        what = r.first if r.files == 1 else f"{r.first} … {r.last}"
        where = found.label_of(r.duplicated_in)
        lines += ["", f"[{n:>{width}}] {human_bytes(r.saved):>9} saved  {what}  "
                      f"({plural(r.files, 'file')}, {human_bytes(r.bytes)} each copy; "
                      f"{r.copies_a}× in {a}, {r.copies_b}× in {b}; extra in {where})"]  # fmt: skip
        for s in r.spots:
            times = f"  ({s.copies} copies)" if s.copies > 1 else ""
            lines.append(f"       {s.pool}: {s.dir}{times}")
    if len(found.runs) > top:
        lines += ["", f"… and {len(found.runs) - top:,} more sequences (see report.xlsx)"]
    return "\n".join(lines) + "\n"


def backed_json(found: BackedDupes) -> dict:
    return {
        "pool": found.pool, "backup": found.backup, "basis": found.basis,
        "runs": [
            {"first": r.first, "last": r.last, "files": r.files, "bytes": r.bytes,
             "saved": r.saved, "copies": {found.pool: r.copies_a, found.backup: r.copies_b},
             "duplicated_in": found.label_of(r.duplicated_in),
             "folders": [{"dir": s.dir, "pool": s.pool, "copies": s.copies} for s in r.spots]}
            for r in found.runs
        ],
    }  # fmt: skip


def spots_by_pool(r: BackedRun, pool: str) -> list[Spot]:
    return [s for s in r.spots if s.pool == pool]

