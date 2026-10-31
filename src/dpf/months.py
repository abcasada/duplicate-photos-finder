"""Photos and videos by month: how much media each month of a pool holds.

Each set of duplicates counts once (its earliest file), so a month shows what was shot
then, not how many copies exist. The date is the camera's capture time (EXIF
DateTimeOriginal) when `dpf exif` has cached it, else the file's modified time, which a
copy usually keeps but an edit or re-save changes. With [categories], only personal
media counts. Dates before 1990 or after next year go to "unknown".
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from dpf import media
from dpf.categories import OTHER, Categories
from dpf.index import Index
from dpf.models import DuplicateGroup
from dpf.report import human_bytes, plural

UNKNOWN = "unknown"
FIRST_YEAR = 1990
_TAKEN = re.compile(r"(\d{4})[:-](\d{2})")


@dataclass(slots=True)
class Month:
    month: str  # "YYYY-MM" or UNKNOWN
    photos: int = 0
    photo_bytes: int = 0
    videos: int = 0
    video_bytes: int = 0

    @property
    def files(self) -> int:
        return self.photos + self.videos

    @property
    def bytes(self) -> int:
        return self.photo_bytes + self.video_bytes


@dataclass(slots=True)
class PoolMonths:
    pool: str
    personal_only: bool
    months: list[Month] = field(default_factory=list)  # every month from first to last
    unknown: Month = field(default_factory=lambda: Month(UNKNOWN))
    from_exif: int = 0  # files dated by their capture time
    from_mtime: int = 0  # files dated by their modified time

    @property
    def total(self) -> Month:
        t = Month("total")
        for m in [*self.months, self.unknown]:
            t.photos += m.photos
            t.photo_bytes += m.photo_bytes
            t.videos += m.videos
            t.video_bytes += m.video_bytes
        return t

    def to_json(self) -> dict:
        return {
            "pool": self.pool,
            "personal_only": self.personal_only,
            "from_exif": self.from_exif,
            "from_mtime": self.from_mtime,
            "months": [_month_json(m) for m in [*self.months, self.unknown] if m.files],
        }


def _month_json(m: Month) -> dict:
    return {"month": m.month, "photos": m.photos, "photo_bytes": m.photo_bytes,
            "videos": m.videos, "video_bytes": m.video_bytes}  # fmt: skip


def _month(taken: str | None, mtime: float, last_year: int) -> tuple[str, bool]:
    """("YYYY-MM" or UNKNOWN, dated by EXIF)."""
    if taken:
        m = _TAKEN.match(taken)
        if m and FIRST_YEAR <= int(m.group(1)) <= last_year and 1 <= int(m.group(2)) <= 12:
            return f"{m.group(1)}-{m.group(2)}", True
    try:
        t = time.localtime(mtime)
    except (OverflowError, OSError, ValueError):
        return UNKNOWN, False
    if FIRST_YEAR <= t.tm_year <= last_year:
        return f"{t.tm_year:04d}-{t.tm_mon:02d}", False
    return UNKNOWN, False


def _span(first: str, last: str) -> list[str]:
    y, m = map(int, first.split("-"))
    end = tuple(map(int, last.split("-")))
    out = []
    while (y, m) <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_totals(
    idx: Index,
    pool: str,
    roots: Sequence[str],
    groups: Sequence[DuplicateGroup],
    cats: Categories | None = None,
    *,
    now: float | None = None,
) -> PoolMonths:
    """Media of `roots` by month, each duplicate group (of this pool's search) once."""
    copies: set[tuple[str, str]] = set()
    for g in groups:
        keep = min(g.files, key=lambda f: (f.mtime, f.dir, f.name))
        copies.update((f.dir, f.name) for f in g.files if f is not keep)
    last_year = time.localtime(time.time() if now is None else now).tm_year + 1
    out = PoolMonths(pool, personal_only=bool(cats))
    by_month: dict[str, Month] = {}
    for dir, name, size, mtime, kind, taken in idx.media_dates(roots):
        if (dir, name) in copies or (cats and cats.of(dir) == OTHER):
            continue
        key, exif = _month(taken, mtime, last_year)
        if exif:
            out.from_exif += 1
        else:
            out.from_mtime += 1
        m = out.unknown if key == UNKNOWN else by_month.setdefault(key, Month(key))
        if kind == media.VIDEO:
            m.videos += 1
            m.video_bytes += size
        else:
            m.photos += 1
            m.photo_bytes += size
    if by_month:
        out.months = [by_month.get(k) or Month(k) for k in _span(min(by_month), max(by_month))]
    return out


def format_months(pools: Sequence[PoolMonths]) -> str:
    """The text section: one line per month that holds media, per pool."""
    lines = [
        "Photos and videos by the month they were taken: the camera's capture time when",
        "`dpf exif` has read it, else the file's modified time. Each set of duplicates",
        "counts once. The xlsx has the same table with a chart.",
    ]
    for p in pools:
        t = p.total
        what = "personal photos/videos" if p.personal_only else "photos/videos"
        lines += [
            "",
            f"{f'Pool {p.pool}: ' if p.pool else ''}{plural(t.files, 'file')}, "
            f"{human_bytes(t.bytes)} of {what} (dated by capture time: "
            f"{p.from_exif:,}; by file date: {p.from_mtime:,})",
            f"  {'Month':<8} {'Photos':>9} {'Size':>10} {'Videos':>8} {'Size':>10} {'Total':>10}",
        ]
        for m in [*p.months, p.unknown]:
            if m.files:
                lines.append(
                    f"  {m.month:<8} {m.photos:>9,} {human_bytes(m.photo_bytes):>10} "
                    f"{m.videos:>8,} {human_bytes(m.video_bytes):>10} {human_bytes(m.bytes):>10}"
                )
    return "\n".join(lines) + "\n"
