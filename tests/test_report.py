import csv
import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime

from dpf.coverage import NOT_BACKED_UP, NOT_IN_OTHER, ONLY_IN_BACKUP, check_coverage
from dpf.grouping import group_duplicates
from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord
from dpf.report import (
    OFFLINE_NOTE,
    format_text,
    human_bytes,
    parse_bytes,
    root_lines,
    format_coverage,
    write_csv,
    write_json,
    write_missing_csv,
    write_pool_csv,
    write_pools_json,
)

from tests.test_grouping import by_name, imgs, rec

SUMMARY = {
    "mode": "1",
    "basis": "name+size+mtime ±2s",
    "roots": ["/drives/one", "/drives/two"],
    "files": 1234,
    "bytes": 3_100_000_000_000,
    "elapsed": 12.34,
}


def build():
    big = 10_000_000
    one, two = "/drives/one", "/drives/two"
    records = imgs(f"{one}/Photos/2019/Hawaii", range(1, 13), big)
    records += imgs(f"{two}/Backup/Hawaii", range(1, 13), big)
    records += [rec(f"{two}/Backup/Other", "unique.jpg")]
    # DCIM shares two sequences with Sorted, which also holds files of its own.
    records += imgs(f"{one}/DCIM/100CANON", range(100, 160), big // 2)
    shared = list(range(100, 140)) + list(range(150, 155))
    records += imgs(f"{two}/Sorted/2016-07", shared, big // 2)
    records += imgs(f"{two}/Sorted/2016-07", range(900, 905), big // 2)
    groups = by_name(records)
    # Renamed copies (content match), as mode 2 would find; Renamed has one extra file.
    edits = [rec(f"{one}/Edits", f"E{n}.jpg", 3_000) for n in range(1, 4)]
    renamed = [rec(f"{two}/Renamed", f"Trip {n:03d}.jpg", 3_000) for n in range(1, 5)]
    groups += [DuplicateGroup((e, r), "sha256") for e, r in zip(edits, renamed)]
    phone = [rec(f"{one}/Phone", "PXL_1.jpg", 1_000), rec(f"{one}/Phone", "PXL_1 (1).jpg", 1_000)]
    groups.append(DuplicateGroup(tuple(phone), "sha256"))
    idx = Index(":memory:")
    idx.replace_root("/drives", records + edits + renamed + phone)
    return group(groups, idx, ["/drives"]), groups


def group(groups, idx, roots=("/",), **kw):
    return group_duplicates(groups, idx.list_dir, idx.subdirs, idx.dir_extras, roots=roots, **kw)


def build_trees():
    """Two near-identical trees, plus a smaller pair of trees that overlap a little."""
    records = []
    for root, skip in (("/d1/Photos", None), ("/d2/Photo backups", "IMG_0030.JPG")):
        for i, sub in enumerate(("2019/Hawaii", "2019/Paris", "2020/Home")):
            records += [
                r for r in imgs(f"{root}/{sub}", range(i * 20, i * 20 + 20), 1_000_000)
                if r.name != skip
            ]
    records += imgs("/d1/Misc/a", range(500, 510)) + imgs("/d2/Old/a", range(500, 503))
    records += imgs("/d1/Misc/b", range(600, 610)) + imgs("/d2/Old/b", range(600, 603))
    records += imgs("/d1/Misc/c", range(700, 790))
    idx = Index(":memory:")
    idx.replace_root("/", records)
    with idx:
        return group(by_name(records), idx, tree_totals=lambda d: idx.totals([d]))


class HumanBytesTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(999), "999 B")
        self.assertEqual(human_bytes(1000), "1.0 kB")
        self.assertEqual(human_bytes(12_400_000_000), "12.4 GB")
        self.assertEqual(human_bytes(3_100_000_000_000), "3.1 TB")
        self.assertEqual(human_bytes(999_960_000), "1.0 GB")

    def test_parse_bytes(self):
        for text, expected in (
            ("5.09TB", 5_090_000_000_000), ("8.00 TB", 8 * 10**12), ("500GB", 500 * 10**9),
            ("123", 123), ("123 B", 123), ("1kB", 1000), (" .5mb ", 500_000), ("2PB", 2 * 10**15),
        ):  # fmt: skip
            self.assertEqual(parse_bytes(text), expected, text)
        for text in ("", "-1", "5T", "4.7TiB", "1.2.3GB", "GB", "1 GiB", "1e3"):
            with self.subTest(text), self.assertRaises(ValueError):
                parse_bytes(text)


class TextReportTest(unittest.TestCase):
    def setUp(self):
        self.result, self.groups = build()

    def test_layout(self):
        text = format_text(self.result, {**SUMMARY, "csv_path": "/tmp/r.csv"})
        self.assertIn("Mode:        1; matched on name+size+mtime ±2s", text)
        self.assertIn("Roots:       /drives/one\n             /drives/two", text)
        self.assertNotIn("Pool:", text)
        pooled = format_text(self.result, {**SUMMARY, "pool": "A"})
        self.assertIn("matched on name+size+mtime ±2s\nPool:        A\nRoots:", pooled)
        self.assertIn("Scanned:     1,234 files, 3.1 TB", text)
        self.assertIn("could free ~", text)
        self.assertIn("Elapsed:     12.3 s", text)
        self.assertIn(
            "[1]  225.0 MB  /drives/one/DCIM/100CANON  ↔  /drives/two/Sorted/2016-07"
            "   (partial overlap)",
            text,
        )
        self.assertIn("IMG_0100.JPG … IMG_0139.JPG  40 files", text)
        self.assertIn("IMG_0150.JPG … IMG_0154.JPG   5 files", text)
        self.assertIn("covers 75% of A (45/60), 90% of B (45/50)", text)
        self.assertIn(
            "/drives/one/Photos/2019/Hawaii  ↔  /drives/two/Backup/Hawaii"
            "   (all photos/videos match, 12 files)",
            text,
        )
        self.assertIn(
            "/drives/one/Edits  ↔  /drives/two/Renamed   (A is fully contained in B; "
            "similar names)",
            text,
        )
        self.assertNotIn("Hawaii   (all photos/videos match, 12 files; similar", text)
        self.assertIn("E1.jpg … E3.jpg  3 files  → in B: Trip 001.jpg … Trip 003.jpg", text)
        self.assertIn("covers 100% of A (3/3), 75% of B (3/4)", text)
        self.assertIn("Same-folder duplicates: 1 group, 1.0 kB redundant, in 1 folder", text)
        self.assertIn("       1 group     1.0 kB  /drives/one/Phone", text)
        self.assertIn("CSV:  /tmp/r.csv", text)
        self.assertNotIn("XLSX:", text)
        self.assertNotIn("IMG_0120", text)  # never every file
        text = format_text(self.result, {**SUMMARY, "xlsx_path": "/tmp/r.xlsx"})
        self.assertIn("Per-file detail:\n  XLSX: /tmp/r.xlsx", text)

    def test_skipped_files_in_hidden_runs_are_counted(self):
        a_names = ["A1.ARW", "A1.JPG", "A2.ARW", "B1.JPG", "C1.ARW", "C1.JPG", "C2.ARW"]
        records = [rec("/a", n, size=i + 1) for i, n in enumerate(a_names)]
        records += [rec("/b", n, size=i + 1) for i, n in enumerate(a_names) if n.endswith("ARW")]
        with Index(":memory:") as idx:
            idx.replace_root("/", records)
            result = group(by_name(records), idx)
        text = format_text(result, SUMMARY, max_runs=1)
        self.assertIn("A1.ARW … A2.ARW  2 files  (+1 inside not matched: A1.JPG)\n", text)
        self.assertIn("… and 1 more run (2 files; 1 inside not matched)", text)

    def test_single_file_run_and_max_runs(self):
        records = imgs("/a", range(0, 40)) + imgs("/b", range(0, 40, 2))
        idx = Index(":memory:")
        idx.replace_root("/", records)
        result = group(by_name(records), idx)
        text = format_text(result, SUMMARY, max_runs=3)
        self.assertIn("       IMG_0000.JPG  1 file\n", text)
        self.assertIn("… and 17 more runs (17 files)", text)
        self.assertIn("covers 50% of A (20/40), 100% of B (20/20)", text)

    def test_top_and_min_files(self):
        text = format_text(self.result, SUMMARY, top=1)
        self.assertIn("[1]", text)
        self.assertNotIn("[2]", text)
        self.assertIn("… 2 more folder pairs not shown (top 1)", text)
        text = format_text(self.result, SUMMARY, min_files=10)
        self.assertNotIn("Renamed", text)
        self.assertIn("1 folder pair with fewer than 10 matched files hidden", text)

    def test_identical_tree_line(self):
        records = []
        for root in ("/x/Photos", "/y/Copy/Photos"):
            for sub in ("a", "b/c"):
                records += [rec(f"{root}/{sub}", f"{sub[0]}{n}.jpg", 1_000_000) for n in range(3)]
        records.append(rec("/y/Copy/keep.jpg", "k.jpg"))
        idx = Index(":memory:")
        idx.replace_root("/", records)
        result = group(by_name(records), idx)
        text = format_text(result, SUMMARY)
        self.assertIn(
            "[1]    6.0 MB  /x/Photos  ↔  /y/Copy/Photos   (matching tree: 2 folders, 6 files)",
            text,
        )

    def test_near_identical_labels(self):
        records = imgs("/a", range(20)) + imgs("/b", list(range(19)) + [900])
        records += imgs("/c", range(100, 120)) + imgs("/d", range(100, 119))
        idx = Index(":memory:")
        self.addCleanup(idx.close)
        idx.replace_root("/", records)
        text = format_text(group(by_name(records), idx), SUMMARY)
        self.assertIn(
            "/a  ↔  /b   (near-identical: 95% / 95%; 1 file only in A, 1 only in B)", text
        )
        self.assertIn("IMG_0000.JPG … IMG_0018.JPG  19 files", text)
        self.assertIn(
            "/c  ↔  /d   (B is fully contained in A; near-identical: 95% / 100%, "
            "1 file only in A)",
            text,
        )
        text = format_text(group(by_name(records), idx, near=1.0), SUMMARY)
        self.assertIn("/a  ↔  /b   (partial overlap)", text)
        self.assertIn("/c  ↔  /d   (B is fully contained in A)\n", text)

    def test_tree_overview(self):
        text = format_text(build_trees(), SUMMARY)
        overview = text[text.index("Tree overview") : text.index("Folder pairs sharing")]
        self.assertIn("Tree overview: 2 tree pairs holding several folder pairs", overview)
        self.assertIn("near-identical = at least 90% of both", overview)
        self.assertIn(
            "[T1]   59.0 MB  /d1/Photos  ↔  /d2/Photo backups   "
            "(near-identical trees: 98% / 100%, 3 folder pairs, 59 files)\n"
            "       covers 98% of A (59/60), 100% of B (59/59)\n",
            overview,
        )
        self.assertIn(
            "[T2]     600 B  /d1/Misc  ↔  /d2/Old   "
            "(B within A: 5% / 100%, 2 folder pairs, 6 files)\n"
            "       covers 5% of A (6/110), 100% of B (6/6)\n",
            overview,
        )
        details = text[text.index("Folder pairs sharing") :]
        self.assertIn("/d1/Photos/2019/Paris  ↔  /d2/Photo backups/2019/Paris", details)
        self.assertIn(
            "(B is fully contained in A; near-identical: 95% / 100%, 1 file only in A)  (in T1)",
            details,
        )
        self.assertIn("/d1/Misc/a  ↔  /d2/Old/a   (B is fully contained in A)  (in T2)", details)

    def test_tree_top(self):
        result = build_trees()
        text = format_text(result, SUMMARY, tree_top=1)
        self.assertIn("[T1]", text)
        self.assertNotIn("[T2]", text)
        self.assertNotIn("(in T2)", text)
        self.assertIn("… 1 more tree pair not shown (tree top 1)", text)
        text = format_text(result, SUMMARY, tree_top=0)
        self.assertNotIn("Tree overview", text)
        self.assertNotIn("(in T", text)

    def test_root_status(self):
        when = datetime(2026, 10, 24, 21, 43).timestamp()
        status = [
            {"root": "/drives/one", "status": "scanned now", "connected": True,
             "scanned_at": when + 60, "files": 1, "bytes": 2},
            {"root": "/drives/two-long", "status": "from index", "connected": False,
             "scanned_at": when, "files": 3, "bytes": 4},
            {"root": "/drives/sub", "status": "from index", "connected": True,
             "scanned_at": None, "files": 0, "bytes": 0},
        ]  # fmt: skip
        text = format_text(self.result, {**SUMMARY, "root_status": status})
        self.assertIn(
            "Roots:       /drives/one        (scanned now)\n"
            "             /drives/two-long   "
            "(from index, scanned 2026-10-24 21:43, not connected)\n"
            "             /drives/sub        (from index, date unknown)\n"
            f"             {OFFLINE_NOTE}\n"
            "Scanned:",
            text,
        )
        status[1]["connected"] = True
        text = format_text(self.result, {**SUMMARY, "root_status": status})
        self.assertNotIn(OFFLINE_NOTE, text)
        self.assertEqual(
            root_lines({"root_status": status[:2]}, align=False),
            [
                "/drives/one   (scanned now)",
                "/drives/two-long   (from index, scanned 2026-10-24 21:43)",
            ],
        )
        self.assertEqual(root_lines(SUMMARY), SUMMARY["roots"])

    def test_no_duplicates(self):
        result = group_duplicates([], lambda d: [], lambda d: [], lambda d: (0, 0), roots=["/"])
        text = format_text(result, {**SUMMARY, "elapsed": 3725.0})
        self.assertIn("No duplicates shared between folders.", text)
        self.assertIn("Elapsed:     1h 02m 05s", text)
        self.assertIn("Per-file detail is in the CSV/JSON reports.", text)


class FileOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_loads(self):
        result, _ = build()
        path = os.path.join(self.tmp.name, "r.json")
        write_json(result, SUMMARY, path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["groups"], result.groups)
        self.assertEqual(data["summary"]["roots"], SUMMARY["roots"])
        self.assertEqual(len(data["pairs"]), len(result.pairs))
        first = data["pairs"][0]
        self.assertEqual(first["relation"], "partial")
        self.assertIsInstance(first["bytes"], int)
        self.assertEqual(first["runs"][0]["count"], 40)
        self.assertEqual(
            sorted(f["path"] for f in data["same_folder"][0]["files"]),
            ["/drives/one/Phone/PXL_1 (1).jpg", "/drives/one/Phone/PXL_1.jpg"],
        )
        self.assertFalse(data["same_folder"][0]["same_names"])
        self.assertEqual(first["runs"][0]["skipped"], [])
        flags = {(p["dir_a"].rsplit("/", 1)[1], p["similar_names"]) for p in data["pairs"]}
        self.assertEqual(flags, {("100CANON", False), ("Hawaii", False), ("Edits", True)})

    def test_json_trees(self):
        result = build_trees()
        path = os.path.join(self.tmp.name, "t.json")
        write_json(result, SUMMARY, path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["near"], 0.9)
        t = data["trees"][0]
        self.assertEqual((t["dir_a"], t["dir_b"]), ("/d1/Photos", "/d2/Photo backups"))
        self.assertEqual(t["relation"], "near_identical")
        counts = (t["matched_a"], t["total_a"], t["matched_b"], t["total_b"])
        self.assertEqual(counts, (59, 60, 59, 59))
        self.assertEqual((t["folders"], len(t["members"]), t["bytes"]), (3, 3, 59_000_000))
        self.assertIn(["/d1/Photos/2019/Paris", "/d2/Photo backups/2019/Paris"], t["members"])
        self.assertEqual(data["trees"][1]["relation"], "b_mostly_in_a")

    def test_csv_round_trip(self):
        mtime = 1_700_000_000.0
        a = FileRecord("/m/Trip, \"2019\"", "Café ünïcode.jpg", 123, mtime, "image")
        b = FileRecord("/n/plain", "Café ünïcode.jpg", 123, mtime + 1, "image")
        c = FileRecord("/n/plain", "v.mp4", 9, mtime, "video")
        d = FileRecord("/m/x", "v.mp4", 9, mtime, "video")
        path = os.path.join(self.tmp.name, "r.csv")
        groups = [DuplicateGroup((a, b), "name+size+mtime"), DuplicateGroup((c, d), "sha256")]
        n = write_csv(iter(groups), path)
        self.assertEqual(n, 2)
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["path"], a.path)
        self.assertEqual(rows[0]["dir"], a.dir)
        self.assertEqual(rows[0]["name"], "Café ünïcode.jpg")
        self.assertEqual((rows[0]["group_id"], rows[3]["group_id"]), ("1", "2"))
        self.assertEqual(rows[3]["basis"], "sha256")
        self.assertEqual(rows[3]["kind"], "video")
        self.assertEqual([r["same_names"] for r in rows], ["yes", "yes", "yes", "yes"])
        copy = FileRecord("/z", "Café ünïcode (1).jpg", 123, mtime, "image")
        similar = [DuplicateGroup((a, copy), "similar name+size+mtime")]
        write_csv(similar, path)
        with open(path, encoding="utf-8", newline="") as fh:
            self.assertEqual({r["same_names"] for r in csv.DictReader(fh)}, {"no"})
        self.assertEqual(int(rows[0]["size"]), 123)
        self.assertEqual(datetime.fromisoformat(rows[1]["mtime"]).timestamp(), mtime + 1)

    def test_csv_empty(self):
        path = os.path.join(self.tmp.name, "e.csv")
        self.assertEqual(write_csv([], path), 0)


def build_coverage(direction=NOT_BACKED_UP, pool="A", other="B"):
    """Pool A (/a) against pool B (/b): a whole missing tree, a partial folder, an empty file."""
    a = [rec("/a/Trip/day1", f"IMG_{n}.JPG", 1_000_000) for n in range(1, 4)]
    a += [rec("/a/Trip/day2", f"IMG_{n}.JPG", 2_000_000) for n in range(1, 3)]
    a += [rec("/a/DCIM", f"DSC{n:05d}.ARW", 100) for n in range(1, 11)]
    a += [rec("/a/DCIM", "empty.jpg", 0)]
    b = [rec("/b/x", f"DSC{n:05d}.ARW", 100) for n in (1, 2, 6)]
    b.append(rec("/b/x", "DSC00010.ARW", 108))  # same name, other size
    idx = Index(":memory:")
    idx.replace_root("/a", a)
    idx.replace_root("/b", b)
    try:
        return check_coverage(idx, ["/a"], ["/b"], pool=pool, other=other, direction=direction)
    finally:
        idx.close()


class CoverageReportTest(unittest.TestCase):
    def setUp(self):
        self.cov = build_coverage()

    def test_text(self):
        text = format_coverage(self.cov)
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["Not backed up — no copy anywhere in pool B", "=" * 42])
        self.assertIn("Compared:       pool B is the backup of pool A; similar name+size", text)
        self.assertIn(
            "Matched by similar name, size and date (or a date whole hours apart), not by "
        "contents; a renamed-and-re-saved "
            "copy counts as missing.",
            text,
        )
        self.assertIn(
            "Not backed up:  12 files, 7.0 MB, in 3 folders (+ 1 empty file not compared)\n"
            "                80% of the 15 media files compared in pool A\n"
            "                1 of them has a file with a similar name in pool B",
            text,
        )
        self.assertIn(
            "[1]    7.0 MB  /a/Trip   (whole tree missing from B: 2 folders, 5 files, 7.0 MB)",
            text,
        )
        self.assertIn(
            "[2]     700 B  /a/DCIM   (7 of 10 files missing from B; 1 with a similar name there)\n"
            "       DSC00003.ARW … DSC00005.ARW  3 files\n"
            "       DSC00007.ARW … DSC00010.ARW  4 files\n",
            text,
        )
        top = format_coverage(self.cov, top=1, max_runs=1)
        self.assertIn("… 1 more folder (7 files, 700 B) not shown (coverage top 1)", top)
        self.assertNotIn("DSC00003", top)

    def test_other_directions(self):
        back = build_coverage(ONLY_IN_BACKUP, "B", "A")
        text = format_coverage(back)
        self.assertTrue(text.startswith("Only in the backup — no copy anywhere in pool A\n"))
        self.assertIn("Compared:       pool B is the backup of pool A;", text)
        self.assertIn("Only in backup: 12 files", text)
        paths = format_coverage(build_coverage(NOT_IN_OTHER, None, "B"))
        self.assertTrue(paths.startswith("Not in pool B — no copy anywhere in pool B\n"))
        self.assertIn("Compared:       these folders compared with pool B;", paths)
        self.assertIn("Not in B:       12 files", paths)

    def test_floored_percent_and_never_scanned(self):
        cov = dataclasses.replace(self.cov, files=1_201, never_scanned=["/m/Storage 9B"])
        cov.missing = cov.missing[:1]
        text = format_coverage(cov)
        self.assertIn("<1% of the 1,201 media files compared", text)
        self.assertIn("1 drive of pool B never scanned", text)
        self.assertIn("\n                  /m/Storage 9B\n", text)
        full = dataclasses.replace(self.cov, files=13)
        full.missing = full.missing + [rec("/a/x", "y.jpg")]
        self.assertIn("100% of the 13 media files", format_coverage(full))
        nearly = dataclasses.replace(self.cov, files=1_300)
        nearly.missing = [rec("/a/x", f"{n}.jpg") for n in range(1_299)]
        self.assertIn("99% of the 1,300 media files", format_coverage(nearly))

    def test_nothing_missing(self):
        cov = dataclasses.replace(self.cov, missing=[], folders=[], similar=set())
        self.assertIn("Every compared media file has a copy in pool B.", format_coverage(cov))

    def test_json_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            result, _ = build()
            write_json(result, SUMMARY, path, coverage=[self.cov])
            with open(path) as fh:
                data = json.load(fh)
            cov = data["coverage"]["A not in B"]
            self.assertEqual(
                (cov["missing_files"], cov["missing_bytes"], cov["missing_folders"],
                 cov["empty_files"], cov["files_compared"], cov["similar_name_files"]),
                (12, 7_000_700, 3, 1, 15, 1),
            )  # fmt: skip
            self.assertEqual(cov["direction"], NOT_BACKED_UP)
            self.assertEqual(cov["folders"][0]["dir"], "/a/Trip")
            self.assertTrue(cov["folders"][0]["whole_tree"])
            write_json(result, SUMMARY, path)
            with open(path) as fh:
                self.assertNotIn("coverage", json.load(fh))

            csv_path = os.path.join(tmp, "missing.csv")
            back = build_coverage(ONLY_IN_BACKUP, "B", "A")
            self.assertEqual(write_missing_csv([self.cov, back], csv_path), 24)
            with open(csv_path, newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 24)
            self.assertEqual(
                {k: rows[0][k] for k in ("pool", "missing_from", "path", "size", "kind")},
                {"pool": "A", "missing_from": "B", "path": "/a/DCIM/DSC00003.ARW",
                 "size": "100", "kind": "image"},
            )  # fmt: skip
            self.assertEqual(datetime.fromisoformat(rows[0]["mtime"]).timestamp(), 1_000_000)
            similar = [r["path"] for r in rows if r["similar_name_there"] == "yes"]
            self.assertEqual(similar, ["/a/DCIM/DSC00010.ARW"] * 2)
            self.assertEqual(rows[-1]["pool"], "B")

    def test_pool_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, groups = build()
            path = os.path.join(tmp, "files.csv")
            self.assertEqual(write_pool_csv([("A", groups), ("B", groups[:1])], path),
                             len(groups) + 1)  # fmt: skip
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0])[:2], ["pool", "group_id"])
            self.assertEqual({r["pool"] for r in rows}, {"A", "B"})
            self.assertEqual([r["group_id"] for r in rows if r["pool"] == "B"], ["1", "1"])
            path = os.path.join(tmp, "report.json")
            write_pools_json(
                [("A", result, {**SUMMARY, "pool": "A"}), ("B", result, SUMMARY)], [self.cov],
                {"pools": ["A", "B"]}, path,
            )  # fmt: skip
            with open(path) as fh:
                data = json.load(fh)
            self.assertEqual(list(data["pools"]), ["A", "B"])
            self.assertEqual(data["pools"]["A"]["summary"]["pool"], "A")
            self.assertEqual(list(data["coverage"]), ["A not in B"])



