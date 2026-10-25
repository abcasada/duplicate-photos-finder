# dpf — duplicate photo finder

Find duplicate photos and videos across many drives and many terabytes, and report them
as **folder-level sequences** instead of millions of individual file lines:

```
[1]  518.7 GB  /run/media/adam/Storage 3A 8TB/Photography/2025/2025-10-11 New England  ↔  /run/media/adam/Storage 3B 8TB/Photography backups/2025/2025-10-11 New England   (matching tree: 4 folders, 5,184 files)

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

Python 3.11+ with no third-party dependencies. The optional Excel report needs
XlsxWriter (`sudo apt install python3-xlsxwriter`, or `pipx install '.[xlsx]'`); without
it dpf skips `report.xlsx` and says so.

```sh
git clone https://github.com/abcasada/duplicate-photos-finder.git
cd duplicate-photos-finder
./bin/dpf --help            # run from the checkout
# or: pipx install .        # puts `dpf` on PATH
```

## Usage

Only the paths you pass are compared against each other. To keep a backup drive out of
the comparison, run separate reports — e.g. duplicates within the primary drives, then
within the backup drive (the second run can reuse the index):

```sh
# Duplicates within the primary ("A") drives, including the 1TB working SSD
dpf find --mode 1 /run/media/adam/Storage\ {1TB,1A,2A,3A}*

# Duplicates within the backup ("B") drive
dpf find --mode 1 /run/media/adam/Storage\ 3B*

# Re-run the report with different settings without rescanning
dpf find --mode 1 --no-scan --top 50 --min-files 10 /run/media/adam/Storage\ 3B*

# Stricter "near-identical" (99% of each side), longer tree overview
dpf find --mode 1 --no-scan --near 0.99 --tree-top 50 /run/media/adam/Storage\ 3B*

# Looser matching: ignore size (catches re-saved copies) or dates
dpf find --mode 1 --ignore-size /run/media/adam/Storage\ 3B*

# Just build/refresh the index
dpf scan /run/media/adam/Storage\ 1TB
```

Each `find` writes these files to `~/.cache/dpf/reports/<timestamp>-<mode>/` (or `--out DIR`):

- `report.txt` — the grouped report (also printed to the terminal)
- `report.xlsx` — the same as a workbook for Excel or LibreOffice: summary, tree overview,
  every folder pair, sequences, same-folder duplicates and every duplicate file, on
  sortable, filterable sheets with colour-coded relations and clickable folder links.
  Sizes and percentages are real numbers (percentages rounded down, as in the text).
  Needs XlsxWriter; `--no-xlsx` skips it
- `report.json` — the same, structured, for scripting
- `files.csv` — every duplicate file, one row each, for when you need per-file detail

### Reading the report

The report starts with a **tree overview**, then lists every folder pair. Both are
sorted by how many bytes they share. Mode 1 compares names, sizes and dates, not file
contents, so the labels say "match" rather than "identical".

#### Tree overview

Two copies of a large tree (say `Photography` and `Photography backups`) that differ in a
handful of files cannot be rolled up into one "matching tree" line, so they would show
as dozens of separate folder pairs. The overview groups those pairs by the trees they sit
in: each folder pair loses the trailing folder names its two sides share (compared
case-insensitively), so `…/3A/Photography/2025/Trip ↔ …/3B/Photography backups/2025/Trip`
belongs to `…/3A/Photography ↔ …/3B/Photography backups`. Trees never extend past the
paths you scanned, and the two sides are never the same folder or nested in each other.

```
[T1]     3.3 TB  /…/Storage 3A 8TB/Photography  ↔  /…/Storage 3B 8TB/Photography backups   (overlapping trees: 87% / 86%, 88 folder pairs, 36,427 files; 5,372 other files not compared)
       covers 87% of A (36,427/41,411), 86% of B (36,427/42,174)

[T3]   268.3 GB  /…/Storage 3A 8TB/Videography  ↔  /…/Storage 3B 8TB/Videography backups   (near-identical trees: 100% / 100%, 6 folder pairs, 281 files; 410 other files not compared)
       covers 100% of A (281/281), 100% of B (281/281)
```

- The percentages are measured against **every photo/video indexed under each tree**,
  counting each file once, so folders with no counterpart lower them. A file only counts
  as matched when its copy sits at the same relative path in the other tree.
- **near-identical trees**: at least `--near` (default 90%) of each tree matched.
  **A within B** / **A largely within B**: all, or at least `--near`, of A matched, but
  less of B (and the reverse). **overlapping trees**: everything else.
- The overview never says "matching": a tree whose folders all match is shown as a
  *matching tree* in the folder-pair list instead. Overview lines only count
  photos/videos; "other files not compared" counts them in the paired folders only.
  Read the folder pairs (tagged `(in T1)` etc.) before deleting either side.
- Only trees holding two or more folder pairs are listed (a single pair already has its
  own line). `--tree-top N` (default 20) limits the overview; `--tree-top 0` hides it.

#### Folder pairs

- **matching tree** — two folder hierarchies whose photos/videos all match, folder by
  folder, with the same subfolders on both sides (rolled up into one line instead of one
  line per subfolder). A tree is never rolled up past the paths you scanned, and never
  when any folder in it holds files that were not compared.
- **all photos/videos match** — every photo/video directly in A is in B and vice versa
  (subfolders are compared as their own pairs)
- **A is fully contained in B** (or the reverse) — e.g. a camera dump that was later
  copied into a larger, sorted folder. When B is also at least `--near` matched the
  label adds it: `(A is fully contained in B; near-identical: 100% / 99%, 4 files only in
  B)`
- **near-identical** — neither side is fully contained, but at least `--near` (default
  0.9) of each side matched: `(near-identical: 99% / 99%; 1 file only in A, 1 only in
  B)`. `--near 1` turns this label off
- **partial** — some overlap

Except for matching folders, the shared files are listed as runs of consecutive names
(`IMG_0100 … IMG_0450`), in natural sort order, followed by the coverage of each side.
Percentages are rounded down, so 100% always means every file.

Only photos and videos are compared (everything, with `--all-files`). Other files such as
`.xmp` sidecars, `.psd` edits or notes, symlinks, and anything unreadable are counted
instead, and a pair that holds any says so, e.g. `(all photos/videos match, 12 files;
3 other files not compared)`. Check those files before deleting either side.

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
