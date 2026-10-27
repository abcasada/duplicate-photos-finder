"""Read-only media inventory: an iterative os.scandir walk, one thread per device.

Only scandir and lstat are used on the scanned drives. Symlinks are never followed.
Worker threads only read the filesystem; the calling thread owns the SQLite index.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator

from dpf import media
from dpf.index import Index
from dpf.models import DriveSpace, FileRecord, RootStats

BATCH_SIZE = 5_000
BATCH_SECONDS = 0.5
PROGRESS_SECONDS = 0.5

Progress = Callable[[str, int, int], None]


def normalize_roots(paths: Iterable[str]) -> list[str]:
    """Resolve, validate and dedupe roots, dropping any nested inside another root."""
    online, missing = split_roots(paths)
    if missing:
        raise ValueError(f"not an existing directory: {missing[0]}")
    return drop_nested(online)


def split_roots(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """(resolved existing directories, paths that do not exist, as given).

    Raises ValueError for a path that exists but is not a directory. Missing paths may
    be unplugged drives whose data is still in the index (see Index.indexed).
    """
    online, missing = [], []
    for p in paths:
        real = os.path.realpath(p)
        if os.path.isdir(real):
            online.append(real)
        elif os.path.exists(real):
            raise ValueError(f"not an existing directory: {p}")
        else:
            missing.append(p)
    return online, missing


def drop_nested(paths: Iterable[str]) -> list[str]:
    """Sorted, deduplicated `paths`, dropping any nested inside another."""
    roots: list[str] = []
    for r in sorted({p.rstrip("/") or "/" for p in paths}):
        if not any(_within(r, kept) for kept in roots):
            roots.append(r)
    return roots


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _encodable(s: str) -> bool:
    """False for names with undecodable bytes (surrogate escapes) SQLite cannot store."""
    if s.isascii():
        return True
    try:
        s.encode()
    except UnicodeEncodeError:
        return False
    return True


def _error(errors: list[str] | None, path: str, exc: OSError) -> None:
    if errors is not None:
        errors.append(f"{path}: {exc.strerror or exc}")


DirStats = dict[str, tuple[int, int]]


def walk(
    root: str,
    *,
    all_files: bool = False,
    errors: list[str] | None = None,
    dir_stats: DirStats | None = None,
) -> Iterator[FileRecord]:
    """Yield media files under `root` without following symlinks.

    Unreadable directories and files are reported to `errors` and skipped. If given,
    `dir_stats` maps each directory holding entries that were not indexed to
    (other, errors): `other` counts non-media files (unless all_files), symlinks and
    special files; `errors` counts unreadable or undecodable entries. A directory that
    cannot be listed is recorded under its own path. AppleDouble "._*" files and
    media.SKIP_DIRS are ignored entirely.
    """
    stack = [os.path.abspath(root)]
    while stack:
        dir = stack.pop()
        subdirs = []
        other = errs = 0
        try:
            with os.scandir(dir) as it:
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_symlink():
                            other += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if name in media.SKIP_DIRS:
                                continue
                            if not _encodable(name):
                                errs += 1
                                _error(errors, entry.path, OSError("undecodable name"))
                                continue
                            subdirs.append(entry.path)
                            continue
                        if media.is_junk(name):
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            other += 1
                            continue
                        kind = media.classify(name)
                        if kind is None:
                            if not all_files:
                                other += 1
                                continue
                            kind = media.OTHER
                        if not _encodable(name):
                            errs += 1
                            _error(errors, entry.path, OSError("undecodable name"))
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        errs += 1
                        _error(errors, entry.path, e)
                        continue
                    yield FileRecord(dir, name, st.st_size, st.st_mtime, kind)
        except OSError as e:
            errs += 1
            _error(errors, dir, e)
        if dir_stats is not None and (other or errs):
            dir_stats[dir] = (other, errs)
        stack.extend(sorted(subdirs, reverse=True))


def drive_space(path: str) -> DriveSpace | None:
    """Used and total bytes of the filesystem holding `path`, or None if unavailable.

    One os.statvfs call (filesystem counters, no file reads) plus os.path.ismount.
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return DriveSpace(
        used=(st.f_blocks - st.f_bfree) * st.f_frsize,
        capacity=st.f_blocks * st.f_frsize,
        is_mount=os.path.ismount(path),
    )


