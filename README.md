# Torso S-4 Smart Sample Converter v7

Standardize, optimize, and organize sample libraries for the Torso S-4.

This is a refactor of the original v6 script, designed for **large external drives**
(e.g. 512GB USB) rather than the S-4's small internal storage. The big changes:

- **Persistent ffprobe cache** — only probes new/changed files (150× faster re-scans)
- **Per-folder markers** — skips entire folders that haven't changed since last run
- **Parallel ffprobe** — uses multiple workers during the initial scan
- **GUI** — review findings in a table, check/uncheck per file, edit names inline
- **Dry-run mode** — preview every change before touching anything
- **Atomic writes** — converter never leaves half-finished files
- **Drive disconnect handling** — gracefully fails if USB unmounts mid-run

---

## Installation

```bash
# Requirements
brew install ffmpeg python@3
pip install PyQt6   # only needed for the GUI
```

Place the `s4_converter/` folder somewhere on your Mac (e.g. `~/scripts/`).

---

## Usage

### GUI (recommended)

```bash
cd ~/scripts
python3 -m s4_converter.gui
```

1. Set the drive path (e.g. `/Volumes/S-4/SAMPLES`) and click **Load**
2. Click into a phase tab and click **Scan**
3. Review findings in the table; uncheck anything you don't want to change
4. For Phase 4 (prefix) and Phase 5 (rename), edit values inline if needed
5. Click **Apply Selected**

Leave the **Incremental** checkbox on for fast scans. Uncheck it to force a full re-scan.

### CLI

```bash
# Full interactive run (preserves original v6 workflow)
python3 -m s4_converter.cli --path /Volumes/S-4/SAMPLES

# Run only specific phases
python3 -m s4_converter.cli --phases 1,3

# Phase 1 only, no prompts (for quick conversion of new drops)
python3 -m s4_converter.cli --quick

# Preview only, change nothing
python3 -m s4_converter.cli --dry-run

# Force full scan, ignore markers
python3 -m s4_converter.cli --full-scan
```

---

## The Phases

### Phase 1 — Non-WAV Conversion
Finds MP3, AIFF, FLAC, M4A, OGG and converts to 48kHz WAV.
Bit depth is auto-chosen: 16-bit for files > 10s, 24-bit for short files.
Original is deleted on success (configurable).

### Phase 2 — Sample Rate Compliance
Finds WAVs not at 48kHz and resamples them.
Preserves bit depth. **Note:** if you have existing S-4 projects referencing
these files, slice markers may drift slightly because the total sample count changes.

### Phase 3 — Bit Depth Optimization
Finds 24-bit WAVs longer than 10 seconds and converts them to 16-bit.
Field recordings, long loops, and stems often don't need 24-bit; this saves ~33%.

### Phase 4 — Prefix Removal
You point it at a folder, it detects shared prefixes
(`"Loopmasters - Dubstep Pack 2024 - Kick 01.wav"`) and offers to strip them.
You can edit the detected prefix in the GUI before applying.

### Phase 5 — Long Filename Cleanup
Finds files with stems > 50 chars (S-4's display limit) and suggests
shorter alternatives. You can edit the suggested name in the GUI.

### Phase 6 — Stereo → Mono Detection
Detects "fake stereo" files where left and right channels are identical
(or nearly so) and offers to convert them to mono, halving the file size.

**Detection is mathematical, not heuristic.** For each stereo file:
- Computes peak level of `L`, `R`, and `L - R`
- Classifies as:
  - `dual_mono` (L = R bit-identical, diff peak ≤ -90 dB) — selected by default
  - `one_side` (one channel silent, > 40 dB louder than the other) — selected by default
  - `near_mono` (small diff, between -90 dB and -60 dB) — only shown in **loose mode**, NOT selected by default
  - `true_stereo` (diff > -60 dB) — **never flagged**, file is left alone

Strict mode is the default; loose mode requires opting in via the checkbox
(GUI) or prompt (CLI), and even then near-mono files are unchecked until you
explicitly select them.

**Typical wins on a sample library:** kick/snare/hat one-shots, bass shots,
and 808s are usually dual mono. Field recordings, pads, FX risers, and
stems are usually true stereo and won't be flagged.

---

## How the Speed Optimizations Work

### Probe Cache (`.s4_cache.json` in your SAMPLES folder)
Every ffprobe result is cached by `path|mtime|size`. If a file hasn't changed,
we never re-probe it. This is the biggest win on a big drive.

### Folder Markers (`.s4_processed` hidden file per folder)
After a successful scan + apply pass, each folder gets a marker file with the
current timestamp. On the next incremental scan, we compare each folder's
contents to its marker mtime — if nothing inside is newer, we skip the
folder entirely (no walking, no probing).

The marker is automatically **invalidated** whenever a file in the folder is
renamed or converted, so the next scan will re-check it.

### When to use `--full-scan`
- After moving files around in Finder (markers may not reflect reality)
- If you suspect the cache is stale
- Once every few months for sanity

---

## File Structure

```
s4_converter/
├── __init__.py     # Package marker
├── config.py       # All settings (edit defaults here)
├── cache.py        # ProbeCache + FolderMarkers
├── core.py         # Scan and apply logic (UI-agnostic)
├── cli.py          # Command-line interface
├── gui.py          # PyQt6 graphical interface
└── README.md       # This file
```

---

## Workflow Recommendation

For your typical use:
1. **CCC mirrors** `~/Download Samples/...` → `USB/Download Samples/...`
2. After CCC sync, eject USB, plug into S-4 *or* run the converter
3. Run **`--quick`** to convert any new MP3s to WAV — done in seconds for incremental
4. Run the GUI for occasional cleanups (Phase 3 to recover space, Phase 4/5 to tidy names)

The converter operates **in-place on the USB**, so your Mac source folder
stays untouched as your archive.
