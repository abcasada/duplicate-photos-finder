import json
import os
import subprocess
import unittest
from unittest import mock

from dpf.index import Index
from tests.test_cli import HAVE_XLSXWRITER, CliFixture, PoolsFixture
from tests.test_exif import HAVE_EXIFTOOL, TINY_JPEG
from tests.test_match_shot import shot


def put_shots(db: str, entries: list[tuple[str, str, int]]) -> None:
    """Cache a shot identity (serial "AAA", shutter count) for each (dir, name, shutter)."""
    with Index(db) as idx:
        rows = []
        for d, name, shutter in entries:
            (f,) = [f for f in idx.list_dir(d) if f.name == name]
            rows.append(shot(f, shutter=shutter))
        idx.put_exif(rows)


class ShotFindCliTest(CliFixture):
    """one/DCIM and two/Backup hold IMG_1 … IMG_10 (mode 1 matches); three/Edited holds a
    re-saved IMG_1 under another name, size and date."""

    def setUp(self):
        super().setUp()
        edited = os.path.join(self.drives, "three", "Edited")
        os.makedirs(edited)
        with open(os.path.join(edited, "Holiday-001.JPG"), "wb") as fh:
            fh.write(b"y" * 4096)
        self.edited = edited

    def test_mode_1b_finds_resaved_copy_and_states_basis(self):
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        one, two = (os.path.join(self.drives, *p) for p in (("one", "DCIM"), ("two", "Backup")))
        put_shots(self.db, [(one, "IMG_1.JPG", 1), (two, "IMG_1.JPG", 1),
                            (self.edited, "Holiday-001.JPG", 1),
                            (one, "IMG_2.JPG", 2), (two, "IMG_2.JPG", 3)])  # fmt: skip
        code, out, err = self.run_cli("find", "-q", "-m", "1b", "--no-scan", "-o", self.out,
                                      self.drives)  # fmt: skip
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            "Mode:        1b (camera shot, else name/date); matched on same shot "
            "(camera serial + shutter count), else similar name+size+mtime", out,
        )  # fmt: skip
        self.assertIn("Shots:       5 of 21 photos/videos have a camera shot identity", out)
        self.assertIn("16 files have no EXIF cached yet (run `dpf exif`)", out)
        self.assertIn("1 group matched as the same shot; 1 of them hold files of different "
                      "sizes (re-saved / metadata differs)", out)  # fmt: skip
        self.assertIn("1 name/size/date group split", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            data = json.load(fh)
        self.assertEqual(data["summary"]["shots"]["shot_groups"], 1)
        self.assertEqual(data["groups"], 9)  # IMG_2 is two shots; IMG_1 has three copies
        code, out, _ = self.run_cli("find", "-q", "--no-scan", "-o", self.out, self.drives)
        self.assertNotIn("Shots:", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            self.assertEqual(json.load(fh)["groups"], 10)  # mode 1 is unchanged


class ShotCoverageCliTest(PoolsFixture):
    """Pool B (1B) backs up A (1A, 2A); 2A also holds a RAW that 1B has re-saved."""

    def setUp(self):
        super().setUp()
        self.write_pools(
            f'A = ["{self.media}/Storage [0-9]A *"]',
            f'B = ["{self.media}/Storage [0-9]B *"]',
            "[backups]",
            'B = "A"',
        )
        self.raw_a = os.path.join(self.a2, "Raw")
        self.raw_b = os.path.join(self.b1, "Raw")
        copies = ((self.raw_a, 1000, 1_600_000_000), (self.raw_b, 1008, 1_700_000_000))
        for d, size, mtime in copies:
            os.makedirs(d)
            path = os.path.join(d, "IMGQ0001.DNG")
            with open(path, "wb") as fh:
                fh.write(b"r" * size)
            os.utime(path, (mtime, mtime))

    def test_coverage_uses_shots_in_mode_1b(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A", "--pool", "B")[0], 0)
        put_shots(self.db, [(self.raw_a, "IMGQ0001.DNG", 7), (self.raw_b, "IMGQ0001.DNG", 7)])
        code, out, _ = self.run_cli("find", "-q", "--no-scan", "--pool", "A", "-o", self.out)
        self.assertEqual(code, 0)
        self.assertIn("Not backed up:  1 file, 1.0 kB", out)  # mode 1: sizes differ
        code, out, _ = self.run_cli("find", "-q", "-m", "1b", "--no-scan", "--pool", "A",
                                    "-o", self.out)  # fmt: skip
        self.assertEqual(code, 0)
        self.assertIn("Every compared media file has a copy in pool B.", out)
        self.assertIn("1 file counts as copied only as the same camera shot in pool B", out)
        self.assertIn("run `dpf exif`", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            cov = json.load(fh)["coverage"]["A not in B"]
        self.assertEqual((cov["missing_files"], cov["same_shot_files"]), (0, 1))
        if HAVE_XLSXWRITER:
            self.assertTrue(os.path.exists(os.path.join(self.out, "report.xlsx")))


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class ExifCliTest(CliFixture):
    def test_exif_reads_indexed_files(self):
        photo = os.path.join(self.drives, "one", "DCIM", "cam.jpg")
        with open(photo, "wb") as fh:
            fh.write(TINY_JPEG)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=SONY", photo], check=True)
        code, _, err = self.run_cli("exif", self.drives)
        self.assertEqual(code, 1)
        self.assertIn("run `dpf scan` on it first", err)
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        code, out, err = self.run_cli("exif", "-q", self.drives)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("read EXIF of 21 files", out)
        self.assertIn("0 with a camera shot identity, 21 without", out)
        code, out, _ = self.run_cli("exif", "-q", self.drives)
        self.assertIn("nothing to read", out)
        with Index(self.db) as idx:
            self.assertEqual(idx.exif_row(os.path.dirname(photo), "cam.jpg")["make"], "SONY")

    def test_exif_refuses_unconnected_path(self):
        code, _, err = self.run_cli("exif", os.path.join(self.tmp, "nowhere"))
        self.assertEqual(code, 1)
        self.assertIn("is not connected; nothing to read", err)

    def test_exif_without_exiftool(self):
        self.assertEqual(self.run_cli("scan", "-q", self.drives)[0], 0)
        with mock.patch("dpf.exif.shutil.which", return_value=None):
            code, _, err = self.run_cli("exif", self.drives)
        self.assertEqual(code, 1)
        self.assertIn("exiftool not found", err)


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class ExifPoolCliTest(PoolsFixture):
    def test_exif_pool_reads_connected_drives_only(self):
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A", "--pool", "B")[0], 0)
        self.unplug(self.a2)
        code, out, err = self.run_cli("exif", "--pool", "A")
        self.assertEqual(code, 0, err)
        self.assertIn(f"skipping {self.a2} (pool A, not connected)", err)
        self.assertIn(f"reading EXIF under {self.a1}", err)
        self.assertNotIn(self.b1, err)
        self.assertIn("read EXIF of 10 files", out)


if __name__ == "__main__":
    unittest.main()
