"""Read SMART data with smartctl for `dpf smart` (ported from the owner's smart-summary).

dpf stays a normal-user process. smartctl needs raw device access, so it runs as
`sudo -n smartctl …`; when sudo wants a password, `sudo -v` asks for it once and the
command is retried. The kernel log is read with plain `dmesg` first and through `sudo -n`
only if that fails. Already root: no sudo.

Only SMART data and block-device metadata (lsblk, losetup, /proc/mounts, /sys) are read;
no filesystem on any drive is opened. A mount is traced to its physical disk through
partitions and loop devices (Storage 4A is mounted as /dev/loop24p2 backed by /dev/sdb).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dpf.smart import labels_of

MOUNTS = "/proc/mounts"
SYS = "/sys"

# ATA attributes most directly tied to imminent failure or cabling problems.
ATA_KEY_ATTRS = {
    5: "reallocated_sectors",
    10: "spin_retry_count",
    187: "reported_uncorrectable",
    188: "command_timeout",
    196: "reallocation_events",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    199: "udma_crc_errors",
}
# Of those, the ones where any non-zero raw value is a warning sign.
ATA_NONZERO_IS_BAD = (5, 187, 197, 198)

# Routine warning-level kernel messages that don't indicate a problem.
BENIGN_KERNEL_MSGS = ("using unchecked data buffer", "No UUID available providing old NGUID")

LSBLK_PARTITIONS = "NAME,TYPE,SIZE,PTTYPE,PARTLABEL,FSTYPE,LABEL,UUID,MOUNTPOINTS,FSUSE%"
DMESG = ["dmesg", "--level=emerg,alert,crit,err,warn", "--time-format=iso"]

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]
Interactive = Callable[[Sequence[str]], int]


class SmartError(Exception):
    """smartctl or sudo cannot be used, or a drive/path cannot be resolved."""


def run_captured(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Run `argv` with its output captured; a missing program gives exit status 127."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: not found\n")


def run_interactive(argv: Sequence[str]) -> int:
    """Run `argv` on the terminal (sudo -v asks for the password there)."""
    try:
        return subprocess.run(list(argv)).returncode
    except FileNotFoundError:
        return 127


def find_smartctl() -> str:
    """Path of smartctl; /usr/sbin and /sbin count even when not on PATH."""
    path = os.environ.get("PATH", "") + os.pathsep + "/usr/sbin:/sbin"
    found = shutil.which("smartctl", path=path)
    if found is None:
        raise SmartError("smartctl not found; install smartmontools (apt install smartmontools)")
    return found


def _sudo_refused(proc: subprocess.CompletedProcess) -> bool:
    """sudo -n declined to run the command (a password is needed, or not allowed)."""
    return proc.returncode == 1 and not proc.stdout.strip() and proc.stderr.startswith("sudo:")


class Commands:
    """Runs commands; `run_root` runs one as root through sudo when dpf is not root."""

    def __init__(
        self,
        *,
        sudo: bool = True,
        run: Runner | None = None,
        interactive: Interactive | None = None,
        euid: int | None = None,
        notice: Callable[[str], None] | None = None,
    ) -> None:
        self.root = (os.geteuid() if euid is None else euid) == 0
        self.sudo = sudo and not self.root
        self._run = run or run_captured
        self._interactive = interactive or run_interactive
        self._notice = notice
        self.prompted = False

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess:
        return self._run(argv)

    def run_root(self, argv: Sequence[str], *, prompt: bool = True) -> subprocess.CompletedProcess:
        """`sudo -n argv`; if sudo wants a password (and `prompt`), `sudo -v` then retry."""
        if not self.sudo:
            return self._run(argv)
        proc = self._run(["sudo", "-n", *argv])
        if prompt and _sudo_refused(proc):
            self.authenticate()
            proc = self._run(["sudo", "-n", *argv])
        return proc

    def authenticate(self) -> None:
        """Ask sudo for the password on the terminal; SmartError if it fails."""
        if self._notice and not self.prompted:
            self._notice("smartctl needs root: sudo asks for your password (dpf itself keeps "
                         "running as you)")  # fmt: skip
        self.prompted = True
        if self._interactive(["sudo", "-v"]) != 0:
            raise SmartError(
                "sudo did not authenticate; smartctl needs root (run `dpf smart` again, or "
                "as root)"
            )


def smartctl_json(cmds: Commands, smartctl: str, *args: str) -> tuple[dict | None, str]:
    """smartctl's parsed JSON output (None if unparseable) and its stderr."""
    proc = cmds.run_root([smartctl, "-j", *args])
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    return (data if isinstance(data, dict) else None), proc.stderr or ""


