"""PyQt6 GUI for S-4 Sample Converter.

Tab-per-phase interface with tables of findings.
Each row has a checkbox; select what to apply, then click Apply.

Run with:
    python -m s4_converter.gui
"""

import sys
from pathlib import Path
from typing import List, Optional

try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
    from PyQt6.QtGui import QAction, QFont
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QLineEdit, QFileDialog, QTabWidget,
        QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
        QProgressBar, QPlainTextEdit, QMessageBox, QStatusBar,
        QSplitter, QInputDialog, QComboBox,
    )
except ImportError:
    print("PyQt6 not installed. Install with:")
    print("  pip install PyQt6")
    sys.exit(1)

from . import config, core
from .cache import FolderMarkers, ProbeCache

# Preset paths for the drive dropdown (label, path-or-None-for-custom)
PATH_PRESETS = [
    ("USB  –  /Volumes/S-4/SAMPLES", "/Volumes/S-4/SAMPLES"),
    ("S-4 Root  –  /Volumes/S-4",    "/Volumes/S-4"),
    ("Custom…",                        None),
]


# ============================================================================
# Worker threads (so the UI stays responsive during scans)
# ============================================================================

class ScanWorker(QObject):
    """Run a scan in a background thread."""
    progress = pyqtSignal(int, int)         # done, total
    finished = pyqtSignal(list)              # List[Finding]
    error = pyqtSignal(str)

    def __init__(self, scan_fn, *args):
        super().__init__()
        self.scan_fn = scan_fn
        self.args = args

    def run(self):
        try:
            findings = self.scan_fn(*self.args, progress_cb=self.progress.emit)
            self.finished.emit(findings)
        except Exception as e:
            self.error.emit(str(e))


class ApplyWorker(QObject):
    """Apply actions in a background thread."""
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, int)          # ok, fail
    error = pyqtSignal(str)

    def __init__(self, apply_fn, findings, extra_args=None):
        super().__init__()
        self.apply_fn = apply_fn
        self.findings = findings
        self.extra_args = extra_args or {}

    def run(self):
        ok = fail = 0
        total = len(self.findings)
        try:
            for i, f in enumerate(self.findings, 1):
                if self.extra_args.get("new_names"):
                    result = self.apply_fn(f, self.extra_args["new_names"].get(id(f), ""))
                elif self.extra_args.get("prefixes"):
                    result = self.apply_fn(f, override_prefix=self.extra_args["prefixes"].get(id(f)))
                    result = bool(result)
                else:
                    result = self.apply_fn(f)
                if result:
                    ok += 1
                else:
                    fail += 1
                self.progress.emit(i, total)
            self.finished.emit(ok, fail)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# Findings table widget
# ============================================================================

class FindingsTable(QTableWidget):
    """Table that displays findings with a checkbox per row."""

    def __init__(self, columns: List[str], editable_col: Optional[int] = None):
        super().__init__()
        self.columns = ["✓"] + columns
        self.editable_col = editable_col  # index into self.columns (after the checkbox col)
        self.findings: List[core.Finding] = []

        self.setColumnCount(len(self.columns))
        self.setHorizontalHeaderLabels(self.columns)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
            if editable_col is not None else QTableWidget.EditTrigger.NoEditTriggers
        )

    def set_findings(self, findings: List[core.Finding], row_builder):
        """row_builder(finding) -> list of strings (one per non-checkbox column)."""
        self.findings = findings
        self.setRowCount(len(findings))

        for row, f in enumerate(findings):
            # Checkbox column
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if f.selected else Qt.CheckState.Unchecked)
            self.setItem(row, 0, chk)

            # Data columns
            for col, value in enumerate(row_builder(f), start=1):
                item = QTableWidgetItem(str(value))
                if self.editable_col is None or col != self.editable_col:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(row, col, item)

        self.resizeColumnsToContents()

    def get_selected_findings(self) -> List[core.Finding]:
        """Sync checkbox state back to findings, return those that are checked."""
        selected = []
        for row, f in enumerate(self.findings):
            checked = self.item(row, 0).checkState() == Qt.CheckState.Checked
            f.selected = checked
            if checked:
                selected.append(f)
        return selected

    def get_edit_value(self, finding: core.Finding) -> str:
        """Get the value from the editable column for the given finding."""
        if self.editable_col is None:
            return ""
        try:
            row = self.findings.index(finding)
            return self.item(row, self.editable_col).text()
        except (ValueError, AttributeError):
            return ""

    def select_all(self, checked: bool):
        for row in range(self.rowCount()):
            self.item(row, 0).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )


