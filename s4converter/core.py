"""Core scanning and conversion logic for S-4 Sample Converter.

Phase layout:
    1  Format Normalization  — non-WAV + wrong SR/bits → 16-bit 48 kHz WAV (combined)
    2  Prefix Removal        — strip shared prefixes from a folder
    3  Long Filenames        — stems > NAME_LENGTH_LIMIT chars
    4  Stereo → Mono         — dual-mono / one-sided / near-mono detection
    5  Silence Removal       — trim leading / trailing silence
    6  BPM Detection         — detect BPM for rhythmic content, optionally rename
"""

import csv
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from . import config
from .cache import FolderMarkers, ProbeCache


log = logging.getLogger(__name__)


# ============================================================================
# Data classes
# ============================================================================

@dataclass
class AudioInfo:
    duration: float
    bits: int
    sample_rate: int
    channels: int


@dataclass
class Finding:
    phase: int
    path: Path
    reason: str
    current: str = ""
    target: str = ""
    savings_bytes: int = 0
    suggested_name: str = ""
    extra: dict = field(default_factory=dict)
    selected: bool = True


# ============================================================================
# Helpers
# ============================================================================

def format_bytes(size: int) -> str:
    n, size_f = 0, float(size)
    for label in ["B", "KB", "MB", "GB", "TB"]:
        if size_f < 1024 or label == "TB":
            return f"{size_f:.2f} {label}"
        size_f /= 1024
        n += 1
    return f"{size_f:.2f} TB"


def is_hidden_or_appledouble(p: Path) -> bool:
    return p.name.startswith(".") or p.name.startswith("._")


def iter_files(base_dir: Path, skip_clean_folders: bool = False,
               extensions: Optional[set] = None) -> Iterator[Path]:
    base_dir = base_dir.resolve()
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in config.EXCLUDED_FOLDER_NAMES
                   and not d.startswith(".")]
        root_path = Path(root)
        if skip_clean_folders and FolderMarkers.is_folder_clean(root_path, extensions):
            continue
        for name in files:
            if name == config.FOLDER_MARKER_NAME:
                continue
            p = root_path / name
            if is_hidden_or_appledouble(p):
                continue
            if extensions is not None and p.suffix.lower() not in extensions:
                continue
            yield p


def ffprobe(path: Path, cache: Optional[ProbeCache] = None) -> Optional[AudioInfo]:
    if cache is not None:
        cached = cache.get(path)
        if cached is not None:
            return AudioInfo(**cached)

    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries",
             "format=duration:stream=bits_per_sample,bits_per_raw_sample,sample_fmt,sample_rate,channels",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=30,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        info = json.loads(res.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    fmt     = info.get("format", {})
    streams = info.get("streams", [])
    if not streams:
        return None
    stream = streams[0]

    try:    duration = float(fmt.get("duration", 0))
    except: duration = 0.0
    try:    sr = int(stream.get("sample_rate", 44100))
    except: sr = 44100
    try:    ch = int(stream.get("channels", 2))
    except: ch = 2

    bits = 16
    for k in ("bits_per_sample", "bits_per_raw_sample"):
        v = stream.get(k)
        if v:
            try:
                b = int(v)
                if b > 0:
                    bits = b
                    break
            except (TypeError, ValueError):
                pass

    if bits == 16:
        sfmt = (stream.get("sample_fmt") or "").lower()
        if "flt" in sfmt or "32" in sfmt:
            bits = 32
        elif "dbl" in sfmt or "64" in sfmt:
            bits = 64
        elif "24" in sfmt:
            bits = 24

    audio_info = AudioInfo(duration=duration, bits=bits, sample_rate=sr, channels=ch)
    if cache is not None:
        cache.set(path, {"duration": audio_info.duration, "bits": audio_info.bits,
                          "sample_rate": audio_info.sample_rate, "channels": audio_info.channels})
    return audio_info


def parallel_ffprobe(paths: List[Path], cache: Optional[ProbeCache],
                     progress_cb: Optional[Callable[[int, int], None]] = None,
                     workers: int = config.PARALLEL_FFPROBE_WORKERS,
                     chunk_size: int = config.FFPROBE_CHUNK_SIZE,
                     file_cb: Optional[Callable[[str], None]] = None,
                     stop_event=None,
                     ) -> List[Tuple[Path, Optional[AudioInfo]]]:
    """Run ffprobe in parallel, processing in chunks to bound memory usage.

    file_cb is called from the calling thread (not OS worker threads) to
    keep Qt signal emissions on the correct thread context.
    stop_event is a threading.Event; when set, scanning stops after the
    current chunk so the cache can be saved cleanly.
    """
    results: List[Tuple[Path, Optional[AudioInfo]]] = []
    total = len(paths)
    done = 0
    for chunk_start in range(0, total, chunk_size):
        if stop_event and stop_event.is_set():
            break
        chunk = paths[chunk_start:chunk_start + chunk_size]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(ffprobe, p, cache): p for p in chunk}
            for fut in as_completed(futures):
                p = futures[fut]
                try:    info = fut.result()
                except: info = None
                results.append((p, info))
                done += 1
                if file_cb:
                    file_cb(str(p))   # called on QThread, not OS worker thread
                if progress_cb:
                    progress_cb(done, total)
        if cache is not None:
            cache.save()  # flush after each chunk — crash-safe resume
    return results