def scan_devices(cmds: Commands, smartctl: str) -> list[str]:
    data, err = smartctl_json(cmds, smartctl, "--scan")
    if data is None:
        raise SmartError(f"smartctl --scan failed: {err.strip() or 'no output'}")
    return [d["name"] for d in data.get("devices", []) if d.get("name")]


def has_smart_data(data: Mapping | None) -> bool:
    return bool(
        data
        and (
            "ata_smart_attributes" in data
            or "nvme_smart_health_information_log" in data
            or "scsi_error_counter_log" in data
            or "scsi_grown_defect_list" in data
        )
    )


def read_drive(cmds: Commands, smartctl: str, dev: str) -> tuple[dict | None, str]:
    """smartctl's JSON for a drive (or the best failed output) and stderr.

    smartctl autodetects the device type, but USB-to-SATA bridges come up as plain SCSI,
    so those are retried with the SAT passthrough protocol.
    """
    auto, err = smartctl_json(cmds, smartctl, "-a", dev)
    if has_smart_data(auto) and auto.get("device", {}).get("protocol") != "SCSI":
        return auto, err
    sat, sat_err = smartctl_json(cmds, smartctl, "-a", "-d", "sat", dev)
    if has_smart_data(sat):
        return sat, sat_err
    return (auto, err) if has_smart_data(auto) else ((sat or auto), err or sat_err)


def error_reason(data: Mapping | None, stderr: str = "") -> str:
    if not data:
        line = stderr.strip().splitlines()[0] if stderr.strip() else ""
        return line or "no output from smartctl"
    errors = [
        m["string"]
        for m in data.get("smartctl", {}).get("messages", [])
        if m.get("severity") == "error"
    ]
    return errors[0] if errors else "drive doesn't expose SMART data"


def detect_flags(data: Mapping, kernel_lines: Sequence[str] | None) -> list[str]:
    """Warning signs in a smartctl reading and the drive's kernel log lines."""
    flags = []
    for msg in data.get("smartctl", {}).get("messages", []):
        if msg.get("severity") == "warning":
            flags.append(f"smartctl warning: {msg['string']}")
    serious = [x for x in kernel_lines or [] if not any(b in x for b in BENIGN_KERNEL_MSGS)]
    if serious:
        flags.append(f"{len(serious)} kernel log warning/error line(s) mention this drive")
    if data.get("smart_status", {}).get("passed") is False:
        flags.append("SMART overall health assessment FAILED")

    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        if attr.get("when_failed"):
            flags.append(f"attribute {attr['name']} failed ({attr['when_failed']})")
        if attr["id"] in ATA_NONZERO_IS_BAD and attr["raw"]["value"] > 0:
            flags.append(f"{attr['name']} raw value is {attr['raw']['value']}")

    self_tests = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    failed = [
        t for t in self_tests
        if t["status"].get("passed") is False and "abort" not in t["status"]["string"].lower()
    ]  # fmt: skip
    if failed:
        flags.append(f"{len(failed)} failed self-test(s) in log")
    ata_errors = data.get("ata_smart_error_log", {}).get("summary", {}).get("count", 0)
    if ata_errors:
        flags.append(f"ATA error log has {ata_errors} entries")

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        if nvme.get("critical_warning"):
            flags.append(f"NVMe critical_warning is {nvme['critical_warning']}")
        if nvme.get("media_errors"):
            flags.append(f"NVMe media_errors is {nvme['media_errors']}")
        if nvme.get("percentage_used", 0) >= 90:
            flags.append(f"NVMe wear is at {nvme['percentage_used']}% of rated life")
        if nvme.get("available_spare", 100) < nvme.get("available_spare_threshold", 0):
            flags.append("NVMe available spare is below threshold")

    if data.get("scsi_grown_defect_list"):
        flags.append(f"{data['scsi_grown_defect_list']} grown defects")
    for op, counters in data.get("scsi_error_counter_log", {}).items():
        if isinstance(counters, dict) and counters.get("total_uncorrected_errors"):
            flags.append(f"{counters['total_uncorrected_errors']} uncorrected {op} errors")
    return flags


