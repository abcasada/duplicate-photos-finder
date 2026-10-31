# CLAUDE.md: guide for AI assistants and developers working on dpf

This file is the handoff document. It describes what dpf is, the rules that must not be
broken, how the code is organised, and **exactly where the project stands**. It is written
so that any assistant (Claude or another model) or a human can resume work without the
original conversation. `AGENTS.md` is a symlink to this file.

Read in this order when resuming:
1. This file, especially "Current status" and "Next steps".
2. [docs/ORIGINAL_REQUEST.md](docs/ORIGINAL_REQUEST.md): the user's requirements, working
   preferences and every decision they have made.
3. [docs/PLAN.md](docs/PLAN.md): design history, research and measurements (speed, EXIF).
4. [README.md](README.md): user-facing usage and how to read the report.

## What dpf is

A Linux CLI that finds duplicate photos and videos across many large USB drives (the
owner has ~33 TB on 6+ NTFS drives, ~550k media files) and reports them as **folder-level
sequences** ("IMG_0100 … IMG_0450, 351 files, in folder A are also in folder B") instead
of file-by-file lists. It wraps existing tools where they fit (rmlint, exiftool, czkawka
are planned for later modes) and adds the grouping and reporting layer that no existing
tool has.

## Hard rules (do not break these)

1. **Never write to the scanned drives.** They are nearly full (one has had 25 MB free),
   and they hold the owner's photo archive. dpf only reads them (`scandir`, `lstat`, and
   later file reads). The index, caches and reports live under `~/.cache/dpf/` on the
   system disk. `cli._check_output_location` refuses `--out`/`--db` inside a scanned path
   or on a scanned drive. Tests must use temp dirs, never `/run/media`.
2. **Report only.** dpf never deletes, moves or renames files, and never generates delete
   scripts. The user decides what to delete.
3. **Never overclaim a match.** The user may delete one side based on the report.
   - Mode 1 compares names/sizes/dates, not contents, so labels say "match", never
     "identical".
   - Percentages are **floored**, so 100% always means every file
     (`report.floor_percent`).
   - Folders holding non-media or unreadable files ("other files not compared") are
     never rolled up into a matching tree, and the report flags them.
   - Tree roll-up never climbs above the scanned roots.
4. **Never wipe the index on a schema change.** The user scans drives a few at a time
   and unplugs them, so data for unplugged drives cannot be regenerated. Migrate in place
   (`ALTER TABLE`, backfill) and bump `PRAGMA user_version`. The one historical wipe
   (version < 2, before non-media tracking existed) is the only exception.
5. **Runtime is Python stdlib only.** The machine has no pip/ensurepip. XlsxWriter
   (`python3-xlsxwriter`, apt) is the single *optional* dependency. Without it dpf skips
   `report.xlsx` with a hint. Tests use `unittest`, not pytest.
6. **Pools are never searched for duplicates across each other.** Pool A (primary) and
   pool B (backups) each get their own duplicate search, also when one run handles both
   (`--pool A --pool B`); no duplicate group or folder pair ever spans two pools. The
   single, explicit cross-pool comparison is **backup coverage** (`[backups]` in
   pools.toml, `src/dpf/coverage.py`): which files of one pool have no copy anywhere in
   the other. It reads the other pool from the index only (never scans it) and reports
   missing files, never duplicates. The `[dumps]` check (`src/dpf/dumps.py`) works the
   same way across every indexed drive: dump-folder photos missing from sorted folders.

## Environment

- Ubuntu 26.04, Python 3.14.4, SQLite 3.46, Ryzen 5 3600 (6 cores / 12 threads), 31 GB RAM.
- Installed: rmlint 2.10.2, exiftool 13.50, ffmpeg, XlsxWriter 3.1.9, LibreOffice, gh,
  smartmontools (smartctl, for `dpf smart`).
  Not installed: czkawka (planned for mode 2 visual), fclones, jdupes.
- Drives mount at `/run/media/adam/<label>` (ntfs3, via udisks) **only while plugged in**.
  The mount folder disappears when unplugged.
- Hostname is `kub-desk` (192.168.1.10). The user sometimes reaches it over SSH from a
  Windows PC, and also has a separate machine `z400-1`.
- Drive pools are in `~/.config/dpf/pools.toml` (not in the repo; the example is
  `docs/pools.example.toml`):
  - **A** = `Storage 1TB` + `Storage [0-9]A *` (1A 4TB, 2A 8TB, 3A 8TB, 4A 8TB)
  - **B** = `Storage [0-9]B *` (1B 4TB, 2B 8TB, 3B 8TB, 4B 10TB). 1B is still labelled
    "Storage 1B 4TB (2)" by mistake.
  - **Storage 4A 8TB may be failing** (user, 2026-10-27): do not read it, list it, stat
    it or run dpf against it unless the user asks. Exception (user, 2026-10-31): reading
    its SMART data (`dpf smart`) is fine; that reads no filesystem. It is mounted via
    `/dev/loop24p2` backed by the disk (serial WD-CA13ZBDK, linked in `[smart] links`).
