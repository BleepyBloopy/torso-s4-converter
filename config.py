"""Configuration for S-4 Sample Converter.

Edit these values to match your setup, or override at runtime via the GUI/CLI.
"""

from pathlib import Path

# --- Drive / Paths ---
# Default path - the GUI/CLI will let you change this at runtime
DEFAULT_BASE_DIR = Path("/Volumes/S-4/SAMPLES")

# Marker file dropped in each folder after it's been fully processed
FOLDER_MARKER_NAME = ".s4_processed"

# Persistent cache database (stored at BASE_DIR root)
CACHE_FILE_NAME = ".s4_cache.json"

# Log file (stored at BASE_DIR root)
LOG_FILE_NAME = ".s4_converter.log"

# --- Audio Targets ---
FORCE_AR = "48000"              # Torso S-4 native sample rate
THRESHOLD_SECONDS = 10.0        # > this -> 16-bit; <= this -> 24-bit if source > 16

WAV_CODEC_16 = "pcm_s16le"
WAV_CODEC_24 = "pcm_s24le"

# --- Behavior ---
DELETE_ORIGINAL = True          # Phase 1: delete non-WAV source after successful conversion
COPY_METADATA = True

# --- Renaming ---
NAME_LENGTH_LIMIT = 70          # Phase 5: warn if stem longer than this
MIN_PREFIX_LENGTH = 8           # Phase 4: minimum meaningful prefix length
MIN_GROUP_SIZE = 3              # Phase 4: how many files must share prefix
PREFIX_SKIP_LENGTH = 30         # Phase 4: skip prefix if all names already short

# --- Performance ---
FAT32_MTIME_TOLERANCE = 2.0     # seconds - FAT32 has 2s mtime resolution
PARALLEL_FFPROBE_WORKERS = 4    # how many ffprobes to run in parallel

# --- Phase 6: Stereo -> Mono Detection ---
# Detection thresholds in dBFS (decibels relative to full scale, lower = more silent)
#   - max(|L - R|) <= STRICT_DB         -> dual_mono (selected by default - safe)
#   - STRICT_DB < max(|L - R|) <= LOOSE -> near_mono (flagged but NOT auto-selected)
#   - max(|L - R|) > LOOSE_DB           -> true_stereo (skipped entirely)
STEREO_STRICT_THRESHOLD_DB = -90.0   # bit-perfect or very nearly so
STEREO_LOOSE_THRESHOLD_DB = -60.0    # noticeable but tiny stereo width
# If one channel's peak is much louder than the other, file is effectively mono-in-one-side
STEREO_PEAK_IMBALANCE_DB = 40.0

# --- Exclusions ---
EXCLUDED_FOLDER_NAMES = {
    ".Trashes", ".Spotlight-V100", ".fseventsd", "System Volume Information",
    ".TemporaryItems", "$RECYCLE.BIN",
}

# Audio extensions to consider in Phase 1 (non-WAV conversion)
NON_WAV_AUDIO_EXTS = {".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".wma", ".alac"}
