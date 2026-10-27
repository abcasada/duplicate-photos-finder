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

On the author's archive (pool A: 4 drives, 381k photos/videos, 18 TB) a full mode 1 run —
scan, match, group, report — takes about 30 seconds.

dpf is **read-only**: it never modifies, moves, or deletes anything, and never writes to
the drives it scans. Its index and reports live on the local disk under `~/.cache/dpf/`.

## Project status (2026-10-25)

| Area | State |
|---|---|
| Mode 1: name/size/date | ✅ Done and used on the author's archive (6 drives, ~550k files, ~33 TB) |
| Folder sequences, tree roll-up, tree overview, near-identical labels | ✅ Done |
| Reports: text, Excel (`.xlsx`), JSON, CSV | ✅ Done |
| Drives that aren't plugged in (compared from the index) | ✅ Done |
| Named drive pools (`--pool A`) | ✅ Done |
| Smarter mode 1 names (copy suffixes, camera numbers in renamed files) | ✅ Done |
| Mode 1b: EXIF (camera serial + shutter count) | 📋 Next: researched and benchmarked, not built |
| Mode 2: content (exact / visual) | 📋 Planned |
| Mode 3: mode 1 then mode 2 | 📋 Planned |

Developers and AI assistants resuming the work: start with [CLAUDE.md](CLAUDE.md)
(also available as `AGENTS.md`).

## Modes

