import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from dpf.cli import main

HAVE_XLSXWRITER = importlib.util.find_spec("xlsxwriter") is not None


class CliFixture(unittest.TestCase):
    """Two drives under `drives`: one/DCIM and two/Backup hold the same 10 photos."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.drives = os.path.join(self.tmp, "drives")
        self.db = os.path.join(self.tmp, "index.db")
        self.out = os.path.join(self.tmp, "report")
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
        self.assertIn("name+size+mtime ±2s", out)
        for name in ("report.txt", "report.json", "files.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
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
        self.assertRegex(lines[0], r"^Drive / folder\s+Files\s+Size\s+Unreadable\s+"
                                   r"Last scanned\s+Connected$")  # fmt: skip
        self.assertRegex(lines[1], rf"^{self.one}\s+10\s+55 B\s+0\s+[\d -:]+\s+yes$")
        self.assertRegex(lines[2], rf"^{self.two}\s+10\s+55 B\s+0\s+[\d -:]+\s+no$")
        self.assertEqual(lines[-1], "2 indexed, 1 connected; 20 files, 110 B in total")

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


if __name__ == "__main__":
    unittest.main()
