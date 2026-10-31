"""A fake machine for SMART tests: smartctl, sudo, lsblk, losetup and dmesg as a runner."""

import json
import os
import subprocess

NAMES = {
    5: "Reallocated_Sector_Ct", 9: "Power_On_Hours", 187: "Reported_Uncorrect",
    197: "Current_Pending_Sector", 198: "Offline_Uncorrectable", 199: "UDMA_CRC_Error_Count",
}  # fmt: skip


def ata(dev, serial, model="WDC WD80EFZZ", pending=0, crc=0, passed=True, protocol="ATA",
        messages=()):  # fmt: skip
    """smartctl -a -j output of an ATA drive."""
    raw = {5: 0, 197: pending, 198: 0, 199: crc}
    return {
        "device": {"name": dev, "protocol": protocol},
        "model_name": model, "serial_number": serial, "firmware_version": "81.00A81",
        "user_capacity": {"bytes": 8_001_563_222_016}, "rotation_rate": 5640,
        "smart_status": {"passed": passed}, "power_on_time": {"hours": 26528},
        "temperature": {"current": 34}, "json_format_version": [1, 0],
        "smartctl": {"version": [7, 5], "exit_status": 0, "messages": list(messages),
                     "argv": ["smartctl"]},
        "ata_smart_attributes": {"table": [
            {"id": i, "name": NAMES[i], "when_failed": "", "raw": {"value": v}}
            for i, v in raw.items()
        ]},
    }  # fmt: skip


def bridge(dev, serial="BRIDGE"):
    """What a USB-SATA bridge returns without -d sat: SCSI, some SCSI logs only."""
    return {
        "device": {"name": dev, "protocol": "SCSI"}, "scsi_model_name": "USB bridge",
        "serial_number": serial, "scsi_error_counter_log": {"read": {}},
        "smartctl": {"version": [7, 5], "exit_status": 4},
    }  # fmt: skip


def denied(dev):
    return {"smartctl": {"messages": [
        {"string": f"Smartctl open device: {dev} failed: Permission denied", "severity": "error"}
    ]}}  # fmt: skip


class FakeSystem:
    """Runs fake commands; `calls` records every argv (interactive ones too)."""

    def __init__(self):
        self.calls = []
        self.devices = ["/dev/sda", "/dev/sdb"]
        self.smart = {}  # tuple of smartctl args after -j -> output dict
        self.password = False  # sudo -n refuses until `sudo -v` succeeded
        self.authed = False
        self.password_ok = True
        self.dmesg_plain = False  # plain dmesg readable (kernel.dmesg_restrict=0)
        self.as_root = False  # dpf itself runs as root
        self.log = ["2026-10-31T10:00:00 sd 2:0:0:0: [sda] tag#1 FAILED Result",
                    "2026-10-31T10:00:01 usb 2-1: reset SuperSpeed USB device"]  # fmt: skip
        self.blocks = [
            {"path": "/dev/sda", "name": "sda", "type": "disk", "pkname": None},
            {"path": "/dev/sda1", "name": "sda1", "type": "part", "pkname": "sda"},
            {"path": "/dev/sdb", "name": "sdb", "type": "disk", "pkname": None},
            {"path": "/dev/loop24", "name": "loop24", "type": "loop", "pkname": None},
            {"path": "/dev/loop24p2", "name": "loop24p2", "type": "part", "pkname": "loop24"},
        ]
        self.loops = [{"name": "/dev/loop24", "back-file": "/dev/sdb"}]
        self.partitions = {
            "/dev/sda": [{"name": "sda", "label": None, "children": [
                {"name": "sda1", "label": "Storage 1A 4TB"}]}],
            "/dev/sdb": [{"name": "sdb", "label": None}],
        }  # fmt: skip

    def run(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "sudo":
            assert argv[1] == "-n", argv
            if self.password and not self.authed:
                return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
            return self._exec(argv[2:], root=True)
        return self._exec(argv, root=self.as_root)

    def interactive(self, argv):
        self.calls.append(list(argv))
        assert list(argv) == ["sudo", "-v"], argv
        self.authed = self.password_ok
        return 0 if self.password_ok else 1

    def _exec(self, argv, root):
        name = os.path.basename(argv[0])
        if name == "smartctl":
            assert argv[1] == "-j", argv
            args = tuple(argv[2:])
            if args == ("--scan",):
                out = {"devices": [{"name": d} for d in self.devices]}
            elif not root:
                out = denied(args[-1])
            else:
                out = self.smart.get(args, {"smartctl": {"messages": [
                    {"string": "Unknown device", "severity": "error"}]}})  # fmt: skip
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        if name == "dmesg":
            if root or self.dmesg_plain:
                return subprocess.CompletedProcess(argv, 0, "\n".join(self.log) + "\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "dmesg: Operation not permitted\n")
        if name == "lsblk":
            if "-l" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": self.blocks}), "")
            parts = [p for dev in argv if dev in self.partitions for p in self.partitions[dev]]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": parts}), "")
        if name == "losetup":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loopdevices": self.loops}), "")
        raise AssertionError(f"unexpected command {argv}")

    def ran(self, *words):
        """Calls containing every word, in order of appearance."""
        return [c for c in self.calls if all(w in c for w in words)]
