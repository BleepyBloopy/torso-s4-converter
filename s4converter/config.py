"""Configuration loader for S-4 Sample Converter.

User-editable settings live in config.json at the repo root.
Edit that file — no Python knowledge required.
This module loads it and fills in defaults for anything not present.
"""

import json
from pathlib import Path

_CONFIG_JSON = Path(__file__).parent.parent / "config.json"


def _load() -> dict:
    if _CONFIG_JSON.exists():
        try:
            return json.loads(_CONFIG_JSON.read_text())
        except Exception:
            pass
    return {}


_u = _load()

# --- Drive / Paths ---
S4_ROOT          = Path(_u.get("s4_root",  "/Volumes/S-4/SAMPLES"))
USB_ROOT         = Path(_u.get("usb_root", "/Volumes/USB"))
DEFAULT_BASE_DIR = USB_ROOT

FOLDER_MARKER_NAME = ".s4_processed"
CACHE_FILE_NAME    = ".s4_cache.json"
LOG_FILE_NAME      = ".s4_converter.log"

# --- Audio Targets ---
FORCE_AR     = "48000"
WAV_CODEC_16 = "pcm_s16le"

# --- Behavior ---
DELETE_ORIGINAL = bool(_u.get("delete_original", True))
COPY_METADATA   = True

# --- Renaming ---
NAME_LENGTH_LIMIT  = int(_u.get("name_length_limit", 70))
MIN_PREFIX_LENGTH  = 8
MIN_GROUP_SIZE     = 3
PREFIX_SKIP_LENGTH = 30

# --- Performance ---
FAT32_MTIME_TOLERANCE    = 2.0
PARALLEL_FFPROBE_WORKERS = 4

# --- Phase 6: Stereo → Mono Detection ---
STEREO_STRICT_THRESHOLD_DB = float(_u.get("stereo_strict_threshold_db", -90.0))
STEREO_LOOSE_THRESHOLD_DB  = float(_u.get("stereo_loose_threshold_db",  -60.0))
STEREO_PEAK_IMBALANCE_DB   = float(_u.get("stereo_peak_imbalance_db",    40.0))

# --- Phase 5: Silence Removal ---
SILENCE_THRESHOLD_DB  = float(_u.get("silence_threshold_db",  -60.0))
SILENCE_MIN_DURATION  = float(_u.get("silence_min_duration",    0.1))

# --- Phase 6: BPM Detection ---
BPM_MIN_DURATION    = float(_u.get("bpm_min_duration",    2.0))
BPM_MAX_DURATION    = float(_u.get("bpm_max_duration",  120.0))
BPM_MIN_BEATS       = int(  _u.get("bpm_min_beats",         4))
BPM_MIN_CONFIDENCE  = float(_u.get("bpm_min_confidence",  0.4))
BPM_TARGET_MIN      = int(  _u.get("bpm_target_min",       70))
BPM_TARGET_MAX      = int(  _u.get("bpm_target_max",      175))
BPM_SKIP_FOLDER_HINTS = set(_u.get("bpm_skip_folder_hints", [
    "one shot", "one-shot", "oneshot", "fx", "field recording",
    "ambience", "ambient", "foley", "sfx", "hit", "hits",
]))

# --- Exclusions ---
EXCLUDED_FOLDER_NAMES = {
    ".Trashes", ".Spotlight-V100", ".fseventsd", "System Volume Information",
    ".TemporaryItems", "$RECYCLE.BIN",
}

NON_WAV_AUDIO_EXTS = {".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".wma", ".alac"}
