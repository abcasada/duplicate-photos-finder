# Plan: `dpf` — duplicate photo/video finder for multi-TB archives

> The user's full original requirements and planning decisions are in
> [ORIGINAL_REQUEST.md](ORIGINAL_REQUEST.md). Read that first when resuming.

## Context
The user has ~30 TB of photos/videos spread across 5 NTFS USB drives (`/run/media/adam/Storage *`, mounted via ntfs3; most are 96–100% full, and one has 25 MB free). They want a Linux CLI that finds duplicates and reports them as **grouped sequences/folders**, not file-by-file. It has three modes: 1 = name/date (fast), 2 = full comparison (exact / visual / both, chosen by the user), 3 = mode 1 then automatically mode 2. The tool **only reports** and never modifies files. It should reuse existing tools (rmlint, exiftool) wherever that makes sense.

Repo `/home/adam/apps/duplicate-photos-finder` is empty (local branch `master`, remote `origin` = github.com/abcasada/duplicate-photos-finder, public and empty).

## Research findings (why we build a thin wrapper)
- **No existing tool covers the whole requirement.** None turns matched runs into sequence lines or reports partial folder overlap.
  - rmlint `-D` and dupeGuru's folder mode only detect *entirely identical* folder trees.
  - czkawka `dup -m SIZE_NAME` comes closest to mode 1, but it ignores dates.
  - Immich and PhotoPrism would mean ingesting all 30 TB, and they still don't report by folder.
- **The value we add is the grouping/reporting layer, plus orchestration.** Heavy lifting is delegated:
  - **Mode 1 (name/date):** our own `os.scandir` walk. rmlint can't do this: it always hashes content, and its `-b`/`-Z` flags are only *extra* filters on top of hashing. Metadata-only matching is only about 50 lines of stdlib code, so no external tool is warranted.
  - **Mode 2 exact:** rmlint (installed, v2.10.2).
    - Paths are fed via stdin (`-0`). We pass `-T df -o json:<cache file>`, and no `--xattr`, so nothing is ever written to the drives.
    - `--replay` lets us re-filter without rehashing.
    - The JSON format is verified: groups share `checksum`, and one entry per group has `is_original`.
  - **Mode 2 metadata-only differences:** exiftool `-ImageDataHash`, run only on candidate groups because it reads whole files.
  - **Mode 2 visual:** `czkawka_cli` 12.0.2, using the GitHub `linux_czkawka_cli_heif_raw_avif_x86_64` binary (the apt version is too old).
    - Used for both `image` and `video`.
    - It has a persistent hash cache keyed by absolute path, so stable mount paths matter.
    - It exits with code 11 when duplicates are found; we must treat that as success.
  - **Advanced date matching:** exiftool reading DateTimeOriginal/CreateDate (`-stay_open`, `-json -fast2`, `-api QuickTimeUTC=1`).
- Alternatives noted for the README (not used): fclones (exact matching, HDD-aware, but no release since 2024), Video Duplicate Finder CLI (strong for video), dupeGuru/digiKam (GUI only).

## Design
**Python 3.14, stdlib only at runtime.** The system has no pip or ensurepip, so nothing needs installing. Tests use `unittest`. A `pyproject.toml` lets the user `pipx install .` later. Run it as `python3 -m dpf …` or via `./bin/dpf`.

```
src/dpf/
  cli.py        argparse: dpf scan|find; --mode {1,name,2,content,3,both}; --compare {exact,visual,both} (phase 2)
  scanner.py    parallel os.scandir walk, one thread per root/device; media-extension filter (--all-files to disable)
  index.py      SQLite inventory at ~/.cache/dpf/index.db (root disk; drives are full and must not be written)
  match_name.py mode 1 matcher
  grouping.py   duplicate groups -> folder-pair runs (the core value)
  report.py     text report (stdout + saved file), JSON, CSV (full per-file detail lives in CSV/JSON)
  xlsx_report.py report.xlsx workbook (optional XlsxWriter; skipped with a hint when missing)
  media.py      extension sets: JPEG/PNG/HEIC/AVIF/WebP/TIFF/RAW (CR2/CR3/NEF/ARW/DNG/RAF/ORF/RW2), MP4/MOV/AVI/MKV/MTS/3GP...
tests/          unittest; fixtures are built in temp dirs (os.utime for mtimes)
```

