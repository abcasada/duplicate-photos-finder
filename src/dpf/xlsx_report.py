"""Excel report (report.xlsx): the text report as sortable, filterable sheets.

Needs the optional XlsxWriter package, imported only when a workbook is written.
Sizes are stored as bytes and percentages as rounded-down fractions, so sorting and
filtering work on real numbers while the cells read like the text report.
"""

from __future__ import annotations

import os
from collections import Counter
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from dpf.categories import ALL, OTHER, TITLES, Category, of_dirs
from dpf.coverage import Coverage, caveat
from dpf.grouping import (
    A_IN_B,
    DEFAULT_NEAR,
    B_IN_A,
    IDENTICAL,
    IDENTICAL_TREE,
    NEAR_IDENTICAL,
    PARTIAL,
    TREE_A_MOSTLY_IN_B,
    TREE_B_MOSTLY_IN_A,
    TREE_NEAR,
    FolderPair,
    GroupingResult,
    TreePair,
)
from dpf.models import DuplicateGroup
from dpf.overview import Overview, personal_lines
from dpf.report import (
    OFFLINE_NOTE,
    PAIR_LABELS,
    any_offline,
    category_tally,
    coverage_category_lines,
    coverage_relation,
    duration,
    floor_percent,
    hour_shift_note,
    human_bytes,
    pair_label,
    plural,
    root_lines,
    run_lines,
    same_folder_stats,
    name_list,
    shift_cell,
    shifted_note,
    tree_label,
    tree_tags,
    unindexed_note,
)

SHEETS = ("Summary", "Trees", "Folder pairs", "Sequences", "Same folder", "Files")
MAX_URL = 2079  # Excel's limit on a hyperlink target
MAX_URLS = 65_530  # Excel's limit on hyperlinks per worksheet
MAX_ROWS = 1_048_576
_SKIPPED_NAMES = 20  # names listed per run on the Sequences sheet
PATH_WIDTH = 70

# Bytes shown in decimal units; the thresholds match human_bytes' rounding. Excel allows
# two conditions per format, so totals start at MB and single files stop at GB.
SIZE_FORMAT = '[<999950000]#,##0.0,," MB";[<999950000000]#,##0.0,,," GB";#,##0.0,,,," TB"'
FILE_SIZE_FORMAT = '[<999950]#,##0.0," kB";[<999950000]#,##0.0,," MB";#,##0.0,,," GB"'

GREEN = "#C6EFCE"
LIGHT_GREEN = "#E2F0D9"
LIGHT_BLUE = "#DDEBF7"
AMBER = "#FFEB9C"

_WORKBOOK_OPTIONS = {
    "strings_to_urls": False,
    "strings_to_formulas": False,
    "strings_to_numbers": False,
}


@dataclass(frozen=True, slots=True)
class PoolSheets:
    """One pool's duplicate report for a combined workbook."""

    name: str
    result: GroupingResult
    groups: Sequence[DuplicateGroup]
    summary: dict


def write_xlsx(
    result: GroupingResult,
    groups: Sequence[DuplicateGroup],
    summary: dict,
    path: str,
    coverage: Sequence[Coverage] = (),
    overview: Overview | None = None,
) -> None:
    """Write report.xlsx. Raises ModuleNotFoundError if XlsxWriter is not installed.

    Unlike the text report, every tree and folder pair is listed (the sheets filter).
    Each coverage section gets a sheet named after it (e.g. "A not in B").
    """
    import xlsxwriter

    with xlsxwriter.Workbook(path, _WORKBOOK_OPTIONS) as wb:
        fmt = _Formats(wb)
        summary_ws = wb.add_worksheet(SHEETS[0])
        used = {SHEETS[0].casefold()}
        drives = _drives_tables(wb, fmt, overview, used)
        tables = drives + _pool_tables(wb, fmt, result, groups, "", used)
        tables += _coverage_tables(wb, fmt, coverage, used)
        for t in tables:
            t.finish()
        w = _SummaryWriter(summary_ws, fmt)
        w.title("dpf duplicate report")
        _personal_block(w, overview)
        _pool_block(w, result, summary, elapsed=True)
        _coverage_block(w, coverage, tables)
        _sheet_list(w, tables)
        _how_to_read(w, result.near, bool(coverage))
        _other_reports(w, summary)


