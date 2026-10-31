"""EXIF for mode 1b: read camera identity tags with exiftool and cache them in the index.

What is read, per photo/video (exiftool tag names, `-n` values):
- Make, Model; the body serial: Sony `InternalSerialNumber` (MakerNotes), any other make
  `SerialNumber` (Pentax MakerNotes or EXIF BodySerialNumber). Sony's own `SerialNumber`
  is never used, so the two cannot mix.
- ShutterCount, DateTimeOriginal (+ OffsetTimeOriginal, SubSecTimeOriginal), ImageWidth
  and ImageHeight; for video QuickTime CreateDate (UTC) and Duration.

A file's shot identity (`ident`) is (make, model, serial, shutter count, capture time),
and its `shot` key adds the format family (ARW, DNG, JPG, …) and, for non-RAW formats,
the pixel size, so RAW+JPG of one shot and a resized export are never the same shot.
Files without make, model, serial and a positive shutter count have no identity.

Speed (docs/PLAN.md, "EXIF research"): per physical device, READERS threads read the
first HEAD_BYTES of each file in folder order and copy them to a temp dir on the system
disk (/dev/shm when available); a pool of `exiftool -stay_open` processes parses those
copies. A file whose head lacks a tag its format should carry is read again by exiftool
from the original path. The drives are only read: files are opened read-only (with
O_NOATIME where allowed), and exiftool gets no write options.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from dpf import media
from dpf.index import Index

HEAD_BYTES = 512 * 1024
READERS = 4  # reader threads per physical device
BATCH = 16  # files per exiftool command
COMMIT_ROWS = 500
COMMIT_SECONDS = 1.0
PROGRESS_SECONDS = 0.5

OK = "ok"  # a shot identity was found
NONE = "none"  # read, but no shot identity (phone, GoPro, video, edited export, …)
ERROR = "error"  # unreadable, or exiftool could not parse it

TAGS = (
    "Make", "Model", "SerialNumber", "InternalSerialNumber", "ShutterCount",
    "DateTimeOriginal", "OffsetTimeOriginal", "SubSecTimeOriginal", "ImageWidth",
    "ImageHeight", "QuickTime:CreateDate", "Duration", "Error", "Warning",
)  # fmt: skip
ARGS = ("-json", "-n", "-fast", "-charset", "filename=utf8", *(f"-{t}" for t in TAGS))

RAW_FORMATS = frozenset(
    {
        "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf", "orf",
        "rw2", "pef", "srw", "x3f", "3fr", "iiq", "mrw", "erf", "kdc", "dcr", "mos",
    }
)  # fmt: skip
_FAMILIES = {"jpeg": "jpg", "jpe": "jpg", "tiff": "tif", "heif": "heic"}
# Formats whose metadata always precedes the image data: the head is enough.
_HEAD_ENOUGH = frozenset({"jpg", "png", "gif", "bmp"})
# Makes whose bodies record a serial and shutter count: a head without them is re-read.
_COUNTER_MAKES = ("sony", "pentax", "ricoh", "nikon")
_NO_DATE = "0000:00:00 00:00:00"


class ExifToolMissing(RuntimeError):
    pass


def fmt_of(name: str) -> str:
    """The format family of a file name: its lowercase extension, with aliases merged."""
    ext = os.path.splitext(name)[1][1:].lower()
    return _FAMILIES.get(ext, ext)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: object) -> str | None:
    text = _text(value)
    return None if text is None or text.startswith(_NO_DATE) else text


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _norm_serial(serial: str) -> str:
    """Serials compare without spaces, case or leading zeros ("0012ab" == "12AB")."""
    return "".join(serial.split()).casefold().lstrip("0")


@dataclass(frozen=True, slots=True)
class ExifData:
    """The tags dpf keeps for one file (None: not present)."""

    fmt: str
    make: str | None = None
    model: str | None = None
    serial: str | None = None
    shutter: int | None = None
    taken: str | None = None  # DateTimeOriginal, "YYYY:MM:DD HH:MM:SS"
    taken_offset: str | None = None
    taken_subsec: str | None = None
    width: int | None = None
    height: int | None = None
    created: str | None = None  # QuickTime CreateDate (UTC)
    duration: float | None = None
    note: str | None = None  # exiftool's error or warning
    error: bool = False

    @property
    def ident(self) -> str | None:
        """make|model|serial|shutter count|capture time, normalised; None without one."""
        if self.error or not (self.make and self.model and self.serial and self.shutter):
            return None
        serial = _norm_serial(self.serial)
        if not serial or self.shutter <= 0:
            return None
        parts = (_norm(self.make), _norm(self.model), serial, str(self.shutter), self.taken or "")
        return "|".join(parts)

    @property
    def shot(self) -> str | None:
        """ident|format family|pixel size (RAW: no size); equal only for the same shot
        in the same format."""
        ident = self.ident
        if ident is None:
            return None
        size = "" if self.fmt in RAW_FORMATS else f"{self.width or ''}x{self.height or ''}"
        return f"{ident}|{self.fmt}|{size}"

    @property
    def status(self) -> str:
        if self.error:
            return ERROR
        return OK if self.ident else NONE


def parse_entry(entry: dict, name: str) -> ExifData:
    """ExifData from one object of `exiftool -json -n` output for the file `name`."""
    make = _text(entry.get("Make"))
    is_sony = bool(make) and make.casefold().startswith("sony")
    serial = entry.get("InternalSerialNumber" if is_sony else "SerialNumber")
    shutter = _int(entry.get("ShutterCount"))
    error = _text(entry.get("Error"))
    note = error or _text(entry.get("Warning"))
    return ExifData(
        fmt=fmt_of(name),
        make=make,
        model=_text(entry.get("Model")),
        serial=_text(serial),
        shutter=shutter if shutter and shutter > 0 else None,
        taken=_date(entry.get("DateTimeOriginal")),
        taken_offset=_text(entry.get("OffsetTimeOriginal")),
        taken_subsec=_text(entry.get("SubSecTimeOriginal")),
        width=_int(entry.get("ImageWidth")),
        height=_int(entry.get("ImageHeight")),
        created=_date(entry.get("CreateDate")),
        duration=_float(entry.get("Duration")),
        note=note[:200] if note else None,
        error=error is not None,
    )


def needs_full_read(data: ExifData, kind: str) -> bool:
    """True if `data`, read from a head copy shorter than the file, may lack tags the
    full file holds.

    A shot identity without a pixel size (a non-RAW format whose image data starts past
    the head) is re-read. Otherwise never for formats whose metadata always comes first
    (JPEG, PNG, GIF, BMP). Videos need CreateDate and Duration (an MP4 may keep its
    `moov` atom at the end). Other images are re-read when no Make was found, or when a
    make that records serial and shutter count yielded no identity.
    """
    if data.ident and data.fmt not in RAW_FORMATS and not (data.width and data.height):
        return True
    if data.fmt in _HEAD_ENOUGH:
        return False
    if kind == media.VIDEO:
        return data.created is None or data.duration is None
    if data.make is None or data.error:
        return True
    return data.ident is None and data.make.casefold().startswith(_COUNTER_MAKES)


def row_values(
    dir: str, name: str, size: int, mtime: float, data: ExifData, read_at: float
) -> tuple:
    """Values for Index.put_exif, in index.EXIF_COLUMNS order."""
    return (
        dir, name, size, mtime, data.status, data.make, data.model, data.serial,
        data.shutter, data.taken, data.taken_offset, data.taken_subsec, data.width,
        data.height, data.created, data.duration, data.fmt, data.ident, data.shot,
        data.note, read_at,
    )  # fmt: skip


def error_data(name: str, message: str) -> ExifData:
    return ExifData(fmt=fmt_of(name), note=message[:200], error=True)


class ExifTool:
    """One `exiftool -stay_open True -@ -` process, fed batches of paths."""

    def __init__(self, exe: str = "exiftool") -> None:
        try:
            self.proc = subprocess.Popen(
                [exe, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )  # fmt: skip
        except FileNotFoundError:
            raise ExifToolMissing(exe) from None
        self.count = itertools.count(1)

    def read(self, paths: Sequence[str]) -> dict[str, dict]:
        """exiftool's JSON object per path read (paths it could not open are absent)."""
        n = next(self.count)
        lines = [*ARGS, *paths, f"-execute{n}"]
        data = b"".join(os.fsencode(line) + b"\n" for line in lines)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        marker = f"{{ready{n}}}".encode()
        out: list[bytes] = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise OSError("exiftool stopped unexpectedly")
            if line.rstrip(b"\r\n") == marker:
                break
            out.append(line)
        text = b"".join(out).decode("utf-8", "replace").strip()
        entries = json.loads(text) if text else []
        return {e.get("SourceFile"): e for e in entries if isinstance(e, dict)}

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write(b"-stay_open\nFalse\n")
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.proc.kill()
            self.proc.wait()
        finally:
            if self.proc.stdout is not None:
                self.proc.stdout.close()


