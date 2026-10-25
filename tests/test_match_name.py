import unittest

from dpf.index import Index
from dpf.match_name import find_name_duplicates
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
        self.assertEqual(got, [("name+size+mtime", ["/r/a/x.jpg", "/r/b/x.jpg"])])

    def test_just_over_tolerance_splits(self):
        got = self.groups([rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 2.01)])
        self.assertEqual(got, [])

    def test_custom_tolerance(self):
        records = [rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 5)]
        self.assertEqual(len(self.groups(records, mtime_tolerance=5.0)), 1)
        self.assertEqual(self.groups(records, mtime_tolerance=0.0), [])

    def test_single_linkage_chains(self):
        records = [
            rec("/r/a", "x.jpg", mtime=T),
            rec("/r/b", "x.jpg", mtime=T + 1.5),
            rec("/r/c", "x.jpg", mtime=T + 3.0),  # 3 s from a, but chained through b
            rec("/r/d", "x.jpg", mtime=T + 10.0),
            rec("/r/e", "x.jpg", mtime=T + 11.0),
            rec("/r/f", "x.jpg", mtime=T + 20.0),  # alone
        ]
        got = sorted(paths for _, paths in self.groups(records))
        self.assertEqual(
            got,
            [["/r/a/x.jpg", "/r/b/x.jpg", "/r/c/x.jpg"], ["/r/d/x.jpg", "/r/e/x.jpg"]],
        )

    def test_files_sorted_by_path_not_mtime(self):
        records = [rec("/r/z", "x.jpg", mtime=T), rec("/r/a", "x.jpg", mtime=T + 1)]
        self.assertEqual(self.groups(records)[0][1], ["/r/a/x.jpg", "/r/z/x.jpg"])

    def test_case_insensitive_names(self):
        got = self.groups([rec("/r/a", "IMG_0001.JPG"), rec("/r/b", "img_0001.jpg")])
        self.assertEqual(got, [("name+size+mtime", ["/r/a/IMG_0001.JPG", "/r/b/img_0001.jpg"])])

    def test_size_must_match_unless_ignored(self):
        records = [rec("/r/a", "x.jpg", size=1), rec("/r/b", "x.jpg", size=2)]
        self.assertEqual(self.groups(records), [])
        self.assertEqual(
            self.groups(records, ignore_size=True),
            [("name+mtime", ["/r/a/x.jpg", "/r/b/x.jpg"])],
        )

    def test_ignore_mtime(self):
        records = [rec("/r/a", "x.jpg"), rec("/r/b", "x.jpg", mtime=T + 9999)]
        self.assertEqual(self.groups(records), [])
        self.assertEqual(
            self.groups(records, ignore_mtime=True),
            [("name+size", ["/r/a/x.jpg", "/r/b/x.jpg"])],
        )

    def test_ignore_both(self):
        records = [
            rec("/r/a", "x.jpg", size=1),
            rec("/r/b", "X.JPG", size=2, mtime=T + 9999),
            rec("/r/c", "y.jpg"),
        ]
        self.assertEqual(
            self.groups(records, ignore_size=True, ignore_mtime=True),
            [("name", ["/r/a/x.jpg", "/r/b/X.JPG"])],
        )

    def test_respects_roots(self):
        self.idx.replace_root("/r1", [rec("/r1", "x.jpg")])
        self.idx.replace_root("/r2", [rec("/r2", "x.jpg")])
        self.assertEqual(list(find_name_duplicates(self.idx, ["/r1"])), [])
        self.assertEqual(len(list(find_name_duplicates(self.idx, ["/r1", "/r2"]))), 1)


if __name__ == "__main__":
    unittest.main()
