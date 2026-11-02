"""The photo/video database of `dpf exif`: metadata fields, video copies, re-reads, the
`media` view and the v9 migration."""

import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from dpf import exif
from dpf.exif import (
    BOXES,
    TAIL_STEP,
    ExifTool,
    copy_head,
    meta_row,
    meta_values,
    parse_entry,
    read_exif,
    row_values,
    walk_bmff,
)
from dpf.index import _VERSION, META_VERSION, Index
from dpf.models import FileRecord
from dpf.scanner import scan_roots
from tests.test_exif import HAVE_EXIFTOOL, SONY, TINY_JPEG

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

PHONE_VIDEO = {
    "SourceFile": "/x/VID_1.mp4", "AndroidMake": "Google", "AndroidModel": "Pixel 7",
    "CreateDate": "2024:05:01 10:00:00-05:00", "Duration": 12.5, "VideoFrameRate": 29.97,
    "CompressorID": "hvc1", "GPSCoordinates": "48.8584 2.2945 35.2", "ShutterCount": 7,
    "Rotation": 90, "ImageWidth": 1920, "ImageHeight": 1080,
}  # fmt: skip
EDITED = {
    "SourceFile": "/x/IMGQ0001.DNG", "Make": "PENTAX", "Model": "PENTAX K-1 Mark II",
    "GPSLatitude": -33.8568, "GPSLongitude": 151.2153, "GPSAltitude": 12,
    "GPSAltitudeRef": 1, "DpfLens": "smc PENTAX-D FA 24-70mm F2.8ED SDM WR",
    "FocalLength": 35, "FNumber": 2.8, "ExposureTime": 0.004, "ISO": 100, "Rating": 2,
    "DpfXMPRating": 4, "Subject": ["Sydney", "Opera"], "Keywords": "Opera",
    "XPKeywords": "Harbour; Sydney", "Software": "Adobe Photoshop Lightroom Classic 13.0",
    "Title": 2024, "ImageDescription": "The harbour", "Creator": ["A. Casada", "B"],
    "Rights": "(c) A. Casada", "PreservedFileName": "IMGQ0001.PEF",
    "DocumentID": "xmp.did:1", "OriginalDocumentID": "ORIG1", "CreateDate": "0000:00:00 00:00:00",
}  # fmt: skip


def rec(dir, name, size=100, mtime=1_000_000.0, kind="image"):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind=kind)


def rows_for(f: FileRecord, entry: dict, size=None, mtime=None) -> tuple[tuple, tuple]:
    data = parse_entry(entry, f.name)
    size, mtime = size or f.size, mtime or f.mtime
    return (row_values(f.dir, f.name, size, mtime, data, 5.0),
            meta_row(f.dir, f.name, size, mtime, data, 5.0))  # fmt: skip