def write_combined_xlsx(
    pools: Sequence[PoolSheets],
    coverage: Sequence[Coverage],
    summary: dict,
    path: str,
    overview: Overview | None = None,
) -> None:
    """One workbook for several pools: each pool's sheets prefixed with its name.

    Sheet order: Summary, then Trees … Files of each pool in turn ("A Trees", …,
    "B Files"), then one sheet per coverage section ("A not in B", "B not in A"). Each
    pool's duplicates come from its own search; pools are never compared for duplicates.
    """
    import xlsxwriter

    with xlsxwriter.Workbook(path, _WORKBOOK_OPTIONS) as wb:
        fmt = _Formats(wb)
        summary_ws = wb.add_worksheet(SHEETS[0])
        used = {SHEETS[0].casefold()}
        drives = _drives_tables(wb, fmt, overview, used)
        per_pool = [_pool_tables(wb, fmt, p.result, p.groups, p.name, used) for p in pools]
        cov_tables = _coverage_tables(wb, fmt, coverage, used)
        tables = drives + [t for ts in per_pool for t in ts] + cov_tables
        for t in tables:
            t.finish()
        w = _SummaryWriter(summary_ws, fmt)
        w.title("dpf duplicate report: " + ", ".join(f"pool {p.name}" for p in pools))
        w.line("Mode", str(summary.get("mode", "?")))
        w.line("Matched on", str(summary.get("basis", "?")))
        w.line("Elapsed", duration(summary.get("elapsed", 0.0)))
        w.line("", "Duplicates are searched within each pool, never between pools.")
        w.skip()
        _personal_block(w, overview)
        if drives:
            _sheet_list(w, drives, title="Drive sheet")
        for p, ts in zip(pools, per_pool):
            w.heading(f"Pool {p.name}")
            _pool_block(w, p.result, p.summary, elapsed=False, basis=False)
            _sheet_list(w, ts, title=f"Pool {p.name} sheets")
        _coverage_block(w, coverage, cov_tables)
        if cov_tables:
            _sheet_list(w, cov_tables, title="Coverage sheets")
        near = pools[0].result.near if pools else DEFAULT_NEAR
        _how_to_read(w, near, bool(coverage))
        _other_reports(w, summary)


_FORBIDDEN_SHEET_CHARS = re.compile(r"[\[\]:*?/\\']")


def sheet_name(text: str, used: set[str]) -> str:
    """`text` as a valid, unique worksheet name (max 31 chars, no []:*?/\\ or ')."""
    base = _FORBIDDEN_SHEET_CHARS.sub("_", text).strip() or "Sheet"
    name, n = base[:31].rstrip(), 1
    while name.casefold() in used:
        n += 1
        suffix = f" ({n})"
        name = base[: 31 - len(suffix)].rstrip() + suffix
    used.add(name.casefold())
    return name


def _drives_tables(wb, fmt: _Formats, ov: Overview | None, used: set[str]) -> list[_Table]:
    """The Drives sheet (none without an overview): one row per drive, sizes in bytes."""
    if ov is None:
        return []
    sole = any(d.sole_files is not None for d in ov.drives)
    columns = [("Drive", None), ("Pool", 6), ("Capacity", 11), ("Used", 11), ("Free", 11),
               ("Photos", 10), ("Videos", 9), ("Media size", 11)]  # fmt: skip
    if ov.categories:
        columns += [("Personal", 11), ("Other media", 11)]
    columns += [("Not media", 11)]
    if sole:
        columns += [("Only copy files", 10), ("Only copy size", 11), ("Name match", 11)]
    columns += [("Space", 10), ("Last scanned", 18)]
    t = _Table(wb, fmt, sheet_name("Drives", used), columns, 1, kind="Drives")
    for d in ov.drives:
        if not t.next_row():
            break
        values: list[int | None] = [d.capacity, d.used, d.free]
        c = 0
        t.folder(c, d.root)
        t.text(c + 1, d.pool)
        for c, v in enumerate(values, 2):
            if v is not None:
                t.num(c, v, fmt.size)
        t.num(5, d.photos)
        t.num(6, d.videos)
        t.num(7, d.media_bytes, fmt.size)
        c = 8
        if ov.categories:
            t.num(c, d.personal_bytes or 0, fmt.size)
            t.num(c + 1, d.other_bytes or 0, fmt.size)
            c += 2
        if d.not_media is not None:
            t.num(c, d.not_media, fmt.size)
        c += 1
        if sole:
            if d.sole_files is not None:
                t.num(c, d.sole_files)
                t.num(c + 1, d.sole_bytes or 0, fmt.size)
                t.num(c + 2, d.sole_similar or 0, fmt.size)
            c += 3
        space = "by hand" if d.manual else "measured"
        if d.capacity is None:
            space = "folder" if d.folder else "not recorded"
        t.text(c, space)
        if d.scanned_at is not None:
            t.ws.write_datetime(t.row, c + 1, datetime.fromtimestamp(d.scanned_at), fmt.date)
    return [t]


def _personal_block(w: _SummaryWriter, ov: Overview | None) -> None:
    if ov is None or not ov.pools:
        return
    lines = personal_lines(ov)
    w.heading(lines[0].split("  (")[0].rstrip(":"))
    for line in lines[1:]:
        w.line("", line.strip())
    w.skip()


def _pool_tables(
    wb, fmt: _Formats, result: GroupingResult, groups: Sequence[DuplicateGroup],
    prefix: str, used: set[str],
) -> list[_Table]:  # fmt: skip
    """The Trees … Files sheets of one pool, their names prefixed with `prefix`."""

    def name(kind: str) -> str:
        return sheet_name(f"{prefix} {kind}" if prefix else kind, used)

    tags = tree_tags(result.trees)
    cat = result.category
    return [
        _trees(wb, fmt, result.trees, name("Trees"), cat is not None),
        _pairs(wb, fmt, result.pairs, tags, result.near, name("Folder pairs"), cat is not None),
        _sequences(wb, fmt, result.pairs, name("Sequences"), cat is not None),
        _same_folder(wb, fmt, result.same_folder, name("Same folder"), cat),
        _files(wb, fmt, groups, name("Files"), cat),
    ]