def _is_unusable_root(path: str) -> bool:
    """True if `path` lists no entries at all, or cannot be listed.

    A leftover mount point of an unplugged drive is empty; a failed or half-mounted drive
    may refuse to list. Either way a scan of it says nothing about the drive's contents.
    """
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return True


class _Stopped(Exception):
    pass


def _device_worker(
    roots: list[str], all_files: bool, out: queue.Queue, stop: threading.Event
) -> None:
    def put(item: tuple) -> None:
        while True:
            if stop.is_set():
                raise _Stopped
            try:
                out.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    try:
        for root in roots:
            errors: list[str] = []
            dir_stats: DirStats = {}
            batch: list[FileRecord] = []
            found = 0
            sent = time.monotonic()
            for rec in walk(root, all_files=all_files, errors=errors, dir_stats=dir_stats):
                batch.append(rec)
                found += 1
                if len(batch) >= BATCH_SIZE or time.monotonic() - sent >= BATCH_SECONDS:
                    put(("batch", root, batch))
                    batch, sent = [], time.monotonic()
            if batch:
                put(("batch", root, batch))
            empty = not found and _is_unusable_root(root) and (
                not (errors or dir_stats) or not os.access(root, os.R_OK | os.X_OK)
            )
            put(("end", root, (errors, dir_stats, empty, drive_space(root))))
        put(("done", None, None))
    except _Stopped:
        pass
    except BaseException as e:
        try:
            put(("error", None, e))
        except _Stopped:
            pass


def scan_roots(
    index: Index,
    roots: list[str],
    *,
    all_files: bool = False,
    progress: Progress | None = None,
    errors: list[str] | None = None,
    allow_empty: bool = False,
    kept: dict[str, int] | None = None,
) -> list[RootStats]:
    """Scan `roots` (as from normalize_roots) into `index`, replacing what each held.

    Roots on the same device are walked sequentially by one thread; devices are walked
    in parallel. Error messages are appended to `errors` if given; per-directory counts
    of entries not indexed (see walk) are stored with the root. A root whose scan is
    interrupted keeps its previous contents.

    A root that lists no entries at all while the index holds files under it is most
    likely an unmounted drive's leftover mount folder, and the index may be the only
    record of that drive. Unless `allow_empty`, such a root keeps its previous contents
    and is added to `kept` (root -> files still indexed). Returns stats, in the order
    of `roots`, for the roots that were replaced.

    Each replaced root also records its drive's space (drive_space, measured once after
    its walk); a kept root keeps the space recorded before.
    """
    roots = [os.path.abspath(r) for r in roots]
    by_dev: dict[int, list[str]] = {}
    for r in roots:
        by_dev.setdefault(os.stat(r).st_dev, []).append(r)
    for r in roots:
        index.begin_root(r)

    out: queue.Queue = queue.Queue(maxsize=64)
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=_device_worker,
            args=(dev_roots, all_files, out, stop),
            name=f"dpf-scan-{dev}",
            daemon=True,
        )
        for dev, dev_roots in by_dev.items()
    ]
    counts = {r: [0, 0] for r in roots}
    reported = {r: 0.0 for r in roots}
    stats: dict[str, RootStats] = {}
    try:
        for t in threads:
            t.start()
        running = len(threads)
        while running:
            kind, root, payload = out.get()
            if kind == "batch":
                index.add_records(payload)
                c = counts[root]
                c[0] += len(payload)
                c[1] += sum(r.size for r in payload)
                now = time.monotonic()
                if progress and now - reported[root] >= PROGRESS_SECONDS:
                    reported[root] = now
                    progress(root, c[0], c[1])
            elif kind == "end":
                root_errors, dir_stats, empty, space = payload
                previous = index.totals([root])[0] if empty and not allow_empty else 0
                if previous:
                    index.discard_staged(root)
                    if kept is not None:
                        kept[root] = previous
                    continue
                stats[root] = index.finish_root(root, len(root_errors), dir_stats, space)
                if errors is not None:
                    errors.extend(root_errors)
                if progress:
                    progress(root, *counts[root])
            elif kind == "done":
                running -= 1
            else:
                raise payload
    finally:
        stop.set()
    return [stats[r] for r in roots if r in stats]
