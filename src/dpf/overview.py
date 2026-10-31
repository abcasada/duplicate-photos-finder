"""Drive overview and personal totals for the top of a report.

Per drive: capacity, used and free space, photos and videos, media size (personal and
other with [categories]), the rest of the used space, and how much personal media has no
copy on any other drive. Per pool and across pools: personal media in total and unique
(each set of duplicates counted once).

Everything is computed from the duplicate groups of each pool and from the coverage
checks between pools, so it inherits their basis (similar name, size and date, not
contents). The figures lean the safe way: a re-saved copy counts as a separate file, so
"unique" may be a little high, and a file counts as an "only copy" unless a duplicate of
it on another drive was actually matched.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from dpf import media
from dpf.categories import OTHER, PERSONAL, Categories, of_dirs, tally
from dpf.coverage import Coverage, same_drive_warnings
from dpf.index import Index
from dpf.models import SPACE_MANUAL, DuplicateGroup
from dpf.report import human_bytes, plural

Key = tuple[str, str]  # (dir, name)


@dataclass(slots=True)
class DriveLine:
    root: str
    pool: str
    scanned_at: float | None
    capacity: int | None  # None: not recorded, or a folder rather than a drive
    used: int | None
    manual: bool  # space entered by hand (dpf space --set)
    folder: bool  # a folder inside a drive, so no space of its own
    photos: int
    videos: int
    media_bytes: int
    personal_bytes: int | None  # None without [categories]
    other_bytes: int | None
    sole_files: int | None = None  # None without a coverage check for its pool
    sole_bytes: int | None = None
    sole_similar: int | None = None  # bytes of those with a same-name, near-size file there

    @property
    def free(self) -> int | None:
        if self.capacity is None or self.used is None:
            return None
        return self.capacity - self.used

    @property
    def not_media(self) -> int | None:
        """Used space that is not indexed media: other files and filesystem overhead."""
        return None if self.used is None else max(self.used - self.media_bytes, 0)


@dataclass(slots=True)
class PoolPersonal:
    pool: str
    files: int
    bytes: int
    unique_files: int
    unique_bytes: int


@dataclass(slots=True)
class Overview:
    drives: list[DriveLine]
    categories: bool  # personal/other split from [categories]
    basis: str = "similar name+size+mtime"  # how files were matched
    pools: list[PoolPersonal] = field(default_factory=list)
    all_unique: tuple[int, int] | None = None  # (files, bytes) across exactly two pools
    sole: tuple[int, int] | None = None  # (files, bytes) with no copy on any other drive
    sole_similar: int = 0  # bytes of those with a same-name, near-size file in the other pool

    def to_json(self) -> dict:
        return {
            "drives": [{**asdict(d), "free": d.free, "not_media": d.not_media} for d in self.drives],
            "categories": self.categories,
            "basis": self.basis,
            "personal": [asdict(p) for p in self.pools],
            "all_unique": _pair(self.all_unique),
            "only_copy": _pair(self.sole),
            "only_copy_similar_name_bytes": self.sole_similar,
        }


def _pair(value: tuple[int, int] | None) -> dict | None:
    return None if value is None else {"files": value[0], "bytes": value[1]}


@dataclass(slots=True)
class PoolInput:
    """One pool of the run: its roots and the duplicate groups found within it."""

    name: str
    roots: list[str]
    groups: Sequence[DuplicateGroup]


def build_overview(
    idx: Index,
    pools: Sequence[PoolInput],
    coverages: Sequence[Coverage],
    cats: Categories,
    basis: str = "similar name+size+mtime",
) -> Overview:
    """The overview of every root of `pools`; coverage figures where a check ran.

    Personal totals need every group of each pool (not a --category subset). Without
    [categories] every file counts as personal. Non-media files (--all-files) are left
    out. Pass no `coverages` when matching was loosened (--ignore-mtime/--ignore-size):
    only-copy figures would then claim copies that are different files.
    """
    category = cats if cats else None

    def personal(dirs) -> bool:
        return of_dirs(category, dirs) == PERSONAL

    root_stats = {r.root: r for r in idx.roots()}
    drives: list[DriveLine] = []
    lines: dict[str, DriveLine] = {}
    for p in pools:
        for root in p.roots:
            kinds = idx.kind_totals([root])
            stats = root_stats.get(root)
            space = stats.space if stats else None
            drive = space is not None and space.is_mount is not False
            split = tally(idx, [root], cats) if cats else None
            line = DriveLine(
                root=root, pool=p.name, scanned_at=stats and stats.scanned_at,
                capacity=space.capacity if drive else None,
                used=space.used if drive else None,
                manual=drive and space.source == SPACE_MANUAL,
                folder=space is not None and space.is_mount is False,
                photos=kinds.get(media.IMAGE, (0, 0))[0],
                videos=kinds.get(media.VIDEO, (0, 0))[0],
                media_bytes=sum(b for k, (_, b) in kinds.items() if k != media.OTHER),
                personal_bytes=split[PERSONAL][1] if split else None,
                other_bytes=split[OTHER][1] if split else None,
            )  # fmt: skip
            drives.append(line)
            lines[root] = line

    overview = Overview(drives, categories=bool(cats), basis=basis)
    groups_of = {id(p): [g for g in p.groups if g.files[0].kind != media.OTHER] for p in pools}
    by_roots = {tuple(p.roots): p for p in pools}
    checked = sole_files = sole_size = 0
    for p in pools:
        files, nbytes = _personal_files(idx, p.roots, cats)
        in_group: dict[Key, DuplicateGroup] = {}
        g_files = g_bytes = 0
        for g in groups_of[id(p)]:
            for f in g.files:
                in_group[(f.dir, f.name)] = g
                if personal([f.dir]):
                    g_files += 1
                    g_bytes += f.size
        mine = [g for g in groups_of[id(p)] if personal({f.dir for f in g.files})]
        unique_files = files - g_files + len(mine)
        unique_bytes = nbytes - g_bytes + sum(_size(g) for g in mine)
        overview.pools.append(PoolPersonal(p.name, files, nbytes, unique_files, unique_bytes))

        cov = next((c for c in coverages if c.roots == p.roots), None)
        # Two roots of one pool that look like one drive would vouch for each other.
        if cov is None or same_drive_warnings(p.roots, p.name):
            continue
        sole: Counter[str] = Counter()
        sole_bytes: Counter[str] = Counter()
        similar: Counter[str] = Counter()
        counted: set[int] = set()
        for f in cov.missing:
            if f.kind == media.OTHER or not personal([f.dir]):
                continue
            g = in_group.get((f.dir, f.name))
            if g is None:
                root, size, members = _root_of(f.dir, p.roots), f.size, (f,)
            elif id(g) not in counted:
                counted.add(id(g))
                roots = {_root_of(m.dir, p.roots) for m in g.files}
                if len(roots) != 1 or not personal({m.dir for m in g.files}):
                    continue
                root, size, members = roots.pop(), _size(g), g.files
            else:
                continue
            sole[root] += 1
            sole_bytes[root] += size
            if any(m.path in cov.near for m in members):
                similar[root] += size
        for root in p.roots:
            lines[root].sole_files = sole[root]
            lines[root].sole_bytes = sole_bytes[root]
            lines[root].sole_similar = similar[root]
        checked += 1
        sole_files += sum(sole.values())
        sole_size += sum(sole_bytes.values())
        overview.sole_similar += sum(similar.values())

        # Across two pools: this pool's unique files plus the other pool's duplicate
        # sets (or single files) none of whose files has a copy in this pool.
        if len(pools) != 2 or p is not pools[0]:
            continue
        other = by_roots.get(tuple(cov.other_roots))
        back = next((c for c in coverages if c.roots == cov.other_roots), None)
        if other is None or back is None:
            continue
        only_there = {(f.dir, f.name) for f in back.missing}
        add_files = add_bytes = 0
        other_groups: dict[Key, DuplicateGroup] = {}
        for g in groups_of[id(other)]:
            for f in g.files:
                other_groups[(f.dir, f.name)] = g
        seen: set[int] = set()
        for f in back.missing:
            if f.kind == media.OTHER:
                continue
            g = other_groups.get((f.dir, f.name))
            if g is None:
                if personal([f.dir]):
                    add_files += 1
                    add_bytes += f.size
            elif id(g) not in seen:
                seen.add(id(g))
                every = all((m.dir, m.name) in only_there for m in g.files)
                if every and personal({m.dir for m in g.files}):
                    add_files += 1
                    add_bytes += _size(g)
        overview.all_unique = (unique_files + add_files, unique_bytes + add_bytes)
    if checked == len(pools):  # an only-copy total needs every pool checked
        overview.sole = (sole_files, sole_size)
    return overview


def _personal_files(idx: Index, roots: list[str], cats: Categories) -> tuple[int, int]:
    """(files, bytes) of personal media under `roots` (all media without categories)."""
    if cats:
        return tuple(tally(idx, roots, cats)[PERSONAL])  # type: ignore[return-value]
    kinds = idx.kind_totals(roots)
    return (
        sum(n for k, (n, _) in kinds.items() if k != media.OTHER),
        sum(b for k, (_, b) in kinds.items() if k != media.OTHER),
    )


def _size(g: DuplicateGroup) -> int:
    return max(f.size for f in g.files)


def _root_of(dir: str, roots: Sequence[str]) -> str:
    best = ""
    for r in roots:
        if (dir == r or dir.startswith(r.rstrip("/") + "/")) and len(r) > len(best):
            best = r
    return best


# --- text -------------------------------------------------------------------------


def _cell(n: int | None, manual: bool = False) -> str:
    if n is None:
        return "—"
    return human_bytes(n) + ("*" if manual else "")


def format_overview(ov: Overview) -> str:
    """The Drives section of report.txt."""
    names = [os.path.basename(d.root) or d.root for d in ov.drives]
    if len(set(names)) != len(names):
        names = [d.root for d in ov.drives]
    sole = any(d.sole_files is not None for d in ov.drives)
    header = ["Drive", "Pool", "Capacity", "Used", "Free", "Photos", "Videos", "Media"]
    header += ["Personal", "Other"] if ov.categories else []
    header += ["Not media"] + (["Only copy", "Name match"] if sole else []) + ["Scanned"]
    rows = [header]

    def row(name: str, pool: str, lines: Sequence[DriveLine], date: str) -> list[str]:
        spaced = [d for d in lines if d.capacity is not None]
        partial = len(spaced) != len(lines)
        manual = any(d.manual for d in lines)

        def total(attr: str) -> int | None:
            values = [getattr(d, attr) for d in spaced]
            values = [v for v in values if v is not None]
            return sum(values) if values else None

        def plus(text: str) -> str:
            return text + "+" if partial and text != "—" else text

        cells = [name, pool, plus(_cell(total("capacity"))), plus(_cell(total("used"), manual)),
                 plus(_cell(total("free"))), f"{sum(d.photos for d in lines):,}",
                 f"{sum(d.videos for d in lines):,}",
                 human_bytes(sum(d.media_bytes for d in lines))]  # fmt: skip
        if ov.categories:
            cells += [human_bytes(sum(d.personal_bytes or 0 for d in lines)),
                      human_bytes(sum(d.other_bytes or 0 for d in lines))]  # fmt: skip
        cells.append(plus(_cell(total("not_media"))))
        if sole:
            for attr in ("sole_bytes", "sole_similar"):
                counts = [getattr(d, attr) for d in lines if getattr(d, attr) is not None]
                cells.append(human_bytes(sum(counts)) if counts else "—")
        cells.append(date)
        return cells

    pools: dict[str, list[DriveLine]] = {}
    for name, d in zip(names, ov.drives):
        date = d.scanned_at and _date(d.scanned_at) or "?"
        rows.append(row(name, d.pool, [d], date))
        pools.setdefault(d.pool, []).append(d)
    if len(ov.drives) > 1:
        rows.append(None)  # type: ignore[arg-type]
        for pool, lines in pools.items():
            if len(lines) > 1:
                rows.append(row(f"Pool {pool} total", pool, lines, ""))
        if len(pools) > 1:
            rows.append(row("All drives", "", ov.drives, ""))

    widths = [max(len(r[c]) for r in rows if r) for c in range(len(header))]
    left = {0, 1, len(header) - 1}
    out = []
    for r in rows:
        if r is None:
            out.append("")
            continue
        cells = [v.ljust(w) if c in left else v.rjust(w) for c, (v, w) in enumerate(zip(r, widths))]
        out.append("  ".join(cells).rstrip())

    notes = ["Sizes in decimal units, as on drive labels. Media = indexed photos and videos."]
    notes.append("Not media = used space minus media: other files and filesystem overhead.")
    if any(d.manual for d in ov.drives):
        notes.append("* entered by hand (dpf space --set).")
    if any(d.capacity is None for d in ov.drives):
        notes.append("— = no space recorded (plug the drive in and run `dpf space --missing`); "
                     "+ = total without those drives.")  # fmt: skip
    if sole:
        what = "personal media" if ov.categories else "media"
        notes.append(f"Only copy = {what} with no copy on any other drive (in this pool or "
                     "its backup pool): what would be lost with that drive.")  # fmt: skip
        notes.append("Name match = the part of Only copy with a same-name file in the other "
                     "pool within 64 kB in size: likely the same photo re-saved with new "
                     "metadata, but not proven (mode 1b/2 will tell). Count it as at risk "
                     "until then.")  # fmt: skip
        dates = sorted(d.scanned_at for d in ov.drives if d.scanned_at is not None)
        if dates:
            notes.append(f"Copies are counted as of each drive's last scan (oldest {_date(dates[0])}"
                         "); files deleted since then still count, so rescan a drive after "
                         "deleting from it.")  # fmt: skip
    out += [""] + notes + [""] + personal_lines(ov)
    return "\n".join(out) + "\n"


def personal_lines(ov: Overview) -> list[str]:
    """The personal totals under the drive table."""
    if not ov.pools:
        return []
    what = "Personal photos/videos" if ov.categories else "Photos/videos"
    lines = [f"{what}:" + ("  (folders under [categories] other left out)" if ov.categories
                            else "  (no [categories] set, so all media counts)")]  # fmt: skip
    width = max(len(f"Pool {p.pool}:") for p in ov.pools)
    width = max(width, len("All drives:") if ov.all_unique else 0, len("Only copy:")) + 2
    for p in ov.pools:
        label = (f"Pool {p.pool}:" if p.pool else "Total:").ljust(width)
        lines.append(
            f"  {label}{plural(p.files, 'file')}, {human_bytes(p.bytes)} on its drives; "
            f"unique: {plural(p.unique_files, 'file')}, {human_bytes(p.unique_bytes)} "
            "(one copy of each duplicate)"
        )
    if ov.all_unique is not None:
        files, nbytes = ov.all_unique
        names = " and ".join(p.pool for p in ov.pools)
        lines.append(
            f"  {'All drives:'.ljust(width)}unique: {plural(files, 'file')}, "
            f"{human_bytes(nbytes)} (a file found in both {names} counts once)"
        )
    if ov.sole is not None:
        files, nbytes = ov.sole
        lines.append(
            f"  {'Only copy:'.ljust(width)}{plural(files, 'file')}, {human_bytes(nbytes)} "
            "exist on one drive only (per drive: Only copy column)"
        )
        if ov.sole_similar:
            lines.append(
                f"  {''.ljust(width)}of which {human_bytes(ov.sole_similar)} have a same-name "
                "file within 64 kB in size in the other pool: likely re-saved copies, unproven"
            )
    lines.append(
        f"  Matched on {ov.basis}, not contents: a re-saved or renamed copy counts "
        "as another file, so unique and only-copy figures may be somewhat high."
    )
    return lines


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))
