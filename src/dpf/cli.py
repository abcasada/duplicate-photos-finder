"""Command-line interface: `dpf scan`, `dpf find` and `dpf drives`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Collection

from dpf import __version__
from dpf.index import Index, default_db_path
from dpf.grouping import DEFAULT_NEAR, GroupingResult
from dpf.models import DuplicateGroup, RootStats
from dpf.report import (
    FROM_INDEX,
    SCANNED_NOW,
    TREE_TOP,
    format_text,
    human_bytes,
    scan_date,
    write_csv,
    write_json,
)

MODES = {"1": "name", "name": "name", "2": "content", "content": "content", "3": "both", "both": "both"}
MODE_TITLES = {
    "name": "1 (name/date)",
    "content": "2 (content comparison)",
    "both": "3 (name/date, then content)",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dpf",
        description="Find duplicate photos and videos across drives, reported as folder sequences.",
    )
    p.add_argument("--version", action="version", version=f"dpf {__version__}")
    p.add_argument("--db", default=None, help=f"index database (default: {default_db_path()})")
    p.add_argument(
        "--allow-same-drive", action="store_true",
        help="allow the index/reports on a drive being scanned (never inside a scanned folder)",
    )  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("roots", nargs="+", metavar="PATH", help="folders or drive mount points")
    common.add_argument(
        "--all-files", action="store_true", help="index every file, not just photos/videos"
    )
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    sub.add_parser("scan", parents=[common], help="index files under PATHs (read-only)")

    d = sub.add_parser(
        "drives", help="list the drives/folders in the index", description="List every "
        "scanned root in the index, or remove one from it (the drive itself is never touched).",
    )  # fmt: skip
    d.add_argument(
        "--forget", nargs="+", metavar="PATH",
        help="remove these roots and their files from the index (e.g. renamed or retired "
        "drives); each must be listed by `dpf drives` exactly",
    )  # fmt: skip

    f = sub.add_parser(
        "find", parents=[common], help="scan PATHs and report duplicates",
        description="Scan the connected PATHs and report duplicates. PATHs that are not "
        "connected (e.g. unplugged drives) are taken from the index.",
    )  # fmt: skip
    f.add_argument(
        "-m", "--mode", default="1", choices=sorted(MODES),
        help="1/name: name+size+date (fast, metadata only); 2/content: compare file contents; "
        "3/both: mode 1 then mode 2 (default: 1)",
    )  # fmt: skip
    f.add_argument(
        "--compare", choices=["exact", "visual", "both"], default="exact",
        help="mode 2: exact copies, visually similar, or both (default: exact)",
    )  # fmt: skip
    f.add_argument(
        "--no-scan", action="store_true", help="reuse the existing index instead of rescanning"
    )
    f.add_argument(
        "--mtime-tolerance", type=float, default=2.0, metavar="SEC",
        help="max modification-time difference for a name/date match (default: 2)",
    )  # fmt: skip
    f.add_argument("--ignore-size", action="store_true", help="mode 1: don't require equal size")
    f.add_argument("--ignore-mtime", action="store_true", help="mode 1: don't compare dates")
    f.add_argument("--top", type=int, default=None, metavar="N", help="show only N folder pairs")
    f.add_argument(
        "--min-files", type=int, default=1, metavar="N",
        help="hide folder pairs sharing fewer than N files (default: 1)",
    )  # fmt: skip
    f.add_argument(
        "--max-runs", type=int, default=8, metavar="N",
        help="sequences listed per folder pair before summarising (default: 8)",
    )  # fmt: skip
    f.add_argument(
        "--near", type=_fraction, default=DEFAULT_NEAR, metavar="FRACTION",
        help="call folders/trees near-identical when at least this share of each side "
        f"matched; 1 disables (default: {DEFAULT_NEAR:g})",
    )  # fmt: skip
    f.add_argument(
        "--tree-top", type=int, default=TREE_TOP, metavar="N",
        help=f"tree pairs shown in the tree overview; 0 hides it (default: {TREE_TOP})",
    )  # fmt: skip
    f.add_argument(
        "--no-xlsx", action="store_true", help="don't write report.xlsx (needs XlsxWriter)"
    )
    f.add_argument(
        "-o", "--out", default=None, metavar="DIR",
        help="report directory (default: ~/.cache/dpf/reports/<timestamp>-<mode>)",
    )  # fmt: skip
    return p


def _fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError(f"expected a fraction in (0, 1], got {text!r}")
    return value


def _progress_printer(quiet: bool):
    if quiet:
        return None
    tty = sys.stderr.isatty()
    last_root = [None]

    def progress(root: str, files: int, nbytes: int) -> None:
        line = f"scanning {root}: {files:,} files, {human_bytes(nbytes)}"
        if tty:
            sys.stderr.write("\r\033[K" + line)
        elif root != last_root[0]:
            sys.stderr.write(line + "\n")
        last_root[0] = root
        sys.stderr.flush()

    return progress


def _end_progress(quiet: bool) -> None:
    if not quiet and sys.stderr.isatty():
        sys.stderr.write("\r\033[K")


def _scan(idx: Index, roots: list[str], args: argparse.Namespace) -> list[RootStats]:
    from dpf.scanner import scan_roots

    errors: list[str] = []
    stats = scan_roots(
        idx, roots, all_files=args.all_files, progress=_progress_printer(args.quiet), errors=errors
    )
    _end_progress(args.quiet)
    for s in stats:
        if s.files == 0 and s.errors:
            print(f"dpf: warning: could not read {s.root}", file=sys.stderr)
        elif not args.quiet:
            errs = f", {s.errors:,} unreadable" if s.errors else ""
            print(f"indexed {s.root}: {s.files:,} files, {human_bytes(s.bytes)}{errs}", file=sys.stderr)
    if errors and not args.quiet:
        for e in errors[:5]:
            print(f"  unreadable: {e}", file=sys.stderr)
        if len(errors) > 5:
            print(f"  … and {len(errors) - 5:,} more", file=sys.stderr)
    return stats


def _default_out_dir(mode: str) -> str:
    base = os.path.dirname(default_db_path())
    return os.path.join(base, "reports", time.strftime("%Y%m%d-%H%M%S") + f"-{mode}")


def _device(path: str) -> int:
    """st_dev of `path`, or of its nearest existing ancestor if it doesn't exist yet."""
    path = os.path.realpath(path)
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return os.stat(path).st_dev


