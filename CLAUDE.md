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
6. **Pools are never compared with each other.** Pool A (primary) and pool B (backups)
   are separate runs (`--pool A`, `--pool B`). One pool per run.

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
  - **A** = `Storage 1TB` + `Storage [0-9]A *` (1A 4TB, 2A 8TB, 3A 8TB)
  - **B** = `Storage [0-9]B *` (1B 4TB, 2B 8TB, 3B 8TB). 1B is currently labelled
    "Storage 1B 4TB (2)" by mistake.
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
| `src/dpf/cli.py` | argparse front end. Subcommands `scan`, `find`, `drives` (`--forget`), `pools`. Resolves roots: online paths, offline roots from the index, or `--pool`. Guards output locations. Runs the pipeline and writes reports. Modes 2/3 currently exit 2 "not implemented". |
| `src/dpf/scanner.py` | Read-only inventory. `walk()` is an iterative `os.scandir` walk: no symlinks, skips `media.SKIP_DIRS` and junk files, and counts non-media/unreadable entries per folder (`dir_stats`). `scan_roots()` runs one thread per physical device (`st_dev`) and streams batches to the calling thread, which alone writes SQLite. Also `normalize_roots`, `split_roots` (online vs missing) and `drop_nested`. |
| `src/dpf/index.py` | SQLite at `~/.cache/dpf/index.db`. Tables: `roots`, `files` (dir, name, name_key, size, mtime, kind), `staging` (atomic per-root swap during scans), `dirs` (per-folder counts of other/unreadable entries). Roots are selected by **path-prefix range scans** (`dir >= 'R/' AND dir < 'R0'`), not LIKE. Key methods: `begin_root`/`add_records`/`finish_root`, `replace_root`, `name_buckets`, `list_dir`, `subdirs`, `dir_extras`, `totals`, `roots`, `indexed`, `containing_root`, `forget_root`. |
| `src/dpf/match_name.py` | Mode 1 matcher. Buckets come from `Index.name_buckets` (same name key, and same size unless `--ignore-size`). Within a bucket, mtime clusters are **anchored on the earliest file** (tolerance default 2 s), so no two members differ by more than the tolerance. |
| `src/dpf/grouping.py` | The core value. `group_duplicates()` turns `DuplicateGroup`s into `FolderPair`s with `Run`s of consecutive files (natural sort). Relations: `identical_tree`, `identical`, `a_in_b`/`b_in_a`, `near_identical`, `partial`. Strict tree roll-up is bounded by roots and blocked by unindexed files. `tree_pairs()` builds the **tree overview** by stripping common trailing path parts (`tree_tops`). Groups spanning more than 16 folders pair only with their first folder (`capped_groups`). |
| `src/dpf/report.py` | `report.txt` (`format_text`), `report.json`, `files.csv`, and shared label/format helpers (`pair_label`, `tree_label`, `run_lines`, `floor_percent`, `human_bytes` (decimal units), root-status lines). |
| `src/dpf/xlsx_report.py` | `report.xlsx` (optional, XlsxWriter). Sheets: Summary, Trees, Folder pairs, Sequences, Same folder, Files. Sizes are stored as bytes with a GB display format, percentages are floored, relations are colour-coded, and folders are `file://` links (stopping at Excel's link limits). |
| `src/dpf/pools.py` | Loads `pools.toml` (stdlib `tomllib`). A pattern is matched against mounted dirs **and** indexed roots, so unplugged drives stay in their pool. |
| `src/dpf/media.py` | Extension sets (images incl. RAW/HEIC, videos), `SKIP_DIRS` (`$RECYCLE.BIN`, `System Volume Information`, …), `is_junk` (`._*`, Thumbs.db, desktop.ini, .DS_Store). |
| `src/dpf/models.py` | `FileRecord`, `DuplicateGroup`, `RootStats` (frozen dataclasses). |
| `bin/dpf` | Launcher that adds `src/` to the path. It works through a symlink (e.g. `~/.local/bin/dpf`). |

Each `find` writes `report.txt`, `report.json`, `files.csv` and `report.xlsx` to
`~/.cache/dpf/reports/<timestamp>-<mode>/` (or `--out`).

## Current status (2026-10-25)

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
- **Index contents:** 6 drives (1TB, 1A, 2A, 3A, 2B, 3B). **Storage 1B has never been
  scanned.**

**In progress:** *smarter mode 1 names.* Match on the camera file number, or on the name
with copy suffixes removed (`DSC00001-2.jpg`, `IMG_1 (1).JPG`, `2023-08-26_DSC01234.ARW`),
while still requiring the same extension, size and mtime. Adds a `match_key` column with
an in-place migration, and `--exact-names` for the old behaviour. Check `git log` and
`src/dpf/names.py` to see whether it has landed.

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
- RAW+JPG runs fragment when only one extension matched.
- A file in N folders yields up to N(N-1)/2 folder pairs.
- Small trees take tree-overview slots.
- Hardlinks are not merged.
- Concurrent runs share the staging table.
- An unmounted drive whose fixed mount folder stays empty would be scanned as empty. This
  doesn't affect udisks mounts.

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
- **Subagents (Claude Code).** The user wants subagents used where helpful, with an
  explicit model and effort level, leaning toward Opus. Definitions are in
  `.claude/agents/`:
  - `implementer`: opus, high
  - `reviewer`: opus, medium, read-only
  - `lookup`: sonnet, low
  
  State the model/effort when launching agents. Run a reviewer pass before declaring
  larger features done.
- **User preferences.** Work like an experienced developer and move reasonably quickly.
  Speed of the tool matters in every mode. Keep reports readable and grouped, never a
  flat list of files. Ask before decisions that are genuinely the user's (scope,
  destructive actions, anything about their drives).
