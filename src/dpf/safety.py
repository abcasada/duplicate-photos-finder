"""Reading files of a scanned drive without ever updating their access times (hard rule 7).

Every open of an original file goes through `open_read`: read-only with O_NOATIME. The
kernel only grants O_NOATIME to the file's owner (or root); a refused open changes
nothing, and is never retried without the flag, except on a mount where the kernel never
updates access times anyway (`ro` or `noatime` in /proc/mounts).

Before any file under a root is read, `check_root` decides for the whole drive:
- a `ro` or `noatime` mount may be read;
- a FUSE mount (ntfs-3g shows as fuseblk) that is neither may not: its daemon keeps
  access times itself, whatever the open flags say;
- otherwise one indexed file is opened with O_NOATIME as a probe (no byte is read): a
  refusal means none of the drive's files can be read safely, so the drive is skipped.
"""

from __future__ import annotations

import errno
import itertools
import os
import pwd
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MOUNTS = "/proc/mounts"
PROBES = 5  # files tried as a probe before the check gives up (gone, unreadable)
README_HINT = 'remount it read-only (see README, "Reading a drive from a read-only mount")'


class NoAtimeDenied(OSError):
    """The kernel refused O_NOATIME (the file belongs to another user)."""


@dataclass(frozen=True, slots=True)
class Mount:
    device: str
    point: str
    fstype: str
    options: frozenset[str]

    @property
    def keeps_atime(self) -> bool:
        """False when the kernel never updates access times here (`ro` or `noatime`)."""
        return not ({"ro", "noatime"} & self.options)

    @property
    def fuse(self) -> bool:
        return self.fstype == "fuse" or self.fstype.startswith("fuse.") or self.fstype == "fuseblk"


def _unescape(text: str) -> str:
    """A /proc/mounts field: spaces and tabs are octal escapes (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def read_mounts(path: str | None = None) -> list[Mount]:
    """Every mount listed in /proc/mounts (or `path`); [] when it cannot be read."""
    out = []
    try:
        with open(path or MOUNTS, encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4:
                    out.append(Mount(
                        _unescape(parts[0]), _unescape(parts[1]), parts[2],
                        frozenset(parts[3].split(",")),
                    ))  # fmt: skip
    except OSError:
        pass
    return out


def mount_of(path: str, mounts: Sequence[Mount]) -> Mount | None:
    """The mount holding `path`, by path only (nothing is accessed); the last one listed
    wins when several share a mount point, as in the kernel."""
    path = os.path.abspath(path).rstrip("/") or "/"
    best: Mount | None = None
    for m in mounts:
        p = m.point.rstrip("/") or "/"
        if path == p or p == "/" or path.startswith(p + "/"):
            if best is None or len(p) >= len(best.point.rstrip("/") or "/"):
                best = m
    return best


@dataclass(frozen=True, slots=True)
class ReadCheck:
    """Whether the files under `root` may be read (reason None) and how."""

    root: str
    mount: Mount | None
    reason: str | None = None  # why the whole drive is skipped

    @property
    def ok(self) -> bool:
        return self.reason is None

    @property
    def plain_ok(self) -> bool:
        """A plain read-only open is acceptable (the kernel never updates atime here)."""
        return self.mount is not None and not self.mount.keeps_atime

    @property
    def message(self) -> str:
        return f"skipping {self.root}: {self.reason}"


def _owner(path: str) -> str | None:
    try:
        uid = os.stat(path).st_uid
    except OSError:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _via(mount: Mount | None) -> str:
    if mount is None:
        return "mount unknown"
    return f"mounted via {mount.fstype}" + (f" from {mount.device}" if mount.device else "")


def check_root(
    root: str, probe: Iterable[str], mounts: Sequence[Mount] | None = None
) -> ReadCheck:
    """May the files under `root` be read without updating their access times?

    `probe`: paths of files under `root` (from the index); up to PROBES of them are
    opened with O_NOATIME and closed at once, until one opens or one is refused.
    """
    mount = mount_of(root, read_mounts() if mounts is None else mounts)
    if mount is not None and not mount.keeps_atime:
        return ReadCheck(root, mount)
    if mount is not None and mount.fuse:
        return ReadCheck(root, mount, (
            f"files can't be read without updating their access time ({_via(mount)}, which "
            f"keeps access times itself); {README_HINT}"
        ))  # fmt: skip
    for path in itertools.islice(probe, PROBES):
        try:
            os.close(_open_noatime(path))
        except NoAtimeDenied:
            owner = _owner(path)
            whose = f", files owned by {owner}" if owner else ""
            return ReadCheck(root, mount, (
                f"files can't be opened without updating their access time ({_via(mount)}"
                f"{whose}); {README_HINT}"
            ))  # fmt: skip
        except OSError:
            continue
        return ReadCheck(root, mount)
    return ReadCheck(root, mount)  # nothing to probe: open_read still refuses per file


def _open_noatime(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if not noatime:
        raise NoAtimeDenied(errno.EPERM, "O_NOATIME is not available", path)
    try:
        return os.open(path, flags | noatime)
    except PermissionError as e:
        if e.errno != errno.EPERM:  # EACCES: not readable at all, flag or not
            raise
        raise NoAtimeDenied(errno.EPERM, "O_NOATIME refused", path) from e


def open_read(path: str, plain_ok: bool = False) -> int:
    """A read-only descriptor that never updates the file's access time.

    Raises NoAtimeDenied when the kernel refuses O_NOATIME, unless `plain_ok` (the file
    lies on a `ro` or `noatime` mount, ReadCheck.plain_ok): then it is opened plainly.
    """
    try:
        return _open_noatime(path)
    except NoAtimeDenied:
        if not plain_ok:
            raise
    return os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
