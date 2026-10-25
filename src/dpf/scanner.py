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
from dpf.models import FileRecord, RootStats

BATCH_SIZE = 5_000
BATCH_SECONDS = 0.5
PROGRESS_SECONDS = 0.5

Progress = Callable[[str, int, int], None]


def normalize_roots(paths: Iterable[str]) -> list[str]:
    """Resolve, validate and dedupe roots, dropping any nested inside another root."""
    resolved = set()
    for p in paths:
        real = os.path.realpath(p)
        if not os.path.isdir(real):
            raise ValueError(f"not an existing directory: {p}")
        resolved.add(real)
    roots: list[str] = []
    for r in sorted(resolved):
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


def walk(
    root: str, *, all_files: bool = False, errors: list[str] | None = None
) -> Iterator[FileRecord]:
    """Yield media files under `root` without following symlinks.

    Unreadable directories and files are reported to `errors` and skipped.
    """
    stack = [os.path.abspath(root)]
    while stack:
        dir = stack.pop()
        subdirs = []
        try:
            with os.scandir(dir) as it:
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if name in media.SKIP_DIRS:
                                continue
                            if not _encodable(name):
                                _error(errors, entry.path, OSError("undecodable name"))
                                continue
                            subdirs.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False) or name.startswith("._"):
                            continue
                        kind = media.classify(name)
                        if kind is None:
                            if not all_files:
                                continue
                            kind = media.OTHER
                        if not _encodable(name):
                            _error(errors, entry.path, OSError("undecodable name"))
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        _error(errors, entry.path, e)
                        continue
                    yield FileRecord(dir, name, st.st_size, st.st_mtime, kind)
        except OSError as e:
            _error(errors, dir, e)
        stack.extend(sorted(subdirs, reverse=True))


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
            batch: list[FileRecord] = []
            sent = time.monotonic()
            for rec in walk(root, all_files=all_files, errors=errors):
                batch.append(rec)
                if len(batch) >= BATCH_SIZE or time.monotonic() - sent >= BATCH_SECONDS:
                    put(("batch", root, batch))
                    batch, sent = [], time.monotonic()
            if batch:
                put(("batch", root, batch))
            put(("end", root, errors))
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
) -> list[RootStats]:
    """Scan `roots` (as from normalize_roots) into `index`, replacing what each held.

    Roots on the same device are walked sequentially by one thread; devices are walked
    in parallel. Error messages are appended to `errors` if given. Returns stats in
    the order of `roots`. A root whose scan is interrupted keeps its previous contents.
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
                stats[root] = index.finish_root(root, len(payload))
                if errors is not None:
                    errors.extend(payload)
                if progress:
                    progress(root, *counts[root])
            elif kind == "done":
                running -= 1
            else:
                raise payload
    finally:
        stop.set()
    return [stats[r] for r in roots]
