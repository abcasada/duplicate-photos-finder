"""Collapse duplicate groups into folder pairs and runs: the core of the dpf report.

Every two folders that share copies become one FolderPair. Its matches are folded into
runs of consecutive files (natural order of folder A), so a copied sequence reads as
"IMG_0100 … IMG_0450 (351 files)" instead of 351 lines. Sibling trees whose folders all
match are rolled up into a single pair. Names may differ between the two sides
(content-based modes, or similar names in mode 1), so matching never relies on equal
names.

Only indexed files are compared. Entries the scanner did not index (non-media files,
symlinks, unreadable entries) are counted per pair as `unindexed`, and a tree is only
rolled up when it holds none, lies wholly inside the scanned roots, and has the same
subfolders on both sides.

Trees that differ in a few files cannot be rolled up, so a second, looser view groups
folder pairs by the trees they sit in (`TreePair`), with each tree's coverage measured
against everything indexed under it.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations

from dpf.models import DuplicateGroup, FileRecord
from dpf.names import match_stem

IDENTICAL_TREE = "identical_tree"
IDENTICAL = "identical"
A_IN_B = "a_in_b"
B_IN_A = "b_in_a"
PARTIAL = "partial"
NEAR_IDENTICAL = "near_identical"

# Tree pair relations: coverage of each tree, measured against all files indexed under it.
TREE_NEAR = "near_identical"
TREE_A_MOSTLY_IN_B = "a_mostly_in_b"
TREE_B_MOSTLY_IN_A = "b_mostly_in_a"
TREE_OVERLAP = "overlapping"

DEFAULT_NEAR = 0.9

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple[str | int, ...]:
    """Case-insensitive sort key that orders digit runs numerically (IMG_9 < IMG_10).

    Text and numbers alternate, starting with text, so keys always compare cleanly.
    """
    parts: list = _DIGITS.split(name.casefold())
    parts[1::2] = map(int, parts[1::2])
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class Run:
    """Consecutive files of folder A (natural order) that all have a copy in folder B.

    An unmatched file beside a matched one of the same shot in another format (the JPG
    of a matched RAW) is passed over rather than ending the run. It lies inside the
    run's range without a copy, so it is listed in `skipped`; `count` and `bytes` cover
    matches only. `same_names` is True when every A name equals its B name exactly.
    """

    a_first: str
    a_last: str
    b_first: str
    b_last: str
    count: int
    bytes: int
    same_names: bool
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FolderPair:
    """Duplicates shared by two folders (or, for identical_tree, two whole trees)."""

    dir_a: str
    dir_b: str
    runs: tuple[Run, ...]
    matched_a: int
    total_a: int
    matched_b: int
    total_b: int
    bytes: int
    relation: str  # IDENTICAL_TREE, IDENTICAL, A_IN_B, B_IN_A, NEAR_IDENTICAL or PARTIAL
    folders: int = 1
    unindexed: int = 0  # entries on either side that were not indexed, so not compared
    similar_names: bool = False  # some matched files have different names on the two sides


@dataclass(frozen=True, slots=True)
class TreePair:
    """Two trees whose folders pair up at the same relative paths (an overview line).

    `matched_*` count each indexed file under the tree at most once; `total_*` are all
    files indexed under it, so unpaired folders lower the coverage.
    """

    dir_a: str
    dir_b: str
    members: tuple[tuple[str, str], ...]  # (dir_a, dir_b) of the folder pairs rolled in
    folders: int  # folder pairs, counting every folder of a matching tree
    matched_a: int
    total_a: int
    matched_b: int
    total_b: int
    bytes: int
    relation: str  # TREE_NEAR, TREE_A_MOSTLY_IN_B, TREE_B_MOSTLY_IN_A or TREE_OVERLAP
    unindexed: int = 0  # unindexed entries in the paired folders (not the whole trees)


@dataclass(slots=True)
class GroupingResult:
    pairs: list[FolderPair]
    same_folder: list[DuplicateGroup]
    groups: int
    redundant_bytes: int
    capped_groups: int
    trees: list[TreePair] = field(default_factory=list)
    near: float = DEFAULT_NEAR


def group_duplicates(
    groups: Iterable[DuplicateGroup],
    list_dir: Callable[[str], list[FileRecord]],
    subdirs: Callable[[str], list[str]],
    dir_extras: Callable[[str], tuple[int, int]],
    *,
    roots: Sequence[str],
    max_dirs_per_group: int = 16,
    near: float = DEFAULT_NEAR,
    tree_totals: Callable[[str], tuple[int, int]] | None = None,
) -> GroupingResult:
    """Turn duplicate groups into folder pairs sorted by duplicated bytes, largest first.

    `list_dir`, `subdirs` and `dir_extras` are normally the Index methods of the same
    names. `roots` are the scanned roots: trees are never rolled up past them, since
    folders outside were not scanned and only look empty. Each directory is listed at
    most once. A group spread over more than `max_dirs_per_group` folders pairs every
    folder only with the first one.

    A partial pair is NEAR_IDENTICAL when at least `near` of each side matched (1.0
    disables it). With `tree_totals` (dir -> (files, bytes) indexed under it, normally
    `lambda d: index.totals([d])`) the result also lists tree pairs; see tree_pairs.
    """
    # (dir_a, dir_b) -> (A name -> matching B name, matched B names)
    matches: dict[tuple[str, str], tuple[dict[str, str], set[str]]] = {}
    similar: set[tuple[str, str]] = set()  # pairs where some matched names differ
    same_folder: list[DuplicateGroup] = []
    count = redundant = capped = 0

    for group in groups:
        count += 1
        redundant += group.redundant_bytes
        by_dir: dict[str, list[str]] = {}
        for f in group.files:
            by_dir.setdefault(f.dir, []).append(f.name)
        if len(by_dir) < len(group.files):
            same_folder.append(group)
        if len(by_dir) < 2:
            continue
        dirs = sorted(by_dir)
        if len(dirs) > max_dirs_per_group:
            capped += 1
            dir_pairs: Iterable[tuple[str, str]] = ((dirs[0], d) for d in dirs[1:])
        else:
            dir_pairs = combinations(dirs, 2)
        for a, b in dir_pairs:
            entry = matches.get((a, b))
            if entry is None:
                entry = matches[(a, b)] = ({}, set())
            a_map, b_set = entry
            b_names = by_dir[b]
            if len(set(by_dir[a]).union(b_names)) > 1:
                similar.add((a, b))
            for name in by_dir[a]:
                if name not in a_map:
                    a_map[name] = _counterpart(name, b_names)
            b_set.update(b_names)

    counts: dict[str, int] = {}
    extras: dict[str, int] = {}

    def dir_count(d: str) -> int:
        n = counts.get(d)
        if n is None:
            n = counts[d] = len(list_dir(d))
        return n

    def unindexed(d: str) -> int:
        n = extras.get(d)
        if n is None:
            n = extras[d] = sum(dir_extras(d))
        return n

    partners: defaultdict[str, list[str]] = defaultdict(list)
    for a, b in matches:
        partners[a].append(b)
    pairs: dict[tuple[str, str], FolderPair] = {}
    # Descending order: every B (> A) that is also some pair's A is listed before it.
    for a in sorted(partners, reverse=True):
        bs = partners.pop(a)
        listing = sorted((natural_key(r.name), r.name, r) for r in list_dir(a))
        counts[a] = len(listing)
        for b in bs:
            a_map, b_set = matches.pop((a, b))
            pairs[(a, b)] = _build_pair(
                a, b, listing, a_map, b_set, dir_count(b), unindexed(a) + unindexed(b), near,
                (a, b) in similar,
            )  # fmt: skip

    _roll_up(pairs, dir_count, unindexed, subdirs, roots)
    ordered = sorted(pairs.values(), key=lambda p: (-p.bytes, p.dir_a, p.dir_b))
    trees = tree_pairs(ordered, roots, tree_totals, near=near) if tree_totals else []
    return GroupingResult(ordered, same_folder, count, redundant, capped, trees, near)


def is_near(matched: int, total: int, near: float) -> bool:
    """True if `matched` is at least the fraction `near` of `total` (never for total 0)."""
    return total > 0 and matched * 1_000_000 >= round(near * 1_000_000) * total


def _counterpart(name: str, b_names: list[str]) -> str:
    """The B-side copy to show for `name`: one with the same name (or case) if there is one."""
    if len(b_names) > 1:
        if name in b_names:
            return name
        key = name.casefold()
        for other in b_names:
            if other.casefold() == key:
                return other
    return b_names[0]


def _build_pair(
    a: str,
    b: str,
    listing: list[tuple[tuple[str | int, ...], str, FileRecord]],
    a_map: dict[str, str],
    b_set: set[str],
    total_b: int,
    unindexed: int,
    near: float = DEFAULT_NEAR,
    similar_names: bool = False,
) -> FolderPair:
    runs: list[Run] = []
    # [a_first, a_last, b_first, b_last, count, bytes, same, b_key, skipped]
    cur: list | None = None
    passed: list[str] = []  # siblings passed over since the run's last match
    matched = nbytes = 0
    for i, (a_key, _, rec) in enumerate(listing):
        other = a_map.get(rec.name)
        if other is None:
            if cur is None:
                continue
            if _beside_sibling(listing, a_map, i):
                passed.append(rec.name)
            else:
                runs.append(_run(cur))
                cur, passed = None, []
            continue
        matched += 1
        nbytes += rec.size
        same = rec.name == other
        b_key = a_key if same else natural_key(other)
        if cur is not None and b_key < cur[7]:
            # B-side order went backwards: "b_first … b_last" would mislead, so split.
            runs.append(_run(cur))
            cur = None
        if cur is None:
            cur = [rec.name, rec.name, other, other, 1, rec.size, same, b_key, []]
        else:
            cur[1], cur[3], cur[7] = rec.name, other, b_key
            cur[4] += 1
            cur[5] += rec.size
            cur[6] = cur[6] and same
            cur[8] += passed
        passed = []
    if cur is not None:
        runs.append(_run(cur))

    total_a = len(listing)
    matched_b = min(len(b_set), total_b)
    if matched == total_a and matched_b == total_b and total_a == total_b:
        relation = IDENTICAL
    elif matched == total_a:
        relation = A_IN_B
    elif matched_b == total_b:
        relation = B_IN_A
    elif is_near(matched, total_a, near) and is_near(matched_b, total_b, near):
        relation = NEAR_IDENTICAL
    else:
        relation = PARTIAL
    return FolderPair(
        a, b, tuple(runs), matched, total_a, matched_b, total_b, nbytes, relation, 1, unindexed,
        similar_names,
    )  # fmt: skip


def _run(cur: list) -> Run:
    return Run(*cur[:7], tuple(cur[8]))


def _beside_sibling(
    listing: list[tuple[tuple[str | int, ...], str, FileRecord]], a_map: dict[str, str], i: int
) -> bool:
    """True if unmatched listing[i] is another format of a matched neighbour's shot.

    The neighbour must share the name stem and have a different extension (the JPG of
    a matched RAW). A same-format file ("X.DNG" beside a matched "X (2).DNG") is a
    different file and ends the run.
    """
    stem, ext = _stem_ext(listing[i][1])
    for j in (i - 1, i + 1):
        if 0 <= j < len(listing):
            name = listing[j][1]
            if name in a_map:
                other_stem, other_ext = _stem_ext(name)
                if other_stem == stem and other_ext != ext:
                    return True
    return False


def _stem_ext(name: str) -> tuple[str, str]:
    return match_stem(name), os.path.splitext(name)[1].casefold()


def _key(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _related(x: str, y: str) -> bool:
    """True if x and y are the same directory or one contains the other."""
    return x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")


@dataclass(frozen=True, slots=True)
class _Tree:
    folders: int
    files: int
    bytes: int
    children: int
    similar: bool = False


def _roll_up(
    pairs: dict[tuple[str, str], FolderPair],
    dir_count: Callable[[str], int],
    unindexed: Callable[[str], int],
    subdirs: Callable[[str], list[str]],
    roots: Sequence[str],
) -> None:
    """Replace identical folder pairs that make up matching trees by one tree pair.

    Every folder of both trees must be an identical pair with nothing unindexed, or
    hold no files and nothing unindexed on either side, and both trees must lie
    inside the scanned roots.
    """
    identical = {
        k: p for k, p in pairs.items() if p.relation == IDENTICAL and p.unindexed == 0
    }
    if not identical:
        return
    scanned = [r.rstrip("/") or "/" for r in roots]

    def inside(d: str) -> bool:
        return any(_within(d, r) for r in scanned)

    memo: dict[tuple[str, str], _Tree | None] = {}
    child_cache: dict[str, dict[str, str]] = {}

    def children(d: str) -> dict[str, str]:
        c = child_cache.get(d)
        if c is None:
            c = child_cache[d] = {os.path.basename(s): s for s in subdirs(d)}
        return c

    def tree(x: str, y: str) -> _Tree | None:
        k = _key(x, y)
        if k in memo:
            return memo[k]
        memo[k] = None
        if _related(x, y) or not (inside(x) and inside(y)):
            return None
        own = identical.get(k)
        if own is None and (dir_count(x) or dir_count(y) or unindexed(x) or unindexed(y)):
            return None
        cx, cy = children(x), children(y)
        if cx.keys() != cy.keys() or (own is None and not cx):
            return None
        folders, files, nbytes = (1, own.matched_a, own.bytes) if own else (0, 0, 0)
        similar = own is not None and own.similar_names
        for name, sub in cx.items():
            t = tree(sub, cy[name])
            if t is None:
                return None
            folders += t.folders
            files += t.files
            nbytes += t.bytes
            similar = similar or t.similar
        result = memo[k] = _Tree(folders, files, nbytes, len(cx), similar)
        return result

    for x, y in identical:
        while True:
            px, py = os.path.dirname(x), os.path.dirname(y)
            if px == x or py == y or os.path.basename(x) != os.path.basename(y):
                break
            if tree(px, py) is None:
                break
            x, y = px, py

    for k, t in memo.items():
        if t is None:
            continue
        x, y = k
        inside = (
            os.path.basename(x) == os.path.basename(y)
            and memo.get(_key(os.path.dirname(x), os.path.dirname(y))) is not None
        )
        if inside:
            pairs.pop(k, None)
        elif t.children:
            pairs[k] = FolderPair(
                k[0], k[1], (), t.files, t.files, t.files, t.files, t.bytes,
                IDENTICAL_TREE, t.folders, 0, t.similar,
            )  # fmt: skip


def tree_tops(dir_a: str, dir_b: str, roots: Sequence[str]) -> tuple[str, str]:
    """The trees two paired folders sit in: both paths minus their common trailing parts.

    Components are compared case-insensitively, so `/d1/Photos/2025/X` and
    `/d2/Photo backups/2025/x` give (`/d1/Photos`, `/d2/Photo backups`). Stripping stops
    before either side would leave the scanned `roots` (a root itself is fine), or the
    two sides would become one folder or one would contain the other.
    """
    scanned = [r.rstrip("/") or "/" for r in roots]
    ta, tb = dir_a, dir_b
    while True:
        name_a, name_b = os.path.basename(ta), os.path.basename(tb)
        if not name_a or name_a.casefold() != name_b.casefold():
            break
        pa, pb = os.path.dirname(ta), os.path.dirname(tb)
        if _related(pa, pb):
            break
        if not (any(_within(pa, r) for r in scanned) and any(_within(pb, r) for r in scanned)):
            break
        ta, tb = pa, pb
    return ta, tb


def tree_pairs(
    pairs: Iterable[FolderPair],
    roots: Sequence[str],
    totals: Callable[[str], tuple[int, int]],
    *,
    near: float = DEFAULT_NEAR,
) -> list[TreePair]:
    """Group folder pairs by the trees they sit in (tree_tops), largest first.

    Only trees holding two or more pairs are returned: a single pair is already its own
    report line. `totals` maps a directory to the (files, bytes) indexed under it and is
    called once per tree directory. Coverage counts each file at most once and never
    exceeds 100%.
    """
    buckets: defaultdict[tuple[str, str], list[tuple[FolderPair, bool]]] = defaultdict(list)
    for p in pairs:
        ta, tb = tree_tops(p.dir_a, p.dir_b, roots)
        if ta < tb:
            buckets[(ta, tb)].append((p, False))
        else:
            buckets[(tb, ta)].append((p, True))

    files: dict[str, int] = {}

    def total(d: str) -> int:
        n = files.get(d)
        if n is None:
            n = files[d] = totals(d)[0]
        return n

    trees = []
    for (ta, tb), members in buckets.items():
        if len(members) < 2:
            continue
        side_a, side_b = [], []
        for p, flipped in members:
            tree = p.relation == IDENTICAL_TREE
            a, b = (p.dir_a, p.matched_a, tree), (p.dir_b, p.matched_b, tree)
            side_a.append(b if flipped else a)
            side_b.append(a if flipped else b)
        kept_a, kept_b = _count_once(side_a), _count_once(side_b)
        total_a, total_b = total(ta), total(tb)
        matched_a = min(sum(side_a[i][1] for i in kept_a), total_a)
        matched_b = min(sum(side_b[i][1] for i in kept_b), total_b)
        near_a, near_b = is_near(matched_a, total_a, near), is_near(matched_b, total_b, near)
        if near_a and near_b:
            relation = TREE_NEAR
        elif near_a:
            relation = TREE_A_MOSTLY_IN_B
        elif near_b:
            relation = TREE_B_MOSTLY_IN_A
        else:
            relation = TREE_OVERLAP
        trees.append(
            TreePair(
                ta, tb,
                tuple((p.dir_a, p.dir_b) for p, _ in members),
                sum(members[i][0].folders for i in kept_a),
                matched_a, total_a, matched_b, total_b,
                sum(members[i][0].bytes for i in kept_a),
                relation,
                sum(p.unindexed for p, _ in members),
            )
        )  # fmt: skip
    trees.sort(key=lambda t: (-t.bytes, t.dir_a, t.dir_b))
    return trees


def _count_once(entries: list[tuple[str, int, bool]]) -> list[int]:
    """Indexes of the (dir, matched, is_tree) entries to count so no file counts twice.

    A folder listed more than once keeps its largest entry, and entries inside a listed
    matching tree are dropped (the tree already counts their files).
    """
    best: dict[str, int] = {}
    for i, (d, matched, _) in enumerate(entries):
        j = best.get(d)
        if j is None or matched > entries[j][1]:
            best[d] = i
    trees = {d for d, i in best.items() if entries[i][2]}
    return [i for d, i in best.items() if not (trees and _under_any(d, trees))]


def _under_any(d: str, tops: set[str]) -> bool:
    """True if a proper ancestor of `d` is in `tops`."""
    parent = os.path.dirname(d)
    while parent != d:
        if parent in tops:
            return True
        d, parent = parent, os.path.dirname(parent)
    return False
