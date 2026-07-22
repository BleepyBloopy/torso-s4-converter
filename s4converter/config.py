"""Configuration loader for S-4 Sample Converter.

User-editable settings live in config.json at the repo root.
Edit that file — no Python knowledge required.
This module loads it and fills in defaults for anything not present.
"""

import json
import os
from pathlib import Path

_CONFIG_JSON = Path(__file__).parent.parent / "config.json"


def _load() -> dict:
    if _CONFIG_JSON.exists():
        try:
            # Use os.open() to avoid triggering macOS Launch Services,
            # which would otherwise open config.json in the default JSON app.
            fd = os.open(str(_CONFIG_JSON), os.O_RDONLY)
            try:
                chunks = []
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(fd)
            return json.loads(b"".join(chunks))
        except Exception:
            pass
    return {}


_u = _load()

# --- Drive / Paths ---
S4_ROOT          = Path(_u.get("s4_root",  "/Volumes/S-4/SAMPLES"))
USB_ROOT         = Path(_u.get("usb_root", "/Volumes/USB"))
DEFAULT_BASE_DIR = USB_ROOT


def cache_root_for(path: Path) -> Path:
    """Return the configured drive root that contains path, or path itself."""
    for root in (USB_ROOT, S4_ROOT):
        try:
            path.relative_to(root)
            return root
        except ValueError:
            pass
    return path

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
S4_DISPLAY_LIMIT   = int(_u.get("s4_display_limit", 45))   # max chars S4 shows per filename
MIN_PREFIX_LENGTH  = 8
MIN_GROUP_SIZE     = 3
PREFIX_SKIP_LENGTH = 30

# --- Performance ---
FAT32_MTIME_TOLERANCE    = 2.0
PARALLEL_FFPROBE_WORKERS = int(_u.get("ffprobe_workers", 2))
FFPROBE_CHUNK_SIZE       = int(_u.get("ffprobe_chunk_size", 500))

# --- Memory limits ---
# Files larger than this are skipped in Phase 4 stereo analysis and Phase 5
# silence detection (ffmpeg loads the full audio; huge files stress RAM).
# Set to 0 to disable the limit.
ANALYSIS_MAX_SIZE_MB = int(_u.get("analysis_max_size_mb", 200))

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

# --- Sync pairs (Mac source → USB destination) ---
SYNC_DB_PATH = Path(__file__).parent.parent / ".s4_sync.json"

_raw_pairs = _u.get("sync_pairs", [])
SYNC_PAIRS = [
    {
        "label": p.get("label", Path(p["source"]).name),
        "source": Path(p["source"]),
        "usb": Path(p["usb"]),
    }
    for p in _raw_pairs
    if "source" in p and "usb" in p
]

# --- Exclusions ---
EXCLUDED_FOLDER_NAMES = {
    ".Trashes", ".Spotlight-V100", ".fseventsd", "System Volume Information",
    ".TemporaryItems", "$RECYCLE.BIN",
}

NON_WAV_AUDIO_EXTS = {".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".wma", ".alac", ".mp4"}
