"""PyQt6 GUI for S-4 Sample Converter.

Tab-per-phase interface with tables of findings.
Each row has a checkbox; select what to apply, then click Apply.

Run with:
    python -m s4converter.gui
"""

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
    from PyQt6.QtGui import QFont, QTextCursor
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QLineEdit, QFileDialog, QTabWidget,
        QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
        QProgressBar, QPlainTextEdit, QTextEdit, QMessageBox, QStatusBar,
        QSplitter, QInputDialog, QComboBox,
    )
except ImportError:
    print("PyQt6 not installed. Install with:")
    print("  pip install PyQt6")
    sys.exit(1)

from . import config, core
from .cache import FolderMarkers, ProbeCache

# Preset paths for the drive dropdown — sourced from config.json
PATH_PRESETS = [
    (f"USB  –  {config.USB_ROOT}", str(config.USB_ROOT)),
    (f"S-4  –  {config.S4_ROOT}",  str(config.S4_ROOT)),
    ("Custom…",                     None),
]


# ============================================================================
# Worker threads (so the UI stays responsive during scans)
# ============================================================================

class ScanWorker(QObject):
    """Run a scan in a background thread."""
    progress = pyqtSignal(int, int)         # done, total
    scan_item = pyqtSignal(str)             # current file/folder being processed
    finished = pyqtSignal(list)              # List[Finding]
    stopped = pyqtSignal()                   # scan was stopped cleanly
    error = pyqtSignal(str)

    def __init__(self, scan_fn, *args):
        super().__init__()
        self.scan_fn = scan_fn
        self.args = args
        import threading
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            findings = self.scan_fn(*self.args,
                                     progress_cb=self.progress.emit,
                                     file_cb=self.scan_item.emit,
                                     stop_event=self.stop_event)
            if self.stop_event.is_set():
                self.stopped.emit()
            else:
                self.finished.emit(findings)
        except Exception as e:
            self.error.emit(str(e))


class ApplyWorker(QObject):
    """Apply actions in a background thread."""
    progress = pyqtSignal(int, int)
    current_file = pyqtSignal(str)            # filename being processed right now
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
                self.current_file.emit(Path(f.path).name)
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


class ReportWorker(QObject):
    """Generate the CSV + Markdown report in a background thread."""
    finished = pyqtSignal(str, str)   # csv_path, md_path
    error = pyqtSignal(str)

    def __init__(self, base_dir: Path, cache: "ProbeCache"):
        super().__init__()
        self.base_dir = base_dir
        self.cache = cache

    def run(self):
        try:
            csv_path, md_path = core.generate_report(self.base_dir, self.cache)
            self.finished.emit(str(csv_path), str(md_path))
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# Findings table widget
# ============================================================================

_TABLE_DISPLAY_CAP = 5000


class FindingsTable(QTableWidget):
    """Table that displays findings with a checkbox per row."""

    def __init__(self, columns: List[str], editable_col: Optional[int] = None):
        super().__init__()
        self.columns = ["✓"] + columns
        self.editable_col = editable_col  # column index in the full table (0 = checkbox)
        self.findings: List[core.Finding] = []  # full list, may exceed display cap

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
        display = findings[:_TABLE_DISPLAY_CAP]

        self.setUpdatesEnabled(False)
        self.setSortingEnabled(False)
        self.setRowCount(len(display))

        for row, f in enumerate(display):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if f.selected else Qt.CheckState.Unchecked)
            self.setItem(row, 0, chk)

            for col, value in enumerate(row_builder(f), start=1):
                item = QTableWidgetItem(str(value))
                if self.editable_col is None or col != self.editable_col:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(row, col, item)

        self.setUpdatesEnabled(True)

    def get_selected_findings(self) -> List[core.Finding]:
        selected = []
        displayed = min(len(self.findings), _TABLE_DISPLAY_CAP)
        # Sync checkbox state for visible rows.
        for row in range(displayed):
            f = self.findings[row]
            checked = self.item(row, 0).checkState() == Qt.CheckState.Checked
            f.selected = checked
            if checked:
                selected.append(f)
        # Hidden rows (beyond cap) keep their default selected state.
        for f in self.findings[displayed:]:
            if f.selected:
                selected.append(f)
        return selected

    def get_edit_value(self, finding: core.Finding) -> str:
        if self.editable_col is None:
            return ""
        try:
            row = self.findings.index(finding)
            if row >= _TABLE_DISPLAY_CAP:
                return ""
            return self.item(row, self.editable_col).text()
        except (ValueError, AttributeError):
            return ""

    def select_all(self, checked: bool):
        for row in range(self.rowCount()):
            self.item(row, 0).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        # Also update hidden findings beyond the cap.
        for f in self.findings[_TABLE_DISPLAY_CAP:]:
            f.selected = checked