def build_categories():
    """Personal duplicates (Photos), other-media duplicates (Plex) and an hour-shifted pair."""
    from dpf.categories import Categories
    from dpf.match_name import find_name_duplicates

    t = 1_600_000_000.0
    records = [rec(f"/d/{side}/Photos", f"IMG_{n}.JPG", 1000 + n, mtime=t + n)
               for side in ("one", "two") for n in range(1, 4)]  # fmt: skip
    records += [rec(f"/d/{side}/Plex", f"m{n}.mkv", 5000 + n, mtime=t + n)
                for side in ("one", "two") for n in range(1, 3)]  # fmt: skip
    records += [rec("/d/one/Trip", "DSC00001.ARW", 70, mtime=t),
                rec("/d/two/Trip", "DSC00001.ARW", 70, mtime=t + 3600)]  # fmt: skip
    idx = Index(":memory:")
    idx.replace_root("/d", records)
    cats = Categories(["Plex"])
    groups = list(find_name_duplicates(idx, ["/d"]))
    result = group(groups, idx, ["/d"], category=cats, tree_totals=lambda d: idx.totals([d]))
    idx.close()
    summary = {
        **SUMMARY,
        "categories": {
            "personal": {"files": 8, "bytes": 6076},
            "other": {"files": 4, "bytes": 20006},
        },
    }
    return result, groups, summary, cats


