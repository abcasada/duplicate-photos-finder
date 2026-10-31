"""Drive health from SMART data, for the Drives section and sheet.

Records are in the format of the owner's former smart-summary tool (one entry per
physical drive: `summary`, `flags`, `partitions`, `kernel_log_issues`, `smartctl`). A
drive is matched to an indexed root by its filesystem label (the mount folder's name),
or by `[smart] links` in the pools file for a drive whose label could not be read:

    [smart]
    links = { "WD-CA13ZBDK" = "Storage 4A 8TB" }   # serial number = drive label
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from dpf.pools import PoolError, default_pools_path

FAILED = "FAILED"
WARNING = "warning"
OK = "OK"

# ATA attributes shown as columns: (summary key, column title).
ATTRS = (
    ("pending_sectors", "Pending"),
    ("reallocated_sectors", "Reallocated"),
    ("offline_uncorrectable", "Uncorrectable"),
    ("udma_crc_errors", "CRC errors"),
)


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

    @property
    def status(self) -> str:
        if self.passed is False:
            return FAILED
        return WARNING if self.flags else OK

    @property
    def name(self) -> str:
        return self.labels[0] if self.labels else f"(no label) {self.model or self.device}"

    @property
    def years(self) -> float | None:
        return None if self.power_on_hours is None else self.power_on_hours / 8766


def _labels(partitions) -> tuple[str, ...]:
    out: list[str] = []

    def walk(nodes) -> None:
        for p in nodes or ():
            if p.get("label") and p["label"] not in out:
                out.append(p["label"])
            walk(p.get("children"))

    walk(partitions)
    return tuple(out)


def from_record(rec: Mapping) -> DriveHealth:
    """One drive entry of a smart-summary report."""
    s = rec.get("summary", {})
    attrs = {k: v for k, v in (s.get("ata_attributes") or {}).items() if isinstance(v, int)}
    nvme = s.get("nvme_health") or {}
    if nvme.get("media_errors") is not None:
        attrs["media_errors"] = nvme["media_errors"]
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
        labels=_labels(rec.get("partitions")),
        device=rec.get("device_path", ""),
        checked=rec.get("collected_at", ""),
    )


def load_smart_report(path: str) -> list[DriveHealth]:
    """Every drive of a smart-summary JSON report; the newest reading per serial."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        records = data["drives"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PoolError(f"cannot read SMART report {path}: {e}") from None
    newest: dict[str, DriveHealth] = {}
    for rec in records:
        h = from_record(rec)
        if h.serial and (h.serial not in newest or h.checked > newest[h.serial].checked):
            newest[h.serial] = h
    return list(newest.values())


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


def match_drives(
    health: Sequence[DriveHealth], roots: Sequence[str], links: Mapping[str, str] = {}
) -> tuple[dict[str, DriveHealth], list[DriveHealth]]:
    """({root: health} for drives whose label (or link) is a root's folder name, the rest)."""
    by_label = {os.path.basename(r.rstrip("/")): r for r in roots}
    matched: dict[str, DriveHealth] = {}
    rest = []
    for h in health:
        labels = [links[h.serial.casefold()]] if h.serial.casefold() in links else h.labels
        root = next((by_label[label] for label in labels if label in by_label), None)
        if root is not None and root not in matched:
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
            *(_num(h.attrs.get(k)) for k, _ in ATTRS), h.checked[:10]]  # fmt: skip


HEALTH_HEADER = ["Health", "Model", "Serial", "Power-on", "Temp",
                 *(title for _, title in ATTRS), "Checked"]  # fmt: skip


def format_health(names: Sequence[str], health: Sequence[DriveHealth | None],
                  others: Sequence[DriveHealth]) -> str:  # fmt: skip
    """The drive health table of report.txt, then the warnings of each drive."""
    rows = [["Drive", *HEALTH_HEADER]]
    rows += [[n, *health_cells(h)] for n, h in zip(names, health)]
    if others:
        rows.append(None)  # type: ignore[arg-type]
        rows += [[h.name, *health_cells(h)] for h in others]
    widths = [max(len(r[c]) for r in rows if r) for c in range(len(rows[0]))]
    right = set(range(4, 5 + len(ATTRS) + 1))  # power-on .. last attribute
    out = []
    for r in rows:
        if r is None:
            out.append("  (not scanned by dpf)")
            continue
        out.append("  ".join(v.rjust(w) if c in right else v.ljust(w)
                             for c, (v, w) in enumerate(zip(r, widths))).rstrip())  # fmt: skip
    warned = [(n, h) for n, h in zip(names, health) if h and h.flags]
    warned += [(h.name, h) for h in others if h.flags]
    if warned:
        out.append("")
        for n, h in warned:
            out.append(f"{n} ({h.status}): " + "; ".join(h.flags))
    out += ["", "SMART data as last read by smartctl; Pending/Reallocated/Uncorrectable above 0 "
            "are warning signs, CRC errors usually point to the cable or USB bridge."]
    return "\n".join(out) + "\n"
