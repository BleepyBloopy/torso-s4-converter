"""Persistent caching for S-4 Sample Converter.

Two caching mechanisms:
1. ffprobe cache (JSON file): stores audio metadata keyed by path+mtime+size.
   Skips re-probing unchanged files - the biggest perf win.
2. Folder markers (.s4_processed files): mark folders as fully processed,
   so we can skip walking into them entirely.
"""

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from . import config


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

    def mark_phase_done(self, path: Path, phase: int) -> None:
        """Record that this file was successfully processed by phase N.

        Keyed by path+mtime+size so the flag auto-invalidates if the file
        is later replaced or modified externally.
        """
        key = self._key(path)
        if key is None:
            return
        self._data[f"done|{phase}|{key}"] = True
        self._dirty = True

    def is_phase_done(self, path: Path, phase: int) -> bool:
        """Return True if this file was previously processed by phase N
        and has not changed since (same mtime+size)."""
        key = self._key(path)
        if key is None:
            return False
        return bool(self._data.get(f"done|{phase}|{key}"))

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


class FolderMarkers:
    """Manage per-folder .s4_processed markers."""

    @staticmethod
    def get_marker_path(folder: Path) -> Path:
        return folder / config.FOLDER_MARKER_NAME

    @staticmethod
    def get_marker_time(folder: Path) -> float:
        """Return mtime of marker, or 0 if no marker exists."""
        marker = FolderMarkers.get_marker_path(folder)
        if marker.exists():
            try:
                return marker.stat().st_mtime
            except OSError:
                return 0.0
        return 0.0

    @staticmethod
    def is_folder_clean(folder: Path, exts: Optional[set] = None) -> bool:
        """Return True if marker exists AND no file in folder is newer than marker.

        Note: only checks direct files in `folder`, not subfolders.
        Accounts for FAT32's 2-second mtime resolution.
        """
        marker_time = FolderMarkers.get_marker_time(folder)
        if marker_time == 0:
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

        # Slow path: directory changed — check individual files.
        try:
            for entry in folder.iterdir():
                if entry.is_file() and entry.name != config.FOLDER_MARKER_NAME:
                    if exts and entry.suffix.lower() not in exts:
                        continue
                    try:
                        if entry.stat().st_mtime > threshold:
                            return False
                    except OSError:
                        return False
        except OSError:
            return False
        return True

    @staticmethod
    def mark_folder(folder: Path) -> None:
        """Drop a marker file with current timestamp."""
        marker = FolderMarkers.get_marker_path(folder)
        try:
            marker.touch(exist_ok=True)
            # Force mtime to now (in case touch didn't update it)
            now = time.time()
            import os
            os.utime(marker, (now, now))
        except OSError:
            pass

    @staticmethod
    def invalidate(folder: Path) -> None:
        """Remove the marker - forces re-scan next time."""
        marker = FolderMarkers.get_marker_path(folder)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
