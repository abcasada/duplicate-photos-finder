"""Drive health from SMART data, for `dpf smart`, `dpf drives` and the Drives section/sheet.

Readings come from `dpf smart` (smartctl, see dpf.smartctl) or a report of the owner's
former smart-summary tool (`dpf smart --import`), and are kept in the index with their
history (`Index.smart_latest`, `Index.smart_history`). A record holds `summary`, `flags`,
`partitions`, `kernel_log_issues` and `smartctl` as smart-summary wrote them, plus
`labels` and `mounts` (mount points traced to the disk). A drive is matched to an indexed
root by `[smart] links` in the pools file, then by a mount point equal to the root, then
by its filesystem label (the mount folder's name):

    [smart]
    links = { "WD-CA13ZBDK" = "Storage 4A 8TB" }   # serial number = drive label
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from dpf.pools import PoolError, default_pools_path

FAILED = "FAILED"
WARNING = "warning"
OK = "OK"

STALE_DAYS = 30  # a newest reading older than this is called out in reports

# ATA attributes shown as columns: (summary key, column title).
ATTRS = (
    ("pending_sectors", "Pending"),
    ("reallocated_sectors", "Reallocated"),
    ("offline_uncorrectable", "Uncorrectable"),
    ("udma_crc_errors", "CRC errors"),
)
# Counters whose change between readings is reported as a trend.
TRENDED = (*ATTRS, ("reported_uncorrectable", "Reported uncorrectable"),
           ("media_errors", "Media errors"))  # fmt: skip


class SmartError(ValueError):
    """A SMART report that cannot be read."""


@dataclass(frozen=True, slots=True)
class DriveHealth:
    serial: str
    model: str | None
    kind: str | None  # HDD, SSD, NVMe SSD
    capacity: int | None
    passed: bool | None  # SMART overall assessment
    power_on_hours: int | None
    temperature: int | None
    attrs: Mapping[str, int] = field(default_factory=dict)
    flags: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()  # filesystem labels of its partitions
    device: str = ""
    checked: str = ""  # ISO time of the reading
    mounts: tuple[str, ...] = ()  # mount points of its filesystems when read
    trend: tuple[str, ...] = ()  # counters that changed since the first reading

    @property
    def status(self) -> str:
        if self.passed is False:
            return FAILED
        return WARNING if self.flags else OK

    @property
    def name(self) -> str:
        if self.labels:
            return self.labels[0]
        if self.mounts:
            return os.path.basename(self.mounts[0].rstrip("/")) or self.mounts[0]
        return f"(no label) {self.model or self.device}"

    @property
    def years(self) -> float | None:
        return None if self.power_on_hours is None else self.power_on_hours / 8766

    @property
    def timestamp(self) -> float | None:
        return iso_seconds(self.checked)

    @property
    def date(self) -> str:
        """Local date of the reading, or ""."""
        stamp = self.timestamp
        return "" if stamp is None else time.strftime("%Y-%m-%d", time.localtime(stamp))

    def stale(self, now: float | None = None) -> bool:
        """The reading is more than STALE_DAYS old (or undated)."""
        stamp = self.timestamp
        now = time.time() if now is None else now
        return stamp is None or now - stamp > STALE_DAYS * 86400


def iso_seconds(text: str | None) -> float | None:
    """Epoch seconds of an ISO time (naive = UTC); None if absent or invalid."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def labels_of(partitions) -> tuple[str, ...]:
    """Filesystem labels in an lsblk tree, in order, without repeats."""
    out: list[str] = []

    def walk(nodes) -> None:
        for p in nodes or ():
            if p.get("label") and p["label"] not in out:
                out.append(p["label"])
            walk(p.get("children"))

    walk(partitions)
    return tuple(out)


def from_record(rec: Mapping) -> DriveHealth:
    """One reading (a smart-summary drive entry, or a record from the index)."""
    s = rec.get("summary") or {}
    attrs = {k: v for k, v in (s.get("ata_attributes") or {}).items() if isinstance(v, int)}
    nvme = s.get("nvme_health") or {}
    if nvme.get("media_errors") is not None:
        attrs["media_errors"] = nvme["media_errors"]
    labels = rec.get("labels")
    return DriveHealth(
        serial=str(s.get("serial_number") or "").strip(),
        model=s.get("model"),
        kind=s.get("type"),
        capacity=s.get("capacity_bytes"),
        passed=s.get("smart_passed"),
        power_on_hours=s.get("power_on_hours"),
        temperature=s.get("temperature_c"),
        attrs=attrs,
        flags=tuple(rec.get("flags") or ()),
        labels=tuple(labels) if labels else labels_of(rec.get("partitions")),
        device=rec.get("device_path") or "",
        checked=rec.get("collected_at") or "",
        mounts=tuple(rec.get("mounts") or ()),
    )


