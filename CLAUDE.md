# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Python tool to standardize and organize audio sample libraries for the **Torso S-4** hardware sampler. Target format is always **48 kHz, 16-bit WAV**. Operates in-place on an external USB drive; the Mac source folder is never modified.

## Commands

```bash
# First-time setup (installs ffmpeg, uv, venv, and all Python deps) — macOS
./setup.sh

# Launch (macOS: double-click launch-s4converter-MacOS.command, or from terminal:)
uv run python -m s4converter.gui

# Run CLI (all phases, interactive)
uv run python -m s4converter.cli --path /Volumes/S-4/SAMPLES

# Common CLI flags
uv run python -m s4converter.cli --quick          # Phase 1 only, no prompts
uv run python -m s4converter.cli --dry-run        # Preview only, no changes
uv run python -m s4converter.cli --phases 1,4     # Run specific phases only
uv run python -m s4converter.cli --full-scan      # Ignore folder markers, re-scan everything
uv run python -m s4converter.cli --report         # Export CSV + Markdown library report
```

No test suite exists. No build step.

## Architecture

```
config.json         ← user-editable thresholds (edit this, not config.py)
.s4_sync.json       ← sync tracker DB (auto-created at project root, gitignored)
s4converter/
    config.py       ← loads config.json, exposes constants (incl. SYNC_PAIRS, SYNC_DB_PATH)
    cache.py        ← ProbeCache + FolderMarkers
    core.py         ← all scan/apply logic (UI-agnostic)
    sync.py         ← SyncTracker + Mac→USB sync logic (Tab 1, independent of core)
    cli.py          ← interactive terminal interface
    gui.py          ← PyQt6 GUI (phase tabs, inline editing)
```

**Dependency direction:** `config.py` ← `cache.py` ← `core.py` ← `cli.py` / `gui.py`. `sync.py` depends only on `config.py`. The core is fully decoupled from both UIs.

## The seven tabs

| Tab | Name | What it does |
|-----|------|-------------|
| 1 | Sync | Copies new/changed Mac source files to USB; move detection; never auto-deletes from USB |
| 2 | Wav Format | Converts non-WAV audio (incl. MP4) + wrong-SR/bit-depth WAVs → 48 kHz 16-bit WAV in one pass |
| 3 | Silence Remover | Trims leading/trailing silence (scan_phase_5 / apply_phase_5) |
| 4 | File Cleanup | Folder collapse (scan_phase_8) + junk file deletion (scan_junk_files) combined |
| 5 | Name Cleanup | BPM relabel (scan_bpm_relabel) + non-ASCII romanization (scan_phase_7) + long prefix/name (scan_long_prefix) combined |
| 6 | Fake Stereo to Mono | Converts fake-stereo to mono (scan_phase_4 / apply_phase_4); has Loose mode checkbox |
| 7 | BPM Detection | BPM detection via `aubio`, renames with `{bpm}bpm_` prefix (scan_phase_6 / apply_phase_6) |

Tab 1 (Sync) is always enabled even before loading a drive. Tabs 2–7 require a drive to be loaded. Internal phase numbers (used in `Finding.phase` and core functions) are 1–10:
- Phases 2, 3, 7 → Name Cleanup tab (prefix, long name, non-ASCII)
- Phase 4 → Fake Stereo to Mono tab
- Phase 5 → Silence Remover tab
- Phase 6 → BPM Detection tab (also used by scan_bpm_relabel Finding objects)
- Phase 8 → File Cleanup tab (folder collapse)
- Phase 9 → File Cleanup tab (junk file deletion)
- Phase 10 → marker-only ID for scan_bpm_relabel (prevents interference with phase 6 markers)

## Core patterns

**scan/apply split:** Every phase has `scan_phase_N()` → `List[Finding]` and `apply_phase_N(finding)`. Scan is always read-only. GUI and CLI call them the same way.

**`FindingsTable` pagination:** `_PAGE_SIZE = 1000` (class constant). `_rebuild_display()` renders only the current page slice (`page_findings()`). A pagination bar (`make_pagination_bar()`) sits below the table and is hidden when all findings fit on one page. Key methods:
- `go_to_page(n)`: calls `_sync_page_to_flags()` to flush checkbox + editable-col state to finding objects before switching, then rebuilds and updates the bar.
- `select_page(checked)`: checks/unchecks only the current page; does not touch other pages' `.selected` flags.
- `select_all(checked)`: updates every finding's `.selected` flag, then reflects state in current page checkboxes.
- `get_selected_findings()`: calls `_sync_page_to_flags()` first so the current page is flushed, then returns `[f for f in self.findings if f.selected]`.
- `_edits: dict[int, str]`: caches editable-col values keyed by `id(finding)` so inline edits on page N survive navigation to page M. `get_col_value()` checks the current page's live table rows first, then falls back to `_edits`.

