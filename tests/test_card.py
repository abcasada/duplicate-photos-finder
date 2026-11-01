import csv
import os
import shutil
import tempfile
import unittest

from dpf.card import check_card, format_card
from dpf.index import Index
from dpf.models import FileRecord
from tests.test_cli import CliFixture, PoolsFixture

T = 1_600_000_000
A, B = "/d/Storage 1A", "/d/Storage 1B"


def make_card(root, files):
    """Write (relative path, size, mtime) files under root; returns root."""
    for rel, size, mtime in files:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        os.utime(path, (mtime, mtime))
    return root


def card_files(extra=()):
    """IMG_1-3 are on the drives, IMG_4 only with a date 1 h off, IMG_5 only as a copy mark;
    IMG_90-91 are on no drive."""
    return [
        *((f"DCIM/100/IMG_{i}.JPG", 100 + i, T + i) for i in (1, 2, 3)),
        ("DCIM/100/IMG_4.JPG", 104, T + 4),
        ("DCIM/100/IMG_5.JPG", 105, T + 5),
        ("DCIM/100/IMG_90.JPG", 190, T + 90),
        ("DCIM/100/IMG_91.JPG", 191, T + 91),
        *extra,
    ]


def on_drive(root, folder, numbers, shift=0, mark=""):
    return [FileRecord(f"{root}/{folder}", f"IMG_{i}{mark}.JPG", 100 + i, T + i + shift, "image")
            for i in numbers]  # fmt: skip


class CardCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.card = make_card(os.path.join(self.tmp, "card"), card_files())
        self.idx = Index(os.path.join(self.tmp, "index.db"))
        self.addCleanup(self.idx.close)
        self.idx.replace_root(A, on_drive(A, "Photos", [1, 2, 3, 4], shift=0)[:3]
                              + on_drive(A, "Photos", [4], shift=3600))  # fmt: skip
        self.idx.replace_root(B, on_drive(B, "Backup", [1, 5], mark=" (1)"))

    def check(self, **kw):
        return check_card(self.idx, self.card, **kw)

    def test_finds_copies_per_drive_and_what_is_missing(self):
        check = self.check()
        self.assertEqual((check.cov.files, check.found), (7, 5))
        self.assertEqual(sorted(f.name for f in check.cov.missing), ["IMG_90.JPG", "IMG_91.JPG"])
        self.assertEqual([(d.root, d.files) for d in check.drives], [(A, 4), (B, 2)])
        copies = {os.path.basename(p): v for p, v in check.copies.items()}
        self.assertEqual(copies["IMG_1.JPG"], [A, B])  # on both drives
        self.assertEqual(copies["IMG_4.JPG"], [A])  # through the whole-hour shift
        self.assertEqual(copies["IMG_5.JPG"], [B])  # through the copy mark
        self.assertNotIn(os.path.join(self.card, "DCIM/100/IMG_90.JPG"), check.copies)
        (folder,) = check.cov.folders
        self.assertEqual((folder.missing, folder.total), (2, 7))

    def test_strict_time_and_exact_names_drop_the_loose_matches(self):
        check = self.check(hour_shift=False, exact_names=True)
        self.assertEqual(check.found, 3)  # IMG_1-3 only

    def test_drive_roots_limits_the_comparison(self):
        check = self.check(drive_roots=[B])
        self.assertEqual((check.drives_compared, check.found), (1, 2))

    def test_index_is_only_read(self):
        before = (self.idx.roots(), self.idx.totals([A, B]))
        self.check()
        self.assertEqual((self.idx.roots(), self.idx.totals([A, B])), before)
        self.assertEqual(self.idx.totals([self.card]), (0, 0))

    def test_the_card_itself_in_the_index_is_ignored(self):
        self.idx.replace_root(self.card, [FileRecord(self.card + "/DCIM/100", "IMG_90.JPG",
                                                     190, T + 90, "image")])  # fmt: skip
        check = self.check()
        self.assertEqual((check.ignored, check.found), ([self.card], 5))

    def test_overlapping_or_no_drives_are_refused(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            check_card(self.idx, A + "/Photos")
        inside = os.path.join(self.tmp, "inside")
        self.idx.replace_root(inside + "/sub", on_drive(inside + "/sub", "x", [1]))
        with self.assertRaisesRegex(ValueError, "overlap"):
            check_card(self.idx, inside)
        with Index(os.path.join(self.tmp, "empty.db")) as empty:
            with self.assertRaisesRegex(ValueError, "no indexed drives"):
                check_card(empty, self.card)

    def test_a_symlink_to_a_scanned_card_is_still_that_card(self):
        self.idx.replace_root(self.card, [FileRecord(self.card + "/DCIM/100", "IMG_1.JPG",
                                                     101, T + 1, "image")])  # fmt: skip
        link = os.path.join(self.tmp, "link")
        os.symlink(self.card, link)
        check = check_card(self.idx, link)
        self.assertEqual((check.ignored, check.found), ([os.path.realpath(self.card)], 5))

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 folder")
    def test_unreadable_entries_are_never_called_all_found(self):
        bad = os.path.join(self.card, "bad")
        os.mkdir(bad)
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o755)
        check = self.check(drive_roots=[A, B])
        self.assertEqual(check.unreadable, 1)
        text = format_card(check, top=5, max_runs=5)
        self.assertIn("1 unreadable, not compared", text)
        self.assertNotIn("non-media", text)  # not counted twice
        self.assertIn("WARNING: 1 entry on the card could not be read", text)
        tidy = make_card(os.path.join(self.tmp, "tidy"), [("DCIM/IMG_1.JPG", 101, T + 1)])
        os.mkdir(os.path.join(tidy, "bad"))
        os.chmod(os.path.join(tidy, "bad"), 0)
        self.addCleanup(os.chmod, os.path.join(tidy, "bad"), 0o755)
        text = format_card(check_card(self.idx, tidy), top=5, max_runs=5)
        self.assertIn("Every photo and video that could be read", text)

    def test_all_found_and_empty_card_texts(self):
        text = format_card(self.check(drive_roots=[A, B]), top=5, max_runs=5)
        self.assertIn("Already on a drive: 5 files", text)
        self.assertIn("Not on any drive:   2 files", text)
        self.assertIn("IMG_90.JPG … IMG_91.JPG  2 files", text)
        self.assertIn(f"{self.card}/DCIM/100   (2 of 7 files missing from any indexed drive)",
                      text)  # fmt: skip
        tidy = make_card(os.path.join(self.tmp, "tidy"), [("DCIM/IMG_1.JPG", 101, T + 1)])
        text = format_card(check_card(self.idx, tidy), top=5, max_runs=5)
        self.assertIn("Every photo and video on the card has a matching copy", text)
        empty = os.path.join(self.tmp, "blank")
        os.mkdir(empty)
        text = format_card(check_card(self.idx, empty), top=5, max_runs=5)
        self.assertIn("No photos or videos found on the card.", text)