def convert_to_wav(src: Path, dst: Path, target_sr: Optional[str] = None) -> bool:
    target_sr = target_sr or config.FORCE_AR
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if config.COPY_METADATA:
        cmd += ["-map_metadata", "0"]
    cmd += ["-ar", target_sr, "-f", "wav", "-c:a", config.WAV_CODEC_16, str(dst)]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", timeout=300)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def atomic_replace(src: Path, target: Path) -> bool:
    try:
        os.replace(src, target)
        return True
    except OSError:
        return False


def _cleanup_tmp(tmp: Path):
    if tmp.exists():
        try: tmp.unlink()
        except OSError: pass


def open_folder(path: Path) -> None:
    """Open a folder in the system file manager (cross-platform)."""
    import sys
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif sys.platform == "win32":
        cmd = ["explorer", str(path)]
    else:
        cmd = ["xdg-open", str(path)]
    try:
        subprocess.run(cmd, check=False)
    except OSError:
        pass


def check_drive_present(base_dir: Path) -> bool:
    return base_dir.exists() and base_dir.is_dir()


# ============================================================================
# Phase 1: Format Normalization — non-WAV + wrong SR/bits in one scan
# ============================================================================

def scan_phase_1(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 file_cb: Optional[Callable[[str], None]] = None,
                 stop_event=None,
                 ) -> List[Finding]:
    """Flag all audio files that need format correction.

    Non-WAV files → convert to WAV.
    WAV files at wrong sample rate or wrong bit depth → re-encode.
    Target: 48 000 Hz, 16-bit PCM.
    """
    findings: List[Finding] = []
    all_exts  = config.NON_WAV_AUDIO_EXTS | {".wav"}
    candidates = list(iter_files(base_dir, skip_clean_folders=only_new, extensions=all_exts))
    if not candidates:
        return findings

    results   = parallel_ffprobe(candidates, cache, progress_cb, file_cb=file_cb, stop_event=stop_event)
    target_sr = int(config.FORCE_AR)

    for path, info in results:
        if info is None:
            continue
        suf = path.suffix.lower()

        if suf != ".wav":
            dst = path.with_suffix(".wav")
            if dst.exists():
                continue
            findings.append(Finding(
                phase=1, path=path,
                reason=f"{suf.upper()[1:]} → WAV",
                current=f"{suf.upper()[1:]}, {info.sample_rate} Hz, {info.bits}-bit",
                target="48000 Hz, 16-bit WAV",
                extra={"type": "non_wav"},
            ))
        else:
            wrong_sr   = info.sample_rate != target_sr
            wrong_bits = info.bits != 16
            if not wrong_sr and not wrong_bits:
                continue
            parts = []
            if wrong_sr:   parts.append(f"{info.sample_rate} Hz → 48000 Hz")
            if wrong_bits: parts.append(f"{info.bits}-bit → 16-bit")
            findings.append(Finding(
                phase=1, path=path,
                reason=", ".join(parts),
                current=f"{info.sample_rate} Hz, {info.bits}-bit",
                target="48000 Hz, 16-bit",
                extra={"type": "wav_format"},
            ))

    return findings


def apply_phase_1(finding: Finding) -> bool:
    src   = finding.path
    ftype = finding.extra.get("type", "wav_format")

    if ftype == "non_wav":
        dst = src.with_suffix(".wav")
        if dst.exists():
            return False
        tmp = dst.with_name(dst.stem + ".__tmp__.wav")
        if not convert_to_wav(src, tmp):
            _cleanup_tmp(tmp)
            return False
        if not atomic_replace(tmp, dst):
            _cleanup_tmp(tmp)
            return False
        if config.DELETE_ORIGINAL:
            try: src.unlink()
            except OSError: pass
    else:
        tmp = src.with_name(src.stem + ".__tmp__.wav")
        if not convert_to_wav(src, tmp):
            _cleanup_tmp(tmp)
            return False
        if not atomic_replace(tmp, src):
            _cleanup_tmp(tmp)
            return False

    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 2: Common prefix removal
