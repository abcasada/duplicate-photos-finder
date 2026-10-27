import unittest

from dpf.coverage import (
    NOT_BACKED_UP,
    ONLY_IN_BACKUP,
    basis_words,
    check_coverage,
    find_missing,
)
from dpf.index import Index
from dpf.models import FileRecord

T = 1_600_000_000.0


def rec(dir, name, size=100, mtime=T):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind="image")


class FindMissingTest(unittest.TestCase):
    """Pool A is everything under /a, pool B everything under /b."""

    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def missing(self, a, b, **kw):
        self.idx.replace_root("/a", a)
        self.idx.replace_root("/b", b)
        missing, totals, similar = find_missing(self.idx, ["/a"], ["/b"], **kw)
        self.similar = similar
        self.totals = totals
        return [f.path for f in missing]

    def test_copy_anywhere_in_other_pool(self):
        a = [rec("/a/DCIM", "x.jpg"), rec("/a/DCIM", "y.jpg", 200)]
        b = [rec("/b/Backup/2020/deep", "x.jpg")]
        self.assertEqual(self.missing(a, b), ["/a/DCIM/y.jpg"])
        self.assertEqual(self.similar, set())
        self.assertEqual(self.totals, {"/a/DCIM": 2})

    def test_size_must_match(self):
        a = [rec("/a/d", "x.dng", 100)]
        b = [rec("/b/d", "x.dng", 108)]
        self.assertEqual(self.missing(a, b), ["/a/d/x.dng"])
        self.assertEqual(self.similar, {"/a/d/x.dng"})  # edited version?
        self.assertEqual(self.missing(a, b, ignore_size=True), [])

    def test_mtime_tolerance(self):
        a = [rec("/a/d", "x.jpg"), rec("/a/d", "y.jpg")]
        b = [rec("/b/d", "x.jpg", mtime=T + 2), rec("/b/d", "y.jpg", mtime=T + 2.5)]
        self.assertEqual(self.missing(a, b), ["/a/d/y.jpg"])
        self.assertEqual(self.similar, {"/a/d/y.jpg"})
        self.assertEqual(self.missing(a, b, mtime_tolerance=3), [])
        self.assertEqual(self.missing(a, b, mtime_tolerance=0), ["/a/d/x.jpg", "/a/d/y.jpg"])
        self.assertEqual(self.missing(a, b, ignore_mtime=True), [])

    def test_hour_offset_is_missing(self):
        """Copies whose mtime moved by a time-zone hour are not matched (reported missing)."""
        a = [rec("/a/d", "x.jpg")]
        b = [rec("/b/d", "x.jpg", mtime=T - 3600)]
        self.assertEqual(self.missing(a, b), ["/a/d/x.jpg"])

    def test_similar_and_exact_names(self):
        a = [rec("/a/d", "DSC00001.ARW"), rec("/a/d", "2023-08-26_DSC00002.ARW", 101)]
        b = [rec("/b/d", "DSC00001 (1).ARW"), rec("/b/d", "DSC00002.ARW", 101)]
        self.assertEqual(self.missing(a, b), [])
        self.assertEqual(
            self.missing(a, b, exact_names=True),
            ["/a/d/2023-08-26_DSC00002.ARW", "/a/d/DSC00001.ARW"],
        )
        self.assertEqual(self.similar, set())

    def test_copy_numbers(self):
        """X (1) never counts as a copy of X (2); X and X (1) are copies."""
        a = [rec("/a/d", "X (1).jpg"), rec("/a/e", "Y.jpg")]
        b = [rec("/b/d", "X (2).jpg"), rec("/b/e", "Y (1).jpg")]
        self.assertEqual(self.missing(a, b), ["/a/d/X (1).jpg"])
        self.assertEqual(self.similar, {"/a/d/X (1).jpg"})

    def test_renamed_series_in_other_pool(self):
        """A folder holding "Vacation (1)" and "Vacation (2)" is a series, not copies of A's."""
        a = [rec("/a/d", "Vacation.jpg")]
        b = [rec("/b/s", "Vacation (1).jpg"), rec("/b/s", "Vacation (2).jpg")]
        self.assertEqual(self.missing(a, b), ["/a/d/Vacation.jpg"])

    def test_empty_files_not_compared(self):
        a = [rec("/a/d", "e.jpg", 0), rec("/a/d", "x.jpg")]
        b = [rec("/b/d", "e.jpg", 0)]
        self.assertEqual(self.missing(a, b), ["/a/d/x.jpg"])
        self.assertEqual(self.totals, {"/a/d": 1})
        cov = check_coverage(
            self.idx, ["/a"], ["/b"], pool="A", other="B", direction=NOT_BACKED_UP
        )
        self.assertEqual((cov.empty, cov.files, len(cov.missing)), (1, 1, 1))

    def test_own_pool_copies_do_not_count(self):
        a = [rec("/a/d", "x.jpg"), rec("/a/e", "x.jpg")]
        self.assertEqual(self.missing(a, [rec("/b/d", "other.jpg")]), ["/a/d/x.jpg", "/a/e/x.jpg"])

    def test_both_directions(self):
        self.idx.replace_root("/a", [rec("/a/d", "x.jpg"), rec("/a/d", "onlya.jpg", 5)])
        self.idx.replace_root("/b", [rec("/b/d", "x.jpg"), rec("/b/d", "onlyb.jpg", 6)])
        ab = check_coverage(self.idx, ["/a"], ["/b"], pool="A", other="B", direction=NOT_BACKED_UP)
        ba = check_coverage(self.idx, ["/b"], ["/a"], pool="B", other="A", direction=ONLY_IN_BACKUP)
        self.assertEqual([f.name for f in ab.missing], ["onlya.jpg"])
        self.assertEqual([f.name for f in ba.missing], ["onlyb.jpg"])
        self.assertEqual((ab.label, ba.label), ("A not in B", "B not in A"))
        self.assertEqual(ab.title, "Not backed up — no copy anywhere in pool B")
        self.assertEqual(ba.title, "Only in the backup — no copy anywhere in pool A")
        self.assertEqual(ab.basis, "similar name+size+mtime ±2s")
        self.assertEqual((ab.files, ab.missing_bytes, ab.missing_dirs), (2, 5, 1))


class RollUpTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def coverage(self, a, b, roots=("/a",)):
        self.idx.replace_root("/a", a)
        self.idx.replace_root("/b", b)
        return check_coverage(
            self.idx, list(roots), ["/b"], pool="A", other="B", direction=NOT_BACKED_UP
        )

    def test_whole_tree_and_partial_folder(self):
        a = [rec("/a/Trip/day1", f"IMG_{n}.JPG", 10) for n in range(1, 4)]
        a += [rec("/a/Trip/day2/raw", f"IMG_{n}.CR2", 20) for n in range(1, 3)]
        a += [rec("/a/DCIM", f"DSC{n:05d}.ARW", 5) for n in range(1, 11)]
        b = [rec("/b/x", f"DSC{n:05d}.ARW", 5) for n in (1, 2, 6)]
        cov = self.coverage(a, b)
        tree, folder = cov.folders
        self.assertEqual(
            (tree.dir, tree.whole_tree, tree.folders, tree.missing, tree.total, tree.bytes),
            ("/a/Trip", True, 2, 5, 5, 70),
        )
        self.assertEqual(tree.runs, ())
        self.assertEqual((folder.dir, folder.whole_tree, folder.missing, folder.total),
                         ("/a/DCIM", False, 7, 10))  # fmt: skip
        self.assertEqual(
            [(r.a_first, r.a_last, r.count) for r in folder.runs],
            [("DSC00003.ARW", "DSC00005.ARW", 3), ("DSC00007.ARW", "DSC00010.ARW", 4)],
        )
        self.assertEqual(cov.missing_dirs, 3)

    def test_roll_up_stops_at_root(self):
        a = [rec("/a/x", "1.jpg"), rec("/a/y", "2.jpg", 101)]
        cov = self.coverage(a, [rec("/b", "z.jpg")])
        self.assertEqual([(m.dir, m.whole_tree, m.folders) for m in cov.folders],
                         [("/a", True, 2)])  # fmt: skip
        sub = check_coverage(
            self.idx, ["/a/x", "/a/y"], ["/b"], pool="A", other="B", direction=NOT_BACKED_UP
        )
        self.assertEqual(sorted((m.dir, m.whole_tree) for m in sub.folders),
                         [("/a/x", False), ("/a/y", False)])  # fmt: skip

    def test_single_full_folder_is_not_a_tree(self):
        a = [rec("/a/p/q", f"{n}.jpg", 10 + n) for n in range(3)] + [rec("/a/r", "k.jpg")]
        cov = self.coverage(a, [rec("/b", "k.jpg")])
        (m,) = cov.folders
        self.assertEqual((m.dir, m.whole_tree, m.missing, m.total), ("/a/p/q", False, 3, 3))
        self.assertEqual([(r.a_first, r.a_last, r.count) for r in m.runs], [("0.jpg", "2.jpg", 3)])

    def test_other_files_counted_in_tree(self):
        a = [rec("/a/T/x", "1.jpg"), rec("/a/T/y", "2.jpg", 101), rec("/a/K", "k.jpg")]
        self.idx.replace_root("/a", a, dir_stats={"/a/T/x": (2, 1)})
        self.idx.replace_root("/b", [rec("/b", "k.jpg")])
        cov = check_coverage(
            self.idx, ["/a"], ["/b"], pool="A", other="B", direction=NOT_BACKED_UP
        )
        self.assertEqual([(m.dir, m.unindexed) for m in cov.folders], [("/a/T", 3)])

    def test_backed_up_sibling_does_not_split_run(self):
        a = [rec("/a/d", f"IMGP000{n}.{ext}", 10) for n in range(1, 5) for ext in ("DNG", "JPG")]
        b = [rec("/b/d", f"IMGP000{n}.JPG", 10) for n in range(1, 5)]
        b.append(rec("/b/d", "IMGP0003.DNG", 10))
        (m,) = self.coverage(a, b).folders
        self.assertEqual(
            [(r.a_first, r.a_last, r.count, r.skipped) for r in m.runs],
            [("IMGP0001.DNG", "IMGP0002.DNG", 2, ("IMGP0001.JPG",)),
             ("IMGP0004.DNG", "IMGP0004.DNG", 1, ())],
        )  # fmt: skip

    def test_sorted_by_bytes(self):
        a = [rec("/a/small", "s.jpg", 1), rec("/a/big", "b.jpg", 1000), rec("/a/c", "c.jpg")]
        cov = self.coverage(a, [rec("/b", "c.jpg")])
        self.assertEqual([m.dir for m in cov.folders], ["/a/big", "/a/small"])

    def test_basis_words(self):
        self.assertEqual(basis_words(), "similar name, size and date")
        self.assertEqual(basis_words(exact_names=True, ignore_mtime=True), "name and size")
        self.assertEqual(basis_words(ignore_size=True, ignore_mtime=True), "similar name")


if __name__ == "__main__":
    unittest.main()
