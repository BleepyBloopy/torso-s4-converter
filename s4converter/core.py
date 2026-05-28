"""Core scanning and conversion logic for S-4 Sample Converter.

This module is UI-agnostic: pure functions return data, callers (CLI or GUI)
decide how to display and what to apply.

Phase layout:
    1  Non-WAV Conversion     — MP3/AIFF/FLAC/… → 16-bit 48 kHz WAV
    2  Sample Rate + Bit Depth — WAVs not at 48 kHz or not 16-bit
    3  Prefix Removal          — strip shared prefixes from a folder
    4  Long Filenames          — stems > NAME_LENGTH_LIMIT chars
    5  Stereo → Mono           — dual-mono / one-sided / near-mono detection
    6  Silence Removal         — trim leading / trailing silence
"""

import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    """A file flagged by a scan, ready for review/action."""
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
    n = 0
    labels = ["B", "KB", "MB", "GB", "TB"]
    size_f = float(size)
    while size_f >= 1024 and n < len(labels) - 1:
        size_f /= 1024
        n += 1
    return f"{size_f:.2f} {labels[n]}"


def is_hidden_or_appledouble(p: Path) -> bool:
    return p.name.startswith(".") or p.name.startswith("._")


def is_audio_file(p: Path, include_wav: bool = True) -> bool:
    suf = p.suffix.lower()
    if suf == ".wav":
        return include_wav
    return suf in config.NON_WAV_AUDIO_EXTS


def iter_files(base_dir: Path, skip_clean_folders: bool = False,
               extensions: Optional[set] = None) -> Iterator[Path]:
    """Yield audio files under base_dir, optionally skipping marker-clean folders."""
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
    """Run ffprobe and return AudioInfo, using cache if available."""
    if cache is not None:
        cached = cache.get(path)
        if cached is not None:
            return AudioInfo(**cached)

    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries",
                "format=duration:stream=bits_per_sample,bits_per_raw_sample,sample_fmt,sample_rate,channels",
                "-of", "json",
                str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=30,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        info = json.loads(res.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    fmt = info.get("format", {})
    streams = info.get("streams", [])
    if not streams:
        return None
    stream = streams[0]

    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    try:
        sr = int(stream.get("sample_rate", 44100))
    except (TypeError, ValueError):
        sr = 44100
    try:
        ch = int(stream.get("channels", 2))
    except (TypeError, ValueError):
        ch = 2

    bits = 16
    for k in ("bits_per_sample", "bits_per_raw_sample"):
        v = stream.get(k)
        if v:
            try:
                bits = int(v)
                if bits > 0:
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
        cache.set(path, {
            "duration": audio_info.duration,
            "bits": audio_info.bits,
            "sample_rate": audio_info.sample_rate,
            "channels": audio_info.channels,
        })

    return audio_info


def parallel_ffprobe(paths: List[Path], cache: Optional[ProbeCache],
                     progress_cb: Optional[Callable[[int, int], None]] = None,
                     workers: int = config.PARALLEL_FFPROBE_WORKERS
                     ) -> List[Tuple[Path, Optional[AudioInfo]]]:
    results: List[Tuple[Path, Optional[AudioInfo]]] = []
    total = len(paths)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(ffprobe, p, cache): p for p in paths}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            results.append((p, info))
            done += 1
            if progress_cb:
                progress_cb(done, total)
    return results


def convert_to_wav(src: Path, dst: Path, target_sr: Optional[str] = None) -> bool:
    """Convert/re-encode audio to 16-bit WAV at target_sr. Returns True on success."""
    target_sr = target_sr or config.FORCE_AR
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if config.COPY_METADATA:
        cmd += ["-map_metadata", "0"]
    cmd += ["-ar", target_sr, "-f", "wav", "-c:a", config.WAV_CODEC_16, str(dst)]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=300)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def atomic_replace(src: Path, target: Path) -> bool:
    try:
        os.replace(src, target)
        return True
    except OSError:
        return False


def check_drive_present(base_dir: Path) -> bool:
    return base_dir.exists() and base_dir.is_dir()


def _cleanup_tmp(tmp: Path):
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass


# ============================================================================
# Phase 1: Non-WAV conversion → always 16-bit 48 kHz WAV
# ============================================================================

