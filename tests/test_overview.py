import os
import shutil
import tempfile
import unittest

from dpf.categories import Categories
from dpf.coverage import NOT_BACKED_UP, ONLY_IN_BACKUP, check_coverage
from dpf.index import Index
from dpf.match_name import find_name_duplicates
from dpf.models import DriveSpace, FileRecord
from dpf.overview import PoolInput, build_overview, format_overview

T = 1_600_000_000
A1, A2, B1 = "/d/Storage 1A", "/d/Storage 2A", "/d/Storage 1B"


def rec(dir, name, size, kind="image"):
    return FileRecord(dir, name, size, T, kind)


class OverviewTest(unittest.TestCase):
    """x is on A1, A2 and B1; y only on A1; z twice on A1; w only on B1; v twice on B1."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.idx.replace_root(A1, [
            rec(A1 + "/p", "x.jpg", 1000), rec(A1 + "/p", "y.jpg", 200),
            rec(A1 + "/d1", "z.jpg", 30), rec(A1 + "/d2", "z.jpg", 30),
            rec(A1 + "/Plex", "m.mkv", 9000, "video"),
        ], space=DriveSpace(used=20_000, capacity=50_000, is_mount=True))  # fmt: skip
        self.idx.replace_root(A2, [rec(A2 + "/q", "x.jpg", 1000)])
        self.idx.replace_root(B1, [
            rec(B1 + "/p", "x.jpg", 1000), rec(B1 + "/n", "w.jpg", 4),
            rec(B1 + "/e1", "v.jpg", 5), rec(B1 + "/e2", "v.jpg", 5),
        ])  # fmt: skip

    def overview(self, cats):
        pools = [
            PoolInput("A", [A1, A2], list(find_name_duplicates(self.idx, [A1, A2]))),
            PoolInput("B", [B1], list(find_name_duplicates(self.idx, [B1]))),
        ]
        cov = [
            check_coverage(self.idx, [A1, A2], [B1], pool="A", other="B",
                           direction=NOT_BACKED_UP),
            check_coverage(self.idx, [B1], [A1, A2], pool="B", other="A",
                           direction=ONLY_IN_BACKUP),
        ]  # fmt: skip
        return build_overview(self.idx, pools, cov, cats)

    def test_personal_unique_and_only_copy(self):
        ov = self.overview(Categories([A1 + "/Plex"]))
        a, b = ov.pools
        self.assertEqual((a.files, a.bytes), (5, 2260))
        self.assertEqual((a.unique_files, a.unique_bytes), (3, 1230))  # x, y, z once
        self.assertEqual((b.files, b.bytes), (4, 1014))
        self.assertEqual((b.unique_files, b.unique_bytes), (3, 1009))
        self.assertEqual(ov.all_unique, (5, 1239))  # A's x, y, z plus B's w, v
        drives = {d.root: d for d in ov.drives}
        self.assertEqual((drives[A1].sole_files, drives[A1].sole_bytes), (2, 230))  # y, z
        self.assertEqual((drives[A2].sole_files, drives[A2].sole_bytes), (0, 0))
        self.assertEqual((drives[B1].sole_files, drives[B1].sole_bytes), (2, 9))  # w, v
        self.assertEqual(ov.sole, (4, 239))
        self.assertEqual(ov.sole_similar, 0)
        d = drives[A1]
        self.assertEqual((d.photos, d.videos, d.media_bytes), (4, 1, 10260))
        self.assertEqual((d.personal_bytes, d.other_bytes), (1260, 9000))
        self.assertEqual((d.capacity, d.used, d.free, d.not_media), (50_000, 20_000, 30_000, 9740))
        self.assertIsNone(drives[A2].capacity)

    def test_only_copy_with_same_name_in_other_pool(self):
        self.idx.replace_root(B1, [rec(B1 + "/p", "x.jpg", 1000), rec(B1 + "/p", "y.jpg", 201)])
        ov = self.overview(Categories([A1 + "/Plex"]))
        a1 = next(d for d in ov.drives if d.root == A1)
        self.assertEqual((a1.sole_bytes, a1.sole_similar), (230, 200))  # y re-saved in B
        self.assertIn("of which 401 B have a same-name file", format_overview(ov))  # y both ways

    def test_without_categories_everything_counts(self):
        ov = self.overview(Categories())
        self.assertEqual((ov.pools[0].files, ov.pools[0].unique_files), (6, 4))
        self.assertIsNone(ov.drives[0].personal_bytes)

    def test_text(self):
        text = format_overview(self.overview(Categories([A1 + "/Plex"])))
        self.assertRegex(text, r"Storage 1A +A +50.0 kB +20.0 kB +30.0 kB +4 +1 +10.3 kB")
        self.assertIn("Pool A total", text)
        self.assertIn("All drives", text)
        self.assertIn("50.0 kB+", text)  # A2 has no space recorded
        self.assertIn("  All drives:  unique: 5 files, 1.2 kB (a file found in both A and B "
                      "counts once)", text)  # fmt: skip
        self.assertIn("  Only copy:   4 files, 239 B exist on one drive only", text)

    def test_non_media_files_are_left_out(self):
        notes = [rec(A1 + "/p", "notes.txt", 7, "other"), rec(A2 + "/q", "notes.txt", 7, "other")]
        self.idx.replace_root(A1, [rec(A1 + "/p", "y.jpg", 200), notes[0]])
        self.idx.replace_root(A2, [notes[1]])
        a = self.overview(Categories()).pools[0]
        self.assertEqual((a.files, a.unique_files), (1, 1))
        a = self.overview(Categories([A1 + "/Plex"])).pools[0]
        self.assertEqual((a.files, a.bytes, a.unique_files), (1, 200, 1))

    def test_same_drive_under_two_labels_gets_no_only_copy(self):
        twin = "/d/Storage 1A (2)"
        self.idx.replace_root(twin, [rec(twin + "/p", "y.jpg", 200)])
        pools = [PoolInput("A", [A1, twin], list(find_name_duplicates(self.idx, [A1, twin])))]
        cov = [check_coverage(self.idx, [A1, twin], [B1], pool="A", other="B",
                              direction=NOT_BACKED_UP)]  # fmt: skip
        ov = build_overview(self.idx, pools, cov, Categories())
        self.assertIsNone(ov.sole)
        self.assertTrue(all(d.sole_files is None for d in ov.drives))
