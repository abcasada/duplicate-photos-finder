import base64
import errno
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from dpf import exif
from dpf.exif import (
    ELECTRONIC,
    ERROR,
    MECHANICAL,
    MORE_SIZE,
    MORE_TAGS,
    NONE,
    OK,
    TOO_LARGE,
    ExifData,
    ExifTool,
    ExifToolMissing,
    ExifToolTimeout,
    NoAtimeDenied,
    clean_stale_temp,
    copy_head,
    fmt_of,
    needs_more,
    norm_serial,
    parse_entry,
    meta_row,
    read_exif,
    row_values,
    walk_tiff,
)
from dpf.index import _EXIF_TABLE, _VERSION, Index
from dpf.models import FileRecord
from dpf.scanner import scan_roots

HAVE_EXIFTOOL = shutil.which("exiftool") is not None

# An 8x8 JPEG without EXIF (made with ffmpeg).
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjExLjEwMAD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBg"
    "YGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABMAAEBAAAAAAAAAAAAAAAAAAAA"
    "BgEBAQAAAAAAAAAAAAAAAAAABgcQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAIAAgD"
    "ASIAAhEAAxEA/9oADAMBAAIRAxEAPwCLAE1/f//Z"
)

# As `exiftool -json -n` prints them: InternalSerialNumber is six decimal bytes.
SONY = {
    "SourceFile": "/x/DSC01234.ARW", "Make": "SONY", "Model": "ILCE-7RM4A",
    "SerialNumber": "00000000", "InternalSerialNumber": "161 178 3 0 4 12",
    "ShutterCount": 4321, "Shutter": "4 1 5", "DateTimeOriginal": "2023:08:26 17:16:18",
    "OffsetTimeOriginal": "+02:00", "ImageWidth": 9504, "ImageHeight": 6336,
}  # fmt: skip
PENTAX = {
    "SourceFile": "/x/IMGQ0001.DNG", "Make": "RICOH IMAGING COMPANY, LTD.",
    "Model": "PENTAX K-1 Mark II", "SerialNumber": 1234567, "ShutterCount": 99,
    "DateTimeOriginal": "2024:01:02 03:04:05", "ShutterType": 0, "ImageWidth": 7360,
    "ImageHeight": 4912,
}  # fmt: skip


