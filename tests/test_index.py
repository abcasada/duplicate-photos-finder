import os
import tempfile
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

    def test_streaming_interleaved_roots(self):
        self.idx.replace_root("/a", [rec("/a", "stale.jpg")])
        self.idx.replace_root("/b", [rec("/b", "keep.jpg")])
        self.idx.begin_root("/a")
        self.idx.begin_root("/c")
        self.idx.add_records([rec("/a", "1.jpg", 10)])
        self.idx.add_records([rec("/c/x", "2.jpg", 20)])
        self.idx.add_records([rec("/a/s", "3.jpg", 30)])
        # nothing visible until finish_root
        self.assertEqual([f.name for f in self.idx.list_dir("/a")], ["stale.jpg"])
        stats = self.idx.finish_root("/a/", errors=2)
        self.assertEqual((stats.root, stats.files, stats.bytes, stats.errors), ("/a", 2, 40, 2))
        self.assertEqual([f.name for f in self.idx.list_dir("/a")], ["1.jpg"])
        self.assertEqual(self.idx.root_stats("/a").errors, 2)
        self.assertEqual(self.idx.totals(["/c"]), (0, 0))
        self.idx.finish_root("/c")
        self.assertEqual(self.idx.totals(["/c"]), (1, 20))
        self.assertEqual(self.idx.totals(["/b"]), (1, 100))

    def test_begin_root_discards_aborted_staging(self):
        self.idx.begin_root("/a")
        self.idx.add_records([rec("/a", "aborted.jpg")])
        self.idx.begin_root("/a")
        self.idx.add_records([rec("/a", "new.jpg")])
        self.assertEqual(self.idx.finish_root("/a").files, 1)
        self.assertEqual([f.name for f in self.idx.list_dir("/a")], ["new.jpg"])

    def test_finish_root_with_nothing_staged_empties_root(self):
        self.idx.replace_root("/a", [rec("/a", "gone.jpg")])
        self.idx.begin_root("/a")
        self.assertEqual(self.idx.finish_root("/a").files, 0)
        self.assertEqual(self.idx.totals(["/a"]), (0, 0))

    def test_dir_extras_and_subdirs_from_dirs_table(self):
        self.idx.replace_root(
            "/r", [rec("/r/a", "1.jpg")], dir_stats={"/r/edits": (2, 0), "/r/a": (1, 1)}
        )
        self.assertEqual(self.idx.dir_extras("/r/edits"), (2, 0))
        self.assertEqual(self.idx.dir_extras("/r/a/"), (1, 1))
        self.assertEqual(self.idx.dir_extras("/r"), (0, 0))
        self.assertEqual(self.idx.subdirs("/r"), ["/r/a", "/r/edits"])

    def test_dirs_replaced_per_root(self):
        self.idx.replace_root("/a", [], dir_stats={"/a/x": (1, 0), "/a/y": (0, 1)})
        self.idx.replace_root("/b", [], dir_stats={"/b": (3, 0)})
        self.idx.begin_root("/a")
        self.idx.finish_root("/a", dir_stats={"/a/z": (0, 2)})
        self.assertEqual(self.idx.dir_extras("/a/x"), (0, 0))
        self.assertEqual(self.idx.dir_extras("/a/z"), (0, 2))
        self.assertEqual(self.idx.subdirs("/a"), ["/a/z"])
        self.idx.replace_root("/a", [])
        self.assertEqual(self.idx.subdirs("/a"), [])
        self.assertEqual(self.idx.dir_extras("/b"), (3, 0))

    def test_roots_and_containing_root(self):
        self.idx.replace_root("/m/B", [rec("/m/B", "x.jpg")], errors=3)
        self.idx.replace_root("/m/A", [rec("/m/A/s", "x.jpg", 7)])
        self.assertEqual([r.root for r in self.idx.roots()], ["/m/A", "/m/B"])
        self.assertEqual(self.idx.roots()[1].errors, 3)
        self.assertEqual(self.idx.containing_root("/m/A/s/deep").root, "/m/A")
        self.assertEqual(self.idx.containing_root("/m/A/").root, "/m/A")
        self.assertIsNone(self.idx.containing_root("/m/A-old"))
        self.assertIsNone(self.idx.containing_root("/m"))

    def test_indexed(self):
        self.idx.replace_root("/m/A", [rec("/m/A/s", "x.jpg")])
        self.idx.replace_root("/m/empty", [])
        for path in ("/m/A", "/m/A/s", "/m/empty", "/m"):
            self.assertTrue(self.idx.indexed(path), path)
        for path in ("/m/A/t", "/m/A-old", "/n"):
            self.assertFalse(self.idx.indexed(path), path)

    def test_rescan_supersedes_nested_roots(self):
        self.idx.replace_root("/m/A/s", [rec("/m/A/s", "x.jpg")])
        self.idx.replace_root("/m/A-old", [rec("/m/A-old", "x.jpg")])
        self.idx.begin_root("/m/A")
        self.idx.finish_root("/m/A")
        self.assertEqual([r.root for r in self.idx.roots()], ["/m/A", "/m/A-old"])

    def test_forget_root(self):
        self.idx.replace_root(
            "/m/A", [rec("/m/A", "1.jpg"), rec("/m/A/s", "2.jpg")], dir_stats={"/m/A/s": (1, 0)}
        )
        self.idx.replace_root("/m/A 2", [rec("/m/A 2", "1.jpg")], dir_stats={"/m/A 2": (1, 0)})
        self.idx.begin_root("/m/A")
        self.idx.add_records([rec("/m/A", "staged.jpg")])
        self.assertEqual(self.idx.forget_root("/m/A/"), 2)
        self.assertEqual([r.root for r in self.idx.roots()], ["/m/A 2"])
        self.assertEqual(self.idx.totals(["/m/A"]), (0, 0))
        self.assertEqual(self.idx.dir_extras("/m/A/s"), (0, 0))
        self.assertEqual(self.idx.conn.execute("SELECT count(*) FROM staging").fetchone()[0], 0)
        self.assertEqual(self.idx.totals(["/m/A 2"])[0], 1)
        self.assertEqual(self.idx.dir_extras("/m/A 2"), (1, 0))
        self.assertEqual(self.idx.forget_root("/m/A"), 0)


class IndexFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bare_filename(self):
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            with Index("index.db") as idx:
                idx.replace_root("/a", [rec("/a", "x.jpg")])
        finally:
            os.chdir(cwd)
        self.assertEqual(idx.path, os.path.join(os.path.realpath(self.tmp.name), "index.db"))
        with Index(idx.path) as idx:
            self.assertEqual(idx.totals(["/a"])[0], 1)

    def test_older_inventory_dropped(self):
        path = os.path.join(self.tmp.name, "index.db")
        with Index(path) as idx:
            idx.replace_root("/a", [rec("/a", "x.jpg")])
            idx.conn.execute("PRAGMA user_version = 1")
        with Index(path) as idx:
            self.assertEqual(idx.totals(["/a"])[0], 0)
            self.assertIsNone(idx.root_stats("/a"))


if __name__ == "__main__":
    unittest.main()
