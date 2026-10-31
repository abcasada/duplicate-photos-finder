"""EXIF for mode 1b: read camera identity tags with exiftool and cache them in the index.

What is read, per photo (exiftool tag names, `-n` values):
- Make, Model; the body serial: Sony `InternalSerialNumber` (MakerNotes; with -n six
  decimal bytes, kept as hex), any other make `SerialNumber` (Pentax MakerNotes or EXIF
  BodySerialNumber). Sony's own `SerialNumber` is never used, so the two cannot mix.
- ShutterCount, DateTimeOriginal (+ OffsetTimeOriginal, SubSecTimeOriginal), ImageWidth
  and ImageHeight (exiftool prefers the full-resolution image of a RAW).
- The shutter mode: Sony `Shutter` ("0 0 0" = silent/electronic), Pentax `ShutterType`
  (1 = electronic) and `LiveView` (1 = on). The shutter count does not advance for those
  (Sony: silent shooting; Pentax: live view), so a burst shares one count: such files
  get no identity.
Videos are not read: they have no shot identity and follow the mode 1 rules.

A file's shot identity (`ident`) is (make, model, serial, shutter count, capture time),
and its `shot` key adds the format family (ARW, DNG, JPG, …) and the pixel size, so RAW+
JPG of one shot and a resized export are never the same shot. A non-RAW file without a
pixel size has no shot key.

Speed (docs/PLAN.md, "EXIF research"): per physical device, READERS threads read the
first HEAD_BYTES of each file in folder order and copy them to a temp dir on the system
disk (/dev/shm when available); a pool of `exiftool -stay_open` processes parses those
copies. A TIFF-based file (DNG, ARW, PEF, NEF, CR2, TIF, …) may keep its directories
past the head (Lightroom re-saved DNGs put IFD0 at the end): its IFDs and the tag data
dpf needs are found by following the TIFF offsets (walk_tiff) and read into a sparse copy
of the file's true size, so the offsets stay right and the holes read as zeros. When a
copy still lacks tags its format should carry, a BIG_HEAD copy is parsed, then (for
missing tags, not just a missing pixel size) the whole file up to MAX_WHOLE bytes.

The drives are only read, and their access times never change: dpf itself opens every
file read-only with O_NOATIME (a file where the kernel refuses it is skipped and counted,
unless allowed), and exiftool only ever sees the temp copies, never an original.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
import queue
import re
import select
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace

from dpf.index import Index

HEAD_BYTES = 512 * 1024
BIG_HEAD = 16 * 1024 * 1024
MAX_WHOLE = 256 * 1024 * 1024
WINDOW = 64 * 1024  # read around a TIFF structure that lies past the head
TAIL = 1024 * 1024  # read at once when IFD0 lies in the file's last TAIL bytes
SPARSE_BUDGET = 8 * 1024 * 1024  # most bytes read past the head for one copy
MAX_IFDS = 64
READERS = 4  # reader threads per physical device
BATCH = 16  # files per exiftool command
TIMEOUT = 60.0  # seconds per exiftool command before the process is killed
COMMIT_ROWS = 500
COMMIT_SECONDS = 1.0
PROGRESS_SECONDS = 0.5
STALE_TEMP = 86_400  # seconds after which a temp dir without a known owner is removed

OK = "ok"  # a shot key was found
NONE = "none"  # read, but no shot key (phone, GoPro, edited export, silent shutter, …)
ERROR = "error"  # exiftool could not parse it
MECHANICAL = "mechanical"
ELECTRONIC = "electronic"  # silent/electronic shutter or live view: count unreliable
TOO_LARGE = "too large to read in full"

TAGS = (
    "Make", "Model", "SerialNumber", "InternalSerialNumber", "ShutterCount",
    "DateTimeOriginal", "OffsetTimeOriginal", "SubSecTimeOriginal", "ImageWidth",
    "ImageHeight", "Shutter", "ShutterType", "LiveView", "Error", "Warning",
)  # fmt: skip
ARGS = ("-json", "-n", "-fast", "-charset", "filename=utf8", *(f"-{t}" for t in TAGS))

RAW_FORMATS = frozenset(
    {
        "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf", "orf",
        "rw2", "pef", "srw", "x3f", "3fr", "iiq", "mrw", "erf", "kdc", "dcr", "mos",
    }
)  # fmt: skip
_FAMILIES = {"jpeg": "jpg", "jpe": "jpg", "tiff": "tif", "heif": "heic"}
# Formats whose metadata always precedes the image data: the head holds every tag.
_HEAD_ENOUGH = frozenset({"jpg", "png", "gif", "bmp"})
# Makes whose bodies record a serial and shutter count: a head without them is re-read.
_COUNTER_MAKES = ("sony", "pentax", "ricoh", "nikon")
_NO_DATE = "0000:00:00 00:00:00"
_BYTES = re.compile(r"\d{1,3}(?: \d{1,3})+")
# Read errors that say nothing about the file (a failing or unplugged drive): not cached.
TRANSIENT_ERRNOS = frozenset(
    {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ETIMEDOUT, errno.ESTALE, errno.ENOTCONN}
)

# TIFF: bytes per value of each field type; IFD pointer tags (SubIFDs, ExifIFD, GPS,
# Interop); tags whose out-of-line data dpf needs (Make, Model, Software, dates and
# their offsets/sub-seconds, BodySerialNumber, MakerNote, DNGPrivateData, UniqueCameraModel).
_TIFF_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_TIFF_MAGIC = frozenset({42, 0x55, 0x4F52, 0x5352})  # TIFF, Panasonic RW2, Olympus ORF
_IFD_POINTERS = frozenset({0x014A, 0x8769, 0x8825, 0xA005})
_DATA_TAGS = frozenset(
    {0x010F, 0x0110, 0x0131, 0x0132, 0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x9290, 0x9291,
     0x9292, 0xA431, 0x927C, 0xC634, 0xC614}
)  # fmt: skip

# Copy steps a file may need (counted per run): a sparse copy with TIFF structures past the
# head, a BIG_HEAD copy, the whole file.
SPARSE, BIG, WHOLE = "sparse", "big", "whole"

# What a read of a head copy still lacks (needs_more).
MORE_TAGS = "tags"  # identity tags: worth a bigger head, then the whole file
MORE_SIZE = "size"  # only the pixel size: worth a bigger head only


class ExifToolMissing(RuntimeError):
    pass


class ExifToolTimeout(OSError):
    pass


class NoAtimeDenied(OSError):
    """The kernel refused O_NOATIME (the file belongs to another user)."""


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


def _date(value: object) -> str | None:
    text = _text(value)
    return None if text is None or text.startswith(_NO_DATE) else text


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def norm_serial(serial: str) -> str:
    """A serial in one canonical form.

    exiftool -n prints byte arrays (Sony InternalSerialNumber) as decimal bytes,
    "161 178 3 0 4 12": those become hex per byte ("a1b20300040c"), keeping every zero.
    A purely numeric serial drops leading zeros (EXIF BodySerialNumber "0123456" is the
    MakerNotes number 123456). Anything else is compared as written, ignoring case and
    with runs of spaces made single.
    """
    text = serial.strip()
    if _BYTES.fullmatch(text) and all(int(b) < 256 for b in text.split()):
        return "".join(f"{int(b):02x}" for b in text.split())
    if text.isdigit():
        return text.lstrip("0") or "0"
    return " ".join(text.split()).casefold()


def _shutter_mode(entry: dict, make: str | None) -> str | None:
    """ELECTRONIC when the shutter count did not advance for this frame, else MECHANICAL
    where the camera says so; None when it does not say."""
    if not make:
        return None
    m = make.casefold()
    if m.startswith("sony"):
        shutter = _text(entry.get("Shutter"))
        if shutter is None:
            return None
        return ELECTRONIC if shutter.split() == ["0", "0", "0"] else MECHANICAL
    if m.startswith(("pentax", "ricoh")):
        kind, live = _int(entry.get("ShutterType")), _int(entry.get("LiveView"))
        if kind == 1 or live == 1:
            return ELECTRONIC
        return MECHANICAL if kind == 0 else None
    return None


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
    shutter_mode: str | None = None  # MECHANICAL, ELECTRONIC or None (not recorded)
    note: str | None = None  # exiftool's error or warning, or TOO_LARGE
    error: bool = False

    @property
    def ident(self) -> str | None:
        """make|model|serial|shutter count|capture time, normalised; None without one.

        None also when the shutter count is unreliable (ELECTRONIC) or the file was too
        large to read in full.
        """
        if self.error or self.shutter_mode == ELECTRONIC or self.note == TOO_LARGE:
            return None
        if not (self.make and self.model and self.serial and self.shutter):
            return None
        serial = norm_serial(self.serial)
        if serial in ("", "0") or self.shutter <= 0:
            return None
        parts = (_norm(self.make), _norm(self.model), serial, str(self.shutter), self.taken or "")
        return "|".join(parts)

    @property
    def shot(self) -> str | None:
        """ident|format family|pixel size: equal only for the same shot in the same format
        and size. A RAW without a pixel size keeps an empty size; other formats need one."""
        ident = self.ident
        if ident is None:
            return None
        size = f"{self.width}x{self.height}" if self.width and self.height else ""
        if not size and self.fmt not in RAW_FORMATS:
            return None
        return f"{ident}|{self.fmt}|{size}"

    @property
    def status(self) -> str:
        if self.error:
            return ERROR
        return OK if self.shot else NONE


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
        shutter_mode=_shutter_mode(entry, make),
        note=note[:200] if note else None,
        error=error is not None,
    )


def needs_more(data: ExifData) -> str | None:
    """What a head copy shorter than the file may have missed: MORE_TAGS, MORE_SIZE or None.

    MORE_SIZE: an identity without a pixel size. MORE_TAGS: no Make or a parse error
    (unless JPEG/PNG/GIF/BMP, whose metadata always comes first), or no identity from a
    make that records serial and shutter count (unless its shutter mode explains it).
    """
    if data.ident and not (data.width and data.height):
        return MORE_SIZE
    if data.fmt in _HEAD_ENOUGH:
        return None
    if data.make is None or data.error:
        return MORE_TAGS
    if data.ident is None and data.shutter_mode != ELECTRONIC:
        return MORE_TAGS if data.make.casefold().startswith(_COUNTER_MAKES) else None
    return None


def row_values(
    dir: str, name: str, size: int, mtime: float, data: ExifData, read_at: float
) -> tuple:
    """Values for Index.put_exif, in index.EXIF_COLUMNS order."""
    return (
        dir, name, size, mtime, data.status, data.make, data.model, data.serial,
        data.shutter, data.taken, data.taken_offset, data.taken_subsec, data.width,
        data.height, data.shutter_mode, data.fmt, data.ident, data.shot, data.note, read_at,
    )  # fmt: skip


def error_data(name: str, message: str) -> ExifData:
    return ExifData(fmt=fmt_of(name), note=message[:200], error=True)


class ExifTool:
    """One `exiftool -stay_open True -@ -` process, fed batches of paths."""

    def __init__(self, exe: str = "exiftool", timeout: float = TIMEOUT) -> None:
        try:
            self.proc = subprocess.Popen(
                [exe, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )  # fmt: skip
        except FileNotFoundError:
            raise ExifToolMissing(exe) from None
        self.timeout = timeout
        self.count = itertools.count(1)

    def read(self, paths: Sequence[str]) -> dict[str, dict]:
        """exiftool's JSON object per path read (paths it could not open are absent).

        Raises ExifToolTimeout (after killing the process) when exiftool takes longer
        than `timeout`, and OSError when it stops.
        """
        n = next(self.count)
        lines = [*ARGS, *paths, f"-execute{n}"]
        data = b"".join(os.fsencode(line) + b"\n" for line in lines)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        marker = f"{{ready{n}}}".encode()
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + self.timeout
        out = b""
        while True:
            end = out.find(marker)
            if end >= 0 and (end == 0 or out[end - 1:end] == b"\n"):
                out = out[:end]
                break
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                self.kill()
                raise ExifToolTimeout(f"exiftool took over {self.timeout:g} s")
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise OSError("exiftool stopped unexpectedly")
            out += chunk
        text = out.decode("utf-8", "replace").strip()
        entries = json.loads(text) if text else []
        return {e.get("SourceFile"): e for e in entries if isinstance(e, dict)}

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait()

    def close(self) -> None:
        try:
            if self.proc.poll() is None and self.proc.stdin is not None:
                self.proc.stdin.write(b"-stay_open\nFalse\n")
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.kill()
        finally:
            for pipe in (self.proc.stdin, self.proc.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass


def exiftool_available(exe: str = "exiftool") -> bool:
    return shutil.which(exe) is not None


@dataclass(slots=True)
class _Job:
    dir: str
    name: str
    size: int  # as found when the file was opened (fstat)
    mtime: float
    head: str | None = None  # temp copy of the first bytes
    whole: bool = False  # the copy holds the whole file
    error: str | None = None  # why it could not be read
    outcome: str = "row"  # "row" (cache it), "gone", "transient" or "noatime"
    steps: list[str] = field(default_factory=list)  # SPARSE, BIG, WHOLE: the copies it took

    @property
    def path(self) -> str:
        return os.path.join(self.dir, self.name)


@dataclass(slots=True)
class ExifRun:
    """What one `dpf exif` run did."""

    todo: int = 0  # photos that needed reading
    read: int = 0  # rows written
    ok: int = 0  # with a shot key
    none: int = 0
    errors: int = 0  # cached as unparseable
    sparse: int = 0  # copies with TIFF structures read from past the head (walk_tiff)
    big: int = 0  # files read again with a BIG_HEAD copy
    whole: int = 0  # files read again in full
    too_large: int = 0  # needed in full but over MAX_WHOLE: cached without identity
    gone: int = 0  # files no longer there (deleted since the scan): not cached
    transient: int = 0  # read errors (I/O, device gone) or exiftool timeouts: not cached
    noatime: int = 0  # skipped: the kernel refused O_NOATIME
    elapsed: float = 0.0
    skipped_roots: list[str] = field(default_factory=list)  # empty-looking mount folders
    stale_temp: int = 0  # leftover temp dirs of earlier runs removed
    interrupted: bool = False

    @property
    def done(self) -> int:
        return self.read + self.gone + self.transient + self.noatime


Progress = Callable[[ExifRun], None]


def _open_read(path: str, allow_atime: bool = False) -> int:
    """A read-only descriptor that never updates the file's access time.

    Raises NoAtimeDenied when the kernel refuses O_NOATIME, unless `allow_atime`.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if not noatime:
        if allow_atime:
            return os.open(path, flags)
        raise NoAtimeDenied(errno.EPERM, "O_NOATIME is not available", path)
    try:
        return os.open(path, flags | noatime)
    except PermissionError as e:
        if allow_atime:
            return os.open(path, flags)
        raise NoAtimeDenied(errno.EPERM, "O_NOATIME refused", path) from e