class ParseTest(unittest.TestCase):
    def test_sony_uses_internal_serial_as_hex_bytes(self):
        d = parse_entry(SONY, "DSC01234.ARW")
        self.assertEqual(norm_serial(d.serial), "a1b20300040c")
        self.assertEqual((d.shutter, d.taken, d.taken_offset), (4321, "2023:08:26 17:16:18",
                                                                 "+02:00"))  # fmt: skip
        self.assertEqual((d.status, d.shutter_mode), (OK, MECHANICAL))
        self.assertEqual(d.ident, "sony|ilce-7rm4a|a1b20300040c|4321|2023:08:26 17:16:18")
        self.assertEqual(d.shot, d.ident + "|arw|9504x6336")  # RAW keys carry the size too
        without = parse_entry({**SONY, "InternalSerialNumber": None}, "DSC01234.ARW")
        self.assertIsNone(without.serial)  # never falls back to Sony's SerialNumber
        self.assertEqual(without.status, NONE)

    def test_silent_shutter_gives_no_identity(self):
        silent = parse_entry({**SONY, "Shutter": "0 0 0"}, "DSC01234.ARW")
        self.assertEqual(silent.shutter_mode, ELECTRONIC)
        self.assertIsNone(silent.ident)
        self.assertEqual(silent.status, NONE)
        unknown = parse_entry({**SONY, "Shutter": None}, "DSC01234.ARW")
        self.assertIsNone(unknown.shutter_mode)
        self.assertIsNotNone(unknown.ident)  # the guard covers bodies that do not say

    def test_pentax_electronic_shutter_or_live_view(self):
        self.assertEqual(parse_entry(PENTAX, "x.DNG").shutter_mode, MECHANICAL)
        for change in ({"ShutterType": 1}, {"LiveView": 1}):
            d = parse_entry({**PENTAX, **change}, "x.DNG")
            self.assertEqual(d.shutter_mode, ELECTRONIC)
            self.assertIsNone(d.ident)

    def test_serial_normalisation_is_unambiguous(self):
        self.assertEqual(norm_serial("161 178 3 0 4 12"), "a1b20300040c")
        self.assertNotEqual(norm_serial("16 11 78"), norm_serial("161 178"))
        self.assertEqual(norm_serial("0 1"), "0001")  # byte zeros are kept
        self.assertEqual(norm_serial("0123456"), norm_serial("123456"))  # numeric: no zeros
        self.assertEqual(norm_serial("AB 12"), norm_serial("ab  12"))
        self.assertNotEqual(norm_serial("AB 12"), norm_serial("AB12"))
        self.assertNotEqual(norm_serial("0AB"), norm_serial("AB"))  # zeros kept unless numeric
        self.assertEqual(norm_serial("300 1"), "300 1")  # not bytes: kept as written
        a = parse_entry({**PENTAX, "SerialNumber": "0001234567"}, "x.dng")
        self.assertEqual(a.shot, parse_entry(PENTAX, "y.DNG").shot)
        self.assertIsNone(parse_entry({**PENTAX, "SerialNumber": "0000"}, "x.dng").ident)

    def test_shot_carries_format_family_and_pixel_size(self):
        jpg = parse_entry(SONY, "DSC01234.JPG")
        jpeg = parse_entry(SONY, "x.jpeg")
        small = parse_entry({**SONY, "ImageWidth": 2000, "ImageHeight": 1333}, "y.jpg")
        raw = parse_entry(SONY, "DSC01234.ARW")
        self.assertEqual(jpg.ident, raw.ident)  # RAW+JPG: one shot ...
        self.assertNotEqual(jpg.shot, raw.shot)  # ... in two formats
        self.assertEqual(jpg.shot, jpeg.shot)
        self.assertTrue(jpg.shot.endswith("|jpg|9504x6336"))
        self.assertNotEqual(jpg.shot, small.shot)  # a resized export is not the same shot
        unsized = {**SONY, "ImageWidth": None}
        self.assertIsNone(parse_entry(unsized, "x.jpg").shot)  # non-RAW needs a size
        self.assertTrue(parse_entry(unsized, "x.arw").shot.endswith("|arw|"))

    def test_no_identity(self):
        for entry in (
            {**SONY, "ShutterCount": 0},
            {**SONY, "ShutterCount": None},
            {**SONY, "Model": None},
            {"SourceFile": "x", "Make": "Apple", "Model": "iPhone 12"},
        ):
            d = parse_entry(entry, "x.ARW")
            self.assertIsNone(d.ident)
            self.assertEqual(d.status, NONE)
        self.assertIsNone(replace_note(parse_entry(SONY, "x.ARW"), TOO_LARGE).ident)

    def test_error(self):
        d = parse_entry({**SONY, "Error": "File format error"}, "x.ARW")
        self.assertEqual(d.status, ERROR)
        self.assertIsNone(d.shot)
        self.assertEqual(d.note, "File format error")

    def test_fmt_of(self):
        self.assertEqual([fmt_of(n) for n in ("a.JPEG", "b.jpe", "c.TIFF", "d.heif", "e.ARW")],
                         ["jpg", "jpg", "tif", "heic", "arw"])  # fmt: skip

    def test_needs_more(self):
        self.assertIsNone(needs_more(ExifData(fmt="jpg")))  # JPEG: the head is enough
        self.assertEqual(needs_more(ExifData(fmt="arw")), MORE_TAGS)  # no Make
        self.assertEqual(needs_more(parse_entry({**SONY, "ShutterCount": None}, "x.ARW")),
                         MORE_TAGS)  # fmt: skip
        self.assertIsNone(needs_more(parse_entry(SONY, "x.ARW")))
        self.assertIsNone(needs_more(parse_entry({**SONY, "Shutter": "0 0 0"}, "x.ARW")))
        self.assertIsNone(needs_more(ExifData(fmt="cr2", make="Canon", model="EOS")))
        self.assertEqual(needs_more(parse_entry({**SONY, "ImageWidth": None}, "x.JPG")),
                         MORE_SIZE)  # fmt: skip
        self.assertEqual(needs_more(parse_entry({**SONY, "ImageWidth": None}, "x.ARW")),
                         MORE_SIZE)  # fmt: skip