class CardCliTest(CliFixture):
    def setUp(self):
        super().setUp()
        self.card = make_card(
            os.path.join(self.tmp, "card"),
            [(f"DCIM/100/IMG_{i}.JPG", i, T + i) for i in (1, 2, 3)]
            + [("DCIM/100/IMG_77.JPG", 77, T + 77), ("DCIM/readme.txt", 5, T)],
        )  # fmt: skip
        for drive in ("one", "two"):
            self.assertEqual(self.run_cli("scan", "-q", os.path.join(self.drives, drive))[0], 0)

    def test_report_and_csv(self):
        out = os.path.join(self.tmp, "csvdir")
        code, text, err = self.run_cli("card", "-q", "-o", out, self.card)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Card check: {self.card}", text)
        self.assertIn("Already on a drive: 3 files", text)
        self.assertIn("Not on any drive:   1 file,", text)
        self.assertIn("2 indexed drives", text)
        self.assertIn(os.path.join(self.drives, "one") + " ", text)
        self.assertIn(f"{self.card}/DCIM/100   (1 of 4 files missing from any indexed drive)",
                      text)  # fmt: skip
        with open(os.path.join(out, "card.csv"), encoding="utf-8", newline="") as fh:
            rows = {os.path.basename(r["path"]): r for r in csv.DictReader(fh)}
        self.assertEqual(sorted(rows), ["IMG_1.JPG", "IMG_2.JPG", "IMG_3.JPG", "IMG_77.JPG"])
        self.assertEqual(rows["IMG_77.JPG"]["status"], "not found")
        self.assertEqual(rows["IMG_77.JPG"]["drives"], "")
        self.assertEqual(rows["IMG_2.JPG"]["status"], "found")
        self.assertEqual(rows["IMG_2.JPG"]["drives"].count(";"), 1)  # both drives
        self.assertFalse(os.path.exists(os.path.join(self.card, "card.csv")))

    def test_refuses_to_write_inside_the_card_and_bad_paths(self):
        with self.assertRaises(SystemExit):
            self.run_cli("card", "-q", "-o", os.path.join(self.card, "out"), self.card)
        code, _, err = self.run_cli("card", os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("is not a folder", err)
        self.assertFalse(os.path.exists(os.path.join(self.card, "out")))


class CardPoolCliTest(PoolsFixture):
    def test_pool_limits_the_drives(self):
        card = make_card(os.path.join(self.tmp, "card"), [("IMG_1.JPG", 1, T + 1)])
        self.assertEqual(self.run_cli("scan", "-q", "--pool", "A", "--pool", "B")[0], 0)
        code, text, _ = self.run_cli("card", "-q", "--pool", "B", card)
        self.assertEqual(code, 0)
        self.assertIn("1 indexed drive ", text)
        self.assertIn(self.b1, text)
        self.assertNotIn(self.a1, text)
        code, _, err = self.run_cli("card", "-q", "--pool", "C", card)
        self.assertEqual(code, 1)
