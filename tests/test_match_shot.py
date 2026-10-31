import unittest

from dpf.coverage import check_coverage, missing_files
from dpf.dumps import DumpConfig, check_dumps
from dpf.exif import ExifData, row_values
from dpf.index import Index
from dpf.match_name import find_name_duplicates
from dpf.match_shot import SHOT_BASIS, find_shot_duplicates, split_by_shot
from dpf.models import FileRecord

T = 1_600_000_000.0


def rec(dir, name, size=100, mtime=T):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind="image")


def shot(f: FileRecord, shutter=1, serial="AAA", model="ILCE-7RM4A", taken="2023:01:01 10:00:00",
         make="SONY", width=None, height=None):  # fmt: skip
    """An EXIF row for `f` (as read at its size and mtime) with the given identity."""
    data = ExifData(
        fmt=f.name.rsplit(".", 1)[-1].lower(), make=make, model=model, serial=serial,
        shutter=shutter, taken=taken, width=width, height=height,
    )  # fmt: skip
    return row_values(f.dir, f.name, f.size, f.mtime, data, 0.0)


def no_id(f: FileRecord):
    return row_values(f.dir, f.name, f.size, f.mtime, ExifData(fmt="jpg", make="Apple"), 0.0)


class SplitTest(unittest.TestCase):
    def test_split_by_shot(self):
        a, b, c, d = (rec("/d", n) for n in "abcd")
        keys = {"a": "K1", "b": "K1", "c": "K2", "d": None}
        parts = split_by_shot([a, b, c, d], lambda f: keys[f.name])
        named = sorted((tuple(f.name for f in p), k or "") for p, k in parts)
        self.assertEqual(named, [(("a", "b"), "K1"), (("c",), "K2"), (("d",), "")])
        whole = split_by_shot([a, b, d], lambda f: keys[f.name])
        self.assertEqual([(len(p), k) for p, k in whole], [(3, "K1")])
        self.assertEqual(split_by_shot([d], lambda f: None), [([d], None)])


class ShotDuplicatesTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def find(self, files, rows):
        self.idx.replace_root("/r", files)
        self.idx.put_exif(rows)
        groups, stats = find_shot_duplicates(self.idx, ["/r"])
        self.stats = stats
        return sorted(sorted(f.path for f in g.files) for g in groups), groups

    def test_resaved_copy_matches_by_shot(self):
        a = rec("/r/A", "IMGQ0001.DNG", 30_000_000, T)
        b = rec("/r/B", "IMGQ0001.DNG", 30_004_096, T + 86_400 * 300)  # Lightroom re-save
        c = rec("/r/C", "renamed.dng", 30_008_000, T + 5)
        found, groups = self.find([a, b, c], [shot(a), shot(b), shot(c)])
        self.assertEqual(found, [["/r/A/IMGQ0001.DNG", "/r/B/IMGQ0001.DNG", "/r/C/renamed.dng"]])
        self.assertEqual(groups[0].basis, SHOT_BASIS)
        self.assertEqual(groups[0].hours, ())
        self.assertEqual((self.stats.shot_groups, self.stats.resaved_groups), (1, 1))
        self.assertEqual(list(find_name_duplicates(self.idx, ["/r"])), [])  # mode 1: none

    def test_raw_and_jpg_of_one_shot_are_not_duplicates(self):
        raw, jpg = rec("/r/A", "DSC00001.ARW"), rec("/r/A", "DSC00001.JPG", 50)
        found, _ = self.find([raw, jpg], [shot(raw), shot(jpg, width=10, height=10)])
        self.assertEqual(found, [])
        self.assertEqual(self.stats.multi_format, 1)

    def test_resized_jpg_is_another_shot(self):
        big, small = rec("/r/A", "DSC00001.JPG", 900), rec("/r/B", "DSC00001.JPG", 300)
        found, _ = self.find([big, small], [shot(big, width=9504, height=6336),
                                            shot(small, width=2000, height=1333)])  # fmt: skip
        self.assertEqual(found, [])

    def test_conflicting_identity_vetoes_name_match(self):
        # Same name, size and mtime, but two bodies (or a counter that wrapped).
        a, b = rec("/r/A", "DSC00001.ARW"), rec("/r/B", "DSC00001.ARW")
        found, _ = self.find([a, b], [shot(a, serial="AAA"), shot(b, serial="BBB")])
        self.assertEqual(found, [])
        self.assertEqual(self.stats.vetoed, 1)
        found, _ = self.find([a, b], [shot(a, shutter=5), shot(b, shutter=6)])
        self.assertEqual(found, [])
        found, _ = self.find([a, b], [shot(a, taken="2023:01:01 10:00:00"),
                                      shot(b, taken="2024:01:01 10:00:00")])  # fmt: skip
        self.assertEqual(found, [])

    def test_files_without_identity_fall_back_to_mode_1(self):
        a, b = rec("/r/A", "PXL_1.jpg"), rec("/r/B", "PXL_1.jpg")
        found, groups = self.find([a, b], [no_id(a)])  # b: no EXIF cached
        self.assertEqual(found, [["/r/A/PXL_1.jpg", "/r/B/PXL_1.jpg"]])
        self.assertNotEqual(groups[0].basis, SHOT_BASIS)
        self.assertEqual((self.stats.no_exif, self.stats.no_identity), (1, 1))

    def test_name_match_joins_shot_group(self):
        a = rec("/r/A", "DSC00001.ARW")
        b = rec("/r/B", "DSC00001.ARW")  # no EXIF yet, same name/size/mtime as a
        c = rec("/r/C", "other.arw", 120, T + 999)  # same shot as a, re-saved
        found, groups = self.find([a, b, c], [shot(a), shot(c)])
        self.assertEqual(found, [["/r/A/DSC00001.ARW", "/r/B/DSC00001.ARW", "/r/C/other.arw"]])
        self.assertEqual(groups[0].basis, SHOT_BASIS)

    def test_file_without_identity_between_two_shots_stays_apart(self):
        a, b, c = (rec(f"/r/{d}", "DSC00001.ARW") for d in "ABC")
        a2 = rec("/r/D", "x.arw", 7)  # the same shot as a
        found, _ = self.find([a, b, c, a2], [shot(a, shutter=1), shot(c, shutter=2), shot(a2)])
        # c's name match with a is refused; b could be either, so it matches neither.
        self.assertEqual(found, [["/r/A/DSC00001.ARW", "/r/D/x.arw"]])
        self.assertEqual(self.stats.vetoed, 1)

    def test_plain_name_group_keeps_hour_shift(self):
        a, b = rec("/r/A", "DSC00001.ARW"), rec("/r/B", "DSC00001.ARW", mtime=T + 3600)
        found, groups = self.find([a, b], [shot(a), shot(b)])
        self.assertEqual(len(found), 1)
        self.assertNotEqual(groups[0].basis, SHOT_BASIS)  # mode 1 already had every copy
        self.assertEqual(groups[0].hours, (0, 1))
        self.assertEqual(self.stats.shot_groups, 0)

    def test_stale_exif_is_ignored(self):
        a, b = rec("/r/A", "x.arw", 100), rec("/r/B", "y.arw", 200)
        stale = shot(b)
        stale = (*stale[:2], 150, *stale[3:])  # read when b had another size
        found, _ = self.find([a, b], [shot(a), stale])
        self.assertEqual(found, [])
        self.assertEqual(self.stats.no_exif, 1)

    def test_groups_are_disjoint(self):
        files = [rec(f"/r/{i}", "DSC00001.ARW", 100 + i % 2) for i in range(6)]
        rows = [shot(f, shutter=1 + i % 3) for i, f in enumerate(files)]
        found, _ = self.find(files, rows)
        paths = [p for g in found for p in g]
        self.assertEqual(len(paths), len(set(paths)))