def exiftool_available(exe: str = "exiftool") -> bool:
    return shutil.which(exe) is not None


@dataclass(slots=True)
class _Job:
    dir: str
    name: str
    kind: str
    size: int  # as found when the file was opened (fstat)
    mtime: float
    head: str | None = None  # temp copy of the first HEAD_BYTES
    whole: bool = False  # the head holds the whole file
    error: str | None = None

    @property
    def path(self) -> str:
        return os.path.join(self.dir, self.name)


@dataclass(slots=True)
class ExifRun:
    """What one `dpf exif` run did."""

    todo: int = 0  # files that needed reading
    read: int = 0  # rows written
    ok: int = 0  # with a shot identity
    none: int = 0
    errors: int = 0
    full_reads: int = 0  # files re-read from the original after the head
    gone: int = 0  # files no longer there (deleted since the scan)
    elapsed: float = 0.0
    skipped_roots: list[str] = field(default_factory=list)  # empty-looking mount folders
    interrupted: bool = False


Progress = Callable[[ExifRun], None]


def _open_read(path: str) -> int:
    """A read-only descriptor, without atime updates where the kernel allows it."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if noatime:
        try:
            return os.open(path, flags | noatime)
        except PermissionError:
            pass
    return os.open(path, flags)


def _read_head(fd: int, limit: int) -> bytes:
    chunks, got = [], 0
    while got < limit:
        chunk = os.read(fd, limit - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _temp_base(db_path: str) -> str:
    """Where head copies go: /dev/shm (RAM) when usable, else beside the index."""
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK | os.X_OK):
        return shm
    base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else None
    return base or tempfile.gettempdir()


def read_exif(
    index: Index,
    roots: Sequence[str],
    *,
    jobs: int | None = None,
    readers: int = READERS,
    retry_errors: bool = False,
    progress: Progress | None = None,
    exe: str = "exiftool",
    head_bytes: int = HEAD_BYTES,
) -> ExifRun:
    """Read EXIF for the indexed media under `roots` lacking a valid cached row.

    `roots` must be connected; the file list comes from the index, so only files of a
    previous scan are read. Rows are committed in batches by the calling thread, so an
    interrupted run keeps its progress (a KeyboardInterrupt is re-raised after the commit).
    """
    if not exiftool_available(exe):
        raise ExifToolMissing(exe)
    from dpf.scanner import _is_unusable_root

    run = ExifRun()
    started = time.monotonic()
    by_dev: dict[int, list[tuple]] = {}
    for root in roots:
        if _is_unusable_root(root):
            run.skipped_roots.append(root)
            continue
        todo = index.exif_todo([root], retry_errors=retry_errors)
        by_dev.setdefault(os.stat(root).st_dev, []).extend(todo)
    run.todo = sum(len(t) for t in by_dev.values())
    if not run.todo:
        run.elapsed = time.monotonic() - started
        return run

    jobs = max(1, jobs or min(6, os.cpu_count() or 1))
    tmp = tempfile.mkdtemp(prefix="dpf-exif-", dir=_temp_base(index.path))
    stop = threading.Event()
    heads: queue.Queue = queue.Queue(maxsize=jobs * BATCH * 2)
    results: queue.Queue = queue.Queue()
    seq = itertools.count()

    def put(q: queue.Queue, item: object) -> bool:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                pass
        return False

    def reader(files: list[tuple], lock: threading.Lock, cursor: list[int]) -> None:
        while not stop.is_set():
            with lock:
                if cursor[0] >= len(files):
                    return
                dir, name, size, mtime, kind = files[cursor[0]]
                cursor[0] += 1
            job = _Job(dir, name, kind, size, mtime)
            try:
                fd = _open_read(job.path)
                try:
                    st = os.fstat(fd)
                    data = _read_head(fd, head_bytes)
                finally:
                    os.close(fd)
                job.size, job.mtime = st.st_size, st.st_mtime
                job.whole = len(data) >= st.st_size
                ext = "".join(c for c in fmt_of(name) if c.isalnum())
                job.head = os.path.join(tmp, f"{next(seq)}.{ext or 'bin'}")
                with open(job.head, "wb") as fh:
                    fh.write(data)
            except FileNotFoundError:
                job.error = "gone"
            except OSError as e:
                job.error = e.strerror or str(e)
            if not put(heads, job):
                return

    def parser() -> None:
        tool: ExifTool | None = None
        try:
            while True:
                job = heads.get()
                if job is None:
                    return
                batch = [job]
                while len(batch) < BATCH:
                    try:
                        more = heads.get_nowait()
                    except queue.Empty:
                        break
                    if more is None:
                        heads.put(None)
                        break
                    batch.append(more)
                if tool is None:
                    tool = ExifTool(exe)
                tool = _parse_batch(tool, batch, exe, results)
        except BaseException as e:  # report, then let the writer stop the run
            results.put(("fail", e))
        finally:
            if tool is not None:
                tool.close()
            results.put(("done", None))

    threads: list[threading.Thread] = []
    for dev, files in by_dev.items():
        lock, cursor = threading.Lock(), [0]
        for i in range(max(1, readers)):
            threads.append(threading.Thread(
                target=reader, args=(files, lock, cursor), name=f"dpf-exif-read-{dev}-{i}",
                daemon=True,
            ))  # fmt: skip
    parsers = [
        threading.Thread(target=parser, name=f"dpf-exif-parse-{i}", daemon=True)
        for i in range(jobs)
    ]
    pending: list[tuple] = []
    last_commit = last_progress = time.monotonic()

    def commit() -> None:
        nonlocal last_commit
        if pending:
            index.put_exif(pending)
            pending.clear()
        last_commit = time.monotonic()

    try:
        for t in threads + parsers:
            t.start()
        closed = False
        running = len(parsers)
        while running:
            if not closed and not any(t.is_alive() for t in threads):
                for _ in parsers:
                    heads.put(None)
                closed = True
            try:
                kind, payload = results.get(timeout=0.2)
            except queue.Empty:
                kind, payload = "idle", None
            if kind == "done":
                running -= 1
            elif kind == "fail":
                raise payload
            elif kind == "row":
                job, data, full = payload
                if job.error == "gone":
                    run.gone += 1
                else:
                    pending.append(
                        row_values(job.dir, job.name, job.size, job.mtime, data, time.time())
                    )
                    run.read += 1
                    run.full_reads += full
                    if data.status == OK:
                        run.ok += 1
                    elif data.status == NONE:
                        run.none += 1
                    else:
                        run.errors += 1
            now = time.monotonic()
            if len(pending) >= COMMIT_ROWS or now - last_commit >= COMMIT_SECONDS:
                commit()
            if progress and now - last_progress >= PROGRESS_SECONDS:
                last_progress = now
                run.elapsed = now - started
                progress(run)
        commit()
    except KeyboardInterrupt:
        run.interrupted = True
        commit()
        raise
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
        while True:  # unparsed heads are dropped; the rerun reads those files again
            try:
                heads.get_nowait()
            except queue.Empty:
                break
        for _ in parsers:
            heads.put(None)
        for t in parsers:
            t.join(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
        run.elapsed = time.monotonic() - started
    return run


def _parse_batch(tool: ExifTool, batch: list[_Job], exe: str, results: queue.Queue) -> ExifTool:
    """Parse one batch of head copies (re-reading originals where needed); returns the
    tool to use next (a fresh one if this one failed)."""
    readable = [j for j in batch if j.error is None and j.head is not None]
    for j in batch:
        if j.error is not None:
            results.put(("row", (j, error_data(j.name, j.error), False)))
    found: dict[str, dict] = {}
    try:
        if readable:
            found = tool.read([j.head for j in readable])
    except (OSError, ValueError) as e:
        tool.close()
        tool = ExifTool(exe)
        for j in readable:
            results.put(("row", (j, error_data(j.name, f"exiftool failed: {e}"), False)))
        _remove_heads(readable)
        return tool
    _remove_heads(readable)
    again: dict[int, _Job] = {}
    parsed: dict[int, ExifData] = {}
    for j in readable:
        entry = found.get(j.head)
        data = parse_entry(entry, j.name) if entry else error_data(j.name, "not read")
        parsed[id(j)] = data
        if not j.whole and needs_full_read(data, j.kind) and "\n" not in j.path:
            again[id(j)] = j
    full: dict[str, dict] = {}
    if again:
        try:
            full = tool.read([j.path for j in again.values()])
        except (OSError, ValueError):
            tool.close()
            tool = ExifTool(exe)
    for j in readable:
        data = parsed[id(j)]
        entry = full.get(j.path) if id(j) in again else None
        if entry:
            data = parse_entry(entry, j.name)
        results.put(("row", (j, data, id(j) in again)))
    return tool


def _remove_heads(jobs: Iterable[_Job]) -> None:
    for j in jobs:
        if j.head:
            try:
                os.remove(j.head)
            except OSError:
                pass
