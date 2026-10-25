import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from dpf.cli import main


class CliTest(unittest.TestCase):
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

    def test_find_mode1_end_to_end(self):
        code, out, _ = self.run_cli("find", "-q", "-o", self.out, self.drives)
        self.assertEqual(code, 0)
        self.assertIn("all photos/videos match, 10 files", out)
        self.assertIn("name+size+mtime ±2s", out)
        for name in ("report.txt", "report.json", "files.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(len(json.load(fh)["pairs"]), 1)

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
        code, _, err = self.run_cli("find", os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("not an existing directory", err)


if __name__ == "__main__":
    unittest.main()