class MetaValuesTest(unittest.TestCase):
    def test_photo_fields(self):
        data = parse_entry(EDITED, "IMGQ0001.DNG")
        v = meta_values(data)
        self.assertEqual((v["gps_lat"], v["gps_lon"], v["gps_alt"]), (-33.8568, 151.2153, -12.0))
        self.assertEqual(v["lens"], "smc PENTAX-D FA 24-70mm F2.8ED SDM WR")  # LensID as text
        self.assertEqual((v["focal"], v["fnumber"], v["exposure"], v["iso"]),
                         (35.0, 2.8, 0.004, 100))  # fmt: skip
        self.assertEqual(v["rating"], 4.0)  # XMP before EXIF
        self.assertEqual(json.loads(v["keywords"]), ["Sydney", "Opera", "Harbour"])
        self.assertEqual(v["software"], "Adobe Photoshop Lightroom Classic 13.0")
        self.assertEqual((v["title"], v["description"]), ("2024", "The harbour"))
        self.assertEqual((v["artist"], v["copyright"]), ("A. Casada; B", "(c) A. Casada"))
        self.assertEqual((v["preserved_name"], v["document_id"], v["original_document_id"]),
                         ("IMGQ0001.PEF", "xmp.did:1", "ORIG1"))  # fmt: skip
        self.assertIsNone(v["created"])  # zero date
        self.assertIsNone(v["duration"])
        row = meta_row("/x", "IMGQ0001.DNG", 5, 1.0, data, 9.0)
        meta = json.loads(row[-2])
        self.assertEqual((meta["Rating"], meta["DpfXMPRating"]), (2, 4))  # every tag kept
        self.assertNotIn("SourceFile", meta)
        self.assertEqual(row[4], META_VERSION)

    def test_identity_is_unchanged_by_the_new_fields(self):
        plain = parse_entry(SONY, "DSC01234.ARW")
        rich = parse_entry({**EDITED, **SONY}, "DSC01234.ARW")
        self.assertEqual(rich.shot, plain.shot)
        self.assertEqual(rich.ident, "sony|ilce-7rm4a|a1b20300040c|4321|2023:08:26 17:16:18")

    def test_video_fields_and_no_identity(self):
        data = parse_entry(PHONE_VIDEO, "VID_1.mp4")
        self.assertTrue(data.video)
        self.assertEqual((data.make, data.model), ("Google", "Pixel 7"))
        self.assertIsNone(data.ident)  # even with a ShutterCount
        v = meta_values(data)
        self.assertEqual(v["created"], "2024:05:01 10:00:00-05:00")
        self.assertEqual((v["gps_lat"], v["gps_lon"], v["gps_alt"]), (48.8584, 2.2945, 35.2))
        self.assertEqual((v["duration"], v["fps"], v["codec"]), (12.5, 29.97, "hvc1"))
        apple = {**PHONE_VIDEO, "CreationDate": "2024:05:01 17:00:00+02:00"}
        apple = meta_values(parse_entry(apple, "IMG_1.MOV"))
        self.assertEqual(apple["created"], "2024:05:01 17:00:00+02:00")  # Apple's, with zone
        sony = parse_entry({"DeviceManufacturer": "Sony", "DeviceModelName": "ILCE-7RM4",
                            "DeviceSerialNo": "3012345", "Duration": 0}, "C0001.MP4")  # fmt: skip
        self.assertEqual((sony.make, sony.model, sony.serial), ("Sony", "ILCE-7RM4", "3012345"))
        self.assertIsNone(meta_values(sony)["duration"])  # 0 is no duration

    def test_no_position(self):
        for gps in ({"GPSLatitude": 0, "GPSLongitude": 0}, {"GPSLatitude": 95, "GPSLongitude": 1},
                    {"GPSCoordinates": "garbage"}):  # fmt: skip
            v = meta_values(parse_entry(gps, "x.jpg"))
            self.assertEqual((v["gps_lat"], v["gps_lon"]), (None, None), gps)

    def test_error_rows_keep_the_message(self):
        row = meta_row("/x", "a.jpg", 5, 1.0, exif.error_data("a.jpg", "Input/output error"), 0)
        self.assertEqual(json.loads(row[-2]), {"Error": "Input/output error"})


def boxes(*parts: tuple[bytes, int | bytes]) -> bytes:
    """An ISO BMFF file: (type, payload bytes or payload size of zeros) per top-level box."""
    out = b""
    for kind, payload in parts:
        data = payload if isinstance(payload, bytes) else bytes(payload)
        out += struct.pack(">I4s", 8 + len(data), kind) + data
    return out


