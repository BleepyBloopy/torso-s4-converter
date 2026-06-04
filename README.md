# Torso S-4 Smart Sample Converter v7

Standardize, optimize, and organize sample libraries for the Torso S-4 — works with both the S-4's internal storage and external USB drives.

Key features over the original v6 script:

- **Persistent ffprobe cache** — only probes new/changed files (150× faster re-scans)
- **Per-folder markers** — skips entire folders that haven't changed since last run
- **Parallel ffprobe** — uses multiple workers during the initial scan
- **GUI** — review findings in a table, check/uncheck per file, edit names inline
- **Busy lock** — all buttons gray out while a scan or apply is running; warns before quit
- **Dry-run mode** — preview every change before touching anything (CLI only)
- **Atomic writes** — converter never leaves half-finished files

---

## Installation & Launch

Clone or download the repo, then double-click the launcher for your platform:

| Platform | File | What it does |
|----------|------|-------------|
| macOS | `launch.command` | Runs setup on first launch, then opens the GUI |
| Windows | `launch.bat` | Checks dependencies, then opens the GUI |

**macOS first-time note:** macOS may show a "downloaded from internet" warning the first time — right-click → Open to allow it. It won't ask again.

**macOS prerequisites:** [Homebrew](https://brew.sh) must be installed. `launch.command` handles everything else (`ffmpeg`, `uv`, Python packages) automatically on first run.

**Windows prerequisites:** Install [ffmpeg](https://ffmpeg.org) (`winget install ffmpeg`) and [uv](https://docs.astral.sh/uv/getting-started/installation/) manually, then run `launch.bat`.

> **aubio on Python 3.14 (macOS):** `setup.sh` passes a `CFLAGS` flag to suppress a compiler error in `aubio` 0.4.9. This is safe and only affects the BPM detection build step.

> **Windows + aubio:** BPM detection (Phase 6) is untested on Windows — `aubio` may need a pre-built wheel. All other phases work without it.

---

## Usage

### GUI (recommended)

```bash
uv run python -m s4converter.gui
```

1. Select your drive from the dropdown (or set a custom path) and click **Load**
2. Click into a phase tab and click **Scan**
3. Review findings in the table; uncheck anything you don't want to change
4. For Phase 2 (prefixes), Phase 3 (long names), and Phase 6 (BPM), edit values inline if needed
5. Click **Apply Selected**

Leave the **Incremental** checkbox on for fast scans. Uncheck it to force a full re-scan.

### CLI

```bash
# Full interactive run
uv run python -m s4converter.cli --path /Volumes/S-4/SAMPLES

# Run only specific phases
uv run python -m s4converter.cli --phases 1,4

# Phase 1 only, no prompts (for quick conversion of new drops)
uv run python -m s4converter.cli --quick

# Preview only, change nothing
uv run python -m s4converter.cli --dry-run

# Force full scan, ignore markers
uv run python -m s4converter.cli --full-scan
```

---

## The Phases

### Phase 1 — Format Normalization
Finds non-WAV files (MP3, AIFF, FLAC, M4A, OGG, WMA, ALAC) and WAV files at the wrong sample rate or bit depth, and converts everything to **48 kHz / 16-bit WAV** in a single pass. Originals are deleted on success (configurable via `delete_original` in `config.json`).

### Phase 2 — Prefix Removal
Scans every subfolder for shared filename prefixes and offers to strip them.
Example: `Loopmasters - Dubstep Pack 2024 - Kick 01.wav` → `Kick 01.wav`.
You can edit the detected prefix inline in the GUI before applying.

### Phase 3 — Long Filename Cleanup
Finds files with stems longer than the limit (default 70 chars) and suggests shorter alternatives. Edit the suggested name inline before applying.

### Phase 4 — Stereo → Mono Detection
Detects "fake stereo" files where left and right channels are identical (or nearly so) and converts them to mono, saving ~50% file size.

**Detection is mathematical, not heuristic.** For each stereo file:
- Computes peak level of `L`, `R`, and `L - R`
- Classifies as:
  - `dual_mono` (diff peak ≤ −90 dB) — selected by default
  - `one_side` (one channel > 40 dB louder) — selected by default
  - `near_mono` (diff between −90 and −60 dB) — shown only in **loose mode**, unchecked by default
  - `true_stereo` (diff > −60 dB) — never flagged

**Typical wins:** kick/snare/hat one-shots, bass shots, and 808s are usually dual mono. Field recordings, pads, FX risers, and stems are usually true stereo and won't be flagged.

### Phase 5 — Silence Removal
Detects and trims leading and trailing silence from WAV files. Threshold and minimum duration are configurable in `config.json`.

### Phase 6 — BPM Detection
Detects BPM for rhythmic loops using `aubio` and offers to rename files with a `{bpm}_` prefix (e.g. `120_my_loop.wav`). Having BPM in the filename enables proper sync-mode loading in DISC.

Multiple filters prevent false positives on one-shots and recordings:
- Duration gate (skips files outside `bpm_min_duration`–`bpm_max_duration`)
- Minimum beat event count
- Consistency score (estimates must converge)
- Half/double correction to land in the target BPM range
- Folder name hints — folders named "one shot", "sfx", "ambient", etc. are skipped

**High-confidence detections (≥ 0.75) are checked by default.** Medium and low confidence results are shown but unchecked — review before selecting.

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
| `delete_original` | `true` | Delete source file after non-WAV conversion |
| `name_length_limit` | `70` | Max filename stem length (Phase 3) |
| `stereo_strict_threshold_db` | `-90.0` | dual_mono threshold (Phase 4) |
| `stereo_loose_threshold_db` | `-60.0` | near_mono threshold (Phase 4) |
| `bpm_min_confidence` | `0.4` | Minimum confidence to report a BPM result |
| `bpm_skip_folder_hints` | (list) | Folder name substrings that skip BPM detection |

---

## File Structure

```
torso-s4-converter/
├── config.json        ← edit this to change paths and thresholds
├── requirements.txt
├── CHANGELOG.md
├── README.md
└── s4converter/
    ├── config.py      ← loads config.json, holds internal constants
    ├── cache.py       ← ProbeCache + FolderMarkers
    ├── core.py        ← scan and apply logic (UI-agnostic)
    ├── cli.py         ← command-line interface
    └── gui.py         ← PyQt6 graphical interface
```

---

## Workflow Recommendation

1. **CCC mirrors** `~/Download Samples/...` → `USB/Download Samples/...`
2. After CCC sync, run **Phase 1 (`--quick`)** to convert any new non-WAV files — done in seconds for incremental
3. Run the **GUI** for occasional cleanups:
   - Phase 2 to strip pack prefixes
   - Phase 3 to shorten long names
   - Phase 4 to halve file size on fake-stereo files
   - Phase 6 to tag loops with BPM for proper S-4 sync mode

The converter operates in-place, so your Mac source folder stays untouched as your archive.
