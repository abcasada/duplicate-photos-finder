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
   missing files, never duplicates.

## Environment

- Ubuntu 26.04, Python 3.14.4, SQLite 3.46, Ryzen 5 3600 (6 cores / 12 threads), 31 GB RAM.
- Installed: rmlint 2.10.2, exiftool 13.50, ffmpeg, XlsxWriter 3.1.9, LibreOffice, gh.
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
    it or run dpf against it unless the user asks.
- Cameras that produce most duplicates: **two Sony A7R IV bodies** (`DSC#####`, ARW+JPG)
  and a **Pentax K-1 Mark II** (`IMGQ####`, DNG+JPG). There is also a Pentax K-50
  (`IMGP`), a Nikon D7500, GoPros (`GX`/`GH`), phones, and Sony video (`C####.MP4`).

## Running and testing

```sh
python3 -m unittest discover -s tests -t .     # full suite, about 1 s, all must pass
./bin/dpf --help                               # run from the checkout (no install)
./bin/dpf drives                               # what's indexed, what's connected, pools
./bin/dpf pools                                # pool membership
./bin/dpf find --mode 1 --pool A               # scan connected A drives + report
./bin/dpf find --mode 1 --no-scan --pool B -o /some/dir   # index only, custom out dir
```

`tests/__init__.py` puts `src/` on `sys.path`. CLI tests point `XDG_CONFIG_HOME` at a
temp dir and pass `--allow-same-drive`, because `/tmp` is its own tmpfs.

## Architecture

Data flows **scan → index → match → group → report**.