def load_report(path: str) -> list[dict]:
    """Every drive entry of a smart-summary JSON report (all readings, as stored)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        records = data["drives"]
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise TypeError("'drives' is not a list of entries")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SmartError(f"cannot read SMART report {path}: {e}") from None
    return [{**r, "labels": list(r.get("labels") or labels_of(r.get("partitions")))}
            for r in records]  # fmt: skip


def trend(history: Sequence[DriveHealth]) -> tuple[str, ...]:
    """Counters that differ between the first and the newest reading of one drive.

    E.g. "Pending 1 → 3 since 2026-10-25".
    """
    if len(history) < 2:
        return ()
    first, last = history[0], history[-1]
    out = []
    for key, title in TRENDED:
        a, b = first.attrs.get(key), last.attrs.get(key)
        if a is not None and b is not None and a != b:
            out.append(f"{title} {a:,} → {b:,} since {first.date or first.checked[:10]}")
    return tuple(out)


def load_health(idx) -> list[DriveHealth]:
    """The newest reading of every drive in the index (an `Index`), with its trend."""
    history: dict[str, list[DriveHealth]] = {}
    for rec in idx.smart_history():
        h = from_record(rec)
        history.setdefault(h.serial, []).append(h)
    return [replace(hs[-1], trend=trend(hs)) for hs in history.values()]


def load_links(path: str | None = None) -> dict[str, str]:
    """`[smart] links` of the pools file: serial number (any case) -> drive label."""
    path = path or default_pools_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PoolError(f"cannot read pools file {path}: {e}") from None
    table = data.get("smart", {})
    links = table.get("links", {}) if isinstance(table, dict) else None
    if not isinstance(links, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in links.items()
    ):
        raise PoolError(f'{path}: [smart] links must map serial numbers to drive labels')
    return {k.strip().casefold(): v for k, v in links.items()}


def display_name(h: DriveHealth, links: Mapping[str, str] = {}) -> str:
    """The drive's linked label, else its own name."""
    return links.get(h.serial.casefold()) or h.name


def match_drives(
    health: Sequence[DriveHealth], roots: Sequence[str], links: Mapping[str, str] = {}
) -> tuple[dict[str, DriveHealth], list[DriveHealth]]:
    """({root: health} for drives linked to a root, the rest).

    A drive belongs to the root whose folder name its `[smart] links` entry names, else
    to a root equal to one of its mount points, else to a root whose folder name is one
    of its filesystem labels. The first root listed wins a tie.
    """
    by_label: dict[str, str] = {}
    for r in roots:
        by_label.setdefault(os.path.basename(r.rstrip("/")), r)
    by_path = {r.rstrip("/") or "/": r for r in roots}
    matched: dict[str, DriveHealth] = {}
    rest = []
    for h in health:
        link = links.get(h.serial.casefold())
        if link is not None:
            candidates = [by_label.get(link)]
        else:
            candidates = [by_path.get(m.rstrip("/") or "/") for m in h.mounts]
            candidates += [by_label.get(label) for label in h.labels]
        root = next((c for c in candidates if c is not None and c not in matched), None)
        if root is not None:
            matched[root] = h
        else:
            rest.append(h)
    return matched, rest


def _num(v: int | None) -> str:
    return "—" if v is None else f"{v:,}"


def health_cells(h: DriveHealth | None) -> list[str]:
    """Text cells: Health, Model, Serial, Power-on, Temp, the ATTRS columns, Checked."""
    if h is None:
        return ["not checked"] + [""] * (5 + len(ATTRS))
    years = f"{h.years:.1f} y" if h.years is not None else "—"
    temp = f"{h.temperature} °C" if h.temperature is not None else "—"
    return [h.status, h.model or "?", h.serial, years, temp,
            *(_num(h.attrs.get(k)) for k, _ in ATTRS), h.date]  # fmt: skip


HEALTH_HEADER = ["Health", "Model", "Serial", "Power-on", "Temp",
                 *(title for _, title in ATTRS), "Checked"]  # fmt: skip


def format_health(names: Sequence[str], health: Sequence[DriveHealth | None],
                  others: Sequence[DriveHealth], links: Mapping[str, str] = {},
                  now: float | None = None) -> str:  # fmt: skip
    """The drive health table of report.txt, then each drive's warnings and trend."""
    rows = [["Drive", *HEALTH_HEADER]]
    rows += [[n, *health_cells(h)] for n, h in zip(names, health)]
    if others:
        rows.append(None)  # type: ignore[arg-type]
        rows += [[display_name(h, links), *health_cells(h)] for h in others]
    widths = [max(len(r[c]) for r in rows if r) for c in range(len(rows[0]))]
    right = set(range(4, 5 + len(ATTRS) + 1))  # power-on .. last attribute
    out = []
    for r in rows:
        if r is None:
            out.append("  (not scanned by dpf)")
            continue
        out.append("  ".join(v.rjust(w) if c in right else v.ljust(w)
                             for c, (v, w) in enumerate(zip(r, widths))).rstrip())  # fmt: skip
    drives = [(n, h) for n, h in zip(names, health) if h]
    drives += [(display_name(h, links), h) for h in others]
    warned = [(n, h) for n, h in drives if h.flags or h.trend]
    if warned:
        out.append("")
        for n, h in warned:
            parts = list(h.flags) + ([f"trend: {', '.join(h.trend)}"] if h.trend else [])
            out.append(f"{n} ({h.status}): " + "; ".join(parts))
    old = [(n, h) for n, h in drives if h.stale(now)]
    if old:
        listed = ", ".join(f"{n} ({h.date or 'undated'})" for n, h in old)
        out += ["", f"SMART reading older than {STALE_DAYS} days: {listed}. Connect "
                f"{'it' if len(old) == 1 else 'them'} and run `dpf smart`."]  # fmt: skip
    out += ["", "SMART data as last read by `dpf smart`; Pending/Reallocated/Uncorrectable "
            "above 0 are warning signs, CRC errors usually point to the cable or USB bridge."]
    return "\n".join(out) + "\n"
