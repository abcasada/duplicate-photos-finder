"""Unsorted dumps: folders of a "dump" area whose photos never made it into a sorted folder.

The optional `[dumps]` table of the pools file turns the check on:

    [dumps]
    dump = "dump"            # a folder is a dump when its path contains this
    sorted = "photography"   # sorted: the path contains this, and not the dump word
    min_photos = 100         # report a dump folder from this many unsorted photos

Words match anywhere in the folder's full path, ignoring case ("_Dumps/2026-09-17" is a
dump; "Photography/2021/Hawaii" is sorted, "Photography/_Dumps/x" is not). A photo of a
dump folder counts as sorted when mode 1 matches it to a file in any sorted folder, on
any indexed drive of any pool (similar name, size and date; contents are not compared).
Like backup coverage, this reports files missing elsewhere, never duplicates.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from dpf import media
from dpf.coverage import _runs, find_missing
from dpf.grouping import Run
from dpf.index import Index, Words
from dpf.pools import PoolError, default_pools_path
from dpf.report import human_bytes, plural, run_lines

MIN_PHOTOS = 100


@dataclass(frozen=True, slots=True)
class DumpConfig:
    dump: str
    sorted: str
    min_photos: int = MIN_PHOTOS


@dataclass(frozen=True, slots=True)
class DumpFolder:
    """A dump folder with at least min_photos photos that no sorted folder holds."""

    dir: str
    pool: str
    photos: int  # unsorted photos
    videos: int  # unsorted videos
    bytes: int  # of the unsorted photos and videos
    total: int  # compared (non-empty) photos and videos in the folder
    similar: int  # unsorted files with a same-name file of near size in a sorted folder
    runs: tuple[Run, ...] = ()

    @property
    def unsorted(self) -> int:
        return self.photos + self.videos


@dataclass(slots=True)
class DumpCheck:
    config: DumpConfig
    basis: str
    folders: list[DumpFolder] = field(default_factory=list)
    dump_folders: int = 0  # dump folders with compared files
    dump_files: int = 0  # compared files in dump folders
    unsorted_files: int = 0  # in every dump folder, listed or not
    unsorted_bytes: int = 0

    @property
    def label(self) -> str:
        return f"{self.config.dump.title()}s not in {self.config.sorted.title()}"


def check_dumps(
    index: Index,
    roots: Sequence[str],
    sorted_roots: Sequence[str],
    config: DumpConfig,
    pool_of: Callable[[str], str],
    *,
    basis: str,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> DumpCheck:
    """Dump folders under `roots` holding at least min_photos photos not in a sorted folder
    under `sorted_roots` (which may overlap `roots`: the words keep the sides apart)."""
    missing, totals, _, _, near = find_missing(
        index, roots, sorted_roots, mtime_tolerance=mtime_tolerance, ignore_size=ignore_size,
        ignore_mtime=ignore_mtime, exact_names=exact_names, hour_shift=hour_shift,
        words=Words((config.dump,)), other_words=Words((config.sorted,), (config.dump,)),
    )  # fmt: skip
    check = DumpCheck(config, basis)
    check.dump_folders = len(totals)
    check.dump_files = sum(totals.values())
    check.unsorted_files = len(missing)
    check.unsorted_bytes = sum(f.size for f in missing)
    by_dir: dict[str, list] = {}
    for f in missing:
        by_dir.setdefault(f.dir, []).append(f)
    for d, files in by_dir.items():
        kinds = Counter(f.kind for f in files)
        if kinds[media.IMAGE] < config.min_photos:
            continue
        names = {f.name for f in files}
        check.folders.append(
            DumpFolder(
                d, pool_of(d), kinds[media.IMAGE], kinds[media.VIDEO],
                sum(f.size for f in files), totals[d],
                sum(f.path in near for f in files),
                _runs(index.list_dir(d), names if len(names) < totals[d] else None),
            )
        )  # fmt: skip
    check.folders.sort(key=lambda f: (-f.bytes, f.dir))
    return check


def parse_dumps(data: Mapping[str, object], path: str = "pools file") -> DumpConfig | None:
    """The optional [dumps] table of a parsed pools file; None when absent."""
    table = data.get("dumps")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise PoolError(f'{path}: [dumps] must be a table, e.g. dump = "dump"')
    unknown = sorted(set(table) - {"dump", "sorted", "min_photos"})
    if unknown:
        raise PoolError(f"{path}: [dumps] has unknown key {unknown[0]}")
    dump, sorted_, least = table.get("dump"), table.get("sorted"), table.get("min_photos")
    for key, value in (("dump", dump), ("sorted", sorted_)):
        if not isinstance(value, str) or not value.strip():
            raise PoolError(f"{path}: [dumps] {key} must be a word, e.g. {key} = \"{key}\"")
    if dump.casefold() in sorted_.casefold() or sorted_.casefold() in dump.casefold():
        raise PoolError(f"{path}: [dumps] dump and sorted words must differ")
    if least is None:
        least = MIN_PHOTOS
    if not isinstance(least, int) or isinstance(least, bool) or least < 1:
        raise PoolError(f"{path}: [dumps] min_photos must be a positive whole number")
    return DumpConfig(dump.strip(), sorted_.strip(), least)


def load_dumps(path: str | None = None) -> DumpConfig | None:
    """[dumps] from the pools file; None when the file (or the table) is absent."""
    path = path or default_pools_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PoolError(f"cannot read pools file {path}: {e}") from None
    return parse_dumps(data, path)


def format_dumps(check: DumpCheck, *, top: int, max_runs: int) -> str:
    """The dump section of report.txt: totals, then the largest listed folders with runs."""
    c = check.config
    lines = [
        f'Dump folders (path contains "{c.dump}") holding at least {c.min_photos:,} photos '
        f'that are in no sorted folder (path contains "{c.sorted}" but not "{c.dump}") '
        "on any indexed drive.",
        f"Matched on {check.basis}, not contents: a renamed and re-saved copy counts as "
        "unsorted. Camera numbers repeat (every 10,000 shots), so a same-name file in a "
        "sorted folder is usually another photo unless its size is within 64 kB.",
        "",
        f"Checked:   {plural(check.dump_files, 'file')} in "
        f"{plural(check.dump_folders, 'dump folder')}",
        f"Unsorted:  {plural(check.unsorted_files, 'file')}, "
        f"{human_bytes(check.unsorted_bytes)} in all dump folders",
    ]
    listed = check.folders
    files = sum(f.unsorted for f in listed)
    lines.append(
        f"Listed:    {plural(len(listed), 'folder')} with {c.min_photos:,}+ unsorted photos: "
        f"{plural(files, 'file')}, {human_bytes(sum(f.bytes for f in listed))}"
    )
    width = len(str(min(len(listed), top)))
    for n, f in enumerate(listed[:top], 1):
        what = plural(f.photos, "photo") + (f", {plural(f.videos, 'video')}" if f.videos else "")
        tail = f"pool {f.pool}; " if f.pool else ""
        tail += f"{what} unsorted, of {f.total:,} files"
        if f.similar:
            tail += f"; {f.similar:,} likely re-saved there (same name, size within 64 kB)"
        lines += ["", f"[{n:>{width}}] {human_bytes(f.bytes):>9}  {f.dir}   ({tail})"]
        lines += run_lines(f.runs, max_runs, skipped_label="in a sorted folder")
    if len(listed) > top:
        lines += ["", f"… and {len(listed) - top:,} more folders (see report.xlsx / report.json)"]
    return "\n".join(lines) + "\n"


def dumps_json(check: DumpCheck) -> dict:
    return {
        "dump_word": check.config.dump,
        "sorted_word": check.config.sorted,
        "min_photos": check.config.min_photos,
        "basis": check.basis,
        "dump_folders": check.dump_folders,
        "dump_files": check.dump_files,
        "unsorted_files": check.unsorted_files,
        "unsorted_bytes": check.unsorted_bytes,
        "folders": [
            {
                "dir": f.dir, "pool": f.pool, "photos": f.photos, "videos": f.videos,
                "bytes": f.bytes, "total": f.total, "similar": f.similar,
                "runs": [{"first": r.a_first, "last": r.a_last, "files": r.count,
                          "bytes": r.bytes} for r in f.runs],
            }
            for f in check.folders
        ],
    }  # fmt: skip
