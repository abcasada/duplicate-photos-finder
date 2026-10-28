import os
import tempfile
import unittest
from collections import defaultdict

from dpf.grouping import (
    A_IN_B,
    B_IN_A,
    IDENTICAL,
    IDENTICAL_TREE,
    NEAR_IDENTICAL,
    PARTIAL,
    TREE_A_MOSTLY_IN_B,
    TREE_NEAR,
    TREE_OVERLAP,
    FolderPair,
    group_duplicates,
    is_near,
    natural_key,
    tree_pairs,
    tree_tops,
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

    def run(self, groups=None, roots=("/",), trees=False, **kw):
        groups = by_name(self.records) if groups is None else groups
        if trees:
            kw["tree_totals"] = lambda d: self.idx.totals([d])
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


class ClosingTest(unittest.TestCase):
    def fixture(self, records):
        fx = Fixture(records)
        self.addCleanup(fx.idx.close)
        return fx


class NearIdenticalTest(ClosingTest):
    def pair(self, shared, only_a, only_b, **kw):
        a = imgs("/a", range(shared + only_a))
        b = imgs("/b", list(range(shared)) + list(range(1000, 1000 + only_b)))
        (p,) = self.fixture(a + b).run(**kw).pairs
        return p

    def test_is_near_boundaries(self):
        self.assertTrue(is_near(9, 10, 0.9))
        self.assertFalse(is_near(8, 10, 0.9))
        self.assertTrue(is_near(27, 30, 0.9))  # 0.9 * 30 is 27.000000000000004 in floats
        self.assertFalse(is_near(0, 0, 0.9))
        self.assertTrue(is_near(10, 10, 1.0))
        self.assertFalse(is_near(9_999, 10_000, 1.0))

    def test_both_sides_at_threshold(self):
        p = self.pair(9, 1, 1)
        self.assertEqual(
            (p.relation, p.matched_a, p.total_a, p.total_b), (NEAR_IDENTICAL, 9, 10, 10)
        )
        self.assertEqual(len(p.runs), 1)

    def test_one_side_below_threshold(self):
        self.assertEqual(self.pair(9, 1, 2).relation, PARTIAL)  # 9/11 of B
        self.assertEqual(self.pair(9, 2, 1).relation, PARTIAL)

    def test_custom_threshold_and_disabled(self):
        self.assertEqual(self.pair(8, 2, 2, near=0.8).relation, NEAR_IDENTICAL)
        self.assertEqual(self.pair(99, 1, 1, near=1.0).relation, PARTIAL)

    def test_contained_stays_contained(self):
        self.assertEqual(self.pair(19, 0, 1).relation, A_IN_B)
        self.assertEqual(self.pair(19, 1, 0).relation, B_IN_A)


class TreeTopsTest(unittest.TestCase):
    def test_strips_common_suffix(self):
        self.assertEqual(
            tree_tops("/m/3A/Photography/2025/X", "/m/3B/Photography backups/2025/X", ["/m"]),
            ("/m/3A/Photography", "/m/3B/Photography backups"),
        )

    def test_case_insensitive(self):
        self.assertEqual(
            tree_tops("/m/A/Trips/2019/Hawaii", "/m/B/trips/2019/HAWAII", ["/m"]),
            ("/m/A", "/m/B"),
        )

    def test_no_common_suffix(self):
        self.assertEqual(tree_tops("/m/A/x", "/m/B/y", ["/m"]), ("/m/A/x", "/m/B/y"))

    def test_stops_at_scanned_roots(self):
        roots = ["/m/3A", "/m/3B"]
        self.assertEqual(tree_tops("/m/3A/P/x", "/m/3B/P/x", roots), ("/m/3A", "/m/3B"))
        # /m is not scanned: stop at the roots themselves even though "x" would match.
        self.assertEqual(tree_tops("/m/a/x", "/m/b/x/x", ["/m/a", "/m/b"]), ("/m/a", "/m/b/x"))
        self.assertEqual(tree_tops("/m/a/x", "/n/x", ["/m/a", "/n/x"]), ("/m/a/x", "/n/x"))

    def test_ancestor_guard(self):
        self.assertEqual(
            tree_tops("/m/3A/Photography/2022", "/m/3A/Dumps/foo/Photography/2022", ["/m/3A"]),
            ("/m/3A/Photography", "/m/3A/Dumps/foo/Photography"),
        )
        self.assertEqual(tree_tops("/x/a", "/x/a/a", ["/"]), ("/x/a", "/x/a/a"))
        self.assertEqual(tree_tops("/x/p/s", "/y/p/s", ["/x/p", "/y"]), ("/x/p", "/y/p"))

    def test_never_same_folder(self):
        self.assertEqual(tree_tops("/r/a/s", "/r/A/s", ["/r"]), ("/r/a", "/r/A"))


class TreePairsTest(ClosingTest):
    @staticmethod
    def photos(root, *, skip=(), extra=()):
        files = []
        for sub, numbers in (("2019/Hawaii", range(1, 21)), ("2019/Paris", range(21, 41)),
                             ("2020/Home", range(41, 61))):  # fmt: skip
            files += [r for r in imgs(f"{root}/{sub}", numbers) if r.name not in skip]
        files += imgs(f"{root}/2021/x", range(100, 105)) + imgs(f"{root}/2021/y", range(200, 205))
        return files + [rec(f"{root}/{d}", n) for d, n in extra]

    def test_near_identical_tree(self):
        a = self.photos("/d1/Photos", extra=[("2022/Solo", "solo.jpg")])
        b = self.photos(
            "/d2/Photo backups", skip={"IMG_0030.JPG"}, extra=[("2020/Home", "new.jpg")]
        )
        res = self.fixture(a + b).run(trees=True)
        kinds = {p.dir_a[len("/d1/Photos/"):]: p.relation for p in res.pairs}
        self.assertEqual(
            kinds,
            {"2019/Hawaii": IDENTICAL, "2019/Paris": B_IN_A, "2020/Home": A_IN_B,
             "2021": IDENTICAL_TREE},
        )  # fmt: skip
        (t,) = res.trees
        self.assertEqual(
            (t.dir_a, t.dir_b, t.relation), ("/d1/Photos", "/d2/Photo backups", TREE_NEAR)
        )
        self.assertEqual(len(t.members), 4)
        self.assertEqual(t.folders, 5)  # the 2021 matching tree counts its two folders
        # A: 20 + 19 + 20 + 10 of 71 (Solo unpaired); B: 20 + 19 + 20 + 10 of 70.
        self.assertEqual((t.matched_a, t.total_a, t.matched_b, t.total_b), (69, 71, 69, 70))
        self.assertEqual(t.bytes, 69 * 100)

    def test_classification(self):
        a = self.photos("/d1/Photos", extra=[("Solo", f"s{i}.jpg") for i in range(30)])
        res = self.fixture(a + self.photos("/d2/Copy")).run(trees=True)
        (t,) = res.trees
        self.assertEqual((t.matched_a, t.total_a, t.matched_b, t.total_b), (70, 100, 70, 70))
        self.assertEqual(t.relation, "b_mostly_in_a")
        (t,) = self.fixture(a + self.photos("/d2/Copy")).run(trees=True, near=0.6).trees
        self.assertEqual(t.relation, TREE_NEAR)
        b = self.photos("/d2/Copy", extra=[("Solo2", f"t{i}.jpg") for i in range(30)])
        (t,) = self.fixture(a + b).run(trees=True).trees
        self.assertEqual(t.relation, TREE_OVERLAP)

    def test_single_pair_trees_not_listed(self):
        res = self.fixture(imgs("/d1/x", range(3)) + imgs("/d2/x", range(3))).run(trees=True)
        self.assertEqual(len(res.pairs), 1)
        self.assertEqual(res.trees, [])
        self.assertEqual(self.fixture(self.photos("/a") + self.photos("/b")).run().trees, [])

    def test_flipped_orientation(self):
        # "/m/a b/..." < "/m/a/...", but the tree pair is ("/m/a", "/m/a b").
        a = imgs("/m/a/x", range(10)) + imgs("/m/a/y", range(10, 20))
        b = imgs("/m/a b/x", range(8)) + imgs("/m/a b/y", range(10, 20))
        res = self.fixture(a + b).run(trees=True)
        self.assertEqual({p.dir_a for p in res.pairs}, {"/m/a b/x", "/m/a b/y"})
        (t,) = res.trees
        self.assertEqual((t.dir_a, t.dir_b), ("/m/a", "/m/a b"))
        self.assertEqual((t.matched_a, t.total_a, t.matched_b, t.total_b), (18, 20, 18, 18))
        self.assertEqual(t.relation, TREE_NEAR)

    def test_files_counted_once(self):
        def fp(a, b, matched_a, matched_b, relation=PARTIAL, folders=1):
            return FolderPair(a, b, (), matched_a, 99, matched_b, 99, matched_a, relation, folders)

        pairs = [
            fp("/a/T", "/b/T", 10, 10, IDENTICAL_TREE, 3),
            fp("/a/T/s", "/b/T/s", 5, 5),  # inside the matching tree: already counted
            fp("/a/U", "/b/U", 4, 4),
            fp("/a/U", "/b/u", 3, 4),  # same A folder twice: count its larger entry
        ]
        totals = {"/a": (14, 0), "/b": (30, 0)}
        (t,) = tree_pairs(pairs, ["/"], totals.__getitem__)
        self.assertEqual((t.matched_a, t.total_a, t.matched_b, t.total_b), (14, 14, 18, 30))
        self.assertEqual((t.folders, t.bytes), (4, 14))
        (t,) = tree_pairs(pairs, ["/"], {"/a": (12, 0), "/b": (16, 0)}.__getitem__)
        self.assertEqual((t.matched_a, t.matched_b), (12, 16))  # never above 100%

    def test_totals_called_once_per_dir(self):
        calls = []
        skip = {"IMG_0030.JPG"}
        q = self.photos("/d2/Q", skip=skip)
        fx = self.fixture(self.photos("/d1/P") + q + self.photos("/d3/R"))

        def totals(d):
            calls.append(d)
            return fx.idx.totals([d])

        trees = tree_pairs(fx.run().pairs, ["/"], totals)
        # P ≡ R rolls up into one matching tree; P ↔ Q and Q ↔ R share Q's total.
        self.assertEqual(
            {(t.dir_a, t.dir_b) for t in trees}, {("/d1/P", "/d2/Q"), ("/d2/Q", "/d3/R")}
        )
        self.assertEqual(sorted(calls), ["/d1/P", "/d2/Q", "/d3/R"])

    def test_root_boundary(self):
        a = self.photos("/d1/Photos", skip={"IMG_0030.JPG"})
        res = self.fixture(a + self.photos("/d2/Photos")).run(
            trees=True, roots=["/d1/Photos/2019", "/d1/Photos/2020", "/d2"]
        )
        tops = {(t.dir_a, t.dir_b) for t in res.trees}
        self.assertEqual(tops, {("/d1/Photos/2019", "/d2/Photos/2019")})
        (t,) = res.trees
        self.assertEqual(t.relation, TREE_NEAR)
        self.assertNotEqual(t.relation, TREE_A_MOSTLY_IN_B)


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


class SimilarNamesTest(unittest.TestCase):
    """Mode 1 with similar names: copies that differ by a suffix still read as runs."""

    def setUp(self):
        self.idx = Index(":memory:")

    def tearDown(self):
        self.idx.close()

    def result(self, records):
        from dpf.match_name import find_name_duplicates

        self.idx.replace_root("/m", records)
        groups = list(find_name_duplicates(self.idx, ["/m"]))
        return group_duplicates(
            groups, self.idx.list_dir, self.idx.subdirs, self.idx.dir_extras, roots=["/m"]
        )

    def test_suffixed_copies_form_one_run(self):
        from dpf.report import run_lines

        a = [rec("/m/A", f"DSC0000{i}.JPG", 100 + i) for i in range(1, 6)]
        b = [rec("/m/B", f"DSC0000{i} (1).JPG", 100 + i) for i in range(1, 6)]
        res = self.result(a + b)
        (pair,) = res.pairs
        self.assertEqual((pair.dir_a, pair.dir_b, pair.relation), ("/m/A", "/m/B", IDENTICAL))
        self.assertEqual(res.same_folder, [])
        partial = self.result(a + b[:4])
        self.assertEqual(
            run_lines(partial.pairs[0].runs, 8, indent=""),
            ["DSC00001.JPG … DSC00004.JPG  4 files  "
             "→ in B: DSC00001 (1).JPG … DSC00004 (1).JPG"],
        )  # fmt: skip

    def test_renamed_copies_keep_order(self):
        from dpf.report import run_lines

        a = [rec("/m/A", f"DSC0000{i}.ARW", 100 + i) for i in range(1, 4)]
        b = [rec("/m/B", f"2023-08-26_DSC0000{i}.ARW", 100 + i) for i in range(1, 4)]
        a.append(rec("/m/A", "DSC00009.ARW", 1))
        (pair,) = self.result(a + b).pairs
        self.assertEqual(pair.relation, B_IN_A)
        self.assertEqual(
            run_lines(pair.runs, 8, indent=""),
            ["DSC00001.ARW … DSC00003.ARW  3 files  → in B: 2023-08-26_DSC00001.ARW … "
             "2023-08-26_DSC00003.ARW"],
        )  # fmt: skip

    def test_same_format_siblings_split_runs(self):
        """X.DNG beside a matched X (2).DNG is a different file: it must end the run."""
        from dpf.report import run_lines

        a, b = [], []
        for i in range(1, 5):
            a.append(rec("/m/A", f"IMGQ594{i} (2).DNG", 100 + i))
            a.append(rec("/m/A", f"IMGQ594{i}.DNG", 200 + i))  # another shot, same counter
            b.append(rec("/m/B", f"IMGQ594{i}.DNG", 100 + i))
        (pair,) = self.result(a + b).pairs
        self.assertEqual(
            [(r.a_first, r.a_last, r.count, r.skipped) for r in pair.runs],
            [(f"IMGQ594{i} (2).DNG", f"IMGQ594{i} (2).DNG", 1, ()) for i in range(1, 5)],
        )
        self.assertEqual(
            run_lines(pair.runs[:1], 8, indent=""),
            ["IMGQ5941 (2).DNG  1 file   → in B: IMGQ5941.DNG"],
        )
        self.assertEqual((pair.matched_a, pair.total_a, pair.relation), (4, 8, B_IN_A))

    def test_unmatched_raw_inside_run_is_listed(self):
        """A holds DSC00001-5 as ARW+JPG, B all but DSC00003.ARW: the gap must show."""
        from dpf.report import run_lines

        a = [rec("/m/A", f"DSC0000{i}.{ext}", i * 10 + n) for i in range(1, 6)
             for n, ext in enumerate(("ARW", "JPG"))]  # fmt: skip
        b = [rec("/m/B", r.name, r.size) for r in a if r.name != "DSC00003.ARW"]
        (pair,) = self.result(a + b).pairs
        (run,) = pair.runs
        self.assertEqual(
            (run.a_first, run.a_last, run.count, run.skipped),
            ("DSC00001.ARW", "DSC00005.JPG", 9, ("DSC00003.ARW",)),
        )
        self.assertEqual((pair.relation, pair.matched_a, pair.total_a), (B_IN_A, 9, 10))
        self.assertEqual(
            run_lines(pair.runs, 8, indent=""),
            ["DSC00001.ARW … DSC00005.JPG  9 files  (+1 inside not matched: DSC00003.ARW)"],
        )

    def test_skipped_names_capped_in_text(self):
        from dpf.report import run_lines

        a = [rec("/m/A", f"DSC0000{i}.{ext}", i * 10 + n) for i in range(1, 8)
             for n, ext in enumerate(("ARW", "JPG"))]  # fmt: skip
        b = [rec("/m/B", r.name, r.size) for r in a if r.name.endswith(".JPG")]
        (pair,) = self.result(a + b).pairs
        (run,) = pair.runs
        # Each ARW sorts just before its matched JPG, so only DSC00001.ARW lies outside.
        self.assertEqual(run.skipped, tuple(f"DSC0000{i}.ARW" for i in range(2, 8)))
        self.assertEqual(
            run_lines(pair.runs, 8, indent=""),
            ["DSC00001.JPG … DSC00007.JPG  7 files  (+6 inside not matched: DSC00002.ARW, "
             "DSC00003.ARW, DSC00004.ARW, +3 more)"],
        )  # fmt: skip

    def test_siblings_outside_a_run_are_not_listed(self):
        a = [rec("/m/A", "X1.ARW", 1), rec("/m/A", "X1.JPG", 2), rec("/m/A", "X2.ARW", 3)]
        a += [rec("/m/A", "X2.JPG", 4), rec("/m/A", "Y.JPG", 5)]
        b = [rec("/m/B", "X1.ARW", 1), rec("/m/B", "X2.ARW", 3)]
        (pair,) = self.result(a + b).pairs
        # X2.JPG trails the run and Y.JPG ends it: neither lies inside X1.ARW … X2.ARW.
        self.assertEqual(
            [(r.a_first, r.a_last, r.skipped) for r in pair.runs],
            [("X1.ARW", "X2.ARW", ("X1.JPG",))],
        )

    def test_raw_jpg_siblings_do_not_split_runs(self):
        a = [rec("/m/A", f"DSC0000{i}.{ext}", i * 10 + n) for i in range(1, 5)
             for n, ext in enumerate(("ARW", "JPG"))]  # fmt: skip
        b = [r for r in a if r.name.endswith(".ARW")]
        b = [rec("/m/B", r.name, r.size) for r in b]
        a.append(rec("/m/A", "DSC00005.ARW", 999))
        a.append(rec("/m/A", "DSC00006.ARW", 999))
        b.append(rec("/m/B", "DSC00006.ARW", 999))
        (pair,) = self.result(a + b).pairs
        self.assertEqual(
            [(r.a_first, r.a_last, r.count, r.skipped) for r in pair.runs],
            [("DSC00001.ARW", "DSC00004.ARW", 4,
              ("DSC00001.JPG", "DSC00002.JPG", "DSC00003.JPG")),
             ("DSC00006.ARW", "DSC00006.ARW", 1, ())],
        )  # fmt: skip
        jpg_first = [rec("/m/A", "X1.JPG", 1), rec("/m/A", "X2.ARW", 5), rec("/m/A", "X2.JPG", 2)]
        jpg_first += [rec("/m/B", "X1.JPG", 1), rec("/m/B", "X2.JPG", 2)]
        (pair,) = self.result(jpg_first).pairs
        self.assertEqual(
            [(r.a_first, r.a_last, r.skipped) for r in pair.runs],
            [("X1.JPG", "X2.JPG", ("X2.ARW",))],
        )

    def test_similar_names_flag(self):
        from dpf.report import run_lines

        exact = [rec("/m/A", "DSC00001.JPG"), rec("/m/B", "DSC00001.JPG")]
        (pair,) = self.result(exact).pairs
        self.assertFalse(pair.similar_names)
        case = [rec("/m/A", "DSC00001.JPG"), rec("/m/B", "dsc00001.jpg")]
        (pair,) = self.result(case).pairs
        self.assertTrue(pair.similar_names)
        self.assertEqual(
            run_lines(pair.runs, 8, indent=""), ["DSC00001.JPG  1 file   → in B: dsc00001.jpg"]
        )
        # A's file has an exact copy in B, but B's "(1)" copy is matched by a similar name.
        extra = exact + [rec("/m/B", "DSC00001 (1).JPG")]
        pair = pair_map(self.result(extra))[("/m/A", "/m/B")]
        self.assertTrue(pair.runs[0].same_names)
        self.assertTrue(pair.similar_names)

    def test_similar_names_flag_survives_tree_roll_up(self):
        records = [rec("/m/A/x", "DSC00001.JPG"), rec("/m/B/x", "DSC00001.JPG")]
        records += [rec("/m/A/y", "DSC00002.JPG"), rec("/m/B/y", "DSC00002 (1).JPG")]
        (pair,) = self.result(records).pairs
        self.assertEqual((pair.dir_a, pair.dir_b, pair.relation), ("/m/A", "/m/B", IDENTICAL_TREE))
        self.assertTrue(pair.similar_names)
        (pair,) = self.result(records[:2] + [rec("/m/A/y", "Z.JPG"), rec("/m/B/y", "Z.JPG")]).pairs
        self.assertEqual(pair.relation, IDENTICAL_TREE)
        self.assertFalse(pair.similar_names)

    def test_copy_in_same_folder(self):
        res = self.result([rec("/m/A", "DSC0001.JPG"), rec("/m/A", "DSC0001 (1).JPG")])
        self.assertEqual(res.pairs, [])
        (group,) = res.same_folder
        self.assertEqual([f.name for f in group.files], ["DSC0001 (1).JPG", "DSC0001.JPG"])
        self.assertEqual(group.basis, "similar name+size+mtime")


if __name__ == "__main__":
    unittest.main()


class HourShiftGroupingTest(unittest.TestCase):
    """Pairs and runs carry the whole-hour offsets (B after A) their matches relied on."""

    def test_pair_and_run_offsets(self):
        a = [rec("/a", f"IMG_{n}.JPG", 10 + n) for n in range(1, 7)]
        b = [rec("/b", f"IMG_{n}.JPG", 10 + n, mtime=1_000_000.0 + (3600 if n > 3 else 0))
             for n in range(1, 7)]  # fmt: skip
        f = Fixture(a + b)
        groups = [
            DuplicateGroup((x, y), "t", (0, 1) if y.mtime != x.mtime else ())
            for x, y in zip(a, b)
        ]
        p = f.run(groups).pairs[0]
        self.assertEqual(p.relation, IDENTICAL)
        self.assertEqual(p.hour_shift, (1,))
        self.assertEqual(p.runs[0].hour_shift, (1,))  # one run, files 4-6 shifted

    def test_offset_sign_and_unshifted_copy(self):
        x = rec("/a", "x.jpg", mtime=10_000.0)
        y = rec("/b", "x.jpg", mtime=10_000.0 - 6 * 3600)
        z = rec("/c", "x.jpg", mtime=10_000.0)
        f = Fixture([x, y, z])
        # sorted by path: /a (+6 h after /b's), /b (earliest), /c (+6 h)
        result = f.run([DuplicateGroup((x, y, z), "t", (6, 0, 6))])
        pairs = pair_map(result)
        self.assertEqual(pairs[("/a", "/b")].hour_shift, (-6,))
        self.assertEqual(pairs[("/b", "/c")].hour_shift, (6,))
        self.assertEqual(pairs[("/a", "/c")].hour_shift, ())
        self.assertEqual(result.hour_shift_groups, 1)

    def test_shift_survives_tree_roll_up(self):
        recs, groups = [], []
        for sub in ("x", "y"):
            for n in range(3):
                a = rec(f"/a/T/{sub}", f"{n}.jpg", 10 + n)
                b = rec(f"/b/T/{sub}", f"{n}.jpg", 10 + n, mtime=1_000_000.0 + 3600)
                recs += [a, b]
                groups.append(DuplicateGroup((a, b), "t", (0, 1)))
        (p,) = Fixture(recs).run(groups).pairs
        self.assertEqual((p.relation, p.dir_a, p.hour_shift), (IDENTICAL_TREE, "/a", (1,)))


class CategoryGroupingTest(unittest.TestCase):
    def setUp(self):
        from dpf.categories import Categories

        self.cats = Categories(["Plex"])

    def test_pairs_tagged(self):
        from dpf.categories import OTHER, PERSONAL

        records = imgs("/a/Plex", range(3)) + imgs("/b/Plex/x", range(3)) + imgs("/c/Photos", [0])
        result = Fixture(records).run(category=self.cats)
        pairs = pair_map(result)
        self.assertEqual(pairs[("/a/Plex", "/b/Plex/x")].category, OTHER)
        self.assertEqual(pairs[("/a/Plex", "/c/Photos")].category, PERSONAL)
        self.assertEqual(pairs[("/b/Plex/x", "/c/Photos")].category, PERSONAL)
        other = result.only(OTHER)
        self.assertEqual([(p.dir_a, p.dir_b) for p in other.pairs], [("/a/Plex", "/b/Plex/x")])
        self.assertEqual(len(result.only(PERSONAL).pairs), 2)
        self.assertEqual(Fixture(records).run().pairs[0].category, PERSONAL)

    def test_roll_up_stops_at_category_boundary(self):
        from dpf.categories import OTHER, PERSONAL

        records = []
        for top in ("/a/T", "/b/T"):
            records += imgs(f"{top}/Photos", range(3)) + imgs(f"{top}/Plex", range(10, 13))
        plain = Fixture(records).run()
        self.assertEqual([p.relation for p in plain.pairs], [IDENTICAL_TREE])
        result = Fixture(records).run(category=self.cats)
        got = {(p.dir_a, p.relation, p.category) for p in result.pairs}
        self.assertEqual(got, {
            ("/a/T/Photos", IDENTICAL, PERSONAL),
            ("/a/T/Plex", IDENTICAL, OTHER),
        })  # fmt: skip

    def test_same_folder_and_trees(self):
        from dpf.categories import OTHER, PERSONAL

        records = imgs("/d/Plex", [1]) + [rec("/d/Plex", "IMG_0001 (1).JPG")]
        group = DuplicateGroup(tuple(records), "t")
        result = Fixture(records).run([group], category=self.cats)
        self.assertEqual(result.category_of(group), OTHER)
        self.assertEqual(result.only(PERSONAL).same_folder, [])
        self.assertEqual(result.only(OTHER).same_folder, [group])