| Mode | What counts as a duplicate | Speed | Status |
|---|---|---|---|
| `1` / `name` | Similar filename (case-insensitive; copy marks like ` (1)`, ` - Copy`, `-2` ignored; files renamed with a date before their camera number still match), same extension, same size, modification time within 2 s. `--exact-names` requires identical names | Seconds: reads only folder listings | ✅ available |
| `1b` (planned) | Same camera body serial + shutter count (from EXIF), so renamed or re-downloaded copies are caught and RAW+JPG pairs are recognised as one shot | ~50 min per large drive the first time (reads each file's header), cached afterwards | 📋 next |
| `2` / `content` | `--compare exact` (byte-identical, via rmlint), `visual` (perceptual, via czkawka), or `both` | Hours: reads every file | 📋 planned |
| `3` / `both` | Mode 1 report first, then mode 2 automatically | | 📋 planned |

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

Only the drives you pass are compared against each other. To keep backup drives out of
the comparison, define **drive pools** and run one report per pool.

### Drive pools

Pools live in `~/.config/dpf/pools.toml` (or `$XDG_CONFIG_HOME/dpf/pools.toml`; any other
file with `dpf --pools-file PATH …`). Each pool is a list of paths or glob patterns
(`*`, `?`, `[..]`, matched per path segment as in the shell, so `*` never spans a `/`);
pool names are case-insensitive:

```toml
[pools]
# Primary drives: the 1TB SSD plus every "Storage <N>A ..." drive
A = ["/run/media/adam/Storage 1TB", "/run/media/adam/Storage [0-9]A *"]
# Backups: every "Storage <N>B ..." drive
B = ["/run/media/adam/Storage [0-9]B *"]
```

A pattern matches both the drives mounted now and the drives in the index, so a pool
includes unplugged drives that were scanned before, and new drives such as
`Storage 4A 8TB` join their pool without editing the file. A plain path (no pattern) is
always part of the pool, so a drive that is neither connected nor scanned is reported
rather than silently left out. See [docs/pools.example.toml](docs/pools.example.toml).

```sh
# Which drives each pool stands for, connected or not, and when each was last scanned
dpf pools

# Duplicates within the primary drives, then within the backups (never between them)
dpf find --mode 1 --pool A
dpf find --mode 1 --pool B

# Index the connected drives of a pool; unplugged members are skipped (and listed)
dpf scan --pool B

# Re-run the report with different settings without rescanning
dpf find --mode 1 --no-scan --top 50 --min-files 10 --pool B

# Stricter "near-identical" (99% of each side), longer tree overview
dpf find --mode 1 --no-scan --near 0.99 --tree-top 50 --pool B

# Looser matching: ignore size (catches re-saved copies) or dates
dpf find --mode 1 --ignore-size --pool A

# Stricter matching: identical names only (no copy-mark / camera-number matching)
dpf find --mode 1 --exact-names --pool A

# Plain paths work too, instead of --pool (not together with it)
dpf find --mode 1 /run/media/adam/Storage\ 1TB

# What's in the index: files, media size, used/total space of each drive, last scan,
# which drives are plugged in, and which pool each belongs to
dpf drives

# Record used/total space of connected indexed drives without scanning them
dpf space --missing            # only drives with no space recorded yet
dpf space --pool B             # or: dpf space PATH...; no argument = all connected
```

A report run with `--pool` names the pool in its header. `dpf drives` shows each drive's
pool in a Pool column; a drive matching several pools lists them all, which means the
patterns overlap and should be fixed: `scan`/`find --pool` refuse to run until it is.

### Drive space

`dpf drives` lists, per drive: Files and Media size (what the index holds), **Used on
drive** and **Capacity** (the whole drive, as `df -H` would show it, in decimal units),
Last scanned, Connected and Pool. Every scan records the drive's space with one `statvfs`
call after its walk. In the table:

- `—`: no space recorded (the drive was scanned before dpf recorded space). A footer counts
  these and suggests `dpf space --missing`.
- `n/a`: the root is a folder inside a drive, not its mount point. Its drive's space is
  not shown, and it is left out of the totals.
- `*`: entered by hand (see below).
- A **Space as of** column appears when a drive's space was recorded at a different time
  than its last scan.

The totals line sums media size over all roots, and used/capacity over the drives that
have space data, marked `(partial)` while some drives have none.

`dpf space` records used/total space **without scanning**: one `statvfs` per connected
indexed drive, no walk and no file reads. It takes PATHs, `--pool NAME`, or nothing (every
connected drive in the index), and `--missing` limits it to drives with no space recorded
yet. It only updates drives that are already in the index: a connected drive that was
never scanned is skipped with "not scanned yet; run `dpf scan` on it first". Unplugged
drives, and folders that are not mount points, are listed as skipped.

For a drive that must not be read at all (e.g. one that may be failing), enter its space
by hand from `df -H` or `df -B1` taken earlier:

```sh
dpf space --set "/run/media/adam/Storage 4A 8TB" --used 5.09TB --capacity 8.00TB
```

`--set` never accesses the path (not even `stat`); it only stores the figures for an
indexed drive, named exactly as `dpf drives` lists it. Sizes are bytes or decimal units
(`500GB`, `5.09TB`); binary units such as `TiB` or `df -h`'s `4.7T` are refused. `dpf
space` without PATHs leaves hand-entered drives alone (it does not touch them at all);
name such a drive as a PATH to measure it instead, and a full scan of it also replaces the
hand-entered figure.

### More drives than you can connect at once

The index keeps every drive you have scanned, so drives can be scanned a few at a time
and compared together afterwards:

```sh
# Batch 1: plug in some drives of pool A, index them, unplug them
dpf scan --pool A
# Batch 2: plug in the rest and index those
dpf scan --pool A

# Compare the whole pool. Connected drives are rescanned, unplugged ones come from the index
dpf find --mode 1 --pool A
```

For each unplugged drive `find` prints `using index for … (not connected; last scanned …)`,
and the report header shows per drive whether it was *scanned now* or taken *from index*,
with the date of that scan. Folder links in the report point at the drive's mount path, so
they only open once it is plugged in again. `--no-scan` takes every drive from the index,
connected or not.

A drive whose mount folder exists but is completely empty (an unclean unplug, a failed
mount) is not taken as an empty drive when the index holds files for it: `scan`/`find`
keep its indexed files, treat it as not connected, and warn `… looks empty now but had N
files when last scanned; keeping the index (not mounted, or unreadable?)`. `--allow-empty` accepts the
empty scan instead.

With plain paths instead of a pool, shell globs only match drives that are mounted, so
spell out the paths of unplugged drives (`"/run/media/adam/Storage 2A 8TB"`), or copy them
from `dpf drives`. A path that is neither connected nor in the index is an error, and
`dpf scan PATH` refuses paths that are not connected (`dpf scan --pool` skips them).

Drives are identified by their **mount path**, which on most desktops comes from the drive
label (`/run/media/<user>/<label>`). A drive mounted at a different path, e.g. after
relabelling it, counts as a different drive: scan it under its new path, then remove the
old entry with `dpf drives --forget "/run/media/adam/Old label"`. `--forget` only edits the
index (never the drive) and needs a path exactly as `dpf drives` lists it. Until then a
pool pattern matches both the old and the new path, so `dpf pools` lists the drive twice.

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
Percentages are rounded down, so 100% always means every file. A run is not broken by
the other format of a matched shot (the JPG of a matched RAW), but such files have no
copy on the other side, so the run names them:
`DSC00001.ARW … DSC00005.JPG  9 files  (+1 inside not matched: DSC00003.ARW)` (the
Sequences sheet has a "Not matched inside" column). When the names differ on the two
sides, the run shows the B names (`→ in B: …`) and the pair's label adds
`; similar names` (the "Names" column in report.xlsx; `same_names` per group in
files.csv).

Only photos and videos are compared (everything, with `--all-files`). Other files such as
`.xmp` sidecars, `.psd` edits or notes, symlinks, and anything unreadable are counted
instead, and a pair that holds any says so, e.g. `(all photos/videos match, 12 files;
3 other files not compared)`. Check those files before deleting either side.

Duplicates sitting in the *same* folder (e.g. `IMG_1.JPG` vs `IMG_1 (1).JPG`) are
summarised separately.

#### How mode 1 compares names

The report header says `matched on similar name+size+mtime ±2s`. "Similar name" means:

- **Case is ignored.**
- **Copy marks are ignored:** ` (1)`, ` - Copy`, ` - Copy (2)`, ` copy`, `Copy of `, `~2`,
  plus `-2` / `_1` / ` 2` directly after a camera number (`DSC00001-2.jpg`).
- **Different copy numbers never match:** `X (1)` matches `X` or `X - Copy`, never
  `X (2)`. A folder holding `X (1)` and `X (2)` (e.g. a Windows bulk rename of a burst)
  is treated as a renamed series, and those names must then match exactly.
- **A date around a camera number is ignored:** `2023-08-26_DSC01234.ARW`,
  `20230826 DSC01234.ARW` and `DSC01234.ARW` are the same name. Any other text must be
  the same on both sides, so `BodyA_DSC01234` ≠ `BodyB_DSC01234` ≠ `DSC01234` and
  `DSC01234-Edit` ≠ `DSC01234-Edit-2`.
- **The extension must be the same,** so a RAW never matches its JPG.
- **Numbers after a space are kept** (`Party 2.jpg` ≠ `Party 3.jpg`), and so is `-2` after
  ordinary text, because real sequences of identical size and time use those.

Camera file numbers repeat: Sony `DSC` numbering restarts every 10,000 shots and is shared
by two bodies. That is why size and date must still match. Empty (0-byte) files are never
matched. `--exact-names` switches back to identical names only.

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

- **Phase 1** (done): scanner, SQLite index, mode 1, sequence/folder grouping, tree
  overview, text/Excel/JSON/CSV reports, offline drives, drive pools.
- **Done since**: smarter mode 1 names (copy suffixes such as `DSC00001-2.jpg` or
  `IMG_1 (1).JPG`, and dates before camera numbers; size and date must still match),
  and runs that stay whole across RAW+JPG siblings while naming the unmatched ones.
- **Next, mode 1b (EXIF)**: shot identity from the camera body serial and shutter count.
  The author's Sony A7R IV bodies and Pentax K-1 Mark II both record these, but not
  sub-second times. Uses a fast exiftool pipeline that reads only the first 512 KB of
  each file, with results cached in the index.
- **Phase 2, mode 2 (content)**: rmlint for exact copies, exiftool `ImageDataHash` for
  same-pixels-different-metadata, czkawka for visually similar (`--compare`).
- **Phase 3, mode 3**: mode 1, then mode 2 automatically.

Design history, research and measurements are in [docs/PLAN.md](docs/PLAN.md). The
requirements and every decision made so far are in
[docs/ORIGINAL_REQUEST.md](docs/ORIGINAL_REQUEST.md).

## Development

```sh
python3 -m unittest discover -s tests -t .
```

See [CLAUDE.md](CLAUDE.md) for the architecture, the hard rules (never write to scanned
drives, never wipe the index, never overclaim a match), and conventions.
