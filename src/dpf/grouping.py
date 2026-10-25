"""Collapse duplicate groups into folder pairs and runs: the core of the dpf report.

Every two folders that share copies become one FolderPair. Its matches are folded into
runs of consecutive files (natural order of folder A), so a copied sequence reads as
"IMG_0100 … IMG_0450 (351 files)" instead of 351 lines. Sibling trees whose folders all
match are rolled up into a single pair. Names may differ between the two sides
(content-based modes), so matching never relies on equal names.

Only indexed files are compared. Entries the scanner did not index (non-media files,
symlinks, unreadable entries) are counted per pair as `unindexed`, and a tree is only
rolled up when it holds none, lies wholly inside the scanned roots, and has the same
subfolders on both sides.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations

from dpf.models import DuplicateGroup, FileRecord

IDENTICAL_TREE = "identical_tree"
IDENTICAL = "identical"
A_IN_B = "a_in_b"
B_IN_A = "b_in_a"
PARTIAL = "partial"

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
    """Consecutive files of folder A (natural order) that all have a copy in folder B."""

    a_first: str
    a_last: str
    b_first: str
    b_last: str
    count: int
    bytes: int
    same_names: bool


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
    relation: str  # IDENTICAL_TREE, IDENTICAL, A_IN_B, B_IN_A or PARTIAL
    folders: int = 1
    unindexed: int = 0  # entries on either side that were not indexed, so not compared


@dataclass(slots=True)
class GroupingResult:
    pairs: list[FolderPair]
    same_folder: list[DuplicateGroup]
    groups: int
    redundant_bytes: int
    capped_groups: int


def group_duplicates(
    groups: Iterable[DuplicateGroup],
    list_dir: Callable[[str], list[FileRecord]],
    subdirs: Callable[[str], list[str]],
    dir_extras: Callable[[str], tuple[int, int]],
    *,
    roots: Sequence[str],
    max_dirs_per_group: int = 16,
) -> GroupingResult:
    """Turn duplicate groups into folder pairs sorted by duplicated bytes, largest first.

    `list_dir`, `subdirs` and `dir_extras` are normally the Index methods of the same
    names. `roots` are the scanned roots: trees are never rolled up past them, since
    folders outside were not scanned and only look empty. Each directory is listed at
    most once. A group spread over more than `max_dirs_per_group` folders pairs every
    folder only with the first one.
    """
    # (dir_a, dir_b) -> (A name -> matching B name, matched B names)
    matches: dict[tuple[str, str], tuple[dict[str, str], set[str]]] = {}
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
                a, b, listing, a_map, b_set, dir_count(b), unindexed(a) + unindexed(b)
            )

    _roll_up(pairs, dir_count, unindexed, subdirs, roots)
    ordered = sorted(pairs.values(), key=lambda p: (-p.bytes, p.dir_a, p.dir_b))
    return GroupingResult(ordered, same_folder, count, redundant, capped)


def _counterpart(name: str, b_names: list[str]) -> str:
    """The B-side copy to show for `name`: one with the same name if there is one."""
    if len(b_names) > 1:
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
) -> FolderPair:
    runs: list[Run] = []
    cur: list | None = None  # [a_first, a_last, b_first, b_last, count, bytes, same, b_key]
    matched = nbytes = 0
    for a_key, _, rec in listing:
        other = a_map.get(rec.name)
        if other is None:
            if cur is not None:
                runs.append(Run(*cur[:7]))
                cur = None
            continue
        matched += 1
        nbytes += rec.size
        same = rec.name.casefold() == other.casefold()
        b_key = a_key if same else natural_key(other)
        if cur is not None and b_key < cur[7]:
            # B-side order went backwards: "b_first … b_last" would mislead, so split.
            runs.append(Run(*cur[:7]))
            cur = None
        if cur is None:
            cur = [rec.name, rec.name, other, other, 1, rec.size, same, b_key]
        else:
            cur[1], cur[3], cur[7] = rec.name, other, b_key
            cur[4] += 1
            cur[5] += rec.size
            cur[6] = cur[6] and same
    if cur is not None:
        runs.append(Run(*cur[:7]))

    total_a = len(listing)
    matched_b = min(len(b_set), total_b)
    if matched == total_a and matched_b == total_b and total_a == total_b:
        relation = IDENTICAL
    elif matched == total_a:
        relation = A_IN_B
    elif matched_b == total_b:
        relation = B_IN_A
    else:
        relation = PARTIAL
    return FolderPair(
        a, b, tuple(runs), matched, total_a, matched_b, total_b, nbytes, relation, 1, unindexed
    )


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
        for name, sub in cx.items():
            t = tree(sub, cy[name])
            if t is None:
                return None
            folders += t.folders
            files += t.files
            nbytes += t.bytes
        result = memo[k] = _Tree(folders, files, nbytes, len(cx))
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
                IDENTICAL_TREE, t.folders,
            )  # fmt: skip