def _pread(fd: int, n: int, offset: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = os.pread(fd, min(1 << 20, n - got), offset + got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def tiff_ifd0(head: bytes) -> tuple[str, int] | None:
    """(struct byte order, IFD0 offset) from a TIFF-based file's 8-byte header, else None."""
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        return None
    order = "<" if head[:2] == b"II" else ">"
    magic, ifd0 = struct.unpack(order + "HI", head[2:8])
    return (order, ifd0) if magic in _TIFF_MAGIC else None


def walk_tiff(read: Callable[[int, int], bytes], size: int) -> int:
    """Follow the IFDs of a TIFF-based file of `size` bytes, reading through `read(offset,
    n)` every IFD (IFD0 and the next ones, SubIFDs, ExifIFD, GPS, Interop) and the
    out-of-line data of the tags dpf needs (_DATA_TAGS: MakerNote, DNGPrivateData, …).

    Image data is never read. Returns the number of IFDs found (0: not TIFF).
    """
    header = tiff_ifd0(read(0, 8))
    if header is None:
        return 0
    order, ifd0 = header
    todo, seen = [ifd0], set()
    while todo and len(seen) < MAX_IFDS:
        off = todo.pop(0)
        if off in seen or off < 8 or off + 2 > size:
            continue
        seen.add(off)
        raw = read(off, 2)
        if len(raw) < 2:
            continue
        (n,) = struct.unpack(order + "H", raw)
        if not 0 < n <= 1024:
            continue
        table = read(off + 2, 12 * n + 4)
        if len(table) < 12 * n + 4:
            continue
        for i in range(n):
            tag, kind, count = struct.unpack(order + "HHI", table[12 * i:12 * i + 8])
            value = table[12 * i + 8:12 * i + 12]
            nbytes = _TIFF_TYPES.get(kind, 0) * count
            if not nbytes:
                continue
            (pointer,) = struct.unpack(order + "I", value)
            if tag in _IFD_POINTERS:
                if nbytes <= 4:
                    todo.append(pointer)
                elif count <= MAX_IFDS and kind in (4, 13):
                    block = read(pointer, nbytes)
                    whole = len(block) // 4
                    todo += struct.unpack(f"{order}{whole}I", block[: whole * 4])
            elif tag in _DATA_TAGS and 4 < nbytes <= SPARSE_BUDGET:
                read(pointer, nbytes)
        (nxt,) = struct.unpack(order + "I", table[12 * n:12 * n + 4])
        if nxt:
            todo.append(nxt)
    return len(seen)


class _Reader:
    """Serves reads from the head, or with pread in WINDOW-sized chunks it keeps (up to
    SPARSE_BUDGET bytes past the head in all)."""

    def __init__(self, fd: int, head: bytes, size: int) -> None:
        self.fd, self.head, self.size = fd, head, size
        self.chunks: dict[int, bytes] = {}
        self.extra = 0

    def read(self, offset: int, n: int) -> bytes:
        if offset < 0 or offset >= self.size:
            return b""
        n = min(n, self.size - offset)
        if offset + n <= len(self.head):
            return self.head[offset:offset + n]
        for start, data in self.chunks.items():
            if start <= offset and offset + n <= start + len(data):
                return data[offset - start:offset - start + n]
        want = min(max(n, WINDOW), self.size - offset)
        if self.extra + want > SPARSE_BUDGET:
            want = n
            if self.extra + want > SPARSE_BUDGET:
                return b""
        data = _pread(self.fd, want, offset)
        self.chunks[offset] = data
        self.extra += len(data)
        return data[:n]


def copy_head(
    path: str, dest: str, limit: int, allow_atime: bool = False
) -> tuple[os.stat_result, int, int]:
    """Copy `path` to `dest` for exiftool: (stat of the open file, bytes read, of which
    past the first `limit`).

    `dest` gets the first `limit` bytes. For a larger TIFF-based file it also gets the
    TIFF structures that lie further on (walk_tiff; when IFD0 sits in the last TAIL bytes,
    those are read in one go): then `dest` is a sparse file of the original's size, so
    exiftool finds everything at its offset and reads zeros in the holes.
    """
    fd = _open_read(path, allow_atime)
    try:
        st = os.fstat(fd)
        head = _pread(fd, limit, 0)
        reader = _Reader(fd, head, st.st_size)
        header = tiff_ifd0(head)
        if header is not None and len(head) < st.st_size:
            ifd0 = header[1]
            if ifd0 >= len(head) and ifd0 >= st.st_size - TAIL:
                reader.read(max(len(head), st.st_size - TAIL), TAIL)
            walk_tiff(reader.read, st.st_size)
    finally:
        os.close(fd)
    with open(dest, "wb") as out:
        out.write(head)
        for start, data in sorted(reader.chunks.items()):
            out.seek(start)
            out.write(data)
        if reader.chunks:
            out.truncate(st.st_size)
    return st, len(head) + reader.extra, reader.extra


def _temp_base(db_path: str) -> str:
    """Where head copies go: /dev/shm (RAM) when usable, else beside the index."""
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK | os.X_OK):
        return shm
    base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else None
    return base or tempfile.gettempdir()


_TEMP_PREFIX = "dpf-exif-"
_TEMP_PID = re.compile(rf"{_TEMP_PREFIX}(\d+)-")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clean_stale_temp(base: str, now: float | None = None) -> int:
    """Remove our leftover temp dirs in `base` from runs that died; returns how many.

    A dir named after a process that no longer exists goes; one without a process id
    only once it is over STALE_TEMP old. Only dirs owned by this user are touched.
    """
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(base))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_TEMP_PREFIX):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if not entry.is_dir(follow_symlinks=False) or st.st_uid != os.getuid():
                continue
        except OSError:
            continue
        m = _TEMP_PID.match(entry.name)
        if m and int(m.group(1)) != os.getpid():
            stale = not _alive(int(m.group(1)))
        else:
            stale = not m and now - st.st_mtime > STALE_TEMP
        if stale:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed


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
    big_head: int = BIG_HEAD,
    max_whole: int = MAX_WHOLE,
    allow_atime: bool = False,
    timeout: float = TIMEOUT,
) -> ExifRun:
    """Read EXIF for the indexed photos under `roots` lacking a valid cached row.

    `roots` (drives or folders inside them) must be connected; the file list comes from
    the index, so only files of a previous scan are read. Rows are committed in batches
    by the calling thread, so an interrupted run keeps its progress (a KeyboardInterrupt
    is re-raised after the commit).
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
    base = _temp_base(index.path)
    run.stale_temp = clean_stale_temp(base)
    tmp = tempfile.mkdtemp(prefix=f"{_TEMP_PREFIX}{os.getpid()}-", dir=base)
    stop = threading.Event()
    heads: queue.Queue = queue.Queue(maxsize=jobs * BATCH * 2)
    results: queue.Queue = queue.Queue()
    seq = itertools.count()
    reader_opts = _Reading(tmp, seq, head_bytes, big_head, max_whole, allow_atime, exe, timeout)

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
                dir, name, size, mtime, _ = files[cursor[0]]
                cursor[0] += 1
            job = _Job(dir, name, size, mtime)
            reader_opts.copy(job, head_bytes)
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
                    tool = ExifTool(exe, timeout)
                tool = reader_opts.parse_batch(tool, batch, results)
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
                _tally(run, pending, *payload)
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
            t.join(timeout=TIMEOUT + 10)
        shutil.rmtree(tmp, ignore_errors=True)
        run.elapsed = time.monotonic() - started
    return run


def _tally(run: ExifRun, pending: list[tuple], job: _Job, data: ExifData) -> None:
    """Count one result, and queue its row when it is to be cached."""
    run.sparse += SPARSE in job.steps
    run.big += BIG in job.steps
    run.whole += WHOLE in job.steps
    if job.outcome == "gone":
        run.gone += 1
        return
    if job.outcome == "transient":
        run.transient += 1
        return
    if job.outcome == "noatime":
        run.noatime += 1
        return
    pending.append(row_values(job.dir, job.name, job.size, job.mtime, data, time.time()))
    run.read += 1
    run.too_large += data.note == TOO_LARGE
    if data.status == OK:
        run.ok += 1
    elif data.status == NONE:
        run.none += 1
    else:
        run.errors += 1


@dataclass(slots=True)
class _Reading:
    """How files are copied and parsed in one run (shared by its threads)."""

    tmp: str
    seq: Iterator[int]
    head_bytes: int
    big_head: int
    max_whole: int
    allow_atime: bool
    exe: str
    timeout: float

    def copy(self, job: _Job, limit: int) -> None:
        """Copy the first `limit` bytes of `job`'s file to a new temp file (job.head)."""
        ext = "".join(c for c in fmt_of(job.name) if c.isalnum())
        dest = os.path.join(self.tmp, f"{next(self.seq)}.{ext or 'bin'}")
        try:
            st, got, extra = copy_head(job.path, dest, limit, self.allow_atime)
        except NoAtimeDenied:
            job.outcome, job.error = "noatime", "O_NOATIME refused"
            return
        except (FileNotFoundError, NotADirectoryError):
            job.outcome, job.error = "gone", "gone"
            return
        except OSError as e:
            transient = e.errno in TRANSIENT_ERRNOS
            job.outcome = "transient" if transient else "row"
            job.error = e.strerror or str(e)
            _remove(dest)
            return
        job.size, job.mtime = st.st_size, st.st_mtime
        job.whole = not extra and got >= st.st_size  # a sparse copy is never the whole file
        job.head = dest
        if extra and not job.steps:
            job.steps.append(SPARSE)

    def parse_batch(self, tool: ExifTool, batch: list[_Job], results: queue.Queue) -> ExifTool:
        """Parse one batch of head copies, reading bigger copies where needed; returns the
        tool to use next (a fresh one if this one failed or timed out)."""
        readable = [j for j in batch if j.error is None and j.head is not None]
        for j in batch:
            if j.error is not None:
                results.put(("row", (j, error_data(j.name, j.error))))
        found, tool, lost = self._run(tool, [j.head for j in readable])
        for j in readable:
            head = j.head
            if head in lost:
                j.outcome = "transient"
                results.put(("row", (j, error_data(j.name, "exiftool failed"))))
                _remove(head)
                continue
            entry = found.get(head)
            data = parse_entry(entry, j.name) if entry else error_data(j.name, "not read")
            _remove(head)
            data, tool = self._more(tool, j, data)
            results.put(("row", (j, data)))
        return tool

    def _more(self, tool: ExifTool, job: _Job, data: ExifData) -> tuple[ExifData, ExifTool]:
        """Read a bigger head, then the whole file, while needs_more asks for it."""
        steps = ((BIG, self.big_head, (MORE_TAGS, MORE_SIZE)), (WHOLE, None, (MORE_TAGS,)))
        for step, limit, wanted in steps:
            want = needs_more(data)
            if job.whole or want not in wanted:
                break
            if limit is None:
                if job.size > self.max_whole:
                    return replace(data, note=TOO_LARGE), tool
                limit = job.size
            job.steps.append(step)
            job.head = None
            self.copy(job, limit)
            if job.head is None:  # unreadable now: keep the outcome the copy decided
                return error_data(job.name, job.error or "unreadable"), tool
            found, tool, lost = self._run(tool, [job.head])
            entry = found.get(job.head)
            _remove(job.head)
            if job.head in lost:
                job.outcome = "transient"
                return error_data(job.name, "exiftool failed"), tool
            if entry:
                data = parse_entry(entry, job.name)
        return data, tool

    def _run(
        self, tool: ExifTool, paths: list[str]
    ) -> tuple[dict[str, dict], ExifTool, set[str]]:
        """(entries, tool, paths lost). When exiftool times out, stops or prints garbage,
        it is restarted and the batch retried one file at a time, so only a file that
        fails on its own is lost (not cached: a rerun tries it again)."""
        if not paths:
            return {}, tool, set()
        try:
            return tool.read(paths), tool, set()
        except (OSError, ValueError):
            tool.close()
            tool = ExifTool(self.exe, self.timeout)
            if len(paths) == 1:
                return {}, tool, set(paths)
        found: dict[str, dict] = {}
        lost: set[str] = set()
        for p in paths:
            more, tool, gone = self._run(tool, [p])
            found.update(more)
            lost |= gone
        return found, tool, lost


def _remove(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
