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
            rec("/r/d", "20230826 DSC01234 (1).ARW"),
        ]
        got = self.groups(records)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "similar name+size+mtime")
        self.assertEqual(len(got[0][1]), 4)

    def names(self, records, **kw):
        return sorted([p.rsplit("/", 2)[-2] + "/" + p.rsplit("/", 1)[-1] for p in paths]
                      for _, paths in self.groups(records, **kw))  # fmt: skip

    def test_copy_marks_table(self):
        """Different copy numbers never match; numbered series fall back to exact names."""
        cases = [
            # (files as "folder/name", expected groups)
            (["a/X.jpg", "b/X (1).jpg"], [["a/X.jpg", "b/X (1).jpg"]]),
            (["a/X (1).jpg", "b/X (1).jpg"], [["a/X (1).jpg", "b/X (1).jpg"]]),
            (["a/X (1).jpg", "b/X (2).jpg"], []),
            (["a/Vacation (1).ARW", "b/Vacation (2).ARW"], []),
            (["a/X.jpg", "a/X (1).jpg"], [["a/X (1).jpg", "a/X.jpg"]]),  # a copy in place
            (["a/X.jpg", "b/X (1).jpg", "c/X (2).jpg"], []),  # ambiguous: no claim
            (["a/X - Copy.jpg", "b/X (1).jpg"], [["a/X - Copy.jpg", "b/X (1).jpg"]]),
            (["a/X - Copy (2).jpg", "b/X (2).jpg"], [["a/X - Copy (2).jpg", "b/X (2).jpg"]]),
            (["a/X - Copy (2).jpg", "b/X (1).jpg"], []),
            # Windows copies in place: unnumbered marks are never a series
            (["a/X.jpg", "a/X - Copy.jpg", "a/X - Copy - Copy.jpg", "a/X - Copy (2).jpg"],
             [["a/X - Copy (2).jpg", "a/X - Copy - Copy.jpg", "a/X - Copy.jpg", "a/X.jpg"]]),
            (["a/X.jpg", "b/2023-08-26 X.jpg"], []),  # no camera number: text kept
            # a folder holding "(1)" and "(2)" is a renamed series, not copies
            (["s/Vacation (1).ARW", "s/Vacation (2).ARW", "b/Vacation.ARW"], []),
            (
                ["s/Vacation (1).ARW", "s/Vacation (2).ARW",
                 "b/Vacation (1).ARW", "b/Vacation (2).ARW"],
                [["b/Vacation (1).ARW", "s/Vacation (1).ARW"],
                 ["b/Vacation (2).ARW", "s/Vacation (2).ARW"]],
            ),
            (["s/IMG_1234-1.JPG", "s/IMG_1234-2.JPG", "b/IMG_1234.JPG"], []),
            (["s/X (1).jpg", "s/X - Copy (2).jpg", "b/X.jpg"], []),
            (["a/IMG_1234-1.JPG", "b/IMG_1234-2.JPG"], []),
            (["a/DSC01234_1.ARW", "b/DSC01234.ARW"], [["a/DSC01234_1.ARW", "b/DSC01234.ARW"]]),
            (["a/DSC01234_1.ARW", "b/DSC01234_2.ARW"], []),
            (
                ["a/DSC01234 (1).ARW", "b/2023-08-26_DSC01234.ARW"],
                [["a/DSC01234 (1).ARW", "b/2023-08-26_DSC01234.ARW"]],
            ),
            # text around a camera number must be neutral or the same on both sides
            (["a/BodyA_DSC01234.ARW", "b/BodyB_DSC01234.ARW"], []),
            (["a/BodyA_DSC01234.ARW", "b/DSC01234.ARW"], []),
            (
                ["a/BodyA_DSC01234.ARW", "b/BodyA_DSC01234 (1).ARW"],
                [["a/BodyA_DSC01234.ARW", "b/BodyA_DSC01234 (1).ARW"]],
            ),
            (["a/DSC01234-Edit.tif", "b/DSC01234-Edit-2.tif"], []),
            (
                ["a/DSC01234-Edit.tif", "b/2023-08-26_DSC01234-Edit.tif"],
                [["a/DSC01234-Edit.tif", "b/2023-08-26_DSC01234-Edit.tif"]],
            ),
            (["a/Lincoln DSC04205.ARW", "b/DSC04205.ARW"], []),
        ]  # fmt: skip
        for files, expected in cases:
            with self.subTest(files=files):
                records = [rec("/r/" + f.split("/")[0], f.split("/")[1]) for f in files]
                self.assertEqual(self.names(records), expected)

    def test_series_guard_only_splits_its_own_key(self):
        records = [
            rec("/r/s", "Vacation (1).ARW"),
            rec("/r/s", "Vacation (2).ARW"),
            rec("/r/b", "Vacation.ARW"),
            rec("/r/c", "X.ARW"),
            rec("/r/d", "X (1).ARW"),
        ]
        self.assertEqual(self.names(records), [["c/X.ARW", "d/X (1).ARW"]])

    def test_series_guard_spans_mtimes(self):
        """A burst renamed in place: frames a few seconds apart still mark a series."""
        records = [
            rec("/r/s", "Vacation (1).ARW", mtime=T),
            rec("/r/s", "Vacation (2).ARW", mtime=T + 30),
            rec("/r/b", "Vacation.ARW", mtime=T),
        ]
        self.assertEqual(self.groups(records), [])

    def test_exact_names_ignores_marks_and_series(self):
        records = [
            rec("/r/s", "Vacation (1).ARW"),
            rec("/r/s", "Vacation (2).ARW"),
            rec("/r/b", "vacation (1).arw"),
        ]
        self.assertEqual(
            self.names(records, exact_names=True), [["b/vacation (1).arw", "s/Vacation (1).ARW"]]
        )

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