# ============================================================================

def detect_common_prefix(filenames: List[str]) -> str:
    if len(filenames) < 2:
        return ""
    p = os.path.commonprefix(filenames)
    if len(p) < config.MIN_PREFIX_LENGTH:
        return ""
    for sep in [" - ", "_-_", "_", " ", "-"]:
        if sep in p:
            idx = p.rfind(sep)
            if idx >= config.MIN_PREFIX_LENGTH - len(sep):
                return p[:idx + len(sep)]
    return p if len(p) >= config.MIN_PREFIX_LENGTH else ""


def scan_phase_2(folder: Path) -> Optional[Finding]:
    if not folder.is_dir():
        return None
    try:
        files = [f for f in folder.iterdir()
                 if f.is_file() and not is_hidden_or_appledouble(f)
                 and f.name != config.FOLDER_MARKER_NAME]
    except OSError:
        return None
    if len(files) < config.MIN_GROUP_SIZE:
        return None
    file_names = sorted([f.name for f in files])
    prefix = detect_common_prefix(file_names)
    if not prefix:
        return None
    if all(len(n) <= config.PREFIX_SKIP_LENGTH for n in file_names):
        return None
    return Finding(
        phase=2, path=folder,
        reason=f"Shared prefix in {len(file_names)} files",
        current=file_names[0],
        target=file_names[0][len(prefix):] if file_names[0].startswith(prefix) else file_names[0],
        suggested_name=prefix,
        extra={"prefix": prefix,
               "affected_files": [str(f) for f in files if f.name.startswith(prefix)]},
    )


def apply_phase_2(finding: Finding, override_prefix: Optional[str] = None) -> int:
    prefix   = override_prefix if override_prefix is not None else finding.extra.get("prefix", "")
    if not prefix:
        return 0
    count = 0
    for path_str in finding.extra.get("affected_files", []):
        p = Path(path_str)
        if not p.exists() or not p.name.startswith(prefix):
            continue
        new_name = p.name[len(prefix):]
        if not new_name or new_name == p.suffix:
            continue
        new_path = p.with_name(new_name)
        if new_path.exists():
            continue
        try:
            p.rename(new_path)
            count += 1
        except OSError:
            pass
    if count:
        FolderMarkers.invalidate(finding.path)
    return count


def scan_phase_2_all(base_dir: Path, only_new: bool = False,
                     progress_cb: Optional[Callable[[int, int], None]] = None,
                     file_cb: Optional[Callable[[str], None]] = None,
                     stop_event=None,
                     ) -> List[Finding]:
    """Scan every subfolder under base_dir for shared filename prefixes."""
    folders: List[Path] = []
    for root, dirs, _ in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in config.EXCLUDED_FOLDER_NAMES
                   and not d.startswith(".")]
        root_path = Path(root)
        if only_new and FolderMarkers.is_folder_clean(root_path):
            continue
        folders.append(root_path)

    findings: List[Finding] = []
    for i, folder in enumerate(folders, 1):
        if stop_event and stop_event.is_set():
            break
        if progress_cb:
            progress_cb(i, len(folders))
        if file_cb:
            file_cb(str(folder))
        finding = scan_phase_2(folder)
        if finding:
            findings.append(finding)
    return findings


# ============================================================================
# Phase 3: Long filename cleanup
# ============================================================================

def suggest_short_names(name: str) -> List[str]:
    stem, suffix = Path(name).stem, Path(name).suffix
    suggestions  = []
    s1 = re.sub(r"[_\-\s]+", "", stem)
    if s1 and s1 != stem:
        suggestions.append(s1 + suffix)
    m = re.search(r"(\d+)\s*([a-zA-Z]+)", stem)
    if m:
        suggestions.append(f"{m.group(1)}{m.group(2)}{suffix}")
    if len(stem) > 15:
        suggestions.append("..." + stem[-15:] + suffix)
    words = re.findall(r"[A-Za-z0-9]+", stem)
    if words:
        initials = "".join(w[0] for w in words if w)
        if len(initials) >= 2:
            suggestions.append(initials + suffix)
    seen, result = set(), []
    for s in suggestions:
        if s not in seen and s != name:
            seen.add(s)
            result.append(s)
    return result