def build_record(
    dev: str,
    data: Mapping,
    partitions: list | None,
    kernel_lines: list[str] | None,
    mounts: Sequence[str] = (),
    *,
    now: datetime | None = None,
    host: str | None = None,
) -> dict:
    """One reading in the smart-summary record format, plus `labels` and `mounts`."""
    ata = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}
    nvme = data.get("nvme_smart_health_information_log")
    rotation = data.get("rotation_rate")
    if data.get("device", {}).get("protocol") == "NVMe":
        kind = "NVMe SSD"
    elif rotation:
        kind = "HDD"
    elif rotation == 0:
        kind = "SSD"
    else:
        kind = None

    summary = {
        "model": data.get("model_name") or data.get("scsi_model_name"),
        "serial_number": str(data.get("serial_number") or "").strip(),
        "firmware": data.get("firmware_version") or data.get("scsi_revision"),
        "type": kind,
        "protocol": data.get("device", {}).get("protocol"),
        "capacity_bytes": data.get("user_capacity", {}).get("bytes"),
        "rotation_rpm": rotation or None,
        "smart_passed": data.get("smart_status", {}).get("passed"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "power_cycle_count": data.get("power_cycle_count"),
        "temperature_c": data.get("temperature", {}).get("current"),
    }
    if ata:
        summary["ata_attributes"] = {
            name: ata[i]["raw"]["value"] for i, name in ATA_KEY_ATTRS.items() if i in ata
        }
    if nvme:
        summary["nvme_health"] = {
            k: nvme.get(k)
            for k in (
                "critical_warning", "percentage_used", "available_spare",
                "available_spare_threshold", "media_errors", "num_err_log_entries",
                "unsafe_shutdowns", "data_units_written",
            )
        }  # fmt: skip

    raw = dict(data)
    raw.pop("json_format_version", None)
    raw.pop("local_time", None)
    raw["smartctl"] = {
        k: v for k, v in data.get("smartctl", {}).items()
        if k in ("version", "exit_status", "messages")
    }  # fmt: skip
    when = now or datetime.now(timezone.utc)
    return {
        "collected_at": when.isoformat(timespec="seconds"),
        "host": host if host is not None else socket.gethostname(),
        "device_path": dev,
        "summary": summary,
        "flags": detect_flags(data, kernel_lines),
        "partitions": partitions,
        "kernel_log_issues": kernel_lines,
        "smartctl": raw,
        "labels": list(labels_of(partitions)),
        "mounts": list(mounts),
    }


# -- block devices ------------------------------------------------------------------


def block_names(dev: str, sys_root: str = SYS) -> list[str]:
    """Block device names behind a smartctl device (an NVMe controller has namespaces)."""
    name = os.path.basename(dev)
    if re.fullmatch(r"nvme\d+", name):
        folder = os.path.join(sys_root, "class", "nvme", name)
        try:
            return sorted(n for n in os.listdir(folder) if re.fullmatch(rf"{name}n\d+", n))
        except OSError:
            return []
    return [name]


def get_partitions(cmds: Commands, dev: str, sys_root: str = SYS) -> list | None:
    """lsblk's view of the drive's partitions and filesystems, or None."""
    names = block_names(dev, sys_root)
    if not names:
        return None
    proc = cmds.run(["lsblk", "-J", "-b", "-o", LSBLK_PARTITIONS, *[f"/dev/{n}" for n in names]])
    try:
        return json.loads(proc.stdout)["blockdevices"]
    except (ValueError, KeyError, TypeError):
        return None


def read_kernel_log(cmds: Commands) -> list[str] | None:
    """Warning-or-worse kernel messages, or None if the log isn't readable.

    Plain dmesg first; through `sudo -n` only if that fails, never asking for a password
    (smartctl has asked for it by then, if needed).
    """
    proc = cmds.run(DMESG)
    if proc.returncode != 0 and cmds.sudo:
        proc = cmds.run_root(DMESG, prompt=False)
    return proc.stdout.splitlines() if proc.returncode == 0 else None


def kernel_issues(
    dev: str, log_lines: Sequence[str] | None, limit: int = 100, sys_root: str = SYS
) -> list[str] | None:
    """Log lines mentioning the drive, its SATA port or its USB port."""
    if log_lines is None:
        return None
    names = block_names(dev, sys_root)
    patterns = [re.escape(n) + r"(?:p?\d+)?" for n in names]  # sda, sda1, nvme0n1p2
    if names:
        for part in os.path.realpath(os.path.join(sys_root, "block", names[0])).split("/"):
            if re.fullmatch(r"ata\d+|\d+-[\d.]+", part):  # ata3, usb port 2-1
                patterns.append(re.escape(part))
    if re.fullmatch(r"nvme\d+", os.path.basename(dev)):
        patterns.append(re.escape(os.path.basename(dev)))
    if not patterns:
        return []
    regex = re.compile(rf"(?<!\w)(?:{'|'.join(patterns)})(?!\w)")
    return [line for line in log_lines if regex.search(line)][-limit:]


def _unescape(field_: str) -> str:
    """A /proc/mounts field: spaces and tabs are octal escapes (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field_)


def read_mounts(path: str | None = None) -> list[tuple[str, str]]:
    """(device, mount point) of every mount (from /proc/mounts)."""
    out = []
    try:
        with open(path or MOUNTS, encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    out.append((_unescape(parts[0]), _unescape(parts[1])))
    except OSError:
        pass
    return out


def mount_of(path: str, mounts: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
    """The (device, mount point) holding `path`, by path only (nothing is accessed)."""
    path = os.path.abspath(path).rstrip("/") or "/"
    best = None
    for dev, point in mounts:
        p = point.rstrip("/") or "/"
        if path == p or p == "/" or path.startswith(p + "/"):
            if best is None or len(p) >= len(best[1].rstrip("/") or "/"):
                best = (dev, point)
    return best


@dataclass(slots=True)
class BlockMap:
    """Block devices (path -> (type, parent name)) and loop backing files."""

    devices: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    loops: dict[str, str] = field(default_factory=dict)

    def disk_of(self, dev: str) -> str | None:
        """The whole disk behind a partition, loop device or mapper device, or None."""
        seen: set[str] = set()
        while dev not in seen:
            seen.add(dev)
            entry = self.devices.get(dev)
            if entry is None and dev.startswith("/dev/"):
                entry = self.devices.get(os.path.realpath(dev))
            if entry is None:
                return None
            kind, parent = entry
            if kind == "disk":
                return dev
            if kind == "loop":
                back = self.loops.get(dev, "").removesuffix(" (deleted)")
                if not back.startswith("/dev/"):
                    return None  # backed by a file, not a disk
                dev = back
            elif parent:
                dev = "/dev/" + parent
            else:
                return None
        return None


def block_map(cmds: Commands) -> BlockMap:
    """Every block device (lsblk) and loop device backing (losetup); no root needed."""
    bmap = BlockMap()
    proc = cmds.run(["lsblk", "-J", "-l", "-o", "PATH,NAME,TYPE,PKNAME"])
    try:
        rows = json.loads(proc.stdout)["blockdevices"]
    except (ValueError, KeyError, TypeError):
        rows = []
    for r in rows:
        entry = (r.get("type") or "", r.get("pkname") or None)
        for key in (r.get("path"), r.get("name") and "/dev/" + r["name"]):
            if key and key not in bmap.devices:
                bmap.devices[key] = entry
    proc = cmds.run(["losetup", "-J", "-l", "-O", "NAME,BACK-FILE"])
    try:
        loops = json.loads(proc.stdout)["loopdevices"]
    except (ValueError, KeyError, TypeError):
        loops = []
    for r in loops:
        if r.get("name") and r.get("back-file"):
            bmap.loops[r["name"]] = r["back-file"]
    return bmap


def disk_mounts(mounts: Sequence[tuple[str, str]], bmap: BlockMap) -> dict[str, list[str]]:
    """Disk name (sdb, nvme0n1) -> mount points of the filesystems on it."""
    out: dict[str, list[str]] = {}
    for dev, point in mounts:
        if not dev.startswith("/dev/"):
            continue
        disk = bmap.disk_of(dev)
        if disk is not None:
            points = out.setdefault(os.path.basename(disk), [])
            if point not in points:
                points.append(point)
    return out


# -- reading ------------------------------------------------------------------------


@dataclass(slots=True)
class SmartRead:
    records: list[dict] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (device, reason)
    kernel_log: bool = True  # False: the kernel log was unreadable


def resolve_disks(
    paths: Sequence[str], mounts: Sequence[tuple[str, str]], bmap: BlockMap
) -> dict[str, str]:
    """{path: disk device} for mounted paths; SmartError naming any that can't be traced."""
    found: dict[str, str] = {}
    for path in paths:
        hit = mount_of(path, mounts)
        if hit is None or not hit[0].startswith("/dev/"):
            what = f"mounted from {hit[0]} at {hit[1]}" if hit else "not mounted"
            raise SmartError(f"{path}: not on a connected drive ({what})")
        disk = bmap.disk_of(hit[0])
        if disk is None:
            raise SmartError(f"{path}: cannot trace {hit[0]} (mounted at {hit[1]}) to a disk")
        found[path] = disk
    return found


def read_smart(
    cmds: Commands,
    smartctl: str,
    *,
    disks: Iterable[str] | None = None,
    mounts: Sequence[tuple[str, str]] | None = None,
    bmap: BlockMap | None = None,
    sys_root: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> SmartRead:
    """A reading of every drive smartctl finds, or of `disks` (e.g. /dev/sdb) only."""
    sys_root = SYS if sys_root is None else sys_root
    mounts = read_mounts() if mounts is None else mounts
    bmap = block_map(cmds) if bmap is None else bmap
    on_disk = disk_mounts(mounts, bmap)
    devices = scan_devices(cmds, smartctl)
    if disks is not None:
        wanted = {os.path.basename(d): d for d in disks}
        devices = [d for d in devices if wanted.keys() & set(block_names(d, sys_root))]
        covered = {n for d in devices for n in block_names(d, sys_root)}
        devices += [d for name, d in wanted.items() if name not in covered]

    result = SmartRead()
    readings: list[tuple[str, dict]] = []
    serials: dict[str, str] = {}
    for dev in devices:
        if progress:
            progress(dev)
        data, err = read_drive(cmds, smartctl, dev)
        serial = str((data or {}).get("serial_number") or "").strip()
        if not has_smart_data(data):
            result.failed.append((dev, error_reason(data, err)))
        elif not serial:
            result.failed.append((dev, "no serial number"))
        elif serial in serials:
            result.failed.append((dev, f"same serial number {serial} as {serials[serial]} "
                                       "(a USB bridge's serial?); not stored"))  # fmt: skip
        else:
            serials[serial] = dev
            readings.append((dev, data))
    log = read_kernel_log(cmds) if readings else []
    result.kernel_log = log is not None
    for dev, data in readings:
        points = [p for n in block_names(dev, sys_root) for p in on_disk.get(n, ())]
        result.records.append(build_record(
            dev, data, get_partitions(cmds, dev, sys_root),
            kernel_issues(dev, log, sys_root=sys_root), points,
        ))  # fmt: skip
    return result