def _check_output_location(
    path: str,
    roots: list[str],
    what: str,
    *,
    allow_same_drive: bool = False,
    offline: Collection[str] = (),
) -> None:
    """Refuse to write `path` inside a scanned root or onto a scanned drive.

    Scanned drives may be nearly full and must stay untouched. The drive holding the
    home directory is exempt so scanning e.g. ~/Pictures can still use ~/.cache.
    `offline` roots (not connected) are only checked by path.
    """
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            raise SystemExit(f"dpf: refusing to write {what} inside scanned path {root}")
    if allow_same_drive:
        return
    dev = _device(real)
    if dev == _device(os.path.expanduser("~")):
        return
    for root in roots:
        if root not in offline and _device(root) == dev:
            raise SystemExit(
                f"dpf: refusing to write {what} onto the drive holding {root} "
                "(use --allow-same-drive to override)"
            )


def _find(idx: Index, roots: list[str], offline: set[str], args: argparse.Namespace) -> int:
    from dpf.grouping import group_duplicates
    from dpf.match_name import find_name_duplicates

    mode = MODES[args.mode]
    if mode != "name":
        print("dpf: mode 2/3 (content comparison) is not implemented yet", file=sys.stderr)
        return 2

    out = args.out or _default_out_dir(mode)
    _check_output_location(
        out, roots, "reports", allow_same_drive=args.allow_same_drive, offline=offline
    )

    started = time.monotonic()
    online = [r for r in roots if r not in offline]
    if not args.quiet:
        for r in roots:
            if r in offline:
                when = idx.containing_root(r)
                print(
                    f"using index for {r} (not connected; last scanned "
                    f"{scan_date(when and when.scanned_at)})",
                    file=sys.stderr,
                )  # fmt: skip
    scanned: dict[str, RootStats] = {}
    if args.no_scan:
        for r in online:
            if not idx.indexed(r):
                print(f"dpf: nothing indexed under {r}; run without --no-scan", file=sys.stderr)
                return 1
    elif online:
        scanned = {s.root: s for s in _scan(idx, online, args)}

    groups = list(
        find_name_duplicates(
            idx, roots,
            mtime_tolerance=args.mtime_tolerance,
            ignore_size=args.ignore_size,
            ignore_mtime=args.ignore_mtime,
        )
    )  # fmt: skip
    result = group_duplicates(
        groups, idx.list_dir, idx.subdirs, idx.dir_extras,
        roots=roots, near=args.near, tree_totals=lambda d: idx.totals([d]),
    )  # fmt: skip
    files, nbytes = idx.totals(roots)

    os.makedirs(out, exist_ok=True)
    csv_path = os.path.join(out, "files.csv")
    json_path = os.path.join(out, "report.json")
    text_path = os.path.join(out, "report.txt")
    write_csv(groups, csv_path)
    summary = {
        "mode": MODE_TITLES[mode],
        "basis": _basis(args),
        "roots": roots,
        "root_status": _root_status(idx, roots, offline, scanned),
        "files": files,
        "bytes": nbytes,
        "elapsed": time.monotonic() - started,
        "csv_path": csv_path,
        "json_path": json_path,
    }
    xlsx_path = os.path.join(out, "report.xlsx")
    if not args.no_xlsx and _write_xlsx(result, groups, summary, xlsx_path):
        summary["xlsx_path"] = xlsx_path
    elif os.path.exists(xlsx_path):
        os.remove(xlsx_path)  # a stale workbook from an earlier run into the same --out
    write_json(result, summary, json_path)
    text = format_text(
        result, summary,
        top=args.top, min_files=args.min_files, max_runs=args.max_runs, tree_top=args.tree_top,
    )  # fmt: skip
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text, end="" if text.endswith("\n") else "\n")
    if not args.quiet:
        print(f"report saved to {text_path}", file=sys.stderr)
    return 0


