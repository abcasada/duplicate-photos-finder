import os
import shutil
import tempfile
import time
import unittest

from dpf.categories import Categories
from dpf.index import EXIF_COLUMNS, Index
from dpf.match_name import find_name_duplicates
from dpf.models import FileRecord
from dpf.months import UNKNOWN, format_months, month_totals

R = "/d/Storage 1A"


def ts(y, m, d=15):
    return time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))


def exif(dir, name, size, mtime, taken):
    row = dict.fromkeys(EXIF_COLUMNS)
    row.update(dir=dir, name=name, read_size=size, read_mtime=mtime, status="none", taken=taken,
               read_at=0.0)
    return [row[c] for c in EXIF_COLUMNS]


class MonthTotalsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.files = [
            FileRecord(R + "/a", "x.jpg", 100, ts(2020, 1), "image"),
            FileRecord(R + "/b", "x.jpg", 100, ts(2020, 1), "image"),  # duplicate of a/x
            FileRecord(R + "/a", "v.mp4", 5000, ts(2020, 3), "video"),
            FileRecord(R + "/a", "old.jpg", 7, ts(1985, 6), "image"),  # before 1990
            FileRecord(R + "/a", "e.jpg", 40, ts(2024, 5), "image"),  # EXIF says 2019-12
            FileRecord(R + "/Plex", "m.mkv", 9000, ts(2020, 1), "video"),
            FileRecord(R + "/a", "notes.txt", 3, ts(2020, 1), "other"),
            FileRecord(R + "/a", "empty.jpg", 0, ts(2020, 1), "image"),
        ]
        self.idx.replace_root(R, self.files)
        self.idx.put_exif([exif(R + "/a", "e.jpg", 40, ts(2024, 5), "2019:12:31 23:59:00")])
        self.groups = list(find_name_duplicates(self.idx, [R]))

    def test_months_count_duplicates_once_and_prefer_capture_time(self):
        p = month_totals(self.idx, "A", [R], self.groups)
        months = {m.month: m for m in p.months}
        self.assertEqual([m.month for m in p.months][:2], ["2019-12", "2020-01"])
        self.assertEqual(p.months[-1].month, "2020-03")
        self.assertEqual((months["2019-12"].photos, months["2019-12"].photo_bytes), (1, 40))
        jan = months["2020-01"]
        self.assertEqual((jan.photos, jan.photo_bytes, jan.videos, jan.video_bytes),
                         (1, 100, 1, 9000))  # fmt: skip
        self.assertEqual(months["2020-02"].files, 0)  # gaps are filled
        self.assertEqual((months["2020-03"].videos, months["2020-03"].video_bytes), (1, 5000))
        self.assertEqual((p.unknown.month, p.unknown.photos), (UNKNOWN, 1))
        self.assertEqual((p.from_exif, p.from_mtime), (1, 4))
        self.assertEqual(p.total.bytes, 40 + 100 + 9000 + 5000 + 7)

    def test_categories_leave_other_media_out(self):
        cats = Categories([R + "/Plex"])
        p = month_totals(self.idx, "A", [R], self.groups, cats)
        self.assertTrue(p.personal_only)
        jan = {m.month: m for m in p.months}["2020-01"]
        self.assertEqual(jan.videos, 0)

    def test_stale_exif_is_ignored(self):
        self.idx.replace_root(R, [FileRecord(R + "/a", "e.jpg", 41, ts(2024, 5), "image")])
        p = month_totals(self.idx, "A", [R], [])
        self.assertEqual([m.month for m in p.months], ["2024-05"])

    def test_format(self):
        text = format_months([month_totals(self.idx, "A", [R], self.groups)])
        self.assertIn("Pool A: 5 files", text)
        self.assertIn("2020-01", text)
        self.assertNotIn("2020-02", text)  # empty months are left out of the text
        self.assertIn("unknown", text)


if __name__ == "__main__":
    unittest.main()
