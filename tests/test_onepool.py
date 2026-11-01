import os
import shutil
import tempfile
import time
import unittest

from dpf.categories import Categories
from dpf.coverage import NOT_BACKED_UP, ONLY_IN_BACKUP, check_coverage
from dpf.index import Index
from dpf.models import FileRecord
from dpf.onepool import build_only_in_one, format_only

T = time.mktime((2024, 5, 1, 12, 0, 0, 0, 0, -1))
DAY = 86400
A1, B1 = "/d/Storage 1A", "/d/Storage 1B"


def rec(dir, name, size=100, mtime=T, kind="image"):
    return FileRecord(dir, name, size, mtime, kind)


class OnlyInOneTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.idx.replace_root(A1, [
            rec(A1 + "/p", "both.jpg"),
            rec(A1 + "/p", "a1.jpg", 300), rec(A1 + "/p", "a2.jpg", 300, T + DAY),
            rec(A1 + "/p", "a3.jpg", 300, T + 2 * DAY), rec(A1 + "/p", "v.mp4", 900, kind="video"),
            rec(A1 + "/Plex", "film.mkv", 5000, kind="video"),
            rec(A1 + "/q", "re.dng", 1000),  # re-saved in B: same name, 10 B larger
        ])  # fmt: skip
        self.idx.replace_root(B1, [
            rec(B1 + "/p", "both.jpg"), rec(B1 + "/x", "b.jpg", 50),
            rec(B1 + "/q", "re.dng", 1010, T + 600),
        ])  # fmt: skip

    def only(self, cats):
        covs = [
            check_coverage(self.idx, [A1], [B1], pool="A", other="B", direction=NOT_BACKED_UP),
            check_coverage(self.idx, [B1], [A1], pool="B", other="A", direction=ONLY_IN_BACKUP),
        ]
        return build_only_in_one(self.idx, covs, cats, "name")

    def test_personal_folders_both_ways(self):
        only = self.only(Categories([A1 + "/Plex"]))
        self.assertTrue(only.personal_only)
        rows = [(f.pool, f.dir, f.photos, f.videos, f.bytes, f.total, f.near)
                for f in only.folders]  # fmt: skip
        self.assertEqual(rows, [
            ("A", A1 + "/p", 3, 1, 1800, 5, 0),
            ("B", B1 + "/q", 1, 0, 1010, 1, 1),  # largest first, both pools mixed
            ("A", A1 + "/q", 1, 0, 1000, 1, 1),
            ("B", B1 + "/x", 1, 0, 50, 1, 0),
        ])  # fmt: skip
        self.assertEqual(only.folders[0].dates, "2024-05-01 - 2024-05-03")
        a, b = only.sides
        self.assertEqual((a.pool, a.other, a.files, a.bytes, a.near_bytes), ("A", "B", 5, 2800, 1000))
        self.assertEqual((b.files, b.bytes), (2, 1060))
        self.assertEqual(dict(a.drives), {A1: 2800})
        text = format_only(only, top=10)
        self.assertIn("Only in pool A (no copy anywhere in B): 5 files", text)
        self.assertIn("1 likely re-saved in B", text)

    def test_without_categories_all_media_counts(self):
        only = self.only(Categories())
        self.assertFalse(only.personal_only)
        self.assertEqual(only.sides[0].bytes, 2800 + 5000)

    def test_needs_both_directions(self):
        cov = check_coverage(self.idx, [A1], [B1], pool="A", other="B", direction=NOT_BACKED_UP)
        self.assertIsNone(build_only_in_one(self.idx, [cov], Categories(), "name"))


if __name__ == "__main__":
    unittest.main()
