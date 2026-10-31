"""Command-line interface: `dpf scan`, `find`, `drives`, `space`, `folders` and `pools`."""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from collections.abc import Collection
from dataclasses import dataclass, field

from dpf import __version__
from dpf.categories import (
    ALL,
    CATEGORIES,
    OTHER,
    PERSONAL,
    Categories,
    load_categories,
    of_dirs,
    tally,
)
from dpf.index import Index, default_db_path
from dpf.grouping import DEFAULT_NEAR, GroupingResult
from dpf.coverage import (
    NOT_BACKED_UP,
    NOT_IN_OTHER,
    ONLY_IN_BACKUP,
    Coverage,
    same_drive_warnings,
    stale_warnings,
)
from dpf.models import SPACE_MANUAL, DriveSpace, DuplicateGroup, RootStats
from dpf.pools import (
    BACKUP,
    Pool,
    PoolError,
    Pools,
    coverage_partners,
    default_pools_path,
    get_pool,
    is_glob,
    load_pools,
    matches,
    pool_paths,
    pools_of,
)
from dpf.report import (
    COVERAGE_TOP,
    FROM_INDEX,
    SCANNED_NOW,
    TREE_TOP,
    footer_lines,
    format_coverage,
    format_text,
    human_bytes,
    parse_bytes,
    plural,
    scan_date,
    write_csv,
    write_json,
    write_missing_csv,
    write_pool_csv,
    write_pools_json,
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
    p.add_argument(
        "--pools-file", default=None, metavar="PATH",
        help=f"drive pool definitions (default: {default_pools_path()})",
    )  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("roots", nargs="*", metavar="PATH", help="folders or drive mount points")
    common.add_argument(
        "--pool", metavar="NAME", action="append",
        help="use the drives of this pool (see `dpf pools`) instead of PATHs; repeat it "
        "(--pool A --pool B) to handle several pools in one run, each searched on its own",
    )  # fmt: skip
    common.add_argument(
        "--all-files", action="store_true", help="index every file, not just photos/videos"
    )
    common.add_argument(
        "--allow-empty", action="store_true",
        help="accept a scan that finds a previously indexed drive/folder completely empty "
        "(by default its index is kept, since an empty mount folder usually means the "
        "drive is not mounted)",
    )  # fmt: skip
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    sub.add_parser(
        "scan", parents=[common], help="index files under PATHs (read-only)",
        description="Index the files under PATHs, or under the connected drives of --pool.",
    )  # fmt: skip

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
        description="Scan the connected PATHs (or --pool drives) and report duplicates. "
        "PATHs that are not connected (e.g. unplugged drives) are taken from the index.",
    )  # fmt: skip
    f.add_argument(
        "-m", "--mode", default="1", choices=sorted(MODES),
        help="1/name: similar name+size+date (fast, metadata only); 2/content: compare file "
        "contents; 3/both: mode 1 then mode 2 (default: 1)",
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
    f.add_argument(
        "--strict-time", action="store_true",
        help="mode 1: dates must match within the tolerance; by default copies whose dates "
        "differ by whole hours (1-14 h, time zones/DST) also match",
    )  # fmt: skip
    f.add_argument(
        "--category", choices=[*CATEGORIES, ALL], default=ALL,
        help="report only personal media, only other media (folders listed under "
        "[categories] other in the pools file), or all (default: all)",
    )  # fmt: skip
    f.add_argument("--ignore-size", action="store_true", help="mode 1: don't require equal size")
    f.add_argument("--ignore-mtime", action="store_true", help="mode 1: don't compare dates")
    f.add_argument(
        "--exact-names", action="store_true",
        help="mode 1: names must be equal (ignoring case); by default copy marks such as "
        "' (1)' or ' - Copy' and text around a camera file number (DSC01234) are ignored",
    )  # fmt: skip
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
        "--no-coverage", action="store_true",
        help="skip the backup coverage check (files with no copy in the pool linked to this "
        "one by [backups] in the pools file)",
    )  # fmt: skip
    f.add_argument(
        "--coverage-against", metavar="POOL",
        help="check coverage against this pool (taken from the index, never scanned), "
        "instead of the pools linked by [backups]; works with PATHs too",
    )  # fmt: skip
    f.add_argument(
        "--coverage-top", type=int, default=COVERAGE_TOP, metavar="N",
        help=f"folders listed per coverage section in report.txt (default: {COVERAGE_TOP})",
    )  # fmt: skip
    f.add_argument(
        "--no-xlsx", action="store_true", help="don't write report.xlsx (needs XlsxWriter)"
    )
    f.add_argument(
        "-o", "--out", default=None, metavar="DIR",
        help="report directory (default: ~/.cache/dpf/reports/<timestamp>-<mode>)",
    )  # fmt: skip
    s = sub.add_parser(
        "space", help="record each drive's used and total space (no scan)",
        description="Record the used and total space of indexed drives without scanning "
        "them: one statvfs call per connected drive, no file reads. Only drives already in "
        "the index (see `dpf drives`) are updated; scans record their space too.",
    )  # fmt: skip
    s.add_argument(
        "roots", nargs="*", metavar="PATH",
        help="indexed drives to measure (default: every connected indexed drive)",
    )  # fmt: skip
    s.add_argument("--pool", metavar="NAME", help="measure the indexed drives of this pool")
    s.add_argument(
        "--missing", action="store_true", help="only drives with no space recorded yet"
    )
    s.add_argument(
        "--set", dest="set_path", metavar="PATH",
        help="enter the space of indexed drive PATH by hand, without touching the drive "
        "(e.g. one that may be failing); needs --used and --capacity",
    )  # fmt: skip
    s.add_argument("--used", type=_size, metavar="SIZE", help="with --set: e.g. 5.09TB")
    s.add_argument("--capacity", type=_size, metavar="SIZE", help="with --set: e.g. 8.00TB")

    fo = sub.add_parser(
        "folders", help="CSV of the folders two levels deep, to note what is personal",
        description="Write a CSV with one row per folder, two levels below each indexed "
        "drive: media counts, size, file dates and main types, plus blank Personal? and "
        "Notes columns to fill in. Reads the index only; the drives are not touched.",
    )  # fmt: skip
    fo.add_argument(
        "--pool", metavar="NAME", action="append",
        help="only the indexed drives of this pool (repeatable; default: every drive)",
    )  # fmt: skip
    fo.add_argument(
        "-o", "--out", default=None, metavar="FILE",
        help="CSV file (default: ~/.cache/dpf/reports/<timestamp>-folders/folders.csv)",
    )  # fmt: skip

    sub.add_parser(
        "pools", help="list the drive pools and their drives",
        description="List each pool from the pools file with its patterns and the drives "
        "they match, connected or only in the index.",
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


def _size(text: str) -> int:
    try:
        return parse_bytes(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


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


def _scan(
    idx: Index, roots: list[str], args: argparse.Namespace
) -> tuple[list[RootStats], dict[str, int]]:
    """Scan `roots`: (stats of the roots replaced, root -> files kept for empty-looking roots)."""
    from dpf.scanner import scan_roots

    errors: list[str] = []
    kept: dict[str, int] = {}
    stats = scan_roots(
        idx, roots, all_files=args.all_files, progress=_progress_printer(args.quiet),
        errors=errors, allow_empty=args.allow_empty, kept=kept,
    )  # fmt: skip
    _end_progress(args.quiet)
    for root, files in kept.items():
        print(
            f"dpf: {root} looks empty now but had {files:,} files when last scanned; keeping "
            "the index (not mounted, or unreadable?). Use --allow-empty to accept the empty scan.",
            file=sys.stderr,
        )  # fmt: skip
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
    return stats, kept


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


@dataclass(slots=True)
class _Target:
    """The roots of one duplicate search: a pool, or the PATHs given (pool None)."""

    pool: Pool | None
    roots: list[str]
    offline: set[str] = field(default_factory=set)

    @property
    def name(self) -> str | None:
        return self.pool.name if self.pool else None


@dataclass(slots=True)
class _CoveragePlan:
    """Check `target` against pool `other`, whose roots come from the index."""

    target: _Target
    other: Pool
    direction: str
    roots: list[str]
    never_scanned: list[str]
    in_run: _Target | None = None  # the other pool, when it is also searched in this run


@dataclass(slots=True)
class _PoolRun:
    target: _Target
    groups: list[DuplicateGroup]
    result: GroupingResult
    summary: dict


def _find(idx: Index, targets: list[_Target], pools: Pools, args: argparse.Namespace) -> int:
    from dpf.coverage import check_coverage
    from dpf.grouping import group_duplicates
    from dpf.match_name import find_name_duplicates

    mode = MODES[args.mode]
    if mode != "name":
        print("dpf: mode 2/3 (content comparison) is not implemented yet", file=sys.stderr)
        return 2

    cats = _load_categories(args)
    if cats is None:
        return 1
    if args.category != ALL and not cats:
        print(
            f"dpf: --category {args.category} needs [categories] other patterns in "
            f"{args.pools_file or default_pools_path()}",
            file=sys.stderr,
        )  # fmt: skip
        return 1
    category = cats if cats else None
    plans = _coverage_plans(idx, targets, pools, args)
    if plans is None:
        return 1
    out = args.out or _default_out_dir(mode)
    roots = [r for t in targets for r in t.roots]
    other = [r for p in plans if p.in_run is None for r in p.roots]
    _check_output_location(
        out, roots + other, "reports", allow_same_drive=args.allow_same_drive,
        offline={r for t in targets for r in t.offline} | set(other),
    )  # fmt: skip

    started = time.monotonic()
    if not args.quiet:
        for t in targets:
            for r in t.roots:
                if r in t.offline:
                    when = idx.containing_root(r)
                    print(
                        f"using index for {r} (not connected; last scanned "
                        f"{scan_date(when and when.scanned_at)})",
                        file=sys.stderr,
                    )  # fmt: skip
    scanned: dict[str, RootStats] = {}
    online = [r for t in targets for r in t.roots if r not in t.offline]
    if args.no_scan:
        for r in online:
            if not idx.indexed(r):
                print(f"dpf: nothing indexed under {r}; run without --no-scan", file=sys.stderr)
                return 1
    elif online:
        stats, kept = _scan(idx, online, args)
        scanned = {s.root: s for s in stats}
        for t in targets:  # taken from the index, like an unplugged drive
            t.offline |= kept.keys() & set(t.roots)

    warned: set[str] = set()

    def warn(messages: list[str]) -> list[str]:
        for m in messages:
            if m not in warned:
                warned.add(m)
                print(f"dpf: warning: {m}", file=sys.stderr)
        return messages

    runs: list[_PoolRun] = []
    status_of: dict[int, list[dict]] = {}
    for t in targets:
        groups = list(
            find_name_duplicates(
                idx, t.roots,
                mtime_tolerance=args.mtime_tolerance,
                ignore_size=args.ignore_size,
                ignore_mtime=args.ignore_mtime,
                exact_names=args.exact_names,
                hour_shift=not args.strict_time,
            )
        )  # fmt: skip
        if args.category != ALL:
            groups = [g for g in groups if _group_category(cats, g) == args.category]
        result = group_duplicates(
            groups, idx.list_dir, idx.subdirs, idx.dir_extras,
            roots=t.roots, near=args.near, tree_totals=lambda d: idx.totals([d]),
            category=category,
        )  # fmt: skip
        if args.category != ALL:
            result = result.only(args.category)
        files, nbytes = idx.totals(t.roots)
        status = _root_status(idx, t.roots, t.offline, scanned, category)
        status_of[id(t)] = status
        summary = {
            "mode": MODE_TITLES[mode],
            "basis": _basis(args),
            **({"pool": t.name} if t.name else {}),
            "roots": t.roots,
            "root_status": status,
            "files": files,
            "bytes": nbytes,
        }
        if category is not None:
            summary["categories"] = _sum_tallies(status)
            summary["category_patterns"] = list(cats.other)
        if args.category != ALL:
            summary["category"] = args.category
        if t.pool is not None:
            found = warn(same_drive_warnings(t.roots, t.name))
            if found:
                summary["warnings"] = found
        runs.append(_PoolRun(t, groups, result, summary))

    coverages: list[Coverage] = []
    for plan in plans:
        began = time.monotonic()
        cov = check_coverage(
            idx, plan.target.roots, plan.roots,
            pool=plan.target.name, other=plan.other.name, direction=plan.direction,
            mtime_tolerance=args.mtime_tolerance, ignore_size=args.ignore_size,
            ignore_mtime=args.ignore_mtime, exact_names=args.exact_names,
            hour_shift=not args.strict_time, category=category, only=args.category,
        )  # fmt: skip
        offline = plan.in_run.offline if plan.in_run else {
            r for r in plan.roots if not os.path.isdir(r)
        }  # fmt: skip
        cov.other_status = _root_status(idx, plan.roots, offline, scanned)
        cov.never_scanned = plan.never_scanned
        cov.warnings = warn(
            stale_warnings(status_of[id(plan.target)], cov)
            + same_drive_warnings(plan.roots, plan.other.name)
        )
        cov.elapsed = time.monotonic() - began
        coverages.append(cov)

    os.makedirs(out, exist_ok=True)
    paths = {
        "csv_path": os.path.join(out, "files.csv"),
        "json_path": os.path.join(out, "report.json"),
    }
    missing_path = os.path.join(out, "missing.csv")
    if coverages:
        write_missing_csv(coverages, missing_path, category)
        paths["missing_csv_path"] = missing_path
    elif os.path.exists(missing_path):
        os.remove(missing_path)  # stale, from an earlier run into the same --out
    xlsx_path = os.path.join(out, "report.xlsx")
    text_path = os.path.join(out, "report.txt")
    elapsed = time.monotonic() - started
    for r in runs:
        r.summary["elapsed"] = elapsed
    text_opts = {
        "top": args.top, "min_files": args.min_files, "max_runs": args.max_runs,
        "tree_top": args.tree_top, "footer": False,
    }  # fmt: skip

    if len(runs) == 1:
        run = runs[0]
        write_csv(run.groups, paths["csv_path"], category)
        summary = {**run.summary, **paths}
        if not args.no_xlsx and _write_xlsx(
            xlsx_path, run.result, run.groups, summary, coverages=coverages
        ):
            summary["xlsx_path"] = xlsx_path
        elif os.path.exists(xlsx_path):
            os.remove(xlsx_path)  # a stale workbook from an earlier run into the same --out
        write_json(run.result, summary, paths["json_path"], coverage=coverages)
        parts = [format_text(run.result, summary, **text_opts)]
    else:
        write_pool_csv(
            [(r.target.name or "", r.groups) for r in runs], paths["csv_path"], category
        )
        summary = {
            "mode": MODE_TITLES[mode],
            "basis": _basis(args),
            "pools": [r.target.name for r in runs],
            "elapsed": elapsed,
            **({"category": args.category} if args.category != ALL else {}),
            **paths,
        }
        if not args.no_xlsx and _write_xlsx(xlsx_path, runs=runs, summary=summary,
                                            coverages=coverages):  # fmt: skip
            summary["xlsx_path"] = xlsx_path
        elif os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        write_pools_json(
            [(r.target.name or "", r.result, r.summary) for r in runs], coverages, summary,
            paths["json_path"],
        )  # fmt: skip
        parts = [
            f"dpf duplicate report: {', '.join(f'pool {r.target.name}' for r in runs)}\n"
            "Duplicates are searched within each pool, never between pools.\n"
        ]
        for r in runs:
            parts.append(_banner(f"Pool {r.target.name}"))
            parts.append(format_text(r.result, r.summary, **text_opts))
        if coverages:
            parts.append(_banner("Backup coverage"))
    for cov in coverages:
        parts.append(format_coverage(cov, top=args.coverage_top, max_runs=args.max_runs))
    parts.append("\n".join(footer_lines(summary)))
    text = "\n\n".join(p.rstrip("\n") for p in parts) + "\n"
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text, end="" if text.endswith("\n") else "\n")
    if not args.quiet:
        print(f"report saved to {text_path}", file=sys.stderr)
    return 0


def _banner(title: str) -> str:
    return f"===== {title} =====\n"


def _coverage_plans(
    idx: Index, targets: list[_Target], pools: Pools, args: argparse.Namespace
) -> list[_CoveragePlan] | None:
    """The coverage checks this run makes; None (after an error message) if one can't run.

    Without --coverage-against, each pool of the run is checked against the pools linked
    to it by [backups]; a link that can't be used is skipped with a warning.
    """
    if args.no_coverage or MODES[args.mode] != "name":
        return []
    in_run = {t.pool.name.casefold(): t for t in targets if t.pool}
    wanted: list[tuple[_Target, Pool, str]] = []
    if args.coverage_against:
        try:
            other = get_pool(pools, args.coverage_against)
        except PoolError as e:
            print(f"dpf: {e}", file=sys.stderr)
            return None
        t = targets[0]
        partners = coverage_partners(pools, t.pool) if t.pool else []
        roles = {p.name: role for p, role in partners}
        if t.pool is not None and t.pool.name == other.name:
            print(f"dpf: --coverage-against {other.name}: that is the pool being searched",
                  file=sys.stderr)  # fmt: skip
            return None
        wanted.append((t, other, _direction(roles.get(other.name))))
    else:
        for t in targets:
            if t.pool is not None:
                wanted += [(t, p, _direction(role)) for p, role in coverage_partners(pools, t.pool)]

    plans: list[_CoveragePlan] = []
    for t, other, direction in wanted:
        problem, plan = _plan(idx, t, other, direction, in_run.get(other.name.casefold()))
        if problem is None:
            plans.append(plan)
            continue
        if args.coverage_against:
            print(f"dpf: {problem}", file=sys.stderr)
            return None
        print(f"dpf: warning: {problem}; coverage check skipped", file=sys.stderr)
    return plans


def _direction(role: str | None) -> str:
    if role is None:
        return NOT_IN_OTHER
    return NOT_BACKED_UP if role == BACKUP else ONLY_IN_BACKUP


def _plan(
    idx: Index, target: _Target, other: Pool, direction: str, in_run: _Target | None
) -> tuple[str | None, _CoveragePlan | None]:
    """(problem, None) or (None, plan) for checking `target` against pool `other`."""
    from dpf.scanner import drop_nested

    never: list[str] = []
    if in_run is not None:
        roots = list(in_run.roots)
    else:
        try:
            paths = pool_paths(other, [r.root for r in idx.roots()])
        except PoolError:
            paths = []
        found = [p for p in paths if idx.indexed(p)]
        roots = drop_nested(found)
        never = [
            p for p in paths
            if p not in found and not any(p.startswith(r.rstrip("/") + "/") for r in roots)
        ]  # fmt: skip
    if not roots:
        return f"pool {other.name} has nothing in the index (scan it first)", None
    for r in target.roots:
        for o in roots:
            if r == o or r.startswith(o.rstrip("/") + "/") or o.startswith(r.rstrip("/") + "/"):
                what = f"pool {target.name}" if target.name else "the PATHs"
                return f"{what} and pool {other.name} overlap at {r} / {o}", None
    return None, _CoveragePlan(target, other, direction, roots, never, in_run)


def _root_status(
    idx: Index,
    roots: list[str],
    offline: set[str],
    scanned: dict[str, RootStats],
    category: Categories | None = None,
) -> list[dict]:
    """Per root: whether it was scanned now or taken from the index, and when.

    With `category`, also its personal and other media (files and bytes).
    """
    status = []
    for r in roots:
        files, nbytes = idx.totals([r])
        if r in scanned:
            source, when = SCANNED_NOW, scanned[r].scanned_at
        else:
            stats = idx.containing_root(r)
            source, when = FROM_INDEX, stats and stats.scanned_at
        entry = {
            "root": r, "status": source, "connected": r not in offline,
            "scanned_at": when, "files": files, "bytes": nbytes,
        }  # fmt: skip
        if category is not None:
            for c, (n, b) in tally(idx, [r], category).items():
                entry[f"{c}_files"], entry[f"{c}_bytes"] = n, b
        status.append(entry)
    return status


def _sum_tallies(status: list[dict]) -> dict[str, dict[str, int]]:
    """{category: {"files", "bytes"}} summed over root_status entries."""
    return {
        c: {
            "files": sum(e[f"{c}_files"] for e in status),
            "bytes": sum(e[f"{c}_bytes"] for e in status),
        }
        for c in CATEGORIES
    }


def _group_category(cats: Categories, group: DuplicateGroup) -> str:
    return of_dirs(cats, {f.dir for f in group.files})


def _load_categories(args: argparse.Namespace) -> Categories | None:
    """[categories] of the pools file (empty without one); None after an error message."""
    try:
        return load_categories(args.pools_file)
    except PoolError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return None


def _drives(idx: Index, args: argparse.Namespace) -> int:
    """`dpf drives`: list the indexed roots, or forget some of them."""
    from dpf.scanner import drop_nested

    known = idx.roots()
    if args.forget:
        return _forget(idx, known, args.forget)
    if not known:
        print("nothing indexed yet; run `dpf scan PATH`")
        return 0
    pools = _pools_for_drives(args.pools_file)
    cats = _categories_for_drives(args.pools_file) if pools else Categories()
    spaces = [r.space for r in known]
    by_hand = any(sp and sp.source == SPACE_MANUAL for sp in spaces)
    dated = [r for r in known if _space_date(r)]
    space_column = bool(dated) and not cats  # no room for it beside the category columns
    header = ["Drive / folder", "Files", "Media size"]
    header += ["Personal", "Other media"] if cats else []
    header += ["Used on drive", "Capacity"] + (["Space as of"] if space_column else [])
    header += ["Last scanned", "Connected"] + (["Pool"] if pools else [])
    rows = [tuple(header)]
    connected = 0
    for r, sp in zip(known, spaces):
        files, nbytes = idx.totals([r.root])
        online = os.path.isdir(r.root)
        connected += online
        row = [r.root, f"{files:,}", human_bytes(nbytes)]
        if cats:
            split = tally(idx, [r.root], cats)
            row += [human_bytes(split[PERSONAL][1]), human_bytes(split[OTHER][1])]
        row += _space_cells(sp, by_hand)
        row += [_space_date(r)] if space_column else []
        row += [scan_date(r.scanned_at), "yes" if online else "no"]
        row += [", ".join(pools_of(pools, r.root)) or "—"] if pools else []
        rows.append(tuple(row))
    texts = ("Drive / folder", "Space as of", "Last scanned", "Connected", "Pool")
    _print_table(rows, left={i for i, h in enumerate(header) if h in texts})

    files, nbytes = idx.totals(drop_nested(r.root for r in known))
    line = f"{len(known)} indexed, {connected} connected; {files:,} files, "
    line += f"{human_bytes(nbytes)} of media"
    drives = [sp for sp in spaces if sp and sp.is_mount is not False]
    missing = sum(sp is None for sp in spaces)
    if drives:
        used = human_bytes(sum(sp.used for sp in drives))
        capacity = human_bytes(sum(sp.capacity for sp in drives))
        line += f"; {used} used of {capacity} on {plural(len(drives), 'drive')}"
        line += " (partial)" if missing else ""
    print(f"\n{line}")
    if cats:
        print("Personal / Other media: media sizes by [categories] in the pools file")
        if dated:  # the Space as of column made way for the category columns
            dates = sorted(_space_date(r) for r in dated)
            span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"
            print(
                f"Used/Capacity of {plural(len(dated), 'drive')} recorded after the last scan "
                f"({span})"
            )
    if by_hand:
        print("* entered by hand (dpf space --set)")
    if any(sp and sp.is_mount is False for sp in spaces):
        print("n/a: a folder inside a drive, not its mount point; its drive's space is not shown")
    if missing:
        print(
            f"{plural(missing, 'drive')} {'has' if missing == 1 else 'have'} no space data "
            f"(scanned before it was recorded); plug {'it' if missing == 1 else 'them'} in "
            "and run 'dpf space --missing'."
        )
    return 0


def _space_cells(space: DriveSpace | None, mark: bool) -> tuple[str, str]:
    """Used and capacity cells: "—" if not recorded, "n/a" for a folder, "*" if by hand."""
    pad = " " if mark else ""
    if space is None:
        return "—" + pad, "—" + pad
    if space.is_mount is False:
        return "n/a" + pad, "n/a" + pad
    star = "*" if space.source == SPACE_MANUAL else pad
    return human_bytes(space.used) + star, human_bytes(space.capacity) + star


def _space_date(r: RootStats) -> str:
    """When a drive's space was recorded, if not at its last scan (to the minute); else ""."""
    if r.space_at is None or r.is_mount is False:
        return ""
    if scan_date(r.space_at) == scan_date(r.scanned_at):
        return ""
    return scan_date(r.space_at)


def _space(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """`dpf space`: record drive space with statvfs, or as entered by hand (--set)."""
    if args.set_path is not None:
        if args.roots or args.pool or args.missing:
            parser.error("space: --set takes no PATHs, --pool or --missing")
        if args.used is None or args.capacity is None:
            parser.error("space: --set needs --used and --capacity")
    elif args.used is not None or args.capacity is not None:
        parser.error("space: --used and --capacity go with --set PATH")
    if args.pool and args.roots:
        parser.error("space: give either PATHs or --pool, not both")
    pool = None
    if args.pool:
        try:
            pool = get_pool(load_pools(args.pools_file), args.pool)
        except PoolError as e:
            print(f"dpf: {e}", file=sys.stderr)
            return 1
    db = args.db or default_db_path()
    if not os.path.exists(db):
        print(f"dpf: nothing indexed yet ({db}); run `dpf scan PATH` first", file=sys.stderr)
        return 1
    with Index(db) as idx:
        if args.set_path is not None:
            return _space_set(idx, args)
        return _space_measure(idx, args, pool)


def _space_set(idx: Index, args: argparse.Namespace) -> int:
    """Store hand-entered space for an indexed root. The path itself is never accessed."""
    if args.capacity <= 0 or args.used > args.capacity:
        print(
            f"dpf: expected 0 <= used <= capacity, capacity > 0; got used "
            f"{human_bytes(args.used)}, capacity {human_bytes(args.capacity)}",
            file=sys.stderr,
        )  # fmt: skip
        return 1
    root = os.path.abspath(args.set_path).rstrip("/") or "/"  # string only: no stat
    space = DriveSpace(args.used, args.capacity, is_mount=None, source=SPACE_MANUAL)
    stats = idx.set_space(root, space)
    if stats is None:
        listing = "\n".join(f"  {r.root}" for r in idx.roots()) or "  (none)"
        print(
            f"dpf: {args.set_path} is not an indexed drive/folder; `dpf space --set` needs one "
            f"of these exactly:\n{listing}",
            file=sys.stderr,
        )  # fmt: skip
        return 1
    print(
        f"recorded {root} by hand: {human_bytes(args.used)} used of "
        f"{human_bytes(args.capacity)} (the drive was not accessed)"
    )
    return 0


def _space_measure(idx: Index, args: argparse.Namespace, pool: Pool | None) -> int:
    """Measure the space of connected indexed roots and print what was recorded.

    Roots whose space was entered by hand are left alone (and not accessed at all)
    unless named as a PATH.
    """
    from dpf.scanner import drive_space

    known = {r.root: r for r in idx.roots()}
    if not known:
        print("nothing indexed yet; run `dpf scan PATH`")
        return 0
    notes: list[str] = []
    if args.roots:
        targets = []
        for p in args.roots:
            root = _indexed_root(known, p)
            if root is None:
                why = "not scanned yet; run `dpf scan` on it first"
                notes.append(f"{p}: {why if os.path.isdir(p) else 'not connected, never scanned'}")
            elif root not in targets:
                targets.append(root)
    elif pool is not None:
        targets = [r for r in known if any(matches(r, pat) for pat in pool.patterns)]
        notes += [
            f"{p}: not scanned yet; run `dpf scan` on it first"
            for p in _unscanned_pool_drives(pool, known)
        ]  # fmt: skip
    else:
        targets = list(known)

    rows = [("Drive / folder", "Used on drive", "Capacity")]
    for root in targets:
        r = known[root]
        if args.missing and r.used_bytes is not None:
            notes.append(f"{root}: space already recorded")
            continue
        if r.space_source == SPACE_MANUAL and not args.roots:
            notes.append(f"{root}: space entered by hand; name it as a PATH to measure it")
            continue
        if not os.path.isdir(root):
            notes.append(f"{root}: not connected")
            continue
        space = drive_space(root)
        if space is None:
            notes.append(f"{root}: cannot read its space")
            continue
        if not space.is_mount:
            notes.append(f"{root}: not a mount point (a folder, or a drive not mounted)")
            continue
        idx.set_space(root, space)
        rows.append((root, human_bytes(space.used), human_bytes(space.capacity)))
    for note in notes:
        print(f"skipped {note}")
    if notes:
        print()
    if len(rows) == 1:
        why = ": no connected drive is missing space data" if args.missing else ""
        print(f"nothing recorded{why}")
        return 0
    print(f"recorded the space of {plural(len(rows) - 1, 'drive')}:")
    _print_table(rows, left=(0,), indent="  ")
    return 0


def _indexed_root(known: Collection[str], path: str) -> str | None:
    """The indexed root `path` names exactly (as given, or with symlinks resolved)."""
    given = os.path.abspath(path).rstrip("/") or "/"
    if given in known:
        return given
    real = os.path.realpath(path)
    return real if real in known else None


def _unscanned_pool_drives(pool: Pool, known: Collection[str]) -> list[str]:
    """Connected drives matching `pool` with nothing indexed at or above them.

    Globs list the mount folder's entries only; the drives themselves are not accessed.
    """
    found = set()
    for pattern in pool.patterns:
        if is_glob(pattern):
            found.update(d.rstrip("/") for d in glob.glob(pattern))
        elif pattern not in known and os.path.isdir(pattern):
            found.add(pattern)
    return sorted(
        d for d in found if not any(d == k or d.startswith(k.rstrip("/") + "/") for k in known)
    )


def _pools_for_drives(path: str | None) -> Pools:
    """Pools for the `drives` Pool column; none (and no column) without a pools file."""
    if path is None and not os.path.exists(default_pools_path()):
        return {}
    try:
        return load_pools(path)
    except PoolError as e:
        print(f"dpf: warning: {e}", file=sys.stderr)
        return {}


def _categories_for_drives(path: str | None) -> Categories:
    """[categories] for the drives table; empty (no columns) when unset or unreadable."""
    try:
        return load_categories(path)
    except PoolError as e:
        print(f"dpf: warning: {e}", file=sys.stderr)
        return Categories()


def _print_table(rows: list[tuple[str, ...]], left: Collection[int], indent: str = "") -> None:
    """Print aligned columns; columns in `left` are left-aligned, the rest right-aligned."""
    widths = [max(len(row[c]) for row in rows) for c in range(len(rows[0]))]
    for row in rows:
        cells = [
            cell.ljust(w) if c in left else cell.rjust(w)
            for c, (cell, w) in enumerate(zip(row, widths))
        ]
        print((indent + "  ".join(cells)).rstrip())


def _pools(args: argparse.Namespace) -> int:
    """`dpf pools`: each pool with its patterns and the drives they match."""
    try:
        pools = load_pools(args.pools_file)
    except PoolError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return 1
    db = args.db or default_db_path()
    idx = Index(db) if os.path.exists(db) else None
    try:
        print(f"Pools from {args.pools_file or default_pools_path()}")
        for pool in pools.values():
            print(f"\nPool {pool.name}")
            for other, role in coverage_partners(pools, pool):
                link = "backed up by" if role == BACKUP else "backup of"
                print(f"  {link}: pool {other.name}")
            for i, pattern in enumerate(pool.patterns):
                print(f"  {'patterns:' if i == 0 else '':<11}{pattern}")
            members = _pool_members(pool, idx)
            if not members:
                print("  (no connected or indexed drive matches)")
                continue
            rows = [("Drive / folder", "Connected", "Files", "Size", "Last scanned")]
            for root, connected in members:
                stats = idx and idx.containing_root(root)
                files, nbytes = idx.totals([root]) if idx else (0, 0)
                state = "yes" if connected else "no"
                if stats or files:
                    when = scan_date(stats and stats.scanned_at)
                    rows.append((root, state, f"{files:,}", human_bytes(nbytes), when))
                else:
                    rows.append((root, state, "-", "-", "never scanned"))
            print()
            _print_table(rows, left=(0, 1, 4), indent="  ")
    finally:
        if idx is not None:
            idx.close()
    return 0


def _folders(idx: Index, args: argparse.Namespace) -> int:
    """`dpf folders`: the folder overview CSV, from the index only."""
    from dpf.folders import sort_roots, write_folders_csv
    from dpf.scanner import drop_nested

    try:
        pools = load_pools(args.pools_file) if args.pool else _pools_for_drives(args.pools_file)
        wanted = {get_pool(pools, n).name for n in args.pool or ()}
    except PoolError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return 1

    def pool_of(root: str) -> str:
        return ", ".join(pools_of(pools, root))

    # Matched against indexed roots only: globbing mount folders would touch the drives.
    roots = drop_nested(r.root for r in idx.roots())
    if wanted:
        roots = [r for r in roots if wanted & set(pools_of(pools, r))]
    if not roots:
        print("dpf: no indexed drives" + (" in that pool" if wanted else ""), file=sys.stderr)
        return 1
    out = args.out or os.path.join(_default_out_dir("folders"), "folders.csv")
    _check_output_location(
        out, roots, "the folder list", allow_same_drive=args.allow_same_drive,
        offline=set(roots),
    )  # fmt: skip
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    roots = sort_roots(roots, pool_of)
    rows = write_folders_csv(idx, roots, out, pool_of)
    print(f"{rows:,} folders from {plural(len(roots), 'drive')} written to {out}")
    return 0


def _pool_members(pool: Pool, idx: Index | None) -> list[tuple[str, bool]]:
    """(root, connected) for each drive a pool resolves to, nested roots dropped."""
    from dpf.scanner import drop_nested

    try:
        paths = pool_paths(pool, [r.root for r in idx.roots()] if idx else [])
    except PoolError:
        return []
    online, missing = [], []
    for p in paths:
        real = os.path.realpath(p)
        if os.path.isdir(real):
            online.append(real)
        else:
            missing.append(_offline_or_abs(idx, p))
    roots = drop_nested(online + missing)
    return [(r, r in online) for r in roots]


def _offline_or_abs(idx: Index | None, path: str) -> str:
    if idx is not None:
        try:
            return _offline_root(idx, path)
        except ValueError:
            pass
    return os.path.abspath(path)


def _resolve_pools(args: argparse.Namespace) -> tuple[Pools, list[tuple[Pool, list[str]]]]:
    """All pools, and each --pool with the paths it stands for (pools.pool_paths)."""
    pools = load_pools(args.pools_file)
    chosen = [get_pool(pools, name) for name in args.pool]
    db = args.db or default_db_path()
    indexed: list[str] = []
    if os.path.exists(db):
        with Index(db) as idx:
            indexed = [r.root for r in idx.roots()]
    return pools, [(pool, pool_paths(pool, indexed)) for pool in chosen]


def _check_one_pool(pools: Pools, roots: list[str], args: argparse.Namespace) -> bool:
    """False (with an error) if a root belongs to several pools: pools are never mixed."""
    for root in roots:
        names = pools_of(pools, root)
        if len(names) > 1:
            print(
                f"dpf: {root} matches more than one pool ({', '.join(names)}); fix "
                f"{args.pools_file or default_pools_path()} so each drive is in one pool",
                file=sys.stderr,
            )  # fmt: skip
            return False
    return True


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
    path: str,
    result: GroupingResult | None = None,
    groups: list[DuplicateGroup] | None = None,
    summary: dict | None = None,
    *,
    runs: list[_PoolRun] | None = None,
    coverages: list[Coverage] | None = None,
) -> bool:
    """Write report.xlsx (one pool, or `runs` combined); False with a hint without XlsxWriter."""
    from dpf.xlsx_report import PoolSheets, write_combined_xlsx, write_xlsx

    try:
        if runs is None:
            write_xlsx(result, groups or [], summary or {}, path, coverages or [])
        else:
            sheets = [PoolSheets(r.target.name or "", r.result, r.groups, r.summary) for r in runs]
            write_combined_xlsx(sheets, coverages or [], summary or {}, path)
    except ModuleNotFoundError as e:
        if e.name != "xlsxwriter":
            raise
        print("dpf: report.xlsx skipped (install python3-xlsxwriter)", file=sys.stderr)
        return False
    return True


def _basis(args: argparse.Namespace) -> str:
    from dpf.match_name import basis

    return basis(
        exact_names=args.exact_names, ignore_size=args.ignore_size,
        ignore_mtime=args.ignore_mtime, tolerance=args.mtime_tolerance,
        hour_shift=not args.strict_time,
    )  # fmt: skip


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

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "pools":
        return _pools(args)
    if args.command == "space":
        return _space(args, parser)
    if args.command == "folders":
        db = args.db or default_db_path()
        if not os.path.exists(db):
            print(f"dpf: nothing indexed yet ({db}); run `dpf scan PATH`", file=sys.stderr)
            return 1
        with Index(db) as idx:
            return _folders(idx, args)
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
    if args.pool and args.roots:
        parser.error(f"{args.command}: give either PATHs or --pool, not both")
    if not args.pool and not args.roots:
        parser.error(f"{args.command}: give PATHs or --pool NAME")
    if args.pool:
        names = [n.casefold() for n in args.pool]
        twice = next((n for n in args.pool if names.count(n.casefold()) > 1), None)
        if twice:
            parser.error(f"{args.command}: --pool {twice} is given twice")
    if args.command == "find":
        if args.no_coverage and args.coverage_against:
            parser.error("find: --no-coverage and --coverage-against exclude each other")
        if args.coverage_against and args.pool and len(args.pool) > 1:
            parser.error("find: --coverage-against goes with a single --pool or with PATHs")
    pools: Pools = {}
    chosen: list[tuple[Pool | None, list[str]]] = [(None, args.roots)]
    try:
        if args.pool:
            pools, chosen = _resolve_pools(args)
        elif getattr(args, "coverage_against", None):
            pools = load_pools(args.pools_file)
    except PoolError as e:
        print(f"dpf: {e}", file=sys.stderr)
        return 1
    split: list[tuple[Pool | None, list[str], list[str]]] = []
    for pool, paths in chosen:
        try:
            online, missing = split_roots(paths)
        except ValueError as e:
            print(f"dpf: {e}", file=sys.stderr)
            return 1
        if args.command == "scan" and missing:
            if pool is None:
                print(f"dpf: {missing[0]} is not connected; nothing to scan", file=sys.stderr)
                return 1
            if not online:
                print(f"dpf: no drives in pool {pool.name} are connected", file=sys.stderr)
                return 1
            if not args.quiet:
                absent = [os.path.abspath(p) for p in missing]
                for p in drop_nested(online + absent):
                    if p not in online:
                        print(f"skipping {p} (pool {pool.name}, not connected)", file=sys.stderr)
            missing = []
        split.append((pool, online, missing))
    every_online = [r for _, online, _ in split for r in online]
    every_missing = [os.path.abspath(p) for _, _, missing in split for p in missing]
    _check_output_location(
        args.db or default_db_path(), every_online + every_missing,
        "the index", allow_same_drive=args.allow_same_drive, offline=set(every_missing),
    )  # fmt: skip
    try:
        with Index(args.db) as idx:
            targets: list[_Target] = []
            for pool, online, missing in split:
                try:
                    offline = {_offline_root(idx, p) for p in missing}
                except ValueError as e:
                    print(f"dpf: {e}", file=sys.stderr)
                    return 1
                roots = drop_nested([*online, *offline])
                if pool is not None and not _check_one_pool(pools, roots, args):
                    return 1
                targets.append(_Target(pool, roots, offline & set(roots)))
            if args.command == "scan":
                _scan(idx, drop_nested(r for t in targets for r in t.roots), args)
                return 0
            return _find(idx, targets, pools, args)
    except KeyboardInterrupt:
        _end_progress(False)
        print("dpf: interrupted", file=sys.stderr)
        return 130
