"""Reports: a folder-level text summary, plus JSON and CSV with full per-file detail."""

from __future__ import annotations

import csv
import dataclasses
import json
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from dpf.grouping import (
    A_IN_B,
    B_IN_A,
    DEFAULT_NEAR,
    IDENTICAL,
    IDENTICAL_TREE,
    NEAR_IDENTICAL,
    PARTIAL,
    TREE_A_MOSTLY_IN_B,
    TREE_B_MOSTLY_IN_A,
    TREE_NEAR,
    FolderPair,
    GroupingResult,
    Run,
    TreePair,
    is_near,
)
from dpf.models import DuplicateGroup

_UNITS = ("kB", "MB", "GB", "TB", "PB", "EB")
_SAME_FOLDER_TOP = 10
_INDENT = " " * 7
TREE_TOP = 20

CSV_FIELDS = (
    "group_id", "basis", "path", "dir", "name", "size", "mtime", "kind", "same_names",
)  # fmt: skip
SKIPPED_SHOWN = 3
SIMILAR_NAMES = "similar names"


def human_bytes(n: int) -> str:
    """Decimal units, as on drive labels: 512 B, 12.4 GB, 3.1 TB."""
    if n < 1000:
        return f"{n} B"
    value = float(n)
    for unit in _UNITS:
        value /= 1000
        if round(value, 1) < 1000 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


_PARSE_UNITS = {"": 1, "b": 1} | {
    unit.casefold(): 1000 ** (i + 1) for i, unit in enumerate(_UNITS)
}


