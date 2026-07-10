# Torso S-4 Smart Sample Converter v7.9

Standardize, organize, and sync audio sample libraries for the Torso S-4 — works with both the S-4's internal storage and external USB drives.

Key features:

- **Internal sync** — copies new/changed Mac source files to USB without re-copying files already converted or renamed by this app; detects source-side moves; never deletes from USB without confirmation
- **Persistent ffprobe cache** — only probes new/changed files (150× faster re-scans)
- **Per-folder markers** — skips entire folders that haven't changed since last run
- **Per-file done flags** — files confirmed clean or already converted are skipped on every subsequent scan; re-scans after adding new samples are O(new files), not O(total library)
- **Parallel ffprobe** — uses multiple workers during the initial scan
- **GUI** — review findings in a table, check/uncheck per file, edit names inline
- **Busy lock** — all buttons gray out while a scan or apply is running; warns before quit
- **Dry-run mode** — preview every change before touching anything (CLI only)
- **Atomic writes** — converter never leaves half-finished files

Tested against libraries of ~300 GB / 270 000+ samples nested across a handful of top-level subfolders on a USB drive.

---

## Installation & Launch

Clone or download the repo, then double-click the launcher for your platform:

| Platform | File | What it does |
|----------|------|-------------|
| macOS | `launch-s4converter-MacOS.command` | Runs setup on first launch, then opens the GUI |
| Windows | `launch-s4converter-Windows.bat` | Checks dependencies, then opens the GUI |

**macOS first-time note:** macOS may show a "downloaded from internet" warning the first time — right-click → Open to allow it. It won't ask again.

