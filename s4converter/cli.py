"""Command-line interface for S-4 Sample Converter.

Usage:
    python -m s4converter.cli
    python -m s4converter.cli --path /Volumes/S-4/SAMPLES
    python -m s4converter.cli --phases 1,3
    python -m s4converter.cli --quick          # phase 1 only, no prompts
    python -m s4converter.cli --dry-run        # scan + report, change nothing
"""

import argparse
import sys
from pathlib import Path
from typing import List

from . import config, core
from .cache import FolderMarkers, ProbeCache


# --- Terminal colors ---
class C:
    HEADER = "\033[95m"
    BLUE   = "\033[94m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    END    = "\033[0m"
    BOLD   = "\033[1m"


def ask(prompt: str) -> bool:
    return input(f"{C.YELLOW}{prompt} (yes/no): {C.END}").strip().lower() in ("y", "yes")


def progress_printer(label: str):
    state = {"last": -1}

    def cb(done: int, total: int):
        if total == 0:
            return
        pct = int(done * 100 / total)
        if pct != state["last"]:
            state["last"] = pct
            sys.stdout.write(f"\r  {label}: {done}/{total} ({pct}%)")
            sys.stdout.flush()
            if done == total:
                sys.stdout.write("\n")
    return cb


def run_phase_1(base_dir: Path, cache: ProbeCache, only_new: bool, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 1: Non-WAV Conversion ==={C.END}")
    print("Scanning for non-WAV audio files...")
    findings = core.scan_phase_1(base_dir, cache, only_new, progress_printer("scan"))

    if not findings:
        print(f"{C.GREEN}No non-WAV files found.{C.END}")
        return

    print(f"\nFound {len(findings)} files to convert:")
    for f in findings[:10]:
        print(f"  - {f.path.name}  ({f.current} -> {f.target})")
    if len(findings) > 10:
        print(f"  ... and {len(findings) - 10} more")

    if dry_run:
        print(f"{C.YELLOW}[dry-run] Would convert {len(findings)} files.{C.END}")
        return

    if not ask(f"Convert all {len(findings)} files to 16-bit 48 kHz WAV?"):
        return

    ok = fail = 0
    for i, f in enumerate(findings, 1):
        sys.stdout.write(f"\r  Converting: {i}/{len(findings)}")
        sys.stdout.flush()
        if core.apply_phase_1(f):
            ok += 1
        else:
            fail += 1
    print(f"\n{C.GREEN}Done: {ok} converted, {fail} failed.{C.END}")


def run_phase_2(base_dir: Path, cache: ProbeCache, only_new: bool, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 2: Sample Rate + Bit Depth ==={C.END}")
    if not ask("Scan WAV files for wrong sample rate or bit depth?"):
        return

    print("Scanning...")
    findings = core.scan_phase_2(base_dir, cache, only_new, progress_printer("scan"))

    if not findings:
        print(f"{C.GREEN}All WAVs already at 48 kHz / 16-bit.{C.END}")
        return

    print(f"\nFound {len(findings)} files needing conversion:")
    for f in findings[:10]:
        print(f"  - {f.path.name}  ({f.current} -> {f.target})")
    if len(findings) > 10:
        print(f"  ... and {len(findings) - 10} more")

    if dry_run:
        print(f"{C.YELLOW}[dry-run] Would convert {len(findings)} files.{C.END}")
        return

    if not ask(f"Convert all {len(findings)} files to 48 kHz / 16-bit?"):
        return

    ok = fail = 0
    for i, f in enumerate(findings, 1):
        sys.stdout.write(f"\r  Converting: {i}/{len(findings)}")
        sys.stdout.flush()
        if core.apply_phase_2(f):
            ok += 1
        else:
            fail += 1
    print(f"\n{C.GREEN}Done: {ok} converted, {fail} failed.{C.END}")


def run_phase_3(base_dir: Path, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 3: Prefix Removal ==={C.END}")
    if not ask("Start interactive prefix cleanup?"):
        return

    while True:
        raw = input(f"\n{C.BLUE}Enter folder path (or 'q' to quit): {C.END}").strip()
        if raw.lower() == "q":
            break

        raw = raw.strip("'\"").replace("\\ ", " ")
        folder = Path(raw).expanduser().resolve()
        if not folder.is_dir():
            print(f"{C.RED}Not a folder: {folder}{C.END}")
            continue

        finding = core.scan_phase_3(folder)
        if not finding:
            print(f"{C.YELLOW}No clear prefix detected. Enter manual prefix? (or empty to skip){C.END}")
            manual = input(f"{C.BLUE}Prefix: {C.END}").strip()
            if not manual:
                continue
            try:
                files = [f for f in folder.iterdir()
                         if f.is_file() and f.name.startswith(manual)]
            except OSError:
                continue
            if not files:
                print(f"{C.RED}No files match that prefix.{C.END}")
                continue
            finding = core.Finding(
                phase=3, path=folder,
                reason="manual prefix",
                extra={"prefix": manual, "affected_files": [str(f) for f in files]},
            )

        prefix   = finding.extra["prefix"]
        affected = finding.extra["affected_files"]
        example  = Path(affected[0]).name
        print(f"{C.GREEN}Prefix:{C.END} '{prefix}'  ({len(affected)} files)")
        print(f"  Example: {example} -> {example[len(prefix):]}")

        if dry_run:
            print(f"{C.YELLOW}[dry-run] Would rename {len(affected)} files.{C.END}")
            continue

        choice = input(f"{C.YELLOW}Strip this prefix? (yes/no/edit): {C.END}").strip().lower()
        if choice == "edit":
            new_prefix = input(f"{C.BLUE}New prefix: {C.END}")
            count = core.apply_phase_3(finding, override_prefix=new_prefix)
        elif choice == "yes":
            count = core.apply_phase_3(finding)
        else:
            continue
        print(f"{C.GREEN}Renamed {count} files.{C.END}")

        if not ask("Another folder?"):
            break


def run_phase_4(base_dir: Path, only_new: bool, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 4: Long Filename Cleanup ==={C.END}")
    if not ask(f"Scan for stems longer than {config.NAME_LENGTH_LIMIT} chars?"):
        return

    print("Scanning...")
    findings = core.scan_phase_4(base_dir, only_new, progress_printer("scan"))

    if not findings:
        print(f"{C.GREEN}No long filenames found.{C.END}")
        return

    print(f"\nFound {len(findings)} long names.")
    if dry_run:
        for f in findings[:20]:
            print(f"  - ({f.reason}) {f.current}")
        print(f"{C.YELLOW}[dry-run] Would prompt for {len(findings)} renames.{C.END}")
        return

    for f in findings:
        print(f"\n{C.RED}({f.reason}){C.END} {f.current}")
        print(f"  in: {f.path.parent}")
        for i, s in enumerate(f.extra.get("suggestions", []), 1):
            print(f"  {i}. {s}")
        print("  [Enter] to skip, number to pick, or type a new name")
        choice = input(f"{C.YELLOW}> {C.END}").strip()
        if not choice:
            continue
        suggestions = f.extra.get("suggestions", [])
        if choice.isdigit() and 1 <= int(choice) <= len(suggestions):
            new_name = suggestions[int(choice) - 1]
        else:
            new_name = choice
        if core.apply_phase_4(f, new_name):
            print(f"  {C.GREEN}Renamed{C.END}")
        else:
            print(f"  {C.RED}Failed (target exists or rename error){C.END}")


def run_phase_5(base_dir: Path, cache: ProbeCache, only_new: bool, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 5: Stereo → Mono Detection ==={C.END}")
    if not ask("Scan stereo WAVs for fake-stereo (identical L/R) files?"):
        return

    loose = ask("Also include 'near-mono' files (loose mode, opt-in per file)?")
    print("Scanning (analyzes every stereo file — may take a while)...")
    findings = core.scan_phase_5(base_dir, cache, only_new=only_new,
                                  include_near_mono=loose,
                                  progress_cb=progress_printer("analyze"))

    if not findings:
        print(f"{C.GREEN}No fake-stereo files found.{C.END}")
        return

    by_class: dict = {}
    for f in findings:
        by_class.setdefault(f.extra.get("classification", "?"), []).append(f)

    total_savings = sum(f.savings_bytes for f in findings if f.selected)
    print(f"\nFound {len(findings)} fake-stereo files. "
          f"Selected by default: {sum(1 for f in findings if f.selected)} "
          f"(savings: {core.format_bytes(total_savings)})")

    for cls, items in by_class.items():
        pretty = {
            "dual_mono": "Dual mono (L = R)",
            "one_side":  "One-sided (silent channel)",
            "near_mono": "Near-mono (faint stereo width)",
        }.get(cls, cls)
        print(f"\n  {C.BOLD}{pretty}{C.END}: {len(items)} files")
        for f in items[:5]:
            print(f"    [{'✓' if f.selected else ' '}] {f.path.name}")
        if len(items) > 5:
            print(f"    ... and {len(items) - 5} more")

    if dry_run:
        print(f"\n{C.YELLOW}[dry-run] Would convert "
              f"{sum(1 for f in findings if f.selected)} files to mono.{C.END}")
        return

    if loose and any(f.extra.get("classification") == "near_mono" for f in findings):
        if ask("Also convert near-mono files?"):
            for f in findings:
                if f.extra.get("classification") == "near_mono":
                    f.selected = True

    selected = [f for f in findings if f.selected]
    if not selected:
        print(f"{C.YELLOW}Nothing selected, skipping.{C.END}")
        return

    if not ask(f"Convert {len(selected)} files to mono?"):
        return

    ok = fail = 0
    for i, f in enumerate(selected, 1):
        sys.stdout.write(f"\r  Converting: {i}/{len(selected)}")
        sys.stdout.flush()
        if core.apply_phase_5(f):
            ok += 1
        else:
            fail += 1
    print(f"\n{C.GREEN}Done: {ok} converted, {fail} failed.{C.END}")


def run_phase_6(base_dir: Path, cache: ProbeCache, only_new: bool, dry_run: bool):
    print(f"\n{C.HEADER}=== PHASE 6: Silence Removal ==={C.END}")
    if not ask("Scan WAV files for leading/trailing silence?"):
        return

    print(f"Scanning (threshold: {config.SILENCE_THRESHOLD_DB} dBFS, "
          f"min: {config.SILENCE_MIN_DURATION}s)...")
    findings = core.scan_phase_6(base_dir, cache, only_new,
                                  progress_cb=progress_printer("scan"))

    if not findings:
        print(f"{C.GREEN}No significant silence found.{C.END}")
        return

    total_savings = sum(f.savings_bytes for f in findings)
    print(f"\nFound {len(findings)} files with leading/trailing silence. "
          f"Est. savings: {core.format_bytes(total_savings)}")
    for f in findings[:10]:
        print(f"  - {f.path.name}  ({f.reason})  {f.current} → {f.target}")
    if len(findings) > 10:
        print(f"  ... and {len(findings) - 10} more")

    if dry_run:
        print(f"{C.YELLOW}[dry-run] Would trim {len(findings)} files.{C.END}")
        return

    if not ask(f"Trim silence from all {len(findings)} files?"):
        return

    ok = fail = 0
    for i, f in enumerate(findings, 1):
        sys.stdout.write(f"\r  Trimming: {i}/{len(findings)}")
        sys.stdout.flush()
        if core.apply_phase_6(f):
            ok += 1
        else:
            fail += 1
    print(f"\n{C.GREEN}Done: {ok} trimmed, {fail} failed.{C.END}")


def main():
    parser = argparse.ArgumentParser(description="Torso S-4 Smart Sample Converter")
    parser.add_argument("--path", type=Path, default=config.DEFAULT_BASE_DIR,
                        help="Base directory containing samples")
    parser.add_argument("--phases", type=str, default="1,2,3,4,5,6",
                        help="Comma-separated phases to run (e.g. 1,2,5)")
    parser.add_argument("--quick", action="store_true",
                        help="Phase 1 only, no prompts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report only, change nothing")
    parser.add_argument("--full-scan", action="store_true",
                        help="Ignore folder markers, scan everything")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    base_dir = args.path.expanduser().resolve()
    if not core.check_drive_present(base_dir):
        print(f"{C.RED}Error: {base_dir} not found or not a directory.{C.END}")
        raise SystemExit(1)

    core.setup_logging(base_dir, verbose=args.verbose)
    cache = ProbeCache(base_dir)

    print(f"{C.BOLD}S-4 Sample Converter{C.END}")
    print(f"Target: {base_dir}")
    print(f"Cache:  {cache.size()} entries")

    only_new = not args.full_scan
    if only_new:
        print(f"{C.BLUE}Incremental scan (use --full-scan to override){C.END}")

    if args.quick:
        run_phase_1(base_dir, cache, only_new=True, dry_run=args.dry_run)
        cache.save()
        if not args.dry_run:
            core.mark_folders_processed(base_dir)
        return

    phases = {int(p.strip()) for p in args.phases.split(",") if p.strip().isdigit()}

    try:
        if 1 in phases:
            run_phase_1(base_dir, cache, only_new, args.dry_run)
        if 2 in phases:
            run_phase_2(base_dir, cache, only_new, args.dry_run)
        if 3 in phases:
            run_phase_3(base_dir, args.dry_run)
        if 4 in phases:
            run_phase_4(base_dir, only_new, args.dry_run)
        if 5 in phases:
            run_phase_5(base_dir, cache, only_new, args.dry_run)
        if 6 in phases:
            run_phase_6(base_dir, cache, only_new, args.dry_run)
    finally:
        cache.save()
        if not args.dry_run:
            print(f"\n{C.BLUE}Updating folder markers...{C.END}")
            n = core.mark_folders_processed(base_dir)
            print(f"Marked {n} folders.")

    print(f"\n{C.BOLD}{C.GREEN}All phases complete.{C.END}")


if __name__ == "__main__":
    main()
