import json
import os
import shutil
import tempfile
import unittest

from dpf.pools import PoolError
from dpf.smart import FAILED, OK, WARNING, format_health, load_links, load_smart_report, match_drives


def record(serial, label=None, passed=True, flags=(), when="2026-10-25T19:00:00+00:00",
           pending=0):  # fmt: skip
    parts = [{"name": "sda", "children": [{"name": "sda1", "label": label}]}] if label else []
    return {
        "collected_at": when, "device_path": "/dev/sda", "flags": list(flags),
        "partitions": parts,
        "summary": {"model": "WD", "serial_number": serial + " ", "type": "HDD",
                    "capacity_bytes": 8_000_000_000_000, "smart_passed": passed,
                    "power_on_hours": 8766, "temperature_c": 30,
                    "ata_attributes": {"pending_sectors": pending, "udma_crc_errors": 2}},
    }  # fmt: skip


class SmartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_load_match_and_format(self):
        path = self.write("r.json", json.dumps({"drives": [
            record("S1", "Storage 1A"),
            record("S1", "Storage 1A", flags=["Current_Pending_Sector raw value is 1"],
                   when="2026-10-30T00:00:00+00:00", pending=1),  # newer reading wins
            record("S4"),  # no label: linked by serial
            record("S9", "Backup 2TB", passed=False, flags=["SMART overall health FAILED"]),
        ]}))  # fmt: skip
        health = load_smart_report(path)
        self.assertEqual(len(health), 3)
        roots = ["/m/Storage 1A", "/m/Storage 4A"]
        matched, rest = match_drives(health, roots, {"s4": "Storage 4A"})
        self.assertEqual(matched["/m/Storage 1A"].status, WARNING)
        self.assertEqual(matched["/m/Storage 1A"].attrs["pending_sectors"], 1)
        self.assertEqual(matched["/m/Storage 4A"].serial, "S4")
        self.assertEqual(matched["/m/Storage 4A"].status, OK)
        self.assertEqual([h.status for h in rest], [FAILED])
        text = format_health(["Storage 1A", "Storage 4A", "Storage 9"],
                             [matched[r] for r in roots] + [None], rest)  # fmt: skip
        self.assertIn("not checked", text)
        self.assertIn("(not scanned by dpf)", text)
        self.assertIn("Backup 2TB (FAILED): SMART overall health FAILED", text)
        self.assertIn("1.0 y", text)

    def test_links_from_pools_file(self):
        path = self.write("pools.toml", '[smart]\nlinks = { "WD-X" = "Storage 4A" }\n')
        self.assertEqual(load_links(path), {"wd-x": "Storage 4A"})
        self.assertEqual(load_links(os.path.join(self.tmp, "none.toml")), {})
        bad = self.write("bad.toml", "[smart]\nlinks = 3\n")
        with self.assertRaises(PoolError):
            load_links(bad)

    def test_bad_report(self):
        with self.assertRaises(PoolError):
            load_smart_report(self.write("x.json", "{}"))


if __name__ == "__main__":
    unittest.main()
