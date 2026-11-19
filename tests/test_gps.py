import csv
import os
import shutil
import tempfile
import unittest
from datetime import date

from dpf.gps import gps_rows, in_ranges, parse_range, write_gps_csv
from dpf.index import EXIF_COLUMNS, META_COLUMNS, Index
from dpf.models import FileRecord

R = "/d/Storage 1A"


def meta(dir, name, size, mtime, **kw):
    row = dict.fromkeys(META_COLUMNS)
    row.update(dir=dir, name=name, read_size=size, read_mtime=mtime, version=1, read_at=0.0, meta="{}",
               **kw)  # fmt: skip
    return [row[c] for c in META_COLUMNS]


def exif(dir, name, size, mtime, taken):
    row = dict.fromkeys(EXIF_COLUMNS)
    row.update(dir=dir, name=name, read_size=size, read_mtime=mtime, status="ok",
               taken=taken, make="PENTAX", model="PENTAX K-50", read_at=0.0)  # fmt: skip
    return [row[c] for c in EXIF_COLUMNS]


class GpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.idx.replace_root(R, [
            FileRecord(R + "/trip", "a.jpg", 10, 1.0, "image"),
            FileRecord(R + "/trip", "v.mp4", 20, 2.0, "video"),
            FileRecord(R + "/trip", "nogps.jpg", 30, 3.0, "image"),
            FileRecord(R + "/trip", "stale.jpg", 40, 4.0, "image"),
        ])  # fmt: skip
        self.idx.put_exif(
            [exif(R + "/trip", "a.jpg", 10, 1.0, "2021:01:02 10:00:00")],
            [meta(R + "/trip", "a.jpg", 10, 1.0, gps_lat=38.5, gps_lon=-97.6, gps_alt=400.0),
             meta(R + "/trip", "v.mp4", 20, 2.0, gps_lat=-1.25, gps_lon=36.8,
                  created="2024-03-01 09:00:00"),
             meta(R + "/trip", "nogps.jpg", 30, 3.0),
             meta(R + "/trip", "stale.jpg", 41, 4.0, gps_lat=1.0, gps_lon=1.0)],  # size changed
        )  # fmt: skip

    def test_rows_and_csv(self):
        rows = gps_rows(self.idx, [R], lambda r: "A")
        self.assertEqual([(r.name, r.taken, r.lat, r.lon, r.kind) for r in rows], [
            ("a.jpg", "2021-01-02 10:00:00", 38.5, -97.6, "image"),
            ("v.mp4", "2024-03-01 09:00:00", -1.25, 36.8, "video"),
        ])  # fmt: skip
        self.assertEqual((rows[0].camera, rows[0].drive, rows[0].pool),
                         ("PENTAX K-50", "Storage 1A", "A"))  # fmt: skip
        path = os.path.join(self.tmp, "gps.csv")
        write_gps_csv(rows, path)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))  # no BOM
        self.assertNotIn(b"\r\n", raw)
        with open(path, encoding="utf-8", newline="") as fh:
            first, video = list(csv.DictReader(fh))
        self.assertEqual((first["file_name"], first["folder_name"], first["path"]),
                         ("a.jpg", "trip", R + "/trip/a.jpg"))  # fmt: skip
        self.assertEqual((first["taken"], first["utc_offset"]), ("2021-01-02T10:00:00", ""))
        self.assertEqual((float(first["latitude"]), first["altitude_m"]), (38.5, "400.0"))
        self.assertIn("38.500000,-97.600000", first["map_link"])
        self.assertEqual(video["altitude_m"], "")

    def test_split_taken(self):
        from dpf.gps import split_taken

        self.assertEqual(split_taken("2011-01-02 09:53:24-05:00"),
                         ("2011-01-02T09:53:24", "-05:00"))  # fmt: skip
        self.assertEqual(split_taken("2020-01-01 00:00:00Z"), ("2020-01-01T00:00:00", "+00:00"))
        self.assertEqual(split_taken(None), ("", ""))
        self.assertEqual(split_taken("0000:00:00"), ("", ""))
        self.assertEqual(split_taken("2011-00-00 00:00:00"), ("", ""))

    def test_date_ranges(self):
        ranges = [parse_range("2020-08-01:2021-03-07"), parse_range("2024-03-01")]
        self.assertEqual(ranges[1], (date(2024, 3, 1), date(2024, 3, 1)))
        self.assertTrue(in_ranges("2021-03-07 23:59:59", ranges))  # end day included
        self.assertFalse(in_ranges("2021-03-08 00:00:00", ranges))
        self.assertFalse(in_ranges(None, ranges))
        self.assertTrue(in_ranges(None, []))
        rows = gps_rows(self.idx, [R], lambda r: "A", [parse_range("2024-01-15:2025-12-01")])
        self.assertEqual([r.name for r in rows], ["v.mp4"])
        with self.assertRaises(ValueError):
            parse_range("2021-03-07:2020-08-01")


if __name__ == "__main__":
    unittest.main()
