import os
import sqlite3
import tempfile
import unittest
from unittest import mock

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

    def test_name_buckets_similar_and_exact_names(self):
        self.idx.replace_root(
            "/r",
            [
                rec("/r/a", "DSC01234.ARW"),
                rec("/r/b", "DSC01234 (1).ARW"),
                rec("/r/c", "2023-08-26_DSC01234.ARW"),
                rec("/r/d", "DSC01234.JPG"),
                rec("/r/e", "DSC01234-2.ARW", 999),
            ],
        )
        (bucket,) = self.idx.name_buckets(["/r"])
        self.assertEqual({f.dir for f in bucket}, {"/r/a", "/r/b", "/r/c"})
        self.assertEqual(list(self.idx.name_buckets(["/r"], exact_names=True)), [])
        (loose,) = self.idx.name_buckets(["/r"], by_size=False)
        self.assertEqual(len(loose), 4)

    def test_match_key_stored_for_staged_records(self):
        self.idx.begin_root("/r")
        self.idx.add_records([rec("/r/a", "x (1).jpg"), rec("/r/b", "x.jpg")])
        self.idx.finish_root("/r")
        keys = self.idx.conn.execute("SELECT DISTINCT match_key FROM files").fetchall()
        self.assertEqual(keys, [("x.jpg",)])

    def test_name_buckets_skip_empty_files(self):
        self.idx.replace_root("/r", [rec("/r/a", "x.jpg", 0), rec("/r/b", "x.jpg", 0)])
        self.assertEqual(list(self.idx.name_buckets(["/r"])), [])
        self.assertEqual(list(self.idx.name_buckets(["/r"], by_size=False)), [])

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

    def test_migrates_version_2_in_place(self):
        path = os.path.join(self.tmp.name, "index.db")
        conn = sqlite3.connect(path)
        conn.executescript(_V2_SCHEMA)
        conn.executemany(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("/m/A/x", "DSC01234.ARW", "dsc01234.arw", 100, 1.0, "image"),
                ("/m/A/y", "DSC01234 (1).ARW", "dsc01234 (1).arw", 100, 1.5, "image"),
                ("/m/B", "Copy of IMG_0001.jpg", "copy of img_0001.jpg", 7, 2.0, "image"),
            ],
        )
        conn.execute(
            "INSERT INTO staging VALUES ('/m/C', 'a - Copy.jpg', 'a - copy.jpg', 1, 1.0, 'image')"
        )
        conn.execute("INSERT INTO dirs VALUES ('/m/A/x', 2, 1)")
        conn.execute("INSERT INTO roots VALUES ('/m/A', 2, 200, 0, 123.0)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        with Index(path) as idx:
            self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(idx.totals(["/m"]), (3, 207))
            self.assertEqual(idx.root_stats("/m/A").scanned_at, 123.0)
            self.assertEqual(idx.dir_extras("/m/A/x"), (2, 1))
            keys = dict(idx.conn.execute("SELECT name, match_key FROM files"))
            self.assertEqual(
                keys,
                {
                    "DSC01234.ARW": "dsc01234.arw",
                    "DSC01234 (1).ARW": "dsc01234.arw",
                    "Copy of IMG_0001.jpg": "img_0001.jpg",
                },
            )
            staged = idx.conn.execute("SELECT match_key FROM staging").fetchall()
            self.assertEqual(staged, [("a.jpg",)])
            indexes = {r[1] for r in idx.conn.execute("PRAGMA index_list(files)")}
            self.assertIn("files_match_size", indexes)
            (bucket,) = idx.name_buckets(["/m"])
            self.assertEqual(len(bucket), 2)
            idx.finish_root("/m/C")
            idx.replace_root("/m/D", [rec("/m/D", "x (2).jpg")])
            self.assertEqual(idx.totals(["/m"])[0], 5)
        with Index(path) as idx:  # reopening is a no-op
            self.assertEqual(idx.totals(["/m"])[0], 5)

    def test_migrates_version_3_match_keys_in_place(self):
        """v4 recomputes match_key: other text around a camera number is now kept."""
        path = os.path.join(self.tmp.name, "index.db")
        with Index(path) as idx:
            idx.replace_root("/m", [rec("/m/a", "BodyA_DSC01234.ARW"), rec("/m/b", "x.jpg")])
            idx.begin_root("/s")
            idx.add_records([rec("/s", "Trip DSC00001.ARW")])
            for table in ("files", "staging"):
                idx.conn.execute(f"UPDATE {table} SET match_key = 'stale'")
            idx.conn.execute("PRAGMA user_version = 3")
            idx.conn.commit()
        with Index(path) as idx:
            self.assertEqual(idx.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            keys = dict(idx.conn.execute("SELECT name, match_key FROM files"))
            self.assertEqual(keys, {"BodyA_DSC01234.ARW": "bodya_dsc01234.arw", "x.jpg": "x.jpg"})
            staged = idx.conn.execute("SELECT match_key FROM staging").fetchall()
            self.assertEqual(staged, [("trip dsc00001.arw",)])
            self.assertEqual(idx.root_stats("/m").files, 2)

    def test_failed_migration_leaves_version_2_untouched(self):
        path = os.path.join(self.tmp.name, "index.db")
        conn = sqlite3.connect(path)
        conn.executescript(_V2_SCHEMA)
        conn.execute("INSERT INTO files VALUES ('/a', 'x.jpg', 'x.jpg', 1, 1.0, 'image')")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        with mock.patch.object(Index, "_refill_match_keys", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                Index(path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        self.assertNotIn("match_key", columns)
        conn.close()
        with Index(path) as idx:
            self.assertEqual(idx.totals(["/a"])[0], 1)

    def test_older_inventory_dropped(self):
        path = os.path.join(self.tmp.name, "index.db")
        with Index(path) as idx:
            idx.replace_root("/a", [rec("/a", "x.jpg")])
            idx.conn.execute("PRAGMA user_version = 1")
        with Index(path) as idx:
            self.assertEqual(idx.totals(["/a"])[0], 0)
            self.assertIsNone(idx.root_stats("/a"))


# The schema of user_version 2, before the match_key column.
_V2_SCHEMA = """
CREATE TABLE roots (
    path TEXT PRIMARY KEY, files INTEGER NOT NULL, bytes INTEGER NOT NULL,
    errors INTEGER NOT NULL, scanned_at REAL NOT NULL
);
CREATE TABLE files (
    dir TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL, size INTEGER NOT NULL,
    mtime REAL NOT NULL, kind TEXT NOT NULL, PRIMARY KEY (dir, name)
) WITHOUT ROWID;
CREATE INDEX files_name_size ON files (name_key, size);
CREATE TABLE staging (
    dir TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL, size INTEGER NOT NULL,
    mtime REAL NOT NULL, kind TEXT NOT NULL, PRIMARY KEY (dir, name)
) WITHOUT ROWID;
CREATE TABLE dirs (
    dir TEXT PRIMARY KEY, other INTEGER NOT NULL, errors INTEGER NOT NULL
) WITHOUT ROWID;
"""


if __name__ == "__main__":
    unittest.main()
