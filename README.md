# dpf — duplicate photo finder

Find duplicate photos and videos across many drives and many terabytes, and report them
as **folder-level sequences** instead of millions of individual file lines:

```
[1]  518.7 GB  /run/media/adam/Storage 3A 8TB/Photography/2025/2025-10-11 New England  ↔  /run/media/adam/Storage 3B 8TB/Photography backups/2025/2025-10-11 New England   (identical tree: 4 folders, 5,184 files)

[2]  169.0 GB  /run/media/adam/Storage 3B 8TB/Photography backups/Aircraft/2024-07-13 Airshow  ↔  /run/media/adam/Storage 3B 8TB/_Dumps BU/Dump on 2025-03-13/8/DCIM/101MSDCF   (B is fully contained in A)
       DSC01215.ARW … DSC09706.ARW  1,608 files
       covers 96% of A (1,608/1,662), 100% of B (1,608/1,608)

[3]  147.7 GB  /run/media/adam/Storage 3B 8TB/_Dumps BU/Dump on 2025-03-13/7/DCIM/101MSDCF  ↔  /run/media/adam/Storage 3B 8TB/_Dumps BU/Dump on 2025-03-13/8/DCIM/101MSDCF   (partial overlap)
       DSC01215.ARW … DSC07132.ARW  601 files
       DSC07135.ARW … DSC07415.ARW  281 files
       DSC07419.ARW … DSC07969.ARW  551 files
       covers 99% of A (1,434/1,438), 89% of B (1,434/1,608)
```

On the author's archive (5 drives, 464k photos/videos, 25.9 TB) a full mode 1 run —
scan, match, group, report — takes about 35 seconds.

dpf is **read-only**: it never modifies, moves, or deletes anything, and never writes to
the drives it scans. Its index and reports live on the local disk under `~/.cache/dpf/`.

## Modes

| Mode | What counts as a duplicate | Speed | Status |
|---|---|---|---|
| `1` / `name` | Same filename (case-insensitive), same size, modification time within 2 s | Seconds: reads only directory metadata | ✅ available |
| `2` / `content` | `--compare exact` (byte-identical, via rmlint), `visual` (perceptual, via czkawka), or `both` | Hours: reads every file | 🚧 Phase 2 |
| `3` / `both` | Mode 1 report first, then mode 2 automatically | | 🚧 Phase 3 |

## Install / run

Python 3.11+ with no third-party dependencies.

```sh
git clone https://github.com/abcasada/duplicate-photos-finder.git
cd duplicate-photos-finder
./bin/dpf --help            # run from the checkout
# or: pipx install .        # puts `dpf` on PATH
```

## Usage

```sh
# Scan all drives and report name/date duplicates
dpf find --mode 1 /run/media/adam/*

# Re-run the report with different settings without rescanning
dpf find --mode 1 --no-scan --top 50 --min-files 10 /run/media/adam/*

# Looser matching: ignore size (catches re-saved copies) or dates
dpf find --mode 1 --ignore-size /run/media/adam/*

# Just build/refresh the index
dpf scan /run/media/adam/Storage\ 1TB
```

Each `find` writes three files to `~/.cache/dpf/reports/<timestamp>-<mode>/` (or `--out DIR`):

- `report.txt` — the grouped report (also printed to the terminal)
- `report.json` — the same, structured, for scripting
- `files.csv` — every duplicate file, one row each, for when you need per-file detail

### Reading the report

Folder pairs are sorted by how many bytes they share. Each pair is labelled:

- **identical tree** — two folder hierarchies with exactly the same contents (rolled up
  into one line instead of one line per subfolder)
- **identical folders** — every file in A is in B and vice versa
- **A is fully contained in B** (or the reverse) — e.g. a camera dump that was later
  copied into a larger, sorted folder
- **partial** — some overlap; the shared files are listed as runs of consecutive names
  (`IMG_0100 … IMG_0450`), in natural sort order

Duplicates sitting in the *same* folder (e.g. `IMG_1.JPG` vs `img_1.jpg`) are summarised
separately.

## Why another tool? (research summary, Oct 2026)

Plenty of tools find duplicate files; none report them as folder sequences, which is what
makes results usable across a 30 TB archive. dpf is a thin layer that does that grouping
and delegates the heavy lifting to established tools:

| Tool | Role in dpf | Notes |
|---|---|---|
| [rmlint](https://github.com/sahib/rmlint) | Mode 2 exact matching (Phase 2) | Fast, multithreaded, JSON output. Its `-D` only finds *entirely* identical folders, and it always hashes content, so it can't do mode 1 |
| [czkawka_cli](https://github.com/qarmin/czkawka) | Mode 2 visual matching (Phase 2) | Perceptual image + video similarity, HEIC/RAW support, hash cache, JSON output |
| [exiftool](https://exiftool.org/) | EXIF capture dates; `ImageDataHash` for "same pixels, different metadata" (Phase 2/3) | |
| fclones | not used | Excellent HDD-aware exact matcher, but no release since 2024 |
| Video Duplicate Finder | not used | Strong perceptual video matching with a CLI; a candidate if czkawka's video mode falls short |
| dupeGuru, digiKam | not used | GUI-only; dupeGuru's folder mode only finds fully identical folders |
| Immich, PhotoPrism | not used | Would require ingesting the whole archive into a photo server |

Mode 1 is implemented natively (`os.scandir` + SQLite) because no external tool matches
on name and date without reading file contents.

## Roadmap

- **Phase 1** (done): scanner, SQLite index, mode 1, sequence/folder grouping, reports.
- **Phase 2**: mode 2 — rmlint exact, exiftool `ImageDataHash`, czkawka visual (`--compare`).
- **Phase 3**: mode 3 (mode 1 then mode 2), EXIF capture-date matching (`--date-source exif`).

See [docs/PLAN.md](docs/PLAN.md) for the design.

## Development

```sh
python3 -m unittest discover -s tests -t .
```