def scan_phase_3(base_dir: Path, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 file_cb: Optional[Callable[[str], None]] = None,
                 stop_event=None,
                 ) -> List[Finding]:
    findings  = []
    all_files = list(iter_files(base_dir, skip_clean_folders=only_new))
    total     = len(all_files)
    for i, path in enumerate(all_files):
        if stop_event and stop_event.is_set():
            break
        if progress_cb and i % 50 == 0:
            progress_cb(i, total)
        if file_cb:
            file_cb(str(path))
        if len(path.stem) > config.NAME_LENGTH_LIMIT:
            suggestions = suggest_short_names(path.name)
            findings.append(Finding(
                phase=3, path=path,
                reason=f"{len(path.stem)} chars",
                current=path.name, target="",
                suggested_name=suggestions[0] if suggestions else "",
                extra={"suggestions": suggestions},
            ))
    if progress_cb:
        progress_cb(total, total)
    return findings


def apply_phase_3(finding: Finding, new_name: str) -> bool:
    src = finding.path
    if not new_name:
        return False
    if not new_name.endswith(src.suffix):
        new_name += src.suffix
    new_path = src.with_name(new_name)
    if new_path.exists():
        return False
    try:
        src.rename(new_path)
        FolderMarkers.invalidate(src.parent)
        return True
    except OSError:
        return False


# ============================================================================
# Phase 4: Stereo → Mono detection
# ============================================================================

@dataclass
class StereoAnalysis:
    max_diff_db: float
    peak_l_db: float
    peak_r_db: float
    classification: str
    keep_channel: str = "L"


def analyze_stereo(path: Path) -> Optional[StereoAnalysis]:
    def _peak_db(filter_expr: str) -> Optional[float]:
        c = ["ffmpeg", "-v", "info", "-nostdin", "-i", str(path),
             "-af", filter_expr + ",astats=metadata=1:reset=0", "-f", "null", "-"]
        try:
            res = subprocess.run(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 encoding="utf-8", errors="replace", timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None
        for line in res.stderr.split("\n"):
            if "Peak level dB:" in line:
                val = line.split("Peak level dB:")[-1].strip()
                if val == "-inf":
                    return -float("inf")
                try:
                    return float(val)
                except ValueError:
                    pass
        return None

    peak_l    = _peak_db("pan=mono|c0=c0")
    peak_r    = _peak_db("pan=mono|c0=c1")
    peak_diff = _peak_db("pan=mono|c0=c0-c1")

    if peak_l is None or peak_r is None or peak_diff is None:
        return None

    keep_channel = "mix"
    if peak_l > peak_r + config.STEREO_PEAK_IMBALANCE_DB:
        classification, keep_channel = "one_side", "L"
    elif peak_r > peak_l + config.STEREO_PEAK_IMBALANCE_DB:
        classification, keep_channel = "one_side", "R"
    elif peak_diff <= config.STEREO_STRICT_THRESHOLD_DB:
        classification = "dual_mono"
    elif peak_diff <= config.STEREO_LOOSE_THRESHOLD_DB:
        classification = "near_mono"
    else:
        classification = "true_stereo"

    return StereoAnalysis(max_diff_db=peak_diff, peak_l_db=peak_l, peak_r_db=peak_r,
                          classification=classification, keep_channel=keep_channel)


def scan_phase_4(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 include_near_mono: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 file_cb: Optional[Callable[[str], None]] = None,
                 stop_event=None,
                 ) -> List[Finding]:
    findings   = []
    candidates = list(iter_files(base_dir, skip_clean_folders=only_new, extensions={".wav"}))
    if not candidates:
        return findings

    probe_results = parallel_ffprobe(candidates, cache, progress_cb=None, file_cb=file_cb, stop_event=stop_event)
    stereo_files  = [p for p, info in probe_results if info is not None and info.channels == 2]
    if not stereo_files:
        return findings

    max_bytes = config.ANALYSIS_MAX_SIZE_MB * 1024 * 1024 if config.ANALYSIS_MAX_SIZE_MB > 0 else None

    for i, path in enumerate(stereo_files, 1):
        if stop_event and stop_event.is_set():
            break
        if progress_cb:
            progress_cb(i, len(stereo_files))
        if file_cb:
            file_cb(str(path))
        try:
            stat = path.stat()
        except OSError:
            continue
        if max_bytes and stat.st_size > max_bytes:
            continue
        key    = f"stereo|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
        cached = cache._data.get(key) if cache else None
        if cached is not None:
            analysis = StereoAnalysis(**cached)
        else:
            analysis = analyze_stereo(path)
            if analysis is not None and cache is not None:
                cache._data[key] = {"max_diff_db": analysis.max_diff_db,
                                     "peak_l_db": analysis.peak_l_db,
                                     "peak_r_db": analysis.peak_r_db,
                                     "classification": analysis.classification,
                                     "keep_channel": analysis.keep_channel}
                cache._dirty = True
        if analysis is None:
            continue

        flag = sel = False
        if analysis.classification in ("dual_mono", "one_side"):
            flag = sel = True
        elif analysis.classification == "near_mono" and include_near_mono:
            flag = True
        if not flag:
            continue

        try:    sz = path.stat().st_size
        except: continue
        diff_str = ("-inf dB (identical)" if analysis.max_diff_db == -float("inf")
                    else f"{analysis.max_diff_db:.1f} dB")
        reason_map = {"dual_mono": "Channels identical",
                      "one_side":  f"Mono in {analysis.keep_channel} only",
                      "near_mono": "Channels nearly identical"}
        findings.append(Finding(
            phase=4, path=path,
            reason=reason_map.get(analysis.classification, analysis.classification),
            current=f"Stereo ({format_bytes(sz)}), L-R diff: {diff_str}",
            target=f"Mono ({format_bytes(sz // 2)})",
            savings_bytes=sz - sz // 2, selected=sel,
            extra={"classification": analysis.classification,
                   "keep_channel": analysis.keep_channel,
                   "peak_l_db": analysis.peak_l_db,
                   "peak_r_db": analysis.peak_r_db,
                   "max_diff_db": analysis.max_diff_db},
        ))
    return findings


def apply_phase_4(finding: Finding) -> bool:
    src  = finding.path
    keep = finding.extra.get("keep_channel", "mix")
    pan  = {"L": "mono|c0=c0", "R": "mono|c0=c1", "mix": "mono|c0=0.5*c0+0.5*c1"}
    tmp  = src.with_name(src.stem + ".__tmp__.wav")
    cmd  = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(src),
             "-af", f"pan={pan.get(keep, pan['mix'])}",
             "-c:a", config.WAV_CODEC_16, "-ar", config.FORCE_AR, str(tmp)]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", timeout=300)
        if res.returncode != 0:
            _cleanup_tmp(tmp)
            return False
    except (subprocess.TimeoutExpired, OSError):
        _cleanup_tmp(tmp)
        return False
    if not atomic_replace(tmp, src):
        _cleanup_tmp(tmp)
        return False
    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 5: Silence removal
