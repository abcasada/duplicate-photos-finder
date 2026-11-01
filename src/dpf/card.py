"""Card check: which photos and videos of a card (or any folder) are already on a drive.

`dpf card PATH` reads PATH once (read-only, like any scan) into a throwaway in-memory
index, copies from the real index only the files whose names could match, and runs the
backup-coverage rules (mode 1: similar name, size and date, whole-hour date shifts
allowed; coverage.missing_files) against the indexed drives. The real index is only read
and the card is never stored in it, so nothing is left behind and no drive needs to be
connected. A file has a copy when a file of an indexed drive matches it directly;
contents are not compared, so a renamed-and-re-saved copy counts as not found.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from dpf import media
from dpf.coverage import NOT_IN_OTHER, Coverage, _related, caveat, check_coverage, missing_files
from dpf.grouping import natural_key
from dpf.index import Index
from dpf.models import FileRecord
from dpf.report import (
    _iso,
    human_bytes,
    missing_label,
    plural,
    run_lines,
    scan_date,
    shifted_note,
)
from dpf.scanner import drop_nested, scan_roots

OTHER = "any indexed drive"  # Coverage.other: "missing from any indexed drive"
FOUND = "found"
NOT_FOUND = "not found"
CSV_FIELDS = (
    "path", "dir", "name", "size", "mtime", "kind", "status", "similar_name_on_drive", "drives",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class CardDrive:
    """An indexed drive holding a copy of at least one card file."""

    root: str
    scanned_at: float | None
    files: int  # card files with a copy there
    bytes: int


@dataclass(slots=True)
class CardCheck:
    card: str
    cov: Coverage  # card files with no copy on any compared drive (basis, folders, notes)
    compared: list[FileRecord]  # every non-empty media file of the card, by folder and name
    drives: list[CardDrive]  # most files first; a file counts for every drive holding it
    copies: dict[str, list[str]]  # card file path -> the drive roots holding a copy
    drives_compared: int
    scanned: int  # entries read from the card (media files, empty ones too)
    unreadable: int  # card entries that could not be read
    ignored: list[str] = field(default_factory=list)  # indexed roots equal to the card

    @property
    def found(self) -> int:
        return self.cov.files - len(self.cov.missing)

    @property
    def found_bytes(self) -> int:
        return sum(f.size for f in self.compared) - self.cov.missing_bytes


def check_card(
    idx: Index,
    card: str,
    *,
    drive_roots: Sequence[str] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    errors: list[str] | None = None,
    mtime_tolerance: float = 2.0,
    ignore_size: bool = False,
    ignore_mtime: bool = False,
    exact_names: bool = False,
    hour_shift: bool = True,
) -> CardCheck:
    """Compare the media under `card` with the indexed drives `drive_roots` (default: every
    indexed root). Raises ValueError when there is nothing to compare with, or when a
    drive root contains the card or lies inside it (an indexed root equal to `card` is
    ignored: it is the same card, scanned before)."""
    card = os.path.realpath(card)  # so a symlink to a scanned folder is still recognised
    indexed = {r.root: r.scanned_at for r in idx.roots()}
    ignored: list[str] = []
    others: list[str] = []
    for root in drop_nested(indexed if drive_roots is None else drive_roots):
        if root == card:
            ignored.append(root)
        elif _related(root, card):
            raise ValueError(f"{card} and the indexed {root} overlap; not a separate card")
        else:
            others.append(root)
    if not others:
        raise ValueError("no indexed drives to compare with (scan them first: dpf scan PATH)")
    match = dict(
        mtime_tolerance=mtime_tolerance, ignore_size=ignore_size, ignore_mtime=ignore_mtime,
        exact_names=exact_names, hour_shift=hour_shift,
    )  # fmt: skip
    with Index(":memory:") as temp:
        stats = scan_roots(temp, [card], progress=progress, errors=errors, allow_empty=True)[0]
        match_keys, name_keys = temp.keys_under([card])
        temp.add_rows(idx.rows_with_keys(others, match_keys, name_keys))
        cov = check_coverage(
            temp, [card], others, pool=None, other=OTHER, direction=NOT_IN_OTHER, **match
        )  # fmt: skip
        compared = sorted(
            (FileRecord(d, n, size, mtime, kind)
             for d, n, size, mtime, kind in temp.file_rows([card])
             if size > 0 and kind != media.OTHER),
            key=lambda f: (f.dir, natural_key(f.name), f.name),
        )  # fmt: skip
        lost = {f.path for f in cov.missing}
        copies: dict[str, list[str]] = {}
        per_drive: list[CardDrive] = []
        for root in others:
            gone = {f.path for f in missing_files(temp, [card], [root], **match).missing}
            here = [f for f in compared if f.path not in gone and f.path not in lost]
            for f in here:
                copies.setdefault(f.path, []).append(root)
            if here:
                per_drive.append(
                    CardDrive(root, indexed.get(root), len(here), sum(f.size for f in here))
                )  # fmt: skip
    per_drive.sort(key=lambda d: (-d.files, d.root))
    return CardCheck(
        card, cov, compared, per_drive, copies, len(others), stats.files, stats.errors, ignored
    )  # fmt: skip


def format_card(check: CardCheck, *, top: int, max_runs: int) -> str:
    """The terminal report: totals, which drives hold copies, then the folders with files
    found on no drive (largest first, with the runs of files that are on a drive)."""
    cov = check.cov
    lost = len(cov.missing)
    kinds = Counter(f.kind for f in check.compared)
    what = plural(kinds[media.IMAGE], "photo") + (
        f", {plural(kinds[media.VIDEO], 'video')}" if kinds[media.VIDEO] else ""
    )
    title = f"Card check: {check.card}"
    out = [title, "=" * len(title)]
    size = human_bytes(sum(f.size for f in check.compared))
    card = f"{plural(cov.files, 'media file')} ({what}), {size}"
    extra = []
    if cov.empty:
        extra.append(f"{plural(cov.empty, 'empty file')} not compared")
    other = cov.unindexed - check.unreadable  # the count holds unreadable entries too
    if other > 0:
        extra.append(f"{plural(other, 'other file')} (non-media) not compared")
    if check.unreadable:
        extra.append(f"{check.unreadable:,} unreadable, not compared")
    out.append(f"{'Card:':<20}{card}" + (f"; {', '.join(extra)}" if extra else ""))
    out.append(
        f"{'Compared with:':<20}{plural(check.drives_compared, 'indexed drive')} (from the "
        f"index, not scanned now); {cov.basis}"
    )
    out.append(f"{'':<20}{caveat(cov.basis_words)}")
    for root in check.ignored:
        out.append(f"{'Note:':<20}{root} is in the index too (scanned earlier); ignored")
    total = max(cov.files, 1)
    out.append(
        f"{'Already on a drive:':<20}{plural(check.found, 'file')}, "
        f"{human_bytes(check.found_bytes)} ({_share(check.found, total)})"
    )
    out.append(
        f"{'Not on any drive:':<20}{plural(lost, 'file')}, {human_bytes(cov.missing_bytes)}, "
        f"in {plural(cov.missing_dirs, 'folder')} ({_share(lost, total)})"
    )
    if cov.shifted:
        out.append(f"{'':<20}{shifted_note(cov)}")
    if cov.similar:
        n = len(cov.similar)
        out.append(
            f"{'':<20}{n:,} of them {'has' if n == 1 else 'have'} a file with a similar name "
            "on a drive, but of another size or date: an edited or re-saved version, or "
            "another photo with the same number"
        )
    if check.drives:
        out += ["", "Copies are on:"]
        width = max(len(d.root) for d in check.drives)
        for d in check.drives:
            out.append(
                f"  {d.root:<{width}}  {plural(d.files, 'file'):>14}  {human_bytes(d.bytes):>9}"
                f"   (index scanned {scan_date(d.scanned_at)})"
            )
        if len(check.drives) > 1:
            out.append("  (a file with copies on several drives counts for each of them)")
    out.append("")
    if check.unreadable:
        out.append(
            f"WARNING: {plural(check.unreadable, 'entry')} on the card could not be read, so "
            "photos or videos there were not checked."
        )
        out.append("")
    if not cov.files:
        out.append("No photos or videos found on the card.")
    elif not lost:
        out.append(
            "Every photo and video that could be read on the card has a matching copy on an "
            "indexed drive." if check.unreadable
            else "Every photo and video on the card has a matching copy on an indexed drive."
        )  # fmt: skip
    else:
        out += _lines(check, top, max_runs)
    return "\n".join(out).rstrip("\n") + "\n"


def _share(part: int, whole: int) -> str:
    """Whole percent rounded down, "<1%" for a tiny share: 100% always means every file."""
    if 0 < part < whole and part * 100 < whole:
        return "<1%"
    return f"{part * 100 // whole}%"


def _lines(check: CardCheck, top: int, max_runs: int) -> list[str]:
    cov = check.cov
    out = ["Not on any drive, by folder:", ""]
    shown = cov.folders[: max(top, 0)]
    num_w = len(f"[{len(shown)}]")
    for n, m in enumerate(shown, 1):
        out.append(
            f"{f'[{n}]'.ljust(num_w)}  {human_bytes(m.bytes):>8}  {m.dir}   "
            f"({missing_label(cov, m)})"
        )
        if m.runs and m.missing < m.total:
            out.extend(run_lines(m.runs, max_runs, skipped_label="on a drive"))
    rest = cov.folders[len(shown) :]
    if rest:
        files = sum(m.missing for m in rest)
        out.append(
            f"… {plural(len(rest), 'more folder')} ({plural(files, 'file')}, "
            f"{human_bytes(sum(m.bytes for m in rest))}) not shown; every file is in the CSV "
            "(-o DIR)"
        )
    return out


def write_card_csv(check: CardCheck, path: str) -> int:
    """One row per media file of the card, found or not, with the drives holding a copy;
    returns the rows written."""
    lost = {f.path for f in check.cov.missing}
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for f in check.compared:
            w.writerow(
                (f.path, f.dir, f.name, f.size, _iso(f.mtime), f.kind,
                 NOT_FOUND if f.path in lost else FOUND,
                 "yes" if f.path in check.cov.similar else "no",
                 "; ".join(check.copies.get(f.path, ())))
            )  # fmt: skip
    return len(check.compared)

