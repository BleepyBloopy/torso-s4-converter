# Changelog

---

## [v8.7] – 2026-07-22

### Fixed
- **BPM Relabel — sparsely-sampled MIDI chromatic packs flagged** — every-3rd-note packs (e.g. Samples From Mars C/E/G-per-octave sets, note numbers 63/66/70…) have BPM-range MIDI note numbers at only ~27% density — too low for the density filter. Added `_MUSIC_NOTE_TAIL_RE`: if a stem ends with a musical note name preceded by whitespace (e.g. `" E3"`, `" G#4"`, `" C-2"`, `" G6_0001"` for Kontakt round-robin), the file is classified as a MIDI chromatic sample and skipped entirely, regardless of density.
- **Long Prefix — detected prefix strips BPM from filenames** — `detect_common_prefix` could return a prefix containing BPM info (e.g. `"SP_TSVI_136bpm_kit_boca_trance_"`). Stripping that prefix deletes the BPM from every file. The prefix is now truncated to stop just before the first BPM match (e.g. → `"SP_TSVI_"`), so files become `"136bpm_kit_boca_trance_kick.wav"` after stripping. If nothing useful remains before the BPM (shorter than `MIN_PREFIX_LENGTH`), the folder is skipped.
- **Non-ASCII scan — parent folders incorrectly marked clean, hiding non-ASCII children** — `scan_phase_7` marked a folder clean if all its direct files were ASCII, even when a child subfolder still had non-ASCII findings. On the next fast scan the parent's clean marker triggered `dirs.clear()`, silently hiding the child. The marking logic now walks up the ancestor chain of every finding folder and excludes all ancestors from clean-marking, so they are always re-evaluated on fast scan.

---

## [v8.6] – 2026-07-22

### Fixed
- **BPM Relabel — MIDI note packs flagged due to size-filter gap interaction** — MIDI chromatic sample packs (e.g. Samples From Mars note numbers 60–84) contain notes of varying sizes; the 400 KB size filter removed ~10 files, leaving 14 findings with a density of 14/25 = 0.58 — just below the 0.70 threshold — so all remaining files were incorrectly flagged. A pre-pass now evaluates the full folder (all files, ignoring the size filter) before the main loop runs, so the true density (24/25 = 0.96) is used to dismiss the pack.
- **Long Prefix apply misses files renamed by BPM Relabel** — `apply_phase_2` was using the file list frozen at scan time (`affected_files`). After BPM Relabel renamed files in the same session, those stale paths no longer existed and the prefix strip silently skipped them. The apply now always re-reads the folder live, so BPM-renamed files are included.
- **Name Cleanup Path column too wide / play button column wrong width after scan** — `resizeColumnToContents` was called for all columns including Fixed-mode button columns (Open Folder, Play), overriding their programmatic widths with the empty-string cell content. Fixed-mode columns are now skipped in the auto-resize loop.

---

## [v8.5] – 2026-07-22

### Added
- **Audio preview in Name Cleanup tab** — a ▶ play button column (col 2, right of Open Folder) lets you listen to any file before deciding to rename it. Clicking ▶ starts playback; clicking again (⏹) stops it. Switching to a different row auto-stops the previous file. A single `QMediaPlayer` owned by the main window handles playback across all tabs and is stopped cleanly on app close.
- **File size column in Name Cleanup tab** — shows KB / MB for each finding. Long Prefix rows (folders) show "—".
- **Path column in Name Cleanup tab** — shows the parent folder path after the Open Folder button, so the folder is visible without opening Finder.
- **Detail and Edit columns merged** — the former "Detail" column (showing `→ target`) and "Edit" column (editable target) are now a single "Detail / Edit" column. For Long Prefix rows the file count moves to the "File / Folder" cell.

---

## [v8.4] – 2026-07-22

### Fixed
- **BPM Relabel — dense-but-gapped numbered packs not dismissed** — the sequential filter required a perfect consecutive sequence (no gaps), so a pack like "VVE2 Kit Claudio 60–100" where file 64 happened to be absent (40 files, range 41) failed the check and all 40 files were incorrectly flagged. The filter now also catches *dense* groups: ≥8 files covering ≥70% of their number range are treated as pack indices. A single missing track no longer defeats the filter.

---

## [v8.3] – 2026-07-22

### Added
- **BPM Relabel — one-shot size filter** — files below `bpm_relabel_max_oneshot_kb` (default 400 KB) are silently skipped by `scan_bpm_relabel`. At 48 kHz / 16-bit this covers anything shorter than roughly 2 seconds stereo or 4 seconds mono — safely below the shortest realistic 1-bar loop. Configurable in `config.json`.

