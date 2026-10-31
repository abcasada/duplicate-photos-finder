import csv
import os
import shutil
import tempfile
import unittest

from dpf.folders import FIELDS, HERE, write_folders_csv
from dpf.index import Index
from dpf.models import FileRecord


class FoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.root = "/drives/Storage 1A"
        r = self.root
        files = [
            FileRecord(r, "loose.jpg", 10, 0, "image"),
            FileRecord(f"{r}/Photos", "a.jpg", 100, 1_600_000_000, "image"),
            FileRecord(f"{r}/Photos/2019", "IMG_1.ARW", 1000, 1_560_000_000, "image"),
            FileRecord(f"{r}/Photos/2019/Hawaii", "IMG_2.ARW", 1000, 1_570_000_000, "image"),
            FileRecord(f"{r}/Photos/2019/Hawaii", "C0001.MP4", 5000, 1_570_000_000, "video"),
            FileRecord(f"{r}/Photos/2020", "b.jpg", 50, 1_590_000_000, "image"),
            FileRecord(f"{r}/Plex", "film.mkv", 9000, 1_500_000_000, "video"),
        ]
        self.idx.replace_root(r, files, dir_stats={f"{r}/Plex": (3, 0), f"{r}/Apps": (7, 0), r: (2, 0)})

    def rows(self):
        path = os.path.join(self.tmp, "folders.csv")
        n = write_folders_csv(self.idx, [self.root], path, lambda root: "A")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], list(FIELDS))
        self.assertEqual(n, len(rows) - 1)
        return [dict(zip(FIELDS, row)) for row in rows[1:]]

    def test_two_levels_with_subtree_totals(self):
        rows = self.rows()
        self.assertEqual(
            [(r["Folder"], r["Subfolder"]) for r in rows],
            [(HERE, ""), ("Photos", ""), ("Photos", HERE), ("Photos", "2019"),
             ("Photos", "2020"), ("Plex", "")],
        )  # fmt: skip
        photos, here, y2019, _, plex = rows[1:]
        self.assertEqual((photos["Media files"], photos["Photos"], photos["Videos"]), ("5", "4", "1"))
        self.assertEqual(photos["Folders inside"], "3")
        self.assertEqual(here["Media files"], "1")
        self.assertEqual(y2019["Media files"], "3")
        self.assertEqual(y2019["Folders inside"], "1")
        self.assertEqual(y2019["Main types"], "ARW 2, MP4 1")
        self.assertEqual(y2019["Path"], f"{self.root}/Photos/2019")
        self.assertEqual((plex["Other files"], plex["Media size (GB)"]), ("3", "0.00"))
        self.assertTrue(all(r["Pool"] == "A" and r["Drive"] == "Storage 1A" for r in rows))
        self.assertTrue(all(r["Personal?"] == "" and r["Notes"] == "" for r in rows))

    def test_no_rows_for_folders_without_media(self):
        self.idx.replace_root(self.root, [], dir_stats={f"{self.root}/Apps/x": (7, 0)})
        self.assertEqual(self.rows(), [])

    def test_no_here_row_without_subfolders(self):
        self.assertNotIn(("Plex", HERE), [(r["Folder"], r["Subfolder"]) for r in self.rows()])
