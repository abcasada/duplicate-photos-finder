import contextlib
import errno
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from dpf import exif, safety
from dpf.cli import main
from dpf.index import Index
from dpf.safety import Mount, NoAtimeDenied, check_root, mount_of, open_read, read_mounts
from dpf.scanner import scan_roots
from tests.test_exif import HAVE_EXIFTOOL, TINY_JPEG


def mounts_line(point: str, fstype: str = "ext4", options: str = "rw,relatime") -> str:
    return f"/dev/sdz1 {point.replace(' ', chr(92) + '040')} {fstype} {options} 0 0\n"


class OpenRecorder:
    """os.open that refuses O_NOATIME with EPERM on existing files (as for another user's
    files) and records every file path opened or tried (not folders)."""

    def __init__(self, refuse_noatime: bool = True) -> None:
        self.refuse = refuse_noatime
        self.calls: list[tuple[str, int]] = []
        self.real = os.open

    def __call__(self, path, flags, *args, **kwargs):
        if isinstance(path, str) and not os.path.isdir(path) and "dir_fd" not in kwargs:
            self.calls.append((path, flags))
            if self.refuse and flags & os.O_NOATIME and os.path.exists(path):
                raise PermissionError(errno.EPERM, "Operation not permitted", path)
        return self.real(path, flags, *args, **kwargs)


class MountsTest(unittest.TestCase):
    def test_read_mounts_and_mount_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mounts")
            with open(path, "w") as fh:
                fh.write(mounts_line("/", options="rw,relatime"))
                fh.write(mounts_line("/run/media/u/Storage 4A 8TB", "fuseblk", "rw,relatime"))
                fh.write(mounts_line("/run/media/u/Travel", "ntfs3", "ro,relatime"))
            mounts = read_mounts(path)
        self.assertEqual(len(mounts), 3)
        m = mount_of("/run/media/u/Storage 4A 8TB/Photos", mounts)
        self.assertEqual((m.point, m.fstype, m.fuse, m.keeps_atime),
                         ("/run/media/u/Storage 4A 8TB", "fuseblk", True, True))  # fmt: skip
        self.assertFalse(mount_of("/run/media/u/Travel", mounts).keeps_atime)  # ro
        self.assertEqual(mount_of("/home/u", mounts).point, "/")
        self.assertEqual(read_mounts(os.path.join("/nonexistent", "mounts")), [])

    def test_noatime_or_ro_never_keeps_atime(self):
        for options, keeps in (("rw,noatime", False), ("ro", False), ("rw,relatime", True),
                               ("rw,strictatime", True)):  # fmt: skip
            m = Mount("/dev/x", "/m", "ext4", frozenset(options.split(",")))
            self.assertEqual(m.keeps_atime, keeps, options)


class CheckRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.file = os.path.join(self.tmp, "a.jpg")
        with open(self.file, "wb") as fh:
            fh.write(b"x")

    def mounts(self, fstype="ext4", options="rw,relatime"):
        return [Mount("/dev/sdz1", self.tmp, fstype, frozenset(options.split(",")))]

    def test_probe_refused_skips_the_drive(self):
        opener = OpenRecorder()
        with mock.patch.object(safety.os, "open", opener):
            mounts = [Mount("/dev/loop24p2", self.tmp, "ext4", frozenset({"rw", "relatime"}))]
            check = check_root(self.tmp, [self.file], mounts)
        self.assertFalse(check.ok)
        self.assertIn("can't be opened without updating their access time", check.message)
        self.assertIn("mounted via ext4 from /dev/loop24p2", check.message)
        self.assertIn("files owned by", check.message)
        self.assertIn("remount it read-only", check.message)
        self.assertEqual([p for p, _ in opener.calls], [self.file])  # the probe only

    def test_probe_granted_reads(self):
        opener = OpenRecorder(refuse_noatime=False)
        with mock.patch.object(safety.os, "open", opener):
            check = check_root(self.tmp, [self.file], self.mounts())
        self.assertTrue(check.ok)
        self.assertFalse(check.plain_ok)
        self.assertTrue(opener.calls[0][1] & os.O_NOATIME)

    def test_noatime_and_ro_mounts_need_no_probe(self):
        for options in ("rw,noatime", "ro,relatime"):
            opener = OpenRecorder()
            with mock.patch.object(safety.os, "open", opener):
                check = check_root(self.tmp, [self.file], self.mounts("fuseblk", options))
            self.assertTrue(check.ok, options)
            self.assertTrue(check.plain_ok)
            self.assertEqual(opener.calls, [])

    def test_fuse_mount_is_skipped_even_when_noatime_opens(self):
        opener = OpenRecorder(refuse_noatime=False)
        with mock.patch.object(safety.os, "open", opener):
            check = check_root(self.tmp, [self.file], self.mounts("fuseblk"))
        self.assertFalse(check.ok)
        self.assertIn("mounted via fuseblk", check.message)
        self.assertEqual(opener.calls, [])

    def test_gone_probe_files_are_passed_over(self):
        gone = os.path.join(self.tmp, "gone.jpg")
        opener = OpenRecorder()
        with mock.patch.object(safety.os, "open", opener):
            check = check_root(self.tmp, [gone, self.file], self.mounts())
        self.assertFalse(check.ok)
        self.assertEqual([p for p, _ in opener.calls], [gone, self.file])

    def test_open_read_never_retries_without_the_flag(self):
        opener = OpenRecorder()
        with mock.patch.object(safety.os, "open", opener):
            with self.assertRaises(NoAtimeDenied):
                open_read(self.file)
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(opener.calls[0][1] & os.O_NOATIME)

    def test_open_read_plain_only_where_atime_is_never_kept(self):
        opener = OpenRecorder()
        with mock.patch.object(safety.os, "open", opener):
            os.close(open_read(self.file, plain_ok=True))
        self.assertEqual(len(opener.calls), 2)
        self.assertFalse(opener.calls[1][1] & os.O_NOATIME)

    def test_eacces_is_not_an_atime_refusal(self):
        def denied(path, flags, *args):
            raise PermissionError(errno.EACCES, "Permission denied", path)

        with mock.patch.object(safety.os, "open", denied):
            with self.assertRaises(PermissionError) as cm:
                open_read(self.file)
        self.assertNotIsInstance(cm.exception, NoAtimeDenied)