### Fixed
- **BPM Relabel — multi-type flat packs not dismissed** — the sequential number filter previously pooled all findings per folder, so packs like Vengeance Freakz On Beatz (9 instrument types — hihat, kick, clap, etc. — all numbered 1–96 in the same directory) produced 333 pooled numbers with duplicates, breaking the consecutive check. The filter now groups by `(folder, stem-prefix-before-the-number)` so `"VFOB2 CL Hihat "` and `"VFOB2 Kick "` are evaluated independently; each prefix group's numbers 60–96 are correctly identified as sequential and dismissed.

---

## [v8.2] – 2026-07-22

### Added
- **Auto-remove succeeded findings after Apply** — when an apply run finishes (or is stopped mid-way), all successfully processed findings are removed from the table automatically. Only failed findings remain, so the user immediately sees what still needs attention without any manual cleanup or re-scan.
  - Works on full apply and on early-stopped applies — whatever finished successfully is pruned.
  - Count label and Apply button update to reflect the remaining failures.
  - `NameCleanupTab` also updates the Not BPM button state after pruning removes BPM Relabel rows.

---

## [v8.1] – 2026-07-22

### Added
- **Pagination in all findings tables** — replaces the old 5,000-row hard cap (which silently hid findings beyond the limit) with full pagination at 1,000 rows per page. A bar below the table appears when there is more than one page, showing `← Prev`, page indicator (`Page 2 of 8  (1,001–2,000 of 7,432)`), `Next →`, and a **Select Page** button.
  - **Select Page** checks only the current page's rows; other pages' selections are untouched — useful for bulk-reviewing one section of Name Cleanup at a time.
  - **Select All / Select None** (existing toolbar buttons) still operate across all pages.
  - Navigating between pages saves checkbox state and inline-edited values so nothing is lost when switching pages.
  - Edits made to the "Edit" column (e.g. prefix replacement in Name Cleanup) survive page navigation and are included in Apply regardless of which page they were made on.
  - Bar is hidden when all findings fit on one page (≤ 1,000), keeping the UI clean for small result sets.
- **BPM Relabel — zero-padded number filter** — numbers whose digit string starts with `0` (e.g. `067`, `082`) are treated as zero-padded sample indices and skipped. If any file in a folder has a zero-padded number, all other BPM-looking numbers in that same folder are also dismissed (e.g. `VEH1 Percussive FX - 067` through `VEH1 Percussive FX - 152` are all treated as pack indices, not BPMs).

---

## [v8.0] – 2026-07-22

### Added
- **Per-phase folder markers** (major architecture change) — replaced the single `.s4_processed` shared marker with per-phase markers `.s4_phase1`–`.s4_phase10`. Each scan phase reads and writes only its own marker; phases no longer interfere. Example: Wav Format completing no longer blocks Name Cleanup from seeing the same folder on a fast scan.
  - `ALL_MARKER_PHASES = {1…10}` — phase 10 is the marker ID for `scan_bpm_relabel`, keeping it separate from phase 6 (BPM Detection) even though both produce `Finding.phase = 6`
  - Migration: phase 1 falls back to legacy `.s4_processed` so existing USB drives don't need a forced full rescan
  - `is_folder_clean` slow path now also checks direct subdirectory mtimes — previously only files were checked, causing newly synced subfolders to be missed when the parent marker was still fresh
  - `invalidate(folder)` removes all phase markers and the legacy `.s4_processed`
- **BPM Relabel — sequential number filter** — after collecting candidates, `scan_bpm_relabel` groups them by folder. Any folder where all flagged numbers form a perfect consecutive integer sequence (e.g. 61, 62 … 81) with ≥ 4 files is silently dropped and its folder marked clean. Those are sample-pack track indices, not BPMs.
- **BPM Relabel — apostrophe filter** — numbers immediately followed by `'` (e.g. `90's`) are skipped; these are decade/possessive references.
- **BPM Relabel — decimal/float filter** — numbers immediately followed by `.digit` (e.g. `135.7744` in GPS coordinates) are skipped.
- **"Not BPM" button** in Name Cleanup tab — highlight any BPM Relabel rows and click "Not BPM" to permanently suppress those files from future BPM Relabel scans. Stored in ProbeCache as `bpm_relabel_skip|{path}`. Files are still scanned by Long Prefix and Non-ASCII passes.
- **Multi-select checkbox toggle** in all findings tables — `ExtendedSelection` mode is now explicit; clicking one checkbox in a multi-row selection propagates the same state to all selected rows instantly. Space key also toggles all highlighted rows at once.
- **Sync — ancestor marker invalidation** (`_invalidate_ancestors` in `sync.py`) — when Sync copies a file to a USB path, phase markers are removed from every ancestor directory up to the pair root so the next fast scan descends into the newly synced subtree instead of being blocked by a stale parent marker.