class _Formats:
    def __init__(self, wb) -> None:
        def add(**props):
            return wb.add_format({"valign": "top", **props})

        self.header = add(bold=True, bg_color="#D9E1F2", bottom=1, text_wrap=True)
        self.text = add()
        self.link = add(font_color="#0563C1", underline=1)
        self.int = add(num_format="#,##0")
        self.size = add(num_format=SIZE_FORMAT)
        self.file_size = add(num_format=FILE_SIZE_FORMAT)
        self.pct = add(num_format="0%")
        self.share = add(num_format='[<0.01]"<1%";0%')
        self.heading = add(bold=True, font_size=12, bottom=1)
        self.tree = add(num_format='"T"0')
        self.date = add(num_format="yyyy-mm-dd hh:mm:ss")
        self.amber = add(num_format="#,##0", bg_color=AMBER, font_color="#9C5700")
        self.warning = add(font_color="#9C5700", bold=True)
        self.warning_text = add(bg_color=AMBER, font_color="#9C5700")
        self.fills = {c: add(bg_color=c) for c in (GREEN, LIGHT_GREEN, LIGHT_BLUE)}
        self.title = add(bold=True, font_size=14)
        self.bold = add(bold=True)
        self.left_int = add(num_format="#,##0", align="left")
        self.left_size = add(num_format=SIZE_FORMAT, align="left")


class _Table:
    """One sheet holding a table: header row, autofilter, frozen panes, fitted widths.

    `columns` are (header, width); a width of None fits the content, up to PATH_WIDTH.
    """

    def __init__(
        self, wb, fmt: _Formats, name: str, columns, freeze_cols: int = 0, *,
        kind: str | None = None, note: str | None = None,
    ) -> None:  # fmt: skip
        self.ws = wb.add_worksheet(name)
        self.fmt = fmt
        self.name = name
        self.note = note or _SHEET_NOTES[kind or name]
        self.columns = columns
        self.freeze_cols = freeze_cols
        self.widths = [len(h) for h, _ in columns]
        self.row = 0
        self.links = 0
        self.dropped = 0
        self.coverage: int | None = None  # id() of the Coverage a coverage sheet shows
        for c, (header, _) in enumerate(columns):
            self.ws.write_string(0, c, header, fmt.header)

    def next_row(self) -> bool:
        """Start a new data row; False (and the row is counted as dropped) if the sheet is full."""
        if self.row >= MAX_ROWS - 1:
            self.dropped += 1
            return False
        self.row += 1
        return True

    def text(self, col: int, value: str, cell_format=None) -> None:
        self.ws.write_string(self.row, col, value, cell_format or self.fmt.text)
        width = max(map(len, value.split("\n"))) if "\n" in value else len(value)
        if width > self.widths[col]:
            self.widths[col] = width

    def num(self, col: int, value: float, cell_format=None) -> None:
        self.ws.write_number(self.row, col, value, cell_format or self.fmt.int)

    def pct(self, col: int, part: int, whole: int) -> None:
        """Rounded down, so 100% always means every file."""
        if whole > 0:
            self.ws.write_number(self.row, col, floor_percent(part, whole) / 100, self.fmt.pct)

    def share(self, col: int, part: int, whole: int) -> None:
        """Like pct, but a share between 0 and 1% shows "<1%" rather than 0%."""
        if whole > 0:
            value = floor_percent(part, whole) / 100
            if part > 0 and value == 0:
                value = 0.001
            self.ws.write_number(self.row, col, value, self.fmt.share)

    def folder(self, col: int, path: str) -> None:
        """A file:// link to the folder showing the plain path; plain text past Excel's limits."""
        url = "file://" + quote(os.fsencode(path))
        if self.links < MAX_URLS and len(url) <= MAX_URL:
            self.ws.write_url(self.row, col, url, self.fmt.link, string=path)
            self.links += 1
            self.widths[col] = max(self.widths[col], len(path))
        else:
            self.text(col, path)

    def data_bar(self, col: int) -> None:
        if self.row:
            self.ws.conditional_format(
                1, col, self.row, col,
                {"type": "data_bar", "bar_color": "#BDD7EE", "bar_solid": True},
            )  # fmt: skip

    def finish(self) -> None:
        for c, (_, fixed) in enumerate(self.columns):
            width = fixed if fixed is not None else min(self.widths[c] + 2, PATH_WIDTH)
            self.ws.set_column(c, c, width)
        self.ws.autofilter(0, 0, self.row, len(self.columns) - 1)
        self.ws.freeze_panes(1, self.freeze_cols)


