"""SQLite inventory of scanned media files.

The database lives on the local disk (default ~/.cache/dpf/index.db), never on the
drives being scanned. Files are addressed by absolute path; queries select a set of
roots by path prefix, so nested or overlapping roots are handled naturally.

Besides media files, the `dirs` table records per directory how many entries were not
indexed (non-media files, symlinks, unreadable entries), so a folder is never called a
match while it holds content that was not compared. Each `roots` row also records the
used and total space of the drive holding the root: measured at each scan or by
`dpf space`, or entered by hand for a drive that must not be read.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from dpf.models import DriveSpace, FileRecord, RootStats
from dpf.names import camera_token, match_key

_TABLES = """
CREATE TABLE IF NOT EXISTS roots (
    path       TEXT PRIMARY KEY,
    files      INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    errors     INTEGER NOT NULL,
    scanned_at REAL NOT NULL,
    used_bytes     INTEGER,
    capacity_bytes INTEGER,
    is_mount       INTEGER,
    space_source   TEXT,
    space_at       REAL
);
CREATE TABLE IF NOT EXISTS files (
    dir       TEXT NOT NULL,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    kind      TEXT NOT NULL,
    match_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dir, name)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS staging (
    dir       TEXT NOT NULL,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    kind      TEXT NOT NULL,
    match_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dir, name)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dirs (
    dir    TEXT PRIMARY KEY,
    other  INTEGER NOT NULL,
    errors INTEGER NOT NULL
) WITHOUT ROWID;
"""

_EXIF_TABLE = """
CREATE TABLE IF NOT EXISTS exif (
    dir          TEXT NOT NULL,
    name         TEXT NOT NULL,
    read_size    INTEGER NOT NULL,
    read_mtime   REAL NOT NULL,
    status       TEXT NOT NULL,
    make         TEXT,
    model        TEXT,
    serial       TEXT,
    shutter      INTEGER,
    taken        TEXT,
    taken_offset TEXT,
    taken_subsec TEXT,
    width        INTEGER,
    height       INTEGER,
    shutter_mode TEXT,
    fmt          TEXT,
    ident        TEXT,
    shot         TEXT,
    note         TEXT,
    read_at      REAL NOT NULL,
    PRIMARY KEY (dir, name)
) WITHOUT ROWID;
"""

# One row per SMART reading of a physical drive (dpf smart, or imported); every reading is
# kept, so reports can show a trend. JSON columns hold the smart-summary record parts.
_SMART_TABLE = """
CREATE TABLE IF NOT EXISTS smart (
    serial     TEXT NOT NULL,
    read_at    REAL NOT NULL,
    source     TEXT NOT NULL,
    host       TEXT,
    device     TEXT,
    model      TEXT,
    labels     TEXT NOT NULL,
    mounts     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    flags      TEXT NOT NULL,
    kernel     TEXT,
    partitions TEXT,
    smartctl   TEXT,
    UNIQUE (serial, read_at)
);
"""

# Created after _migrate, since they may name columns that older databases lack.
_INDEXES = """
CREATE INDEX IF NOT EXISTS files_name_size ON files (name_key, size);
CREATE INDEX IF NOT EXISTS files_match_size ON files (match_key, size);
CREATE INDEX IF NOT EXISTS exif_shot ON exif (shot);
"""

EXIF_COLUMNS = (
    "dir", "name", "read_size", "read_mtime", "status", "make", "model", "serial", "shutter",
    "taken", "taken_offset", "taken_subsec", "width", "height", "shutter_mode", "fmt",
    "ident", "shot", "note", "read_at",
)  # fmt: skip

_VALID_EXIF = "e.read_size = files.size AND e.read_mtime = files.mtime"
# A shot key the guard found unreliable (Index._shot_guard), for the exif row `e`.
_BAD_SHOT = "EXISTS (SELECT 1 FROM temp.bad_shots b WHERE b.shot = e.shot)"
# The usable shot of a files row joined as `e`: NULL when absent, out of date or unreliable.
_SHOT = f"CASE WHEN {_VALID_EXIF} AND NOT {_BAD_SHOT} THEN e.shot END"

_COLUMNS = "dir, name, name_key, size, mtime, kind, match_key"
_ROOT_COLUMNS = (
    "path, files, bytes, scanned_at, errors, "
    "used_bytes, capacity_bytes, is_mount, space_source, space_at"
)
_SPACE_COLUMNS = {
    "used_bytes": "INTEGER",
    "capacity_bytes": "INTEGER",
    "is_mount": "INTEGER",
    "space_source": "TEXT",
    "space_at": "REAL",
}

# Schema history (PRAGMA user_version):
#   1  files, staging, roots
#   2  dirs table (older inventories never recorded non-media content: dropped)
#   3  match_key column (names.match_key), added and filled in place
#   4  match_key recomputed: text around a camera number is only dropped when neutral
#   5  roots.used_bytes, capacity_bytes, is_mount, space_source, space_at: the drive's
#      space (statvfs at scan time, `dpf space`, or entered by hand); NULL until recorded
#   6  exif table (dpf exif; mode 1b), created empty: no existing row changes
#   7  exif table recreated when it lacks shutter_mode: rows of the first 1b build kept
#      Sony serials in another form and no shutter mode, so their keys cannot be trusted
#      (a cache of connected drives only; nothing else changes)
#   8  smart table (dpf smart): every SMART reading per drive serial, created empty;
#      nothing else changes. forget_root leaves it alone (readings are per physical drive)
# Rule: schema changes migrate in place (ALTER TABLE, recompute derived columns) and
# bump _VERSION. Never drop an inventory unless its data cannot be trusted: drives that
# are not connected cannot be rescanned, so the index may be their only record.
# Changing names.match_key also needs a version bump and a _refill_match_keys step.
_VERSION = 8

_BATCH = 10_000
_KEY_CHUNK = 500  # keys per IN (...) query, well under SQLite's variable limit

DirStats = Mapping[str, tuple[int, int]]


def default_db_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "dpf", "index.db")


def name_key(name: str) -> str:
    return name.casefold()


def _prefix_clause(roots: Sequence[str]) -> tuple[str, list[str]]:
    """SQL matching files whose dir is a root or lies beneath one.

    Uses a range scan instead of LIKE so '%' and '_' in paths need no escaping:
    every path under "R/" sorts between "R/" and "R0" ('0' follows '/').
    """
    if not roots:
        raise ValueError("at least one root is required")
    parts, params = [], []
    for root in roots:
        root = root.rstrip("/") or "/"
        if root == "/":
            return "1", []
        parts.append("(dir = ? OR (dir >= ? AND dir < ?))")
        params += [root, root + "/", root + "0"]
    return "(" + " OR ".join(parts) + ")", params


@dataclass(frozen=True, slots=True)
class Words:
    """Keeps files whose folder path holds every word of `contains` and none of `excludes`.

    Case is ignored for ASCII letters (SQLite lower()). Empty: no filter.
    """

    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()

    def clause(self) -> tuple[str, list[str]]:
        parts = ["instr(lower(dir), ?) > 0"] * len(self.contains)
        parts += ["instr(lower(dir), ?) = 0"] * len(self.excludes)
        params = [w.lower() for w in (*self.contains, *self.excludes)]
        return (" AND " + " AND ".join(parts) if parts else ""), params


def _subtree_range(root: str) -> tuple[str, str]:
    """Bounds of the paths strictly beneath `root` (see _prefix_clause)."""
    root = root.rstrip("/")
    return root + "/", root + "0"


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _row(r: FileRecord) -> tuple:
    return (r.dir, r.name, name_key(r.name), r.size, r.mtime, r.kind, match_key(r.name))


def _record(row: sqlite3.Row | tuple) -> FileRecord:
    return FileRecord(dir=row[0], name=row[1], size=row[2], mtime=row[3], kind=row[4])


def _root_stats(row: tuple) -> RootStats:
    *head, is_mount, source, space_at = row
    return RootStats(
        *head, is_mount=None if is_mount is None else bool(is_mount),
        space_source=source, space_at=space_at,
    )  # fmt: skip


def _space_values(space: DriveSpace | None, now: float) -> tuple:
    """(used_bytes, capacity_bytes, is_mount, space_source, space_at) column values."""
    if space is None:
        return (None,) * 5
    is_mount = None if space.is_mount is None else int(space.is_mount)
    taken = now if space.taken_at is None else space.taken_at
    return space.used, space.capacity, is_mount, space.source, taken


def _new_root_stats(
    root: str, files: int, total: int, errors: int, space: DriveSpace | None
) -> RootStats:
    now = time.time()
    return _root_stats((root, files, total, now, errors, *_space_values(space, now)))


class Index:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_db_path()
        if self.path != ":memory:":
            self.path = os.path.abspath(self.path)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._guarded = False  # temp.bad_shots is up to date (_shot_guard)
        try:
            self.conn.executescript(_TABLES + _EXIF_TABLE + _SMART_TABLE)
            self._migrate()
            self.conn.executescript(_INDEXES)
        except BaseException:
            self.conn.close()
            raise

    def _migrate(self) -> None:
        """Bring an older database up to _VERSION in place, in one transaction."""
        if self._version() >= _VERSION:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            version = self._version()  # another process may have migrated meanwhile
            if version < 2:
                # Scans from before the dirs table never recorded non-media content.
                for table in ("files", "staging", "dirs", "roots"):
                    self.conn.execute(f"DELETE FROM {table}")
            if version < 4:
                for table in ("files", "staging"):
                    columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if "match_key" not in columns:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN match_key TEXT NOT NULL DEFAULT ''"
                        )
                    self._refill_match_keys(table)
            if version < 5:
                columns = {r[1] for r in self.conn.execute("PRAGMA table_info(roots)")}
                for column, kind in _SPACE_COLUMNS.items():
                    if column not in columns:
                        self.conn.execute(f"ALTER TABLE roots ADD COLUMN {column} {kind}")
            # 6: the exif table is created by _EXIF_TABLE (IF NOT EXISTS); nothing to convert.
            if version < 7:
                columns = {r[1] for r in self.conn.execute("PRAGMA table_info(exif)")}
                if "shutter_mode" not in columns:
                    self.conn.execute("DROP TABLE exif")
                    self.conn.execute(_EXIF_TABLE)
            # 8: the smart table is created by _SMART_TABLE (IF NOT EXISTS); nothing to convert.
            if version < _VERSION:
                self.conn.execute(f"PRAGMA user_version = {_VERSION}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _refill_match_keys(self, table: str) -> None:
        """Recompute match_key for every row of `table`, a batch at a time in key order.

        Runs inside the caller's transaction.
        """
        last: tuple[str, str] = ("", "")
        while True:
            rows = self.conn.execute(
                f"SELECT dir, name FROM {table} WHERE (dir, name) > (?, ?) "
                "ORDER BY dir, name LIMIT ?",
                (*last, _BATCH),
            ).fetchall()
            if not rows:
                return
            self.conn.executemany(
                f"UPDATE {table} SET match_key = ? WHERE dir = ? AND name = ?",
                [(match_key(n), d, n) for d, n in rows],
            )
            last = rows[-1]

    def close(self) -> None:
        self.conn.close()

    def __del__(self) -> None:
        conn = getattr(self, "conn", None)  # absent if __init__ failed early
        if conn is not None:
            conn.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ---------------------------------------------------------------------

    def replace_root(
        self,
        root: str,
        records: Iterable[FileRecord],
        errors: int = 0,
        dir_stats: DirStats | None = None,
        space: DriveSpace | None = None,
    ) -> RootStats:
        """Replace everything indexed under `root` with `records`, in one transaction.

        `dir_stats` maps directories to (other, errors) counts of entries not indexed;
        `space` is the drive's space as measured after the walk (None: not recorded).
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        files = total = 0
        self._guarded = False
        with self.conn:
            self.conn.execute(f"DELETE FROM files WHERE {where}", params)
            self._replace_dirs(where, params, dir_stats)
            it = iter(records)
            while batch := list(itertools.islice(it, _BATCH)):
                self.conn.executemany(
                    f"INSERT OR REPLACE INTO files ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [_row(r) for r in batch],
                )
                files += len(batch)
                total += sum(r.size for r in batch)
            stats = _new_root_stats(root, files, total, errors, space)
            self._set_root(stats)
        return stats

    # Streaming replacement: records for several roots may arrive interleaved. They
    # accumulate in `staging` (committed per batch), and finish_root swaps them into
    # `files` in one short transaction, so an interrupted scan leaves the root's
    # previous contents intact.

    def begin_root(self, root: str) -> None:
        """Start a streaming rescan of `root`, discarding leftovers of an aborted one."""
        self.discard_staged(root)

    def discard_staged(self, root: str) -> None:
        """Drop the records staged for `root`, leaving its indexed contents as they were."""
        where, params = _prefix_clause([root])
        with self.conn:
            self.conn.execute(f"DELETE FROM staging WHERE {where}", params)

    def add_records(self, records: Iterable[FileRecord]) -> None:
        """Stage records for roots opened with begin_root."""
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO staging ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_row(r) for r in records),
            )

    def finish_root(
        self,
        root: str,
        errors: int = 0,
        dir_stats: DirStats | None = None,
        space: DriveSpace | None = None,
    ) -> RootStats:
        """Replace everything indexed under `root` with its staged records.

        `dir_stats` (as from scanner.walk) replaces the root's per-directory counts, and
        `space` (as from scanner.drive_space) its recorded drive space.
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        self._guarded = False
        with self.conn:
            files, total = self.conn.execute(
                f"SELECT count(*), coalesce(sum(size), 0) FROM staging WHERE {where}", params
            ).fetchone()
            self.conn.execute(f"DELETE FROM files WHERE {where}", params)
            self._replace_dirs(where, params, dir_stats)
            self.conn.execute(
                f"INSERT INTO files ({_COLUMNS}) SELECT {_COLUMNS} FROM staging WHERE {where}",
                params,
            )
            self.conn.execute(f"DELETE FROM staging WHERE {where}", params)
            stats = _new_root_stats(root, files, total, errors, space)
            self._set_root(stats)
        return stats

    def _set_root(self, stats: RootStats) -> None:
        """Record `stats`; rows of roots nested inside it are superseded by its data."""
        self.conn.execute(
            "DELETE FROM roots WHERE path >= ? AND path < ?", _subtree_range(stats.root)
        )
        self.conn.execute(
            f"INSERT OR REPLACE INTO roots ({_ROOT_COLUMNS}) VALUES ({', '.join('?' * 10)})",
            (
                stats.root, stats.files, stats.bytes, stats.scanned_at, stats.errors,
                *_space_values(stats.space, stats.scanned_at),
            ),
        )  # fmt: skip

    def set_space(self, root: str, space: DriveSpace) -> RootStats | None:
        """Record `space` for the indexed root `root` (exact path); None if not indexed.

        Never adds a roots row: a root that was never scanned stays unscanned.
        """
        root = root.rstrip("/") or "/"
        with self.conn:
            updated = self.conn.execute(
                "UPDATE roots SET used_bytes = ?, capacity_bytes = ?, is_mount = ?, "
                "space_source = ?, space_at = ? WHERE path = ?",
                (*_space_values(space, time.time()), root),
            ).rowcount
        return self.root_stats(root) if updated else None

    def forget_root(self, root: str) -> int:
        """Remove everything indexed at or under `root` (files, dirs, staging, exif, roots).

        SMART readings stay: they belong to a physical drive, not a path. Only the index
        is touched. Returns the number of files removed.
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        self._guarded = False
        with self.conn:
            removed = self.conn.execute(f"DELETE FROM files WHERE {where}", params).rowcount
            for table in ("dirs", "staging", "exif"):
                self.conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            self.conn.execute(
                "DELETE FROM roots WHERE path = ? OR (path >= ? AND path < ?)",
                (root, *_subtree_range(root)),
            )
        return removed

    def _replace_dirs(self, where: str, params: list[str], dir_stats: DirStats | None) -> None:
        self.conn.execute(f"DELETE FROM dirs WHERE {where}", params)
        if dir_stats:
            self.conn.executemany(
                "INSERT OR REPLACE INTO dirs VALUES (?, ?, ?)",
                ((d.rstrip("/") or "/", o, e) for d, (o, e) in dir_stats.items() if o or e),
            )

    # -- reading ---------------------------------------------------------------------

    def root_stats(self, root: str) -> RootStats | None:
        row = self.conn.execute(
            f"SELECT {_ROOT_COLUMNS} FROM roots WHERE path = ?", (root.rstrip("/") or "/",)
        ).fetchone()
        return _root_stats(row) if row else None

    def roots(self) -> list[RootStats]:
        """Every scanned root, sorted by path."""
        rows = self.conn.execute(f"SELECT {_ROOT_COLUMNS} FROM roots ORDER BY path")
        return [_root_stats(r) for r in rows]

    def containing_root(self, path: str) -> RootStats | None:
        """The innermost scanned root that is `path` or contains it."""
        path = path.rstrip("/") or "/"
        found = [r for r in self.roots() if _within(path, r.root)]
        return max(found, key=lambda r: len(r.root)) if found else None

    def indexed(self, path: str) -> bool:
        """True if `path` was scanned as a root or has indexed files under it."""
        return self.root_stats(path) is not None or self.totals([path])[0] > 0

    def totals(self, roots: Sequence[str]) -> tuple[int, int]:
        """(file count, total bytes) indexed under `roots`."""
        where, params = _prefix_clause(roots)
        count, total = self.conn.execute(
            f"SELECT count(*), coalesce(sum(size), 0) FROM files WHERE {where}", params
        ).fetchone()
        return count, total

    def dir_totals(self, roots: Sequence[str]) -> Iterator[tuple[str, int, int]]:
        """(dir, file count, total bytes) of the photos/videos of every folder under `roots`.

        Files indexed with --all-files (kind 'other') are left out.
        """
        where, params = _prefix_clause(roots)
        yield from self.conn.execute(
            f"SELECT dir, count(*), coalesce(sum(size), 0) FROM files "
            f"WHERE {where} AND kind != 'other' GROUP BY dir",
            params,
        )

    def kind_totals(self, roots: Sequence[str]) -> dict[str, tuple[int, int]]:
        """kind -> (file count, total bytes) indexed under `roots`."""
        where, params = _prefix_clause(roots)
        rows = self.conn.execute(
            f"SELECT kind, count(*), coalesce(sum(size), 0) FROM files WHERE {where} GROUP BY kind",
            params,
        )
        return {kind: (n, b) for kind, n, b in rows}

    def name_buckets(
        self, roots: Sequence[str], *, by_size: bool = True, exact_names: bool = False
    ) -> Iterator[list[FileRecord]]:
        """Yield lists of files sharing a name key (and size, if by_size).

        The key is names.match_key (names equal up to copy marks or renaming around a
        camera file number), or with `exact_names` the case-insensitive name. Only
        buckets with two or more files are yielded. Streams from SQLite, so memory use
        is bounded by the largest bucket rather than the inventory size.
        """
        where, params = _prefix_clause(roots)
        column = "name_key" if exact_names else "match_key"
        keys = f"{column}, size" if by_size else column
        sql = f"""
            WITH files_in AS (SELECT * FROM files WHERE {where} AND size > 0),
                 dup AS (SELECT {keys} FROM files_in GROUP BY {keys} HAVING count(*) > 1)
            SELECT dir, name, size, mtime, kind, {column}
            FROM files_in JOIN dup USING ({keys})
            ORDER BY {keys}, dir, name
        """
        rows = self.conn.execute(sql, params)
        keyfn = (lambda r: (r[5], r[2])) if by_size else (lambda r: r[5])
        for _, group in itertools.groupby(rows, key=keyfn):
            yield [_record(r) for r in group]

    def coverage_buckets(
        self,
        roots: Sequence[str],
        other_roots: Sequence[str],
        *,
        by_size: bool = True,
        exact_names: bool = False,
        words: Words = Words(),
        other_words: Words = Words(),
    ) -> Iterator[tuple[str, list[tuple[FileRecord, bool]]]]:
        """Yield (key, bucket) for the name buckets (as name_buckets) of files under `roots`.

        Only non-empty files are included. Each bucket entry is (file, True if it lies
        under `other_roots`). Every non-empty file
        under `roots` is in exactly one bucket, with every file under `other_roots`
        sharing its key; buckets without a file under `roots` are skipped. The two sides
        must not overlap: disjoint roots, or `words`/`other_words` filters that no folder
        can pass both of. Streams from SQLite in key order.
        """
        where, params = _prefix_clause(roots)
        other, other_params = _prefix_clause(other_roots)
        extra, extra_params = words.clause()
        other_extra, other_extra_params = other_words.clause()
        where, params = where + extra, params + extra_params
        other, other_params = other + other_extra, other_params + other_extra_params
        column = "name_key" if exact_names else "match_key"
        order = "6, 3, 1, 2" if by_size else "6, 1, 2"
        sql = f"""
            SELECT dir, name, size, mtime, kind, {column}, 0 FROM files
            WHERE {where} AND size > 0
            UNION ALL
            SELECT dir, name, size, mtime, kind, {column}, 1 FROM files
            WHERE {other} AND size > 0
            ORDER BY {order}
        """
        rows = self.conn.execute(sql, params + other_params)
        keyfn = (lambda r: (r[5], r[2])) if by_size else (lambda r: r[5])
        for _, group in itertools.groupby(rows, key=keyfn):
            bucket = list(group)
            if any(not r[6] for r in bucket):
                yield bucket[0][5], [(_record(r), bool(r[6])) for r in bucket]

    def name_sizes(
        self, roots: Sequence[str], *, exact_names: bool = False, words: Words = Words()
    ) -> dict[str, list[int]]:
        """Match key (or name key) -> sorted sizes of the non-empty files under `roots`."""
        where, params = _prefix_clause(roots)
        extra, extra_params = words.clause()
        column = "name_key" if exact_names else "match_key"
        sizes: dict[str, list[int]] = {}
        rows = self.conn.execute(
            f"SELECT {column}, size FROM files WHERE {where}{extra} AND size > 0 "
            f"ORDER BY {column}, size",
            params + extra_params,
        )
        for key, size in rows:
            sizes.setdefault(key, []).append(size)
        return sizes

    def empty_files(self, roots: Sequence[str]) -> int:
        """Number of 0-byte files indexed under `roots`."""
        where, params = _prefix_clause(roots)
        return self.conn.execute(
            f"SELECT count(*) FROM files WHERE {where} AND size = 0", params
        ).fetchone()[0]

    def extras_under(self, roots: Sequence[str]) -> dict[str, int]:
        """dir -> entries not indexed (other + errors), for every such dir under `roots`."""
        where, params = _prefix_clause(roots)
        rows = self.conn.execute(f"SELECT dir, other + errors FROM dirs WHERE {where}", params)
        return {d: n for d, n in rows if n}

    def list_dir(self, dir: str) -> list[FileRecord]:
        """All indexed files directly inside `dir` (not subfolders), sorted by name."""
        rows = self.conn.execute(
            "SELECT dir, name, size, mtime, kind FROM files WHERE dir = ? ORDER BY name",
            (dir,),
        )
        return [_record(r) for r in rows]

    def file_rows(self, roots: Sequence[str]) -> Iterator[tuple[str, str, int, float, str]]:
        """(dir, name, size, mtime, kind) of every file indexed under `roots`, unordered."""
        where, params = _prefix_clause(roots)
        yield from self.conn.execute(
            f"SELECT dir, name, size, mtime, kind FROM files WHERE {where}", params
        )

    def keys_under(self, roots: Sequence[str]) -> tuple[set[str], set[str]]:
        """(match keys, name keys) of the non-empty files under `roots`."""
        where, params = _prefix_clause(roots)
        rows = self.conn.execute(
            f"SELECT DISTINCT match_key, name_key FROM files WHERE {where} AND size > 0", params
        )
        pairs = rows.fetchall()
        return {m for m, _ in pairs}, {n for _, n in pairs}

    def rows_with_keys(
        self, roots: Sequence[str], match_keys: Iterable[str], name_keys: Iterable[str]
    ) -> Iterator[tuple]:
        """Raw rows (in `_COLUMNS` order) of the non-empty files under `roots` whose match
        key is in `match_keys` or whose name key is in `name_keys`; a row may repeat."""
        where, params = _prefix_clause(roots)
        for column, keys in (("match_key", match_keys), ("name_key", name_keys)):
            keys = list(keys)
            for i in range(0, len(keys), _KEY_CHUNK):
                chunk = keys[i : i + _KEY_CHUNK]
                yield from self.conn.execute(
                    f"SELECT {_COLUMNS} FROM files WHERE {where} AND size > 0 "
                    f"AND {column} IN ({', '.join('?' * len(chunk))})",
                    params + chunk,
                )

    def add_rows(self, rows: Iterable[tuple]) -> None:
        """Insert raw rows from rows_with_keys (another index's files); no roots entry."""
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO files ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )

    def media_dates(
        self, roots: Sequence[str]
    ) -> Iterator[tuple[str, str, int, float, str, str | None]]:
        """(dir, name, size, mtime, kind, taken) of the non-empty photos/videos under
        `roots`; `taken` is the cached EXIF DateTimeOriginal when valid, else None."""
        where, params = _prefix_clause(roots)
        yield from self.conn.execute(
            f"SELECT dir, name, size, mtime, kind, CASE WHEN {_VALID_EXIF} THEN e.taken END "
            f"FROM files LEFT JOIN exif e USING (dir, name) "
            f"WHERE {where} AND size > 0 AND kind != 'other'",
            params,
        )

    def taken_in(self, dir: str) -> dict[str, str]:
        """Name -> cached EXIF DateTimeOriginal of the photos directly in `dir` whose EXIF
        row is still valid for the file as indexed."""
        rows = self.conn.execute(
            f"SELECT name, e.taken FROM files JOIN exif e USING (dir, name) "
            f"WHERE dir = ? AND e.taken IS NOT NULL AND {_VALID_EXIF}",
            (dir,),
        )
        return dict(rows)

    def dir_extras(self, dir: str) -> tuple[int, int]:
        """(other, errors): entries directly in `dir` that were not indexed."""
        row = self.conn.execute(
            "SELECT other, errors FROM dirs WHERE dir = ?", (dir.rstrip("/") or "/",)
        ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def subdirs(self, dir: str) -> list[str]:
        """Immediate children of `dir` that hold indexed files or unindexed entries."""
        lo, hi = dir.rstrip("/") + "/", dir.rstrip("/") + "0"
        rows = self.conn.execute(
            "SELECT dir FROM files WHERE dir >= ? AND dir < ? "
            "UNION SELECT dir FROM dirs WHERE dir >= ? AND dir < ?",
            (lo, hi, lo, hi),
        )
        children = set()
        for (d,) in rows:
            rest = d[len(lo):]
            children.add(lo + rest.split("/", 1)[0])
        return sorted(children)

    # -- EXIF cache (dpf exif, mode 1b) ----------------------------------------------

    def exif_todo(
        self, roots: Sequence[str], *, retry_errors: bool = False
    ) -> list[tuple[str, str, int, float, str]]:
        """(dir, name, size, mtime, kind) of the non-empty photos under `roots` that lack a
        valid EXIF row (or whose row is an error, with `retry_errors`), in folder order.

        Videos are never read: they have no shot identity and match by mode 1 rules.
        """
        where, params = _prefix_clause(roots)
        stale = "e.status IS NULL OR NOT (" + _VALID_EXIF + ")"
        if retry_errors:
            stale += " OR e.status = 'error'"
        return self.conn.execute(
            f"SELECT dir, name, size, mtime, kind FROM files LEFT JOIN exif e USING (dir, name) "
            f"WHERE {where} AND size > 0 AND kind = 'image' AND ({stale}) ORDER BY dir, name",
            params,
        ).fetchall()

    def put_exif(self, rows: Iterable[Sequence]) -> None:
        """Insert or replace EXIF rows (values in EXIF_COLUMNS order), in one transaction."""
        marks = ", ".join("?" * len(EXIF_COLUMNS))
        self._guarded = False
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO exif ({', '.join(EXIF_COLUMNS)}) VALUES ({marks})", rows
            )

    def exif_row(self, dir: str, name: str) -> dict | None:
        """The cached EXIF row of one file (valid or not), as a dict; None if absent."""
        row = self.conn.execute(
            f"SELECT {', '.join(EXIF_COLUMNS)} FROM exif WHERE dir = ? AND name = ?", (dir, name)
        ).fetchone()
        return dict(zip(EXIF_COLUMNS, row)) if row else None

    def exif_status(self, roots: Sequence[str]) -> dict[str, int]:
        """Non-empty photos/videos under `roots` by EXIF state: "ok" (a usable shot key),
        "unreliable" (a key the guard dropped), "none" (read, no key), "error", "missing"
        (a photo with no valid row) and "video" (videos are never read)."""
        self._shot_guard()
        where, params = _prefix_clause(roots)
        rows = self.conn.execute(
            f"SELECT CASE WHEN kind = 'video' THEN 'video' "
            f"WHEN e.status IS NULL OR NOT ({_VALID_EXIF}) THEN 'missing' "
            f"WHEN e.status = 'ok' AND {_BAD_SHOT} THEN 'unreliable' "
            f"ELSE e.status END, count(*) FROM files LEFT JOIN exif e USING (dir, name) "
            f"WHERE {where} AND size > 0 AND kind != 'other' GROUP BY 1",
            params,
        )
        counts = {"ok": 0, "unreliable": 0, "none": 0, "error": 0, "missing": 0, "video": 0}
        counts.update(dict(rows.fetchall()))
        return counts

    def multi_format_shots(self, roots: Sequence[str]) -> int:
        """Camera shots under `roots` cached in two or more formats (e.g. RAW+JPG)."""
        self._shot_guard()
        where, params = _prefix_clause(roots)
        return self.conn.execute(
            f"SELECT count(*) FROM (SELECT e.ident FROM files JOIN exif e USING (dir, name) "
            f"WHERE {where} AND size > 0 AND e.shot IS NOT NULL AND {_VALID_EXIF} "
            f"AND NOT {_BAD_SHOT} "
            f"GROUP BY e.ident HAVING count(DISTINCT e.fmt) > 1)",
            params,
        ).fetchone()[0]

    def shot_groups(self, roots: Sequence[str]) -> Iterator[tuple[str, list[FileRecord]]]:
        """Yield (shot, files) for every shot key held by two or more non-empty files under
        `roots` with valid EXIF rows. Streams in shot order. Unreliable keys are left out."""
        self._shot_guard()
        where, params = _prefix_clause(roots)
        sql = f"""
            WITH s AS (
                SELECT dir, name, size, mtime, kind, e.shot AS shot
                FROM files JOIN exif e USING (dir, name)
                WHERE {where} AND size > 0 AND e.shot IS NOT NULL AND {_VALID_EXIF}
                  AND NOT {_BAD_SHOT}
            ), d AS (SELECT shot FROM s GROUP BY shot HAVING count(*) > 1)
            SELECT dir, name, size, mtime, kind, shot FROM s JOIN d USING (shot)
            ORDER BY shot, dir, name
        """
        rows = self.conn.execute(sql, params)
        for shot, group in itertools.groupby(rows, key=lambda r: r[5]):
            yield shot, [_record(r) for r in group]

    def shot_name_buckets(
        self, roots: Sequence[str], *, by_size: bool = True, exact_names: bool = False
    ) -> Iterator[list[tuple[FileRecord, str | None]]]:
        """name_buckets, with each file's usable shot key (None without one)."""
        self._shot_guard()
        where, params = _prefix_clause(roots)
        column = "name_key" if exact_names else "match_key"
        keys = f"{column}, size" if by_size else column
        sql = f"""
            WITH files_in AS (SELECT * FROM files WHERE {where} AND size > 0),
                 dup AS (SELECT {keys} FROM files_in GROUP BY {keys} HAVING count(*) > 1),
                 picked AS (SELECT files_in.* FROM files_in JOIN dup USING ({keys}))
            SELECT dir, name, files.size, files.mtime, kind, {column}, {_SHOT}
            FROM picked AS files LEFT JOIN exif e USING (dir, name)
            ORDER BY {column}, {"files.size, " if by_size else ""}dir, name
        """
        rows = self.conn.execute(sql, params)
        keyfn = (lambda r: (r[5], r[2])) if by_size else (lambda r: r[5])
        for _, group in itertools.groupby(rows, key=keyfn):
            yield [(_record(r), r[6]) for r in group]

    def shot_coverage_buckets(
        self,
        roots: Sequence[str],
        other_roots: Sequence[str],
        *,
        by_size: bool = True,
        exact_names: bool = False,
        words: Words = Words(),
        other_words: Words = Words(),
    ) -> Iterator[tuple[str, list[tuple[FileRecord, bool, str | None, int]]]]:
        """coverage_buckets, each entry extended to (file, other side, shot, elsewhere).

        `shot` is the file's usable shot key (None without one). `elsewhere`, for a file
        under `roots`, is 2 when a non-empty file of the same size under `other_roots`
        holds its shot (valid row), wherever it lies and whatever its name; 1 when only
        files of other sizes do; else 0.
        """
        self._shot_guard()
        where, params = _prefix_clause(roots)
        other, other_params = _prefix_clause(other_roots)
        extra, extra_params = words.clause()
        other_extra, other_extra_params = other_words.clause()
        where, params = where + extra, params + extra_params
        other, other_params = other + other_extra, other_params + other_extra_params
        column = "name_key" if exact_names else "match_key"
        order = "6, 3, 1, 2" if by_size else "6, 1, 2"
        elsewhere = f"""
            CASE WHEN e.shot IS NOT NULL AND {_VALID_EXIF} AND NOT {_BAD_SHOT} THEN (
                SELECT max(CASE WHEN f2.size = files.size THEN 2 ELSE 1 END)
                FROM exif e2 JOIN files f2 USING (dir, name)
                WHERE e2.shot = e.shot AND f2.size = e2.read_size AND f2.mtime = e2.read_mtime
                  AND f2.size > 0 AND {other}
            ) ELSE 0 END
        """
        sql = f"""
            SELECT dir, name, size, mtime, kind, {column}, 0, {_SHOT}, {elsewhere}
            FROM files LEFT JOIN exif e USING (dir, name)
            WHERE {where} AND size > 0
            UNION ALL
            SELECT dir, name, size, mtime, kind, {column}, 1, {_SHOT}, 0
            FROM files LEFT JOIN exif e USING (dir, name)
            WHERE {other} AND size > 0
            ORDER BY {order}
        """
        rows = self.conn.execute(sql, other_params + params + other_params)
        keyfn = (lambda r: (r[5], r[2])) if by_size else (lambda r: r[5])
        for _, group in itertools.groupby(rows, key=keyfn):
            bucket = list(group)
            if any(not r[6] for r in bucket):
                yield bucket[0][5], [(_record(r), bool(r[6]), r[7], r[8] or 0) for r in bucket]

    def _shot_guard(self) -> None:
        """Fill temp.bad_shots with the shot keys that cannot identify one photo.

        A key is unreliable when, across the whole index, its files (same format, valid
        rows) carry different camera numbers (names.camera_token of their match keys:
        "DSC01000" and "DSC01001" of a silent-shutter burst share serial, shutter count
        and second), or when one folder holds it under two names (match keys). A copy
        renamed without any camera number never triggers it alone. Recomputed after any
        change to files or exif rows.
        """
        if self._guarded:
            return
        self.conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS bad_shots (shot TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        rows = self.conn.execute(f"""
            SELECT e.shot, dir, match_key FROM files JOIN exif e USING (dir, name)
            WHERE {_VALID_EXIF} AND e.shot IN (
                SELECT shot FROM exif WHERE shot IS NOT NULL GROUP BY shot HAVING count(*) > 1
            )
            ORDER BY e.shot
        """)
        bad = []
        for shot, group in itertools.groupby(rows, key=lambda r: r[0]):
            numbers: set[str] = set()
            keys: dict[str, set[str]] = {}
            for _, d, key in group:
                number = camera_token(os.path.splitext(key)[0])
                if number:
                    numbers.add(number)
                keys.setdefault(d, set()).add(key)
            if len(numbers) > 1 or any(len(k) > 1 for k in keys.values()):
                bad.append((shot,))
        with self.conn:
            self.conn.execute("DELETE FROM temp.bad_shots")
            self.conn.executemany("INSERT INTO temp.bad_shots VALUES (?)", bad)
        self._guarded = True

    # -- SMART readings (dpf smart) --------------------------------------------------

    def put_smart(self, records: Iterable[Mapping], source: str) -> int:
        """Store SMART readings (smart-summary drive entries plus `labels`/`mounts`).

        A reading is keyed by serial number and time (`collected_at`), so storing one again
        (e.g. importing a report twice) changes nothing. Entries without a serial number
        or a valid time are skipped. Returns the number of readings added.
        """
        rows = [row for rec in records if (row := _smart_row(rec, source)) is not None]
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany(
                f"INSERT OR IGNORE INTO smart ({', '.join(_SMART_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_SMART_COLUMNS))})",
                rows,
            )
            return self.conn.total_changes - before

    def smart_latest(self, *, full: bool = False) -> list[dict]:
        """The newest reading of each drive, by serial; `full` adds smartctl's output."""
        rows = self.conn.execute(
            f"SELECT {_smart_select(full)} FROM smart s WHERE read_at = "
            "(SELECT max(read_at) FROM smart WHERE serial = s.serial) ORDER BY serial"
        )
        return [_smart_record(r, full) for r in rows]

    def smart_history(self, serial: str | None = None, *, full: bool = False) -> list[dict]:
        """Every reading of drive `serial` (of every drive if None), oldest first."""
        where, params = ("WHERE serial = ?", (serial.strip(),)) if serial is not None else ("", ())
        rows = self.conn.execute(
            f"SELECT {_smart_select(full)} FROM smart {where} ORDER BY serial, read_at", params
        )
        return [_smart_record(r, full) for r in rows]


_SMART_COLUMNS = (
    "serial", "read_at", "source", "host", "device", "model", "labels", "mounts", "summary",
    "flags", "kernel", "partitions", "smartctl",
)  # fmt: skip


def _smart_select(full: bool) -> str:
    return ", ".join(_SMART_COLUMNS[:-1] + (("smartctl",) if full else ()))


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _smart_row(rec: Mapping, source: str) -> tuple | None:
    summary = rec.get("summary") or {}
    serial = str(summary.get("serial_number") or "").strip()
    try:
        when = datetime.fromisoformat(rec.get("collected_at") or "")
    except (TypeError, ValueError):
        return None
    if not serial:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (
        serial, when.timestamp(), source, rec.get("host"), rec.get("device_path"),
        summary.get("model"), _json(list(rec.get("labels") or [])),
        _json(list(rec.get("mounts") or [])), _json({**summary, "serial_number": serial}),
        _json(list(rec.get("flags") or [])),
        _json(rec.get("kernel_log_issues")), _json(rec.get("partitions")),
        _json(rec.get("smartctl")),
    )  # fmt: skip


def _smart_record(row: Sequence, full: bool) -> dict:
    """A stored reading in the smart-summary record format (plus labels, mounts, source)."""
    values = dict(zip(_SMART_COLUMNS, row))

    def load(key: str):
        text = values.get(key)
        return None if text is None else json.loads(text)

    rec = {
        "collected_at": datetime.fromtimestamp(values["read_at"], timezone.utc).isoformat(
            timespec="seconds"
        ),
        "host": values["host"],
        "device_path": values["device"],
        "summary": load("summary"),
        "flags": load("flags"),
        "partitions": load("partitions"),
        "kernel_log_issues": load("kernel"),
        "labels": load("labels"),
        "mounts": load("mounts"),
        "source": values["source"],
    }
    if full:
        rec["smartctl"] = load("smartctl")
    return rec
