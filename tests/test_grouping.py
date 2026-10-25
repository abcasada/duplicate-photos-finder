import os
import tempfile
import unittest
from collections import defaultdict

from dpf.grouping import (
    A_IN_B,
    B_IN_A,
    IDENTICAL,
    IDENTICAL_TREE,
    PARTIAL,
    group_duplicates,
    natural_key,
)
from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord
from dpf.scanner import normalize_roots, scan_roots
from tests.test_scanner import touch


def rec(dir, name, size=100, mtime=1_000_000.0, kind="image"):
    return FileRecord(dir=dir, name=name, size=size, mtime=mtime, kind=kind)


def imgs(dir, numbers, size=100):
    return [rec(dir, f"IMG_{n:04d}.JPG", size) for n in numbers]


def by_name(records):
    """Mode-1 style groups: same casefolded name and size."""
    buckets = defaultdict(list)
    for r in records:
        buckets[(r.name.casefold(), r.size)].append(r)
    return [DuplicateGroup(tuple(fs), "name+size") for fs in buckets.values() if len(fs) > 1]


class Fixture:
    def __init__(self, records, dir_stats=None):
        self.records = list(records)
        self.idx = Index(":memory:")
        self.idx.replace_root("/", self.records, dir_stats=dir_stats)
        self.list_calls = defaultdict(int)

    def list_dir(self, d):
        self.list_calls[d] += 1
        return self.idx.list_dir(d)

    def run(self, groups=None, roots=("/",), **kw):
        groups = by_name(self.records) if groups is None else groups
        return group_duplicates(
            groups, self.list_dir, self.idx.subdirs, self.idx.dir_extras, roots=roots, **kw
        )


def pair_map(result):
    return {(p.dir_a, p.dir_b): p for p in result.pairs}


class NaturalKeyTest(unittest.TestCase):
    def test_numeric_and_case_insensitive(self):
        names = ["IMG_10.jpg", "img_9.jpg", "IMG_100.jpg", "IMG_1.jpg", "a.jpg", "B.jpg"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["a.jpg", "B.jpg", "IMG_1.jpg", "img_9.jpg", "IMG_10.jpg", "IMG_100.jpg"],
        )

    def test_mixed_shapes_compare(self):
        names = ["2019-07-01 12.jpg", "DSC1.jpg", "1.jpg", "x", "x1y2"]
        sorted(names, key=natural_key)  # must not raise TypeError


