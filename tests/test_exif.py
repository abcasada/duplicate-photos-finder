import base64
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from dpf import media
from dpf.exif import (
    ERROR,
    NONE,
    OK,
    ExifData,
    ExifTool,
    ExifToolMissing,
    fmt_of,
    needs_full_read,
    parse_entry,
    read_exif,
    row_values,
)
from dpf.index import _VERSION, Index
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

SONY = {
    "SourceFile": "/x/DSC01234.ARW", "Make": "SONY", "Model": "ILCE-7RM4A",
    "SerialNumber": "00000000", "InternalSerialNumber": "a1b2c3d4e5f6", "ShutterCount": 4321,
    "DateTimeOriginal": "2023:08:26 17:16:18", "OffsetTimeOriginal": "+02:00",
    "ImageWidth": 9504, "ImageHeight": 6336,
}  # fmt: skip
PENTAX = {
    "SourceFile": "/x/IMGQ0001.DNG", "Make": "RICOH IMAGING COMPANY, LTD.",
    "Model": "PENTAX K-1 Mark II", "SerialNumber": 1234567, "ShutterCount": 99,
    "DateTimeOriginal": "2024:01:02 03:04:05",
}  # fmt: skip


class ParseTest(unittest.TestCase):
    def test_sony_uses_internal_serial_only(self):
        d = parse_entry(SONY, "DSC01234.ARW")
        self.assertEqual(d.serial, "a1b2c3d4e5f6")
        self.assertEqual((d.shutter, d.taken, d.taken_offset),
                         (4321, "2023:08:26 17:16:18", "+02:00"))  # fmt: skip
        self.assertEqual(d.status, OK)
        self.assertEqual(d.ident, "sony|ilce-7rm4a|a1b2c3d4e5f6|4321|2023:08:26 17:16:18")
        self.assertEqual(d.shot, d.ident + "|arw|")  # RAW: no pixel size in the key
        without = parse_entry({**SONY, "InternalSerialNumber": None}, "DSC01234.ARW")
        self.assertIsNone(without.serial)  # never falls back to Sony's SerialNumber
        self.assertEqual(without.status, NONE)

    def test_pentax_numeric_serial(self):
        d = parse_entry(PENTAX, "IMGQ0001.DNG")
        self.assertEqual(d.serial, "1234567")
        self.assertEqual(d.shot, "ricoh imaging company, ltd.|pentax k-1 mark ii|1234567|99|"
                                 "2024:01:02 03:04:05|dng|")  # fmt: skip

    def test_serial_normalised(self):
        a = parse_entry({**PENTAX, "SerialNumber": "0012AB "}, "x.dng")
        b = parse_entry({**PENTAX, "SerialNumber": "12ab"}, "y.DNG")
        self.assertEqual(a.shot, b.shot)
        zero = parse_entry({**PENTAX, "SerialNumber": "0000"}, "x.dng")
        self.assertIsNone(zero.ident)

    def test_jpg_shot_carries_pixel_size_and_family(self):
        jpg = parse_entry({**SONY, "SourceFile": "a.JPG"}, "DSC01234.JPG")
        jpeg = parse_entry({**SONY, "SourceFile": "a.jpeg"}, "x.jpeg")
        small = parse_entry({**SONY, "ImageWidth": 2000, "ImageHeight": 1333}, "y.jpg")
        raw = parse_entry(SONY, "DSC01234.ARW")
        self.assertEqual(jpg.ident, raw.ident)  # RAW+JPG: one shot ...
        self.assertNotEqual(jpg.shot, raw.shot)  # ... in two formats
        self.assertEqual(jpg.shot, jpeg.shot)
        self.assertTrue(jpg.shot.endswith("|jpg|9504x6336"))
        self.assertNotEqual(jpg.shot, small.shot)  # a resized export is not the same shot

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

    def test_zero_dates_and_video(self):
        d = parse_entry(
            {"SourceFile": "v", "CreateDate": "2023:01:01 10:00:00", "Duration": 12.5,
             "DateTimeOriginal": "0000:00:00 00:00:00"},
            "C0001.MP4",
        )  # fmt: skip
        self.assertEqual((d.created, d.duration, d.taken), ("2023:01:01 10:00:00", 12.5, None))
        self.assertIsNone(parse_entry({"CreateDate": "0000:00:00 00:00:00"}, "a.mp4").created)

    def test_error(self):
        d = parse_entry({**SONY, "Error": "File format error"}, "x.ARW")
        self.assertEqual(d.status, ERROR)
        self.assertIsNone(d.shot)
        self.assertEqual(d.note, "File format error")

    def test_fmt_of(self):
        self.assertEqual([fmt_of(n) for n in ("a.JPEG", "b.jpe", "c.TIFF", "d.heif", "e.ARW")],
                         ["jpg", "jpg", "tif", "heic", "arw"])  # fmt: skip

    def test_needs_full_read(self):
        video = ExifData(fmt="mp4", duration=3.0)
        self.assertTrue(needs_full_read(video, media.VIDEO))  # moov at the end?
        done = ExifData(fmt="mp4", created="2023:01:01 00:00:00", duration=3.0)
        self.assertFalse(needs_full_read(done, media.VIDEO))
        self.assertFalse(needs_full_read(ExifData(fmt="jpg"), media.IMAGE))  # head enough
        self.assertTrue(needs_full_read(ExifData(fmt="arw"), media.IMAGE))  # no Make
        sony = parse_entry({**SONY, "ShutterCount": None}, "x.ARW")
        self.assertTrue(needs_full_read(sony, media.IMAGE))
        self.assertFalse(needs_full_read(parse_entry(SONY, "x.ARW"), media.IMAGE))
        canon = ExifData(fmt="cr2", make="Canon", model="EOS")
        self.assertFalse(needs_full_read(canon, media.IMAGE))  # no counter expected
        sized = parse_entry(SONY, "x.JPG")
        self.assertFalse(needs_full_read(sized, media.IMAGE))
        unsized = parse_entry({**SONY, "ImageWidth": None}, "x.JPG")
        self.assertTrue(needs_full_read(unsized, media.IMAGE))  # the shot key needs the size


