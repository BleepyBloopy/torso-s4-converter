# Changelog

---

## [v7.2] – 2026-06-04

### Fixed
- **OOM crash during scan** — `QTextEdit` undo stack no longer accumulates one entry per file scanned (disabled via `setUndoRedoEnabled(False)`); folder tree now only re-renders on folder transitions, not every file; current filename moved to a cheap `QLabel`
- **OOM crash at scan completion** — `QTableWidget` population now uses `setUpdatesEnabled(False)` to suppress repaint cascades; display capped at 5 000 rows (all findings still included in Apply)
- **UTF-8 crash on filenames with special characters** — all `subprocess.run` calls in `core.py` and cache load/save in `cache.py` now use `encoding="utf-8", errors="replace"` so filenames containing `©`, `é`, etc. no longer crash ffprobe, ffmpeg, or cache I/O

### Added
- **Apply progress feedback** — progress bar now shows an accurate `X / Y files` count label and a `⟳ filename` line while Apply is running, matching the scan feedback style

---

## [v7.1] – 2026-06-04

### Added
- **Live scan progress panel** — visible during any scan; shows completed folders (✓ green), active folder (▶ white, full path), current file being scanned (⟳), and a dimmed Pending section for folders not yet started. Covers all 6 phases.
- **⏹ Stop button** — appears during scanning; gracefully stops after the current batch, saves the cache, and re-enables the UI without requiring the app to close
- **`setup.sh`** — first-time setup script for macOS/Linux; installs ffmpeg + uv via Homebrew, creates venv, installs Python deps with aubio CFLAGS workaround
- **`launch-s4converter-MacOS.command`** — double-click launcher for macOS; runs `setup.sh` automatically on first launch
- **`launch-s4converter-Windows.bat`** — double-click launcher for Windows; checks for ffmpeg/uv then sets up venv
- **`core.open_folder()`** — cross-platform file manager opener replacing macOS-only `open` subprocess calls (`xdg-open` on Linux, `explorer` on Windows)
- **Global busy lock** — all scan/apply buttons across all tabs are disabled while any operation is running; re-enabled with correct state on completion
- **Quit guard** — warns before closing if a scan or apply is in progress; explains files are safe but a `.__tmp__.wav` may remain
- **`scan_phase_2_all()`** — Phase 2 now auto-scans all subfolders like every other phase instead of requiring manual folder-by-folder selection via folder picker
- **`config.json` dual paths** — `s4_root` + `usb_root` replace the single `base_dir`; GUI drive dropdown presets are now sourced from config values
- **Memory safeguards for large drives** — `parallel_ffprobe` processes files in configurable chunks (default 500); Phases 4 and 5 skip files above `analysis_max_size_mb` (default 200 MB) to prevent OOM when scanning USB root directories
- **CLAUDE.md** — codebase documentation for Claude Code

### Changed
- GUI consolidated from 6 tabs to 4: Prefixes + Long Names → **Names** tab; Stereo→Mono + Silence → **File Size** tab. Internal phase numbers (1–6) unchanged in core logic.
- `parallel_ffprobe` saves the probe cache to disk after every chunk (default every 500 files) — crash-safe resume: restarting re-scans using cache hits so at most one chunk is re-probed
- GUI prevents Mac idle sleep via `caffeinate -i` during any scan or apply (macOS only); terminated cleanly on completion or app close. Note: `caffeinate` prevents idle sleep but does not prevent display loss when switching monitor inputs on a shared monitor setup — use the CLI for unattended scans in that scenario, or use a headless dummy plug
- Phase 6 (BPM detection): high-confidence results (≥ 0.75) are now **checked by default** following S-4 OS v2.2 confirmation that BPM-in-filename is required for proper DISC sync-mode loading
- `PARALLEL_FFPROBE_WORKERS` reduced from 4 → 2 (configurable via `ffprobe_workers` in `config.json`)
- Phase 2 tab label and help text updated to reflect auto-scan behaviour

### Fixed
- Drive dropdown presets were hardcoded; now read from `config.USB_ROOT` and `config.S4_ROOT`
- Phase 6 BPM skip logic: folder hints now only skip when the folder name does **not** contain "loop" — e.g. "Kick Drums" is skipped but "Kick Loops" is detected. Expanded hints to include common drum hit terms: kick, snare, hat, hihat, cymbal, clap, tom, ride, crash, rimshot, 808, perc, shaker, tamb, conga, bongo
- Drive dropdown now snaps to "Custom…" when a manually typed path doesn't match any preset
- Cache always lives at the configured drive root (`usb_root` or `s4_root`) via `config.cache_root_for()` — scanning any subfolder (e.g. `Download Samples/drums/`) reuses the same probe data as a full-library scan; status bar and log show the cache file path

---

## [v7.0] – 2026-05-27

Complete rewrite of the original single-file script into a structured Python package.

### Added
- `s4converter/` package with separated modules: `core`, `cache`, `cli`, `gui`, `config`
- **Persistent ffprobe cache** (`ProbeCache`) — skips unchanged files on re-scans (~150× faster)
- **Per-folder markers** (`FolderMarkers`) — skips entire folders that haven't changed
- **Parallel ffprobe workers** — probes multiple files simultaneously on first scan
- **PyQt6 GUI** — tab-per-phase interface with background worker threads, inline editing, progress bars
- **CLI** with `--dry-run`, `--quick`, `--phases`, `--full-scan` flags
- **Phase 6: Stereo → Mono detection** — classifies files as `dual_mono`, `one_side`, `near_mono`, or `true_stereo` using peak dB math; saves ~50 % per converted file
- Drive preset dropdown in GUI (USB / S-4 Root / Custom)
- Per-phase help panels with thresholds and workflow tips
- `config.json` at repo root for user-editable settings (no Python required)
- `requirements.txt`

### Changed
- `NAME_LENGTH_LIMIT` raised from 50 → 70 characters
- Config split: `config.json` (user edits) + `config.py` (loader + internal constants)

---

## [v6.0] – 2025-12-31

Original single-file script (`smart_converter_v6.py`).

### Added
- 5 interactive CLI phases run sequentially with user prompts
- **Phase 1** – Non-WAV conversion (MP3, AIFF, FLAC → 48 kHz WAV; auto bit depth)
- **Phase 2** – Sample rate compliance (resample to 48 kHz, preserve bit depth)
- **Phase 3** – Bit depth optimisation (24-bit files > 10 s → 16-bit)
- **Phase 4** – Shared prefix removal (folder-targeted, auto-detect + manual fallback)
- **Phase 5** – Long filename cleanup (stems > 50 chars, suggest + manual rename)
- Smart history: remembers last run time, offers incremental scan on launch
- `NAME_LENGTH_LIMIT = 50`

---

<!-- TODO: backfill v1–v5 history from Gemini chat -->