class RunsTest(unittest.TestCase):
    def test_single_run_a_in_b(self):
        fx = Fixture(imgs("/a", range(1, 11)) + imgs("/b", range(1, 21)))
        (p,) = fx.run().pairs
        self.assertEqual((p.dir_a, p.dir_b, p.relation), ("/a", "/b", A_IN_B))
        self.assertEqual(len(p.runs), 1)
        r = p.runs[0]
        self.assertEqual(
            (r.a_first, r.a_last, r.count, r.bytes),
            ("IMG_0001.JPG", "IMG_0010.JPG", 10, 1000),
        )
        self.assertEqual((r.b_first, r.b_last), ("IMG_0001.JPG", "IMG_0010.JPG"))
        self.assertTrue(r.same_names)
        self.assertEqual(
            (p.matched_a, p.total_a, p.matched_b, p.total_b, p.bytes),
            (10, 10, 10, 20, 1000),
        )

    def test_natural_order_runs(self):
        names = [f"IMG_{n}.jpg" for n in (8, 9, 10, 11)]
        fx = Fixture([rec("/a", n) for n in names] + [rec("/b", n) for n in names])
        (p,) = fx.run().pairs
        self.assertEqual(p.relation, IDENTICAL)
        self.assertEqual((p.runs[0].a_first, p.runs[0].a_last), ("IMG_8.jpg", "IMG_11.jpg"))

    def test_runs_with_gaps_partial(self):
        a = imgs("/a", range(100, 200))
        b = imgs("/b", list(range(100, 140)) + list(range(150, 160)) + [199]) + imgs("/b", [500])
        fx = Fixture(a + b)
        (p,) = fx.run().pairs
        self.assertEqual(p.relation, PARTIAL)
        spans = [(r.a_first, r.a_last, r.count) for r in p.runs]
        self.assertEqual(
            spans,
            [
                ("IMG_0100.JPG", "IMG_0139.JPG", 40),
                ("IMG_0150.JPG", "IMG_0159.JPG", 10),
                ("IMG_0199.JPG", "IMG_0199.JPG", 1),
            ],
        )
        self.assertEqual((p.matched_a, p.total_a, p.matched_b, p.total_b), (51, 100, 51, 52))

    def test_b_in_a(self):
        fx = Fixture(imgs("/a", range(1, 30)) + imgs("/b", range(5, 10)))
        (p,) = fx.run().pairs
        self.assertEqual(p.relation, B_IN_A)

    def test_differing_names_mode2(self):
        a = imgs("/a", range(1, 6))
        b = [rec("/b", f"2019-07-01 {n:03d}.jpg") for n in range(1, 6)]
        groups = [DuplicateGroup((x, y), "sha256") for x, y in zip(a, b)]
        (p,) = Fixture(a + b).run(groups).pairs
        self.assertEqual(p.relation, IDENTICAL)
        (r,) = p.runs
        self.assertFalse(r.same_names)
        self.assertEqual((r.b_first, r.b_last), ("2019-07-01 001.jpg", "2019-07-01 005.jpg"))

    def test_shuffled_b_side_splits_runs(self):
        a = imgs("/a", range(1, 5))
        b = [rec("/b", n) for n in ("w.jpg", "x.jpg", "c.jpg", "d.jpg")]
        groups = [DuplicateGroup((x, y), "sha256") for x, y in zip(a, b)]
        (p,) = Fixture(a + b).run(groups).pairs
        self.assertEqual(
            [(r.b_first, r.b_last) for r in p.runs],
            [("w.jpg", "x.jpg"), ("c.jpg", "d.jpg")],
        )

    def test_dir_a_is_canonical_smaller_path(self):
        fx = Fixture(imgs("/z", range(3)) + imgs("/m", range(3)))
        (p,) = fx.run().pairs
        self.assertEqual((p.dir_a, p.dir_b), ("/m", "/z"))