def replace_note(data: ExifData, note: str) -> ExifData:
    from dataclasses import replace

    return replace(data, note=note)


def rec(dir, name, size=100, mtime=1.0, kind="image"):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind=kind)


def exif_row(f: FileRecord, entry: dict = SONY, size=None, mtime=None):
    data = parse_entry(entry, f.name)
    return row_values(f.dir, f.name, size or f.size, mtime or f.mtime, data, 5.0)


def meta_of(f: FileRecord, entry: dict = SONY, size=None, mtime=None):
    data = parse_entry(entry, f.name)
    return meta_row(f.dir, f.name, size or f.size, mtime or f.mtime, data, 5.0)


class IndexCacheTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def test_todo_skips_valid_rows(self):
        a, b, c = rec("/r", "a.ARW"), rec("/r", "b.ARW"), rec("/r", "c.ARW")
        video = rec("/r", "C0001.MP4", kind="video")
        self.idx.replace_root("/r", [a, b, c, video, rec("/r", "empty.jpg", 0)])
        self.idx.put_exif([exif_row(a), exif_row(b, mtime=2.0)],  # b changed since
                          [meta_of(a), meta_of(b, mtime=2.0)])  # fmt: skip
        todo = [r[1] for r in self.idx.exif_todo(["/r"])]
        self.assertEqual(todo, ["C0001.MP4", "b.ARW", "c.ARW"])  # never 0-byte files
        todo = [r[1] for r in self.idx.exif_todo(["/r"], photos_only=True)]
        self.assertEqual(todo, ["b.ARW", "c.ARW"])
        status = self.idx.exif_status(["/r"])
        self.assertEqual(status, {"ok": 1, "unreliable": 0, "none": 0, "error": 0,
                                  "missing": 2, "video": 1})  # fmt: skip

    def test_retry_errors(self):
        a = rec("/r", "a.ARW")
        self.idx.replace_root("/r", [a])
        self.idx.put_exif([exif_row(a, {"Error": "bad"})], [meta_of(a, {"Error": "bad"})])
        self.assertEqual(self.idx.exif_todo(["/r"]), [])
        self.assertEqual(len(self.idx.exif_todo(["/r"], retry_errors=True)), 1)

    def test_rescan_keeps_exif_forget_drops_it(self):
        a = rec("/r/x", "a.ARW")
        self.idx.replace_root("/r", [a])
        self.idx.put_exif([exif_row(a)])
        self.idx.begin_root("/r")
        self.idx.add_records([a])
        self.idx.finish_root("/r")
        self.assertEqual(self.idx.exif_status(["/r"])["ok"], 1)
        self.idx.forget_root("/r")
        self.assertIsNone(self.idx.exif_row("/r/x", "a.ARW"))

    def test_multi_format_shots(self):
        raw, jpg = rec("/r", "DSC01234.ARW"), rec("/r", "DSC01234.JPG")
        self.idx.replace_root("/r", [raw, jpg])
        self.idx.put_exif([exif_row(raw), exif_row(jpg)])
        self.assertEqual(self.idx.multi_format_shots(["/r"]), 1)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "index.db")

    def test_version_5_gains_exif_table(self):
        with Index(self.path) as idx:
            idx.replace_root("/m", [rec("/m", "x.jpg")])
            idx.conn.execute("DROP TABLE exif")
            idx.conn.execute("PRAGMA user_version = 5")
            idx.conn.commit()
        with Index(self.path) as idx:
            self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
            self.assertEqual(idx.totals(["/m"])[0], 1)
            self.assertEqual(idx.exif_status(["/m"])["missing"], 1)
            indexes = {r[1] for r in idx.conn.execute("PRAGMA index_list(exif)")}
            self.assertIn("exif_shot", indexes)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
        conn.close()

    def test_version_6_cache_of_first_build_is_replaced(self):
        with Index(self.path) as idx:
            idx.replace_root("/m", [rec("/m", "x.jpg")])
            idx.conn.execute("DROP TABLE exif")
            idx.conn.execute(_EXIF_TABLE.replace("shutter_mode TEXT", "created TEXT"))
            idx.conn.execute(
                "INSERT INTO exif (dir, name, read_size, read_mtime, status, read_at) "
                "VALUES ('/m', 'x.jpg', 100, 1.0, 'ok', 0)"
            )
            idx.conn.execute("PRAGMA user_version = 6")
            idx.conn.commit()
        with Index(self.path) as idx:
            self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
            self.assertIsNone(idx.exif_row("/m", "x.jpg"))
            self.assertEqual(idx.totals(["/m"])[0], 1)  # the inventory is untouched
            columns = {r[1] for r in idx.conn.execute("PRAGMA table_info(exif)")}
            self.assertIn("shutter_mode", columns)


