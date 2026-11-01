"""Personal files in only one pool: the focus of a run over a pool and its backup.

Taken from the two backup-coverage checks (A not in B, B not in A): their missing files,
keeping personal media only when [categories] is set. A file listed here has no copy
anywhere in the other pool under the run's matching rules, so it exists in one pool
only: in A it is not backed up, in B it is missing from the primary drives. Rows are
folders, largest first, with the days the files date from and how many have a same-name
file of nearly the same size in the other pool (likely re-saved there, but unproven).
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from dpf import media
from dpf.categories import OTHER, Categories
from dpf.coverage import Coverage
from dpf.dumps import file_days, format_days
from dpf.index import Index
from dpf.models import FileRecord
from dpf.report import human_bytes, plural


@dataclass(frozen=True, slots=True)
class OnlyFolder:
    dir: str
    pool: str  # the pool holding the files
    other: str  # the pool missing them
    drive: str  # the indexed root (drive) holding the folder
    photos: int
    videos: int
    bytes: int
    total: int  # compared photos/videos directly in the folder
    near: int  # of these, files with a same-name, near-size file in the other pool
    days: tuple[date, ...] = ()

    @property
    def files(self) -> int:
        return self.photos + self.videos

    @property
    def dates(self) -> str:
        return format_days(self.days)


@dataclass(slots=True)
class OnlySide:
    pool: str
    other: str
    files: int = 0
    bytes: int = 0
    near_bytes: int = 0
    drives: Counter = field(default_factory=Counter)  # drive -> bytes
    drive_files: Counter = field(default_factory=Counter)


@dataclass(slots=True)
class OnlyInOne:
    personal_only: bool
    basis: str
    sides: list[OnlySide]
    folders: list[OnlyFolder]  # both sides, largest first

    @property
    def title(self) -> str:
        return ("Personal files" if self.personal_only else "Files") + " in only one pool"

    def to_json(self) -> dict:
        return {
            "personal_only": self.personal_only,
            "basis": self.basis,
            "pools": [{"pool": s.pool, "missing_in": s.other, "files": s.files,
                       "bytes": s.bytes, "near_bytes": s.near_bytes,
                       "drives": dict(s.drives)} for s in self.sides],
            "folders": [{"dir": f.dir, "pool": f.pool, "drive": f.drive, "photos": f.photos,
                         "videos": f.videos, "bytes": f.bytes, "total": f.total,
                         "near": f.near, "dates": f.dates} for f in self.folders],
        }  # fmt: skip


def _root_of(dir: str, roots: Sequence[str]) -> str:
    best = ""
    for r in roots:
        if (dir == r or dir.startswith(r.rstrip("/") + "/")) and len(r) > len(best):
            best = r
    return best


def build_only_in_one(
    index: Index, coverages: Sequence[Coverage], cats: Categories, basis: str
) -> OnlyInOne | None:
    """From the coverage checks of a run; None when no check ran both ways."""
    if len(coverages) < 2:
        return None
    last_year = time.localtime().tm_year + 1
    out = OnlyInOne(bool(cats), basis, [], [])
    for cov in coverages:
        side = OnlySide(cov.pool or "", cov.other)
        by_dir: dict[str, list[FileRecord]] = {}
        for f in cov.missing:
            if cats and cats.of(f.dir) == OTHER:
                continue
            by_dir.setdefault(f.dir, []).append(f)
        for d, files in by_dir.items():
            kinds = Counter(f.kind for f in files)
            nbytes = sum(f.size for f in files)
            near = [f for f in files if os.path.join(f.dir, f.name) in cov.near]
            drive = _root_of(d, cov.roots)
            total = sum(1 for r in index.list_dir(d) if r.size > 0 and r.kind != media.OTHER)
            out.folders.append(OnlyFolder(
                d, side.pool, side.other, drive, kinds[media.IMAGE], kinds[media.VIDEO],
                nbytes, total, len(near), file_days(files, index.taken_in(d), last_year),
            ))  # fmt: skip
            side.files += len(files)
            side.bytes += nbytes
            side.near_bytes += sum(f.size for f in near)
            side.drives[drive] += nbytes
            side.drive_files[drive] += len(files)
        out.sides.append(side)
    out.folders.sort(key=lambda f: (-f.bytes, f.dir))
    return out


def only_lines(only: OnlyInOne) -> list[str]:
    """The totals: per pool, then per drive."""
    lines = []
    for s in only.sides:
        lines.append(f"Only in pool {s.pool} (no copy anywhere in {s.other}): "
                     f"{plural(s.files, 'file')}, {human_bytes(s.bytes)}")  # fmt: skip
        if s.near_bytes:
            lines.append(f"  of which {human_bytes(s.near_bytes)} have a same-name file "
                         f"within 64 kB in size in {s.other}: likely re-saved, unproven")  # fmt: skip
        for drive, nbytes in s.drives.most_common():
            name = os.path.basename(drive) or drive
            lines.append(f"  {name:<24} {human_bytes(nbytes):>9}  "
                         f"({plural(s.drive_files[drive], 'file')})")  # fmt: skip
    return lines


def format_only(only: OnlyInOne, *, top: int) -> str:
    """The text section: totals, then the largest folders of both pools."""
    what = "personal photos and videos" if only.personal_only else "photos and videos"
    lines = [
        f"The {what} that exist in one pool only: in the primary pool they have no "
        "backup, in the backup pool they are missing from the primary drives.",
        f"Matched on {only.basis}, not contents.",
        "",
        *only_lines(only),
    ]
    width = len(str(min(len(only.folders), top)))
    for n, f in enumerate(only.folders[:top], 1):
        what = plural(f.photos, "photo") + (f", {plural(f.videos, 'video')}" if f.videos else "")
        tail = f"only in {f.pool}; {what} of {f.total:,} in the folder"
        if f.near:
            tail += f"; {f.near:,} likely re-saved in {f.other} (same name, within 64 kB)"
        lines += ["", f"[{n:>{width}}] {human_bytes(f.bytes):>9}  {f.dir}   ({tail})"]
        if f.days:
            lines.append(f"{'':>{width + 13}}dates: {f.dates}")
    if len(only.folders) > top:
        lines += ["", f"… and {len(only.folders) - top:,} more folders (see report.xlsx)"]
    return "\n".join(lines) + "\n"