@unittest.skipUnless(HAVE_EXIFTOOL, "exiftool not installed")
class ExifAtimeTest(unittest.TestCase):
    """dpf exif on a drive whose mount options and file owners are faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "Storage 4A 8TB")
        os.makedirs(os.path.join(self.root, "DCIM"))
        for name in ("a.jpg", "b.jpg"):
            with open(os.path.join(self.root, "DCIM", name), "wb") as fh:
                fh.write(TINY_JPEG)
        self.db = os.path.join(self.tmp, "index.db")
        with Index(self.db) as idx:
            scan_roots(idx, [self.root])
        self.mounts = os.path.join(self.tmp, "mounts")
        patcher = mock.patch.object(safety, "MOUNTS", self.mounts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mount(self, fstype: str, options: str) -> None:
        with open(self.mounts, "w") as fh:
            fh.write(mounts_line("/", options="rw,relatime"))
            fh.write(mounts_line(self.root, fstype, options))

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--db", self.db, "--allow-same-drive", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_refused_probe_skips_the_whole_drive(self):
        self.mount("fuseblk", "rw,relatime")
        opener = OpenRecorder()
        with mock.patch.object(safety.os, "open", opener), \
                mock.patch.object(exif, "copy_head", side_effect=AssertionError("read")):
            code, out, err = self.run_cli("exif", self.root)
        self.assertEqual(code, 1)
        self.assertIn(f"skipping {self.root}: files can't be read without updating their "
                      "access time (mounted via fuseblk", err)  # fmt: skip
        self.assertIn("remount it read-only", err)
        self.assertIn("read nothing: 1 drive skipped", out)
        self.assertEqual(opener.calls, [])  # FUSE: not even a probe
        self.mount("ntfs3", "rw,relatime")
        with mock.patch.object(safety.os, "open", opener), \
                mock.patch.object(exif, "copy_head", side_effect=AssertionError("read")):
            code, out, err = self.run_cli("exif", self.root)
        self.assertEqual(code, 1)
        self.assertIn("can't be opened without updating their access time (mounted via ntfs3",
                      err)  # fmt: skip
        self.assertEqual(len(opener.calls), 1)  # one probe, refused: nothing else opened
        with Index(self.db) as idx:
            self.assertEqual(idx.exif_status([self.root])["missing"], 2)

    def test_noatime_and_ro_mounts_are_read(self):
        for options in ("rw,noatime", "ro"):
            with Index(self.db) as idx:
                idx.conn.execute("DELETE FROM exif")
                idx.conn.commit()
            self.mount("ntfs3", options)
            opener = OpenRecorder()  # O_NOATIME refused: files of another user
            with mock.patch.object(safety.os, "open", opener):
                code, out, err = self.run_cli("exif", "-q", self.root)
            self.assertEqual((code, err), (0, ""), options)
            self.assertIn("2 photos", out)
            self.assertNotIn("skipped", out)
            with Index(self.db) as idx:
                self.assertEqual(idx.exif_status([self.root])["missing"], 0)

    def test_allow_atime_is_gone(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main(["--db", self.db, "exif", "--allow-atime", self.root])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
