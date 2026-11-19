"""`dpf gps`: every GPS position found in the photos and videos, as a table.

Read from the metadata `dpf exif` cached in the index (photos and videos whose cached
row is still valid for the file as indexed); the drives are not touched. One row per
file: name, folder, full path, date/time taken, coordinates, plus camera, drive, pool
and a map link. Files never read by `dpf exif`, or without GPS, are not listed.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from dpf.index import Index

FIELDS = (
    "File name", "Folder name", "Full path", "Date/time taken", "Latitude", "Longitude",
    "Altitude (m)", "Kind", "Camera", "Drive", "Pool", "Map link",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class GpsRow:
    dir: str
    name: str
    kind: str
    taken: str | None
    lat: float
    lon: float
    alt: float | None
    camera: str
    drive: str
    pool: str

    @property
    def path(self) -> str:
        return os.path.join(self.dir, self.name)

    @property
    def map_link(self) -> str:
        return f"https://www.google.com/maps?q={self.lat:.6f},{self.lon:.6f}"


def _camera(make: str | None, model: str | None) -> str:
    make, model = (make or "").strip(), (model or "").strip()
    if make and model.lower().startswith(make.split()[0].lower()):
        return model  # "PENTAX K-1 Mark II", not "RICOH IMAGING ... PENTAX K-1 Mark II"
    return " ".join(p for p in (make, model) if p)


DateRange = tuple[date, date]


def parse_range(text: str) -> DateRange:
    """"2020-08-01:2021-03-07" (both ends included) or one day "2020-08-01"."""
    first, _, last = text.partition(":")
    try:
        start = date.fromisoformat(first.strip())
        end = date.fromisoformat(last.strip()) if last else start
    except ValueError:
        raise ValueError(f"bad date range {text!r}; use YYYY-MM-DD:YYYY-MM-DD") from None
    if end < start:
        raise ValueError(f"date range {text!r} ends before it starts")
    return start, end


def in_ranges(taken: str | None, ranges: Sequence[DateRange]) -> bool:
    """True without ranges; else the day taken lies in one of them (undated: never)."""
    if not ranges:
        return True
    try:
        day = date.fromisoformat((taken or "")[:10])
    except ValueError:
        return False
    return any(start <= day <= end for start, end in ranges)


def gps_rows(
    idx: Index, roots: Sequence[str], pool_of: Callable[[str], str],
    ranges: Sequence[DateRange] = (),
) -> list[GpsRow]:
    """Every GPS position under `roots` (taken within `ranges`, when given), by date
    taken (undated last), then path."""
    rows = []
    for dir, name, kind, taken, lat, lon, alt, make, model in idx.gps_rows(roots):
        if not in_ranges(taken, ranges):
            continue
        root = max((r for r in roots if dir == r or dir.startswith(r.rstrip("/") + "/")),
                   key=len, default="")  # fmt: skip
        rows.append(GpsRow(dir, name, kind, taken, lat, lon, alt, _camera(make, model),
                           os.path.basename(root) or root, pool_of(root)))  # fmt: skip
    rows.sort(key=lambda r: (r.taken is None, r.taken or "", r.dir, r.name))
    return rows


def _cells(r: GpsRow) -> list:
    return [r.name, os.path.basename(r.dir) or r.dir, r.path, r.taken or "", r.lat, r.lon,
            "" if r.alt is None else round(r.alt, 1), r.kind, r.camera, r.drive, r.pool,
            r.map_link]  # fmt: skip


def write_gps_csv(rows: Sequence[GpsRow], path: str) -> None:
    """UTF-8 with a byte-order mark, so Excel shows non-ASCII names correctly."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        w.writerows(_cells(r) for r in rows)


def write_gps_xlsx(rows: Sequence[GpsRow], path: str) -> None:
    """One sheet with a filter; raises ModuleNotFoundError without XlsxWriter."""
    import xlsxwriter

    with xlsxwriter.Workbook(path, {"strings_to_urls": False}) as wb:
        ws = wb.add_worksheet("GPS")
        head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "bottom": 1})
        coord = wb.add_format({"num_format": "0.000000"})
        widths = [24, 30, 70, 19, 11, 12, 10, 7, 22, 18, 5, 14]
        for c, (title, width) in enumerate(zip(FIELDS, widths)):
            ws.write_string(0, c, title, head)
            ws.set_column(c, c, width)
        for n, r in enumerate(rows[:1_048_575], 1):
            cells = _cells(r)
            for c in (0, 1, 2, 3, 7, 8, 9, 10):
                ws.write_string(n, c, cells[c])
            ws.write_number(n, 4, r.lat, coord)
            ws.write_number(n, 5, r.lon, coord)
            if r.alt is not None:
                ws.write_number(n, 6, round(r.alt, 1))
            # A formula, not a cell link: Excel allows only 65,530 links per sheet.
            ws.write_formula(n, 11, f'=HYPERLINK("{r.map_link}","map")', None, "map")
        ws.autofilter(0, 0, max(len(rows), 1), len(FIELDS) - 1)
        ws.freeze_panes(1, 1)
