"""Command-line interface: `dpf scan` and `dpf find`."""

from __future__ import annotations

import argparse
import os
import sys
import time

from dpf import __version__
from dpf.index import Index, default_db_path
from dpf.report import format_text, human_bytes, write_csv, write_json

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

    f = sub.add_parser("find", parents=[common], help="scan PATHs and report duplicates")
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
        "-o", "--out", default=None, metavar="DIR",
        help="report directory (default: ~/.cache/dpf/reports/<timestamp>-<mode>)",
    )  # fmt: skip
    return p


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


def _scan(idx: Index, roots: list[str], args: argparse.Namespace) -> None:
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
    path: str, roots: list[str], what: str, *, allow_same_drive: bool = False
) -> None:
    """Refuse to write `path` inside a scanned root or onto a scanned drive.

    Scanned drives may be nearly full and must stay untouched. The drive holding the
    home directory is exempt so scanning e.g. ~/Pictures can still use ~/.cache.
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
        if _device(root) == dev:
            raise SystemExit(
                f"dpf: refusing to write {what} onto the drive holding {root} "
                "(use --allow-same-drive to override)"
            )


def _find(idx: Index, roots: list[str], args: argparse.Namespace) -> int:
    from dpf.grouping import group_duplicates
    from dpf.match_name import find_name_duplicates

    mode = MODES[args.mode]
    if mode != "name":
        print("dpf: mode 2/3 (content comparison) is not implemented yet", file=sys.stderr)
        return 2

    out = args.out or _default_out_dir(mode)
    _check_output_location(out, roots, "reports", allow_same_drive=args.allow_same_drive)

    started = time.monotonic()
    if args.no_scan:
        if idx.totals(roots)[0] == 0:
            print("dpf: nothing indexed under these paths; run without --no-scan", file=sys.stderr)
            return 1
    else:
        _scan(idx, roots, args)

    groups = list(
        find_name_duplicates(
            idx, roots,
            mtime_tolerance=args.mtime_tolerance,
            ignore_size=args.ignore_size,
            ignore_mtime=args.ignore_mtime,
        )
    )  # fmt: skip
    result = group_duplicates(groups, idx.list_dir, idx.subdirs)
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
        "files": files,
        "bytes": nbytes,
        "elapsed": time.monotonic() - started,
        "csv_path": csv_path,
        "json_path": json_path,
    }
    write_json(result, summary, json_path)
    text = format_text(result, summary, top=args.top, min_files=args.min_files, max_runs=args.max_runs)
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text, end="" if text.endswith("\n") else "\n")
    if not args.quiet:
        print(f"report saved to {text_path}", file=sys.stderr)
    return 0


def _basis(args: argparse.Namespace) -> str:
    parts = ["name"]
    if not args.ignore_size:
        parts.append("size")
    if args.ignore_mtime:
        return "+".join(parts)
    return "+".join(parts + ["mtime"]) + f" ±{args.mtime_tolerance:g}s"


def main(argv: list[str] | None = None) -> int:
    from dpf.scanner import normalize_roots

    args = _build_parser().parse_args(argv)
    try:
        roots = normalize_roots(args.roots)
    except ValueError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return 1
    _check_output_location(
        args.db or default_db_path(), roots, "the index", allow_same_drive=args.allow_same_drive
    )
    try:
        with Index(args.db) as idx:
            if args.command == "scan":
                _scan(idx, roots, args)
                return 0
            return _find(idx, roots, args)
    except KeyboardInterrupt:
        _end_progress(False)
        print("dpf: interrupted", file=sys.stderr)
        return 130
