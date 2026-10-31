import contextlib
import csv
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from dpf.cli import main
from dpf.index import Index
from dpf.report import CSV_FIELDS

HAVE_XLSXWRITER = importlib.util.find_spec("xlsxwriter") is not None


class CliFixture(unittest.TestCase):
    """Two drives under `drives`: one/DCIM and two/Backup hold the same 10 photos."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.drives = os.path.join(self.tmp, "drives")
        self.db = os.path.join(self.tmp, "index.db")
        self.out = os.path.join(self.tmp, "report")
        config = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.tmp, "config")})
        config.start()
        self.addCleanup(config.stop)
        src = os.path.join(self.drives, "one", "DCIM")
        dst = os.path.join(self.drives, "two", "Backup")
        os.makedirs(src)
        os.makedirs(dst)
        for i in range(1, 11):
            for d in (src, dst):
                path = os.path.join(d, f"IMG_{i}.JPG")
                with open(path, "wb") as fh:
                    fh.write(b"x" * i)
                os.utime(path, (1_600_000_000 + i, 1_600_000_000 + i))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--db", self.db, "--allow-same-drive", *args])
        return code, stdout.getvalue(), stderr.getvalue()


class CliTest(CliFixture):
    def test_find_mode1_end_to_end(self):
        code, out, _ = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("all photos/videos match, 10 files", out)
        self.assertIn("matched on similar name+size+mtime ±2s", out)
        for name in ("report.txt", "report.json", "files.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(len(json.load(fh)["pairs"]), 1)

    def test_similar_names_and_exact_names_flag(self):
        copies = os.path.join(self.drives, "three", "Copies")
        os.makedirs(copies)
        for i in range(1, 11):
            path = os.path.join(copies, f"IMG_{i} (1).JPG")
            with open(path, "wb") as fh:
                fh.write(b"x" * i)
            os.utime(path, (1_600_000_000 + i, 1_600_000_000 + i))
        code, out, _ = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("one/DCIM  ↔  ", out)
        self.assertIn("three/Copies   (all photos/videos match, 10 files; similar names)", out)
        self.assertIn("two/Backup   (all photos/videos match, 10 files)\n", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(len(json.load(fh)["pairs"]), 3)
        code, out, _ = self.run_cli(
            "find", "-q", "--no-scan", "--exact-names", "-o", self.out, self.drives
        )
        self.assertEqual(code, 0)
        self.assertIn("matched on name+size+mtime ±2s", out)
        self.assertNotIn("Copies", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(len(json.load(fh)["pairs"]), 1)

    @unittest.skipUnless(HAVE_XLSXWRITER, "XlsxWriter is not installed")
    def test_xlsx_written(self):
        code, out, err = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        xlsx = os.path.join(self.out, "report.xlsx")
        self.assertTrue(os.path.getsize(xlsx) > 0)
        self.assertIn(f"XLSX: {xlsx}", out)
        self.assertEqual(err, "")
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(json.load(fh)["summary"]["xlsx_path"], xlsx)

    def test_no_xlsx_flag(self):
        xlsx = os.path.join(self.out, "report.xlsx")
        os.makedirs(self.out)
        open(xlsx, "wb").close()  # stale workbook from an earlier run
        code, out, err = self.run_cli("find", "-q", "--no-xlsx", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(xlsx))
        self.assertNotIn("XLSX:", out)
        self.assertEqual(err, "")

    def test_xlsx_skipped_without_xlsxwriter(self):
        with mock.patch.dict(sys.modules, {"xlsxwriter": None}):
            code, out, err = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("all photos/videos match", out)
        self.assertNotIn("XLSX:", out)
        self.assertEqual(err, "dpf: report.xlsx skipped (install python3-xlsxwriter)\n")
        self.assertFalse(os.path.exists(os.path.join(self.out, "report.xlsx")))
        for name in ("report.txt", "report.json", "files.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)

    def test_no_scan_requires_index(self):
        code, _, err = self.run_cli("find", "-q", "--no-scan", "-o", self.out, self.drives)
        self.assertEqual(code, 1)
        self.assertIn("nothing indexed", err)

    def test_no_scan_reuses_index(self):
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        code, out, _ = self.run_cli("find", "-q", "--no-scan", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("all photos/videos match", out)

    def test_refuses_report_inside_scanned_path(self):
        with self.assertRaises(SystemExit):
            self.run_cli("find", "-q", "-o", os.path.join(self.drives, "rep"), self.drives)

    def test_refuses_output_on_scanned_drive(self):
        home = os.path.realpath(os.path.expanduser("~"))
        drives = os.path.realpath(self.drives)

        def fake_device(path):
            path = os.path.realpath(path)
            if path.startswith(drives) or path.startswith(os.path.join(self.tmp, "same-drive")):
                return 2
            return 1 if path.startswith(home) else 3

        with mock.patch("dpf.cli._device", side_effect=fake_device):
            out = os.path.join(self.tmp, "same-drive")
            with self.assertRaisesRegex(SystemExit, "onto the drive"):
                main(["--db", self.db, "find", "-q", "-o", out, self.drives])
            with self.assertRaisesRegex(SystemExit, "the index"):
                main(["--db", os.path.join(self.tmp, "same-drive", "i.db"), "scan", self.drives])

    def test_tree_overview_and_near_flags(self):
        for d, names in (("one/T/a", "abcde"), ("one/T/b", "fghij"), ("two/U/a", "abcde"),
                         ("two/U/b", "fghi")):  # fmt: skip
            os.makedirs(os.path.join(self.drives, d))
            for n in names:
                path = os.path.join(self.drives, d, f"{n}.jpg")
                with open(path, "wb") as fh:
                    fh.write(b"y")
                os.utime(path, (1_600_000_000, 1_600_000_000))
        code, out, _ = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("Tree overview: 1 tree pair", out)
        self.assertIn("(near-identical trees: 90% / 100%, 2 folder pairs, 9 files)", out)
        self.assertIn("(in T1)", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(len(json.load(fh)["trees"]), 1)
        code, out, _ = self.run_cli(
            "find", "-q", "--no-scan", "--near", "1", "--tree-top", "0", "-o", self.out, self.drives
        )
        self.assertEqual(code, 0)
        self.assertNotIn("Tree overview", out)
        self.assertIn("B is fully contained in A)", out)
        for bad in ("0", "1.5", "90%"):
            with self.assertRaises(SystemExit):
                self.run_cli("find", "-q", "--near", bad, self.drives)

    def test_content_mode_not_yet_available(self):
        code, _, err = self.run_cli("find", "-q", "-m", "2", "-o", self.out, self.drives)
        self.assertEqual(code, 2)
        self.assertIn("not implemented", err)

    def test_missing_path(self):
        nope = os.path.join(self.tmp, "nope")
        code, _, err = self.run_cli("find", nope)
        self.assertEqual(code, 1)
        self.assertEqual(err, f"dpf: {nope} is not connected and has never been scanned\n")

    def test_file_path(self):
        path = os.path.join(self.drives, "one", "DCIM", "IMG_1.JPG")
        code, _, err = self.run_cli("find", path)
        self.assertEqual(code, 1)
        self.assertIn("not an existing directory", err)


class OfflineRootsTest(CliFixture):
    """Drives scanned earlier and unplugged since are compared from the index."""

    def setUp(self):
        super().setUp()
        self.one = os.path.realpath(os.path.join(self.drives, "one"))
        self.two = os.path.realpath(os.path.join(self.drives, "two"))
        self.assertEqual(self.run_cli("scan", "-q", self.one, self.two)[0], 0)

    def unplug(self, path):
        os.rename(path, path + ".unplugged")

    def report_json(self):
        with open(os.path.join(self.out, "report.json")) as fh:
            return json.load(fh)

    def test_find_uses_index_for_unplugged_drive(self):
        self.unplug(self.two)
        code, out, err = self.run_cli("find", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 0, err)
        self.assertIn("all photos/videos match, 10 files", out)
        self.assertRegex(
            err, rf"using index for {self.two} \(not connected; last scanned "
            r"\d{4}-\d\d-\d\d \d\d:\d\d\)\n",
        )  # fmt: skip
        self.assertIn(f"indexed {self.one}: 10 files", err)
        self.assertNotIn(f"indexed {self.two}", err)
        self.assertRegex(out, rf"Roots:\s+{self.one}\s+\(scanned now\)\n")
        self.assertRegex(
            out, rf"\s+{self.two}\s+\(from index, scanned [\d -:]+, not connected\)\n"
        )
        self.assertIn("Some drives are not connected", out)
        status = self.report_json()["summary"]["root_status"]
        self.assertEqual([s["root"] for s in status], [self.one, self.two])
        self.assertEqual([s["status"] for s in status], ["scanned now", "from index"])
        self.assertEqual([s["connected"] for s in status], [True, False])
        self.assertEqual([(s["files"], s["bytes"]) for s in status], [(10, 55), (10, 55)])
        self.assertTrue(all(isinstance(s["scanned_at"], float) for s in status))

    def test_quiet_and_all_connected(self):
        code, out, err = self.run_cli("find", "-q", "-o", self.out, self.one, self.two)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Some drives are not connected", out)
        self.assertNotIn("from index", out)
        self.unplug(self.two)
        code, out, err = self.run_cli("find", "-q", "-o", self.out, self.one, self.two)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("from index", out)

    def empty_out(self, path):
        """Leave `path` as an empty folder, like a drive that failed to mount."""
        moved = path + ".contents"
        os.rename(path, moved)
        os.makedirs(path)
        return moved

    def test_empty_mount_folder_keeps_index(self):
        self.empty_out(self.two)
        code, out, err = self.run_cli("find", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"dpf: {self.two} looks empty now but had 10 files when last scanned; keeping the "
            "index (not mounted, or unreadable?). Use --allow-empty to accept the empty scan.\n",
            err,
        )  # fmt: skip
        self.assertIn("all photos/videos match, 10 files", out)
        self.assertRegex(out, rf"\s+{self.two}\s+\(from index, scanned [\d -:]+, not connected\)")
        status = self.report_json()["summary"]["root_status"]
        self.assertEqual([s["status"] for s in status], ["scanned now", "from index"])
        self.assertEqual([s["files"] for s in status], [10, 10])
        # quiet runs still warn; `scan` keeps the index too
        code, _, err = self.run_cli("scan", "-q", self.two)
        self.assertEqual(code, 0)
        self.assertIn("looks empty now but had 10 files", err)
        code, out, err = self.run_cli("drives")
        self.assertRegex(out, rf"{self.two}\s+10 ")

    def test_allow_empty_accepts_empty_scan(self):
        self.empty_out(self.two)
        code, _, err = self.run_cli("scan", "-q", "--allow-empty", self.two)
        self.assertEqual((code, err), (0, ""))
        code, out, err = self.run_cli("find", "-q", "--no-scan", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 0, err)
        self.assertIn("No duplicates shared between folders.", out)
        code, out, err = self.run_cli("find", "-q", "--allow-empty", "-o", self.out, self.two)
        self.assertEqual((code, err), (0, ""))

    def test_no_scan_marks_connected_roots_from_index(self):
        code, out, _ = self.run_cli("find", "-q", "--no-scan", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 0)
        status = self.report_json()["summary"]["root_status"]
        self.assertEqual([s["status"] for s in status], ["from index"] * 2)
        self.assertEqual([s["connected"] for s in status], [True, True])
        self.assertNotIn("Some drives are not connected", out)

    def test_no_scan_reports_unindexed_root(self):
        three = os.path.realpath(os.path.join(self.drives, "three"))
        os.makedirs(three)
        code, _, err = self.run_cli(
            "find", "-q", "--no-scan", "-o", self.out, self.one, self.two, three
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, f"dpf: nothing indexed under {three}; run without --no-scan\n")

    def test_never_scanned_missing_path(self):
        three = os.path.join(self.drives, "three")
        code, _, err = self.run_cli("find", "-o", self.out, self.one, three)
        self.assertEqual(code, 1)
        self.assertIn(f"{three} is not connected and has never been scanned", err)
        # sibling sharing a prefix with an indexed root is not indexed
        code, _, err = self.run_cli("find", "-o", self.out, self.one + "-old")
        self.assertIn("has never been scanned", err)

    def test_scan_refuses_missing_path(self):
        self.unplug(self.two)
        code, _, err = self.run_cli("scan", "-q", self.one, self.two)
        self.assertEqual(code, 1)
        self.assertEqual(err, f"dpf: {self.two} is not connected; nothing to scan\n")

    def test_offline_subfolder(self):
        self.unplug(self.two)
        backup = os.path.join(self.two, "Backup")
        code, out, err = self.run_cli("find", "-o", self.out, self.one, backup + "/")
        self.assertEqual(code, 0, err)
        self.assertIn(f"using index for {backup} (not connected; last scanned", err)
        self.assertIn("all photos/videos match, 10 files", out)
        status = self.report_json()["summary"]["root_status"]
        self.assertEqual(status[1]["root"], backup)
        self.assertIsNotNone(status[1]["scanned_at"])
        self.assertEqual(status[1]["files"], 10)

    def test_offline_nested_in_other_root_is_dropped(self):
        self.unplug(self.two)
        code, out, err = self.run_cli(
            "find", "-q", "-o", self.out, self.two, os.path.join(self.two, "Backup"), self.one
        )
        self.assertEqual(code, 0, err)
        roots = [s["root"] for s in self.report_json()["summary"]["root_status"]]
        self.assertEqual(roots, [self.one, self.two])

    def test_output_guard_with_offline_roots(self):
        self.unplug(self.two)
        devices = []

        def fake_device(path):
            devices.append(path)
            if not os.path.exists(path) and not path.startswith(self.tmp):
                raise FileNotFoundError(path)
            return 1

        with mock.patch("dpf.cli._device", side_effect=fake_device):
            code, out, err = self.run_cli("find", "-q", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 0, err)
        self.assertNotIn(self.two, devices)
        with self.assertRaisesRegex(SystemExit, "inside scanned path"):
            main(["--db", self.db, "find", "-q", "-o", os.path.join(self.two, "r"), self.two])
        with self.assertRaisesRegex(SystemExit, "the index inside scanned path"):
            main(["--db", os.path.join(self.two, "i.db"), "find", "-q", self.two])
        # without --allow-same-drive the real device check runs on connected roots only
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--db", self.db, "find", "-q", "-o", self.out, self.two])
        self.assertEqual(code, 0)

    def test_drives_lists_roots(self):
        self.unplug(self.two)
        code, out, err = self.run_cli("drives")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^Drive / folder\s+Files\s+Media size\s+Used on drive\s+"
                                   r"Capacity\s+Last scanned\s+Connected$")  # fmt: skip
        self.assertRegex(lines[1], rf"^{self.one}\s+10\s+55 B\s+n/a\s+n/a\s+[\d -:]+\s+yes$")
        self.assertRegex(lines[2], rf"^{self.two}\s+10\s+55 B\s+n/a\s+n/a\s+[\d -:]+\s+no$")
        self.assertEqual(lines[4:], [
            "2 indexed, 1 connected; 20 files, 110 B of media",
            "n/a: a folder inside a drive, not its mount point; its drive's space is not shown",
        ])  # fmt: skip

    def test_drives_empty_index(self):
        db = os.path.join(self.tmp, "none", "index.db")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--db", db, "drives"]), 0)
        self.assertIn("nothing indexed yet", stdout.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", db, "drives", "--forget", self.one]), 1)
        self.assertFalse(os.path.exists(db))

    def test_drives_forget(self):
        self.unplug(self.two)
        code, out, err = self.run_cli("drives", "--forget", self.two + "/")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"forgetting {self.two}: 10 files, 55 B (last scanned", out)
        self.assertIn("the drives were not touched", out)
        out = self.run_cli("drives")[1]
        self.assertIn(self.one, out)
        self.assertNotIn(self.two, out)
        code, _, err = self.run_cli("find", "-q", "-o", self.out, self.one, self.two)
        self.assertEqual(code, 1)
        self.assertIn("has never been scanned", err)
        self.assertTrue(os.path.isdir(self.two + ".unplugged"))

    def test_drives_forget_refuses_unknown_and_prefix_paths(self):
        for bad in (os.path.join(self.one, "DCIM"), self.drives, self.one + "-old"):
            code, out, err = self.run_cli("drives", "--forget", self.one, bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(out, "")
            self.assertIn(f"{bad} is not an indexed drive/folder", err)
            self.assertIn(f"\n  {self.one}\n  {self.two}\n", err)
        self.assertIn(self.two, self.run_cli("drives")[1])

    def test_drives_forget_refuses_root_inside_another(self):
        backup = os.path.join(self.two, "Backup")
        self.assertEqual(self.run_cli("scan", "-q", backup)[0], 0)
        self.assertEqual(self.run_cli("scan", "-q", self.two)[0], 0)
        self.assertNotIn(backup, self.run_cli("drives")[1])  # superseded by the rescan
        self.assertEqual(self.run_cli("scan", "-q", backup)[0], 0)
        code, _, err = self.run_cli("drives", "--forget", backup)
        self.assertEqual(code, 1)
        self.assertIn(f"{backup} lies inside indexed {self.two}", err)



class PoolsFixture(CliFixture):
    """Pools A (1A, 2A) and B (1B) over drives mounted under `media`."""

    def setUp(self):
        super().setUp()
        self.media = os.path.realpath(os.path.join(self.tmp, "media"))
        names = ("Storage 1A 4TB", "Storage 2A 8TB", "Storage 1B 4TB (2)")
        self.a1, self.a2, self.b1 = (os.path.join(self.media, n) for n in names)
        for drive in (self.a1, self.a2, self.b1):
            shutil.copytree(os.path.join(self.drives, "one"), os.path.join(drive, "DCIM"))
        self.pools_file = os.path.join(self.tmp, "pools.toml")
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
        )

    def write_pools(self, *lines):
        with open(self.pools_file, "w") as fh:
            fh.write("[pools]\n" + "\n".join(lines) + "\n")

    def run_cli(self, *args):
        return super().run_cli("--pools-file", self.pools_file, *args)

    def unplug(self, path):
        os.rename(path, os.path.join(self.tmp, "unplugged-" + os.path.basename(path)))

    def report_json(self):
        with open(os.path.join(self.out, "report.json")) as fh:
            return json.load(fh)



class PoolsCliTest(PoolsFixture):
    def test_find_pool_with_member_offline(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A")[0], 0)
        self.unplug(self.a2)
        code, out, err = self.run_cli("find", "--pool", "a", "-o", self.out)
        self.assertEqual(code, 0, err)
        self.assertIn(f"using index for {self.a2} (not connected", err)
        self.assertIn(f"indexed {self.a1}: 10 files", err)
        self.assertNotIn(self.b1, out + err)
        self.assertIn("\nPool:        A\nRoots:", out)
        self.assertIn("(matching tree: 1 folder, 10 files)", out)
        summary = self.report_json()["summary"]
        self.assertEqual(summary["pool"], "A")
        self.assertEqual(summary["roots"], [self.a1, self.a2])
        self.assertEqual([s["connected"] for s in summary["root_status"]], [True, False])

    def test_find_pool_drops_nested_and_duplicate_members(self):
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *", "{self.a1}/DCIM", "{self.a2}/"]',
        )  # fmt: skip
        self.assertEqual(self.run_cli("scan", "-q", self.a2)[0], 0)
        self.assertEqual(self.run_cli("scan", "-q", os.path.join(self.a2, "DCIM"))[0], 0)
        self.unplug(self.a2)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.report_json()["summary"]["roots"], [self.a1, self.a2])

    def test_find_pool_literal_never_scanned(self):
        never = os.path.join(self.media, "Storage 1TB")
        self.write_pools(f'A = ["{never}", "{self.media}/Storage [0-9]A *"]')
        code, _, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 1)
        self.assertEqual(err, f"dpf: {never} is not connected and has never been scanned\n")

    def test_scan_pool_skips_offline_members(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a2)[0], 0)
        self.unplug(self.a2)
        code, out, err = self.run_cli("scan", "--pool", "A")
        self.assertEqual(code, 0, err)
        self.assertIn(f"skipping {self.a2} (pool A, not connected)", err)
        self.assertIn(f"indexed {self.a1}: 10 files", err)
        self.assertNotIn(self.b1, err)
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A"), (0, "", ""))

    def test_scan_pool_none_connected(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        self.unplug(self.b1)
        code, _, err = self.run_cli("scan", "-q", "--pool", "B")
        self.assertEqual(code, 1)
        self.assertEqual(err, "dpf: no drives in pool B are connected\n")

    def test_pool_patterns_match_per_segment(self):
        """A B-drive subfolder named like an A drive stays out of pool A."""
        b2 = os.path.join(self.media, "Storage 2B 8TB")
        nested = os.path.join(b2, "old", "Storage 1A x")
        shutil.copytree(os.path.join(self.drives, "one"), nested)
        self.assertEqual(self.run_cli("scan", "-q", nested)[0], 0)
        self.unplug(b2)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.report_json()["summary"]["roots"], [self.a1, self.a2])
        code, out, _ = self.run_cli("pools")
        self.assertNotIn("old/Storage 1A x", out.split("Pool B")[0])

    def test_root_in_two_pools_is_refused(self):
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'All = ["{self.media}/Storage *"]',
        )
        for args in (("find", "-o", self.out), ("scan",)):
            with self.subTest(args):
                code, out, err = self.run_cli(*args, "-q", "--pool", "A")
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(
                    err,
                    f"dpf: {self.a1} matches more than one pool (A, All); fix {self.pools_file} "
                    "so each drive is in one pool\n",
                )
        self.assertFalse(os.path.exists(os.path.join(self.out, "report.txt")))

    def test_pool_and_paths_are_exclusive(self):
        for args in (("find", "--pool", "A", self.a1), ("scan", "--pool", "A", self.a1),
                     ("find",), ("scan", "-q")):  # fmt: skip
            with self.subTest(args), self.assertRaises(SystemExit) as cm:
                self.run_cli(*args)
            self.assertEqual(cm.exception.code, 2)

    def test_pool_errors(self):
        code, _, err = self.run_cli("find", "--pool", "C")
        self.assertEqual((code, err), (1, "dpf: unknown pool C; defined pools: A, B\n"))
        self.write_pools(f'A = ["{self.media}/Nothing *"]')
        code, _, err = self.run_cli("scan", "--pool", "A")
        self.assertEqual(code, 1)
        self.assertIn("pool A matches no connected or indexed drive", err)
        self.assertIn(f"  {self.media}/Nothing *", err)
        os.remove(self.pools_file)
        code, _, err = self.run_cli("find", "--pool", "A")
        self.assertEqual(code, 1)
        self.assertIn(f"dpf: no pools file at {self.pools_file}", err)
        self.assertIn("[pools]\nA = [", err)
        self.assertFalse(os.path.exists(self.db))

    def test_default_pools_file(self):
        config = os.path.join(os.environ["XDG_CONFIG_HOME"], "dpf")
        os.makedirs(config)
        shutil.copy(self.pools_file, os.path.join(config, "pools.toml"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", self.db, "pools"]), 0)
        self.assertIn(f"Pools from {config}/pools.toml", stdout.getvalue())

    def test_pools_listing(self):
        never = os.path.join(self.media, "Storage 1TB")
        self.write_pools(
            f'A = ["{never}", "{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            f'Empty = ["{self.media}/Nothing *"]',
        )
        self.assertEqual(self.run_cli("scan", "-q", self.a2)[0], 0)
        self.unplug(self.a2)
        code, out, err = self.run_cli("pools")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Pools from {self.pools_file}")
        self.assertEqual(lines[2:5], [
            "Pool A",
            f"  patterns:  {never}",
            f"             {self.media}/Storage [0-9]A *",
        ])  # fmt: skip
        self.assertRegex(lines[6], r"^  Drive / folder\s+Connected\s+Files\s+Size\s+Last scanned$")
        self.assertRegex(lines[7], rf"^  {self.a1}\s+yes\s+-\s+-\s+never scanned$")
        self.assertRegex(lines[8], rf"^  {never}\s+no\s+-\s+-\s+never scanned$")
        self.assertRegex(lines[9], rf"^  {self.a2}\s+no\s+10\s+55 B\s+\d{{4}}-\d\d-\d\d \d\d:\d\d$")
        self.assertIn(f"Pool B\n  patterns:  {self.media}/Storage [0-9]B *\n", out)
        self.assertRegex(out, rf"\n  {re.escape(self.b1)}\s+yes\s+-\s+-\s+never scanned\n")
        self.assertTrue(out.endswith(
            f"Pool Empty\n  patterns:  {self.media}/Nothing *\n"
            "  (no connected or indexed drive matches)\n"
        ))  # fmt: skip

    def test_pools_without_index(self):
        code, out, _ = self.run_cli("pools")
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"\n  {self.a1}\s+yes\s+-\s+-\s+never scanned\n")
        self.assertFalse(os.path.exists(self.db))
        os.remove(self.pools_file)
        code, _, err = self.run_cli("pools")
        self.assertEqual(code, 1)
        self.assertIn("no pools file at", err)

    def test_drives_pool_column(self):
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            f'Old = ["{self.a2}"]',
        )
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.a2, self.b1, self.drives)[0], 0)
        code, out, err = self.run_cli("drives")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"\s+Connected\s+Pool$")
        rows = {line.split("  ")[0]: re.split(r"\s\s+", line)[-1] for line in lines[1:5]}
        self.assertEqual(rows, {
            os.path.realpath(self.drives): "—",
            self.a1: "A",
            self.a2: "A, Old",
            self.b1: "B",
        })  # fmt: skip
        os.remove(self.pools_file)
        code, out, err = self.run_cli("drives")
        self.assertEqual(code, 0)
        self.assertIn("dpf: warning: no pools file at", err)
        self.assertRegex(out.splitlines()[0], r"Connected$")


def write_photos(folder, names, size=1000, mtime=1_700_000_000):
    os.makedirs(folder, exist_ok=True)
    for i, name in enumerate(names):
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(b"y" * (size + i))
        os.utime(path, (mtime + i, mtime + i))


class CoverageCliTest(PoolsFixture):
    """Pool B (1B) backs up pool A (1A, 2A); each drive holds DCIM with IMG_1 … IMG_10."""

    def setUp(self):
        super().setUp()
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            "[backups]",
            'B = "A"',
        )
        write_photos(os.path.join(self.a2, "Trip", "day1"), [f"DSC{n:05d}.ARW" for n in (1, 2)])
        write_photos(os.path.join(self.a2, "Trip", "day2"), [f"DSC{n:05d}.ARW" for n in (3, 4)])
        write_photos(os.path.join(self.b1, "Only B"), ["PXL_1.jpg", "PXL_2.jpg"], 50)
        open(os.path.join(self.a1, "DCIM", "empty.jpg"), "wb").close()

    def read(self, name):
        with open(os.path.join(self.out, name), newline="") as fh:
            return fh.read()

    def test_drives_section_and_loose_matching(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        code, out, _ = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("===== Drives =====", out)
        self.assertIn("Only copy", out)
        code, out, _ = self.run_cli("find", "-q", "--pool", "A", "--ignore-mtime", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("===== Drives =====", out)
        self.assertNotIn("Only copy", out)  # different files could count as copies

    def test_pool_a_reports_not_backed_up(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Not backed up — no copy anywhere in pool B\n", out)
        self.assertIn(f"Pool B:         {self.b1}   (from index, scanned ", out)
        self.assertIn(
            "Not backed up:  4 files, 4.0 kB, in 2 folders (+ 1 empty file not compared)", out
        )
        self.assertIn(
            f"{self.a2}/Trip   (whole tree missing from B: 2 folders, 4 files, 4.0 kB)", out
        )
        self.assertNotIn("Only in the backup", out)
        self.assertIn(f"Missing files: {self.out}/missing.csv", out)
        rows = list(csv.DictReader(io.StringIO(self.read("missing.csv"))))
        self.assertEqual([r["name"] for r in rows], [f"DSC{n:05d}.ARW" for n in range(1, 5)])
        self.assertEqual({(r["pool"], r["missing_from"]) for r in rows}, {("A", "B")})
        data = self.report_json()
        self.assertEqual(list(data["coverage"]), ["A not in B"])
        self.assertEqual(data["coverage"]["A not in B"]["missing_files"], 4)
        self.assertEqual(data["summary"]["pool"], "A")
        if HAVE_XLSXWRITER:
            from tests.test_xlsx_report import Workbook

            wb = Workbook(os.path.join(self.out, "report.xlsx"))
            self.addCleanup(wb.close)
            self.assertEqual(wb.names[-1], "A not in B")
            self.assertEqual(wb.names[1:3], ["Drives", "Trees"])

    def test_pool_b_from_index_reports_only_in_backup(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A")[0], 0)
        self.unplug(self.a1)
        self.unplug(self.a2)
        code, out, err = self.run_cli("find", "-q", "--pool", "B", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Only in the backup — no copy anywhere in pool A\n", out)
        self.assertIn(f"Pool A:         {self.a1}   (from index, scanned ", out)
        self.assertIn("not connected)", out)
        self.assertIn("Pool A comes from the index; it was not scanned now.", out)
        self.assertIn("Only in backup: 2 files, 101 B, in 1 folder\n", out)
        self.assertIn(f"{self.b1}/Only B   (all 2 files missing from A)", out)

    def test_other_pool_not_scanned_by_this_run(self):
        """Coverage reads the other pool from the index only; it never scans it."""
        self.assertEqual(self.run_cli("scan", "-q", self.b1)[0], 0)
        write_photos(os.path.join(self.b1, "Later"), [f"DSC{n:05d}.ARW" for n in (1, 2)])
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Not backed up:  4 files", out)
        self.assertIn(f"{self.b1}   (from index, scanned ", out)

    def test_never_scanned_member_and_unindexed_pool(self):
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *", "{self.media}/Storage 9B"]',
            "[backups]",
            'B = "A"',
        )
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertEqual(
            err,
            "dpf: warning: pool B has nothing in the index (scan it first); coverage check "
            "skipped\n",
        )
        self.assertNotIn("Not backed up", out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "missing.csv")))
        self.assertEqual(self.run_cli("scan", "-q", self.b1)[0], 0)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 drive of pool B never scanned", out)
        self.assertIn(f"\n                  {self.media}/Storage 9B\n", out)
        code, _, err = self.run_cli(
            "find", "-q", self.a1, "--coverage-against", "Nothing", "-o", self.out
        )
        self.assertEqual((code, err), (1, "dpf: unknown pool Nothing; defined pools: A, B\n"))

    def test_no_coverage(self):
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "--no-coverage", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Not backed up", out)
        self.assertNotIn("Missing files:", out)
        self.assertNotIn("coverage", self.report_json())
        self.assertFalse(os.path.exists(os.path.join(self.out, "missing.csv")))

    def test_coverage_against_with_paths(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        trip = os.path.join(self.a2, "Trip")
        code, out, err = self.run_cli(
            "find", "-q", trip, os.path.join(self.a1, "DCIM"), "--coverage-against", "b",
            "-o", self.out,
        )  # fmt: skip
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Not in pool B — no copy anywhere in pool B\n", out)
        self.assertIn("Compared:       these folders compared with pool B;", out)
        self.assertIn(f"{trip}   (whole tree missing from B: 2 folders, 4 files, 4.0 kB)", out)
        self.assertEqual(list(self.report_json()["coverage"]), ["Not in B"])
        code, _, err = self.run_cli(
            "find", "-q", self.b1, "--coverage-against", "B", "-o", self.out
        )
        self.assertEqual(code, 1)
        self.assertIn(f"dpf: the PATHs and pool B overlap at {self.b1} / {self.b1}", err)

    def test_coverage_against_explicit_pool(self):
        code, out, err = self.run_cli(
            "find", "-q", "--pool", "B", "--coverage-against", "A", "-o", self.out
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, "dpf: pool A has nothing in the index (scan it first)\n")
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A")[0], 0)
        code, out, err = self.run_cli(
            "find", "-q", "--pool", "B", "--coverage-against", "A", "-o", self.out
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Only in the backup — no copy anywhere in pool A", out)
        code, _, err = self.run_cli(
            "find", "-q", "--pool", "B", "--coverage-against", "b", "-o", self.out
        )
        self.assertEqual(
            (code, err), (1, "dpf: --coverage-against B: that is the pool being searched\n")
        )

    def test_argument_errors(self):
        for args in (
            ("--pool", "A", "--pool", "a"),
            ("--pool", "A", "--no-coverage", "--coverage-against", "B"),
            ("--pool", "A", "--pool", "B", "--coverage-against", "B"),
            ("--pool", "A", self.a1),
        ):
            with self.subTest(args), self.assertRaises(SystemExit) as cm:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_cli("find", "-q", *args, "-o", self.out)
            self.assertEqual(cm.exception.code, 2)

    def test_two_pools_in_one_run(self):
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "--pool", "B", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("dpf duplicate report: pool A, pool B\n"))
        a, b, cov = out.index("===== Pool A ====="), out.index("===== Pool B ====="), out.index(
            "===== Backup coverage ====="
        )
        self.assertLess(a, b)
        self.assertLess(b, cov)
        pool_a, pool_b = out[a:b], out[b:cov]
        self.assertIn(f"{self.a1}/DCIM/DCIM  ↔  {self.a2}/DCIM/DCIM", pool_a)
        self.assertNotIn(self.b1, pool_a)  # never a duplicate pair across pools
        self.assertIn("No duplicates shared between folders.", pool_b)
        self.assertNotIn(self.a1, pool_b)
        self.assertIn("Not backed up — no copy anywhere in pool B", out[cov:])
        self.assertIn("Only in the backup — no copy anywhere in pool A", out[cov:])
        self.assertEqual(out.count("Per-file detail:"), 1)
        with open(os.path.join(self.out, "report.txt")) as fh:
            self.assertEqual(fh.read(), out)

        data = self.report_json()
        self.assertEqual(list(data["pools"]), ["A", "B"])
        self.assertEqual(data["summary"]["pools"], ["A", "B"])
        self.assertEqual(list(data["coverage"]), ["A not in B", "B not in A"])
        for pair in data["pools"]["A"]["pairs"]:
            self.assertNotIn(self.b1, pair["dir_a"] + pair["dir_b"])
        self.assertEqual(data["pools"]["B"]["pairs"], [])
        rows = list(csv.DictReader(io.StringIO(self.read("files.csv"))))
        self.assertEqual({r["pool"] for r in rows}, {"A"})
        self.assertTrue(all(r["path"].startswith((self.a1, self.a2)) for r in rows))
        rows = list(csv.DictReader(io.StringIO(self.read("missing.csv"))))
        self.assertEqual(
            sorted({(r["pool"], r["missing_from"]) for r in rows}), [("A", "B"), ("B", "A")]
        )
        if HAVE_XLSXWRITER:
            from tests.test_xlsx_report import Workbook

            wb = Workbook(os.path.join(self.out, "report.xlsx"))
            self.addCleanup(wb.close)
            self.assertEqual(wb.names, [
                "Summary", "Drives",
                "A Trees", "A Folder pairs", "A Sequences", "A Same folder", "A Files",
                "B Trees", "B Folder pairs", "B Sequences", "B Same folder", "B Files",
                "A not in B", "B not in A",
            ])  # fmt: skip
            self.assertEqual(wb.cells("A Folder pairs")["F2"], f"{self.a1}/DCIM/DCIM")
            self.assertNotIn("A2", wb.cells("B Folder pairs"))

    def test_single_pool_run_unchanged_without_backups(self):
        self.write_pools(f'A = ["{self.media}/Storage [0-9]A *"]', f'B = ["{self.b1}"]')
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("===== Pool", out)
        self.assertNotIn("Only copy", out)  # no coverage check, so no only-copy figures
        self.assertNotIn("Not backed up", out)
        self.assertEqual(self.read("files.csv").splitlines()[0], ",".join(CSV_FIELDS))

    def test_pools_listing_shows_links(self):
        code, out, _ = self.run_cli("pools")
        self.assertEqual(code, 0)
        self.assertIn("Pool A\n  backed up by: pool B\n", out)
        self.assertIn("Pool B\n  backup of: pool A\n", out)


class HourShiftCliTest(CliFixture):
    def test_hour_shifted_copies_match_unless_strict(self):
        copies = os.path.join(self.drives, "three", "Zone")
        os.makedirs(copies)
        for i in range(1, 4):  # sizes as in the fixture (i bytes)
            with open(os.path.join(copies, f"IMG_{i}.JPG"), "wb") as fh:
                fh.write(b"x" * i)
            os.utime(os.path.join(copies, f"IMG_{i}.JPG"), (0, 1_600_000_000 + i + 3600))
        code, out, _ = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("matched on similar name+size+mtime ±2s (or whole hours)", out)
        self.assertIn("3 groups rely on a whole-hour time shift", out)
        self.assertIn("three/Zone   (B is fully contained in A; time shifted +1 h)\n"
                      "       IMG_1.JPG … IMG_3.JPG  3 files  (time shifted +1 h)", out)
        self.assertIn("three/Zone  ↔  ", out)
        self.assertIn("two/Backup   (A is fully contained in B; time shifted -1 h)", out)
        with open(os.path.join(self.out, "files.csv"), newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual({r["hour_shift"] for r in rows if "Zone" in r["dir"]}, {"1"})
        code, out, _ = self.run_cli("find", "-q", "--strict-time", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("matched on similar name+size+mtime ±2s\n", out)
        self.assertNotIn("Zone", out)
        self.assertNotIn("time shift", out)


class CategoriesCliTest(PoolsFixture):
    read = CoverageCliTest.read

    def setUp(self):
        super().setUp()
        write_photos(os.path.join(self.a1, "Plex", "Films"), ["film.mkv"], 5000)
        write_photos(os.path.join(self.a2, "Plex", "Films"), ["film.mkv"], 5000)
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            "[backups]",
            'B = "A"',
            "[categories]",
            'other = ["*/Plex"]',
        )

    def test_find_sections_tallies_and_filter(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Categories:  personal 20 files, 110 B; other media 2 files, 10.0 kB", out)
        personal = out.index("--- Personal photos/videos: 1 folder pair ---")
        other = out.index("--- Other media (folders listed under [categories] other): 1 folder")
        self.assertLess(personal, other)
        self.assertIn(f"{self.a1}/Plex  ↔  {self.a2}/Plex   (matching tree", out[other:])
        self.assertIn("other media: 2 files, 10.0 kB missing of 2 compared (100%)", out)
        data = self.report_json()
        self.assertEqual(data["summary"]["categories"]["other"], {"files": 2, "bytes": 10000})
        self.assertEqual(data["summary"]["root_status"][0]["other_files"], 1)
        self.assertEqual(data["coverage"]["A not in B"]["by_category"]["other"]["missing"], 2)

        code, out, err = self.run_cli(
            "find", "-q", "--pool", "A", "--category", "personal", "-o", self.out
        )
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Plex", out)
        self.assertIn("Showing:     personal photos/videos only (--category personal)", out)
        rows = list(csv.DictReader(io.StringIO(self.read("files.csv"))))
        self.assertEqual({r["category"] for r in rows}, {"personal"})
        self.assertEqual(list(csv.DictReader(io.StringIO(self.read("missing.csv")))), [])

        code, out, _ = self.run_cli(
            "find", "-q", "--pool", "A", "--category", "other", "-o", self.out
        )
        self.assertEqual(code, 0)
        self.assertNotIn("DCIM", out)
        self.assertIn("Not backed up:  2 files, 10.0 kB, in 2 folders", out)

    def test_category_needs_patterns(self):
        self.write_pools(f'A = ["{self.media}/Storage [0-9]A *"]')
        code, _, err = self.run_cli("find", "-q", "--pool", "A", "--category", "other",
                                    "-o", self.out)  # fmt: skip
        self.assertEqual(code, 1)
        self.assertIn("--category other needs [categories] other patterns in", err)
        with open(self.pools_file, "a") as fh:
            fh.write('[categories]\npersonal = ["x"]\n')
        code, _, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 1)
        self.assertIn("[categories] has unknown key personal", err)

    def test_drives_category_columns(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.b1)[0], 0)
        code, out, err = self.run_cli("drives")
        self.assertEqual((code, err), (0, ""))
        header = out.splitlines()[0]
        self.assertRegex(header, r"Media size\s+Personal\s+Other media\s+Used on drive")
        row = next(line for line in out.splitlines() if line.startswith(self.a1))
        self.assertRegex(row, r"5\.1 kB\s+55 B\s+5\.0 kB\s")
        self.assertIn("Personal / Other media: media sizes by [categories]", out)
        # With the real mount paths (at most 34 characters) the table stays within 150.
        lines = out.splitlines()[:3]
        first = max(len(self.a1), len(self.b1))
        self.assertLessEqual(max(len(line) for line in lines) - first + 34, 150)


class CoverageWarningsCliTest(PoolsFixture):
    def setUp(self):
        super().setUp()
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            "[backups]",
            'B = "A"',
        )

    def test_stale_other_pool(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "B")[0], 0)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE roots SET scanned_at = scanned_at - 3 * 86400")
        conn.close()
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0)
        warning = f"pool B's index may be out of date: {self.b1} last scanned "
        self.assertIn(f"dpf: warning: {warning}", err)
        self.assertIn(f"Warning:        {warning}", out)
        self.assertIn(warning, self.report_json()["coverage"]["A not in B"]["warnings"][0])

    def test_same_drive_twice_in_pool(self):
        twin = os.path.join(self.media, "Storage 1B 4TB")
        shutil.copytree(self.b1, twin)
        self.assertEqual(self.run_cli("scan", "-q", self.b1, twin)[0], 0)
        code, out, err = self.run_cli("find", "-q", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertEqual(err.count("look like the same drive"), 1)
        self.assertIn(f"pool B: {twin} and {self.b1} look like the same drive", out)
        code, out, err = self.run_cli("find", "-q", "--pool", "B", "--no-coverage", "-o", self.out)
        self.assertIn("Warning:     pool B: ", out)
        self.assertEqual(err.count("look like the same drive"), 1)


GB = 10**9


class SpaceCliTest(PoolsCliTest):
    """Drive space: recorded by scans, by `dpf space`, or entered by hand.

    Every folder directly under `media` passes for a mounted drive of 1000 GB with
    250 GB used (os.statvfs and os.path.ismount are faked for them).
    """

    def setUp(self):
        super().setUp()
        statvfs, ismount = os.statvfs, os.path.ismount
        fake = os.statvfs_result((1000, 1000, GB, GB * 3 // 4, 0, 0, 0, 0, 0, 255))

        def is_drive(path):
            return os.path.dirname(os.path.abspath(path)) == self.media

        def fake_statvfs(path):
            if is_drive(path) and os.path.isdir(path):
                return fake
            return statvfs(path)

        def fake_ismount(path):
            return (is_drive(path) and os.path.isdir(path)) or ismount(path)

        for target, new in (("os.statvfs", fake_statvfs), ("os.path.ismount", fake_ismount)):
            patcher = mock.patch(target, side_effect=new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def forget_space(self, *roots):
        """Make roots look scanned before schema v5."""
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "UPDATE roots SET used_bytes = NULL, capacity_bytes = NULL, is_mount = NULL, "
            "space_source = NULL, space_at = NULL WHERE path = ?",
            [(r,) for r in roots],
        )
        conn.commit()
        conn.close()

    def stats(self, root):
        with Index(self.db) as idx:
            return idx.root_stats(root)

    def test_scan_records_space(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.drives), (0, "", ""))
        st = self.stats(self.a1)
        self.assertEqual(
            (st.used_bytes, st.capacity_bytes, st.is_mount), (250 * GB, 1000 * GB, True)
        )
        self.assertEqual((st.space_source, st.space_at), ("statvfs", st.scanned_at))
        st = self.stats(os.path.realpath(self.drives))
        self.assertEqual((st.is_mount, st.space_source), (False, "statvfs"))

    def test_drives_space_columns(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A")[0], 0)
        self.assertEqual(self.run_cli("scan", "-q", self.b1, self.drives)[0], 0)
        self.forget_space(self.b1)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE roots SET scanned_at = scanned_at - 86400 WHERE path = ?", (self.a2,))
        conn.commit()
        conn.close()
        code, out, err = self.run_cli("space", "--set", self.a2, "--used", "5.09TB",
                                      "--capacity", "8.00TB")  # fmt: skip
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out, f"recorded {self.a2} by hand: 5.1 TB used of 8.0 TB (the drive was not accessed)\n"
        )
        code, out, err = self.run_cli("drives")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^Drive / folder\s+Files\s+Media size\s+Used on drive\s+"
                                   r"Capacity\s+Space as of\s+Last scanned\s+Connected\s+Pool$")
        date = r"\d{4}-\d\d-\d\d \d\d:\d\d"
        rows = dict(zip([os.path.realpath(self.drives), self.a1, self.b1, self.a2], lines[1:5]))
        self.assertRegex(rows[self.a1], rf"\s+10\s+55 B\s+250.0 GB \s+1.0 TB \s+{date}\s+yes\s+A$")
        self.assertRegex(rows[self.a2], rf"\s+55 B\s+5.1 TB\*\s+8.0 TB\*\s+{date}\s+{date}\s+yes")
        self.assertRegex(rows[self.b1], rf"\s+55 B\s+— \s+— \s+{date}\s+yes\s+B$")
        folder = rows[os.path.realpath(self.drives)]
        self.assertRegex(folder, rf"\s+n/a \s+n/a \s+{date}\s+yes\s+—$")
        self.assertTrue(all(len(line) <= 150 for line in lines[:5]))
        self.assertEqual(lines[6:], [
            "4 indexed, 4 connected; 50 files, 275 B of media; 5.3 TB used of 9.0 TB on 2 drives "
            "(partial)",
            "* entered by hand (dpf space --set)",
            "n/a: a folder inside a drive, not its mount point; its drive's space is not shown",
            "1 drive has no space data (scanned before it was recorded); plug it in and run "
            "'dpf space --missing'.",
        ])  # fmt: skip

    def test_space_measures_without_scanning(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.a2, self.drives)[0], 0)
        self.forget_space(self.a1, self.a2)
        before = self.stats(self.a1)
        with mock.patch("dpf.scanner.walk", side_effect=AssertionError("no walk")), \
                mock.patch("os.scandir", side_effect=AssertionError("no listing")):  # fmt: skip
            code, out, err = self.run_cli("space")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[:2], [
            f"skipped {os.path.realpath(self.drives)}: not a mount point (a folder, or a drive "
            "not mounted)",
            "",
        ])  # fmt: skip
        self.assertIn("recorded the space of 2 drives:\n", out)
        self.assertRegex(out, rf"\n  {re.escape(self.a1)}\s+250.0 GB\s+1.0 TB\n")
        st = self.stats(self.a1)
        self.assertEqual((st.files, st.scanned_at), (before.files, before.scanned_at))
        self.assertEqual((st.used_bytes, st.space_source, st.is_mount), (250 * GB, "statvfs", True))

    def test_space_missing_and_unplugged(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.a2, self.b1)[0], 0)
        self.forget_space(self.a2, self.b1)
        self.unplug(self.b1)
        code, out, err = self.run_cli("space", "--missing")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[:4], [
            f"skipped {self.a1}: space already recorded",
            f"skipped {self.b1}: not connected",
            "",
            "recorded the space of 1 drive:",
        ])  # fmt: skip
        self.assertIsNotNone(self.stats(self.a2).used_bytes)
        self.assertIsNone(self.stats(self.b1).used_bytes)
        code, out, _ = self.run_cli("space", "--missing", "--pool", "A")
        self.assertEqual(code, 0)
        self.assertTrue(
            out.endswith("nothing recorded: no connected drive is missing space data\n")
        )

    def test_space_pool_and_paths_only_update_indexed_roots(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1)[0], 0)
        code, out, err = self.run_cli("space", "--pool", "a")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"skipped {self.a2}: not scanned yet; run `dpf scan` on it first\n", out)
        self.assertIn("recorded the space of 1 drive:", out)
        self.assertNotIn(self.b1, out)
        never = os.path.join(self.media, "Storage 9A gone")
        code, out, err = self.run_cli("space", self.a2, never, self.a1 + "/")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"skipped {self.a2}: not scanned yet; run `dpf scan` on it first\n", out)
        self.assertIn(f"skipped {never}: not connected, never scanned\n", out)
        self.assertIn("recorded the space of 1 drive:", out)
        with Index(self.db) as idx:
            self.assertEqual([r.root for r in idx.roots()], [self.a1])

    def test_space_leaves_hand_entered_drives_alone_unless_named(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.a2)[0], 0)
        self.assertEqual(self.run_cli("space", "--set", self.a2, "--used", "1TB",
                                      "--capacity", "8000000000000")[0], 0)  # fmt: skip
        with mock.patch("os.path.isdir", side_effect=os.path.isdir) as isdir:
            for args in (("space",), ("space", "--pool", "A"), ("space", "--missing")):
                code, out, _ = self.run_cli(*args)
                self.assertEqual(code, 0)
                self.assertNotIn(f"  {self.a2}", out)
        self.assertNotIn(self.a2, [c.args[0] for c in isdir.call_args_list])
        st = self.stats(self.a2)
        self.assertEqual(
            (st.used_bytes, st.capacity_bytes, st.space_source), (10**12, 8 * 10**12, "manual")
        )
        self.assertIsNone(st.is_mount)
        code, out, _ = self.run_cli("space", self.a2)
        self.assertIn("recorded the space of 1 drive:", out)
        self.assertEqual(self.stats(self.a2).space_source, "statvfs")

    def test_space_set_never_touches_the_drive(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1, self.a2)[0], 0)
        touched = []

        def spy(module, name):
            original = getattr(module, name)

            def wrapper(path, *args, **kwargs):
                touched.append(path)
                return original(path, *args, **kwargs)

            return mock.patch.object(module, name, side_effect=wrapper)

        os_calls = ("stat", "lstat", "statvfs", "scandir", "listdir", "access", "open", "readlink")
        path_calls = ("ismount", "isdir", "exists", "realpath")
        with contextlib.ExitStack() as stack:
            for module, names in ((os, os_calls), (os.path, path_calls)):
                for name in names:
                    stack.enter_context(spy(module, name))
            code, out, err = self.run_cli(
                "space", "--set", self.a2 + "/", "--used", "5.09 TB", "--capacity", "8.00TB"
            )
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue(touched)  # the spies see the index's own path checks
        self.assertEqual([p for p in touched if str(p).startswith(self.a2)], [])
        st = self.stats(self.a2)
        self.assertEqual((st.used_bytes, st.capacity_bytes), (5_090_000_000_000, 8 * 10**12))

    def test_space_set_errors(self):
        self.assertEqual(self.run_cli("scan", "-q", self.a1)[0], 0)
        code, out, err = self.run_cli(
            "space", "--set", self.a2, "--used", "1GB", "--capacity", "2GB"
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"dpf: {self.a2} is not an indexed drive/folder", err)
        self.assertIn(f"\n  {self.a1}\n", err)
        code, _, err = self.run_cli("space", "--set", self.a1, "--used", "3GB", "--capacity", "2GB")
        self.assertEqual(code, 1)
        self.assertIn("expected 0 <= used <= capacity", err)
        for args in (
            ("--set", self.a1, "--used", "1GB"),
            ("--set", self.a1, "--used", "1GB", "--capacity", "4.7T"),
            ("--set", self.a1, "--used", "1GB", "--capacity", "2GB", "--missing"),
            ("--set", self.a1, "--used", "1GB", "--capacity", "2GB", self.a1),
            ("--used", "1GB", "--capacity", "2GB"),
            ("--pool", "A", self.a1),
        ):  # fmt: skip
            with self.subTest(args), self.assertRaises(SystemExit) as cm:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_cli("space", *args)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.stats(self.a1).space_source, "statvfs")

    def test_space_without_index(self):
        code, out, err = self.run_cli("space")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("nothing indexed yet", err)
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()


class FoldersCliTest(CliFixture):
    def test_folders_csv_from_index(self):
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        out = os.path.join(self.tmp, "folders.csv")
        code, stdout, err = self.run_cli("folders", "-o", out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"4 folders from 1 drive written to {out}", stdout)
        with open(out, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(
            [(r["Folder"], r["Subfolder"], r["Media files"]) for r in rows],
            [("one", "", "10"), ("one", "DCIM", "10"), ("two", "", "10"), ("two", "Backup", "10")],
        )  # fmt: skip

    def test_folders_refuses_output_inside_a_drive(self):
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        with self.assertRaises(SystemExit):
            self.run_cli("folders", "-o", os.path.join(self.drives, "one", "f.csv"))