# ============================================================================
# Phase tab base class
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
        self._applying: bool = False
        self._all_top: List[str] = []
        self._seen_top: set = set()     # folders we've seen at least one file from
        self._active_top: str = ""      # most recently seen top-level folder
        self._active_full_path: str = ""
        self._current_file: str = ""

        layout = QVBoxLayout(self)

        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(desc_label)

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

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.clicked.connect(self._request_stop)
        self.stop_btn.setVisible(False)
        self.stop_btn.setStyleSheet("padding: 6px 12px;")
        toolbar.addWidget(self.stop_btn)

        self.apply_btn = QPushButton("✓ Apply Selected")
        self.apply_btn.clicked.connect(self.start_apply)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet(
            "background-color: #2c7a3d; color: white; padding: 6px 12px;"
        )
        toolbar.addWidget(self.apply_btn)

        if help_text:
            help_btn = QPushButton("ℹ")
            help_btn.setFixedWidth(32)
            help_btn.setToolTip("Phase help & limits")
            help_btn.clicked.connect(self._show_help)
            toolbar.addWidget(help_btn)

        layout.addLayout(toolbar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self._status = QTextEdit()
        self._status.setReadOnly(True)
        self._status.setUndoRedoEnabled(False)
        self._status.setMaximumHeight(140)
        self._status.setVisible(False)
        status_font = QFont("Menlo, Consolas, monospace")
        status_font.setPointSize(11)
        self._status.setFont(status_font)
        self._status.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._status)

        self._current_file_label = QLabel("")
        self._current_file_label.setVisible(False)
        cur_font = QFont("Menlo, Consolas, monospace")
        cur_font.setPointSize(11)
        self._current_file_label.setFont(cur_font)
        self._current_file_label.setStyleSheet("color: #444; padding-left: 24px;")
        layout.addWidget(self._current_file_label)

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

        self._reset_scan_status()
        self.main_window.set_busy(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._status.setVisible(True)
        self._current_file_label.setVisible(True)
        self.stop_btn.setVisible(True)
        self.count_label.setText("Scanning…")
        self._render_status()
        self.main_window.log(f"[{self.title}] Scanning...")

        self.thread = QThread()
        scan_fn, args = self.scan_fn()
        self.worker = ScanWorker(scan_fn, *args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.scan_item.connect(self.on_scan_item)
        self.worker.finished.connect(self.on_scan_done)
        self.worker.stopped.connect(self.on_scan_stopped)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.stopped.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def on_progress(self, done: int, total: int):
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
            if self._applying:
                self.count_label.setText(f"{done:,} / {total:,} files")

    def on_scan_done(self, findings: list):
        self._active_top = ""
        self.findings = findings
        self.table.set_findings(findings, self.row_builder)
        n = len(findings)
        if n > _TABLE_DISPLAY_CAP:
            self.count_label.setText(
                f"{n} findings  (showing first {_TABLE_DISPLAY_CAP} — all included in Apply)"
            )
        else:
            self.count_label.setText(f"{n} findings")
        self.progress.setVisible(False)
        self._status.setVisible(False)
        self._current_file_label.setVisible(False)
        self.stop_btn.setVisible(False)
        self.main_window.set_busy(False)
        self.main_window.log(
            f"[{self.title}] Scan complete: {len(findings)} findings."
        )

    def on_scan_stopped(self):
        self.progress.setVisible(False)
        self._status.setVisible(False)
        self._current_file_label.setVisible(False)
        self.stop_btn.setVisible(False)
        self.main_window.set_busy(False)
        if self.main_window.cache:
            self.main_window.cache.save()
        self.main_window.log(f"[{self.title}] Scan stopped. Cache saved.")

    def on_error(self, msg: str):
        self._applying = False
        self.progress.setVisible(False)
        self._status.setVisible(False)
        self._current_file_label.setVisible(False)
        self.stop_btn.setVisible(False)
        self.main_window.set_busy(False)
        QMessageBox.critical(self, "Error", msg)
        self.main_window.log(f"[{self.title}] ERROR: {msg}")

    def _request_stop(self):
        if hasattr(self, 'worker'):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("⏹ Stopping…")
            self.main_window.log(f"[{self.title}] Stop requested — finishing current batch…")

    def _reset_scan_status(self):
        self.stop_btn.setText("⏹ Stop")
        self.stop_btn.setEnabled(True)
        self._seen_top = set()
        self._active_top = ""
        self._active_full_path = ""
        self._current_file = ""
        self._current_file_label.setText("")
        base = self.main_window.base_dir
        if base and base.exists():
            try:
                self._all_top = sorted([
                    d.name for d in base.iterdir()
                    if d.is_dir()
                    and not d.name.startswith(".")
                    and d.name not in config.EXCLUDED_FOLDER_NAMES
                ])
            except OSError:
                self._all_top = []
        else:
            self._all_top = []

    def on_scan_item(self, path: str):
        base = self.main_window.base_dir
        if base is None:
            return
        try:
            rel = Path(path).relative_to(base)
            top = rel.parts[0] if rel.parts else ""
        except ValueError:
            top = ""

        folder_changed = top and top != self._active_top
        if top:
            self._seen_top.add(top)
            self._active_top = top
        self._active_full_path = path
        self._current_file = Path(path).name

        # Only rebuild the full HTML tree on folder transitions — not every file.
        if folder_changed:
            self._render_status()

        # Current file updates cheaply via QLabel (no undo stack, no DOM rebuild).
        self._current_file_label.setText(f"⟳  {self._current_file}")

    def _render_status(self):
        import html as _html
        def esc(s): return _html.escape(str(s))

        parts = []
        for folder in self._all_top:
            if folder == self._active_top:
                active_folder = str(Path(self._active_full_path).parent) if self._active_full_path else ""
                parts.append(
                    f'<div style="color:#0d47a1; font-weight:bold;">▶&nbsp;&nbsp;{esc(active_folder)}</div>'
                )
            elif folder in self._seen_top:
                parts.append(f'<div style="color:#2e7d32;">✓&nbsp;&nbsp;{esc(folder)}</div>')

        pending = [f for f in self._all_top if f not in self._seen_top]
        if pending:
            parts.append('<div style="color:#9e9e9e; margin-top:4px;">── Pending ────────────────────</div>')
            for f in pending:
                parts.append(f'<div style="color:#9e9e9e;">&nbsp;&nbsp;&nbsp;{esc(f)}</div>')

        self._status.setHtml("".join(parts))
        cursor = self._status.document().find("▶")
        if not cursor.isNull():
            self._status.setTextCursor(cursor)
            self._status.ensureCursorVisible()

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

        self.main_window.set_busy(True)
        self._applying = True
        self.progress.setVisible(True)
        self.progress.setMaximum(len(selected))
        self.progress.setValue(0)
        self._current_file_label.setText("")
        self._current_file_label.setVisible(True)
        self.count_label.setText(f"0 / {len(selected):,} files")
        self.main_window.log(
            f"[{self.title}] Applying to {len(selected)} items..."
        )

        extra = self.get_apply_extra(selected)

        self.thread = QThread()
        self.worker = ApplyWorker(self.apply_fn(), selected, extra)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.current_file.connect(self._on_apply_file)
        self.worker.finished.connect(self.on_apply_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def get_apply_extra(self, selected: list) -> dict:
        return {}

    def _on_apply_file(self, filename: str):
        self._current_file_label.setText(f"⟳  {filename}")

    def _show_help(self):
        QMessageBox.information(self, self.title, self._help_text)

    def on_apply_done(self, ok: int, fail: int):
        self._applying = False
        self.progress.setVisible(False)
        self._current_file_label.setVisible(False)
        self._current_file_label.setText("")
        self.main_window.log(
            f"[{self.title}] Done: {ok} succeeded, {fail} failed."
        )
        QMessageBox.information(self, "Complete", f"{ok} succeeded, {fail} failed.")
        self.start_scan()


# ============================================================================
# Phase 1 — Format Normalization (non-WAV + wrong SR/bits combined)
# ============================================================================

class Phase1Tab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 1, "Format Normalization",
            "Convert non-WAV files and fix WAVs at wrong sample rate or bit depth "
            "→ 16-bit 48 kHz WAV in one pass.",
            help_text=(
                "Scans your entire sample library for two classes of audio problems:\n\n"
                "1. Non-WAV files (MP3, AIFF, FLAC, M4A, OGG, WMA, ALAC …)\n"
                "   → Converts to WAV. Original deleted if delete_original=true in config.json.\n\n"
                "2. WAV files at the wrong sample rate or bit depth\n"
                "   → Re-encodes in place. No copy is made.\n\n"
                "Target always: 48 000 Hz, 16-bit PCM (pcm_s16le).\n"
                "Files already at 48 kHz AND 16-bit WAV are skipped entirely.\n\n"
                "The 'Issue' column shows what is wrong with each file."
            ),
        )

    def build_table(self):
        return FindingsTable(["File", "Issue", "Current", "Target"])

    def row_builder(self, f):
        ftype = f.extra.get("type", "wav_format")
        issue = "Non-WAV → WAV" if ftype == "non_wav" else "Wrong format (SR/bits)"
        return [f.path.name, issue, f.current, f.target]

    def scan_fn(self):
        return (core.scan_phase_1,
                (self.main_window.base_dir, self.main_window.cache,
                 self.main_window.only_new))

    def apply_fn(self):
        return core.apply_phase_1


# ============================================================================
# Names tab — Prefix Removal (Phase 2) + Long Filename Cleanup (Phase 3)
# ============================================================================

class NamesTab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 2, "Name Cleanup",
            "Strip shared prefixes and shorten long filenames. "
            "Edit values inline before applying.",
            help_text=(
                "Combines two name-related cleanup passes:\n\n"
                "Prefix Removal — scans every subfolder for shared filename prefixes.\n"
                "  Example: KickDrum_Tight.wav, KickDrum_Open.wav → Tight.wav, Open.wav\n"
                f"  (min prefix: {config.MIN_PREFIX_LENGTH} chars, min group: {config.MIN_GROUP_SIZE} files)\n\n"
                f"Long Filenames — finds files with stems > {config.NAME_LENGTH_LIMIT} chars.\n"
                "  Suggested shorter names are auto-generated.\n\n"
                "Edit the 'Edit' column before applying — it acts as the prefix to strip "
                "for Prefix rows, and the new filename for Long Name rows.\n\n"
                "Tip: run prefix removal first — stripping a prefix often brings names "
                "under the length limit automatically."
            ),
        )

    def build_table(self):
        return FindingsTable(
            ["Type", "File / Folder", "Detail", "Edit (editable)"],
            editable_col=4,
        )

    def row_builder(self, f):
        if f.phase == 2:
            prefix   = f.extra.get("prefix", "")
            affected = len(f.extra.get("affected_files", []))
            try:    loc = str(f.path.relative_to(self.main_window.base_dir))
            except ValueError: loc = str(f.path)
            return ["Prefix", loc, f"{affected} files", prefix]
        else:
            suggestions = f.extra.get("suggestions", [])
            suggested   = suggestions[0] if suggestions else ""
            try:    loc = str(f.path.parent.relative_to(self.main_window.base_dir))
            except ValueError: loc = str(f.path.parent)
            return ["Long Name", f.current, f.reason, suggested]

    def scan_fn(self):
        base     = self.main_window.base_dir
        only_new = self.main_window.only_new
        def combined(base_dir, only_new, progress_cb=None, file_cb=None, stop_event=None):
            findings = core.scan_phase_2_all(base_dir, only_new, progress_cb, file_cb, stop_event)
            if not (stop_event and stop_event.is_set()):
                findings += core.scan_phase_3(base_dir, only_new, progress_cb, file_cb, stop_event)
            return findings
        return (combined, (base, only_new))

    def start_apply(self):
        selected = self.table.get_selected_findings()
        if not selected:
            QMessageBox.information(self, "Nothing Selected", "No items are checked.")
            return
        reply = QMessageBox.question(
            self, "Confirm",
            f"Apply changes to {len(selected)} items?\n\nThis is not reversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        edit_map = {id(f): self.table.get_edit_value(f) for f in selected}

        def apply_fn(finding):
            edited = edit_map.get(id(finding), "")
            if finding.phase == 2:
                return bool(core.apply_phase_2(finding, override_prefix=edited or None))
            return core.apply_phase_3(finding, edited)

        self.main_window.set_busy(True)
        self._applying = True
        self.progress.setVisible(True)
        self.progress.setMaximum(len(selected))
        self.progress.setValue(0)
        self._current_file_label.setText("")
        self._current_file_label.setVisible(True)
        self.count_label.setText(f"0 / {len(selected):,} files")
        self.main_window.log(f"[{self.title}] Applying to {len(selected)} items...")
        self.thread = QThread()
        self.worker = ApplyWorker(apply_fn, selected)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.current_file.connect(self._on_apply_file)
        self.worker.finished.connect(self.on_apply_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def apply_fn(self):
        pass  # overridden by start_apply


# ============================================================================
# File Size tab — Stereo → Mono (Phase 4) + Silence Removal (Phase 5)
# ============================================================================

class FileSizeTab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 4, "File Size",
            "Convert fake-stereo files to mono and trim leading/trailing silence.",
            help_text=(
                "Combines two file-size reduction passes:\n\n"
                "Stereo → Mono — detects 'fake stereo' where L and R are identical.\n"
                "  • Dual mono (auto-selected): L−R diff ≤ −90 dBFS\n"
                "  • One-sided (auto-selected): one channel ≥ 40 dB quieter\n"
                "  • Near-mono (Loose mode, opt-in): diff ≤ −60 dBFS\n"
                "  Saves ~50 % per converted file.\n\n"
                "Silence Removal — trims leading/trailing silence.\n"
                f"  Threshold: {config.SILENCE_THRESHOLD_DB} dBFS, "
                f"min duration: {config.SILENCE_MIN_DURATION}s\n\n"
                "Enable Loose mode to also surface near-mono files."
            ),
        )
        self.include_near_mono = False
        loose_chk = QCheckBox("Loose mode (include near-mono)")
        loose_chk.setToolTip(
            "Also flag files with very small (≤ -60 dB) L/R differences. "
            "Listed but UNCHECKED — review carefully."
        )
        loose_chk.stateChanged.connect(
            lambda s: setattr(self, "include_near_mono", s == Qt.CheckState.Checked.value)
        )
        toolbar = self.layout().itemAt(1).layout()
        toolbar.insertWidget(3, loose_chk)

    def build_table(self):
        return FindingsTable(["Type", "File", "Issue", "Current", "Target", "Savings"])

    def row_builder(self, f):
        if f.phase == 4:
            cls = f.extra.get("classification", "?")
            cls_pretty = {
                "dual_mono": "Dual mono",
                "one_side":  f"One-sided ({f.extra.get('keep_channel', '?')} only)",
                "near_mono": "Near-mono",
            }.get(cls, cls)
            return ["Stereo→Mono", f.path.name, cls_pretty,
                    f.current, f.target, core.format_bytes(f.savings_bytes)]
        lead  = f.extra.get("lead",  0.0)
        trail = f.extra.get("trail", 0.0)
        issue = (f"{lead:.2f}s lead" if lead  >= config.SILENCE_MIN_DURATION else "") + \
                (" / " if lead >= config.SILENCE_MIN_DURATION and trail >= config.SILENCE_MIN_DURATION else "") + \
                (f"{trail:.2f}s trail" if trail >= config.SILENCE_MIN_DURATION else "")
        return ["Silence", f.path.name, issue,
                f.current, f.target, core.format_bytes(f.savings_bytes)]

    def scan_fn(self):
        base     = self.main_window.base_dir
        cache    = self.main_window.cache
        only_new = self.main_window.only_new
        include_near_mono = self.include_near_mono
        def combined(base_dir, cache, only_new, progress_cb=None, file_cb=None, stop_event=None):
            findings = core.scan_phase_4(base_dir, cache, only_new, include_near_mono,
                                          progress_cb, file_cb, stop_event)
            if not (stop_event and stop_event.is_set()):
                findings += core.scan_phase_5(base_dir, cache, only_new,
                                               progress_cb, file_cb, stop_event)
            return findings
        return (combined, (base, cache, only_new))

    def apply_fn(self):
        def dispatch(finding):
            if finding.phase == 4:
                return core.apply_phase_4(finding)
            return core.apply_phase_5(finding)
        return dispatch


# ============================================================================
# Phase 6 — BPM Detection
# ============================================================================

class Phase6Tab(PhaseTab):
    """Phase 6 — BPM detection for rhythmic loops. All rows start unchecked."""

    def __init__(self, main_window):
        super().__init__(
            main_window, 6, "BPM Detection",
            "Detect BPM for rhythmic loops and optionally rename with a BPM prefix. "
            "One-shots and field recordings are filtered out automatically.",
            help_text=(
                "Analyses WAV files to detect BPM using the aubio library. "
                "Multiple filters prevent false positives on one-shots and recordings:\n\n"
                "  1. Duration gate — files shorter than "
                f"{config.BPM_MIN_DURATION}s or longer than "
                f"{config.BPM_MAX_DURATION}s are skipped.\n"
                "  2. Beat event count — fewer than "
                f"{config.BPM_MIN_BEATS} detected beats → skipped.\n"
                "  3. Consistency score — BPM estimates must converge "
                f"(confidence ≥ {config.BPM_MIN_CONFIDENCE}).\n"
                "  4. Half/double correction — rescales estimates outside "
                f"{config.BPM_TARGET_MIN}–{config.BPM_TARGET_MAX} BPM.\n"
                "  5. Folder name hints — folders named 'one shot', 'sfx', "
                "'ambient', etc. are skipped entirely.\n\n"
                "High-confidence detections (≥ 0.75) start CHECKED automatically.\n"
                "Med and Low confidence rows start unchecked — review before selecting.\n"
                "Edit the 'New Name' column to customise the filename.\n\n"
                "Requires: pip install aubio"
            ),
        )

    def build_table(self):
        return FindingsTable(
            ["File", "BPM", "Confidence", "Duration", "New Name (editable)"],
            editable_col=5,
        )

    def row_builder(self, f):
        bpm      = f.extra.get("bpm", "?")
        conf_lbl = f.extra.get("conf_label", "?")
        dur      = f.extra.get("duration", 0.0)
        return [
            f.path.name,
            str(int(bpm)) if isinstance(bpm, (int, float)) else str(bpm),
            conf_lbl,
            f"{dur:.1f}s",
            f.target,
        ]

    def scan_fn(self):
        return (core.scan_phase_6,
                (self.main_window.base_dir, self.main_window.cache,
                 self.main_window.only_new))

    def get_apply_extra(self, selected):
        new_names = {}
        for f in selected:
            edited = self.table.get_edit_value(f)
            new_names[id(f)] = edited if edited else f.target
        return {"new_names": new_names}

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
        self._busy: bool = False
        self._caffeinate_proc = None
        self._report_thread: Optional[QThread] = None

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
        self.path_edit.textChanged.connect(self._sync_preset_combo)
        top.addWidget(self.path_edit, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self.browse_dir)
        top.addWidget(browse_btn)

        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self.load_dir)
        load_btn.setStyleSheet(
            "background-color: #1e5a8a; color: white; padding: 4px 10px;"
        )
        top.addWidget(load_btn)

        self.incremental_chk = QCheckBox("Fast scan (skip unchanged folders)")
        self.incremental_chk.setChecked(True)
        self.incremental_chk.stateChanged.connect(
            lambda s: setattr(self, "only_new", s == Qt.CheckState.Checked.value)
        )
        top.addWidget(self.incremental_chk)

        self.report_btn = QPushButton("📊 Export Report")
        self.report_btn.setEnabled(False)
        self.report_btn.setToolTip(
            "Generate a CSV + Markdown summary of the sample library "
            "(uses cached probe data — fast)"
        )
        self.report_btn.clicked.connect(self.export_report)
        top.addWidget(self.report_btn)

        layout.addLayout(top)

        # --- Splitter: tabs on top, log on bottom ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tabs = QTabWidget()
        self.tabs.addTab(Phase1Tab(self), "1. Format")
        self.tabs.addTab(NamesTab(self),  "2. Names")
        self.tabs.addTab(FileSizeTab(self), "3. File Size")
        self.tabs.addTab(Phase6Tab(self), "4. BPM")
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
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
        else:
            self.path_edit.setText(path)

    def _sync_preset_combo(self, text: str):
        """Snap dropdown to Custom… if typed path doesn't match any preset."""
        resolved = str(Path(text.strip()).expanduser().resolve()) if text.strip() else ""
        for i, (_, p) in enumerate(PATH_PRESETS):
            if p is not None and p == resolved:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(i)
                self.preset_combo.blockSignals(False)
                return
        custom_idx = next(i for i, (_, p) in enumerate(PATH_PRESETS) if p is None)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(custom_idx)
        self.preset_combo.blockSignals(False)

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
        self.cache = ProbeCache(path, cache_root=config.cache_root_for(path))
        core.setup_logging(path, verbose=False)

        self.tabs.setEnabled(True)
        self.report_btn.setEnabled(True)
        cache_root = self.cache.cache_file.parent
        cache_note = f"  (cache at {cache_root})" if cache_root != path else ""
        self.statusBar().showMessage(
            f"Loaded: {path}  |  Cache: {self.cache.size()} entries{cache_note}"
        )
        self.log(f"Loaded drive: {path}")
        self.log(f"Cache: {self.cache.size()} entries — {self.cache.cache_file}")

    def check_base_dir(self) -> bool:
        if self.base_dir is None or not core.check_drive_present(self.base_dir):
            QMessageBox.warning(self, "No Drive",
                                "Drive is not loaded or has been disconnected. Click Load.")
            return False
        return True

    # --- Report export ---

    def export_report(self):
        if not self.check_base_dir():
            return
        self.report_btn.setEnabled(False)
        self.log("Generating library report…")

        self._report_thread = QThread()
        self._report_worker = ReportWorker(self.base_dir, self.cache)
        self._report_worker.moveToThread(self._report_thread)
        self._report_thread.started.connect(self._report_worker.run)
        self._report_worker.finished.connect(self._on_report_done)
        self._report_worker.error.connect(self._on_report_error)
        self._report_worker.finished.connect(self._report_thread.quit)
        self._report_worker.error.connect(self._report_thread.quit)
        self._report_thread.finished.connect(
            lambda: self.report_btn.setEnabled(True)
        )
        self._report_thread.start()

    def _on_report_done(self, csv_path: str, md_path: str):
        self.log(f"Report exported: {csv_path}")
        msg = QMessageBox(self)
        msg.setWindowTitle("Report Generated")
        msg.setText(
            "Library report exported successfully!\n\n"
            f"CSV:      {csv_path}\n"
            f"Markdown: {md_path}"
        )
        open_btn = msg.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Ok)
        msg.exec()
        if msg.clickedButton() == open_btn:
            core.open_folder(Path(csv_path).parent)

    def _on_report_error(self, msg: str):
        self.log(f"Report error: {msg}")
        QMessageBox.critical(self, "Report Error", msg)

    def set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            self._start_caffeinate()
        else:
            self._stop_caffeinate()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if not hasattr(tab, 'scan_btn'):
                continue
            tab.scan_btn.setEnabled(not busy)
            tab.apply_btn.setEnabled(False if busy else bool(tab.findings))

    def _start_caffeinate(self):
        import sys
        if sys.platform != "darwin" or self._caffeinate_proc is not None:
            return
        try:
            self._caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-i"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _stop_caffeinate(self):
        if self._caffeinate_proc is not None:
            self._caffeinate_proc.terminate()
            self._caffeinate_proc = None

    def log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def closeEvent(self, event):
        if self._busy:
            reply = QMessageBox.warning(
                self, "Operation in Progress",
                "A scan or conversion is currently running.\n\n"
                "Quitting now is safe — your audio files will not be corrupted. "
                "Any conversion in progress may leave a temporary .__tmp__.wav file "
                "on the drive, which you can delete manually.\n\n"
                "Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._stop_caffeinate()
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