def scan_phase_1(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    findings: List[Finding] = []
    candidates = list(iter_files(
        base_dir, skip_clean_folders=only_new,
        extensions=config.NON_WAV_AUDIO_EXTS,
    ))
    if not candidates:
        return findings
    if progress_cb:
        progress_cb(0, len(candidates))
    results = parallel_ffprobe(candidates, cache, progress_cb)
    for path, info in results:
        if info is None:
            continue
        dst = path.with_suffix(".wav")
        if dst.exists():
            continue
        src_desc = f"{path.suffix.upper()[1:]}, {info.sample_rate} Hz, {info.bits}-bit"
        findings.append(Finding(
            phase=1, path=path,
            reason=f"{path.suffix.upper()[1:]} → WAV",
            current=src_desc,
            target="48000 Hz, 16-bit",
        ))
    return findings


def apply_phase_1(finding: Finding) -> bool:
    src = finding.path
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
        try:
            src.unlink()
        except OSError:
            pass
    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 2: Sample rate + bit depth — everything → 48 kHz, 16-bit
# ============================================================================

def scan_phase_2(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find WAV files not at 48 kHz or not at 16-bit."""
    findings: List[Finding] = []
    candidates = list(iter_files(
        base_dir, skip_clean_folders=only_new, extensions={".wav"},
    ))
    if not candidates:
        return findings
    results = parallel_ffprobe(candidates, cache, progress_cb)
    target_sr = int(config.FORCE_AR)
    for path, info in results:
        if info is None:
            continue
        wrong_sr   = info.sample_rate != target_sr
        wrong_bits = info.bits != 16
        if not wrong_sr and not wrong_bits:
            continue
        parts = []
        if wrong_sr:
            parts.append(f"{info.sample_rate} Hz → 48000 Hz")
        if wrong_bits:
            parts.append(f"{info.bits}-bit → 16-bit")
        findings.append(Finding(
            phase=2, path=path,
            reason=", ".join(parts),
            current=f"{info.sample_rate} Hz, {info.bits}-bit",
            target="48000 Hz, 16-bit",
            extra={"wrong_sr": wrong_sr, "wrong_bits": wrong_bits},
        ))
    return findings


def apply_phase_2(finding: Finding) -> bool:
    src = finding.path
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
# Phase 3: Common prefix removal
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


def scan_phase_3(folder: Path) -> Optional[Finding]:
    """Scan a single folder for a removable prefix. Returns one Finding or None."""
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
        phase=3, path=folder,
        reason=f"Shared prefix in {len(file_names)} files",
        current=file_names[0],
        target=file_names[0][len(prefix):] if file_names[0].startswith(prefix) else file_names[0],
        suggested_name=prefix,
        extra={"prefix": prefix, "affected_files": [str(f) for f in files
                                                      if f.name.startswith(prefix)]},
    )


def apply_phase_3(finding: Finding, override_prefix: Optional[str] = None) -> int:
    """Strip prefix from all files in the folder. Returns count of renamed files."""
    prefix = override_prefix if override_prefix is not None else finding.extra.get("prefix", "")
    if not prefix:
        return 0
    folder = finding.path
    affected = finding.extra.get("affected_files", [])
    count = 0
    for path_str in affected:
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
        FolderMarkers.invalidate(folder)
    return count


# ============================================================================
# Phase 4: Long filename cleanup
# ============================================================================

def suggest_short_names(name: str) -> List[str]:
    stem = Path(name).stem
    suffix = Path(name).suffix
    suggestions = []
    s1 = re.sub(r"[_\-\s]+", "", stem)
    if s1 and s1 != stem:
        suggestions.append(s1 + suffix)
    match = re.search(r"(\d+)\s*([a-zA-Z]+)", stem)
    if match:
        suggestions.append(f"{match.group(1)}{match.group(2)}{suffix}")
    if len(stem) > 15:
        suggestions.append("..." + stem[-15:] + suffix)
    words = re.findall(r"[A-Za-z0-9]+", stem)
    if words:
        initials = "".join(w[0] for w in words if w)
        if len(initials) >= 2:
            suggestions.append(initials + suffix)
    seen = set()
    result = []
    for s in suggestions:
        if s not in seen and s != name:
            seen.add(s)
            result.append(s)
    return result


def scan_phase_4(base_dir: Path, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    findings: List[Finding] = []
    all_files = list(iter_files(base_dir, skip_clean_folders=only_new))
    total = len(all_files)
    for i, path in enumerate(all_files):
        if progress_cb and i % 50 == 0:
            progress_cb(i, total)
        stem = path.stem
        if len(stem) > config.NAME_LENGTH_LIMIT:
            suggestions = suggest_short_names(path.name)
            findings.append(Finding(
                phase=4, path=path,
                reason=f"{len(stem)} chars",
                current=path.name,
                target="",
                suggested_name=suggestions[0] if suggestions else "",
                extra={"suggestions": suggestions},
            ))
    if progress_cb:
        progress_cb(total, total)
    return findings


def apply_phase_4(finding: Finding, new_name: str) -> bool:
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
# Phase 5: Stereo → Mono detection
# ============================================================================

@dataclass
class StereoAnalysis:
    max_diff_db: float
    peak_l_db: float
    peak_r_db: float
    classification: str
    keep_channel: str = "L"


def analyze_stereo(path: Path) -> Optional[StereoAnalysis]:
    """Detect whether a 2-channel WAV is mono in disguise via peak dB analysis."""

    def _peak_db(filter_expr: str) -> Optional[float]:
        c = [
            "ffmpeg", "-v", "info", "-nostdin",
            "-i", str(path),
            "-af", filter_expr + ",astats=metadata=1:reset=0",
            "-f", "null", "-",
        ]
        try:
            res = subprocess.run(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=60)
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
        classification = "one_side"
        keep_channel = "L"
    elif peak_r > peak_l + config.STEREO_PEAK_IMBALANCE_DB:
        classification = "one_side"
        keep_channel = "R"
    elif peak_diff <= config.STEREO_STRICT_THRESHOLD_DB:
        classification = "dual_mono"
    elif peak_diff <= config.STEREO_LOOSE_THRESHOLD_DB:
        classification = "near_mono"
    else:
        classification = "true_stereo"

    return StereoAnalysis(
        max_diff_db=peak_diff,
        peak_l_db=peak_l,
        peak_r_db=peak_r,
        classification=classification,
        keep_channel=keep_channel,
    )


def scan_phase_5(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 include_near_mono: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    findings: List[Finding] = []
    candidates = list(iter_files(
        base_dir, skip_clean_folders=only_new, extensions={".wav"},
    ))
    if not candidates:
        return findings

    probe_results = parallel_ffprobe(candidates, cache, progress_cb=None)
    stereo_files = [p for p, info in probe_results
                    if info is not None and info.channels == 2]
    if not stereo_files:
        return findings

    total = len(stereo_files)
    for i, path in enumerate(stereo_files, 1):
        if progress_cb:
            progress_cb(i, total)

        try:
            stat = path.stat()
        except OSError:
            continue
        stereo_key = f"stereo|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
        cached = cache._data.get(stereo_key) if cache else None
        if cached is not None:
            analysis = StereoAnalysis(**cached)
        else:
            analysis = analyze_stereo(path)
            if analysis is not None and cache is not None:
                cache._data[stereo_key] = {
                    "max_diff_db": analysis.max_diff_db,
                    "peak_l_db": analysis.peak_l_db,
                    "peak_r_db": analysis.peak_r_db,
                    "classification": analysis.classification,
                    "keep_channel": analysis.keep_channel,
                }
                cache._dirty = True

        if analysis is None:
            continue

        flag = selected_default = False
        if analysis.classification in ("dual_mono", "one_side"):
            flag = selected_default = True
        elif analysis.classification == "near_mono" and include_near_mono:
            flag = True
        if not flag:
            continue

        try:
            current_size = path.stat().st_size
        except OSError:
            continue
        savings = current_size - current_size // 2

        diff_str = ("-inf dB (identical)" if analysis.max_diff_db == -float("inf")
                    else f"{analysis.max_diff_db:.1f} dB")
        reason_map = {
            "dual_mono": "Channels identical",
            "one_side":  f"Mono in {analysis.keep_channel} only",
            "near_mono": "Channels nearly identical",
        }
        findings.append(Finding(
            phase=5, path=path,
            reason=reason_map.get(analysis.classification, analysis.classification),
            current=f"Stereo ({format_bytes(current_size)}), L-R diff: {diff_str}",
            target=f"Mono ({format_bytes(current_size // 2)})",
            savings_bytes=savings,
            selected=selected_default,
            extra={
                "classification": analysis.classification,
                "keep_channel": analysis.keep_channel,
                "peak_l_db": analysis.peak_l_db,
                "peak_r_db": analysis.peak_r_db,
                "max_diff_db": analysis.max_diff_db,
            },
        ))

    return findings


def apply_phase_5(finding: Finding) -> bool:
    """Convert stereo file to mono."""
    src = finding.path
    keep = finding.extra.get("keep_channel", "mix")
    pan_map = {
        "L":   "mono|c0=c0",
        "R":   "mono|c0=c1",
        "mix": "mono|c0=0.5*c0+0.5*c1",
    }
    pan_expr = pan_map.get(keep, pan_map["mix"])

    tmp = src.with_name(src.stem + ".__tmp__.wav")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-nostdin",
        "-i", str(src),
        "-af", f"pan={pan_expr}",
        "-c:a", config.WAV_CODEC_16,
        "-ar", config.FORCE_AR,
        str(tmp),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=300)
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
# Phase 6: Silence removal
# ============================================================================

def detect_silence_bounds(path: Path, info: Optional[AudioInfo] = None,
                          ) -> Optional[Tuple[float, float]]:
    """Return (leading_silence_s, trailing_silence_s) or None on failure.

    Uses ffmpeg silencedetect. Leading silence = silence starting at t≈0.
    Trailing silence = last silence region reaching to near end of file.
    """
    if info is None:
        info = ffprobe(path)
    if info is None or info.duration <= 0:
        return None

    noise = f"{config.SILENCE_THRESHOLD_DB}dB"
    dur   = str(config.SILENCE_MIN_DURATION)
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(path),
        "-af", f"silencedetect=noise={noise}:duration={dur}",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None

    starts: List[float] = []
    ends:   List[float] = []
    for line in res.stderr.split("\n"):
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[-1].strip()))
            except ValueError:
                pass
        elif "silence_end:" in line:
            try:
                ends.append(float(line.split("silence_end:")[1].split("|")[0].strip()))
            except (ValueError, IndexError):
                pass

    lead = 0.0
    trail = 0.0

    # Leading: first region starts at or before 50 ms
    if starts and starts[0] <= 0.05 and ends:
        lead = ends[0]

    # Trailing: last region reaches to within 100 ms of file end
    if starts:
        last_start = starts[-1]
        last_end = ends[-1] if len(ends) >= len(starts) else info.duration
        if last_end >= info.duration - 0.1 or len(ends) < len(starts):
            # Make sure this isn't also the leading region
            if last_start > lead:
                trail = info.duration - last_start

    return (lead, trail)


def scan_phase_6(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find WAV files with leading or trailing silence above the minimum threshold."""
    findings: List[Finding] = []
    candidates = list(iter_files(
        base_dir, skip_clean_folders=only_new, extensions={".wav"},
    ))
    if not candidates:
        return findings

    total = len(candidates)
    for i, path in enumerate(candidates, 1):
        if progress_cb:
            progress_cb(i, total)

        try:
            stat = path.stat()
        except OSError:
            continue
        silence_key = f"silence|{path}|{stat.st_mtime:.0f}|{stat.st_size}"
        cached = cache._data.get(silence_key) if cache else None

        if cached is not None:
            lead     = cached["lead"]
            trail    = cached["trail"]
            duration = cached["duration"]
        else:
            info = ffprobe(path, cache)
            bounds = detect_silence_bounds(path, info)
            if bounds is None:
                continue
            lead, trail = bounds
            duration = info.duration if info else 0.0
            if cache is not None:
                cache._data[silence_key] = {"lead": lead, "trail": trail,
                                             "duration": duration}
                cache._dirty = True

        if lead < config.SILENCE_MIN_DURATION and trail < config.SILENCE_MIN_DURATION:
            continue

        trimmed = max(0.0, duration - lead - trail)
        savings = int(stat.st_size * (lead + trail) / duration) if duration > 0 else 0

        findings.append(Finding(
            phase=6, path=path,
            reason=(f"Lead {lead:.2f}s  Trail {trail:.2f}s"),
            current=f"{duration:.2f}s",
            target=f"~{trimmed:.2f}s",
            savings_bytes=savings,
            extra={"lead": lead, "trail": trail, "duration": duration},
        ))

    return findings


def apply_phase_6(finding: Finding) -> bool:
    """Trim leading and/or trailing silence from a WAV file."""
    src   = finding.path
    lead  = finding.extra.get("lead", 0.0)
    trail = finding.extra.get("trail", 0.0)

    if lead < config.SILENCE_MIN_DURATION and trail < config.SILENCE_MIN_DURATION:
        return False

    noise_param = f"{config.SILENCE_THRESHOLD_DB}dB"
    dur_param   = str(config.SILENCE_MIN_DURATION)
    sr_param    = f"start_threshold={noise_param}:start_duration={dur_param}:detection=peak"
    base_filter = f"silenceremove=start_periods=1:{sr_param}"

    filters = []
    if lead >= config.SILENCE_MIN_DURATION:
        filters.append(base_filter)
    if trail >= config.SILENCE_MIN_DURATION:
        filters += ["areverse", base_filter, "areverse"]

    tmp = src.with_name(src.stem + ".__tmp__.wav")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-nostdin",
        "-i", str(src),
        "-af", ",".join(filters),
        "-c:a", config.WAV_CODEC_16,
        "-ar", config.FORCE_AR,
        str(tmp),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=300)
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
# Folder marker bookkeeping
# ============================================================================

def mark_folders_processed(base_dir: Path) -> int:
    count = 0
    for root, dirs, _ in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in config.EXCLUDED_FOLDER_NAMES
                   and not d.startswith(".")]
        FolderMarkers.mark_folder(Path(root))
        count += 1
    return count


# ============================================================================
# Logging setup
# ============================================================================

def setup_logging(base_dir: Path, verbose: bool = False) -> None:
    log_file = base_dir / config.LOG_FILE_NAME
    fmt  = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
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