**macOS prerequisites:** [Homebrew](https://brew.sh) must be installed. `launch.command` handles everything else (`ffmpeg`, `uv`, Python packages) automatically on first run.

**Windows prerequisites:** Install [ffmpeg](https://ffmpeg.org) (`winget install ffmpeg`) and [uv](https://docs.astral.sh/uv/getting-started/installation/) manually, then run `launch.bat`.

> **aubio on Python 3.14 (macOS):** `setup.sh` passes a `CFLAGS` flag to suppress a compiler error in `aubio` 0.4.9. This is safe and only affects the BPM detection build step.

> **Windows + aubio:** BPM detection (Phase 4 / BPM tab) is untested on Windows — `aubio` may need a pre-built wheel. All other phases work without it.

---

## Usage

### GUI (recommended)

```bash
uv run python -m s4converter.gui
```

**Typical first session (new files dropped on Mac):**

1. Open the app — the **Sync** tab (Tab 0) is active immediately, no drive load needed
2. Click **⚡ Sync + Convert** — copies new source files to USB, then automatically switches to the Wav Format tab and starts its scan
3. Review and apply findings in each processing tab as needed

**Manual workflow:**

1. Go to **Tab 0 — Sync**, click **Scan**, review findings, click **Copy Selected**
2. Select your drive from the dropdown and click **Load**
3. Click into a processing tab and click **Scan**
4. While scanning, a live progress panel shows completed folders (✓), the active folder with its full path, and the current file being scanned. A **⏹ Stop** button lets you exit gracefully after the current batch — the cache is saved automatically.
5. Review findings in the table; uncheck anything you don't want to change. Tables with more than 5 000 findings show the first 5 000 rows — all findings are still included when you click Apply.
6. For the **Name** tab (Tab 5) and **BPM** tab (Tab 4), edit values inline if needed
7. Click **Apply Selected** — a live `X / Y files` counter and current filename are shown while applying

Leave the **Fast scan** checkbox on for fast scans. Uncheck it to force a full re-scan.

### CLI

```bash
# Full interactive run
uv run python -m s4converter.cli --path /Volumes/S-4/SAMPLES

# Run only specific phases (CLI uses internal phase numbers 1–6)
uv run python -m s4converter.cli --phases 1,4

# Phase 1 only, no prompts (for quick conversion of new drops)
uv run python -m s4converter.cli --quick

# Preview only, change nothing
uv run python -m s4converter.cli --dry-run

# Force full scan, ignore markers
uv run python -m s4converter.cli --full-scan
```

---

## The Tabs

### Tab 0 — Sync
Copies new and updated files from Mac source folders to USB. The tracker (`.s4_sync.json`) records source-file identity (path + mtime + size) so that files already converted or renamed by this app are never re-copied — it's source-centric, so USB-side reorganisation is transparent.

- **NEW** — file exists on source but was never synced
- **UPDATED** — source file changed since last sync (different mtime or size)
- **MOVED** — file disappeared from one source path, appeared at another with the same filename + size. Default action: duplicate the existing USB file to the new path (preserving any conversions), then ask whether to delete the old USB copy
- **DELETED** — file removed from source; starts unchecked — never auto-deleted from USB

Configure sync pairs in `config.json` under `sync_pairs`. Hover a pair label to see its full source → USB mapping.

**Register Existing…** — one-time setup if your USB already has files from a previous transfer. Scans both source and USB, registers files found on USB as synced. Run this once; afterwards just use Scan or ⚡ Sync + Convert.

### Tab 1 — Wav Format
Finds non-WAV files (MP3, AIFF, FLAC, M4A, OGG, WMA, ALAC) and WAV files at the wrong sample rate or bit depth, and converts everything to **48 kHz / 16-bit WAV** in a single pass. Originals are deleted on success (configurable via `delete_original` in `config.json`).

### Tab 2 — Silence Remover
Trims leading and trailing silence from WAV files using ffmpeg's `silencedetect` filter. Both threshold (default −60 dBFS) and minimum duration (default 0.1 s) are configurable in `config.json`. Stem exports from DAWs commonly have 30–60 seconds of leading silence — this removes it in one pass.

### Tab 4 — Fake Stereo to Mono
Detects "fake stereo" files and converts them to mono, saving ~50% file size per file.
- `dual_mono` (L−R diff ≤ −90 dB) — selected by default
- `one_side` (one channel > 40 dB louder) — selected by default
- `near_mono` (diff between −90 and −60 dB) — **Loose mode** only, unchecked by default
- `true_stereo` — never flagged

Enable **Loose mode** to also surface near-mono files (listed but unchecked — review carefully).

### Tab 5 — BPM
Detects BPM for rhythmic loops using `aubio` and renames files with a `{bpm}bpm_` prefix (e.g. `120bpm_my_loop.wav`). Having BPM in the filename in this format enables proper sync-mode loading in DISC.

Also scans for files that already have a bare number prefix but are missing the `bpm` label (e.g. `120_kick.wav` → `120bpm_kick.wav`). These are auto-selected and shown at the top of the results.

Multiple filters prevent false positives on one-shots and recordings:
- Duration gate (skips files outside `bpm_min_duration`–`bpm_max_duration`)
- Minimum beat event count
- Consistency score (estimates must converge)
- Half/double correction to land in the target BPM range
- Folder name hints — folders named "one shot", "sfx", "ambient", etc. are skipped

**High-confidence detections (≥ 0.75) are checked by default.** Medium and low confidence results are shown but unchecked — review before selecting.

### Tab 3 — Name
Four passes in one tab:

**Folder Collapse** — finds folders that contain exactly one subfolder and nothing else, and moves the subfolder's contents up one level. Example: `Drums/Kicks/kick.wav` → `Drums/kick.wav` (empty `Kicks/` folder is deleted). Sorted deepest-first so nested collapses work in a single pass.

**Prefix Removal** — scans every subfolder for shared filename prefixes and offers to strip them.
Example: `Loopmasters - Dubstep Pack 2024 - Kick 01.wav` → `Kick 01.wav`.

The table has two editable columns:
- **Detected Prefix** — what the scan found; double-click to correct it. This is the string that gets stripped.
- **New Name (opt.)** — leave empty to just strip the prefix. Type a replacement string to prepend it after stripping. Example: enter `Caribou140-` to rename `Shared_Kick.wav` → `Caribou140-Kick.wav`.

**Long Filenames** — finds files with stems longer than the limit (default 70 chars) and suggests shorter alternatives. Edit the suggested name in the New Name column.

**Non-ASCII** — finds filenames containing non-English characters (Chinese, Japanese, Korean, accented Latin, Cyrillic, etc.) and suggests ASCII transliterations so the S-4 can read them:
- Chinese → pinyin without tones (e.g. `踢鼓_Loop` → `tigu_Loop`)
- Accented Latin → stripped accent (e.g. `Café` → `Cafe`)
- All other scripts → best-effort ASCII via unidecode

Review the suggested name in the New Name column and edit before applying if needed.

Running prefix removal first often brings long names under the limit automatically.

---

## How the Speed Optimizations Work

### Probe Cache (`.s4_cache.json` in your samples folder)
Every ffprobe result is cached by `path|mtime|size`. If a file hasn't changed, we never re-probe it. This is the biggest win on a large drive.

### Folder Markers (`.s4_processed` hidden file per folder)
After a successful scan + apply pass, each folder gets a marker file. On the next incremental scan, folders where nothing is newer than the marker are skipped entirely — no walking, no probing.

The marker is automatically invalidated whenever a file in the folder is renamed or converted, so the next scan will re-check it.

### When to use `--full-scan`
- After moving files around in Finder (markers may not reflect reality)
- If you suspect the cache is stale
- Once every few months for sanity

---

## Configuration

Edit `config.json` to change paths and thresholds — no Python knowledge required.

| Key | Default | Description |
|-----|---------|-------------|
| `s4_root` | `/Volumes/S-4/SAMPLES` | Path to the S-4's internal storage |
| `usb_root` | `/Volumes/USB` | Path to your external USB drive |
| `sync_pairs` | `[]` | List of `{label, source, usb}` objects for Tab 0 sync |
| `delete_original` | `true` | Delete source file after non-WAV conversion |
| `name_length_limit` | `70` | Max filename stem length (Tab 5 / Name tab) |
| `silence_threshold_db` | `-60.0` | Silence detection threshold (Tab 2 / Silence Remover) |
| `silence_min_duration` | `0.1` | Minimum silence length to trim, in seconds (Tab 2) |
| `stereo_strict_threshold_db` | `-90.0` | dual_mono threshold (Tab 3 / Stereo to Mono) |
| `stereo_loose_threshold_db` | `-60.0` | near_mono threshold (Tab 3 / Stereo to Mono) |
| `bpm_min_confidence` | `0.4` | Minimum confidence to report a BPM result |
| `bpm_skip_folder_hints` | (list) | Folder name substrings that skip BPM detection |

---

## Platform Support

| Feature | macOS | Windows | Linux |
|---------|-------|---------|-------|
| GUI | ✅ | ✅ | ✅ |
| CLI | ✅ | ✅ | ✅ |
| Tabs 1–3, 5 | ✅ | ✅ | ✅ |
| Tab 4 (BPM) | ✅ | ⚠️ untested | ✅ |
| One-click launcher | ✅ `launch-s4converter-MacOS.command` | ✅ `launch-s4converter-Windows.bat` | run `./setup.sh` then `uv run python -m s4converter.gui` |
| Auto-setup | ✅ via Homebrew | manual (winget) | manual (apt/pacman) |

---

## File Structure

```
torso-s4-converter/
├── config.json        ← edit this to change paths and thresholds
├── .s4_sync.json      ← sync tracker DB (auto-created, gitignored)
├── requirements.txt
├── CHANGELOG.md
├── README.md
└── s4converter/
    ├── config.py      ← loads config.json, holds internal constants
    ├── cache.py       ← ProbeCache + FolderMarkers
    ├── core.py        ← scan and apply logic (UI-agnostic)
    ├── sync.py        ← SyncTracker + Mac→USB sync logic (Tab 0)
    ├── cli.py         ← command-line interface
    └── gui.py         ← PyQt6 graphical interface
```

---

## Long / Unattended Scans

### Crash recovery and resume

The probe cache is written to disk after every 500 files scanned. If the app crashes or is force-quit mid-scan, restarting and re-scanning picks up from the last saved point — already-probed files are cache hits and are skipped instantly. Folders where changes were already applied have markers set and are skipped by the fast scan too. At worst you lose one 500-file chunk of probe work.

### Mac idle sleep

The GUI automatically prevents your Mac from idle-sleeping during a scan or apply using macOS's built-in `caffeinate` tool. This means even if you walk away for hours, the scan keeps running. `caffeinate` is stopped automatically when the operation finishes or the app closes.

### Shared monitor / Mac mini quirk

**Situation:** You have a Mac mini (or any desktop) sharing one physical monitor with another computer via an input switch. When you switch the monitor input to the other machine, macOS loses the display connection. PyQt6 (the GUI framework) needs an active display to function — losing it mid-scan can stall or crash the app. This is a macOS + GUI limitation, not a bug in this app.

**Workarounds:**

- **Hardware fix (recommended, $8–15):** A headless HDMI or DisplayPort dummy plug tricks the Mac into thinking a monitor is always connected — even when you've switched the input elsewhere. Search "HDMI dummy plug Mac mini" on Amazon. Standard tool for Mac minis used as servers or shared machines.
- **Software fix:** Use the **CLI** for any scan you plan to run unattended or while switching displays. The CLI has no display dependency — it keeps running in Terminal regardless of monitor state:
  ```bash
  uv run python -m s4converter.cli --path "/Volumes/USB/Download Samples"
  ```
  The cache saved by the CLI scan is shared with the GUI, so you can review and apply findings in the GUI afterwards.

---

## Workflow Recommendation

**When you drop new samples on the Mac:**

1. Open the app → **Tab 0 (Sync)** → click **⚡ Sync + Convert**
   - Copies all new/changed source files to USB
   - Automatically runs the Wav Format scan on completion
2. Apply the Wav Format findings to convert any non-WAV files
3. Run other tabs as needed (Silence Remover, Stereo to Mono, BPM, Name)

**First-time setup (USB already has files):**

1. Plug in USB
2. Tab 0 → **Register Existing…** — scans both Mac and USB, registers files already on USB so they won't be re-copied
3. After that, use the normal workflow above

The converter operates in-place on USB; the Mac source folder is never modified.
