"""Persistent caching for S-4 Sample Converter.

Two caching mechanisms:
1. ffprobe cache (JSON file): stores audio metadata keyed by path+mtime+size.
   Skips re-probing unchanged files - the biggest perf win.
2. Per-phase folder markers (.s4_phase{N} files): each scan phase writes its own
   marker so fast-scans can skip folders already confirmed clean for that phase.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

from . import config

# Marker phase IDs.  scan_bpm_relabel uses 10 to avoid colliding with
# scan_phase_6 (BPM detection), even though both produce Finding.phase=6.
ALL_MARKER_PHASES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10})

_LEGACY_MARKER = ".s4_processed"   # old shared marker — migrated to .s4_phase1


class ProbeCache:
    """In-memory + on-disk cache for ffprobe results.

    Key format: 'path|mtime|size' - any change invalidates the entry.
    """

    def __init__(self, base_dir: Path, cache_root: Optional[Path] = None):
        self.base_dir = base_dir
        # Cache always lives at the configured drive root so all subfolder
        # scans share the same probe data.
        self.cache_file = (cache_root or base_dir) / config.CACHE_FILE_NAME
        self._data: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self.load()

    def _key(self, path: Path) -> Optional[str]:
        try:
            st = path.stat()
            return f"{path}|{st.st_mtime:.0f}|{st.st_size}"
        except OSError:
            return None

    def get(self, path: Path) -> Optional[Dict[str, Any]]:
        key = self._key(path)
        if key is None:
            return None
        return self._data.get(key)

    def set(self, path: Path, probe_data: Dict[str, Any]) -> None:
        key = self._key(path)
        if key is None:
            return
        self._data[key] = probe_data
        self._dirty = True

    def load(self) -> None:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8", errors="replace") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                self._data = {}

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            tmp = self.cache_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=True)
            tmp.replace(self.cache_file)
            self._dirty = False
        except OSError:
            pass

    def prune(self) -> int:
        """Remove cache entries whose underlying files no longer exist.
        Returns count of pruned entries.
        """
        valid: Dict[str, Dict[str, Any]] = {}
        for key, value in self._data.items():
            path_str = key.split("|", 1)[0]
            if Path(path_str).exists():
                valid[key] = value
        pruned = len(self._data) - len(valid)
        if pruned:
            self._data = valid
            self._dirty = True
        return pruned

    def size(self) -> int:
        return len(self._data)

    def mark_bpm_relabel_reviewed(self, path: Path) -> None:
        """Permanently suppress a file from BPM Relabel scan results."""
        self._data[f"bpm_relabel_skip|{path}"] = True
        self._dirty = True

    def is_bpm_relabel_reviewed(self, path: Path) -> bool:
        return bool(self._data.get(f"bpm_relabel_skip|{path}"))


class FolderMarkers:
    """Per-phase folder markers (.s4_phase{N}).

    Each scan phase writes its own marker file after confirming a folder is
    clean.  Phases only read their own marker — no cross-phase coupling.
    invalidate() removes all markers so any apply always triggers a full
    re-scan by every phase on the next run.
    """

    @staticmethod
    def _marker_path(folder: Path, phase: int) -> Path:
        return folder / f".s4_phase{phase}"

    @staticmethod
    def is_folder_clean(folder: Path, phase: int, exts: Optional[set] = None) -> bool:
        """Return True if this phase's marker exists AND no file in folder is newer.

        For phase 1 only: falls back to the legacy .s4_processed marker so
        existing USB drives benefit immediately without a full re-scan.
        Note: only checks direct files in `folder`, not subfolders.
        Accounts for FAT32's 2-second mtime resolution.
        """
        marker = FolderMarkers._marker_path(folder, phase)
        marker_time = 0.0
        if marker.exists():
            try:
                marker_time = marker.stat().st_mtime
            except OSError:
                return False
        elif phase == 1:
            legacy = folder / _LEGACY_MARKER
            if legacy.exists():
                try:
                    marker_time = legacy.stat().st_mtime
                except OSError:
                    return False

        if marker_time == 0.0:
            return False

        threshold = marker_time + config.FAT32_MTIME_TOLERANCE

        # Fast path: directory mtime only updates when files are added/deleted/
        # renamed inside it. If it hasn't changed since the marker, skip the
        # per-file stat walk entirely (O(1) instead of O(N files)).
        try:
            if folder.stat().st_mtime <= threshold:
                return True
        except OSError:
            return False

        # Slow path: directory changed — check files and direct subdirs.
        # Subdirectory mtimes must be checked too: a newly synced subfolder
        # has no files directly in the parent, so a file-only check would
        # incorrectly report the parent as clean.
        try:
            for entry in folder.iterdir():
                if entry.name.startswith(".s4_"):
                    continue
                try:
                    if entry.is_dir():
                        if entry.stat().st_mtime > threshold:
                            return False
                    elif entry.is_file():
                        if exts and entry.suffix.lower() not in exts:
                            continue
                        if entry.stat().st_mtime > threshold:
                            return False
                except OSError:
                    return False
        except OSError:
            return False
        return True

    @staticmethod
    def mark_folder(folder: Path, phase: int) -> None:
        """Touch this phase's marker with current timestamp."""
        marker = FolderMarkers._marker_path(folder, phase)
        try:
            marker.touch(exist_ok=True)
            now = time.time()
            os.utime(marker, (now, now))
        except OSError:
            pass

    @staticmethod
    def invalidate(folder: Path) -> None:
        """Remove ALL phase markers — forces every phase to re-scan next time."""
        for phase in ALL_MARKER_PHASES:
            try:
                FolderMarkers._marker_path(folder, phase).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (folder / _LEGACY_MARKER).unlink(missing_ok=True)
        except OSError:
            pass
