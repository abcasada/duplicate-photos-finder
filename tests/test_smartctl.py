import os
import shutil
import subprocess
import tempfile
import unittest

from dpf.smartctl import (
    BlockMap,
    Commands,
    SmartError,
    block_map,
    detect_flags,
    disk_mounts,
    kernel_issues,
    mount_of,
    read_drive,
    read_mounts,
    read_smart,
    resolve_disks,
)
from tests.smart_fake import FakeSystem, ata, bridge


def commands(fake, euid=1000, sudo=True):
    return Commands(sudo=sudo, run=fake.run, interactive=fake.interactive, euid=euid)


class FlagTest(unittest.TestCase):
    def test_ata_flags(self):
        data = ata("/dev/sda", "S1", pending=3, crc=40,
                   messages=[{"string": "Unknown USB bridge", "severity": "warning"},
                             {"string": "info only", "severity": "information"}])  # fmt: skip
        data["ata_smart_attributes"]["table"].append(
            {"id": 1, "name": "Raw_Read_Error_Rate", "when_failed": "now", "raw": {"value": 0}}
        )
        data["ata_smart_self_test_log"] = {"standard": {"table": [
            {"status": {"passed": False, "string": "Completed: read failure"}},
            {"status": {"passed": False, "string": "Aborted by host"}},
        ]}}  # fmt: skip
        data["ata_smart_error_log"] = {"summary": {"count": 6}}
        data["smart_status"]["passed"] = False
        lines = ["[sda] I/O error", "sda: using unchecked data buffer"]
        self.assertEqual(detect_flags(data, lines), [
            "smartctl warning: Unknown USB bridge",
            "1 kernel log warning/error line(s) mention this drive",
            "SMART overall health assessment FAILED",
            "Current_Pending_Sector raw value is 3",
            "attribute Raw_Read_Error_Rate failed (now)",
            "1 failed self-test(s) in log",
            "ATA error log has 6 entries",
        ])  # fmt: skip

    def test_clean_drive_has_no_flags(self):
        self.assertEqual(detect_flags(ata("/dev/sda", "S1", crc=48), []), [])
        self.assertEqual(detect_flags(ata("/dev/sda", "S1"), None), [])

    def test_nvme_and_scsi_flags(self):
        data = {"nvme_smart_health_information_log": {
            "critical_warning": 1, "media_errors": 2, "percentage_used": 95,
            "available_spare": 5, "available_spare_threshold": 10,
        }, "scsi_grown_defect_list": 4,
            "scsi_error_counter_log": {"read": {"total_uncorrected_errors": 7}}}  # fmt: skip
        self.assertEqual(detect_flags(data, []), [
            "NVMe critical_warning is 1", "NVMe media_errors is 2",
            "NVMe wear is at 95% of rated life", "NVMe available spare is below threshold",
            "4 grown defects", "7 uncorrected read errors",
        ])  # fmt: skip


class SatRetryTest(unittest.TestCase):
    def test_usb_bridge_is_retried_with_sat(self):
        fake = FakeSystem()
        fake.smart[("-a", "/dev/sdb")] = bridge("/dev/sdb")
        fake.smart[("-a", "-d", "sat", "/dev/sdb")] = ata("/dev/sdb", "WD-REAL")
        data, _ = read_drive(commands(fake), "smartctl", "/dev/sdb")
        self.assertEqual(data["serial_number"], "WD-REAL")
        self.assertEqual(len(fake.ran("-d", "sat")), 1)

    def test_ata_needs_no_retry(self):
        fake = FakeSystem()
        fake.smart[("-a", "/dev/sda")] = ata("/dev/sda", "S1")
        data, _ = read_drive(commands(fake), "smartctl", "/dev/sda")
        self.assertEqual(data["serial_number"], "S1")
        self.assertEqual(fake.ran("-d", "sat"), [])

    def test_scsi_kept_when_sat_fails(self):
        fake = FakeSystem()
        fake.smart[("-a", "/dev/sdb")] = bridge("/dev/sdb", "SCSI-ONLY")
        data, _ = read_drive(commands(fake), "smartctl", "/dev/sdb")
        self.assertEqual(data["serial_number"], "SCSI-ONLY")

    def test_sat_retry_only_on_no_data(self):
        fake = FakeSystem()
        fake.smart[("-a", "-d", "sat", "/dev/sdb")] = ata("/dev/sdb", "WD-SAT")
        data, _ = read_drive(commands(fake), "smartctl", "/dev/sdb")
        self.assertEqual(data["serial_number"], "WD-SAT")


class SudoTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSystem()
        self.fake.smart[("-a", "/dev/sda")] = ata("/dev/sda", "S1")
        self.fake.smart[("-a", "/dev/sdb")] = bridge("/dev/sdb")
        self.fake.smart[("-a", "-d", "sat", "/dev/sdb")] = ata("/dev/sdb", "S2", pending=2)

    def read(self, cmds):
        return read_smart(cmds, "smartctl", mounts=[], sys_root="/nonexistent")

    def test_password_asked_once(self):
        self.fake.password = True
        result = self.read(commands(self.fake))
        self.assertEqual([r["summary"]["serial_number"] for r in result.records], ["S1", "S2"])
        self.assertEqual(self.fake.calls.count(["sudo", "-v"]), 1)
        smartctl = [c for c in self.fake.calls if "smartctl" in c]
        self.assertTrue(all(c[:2] == ["sudo", "-n"] for c in smartctl), smartctl)
        # plain dmesg is tried first, then sudo -n without a prompt
        dmesg = [c for c in self.fake.calls if "dmesg" in c]
        self.assertEqual([c[0] for c in dmesg], ["dmesg", "sudo"])
        self.assertTrue(result.kernel_log)
        self.assertEqual(result.records[0]["kernel_log_issues"], [self.fake.log[0]])

    def test_cached_sudo_needs_no_prompt(self):
        result = self.read(commands(self.fake))
        self.assertEqual(len(result.records), 2)
        self.assertNotIn(["sudo", "-v"], self.fake.calls)

    def test_failed_password_is_an_error(self):
        self.fake.password = True
        self.fake.password_ok = False
        with self.assertRaises(SmartError):
            self.read(commands(self.fake))

    def test_root_runs_without_sudo(self):
        self.fake.password = True
        self.fake.as_root = True
        result = self.read(commands(self.fake, euid=0))
        self.assertEqual(len(result.records), 2)
        self.assertFalse(any(c[0] == "sudo" for c in self.fake.calls))

    def test_no_sudo_reports_permission_denied(self):
        result = self.read(commands(self.fake, sudo=False))
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.failed), 2)
        self.assertIn("Permission denied", result.failed[0][1])
        self.assertFalse(any(c[0] == "sudo" for c in self.fake.calls))

    def test_plain_dmesg_needs_no_sudo(self):
        self.fake.dmesg_plain = True
        self.read(commands(self.fake))
        self.assertEqual([c[0] for c in self.fake.calls if "dmesg" in c], ["dmesg"])

    def test_unreadable_kernel_log(self):
        self.fake.password = True
        cmds = commands(self.fake)
        real = self.fake.run

        def run(argv):  # sudo allows smartctl only
            if list(argv[:3]) == ["sudo", "-n", "dmesg"]:
                return subprocess.CompletedProcess(argv, 1, "", "sudo: not allowed\n")
            return real(argv)

        cmds._run = run
        result = self.read(cmds)
        self.assertFalse(result.kernel_log)
        self.assertIsNone(result.records[0]["kernel_log_issues"])
        self.assertEqual(self.fake.calls.count(["sudo", "-v"]), 1)  # no second prompt

    def test_same_serial_twice_is_not_stored(self):
        self.fake.smart[("-a", "-d", "sat", "/dev/sdb")] = ata("/dev/sdb", "S1")
        result = self.read(commands(self.fake))
        self.assertEqual(len(result.records), 1)
        self.assertIn("same serial number S1 as /dev/sda", result.failed[0][1])


