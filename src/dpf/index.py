"""SQLite inventory of scanned media files.

The database lives on the local disk (default ~/.cache/dpf/index.db), never on the
drives being scanned. Files are addressed by absolute path; queries select a set of
roots by path prefix, so nested or overlapping roots are handled naturally.

Besides media files, the `dirs` table records per directory how many entries were not
indexed (non-media files, symlinks, unreadable entries), so a folder is never called a
match while it holds content that was not compared.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence

from dpf.models import FileRecord, RootStats
from dpf.names import match_key

_TABLES = """
CREATE TABLE IF NOT EXISTS roots (
    path       TEXT PRIMARY KEY,
    files      INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    errors     INTEGER NOT NULL,
    scanned_at REAL NOT NULL
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

# Created after _migrate, since they may name columns that older databases lack.
_INDEXES = """
CREATE INDEX IF NOT EXISTS files_name_size ON files (name_key, size);
CREATE INDEX IF NOT EXISTS files_match_size ON files (match_key, size);
"""

_COLUMNS = "dir, name, name_key, size, mtime, kind, match_key"

# Schema history (PRAGMA user_version):
#   1  files, staging, roots
#   2  dirs table (older inventories never recorded non-media content: dropped)
#   3  match_key column (names.match_key), added and filled in place
# Rule: schema changes migrate in place (ALTER TABLE, recompute derived columns) and
# bump _VERSION. Never drop an inventory unless its data cannot be trusted: drives that
# are not connected cannot be rescanned, so the index may be their only record.
# Changing names.match_key also needs a version bump and a _refill_match_keys step.
_VERSION = 3

_BATCH = 10_000

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


class Index:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_db_path()
        if self.path != ":memory:":
            self.path = os.path.abspath(self.path)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        try:
            self.conn.executescript(_TABLES)
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
            if version < 3:
                for table in ("files", "staging"):
                    columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if "match_key" not in columns:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN match_key TEXT NOT NULL DEFAULT ''"
                        )
                    self._refill_match_keys(table)
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
    ) -> RootStats:
        """Replace everything indexed under `root` with `records`, in one transaction.

        `dir_stats` maps directories to (other, errors) counts of entries not indexed.
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        files = total = 0
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
            stats = RootStats(root, files, total, time.time(), errors)
            self._set_root(stats)
        return stats

    # Streaming replacement: records for several roots may arrive interleaved. They
    # accumulate in `staging` (committed per batch), and finish_root swaps them into
    # `files` in one short transaction, so an interrupted scan leaves the root's
    # previous contents intact.

    def begin_root(self, root: str) -> None:
        """Start a streaming rescan of `root`, discarding leftovers of an aborted one."""
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
        self, root: str, errors: int = 0, dir_stats: DirStats | None = None
    ) -> RootStats:
        """Replace everything indexed under `root` with its staged records.

        `dir_stats` (as from scanner.walk) replaces the root's per-directory counts.
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
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
            stats = RootStats(root, files, total, time.time(), errors)
            self._set_root(stats)
        return stats

    def _set_root(self, stats: RootStats) -> None:
        """Record `stats`; rows of roots nested inside it are superseded by its data."""
        self.conn.execute(
            "DELETE FROM roots WHERE path >= ? AND path < ?", _subtree_range(stats.root)
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO roots VALUES (?, ?, ?, ?, ?)",
            (stats.root, stats.files, stats.bytes, stats.errors, stats.scanned_at),
        )

    def forget_root(self, root: str) -> int:
        """Remove everything indexed at or under `root` (files, dirs, staging, roots rows).

        Only the index is touched. Returns the number of files removed.
        """
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        with self.conn:
            removed = self.conn.execute(f"DELETE FROM files WHERE {where}", params).rowcount
            for table in ("dirs", "staging"):
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
            "SELECT path, files, bytes, scanned_at, errors FROM roots WHERE path = ?",
            (root.rstrip("/") or "/",),
        ).fetchone()
        return RootStats(*row) if row else None

    def roots(self) -> list[RootStats]:
        """Every scanned root, sorted by path."""
        rows = self.conn.execute(
            "SELECT path, files, bytes, scanned_at, errors FROM roots ORDER BY path"
        )
        return [RootStats(*r) for r in rows]

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

    def list_dir(self, dir: str) -> list[FileRecord]:
        """All indexed files directly inside `dir` (not subfolders), sorted by name."""
        rows = self.conn.execute(
            "SELECT dir, name, size, mtime, kind FROM files WHERE dir = ? ORDER BY name",
            (dir,),
        )
        return [_record(r) for r in rows]

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
