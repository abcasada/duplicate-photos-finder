import unittest

from dpf.index import Index
from dpf.match_name import basis, find_name_duplicates
from dpf.models import FileRecord

T = 1_600_000_000.0


def rec(dir, name, size=100, mtime=T):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind="image")


class MatchNameTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")

    def tearDown(self):
        self.idx.close()

    def groups(self, records, **kw):
        self.idx.replace_root("/r", records)
        return [
            (g.basis, [f.path for f in g.files])
            for g in find_name_duplicates(self.idx, ["/r"], **kw)
        ]

    def test_exact_tolerance_matches(self):
        got = self.groups([rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 2.0)])
        self.assertEqual(got, [("similar name+size+mtime", ["/r/a/x.jpg", "/r/b/x.jpg"])])

    def test_just_over_tolerance_splits(self):
        got = self.groups([rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 2.01)])
        self.assertEqual(got, [])

    def test_custom_tolerance(self):
        records = [rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 5)]
        self.assertEqual(len(self.groups(records, mtime_tolerance=5.0)), 1)
        self.assertEqual(self.groups(records, mtime_tolerance=0.0), [])

    def test_clusters_do_not_chain_beyond_tolerance(self):
        records = [
            rec("/r/a", "x.jpg", mtime=T),
            rec("/r/b", "x.jpg", mtime=T + 1.5),
            rec("/r/c", "x.jpg", mtime=T + 3.0),  # within 2 s of b, but 3 s from a
            rec("/r/d", "x.jpg", mtime=T + 4.0),
            rec("/r/e", "x.jpg", mtime=T + 20.0),  # alone
        ]
        got = sorted(paths for _, paths in self.groups(records))
        self.assertEqual(got, [["/r/a/x.jpg", "/r/b/x.jpg"], ["/r/c/x.jpg", "/r/d/x.jpg"]])

    def test_files_sorted_by_path_not_mtime(self):
        records = [rec("/r/z", "x.jpg", mtime=T), rec("/r/a", "x.jpg", mtime=T + 1)]
        self.assertEqual(self.groups(records)[0][1], ["/r/a/x.jpg", "/r/z/x.jpg"])

    def test_case_insensitive_names(self):
        got = self.groups([rec("/r/a", "IMG_0001.JPG"), rec("/r/b", "img_0001.jpg")])
        paths = ["/r/a/IMG_0001.JPG", "/r/b/img_0001.jpg"]
        self.assertEqual(got, [("similar name+size+mtime", paths)])

    def test_size_must_match_unless_ignored(self):
        records = [rec("/r/a", "x.jpg", size=1), rec("/r/b", "x.jpg", size=2)]
        self.assertEqual(self.groups(records), [])
        self.assertEqual(
            self.groups(records, ignore_size=True),
            [("similar name+mtime", ["/r/a/x.jpg", "/r/b/x.jpg"])],
        )

    def test_ignore_mtime(self):
        records = [rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 9999)]
        self.assertEqual(self.groups(records), [])
        self.assertEqual(
            self.groups(records, ignore_mtime=True),
            [("similar name+size", ["/r/a/x.jpg", "/r/b/x.jpg"])],
        )

    def test_ignore_both(self):
        records = [
            rec("/r/a", "x.jpg", size=1),
            rec("/r/b", "X.JPG", size=2, mtime=T + 9999),
            rec("/r/c", "y.jpg"),
        ]
        self.assertEqual(
            self.groups(records, ignore_size=True, ignore_mtime=True),
            [("similar name", ["/r/a/x.jpg", "/r/b/X.JPG"])],
        )

    def test_similar_names_match(self):
        records = [
            rec("/r/a", "DSC01234.ARW"),
            rec("/r/b", "DSC01234 (1).ARW", mtime=T + 1),
            rec("/r/c", "2023-08-26_DSC01234.ARW"),
            rec("/r/d", "Copy of DSC01234.ARW"),
        ]
        got = self.groups(records)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "similar name+size+mtime")
        self.assertEqual(len(got[0][1]), 4)

    def test_exact_names(self):
        records = [rec("/r/a", "x.jpg"), rec("/r/b", "X.JPG"), rec("/r/c", "x (1).jpg")]
        self.assertEqual(
            self.groups(records, exact_names=True),
            [("name+size+mtime", ["/r/a/x.jpg", "/r/b/X.JPG"])],
        )

    def test_similar_names_still_need_size_and_mtime(self):
        records = [
            rec("/r/a", "DSC01234.ARW"),
            rec("/r/b", "Show DSC01234.ARW", size=101),  # same counter, another photo
            rec("/r/c", "DSC01234 (1).ARW", mtime=T + 60),
        ]
        self.assertEqual(self.groups(records), [])

    def test_similar_names_never_cross_extensions(self):
        records = [rec("/r/a", "DSC01234.ARW"), rec("/r/b", "DSC01234.JPG")]
        self.assertEqual(self.groups(records), [])

    def test_basis(self):
        self.assertEqual(basis(), "similar name+size+mtime")
        self.assertEqual(basis(exact_names=True), "name+size+mtime")
        self.assertEqual(basis(ignore_size=True), "similar name+mtime")
        self.assertEqual(basis(exact_names=True, ignore_size=True, ignore_mtime=True), "name")

    def test_respects_roots(self):
        self.idx.replace_root("/r1", [rec("/r1", "x.jpg")])
        self.idx.replace_root("/r2", [rec("/r2", "x.jpg")])
        self.assertEqual(list(find_name_duplicates(self.idx, ["/r1"])), [])
        self.assertEqual(len(list(find_name_duplicates(self.idx, ["/r1", "/r2"]))), 1)


if __name__ == "__main__":
    unittest.main()
