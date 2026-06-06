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
s4converter/
    config.py       ← loads config.json, exposes constants
    cache.py        ← ProbeCache + FolderMarkers
    core.py         ← all scan/apply logic (UI-agnostic)
    cli.py          ← interactive terminal interface
    gui.py          ← PyQt6 GUI (phase tabs, inline editing)
```

**Dependency direction:** `config.py` ← `cache.py` ← `core.py` ← `cli.py` / `gui.py`. The core is fully decoupled from both UIs.

## The four tabs / phases

| Tab | Name | What it does |
|-----|------|-------------|
| 1 | Format | Converts non-WAV audio + wrong-SR/bit-depth WAVs → 48 kHz 16-bit WAV in one pass |
| 2 | Names | Prefix removal (scan_phase_2_all) + long filename cleanup (scan_phase_3) combined |
| 3 | File Size | Stereo → Mono (scan_phase_4) + Silence removal (scan_phase_5) combined |
| 4 | BPM | BPM detection via `aubio`, renames with `{bpm}_` prefix (high-confidence auto-selected) |

Internal phase numbers (used in `Finding.phase` and core functions) are still 1–6; only the GUI tabs are consolidated.

## Core patterns

**scan/apply split:** Every phase has `scan_phase_N()` → `List[Finding]` and `apply_phase_N(finding)`. Scan is always read-only. GUI and CLI call them the same way.

**`FindingsTable` display cap:** `_TABLE_DISPLAY_CAP = 5000`. Tables with more findings only render the first 5 000 rows to avoid OOM from `QTableWidgetItem` allocation. `get_selected_findings()` and `select_all()` handle findings beyond the cap via the `f.selected` flag directly. The count label shows "X findings (showing first 5 000 — all included in Apply)" when the cap is hit.

**`Finding` dataclass** (`core.py`): `phase`, `path`, `reason`, `current`, `target`, `suggested_name`, `extra` (phase-specific dict), `selected` (bool). The `extra` dict carries phase-specific data (e.g. `{"type": "non_wav"}` for phase 1, `{"classification": "dual_mono", "keep_channel": "L"}` for phase 4).

**Atomic writes:** All conversions write to `stem.__tmp__.wav` then `os.replace()` onto the target. `_cleanup_tmp()` removes the temp on failure.

**`ProbeCache`** (`cache.py`): Keyed by `"path|mtime|size"`. Stores ffprobe results in `.s4_cache.json` at the base dir. Phases 4, 5, and 6 also store their own analysis results directly in `cache._data` using prefixed keys (`"stereo|..."`, `"silence|..."`, `"bpm|..."`). `cache.save()` is always called at the end of a run. Cache I/O uses `encoding="utf-8", errors="replace"` to survive filenames with non-ASCII characters (©, é, etc.).

**Subprocess encoding:** All `subprocess.run` calls in `core.py` use `encoding="utf-8", errors="replace"` (never bare `text=True`) so that ffprobe/ffmpeg output containing non-ASCII filenames does not raise `UnicodeDecodeError`.

**Per-file "done" flags:** `ProbeCache` exposes `mark_phase_done(path, phase)` and `is_phase_done(path, phase)`. Keys are `done|N|path|mtime|size` — auto-invalidated if the file changes. `scan_phase_1` pre-filters candidates against this flag and marks passing files done. `ApplyWorker` marks each successfully converted file done via `core.get_apply_output_path(finding)`. This makes re-scans O(new files) after the first full scan+apply pass.

**`FolderMarkers`:** After a successful apply, a `.s4_processed` hidden file is touched in each folder. Incremental scans (`only_new=True`) skip folders where no file is newer than the marker. Any rename/conversion calls `FolderMarkers.invalidate(folder)` to force re-scan of that folder next time.

**Names tab (phase 2) — two-column prefix UI:** The `NamesTab` table has columns `["Type", "File/Folder", "Detail", "Detected Prefix", "Override (optional)"]` with `editable_col=5`. For Prefix rows: "Detected Prefix" (col 4) is read-only and shows the auto-detected prefix; "Override" (col 5) is empty by default — leaving it empty strips the detected prefix, typing a custom value strips that instead (folder re-read live). For Long Name rows: col 4 is empty, col 5 shows the suggested short name. `apply_phase_2` re-reads the folder when `override_prefix` is set to avoid stale `affected_files` mismatches.

**Parallelism:** `parallel_ffprobe()` uses `ThreadPoolExecutor` (default 4 workers) only for probing. All actual conversions are single-threaded and sequential.

**Phase 4 stereo classification thresholds** (all configurable via `config.json`):
- `dual_mono`: L-R diff ≤ `stereo_strict_threshold_db` (default -90 dB) — selected by default
- `one_side`: one channel > 40 dB louder — selected by default  
- `near_mono`: diff between -90 and -60 dB — shown only in loose mode, unselected by default
- `true_stereo`: diff > -60 dB — never flagged

**Phase 6 BPM skip logic:** Files whose stem already contains a BPM-like pattern (`\d{2,3}bpm` or surrounding separators) are skipped. Folders whose names match `bpm_skip_folder_hints` are also skipped — unless the folder name contains "loop", which overrides the hint (e.g. "Kick Drums" → skip, "Kick Loops" → detect). Hints cover one-shots, FX, field recordings, and individual drum hit types (kick, snare, hat, cymbal, etc.).