**Mode 1 rule:**
- The match key is case-insensitive basename + size + mtime, within `--mtime-tolerance` (default 2 s, which absorbs FAT/NTFS rounding).
- Flags: `--ignore-size`, `--ignore-mtime`.
- Implementation: SQL `GROUP BY lower(name), size HAVING count>1` produces candidate buckets. Python then chains the files in each bucket by mtime tolerance. This scales to millions of files without loading everything into memory.

**Grouping algorithm (`grouping.py`):**
1. For each duplicate group, emit the pairs of members that sit in different directories, keyed by a canonical `(dirA, dirB)`. Same-directory duplicates go in a separate section.
2. Within each directory pair, walk dirA's media files in natural sort order (`IMG_9` < `IMG_10`). Collapse consecutive matched files into runs, and record gaps and offsets.
3. Compute overlap per pair: % of dirA matched, % of dirB matched, and bytes. If both are 100%, the pair is labelled "identical folders".
4. **Tree roll-up:** when every subfolder of parent pair (P, Q) is identical to the same-named subfolder of the other, report a single "identical tree P ≡ Q" line instead of its children.
5. Sort the output by duplicated bytes, descending. Use `--top N` / `--min-files N` to limit noise.

Sample text output:
```
[1] 12.4 GB  /…/Storage 2A 8TB/Photos/2019/Hawaii  ↔  /…/Storage 3B 8TB/Backup/Hawaii   (identical folders, 812 files)
[2]  3.1 GB  /…/Storage 1A 4TB/DCIM/100CANON  ↔  /…/Storage 3A 8TB/Sorted/2016-07
       IMG_0100 … IMG_0450   351 files  (in B: same names)
       IMG_0502 … IMG_0519    18 files
       covers 64% of A, 100% of B
…
Same-folder duplicates: 213 groups (see CSV)
```

## Phases (stop after Phase 1 for user review)
**Phase 1: scaffold + mode 1 (this session)**
1. Set up git:
   - `git config user.name "Adam Casada"` and `user.email 40399292+abcasada@users.noreply.github.com` (repo-local).
   - Rename `master` → `main`.
2. Add `.claude/agents/` definitions with explicit model and effort (verify the frontmatter key name first via claude-code-guide):
   - `implementer`: opus, high
   - `reviewer`: opus, medium
   - `lookup`: sonnet, low
3. Commit sequence (small and focused; pushed at milestones):
   1. `chore: project scaffold` (pyproject, .gitignore, README stub, LICENSE?, agent defs)
   2. `feat(scan): parallel media inventory into SQLite`, with tests
   3. `feat(match): name/size/mtime duplicate matcher (mode 1)`, with tests
   4. `feat(group): collapse duplicates into folder-pair sequences`, with tests
   5. `feat(report): text/JSON/CSV reports`, plus the `cli` wiring
   6. `docs: README` (usage, research findings and tool comparison, roadmap)
4. Delegation:
   - I (Opus, high) write the shared data model, `index.py` schema and CLI skeleton.
   - Then two **implementer** subagents (Opus, high) work in parallel: one on `scanner.py` + `match_name.py`, the other on `grouping.py` + `report.py`, each with unittest coverage.
   - A **reviewer** subagent (Opus, medium) does a correctness review before the final push.
   - README tool-comparison research is already done, so no extra agent is needed.

**Phase 2: mode 2** (later)
- **rmlint exact:** feed paths from the index; one JSON per run is stored in `~/.cache/dpf/`.
- **Optional `ImageDataHash`** pass for groups that differ only in metadata.
- **czkawka_cli** `image`/`video` for visual matching. Includes an install helper/check, version pin and a parser test against a sample JSON.
- `--compare exact|visual|both`. Results flow into the same `grouping.py`.

**Phase 3: mode 3 + EXIF**
- Mode 3 runs mode 1, prints that report, then runs mode 2. Whether mode 2 checks only mode-1 candidates or does a full scan will be decided in Phase 2.
- `--date-source exif` uses exiftool `-stay_open` for capture dates.

