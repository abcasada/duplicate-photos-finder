import os
import tempfile
import threading
import unittest
from unittest import mock

from dpf import scanner
from dpf.index import Index
from dpf.scanner import normalize_roots, scan_roots, walk


def touch(path, data=b"", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.root, *parts)


class NormalizeRootsTest(TempTree):
    def test_dedupes_sorts_and_drops_nested(self):
        for d in ("b/inner", "a", "a-sibling"):
            os.makedirs(self.p(d))
        roots = normalize_roots(
            [self.p("b"), self.p("b/inner"), self.p("a") + "/", self.p("a"), self.p("a-sibling")]
        )
        self.assertEqual(roots, [self.p("a"), self.p("a-sibling"), self.p("b")])

    def test_resolves_symlinks(self):
        os.makedirs(self.p("real"))
        os.symlink(self.p("real"), self.p("link"))
        self.assertEqual(normalize_roots([self.p("link"), self.p("real")]), [self.p("real")])

    def test_rejects_missing_and_files(self):
        touch(self.p("f.jpg"))
        with self.assertRaisesRegex(ValueError, "not an existing directory"):
            normalize_roots([self.p("missing")])
        with self.assertRaisesRegex(ValueError, "not an existing directory"):
            normalize_roots([self.p("f.jpg")])


class WalkTest(TempTree):
    def names(self, **kw):
        return sorted(r.path[len(self.root) + 1:] for r in walk(self.root, **kw))

    def test_records_fields(self):
        touch(self.p("sub", "IMG_1.JPG"), b"12345", mtime=1_500_000_000.5)
        touch(self.p("clip.MOV"), b"1")
        recs = {r.name: r for r in walk(self.root + "/")}
        r = recs["IMG_1.JPG"]
        self.assertEqual((r.dir, r.size, r.mtime, r.kind), (self.p("sub"), 5, 1_500_000_000.5, "image"))
        self.assertEqual((recs["clip.MOV"].dir, recs["clip.MOV"].kind), (self.root, "video"))

    def test_filters_non_media_unless_all_files(self):
        touch(self.p("a.jpg"))
        touch(self.p("notes.txt"))
        touch(self.p("noext"))
        self.assertEqual(self.names(), ["a.jpg"])
        recs = {r.name: r.kind for r in walk(self.root, all_files=True)}
        self.assertEqual(recs, {"a.jpg": "image", "notes.txt": "other", "noext": "other"})

    def test_skips_skip_dirs_and_appledouble(self):
        touch(self.p("$RECYCLE.BIN", "x.jpg"))
        touch(self.p("System Volume Information", "x.jpg"))
        touch(self.p("deep", "@eaDir", "x.jpg"))
        touch(self.p("deep", "._x.jpg"))
        touch(self.p("deep", "x.jpg"))
        self.assertEqual(self.names(all_files=True), ["deep/x.jpg"])

    def test_symlinks_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        touch(os.path.join(outside.name, "out.jpg"))
        touch(self.p("real", "in.jpg"))
        os.symlink(outside.name, self.p("dirlink"))
        os.symlink(self.p("real"), self.p("loop"))
        os.symlink(self.p("real", "in.jpg"), self.p("filelink.jpg"))
        os.symlink(self.p("nowhere.jpg"), self.p("broken.jpg"))
        self.assertEqual(self.names(all_files=True), ["real/in.jpg"])

    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 directories")
    def test_unreadable_dir_recorded_and_walk_continues(self):
        touch(self.p("ok", "a.jpg"))
        touch(self.p("locked", "b.jpg"))
        os.chmod(self.p("locked"), 0)
        try:
            errors = []
            names = sorted(r.name for r in walk(self.root, errors=errors))
        finally:
            os.chmod(self.p("locked"), 0o755)
        self.assertEqual(names, ["a.jpg"])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(self.p("locked") + ": "), errors[0])

    def test_unreadable_root_recorded(self):
        errors = []
        self.assertEqual(list(walk(self.p("missing"), errors=errors)), [])
        self.assertEqual(len(errors), 1)

    def test_undecodable_name_recorded_not_yielded(self):
        bad = os.fsdecode(b"bad\xff.jpg")
        try:
            touch(self.p(bad))
        except (OSError, UnicodeError):
            self.skipTest("filesystem rejects non-UTF-8 names")
        touch(self.p("good.jpg"))
        errors = []
        self.assertEqual([r.name for r in walk(self.root, errors=errors)], ["good.jpg"])
        self.assertEqual(len(errors), 1)