# ============================================================================

def detect_silence_bounds(path: Path,
                           info: Optional[AudioInfo] = None,
                           ) -> Optional[Tuple[float, float]]:
    if info is None:
        info = ffprobe(path)
    if info is None or info.duration <= 0:
        return None

    noise = f"{config.SILENCE_THRESHOLD_DB}dB"
    dur   = str(config.SILENCE_MIN_DURATION)
    cmd   = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
              "-af", f"silencedetect=noise={noise}:duration={dur}",
              "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None

    starts: List[float] = []
    ends:   List[float] = []
    for line in res.stderr.split("\n"):
        if "silence_start:" in line:
            try:    starts.append(float(line.split("silence_start:")[-1].strip()))
            except: pass
        elif "silence_end:" in line:
            try:    ends.append(float(line.split("silence_end:")[1].split("|")[0].strip()))
            except: pass

    lead = trail = 0.0
    if starts and starts[0] <= 0.05 and ends:
        lead = ends[0]
    if starts:
        last_start = starts[-1]
        last_end   = ends[-1] if len(ends) >= len(starts) else info.duration
        if last_end >= info.duration - 0.1 or len(ends) < len(starts):
            if last_start > lead:
                trail = info.duration - last_start
    return (lead, trail)


def scan_phase_5(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 file_cb: Optional[Callable[[str], None]] = None,
                 stop_event=None,
                 ) -> List[Finding]:
    findings   = []
    candidates = list(iter_files(base_dir, skip_clean_folders=only_new, extensions={".wav"}))
    if not candidates:
        return findings

    max_bytes = config.ANALYSIS_MAX_SIZE_MB * 1024 * 1024 if config.ANALYSIS_MAX_SIZE_MB > 0 else None

    for i, path in enumerate(candidates, 1):
        if progress_cb:
            progress_cb(i, len(candidates))
        if stop_event and stop_event.is_set():
            break
        if file_cb:
            file_cb(str(path))
        try:    stat = path.stat()
        except: continue
        if max_bytes and stat.st_size > max_bytes:
            continue
        key    = f"silence|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
        cached = cache._data.get(key) if cache else None
        if cached is not None:
            lead, trail, duration = cached["lead"], cached["trail"], cached["duration"]
        else:
            info   = ffprobe(path, cache)
            bounds = detect_silence_bounds(path, info)
            if bounds is None:
                continue
            lead, trail = bounds
            duration    = info.duration if info else 0.0
            if cache is not None:
                cache._data[key] = {"lead": lead, "trail": trail, "duration": duration}
                cache._dirty = True

        if lead < config.SILENCE_MIN_DURATION and trail < config.SILENCE_MIN_DURATION:
            continue
        trimmed = max(0.0, duration - lead - trail)
        savings = int(stat.st_size * (lead + trail) / duration) if duration > 0 else 0
        findings.append(Finding(
            phase=5, path=path,
            reason=f"Lead {lead:.2f}s  Trail {trail:.2f}s",
            current=f"{duration:.2f}s",
            target=f"~{trimmed:.2f}s",
            savings_bytes=savings,
            extra={"lead": lead, "trail": trail, "duration": duration},
        ))
    return findings


