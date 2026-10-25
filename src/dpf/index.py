"""SQLite inventory of scanned media files.

The database lives on the local disk (default ~/.cache/dpf/index.db), never on the
drives being scanned. Files are addressed by absolute path; queries select a set of
roots by path prefix, so nested or overlapping roots are handled naturally.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence

from dpf.models import FileRecord, RootStats

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    path       TEXT PRIMARY KEY,
    files      INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    errors     INTEGER NOT NULL,
    scanned_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    dir      TEXT NOT NULL,
    name     TEXT NOT NULL,
    name_key TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    kind     TEXT NOT NULL,
    PRIMARY KEY (dir, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS files_name_size ON files (name_key, size);
CREATE TABLE IF NOT EXISTS staging (
    dir      TEXT NOT NULL,
    name     TEXT NOT NULL,
    name_key TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    kind     TEXT NOT NULL,
    PRIMARY KEY (dir, name)
) WITHOUT ROWID;
"""

_BATCH = 10_000


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


def _row(r: FileRecord) -> tuple:
    return (r.dir, r.name, name_key(r.name), r.size, r.mtime, r.kind)


def _record(row: sqlite3.Row | tuple) -> FileRecord:
    return FileRecord(dir=row[0], name=row[1], size=row[2], mtime=row[3], kind=row[4])


class Index:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_db_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ---------------------------------------------------------------------

    def replace_root(
        self, root: str, records: Iterable[FileRecord], errors: int = 0
    ) -> RootStats:
        """Replace everything indexed under `root` with `records`, in one transaction."""
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        files = total = 0
        with self.conn:
            self.conn.execute(f"DELETE FROM files WHERE {where}", params)
            it = iter(records)
            while batch := list(itertools.islice(it, _BATCH)):
                self.conn.executemany(
                    "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)",
                    [_row(r) for r in batch],
                )
                files += len(batch)
                total += sum(r.size for r in batch)
            stats = RootStats(root, files, total, time.time(), errors)
            self.conn.execute(
                "INSERT OR REPLACE INTO roots VALUES (?, ?, ?, ?, ?)",
                (root, files, total, errors, stats.scanned_at),
            )
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
                "INSERT OR REPLACE INTO staging VALUES (?, ?, ?, ?, ?, ?)",
                (_row(r) for r in records),
            )

    def finish_root(self, root: str, errors: int = 0) -> RootStats:
        """Replace everything indexed under `root` with its staged records."""
        root = root.rstrip("/") or "/"
        where, params = _prefix_clause([root])
        with self.conn:
            files, total = self.conn.execute(
                f"SELECT count(*), coalesce(sum(size), 0) FROM staging WHERE {where}", params
            ).fetchone()
            self.conn.execute(f"DELETE FROM files WHERE {where}", params)
            self.conn.execute(f"INSERT INTO files SELECT * FROM staging WHERE {where}", params)
            self.conn.execute(f"DELETE FROM staging WHERE {where}", params)
            stats = RootStats(root, files, total, time.time(), errors)
            self.conn.execute(
                "INSERT OR REPLACE INTO roots VALUES (?, ?, ?, ?, ?)",
                (root, files, total, errors, stats.scanned_at),
            )
        return stats

    # -- reading ---------------------------------------------------------------------

    def root_stats(self, root: str) -> RootStats | None:
        row = self.conn.execute(
            "SELECT path, files, bytes, scanned_at, errors FROM roots WHERE path = ?",
            (root.rstrip("/") or "/",),
        ).fetchone()
        return RootStats(*row) if row else None

    def totals(self, roots: Sequence[str]) -> tuple[int, int]:
        """(file count, total bytes) indexed under `roots`."""
        where, params = _prefix_clause(roots)
        count, total = self.conn.execute(
            f"SELECT count(*), coalesce(sum(size), 0) FROM files WHERE {where}", params
        ).fetchone()
        return count, total

    def name_buckets(
        self, roots: Sequence[str], *, by_size: bool = True
    ) -> Iterator[list[FileRecord]]:
        """Yield lists of files sharing a case-insensitive name (and size, if by_size).

        Only buckets with two or more files are yielded. Streams from SQLite, so memory
        use is bounded by the largest bucket rather than the inventory size.
        """
        where, params = _prefix_clause(roots)
        keys = "name_key, size" if by_size else "name_key"
        sql = f"""
            WITH files_in AS (SELECT * FROM files WHERE {where}),
                 dup AS (SELECT {keys} FROM files_in GROUP BY {keys} HAVING count(*) > 1)
            SELECT dir, name, size, mtime, kind, name_key
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

    def subdirs(self, dir: str) -> list[str]:
        """Indexed directories that are immediate children of `dir`."""
        lo, hi = dir.rstrip("/") + "/", dir.rstrip("/") + "0"
        rows = self.conn.execute(
            "SELECT DISTINCT dir FROM files WHERE dir >= ? AND dir < ?", (lo, hi)
        )
        children = set()
        for (d,) in rows:
            rest = d[len(lo):]
            children.add(lo + rest.split("/", 1)[0])
        return sorted(children)