### Changed
- **Removed per-file "done" flags** — `mark_phase_done` / `is_phase_done` removed from `ProbeCache`. Folder markers (fast-scan layer) and the per-file audio analysis cache (ffprobe cache) together cover the same ground; the done-flag layer was redundant.
- **Column resize** — all findings table columns are now fully interactive (draggable). The "Path" column was incorrectly locked as `Stretch`, preventing resize and causing inverted drag behavior in adjacent columns.

### Fixed
- **Sync+Convert tooltip stuck as "Click Load to load the drive first"** even after the drive was loaded — the normal description was only set in the error branch.

---

## [v7.11] – 2026-07-18 – 2026-07-21

### Added
- **File Cleanup tab** (Tab 4) — new dedicated tab for Folder Collapse (`scan_phase_8`) and Junk File Deletion (`scan_junk_files`). Previously folder collapse lived in the Name tab. Both operations now have their own "Open Folder" button column.
- **Name Cleanup tab** (Tab 5) — successor to the old Name tab. Combines BPM Relabel (`scan_bpm_relabel`, runs first), Non-ASCII romanization (`scan_phase_7`), and Long Prefix removal (`scan_long_prefix` — covers both prefix detection and long-name shortening). Running BPM relabel first means BPM renames are in place before prefix evaluation.
- **MP4 support** — `scan_phase_1` detects audio-bearing `.mp4` files and converts them to 48 kHz / 16-bit WAV. Remaining (video-only or empty) `.mp4` files are flagged in File Cleanup for deletion.
- **Skip Selected button** — all processing tabs gain a "Skip Selected" button next to Apply. Checked rows are removed from the table without applying any changes — useful for dismissing findings you've reviewed but don't want to act on yet.
- **Column sort** — clicking any column header sorts the findings table; a second click reverses; a third click resets to scan order. Sort arrow shown in header. Size column sorts numerically.
- **Cascade cleanup progress** — applying File Cleanup (folder collapse) shows the current file path in the status label during cascaded multi-level collapses.
- **Long Prefix detection in Name Cleanup** — `scan_long_prefix` added to the Name Cleanup scan, combining the old separate prefix scan and long-filename scan into one integrated pass.

### Changed
- **7-tab layout** — new order: 1. Sync → 2. Wav Format → 3. Silence Remover → 4. File Cleanup → 5. Name Cleanup → 6. Fake Stereo to Mono → 7. BPM Detection
- **Unified column format** — all processing tabs now share the same `["Path", "File", …]` column layout with a consistent row-number column and Open Folder button where applicable.
- **S4_DISPLAY_LIMIT** default lowered from 49 → 45 characters to match observed S-4 truncation.

### Fixed
- `scan_junk_files` now respects the fast-scan (only_new) flag and folder markers instead of always doing a full walk.
- `apply_phase_8` failure when the single-child folder contained only hidden files (e.g. `.DS_Store`) — now uses `shutil.rmtree` instead of `rmdir` to clean up.
- Failed Apply items now logged by full path rather than just filename, making them easier to locate.
- `scan_bpm_relabel` now detects BPM numbers anywhere in the stem (not just at the start) and correctly collapses spaced-BPM format (`138 BPM` → `138bpm`).
- Name Cleanup: BPM-labeled filenames are now protected from prefix stripping (a `120bpm_` prefix is not an "album prefix"). Non-ASCII letter filter corrected. Fast-scan marker no longer blocks Name Cleanup from re-visiting folders that Wav Format just processed.

---

## [v7.10] – 2026-07-09

### Added
- **Sync tab — Configure Pairs dialog** — pair source/USB paths now visible directly in the tab; a "Configure Pairs…" button opens an inline editor to add, remove, and reorder sync pairs without editing `config.json` manually.

### Changed
- **Folder marker performance** — `is_folder_clean` now uses the directory's own `mtime` as a fast-path O(1) check; per-file stat walk is only triggered when the directory has changed. Marker propagation walks up the ancestor chain after a clean all-folders pass, so the second scan of a large drive is near-instant.