- Cameras that produce most duplicates: **two Sony A7R IV bodies** (`DSC#####`, ARW+JPG)
  and a **Pentax K-1 Mark II** (`IMGQ####`, DNG+JPG). There is also a Pentax K-50
  (`IMGP`), a Nikon D7500, GoPros (`GX`/`GH`), phones, and Sony video (`C####.MP4`).

## Running and testing

```sh
python3 -m unittest discover -s tests -t .     # full suite, about 4 s, all must pass
./bin/dpf --help                               # run from the checkout (no install)
./bin/dpf drives                               # what's indexed, what's connected, pools
./bin/dpf pools                                # pool membership
./bin/dpf find --mode 1 --pool A               # scan connected A drives + report
./bin/dpf find --mode 1 --no-scan --pool B -o /some/dir   # index only, custom out dir
./bin/dpf exif --pool A                        # cache camera EXIF of connected A drives
./bin/dpf find --mode 1b --no-scan --pool A --pool B      # same shot first, else mode 1
./bin/dpf smart                                # SMART of every drive into the index (sudo)
```

`tests/__init__.py` puts `src/` on `sys.path`. CLI tests point `XDG_CONFIG_HOME` at a
temp dir and pass `--allow-same-drive`, because `/tmp` is its own tmpfs. The EXIF tests
run the real exiftool on tiny generated JPEGs and skip when it is missing. SMART tests
never run smartctl or sudo: `tests/smart_fake.py` fakes smartctl, sudo, lsblk, losetup and
dmesg as an injected runner (CLI tests patch `dpf.smartctl.run_captured` and friends).

## Architecture

Data flows **scan → index → match → group → report**.