class CategoryTextTest(unittest.TestCase):
    def test_sections_and_hour_shift(self):
        result, groups, summary, _ = build_categories()
        text = format_text(result, summary, footer=False)
        self.assertIn(
            "Categories:  personal 8 files, 6.1 kB; other media 4 files, 20.0 kB\n", text
        )
        self.assertIn(
            "             1 group relies on a whole-hour time shift (time zone or DST copies)\n",
            text,
        )
        personal = text.index("--- Personal photos/videos: 2 folder pairs ---")
        other = text.index(
            "--- Other media (folders listed under [categories] other): 1 folder pair ---"
        )
        self.assertLess(personal, other)
        self.assertIn("/d/one/Trip  ↔  /d/two/Trip   (all photos/videos match, 1 file; time "
                      "shifted +1 h)", text[personal:other])  # fmt: skip
        self.assertIn("/d/one/Plex  ↔  /d/two/Plex", text[other:])
        self.assertNotIn("Plex", text[personal:other])
        only = format_text(result.only("other"), {**summary, "category": "other"}, footer=False)
        self.assertIn("Showing:     other media only (--category other)", only)
        self.assertNotIn("--- Personal", only)
        plain = format_text(result, SUMMARY, footer=False)
        self.assertNotIn("---", plain)
        self.assertNotIn("Categories:", plain)

    def test_run_shift_note(self):
        from dpf.grouping import Run
        from dpf.report import run_lines

        run = Run("a.jpg", "c.jpg", "a.jpg", "c.jpg", 3, 30, True, (), (-6, 1))
        self.assertEqual(
            run_lines((run,), 8), ["       a.jpg … c.jpg  3 files  (time shifted -6 h, +1 h)"]
        )

    def test_csv_and_json_columns(self):
        result, groups, summary, cats = build_categories()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "files.csv")
            write_csv(groups, path, cats)
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
            trip = [r for r in rows if r["name"] == "DSC00001.ARW"]
            self.assertEqual([(r["dir"], r["hour_shift"]) for r in trip],
                             [("/d/one/Trip", "0"), ("/d/two/Trip", "1")])  # fmt: skip
            self.assertEqual({r["category"] for r in rows if r["name"].endswith(".mkv")}, {"other"})
            self.assertEqual({r["category"] for r in rows if r["name"].endswith(".JPG")},
                             {"personal"})  # fmt: skip
            path = os.path.join(tmp, "report.json")
            write_json(result, summary, path)
            with open(path) as fh:
                data = json.load(fh)
        self.assertEqual(data["hour_shift_groups"], 1)
        pairs = {p["dir_a"]: p for p in data["pairs"]}
        self.assertEqual(pairs["/d/one/Trip"]["hour_shift"], [1])
        self.assertEqual(pairs["/d/one/Plex"]["category"], "other")