### Fixed
- Sync tab action buttons (Scan, Copy, Sync+Convert) now correctly require a USB drive to be loaded before enabling — previously they could fire without a valid USB path.
- Sync scan progress now reports across all pairs in aggregate rather than resetting per pair.
- USB mount check loosened to the drive root rather than the exact configured subfolder — prevents false "not mounted" errors when scanning a subdirectory.
- Folder marker propagation now correctly reaches top-level pair folders.

---

## [v7.9] – 2026-07-09

### Added
- **Folder Collapse in Tab 3 (Name)** — new `scan_phase_8` finds folders that contain exactly one subfolder and nothing else. Suggests collapsing: move the child's contents up one level and delete the empty intermediate folder (e.g. `Drums/Kicks/kick.wav` → `Drums/kick.wav`). Findings are sorted deepest-first so nested collapse chains (`A/B/C/`) are fully flattened in one Apply pass without re-scanning.

---

## [v7.8] – 2026-07-09

### Changed
- **Tab reorder** — Name tab moved to position 3 (right after Silence Remover); BPM moved to position 5 (last). New order: Sync → Wav Format → Silence Remover → Name → Fake Stereo to Mono → BPM.
- **"Stereo to Mono" → "Fake Stereo to Mono"** — more accurately describes what the tab does (only flags files where channels are identical or heavily imbalanced, never true stereo).
- **BPM output format** — phase 6 now renames files to `{bpm}bpm_` prefix (e.g. `120bpm_kick.wav`) instead of the previous `120_` format, so the S-4 can register the value as the default BPM.

### Added
- **BPM relabel scan** — new `scan_bpm_relabel` in `core.py` runs alongside phase 6 in the BPM tab. Finds WAV files whose stem already starts with a bare 2–3 digit number (e.g. `120_kick.wav`) but is missing the `bpm` label the S-4 needs, and suggests renaming them (→ `120bpm_kick.wav`). Results appear at the top of the BPM tab table, auto-selected, with Confidence shown as `—` (no audio analysis needed).

---

## [v7.7] – 2026-07-09

### Added
- **Non-ASCII romanization in Tab 5 (Name)** — third scan pass in the Name tab that finds filenames containing non-English characters and suggests ASCII transliterations the S-4 can read.
  - Chinese (CJK Unified Ideographs) → pinyin without tone marks via `pypinyin` (e.g. `踢鼓_Loop` → `tigu_Loop`; syllables joined without spaces).
  - Accented Latin, Cyrillic, hiragana, katakana, hangul, and all other non-ASCII → best-effort ASCII via `unidecode` (e.g. `Café_Beat` → `Cafe_Beat`).
  - Mixed filenames are processed character by character — ASCII chars pass through unchanged.
  - Result shown in the **New Name** column (editable before applying); no files are changed until you click Apply.
  - New dependencies: `unidecode>=1.3` and `pypinyin>=0.48` (both pure Python, no native build required).
  - Both packages degrade gracefully if missing: `pypinyin` absent → CJK falls back to `unidecode`; `unidecode` absent → non-ASCII chars are dropped from the suggested name.
- **Name column header renamed** — "New prefix (opt.)" → "New Name (opt.)" to reflect its wider use across prefix removal, long-name shortening, and romanization.

---

## [v7.6] – 2026-07-09

### Added
- **Tab 0 — Sync** (always enabled, even before loading a drive): copies new and updated files from Mac source folders to USB without overwriting files already converted or renamed by this app.
  - `s4converter/sync.py` — `SyncTracker` class persists source-file identity (path + mtime + size) in `.s4_sync.json` at the project root. Tracker is source-centric: USB-side reorganisation is invisible to subsequent scans.
  - **Move detection** — if a source file disappears from one path and appears at another with the same filename + size, it is flagged `MOVED` instead of `DELETED`. Default action: duplicate the existing USB file (preserving any app conversions) to the new USB path, then prompt to delete the old copy.
  - `DELETED` findings start unchecked — nothing is removed from USB without explicit user confirmation.
  - **⚡ Sync + Convert** button: scans for new source files, copies them all to USB, then automatically switches to the Wav Format tab and starts its scan — the full first-session workflow in one click.
  - **Register Existing…** button: one-time setup for users who already have files on USB from a previous transfer. Scans both Mac source and USB, registers files already present on USB as synced (by stem match, handles format conversions). Files missing from USB appear as NEW on the next scan.