| Module | Role |
|---|---|
| `src/dpf/cli.py` | argparse front end. Subcommands `scan`, `exif`, `find` (`--mode 1` or `1b`), `drives` (`--forget`; Health column once `dpf smart` has stored readings), `space` (`--missing`, `--set`), `smart` (PATHs / `--pool` / `--import JSON`, `--[no-]sudo`; as root via sudo it drops to `SUDO_UID` before opening the index: `_sudo_user`/`_drop_root`), `folders`, `pools`. Resolves roots: online paths, offline roots from the index, or `--pool`. Guards output locations. Runs the pipeline and writes reports. Modes 2/3 currently exit 2 "not implemented". |
| `src/dpf/scanner.py` | Read-only inventory. `walk()` is an iterative `os.scandir` walk: no symlinks, skips `media.SKIP_DIRS` and junk files, and counts non-media/unreadable entries per folder (`dir_stats`). `scan_roots()` runs one thread per physical device (`st_dev`) and streams batches to the calling thread, which alone writes SQLite. Also `normalize_roots`, `split_roots` (online vs missing), `drop_nested`, and `drive_space` (one `os.statvfs` + `ismount`, taken once per root after its walk). |
| `src/dpf/index.py` | SQLite at `~/.cache/dpf/index.db`. Tables: `roots` (also the drive's `used_bytes`, `capacity_bytes`, `is_mount`, `space_source` = statvfs/manual, `space_at`), `files` (dir, name, name_key, match_key, size, mtime, kind), `staging` (atomic per-root swap during scans), `dirs` (per-folder counts of other/unreadable entries), `exif` (schema v6/v7: the `dpf exif` cache of photos keyed by (dir, name) with the read_size/read_mtime it was read at, valid only while they equal the files row's; kept apart from `files` so rescans keep it; `shot`/`ident` keys, `shutter_mode`, index `exif_shot`), `smart` (schema v8: every SMART reading, UNIQUE (serial, read_at); JSON columns labels, mounts, summary, flags, kernel, partitions, smartctl; `source` = smartctl/import). Roots are selected by **path-prefix range scans** (`dir >= 'R/' AND dir < 'R0'`), not LIKE. Key methods: `begin_root`/`add_records`/`finish_root`, `replace_root`, `name_buckets`, `coverage_buckets`, `name_keys`, `empty_files`, `extras_under`, `list_dir`, `subdirs`, `dir_extras`, `totals`, `roots`, `indexed`, `containing_root`, `forget_root` (also drops exif rows, never smart rows), `put_smart` (INSERT OR IGNORE: re-imports add nothing), `smart_latest`, `smart_history`, `set_space` (updates an existing roots row only); EXIF: `exif_todo`, `put_exif`, `exif_row`, `exif_status`, `multi_format_shots`, `shot_groups`, `shot_name_buckets`, `shot_coverage_buckets` (adds each file's shot and whether the other side holds it, 2 = with the same size, via a subquery on `exif_shot`); `_shot_guard` (temp table `bad_shots`: keys held by files with different camera numbers or under two match keys in one folder, index-wide; every shot query leaves them out; recomputed after writes); `__del__` closes the connection. |
| `src/dpf/names.py` | `match_key(name)`: the similar-name key for mode 1 (copy marks stripped; around a camera-number token only *neutral* text (dates, separators) is dropped, other text is kept; extension kept). `copy_numbers(name)` feeds the matcher's copy-number rules. `match_key` is stored in the index, so **any change to it needs a schema-version bump and a backfill**, never a wipe. |
| `src/dpf/match_name.py` | Mode 1 matcher; `bucket_groups` holds the per-bucket rules and is shared with coverage. Buckets come from `Index.name_buckets` (same `match_key`, or `name_key` with `--exact-names`, and same size unless `--ignore-size`; 0-byte files excluded). A bucket where one folder holds two different copy numbers (`X (1)`, `X (2)`) is a renamed series and falls back to exact names (`is_series`). Within a bucket, mtime clusters are **anchored on the earliest file** (tolerance default 2 s), so no two members differ by more than the tolerance; then (unless `--strict-time`) whole clusters are merged across **whole-hour shifts** (`_hour_merge`: all residues mod 3600 within the tolerance, span ≤ 14 h + tol), so every pair in a group satisfies `times_match` and nothing chains; strict clusters are never split. A cluster with several copy numbers is then split by number. `DuplicateGroup.hours` holds each file's offset in hours. |
| `src/dpf/grouping.py` | The core value. `group_duplicates()` turns `DuplicateGroup`s into `FolderPair`s with `Run`s of consecutive files (natural sort). Relations: `identical_tree`, `identical`, `a_in_b`/`b_in_a`, `near_identical`, `partial`. Strict tree roll-up is bounded by roots and blocked by unindexed files. `tree_pairs()` builds the **tree overview** by stripping common trailing path parts (`tree_tops`). Groups spanning more than 16 folders pair only with their first folder (`capped_groups`). Pairs, runs and trees carry `hour_shift` (offsets B after A that some match needed) and `category` (OTHER only when both folders are other media); roll-up never crosses a category boundary; `GroupingResult.only(cat)` filters. |
| `src/dpf/report.py` | `report.txt` (`format_text`, `format_coverage`, `footer_lines`), `report.json` (`write_json`, combined `write_pools_json`), `files.csv` (`write_pool_csv` adds a pool column), `missing.csv` (`write_missing_csv`), and shared label/format helpers (`pair_label`, `tree_label`, `run_lines`, `floor_percent`, `human_bytes` (decimal units), root-status lines). |
| `src/dpf/xlsx_report.py` | `report.xlsx` (optional, XlsxWriter). Sheets: Summary, Trees, Folder pairs, Sequences, Same folder, Files, plus one per coverage direction ("A not in B"). `write_combined_xlsx` writes several pools into one workbook with pool-prefixed sheet names (`sheet_name`: unique, ≤31 chars). Sizes are stored as bytes with a GB display format, percentages are floored, relations are colour-coded, and folders are `file://` links (stopping at Excel's link limits). |
| `src/dpf/pools.py` | Loads `pools.toml` (stdlib `tomllib`), including the optional `[backups]` table (`B = "A"`: B backs up A; validated: known pools, no self-link, no cycles) into `Pool.backs_up`; `coverage_partners` lists a pool's linked pools. A pattern is matched against mounted dirs **and** indexed roots, so unplugged drives stay in their pool. Matching is per path segment (`*` never spans `/`); `cli` refuses a pool run when a root matches more than one pool. |
| `src/dpf/categories.py` | Personal vs other media from the optional `[categories] other = [...]` table of pools.toml. `Categories.of(dir)` (memoised; ancestors count; per-segment patterns, case-insensitive; absolute = anchored, relative = any depth), `of_dirs` (OTHER only if every folder is), `tally` (via `Index.dir_totals`), `load_categories`/`parse_categories`. |
| `src/dpf/folders.py` | `dpf folders`: CSV overview (`write_folders_csv`), one row per folder two levels below each indexed root, with subtree totals (`summarize`, via `Index.file_rows` and `extras_under`) and blank Personal?/Notes columns for the owner to classify folders by hand. Index only; pools are matched against indexed roots, never by globbing mounts, so no drive is touched. |
| `src/dpf/overview.py` | The Drives section/sheet (`build_overview`, `format_overview`, `personal_lines`): per drive space, photos/videos, personal/other bytes, not-media (used − media), **only copy** (personal media with no copy on another drive: in the coverage `missing` list and not in a within-pool group spanning another drive; counts lean high) and **name match** (`Coverage.near`: a same-name file within `coverage.NEAR_SIZE` = 64 kB in size; likely re-saved, unproven). Per pool personal total and unique (groups counted once); across exactly two pools, unique = A's + B's groups/singles entirely missing from A. Built only for `--category all`, since it needs every group. |
| `src/dpf/dumps.py` | Optional `[dumps]` table (`dump`, `sorted`, `min_photos`): dump folders (path contains the dump word) under the run's roots holding ≥ min_photos photos with no mode-1 match in a sorted folder (path contains the sorted word, not the dump word) on any indexed drive. `find_missing` with `index.Words` path filters (SQL `instr(lower(dir))`), so both sides may share roots. Section/sheet "Dumps not in Photography", JSON `dumps`. |
| `src/dpf/months.py` | "Photos and videos by month" (`month_totals`, `format_months`): per pool, photos/videos per month (EXIF DateTimeOriginal when cached and valid, else mtime; before 1990 or after next year = "unknown"), each duplicate group once (earliest file), personal only with `[categories]`. Built only for `--category all`. Text section, xlsx "<pool> Months" sheets with a stacked column chart (`xlsx_report._months_tables`), JSON `months`. Data from `Index.media_dates`. |
| `src/dpf/smartctl.py` | `dpf smart` reading, ported from the owner's former smart-summary tool (merged; that project is abandoned). `Commands` (runner injection; `run_root` = `sudo -n …`, on a sudo refusal `sudo -v` on the terminal once, then retry; root: no sudo), `find_smartctl` (also /usr/sbin), `scan_devices`, `read_drive` (`-a`, SAT retry when the autodetected protocol is SCSI or has no data), `has_smart_data`, `error_reason`, `detect_flags`, `build_record` (smart-summary record + `labels` + `mounts`), `get_partitions` (lsblk), `read_kernel_log` (plain dmesg, then `sudo -n` without prompting), `kernel_issues` (lines naming the disk, its ata/usb port), `read_mounts` (/proc/mounts, octal escapes), `mount_of`, `BlockMap`/`block_map` (`lsblk -l` PKNAME + `losetup` BACK-FILE: partition → loop → backing disk), `disk_mounts`, `resolve_disks`, `read_smart` (skips a second drive with the same serial). Never opens a filesystem. |
| `src/dpf/smart.py` | SMART display layer. `DriveHealth` (status OK/warning/FAILED, `name`: label, else mount folder name; `trend`; `stale()` > `STALE_DAYS` = 30), `from_record`, `labels_of`, `load_report` (smart-summary JSON, for `--import`), `trend` (TRENDED counters first vs newest reading: "Pending 1 → 3 since …"), `load_health(idx)` (newest per serial with trend), `load_links` (`[smart] links`), `match_drives` (link → mount point = root → label = root folder name), `display_name`, `health_cells`, `format_health` (text table, warnings + trend lines, old-reading note). `find` always takes health from the index (`cli._add_health`; drives of indexed roots outside the run are left out, unscanned drives listed apart); xlsx `_drives_tables`/`_health_cells` (SMART trend column, amber stale date). |
| `src/dpf/exif.py` | Mode 1b EXIF reading (photos only). `parse_entry` (exiftool `-json -n` → `ExifData`; serial tag per make: Sony `InternalSerialNumber`, others `SerialNumber`; `norm_serial`: -n byte lists to hex, numeric without leading zeros; shutter mode from Sony `Shutter`, Pentax `ShutterType`/`LiveView`: electronic = no identity), `ExifData.ident` (make\|model\|serial\|shutter\|DateTimeOriginal) and `.shot` (+ format family + pixel size; non-RAW needs one), `needs_more`, `ExifTool` (one `-stay_open` process, 60 s watchdog), `read_exif` (file list from `Index.exif_todo`; per `st_dev` 4 reader threads copy the first 512 kB with O_NOATIME (EPERM: skipped, counted) to `/dev/shm`; TIFF-based files get a sparse copy of their true size with the IFDs and needed tag data found past the head (`walk_tiff`, `_Reader`: 64 kB windows, the last 1 MB at once when IFD0 lies there, ≤ 8 MB per file), for Lightroom DNGs with IFD0 at the end; min(6, CPUs) exiftool processes parse batches of 16; more needed: a 16 MB copy (also sparse), then the whole file up to 256 MB, never the original; steps counted (`sparse`/`big`/`whole`); I/O errors and exiftool failures not cached; one writer commits every 500 rows/1 s, so Ctrl-C keeps progress; stale temp dirs cleaned). |
| `src/dpf/match_shot.py` | Mode 1b matcher. `split_by_shot` (the veto: a mode-1 group with two shot keys splits by key, keyless files apart), `find_shot_duplicates` (shot groups from `Index.shot_groups` unioned with the vetoed mode-1 groups of the same key; disjoint, no chaining; shot groups get basis `SHOT_BASIS`, `shot=True` and no hours; `DuplicateGroup.sizes_differ` feeds `Run`/`FolderPair`/`TreePair.sizes_differ`, "; same shot, sizes differ"), `ShotStats` (summary["shots"]), `basis`. Coverage lists files copied only as a shot of another size (`Coverage.resaved`: text section, "… re-saved" sheet, missing.csv `status`). |
| `src/dpf/coverage.py` | Backup coverage (see hard rule 6). `find_missing` / `missing_files` (`shots=True`: mode 1b via `_missing_by_shot`, counting `by_shot` and `vetoed` per dir) (uses `bucket_groups`, so hour shifts apply; counts files covered only through a shift), `roll_up` (whole trees bounded by roots and categories), `check_coverage` (raises ValueError on equal/nested roots across sides; `by_category`, `only`, `unindexed` non-media counts), `stale_warnings` (other pool scanned > 1 day before this pool's newest scan), `same_drive_warnings` (labels equal up to case / trailing " (N)"). |
| `src/dpf/media.py` | Extension sets (images incl. RAW/HEIC, videos), `SKIP_DIRS` (`$RECYCLE.BIN`, `System Volume Information`, …), `is_junk` (`._*`, Thumbs.db, desktop.ini, .DS_Store). |
| `src/dpf/models.py` | `FileRecord`, `DuplicateGroup`, `RootStats` (frozen dataclasses). |
| `bin/dpf` | Launcher that adds `src/` to the path. It works through a symlink (e.g. `~/.local/bin/dpf`). |

Each `find` writes `report.txt`, `report.json`, `files.csv` and `report.xlsx` (plus
`missing.csv` when coverage ran) to `~/.cache/dpf/reports/<timestamp>-<mode>/` (or
`--out`). With several `--pool`s they are combined: text sections per pool, one workbook
with pool-prefixed sheets, `{"pools": …, "coverage": …}` JSON, a `pool` CSV column.

## Current status (2026-10-31)

**Done, on `main`, pushed to github.com/abcasada/duplicate-photos-finder.**

- **Mode 1 (name/size/date):** scanner, index, matcher, grouping with sequences, tree
  roll-up, tree overview, near-identical labels, and text/JSON/CSV/XLSX reports.
- **Speed:** verified on real data. Pool A (381k files, 18 TB) takes ~27 s end to end;
  pool B (3B alone) takes ~5 s; a 2B scan takes ~10 s.
- **Offline drives:** `find` uses the index for unplugged drives, and reports show
  "scanned now" or "from index, scanned <date>".
- **Named pools** (`--pool`, `dpf pools`) and `dpf drives` (`--forget`).
- **Reviews:** an independent review found two serious false-"identical" bugs (roll-up
  above roots; ignoring sidecar/non-media files). Both are fixed, with regression tests.
- **Index contents:** 9 drives (1TB, 1A, 1B, 2A, 2B, 3A, 3B, 4A, 4B).
- **Storage 4A may be failing** (user, 2026-10-27): do not read it unless the user asks.
  Its space was recorded by hand from `df` on 2026-10-27 (5.09 TB used of 8.00 TB) with
  `dpf space --set`, which never accesses the path.
- **Drive space (schema v5):** `roots` records each drive's used/total space.
  - Every scan takes one `os.statvfs` per root after its walk (`space_source`
    'statvfs'). A kept (empty-looking) root keeps its previous values.
  - `dpf space [PATH… | --pool NAME] [--missing]` measures connected indexed drives
    without scanning (no walk, no file reads). It never creates a roots row: a connected
    drive never scanned is skipped ("not scanned yet"). Folders that are not mount
    points are skipped, as are hand-entered drives unless named as a PATH (they are not
    touched at all otherwise).
  - `dpf space --set PATH --used 5.09TB --capacity 8.00TB` stores figures by hand
    (`space_source` 'manual') for an indexed root named exactly. It never stats the path.
    Sizes are decimal (`report.parse_bytes`); binary units are refused.
  - `dpf drives` columns: Drive / folder, Files, Media size, Used on drive, Capacity,
    [Space as of, only when space was recorded at another time than the last scan], Last
    scanned, Connected, Pool. `—` = not recorded, `n/a` = folder rather than mount point
    (not in totals), `*` = entered by hand. Footer counts drives without space data and
    points to `dpf space --missing`. The old Unreadable column was dropped (scans still
    print unreadable counts).

- **Smarter mode 1 names (`src/dpf/names.py`):** the match key is the name with copy
  marks stripped (`DSC00001-2.jpg`, `IMG_1 (1).JPG`) and dates around a camera file number
  dropped (`2023-08-26_DSC01234.ARW`); other text around the number is kept
  (`BodyA_DSC01234` ≠ `BodyB_DSC01234`). Different copy numbers never match, and a
  folder holding `X (1)` and `X (2)` is a renamed series (exact names only). Extension,
  size and mtime must still match.
  - `--exact-names` restores the old rule.
  - 0-byte files are never matched.
  - The rules and their false-match reasoning are in the `names.py` docstring and
    `tests/test_names.py`. For example, ` 2` after a space and `-N` after plain text are
    kept, because real BMP sequences of identical size and time matched otherwise.
  - Real gain: pool A +27 new groups and 23 groups with added copies (+4 GB), pool B +2.
    Renamed-*and-rewritten* copies (Lightroom) differ in size, so they need mode 1b or 2.
- **Runs stay whole across same-shot siblings:** an unmatched `X.JPG` next to a matched
  `X.ARW` no longer splits a run (`grouping.beside_sibling`), but it is listed in
  `Run.skipped` and shown (`(+1 inside not matched: X.JPG)`). Same-extension siblings
  break the run. Pairs with differently named matches carry `FolderPair.similar_names`
  (`; similar names` in the text label, "Names" column in xlsx). Real data (2026-10-26):
  pool A 4,689 → 5,843 runs, with 1,429 previously hidden unmatched files now named in
  37 runs; pool B 293 runs, 789 files named in 9 runs. Group sets did not change.
- **Index schema v4** (v3 added `match_key`; v4 recomputed it with the neutral-text rule)
  is migrated in place on the real index (2026-10-26, all 549,217 rows kept). Backups
  taken before each migration: `~/.cache/dpf/index.v2-backup-20261025.db` and
  `~/.cache/dpf/index.v3-backup-20261026.db`. Delete them once v4 has proven fine.
- **Index schema v5** adds the five nullable drive-space columns to `roots`
  (`ALTER TABLE`, all NULL for existing rows; nothing else changes). It migrates in
  place on the first open by any dpf command; back up `index.db` before that first run.
- **Backup coverage (2026-10-27):** `[backups] B = "A"` in pools.toml (added to the
  user's file). `find --pool A` adds "Not backed up — no copy anywhere in pool B",
  `--pool B` "Only in the backup — no copy anywhere in pool A"; `--pool A --pool B` does
  both pools and both directions in one folder/workbook (README "Backup coverage").
  No schema change (computed from `match_key`, `size`, `mtime`). Real index (9 drives,
  881,824 files, everything from the index): A not in B 292,454 files / 5.0 TB in 43,497
  folders; B not in A 152,135 files / 4.0 TB in 1,209 folders. About 8.5 s and 6 s per
  direction; the combined `--pool A --pool B --no-scan` run takes ~37 s and writes a
  10 MB workbook.
  - Why so much is "missing": 262k A files have no same-name+size file in B at all
    (4A's 120k files vs 4B's 21k, Plex, recent shoots). 114k missing A files do have a
    similarly named file in B with another size or date (flagged "with a similar name
    there"): mostly DNG/ARW re-saved with metadata (a few kB larger, newer mtime), and
    6,442 same-name+size copies whose mtime differs by exactly whole hours (3,984 at
    −1 h, 2,453 at −6 h; e.g. 2025-11-12 Northern Lights vs 4B `_Dumps/2026-09-17`).
    Whole-hour offsets are now accepted by default (see below).
  - A backup of the index was taken before the real-data runs
    (`~/.cache/dpf/index.v5-backup-20261027.db`); no migration was needed.
- **Whole-hour time shifts (2026-10-27, user decision: on by default):** mtimes match when
  `|d| <= tol` or `|d - k*3600| <= tol`, `1 <= |k| <= 14` (`match_name.times_match`),
  for duplicates and coverage alike; `--strict-time` restores the old rule. Basis text
  `similar name+size+mtime ±2s (or whole hours)`. Reported as `; time shifted +1 h` on
  pairs/runs/trees (B after A), "N groups rely on a whole-hour time shift" in the header,
  "N files count as copied only through a whole-hour time shift" in coverage, a Time shift
  column in xlsx, `hour_shift` in files.csv/JSON.
  - Real index, `--no-scan --pool A --pool B` (9 drives): A not in B 292,454 / 5.0 TB →
    286,012 / 4.3 TB (6,442 files via a shift; big Sony C####.MP4 clips at +6 h); B not
    in A 152,135 / 4.0 TB → 146,892 / 3.4 TB (5,243 via a shift). Duplicate groups: A
    63,780 → 66,871 (10,526 rely on a shift; could free 3.8 → 4.6 TB), B 40,037 → 45,064
    (10,171; 4.6 → 5.4 TB). Shift offsets seen: +6/+7/±1/±5/±2 h. Run ~43 s vs ~38 s
    strict.
  - Known risk: fixed-size formats (uncompressed RAW, BMP) with the same name can match
    by a whole-hour coincidence; e.g. Cakewalk theme BMPs ("Green Meters/09" vs "Purple
    Meters/02", −1 h) in pool B. In pool A, 41 partial folder pairs share exactly one
    shifted file (some are genuine, e.g. one music video in 4 recovery folders).
- **Categories (2026-10-27):** `[categories] other = [...]` in pools.toml splits media
  into personal and other (README "Personal and other media"). The user's file has only
  an empty, commented-out block so far: **the patterns await the user's confirmation.**
  `find --category personal|other|all`; text report sections, xlsx Category columns,
  CSV/JSON `category`, coverage totals split, `dpf drives` Personal / Other media columns
  (its "Space as of" column becomes a footer line then, to stay within 150 columns).
  - 2026-10-30: `dpf folders` wrote the user a CSV of 386 folders (9 drives, two levels
    deep) with blank Personal?/Notes columns.
  - 2026-10-31: the user marked 20 rows "no" (everything else personal). Their answers
    are now 7 exact-path `other` patterns in `~/.config/dpf/pools.toml` (backup
    `pools.toml.bak-20261031`): 3A Plex, 4A BDs decrypted, 4A DVDs decrypted/SBXLIChampions,
    4A Plex/TV Shows, 4A Plex/TV Shows 3, 4A Plex New, 4B Plex. 2.4 TB is other media.
- **Drive summary (2026-10-31):** `src/dpf/overview.py`, see README "Drive summary and
  personal totals". Real index: 42.1 TB personal media on 9 drives (21.1 TB in A, 21.0 TB
  in B); unique 16.5 TB in A, 15.5 TB in B, **18.5 TB across all drives**; only copy
  4.8 TB, of which 3.5 TB has a same-name file in the other pool (mostly 1A ↔ 1B
  re-saved DNGs). **4A (may be failing): 673 GB personal only-copy, 93 GB of it name
  matches** (≈580 GB truly at risk); biggest is `Photography/2026` (413 GB, 138 files).
  - Name match was first any same-name file (3.5 TB). Sony counters repeat every 10,000
    shots and there are two A7R IV bodies, so same-name ARWs months apart (sizes MBs
    apart) are different photos. Since 2026-10-31 it needs the size within 64 kB
    (`Coverage.near`): 3.1 TB. `Coverage.similar` (any size) still drives the coverage
    report's "with a similar name there" note.
- **Photos and videos by month (2026-10-31, user request):** `src/dpf/months.py`, README
  "Photos and videos by month". Real index (snapshot while 1A/1B EXIF was being read):
  pool A 414,049 files / 16.6 TB personal, pool B 317,730 / 16.2 TB, each group once.
- **Dumps check (2026-10-31, user request):** `[dumps] dump = "dump", sorted =
  "photography", min_photos = 100` in the user's pools.toml (backup
  `pools.toml.bak-20261031b`). Real index: 173,172 files in 13,681 dump folders;
  77,156 (7.2 TB) not in any Photography folder; 100 folders with 100+ such photos
  (52,822 files, 3.6 TB), mostly 3A `_Dumps/_PhotographyDumps` and its 3B backup
  `_Dumps BU`. Many of the unsorted photos share names with sorted ones but are other
  shots (counter wrap). The combined report now takes ~77 s. Storage 1TB's space was
  recorded on 2026-10-31 (1.0 TB, 430.6 GB used); every drive now has space data.
- **Coverage review fixes (2026-10-27):** coverage says "media files" and adds a line
  "N other files (non-media) in M folders are not compared by this check …"; warnings
  for a stale other-pool index (> 1 day older than this pool's newest scan; fires on
  the real index today) and for two roots of one pool that look like the same drive;
  `check_coverage` refuses overlapping roots itself. XlsxWriter `constant_memory` (optional
  item) was skipped: peak memory of the real combined run is ~750 MB, and it switches to
  inline strings, which the tests' workbook reader does not parse.
- **Empty or unreadable mount folders:** a root that lists no entries at all, or cannot
  be listed, while the index holds files for it keeps its index, is reported "from index", and warns
  (`scanner.scan_roots(kept=…)`); `--allow-empty` accepts the empty scan.

- **Mode 1b, EXIF shot matching (2026-10-31, built and reviewed; not yet run on real
  data):** `dpf exif [PATH… | --pool NAME]` caches EXIF of photos (not videos) in the
  `exif` table (schema v6 CREATE TABLE; v7 recreates a table without `shutter_mode`,
  i.e. one written by the first, pre-review build); `find --mode 1b` matches by shot
  first, else mode 1 (README "Mode 1b"). Rules: same shot = same make, model, body serial
  (Sony `InternalSerialNumber` as hex bytes, others `SerialNumber`), shutter count (> 0)
  and DateTimeOriginal (as cached; a missing DTO only equals a missing DTO), same format
  family (jpeg=jpg, tiff=tif, heif=heic) and pixel size (non-RAW must have one). No
  identity for a silent/electronic shutter or Pentax live view (the counter does not
  advance), nor for any key the index-wide guard finds on files with different camera
  numbers or under two names in one folder (bursts).
  Two shot keys in one mode-1 group veto it (split by key; keyless files kept apart).
  Files without a key fall back to mode 1. Coverage, the Drives overview and the dumps
  check use the same rule. Report: "Shots:" header lines (identity count, files without
  EXIF cached, shot groups and how many differ in size, vetoed groups, RAW+JPG shots);
  coverage lines for files copied only as the same shot, vetoed missing files and files
  without EXIF on each side, plus the per-folder list of files copied only as a shot of
  another size. Synthetic 540k-file index: coverage 6.6 s vs 4.1 s mode 1, duplicates
  +3.3 s (the guard, about 2 s, runs once per run). **Back up `index.db` before the
  first run** (v7 migrates on open). The reviewer's burst repro is
  `ShotDuplicatesTest.test_silent_burst_is_not_one_shot`.

- **Drive health, smart-summary merged (2026-10-31, user request):** the owner's separate
  `~/apps/smart-summary` tool is folded into dpf and that project is abandoned (README
  "Drive health (SMART)"). `dpf smart [PATH… | --pool NAME | --import JSON]` reads SMART
  with smartctl through `sudo -n` (one `sudo -v` password prompt when needed; dpf never
  runs as root itself, and drops to `SUDO_UID` when started as root) and stores **every
  reading** in the index (**schema v8**: `smart` table, CREATE TABLE only, nothing else
  changes; back up `index.db` first). Default: every drive smartctl finds, including ones
  dpf does not scan (Backup 2TB, SSDs, NVMe). PATHs resolve via /proc/mounts → lsblk
  PKNAME → losetup backing disk (Storage 4A: `/dev/loop24p2` → `/dev/sdb`). `find` always
  shows the newest readings on the Drives section/sheet, with a trend ("Pending 1 → 3
  since …", xlsx "SMART trend" column) and a note for readings older than 30 days;
  `dpf drives` has a Health column (fits the 150-column budget: 147 with categories and
  pools). `find --smart-report` (the first, file-based step) is removed: import the file
  once with `dpf smart --import ~/apps/smart-summary/smart_report.json` (12 drives).
  Not yet run against real drives or the real index.

## Next steps (agreed with the user)

1. **Mode 1b on real data.** Back up `index.db`, run `dpf exif` per pool (1A ≈ 50 min
   estimated), then `find --mode 1b --no-scan --pool A --pool B`; compare "A not in B"
   and only-copy figures with mode 1, and spot-check shot matches whose sizes differ.
   Sample first: `dpf --db <copy> exif <folder inside a drive>`. A first sample (1,384
   photos of 1A/1B) matched every same-name DNG/JPG pair but ran at 4 files/s: 341
   Lightroom DNGs keep IFD0 at the end, so each fell through to 16 MB and full reads;
   sparse TIFF copies now handle them. Re-measure files/s and the step counts, check
   that ImageWidth is the same for an original and its Lightroom re-saved DNG, and that
   Sony `Shutter` is present on the A7R IV files. Not done: video identity (videos are
   not read).
   - **First `dpf smart` run** (after the running `dpf exif` finishes: any dpf command
     migrates the index to v8 on open). Back up `index.db`, then
     `dpf smart --import ~/apps/smart-summary/smart_report.json` and `dpf smart` with the
     drives connected; check that Storage 4A resolves through its loop device and that
     `dpf drives` / `find` show every drive's Health. Then the user can delete
     `~/apps/smart-summary`.
2. **Phase 2: mode 2 (content):** `--compare exact|visual|both`.
   - Exact: rmlint (`-T df`, `-o json`, paths via stdin `-0`, **no `--xattr`**).
   - Same pixels, different metadata: exiftool `ImageDataHash`.
   - Visual: `czkawka_cli` 12.x (GitHub `heif_raw_avif` build; exit code 11 means
     "found"; its cache is keyed by absolute path).
   - Speed rules: read only same-size candidates, one sequential reader per HDD, all
     drives in parallel, cache hashes per drive so drives needn't be connected together.
3. **Phase 3: mode 3** = mode 1 then mode 2.
   - **Open question for the user:** should mode 2 verify only the mode 1 candidates
     (fast) or do a full content scan?

Known limitations (documented, not fixed):
- A file in N folders yields up to N(N-1)/2 folder pairs.
- Small trees take tree-overview slots.
- Hardlinks are not merged.
- Concurrent runs share the staging table.

## Working conventions

- **Git.** Small, focused commits with conventional messages (`feat(scope):`, `fix:`,
  `docs:`, `chore:`). The body explains *why*. Push to `origin main` at milestones.
  - Author is set repo-locally: Adam Casada <40399292+abcasada@users.noreply.github.com>.
  - Claude sessions add a `Co-Authored-By:` trailer.
- **Code style.** Type hints, frozen/slotted dataclasses, short docstrings, little comment
  noise, ruff line length 100. Match the surrounding code.
- **Tests.** Every behaviour change gets unittest coverage, built on temp dirs. Validate
  on real data **read-only**, using `--no-scan` and `-o <scratch dir>` when drives aren't
  connected.
- **Subagents (Claude Code).** Use agents to get a high-quality result with only the
  time and tokens required. **Time and tokens are of equal value**: don't spend more of
  one to save the other. In practice, make small changes directly when you already have
  the context, and delegate large features, independent parallel work and fresh-eyes
  reviews. Every agent gets an explicit model and effort level, leaning toward Opus.
  Definitions are in `.claude/agents/`:
  - `implementer`: opus, high
  - `reviewer`: opus, medium, read-only
  - `lookup`: sonnet, low
  
  State the model/effort when launching agents. Run a reviewer pass before declaring
  larger features done.
- **User preferences.** Work like an experienced developer and move reasonably quickly.
  Speed of the tool matters in every mode. Keep reports readable and grouped, never a
  flat list of files. Ask before decisions that are genuinely the user's (scope,
  destructive actions, anything about their drives).