| Module | Role |
|---|---|
| `src/dpf/cli.py` | argparse front end. Subcommands `scan`, `find`, `drives` (`--forget`), `space` (`--missing`, `--set`), `pools`. Resolves roots: online paths, offline roots from the index, or `--pool`. Guards output locations. Runs the pipeline and writes reports. Modes 2/3 currently exit 2 "not implemented". |
| `src/dpf/scanner.py` | Read-only inventory. `walk()` is an iterative `os.scandir` walk: no symlinks, skips `media.SKIP_DIRS` and junk files, and counts non-media/unreadable entries per folder (`dir_stats`). `scan_roots()` runs one thread per physical device (`st_dev`) and streams batches to the calling thread, which alone writes SQLite. Also `normalize_roots`, `split_roots` (online vs missing), `drop_nested`, and `drive_space` (one `os.statvfs` + `ismount`, taken once per root after its walk). |
| `src/dpf/index.py` | SQLite at `~/.cache/dpf/index.db`. Tables: `roots` (also the drive's `used_bytes`, `capacity_bytes`, `is_mount`, `space_source` = statvfs/manual, `space_at`), `files` (dir, name, name_key, match_key, size, mtime, kind), `staging` (atomic per-root swap during scans), `dirs` (per-folder counts of other/unreadable entries). Roots are selected by **path-prefix range scans** (`dir >= 'R/' AND dir < 'R0'`), not LIKE. Key methods: `begin_root`/`add_records`/`finish_root`, `replace_root`, `name_buckets`, `coverage_buckets`, `name_keys`, `empty_files`, `extras_under`, `list_dir`, `subdirs`, `dir_extras`, `totals`, `roots`, `indexed`, `containing_root`, `forget_root`, `set_space` (updates an existing roots row only). |
| `src/dpf/names.py` | `match_key(name)`: the similar-name key for mode 1 (copy marks stripped; around a camera-number token only *neutral* text (dates, separators) is dropped, other text is kept; extension kept). `copy_numbers(name)` feeds the matcher's copy-number rules. `match_key` is stored in the index, so **any change to it needs a schema-version bump and a backfill**, never a wipe. |
| `src/dpf/match_name.py` | Mode 1 matcher; `bucket_groups` holds the per-bucket rules and is shared with coverage. Buckets come from `Index.name_buckets` (same `match_key`, or `name_key` with `--exact-names`, and same size unless `--ignore-size`; 0-byte files excluded). A bucket where one folder holds two different copy numbers (`X (1)`, `X (2)`) is a renamed series and falls back to exact names (`is_series`). Within a bucket, mtime clusters are **anchored on the earliest file** (tolerance default 2 s), so no two members differ by more than the tolerance, and a cluster with several copy numbers is split by number. |
| `src/dpf/grouping.py` | The core value. `group_duplicates()` turns `DuplicateGroup`s into `FolderPair`s with `Run`s of consecutive files (natural sort). Relations: `identical_tree`, `identical`, `a_in_b`/`b_in_a`, `near_identical`, `partial`. Strict tree roll-up is bounded by roots and blocked by unindexed files. `tree_pairs()` builds the **tree overview** by stripping common trailing path parts (`tree_tops`). Groups spanning more than 16 folders pair only with their first folder (`capped_groups`). |
| `src/dpf/report.py` | `report.txt` (`format_text`, `format_coverage`, `footer_lines`), `report.json` (`write_json`, combined `write_pools_json`), `files.csv` (`write_pool_csv` adds a pool column), `missing.csv` (`write_missing_csv`), and shared label/format helpers (`pair_label`, `tree_label`, `run_lines`, `floor_percent`, `human_bytes` (decimal units), root-status lines). |
| `src/dpf/xlsx_report.py` | `report.xlsx` (optional, XlsxWriter). Sheets: Summary, Trees, Folder pairs, Sequences, Same folder, Files, plus one per coverage direction ("A not in B"). `write_combined_xlsx` writes several pools into one workbook with pool-prefixed sheet names (`sheet_name`: unique, ≤31 chars). Sizes are stored as bytes with a GB display format, percentages are floored, relations are colour-coded, and folders are `file://` links (stopping at Excel's link limits). |
| `src/dpf/pools.py` | Loads `pools.toml` (stdlib `tomllib`), including the optional `[backups]` table (`B = "A"`: B backs up A; validated: known pools, no self-link, no cycles) into `Pool.backs_up`; `coverage_partners` lists a pool's linked pools. A pattern is matched against mounted dirs **and** indexed roots, so unplugged drives stay in their pool. Matching is per path segment (`*` never spans `/`); `cli` refuses a pool run when a root matches more than one pool. |
| `src/dpf/media.py` | Extension sets (images incl. RAW/HEIC, videos), `SKIP_DIRS` (`$RECYCLE.BIN`, `System Volume Information`, …), `is_junk` (`._*`, Thumbs.db, desktop.ini, .DS_Store). |
| `src/dpf/models.py` | `FileRecord`, `DuplicateGroup`, `RootStats` (frozen dataclasses). |
| `bin/dpf` | Launcher that adds `src/` to the path. It works through a symlink (e.g. `~/.local/bin/dpf`). |

Each `find` writes `report.txt`, `report.json`, `files.csv` and `report.xlsx` (plus
`missing.csv` when coverage ran) to `~/.cache/dpf/reports/<timestamp>-<mode>/` (or
`--out`). With several `--pool`s they are combined: text sections per pool, one workbook
with pool-prefixed sheets, `{"pools": …, "coverage": …}` JSON, a `pool` CSV column.

## Current status (2026-10-27)

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
    The user may want an option to accept whole-hour offsets; not built.
  - A backup of the index was taken before the real-data runs
    (`~/.cache/dpf/index.v5-backup-20261027.db`); no migration was needed.
- **Empty or unreadable mount folders:** a root that lists no entries at all, or cannot
  be listed, while the index holds files for it keeps its index, is reported "from index", and warns
  (`scanner.scan_roots(kept=…)`); `--allow-empty` accepts the empty scan.

## Next steps (agreed with the user)

1. **Mode 1b: EXIF matching** (slow, so it's a separate mode).
   - Read the body serial (`InternalSerialNumber` for Sony, `SerialNumber` for Pentax),
     `ShutterCount`, `DateTimeOriginal` (+ `OffsetTimeOriginal`), Make/Model and
     dimensions; for video use QuickTime `CreateDate` + `Duration`.
   - **Shot identity = (Model, serial, ShutterCount)**. The cameras record no sub-second
     time, and bursts make capture time ambiguous. RAW+JPG of one shot share that
     identity and should be reported as "same shot, different format", not duplicates.
   - Use the fast pipeline from `docs/PLAN.md` ("EXIF research"): read the first 512 KB of
     each file in folder order with ~4 reader threads per HDD, and parse with a pool of
     `exiftool -stay_open -fast` processes (CPU-bound, ~22 ms per ARW per process). Fall
     back to the full file when a tag is missing.
   - Cache results in the index keyed by (path, size, mtime) so reruns only read
     changed files.
   - Expected: ~70 files/s per HDD, so ~50 min for 1A on the first pass.
   - Proposed CLI: `dpf scan --exif`, `dpf find --mode 1b`.
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