class GroupsTest(unittest.TestCase):
    def test_same_folder_duplicates(self):
        a = [rec("/a", "x.jpg"), rec("/a", "x (1).jpg"), rec("/b", "x.jpg"), rec("/a", "y.jpg")]
        g = DuplicateGroup(tuple(a[:3]), "sha256")
        solo = DuplicateGroup((rec("/c", "p.jpg"), rec("/c", "q.jpg")), "sha256")
        res = Fixture(a + list(solo.files)).run([g, solo])
        self.assertEqual(res.same_folder, [g, solo])
        (p,) = res.pairs
        self.assertEqual((p.dir_a, p.dir_b), ("/a", "/b"))
        # Both copies in /a count as matched; /a still has an unmatched y.jpg.
        self.assertEqual((p.matched_a, p.total_a, p.matched_b, p.relation), (2, 3, 1, B_IN_A))
        (r,) = p.runs
        self.assertEqual(
            (r.a_first, r.a_last, r.b_first, r.b_last),
            ("x (1).jpg", "x.jpg", "x.jpg", "x.jpg"),
        )
        self.assertFalse(r.same_names)
        self.assertEqual(res.groups, 2)
        self.assertEqual(res.redundant_bytes, 200 + 100)

    def test_three_way_group(self):
        fx = Fixture(imgs("/a", range(5)) + imgs("/b", range(5)) + imgs("/c", range(5)))
        res = fx.run()
        self.assertEqual(set(pair_map(res)), {("/a", "/b"), ("/a", "/c"), ("/b", "/c")})
        self.assertTrue(all(p.relation == IDENTICAL for p in res.pairs))
        self.assertEqual(res.redundant_bytes, 5 * 200)

    def test_max_dirs_cap(self):
        dirs = [f"/d{i:02d}" for i in range(20)]
        files = tuple(rec(d, "x.jpg") for d in dirs)
        res = Fixture(files).run([DuplicateGroup(files, "name")], max_dirs_per_group=16)
        self.assertEqual(res.capped_groups, 1)
        self.assertEqual(set(pair_map(res)), {("/d00", d) for d in dirs[1:]})
        res = Fixture(files[:16]).run([DuplicateGroup(files[:16], "name")], max_dirs_per_group=16)
        self.assertEqual((res.capped_groups, len(res.pairs)), (0, 16 * 15 // 2))

    def test_each_dir_listed_once(self):
        fx = Fixture(imgs("/a", range(5)) + imgs("/b", range(5)) + imgs("/c", range(5)))
        fx.run()
        self.assertTrue(all(n == 1 for n in fx.list_calls.values()), fx.list_calls)

    def test_sorted_by_bytes(self):
        small = imgs("/a", range(3), 10) + imgs("/b", range(3), 10)
        fx = Fixture(small + imgs("/c", range(9), 50) + imgs("/d", range(9), 50))
        res = fx.run()
        self.assertEqual([(p.dir_a, p.bytes) for p in res.pairs], [("/c", 450), ("/a", 30)])

    def test_empty(self):
        res = Fixture([]).run([])
        self.assertEqual((res.pairs, res.groups, res.redundant_bytes), ([], 0, 0))


class TreeRollUpTest(unittest.TestCase):
    @staticmethod
    def tree(root, *, extra_child=None, top_files=True):
        files = []
        if top_files:
            files += [rec(root, "notes.jpg", 7)]
        for i, sub in enumerate(("2019/Hawaii", "2019/Paris", "2020/Home")):
            files += imgs(f"{root}/{sub}", range(i * 10 + 1, i * 10 + 4))
        if extra_child:
            files += [rec(f"{root}/{extra_child}", "odd.jpg", 1)]
        return files

    def test_two_level_roll_up_with_direct_files(self):
        noise = [rec("/d1/Other", "unique.jpg", 5)]
        fx = Fixture(self.tree("/d1/Photos") + self.tree("/d2/Backup/Photos") + noise)
        res = fx.run()
        (p,) = res.pairs
        self.assertEqual(
            (p.dir_a, p.dir_b, p.relation),
            ("/d1/Photos", "/d2/Backup/Photos", IDENTICAL_TREE),
        )
        self.assertEqual((p.folders, p.matched_a, p.total_b, p.bytes), (4, 10, 10, 907))
        self.assertEqual(p.runs, ())

    def test_roll_up_without_direct_files_climbs_while_it_holds(self):
        # /d1 holds only P and /d2/Q holds only P, so the tree extends up to them.
        fx = Fixture(self.tree("/d1/P", top_files=False) + self.tree("/d2/Q/P", top_files=False))
        (p,) = fx.run().pairs
        self.assertEqual(
            (p.dir_a, p.dir_b, p.relation, p.folders),
            ("/d1", "/d2/Q", IDENTICAL_TREE, 3),
        )

    def test_one_child_differs_stops_roll_up(self):
        a = self.tree("/d1/Photos")
        missing = ("/d2/Photos/2019/Paris", "IMG_0013.JPG")
        b = [r for r in self.tree("/d2/Photos") if (r.dir, r.name) != missing]
        res = Fixture(a + b).run()
        got = {(p.dir_a, p.relation, p.folders) for p in res.pairs}
        self.assertEqual(
            got,
            {
                ("/d1/Photos", IDENTICAL, 1),
                ("/d1/Photos/2019/Hawaii", IDENTICAL, 1),
                ("/d1/Photos/2019/Paris", B_IN_A, 1),
                # 2020 still rolls up on its own: /d1/Photos/2020 ≡ /d2/Photos/2020.
                ("/d1/Photos/2020", IDENTICAL_TREE, 1),
            },
        )

    def test_extra_subfolder_stops_roll_up(self):
        a = self.tree("/d1/Photos", extra_child="2019/Extra")
        res = Fixture(a + self.tree("/d2/Photos")).run()
        kinds = {p.dir_a: p.relation for p in res.pairs}
        self.assertEqual(kinds["/d1/Photos/2019/Hawaii"], IDENTICAL)
        self.assertEqual(kinds["/d1/Photos/2020"], IDENTICAL_TREE)
        self.assertNotIn("/d1/Photos/2019", kinds)
        self.assertEqual(kinds["/d1/Photos"], IDENTICAL)

    def test_parent_direct_files_differ(self):
        a = self.tree("/d1/Photos")
        b = self.tree("/d2/Photos", top_files=False)
        kinds = {p.dir_a: p.relation for p in Fixture(a + b).run().pairs}
        self.assertNotIn("/d1/Photos", kinds)
        self.assertEqual(kinds["/d1/Photos/2019"], IDENTICAL_TREE)
        self.assertEqual(kinds["/d1/Photos/2020"], IDENTICAL_TREE)

    def test_nested_roots_not_rolled_into_ancestor(self):
        # /x/a ≡ /x/a/a: parents (/x, /x/a) are ancestor-related, so no tree pair.
        files = imgs("/x/a", range(3)) + imgs("/x/a/a", range(3))
        res = Fixture(files).run()
        self.assertEqual(
            [(p.dir_a, p.dir_b, p.relation) for p in res.pairs],
            [("/x/a", "/x/a/a", IDENTICAL)],
        )

    def test_flipped_orientation_children(self):
        # "/m/a b" < "/m/a/..." but "/m/a" < "/m/a b": canonical order flips per level.
        fx = Fixture(imgs("/m/a/x", range(3)) + imgs("/m/a b/x", range(3)))
        (p,) = fx.run().pairs
        self.assertEqual(
            (p.dir_a, p.dir_b, p.relation, p.folders),
            ("/m/a", "/m/a b", IDENTICAL_TREE, 1),
        )

    def test_not_rolled_up_past_scanned_roots(self):
        # Only the two 2019 folders were scanned; their parents just look empty.
        files = imgs("/d1/Photos/2019/Trip", range(3)) + imgs("/d2/Photos/2019/Trip", range(3))
        (p,) = Fixture(files).run(roots=["/d1/Photos/2019", "/d2/Photos/2019"]).pairs
        self.assertEqual(
            (p.dir_a, p.dir_b, p.relation),
            ("/d1/Photos/2019", "/d2/Photos/2019", IDENTICAL_TREE),
        )
        (p,) = Fixture(files).run(roots=["/d1/Photos/2019", "/d2"]).pairs
        self.assertEqual((p.dir_a, p.dir_b), ("/d1/Photos/2019", "/d2/Photos/2019"))

    def test_unindexed_entries_block_roll_up(self):
        files = self.tree("/d1/Photos") + self.tree("/d2/Photos")
        res = Fixture(files, {"/d1/Photos/2019/Paris": (2, 1)}).run()
        kinds = {p.dir_a: (p.relation, p.unindexed) for p in res.pairs}
        self.assertEqual(kinds["/d1/Photos/2019/Paris"], (IDENTICAL, 3))
        self.assertEqual(kinds["/d1/Photos/2019/Hawaii"], (IDENTICAL, 0))
        self.assertEqual(kinds["/d1/Photos/2020"], (IDENTICAL_TREE, 0))
        self.assertEqual(kinds["/d1/Photos"], (IDENTICAL, 0))

    def test_unindexed_in_fileless_folder_blocks_roll_up(self):
        files = imgs("/d1/P/a", range(3)) + imgs("/d2/P/a", range(3))
        res = Fixture(files, {"/d1/P": (1, 0)}).run()
        self.assertEqual(
            [(p.dir_a, p.relation) for p in res.pairs], [("/d1/P/a", IDENTICAL)]
        )


class ScannedTreeTest(unittest.TestCase):
    """End to end from real directories: scanner -> index -> grouping."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.idx = Index(":memory:")

    def tearDown(self):
        self.idx.close()
        self._tmp.cleanup()

    def p(self, rel):
        return os.path.join(self.base, rel)

    def photos(self, rel, n=3):
        for i in range(n):
            touch(self.p(f"{rel}/IMG_{i}.JPG"), b"x" * (i + 1), mtime=1_600_000_000 + i)

    def pairs(self, *rel_roots):
        roots = normalize_roots(self.p(r) for r in rel_roots)
        scan_roots(self.idx, roots)
        groups = [DuplicateGroup(tuple(b), "name+size") for b in self.idx.name_buckets(roots)]
        res = group_duplicates(
            groups, self.idx.list_dir, self.idx.subdirs, self.idx.dir_extras, roots=roots
        )
        n = len(self.base) + 1
        return {(p.dir_a[n:], p.dir_b[n:]): (p.relation, p.unindexed) for p in res.pairs}

    def test_unscanned_sibling_does_not_make_parents_match(self):
        self.photos("d1/Photos/2019/Trip")
        self.photos("d1/Photos/2018/Old")  # never scanned
        self.photos("d2/Photos/2019/Trip")
        self.assertEqual(
            self.pairs("d1/Photos/2019", "d2/Photos/2019"),
            {("d1/Photos/2019", "d2/Photos/2019"): (IDENTICAL_TREE, 0)},
        )

    def test_full_scan_still_rolls_up(self):
        self.photos("d1/Photos/2019/Trip")
        self.photos("d2/Photos/2019/Trip")
        self.assertEqual(self.pairs("d1", "d2"), {("d1", "d2"): (IDENTICAL_TREE, 0)})

    def test_sidecar_file_not_identical_tree(self):
        self.photos("d1/Photos/2019/Trip")
        touch(self.p("d1/Photos/2019/Trip/IMG_0.xmp"))
        touch(self.p("d1/Photos/2019/Trip/._IMG_0.JPG"))  # AppleDouble is ignored
        self.photos("d2/Photos/2019/Trip")
        self.assertEqual(
            self.pairs("d1", "d2"),
            {("d1/Photos/2019/Trip", "d2/Photos/2019/Trip"): (IDENTICAL, 1)},
        )

    def test_subfolder_with_only_non_media_blocks_roll_up(self):
        self.photos("d1/Photos/Trip")
        touch(self.p("d1/Photos/Trip/edits/IMG_1.psd"))
        self.photos("d2/Photos/Trip")
        self.assertEqual(
            self.pairs("d1", "d2"),
            {("d1/Photos/Trip", "d2/Photos/Trip"): (IDENTICAL, 0)},
        )
        self.assertEqual(
            self.idx.subdirs(self.p("d1/Photos/Trip")), [self.p("d1/Photos/Trip/edits")]
        )

    def test_matching_non_media_subfolders_still_block_roll_up(self):
        for d in ("d1", "d2"):
            self.photos(f"{d}/Photos/Trip")
            touch(self.p(f"{d}/Photos/Trip/edits/IMG_1.psd"))
        self.assertEqual(
            self.pairs("d1", "d2"),
            {("d1/Photos/Trip", "d2/Photos/Trip"): (IDENTICAL, 0)},
        )

    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 directories")
    def test_unreadable_subfolder_blocks_roll_up(self):
        self.photos("d1/Photos/Trip")
        self.photos("d1/Photos/Trip/noperm", 1)
        self.photos("d2/Photos/Trip")
        os.chmod(self.p("d1/Photos/Trip/noperm"), 0)
        try:
            got = self.pairs("d1", "d2")
        finally:
            os.chmod(self.p("d1/Photos/Trip/noperm"), 0o755)
        self.assertEqual(got, {("d1/Photos/Trip", "d2/Photos/Trip"): (IDENTICAL, 0)})
        self.assertEqual(self.idx.dir_extras(self.p("d1/Photos/Trip/noperm")), (0, 1))


if __name__ == "__main__":
    unittest.main()