def _root_status(
    idx: Index, roots: list[str], offline: set[str], scanned: dict[str, RootStats]
) -> list[dict]:
    """Per root: whether it was scanned now or taken from the index, and when."""
    status = []
    for r in roots:
        files, nbytes = idx.totals([r])
        if r in scanned:
            source, when = SCANNED_NOW, scanned[r].scanned_at
        else:
            stats = idx.containing_root(r)
            source, when = FROM_INDEX, stats and stats.scanned_at
        status.append({
            "root": r, "status": source, "connected": r not in offline,
            "scanned_at": when, "files": files, "bytes": nbytes,
        })  # fmt: skip
    return status


def _drives(idx: Index, args: argparse.Namespace) -> int:
    """`dpf drives`: list the indexed roots, or forget some of them."""
    known = idx.roots()
    if args.forget:
        return _forget(idx, known, args.forget)
    if not known:
        print("nothing indexed yet; run `dpf scan PATH`")
        return 0
    rows = [("Drive / folder", "Files", "Size", "Unreadable", "Last scanned", "Connected")]
    for r in known:
        files, nbytes = idx.totals([r.root])
        rows.append((
            r.root, f"{files:,}", human_bytes(nbytes), f"{r.errors:,}",
            scan_date(r.scanned_at), "yes" if os.path.isdir(r.root) else "no",
        ))  # fmt: skip
    widths = [max(len(row[c]) for row in rows) for c in range(len(rows[0]))]
    for row in rows:
        cells = [
            cell.ljust(w) if c in (0, 4, 5) else cell.rjust(w)
            for c, (cell, w) in enumerate(zip(row, widths))
        ]
        print("  ".join(cells).rstrip())
    from dpf.scanner import drop_nested

    files, nbytes = idx.totals(drop_nested(r.root for r in known))
    connected = sum(row[5] == "yes" for row in rows[1:])
    print(
        f"\n{len(known)} indexed, {connected} connected; "
        f"{files:,} files, {human_bytes(nbytes)} in total"
    )
    return 0


