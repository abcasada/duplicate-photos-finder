import unittest

from dpf.index import Index
from dpf.models import FileRecord


def rec(dir, name, size=100, mtime=1_000_000.0, kind="image"):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind=kind)


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")

    def tearDown(self):
        self.idx.close()

    def test_replace_root_and_totals(self):
        stats = self.idx.replace_root("/a", [rec("/a", "x.jpg", 10), rec("/a/b", "y.jpg", 5)])
        self.assertEqual((stats.files, stats.bytes), (2, 15))
        self.assertEqual(self.idx.totals(["/a"]), (2, 15))
        self.assertEqual(self.idx.root_stats("/a/").files, 2)

    def test_rescan_replaces_only_that_root(self):
        self.idx.replace_root("/a", [rec("/a", "x.jpg")])
        self.idx.replace_root("/b", [rec("/b", "x.jpg")])
        self.idx.replace_root("/a", [rec("/a", "z.jpg")])
        self.assertEqual([f.name for f in self.idx.list_dir("/a")], ["z.jpg"])
        self.assertEqual(self.idx.totals(["/b"])[0], 1)

    def test_prefix_does_not_match_sibling_with_shared_prefix(self):
        self.idx.replace_root("/m/A", [rec("/m/A", "x.jpg")])
        self.idx.replace_root("/m/A-old", [rec("/m/A-old", "x.jpg")])
        self.idx.replace_root("/m/A 2", [rec("/m/A 2", "x.jpg")])
        self.assertEqual(self.idx.totals(["/m/A"])[0], 1)

    def test_paths_with_like_wildcards(self):
        self.idx.replace_root("/m/100%_done", [rec("/m/100%_done", "x.jpg")])
        self.idx.replace_root("/m/1000", [rec("/m/1000", "x.jpg")])
        self.assertEqual(self.idx.totals(["/m/100%_done"])[0], 1)

    def test_name_buckets_case_insensitive_and_by_size(self):
        self.idx.replace_root(
            "/r",
            [
                rec("/r/a", "IMG_1.JPG", 100),
                rec("/r/b", "img_1.jpg", 100),
                rec("/r/c", "IMG_1.JPG", 999),
                rec("/r/a", "solo.jpg", 100),
            ],
        )
        buckets = list(self.idx.name_buckets(["/r"]))
        self.assertEqual(len(buckets), 1)
        self.assertEqual({f.dir for f in buckets[0]}, {"/r/a", "/r/b"})
        loose = list(self.idx.name_buckets(["/r"], by_size=False))
        self.assertEqual(len(loose), 1)
        self.assertEqual(len(loose[0]), 3)

    def test_name_buckets_respect_roots(self):
        self.idx.replace_root("/r1", [rec("/r1", "a.jpg")])
        self.idx.replace_root("/r2", [rec("/r2", "a.jpg")])
        self.assertEqual(list(self.idx.name_buckets(["/r1"])), [])
        self.assertEqual(len(list(self.idx.name_buckets(["/r1", "/r2"]))), 1)

    def test_subdirs(self):
        self.idx.replace_root(
            "/r", [rec("/r/a", "1.jpg"), rec("/r/a/deep/er", "2.jpg"), rec("/r/b", "3.jpg")]
        )
        self.assertEqual(self.idx.subdirs("/r"), ["/r/a", "/r/b"])
        self.assertEqual(self.idx.subdirs("/r/a"), ["/r/a/deep"])


if __name__ == "__main__":
    unittest.main()