class ScanRootsTest(TempTree):
    def setUp(self):
        super().setUp()
        self.idx = Index(":memory:")

    def tearDown(self):
        self.idx.close()
        super().tearDown()

    def test_multiple_roots_and_progress(self):
        touch(self.p("r1", "a.jpg"), b"xx")
        touch(self.p("r1", "s", "b.jpg"), b"xxx")
        touch(self.p("r2", "c.mp4"), b"x")
        roots = normalize_roots([self.p("r2"), self.p("r1")])
        calls = []
        stats = scan_roots(self.idx, roots, progress=lambda *a: calls.append(a))
        self.assertEqual([(s.root, s.files, s.bytes, s.errors) for s in stats],
                         [(self.p("r1"), 2, 5, 0), (self.p("r2"), 1, 1, 0)])
        self.assertEqual(self.idx.totals(roots), (3, 6))
        self.assertIn((self.p("r1"), 2, 5), calls)
        self.assertIn((self.p("r2"), 1, 1), calls)

    def test_rescan_removes_stale_entries(self):
        touch(self.p("r", "old.jpg"))
        touch(self.p("r", "sub", "keep.jpg"))
        touch(self.p("other", "x.jpg"))
        scan_roots(self.idx, [self.p("r"), self.p("other")])
        os.remove(self.p("r", "old.jpg"))
        touch(self.p("r", "new.jpg"))
        scan_roots(self.idx, [self.p("r")])
        rows = self.idx.conn.execute("SELECT dir, name FROM files ORDER BY dir, name").fetchall()
        self.assertEqual(rows, [(self.p("other"), "x.jpg"), (self.p("r"), "new.jpg"),
                                (self.p("r", "sub"), "keep.jpg")])

    def test_errors_counted(self):
        touch(self.p("r", "a.jpg"))
        errors = []
        with mock.patch.object(scanner.os, "scandir", side_effect=PermissionError(13, "Permission denied")):
            stats = scan_roots(self.idx, [self.p("r")], errors=errors)
        self.assertEqual(stats[0].errors, 1)
        self.assertEqual(self.idx.root_stats(self.p("r")).errors, 1)
        self.assertEqual(errors, [self.p("r") + ": Permission denied"])

    def test_one_thread_per_device(self):
        for r in ("r1", "r2", "r3"):
            touch(self.p(r, "a.jpg"))
        seen = []
        real_walk = scanner.walk

        def spy(root, **kw):
            seen.append(threading.current_thread().name)
            return real_walk(root, **kw)

        with mock.patch.object(scanner, "walk", spy):
            stats = scan_roots(self.idx, [self.p("r1"), self.p("r2"), self.p("r3")])
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual([s.files for s in stats], [1, 1, 1])

    def test_many_files_batched(self):
        for i in range(25):
            touch(self.p("r", f"d{i % 3}", f"{i}.jpg"))
        with mock.patch.object(scanner, "BATCH_SIZE", 4):
            stats = scan_roots(self.idx, [self.p("r")])
        self.assertEqual(stats[0].files, 25)
        self.assertEqual(self.idx.totals([self.p("r")])[0], 25)

    def test_worker_exception_propagates_and_keeps_old_contents(self):
        touch(self.p("r", "a.jpg"))
        scan_roots(self.idx, [self.p("r")])

        def boom(root, **kw):
            raise RuntimeError("disk on fire")
            yield

        with mock.patch.object(scanner, "walk", boom):
            with self.assertRaisesRegex(RuntimeError, "disk on fire"):
                scan_roots(self.idx, [self.p("r")])
        self.assertEqual(self.idx.totals([self.p("r")])[0], 1)


if __name__ == "__main__":
    unittest.main()