- Sync pairs configured in `config.json` under `sync_pairs` (label, source path, USB path per pair).
- Pair status labels in Sync tab show tracked file count and last sync date; hover shows full source → USB paths.
- `.s4_sync.json` added to `.gitignore` (contains machine-local absolute paths; 50 MB+ for large libraries).

---

## [v7.5] – 2026-06-06

### Changed
- **5 tabs instead of 4** — new order: 1. Wav Format → 2. Silence Remover → 3. Stereo to Mono → 4. BPM → 5. Name
  - "Format" renamed to "Wav Format"
  - "File Size" tab split into dedicated **Silence Remover** (phase 5) and **Stereo to Mono** (phase 4) tabs, each with their own columns and apply logic
  - "Names" renamed to "Name" and moved to last position
- **Name tab prefix UI** — "Override (optional)" column replaced with **"New prefix (opt.)"** with a clearer purpose: type a string to prepend to files *after* stripping the detected prefix (e.g. enter `Caribou140-` to produce `Caribou140-Kick.wav` from `SharedPrefix_Kick.wav`). "Detected Prefix" column remains editable for correcting the auto-detected value
- **Column auto-sizing** — table columns resize to content after every scan; last column always stretches to fill the full width; capped at 35% of viewport so long folder paths cannot push other columns off screen

### Fixed
- **Silence Remover always returned 0 findings** — `silencedetect` filter output is logged by ffmpeg at `AV_LOG_INFO` level, which was suppressed by `-v error`. Changed to `-v info`; all files with leading/trailing silence are now correctly detected
- **Silence scan skipped recently-renamed folders** — `apply_phase_2` was calling `FolderMarkers.mark_folder` after a prefix rename, blocking subsequent silence/stereo scans via the fast-scan folder marker. Now uses `FolderMarkers.invalidate` again so other tabs can rescan the folder
- **Replacement prefix re-detected on rescan** — after a Names apply that added a new prefix (e.g. `Caribou140-`), the next scan would detect that prefix as something to strip. Fixed by marking each renamed file as phase-2-done in the cache (`cache.mark_phase_done(new_path, 2)`); `scan_phase_2` filters these files out so the new prefix is never re-reported
- **0-findings scan collapsed column headers** — `resizeColumnToContents` with no rows shrinks all columns to header-text width. Now only called when there are rows to measure against

---

## [v7.4] – 2026-06-06

### Changed
- **Names tab — Prefix rows now show two columns**: "Detected Prefix" (read-only, what the scan found) and "Override (optional)" (editable, empty by default). Leaving Override empty strips the full detected prefix. Typing a custom value (e.g. `Caribou140bpm`) re-reads the folder live and strips that prefix instead — no stale-path mismatches.
- `apply_phase_2` now re-reads the folder live when a custom override prefix is provided, instead of relying on the `affected_files` list captured at scan time

### Fixed
- `apply_phase_2` failure when the Override column was edited to a value that didn't match the original `affected_files` paths; added per-file skip logging to surface the reason in the log pane

---

## [v7.3] – 2026-06-05

### Changed
- **No auto-rescan after Apply** — the automatic re-scan that triggered after every Apply is removed; rescan manually when needed
- **Per-file "done" flags in cache** — after Apply converts a file, a `done|phase|path|mtime|size` flag is written to the cache. On the next scan, files with a valid done flag are skipped entirely (no ffprobe). Files that were already correct format during a scan also get flagged. This makes re-scans O(new files) instead of O(total files) for the workflow of repeatedly adding samples to an existing library.

---

## [v7.2] – 2026-06-04

### Fixed
- **OOM crash during scan** — `QTextEdit` undo stack no longer accumulates one entry per file scanned (disabled via `setUndoRedoEnabled(False)`); folder tree now only re-renders on folder transitions, not every file; current filename moved to a cheap `QLabel`
- **OOM crash at scan completion** — `QTableWidget` population now uses `setUpdatesEnabled(False)` to suppress repaint cascades; display capped at 5 000 rows (all findings still included in Apply)
- **UTF-8 crash on filenames with special characters** — all `subprocess.run` calls in `core.py` and cache load/save in `cache.py` now use `encoding="utf-8", errors="replace"` so filenames containing `©`, `é`, etc. no longer crash ffprobe, ffmpeg, or cache I/O

### Added
- **Apply progress feedback** — progress bar now shows an accurate `X / Y files` count label and a `⟳ filename` line while Apply is running, matching the scan feedback style
- **⏹ Stop button during Apply** — same stop button used during scanning now also appears during Apply; stops cleanly after the current file completes, saves the cache, and logs how many files succeeded/failed

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
