import csv
import json
import os
import tempfile
import unittest
from datetime import datetime

from dpf.grouping import group_duplicates
from dpf.index import Index
from dpf.models import DuplicateGroup, FileRecord
from dpf.report import format_text, human_bytes, write_csv, write_json

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


class TextReportTest(unittest.TestCase):
    def setUp(self):
        self.result, self.groups = build()

    def test_layout(self):
        text = format_text(self.result, {**SUMMARY, "csv_path": "/tmp/r.csv"})
        self.assertIn("Mode:        1; matched on name+size+mtime ±2s", text)
        self.assertIn("Roots:       /drives/one\n             /drives/two", text)
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
            "/drives/one/Edits  ↔  /drives/two/Renamed   (A is fully contained in B)", text
        )
        self.assertIn("E1.jpg … E3.jpg  3 files  → in B: Trip 001.jpg … Trip 003.jpg", text)
        self.assertIn("covers 100% of A (3/3), 75% of B (3/4)", text)
        self.assertIn("Same-folder duplicates: 1 group, 1.0 kB redundant, in 1 folder", text)
        self.assertIn("       1 group     1.0 kB  /drives/one/Phone", text)
        self.assertIn("CSV:  /tmp/r.csv", text)
        self.assertNotIn("XLSX:", text)
        self.assertNotIn("IMG_0120", text)  # never every file
        text = format_text(self.result, {**SUMMARY, "xlsx_path": "/tmp/r.xlsx"})
        self.assertIn("Per-file detail:\n  XLSX: /tmp/r.xlsx", text)

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
        self.assertEqual(int(rows[0]["size"]), 123)
        self.assertEqual(datetime.fromisoformat(rows[1]["mtime"]).timestamp(), mtime + 1)

    def test_csv_empty(self):
        path = os.path.join(self.tmp.name, "e.csv")
        self.assertEqual(write_csv([], path), 0)


if __name__ == "__main__":
    unittest.main()