def _pair_fill(p: FolderPair) -> str | None:
    if p.relation in (IDENTICAL_TREE, IDENTICAL):
        return GREEN
    if p.relation == NEAR_IDENTICAL:
        return LIGHT_GREEN
    if p.relation in (A_IN_B, B_IN_A):
        return LIGHT_BLUE
    return None


def _tree_fill(t: TreePair) -> str | None:
    if t.relation == TREE_NEAR:
        return LIGHT_GREEN
    if (t.relation == TREE_A_MOSTLY_IN_B and t.matched_a == t.total_a) or (
        t.relation == TREE_B_MOSTLY_IN_A and t.matched_b == t.total_b
    ):
        return LIGHT_BLUE
    return None


def _shift(table: _Table, col: int, hours: Sequence[int]) -> None:
    if hours:
        table.text(col, shift_cell(hours), table.fmt.warning_text)


def _category(table: _Table, col: int, category: str | None) -> None:
    """The category cell (amber for other media); empty when no categories are set."""
    if category:
        table.text(col, category, table.fmt.warning_text if category == OTHER else None)


_EXTRA_COLUMNS = [("Time shift", 11), ("Category", 10)]


def _relation(table: _Table, col: int, label: str, fill: str | None) -> None:
    table.text(col, label, table.fmt.fills[fill] if fill else None)


def _not_compared(table: _Table, col: int, n: int) -> None:
    table.num(col, n, table.fmt.amber if n else None)


def _sides(table: _Table, col: int, ma: int, ta: int, mb: int, tb: int) -> None:
    """A matched, A total, A %, B matched, B total, B % from column `col` on."""
    for i, (matched, total) in enumerate(((ma, ta), (mb, tb))):
        table.num(col + 3 * i, matched)
        table.num(col + 3 * i + 1, total)
        table.pct(col + 3 * i + 2, matched, total)


_SIDE_COLUMNS = [
    ("A matched", 10), ("A total", 9), ("A %", 7),
    ("B matched", 10), ("B total", 9), ("B %", 7),
]  # fmt: skip


def _trees(
    wb, fmt: _Formats, trees: list[TreePair], name: str = "Trees", categories: bool = False
) -> _Table:
    columns = [
        ("#", 6), ("Shared size", 13), ("Relation", None), ("Folder A", None),
        ("Folder B", None), *_SIDE_COLUMNS, ("Folder pairs", 9),
        ("Other files not compared", 12), *_EXTRA_COLUMNS,
    ]  # fmt: skip
    t = _Table(wb, fmt, name, columns, freeze_cols=1, kind="Trees")
    for n, tree in enumerate(trees, 1):
        if not t.next_row():
            continue
        t.num(0, n, fmt.tree)
        t.num(1, tree.bytes, fmt.size)
        _relation(t, 2, tree_label(tree), _tree_fill(tree))
        t.folder(3, tree.dir_a)
        t.folder(4, tree.dir_b)
        _sides(t, 5, tree.matched_a, tree.total_a, tree.matched_b, tree.total_b)
        t.num(11, tree.folders)
        _not_compared(t, 12, tree.unindexed)
        _shift(t, 13, tree.hour_shift)
        _category(t, 14, categories and tree.category)
    t.data_bar(1)
    return t


def _sequence_text(p: FolderPair) -> str:
    """The first run on one line, so every row keeps a single-line height.

    All runs are listed on the Sequences sheet. Files not matched inside the other runs
    are counted, since the first run's line only names its own.
    """
    if not p.runs:
        return ""
    first = run_lines(p.runs[:1], 1, indent="", align=False)[0]
    more = len(p.runs) - 1
    skipped = sum(len(r.skipped) for r in p.runs[1:])
    notes = [f"+{more} more on Sequences sheet"] if more else []
    if skipped:
        notes.append(f"{skipped:,} inside them not matched")
    return f"{first}  ({'; '.join(notes)})" if notes else first


def _names(p: FolderPair) -> str:
    return "similar" if p.similar_names else "exact"


def _pairs(
    wb, fmt: _Formats, pairs: list[FolderPair], tags: dict, near: float,
    name: str = "Folder pairs", categories: bool = False,
) -> _Table:  # fmt: skip
    columns = [
        ("#", 7), ("Tree", 6), ("Shared size", 13), ("Relation", None), ("Names", 9),
        ("Folder A", None), ("Folder B", None), *_SIDE_COLUMNS, ("Only in A", 9),
        ("Only in B", 9), ("Other files not compared", 12), ("Runs", 7),
        ("First sequence", 60), *_EXTRA_COLUMNS,
    ]  # fmt: skip
    t = _Table(wb, fmt, name, columns, freeze_cols=2, kind="Folder pairs")
    for n, p in enumerate(pairs, 1):
        if not t.next_row():
            continue
        t.num(0, n)
        tag = tags.get((p.dir_a, p.dir_b))
        if tag:
            t.num(1, tag, fmt.tree)
        t.num(2, p.bytes, fmt.size)
        label = pair_label(p, near)
        if p.relation == IDENTICAL_TREE:
            label += f": {plural(p.folders, 'folder')}"
        _relation(t, 3, label, _pair_fill(p))
        t.text(4, _names(p), fmt.warning_text if p.similar_names else None)
        t.folder(5, p.dir_a)
        t.folder(6, p.dir_b)
        _sides(t, 7, p.matched_a, p.total_a, p.matched_b, p.total_b)
        t.num(13, p.total_a - p.matched_a)
        t.num(14, p.total_b - p.matched_b)
        _not_compared(t, 15, p.unindexed)
        t.num(16, len(p.runs))
        text = _sequence_text(p)
        if text:
            t.text(17, text)
        _shift(t, 18, p.hour_shift)
        _category(t, 19, categories and p.category)
    t.data_bar(2)
    return t