## Speed (user priority for all modes; Ryzen 5 3600, 6 cores / 12 threads, 31 GB RAM)
Measured on 2026-10-24: a full mode 1 scan of all 5 drives found **464,247 media files
(25.9 TB) in 35.6 s**. Each drive gets its own walker thread, and drives run in parallel; the
slowest drive was Storage 1A, with 205k files in 35 s. Mode 1 is therefore not a bottleneck.
Spot benchmarks with 2–8 threads per HDD gave inconsistent gains of up to about 2×, so
in-device walker threads are deferred.

Mode 2 is I/O-bound: reading every byte would take around 12 h, set by the largest 8 TB HDD at
roughly 180 MB/s. The design rules:
1. **Read only possible duplicates.** For exact matching, rmlint hashes only files whose size
   matches another file, and it reads partial hashes before full ones.
2. **One sequential reader per HDD, all drives in parallel.** Concurrent reads on one
   spinning disk destroy throughput. The CPU threads are for hashing and decoding.
3. **Mode 3 can limit mode 2 to the mode 1 candidates**, as a fast confirmation pass.
4. **Use persistent caches** (czkawka's hash cache, rmlint JSON / `--replay`) so reruns are
   cheap.

## Safety constraints
- **Never write to the scanned drives.** The index, cache and reports all go under `~/.cache/dpf` or `--out`.
- No rmlint `--xattr` and no generated delete scripts.
- Follow symlinks: no. Skip hidden/system folders on NTFS (`$RECYCLE.BIN`, `System Volume Information`).

## Verification
- `python3 -m unittest discover -s tests`: covers the scanner, matcher tolerance, natural-sort runs with gaps, identical-folder and tree roll-up, and report snapshots.
- End-to-end on a synthetic tree in a temp dir: copy runs of files between folders, then check that the report shows the expected runs and percentages.
- Smoke test on real data, read-only: `python3 -m dpf find --mode 1 "/run/media/adam/Storage 1TB"` (the smallest drive). Check the runtime, the report readability, and that the drive's mtime/atime is untouched apart from normal reads. Then run it across all 5 drives and share the timing and summary.
- Push to `origin main` and confirm with `gh repo view`.

## Status after Phase 1 (2026-10-24)
Phase 1 is complete and was verified on the real drives (read-only):
- **A set** (1TB, 1A, 2A, 3A): 381,143 files, 18.1 TB, 32,403 duplicate groups (~2.6 TB
  redundant), 2,021 folder pairs and 135 tree pairs. Takes **27 s** end to end.
- **B set** (3B): 83,104 files, 7.8 TB, 11,342 groups (~1.4 TB redundant), 66 folder pairs.
  Takes **5 s**.

Additions beyond the original Phase 1 scope, all requested or approved by the user:
- A tree overview that rolls up near-identical trees.
- `--near` labelling for near-identical folder pairs.
- Per-folder tracking of non-media and unreadable files, so dpf never claims a "match"
  for content it didn't see.
- Ignoring OS clutter files (Thumbs.db, .DS_Store and similar).
- A guard that keeps the index and reports off the scanned drives.

**Known limitations / candidates for later:**
- **RAW+JPG sets split runs.** When only one extension of a pair matched (e.g. the .ARW
  but not the .JPG), the unmatched sibling breaks the run into many 1-file runs. Fix: let
  runs skip unmatched files whose name stem matches a matched neighbour.
- **Repeated pairs.** A file present in N folders yields up to N(N−1)/2 folder pairs, so
  the same run can appear 2–3 times. The cap is 16 folders per group, after which dirs
  pair only with the first.
- **Small trees in the overview.** Tiny, low-coverage tree pairs still take overview slots.
  Idea: `--tree-min-coverage`.
- **Not handled:** hardlinks (not merged), and concurrent runs on the same index (they
  share the staging table).
- **Custom agent types load only at session start.** In this session the implementer and
  reviewer ran as `general-purpose` agents on Opus following the `.claude/agents/*.md`
  rules, inheriting the session's effort level. From the next session on,
  `subagent_type: implementer|reviewer|lookup` applies the model and effort set in those
  files.

**Next: Phase 2 (mode 2).** Open decision: when mode 3 hands off to mode 2, should mode 2
only verify the mode 1 candidates (fast), or do a full content scan?
