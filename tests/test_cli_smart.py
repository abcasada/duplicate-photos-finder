import json
import os
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

from dpf.index import Index
from tests.smart_fake import FakeSystem, ata
from tests.test_cli import HAVE_XLSXWRITER, CliFixture
from tests.test_xlsx_report import Workbook


def report_entry(serial, label, when, pending=0, passed=True, flags=()):
    return {
        "collected_at": when, "host": "kub-desk", "device_path": "/dev/sda",
        "flags": list(flags), "kernel_log_issues": [],
        "partitions": [{"name": "sda", "children": [{"name": "sda1", "label": label}]}],
        "summary": {"model": "WDC WD80EFZZ", "serial_number": serial, "type": "HDD",
                    "smart_passed": passed, "power_on_hours": 26528, "temperature_c": 34,
                    "ata_attributes": {"pending_sectors": pending, "udma_crc_errors": 0}},
        "smartctl": {"device": {"name": "/dev/sda"}},
    }  # fmt: skip


class SmartCliTest(CliFixture):
    def write_report(self, entries, name="smart_report.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump({"report_format": 1, "drives": entries}, fh)
        return path

    def readings(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT serial, source FROM smart ORDER BY serial, read_at").fetchall()
        finally:
            conn.close()

    def test_import_twice_adds_nothing_the_second_time(self):
        path = self.write_report([
            report_entry("S1", "one", "2026-10-25T19:43:34+00:00"),
            report_entry("S9", "Backup 2TB", "2026-10-25T19:59:48+00:00", passed=False,
                         flags=["SMART overall health assessment FAILED"]),
        ])  # fmt: skip
        code, out, _ = self.run_cli("smart", "--import", path)
        self.assertEqual(code, 0)
        self.assertIn("imported 2 readings of 2 drives", out)
        self.assertRegex(out, r"Backup 2TB\s+WDC WD80EFZZ\s+S9\s+FAILED\s+1 flag")
        code, out, _ = self.run_cli("smart", "--import", path)
        self.assertEqual(code, 0)
        self.assertIn("imported 0 readings", out)
        self.assertEqual(self.readings(), [("S1", "import"), ("S9", "import")])

    def test_import_errors(self):
        with self.assertRaises(SystemExit):
            self.run_cli("smart", "--import", "x.json", self.drives)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("[]")
        code, _, err = self.run_cli("smart", "--import", bad)
        self.assertEqual(code, 1)
        self.assertIn("cannot read SMART report", err)

    def test_drives_and_find_show_health_from_the_index(self):
        self.run_cli("scan", "-q", os.path.join(self.drives, "one"), os.path.join(self.drives, "two"))
        path = self.write_report([
            report_entry("S1", "one", "2026-08-01T12:00:00+00:00", pending=1),
            report_entry("S9", "Backup 2TB", "2026-10-25T12:00:00+00:00", passed=False),
        ])  # fmt: skip
        self.run_cli("smart", "--import", path)
        with Index(self.db) as idx:  # a newer reading of S1, as `dpf smart` would store it
            idx.put_smart([report_entry("S1", "one", "2026-10-30T12:00:00+00:00", pending=3,
                                        flags=["Current_Pending_Sector raw value is 3"])],
                          "smartctl")  # fmt: skip
        code, out, _ = self.run_cli("drives")
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        self.assertIn("Health", header)
        self.assertTrue(all(len(line) <= 150 for line in out.splitlines()))
        one = next(line for line in out.splitlines() if line.startswith(os.path.join(self.drives, "one")))
        two = next(line for line in out.splitlines() if line.startswith(os.path.join(self.drives, "two")))
        self.assertIn(" warning", one)
        self.assertRegex(two, r"\s—$")
        self.assertIn("1 other drive with SMART readings, not in the index: Backup 2TB FAILED", out)

        with mock.patch("time.time", return_value=1_793_448_000.0):  # 2026-10-31
            code, out, _ = self.run_cli("find", "-q", "--no-scan", "-o", self.out,
                                        os.path.join(self.drives, "one"),
                                        os.path.join(self.drives, "two"))  # fmt: skip
        self.assertEqual(code, 0)
        self.assertIn("Drive health (SMART):", out)
        self.assertIn("one (warning): Current_Pending_Sector raw value is 3; trend: Pending 1 → 3 "
                      "since 2026-08-01", out)  # fmt: skip
        self.assertIn("  (not scanned by dpf)", out)
        self.assertNotIn("older than 30 days", out)
        with open(os.path.join(self.out, "report.json")) as fh:
            health = json.load(fh)["summary"]["overview"]["health"]
        (entry,) = health.values()
        self.assertEqual((entry["serial"], entry["status"], entry["stale"]), ("S1", "warning", False))
        if HAVE_XLSXWRITER:
            wb = Workbook(os.path.join(self.out, "report.xlsx"))
            self.addCleanup(wb.close)
            values = list(wb.cells("Drives").values())
            self.assertIn("SMART trend", values)
            self.assertIn("Pending 1 → 3 since 2026-08-01", values)
            self.assertIn("Backup 2TB", values)

    def test_drives_without_readings_has_no_health_column(self):
        self.run_cli("scan", "-q", self.drives)
        code, out, _ = self.run_cli("drives")
        self.assertNotIn("Health", out)


class SmartReadCliTest(CliFixture):
    """`dpf smart` reading drives through a fake smartctl/sudo/lsblk/losetup/dmesg."""

    def setUp(self):
        super().setUp()
        self.fake = FakeSystem()
        self.fake.as_root = os.geteuid() == 0
        self.fake.smart[("-a", "/dev/sda")] = ata("/dev/sda", "WD-1A", pending=1)
        self.fake.smart[("-a", "-d", "sat", "/dev/sdb")] = ata("/dev/sdb", "WD-CA13ZBDK")
        self.media = os.path.join(self.tmp, "media")
        self.mounts = [
            ("/dev/sda1", os.path.join(self.media, "Storage 1A 4TB")),
            ("/dev/loop24p2", os.path.join(self.media, "Storage 4A 8TB")),
        ]
        for target, value in (
            ("dpf.smartctl.run_captured", self.fake.run),
            ("dpf.smartctl.run_interactive", self.fake.interactive),
            ("dpf.smartctl.find_smartctl", lambda: "/usr/sbin/smartctl"),
            ("dpf.smartctl.read_mounts", lambda path=None: self.mounts),
            ("dpf.smartctl.SYS", os.path.join(self.tmp, "sys")),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reads_every_drive(self):
        self.fake.password = True
        patcher = mock.patch("dpf.smartctl.datetime")
        clock = patcher.start()
        self.addCleanup(patcher.stop)
        clock.now.return_value = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        code, out, err = self.run_cli("smart")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"Storage 1A 4TB\s+WDC WD80EFZZ\s+WD-1A\s+warning\s+2 flags")
        self.assertRegex(out, r"Storage 4A 8TB\s+WDC WD80EFZZ\s+WD-CA13ZBDK\s+OK")
        self.assertIn("stored 2 readings", out)
        self.assertEqual(self.fake.calls.count(["sudo", "-v"]), 0 if self.fake.as_root else 1)
        with Index(self.db) as idx:
            (latest, _) = idx.smart_latest(full=True)
        self.assertEqual(latest["mounts"], [os.path.join(self.media, "Storage 1A 4TB")])
        self.assertEqual(latest["source"], "smartctl")
        self.assertIn("ata_smart_attributes", latest["smartctl"])

        code, out, _ = self.run_cli("smart")  # within the same second: the same reading
        self.assertIn("stored 0 readings", out)
        self.fake.smart[("-a", "/dev/sda")] = ata("/dev/sda", "WD-1A", pending=4)
        clock.now.return_value = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        code, out, _ = self.run_cli("smart")
        self.assertIn("stored 2 readings", out)
        self.assertIn("Pending 1 → 4 since 2026-10-31", out)
        with Index(self.db) as idx:
            self.assertEqual(len(idx.smart_history("WD-1A")), 2)

    def test_path_resolves_through_the_loop_device(self):
        code, out, err = self.run_cli("smart", os.path.join(self.media, "Storage 4A 8TB", "x"))
        self.assertEqual(code, 0, err)
        self.assertIn("Storage 4A 8TB/x: /dev/sdb", err)
        self.assertIn("WD-CA13ZBDK", out)
        self.assertNotIn("WD-1A", out)
        self.assertEqual(self.fake.ran("-a", "/dev/sda"), [])

    def test_pool_reads_its_connected_drives(self):
        pools = os.path.join(self.tmp, "config", "dpf", "pools.toml")
        os.makedirs(os.path.dirname(pools))
        with open(pools, "w") as fh:
            fh.write(f'[pools]\nA = ["{self.media}/Storage [0-9]A *"]\n'
                     f'B = ["{self.media}/Storage [0-9]B *"]\n')  # fmt: skip
        code, out, err = self.run_cli("smart", "--pool", "A")
        self.assertEqual(code, 0, err)
        self.assertIn("WD-1A", out)
        self.assertIn("WD-CA13ZBDK", out)
        code, _, err = self.run_cli("smart", "--pool", "B")
        self.assertEqual(code, 1)
        self.assertIn("no drive of pool B is connected", err)

    def test_path_not_on_a_drive(self):
        code, _, err = self.run_cli("smart", os.path.join(self.tmp, "elsewhere"))
        self.assertEqual(code, 1)
        self.assertIn("not on a connected drive", err)
        self.assertEqual([c for c in self.fake.calls if "smartctl" in c[:3]], [])

    def test_unreadable_drive_fails_the_run(self):
        del self.fake.smart[("-a", "-d", "sat", "/dev/sdb")]
        code, out, _ = self.run_cli("smart")
        self.assertEqual(code, 1)
        self.assertIn("! /dev/sdb: could not read (Unknown device)", out)
        self.assertIn("stored 1 reading", out)

    def test_root_through_sudo_drops_to_the_user_before_writing(self):
        events = []
        real_index = Index

        def index(*args, **kwargs):
            events.append("index")
            return real_index(*args, **kwargs)

        self.fake.as_root = True
        env = {"SUDO_UID": str(os.getuid()), "HOME": "/root"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("os.geteuid", return_value=0), \
                mock.patch("os.initgroups", lambda *a: events.append("initgroups")), \
                mock.patch("os.setgid", lambda *a: events.append("setgid")), \
                mock.patch("os.setuid", lambda *a: events.append("setuid")), \
                mock.patch("dpf.cli.Index", index):  # fmt: skip
            code, _, err = self.run_cli("smart")
            home = os.environ["HOME"]
        self.assertEqual(code, 0, err)
        self.assertEqual(events, ["initgroups", "setgid", "setuid", "index"])
        self.assertNotEqual(home, "/root")
        self.assertFalse(any(c[0] == "sudo" for c in self.fake.calls))


if __name__ == "__main__":
    unittest.main()