def _sequences(
    wb, fmt: _Formats, pairs: list[FolderPair], name: str = "Sequences",
    categories: bool = False,
) -> _Table:  # fmt: skip
    columns = [
        ("Pair #", 7), ("Folder A", None), ("Folder B", None), ("First (A)", None),
        ("Last (A)", None), ("First (B)", None), ("Last (B)", None), ("Files", 8),
        ("Size", 12), ("Not matched inside", 12), ("Not matched names", None),
        *_EXTRA_COLUMNS,
    ]  # fmt: skip
    t = _Table(wb, fmt, name, columns, freeze_cols=1, kind="Sequences")
    for n, p in enumerate(pairs, 1):
        for r in p.runs:
            if not t.next_row():
                continue
            t.num(0, n)
            t.text(1, p.dir_a)
            t.text(2, p.dir_b)
            t.text(3, r.a_first)
            t.text(4, r.a_last)
            if not r.same_names:
                t.text(5, r.b_first)
                t.text(6, r.b_last)
            t.num(7, r.count)
            t.num(8, r.bytes, fmt.size)
            if r.skipped:
                t.num(9, len(r.skipped), fmt.amber)
                t.text(10, name_list(r.skipped, _SKIPPED_NAMES))
            _shift(t, 11, r.hour_shift)
            _category(t, 12, categories and p.category)
    return t


def _same_folder(
    wb, fmt: _Formats, groups: list[DuplicateGroup], name: str = "Same folder",
    category: Category | None = None,
) -> _Table:  # fmt: skip
    columns = [
        ("Folder", None), ("Groups", 9), ("Extra copies", 9), ("Redundant size", 12),
        ("Category", 10),
    ]  # fmt: skip
    t = _Table(wb, fmt, name, columns, kind="Same folder")
    _, ranked = same_folder_stats(groups)
    ranked.sort(key=lambda r: (-r[3], -r[1], r[0]))
    for d, n_groups, extra, nbytes in ranked:
        if not t.next_row():
            continue
        t.folder(0, d)
        t.num(1, n_groups)
        t.num(2, extra)
        t.num(3, nbytes, fmt.size)
        _category(t, 4, category and category(d))
    t.data_bar(3)
    return t


def _files(
    wb, fmt: _Formats, groups: Sequence[DuplicateGroup], name: str = "Files",
    category: Category | None = None,
) -> _Table:  # fmt: skip
    columns = [
        ("Group", 8), ("Basis", None), ("Folder", None), ("Name", None), ("Size", 11),
        ("Modified", 19), ("Kind", 7), ("Names", 9), ("Hours after first", 10),
        ("Category", 10),
    ]  # fmt: skip
    t = _Table(wb, fmt, name, columns, freeze_cols=1, kind="Files")
    for n, g in enumerate(groups, 1):
        names = "exact" if g.same_names else "similar"
        cat = category and of_dirs(category, {f.dir for f in g.files})
        for i, f in enumerate(g.files):
            if not t.next_row():
                continue
            t.num(0, n)
            t.text(1, g.basis)
            t.text(2, f.dir)
            t.text(3, f.name)
            t.num(4, f.size, fmt.file_size)
            modified = _datetime(f.mtime)
            if modified is None:
                t.text(5, str(f.mtime))
            else:
                t.ws.write_datetime(t.row, 5, modified, fmt.date)
            t.text(6, f.kind)
            t.text(7, names)
            if g.hours:
                t.num(8, g.hours[i], fmt.amber if g.hours[i] else None)
            _category(t, 9, cat)
    return t


def _coverage_tables(
    wb, fmt: _Formats, coverage: Sequence[Coverage], used: set[str]
) -> list[_Table]:
    return [_coverage_sheet(wb, fmt, cov, sheet_name(cov.label, used)) for cov in coverage]