def apply_phase_5(finding: Finding) -> bool:
    src   = finding.path
    lead  = finding.extra.get("lead", 0.0)
    trail = finding.extra.get("trail", 0.0)
    if lead < config.SILENCE_MIN_DURATION and trail < config.SILENCE_MIN_DURATION:
        return False

    noise_p = f"{config.SILENCE_THRESHOLD_DB}dB"
    dur_p   = str(config.SILENCE_MIN_DURATION)
    base_f  = f"silenceremove=start_periods=1:start_threshold={noise_p}:start_duration={dur_p}:detection=peak"

    filters = []
    if lead  >= config.SILENCE_MIN_DURATION: filters.append(base_f)
    if trail >= config.SILENCE_MIN_DURATION: filters += ["areverse", base_f, "areverse"]

    tmp = src.with_name(src.stem + ".__tmp__.wav")
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(src),
           "-af", ",".join(filters), "-c:a", config.WAV_CODEC_16, "-ar", config.FORCE_AR, str(tmp)]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", timeout=300)
        if res.returncode != 0:
            _cleanup_tmp(tmp)
            return False
    except (subprocess.TimeoutExpired, OSError):
        _cleanup_tmp(tmp)
        return False
    if not atomic_replace(tmp, src):
        _cleanup_tmp(tmp)
        return False
    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 6: BPM Detection
# ============================================================================

