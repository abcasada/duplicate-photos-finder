"""Excel report (report.xlsx): the text report as sortable, filterable sheets.

Needs the optional XlsxWriter package, imported only when a workbook is written.
Sizes are stored as bytes and percentages as rounded-down fractions, so sorting and
filtering work on real numbers while the cells read like the text report.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import quote

from dpf.grouping import (
    A_IN_B,
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
from dpf.report import (
    OFFLINE_NOTE,
    PAIR_LABELS,
    any_offline,
    duration,
    floor_percent,
    human_bytes,
    pair_label,
    plural,
    root_lines,
    run_lines,
    same_folder_stats,
    name_list,
    tree_label,
    tree_tags,
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


def write_xlsx(
    result: GroupingResult,
    groups: Sequence[DuplicateGroup],
    summary: dict,
    path: str,
) -> None:
    """Write report.xlsx. Raises ModuleNotFoundError if XlsxWriter is not installed.

    Unlike the text report, every tree and folder pair is listed (the sheets filter).
    """
    import xlsxwriter

    with xlsxwriter.Workbook(path, _WORKBOOK_OPTIONS) as wb:
        fmt = _Formats(wb)
        summary_ws = wb.add_worksheet(SHEETS[0])
        tags = tree_tags(result.trees)
        tables = [
            _trees(wb, fmt, result.trees),
            _pairs(wb, fmt, result.pairs, tags, result.near),
            _sequences(wb, fmt, result.pairs),
            _same_folder(wb, fmt, result.same_folder),
            _files(wb, fmt, groups),
        ]
        for t in tables:
            t.finish()
        _summary(summary_ws, fmt, result, summary, tables)


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

    def __init__(self, wb, fmt: _Formats, name: str, columns, freeze_cols: int = 0) -> None:
        self.ws = wb.add_worksheet(name)
        self.fmt = fmt
        self.name = name
        self.columns = columns
        self.freeze_cols = freeze_cols
        self.widths = [len(h) for h, _ in columns]
        self.row = 0
        self.links = 0
        self.dropped = 0
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


def _trees(wb, fmt: _Formats, trees: list[TreePair]) -> _Table:
    columns = [
        ("#", 6), ("Shared size", 13), ("Relation", None), ("Folder A", None),
        ("Folder B", None), *_SIDE_COLUMNS, ("Folder pairs", 9),
        ("Other files not compared", 12),
    ]  # fmt: skip
    t = _Table(wb, fmt, "Trees", columns, freeze_cols=1)
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
    wb, fmt: _Formats, pairs: list[FolderPair], tags: dict, near: float
) -> _Table:
    columns = [
        ("#", 7), ("Tree", 6), ("Shared size", 13), ("Relation", None), ("Names", 9),
        ("Folder A", None), ("Folder B", None), *_SIDE_COLUMNS, ("Only in A", 9),
        ("Only in B", 9), ("Other files not compared", 12), ("Runs", 7),
        ("First sequence", 60),
    ]  # fmt: skip
    t = _Table(wb, fmt, "Folder pairs", columns, freeze_cols=2)
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
    t.data_bar(2)
    return t


def _sequences(wb, fmt: _Formats, pairs: list[FolderPair]) -> _Table:
    columns = [
        ("Pair #", 7), ("Folder A", None), ("Folder B", None), ("First (A)", None),
        ("Last (A)", None), ("First (B)", None), ("Last (B)", None), ("Files", 8),
        ("Size", 12), ("Not matched inside", 12), ("Not matched names", None),
    ]  # fmt: skip
    t = _Table(wb, fmt, "Sequences", columns, freeze_cols=1)
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
    return t


def _same_folder(wb, fmt: _Formats, groups: list[DuplicateGroup]) -> _Table:
    columns = [("Folder", None), ("Groups", 9), ("Extra copies", 9), ("Redundant size", 12)]
    t = _Table(wb, fmt, "Same folder", columns)
    _, ranked = same_folder_stats(groups)
    ranked.sort(key=lambda r: (-r[3], -r[1], r[0]))
    for d, n_groups, extra, nbytes in ranked:
        if not t.next_row():
            continue
        t.folder(0, d)
        t.num(1, n_groups)
        t.num(2, extra)
        t.num(3, nbytes, fmt.size)
    t.data_bar(3)
    return t


def _files(wb, fmt: _Formats, groups: Sequence[DuplicateGroup]) -> _Table:
    columns = [
        ("Group", 8), ("Basis", None), ("Folder", None), ("Name", None), ("Size", 11),
        ("Modified", 19), ("Kind", 7), ("Names", 9),
    ]  # fmt: skip
    t = _Table(wb, fmt, "Files", columns, freeze_cols=1)
    for n, g in enumerate(groups, 1):
        names = "exact" if g.same_names else "similar"
        for f in g.files:
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
    return t


def _datetime(mtime: float) -> datetime | None:
    """Local naive datetime, or None where Excel can't show it (before 1900)."""
    try:
        dt = datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if dt.year >= 1900 else None