def _coverage_sheet(wb, fmt: _Formats, cov: Coverage, name: str) -> _Table:
    """One row per folder (or whole missing tree) with files that have no copy."""
    columns = [
        ("#", 6), ("Missing size", 13), ("Folder", None), ("Whole tree", 8), ("Folders", 9),
        ("Missing files", 10), ("Total files", 9), ("Missing %", 9),
        ("Similar name there", 10), ("Other files not compared", 12), ("Runs", 7),
        ("First missing run", 60), ("Category", 10),
    ]  # fmt: skip
    mine = f"pool {cov.pool}" if cov.pool else "these folders"
    note = f"media files of {mine} with no copy anywhere in pool {cov.other}, one row per folder"
    if cov.category != ALL:
        note += f" ({TITLES[cov.category].lower()} only)"
    t = _Table(wb, fmt, name, columns, freeze_cols=1, note=note)
    t.coverage = id(cov)
    for n, m in enumerate(cov.folders, 1):
        if not t.next_row():
            continue
        t.num(0, n)
        t.num(1, m.bytes, fmt.size)
        t.folder(2, m.dir)
        if m.whole_tree:
            t.text(3, "yes", fmt.fills[LIGHT_BLUE])
        t.num(4, m.folders)
        t.num(5, m.missing)
        t.num(6, m.total)
        t.share(7, m.missing, m.total)
        t.num(8, m.similar, fmt.amber if m.similar else None)
        _not_compared(t, 9, m.unindexed)
        if not m.whole_tree:
            t.num(10, len(m.runs))
            if m.runs:
                label = f"with a copy in {cov.other}"
                first = run_lines(m.runs[:1], 1, indent="", align=False, skipped_label=label)[0]
                more = len(m.runs) - 1
                t.text(11, f"{first}  (+{more:,} more runs)" if more else first)
        _category(t, 12, cov.by_category and m.category)
    t.data_bar(1)
    return t


def _datetime(mtime: float) -> datetime | None:
    """Local naive datetime, or None where Excel can't show it (before 1900)."""
    try:
        dt = datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if dt.year >= 1900 else None


_SHEET_NOTES = {
    "Drives": "each drive: space, photos/videos, personal media, only copies",
    "Trees": "tree overview: folder pairs grouped by the trees they sit in",
    "Folder pairs": "every pair of folders sharing duplicates, largest first",
    "Sequences": "one row per run of consecutive matched files",
    "Same folder": "folders holding duplicates of their own files",
    "Files": "every duplicate file, one row each (as files.csv)",
}


class _SummaryWriter:
    """Writes the Summary sheet top to bottom: label in column A, value in column B."""

    def __init__(self, ws, fmt: _Formats) -> None:
        self.ws = ws
        self.fmt = fmt
        self.row = 0
        ws.set_column(0, 0, 30)
        ws.set_column(1, 1, 24)
        ws.set_column(2, 7, 14)

    def title(self, text: str) -> None:
        self.ws.write_string(self.row, 0, text, self.fmt.title)
        self.row += 2

    def heading(self, text: str) -> None:
        self.ws.write_string(self.row, 0, text, self.fmt.heading)
        self.row += 1

    def skip(self, n: int = 1) -> None:
        self.row += n

    def line(self, label: str = "", value=None, value_format=None, label_format=None) -> None:
        if label:
            self.ws.write_string(self.row, 0, label, label_format or self.fmt.bold)
        if isinstance(value, str):
            self.ws.write_string(self.row, 1, value, value_format or self.fmt.text)
        elif value is not None:
            self.ws.write_number(self.row, 1, value, value_format or self.fmt.left_int)
        self.row += 1


def _pool_block(
    w: _SummaryWriter, result: GroupingResult, summary: dict, *, elapsed: bool,
    basis: bool = True,
) -> None:  # fmt: skip
    """Header lines, totals and relation counts of one duplicate report."""
    fmt = w.fmt
    if basis:
        w.line("Mode", str(summary.get("mode", "?")))
        w.line("Matched on", str(summary.get("basis", "?")))
    if summary.get("pool"):
        w.line("Pool", str(summary["pool"]))
    for i, root in enumerate(root_lines(summary, align=False)):
        w.line("Roots" if i == 0 else "", root)
    if any_offline(summary):
        w.line("", OFFLINE_NOTE, fmt.warning)
    w.line("Files scanned", summary.get("files", 0))
    w.line("Total size", summary.get("bytes", 0), fmt.left_size)
    tallies = summary.get("categories")
    if tallies:
        w.line("Categories", category_tally(tallies))
        _drive_categories(w, summary)
    only = summary.get("category", ALL)
    if only != ALL:
        w.line("Showing", f"{TITLES[only].lower()} only (--category {only})", fmt.warning)
    w.line("Duplicate groups", result.groups)
    if result.hour_shift_groups:
        w.line("", hour_shift_note(result.hour_shift_groups))
    w.line("Could free (keeping one copy)", result.redundant_bytes, fmt.left_size)
    for warning in summary.get("warnings", ()):
        w.line("Warning", warning, fmt.warning)
    if result.capped_groups:
        w.line(
            "",
            f"{plural(result.capped_groups, 'group')} spread over very many folders "
            "were paired only with their first folder",
        )
    if elapsed:
        w.line("Elapsed", duration(summary.get("elapsed", 0.0)))
    w.line("Near-identical threshold", f"{result.near * 100:g}% of each side")
    w.skip()

    by_tree = Counter(tree_label(t) for t in result.trees)
    tree_bytes = Counter()
    for t in result.trees:
        tree_bytes[tree_label(t)] += t.bytes
    _counts(w.ws, fmt, w.row, "Tree pairs", by_tree, tree_bytes)
    w.skip(len(by_tree) + 3)

    by_pair = Counter(PAIR_LABELS.get(p.relation, p.relation) for p in result.pairs)
    pair_bytes = Counter()
    for p in result.pairs:
        pair_bytes[PAIR_LABELS.get(p.relation, p.relation)] += p.bytes
    order = [PAIR_LABELS[r] for r in (IDENTICAL_TREE, IDENTICAL, A_IN_B, B_IN_A, NEAR_IDENTICAL,
                                      PARTIAL)]  # fmt: skip
    ranked = {k: by_pair[k] for k in order if k in by_pair}
    ranked.update({k: v for k, v in by_pair.items() if k not in ranked})
    _counts(w.ws, fmt, w.row, "Folder pairs", ranked, pair_bytes)
    w.skip(len(ranked) + 3)