class RecordTest(unittest.TestCase):
    def test_record_fields(self):
        fake = FakeSystem()
        fake.smart[("-a", "/dev/sda")] = ata("/dev/sda", "S1 ", pending=1)
        fake.devices = ["/dev/sda"]
        mounts = [("/dev/sda1", "/mnt/fake/Storage 1A 4TB")]
        (rec,) = read_smart(commands(fake), "smartctl", mounts=mounts, sys_root="/x").records
        self.assertEqual(rec["summary"]["serial_number"], "S1")
        self.assertEqual(rec["summary"]["type"], "HDD")
        self.assertEqual(rec["summary"]["ata_attributes"]["pending_sectors"], 1)
        self.assertEqual(rec["flags"], ["1 kernel log warning/error line(s) mention this drive",
                                        "Current_Pending_Sector raw value is 1"])
        self.assertEqual(rec["labels"], ["Storage 1A 4TB"])
        self.assertEqual(rec["mounts"], ["/mnt/fake/Storage 1A 4TB"])
        self.assertEqual(rec["device_path"], "/dev/sda")
        self.assertNotIn("json_format_version", rec["smartctl"])
        self.assertNotIn("argv", rec["smartctl"]["smartctl"])
        self.assertRegex(rec["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


class MountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fake = FakeSystem()
        self.bmap = block_map(commands(self.fake))
        self.mounts = [
            ("/dev/nvme0n1p2", "/"),
            ("tmpfs", "/mnt"),
            ("/dev/sda1", "/mnt/fake/Storage 1A 4TB"),
            ("/dev/loop24p2", "/mnt/fake/Storage 4A 8TB"),
        ]

    def test_read_mounts_unescapes(self):
        path = os.path.join(self.tmp, "mounts")
        with open(path, "w") as fh:
            fh.write("/dev/loop24p2 /mnt/fake/Storage\\0404A\\0408TB ntfs3 ro 0 0\n"
                     "tmpfs /mnt tmpfs rw 0 0\n")  # fmt: skip
        self.assertEqual(read_mounts(path), [
            ("/dev/loop24p2", "/mnt/fake/Storage 4A 8TB"), ("tmpfs", "/mnt"),
        ])  # fmt: skip
        self.assertEqual(read_mounts(os.path.join(self.tmp, "none")), [])

    def test_mount_of_takes_the_innermost(self):
        self.assertEqual(mount_of("/mnt/fake/Storage 1A 4TB/DCIM/x", self.mounts)[0],
                         "/dev/sda1")  # fmt: skip
        self.assertEqual(mount_of("/mnt/fake/Storage 1A 4TB", self.mounts)[0], "/dev/sda1")
        self.assertEqual(mount_of("/mnt/fake/Storage 1A 4TBx", self.mounts)[0], "tmpfs")
        self.assertEqual(mount_of("/home/u", self.mounts)[0], "/dev/nvme0n1p2")

    def test_loop_partition_resolves_to_backing_disk(self):
        paths = ["/mnt/fake/Storage 4A 8TB/Photography", "/mnt/fake/Storage 1A 4TB"]
        disks = resolve_disks(paths, self.mounts, self.bmap)
        self.assertEqual(list(disks.values()), ["/dev/sdb", "/dev/sda"])
        self.assertEqual(disk_mounts(self.mounts, self.bmap), {
            "sda": ["/mnt/fake/Storage 1A 4TB"], "sdb": ["/mnt/fake/Storage 4A 8TB"],
        })  # fmt: skip
        self.assertIn(["losetup", "-J", "-l", "-O", "NAME,BACK-FILE"], self.fake.calls)

    def test_unresolvable_paths(self):
        with self.assertRaisesRegex(SmartError, "not on a connected drive"):
            resolve_disks(["/mnt/fake/Storage 2B 8TB"], self.mounts, self.bmap)
        with self.assertRaisesRegex(SmartError, "cannot trace"):
            resolve_disks(["/"], self.mounts, self.bmap)  # nvme0n1p2 is not in lsblk here

    def test_file_backed_loop_and_partition_chain(self):
        bmap = BlockMap(
            devices={"/dev/loop1": ("loop", None), "/dev/loop1p1": ("part", "loop1"),
                     "/dev/loop2": ("loop", None), "/dev/sdc": ("disk", None),
                     "/dev/sdc2": ("part", "sdc")},
            loops={"/dev/loop1": "/home/u/disk.img", "/dev/loop2": "/dev/sdc2"},
        )  # fmt: skip
        self.assertIsNone(bmap.disk_of("/dev/loop1p1"))
        self.assertEqual(bmap.disk_of("/dev/loop2"), "/dev/sdc")
        self.assertIsNone(bmap.disk_of("/dev/nope"))

    def test_read_smart_only_wanted_disks(self):
        self.fake.smart[("-a", "/dev/sdb")] = ata("/dev/sdb", "WD-CA13ZBDK")
        result = read_smart(commands(self.fake), "smartctl", disks=["/dev/sdb"],
                            mounts=self.mounts, bmap=self.bmap, sys_root=self.tmp)  # fmt: skip
        (rec,) = result.records
        self.assertEqual(rec["summary"]["serial_number"], "WD-CA13ZBDK")
        self.assertEqual(rec["mounts"], ["/mnt/fake/Storage 4A 8TB"])
        self.assertEqual(rec["labels"], [])
        self.assertEqual(self.fake.ran("-a", "/dev/sda"), [])

    def test_wanted_disk_missing_from_scan_is_read_directly(self):
        self.fake.devices = ["/dev/sda"]
        self.fake.smart[("-a", "/dev/sdb")] = ata("/dev/sdb", "S9")
        result = read_smart(commands(self.fake), "smartctl", disks=["/dev/sdb"],
                            mounts=self.mounts, bmap=self.bmap, sys_root=self.tmp)  # fmt: skip
        self.assertEqual([r["device_path"] for r in result.records], ["/dev/sdb"])


class KernelLogTest(unittest.TestCase):
    def test_lines_of_the_drive_and_its_ports(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        target = os.path.join(tmp, "devices", "pci0", "usb2", "2-1", "host6", "block", "sdb")
        os.makedirs(target)
        os.makedirs(os.path.join(tmp, "block"))
        os.symlink(target, os.path.join(tmp, "block", "sdb"))
        lines = ["usb 2-1: reset", "usb 2-10: reset", "[sdb1] error", "[sdba] x",
                 "sdb: using unchecked data buffer"]  # fmt: skip
        self.assertEqual(kernel_issues("/dev/sdb", lines, sys_root=tmp),
                         ["usb 2-1: reset", "[sdb1] error", "sdb: using unchecked data buffer"])
        self.assertIsNone(kernel_issues("/dev/sdb", None, sys_root=tmp))
        self.assertEqual(kernel_issues("/dev/sdb", lines * 60, limit=5, sys_root=tmp),
                         (["usb 2-1: reset", "[sdb1] error", "sdb: using unchecked data buffer"]
                          * 60)[-5:])  # fmt: skip


if __name__ == "__main__":
    unittest.main()