**`FindingsTable` multi-select:** `ExtendedSelection` mode is set explicitly. Clicking a checkbox in a multi-row selection propagates the same state to all selected rows via `_on_item_changed`. Space key toggles all highlighted rows. `_rebuilding` flag prevents `itemChanged` firing during `_rebuild_display`; `_propagating` flag prevents cascading when bulk-setting checkboxes (used in `select_all`, `keyPressEvent`, and `_on_item_changed`).

**`Finding` dataclass** (`core.py`): `phase`, `path`, `reason`, `current`, `target`, `suggested_name`, `extra` (phase-specific dict), `selected` (bool). The `extra` dict carries phase-specific data (e.g. `{"type": "non_wav"}` for phase 1, `{"classification": "dual_mono", "keep_channel": "L"}` for phase 4).

**Atomic writes:** All conversions write to `stem.__tmp__.wav` then `os.replace()` onto the target. `_cleanup_tmp()` removes the temp on failure.

**`ProbeCache`** (`cache.py`): Keyed by `"path|mtime|size"`. Stores ffprobe results in `.s4_cache.json` at the base dir. Phases 4, 5, and 6 also store their own analysis results directly in `cache._data` using prefixed keys (`"stereo|..."`, `"silence|..."`, `"bpm|..."`). `cache.save()` is always called at the end of a run. Cache I/O uses `encoding="utf-8", errors="replace"` to survive filenames with non-ASCII characters (©, é, etc.).

`ProbeCache` also stores user-reviewed BPM Relabel suppressions: `mark_bpm_relabel_reviewed(path)` writes `"bpm_relabel_skip|{path}" = True`; `is_bpm_relabel_reviewed(path)` reads it. Used by `scan_bpm_relabel` to permanently skip files the user has dismissed via the "Not BPM" button.

**Subprocess encoding:** All `subprocess.run` calls in `core.py` use `encoding="utf-8", errors="replace"` (never bare `text=True`) so that ffprobe/ffmpeg output containing non-ASCII filenames does not raise `UnicodeDecodeError`.

**`FolderMarkers`** (`cache.py`): Per-phase hidden marker files `.s4_phase{N}` in each folder. `ALL_MARKER_PHASES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10})`. Each scan phase reads and writes only its own marker — no cross-phase interference.

- `is_folder_clean(folder, phase, exts)`: checks the phase marker, with O(1) fast path on `folder.stat().st_mtime`. Slow path (directory changed) checks both files and direct subdirectory mtimes — subdirs must be checked too because a newly synced subfolder won't change the parent's file listing but will change the parent's mtime. Phase 1 falls back to legacy `.s4_processed` for migration.
- `mark_folder(folder, phase)`: touches `.s4_phase{N}` with current timestamp (explicit `os.utime` to override FAT32 rounding).
- `invalidate(folder)`: removes all `.s4_phase{N}` files plus the legacy `.s4_processed`.

`iter_files` passes `phase` to `is_folder_clean` and calls `dirs.clear()` to stop descent into clean subtrees. `_propagate_ancestor_markers(base_dir, seed_folders, phase)` propagates markers up the ancestor chain after an all-clean pass, so the second scan of a large drive is near-instant at the top level.

**`_invalidate_ancestors`** (`sync.py`): Called after every `apply_copy` that writes a file to USB. Walks from the file's parent directory up to the sync pair's USB root and calls `FolderMarkers.invalidate()` at each level. Ensures the next fast scan descends through newly populated directories instead of being blocked by stale ancestor markers.

**Parallelism:** `parallel_ffprobe()` uses `ThreadPoolExecutor` (default 2 workers) only for probing. All actual conversions are single-threaded and sequential.

**Phase 4 stereo classification thresholds** (all configurable via `config.json`):
- `dual_mono`: L-R diff ≤ `stereo_strict_threshold_db` (default -90 dB) — selected by default
- `one_side`: one channel > 40 dB louder — selected by default
- `near_mono`: diff between -90 and -60 dB — shown only in loose mode, unselected by default
- `true_stereo`: diff > -60 dB — never flagged