class CoverageExtrasTextTest(unittest.TestCase):
    def test_lines(self):
        from dpf.categories import Categories

        a = [rec("/a/Photos", "p.jpg", 10), rec("/a/Photos", "s.jpg", 12, mtime=5_000.0),
             rec("/a/Plex", "m.mkv", 20)]  # fmt: skip
        b = [rec("/b", "s.jpg", 12, mtime=5_000.0 + 3600)]
        idx = Index(":memory:")
        idx.replace_root("/a", a, dir_stats={"/a/Photos": (3, 0)})
        idx.replace_root("/b", b)
        cov = check_coverage(idx, ["/a"], ["/b"], pool="A", other="B", direction=NOT_BACKED_UP,
                             category=Categories(["Plex"]))  # fmt: skip
        idx.close()
        cov.warnings = ["pool B's index may be out of date: /b last scanned x"]
        text = format_coverage(cov)
        for line in (
            "Warning:        pool B's index may be out of date: /b last scanned x\n",
            "                personal: 1 file, 10 B missing of 2 compared (50%)\n",
            "                other media: 1 file, 20 B missing of 1 compared (100%)\n",
            "                1 file counts as copied only through a whole-hour time shift (time "
            "zone or DST copies)\n",
            "                3 other files (non-media) in 1 folder are not compared by this check "
            "— sidecars/edits/catalogs may exist only on this side\n",
            "--- Personal photos/videos: 1 folder ---\n",
            "--- Other media (folders listed under [categories] other): 1 folder ---\n",
        ):
            self.assertIn(line, text)
        self.assertLess(text.index("/a/Photos   ("), text.index("/a/Plex   ("))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.csv")
            write_missing_csv([cov], path, Categories(["Plex"]))
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual({r["name"]: r["category"] for r in rows},
                         {"p.jpg": "personal", "m.mkv": "other"})  # fmt: skip


if __name__ == "__main__":
    unittest.main()