# ============================================================================
# Phase tabs
# ============================================================================

class PhaseTab(QWidget):
    """Base class for a phase tab."""

    def __init__(self, main_window, phase_num: int, title: str, description: str,
                 help_text: str = ""):
        super().__init__()
        self.main_window = main_window
        self.phase_num = phase_num
        self.title = title
        self._help_text = help_text
        self.findings: List[core.Finding] = []
        self.thread: Optional[QThread] = None

        layout = QVBoxLayout(self)

        # Description
        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(desc_label)

        # Toolbar
        toolbar = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 Scan")
        self.scan_btn.clicked.connect(self.start_scan)
        toolbar.addWidget(self.scan_btn)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(lambda: self.table.select_all(True))
        toolbar.addWidget(self.select_all_btn)

        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(lambda: self.table.select_all(False))
        toolbar.addWidget(self.select_none_btn)

        toolbar.addStretch()

        self.count_label = QLabel("0 findings")
        toolbar.addWidget(self.count_label)

        self.apply_btn = QPushButton("✓ Apply Selected")
        self.apply_btn.clicked.connect(self.start_apply)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet("background-color: #2c7a3d; color: white; padding: 6px 12px;")
        toolbar.addWidget(self.apply_btn)

        if help_text:
            help_btn = QPushButton("ℹ")
            help_btn.setFixedWidth(32)
            help_btn.setToolTip("Phase help & limits")
            help_btn.clicked.connect(self._show_help)
            toolbar.addWidget(help_btn)

        layout.addLayout(toolbar)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Table (subclasses build this)
        self.table = self.build_table()
        layout.addWidget(self.table)

    def build_table(self) -> FindingsTable:
        raise NotImplementedError

    def row_builder(self, f: core.Finding) -> list:
        raise NotImplementedError

    def scan_fn(self):
        raise NotImplementedError

    def apply_fn(self):
        raise NotImplementedError

    def start_scan(self):
        if not self.main_window.check_base_dir():
            return

        self.scan_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.main_window.log(f"[Phase {self.phase_num}] Scanning...")

        self.thread = QThread()
        scan_fn, args = self.scan_fn()
        self.worker = ScanWorker(scan_fn, *args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_scan_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def on_progress(self, done: int, total: int):
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(done)

    def on_scan_done(self, findings: list):
        self.findings = findings
        self.table.set_findings(findings, self.row_builder)
        self.count_label.setText(f"{len(findings)} findings")
        self.apply_btn.setEnabled(len(findings) > 0)
        self.scan_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.main_window.log(f"[Phase {self.phase_num}] Scan complete: {len(findings)} findings.")

    def on_error(self, msg: str):
        self.scan_btn.setEnabled(True)
        self.apply_btn.setEnabled(False)
        self.progress.setVisible(False)
        QMessageBox.critical(self, "Scan Error", msg)
        self.main_window.log(f"[Phase {self.phase_num}] ERROR: {msg}")

    def start_apply(self):
        selected = self.table.get_selected_findings()
        if not selected:
            QMessageBox.information(self, "Nothing Selected", "No items are checked.")
            return

        reply = QMessageBox.question(
            self, "Confirm",
            f"Apply changes to {len(selected)} files?\n\nThis is not reversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.scan_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.main_window.log(f"[Phase {self.phase_num}] Applying to {len(selected)} files...")

        extra = self.get_apply_extra(selected)

        self.thread = QThread()
        self.worker = ApplyWorker(self.apply_fn(), selected, extra)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_apply_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def get_apply_extra(self, selected: list) -> dict:
        """Override in subclasses to pass per-finding overrides (e.g. names)."""
        return {}

    def _show_help(self):
        QMessageBox.information(self, f"Phase {self.phase_num} – {self.title}", self._help_text)

    def on_apply_done(self, ok: int, fail: int):
        self.scan_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.main_window.log(f"[Phase {self.phase_num}] Done: {ok} succeeded, {fail} failed.")
        QMessageBox.information(self, "Complete", f"{ok} succeeded, {fail} failed.")
        self.start_scan()  # Re-scan to refresh the view


# --- Concrete tabs ---

class Phase1Tab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(main_window, 1, "Non-WAV Conversion",
                         "Convert MP3, AIFF, FLAC etc → 16-bit 48 kHz WAV.",
                         help_text=(
                             "Scans for audio files in non-WAV formats (MP3, AIFF, FLAC, M4A, "
                             "OGG, WMA, ALAC, …) and converts them to WAV using ffmpeg.\n\n"
                             "Rules applied:\n"
                             "  • Target: 48 000 Hz, 16-bit PCM (pcm_s16le) — always\n"
                             "  • Original file deleted after successful conversion "
                             "(configurable via delete_original in config.json)\n\n"
                             "Tip: Run this first — Phase 2 will then catch any WAVs "
                             "that still need sample-rate or bit-depth correction."
                         ))

    def build_table(self):
        return FindingsTable(["File", "Current", "Target"])

    def row_builder(self, f):
        return [f.path.name, f.current, f.target]

    def scan_fn(self):
        return core.scan_phase_1, (self.main_window.base_dir, self.main_window.cache,
                                    self.main_window.only_new)

    def apply_fn(self):
        return core.apply_phase_1


class Phase2Tab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(main_window, 2, "Sample Rate + Bit Depth",
                         "Find WAVs not at 48 kHz or not at 16-bit and fix both in one pass.",
                         help_text=(
                             "Scans all WAV files and flags any that are not at 48 000 Hz "
                             "or not at 16-bit. Both issues are corrected in a single ffmpeg pass.\n\n"
                             "Rules applied:\n"
                             "  • Target sample rate: 48 000 Hz (S-4 native)\n"
                             "  • Target bit depth: 16-bit (pcm_s16le) — always\n"
                             "  • Files already at 48 kHz AND 16-bit are skipped\n\n"
                             "Tip: Run after Phase 1 so freshly converted WAVs are included."
                         ))

    def build_table(self):
        return FindingsTable(["File", "Current", "Target"])

    def row_builder(self, f):
        return [f.path.name, f.current, f.target]

    def scan_fn(self):
        return core.scan_phase_2, (self.main_window.base_dir, self.main_window.cache,
                                    self.main_window.only_new)

    def apply_fn(self):
        return core.apply_phase_2


class Phase3Tab(PhaseTab):
    """Phase 3 works folder-by-folder — the Scan button opens a folder picker."""

    def __init__(self, main_window):
        super().__init__(main_window, 3, "Prefix Removal",
                         "Detect & strip shared prefixes from a folder of samples. "
                         "Pick a folder to scan.",
                         help_text=(
                             "Detects and strips shared filename prefixes within a single folder. "
                             "For example, if a folder contains:\n"
                             "  KickDrum_Tight.wav, KickDrum_Open.wav, KickDrum_Hard.wav …\n"
                             "the prefix \"KickDrum_\" is identified and stripped from all of them.\n\n"
                             "Detection thresholds (config.json):\n"
                             "  • Minimum prefix length: 8 characters\n"
                             "  • Minimum group size: 3 files must share the prefix\n"
                             "  • Short-name skip: folders where all names are ≤ 30 chars are ignored\n\n"
                             "You can edit the detected prefix in the table before applying.\n"
                             "Each folder is scanned separately — click \"Pick Folder & Scan\" "
                             "for each folder you want to process."
                         ))
        self.scan_btn.setText("📁 Pick Folder & Scan")

    def build_table(self):
        return FindingsTable(["Folder", "Detected Prefix (editable)", "Files", "Example Rename"],
                            editable_col=2)

    def row_builder(self, f):
        affected = f.extra.get("affected_files", [])
        prefix = f.extra.get("prefix", "")
        example = ""
        if affected:
            ex = Path(affected[0]).name
            if ex.startswith(prefix):
                example = f"{ex} → {ex[len(prefix):]}"
        return [str(f.path.relative_to(self.main_window.base_dir)
                    if f.path.is_relative_to(self.main_window.base_dir) else f.path),
                prefix, len(affected), example]

    def start_scan(self):
        if not self.main_window.check_base_dir():
            return
        folder_str = QFileDialog.getExistingDirectory(
            self, "Select folder to scan for prefix",
            str(self.main_window.base_dir),
        )
        if not folder_str:
            return
        folder = Path(folder_str)

        finding = core.scan_phase_3(folder)
        if not finding:
            manual, ok = QInputDialog.getText(
                self, "No prefix detected",
                f"No clear prefix found in {folder.name}.\nEnter prefix manually (or cancel):"
            )
            if ok and manual:
                try:
                    files = [f for f in folder.iterdir()
                             if f.is_file() and f.name.startswith(manual)]
                except OSError:
                    files = []
                if files:
                    finding = core.Finding(
                        phase=3, path=folder,
                        reason="manual prefix",
                        extra={"prefix": manual, "affected_files": [str(f) for f in files]},
                    )

        if finding:
            self.findings.append(finding)
            self.table.set_findings(self.findings, self.row_builder)
            self.count_label.setText(f"{len(self.findings)} folders queued")
            self.apply_btn.setEnabled(True)
            self.main_window.log(f"[Phase 3] Added folder: {folder.name}")
        else:
            QMessageBox.information(self, "No Findings", "No prefix to remove in this folder.")

    def get_apply_extra(self, selected):
        prefixes = {}
        for f in selected:
            edited = self.table.get_edit_value(f)
            if edited:
                prefixes[id(f)] = edited
        return {"prefixes": prefixes}

    def apply_fn(self):
        return core.apply_phase_3


class Phase4Tab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(main_window, 4, "Long Filenames",
                         f"Find files with stems > {config.NAME_LENGTH_LIMIT} chars. "
                         "Edit the 'New Name' column to rename.",
                         help_text=(
                             f"Finds WAV files whose stem (name without extension) is longer than "
                             f"{config.NAME_LENGTH_LIMIT} characters. While FAT32 allows 255-char "
                             f"names, the S-4 display truncates long names.\n\n"
                             f"Rules applied:\n"
                             f"  • Limit: {config.NAME_LENGTH_LIMIT} characters for the stem\n"
                             f"  • Suggested shorter names are auto-generated (truncated + cleaned)\n"
                             f"  • Edit the \"New Name\" column before applying\n\n"
                             f"Tip: Run Phase 3 (prefix removal) first — stripping a shared prefix "
                             f"often brings names under the limit automatically."
                         ))

    def build_table(self):
        return FindingsTable(["Current Name", "Length", "New Name (editable)", "Folder"],
                            editable_col=3)

    def row_builder(self, f):
        suggestions = f.extra.get("suggestions", [])
        suggested = suggestions[0] if suggestions else ""
        try:
            rel = str(f.path.parent.relative_to(self.main_window.base_dir))
        except ValueError:
            rel = str(f.path.parent)
        return [f.current, f.reason, suggested, rel]

    def scan_fn(self):
        return core.scan_phase_4, (self.main_window.base_dir, self.main_window.only_new)

    def get_apply_extra(self, selected):
        new_names = {}
        for f in selected:
            edited = self.table.get_edit_value(f)
            if edited:
                new_names[id(f)] = edited
        return {"new_names": new_names}

    def apply_fn(self):
        return core.apply_phase_4


class Phase5Tab(PhaseTab):
    """Phase 5 — Stereo → Mono. Adds a Loose mode checkbox."""

    def __init__(self, main_window):
        super().__init__(main_window, 5, "Stereo → Mono",
                         "Detect 'fake stereo' files where L and R are identical. "
                         "Saves ~50 % per file. True stereo is never touched.",
                         help_text=(
                             "Analyses stereo WAV files to detect \"fake stereo\" — files where "
                             "both channels carry identical (or nearly identical) audio. "
                             "Converting these to true mono saves ~50 % file size.\n\n"
                             "Detection thresholds (config.json):\n"
                             "  • Dual mono   (auto-selected): max |L−R| ≤ −90 dBFS\n"
                             "      Channels are bit-perfect or essentially identical.\n"
                             "  • One-sided   (auto-selected): one channel ≥ 40 dB quieter\n"
                             "      The quiet side is silence; only the loud side is kept.\n"
                             "  • Near-mono   (Loose mode, opt-in): max |L−R| ≤ −60 dBFS\n"
                             "      Very faint stereo width — shown unchecked for manual review.\n"
                             "  • True stereo: above all thresholds — skipped entirely.\n\n"
                             "Enable \"Loose mode\" to also surface near-mono files."
                         ))
        self.include_near_mono = False

        loose_chk = QCheckBox("Loose mode (include near-mono, opt-in)")
        loose_chk.setToolTip("Also flag files with very small (≤ -60 dB) L/R differences. "
                             "Listed but UNCHECKED by default — review carefully.")
        loose_chk.stateChanged.connect(self._on_loose_changed)
        toolbar = self.layout().itemAt(1).layout()
        toolbar.insertWidget(3, loose_chk)

    def _on_loose_changed(self, state):
        self.include_near_mono = (state == Qt.CheckState.Checked.value)

    def build_table(self):
        return FindingsTable(["File", "Classification", "Current", "Target", "Savings"])

    def row_builder(self, f):
        cls = f.extra.get("classification", "?")
        cls_pretty = {
            "dual_mono": "Dual mono (identical L/R)",
            "one_side":  f"One-sided ({f.extra.get('keep_channel', '?')} only)",
            "near_mono": "Near-mono (faint stereo)",
        }.get(cls, cls)
        return [f.path.name, cls_pretty, f.current, f.target,
                core.format_bytes(f.savings_bytes)]

    def scan_fn(self):
        return core.scan_phase_5, (self.main_window.base_dir, self.main_window.cache,
                                    self.main_window.only_new, self.include_near_mono)

    def apply_fn(self):
        return core.apply_phase_5


class Phase6Tab(PhaseTab):
    """Phase 6 — Silence removal."""

    def __init__(self, main_window):
        super().__init__(main_window, 6, "Silence Removal",
                         "Find WAVs with leading/trailing silence and trim it off.",
                         help_text=(
                             "Scans WAV files for silence at the start and/or end and trims it. "
                             "Particularly useful for stems and one-shots that have dead air "
                             "before the transient or a long tail after the sound ends.\n\n"
                             "Detection thresholds (config.json):\n"
                             f"  • Noise floor: {config.SILENCE_THRESHOLD_DB} dBFS "
                             "(below this = silence)\n"
                             f"  • Minimum duration: {config.SILENCE_MIN_DURATION}s "
                             "(shorter gaps are ignored)\n\n"
                             "The filter is applied with ffmpeg's silenceremove. "
                             "The areverse trick is used to detect trailing silence accurately.\n\n"
                             "Tip: Run this after Phases 1–2 so all files are already at "
                             "48 kHz / 16-bit before trimming."
                         ))

    def build_table(self):
        return FindingsTable(["File", "Lead Silence", "Trail Silence", "Duration", "Est. Savings"])

    def row_builder(self, f):
        lead  = f.extra.get("lead", 0.0)
        trail = f.extra.get("trail", 0.0)
        return [
            f.path.name,
            f"{lead:.2f}s" if lead >= config.SILENCE_MIN_DURATION else "—",
            f"{trail:.2f}s" if trail >= config.SILENCE_MIN_DURATION else "—",
            f.current,
            core.format_bytes(f.savings_bytes),
        ]

    def scan_fn(self):
        return core.scan_phase_6, (self.main_window.base_dir, self.main_window.cache,
                                    self.main_window.only_new)

    def apply_fn(self):
        return core.apply_phase_6


# ============================================================================
# Main window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Torso S-4 Sample Converter")
        self.resize(1100, 750)

        self.base_dir: Optional[Path] = None
        self.cache: Optional[ProbeCache] = None
        self.only_new: bool = True

        self._build_ui()
        self._load_default_dir()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        # --- Top toolbar: drive selector + options ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Drive:"))

        self.preset_combo = QComboBox()
        for label, _ in PATH_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        top.addWidget(self.preset_combo)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/Volumes/S-4/SAMPLES")
        top.addWidget(self.path_edit, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self.browse_dir)
        top.addWidget(browse_btn)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self.load_dir)
        load_btn.setStyleSheet("background-color: #1e5a8a; color: white; padding: 4px 10px;")
        top.addWidget(load_btn)

        self.incremental_chk = QCheckBox("Incremental (skip marker-clean folders)")
        self.incremental_chk.setChecked(True)
        self.incremental_chk.stateChanged.connect(
            lambda s: setattr(self, "only_new", s == Qt.CheckState.Checked.value)
        )
        top.addWidget(self.incremental_chk)

        layout.addLayout(top)

        # --- Splitter: tabs on top, log on bottom ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tabs = QTabWidget()
        self.tabs.addTab(Phase1Tab(self), "1. Non-WAV")
        self.tabs.addTab(Phase2Tab(self), "2. SR + Bit Depth")
        self.tabs.addTab(Phase3Tab(self), "3. Prefixes")
        self.tabs.addTab(Phase4Tab(self), "4. Long Names")
        self.tabs.addTab(Phase5Tab(self), "5. Stereo→Mono")
        self.tabs.addTab(Phase6Tab(self), "6. Silence")
        self.tabs.setEnabled(False)
        splitter.addWidget(self.tabs)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        log_font = QFont("Menlo, Consolas, monospace")
        log_font.setPointSize(11)
        self.log_view.setFont(log_font)
        splitter.addWidget(self.log_view)

        splitter.setSizes([600, 150])
        layout.addWidget(splitter, stretch=1)

        # --- Status bar ---
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("No drive loaded.")

    def _load_default_dir(self):
        default = str(config.DEFAULT_BASE_DIR)
        self.path_edit.setText(default)
        for i, (_, path) in enumerate(PATH_PRESETS):
            if path == default:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(i)
                self.preset_combo.blockSignals(False)
                break

    def _on_preset_changed(self, idx: int):
        _, path = PATH_PRESETS[idx]
        if path is None:
            self.browse_dir()
            # Reset combo to avoid staying on "Custom…" after cancel
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
        else:
            self.path_edit.setText(path)

    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select SAMPLES folder",
            self.path_edit.text() or str(Path.home()),
        )
        if d:
            self.path_edit.setText(d)

    def load_dir(self):
        raw = self.path_edit.text().strip()
        if not raw:
            return
        path = Path(raw).expanduser().resolve()
        if not core.check_drive_present(path):
            QMessageBox.warning(self, "Not Found",
                                f"{path} does not exist or is not a directory.\n"
                                "Is the drive mounted?")
            return

        self.base_dir = path
        self.cache = ProbeCache(path)
        core.setup_logging(path, verbose=False)

        self.tabs.setEnabled(True)
        self.statusBar().showMessage(
            f"Loaded: {path}  |  Cache: {self.cache.size()} entries"
        )
        self.log(f"Loaded drive: {path}")
        self.log(f"Cache has {self.cache.size()} entries.")

    def check_base_dir(self) -> bool:
        if self.base_dir is None or not core.check_drive_present(self.base_dir):
            QMessageBox.warning(self, "No Drive",
                                "Drive is not loaded or has been disconnected. Click Load.")
            return False
        return True

    def log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def closeEvent(self, event):
        if self.cache:
            self.cache.save()
            self.log("Cache saved.")
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