**`scan_bpm_relabel`** (`core.py`): Finds WAV files with a bare 2–3 digit number in the stem (BPM range 60–220) not followed by `bpm`. Runs in the Name Cleanup tab (before non-ASCII and long prefix passes). Accepts `cache: Optional[ProbeCache]` to skip files marked as reviewed. False-positive filters (applied in order):
- `is_bpm_relabel_reviewed(path)` — user-reviewed via "Not BPM" button
- `_BPM_LABELED_RE` — already has `bpm` label
- Leading-zero check: `m.group(1)[0] == '0'` → number is zero-padded (e.g. `067`) → add folder to `leading_zero_folders` and skip. Post-loop: all findings from any folder in `leading_zero_folders` are dropped, so `101`, `120`, etc. in the same folder are also dismissed.
- Apostrophe check: number immediately followed by `'` → skip (e.g. `90's`)
- Decimal check: number immediately followed by `.digit` → skip (e.g. GPS coords `72.87109`)
- Sequential folder filter (post-loop, applied after leading-zero filter): groups remaining findings by folder; if all numbers form a perfect consecutive sequence (step=1, ≥4 files), drops the entire folder and marks it clean for phase 10

Uses marker phase 10 (distinct from phase 6) so BPM Detection and BPM Relabel don't overwrite each other's folder markers.

**Phase 6 BPM detection skip logic:** Files whose stem already contains a BPM-like pattern (`\d{2,3}bpm` or surrounding separators) are skipped. Folders whose names match `bpm_skip_folder_hints` are also skipped — unless the folder name contains "loop", which overrides the hint (e.g. "Kick Drums" → skip, "Kick Loops" → detect). Hints cover one-shots, FX, field recordings, and individual drum hit types (kick, snare, hat, cymbal, etc.).

**`SyncTracker`** (`sync.py`): Persists in `.s4_sync.json` at the project root (not on any drive). Key format: `"{pair_label}|{rel_path}"` → `{mtime, size, synced_at}`. Tracker is **source-centric** — records source file identity, not USB file location. USB-side reorganisation (moving files into subfolders on USB) is transparent: if the source file hasn't changed (same mtime + size), it's always skipped regardless of where its USB copy ended up.

**`SyncFinding`** (`sync.py`): `status` (`new`/`updated`/`moved`/`deleted`), `pair_label`, `rel_path` (source-relative, old path for `moved`), `source_path`, `usb_path`, `size`. For `moved` status: `moved_to_rel_path`, `moved_to_source_path`, `moved_to_usb_path`. `deleted` findings default `selected=False`.

**Move detection** (`sync._detect_moves`): After scanning, cross-references `new` vs `deleted` findings by `(filename_lower, size)`. Unique match → merged into a `moved` finding. Ambiguous (same name+size in multiple places) → left as `deleted` + `new`. Apply action for `moved`: duplicate the old USB file (preserving app conversions) to the new USB path; update tracker; prompt user to delete old USB copy.

**Bootstrap / Register Existing** (`sync.bootstrap_all`): One-time setup. Scans both source and USB per pair; builds a USB stem index (`{usb_folder: {lowercase_stems}}`); registers source files whose stem exists on USB. Files missing from USB are left unregistered → appear as NEW. Uses stem matching (not full filename) to handle format conversions (`.aiff` → `.wav`).

**`SyncTab`** (`gui.py`): Tab 1, always enabled. Uses `main_window.sync_tracker` (a `SyncTracker` instance created at `MainWindow.__init__`). Workers: `SyncCopyWorker` (copy/move/delete), `BootstrapWorker` (register existing). Calls `main_window.set_busy(running)` so caffeinate and global tab locking work during sync operations. `⚡ Sync + Convert` chains scan → copy → `Phase1Tab.start_scan()` automatically.

**`NameCleanupTab`** (`gui.py`): Tab 5. Runs three scans in sequence: `scan_bpm_relabel` (phase 10 marker) → `scan_phase_7` (phase 7 marker) → `scan_long_prefix` (phases 2+3 marker). Has a "Not BPM" button (inserted into the toolbar after "Select None") that marks highlighted BPM Relabel rows as reviewed in ProbeCache and removes them from the table immediately. Button is enabled only when BPM Relabel findings are present.
