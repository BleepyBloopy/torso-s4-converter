"""Core scanning and conversion logic for S-4 Sample Converter.

This module is UI-agnostic: pure functions return data, callers (CLI or GUI)
decide how to display and what actions to apply.

Each phase has two functions:
    scan_phase_N(...) -> list of finding dicts
    apply_phase_N_action(finding) -> bool (success)

This separation enables dry-run, batch preview, and per-file approval.
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
    current: str = ""           # e.g. "44100 Hz, 24-bit"
    target: str = ""            # e.g. "48000 Hz, 16-bit"
    savings_bytes: int = 0
    suggested_name: str = ""    # for rename phases
    extra: dict = field(default_factory=dict)
    selected: bool = True       # whether user wants to apply this action


# ============================================================================
# Helpers
# ============================================================================

def format_bytes(size: int) -> str:
    """Human-readable byte size."""
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
        # Prune excluded folder names
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
    """Run ffprobe on many files in parallel. Returns list of (path, info)."""
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


def decide_best_practice_bits(duration_s: float, src_bits: int) -> int:
    if duration_s > config.THRESHOLD_SECONDS:
        return 16
    return 24 if src_bits > 16 else 16


def convert_to_wav(src: Path, dst: Path, target_bits: int,
                   target_sr: Optional[str] = None) -> bool:
    """Convert (or re-encode) audio to WAV. Returns True on success."""
    target_sr = target_sr or config.FORCE_AR
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if config.COPY_METADATA:
        cmd += ["-map_metadata", "0"]
    if target_sr:
        cmd += ["-ar", target_sr]
    codec = config.WAV_CODEC_24 if target_bits >= 24 else config.WAV_CODEC_16
    cmd += ["-f", "wav", "-c:a", codec, str(dst)]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=300)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def atomic_replace(src: Path, target: Path) -> bool:
    """Replace target with src atomically."""
    try:
        os.replace(src, target)
        return True
    except OSError:
        return False


def check_drive_present(base_dir: Path) -> bool:
    """Verify the drive is still mounted. Important for external drives."""
    return base_dir.exists() and base_dir.is_dir()


# ============================================================================
# Phase 1: Non-WAV conversion
# ============================================================================

def scan_phase_1(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find non-WAV audio files needing conversion."""
    findings: List[Finding] = []

    candidates = list(iter_files(
        base_dir,
        skip_clean_folders=only_new,
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
        target_bits = decide_best_practice_bits(info.duration, info.bits)
        dst = path.with_suffix(".wav")
        if dst.exists():
            continue
        findings.append(Finding(
            phase=1,
            path=path,
            reason=f"{path.suffix.upper()[1:]} -> WAV",
            current=f"{info.sample_rate} Hz, {info.bits}-bit, {info.duration:.1f}s",
            target=f"48000 Hz, {target_bits}-bit",
            extra={"target_bits": target_bits},
        ))

    return findings


def apply_phase_1(finding: Finding) -> bool:
    src = finding.path
    dst = src.with_suffix(".wav")
    target_bits = finding.extra.get("target_bits", 16)

    if dst.exists():
        return False

    tmp = dst.with_name(dst.stem + ".__tmp__.wav")
    if not convert_to_wav(src, tmp, target_bits):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    if not atomic_replace(tmp, dst):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    if config.DELETE_ORIGINAL:
        try:
            src.unlink()
        except OSError:
            pass

    # Invalidate the folder marker since we changed contents
    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 2: Sample rate compliance (force 48kHz)
# ============================================================================

def scan_phase_2(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find WAV files not at 48kHz."""
    findings: List[Finding] = []

    candidates = list(iter_files(
        base_dir,
        skip_clean_folders=only_new,
        extensions={".wav"},
    ))

    if not candidates:
        return findings

    results = parallel_ffprobe(candidates, cache, progress_cb)

    target_sr = int(config.FORCE_AR)
    for path, info in results:
        if info is None:
            continue
        if info.sample_rate != target_sr:
            findings.append(Finding(
                phase=2,
                path=path,
                reason=f"Sample rate {info.sample_rate} Hz",
                current=f"{info.sample_rate} Hz, {info.bits}-bit",
                target=f"48000 Hz, {info.bits}-bit",
                extra={"target_bits": info.bits, "current_sr": info.sample_rate},
            ))

    return findings


def apply_phase_2(finding: Finding) -> bool:
    src = finding.path
    target_bits = finding.extra.get("target_bits", 16)
    tmp = src.with_name(src.stem + ".__tmp__.wav")

    if not convert_to_wav(src, tmp, target_bits):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    if not atomic_replace(tmp, src):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 3: Bit depth optimization (24-bit -> 16-bit for long files)
# ============================================================================

def scan_phase_3(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find 24-bit files >10s that should be 16-bit."""
    findings: List[Finding] = []

    candidates = list(iter_files(
        base_dir,
        skip_clean_folders=only_new,
        extensions={".wav"},
    ))

    if not candidates:
        return findings

    results = parallel_ffprobe(candidates, cache, progress_cb)

    for path, info in results:
        if info is None:
            continue
        if info.bits > 16 and info.duration > config.THRESHOLD_SECONDS:
            try:
                current_size = path.stat().st_size
            except OSError:
                continue
            estimated_new = int(current_size * 16 / info.bits)
            savings = current_size - estimated_new
            findings.append(Finding(
                phase=3,
                path=path,
                reason=f"{info.bits}-bit / {info.duration:.1f}s",
                current=f"{info.bits}-bit ({format_bytes(current_size)})",
                target=f"16-bit (~{format_bytes(estimated_new)})",
                savings_bytes=savings,
                extra={"target_bits": 16},
            ))

    return findings


def apply_phase_3(finding: Finding) -> bool:
    src = finding.path
    tmp = src.with_name(src.stem + ".__tmp__.wav")

    if not convert_to_wav(src, tmp, target_bits=16):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    if not atomic_replace(tmp, src):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Phase 4: Common prefix removal
# ============================================================================

def detect_common_prefix(filenames: List[str]) -> str:
    """Detect a meaningful shared prefix among filenames."""
    if len(filenames) < 2:
        return ""

    p = os.path.commonprefix(filenames)
    if len(p) < config.MIN_PREFIX_LENGTH:
        return ""

    # Trim at the last sensible separator so we don't cut a word in half
    for sep in [" - ", "_-_", "_", " ", "-"]:
        if sep in p:
            idx = p.rfind(sep)
            if idx >= config.MIN_PREFIX_LENGTH - len(sep):
                return p[:idx + len(sep)]

    return p if len(p) >= config.MIN_PREFIX_LENGTH else ""


def scan_phase_4(folder: Path) -> Optional[Finding]:
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

    # Skip if all names are already reasonably short
    if all(len(n) <= config.PREFIX_SKIP_LENGTH for n in file_names):
        return None

    return Finding(
        phase=4,
        path=folder,
        reason=f"Shared prefix in {len(file_names)} files",
        current=file_names[0],
        target=file_names[0][len(prefix):] if file_names[0].startswith(prefix) else file_names[0],
        suggested_name=prefix,
        extra={"prefix": prefix, "affected_files": [str(f) for f in files
                                                      if f.name.startswith(prefix)]},
    )


def apply_phase_4(finding: Finding, override_prefix: Optional[str] = None) -> int:
    """Strip prefix from all files in the affected list.
    
    Returns count of successfully renamed files.
    """
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
# Phase 5: Long filename cleanup
# ============================================================================

def suggest_short_names(name: str) -> List[str]:
    """Generate shorter name candidates for a given filename."""
    stem = Path(name).stem
    suffix = Path(name).suffix
    suggestions = []

    # 1. Strip separator chars
    s1 = re.sub(r"[_\-\s]+", "", stem)
    if s1 and s1 != stem:
        suggestions.append(s1 + suffix)

    # 2. BPM + style pattern (e.g. "89 Tekno")
    match = re.search(r"(\d+)\s*([a-zA-Z]+)", stem)
    if match:
        suggestions.append(f"{match.group(1)}{match.group(2)}{suffix}")

    # 3. Last 15 chars
    if len(stem) > 15:
        suggestions.append("..." + stem[-15:] + suffix)

    # 4. Initials of words
    words = re.findall(r"[A-Za-z0-9]+", stem)
    if words:
        initials = "".join(w[0] for w in words if w)
        if len(initials) >= 2:
            suggestions.append(initials + suffix)

    # Dedupe while preserving order
    seen = set()
    result = []
    for s in suggestions:
        if s not in seen and s != name:
            seen.add(s)
            result.append(s)
    return result


def scan_phase_5(base_dir: Path, only_new: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find files whose stem exceeds NAME_LENGTH_LIMIT chars."""
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
                phase=5,
                path=path,
                reason=f"{len(stem)} chars",
                current=path.name,
                target="",
                suggested_name=suggestions[0] if suggestions else "",
                extra={"suggestions": suggestions},
            ))

    if progress_cb:
        progress_cb(total, total)
    return findings


def apply_phase_5(finding: Finding, new_name: str) -> bool:
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
# Phase 6: Stereo -> Mono Detection
# ============================================================================

@dataclass
class StereoAnalysis:
    """Result of analyzing a stereo file's L/R relationship."""
    max_diff_db: float          # max |L - R| in dBFS (-inf for identical)
    peak_l_db: float            # peak level of left channel in dBFS
    peak_r_db: float            # peak level of right channel in dBFS
    classification: str         # 'dual_mono' | 'near_mono' | 'one_side' | 'true_stereo'
    keep_channel: str = "L"     # 'L', 'R', or 'mix' (which channel to keep when converting)


def analyze_stereo(path: Path) -> Optional[StereoAnalysis]:
    """Detect whether a 2-channel WAV is actually mono in disguise.

    Uses ffmpeg's astats filter to compute peak levels of:
    - The L-R difference signal (revealed by amerge then channel subtraction)
    - Each channel independently

    Returns StereoAnalysis or None on failure.
    """
    # Build a filter chain that:
    # 1. Splits the stereo input into L and R mono streams
    # 2. Computes (L - R) for the diff signal
    # 3. Runs astats on each so we get peak levels
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(path),
        "-filter_complex",
        # Take stream 0, split to L and R; subtract for diff
        "[0:a]channelsplit=channel_layout=stereo[L][R];"
        "[L]astats=metadata=1:reset=0,ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-[lstats];"
        "[R]astats=metadata=1:reset=0,ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-[rstats];"
        "[lstats][rstats]amerge=inputs=2[merged];"
        "[merged]pan=mono|c0=c0-c1,astats=metadata=1:reset=0,ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-",
        "-f", "null", "-",
    ]

    # The filter chain above is fragile across ffmpeg versions. Use a simpler approach:
    # run three astats invocations - L peak, R peak, and (L-R) peak.

    def _peak_db(filter_expr: str) -> Optional[float]:
        """Run ffmpeg with given filter, parse Peak_level from astats output."""
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

        # astats writes to stderr like: "[Parsed_astats_0 @ 0x...] Peak level dB: -23.45"
        # If the channel is silent: "Peak level dB: -inf"
        output = res.stderr
        peak = None
        for line in output.split("\n"):
            if "Peak level dB:" in line:
                val = line.split("Peak level dB:")[-1].strip()
                if val == "-inf":
                    peak = -float("inf")
                else:
                    try:
                        peak = float(val)
                    except ValueError:
                        pass
                # First (Overall) peak is what we want; keep first hit per channel
                if peak is not None:
                    break
        return peak

    peak_l = _peak_db("pan=mono|c0=c0")          # left channel only
    peak_r = _peak_db("pan=mono|c0=c1")          # right channel only
    peak_diff = _peak_db("pan=mono|c0=c0-c1")    # L - R difference

    if peak_l is None or peak_r is None or peak_diff is None:
        return None

    # Classification
    keep_channel = "mix"  # default: average L+R

    # Check for one-sided file (almost all energy in one channel)
    if peak_l > peak_r + config.STEREO_PEAK_IMBALANCE_DB:
        classification = "one_side"
        keep_channel = "L"
    elif peak_r > peak_l + config.STEREO_PEAK_IMBALANCE_DB:
        classification = "one_side"
        keep_channel = "R"
    elif peak_diff <= config.STEREO_STRICT_THRESHOLD_DB:
        classification = "dual_mono"
        keep_channel = "mix"  # they're identical, mix is fine
    elif peak_diff <= config.STEREO_LOOSE_THRESHOLD_DB:
        classification = "near_mono"
        keep_channel = "mix"
    else:
        classification = "true_stereo"

    return StereoAnalysis(
        max_diff_db=peak_diff,
        peak_l_db=peak_l,
        peak_r_db=peak_r,
        classification=classification,
        keep_channel=keep_channel,
    )


def scan_phase_6(base_dir: Path, cache: ProbeCache, only_new: bool = False,
                 include_near_mono: bool = False,
                 progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> List[Finding]:
    """Find 2-channel WAVs that are effectively mono.

    Workflow:
    1. ffprobe everything (fast, cached) to get channel counts
    2. For each 2-channel file, run analyze_stereo (slower, also cached separately)
    3. Classify and produce findings

    If include_near_mono is False (default): only dual_mono + one_side are flagged.
    If True: also flag near_mono files (offered with checkbox UNCHECKED by default).
    """
    findings: List[Finding] = []

    # First pass: find all 2-channel WAVs (use existing probe cache)
    candidates = list(iter_files(
        base_dir,
        skip_clean_folders=only_new,
        extensions={".wav"},
    ))

    if not candidates:
        return findings

    # ffprobe everything in parallel; filter to stereo
    probe_results = parallel_ffprobe(candidates, cache, progress_cb=None)
    stereo_files = [p for p, info in probe_results
                    if info is not None and info.channels == 2]

    if not stereo_files:
        return findings

    # Second pass: run stereo analysis (per-file cache via cache._data under a separate key prefix)
    total = len(stereo_files)
    for i, path in enumerate(stereo_files, 1):
        if progress_cb:
            progress_cb(i, total)

        # Try cache first (stereo analysis is keyed separately)
        stereo_cache_key = f"stereo|{path}|{path.stat().st_mtime:.0f}|{path.stat().st_size}"
        cached = cache._data.get(stereo_cache_key) if cache else None
        if cached is not None:
            analysis = StereoAnalysis(**cached)
        else:
            analysis = analyze_stereo(path)
            if analysis is not None and cache is not None:
                cache._data[stereo_cache_key] = {
                    "max_diff_db": analysis.max_diff_db,
                    "peak_l_db": analysis.peak_l_db,
                    "peak_r_db": analysis.peak_r_db,
                    "classification": analysis.classification,
                    "keep_channel": analysis.keep_channel,
                }
                cache._dirty = True

        if analysis is None:
            continue

        # Decide whether to flag and whether to pre-select
        flag = False
        selected_default = False

        if analysis.classification == "dual_mono":
            flag = True
            selected_default = True
        elif analysis.classification == "one_side":
            flag = True
            selected_default = True
        elif analysis.classification == "near_mono" and include_near_mono:
            flag = True
            selected_default = False  # flagged but user must opt in
        # true_stereo never flagged

        if not flag:
            continue

        try:
            current_size = path.stat().st_size
        except OSError:
            continue
        estimated_new = current_size // 2
        savings = current_size - estimated_new

        # Human-readable diff
        if analysis.max_diff_db == -float("inf"):
            diff_str = "-inf dB (identical)"
        else:
            diff_str = f"{analysis.max_diff_db:.1f} dB"

        reason_map = {
            "dual_mono": "Channels identical",
            "one_side":  f"Mono in {analysis.keep_channel} only",
            "near_mono": "Channels nearly identical",
        }

        findings.append(Finding(
            phase=6,
            path=path,
            reason=reason_map.get(analysis.classification, analysis.classification),
            current=f"Stereo ({format_bytes(current_size)}), L-R diff: {diff_str}",
            target=f"Mono ({format_bytes(estimated_new)})",
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


def apply_phase_6(finding: Finding) -> bool:
    """Convert stereo file to mono. Keep_channel determines how."""
    src = finding.path
    keep = finding.extra.get("keep_channel", "mix")

    # ffmpeg pan filter expression
    if keep == "L":
        pan_expr = "mono|c0=c0"
    elif keep == "R":
        pan_expr = "mono|c0=c1"
    else:  # mix - average both channels
        pan_expr = "mono|c0=0.5*c0+0.5*c1"

    # Determine the target bit depth from the existing file
    info = ffprobe(src)
    target_bits = info.bits if info else 16
    if target_bits > 24:  # float formats - downcast to 24
        target_bits = 24
    codec = config.WAV_CODEC_24 if target_bits >= 24 else config.WAV_CODEC_16

    tmp = src.with_name(src.stem + ".__tmp__.wav")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-nostdin",
        "-i", str(src),
        "-af", f"pan={pan_expr}",
        "-c:a", codec,
        "-ar", config.FORCE_AR,
        str(tmp),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=300)
        if res.returncode != 0:
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            return False
    except (subprocess.TimeoutExpired, OSError):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    if not atomic_replace(tmp, src):
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        return False

    FolderMarkers.invalidate(src.parent)
    return True


# ============================================================================
# Folder marker bookkeeping
# ============================================================================

def mark_folders_processed(base_dir: Path) -> int:
    """Drop a .s4_processed marker in every folder under base_dir.
    
    Call after a successful full scan + apply cycle.
    Returns count of folders marked.
    """
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
    """Configure logging to file and console."""
    log_file = base_dir / config.LOG_FILE_NAME

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Clear any existing handlers
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