def _forget(idx: Index, known: list[RootStats], paths: list[str]) -> int:
    by_root = {r.root: r for r in known}
    chosen: list[RootStats] = []
    for p in paths:
        candidates = (os.path.abspath(p).rstrip("/") or "/", os.path.realpath(p))
        match = next((by_root[c] for c in candidates if c in by_root), None)
        if match is None:
            listing = "\n".join(f"  {r}" for r in by_root) or "  (none)"
            print(
                f"dpf: {p} is not an indexed drive/folder; `dpf drives --forget` needs one of "
                f"these exactly:\n{listing}",
                file=sys.stderr,
            )  # fmt: skip
            return 1
        parent = idx.containing_root(os.path.dirname(match.root))
        if parent is not None and match.root != "/":
            print(
                f"dpf: {match.root} lies inside indexed {parent.root}; forget that instead",
                file=sys.stderr,
            )  # fmt: skip
            return 1
        if match not in chosen:
            chosen.append(match)
    for r in chosen:
        files, nbytes = idx.totals([r.root])
        print(
            f"forgetting {r.root}: {files:,} files, {human_bytes(nbytes)} "
            f"(last scanned {scan_date(r.scanned_at)})"
        )
        idx.forget_root(r.root)
    print(f"removed {len(chosen)} from the index {idx.path}; the drives were not touched")
    return 0


def _write_xlsx(
    result: GroupingResult, groups: list[DuplicateGroup], summary: dict, path: str
) -> bool:
    """Write report.xlsx; False (with a hint) when the optional XlsxWriter is missing."""
    from dpf.xlsx_report import write_xlsx

    try:
        write_xlsx(result, groups, summary, path)
    except ModuleNotFoundError as e:
        if e.name != "xlsxwriter":
            raise
        print("dpf: report.xlsx skipped (install python3-xlsxwriter)", file=sys.stderr)
        return False
    return True


def _basis(args: argparse.Namespace) -> str:
    parts = ["name"]
    if not args.ignore_size:
        parts.append("size")
    if args.ignore_mtime:
        return "+".join(parts)
    return "+".join(parts + ["mtime"]) + f" ±{args.mtime_tolerance:g}s"


def _offline_root(idx: Index, path: str) -> str:
    """The indexed root for a `path` that is not connected; ValueError if never scanned.

    A missing mount point cannot be resolved, so the path is used as given (made
    absolute), falling back to resolving any symlinks in its existing parents.
    """
    for candidate in dict.fromkeys((os.path.abspath(path), os.path.realpath(path))):
        if idx.indexed(candidate):
            return candidate.rstrip("/") or "/"
    raise ValueError(f"{path} is not connected and has never been scanned")


def main(argv: list[str] | None = None) -> int:
    from dpf.scanner import drop_nested, split_roots

    args = _build_parser().parse_args(argv)
    if args.command == "drives":
        db = args.db or default_db_path()
        if not os.path.exists(db):
            if args.forget:
                print(f"dpf: nothing indexed yet ({db}); nothing to forget", file=sys.stderr)
                return 1
            print("nothing indexed yet; run `dpf scan PATH`")
            return 0
        with Index(db) as idx:
            return _drives(idx, args)
    try:
        online, missing = split_roots(args.roots)
    except ValueError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return 1
    if args.command == "scan" and missing:
        print(f"dpf: {missing[0]} is not connected; nothing to scan", file=sys.stderr)
        return 1
    _check_output_location(
        args.db or default_db_path(), online + [os.path.abspath(p) for p in missing],
        "the index", allow_same_drive=args.allow_same_drive,
        offline={os.path.abspath(p) for p in missing},
    )  # fmt: skip
    try:
        with Index(args.db) as idx:
            try:
                offline = {_offline_root(idx, p) for p in missing}
            except ValueError as e:
                print(f"dpf: {e}", file=sys.stderr)
                return 1
            roots = drop_nested([*online, *offline])
            offline &= set(roots)
            if args.command == "scan":
                _scan(idx, roots, args)
                return 0
            return _find(idx, roots, offline, args)
    except KeyboardInterrupt:
        _end_progress(False)
        print("dpf: interrupted", file=sys.stderr)
        return 130
