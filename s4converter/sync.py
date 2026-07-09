"""Internal sync tracker for S-4 Sample Converter.

Replaces CCC for keeping the USB sample library current with Mac source folders.

The tracker records source-file identity (pair_label + rel_path + mtime + size).
When a source file hasn't changed, its USB counterpart is skipped on the next
sync — even if the app converted or renamed the USB copy.  Only genuinely new
or changed source files are copied over.

Sync DB is stored at the project root (.s4_sync.json) so it persists across
drive remounts without relying on either drive being mounted.

Move detection: files that disappear from one source location and reappear at
another (same filename + size) are flagged as 'moved' rather than 'deleted'.
The default action is to duplicate the existing USB file to the new USB path
(preserving any app conversions), then ask the user whether to delete the old
USB copy.  No files are deleted from USB without explicit user confirmation.
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config

AUDIO_EXTENSIONS = {
    ".wav", ".aiff", ".aif", ".mp3", ".flac", ".m4a", ".ogg", ".wma", ".alac",
}


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------

@dataclass
class SyncFinding:
    status: str          # 'new' | 'updated' | 'deleted' | 'moved'
    pair_label: str
    rel_path: str        # relative to source root; old path for 'moved'
    source_path: Path    # old (may not exist) for 'moved'
    usb_path: Path       # old USB path (may already have been converted by app)
    size: int = 0
    # Populated only for 'moved' status:
    moved_to_rel_path: Optional[str] = None
    moved_to_source_path: Optional[Path] = None   # new source path (exists)
    moved_to_usb_path: Optional[Path] = None       # where to put it on USB
    selected: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        # Deletions start unchecked — require explicit confirmation
        if self.status == "deleted":
            self.selected = False


# ---------------------------------------------------------------------------
# Sync tracker
# ---------------------------------------------------------------------------

class SyncTracker:
    """Tracks which source files have been copied to USB.

    Key format: "{pair_label}|{rel_path}" → {mtime, size, synced_at}
    Uses ±2 s mtime tolerance to survive FAT32 filesystem rounding.
    """

    def __init__(self) -> None:
        self._data: Dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if config.SYNC_DB_PATH.exists():
            try:
                with open(config.SYNC_DB_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def save(self) -> None:
        if not self._dirty:
            return
        tmp = config.SYNC_DB_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=True)
            tmp.replace(config.SYNC_DB_PATH)
            self._dirty = False
        except OSError:
            pass

    def _key(self, pair_label: str, rel_path: str) -> str:
        return f"{pair_label}|{rel_path}"

    def is_synced(self, pair_label: str, rel_path: str, mtime: float, size: int) -> bool:
        """True if source file is unchanged since last sync."""
        entry = self._data.get(self._key(pair_label, rel_path))
        if not entry:
            return False
        return (
            abs(entry.get("mtime", 0) - mtime) <= config.FAT32_MTIME_TOLERANCE
            and entry.get("size", -1) == size
        )

    def mark_synced(self, pair_label: str, rel_path: str, mtime: float, size: int) -> None:
        self._data[self._key(pair_label, rel_path)] = {
            "mtime": mtime,
            "size": size,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True

    def bootstrap(
        self, pair_label: str, rel_path: str, mtime: float, size: int, synced_at: str
    ) -> None:
        """Register a file as already synced without copying it (CCC handoff)."""
        self._data[self._key(pair_label, rel_path)] = {
            "mtime": mtime,
            "size": size,
            "synced_at": synced_at,
        }
        self._dirty = True

    def all_rel_paths_for_pair(self, pair_label: str) -> Dict[str, dict]:
        prefix = f"{pair_label}|"
        return {k[len(prefix):]: v for k, v in self._data.items() if k.startswith(prefix)}

    def remove(self, pair_label: str, rel_path: str) -> None:
        key = self._key(pair_label, rel_path)
        if key in self._data:
            del self._data[key]
            self._dirty = True

    def count_for_pair(self, pair_label: str) -> int:
        prefix = f"{pair_label}|"
        return sum(1 for k in self._data if k.startswith(prefix))

    def last_sync_time(self, pair_label: str) -> Optional[str]:
        times = [
            v["synced_at"]
            for k, v in self._data.items()
            if k.startswith(f"{pair_label}|") and "synced_at" in v
        ]
        return max(times) if times else None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _walk_source(source: Path, stop_event=None) -> Dict[str, Tuple[float, int]]:
    """Return {rel_path: (mtime, size)} for all audio files under source."""
    result: Dict[str, Tuple[float, int]] = {}
    try:
        for p in source.rglob("*"):
            if stop_event and stop_event.is_set():
                return result
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                try:
                    st = p.stat()
                    result[str(p.relative_to(source))] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return result


def _detect_moves(
    new_findings: List[SyncFinding],
    deleted_findings: List[SyncFinding],
    pair_usb: Path,
) -> Tuple[List[SyncFinding], List[SyncFinding], List[SyncFinding]]:
    """Cross-reference new vs deleted findings to identify moved files.

    Match heuristic: same filename (stem + ext) and same file size.
    Returns (kept_new, kept_deleted, moved).
    Ambiguous matches (one-to-many) are left as new + deleted.
    """
    # Index new findings by (lowered filename, size) → list of findings
    new_by_key: Dict[Tuple[str, int], List[SyncFinding]] = {}
    for f in new_findings:
        key = (Path(f.rel_path).name.lower(), f.size)
        new_by_key.setdefault(key, []).append(f)

    moved: List[SyncFinding] = []
    absorbed_new: set = set()
    kept_deleted: List[SyncFinding] = []

    for d in deleted_findings:
        key = (Path(d.rel_path).name.lower(), d.size)
        candidates = new_by_key.get(key, [])
        # Only treat as "moved" when there is exactly one unambiguous new match
        unabsorbed = [c for c in candidates if id(c) not in absorbed_new]
        if len(unabsorbed) == 1:
            n = unabsorbed[0]
            absorbed_new.add(id(n))
            moved.append(SyncFinding(
                status="moved",
                pair_label=d.pair_label,
                rel_path=d.rel_path,           # old location
                source_path=d.source_path,     # old source (gone)
                usb_path=d.usb_path,           # old USB file (may be converted)
                size=d.size,
                moved_to_rel_path=n.rel_path,
                moved_to_source_path=n.source_path,
                moved_to_usb_path=pair_usb / n.rel_path,
            ))
        else:
            kept_deleted.append(d)

    kept_new = [f for f in new_findings if id(f) not in absorbed_new]
    return kept_new, kept_deleted, moved


def scan_all(
    tracker: SyncTracker,
    progress_cb: Optional[Callable] = None,
    file_cb: Optional[Callable] = None,
    stop_event=None,
) -> List[SyncFinding]:
    """Scan all configured sync pairs; return files that need attention."""
    all_findings: List[SyncFinding] = []

    for pair in config.SYNC_PAIRS:
        if stop_event and stop_event.is_set():
            break
        label = pair["label"]
        source: Path = pair["source"]
        usb: Path = pair["usb"]

        if not source.exists():
            continue

        source_files = _walk_source(source, stop_event)
        total = len(source_files)

        new_or_updated: List[SyncFinding] = []
        deleted: List[SyncFinding] = []

        for i, (rel, (mtime, size)) in enumerate(sorted(source_files.items())):
            if stop_event and stop_event.is_set():
                return all_findings
            if file_cb:
                file_cb(str(source / rel))
            if progress_cb and total:
                progress_cb(i + 1, total)
            if tracker.is_synced(label, rel, mtime, size):
                continue
            prev = tracker._data.get(tracker._key(label, rel))
            new_or_updated.append(SyncFinding(
                status="updated" if prev else "new",
                pair_label=label,
                rel_path=rel,
                source_path=source / rel,
                usb_path=usb / rel,
                size=size,
            ))

        # Files previously tracked but no longer in source
        for rel in tracker.all_rel_paths_for_pair(label):
            if rel not in source_files:
                deleted.append(SyncFinding(
                    status="deleted",
                    pair_label=label,
                    rel_path=rel,
                    source_path=source / rel,
                    usb_path=usb / rel,
                ))

        # Cross-reference new ↔ deleted to detect moves
        kept_new, kept_deleted, moved = _detect_moves(new_or_updated, deleted, usb)

        all_findings.extend(kept_new)
        all_findings.extend(moved)
        all_findings.extend(kept_deleted)

    return all_findings


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_copy(finding: SyncFinding, tracker: SyncTracker) -> bool:
    """Copy source → USB (new/updated) or duplicate old USB → new USB (moved).

    Returns True on success.
    """
    if finding.status == "moved":
        return _apply_moved_duplicate(finding, tracker)

    try:
        finding.usb_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = finding.usb_path.suffix
        tmp = finding.usb_path.parent / (finding.usb_path.stem + ".__synctmp__" + suffix)
        shutil.copy2(finding.source_path, tmp)
        tmp.replace(finding.usb_path)
        st = finding.source_path.stat()
        tracker.mark_synced(finding.pair_label, finding.rel_path, st.st_mtime, st.st_size)
        return True
    except (OSError, shutil.Error):
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return False


def _apply_moved_duplicate(finding: SyncFinding, tracker: SyncTracker) -> bool:
    """Duplicate: copy the old USB file to the new USB path, update tracker.

    If the old USB file no longer exists (already deleted manually), falls back
    to copying the new source file instead.  Either way the old tracker entry
    is replaced with the new location.
    """
    if not finding.moved_to_usb_path or not finding.moved_to_rel_path:
        return False

    dst = finding.moved_to_usb_path
    src = finding.usb_path if finding.usb_path.exists() else finding.moved_to_source_path

    if src is None or not src.exists():
        return False

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / (dst.stem + ".__synctmp__" + dst.suffix)
        shutil.copy2(src, tmp)
        tmp.replace(dst)
        # Update tracker: remove old entry, record new location
        tracker.remove(finding.pair_label, finding.rel_path)
        if finding.moved_to_source_path and finding.moved_to_source_path.exists():
            st = finding.moved_to_source_path.stat()
            tracker.mark_synced(
                finding.pair_label, finding.moved_to_rel_path, st.st_mtime, st.st_size
            )
        return True
    except (OSError, shutil.Error):
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return False


def apply_delete_usb(finding: SyncFinding, tracker: SyncTracker) -> bool:
    """Remove a USB file whose source was deleted, and remove from tracker.

    Only called when the user explicitly selects a 'deleted' finding for removal.
    """
    try:
        if finding.usb_path.exists():
            finding.usb_path.unlink()
        tracker.remove(finding.pair_label, finding.rel_path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Bootstrap (CCC handoff)
# ---------------------------------------------------------------------------

def _build_usb_stem_index(usb: Path) -> Dict[str, set]:
    """Build {usb_folder_str: {lowercase_stem, ...}} for quick USB presence checks.

    Used by bootstrap to detect files that are already on USB, even if the app
    converted their format (e.g. Kick.aiff on source → Kick.wav on USB).
    """
    index: Dict[str, set] = {}
    try:
        for p in usb.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                key = str(p.parent)
                index.setdefault(key, set()).add(p.stem.lower())
    except OSError:
        pass
    return index


def bootstrap_all(
    tracker: SyncTracker,
    synced_at: str,
    progress_cb: Optional[Callable] = None,
    file_cb: Optional[Callable] = None,
    stop_event=None,
) -> Tuple[int, int]:
    """Register source files that are already present on USB as synced.

    Ground-truth approach: checks what is actually on USB rather than
    relying on file timestamps (audio samples have old mtimes from the
    producer, not from when you added them to the Mac folder).

    A source file is considered 'already synced' if a file with the same
    stem exists on USB at the corresponding path — handles the case where
    the app already converted the format (e.g. .aiff → .wav).

    Files with no USB counterpart are left unregistered and appear as NEW
    on the next scan.

    Returns (registered, skipped_new) counts.
    """
    registered = skipped_new = 0
    for pair in config.SYNC_PAIRS:
        if stop_event and stop_event.is_set():
            break
        label = pair["label"]
        source: Path = pair["source"]
        usb: Path = pair["usb"]

        if not source.exists():
            continue

        if file_cb:
            file_cb(str(source))

        # Build a stem index of what's already on USB for this pair
        usb_index = _build_usb_stem_index(usb) if usb.exists() else {}

        source_files = _walk_source(source, stop_event)
        n = len(source_files)

        for i, (rel, (mtime, size)) in enumerate(sorted(source_files.items())):
            if stop_event and stop_event.is_set():
                break
            if file_cb:
                file_cb(str(source / rel))
            if progress_cb and n:
                progress_cb(i + 1, n)

            # Check if a file with the same stem exists on USB
            usb_file = usb / rel
            usb_folder_key = str(usb_file.parent)
            usb_stems = usb_index.get(usb_folder_key, set())
            on_usb = usb_file.stem.lower() in usb_stems

            if on_usb:
                tracker.bootstrap(label, rel, mtime, size, synced_at)
                registered += 1
            else:
                skipped_new += 1  # not on USB — will appear as NEW

    return registered, skipped_new