class BmffWalkTest(unittest.TestCase):
    def reader(self, data: bytes):
        reads = []

        def read(offset: int, n: int) -> bytes:
            reads.append((offset, n))
            return data[offset:offset + n]

        return read, reads

    def test_reads_every_box_but_media_data(self):
        moov = b"moov-payload" * 10
        data = boxes((b"ftyp", b"isom0000"), (b"mdat", 100_000), (b"moov", moov),
                     (b"uuid", b"x" * 40))  # fmt: skip
        read, reads = self.reader(data)
        self.assertEqual(walk_bmff(read, len(data)), 4)
        moov_at = data.index(b"moov") - 4
        self.assertIn((moov_at, 8 + len(moov)), reads)  # the whole moov box
        self.assertNotIn(16, [off for off, n in reads if n > 16])  # mdat: header only
        self.assertEqual(max(n for _, n in reads), 8 + len(moov))

    def test_large_size_zero_size_and_trailer(self):
        big = struct.pack(">I4sQ", 1, b"mdat", 16 + 5000) + bytes(5000)
        data = boxes((b"ftyp", b"isom")) + big + boxes((b"moov", b"m" * 20))
        read, _ = self.reader(data)
        self.assertEqual(walk_bmff(read, len(data)), 3)
        to_end = boxes((b"ftyp", b"isom")) + struct.pack(">I4s", 0, b"mdat") + bytes(300)
        read, _ = self.reader(to_end)
        self.assertEqual(walk_bmff(read, len(to_end)), 2)
        trailer = boxes((b"ftyp", b"isom"), (b"moov", b"m" * 20)) + b"\xff" * 100
        read, reads = self.reader(trailer)
        walk_bmff(read, len(trailer))
        self.assertEqual(reads[-1], (0, exif.WINDOW))  # the rest is read as the tail

    def test_copy_of_a_video_with_moov_at_the_end_is_sparse(self):
        moov = b"\x01" * 3000
        data = boxes((b"ftyp", b"isom0000"), (b"mdat", 3_000_000), (b"moov", moov))
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = os.path.join(tmp, "C0001.MP4"), os.path.join(tmp, "copy.mp4")
            with open(src, "wb") as fh:
                fh.write(data)
            st, got, extra, how = copy_head(src, dest, 1024, video=True)
            self.assertEqual(how, BOXES)
            self.assertLess(got, 200_000)
            self.assertEqual(os.path.getsize(dest), len(data))
            with open(dest, "rb") as fh:
                copy = fh.read()
            self.assertTrue(copy.endswith(b"moov" + moov))
            self.assertEqual(copy[:1024], data[:1024])

    def test_other_videos_get_head_and_tail(self):
        data = b"RIFF" + bytes(3_000_000) + b"TAILDATA"
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = os.path.join(tmp, "x.avi"), os.path.join(tmp, "copy.avi")
            with open(src, "wb") as fh:
                fh.write(data)
            st, got, extra, how = copy_head(src, dest, 1024, video=True)
            self.assertEqual((how, extra), (TAIL_STEP, exif.TAIL))
            with open(dest, "rb") as fh:
                self.assertTrue(fh.read().endswith(b"TAILDATA"))
            self.assertEqual(copy_head(src, dest, 1024)[3], None)  # a photo: head only


class MediaViewTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def test_one_row_per_photo_and_video_with_valid_metadata(self):
        photo = rec("/d/Drive/2024", "IMGQ0001.DNG", mtime=1_700_000_000.0)
        video = rec("/d/Drive/2024", "VID_1.mp4", 5000, kind="video")
        stale = rec("/d/Drive/2024", "old.jpg")
        other = rec("/d/Drive/2024", "notes.txt", kind="other")
        self.idx.replace_root("/d/Drive", [photo, video, stale, other])
        entry = {**EDITED, "DateTimeOriginal": "2024:01:02 03:04:05",
                 "OffsetTimeOriginal": "+11:00"}  # fmt: skip
        rows = [rows_for(photo, entry), rows_for(video, PHONE_VIDEO),
                rows_for(stale, EDITED, mtime=2.0)]  # stale: read before the file changed
        self.idx.put_exif([r for r, _ in rows], [m for _, m in rows])
        view = self.idx.conn.execute("SELECT * FROM media ORDER BY name")
        names = [d[0] for d in view.description]
        got = {r[names.index("name")]: dict(zip(names, r)) for r in view}
        self.assertEqual(sorted(got), ["IMGQ0001.DNG", "VID_1.mp4", "old.jpg"])  # no "other"
        p = got["IMGQ0001.DNG"]
        self.assertEqual((p["path"], p["root"], p["kind"]),
                         ("/d/Drive/2024/IMGQ0001.DNG", "/d/Drive", "image"))  # fmt: skip
        self.assertEqual((p["taken"], p["taken_offset"]), ("2024-01-02 03:04:05", "+11:00"))
        self.assertEqual(p["mtime"], time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(1_700_000_000)))  # fmt: skip
        self.assertEqual((p["make"], p["lens"]), ("PENTAX", EDITED["DpfLens"]))
        self.assertEqual((p["aperture"], p["iso"], p["rating"]), (2.8, 100, 4.0))
        self.assertEqual((p["gps_lat"], p["preserved_name"], p["exif_status"]),
                         (-33.8568, "IMGQ0001.PEF", "none"))  # fmt: skip
        self.assertEqual(json.loads(p["meta"])["PreservedFileName"], "IMGQ0001.PEF")
        v = got["VID_1.mp4"]
        self.assertEqual((v["taken"], v["duration"], v["make"], v["width"]),
                         ("2024-05-01 10:00:00-05:00", 12.5, "Google", 1920))  # fmt: skip
        self.assertIsNone(v["shot"])  # a video never has a shot key
        s = got["old.jpg"]
        self.assertIsNone(s["exif_status"])  # out of date: no metadata shown
        self.assertIsNone(s["meta"])
        count = self.idx.conn.execute(
            "SELECT count(*) FROM media WHERE gps_lat IS NOT NULL AND kind = 'image'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_forget_root_drops_metadata(self):
        f = rec("/d/Drive", "a.jpg")
        self.idx.replace_root("/d/Drive", [f])
        row, meta = rows_for(f, EDITED)
        self.idx.put_exif([row], [meta])
        self.idx.forget_root("/d/Drive")
        self.assertIsNone(self.idx.meta_row("/d/Drive", "a.jpg"))


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def test_rows_of_an_older_build_are_refreshed(self):
        a, b, c = rec("/r", "a.ARW"), rec("/r", "b.ARW"), rec("/r", "c.ARW")
        self.idx.replace_root("/r", [a, b, c])
        ra, ma = rows_for(a, SONY)
        rb, mb = rows_for(b, SONY)
        rc, _ = rows_for(c, {**SONY, "ShutterCount": 99})
        self.idx.put_exif([ra, rb, rc], [ma, mb])  # c: an exif row of the old build only
        self.assertEqual(self.idx.exif_todo(["/r"]), [
            ("/r", "c.ARW", 100, 1_000_000.0, "image", True, rc[17]),
        ])  # fmt: skip
        self.idx.conn.execute("UPDATE exif_meta SET version = 0 WHERE name = 'b.ARW'")
        self.assertEqual([t[1] for t in self.idx.exif_todo(["/r"])], ["b.ARW", "c.ARW"])
        self.idx.put_exif([], [rows_for(c, SONY)[1]])
        self.idx.conn.execute("UPDATE exif SET status = 'error' WHERE name = 'c.ARW'")
        todo = self.idx.exif_todo(["/r"], retry_errors=True)
        self.assertEqual([(t[1], t[5], t[6]) for t in todo],
                         [("b.ARW", True, rb[17]), ("c.ARW", False, None)])  # fmt: skip


class MigrationTest(unittest.TestCase):
    def test_version_8_keeps_every_row_and_gains_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.db")
            files = [rec("/m/A", f"DSC0{i}.ARW") for i in range(5)] + [
                rec("/m/A", "C0001.MP4", kind="video")]
            with Index(path) as idx:
                idx.replace_root("/m/A", files)
                idx.put_exif([rows_for(f, {**SONY, "ShutterCount": i + 1})[0]
                              for i, f in enumerate(files[:5])])  # fmt: skip
                before = idx.conn.execute("SELECT * FROM exif ORDER BY name").fetchall()
                idx.conn.execute("DROP VIEW media")
                idx.conn.execute("DROP TABLE exif_meta")
                idx.conn.execute("PRAGMA user_version = 8")
                idx.conn.commit()
            with Index(path) as idx:
                self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], _VERSION)
                self.assertEqual(_VERSION, 9)
                after = idx.conn.execute("SELECT * FROM exif ORDER BY name").fetchall()
                self.assertEqual(after, before)  # every exif row kept as it was
                self.assertEqual(idx.totals(["/m"])[0], 6)
                todo = idx.exif_todo(["/m"])
                self.assertEqual([(t[1], t[5]) for t in todo], [("C0001.MP4", False)] + [
                    (f"DSC0{i}.ARW", True) for i in range(5)])  # fmt: skip
                n = idx.conn.execute("SELECT count(*) FROM media WHERE shutter_count > 0")
                self.assertEqual(n.fetchone()[0], 5)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            conn.close()


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class ExifToolMetaTest(unittest.TestCase):
    """The real exiftool on generated files in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "drive")
        self.dir = os.path.join(self.root, "DCIM")
        os.makedirs(self.dir)
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def tag(self, path: str, *tags: str) -> None:
        subprocess.run(["exiftool", "-q", "-overwrite_original", *tags, path], check=True)

    def test_jpeg_xmp_and_iptc_lie_in_the_head(self):
        path = self.path("IMG_0001.JPG")
        with open(path, "wb") as fh:
            fh.write(TINY_JPEG + os.urandom(900_000))  # image data past the 512 kB head
        self.tag(path, "-XMP:Subject=Hawaii", "-XMP:Subject=Beach", "-IPTC:Keywords=Surf",
                 "-XMP:Rating=3", "-EXIF:Rating=1", "-XMP:Title=Sunset", "-XMP:Label=Red",
                 "-ImageDescription=At the beach", "-Software=Lightroom",
                 "-XMP-xmpMM:PreservedFileName=DSC01234.ARW", "-GPSLatitude=21.3",
                 "-GPSLatitudeRef=N", "-GPSLongitude=157.8", "-GPSLongitudeRef=W")  # fmt: skip
        self.assertGreater(os.path.getsize(path), exif.HEAD_BYTES)
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root])
        self.assertEqual((run.read, run.big, run.whole, run.sparse), (1, 0, 0, 0))
        m = self.idx.meta_row(self.dir, "IMG_0001.JPG")
        self.assertEqual(json.loads(m["keywords"]), ["Hawaii", "Beach", "Surf"])
        self.assertEqual((m["rating"], m["title"], m["label"]), (3.0, "Sunset", "Red"))
        self.assertEqual((m["description"], m["software"]), ("At the beach", "Lightroom"))
        self.assertEqual((m["gps_lat"], m["gps_lon"]), (21.3, -157.8))
        self.assertEqual(m["preserved_name"], "DSC01234.ARW")
        self.assertEqual((m["meta"]["DpfXMPRating"], m["meta"]["DpfEXIFRating"]), (3, 1))
        self.assertEqual(m["version"], META_VERSION)

    def test_tiff_xmp_past_the_head_is_found_by_the_walk(self):
        from tests.test_exif import tiff_at_end

        xmp = XMP_PACKET.encode()
        data = tiff_at_end(2_000_000, make="Canon", xmp=xmp)
        with open(self.path("IMGQ0001.tif"), "wb") as fh:
            fh.write(data)
        scan_roots(self.idx, [self.root])
        tiny = {"WINDOW": 16, "TAIL": 16}  # no window or tail read covers the XMP by chance
        with mock.patch.multiple(exif, **tiny):
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.sparse, run.big, run.whole), (1, 0, 0))
        m = self.idx.meta_row(self.dir, "IMGQ0001.tif")
        self.assertEqual(json.loads(m["keywords"]), ["Hawaii", "Beach"])
        self.assertEqual((m["rating"], m["preserved_name"]), (3.0, "IMGQ0001.PEF"))
        self.assertEqual(m["software"], "Adobe Photoshop Lightroom Classic 13.0")
        self.assertEqual(self.idx.exif_row(self.dir, "IMGQ0001.tif")["make"], "Canon")
        self.idx.conn.execute("DELETE FROM exif_meta")
        self.idx.conn.commit()
        without = exif._DATA_TAGS - {0x02BC}  # not fetching tag 700 loses the XMP
        with mock.patch.multiple(exif, _DATA_TAGS=without, **tiny):
            read_exif(self.idx, [self.root])
        self.assertIsNone(self.idx.meta_row(self.dir, "IMGQ0001.tif")["keywords"])

    def test_refresh_keeps_the_cached_identity(self):
        path = self.path("DSC01234.JPG")
        with open(path, "wb") as fh:
            fh.write(TINY_JPEG)
        self.tag(path, "-Make=SONY", "-Model=ILCE-7RM4A", "-XMP:Rating=2")
        scan_roots(self.idx, [self.root])
        (f,) = self.idx.list_dir(self.dir)
        old = rows_for(f, SONY)[0]  # as cached by an earlier build: a shot key
        self.idx.put_exif([old])
        before = self.idx.exif_row(self.dir, f.name)
        run = read_exif(self.idx, [self.root])
        self.assertEqual((run.read, run.refreshed, run.ok, run.none), (1, 1, 0, 0))
        self.assertEqual(self.idx.exif_row(self.dir, f.name), before)  # identity kept
        self.assertEqual(self.idx.meta_row(self.dir, f.name)["rating"], 2.0)
        self.assertEqual(read_exif(self.idx, [self.root]).todo, 0)
        os.utime(path, (5, 5))  # changed since: read in full, both rows anew
        scan_roots(self.idx, [self.root])
        run = read_exif(self.idx, [self.root])
        self.assertEqual((run.refreshed, run.none), (0, 1))
        self.assertIsNone(self.idx.exif_row(self.dir, f.name)["shot"])

    def test_refresh_recomputes_the_same_shot_key(self):
        for name in ("DSC01234.ARW", "DSC01235.ARW"):
            with open(self.path(name), "wb") as fh:
                fh.write(TINY_JPEG)
        scan_roots(self.idx, [self.root])
        entries = {"DSC01234.ARW": SONY, "DSC01235.ARW": {**SONY, "ShutterCount": 4322}}
        real = ExifTool.read
        names: dict[str, str] = {}

        def read(tool, paths, args=exif.ARGS):  # exiftool sees camera tags in these files
            found = real(tool, paths, args)
            return {p: {**e, **entries[names[p]]} for p, e in found.items()}

        def copy(src, dest, *a, **k):
            names[dest] = os.path.basename(src)
            return real_copy(src, dest, *a, **k)

        real_copy = exif.copy_head
        with mock.patch.object(ExifTool, "read", read), mock.patch.object(exif, "copy_head", copy):
            first = read_exif(self.idx, [self.root])
            self.assertEqual(first.ok, 2)
            keys = {n: self.idx.exif_row(self.dir, n)["shot"] for n in entries}
            self.idx.conn.execute("DELETE FROM exif_meta")  # as before schema v9
            self.idx.conn.commit()
            again = read_exif(self.idx, [self.root])
            self.assertEqual((again.refreshed, again.changed), (2, 0))  # same keys recomputed
            self.idx.conn.execute("UPDATE exif_meta SET version = 0")
            self.idx.conn.commit()
            entries["DSC01235.ARW"] = {**SONY, "ShutterCount": 9999}
            third = read_exif(self.idx, [self.root])
        self.assertEqual((third.refreshed, third.changed), (2, 1))  # counted, not applied
        self.assertEqual({n: self.idx.exif_row(self.dir, n)["shot"] for n in entries}, keys)
        self.assertEqual(self.idx.meta_row(self.dir, "DSC01234.ARW")["meta"]["ShutterCount"], 4321)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_mp4_with_moov_at_the_end_is_read_from_its_boxes(self):
        late, early = self.path("C0001.MP4"), self.path("PXL_1.mp4")
        noise = ["-f", "lavfi", "-i", "nullsrc=s=320x240:r=25,geq=random(1)*255:128:128",
                 "-t", "1", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
                 "-metadata", "creation_time=2023-08-26T15:16:18Z",
                 "-metadata", "location=+37.1234-122.1234/"]  # fmt: skip
        for path, flags in ((late, []), (early, ["-movflags", "+faststart"])):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *noise, *flags, path],
                           check=True)  # fmt: skip
        self.assertGreater(os.path.getsize(late), 4 * exif.HEAD_BYTES)
        scan_roots(self.idx, [self.root])
        copied = []
        real_copy = exif.copy_head

        def copy(src, dest, *a, **k):
            result = real_copy(src, dest, *a, **k)
            copied.append((os.path.basename(src), result[1], result[0].st_size))
            return result

        with mock.patch.object(exif, "copy_head", copy):
            run = read_exif(self.idx, [self.root])
        self.assertEqual((run.read, run.videos, run.boxes, run.tail, run.big, run.whole),
                         (2, 2, 1, 0, 0, 0))  # fmt: skip
        for name, got, size in copied:  # never a full read
            self.assertLess(got, size / 2, name)
        for name in ("C0001.MP4", "PXL_1.mp4"):
            row, m = self.idx.exif_row(self.dir, name), self.idx.meta_row(self.dir, name)
            self.assertEqual((row["status"], row["shot"], row["width"]), ("none", None, 320))
            self.assertEqual((m["duration"], m["fps"], m["codec"]), (1.0, 25.0, "avc1"))
            self.assertEqual((m["gps_lat"], m["gps_lon"]), (37.1234, -122.1234))
            self.assertRegex(m["created"], r"^2023:08:2[67] \d\d:\d6:18[+-]\d\d:\d\d$")  # local
        self.assertEqual(read_exif(self.idx, [self.root], photos_only=True).todo, 0)


XMP_PACKET = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
 xmp:Rating="3" xmp:CreatorTool="Adobe Photoshop Lightroom Classic 13.0"
 xmpMM:PreservedFileName="IMGQ0001.PEF" xmpMM:DocumentID="xmp.did:abc">
<dc:subject><rdf:Bag><rdf:li>Hawaii</rdf:li><rdf:li>Beach</rdf:li></rdf:Bag></dc:subject>
</rdf:Description></rdf:RDF></x:xmpmeta>""" + " " * 600 + """<?xpacket end="w"?>"""


if __name__ == "__main__":
    unittest.main()
