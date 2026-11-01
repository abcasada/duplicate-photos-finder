import os
import shutil
import tempfile
import time
import unittest
from datetime import date

from dpf.dumps import DumpConfig, check_dumps, format_days, format_dumps, parse_dumps
from dpf.index import Index
from dpf.models import FileRecord
from dpf.pools import PoolError
from tests.test_match_shot import shot

T = 1_600_000_000
A, B = "/d/Storage 1A", "/d/Storage 1B"
DUMP = A + "/Photography/_Dumps/2026-01"


def photos(dir, numbers, kind="image", ext="ARW"):
    return [FileRecord(dir, f"DSC{n:05}.{ext}", 1000 + n, T + n, kind) for n in numbers]


class DumpsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.idx.replace_root(A, [
            *photos(DUMP, range(1, 13)),  # 1-12 dumped
            *photos(DUMP, [1], "video", "MP4"),
            *photos(A + "/Photography/2026/Trip", [1, 2]),  # sorted on the same drive
            *photos(A + "/Dumps/small", range(50, 54)),  # only 4: under the threshold
            *photos(A + "/Archive", [5, 6]),  # neither dump nor sorted: no help
        ])  # fmt: skip
        self.idx.replace_root(B, [
            *photos(B + "/Backup/photography/2026", [3, 4]),  # sorted on another drive
            *photos(B + "/DUMPS/x", range(1, 13)),  # another dump: never counts as sorted
        ])  # fmt: skip

    def check(self, roots=(A, B), least=5):
        pool = {A: "A", B: "B"}
        return check_dumps(
            self.idx, list(roots), [A, B], DumpConfig("dump", "photography", least),
            lambda d: pool[A] if d.startswith(A) else pool[B], basis="name+size+mtime",
        )  # fmt: skip

    def test_lists_dump_folders_with_enough_unsorted_photos(self):
        check = self.check()
        self.assertEqual([(f.dir, f.pool) for f in check.folders],
                         [(DUMP, "A"), (B + "/DUMPS/x", "B")])  # fmt: skip
        f = check.folders[0]
        self.assertEqual((f.photos, f.videos, f.total), (8, 1, 13))  # 5-12 and the MP4
        self.assertEqual(f.bytes, sum(1000 + n for n in range(5, 13)) + 1001)
        self.assertEqual([(r.a_first, r.a_last, r.count) for r in f.runs],
                         [("DSC00001.MP4", "DSC00001.MP4", 1), ("DSC00005.ARW", "DSC00012.ARW", 8)])
        self.assertEqual((check.dump_folders, check.dump_files), (3, 29))
        self.assertEqual(check.unsorted_files, 9 + 4 + 8)

    def test_threshold_counts_photos(self):
        self.assertEqual(self.check(least=9).folders, [])
        self.assertEqual(len(self.check(least=8).folders), 2)

    def test_only_given_roots_are_reported(self):
        self.assertEqual([f.pool for f in self.check(roots=[B]).folders], ["B"])

    def test_text(self):
        text = format_dumps(self.check(), top=1, max_runs=5)
        self.assertIn("Listed:    2 folders with 5+ unsorted photos: 17 files", text)
        self.assertIn(f"{DUMP}   (pool A; 8 photos, 1 video unsorted, of 13 files)", text)
        self.assertIn("DSC00005.ARW … DSC00012.ARW", text)
        self.assertIn("… and 1 more folders", text)

    def test_parse(self):
        self.assertIsNone(parse_dumps({}))
        self.assertEqual(parse_dumps({"dumps": {"dump": "dump", "sorted": "photography"}}),
                         DumpConfig("dump", "photography", 100))  # fmt: skip
        for bad in ({"dump": "dump"}, {"dump": "dump", "sorted": "dumpster"},
                    {"dump": "a", "sorted": "b", "min_photos": 0},
                    {"dump": "a", "sorted": "b", "extra": 1}):  # fmt: skip
            with self.assertRaises(PoolError, msg=bad):
                parse_dumps({"dumps": bad})


def noon(y, m, d):
    return time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))


class DumpDatesTest(unittest.TestCase):
    def test_format_days(self):
        d = [date(2026, 9, n) for n in (1, 2, 3, 5, 7, 8, 10, 11, 12, 13, 15)]
        self.assertEqual(
            format_days(d[::-1] + d),  # order and repeats do not matter
            "2026-09-01 - 2026-09-03, 2026-09-05, 2026-09-07, 2026-09-08, "
            "2026-09-10 - 2026-09-13, 2026-09-15",
        )
        self.assertEqual(format_days([date(2026, 12, 30), date(2027, 1, 1)]),
                         "2026-12-30, 2027-01-01")  # fmt: skip
        self.assertEqual(format_days([date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)]),
                         "2026-12-31 - 2027-01-02")  # fmt: skip
        self.assertEqual(format_days([]), "")

    def test_folder_dates_come_from_unsorted_files_exif_first(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(idx.close)
        dump = A + "/_Dumps/trip"
        days = [(2026, 9, 1), (2026, 9, 2), (2026, 9, 3), (2026, 9, 5), (2026, 9, 7)]
        recs = [FileRecord(dump, f"IMG{i}.JPG", 100 + i, noon(*day), "image")
                for i, day in enumerate(days)]
        exif = FileRecord(dump, "IMG_E.JPG", 200, noon(2026, 9, 20), "image")  # taken earlier
        bad = FileRecord(dump, "IMG_B.JPG", 201, noon(2026, 9, 7), "image")  # EXIF unusable
        video = FileRecord(dump, "C1.MP4", 300, noon(2026, 9, 8), "video")
        sorted_ = FileRecord(A + "/Photography/x", "IMG3.JPG", 103, noon(*days[3]), "image")
        idx.replace_root(A, [*recs, exif, bad, video, sorted_])
        idx.put_exif([shot(exif, taken="2026:08:30 18:00:00"),
                      shot(bad, taken="0000:00:00 00:00:00")])  # fmt: skip
        check = check_dumps(
            idx, [A], [A], DumpConfig("dump", "photography", 1), lambda d: "A", basis="b",
        )  # fmt: skip
        (f,) = check.folders
        # 09-05 is left out: that photo is in a sorted folder
        self.assertEqual(f.dates, "2026-08-30, 2026-09-01 - 2026-09-03, 2026-09-07, 2026-09-08")