class ShotCoverageTest(unittest.TestCase):
    """Pool A is /a, pool B is /b."""

    def setUp(self):
        self.idx = Index(":memory:")
        self.addCleanup(self.idx.close)

    def missing(self, a, b, rows, **kw):
        self.idx.replace_root("/a", a)
        self.idx.replace_root("/b", b)
        self.idx.put_exif(rows)
        self.found = missing_files(self.idx, ["/a"], ["/b"], shots=True, **kw)
        return [f.path for f in self.found.missing]

    def test_resaved_backup_counts_as_copied(self):
        a = rec("/a/2023", "IMGQ0001.DNG", 30_000_000)
        b = rec("/b/2023", "IMGQ0001.DNG", 30_004_096, T + 9_999_999)
        self.assertEqual(self.missing([a], [b], [shot(a), shot(b)]), [])
        self.assertEqual(sum(self.found.by_shot.values()), 1)
        plain = missing_files(self.idx, ["/a"], ["/b"])  # mode 1 alone
        self.assertEqual([f.path for f in plain.missing], ["/a/2023/IMGQ0001.DNG"])
        self.assertEqual(plain.near, {"/a/2023/IMGQ0001.DNG"})

    def test_other_shot_with_same_name_is_missing(self):
        a, b = rec("/a/d", "DSC00001.ARW"), rec("/b/d", "DSC00001.ARW")
        self.assertEqual(self.missing([a], [b], [shot(a, serial="A"), shot(b, serial="B")]),
                         ["/a/d/DSC00001.ARW"])  # fmt: skip
        self.assertEqual(sum(self.found.vetoed.values()), 1)

    def test_raw_backed_up_as_jpg_only_is_missing(self):
        a, b = rec("/a/d", "DSC00001.ARW"), rec("/b/d", "DSC00001.JPG")
        self.assertEqual(self.missing([a], [b], [shot(a), shot(b, width=1, height=1)]),
                         ["/a/d/DSC00001.ARW"])  # fmt: skip

    def test_file_without_identity_covered_through_its_name_group(self):
        a1 = rec("/a/x", "DSC00001.ARW")  # identity, its shot is in B under another name
        a2 = rec("/a/y", "DSC00001.ARW")  # no EXIF yet, a name copy of a1
        b = rec("/b/z", "renamed.arw", 555, T + 77)
        self.assertEqual(self.missing([a1, a2], [b], [shot(a1), shot(b)]), [])
        self.assertEqual(sum(self.found.by_shot.values()), 2)

    def test_shot_covered_through_a_name_match_in_another_bucket(self):
        # a1 has no name copy in B, but its shot a2 does (b: no EXIF, same name as a2).
        a1 = rec("/a/x", "first.arw", 100)
        a2 = rec("/a/y", "zzz.arw", 300, T + 50)
        b = rec("/b/y", "zzz.arw", 300, T + 50)
        self.assertEqual(self.missing([a1, a2], [b], [shot(a1), shot(a2)]), [])
        self.assertEqual(sum(self.found.by_shot.values()), 1)

    def test_without_exif_same_as_mode_1(self):
        a = [rec("/a/d", "x.jpg"), rec("/a/d", "y.jpg", 200)]
        b = [rec("/b/d", "x.jpg"), rec("/b/d", "y.jpg", 201)]
        self.assertEqual(self.missing(a, b, []), ["/a/d/y.jpg"])
        plain = missing_files(self.idx, ["/a"], ["/b"])
        self.assertEqual([f.path for f in plain.missing], ["/a/d/y.jpg"])

    def test_check_coverage_reports_shot_counts(self):
        a = [rec("/a/d", "x.dng", 100), rec("/a/d", "y.dng"), rec("/a/d", "z.jpg")]
        b = [rec("/b/d", "x.dng", 104, T + 500), rec("/b/d", "y.dng")]
        self.idx.replace_root("/a", a)
        self.idx.replace_root("/b", b)
        self.idx.put_exif([shot(a[0]), shot(b[0]), shot(a[1], serial="A"), shot(b[1], serial="B")])
        cov = check_coverage(self.idx, ["/a"], ["/b"], pool="A", other="B", direction="x",
                             shots=True)  # fmt: skip
        self.assertEqual([f.name for f in cov.missing], ["y.dng", "z.jpg"])
        self.assertEqual((cov.by_shot, cov.vetoed, cov.no_exif, cov.other_no_exif), (1, 1, 1, 0))
        self.assertTrue(cov.basis.startswith("same shot"))
        self.assertIn("camera shot", cov.basis_words)


class ShotDumpsTest(unittest.TestCase):
    def test_dump_photo_sorted_under_another_name(self):
        idx = Index(":memory:")
        self.addCleanup(idx.close)
        dump = [rec("/d/_Dumps/x", f"DSC{i:05}.ARW", 100 + i) for i in range(3)]
        done = [rec("/d/Photography/2023", f"Trip-{i}.arw", 999, T + 7) for i in range(2)]
        idx.replace_root("/d", dump + done)
        idx.put_exif([shot(f, shutter=i + 1) for i, f in enumerate(dump)])
        idx.put_exif([shot(f, shutter=i + 1) for i, f in enumerate(done)])
        config = DumpConfig("dump", "photography", 1)

        def unsorted(**kw):
            return check_dumps(idx, ["/d"], ["/d"], config, lambda d: "", basis="x", **kw)

        self.assertEqual(unsorted().unsorted_files, 3)
        self.assertEqual(unsorted(shots=True).unsorted_files, 1)


if __name__ == "__main__":
    unittest.main()