_SHEET_NOTES = {
    "Trees": "tree overview: folder pairs grouped by the trees they sit in",
    "Folder pairs": "every pair of folders sharing duplicates, largest first",
    "Sequences": "one row per run of consecutive matched files",
    "Same folder": "folders holding duplicates of their own files",
    "Files": "every duplicate file, one row each (as files.csv)",
}


def _summary(ws, fmt: _Formats, result: GroupingResult, summary: dict, tables) -> None:
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 24)
    ws.set_column(2, 2, 14)
    row = 0

    def line(label: str = "", value=None, value_format=None, label_format=None) -> None:
        nonlocal row
        if label:
            ws.write_string(row, 0, label, label_format or fmt.bold)
        if isinstance(value, str):
            ws.write_string(row, 1, value, value_format or fmt.text)
        elif value is not None:
            ws.write_number(row, 1, value, value_format or fmt.left_int)
        row += 1

    ws.write_string(row, 0, "dpf duplicate report", fmt.title)
    row += 2
    line("Mode", str(summary.get("mode", "?")))
    line("Matched on", str(summary.get("basis", "?")))
    if summary.get("pool"):
        line("Pool", str(summary["pool"]))
    for i, root in enumerate(root_lines(summary, align=False)):
        line("Roots" if i == 0 else "", root)
    if any_offline(summary):
        line("", OFFLINE_NOTE, fmt.warning)
    line("Files scanned", summary.get("files", 0))
    line("Total size", summary.get("bytes", 0), fmt.left_size)
    line("Duplicate groups", result.groups)
    line("Could free (keeping one copy)", result.redundant_bytes, fmt.left_size)
    if result.capped_groups:
        line(
            "",
            f"{plural(result.capped_groups, 'group')} spread over very many folders "
            "were paired only with their first folder",
        )
    line("Elapsed", duration(summary.get("elapsed", 0.0)))
    line("Near-identical threshold", f"{result.near * 100:g}% of each side")
    row += 1

    by_tree = Counter(tree_label(t) for t in result.trees)
    tree_bytes = Counter()
    for t in result.trees:
        tree_bytes[tree_label(t)] += t.bytes
    _counts(ws, fmt, row, "Tree pairs", by_tree, tree_bytes)
    row += len(by_tree) + 3

    by_pair = Counter(PAIR_LABELS.get(p.relation, p.relation) for p in result.pairs)
    pair_bytes = Counter()
    for p in result.pairs:
        pair_bytes[PAIR_LABELS.get(p.relation, p.relation)] += p.bytes
    order = [PAIR_LABELS[r] for r in (IDENTICAL_TREE, IDENTICAL, A_IN_B, B_IN_A, NEAR_IDENTICAL,
                                      PARTIAL)]  # fmt: skip
    ranked = {k: by_pair[k] for k in order if k in by_pair}
    ranked.update({k: v for k, v in by_pair.items() if k not in ranked})
    _counts(ws, fmt, row, "Folder pairs", ranked, pair_bytes)
    row += len(ranked) + 3

    ws.write_string(row, 0, "Sheets", fmt.bold)
    row += 1
    for t in tables:
        ws.write_url(row, 0, f"internal:'{t.name}'!A1", fmt.link, string=t.name)
        ws.write_string(row, 1, _SHEET_NOTES[t.name], fmt.text)
        row += 1
        if t.dropped:
            ws.write_string(
                row, 1,
                f"{plural(t.dropped, 'row')} left out: Excel's row limit. See the CSV/JSON.",
                fmt.amber,
            )  # fmt: skip
            row += 1
    row += 1

    ws.write_string(row, 0, "How to read this", fmt.bold)
    row += 1
    for note in _notes(result.near):
        ws.write_string(row, 0, note, fmt.text)
        row += 1
    row += 1

    outputs = [(k, summary.get(f"{k.lower()}_path")) for k in ("CSV", "JSON")]
    outputs = [(k, v) for k, v in outputs if v]
    if outputs:
        ws.write_string(row, 0, "Other reports", fmt.bold)
        row += 1
        for k, v in outputs:
            line(k, v, label_format=fmt.text)


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
        "Folder cells link to the folder on this computer (file:// links).",
    ]