def _drive_categories(w: _SummaryWriter, summary: dict) -> None:
    """Per drive: personal and other media, files and bytes (root_status entries)."""
    status = [e for e in summary.get("root_status") or [] if "personal_files" in e]
    if not status:
        return
    fmt, ws = w.fmt, w.ws
    headers = ("Personal files", "Personal size", "Other files", "Other size")
    ws.write_string(w.row, 0, "Drive / folder", fmt.bold)
    for c, header in enumerate(headers, 1):
        ws.write_string(w.row, c, header, fmt.bold)
    w.skip()
    for e in status:
        ws.write_string(w.row, 0, e["root"], fmt.text)
        ws.write_number(w.row, 1, e["personal_files"], fmt.int)
        ws.write_number(w.row, 2, e["personal_bytes"], fmt.size)
        ws.write_number(w.row, 3, e["other_files"], fmt.int)
        ws.write_number(w.row, 4, e["other_bytes"], fmt.size)
        w.skip()


_COVERAGE_SUMMARY = (
    "Missing files", "Missing size", "Missing %", "Folders", "Files compared",
    "Empty files not compared", "Similar name there",
)  # fmt: skip


def _coverage_block(w: _SummaryWriter, coverage: Sequence[Coverage], tables) -> None:
    """Totals of each coverage section, linked to its sheet."""
    if not coverage:
        return
    fmt, ws = w.fmt, w.ws
    w.heading("Backup coverage")
    ws.write_string(w.row, 0, "Files with no copy in the other pool", fmt.bold)
    for c, header in enumerate(_COVERAGE_SUMMARY, 1):
        ws.write_string(w.row, c, header, fmt.bold)
    w.skip()
    sheets = {t.coverage: t.name for t in tables if t.coverage is not None}
    for cov in coverage:
        name = sheets.get(id(cov))
        if name:
            ws.write_url(w.row, 0, f"internal:'{name}'!A1", fmt.link, string=cov.label)
        else:
            ws.write_string(w.row, 0, cov.label, fmt.text)
        ws.write_number(w.row, 1, len(cov.missing), fmt.int)
        ws.write_number(w.row, 2, cov.missing_bytes, fmt.size)
        if cov.files:
            value = floor_percent(len(cov.missing), cov.files) / 100
            ws.write_number(w.row, 3, 0.001 if cov.missing and not value else value, fmt.share)
        ws.write_number(w.row, 4, cov.missing_dirs, fmt.int)
        ws.write_number(w.row, 5, cov.files, fmt.int)
        ws.write_number(w.row, 6, cov.empty, fmt.amber if cov.empty else fmt.int)
        ws.write_number(w.row, 7, len(cov.similar), fmt.amber if cov.similar else fmt.int)
        w.skip()
    for cov in coverage:
        w.line("", f"{cov.title}: {coverage_relation(cov)}.")
        if cov.category != ALL:
            w.line("", f"  {TITLES[cov.category].lower()} only", fmt.warning)
        for line in coverage_category_lines(cov):
            w.line("", f"  {line}")
        if cov.shifted:
            w.line("", f"  {shifted_note(cov)}")
        if cov.unindexed:
            w.line("", f"  {unindexed_note(cov)}", fmt.warning)
        for warning in cov.warnings:
            w.line("", f"  Warning: {warning}", fmt.warning)
        for line in root_lines({"root_status": cov.other_status, "roots": cov.other_roots},
                               align=False):  # fmt: skip
            w.line("", f"  pool {cov.other}: {line}")
        if cov.never_scanned:
            w.line(
                "",
                f"  {plural(len(cov.never_scanned), 'drive')} of pool {cov.other} never "
                f"scanned (not compared): {', '.join(cov.never_scanned)}",
                fmt.warning,
            )  # fmt: skip
    w.line("", caveat(coverage[0].basis_words), fmt.warning)
    w.skip()