def detect_bpm(path: Path, info: AudioInfo) -> Optional[Tuple[float, float]]:
    """Return (bpm, confidence) for rhythmic content, or None.

    Three filters before trusting any result:
    1. Duration gate  — skips one-shots (< BPM_MIN_DURATION) and long recordings
    2. Beat event count — rhythmic files produce many regular beat events
    3. Consistency score — loop BPM estimates converge; non-rhythmic ones scatter

    Also applies a half/double correction to land estimates in the target range,
    and checks parent folder name against skip hints.
    """
    if info.duration < config.BPM_MIN_DURATION or info.duration > config.BPM_MAX_DURATION:
        return None

    # Folder hint: skip known non-loop folders
    folder_lower = path.parent.name.lower()
    if "loop" not in folder_lower and any(hint in folder_lower for hint in config.BPM_SKIP_FOLDER_HINTS):
        return None

    try:
        import aubio  # type: ignore
    except ImportError:
        log.warning("aubio not installed — BPM detection unavailable. Run: pip install aubio")
        return None

    hop_s = 512
    try:
        src   = aubio.source(str(path), samplerate=0, hop_size=hop_s)
        tempo = aubio.tempo("default", 1024, hop_s, src.samplerate)
    except Exception:
        return None

    beats: List[float] = []
    try:
        while True:
            samples, read = src()
            if tempo(samples):
                bpm_est = tempo.get_bpm()
                if bpm_est > 0:
                    beats.append(bpm_est)
            if read < hop_s:
                break
    except Exception:
        return None

    # Filter 2: require minimum beat event count
    if len(beats) < config.BPM_MIN_BEATS:
        return None

    # Filter 3: measure consistency on the last ¾ of estimates (detector settles over time)
    stable   = beats[len(beats) // 4:]
    mean_bpm = sum(stable) / len(stable)
    std_dev  = (sum((b - mean_bpm) ** 2 for b in stable) / len(stable)) ** 0.5

    # Confidence: 0–1, higher = more consistent estimates
    relative_std = std_dev / mean_bpm if mean_bpm > 0 else 1.0
    confidence   = max(0.0, 1.0 - relative_std * 5)

    if confidence < config.BPM_MIN_CONFIDENCE:
        return None

    # Half/double correction to land in musical target range
    bpm     = mean_bpm
    lo, hi  = config.BPM_TARGET_MIN, config.BPM_TARGET_MAX
    if bpm < lo and bpm * 2 <= hi * 1.1:
        bpm *= 2
    elif bpm > hi and bpm / 2 >= lo * 0.9:
        bpm /= 2

    if not (lo <= bpm <= hi):
        return None

    return (round(bpm), round(confidence, 2))


_BPM_IN_NAME_RE = re.compile(
    r'(?:^|[_\s\-])(\d{2,3})(?:bpm|[_\s\-]|$)',
    re.IGNORECASE,
)


def _stem_has_bpm(path: Path) -> bool:
    """Return True if the filename already contains a BPM-like number."""
    return bool(_BPM_IN_NAME_RE.search(path.stem))


def scan_phase_6(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 file_cb: Optional[Callable[[str], None]] = None,
                 stop_event=None,
                 ) -> List[Finding]:
    """Detect BPM for WAV files that appear to be rhythmic loops."""
    findings   = []
    candidates = list(iter_files(base_dir, skip_clean_folders=only_new, extensions={".wav"}))
    if not candidates:
        return findings

    # First pass: use cached probe data to filter by duration (fast)
    # Also skip files whose names already contain a BPM value.
    probe_results = parallel_ffprobe(candidates, cache, progress_cb=None, file_cb=file_cb, stop_event=stop_event)
    duration_candidates = [
        (p, info) for p, info in probe_results
        if info is not None
        and config.BPM_MIN_DURATION <= info.duration <= config.BPM_MAX_DURATION
        and not _stem_has_bpm(p)
    ]

    total = len(duration_candidates)
    for i, (path, info) in enumerate(duration_candidates, 1):
        if stop_event and stop_event.is_set():
            break
        if progress_cb:
            progress_cb(i, total)
        if file_cb:
            file_cb(str(path))

        try:    stat = path.stat()
        except: continue
        key    = f"bpm|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
        cached = cache._data.get(key) if cache else None

        if cached is not None:
            bpm_val    = cached.get("bpm")
            confidence = cached.get("confidence")
            if bpm_val is None:
                continue  # previously tried and failed — skip
        else:
            result = detect_bpm(path, info)
            if cache is not None:
                # Cache both successes and failures (None = not rhythmic)
                cache._data[key] = {"bpm": result[0] if result else None,
                                     "confidence": result[1] if result else None}
                cache._dirty = True
            if result is None:
                continue
            bpm_val, confidence = result

        if bpm_val is None:
            continue

        bpm_prefix = f"{int(bpm_val)}_"
        new_name   = bpm_prefix + path.name

        conf_label = ("High" if confidence >= 0.75 else
                      "Med"  if confidence >= 0.50 else "Low")

        findings.append(Finding(
            phase=6, path=path,
            reason=f"{int(bpm_val)} BPM",
            current=path.name,
            target=new_name,
            selected=(confidence >= 0.75),  # auto-select high confidence; med/low opt-in
            extra={"bpm": bpm_val, "confidence": confidence,
                   "conf_label": conf_label, "duration": info.duration},
        ))

    return findings


def apply_phase_6(finding: Finding, new_name: str) -> bool:
    """Rename file with BPM prefix."""
    src = finding.path
    if not new_name:
        return False
    if not new_name.endswith(src.suffix):
        new_name += src.suffix
    new_path = src.with_name(new_name)
    if new_path.exists():
        return False
    try:
        src.rename(new_path)
        FolderMarkers.invalidate(src.parent)
        return True
    except OSError:
        return False


# ============================================================================
# Report / Export
# ============================================================================

def generate_report(base_dir: Path, cache: ProbeCache) -> Tuple[Path, Path]:
    """Generate a CSV file list + Markdown summary of the sample library.

    Uses cached probe data — fast, no re-scanning. BPM data included if
    Phase 6 has been run previously (cached).

    Returns (csv_path, md_path).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = base_dir / f"s4_report_{timestamp}.csv"
    md_path   = base_dir / f"s4_report_{timestamp}.md"

    all_exts   = config.NON_WAV_AUDIO_EXTS | {".wav"}
    rows       = []
    total_size = 0
    fmt_counts: dict = {}
    sr_counts:  dict = {}
    bit_counts: dict = {}
    ch_counts:  dict = {}
    bpm_found  = 0

    for path in iter_files(base_dir, extensions=all_exts):
        info = ffprobe(path, cache)
        try:    sz = path.stat().st_size
        except: sz = 0
        total_size += sz

        fmt = path.suffix.upper()[1:]
        fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1

        bpm_val = conf_val = ""
        try:
            stat   = path.stat()
            b_key  = f"bpm|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
            b_data = cache._data.get(b_key) if cache else None
            if b_data and b_data.get("bpm") is not None:
                bpm_val  = str(int(b_data["bpm"]))
                conf_val = str(b_data.get("confidence", ""))
                bpm_found += 1
        except OSError:
            pass

        if info:
            sr_counts [str(info.sample_rate)] = sr_counts .get(str(info.sample_rate), 0) + 1
            bit_counts[str(info.bits)]         = bit_counts.get(str(info.bits), 0) + 1
            ch_counts [str(info.channels)]     = ch_counts .get(str(info.channels), 0) + 1

        rows.append({
            "path":        str(path.relative_to(base_dir)),
            "filename":    path.name,
            "format":      fmt,
            "sample_rate": info.sample_rate if info else "",
            "bit_depth":   info.bits        if info else "",
            "channels":    info.channels    if info else "",
            "duration_s":  f"{info.duration:.2f}" if info else "",
            "bpm":         bpm_val,
            "bpm_conf":    conf_val,
            "size_bytes":  sz,
            "size":        format_bytes(sz),
        })

    # --- CSV ---
    fieldnames = ["path", "filename", "format", "sample_rate", "bit_depth",
                  "channels", "duration_s", "bpm", "bpm_conf", "size_bytes", "size"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # --- Markdown ---
    target_sr   = int(config.FORCE_AR)
    wrong_sr    = sum(1 for r in rows if r["sample_rate"] and int(r["sample_rate"]) != target_sr)
    wrong_bits  = sum(1 for r in rows if r["bit_depth"]   and int(r["bit_depth"])   != 16)
    non_wav     = sum(1 for r in rows if r["format"] != "WAV")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# S-4 Sample Library Report\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n")
        f.write(f"Path: `{base_dir}`\n\n")
        f.write(f"## Summary\n\n")
        f.write(f"| | |\n|---|---|\n")
        f.write(f"| Total files | {len(rows):,} |\n")
        f.write(f"| Total size  | {format_bytes(total_size)} |\n")
        f.write(f"| Files with BPM detected | {bpm_found} |\n\n")

        if non_wav or wrong_sr or wrong_bits:
            f.write(f"## ⚠ Needs Attention\n\n")
            if non_wav:    f.write(f"- **{non_wav}** non-WAV files — run Phase 1\n")
            if wrong_sr:   f.write(f"- **{wrong_sr}** WAVs at wrong sample rate — run Phase 1\n")
            if wrong_bits: f.write(f"- **{wrong_bits}** WAVs at wrong bit depth — run Phase 1\n")
            f.write("\n")

        def _section(title: str, counts: dict, unit: str = ""):
            f.write(f"## {title}\n\n")
            for k, v in sorted(counts.items(), key=lambda x: -x[1]):
                f.write(f"- {k}{unit}: {v:,} files\n")
            f.write("\n")

        _section("Formats",      fmt_counts)
        _section("Sample Rates", sr_counts,  " Hz")
        _section("Bit Depths",   bit_counts, "-bit")
        _section("Channels",     {("Mono" if k == "1" else "Stereo" if k == "2" else k): v
                                   for k, v in ch_counts.items()})

        f.write(f"*Full file list: [{csv_path.name}]({csv_path.name})*\n")

    return csv_path, md_path


# ============================================================================
# Folder marker bookkeeping + logging
# ============================================================================

def mark_folders_processed(base_dir: Path) -> int:
    count = 0
    for root, dirs, _ in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in config.EXCLUDED_FOLDER_NAMES
                   and not d.startswith(".")]
        FolderMarkers.mark_folder(Path(root))
        count += 1
    return count


def setup_logging(base_dir: Path, verbose: bool = False) -> None:
    log_file = base_dir / config.LOG_FILE_NAME
    fmt      = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root     = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers = []
    try:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)
    except OSError:
        pass
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    root.addHandler(ch)