class StaleTempTest(unittest.TestCase):
    def test_removes_only_dead_or_old_dirs_of_ours(self):
        with tempfile.TemporaryDirectory() as base:
            dead = subprocess.Popen(["true"])
            dead.wait()
            names = {
                f"dpf-exif-{dead.pid}-a": True,  # its process is gone
                f"dpf-exif-{os.getpid()}-b": False,  # this process
                f"dpf-exif-{os.getppid()}-c": False,  # a live process
                "dpf-exif-old": True,  # no pid, a day old
                "dpf-exif-new": False,
                "something-else": False,
            }
            for name in names:
                os.mkdir(os.path.join(base, name))
            old = time.time() - 2 * exif.STALE_TEMP
            os.utime(os.path.join(base, "dpf-exif-old"), (old, old))
            self.assertEqual(clean_stale_temp(base), 2)
            self.assertEqual(
                sorted(os.listdir(base)), sorted(n for n, gone in names.items() if not gone)
            )


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class ExifToolTest(unittest.TestCase):
    """Runs the real exiftool on tiny JPEGs in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "drive")
        self.dir = os.path.join(self.root, "DCIM")
        os.makedirs(self.dir)
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)

    def jpeg(self, name: str, *tags: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(TINY_JPEG)
        if tags:
            subprocess.run(["exiftool", "-q", "-overwrite_original", *tags, path], check=True)
        return path

    def watch_exiftool(self) -> list[str]:
        """Record every path exiftool is given during the test."""
        seen: list[str] = []
        real = ExifTool.read

        def read(tool, paths, args=exif.ARGS):
            seen.extend(paths)
            return real(tool, paths, args)

        patcher = mock.patch.object(ExifTool, "read", read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_reads_tags_and_caches_them(self):
        self.jpeg(
            "IMGQ0001.JPG", "-Make=PENTAX", "-Model=PENTAX K-1 Mark II",
            "-SerialNumber=1234567", "-DateTimeOriginal=2023:08:26 17:16:18",
        )  # fmt: skip
        self.jpeg("plain.jpg")
        with open(os.path.join(self.dir, "broken.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8 not really a jpeg" * 10)
        with open(os.path.join(self.dir, "clip.mp4"), "wb") as fh:
            fh.write(b"\0" * 64)
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root], jobs=2, readers=2, head_bytes=4096)
        self.assertEqual((run.todo, run.read, run.errors, run.gone), (4, 4, 1, 0))
        self.assertEqual((run.videos, run.video_errors), (1, 1))  # 64 zero bytes
        row = self.idx.exif_row(self.dir, "IMGQ0001.JPG")
        self.assertEqual(
            (row["make"], row["model"], row["serial"], row["taken"], row["width"], row["fmt"]),
            ("PENTAX", "PENTAX K-1 Mark II", "1234567", "2023:08:26 17:16:18", 8, "jpg"),
        )
        self.assertEqual(row["status"], NONE)  # no ShutterCount: no shot identity
        self.assertEqual(self.idx.exif_row(self.dir, "broken.jpg")["status"], ERROR)
        self.assertEqual(self.idx.exif_row(self.dir, "clip.mp4")["status"], ERROR)
        self.assertEqual(read_exif(self.idx, [self.root]).todo, 0)  # cached: nothing to do
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["IMGQ0001.JPG", "broken.jpg", "clip.mp4", "plain.jpg"])  # fmt: skip

    def test_files_only_read_and_exiftool_never_sees_them(self):
        path = self.jpeg("a.jpg", "-Make=SONY")
        os.rename(self.jpeg("b.jpg", "-Make=PENTAX"), os.path.join(self.dir, "b.dng"))
        before = os.stat(path)
        with open(path, "rb") as fh:
            content = fh.read()
        scan_roots(self.idx, [self.root])
        seen = self.watch_exiftool()
        run = read_exif(self.idx, [self.root], head_bytes=16)  # b.dng needs a bigger part
        self.assertEqual((run.big, run.whole), (1, 0))  # 16 MB covers the small file
        self.assertTrue(seen)
        self.assertFalse([p for p in seen if p.startswith(self.root)])  # copies only
        after = os.stat(path)
        self.assertEqual((before.st_size, before.st_mtime_ns, before.st_atime_ns),
                         (after.st_size, after.st_mtime_ns, after.st_atime_ns))  # fmt: skip
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), content)
        self.assertEqual(sorted(os.listdir(self.dir)), ["a.jpg", "b.dng"])

    def test_bigger_part_then_whole_file_then_too_large(self):
        # Not a JPEG/PNG by name, so a head without Make asks for more.
        os.rename(self.jpeg("x.jpg", "-Make=PENTAX"), os.path.join(self.dir, "x.dng"))
        self.jpeg("y.jpg", "-Make=PENTAX")  # JPEG: the head is trusted
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root], head_bytes=16, big_head=32)
        self.assertEqual((run.read, run.big, run.whole, run.too_large), (2, 1, 1, 0))
        self.assertEqual(self.idx.exif_row(self.dir, "x.dng")["make"], "PENTAX")  # whole file
        self.assertIsNone(self.idx.exif_row(self.dir, "y.jpg")["make"])
        self.idx.conn.execute("DELETE FROM exif")
        self.idx.conn.commit()
        run = read_exif(self.idx, [self.root], head_bytes=16, big_head=32, max_whole=64)
        self.assertEqual(run.too_large, 1)
        self.assertEqual(self.idx.exif_row(self.dir, "x.dng")["note"], TOO_LARGE)

    def test_changed_file_read_again_and_gone_file_skipped(self):
        path = self.jpeg("a.jpg")
        gone = self.jpeg("b.jpg")
        scan_roots(self.idx, [self.root])
        os.remove(gone)
        run = read_exif(self.idx, [self.root])
        self.assertEqual((run.read, run.gone), (1, 1))
        self.assertIsNone(self.idx.exif_row(self.dir, "b.jpg"))
        os.utime(path, (5, 5))
        scan_roots(self.idx, [self.root])
        self.assertEqual(read_exif(self.idx, [self.root]).read, 1)
        self.assertEqual(self.idx.exif_row(self.dir, "a.jpg")["read_mtime"], 5.0)

    def test_noatime_refused_skips_file(self):
        self.jpeg("a.jpg")
        scan_roots(self.idx, [self.root])
        refused = NoAtimeDenied(errno.EPERM, "O_NOATIME refused")
        with mock.patch.object(exif, "open_read", side_effect=refused):
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.noatime, run.read), (1, 0))
        self.assertIsNone(self.idx.exif_row(self.dir, "a.jpg"))

    def test_io_errors_are_not_cached(self):
        self.jpeg("a.jpg")
        scan_roots(self.idx, [self.root])
        with mock.patch.object(exif, "copy_head", side_effect=OSError(errno.EIO, "I/O error")):
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.transient, run.read), (1, 0))
        self.assertIsNone(self.idx.exif_row(self.dir, "a.jpg"))

    def test_exiftool_process(self):
        path = self.jpeg("a.jpg", "-Make=SONY")
        tool = ExifTool()
        try:
            found = tool.read([path, os.path.join(self.dir, "missing.jpg")])
            self.assertEqual(found[path]["Make"], "SONY")
            self.assertEqual(len(found), 1)
            self.assertEqual(tool.read([]), {})
        finally:
            tool.close()

    def test_missing_exiftool(self):
        with self.assertRaises(ExifToolMissing):
            read_exif(self.idx, [self.root], exe="no-such-exiftool-binary")


def tiff_at_end(pad: int, order: str = "<", make: str = "PENTAX",
                taken: str = "2023:08:26 17:16:18", xmp: bytes = b"") -> bytes:  # fmt: skip
    """A TIFF whose IFD0, ExifIFD and tag data all follow `pad` zero bytes, as in a DNG
    re-saved by Lightroom (IFD0 at the end of the file); `xmp`: an XMP packet (tag 700)."""
    model = "PENTAX K-1 Mark II"
    ifd0 = 8 + pad
    entries = 6 if xmp else 5
    exif_ifd = ifd0 + 2 + 12 * entries + 4
    data = exif_ifd + 2 + 12 * 1 + 4
    blob = b""

    def text(tag: int, value: str) -> bytes:
        nonlocal blob
        raw = value.encode() + b"\0"
        entry = struct.pack(order + "HHII", tag, 2, len(raw), data + len(blob))
        blob += raw
        return entry

    def short(tag: int, value: int) -> bytes:
        return struct.pack(order + "HHIHH", tag, 3, 1, value, 0)

    first = struct.pack(order + "H", entries) + short(0x0100, 8) + short(0x0101, 8)
    first += text(0x010F, make) + text(0x0110, model)
    if xmp:
        first += struct.pack(order + "HHII", 0x02BC, 1, len(xmp), data + len(blob))
        blob += xmp
    first += struct.pack(order + "HHII", 0x8769, 4, 1, exif_ifd) + struct.pack(order + "I", 0)
    second = struct.pack(order + "H", 1) + text(0x9003, taken) + struct.pack(order + "I", 0)
    head = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, ifd0)
    return head + bytes(pad) + first + second + blob


class TiffWalkTest(unittest.TestCase):
    def reader(self, data: bytes):
        reads = []

        def read(offset: int, n: int) -> bytes:
            reads.append((offset, n))
            return data[offset:offset + n]

        return read, reads

    def test_follows_ifds_and_needed_data(self):
        for order in ("<", ">"):
            data = tiff_at_end(1000, order)
            read, reads = self.reader(data)
            self.assertEqual(walk_tiff(read, len(data)), 2)  # IFD0 and ExifIFD
            covered = {off for off, _ in reads}
            self.assertIn(1008, covered)  # IFD0
            self.assertIn(data.index(b"PENTAX\0"), covered)  # Make's data
            self.assertIn(data.index(b"2023:08:26"), covered)  # DateTimeOriginal's data

    def test_not_tiff(self):
        read, _ = self.reader(TINY_JPEG)
        self.assertEqual(walk_tiff(read, len(TINY_JPEG)), 0)

    def test_sparse_copy_keeps_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "x.tif")
            data = tiff_at_end(3 * 1024 * 1024)
            with open(src, "wb") as fh:
                fh.write(data)
            dest = os.path.join(tmp, "copy.tif")
            st, got, extra, how = copy_head(src, dest, 1024)
            self.assertEqual(how, exif.SPARSE)
            self.assertEqual(st.st_size, len(data))
            self.assertEqual(os.path.getsize(dest), len(data))  # true size, holes inside
            self.assertLess(got, 2 * 1024 * 1024)  # not the whole file
            self.assertGreater(extra, 0)
            with open(dest, "rb") as fh:
                copy = fh.read()
            ifd0 = 8 + 3 * 1024 * 1024
            self.assertEqual(copy[ifd0:], data[ifd0:])
            self.assertEqual(copy[:1024], data[:1024])
            plain = os.path.join(tmp, "plain.jpg")
            with open(plain, "wb") as fh:
                fh.write(TINY_JPEG * 100)
            self.assertEqual(copy_head(plain, dest, 1024)[2], 0)  # not TIFF: head only
            self.assertEqual(os.path.getsize(dest), 1024)


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class SparseExifToolTest(unittest.TestCase):
    """Lightroom-style DNG/TIFF with IFD0 past the 512 kB head: no 16 MB or full read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "drive")
        os.makedirs(self.root)
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)

    def write(self, name: str, data: bytes) -> None:
        with open(os.path.join(self.root, name), "wb") as fh:
            fh.write(data)

    # Make "Canon": a make without shutter counts, so a found Make ends the search; a
    # synthetic PENTAX file lacks the MakerNote that would carry its count.

    def test_ifd0_at_the_end_resolves_without_a_full_read(self):
        self.write("IMGQ0001.tif", tiff_at_end(700_000, make="Canon"))
        self.write("IMGQ0002.tif", tiff_at_end(700_000, ">", make="Canon",
                                               taken="2024:01:02 03:04:05"))  # fmt: skip
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root])
        self.assertEqual((run.read, run.sparse, run.big, run.whole), (2, 2, 0, 0))
        row = self.idx.exif_row(self.root, "IMGQ0001.tif")
        self.assertEqual((row["make"], row["model"], row["taken"], row["width"]),
                         ("Canon", "PENTAX K-1 Mark II", "2023:08:26 17:16:18", 8))  # fmt: skip
        self.assertEqual(self.idx.exif_row(self.root, "IMGQ0002.tif")["taken"],
                         "2024:01:02 03:04:05")  # fmt: skip

    def test_head_alone_would_need_the_big_read(self):
        self.write("x.tif", tiff_at_end(700_000, make="Canon"))
        scan_roots(self.idx, [self.root])
        with mock.patch.object(exif, "tiff_ifd0", return_value=None):  # treat as not TIFF
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.sparse, run.big), (0, 1))

    def test_structures_far_apart_are_each_read(self):
        # With tiny windows, IFD0, the ExifIFD and each tag's data need reads of their own.
        self.write("x.tif", tiff_at_end(2_000_000, make="Canon"))
        scan_roots(self.idx, [self.root])
        with mock.patch.object(exif, "WINDOW", 16), mock.patch.object(exif, "TAIL", 16):
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.sparse, run.big, run.whole), (1, 0, 0))
        self.assertEqual(self.idx.exif_row(self.root, "x.tif")["taken"], "2023:08:26 17:16:18")


