# Original request & decisions (session of 2026-10-24)

This file preserves the user's original requirements and the answers given during
planning, so later sessions don't lose them. See [PLAN.md](PLAN.md) for the design.

## Original requirements (paraphrased closely)

**The product**
- A CLI program that runs on Linux.
- It scans many drives and many TB of photos **and videos** and finds duplicates.

**Modes**
- **Mode 1:** name/date only.
- **Mode 2:** full image comparison.
- **Mode 3:** run mode 1, then automatically re-run in mode 2.

**Reporting**
- Do **not** list every individual file.
- Group findings into **sequences**. When a set of many files was duplicated across
  folders, report it as one group (e.g. a run of IMG_0100–IMG_0450 copied from
  folder A to folder B).

**Reuse, don't reinvent**
- The user knows many programs already do parts of this. Call existing tools like
  **rmlint** where possible.
- Investigate **exiftool**. Reading EXIF data may be useful for a more advanced mode.
- Research what other tools people have built for scanning large photo archives for
  duplicates. It's possible an existing tool on its own is enough.
  - Outcome: no single tool does it all. See PLAN.md "Research findings".

**Phasing**
- If we build something, plan in phases, and **stop after completing mode 1**
  (Phase 1) for review.

**How to work**
- Work like an experienced developer.
- **Push everything to GitHub** (repo: github.com/abcasada/duplicate-photos-finder).
- Make commits the way an experienced dev would: small, focused, conventional
  messages.
- This isn't a super advanced project, so work somewhat quickly.
- Aim for a high-quality product while spending only the time and Claude usage that's
  needed.

**Subagents**
- Make good use of subagents whenever it's beneficial.
- **Explicitly assign a model and an effort level** to each one.
- Lean mostly toward **Opus** at appropriate effort levels, unless a lower model fits
  better than lowering the effort.
- The user may ask which model and effort each agent is using, so state it.

## Decisions made during planning (user answers)

| Question | Answer |
|---|---|
| What counts as a duplicate in mode 2? | **Offer all three options**: exact + visually similar, exact only, or visually similar only (`--compare exact\|visual\|both`). |
| What should the tool do with duplicates? | **Report only.** It never touches files and generates no delete scripts. |
| Git author | **Adam Casada**, `40399292+abcasada@users.noreply.github.com` (set repo-locally). |
| Plan approval | The user said to write the plan into the repo, commit it, and resume implementation in a later session. |

## Environment facts gathered (2026-10-24)

**Machine**
- Ubuntu 26.04, Python 3.14.4, SQLite 3.46.1.
- **No pip or ensurepip**, so runtime code is stdlib-only and tests use `unittest`.
- Already installed: rmlint 2.10.2, exiftool 13.50, ffmpeg, gh (logged in as
  `abcasada`).
- Not installed: czkawka, fclones, jdupes.
- Hardware: 12 cores, 31 GB RAM. The root disk (ext4) has about 78 GB free.

**Data drives:** 5 NTFS (ntfs3) drives, about 30 TB total, mounted under
`/run/media/adam/`.

| Drive | Free space |
|---|---|
| Storage 1TB (SSD) | 531 GB |
| Storage 1A 4TB | 128 GB |
| Storage 2A 8TB | 56 GB |
| Storage 3A 8TB | 337 GB |
| Storage 3B 8TB | **25 MB** |

Drives are nearly full and must **never be written to**. All caches, indexes and
reports go under `~/.cache/dpf` or a user-chosen `--out`.

## Next step when resuming

Start **Phase 1** in PLAN.md:
1. Scaffold the project and add `.claude/agents` definitions with model and effort.
2. Build the scanner and SQLite index.
3. Build the mode 1 matcher.
4. Build sequence/folder grouping.
5. Build reports and README.

Then stop for review.