def _sheet_list(w: _SummaryWriter, tables, title: str = "Sheets") -> None:
    fmt, ws = w.fmt, w.ws
    ws.write_string(w.row, 0, title, fmt.bold)
    w.skip()
    for t in tables:
        ws.write_url(w.row, 0, f"internal:'{t.name}'!A1", fmt.link, string=t.name)
        ws.write_string(w.row, 1, t.note, fmt.text)
        w.skip()
        if t.dropped:
            ws.write_string(
                w.row, 1,
                f"{plural(t.dropped, 'row')} left out: Excel's row limit. See the CSV/JSON.",
                fmt.amber,
            )  # fmt: skip
            w.skip()
    w.skip()


def _how_to_read(w: _SummaryWriter, near: float, coverage: bool) -> None:
    w.ws.write_string(w.row, 0, "How to read this", w.fmt.bold)
    w.skip()
    for note in _notes(near) + (_COVERAGE_NOTES if coverage else []):
        w.ws.write_string(w.row, 0, note, w.fmt.text)
        w.skip()
    w.skip()


def _other_reports(w: _SummaryWriter, summary: dict) -> None:
    keys = (("CSV", "csv_path"), ("JSON", "json_path"), ("Missing files CSV", "missing_csv_path"))
    outputs = [(k, summary.get(key)) for k, key in keys]
    outputs = [(k, v) for k, v in outputs if v]
    if outputs:
        w.ws.write_string(w.row, 0, "Other reports", w.fmt.bold)
        w.skip()
        for k, v in outputs:
            w.line(k, v, label_format=w.fmt.text)


_COVERAGE_NOTES = [
    "Coverage sheets (e.g. \"A not in B\"): photos/videos of one pool with no copy anywhere "
    "in the other pool, one row per folder, largest first.",
    "  A copy is a file mode 1 would match (similar name, size, date within the tolerance or "
    "whole hours apart), wherever it sits; file contents are not compared.",
    "  Only media files are compared: sidecars, edits and catalogs (other files) may exist "
    "only on one side.",
    "  A renamed-and-re-saved copy counts as missing. Whole tree = every compared file under "
    "the folder is missing; Missing % is rounded down (\"<1%\" when above 0).",
    "  0-byte files are not compared; other files (non-photo/video) are counted per folder, "
    "not checked.",
    "  Similar name there (amber): missing files whose name does occur in the other pool, "
    "with another size or date: an edited or re-saved version, or another photo with the "
    "same number. Check them before relying on either copy.",
]


def _counts(ws, fmt: _Formats, row: int, title: str, counts: dict, sizes: Counter) -> None:
    ws.write_string(row, 0, title, fmt.bold)
    ws.write_string(row, 1, "Count", fmt.bold)
    ws.write_string(row, 2, "Shared size", fmt.bold)
    if not counts:
        ws.write_string(row + 1, 0, "none", fmt.text)
    for i, (label, n) in enumerate(counts.items(), row + 1):
        ws.write_string(i, 0, label, fmt.text)
        ws.write_number(i, 1, n, fmt.int)
        ws.write_number(i, 2, sizes[label], fmt.size)


def _notes(near: float) -> list[str]:
    pct = f"{near * 100:g}%"
    return [
        "Mode 1 compares file names, sizes and dates, not file contents: the labels say "
        '"match", never "identical".',
        "Percentages are rounded down, so 100% always means every file. Sizes are decimal "
        f"(1 GB = 1,000,000,000 bytes); e.g. {human_bytes(12_400_000_000)}.",
        "Folder pairs: A is the left folder, B the right. Relations:",
        "  matching tree: two folder hierarchies whose photos/videos all match, folder by "
        "folder (green)",
        "  all photos/videos match: every photo/video directly in A is in B and vice versa "
        "(green)",
        "  A is fully contained in B (or the reverse): every photo/video of one side is in "
        "the other (blue)",
        f"  near-identical: neither side is fully contained, but at least {pct} of each "
        "side matched (light green)",
        "  partial overlap: some files in common (no colour)",
        'Names: "similar" when some matched files are named differently on the two sides '
        '(copy marks such as " (1)", a date before a camera number, or case); "exact" '
        "otherwise. Files sheet: the same per group.",
        'Sequences: "Not matched inside" counts files inside a run\'s range (the other '
        "format of a matched RAW+JPG shot) that have no copy on the other side.",
        '"Other files not compared" (amber when not 0): non-photo/video files such as '
        "sidecars, edits or notes, symlinks and unreadable entries.",
        "  They were not compared at all: check them before deleting either side.",
        "Trees: folder pairs grouped by the trees they sit in, matched at the same relative "
        "path; A/B % are of all photos/videos indexed under each tree.",
        "  The Tree column of Folder pairs shows the tree a pair belongs to. Read the pairs "
        "before deleting: the overview hides unmatched subfolders and other files.",
        "Time shift (amber): some matches differ by whole hours (copies made across time "
        "zones or a DST change); +1 h = B's dates are one hour later than A's. "
        "--strict-time turns this off.",
        "Category: personal, or other (folders listed under [categories] other in the pools "
        "file); empty when no categories are set. A pair is other only when both folders are.",
        "Folder cells link to the folder on this computer (file:// links).",
    ]