def parse_bytes(text: str) -> int:
    """Bytes from "5090000000000", "500GB" or "5.09 TB": decimal units, as in human_bytes.

    Binary sizes (TiB, or df -h's bare "T") are refused rather than guessed.
    """
    m = re.fullmatch(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([A-Za-z]*)\s*", text)
    unit = m and m[2].casefold()
    if not m or unit not in _PARSE_UNITS:
        raise ValueError(
            f"not a size: {text!r} (use bytes, as `df -B1` shows, or decimal units: 500GB, 5.09TB)"
        )
    return int((Decimal(m[1]) * _PARSE_UNITS[unit]).to_integral_value())


PAIR_LABELS = {
    IDENTICAL_TREE: "matching tree",
    IDENTICAL: "all photos/videos match",
    A_IN_B: "A is fully contained in B",
    B_IN_A: "B is fully contained in A",
    NEAR_IDENTICAL: "near-identical",
    PARTIAL: "partial overlap",
}


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"



SCANNED_NOW = "scanned now"
FROM_INDEX = "from index"
OFFLINE_NOTE = (
    "Some drives are not connected: their folder links won't open until they are plugged in."
)


def scan_date(scanned_at: float | None) -> str:
    """Local "YYYY-MM-DD HH:MM", or "date unknown"."""
    if scanned_at is None:
        return "date unknown"
    return datetime.fromtimestamp(scanned_at).strftime("%Y-%m-%d %H:%M")


def root_status_label(entry: dict) -> str:
    """E.g. "scanned now" or "from index, scanned 2026-10-24 21:43, not connected"."""
    if entry.get("status") == SCANNED_NOW:
        return SCANNED_NOW
    when = entry.get("scanned_at")
    text = f"{FROM_INDEX}, " + ("date unknown" if when is None else f"scanned {scan_date(when)}")
    if not entry.get("connected", True):
        text += ", not connected"
    return text


def root_lines(summary: dict, *, align: bool = True) -> list[str]:
    """One line per root with where its data came from (summary["root_status"]).

    Falls back to the bare summary["roots"] for summaries without root_status.
    """
    status = summary.get("root_status")
    if not status:
        return list(summary.get("roots") or [])
    width = max(len(e["root"]) for e in status) if align else 0
    return [f"{e['root']:<{width}}   ({root_status_label(e)})" for e in status]


def any_offline(summary: dict) -> bool:
    return any(not e.get("connected", True) for e in summary.get("root_status") or [])


def floor_percent(part: int, whole: int) -> int:
    """Whole percent, rounded down so that 100 always means every file (0 if `whole` is 0)."""
    return part * 100 // whole if whole > 0 else 0


def _percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "0%"
    if 0 < part < whole and part * 100 < whole:
        return "<1%"
    return f"{floor_percent(part, whole)}%"


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _coverage(matched_a: int, total_a: int, matched_b: int, total_b: int) -> str:
    return f"{_percent(matched_a, total_a)} / {_percent(matched_b, total_b)}"


def contained_and_near(p: FolderPair, near: float = DEFAULT_NEAR) -> bool:
    """True if one side is fully contained and the other, not complete, is near-matched."""
    if p.relation == A_IN_B:
        return p.matched_b < p.total_b and is_near(p.matched_b, p.total_b, near)
    if p.relation == B_IN_A:
        return p.matched_a < p.total_a and is_near(p.matched_a, p.total_a, near)
    return False


def pair_label(p: FolderPair, near: float = DEFAULT_NEAR) -> str:
    """Short relation label without counts, e.g. "A is fully contained in B; near-identical"."""
    label = PAIR_LABELS.get(p.relation, p.relation)
    return label + "; near-identical" if contained_and_near(p, near) else label


def _describe(p: FolderPair, near: float = DEFAULT_NEAR) -> str:
    """Relation label. File contents are not compared in mode 1, so never "identical"."""
    only_a, only_b = p.total_a - p.matched_a, p.total_b - p.matched_b
    cover = _coverage(p.matched_a, p.total_a, p.matched_b, p.total_b)
    text = PAIR_LABELS.get(p.relation, p.relation)
    if p.relation == IDENTICAL_TREE:
        text += f": {plural(p.folders, 'folder')}, {plural(p.matched_a, 'file')}"
    elif p.relation == IDENTICAL:
        text += f", {plural(p.matched_a, 'file')}"
    elif p.relation == NEAR_IDENTICAL:
        text += f": {cover}; {plural(only_a, 'file')} only in A, {only_b:,} only in B"
    elif contained_and_near(p, near):
        only = (only_b, "B") if p.relation == A_IN_B else (only_a, "A")
        text += f"; near-identical: {cover}, {plural(only[0], 'file')} only in {only[1]}"
    if p.similar_names:
        text += f"; {SIMILAR_NAMES}"
    if p.unindexed:
        text += f"; {plural(p.unindexed, 'other file')} not compared"
    return text


def tree_label(t: TreePair) -> str:
    """Tree pair relation label without counts, e.g. "A largely within B"."""
    if t.relation == TREE_NEAR:
        return "near-identical trees"
    if t.relation == TREE_A_MOSTLY_IN_B:
        return "A within B" if t.matched_a == t.total_a else "A largely within B"
    if t.relation == TREE_B_MOSTLY_IN_A:
        return "B within A" if t.matched_b == t.total_b else "B largely within A"
    return "overlapping trees"


def _describe_tree(t: TreePair) -> str:
    text = (
        f"{tree_label(t)}: {_coverage(t.matched_a, t.total_a, t.matched_b, t.total_b)}, "
        f"{plural(t.folders, 'folder pair')}, {plural(t.matched_a, 'file')}"
    )
    if t.unindexed:
        text += f"; {plural(t.unindexed, 'other file')} not compared"
    return text


def _covers(matched_a: int, total_a: int, matched_b: int, total_b: int) -> str:
    return (
        f"{_INDENT}covers {_percent(matched_a, total_a)} of A ({matched_a:,}/{total_a:,}), "
        f"{_percent(matched_b, total_b)} of B ({matched_b:,}/{total_b:,})"
    )


def _span(first: str, last: str) -> str:
    return first if first == last else f"{first} … {last}"


def name_list(names: tuple[str, ...], shown: int = SKIPPED_SHOWN) -> str:
    """Up to `shown` names, then "+N more"."""
    listed = ", ".join(names[:shown])
    if len(names) > shown:
        listed += f", +{len(names) - shown:,} more"
    return listed


def skipped_note(names: tuple[str, ...]) -> str:
    """E.g. "+1 inside not matched: DSC00003.ARW" for files passed over inside a run."""
    return f"+{len(names):,} inside not matched: {name_list(names)}"


def run_lines(
    runs: tuple[Run, ...], max_runs: int, *, indent: str = _INDENT, align: bool = True
) -> list[str]:
    """One line per run, up to `max_runs`, then an "… and N more runs" line.

    Files passed over inside a run (Run.skipped) are named on its line. `align` pads
    names and counts into columns (for monospaced text).
    """
    shown = runs[:max_runs]
    if not shown:
        return []
    labels = [_span(r.a_first, r.a_last) for r in shown]
    label_w = max(len(s) for s in labels) if align else 0
    count_w = max(len(f"{r.count:,}") for r in shown) if align else 0
    lines = []
    for r, label in zip(shown, labels):
        unit = "file" if r.count == 1 else "files"
        if align:
            unit = unit.ljust(5)
        line = f"{indent}{label:<{label_w}}  {r.count:>{count_w},} {unit}"
        if r.skipped:
            line = f"{line.rstrip()}  ({skipped_note(r.skipped)})"
        if not r.same_names:
            line += f"  → in B: {_span(r.b_first, r.b_last)}"
        lines.append(line.rstrip())
    rest = runs[max_runs:]
    if rest:
        files = sum(r.count for r in rest)
        more = plural(len(rest), "more run")
        skipped = sum(len(r.skipped) for r in rest)
        extra = f"; {skipped:,} inside not matched" if skipped else ""
        lines.append(f"{indent}… and {more} ({plural(files, 'file')}{extra})")
    return lines


def _pair_block(
    n: int, num_w: int, p: FolderPair, max_runs: int, near: float, tree: str | None
) -> list[str]:
    num = f"[{n}]".ljust(num_w)
    head = f"{num}  {human_bytes(p.bytes):>8}  {p.dir_a}  ↔  {p.dir_b}   ({_describe(p, near)})"
    if tree:
        head += f"  (in {tree})"
    if p.relation in (IDENTICAL, IDENTICAL_TREE):
        return [head]
    lines = [head, *run_lines(p.runs, max_runs)]
    lines.append(_covers(p.matched_a, p.total_a, p.matched_b, p.total_b))
    return lines


def _tree_overview(trees: list[TreePair], top: int, near: float) -> tuple[list[str], dict]:
    """Overview lines, and (dir_a, dir_b) -> tree label for the folder pairs shown in it."""
    shown = trees[: max(top, 0)]
    if not shown:
        return [], {}
    out = [
        f"Tree overview: {plural(len(trees), 'tree pair')} holding several folder pairs "
        "(largest first)",
        "  Folders are grouped by the trees they sit in, matched at the same relative path.",
        "  A / B % = share of all photos/videos indexed under each tree that matched;",
        f"  near-identical = at least {near * 100:g}% of both. Check the folder pairs before",
        "  deleting: other files and unmatched subfolders are not listed here.",
        "",
    ]
    num_w = len(f"[T{len(shown)}]")
    for n, t in enumerate(shown, 1):
        num = f"[T{n}]".ljust(num_w)
        out.append(
            f"{num}  {human_bytes(t.bytes):>8}  {t.dir_a}  ↔  {t.dir_b}   ({_describe_tree(t)})"
        )
        out.append(_covers(t.matched_a, t.total_a, t.matched_b, t.total_b))
        out.append("")
    if len(trees) > len(shown):
        more = plural(len(trees) - len(shown), "more tree pair")
        out.append(f"… {more} not shown (tree top {top})")
        out.append("")
    return out, tree_tags(shown)


def tree_tags(trees: Iterable[TreePair]) -> dict[tuple[str, str], int]:
    """(dir_a, dir_b) of each member folder pair -> 1-based number of its tree (T1, T2…)."""
    return {key: n for n, t in enumerate(trees, 1) for key in t.members}


def same_folder_stats(groups: list[DuplicateGroup]) -> tuple[int, list[tuple[str, int, int, int]]]:
    """(redundant bytes, [(dir, groups, extra copies, redundant bytes)] by group count)."""
    per_dir: dict[str, list[int]] = {}
    total = 0
    for g in groups:
        sizes: dict[str, list[int]] = {}
        for f in g.files:
            sizes.setdefault(f.dir, []).append(f.size)
        for d, s in sizes.items():
            if len(s) < 2:
                continue
            extra = sum(s) - max(s)
            total += extra
            stats = per_dir.setdefault(d, [0, 0, 0])
            stats[0] += 1
            stats[1] += len(s) - 1
            stats[2] += extra
    ranked = sorted(per_dir.items(), key=lambda kv: (-kv[1][0], -kv[1][2], kv[0]))
    return total, [(d, *s) for d, s in ranked]


def format_text(
    result: GroupingResult,
    summary: dict,
    *,
    top: int | None = None,
    min_files: int = 1,
    max_runs: int = 8,
    tree_top: int = TREE_TOP,
) -> str:
    """Folder-level report. Never lists every file; the CSV/JSON hold that detail.

    A tree overview of up to `tree_top` tree pairs precedes the folder pairs.
    """
    out: list[str] = ["dpf duplicate report", ""]
    out.append(f"Mode:        {summary.get('mode', '?')}; matched on {summary.get('basis', '?')}")
    if summary.get("pool"):
        out.append(f"Pool:        {summary['pool']}")
    for i, line in enumerate(root_lines(summary)):
        out.append(f"{'Roots:' if i == 0 else '':<13}{line}")
    if any_offline(summary):
        out.append(f"{'':<13}{OFFLINE_NOTE}")
    out.append(
        f"Scanned:     {plural(summary.get('files', 0), 'file')}, "
        f"{human_bytes(summary.get('bytes', 0))}"
    )
    out.append(
        f"Duplicates:  {plural(result.groups, 'group')}; could free "
        f"~{human_bytes(result.redundant_bytes)} by keeping one copy of each"
    )
    if result.capped_groups:
        out.append(
            f"             {plural(result.capped_groups, 'group')} spread over very many "
            "folders were paired only with their first folder"
        )
    out.append(f"Elapsed:     {duration(summary.get('elapsed', 0.0))}")
    out.append("")

    overview, tags = _tree_overview(result.trees, tree_top, result.near)
    out.extend(overview)

    eligible = [p for p in result.pairs if max(p.matched_a, p.matched_b) >= min_files]
    hidden_small = len(result.pairs) - len(eligible)
    shown = eligible if top is None else eligible[: max(top, 0)]
    if not result.pairs:
        out.append("No duplicates shared between folders.")
    else:
        out.append(
            f"Folder pairs sharing duplicates: {len(result.pairs):,} "
            "(largest first; A is the left folder, B the right)"
        )
        out.append("")
        num_w = len(f"[{len(shown)}]")
        for n, p in enumerate(shown, 1):
            tag = tags.get((p.dir_a, p.dir_b))
            block = _pair_block(n, num_w, p, max_runs, result.near, tag and f"T{tag}")
            if len(block) > 1 and out[-1] != "":
                out.append("")
            out.extend(block)
            if len(block) > 1:
                out.append("")
        if out[-1] != "":
            out.append("")
        omitted = len(eligible) - len(shown)
        if omitted:
            out.append(f"… {plural(omitted, 'more folder pair')} not shown (top {top})")
        if hidden_small:
            out.append(
                f"{plural(hidden_small, 'folder pair')} with fewer than "
                f"{plural(min_files, 'matched file')} hidden"
            )
        if omitted or hidden_small:
            out.append("")

    if result.same_folder:
        total, ranked = same_folder_stats(result.same_folder)
        out.append(
            f"Same-folder duplicates: {plural(len(result.same_folder), 'group')}, "
            f"{human_bytes(total)} redundant, in {plural(len(ranked), 'folder')}"
        )
        for d, n_groups, extra, nbytes in ranked[:_SAME_FOLDER_TOP]:
            unit = "group " if n_groups == 1 else "groups"
            out.append(f"  {n_groups:>6,} {unit}  {human_bytes(nbytes):>8}  {d}")
        if len(ranked) > _SAME_FOLDER_TOP:
            out.append(f"  … and {plural(len(ranked) - _SAME_FOLDER_TOP, 'more folder')}")
        out.append("")

    csv_path, json_path = summary.get("csv_path"), summary.get("json_path")
    xlsx_path = summary.get("xlsx_path")
    if csv_path or json_path or xlsx_path:
        out.append("Per-file detail:")
        if csv_path:
            out.append(f"  CSV:  {csv_path}")
        if json_path:
            out.append(f"  JSON: {json_path}")
        if xlsx_path:
            out.append(f"  XLSX: {xlsx_path}")
    else:
        out.append("Per-file detail is in the CSV/JSON reports.")
    return "\n".join(out) + "\n"


def _group_files(g: DuplicateGroup) -> list[dict]:
    return [
        {"path": f.path, "size": f.size, "mtime": f.mtime, "kind": f.kind} for f in g.files
    ]


def write_json(result: GroupingResult, summary: dict, path: str) -> None:
    """Everything structured: summary, tree pairs, folder pairs with runs, same-folder groups."""
    data = {
        "summary": summary,
        "groups": result.groups,
        "redundant_bytes": result.redundant_bytes,
        "capped_groups": result.capped_groups,
        "near": result.near,
        "trees": [dataclasses.asdict(t) for t in result.trees],
        "pairs": [dataclasses.asdict(p) for p in result.pairs],
        "same_folder": [
            {
                "basis": g.basis,
                "same_names": g.same_names,
                "size": g.size,
                "redundant_bytes": g.redundant_bytes,
                "files": _group_files(g),
            }
            for g in result.same_folder
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
        fh.write("\n")


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")


def write_csv(groups: Iterable[DuplicateGroup], path: str) -> int:
    """One row per file of every group; returns the number of groups written."""
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for n, g in enumerate(groups, 1):
            same = "yes" if g.same_names else "no"
            for f in g.files:
                w.writerow(
                    (n, g.basis, f.path, f.dir, f.name, f.size, _iso(f.mtime), f.kind, same)
                )
    return n