class HangingExifToolTest(unittest.TestCase):
    """An "exiftool" that never answers: the watchdog kills it, nothing is cached."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.exe = os.path.join(self.tmp, "hang")
        with open(self.exe, "w") as fh:
            fh.write("#!/bin/sh\nwhile read -r line; do :; done\n")  # silent until EOF
        os.chmod(self.exe, stat.S_IRWXU)

    def test_timeout_kills_the_process(self):
        tool = ExifTool(self.exe, timeout=0.2)
        try:
            with self.assertRaises(ExifToolTimeout):
                tool.read(["/nowhere.jpg"])
            self.assertIsNotNone(tool.proc.poll())
        finally:
            tool.close()

    def test_timed_out_files_are_not_cached(self):
        root = os.path.join(self.tmp, "drive")
        os.makedirs(root)
        for name in ("a.jpg", "b.jpg"):
            with open(os.path.join(root, name), "wb") as fh:
                fh.write(TINY_JPEG)
        with Index(os.path.join(self.tmp, "index.db")) as idx:
            scan_roots(idx, [root])
            run = read_exif(idx, [root], exe=self.exe, timeout=0.2, jobs=1)
            self.assertEqual((run.transient, run.read), (2, 0))
            self.assertEqual(idx.exif_status([root])["missing"], 2)


if __name__ == "__main__":
    unittest.main()