def rec(dir, name, size=100, mtime=1.0, kind="image"):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind=kind)


def exif_row(f: FileRecord, entry: dict = SONY, size=None, mtime=None):
    data = parse_entry(entry, f.name)
    return row_values(f.dir, f.name, size or f.size, mtime or f.mtime, data, 5.0)


class IndexCacheTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def test_todo_skips_valid_rows(self):
        a, b, c = rec("/r", "a.ARW"), rec("/r", "b.ARW"), rec("/r", "c.ARW")
        self.idx.replace_root("/r", [a, b, c, rec("/r", "empty.jpg", 0)])
        self.idx.put_exif([exif_row(a), exif_row(b, mtime=2.0)])  # b changed since
        todo = [r[1] for r in self.idx.exif_todo(["/r"])]
        self.assertEqual(todo, ["b.ARW", "c.ARW"])  # 0-byte files are never read
        status = self.idx.exif_status(["/r"])
        self.assertEqual(status, {"ok": 1, "none": 0, "error": 0, "missing": 2})

    def test_retry_errors(self):
        a = rec("/r", "a.ARW")
        self.idx.replace_root("/r", [a])
        self.idx.put_exif([exif_row(a, {"Error": "bad"})])
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
    def test_version_5_gains_exif_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.db")
            with Index(path) as idx:
                idx.replace_root("/m", [rec("/m", "x.jpg")])
                idx.conn.execute("DROP TABLE exif")
                idx.conn.execute("PRAGMA user_version = 5")
                idx.conn.commit()
            with Index(path) as idx:
                self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
                self.assertEqual(idx.totals(["/m"])[0], 1)
                self.assertEqual(idx.exif_status(["/m"])["missing"], 1)
                indexes = {r[1] for r in idx.conn.execute("PRAGMA index_list(exif)")}
                self.assertIn("exif_shot", indexes)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
            conn.close()


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

    def test_reads_tags_and_caches_them(self):
        self.jpeg(
            "IMGQ0001.JPG", "-Make=PENTAX", "-Model=PENTAX K-1 Mark II",
            "-SerialNumber=1234567", "-DateTimeOriginal=2023:08:26 17:16:18",
        )  # fmt: skip
        self.jpeg("plain.jpg")
        with open(os.path.join(self.dir, "broken.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8 not really a jpeg" * 10)
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root], jobs=2, readers=2, head_bytes=4096)
        self.assertEqual((run.todo, run.read, run.errors, run.gone), (3, 3, 1, 0))
        row = self.idx.exif_row(self.dir, "IMGQ0001.JPG")
        self.assertEqual(
            (row["make"], row["model"], row["serial"], row["taken"], row["width"], row["fmt"]),
            ("PENTAX", "PENTAX K-1 Mark II", "1234567", "2023:08:26 17:16:18", 8, "jpg"),
        )
        self.assertEqual(row["status"], NONE)  # no ShutterCount: no shot identity
        self.assertEqual(self.idx.exif_row(self.dir, "broken.jpg")["status"], ERROR)
        self.assertEqual(read_exif(self.idx, [self.root]).todo, 0)  # cached: nothing to do
        self.assertEqual(sorted(os.listdir(self.dir)), ["IMGQ0001.JPG", "broken.jpg", "plain.jpg"])

    def test_files_only_read(self):
        path = self.jpeg("a.jpg", "-Make=SONY")
        before = os.stat(path)
        with open(path, "rb") as fh:
            content = fh.read()
        scan_roots(self.idx, [self.root])
        read_exif(self.idx, [self.root])
        after = os.stat(path)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), content)
        self.assertEqual(sorted(os.listdir(self.dir)), ["a.jpg"])  # no sidecars or temp files

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

    def test_full_read_when_head_lacks_tags(self):
        # Not a JPEG/PNG by name, so a head without Make sends exiftool to the original.
        path = self.jpeg("x.jpg", "-Make=PENTAX")
        os.rename(path, os.path.join(self.dir, "x.dng"))
        self.jpeg("y.jpg", "-Make=PENTAX")  # JPEG: the head is trusted
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root], head_bytes=16)
        self.assertEqual((run.read, run.full_reads), (2, 1))
        self.assertEqual(self.idx.exif_row(self.dir, "x.dng")["make"], "PENTAX")
        self.assertIsNone(self.idx.exif_row(self.dir, "y.jpg")["make"])

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


if __name__ == "__main__":
    unittest.main()
