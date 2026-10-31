import json
import os
import shutil
import tempfile
import unittest

from dpf.index import Index
from dpf.pools import PoolError
from dpf.smart import (
    FAILED,
    OK,
    SmartError,
    from_record,
    format_health,
    iso_seconds,
    load_health,
    load_links,
    load_report,
    match_drives,
    trend,
)


def record(serial, label=None, passed=True, flags=(), when="2026-10-25T19:00:00+00:00",
           pending=0, crc=2, mounts=None):  # fmt: skip
    parts = [{"name": "sda", "children": [{"name": "sda1", "label": label}]}] if label else []
    rec = {
        "collected_at": when, "device_path": "/dev/sda", "flags": list(flags),
        "partitions": parts,
        "summary": {"model": "WD", "serial_number": serial + " ", "type": "HDD",
                    "capacity_bytes": 8_000_000_000_000, "smart_passed": passed,
                    "power_on_hours": 8766, "temperature_c": 30,
                    "ata_attributes": {"pending_sectors": pending, "udma_crc_errors": crc}},
    }  # fmt: skip
    if mounts is not None:
        rec["mounts"] = mounts
    return rec


NOW = iso_seconds("2026-10-31T00:00:00+00:00")


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
            record("S4"),  # no label: linked by serial
            record("S9", "Backup 2TB", passed=False, flags=["SMART overall health FAILED"]),
        ]}))  # fmt: skip
        records = load_report(path)
        self.assertEqual(records[0]["labels"], ["Storage 1A"])
        health = [from_record(r) for r in records]
        roots = ["/m/Storage 1A", "/m/Storage 4A"]
        matched, rest = match_drives(health, roots, {"s4": "Storage 4A"})
        self.assertEqual(matched["/m/Storage 1A"].status, OK)
        self.assertEqual(matched["/m/Storage 4A"].serial, "S4")
        self.assertEqual([h.status for h in rest], [FAILED])
        text = format_health(["Storage 1A", "Storage 4A", "Storage 9"],
                             [matched[r] for r in roots] + [None], rest, now=NOW)  # fmt: skip
        self.assertIn("not checked", text)
        self.assertIn("(not scanned by dpf)", text)
        self.assertIn("Backup 2TB (FAILED): SMART overall health FAILED", text)
        self.assertIn("1.0 y", text)
        self.assertNotIn("older than", text)

    def test_mount_point_matches_before_label(self):
        h = from_record(record("S4", "Other", mounts=["/mnt/fake/Storage 4A 8TB"]))
        self.assertEqual(h.name, "Other")
        roots = ["/mnt/fake/Other", "/mnt/fake/Storage 4A 8TB"]
        matched, rest = match_drives([h], roots)
        self.assertEqual(list(matched), ["/mnt/fake/Storage 4A 8TB"])
        unlabelled = from_record(record("S5", mounts=["/mnt/fake/Storage 4A 8TB"]))
        self.assertEqual(unlabelled.name, "Storage 4A 8TB")
        self.assertEqual(from_record(record("S6")).name, "(no label) WD")

    def test_newest_reading_wins_a_shared_label(self):
        root = "/mnt/fake/Storage 4A 8TB"
        old = from_record(record("A-OLD", "Storage 4A 8TB", passed=False,
                                 when="2026-10-01T12:00:00+00:00"))  # fmt: skip
        new = from_record(record("Z-NEW", "Storage 4A 8TB", when="2026-10-31T12:00:00+00:00"))
        for health in ([old, new], [new, old]):
            matched, rest = match_drives(health, [root])
            self.assertEqual(matched[root].serial, "Z-NEW")
            self.assertEqual([h.serial for h in rest], ["A-OLD"])
        # an exact mount point beats a newer reading that only shares the label
        mounted = from_record(record("M", "Storage 4A 8TB", when="2026-09-01T12:00:00+00:00",
                                     mounts=[root]))  # fmt: skip
        matched, _ = match_drives([new, mounted], [root])
        self.assertEqual(matched[root].serial, "M")

    def test_link_beats_a_newer_drive_with_the_same_label(self):
        root = "/mnt/fake/Storage 4A 8TB"
        linked = from_record(record("WD-L", when="2026-10-01T12:00:00+00:00"))
        other = from_record(record("X", "Storage 4A 8TB", when="2026-10-31T12:00:00+00:00",
                                   mounts=[root]))  # fmt: skip
        matched, rest = match_drives([other, linked], [root], {"wd-l": "Storage 4A 8TB"})
        self.assertEqual(matched[root].serial, "WD-L")
        self.assertEqual([h.serial for h in rest], ["X"])
        # a link to a label no root has: the linked drive is not matched otherwise
        stray = from_record(record("WD-S", "Storage 4A 8TB"))
        matched, rest = match_drives([stray], [root], {"wd-s": "Gone"})
        self.assertEqual((matched, [h.serial for h in rest]), ({}, ["WD-S"]))

    def test_trend_and_stale(self):
        history = [
            from_record(record("S1", "D", when="2026-09-01T12:00:00+00:00", pending=1)),
            from_record(record("S1", "D", when="2026-10-01T12:00:00+00:00", pending=2)),
            from_record(record("S1", "D", when="2026-10-30T12:00:00+00:00", pending=3, crc=5,
                               flags=["Current_Pending_Sector raw value is 3"])),
        ]  # fmt: skip
        self.assertEqual(trend(history), ("Pending 1 → 3 since 2026-09-01",
                                          "CRC errors 2 → 5 since 2026-09-01"))  # fmt: skip
        self.assertEqual(trend(history[:1]), ())
        self.assertEqual(trend([history[0], history[0]]), ())
        self.assertTrue(history[0].stale(NOW))
        self.assertFalse(history[2].stale(NOW))
        self.assertTrue(from_record(record("S2", when="")).stale(NOW))

    def test_format_shows_trend_and_old_readings(self):
        from dataclasses import replace

        new = replace(from_record(record("S1", "D", when="2026-10-30T12:00:00+00:00", crc=5)),
                      trend=("CRC errors 2 → 5 since 2026-09-01",))  # fmt: skip
        old = from_record(record("S2", "E", when="2026-08-01T12:00:00+00:00"))
        text = format_health(["D", "E"], [new, old], [], now=NOW)
        self.assertIn("D (OK): trend: CRC errors 2 → 5 since 2026-09-01", text)
        self.assertIn("SMART reading older than 30 days: E (2026-08-01). Connect it", text)

    def test_load_health_from_index(self):
        with Index(os.path.join(self.tmp, "i.db")) as idx:
            idx.put_smart([record("S1", "D", when="2026-10-01T12:00:00+00:00", pending=1),
                           record("S1", "D", when="2026-10-30T12:00:00+00:00", pending=4),
                           record("S2", "E")], "import")  # fmt: skip
            health = {h.serial: h for h in load_health(idx)}
        self.assertEqual(health["S1"].attrs["pending_sectors"], 4)
        self.assertEqual(health["S1"].trend, ("Pending 1 → 4 since 2026-10-01",))
        self.assertEqual(health["S1"].labels, ("D",))
        self.assertEqual(health["S2"].trend, ())

    def test_bridge_serial_keeps_drives_apart(self):
        """One serial, two models or capacities: two drives, each with its own history."""
        first = record("BRIDGE", "D", when="2026-10-01T12:00:00+00:00", pending=0)
        second = record("BRIDGE", "E", when="2026-10-20T12:00:00+00:00", pending=7)
        second["summary"]["model"] = "Seagate"
        third = record("BRIDGE", "D", when="2026-10-30T12:00:00+00:00", pending=1)
        bigger = record("BRIDGE", "F", when="2026-10-31T12:00:00+00:00")
        bigger["summary"]["capacity_bytes"] = 4_000_000_000_000
        with Index(os.path.join(self.tmp, "i.db")) as idx:
            idx.put_smart([first, second, third, bigger], "smartctl")
            health = {h.labels[0]: h for h in load_health(idx)}
        self.assertEqual(sorted(health), ["D", "E", "F"])
        self.assertEqual(health["D"].trend, ("Pending 0 → 1 since 2026-10-01",))
        self.assertEqual(health["E"].trend, ())
        self.assertEqual(health["F"].trend, ())

    def test_links_from_pools_file(self):
        path = self.write("pools.toml", '[smart]\nlinks = { "WD-X" = "Storage 4A" }\n')
        self.assertEqual(load_links(path), {"wd-x": "Storage 4A"})
        self.assertEqual(load_links(os.path.join(self.tmp, "none.toml")), {})
        bad = self.write("bad.toml", "[smart]\nlinks = 3\n")
        with self.assertRaises(PoolError):
            load_links(bad)

    def test_bad_report(self):
        with self.assertRaises(SmartError):
            load_report(self.write("x.json", "{}"))
        with self.assertRaises(SmartError):
            load_report(self.write("y.json", '{"drives": [1]}'))
        with self.assertRaises(SmartError):
            load_report(os.path.join(self.tmp, "missing.json"))


if __name__ == "__main__":
    unittest.main()
