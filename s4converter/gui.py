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
        QDialog, QDialogButtonBox, QFrame, QScrollArea,
    )
except ImportError:
    print("PyQt6 not installed. Install with:")
    print("  pip install PyQt6")
    sys.exit(1)

from . import config, core, sync
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
    stopped = pyqtSignal(int, int)            # ok, fail — stopped early
    error = pyqtSignal(str)

    def __init__(self, apply_fn, findings, extra_args=None, cache=None):
        super().__init__()
        self.apply_fn = apply_fn
        self.findings = findings
        self.extra_args = extra_args or {}
        self.cache = cache
        import threading
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        ok = fail = 0
        total = len(self.findings)
        touched_folders: set = set()
        try:
            for i, f in enumerate(self.findings, 1):
                if self.stop_event.is_set():
                    self._finalize(touched_folders)
                    self.stopped.emit(ok, fail)
                    return
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
                    touched_folders.add(Path(f.path).parent)
                    if self.cache is not None:
                        out = core.get_apply_output_path(f)
                        if out is not None and out.exists():
                            self.cache.mark_phase_done(out, f.phase)
                else:
                    fail += 1
                self.progress.emit(i, total)
            self._finalize(touched_folders)
            self.finished.emit(ok, fail)
        except Exception as e:
            self.error.emit(str(e))

    def _finalize(self, touched_folders: set) -> None:
        if self.cache is not None and touched_folders:
            self.cache.save()


class SyncCopyWorker(QObject):
    """Copy new/updated/moved findings from source to USB in a background thread."""
    progress = pyqtSignal(int, int)
    current_file = pyqtSignal(str)
    finished = pyqtSignal(int, int, list)   # ok, fail, duplicated_usb_paths
    stopped = pyqtSignal(int, int, list)
    error = pyqtSignal(str)

    def __init__(self, findings: list, tracker: "sync.SyncTracker"):
        super().__init__()
        self.findings = findings
        self.tracker = tracker
        import threading
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        ok = fail = 0
        duplicated: list = []   # old USB paths that were duplicated (user may want to delete)
        total = len(self.findings)
        try:
            for i, f in enumerate(self.findings, 1):
                if self.stop_event.is_set():
                    self.tracker.save()
                    self.stopped.emit(ok, fail, duplicated)
                    return
                self.current_file.emit(Path(f.source_path).name)
                if f.status == "deleted":
                    result = sync.apply_delete_usb(f, self.tracker)
                else:
                    if f.status == "moved":
                        duplicated.append(str(f.usb_path))
                    result = sync.apply_copy(f, self.tracker)
                if result:
                    ok += 1
                else:
                    fail += 1
                self.progress.emit(i, total)
            self.tracker.save()
            self.finished.emit(ok, fail, duplicated)
        except Exception as e:
            self.error.emit(str(e))


class BootstrapWorker(QObject):
    """Register source files ≤ cutoff date as synced (CCC handoff) in a background thread."""
    progress = pyqtSignal(int, int)
    current_file = pyqtSignal(str)
    finished = pyqtSignal(int, int)   # registered, skipped_new
    error = pyqtSignal(str)

    def __init__(self, tracker: "sync.SyncTracker", synced_at: str):
        super().__init__()
        self.tracker = tracker
        self.synced_at = synced_at
        import threading
        self.stop_event = threading.Event()

    def run(self) -> None:
        try:
            registered, skipped_new = sync.bootstrap_all(
                self.tracker,
                self.synced_at,
                progress_cb=self.progress.emit,
                file_cb=self.current_file.emit,
                stop_event=self.stop_event,
            )
            self.tracker.save()
            self.finished.emit(registered, skipped_new)
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

    def __init__(self, columns: List[str], editable_col: Optional[int] = None,
                 editable_cols: Optional[List[int]] = None):
        super().__init__()
        self.columns = ["✓"] + columns
        # Support either a single editable_col or a list of editable_cols.
        if editable_cols is not None:
            self._editable_cols: set = set(editable_cols)
            self.editable_col = editable_cols[0] if editable_cols else None
        elif editable_col is not None:
            self._editable_cols = {editable_col}
            self.editable_col = editable_col
        else:
            self._editable_cols = set()
            self.editable_col = None
        self.findings: List[core.Finding] = []  # full list, may exceed display cap

        self.setColumnCount(len(self.columns))
        self.setHorizontalHeaderLabels(self.columns)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
            if self._editable_cols else QTableWidget.EditTrigger.NoEditTriggers
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
                if col not in self._editable_cols:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(row, col, item)

        self.setUpdatesEnabled(True)
        if display:
            # Cap each column at 35% of viewport width so long paths can't push
            # other columns off screen. Last column is always left to stretch.
            max_w = max(120, int(self.viewport().width() * 0.35))
            for col in range(self.columnCount() - 1):
                self.resizeColumnToContents(col)
                if self.columnWidth(col) > max_w:
                    self.setColumnWidth(col, max_w)

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
        return self.get_col_value(finding, self.editable_col)

    def get_col_value(self, finding: core.Finding, col: int) -> str:
        try:
            row = self.findings.index(finding)
            if row >= _TABLE_DISPLAY_CAP:
                return ""
            return self.item(row, col).text()
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

        self.skip_btn = QPushButton("🚫 Skip Selected")
        self.skip_btn.clicked.connect(self.skip_selected)
        self.skip_btn.setEnabled(False)
        self.skip_btn.setToolTip(
            "Mark selected files as 'already handled' in the cache — "
            "they won't appear in future scans unless the file changes."
        )
        self.skip_btn.setStyleSheet("padding: 6px 12px;")
        toolbar.addWidget(self.skip_btn)

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

    def start_scan(self, phase_label: str = ""):
        if not self.main_window.check_base_dir():
            return

        self._reset_scan_status()
        self.main_window.set_busy(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._status.setVisible(True)
        self._current_file_label.setVisible(True)
        self.stop_btn.setVisible(True)
        self.count_label.setText(phase_label or "Scanning…")
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
        if n == 0:
            msg = "0 findings — nothing to do in this pass."
        else:
            msg = f"{n} finding{'s' if n != 1 else ''} — review the table and apply as needed."
        self.apply_btn.setEnabled(n > 0)
        self.skip_btn.setEnabled(n > 0)
        QMessageBox.information(self, f"{self.title} — Scan Complete", msg)

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
            action = "apply" if self._applying else "scan"
            self.main_window.log(f"[{self.title}] Stop requested — finishing current file…" if self._applying else f"[{self.title}] Stop requested — finishing current batch…")

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
        self.stop_btn.setVisible(True)
        self.count_label.setText(f"0 / {len(selected):,} files")
        self.main_window.log(
            f"[{self.title}] Applying to {len(selected)} items..."
        )

        extra = self.get_apply_extra(selected)

        self.thread = QThread()
        self.worker = ApplyWorker(self.apply_fn(), selected, extra,
                                  cache=self.main_window.cache)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.current_file.connect(self._on_apply_file)
        self.worker.finished.connect(self.on_apply_done)
        self.worker.stopped.connect(self.on_apply_stopped)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.stopped.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def get_apply_extra(self, selected: list) -> dict:
        return {}

    def _on_apply_file(self, filename: str):
        self._current_file_label.setText(f"⟳  {filename}")

    def _hide_apply_ui(self):
        self._applying = False
        self.progress.setVisible(False)
        self._current_file_label.setVisible(False)
        self._current_file_label.setText("")
        self.stop_btn.setVisible(False)
        self.stop_btn.setText("⏹ Stop")
        self.stop_btn.setEnabled(True)
        self.main_window.set_busy(False)

    def skip_selected(self):
        """Mark selected findings as phase-done in the cache without applying anything.
        They won't appear in future scans unless the file itself changes."""
        selected = self.table.get_selected_findings()
        if not selected:
            QMessageBox.information(self, "Nothing Selected", "No items are checked.")
            return
        cache = self.main_window.cache
        if cache is None:
            QMessageBox.warning(self, "No Cache", "No drive loaded.")
            return
        skipped = 0
        for f in selected:
            if f.path.exists():
                cache.mark_phase_done(f.path, f.phase)
                skipped += 1
        cache.save()
        # Remove skipped findings from the table.
        skipped_ids = {id(f) for f in selected}
        self.findings = [f for f in self.findings if id(f) not in skipped_ids]
        self.table.set_findings(self.findings, self.row_builder)
        n = len(self.findings)
        self.count_label.setText(f"{n} findings" if n else "0 findings")
        self.apply_btn.setEnabled(bool(self.findings))
        self.skip_btn.setEnabled(bool(self.findings))
        self.main_window.log(
            f"[{self.title}] Skipped {skipped} file{'s' if skipped != 1 else ''} — "
            "won't appear in future scans unless file changes."
        )
        QMessageBox.information(
            self, "Skipped",
            f"{skipped} file{'s' if skipped != 1 else ''} marked as skipped.\n"
            "They won't appear in future scans."
        )

    def _show_help(self):
        QMessageBox.information(self, self.title, self._help_text)

    def _file_cols(self, f: "core.Finding"):
        """Return (folder_rel, filename, size_str) for a finding."""
        try:
            folder = str(f.path.parent.relative_to(self.main_window.base_dir))
        except (ValueError, AttributeError):
            folder = str(f.path.parent)
        try:
            size = core.format_bytes(f.path.stat().st_size)
        except OSError:
            size = "—"
        return folder, f.path.name, size

    def on_apply_done(self, ok: int, fail: int):
        self._hide_apply_ui()
        self.main_window.log(
            f"[{self.title}] Done: {ok} succeeded, {fail} failed."
        )
        QMessageBox.information(self, "Complete", f"{ok} succeeded, {fail} failed.")

    def on_apply_stopped(self, ok: int, fail: int):
        self._hide_apply_ui()
        if self.main_window.cache:
            self.main_window.cache.save()
        self.main_window.log(
            f"[{self.title}] Apply stopped. {ok} succeeded, {fail} failed. Cache saved."
        )


# ============================================================================
# Phase 1 — Format Normalization (non-WAV + wrong SR/bits combined)
# ============================================================================

class Phase1Tab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 1, "Wav Format",
            "Convert non-WAV files and fix WAVs at wrong sample rate or bit depth "
            "→ 16-bit 48 kHz WAV in one pass.",
            help_text=(
                "Scans your entire sample library for two classes of audio problems:\n\n"
                "1. Non-WAV files (MP3, AIFF, FLAC, M4A, OGG, WMA, ALAC, MP4 …)\n"
                "   → Converts to WAV. Original deleted if delete_original=true in config.json.\n\n"
                "2. WAV files at the wrong sample rate or bit depth\n"
                "   → Re-encodes in place. No copy is made.\n\n"
                "Target always: 48 000 Hz, 16-bit PCM (pcm_s16le).\n"
                "Files already at 48 kHz AND 16-bit WAV are skipped entirely.\n\n"
                "The 'Issue' column shows what is wrong with each file.\n\n"
                "Tip: if you recently added MP4 or another new format and Fast scan\n"
                "is on, uncheck Fast scan once so every folder gets a fresh walk.\n"
                "After that first full pass, Fast scan is safe to re-enable."
            ),
        )

    def build_table(self):
        table = FindingsTable(["Path", "File", "Issue", "Current", "Target", "Size"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def row_builder(self, f):
        folder, name, size = self._file_cols(f)
        ftype = f.extra.get("type", "wav_format")
        issue = "Non-WAV → WAV" if ftype == "non_wav" else "Wrong format (SR/bits)"
        return [folder, name, issue, f.current, f.target, size]

    def scan_fn(self):
        return (core.scan_phase_1,
                (self.main_window.base_dir, self.main_window.cache,
                 self.main_window.only_new))

    def apply_fn(self):
        return core.apply_phase_1


# ============================================================================
# Names tab — Prefix Removal (Phase 2) + Long Filename Cleanup (Phase 3)
# ============================================================================

class FileCleanupTab(PhaseTab):
    """Tab 4 — delete junk files, collapse single-child folder layers."""

    def __init__(self, main_window):
        super().__init__(
            main_window, 9, "File Cleanup",
            "Delete DAW-specific and unreadable files, then flatten unnecessary folder layers.",
            help_text=(
                "Two passes in one tab:\n\n"
                "Junk Files — deletes files with extensions that the S-4 cannot use:\n"
                "  .als .adg  Ableton Live Set / device rack (references audio, no embed)\n"
                "  .nki .nkm .nkr  Kontakt instrument / multiscript / resource\n"
                "  .exs  Logic EXS24 sampler patch\n"
                "  .sxt  Reason NN-XT patch\n"
                "  .sfz  SFZ sampler definition\n"
                "  .rx2  ReCycle REX2 loop\n"
                "  .ncw  NI Compressed WAV (ffmpeg cannot decode)\n"
                "  .mid .midi  MIDI data (not audio)\n"
                "  .asd  Ableton analysis sidecar\n"
                "  .mp4  Video files (instruction videos in Docs/ folders)\n"
                "  .fst .agr .dwp .mxgrp .nbkt .kong .pgm .patch .cfg .xpm .snd\n\n"
                "Folder Collapse — finds folders containing exactly one subfolder and\n"
                "  nothing else. Moves the contents up one level and removes the empty shell.\n"
                "  Example: Drums/Kicks/kick.wav → Drums/kick.wav\n\n"
                "After applying, empty folders and newly single-child folders are cleaned\n"
                "up automatically (cascade cleanup)."
            ),
        )

    def build_table(self):
        table = FindingsTable(["Open Folder", "Type", "Path", "File", "Detail", "Size"])
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        table.setColumnWidth(1, 36)
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        return table

    def on_scan_done(self, findings):
        super().on_scan_done(findings)
        self._add_open_buttons()

    def _add_open_buttons(self):
        import subprocess as _sp
        displayed = min(len(self.findings), _TABLE_DISPLAY_CAP)
        for row in range(displayed):
            f = self.findings[row]
            reveal_path = str(f.path if f.path.is_dir() else f.path.parent)
            btn = QPushButton("📂")
            btn.setFlat(True)
            btn.setFixedSize(32, 24)
            btn.setToolTip(reveal_path)
            btn.clicked.connect(
                lambda checked=False, p=reveal_path: _sp.Popen(["open", "-R", p])
            )
            self.table.setCellWidget(row, 1, btn)

    def row_builder(self, f):
        if f.phase == 9:
            ext = f.extra.get("ext", f.path.suffix.lower())
            try:    folder = str(f.path.parent.relative_to(self.main_window.base_dir))
            except ValueError: folder = str(f.path.parent)
            size = core.format_bytes(f.savings_bytes) if f.savings_bytes else "—"
            return ["", "Junk", folder, f.path.name, ext, size]
        else:
            child = f.extra.get("child", "")
            count = f.extra.get("child_count", 0)
            try:    folder = str(f.path.relative_to(self.main_window.base_dir))
            except ValueError: folder = str(f.path)
            return ["", "Collapse", folder, child, f"Move up — remove this layer ({count} items)", ""]

    def scan_fn(self):
        base     = self.main_window.base_dir
        only_new = self.main_window.only_new

        def combined(base_dir, only_new, progress_cb=None, file_cb=None, stop_event=None):
            n_phases = 2
            phase_idx = [0]

            def phase_cb(done, total):
                if progress_cb and total > 0:
                    progress_cb(phase_idx[0] * total + done, n_phases * total)

            cb = phase_cb if progress_cb else None
            findings = core.scan_junk_files(base_dir, only_new, cb, file_cb, stop_event)
            phase_idx[0] = 1
            if not (stop_event and stop_event.is_set()):
                findings += core.scan_phase_8(base_dir, only_new, cb, file_cb, stop_event)
            return findings

        return (combined, (base, only_new))

    def apply_fn(self):
        def dispatch(finding):
            if finding.phase == 9:
                return core.apply_delete_junk(finding)
            return core.apply_phase_8(finding)
        return dispatch

    def on_apply_done(self, ok: int, fail: int):
        # Run cascade cleanup after junk/collapse apply — removes empty folders and
        # any newly-created single-child layers.
        base = self.main_window.base_dir
        empty_deleted = flattened = 0
        if base and base.exists():
            try:
                empty_deleted, flattened = core.cascade_cleanup(base)
            except Exception:
                pass
        self._hide_apply_ui()
        self.main_window.log(
            f"[{self.title}] Done: {ok} succeeded, {fail} failed. "
            f"Cascade: {empty_deleted} empty folders removed, {flattened} layers flattened."
        )
        extra = ""
        if empty_deleted or flattened:
            extra = f"\n\nCascade cleanup: {empty_deleted} empty folder{'s' if empty_deleted != 1 else ''} removed, {flattened} folder layer{'s' if flattened != 1 else ''} flattened."
        QMessageBox.information(
            self, "Complete",
            f"{ok} succeeded, {fail} failed.{extra}"
        )


class NameCleanupTab(PhaseTab):
    """Tab 5 — Long prefix removal, BPM relabeling, Non-ASCII romanization."""

    def __init__(self, main_window):
        super().__init__(
            main_window, 7, "Name Cleanup",
            "Strip long shared prefixes, add missing 'bpm' labels, and romanize non-ASCII filenames.",
            help_text=(
                f"Three name-related passes:\n\n"
                f"Long Prefix — finds folders where files exceed the S-4's display limit\n"
                f"  ({config.S4_DISPLAY_LIMIT} chars). Detects the shared prefix among all files\n"
                f"  in that folder and offers to strip it, bringing long names under the limit.\n"
                f"  Example: 'Lurka - Lurka Sample Pack - 18 LurkaKick.wav'\n"
                f"    prefix detected: 'Lurka - Lurka Sample Pack - '\n"
                f"    result: '18 LurkaKick.wav'\n"
                f"  Edit the 'Edit' column to correct the detected prefix before applying.\n\n"
                "BPM Relabel — finds WAV files whose names start with a bare number\n"
                "  that looks like a BPM but is missing the 'bpm' label.\n"
                "  Example: '120_kick.wav' → '120bpm_kick.wav'\n\n"
                "Non-ASCII — finds filenames with non-English characters and suggests\n"
                "  ASCII transliterations so the S-4 can read them:\n"
                "  • Chinese → pinyin (e.g. 踢鼓 → tigu)\n"
                "  • Accented Latin → stripped accent (e.g. Café → Cafe)\n\n"
                "Edit the 'Edit' column to customise any value before applying.\n"
                "For Long Prefix rows, the edit field holds the prefix to strip.\n"
                "For BPM / Non-ASCII rows, it holds the full new filename."
            ),
        )

    def build_table(self):
        table = FindingsTable(
            ["Open Folder", "Type", "File / Folder", "Detail", "Edit"],
            editable_col=5,
        )
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        table.setColumnWidth(1, 36)
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        return table

    def on_scan_done(self, findings):
        super().on_scan_done(findings)
        self._add_open_buttons()

    def _add_open_buttons(self):
        import subprocess as _sp
        displayed = min(len(self.findings), _TABLE_DISPLAY_CAP)
        for row in range(displayed):
            f = self.findings[row]
            reveal_path = str(f.path if f.path.is_dir() else f.path.parent)
            btn = QPushButton("📂")
            btn.setFlat(True)
            btn.setFixedSize(32, 24)
            btn.setToolTip(reveal_path)
            btn.clicked.connect(
                lambda checked=False, p=reveal_path: _sp.Popen(["open", "-R", p])
            )
            self.table.setCellWidget(row, 1, btn)

    def row_builder(self, f):
        if f.phase == 2:
            prefix = f.extra.get("prefix", f.current)
            n_long = f.extra.get("n_long", 0)
            n_total = f.extra.get("n_total", 0)
            try:    loc = str(f.path.relative_to(self.main_window.base_dir))
            except ValueError: loc = str(f.path)
            return ["", "Long Prefix", loc,
                    f"{n_long} of {n_total} files too long — strip prefix",
                    prefix]
        elif f.phase == 6:
            return ["", "BPM Relabel", f.current, f"→ {f.target}", f.target]
        else:
            return ["", "Non-ASCII", f.current, f"→ {f.target}", f.target]

    def scan_fn(self):
        base     = self.main_window.base_dir
        only_new = self.main_window.only_new

        def combined(base_dir, only_new, progress_cb=None, file_cb=None, stop_event=None):
            n_phases = 3
            phase_idx = [0]

            def phase_cb(done, total):
                if progress_cb and total > 0:
                    progress_cb(phase_idx[0] * total + done, n_phases * total)

            cb = phase_cb if progress_cb else None
            findings = core.scan_long_prefix(base_dir, only_new, cb, file_cb, stop_event)
            phase_idx[0] = 1
            if not (stop_event and stop_event.is_set()):
                findings += core.scan_bpm_relabel(base_dir, only_new, cb, file_cb, stop_event)
            phase_idx[0] = 2
            if not (stop_event and stop_event.is_set()):
                findings += core.scan_phase_7(base_dir, only_new, cb, file_cb, stop_event)
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

        # Build edit-value map: col 5 holds prefix (phase 2) or new name (phases 6/7).
        edit_map = {id(f): self.table.get_edit_value(f) for f in selected}
        log   = self.main_window.log
        cache = self.main_window.cache

        def apply_fn(finding):
            edited = edit_map.get(id(finding), "").strip()
            if finding.phase == 2:
                return bool(core.apply_phase_2(
                    finding,
                    override_prefix=edited or None,
                    cache=cache,
                    log_cb=lambda msg: log(f"[{self.title}] {msg}"),
                ))
            if finding.phase == 6:
                return core.apply_phase_6(finding, edited)
            return core.apply_phase_7(finding, edited)

        self.main_window.set_busy(True)
        self._applying = True
        self.progress.setVisible(True)
        self.progress.setMaximum(len(selected))
        self.progress.setValue(0)
        self._current_file_label.setText("")
        self._current_file_label.setVisible(True)
        self.stop_btn.setVisible(True)
        self.count_label.setText(f"0 / {len(selected):,} items")
        self.main_window.log(f"[{self.title}] Applying to {len(selected)} items...")
        self.thread = QThread()
        self.worker = ApplyWorker(apply_fn, selected, cache=self.main_window.cache)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.current_file.connect(self._on_apply_file)
        self.worker.finished.connect(self.on_apply_done)
        self.worker.stopped.connect(self.on_apply_stopped)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.stopped.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def apply_fn(self):
        pass  # overridden by start_apply


# ============================================================================
# File Size tab — Stereo → Mono (Phase 4) + Silence Removal (Phase 5)
# ============================================================================

class SilenceTab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 5, "Silence Remover",
            "Trim leading and trailing silence from samples.",
            help_text=(
                "Detects and removes leading/trailing silence from WAV files.\n\n"
                f"Silence threshold: {config.SILENCE_THRESHOLD_DB} dBFS\n"
                f"Minimum silence duration: {config.SILENCE_MIN_DURATION}s\n\n"
                "Both thresholds are configurable in config.json.\n\n"
                "The file is rewritten in place using ffmpeg; "
                "the original is not kept."
            ),
        )

    def build_table(self):
        table = FindingsTable(["Path", "File", "Lead silence", "Trail silence", "Savings", "Size"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def row_builder(self, f):
        folder, name, size = self._file_cols(f)
        lead  = f.extra.get("lead",  0.0)
        trail = f.extra.get("trail", 0.0)
        lead_s  = f"{lead:.2f}s"  if lead  >= config.SILENCE_MIN_DURATION else "—"
        trail_s = f"{trail:.2f}s" if trail >= config.SILENCE_MIN_DURATION else "—"
        return [folder, name, lead_s, trail_s,
                core.format_bytes(f.savings_bytes), size]

    def scan_fn(self):
        base     = self.main_window.base_dir
        cache    = self.main_window.cache
        only_new = self.main_window.only_new
        return (core.scan_phase_5, (base, cache, only_new))

    def apply_fn(self):
        return core.apply_phase_5


class StereoMonoTab(PhaseTab):
    def __init__(self, main_window):
        super().__init__(
            main_window, 4, "Fake Stereo to Mono",
            "Convert fake-stereo files to mono. Saves ~50 % file size per converted file.",
            help_text=(
                "Detects 'fake stereo' files where L and R channels carry the same signal.\n\n"
                "  • Dual mono (auto-selected): L−R diff ≤ −90 dBFS\n"
                "  • One-sided (auto-selected): one channel ≥ 40 dB quieter\n"
                "  • Near-mono (Loose mode, opt-in): diff between −90 and −60 dBFS\n\n"
                "Enable Loose mode to also surface near-mono files "
                "(shown unchecked — review carefully)."
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
        table = FindingsTable(["Path", "File", "Classification", "Savings", "Size"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def row_builder(self, f):
        folder, name, size = self._file_cols(f)
        cls = f.extra.get("classification", "?")
        cls_pretty = {
            "dual_mono": "Dual mono",
            "one_side":  f"One-sided ({f.extra.get('keep_channel', '?')} only)",
            "near_mono": "Near-mono",
        }.get(cls, cls)
        return [folder, name, cls_pretty,
                core.format_bytes(f.savings_bytes), size]

    def scan_fn(self):
        base     = self.main_window.base_dir
        cache    = self.main_window.cache
        only_new = self.main_window.only_new
        include_near_mono = self.include_near_mono
        def run(base_dir, cache, only_new, progress_cb=None, file_cb=None, stop_event=None):
            return core.scan_phase_4(base_dir, cache, only_new, include_near_mono,
                                     progress_cb, file_cb, stop_event)
        return (run, (base, cache, only_new))

    def apply_fn(self):
        return core.apply_phase_4


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
        table = FindingsTable(
            ["Path", "File", "BPM", "Confidence", "Duration", "New Name (editable)"],
            editable_col=6,
        )
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def row_builder(self, f):
        folder, name, _ = self._file_cols(f)
        bpm      = f.extra.get("bpm", "?")
        conf_lbl = f.extra.get("conf_label", "?")
        dur      = f.extra.get("duration")
        return [
            folder, name,
            str(int(bpm)) if isinstance(bpm, (int, float)) else str(bpm),
            conf_lbl,
            f"{dur:.1f}s" if dur is not None else "—",
            f.target,
        ]

    def scan_fn(self):
        base     = self.main_window.base_dir
        cache    = self.main_window.cache
        only_new = self.main_window.only_new
        return (core.scan_phase_6, (base, cache, only_new))

    def get_apply_extra(self, selected):
        new_names = {}
        for f in selected:
            edited = self.table.get_edit_value(f)
            new_names[id(f)] = edited if edited else f.target
        return {"new_names": new_names}

    def apply_fn(self):
        return core.apply_phase_6


# ============================================================================
# Pair configuration dialog
# ============================================================================

class PairConfigDialog(QDialog):
    """View, add, edit, and remove sync pairs. Saves changes to config.json."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Sync Pairs")
        self.setMinimumWidth(720)
        self._pair_rows: list = []

        layout = QVBoxLayout(self)

        info = QLabel(
            "Each pair copies files from a Mac source folder to a USB folder. "
            "Changes are saved to config.json and take effect immediately."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888; padding-bottom: 6px;")
        layout.addWidget(info)

        # Scrollable list of existing pairs
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidget(self._list_widget)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(120)
        scroll.setMaximumHeight(280)
        layout.addWidget(scroll)

        for p in config.SYNC_PAIRS:
            self._add_row(p["label"], str(p["source"]), str(p["usb"]))

        if not config.SYNC_PAIRS:
            self._list_layout.addWidget(QLabel("No pairs yet — add one below."))

        # Add-new-pair form
        add_frame = QFrame()
        add_frame.setStyleSheet("QFrame{border:1px solid #333;border-radius:4px;padding:4px;}")
        add_fl = QVBoxLayout(add_frame)
        add_fl.addWidget(QLabel("Add new pair:"))
        form_row = QHBoxLayout()
        self._nl = QLineEdit(); self._nl.setPlaceholderText("Label")
        self._nl.setFixedWidth(130)
        self._ns = QLineEdit(); self._ns.setPlaceholderText("Mac source folder")
        sb = QPushButton("…"); sb.setFixedWidth(28)
        sb.clicked.connect(lambda: self._browse(self._ns))
        self._nu = QLineEdit(); self._nu.setPlaceholderText("USB folder")
        ub = QPushButton("…"); ub.setFixedWidth(28)
        ub.clicked.connect(lambda: self._browse(self._nu))
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_pair)
        for w in (self._nl, QLabel("Source:"), self._ns, sb,
                  QLabel("USB:"), self._nu, ub, add_btn):
            form_row.addWidget(w)
        add_fl.addLayout(form_row)
        layout.addWidget(add_frame)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _browse(self, line_edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Choose Folder", line_edit.text())
        if d:
            line_edit.setText(d)

    def _add_row(self, label: str, source: str, usb: str):
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        le_lbl = QLineEdit(label); le_lbl.setFixedWidth(130)
        le_src = QLineEdit(source)
        le_usb = QLineEdit(usb)

        def browse_src(): self._browse(le_src)
        def browse_usb(): self._browse(le_usb)
        sb = QPushButton("…"); sb.setFixedWidth(28); sb.clicked.connect(browse_src)
        ub = QPushButton("…"); ub.setFixedWidth(28); ub.clicked.connect(browse_usb)
        rm = QPushButton("✕"); rm.setFixedWidth(28)
        rm.setStyleSheet("color:#c0392b;")

        for w in (le_lbl, QLabel("→"), le_src, sb, le_usb, ub, rm):
            row.addWidget(w if not isinstance(w, str) else QLabel(w))
        # stretch source+usb fields
        row.setStretchFactor(le_src, 1)
        row.setStretchFactor(le_usb, 1)

        entry = {"label": le_lbl, "source": le_src, "usb": le_usb, "widget": row_w}
        self._pair_rows.append(entry)
        self._list_layout.addWidget(row_w)

        def remove():
            self._pair_rows.remove(entry)
            row_w.setParent(None)
        rm.clicked.connect(remove)

    def _add_pair(self):
        label  = self._nl.text().strip()
        source = self._ns.text().strip()
        usb    = self._nu.text().strip()
        if not label or not source or not usb:
            QMessageBox.warning(self, "Incomplete", "Fill in Label, Source, and USB folder.")
            return
        self._add_row(label, source, usb)
        self._nl.clear(); self._ns.clear(); self._nu.clear()

    def _save(self):
        import json as _json
        cfg_path = Path(__file__).parent.parent / "config.json"
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Error", f"Cannot read config.json:\n{e}")
            return

        new_pairs = [
            {"label": r["label"].text().strip(),
             "source": r["source"].text().strip(),
             "usb":    r["usb"].text().strip()}
            for r in self._pair_rows
            if r["label"].text().strip() and r["source"].text().strip() and r["usb"].text().strip()
        ]
        cfg["sync_pairs"] = new_pairs
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, indent=2)
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Cannot save config.json:\n{e}")
            return

        config.SYNC_PAIRS = [
            {"label": p["label"], "source": Path(p["source"]), "usb": Path(p["usb"])}
            for p in new_pairs
        ]
        self.accept()


# ============================================================================
# Sync tab — must be first tab; independent of loaded drive
# ============================================================================

class SyncTab(QWidget):
    """Tab 0: sync new/changed Mac source files to USB before processing.

    Operates independently of the drive loaded for processing.  The tracker
    (.s4_sync.json at the project root) persists across drive remounts.
    """

    # Emitted when sync+convert is requested so MainWindow can chain Phase 1.
    request_phase1_scan = pyqtSignal()

    STATUS_COLORS = {
        "new":     "#1a7a1a",
        "updated": "#b36200",
        "moved":   "#1a4fa0",
        "deleted": "#a01a1a",
    }

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.findings: List[sync.SyncFinding] = []
        self.thread: Optional[QThread] = None
        self._running: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        desc = QLabel(
            "Copy new and updated source files from Mac to USB.  "
            "Files already converted or renamed by this app are never overwritten — "
            "only genuinely new or changed source files are copied."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(desc)

        # Per-pair status display — always shows source → USB paths
        self._pair_status_labels: dict = {}   # label → status QLabel
        self._pair_path_labels: dict = {}     # label → path QLabel
        self._pairs_area = QWidget()
        self._pairs_layout = QVBoxLayout(self._pairs_area)
        self._pairs_layout.setContentsMargins(0, 2, 0, 2)
        self._pairs_layout.setSpacing(2)
        self._rebuild_pair_display()
        layout.addWidget(self._pairs_area)

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

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.clicked.connect(self._request_stop)
        self.stop_btn.setVisible(False)
        toolbar.addWidget(self.stop_btn)

        self.apply_btn = QPushButton("✓ Copy Selected")
        self.apply_btn.clicked.connect(self.start_copy)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet(
            "background-color: #2c7a3d; color: white; padding: 6px 12px;"
        )
        toolbar.addWidget(self.apply_btn)

        self.sync_convert_btn = QPushButton("⚡ Sync + Convert")
        self.sync_convert_btn.setToolTip(
            "Scan for new files, copy them all to USB, then immediately run the "
            "Wav Format scan so everything is ready in one step."
        )
        self.sync_convert_btn.clicked.connect(self.start_sync_and_convert)
        self.sync_convert_btn.setStyleSheet(
            "background-color: #1e5a8a; color: white; padding: 6px 12px;"
        )
        toolbar.addWidget(self.sync_convert_btn)

        self.configure_btn = QPushButton("Configure Pairs…")
        self.configure_btn.setToolTip("Add, edit, or remove source → USB sync pairs.")
        self.configure_btn.clicked.connect(self._open_pair_config)
        toolbar.addWidget(self.configure_btn)

        self.bootstrap_btn = QPushButton("Register Existing…")
        self.bootstrap_btn.setToolTip(
            "One-time setup: if your USB already has files from a previous transfer, "
            "scan source + USB to register what's already there. "
            "New files added to the Mac after this point will appear on the next Scan."
        )
        self.bootstrap_btn.clicked.connect(self.start_bootstrap)
        toolbar.addWidget(self.bootstrap_btn)

        layout.addLayout(toolbar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self._current_file_label = QLabel("")
        self._current_file_label.setVisible(False)
        mono = QFont("Menlo, Consolas, monospace")
        mono.setPointSize(11)
        self._current_file_label.setFont(mono)
        self._current_file_label.setStyleSheet("color: #444; padding-left: 24px;")
        layout.addWidget(self._current_file_label)

        self.table = FindingsTable(["Pair", "Status", "File / Path", "Size"])
        layout.addWidget(self.table)

        self._refresh_pair_labels()

    # ------------------------------------------------------------------
    # Pair display
    # ------------------------------------------------------------------

    def _rebuild_pair_display(self) -> None:
        """Recreate the pair status rows (called on init and after Configure Pairs)."""
        while self._pairs_layout.count():
            item = self._pairs_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._pair_status_labels.clear()
        self._pair_path_labels.clear()

        if not config.SYNC_PAIRS:
            no_lbl = QLabel("No sync pairs configured — click Configure Pairs… to add one.")
            no_lbl.setStyleSheet("color: #c0392b; font-size: 11px;")
            self._pairs_layout.addWidget(no_lbl)
            return

        def _abbrev(p: str, n: int = 60) -> str:
            return ("…" + p[-(n - 1):]) if len(p) > n else p

        for pair in config.SYNC_PAIRS:
            row_w = QWidget()
            row = QHBoxLayout(row_w)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)

            col_w = QWidget()
            col = QVBoxLayout(col_w)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(1)

            status_lbl = QLabel(f"{pair['label']}: …")
            status_lbl.setStyleSheet("color: #555; font-size: 11px;")

            src, usb = str(pair["source"]), str(pair["usb"])
            path_lbl = QLabel(f"  {_abbrev(src)}  →  {_abbrev(usb)}")
            path_lbl.setStyleSheet("color: #3a3a3a; font-size: 10px;")
            path_lbl.setToolTip(f"Source: {src}\nUSB:    {usb}")

            col.addWidget(status_lbl)
            col.addWidget(path_lbl)
            row.addWidget(col_w)
            row.addStretch()
            self._pairs_layout.addWidget(row_w)

            self._pair_status_labels[pair["label"]] = status_lbl
            self._pair_path_labels[pair["label"]] = path_lbl

    def _open_pair_config(self) -> None:
        dlg = PairConfigDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._rebuild_pair_display()
            self._refresh_pair_labels()

    def _any_usb_mounted(self) -> bool:
        """Return True if any pair's USB mount point is accessible.

        Walks up from the configured USB path to the mount point, but stops
        before filesystem boundary dirs (/Volumes, /mnt, /media, /) so that
        the always-present /Volumes directory does not count as "mounted".
        """
        _BOUNDARIES = {Path("/"), Path("/Volumes"), Path("/mnt"), Path("/media")}
        for p in config.SYNC_PAIRS:
            candidate = Path(p["usb"])
            while not candidate.exists():
                parent = candidate.parent
                if parent == candidate or parent in _BOUNDARIES:
                    candidate = None
                    break
                candidate = parent
            if candidate and candidate.exists() and candidate not in _BOUNDARIES:
                return True
        return False

    def _update_scan_buttons(self) -> None:
        """Enable action buttons using the same gate as all other tabs:
        drive must be loaded AND USB must be reachable."""
        has_pairs   = bool(config.SYNC_PAIRS)
        drive_ok    = self.main_window.base_dir is not None
        any_usb     = self._any_usb_mounted()
        can_act     = has_pairs and drive_ok and any_usb
        self.scan_btn.setEnabled(can_act)
        self.sync_convert_btn.setEnabled(can_act)
        self.bootstrap_btn.setEnabled(can_act)
        if not has_pairs:
            tip = "No sync pairs configured — click Configure Pairs… to add one."
        elif not drive_ok:
            tip = "Click Load to load the drive first."
        elif not any_usb:
            tip = "USB drive not mounted."
        else:
            tip = ""
        self.scan_btn.setToolTip(tip)
        self.bootstrap_btn.setToolTip(tip or
            "One-time setup: if your USB already has files from a previous transfer, "
            "scan source + USB to register what's already there. "
            "New files added to the Mac after this point will appear on the next Scan.")
        if tip:
            self.sync_convert_btn.setToolTip(tip)

    def _refresh_pair_labels(self) -> None:
        tracker = self.main_window.sync_tracker
        for pair in config.SYNC_PAIRS:
            lbl = self._pair_status_labels.get(pair["label"])
            if lbl is None:
                continue
            count = tracker.count_for_pair(pair["label"])
            last = tracker.last_sync_time(pair["label"])
            src_ok = pair["source"].exists()
            usb_ok = pair["usb"].exists()
            if not src_ok or not usb_ok:
                missing = [n for n, ok in [("source", src_ok), ("USB", usb_ok)] if not ok]
                lbl.setText(f"⚠ {pair['label']}: {', '.join(missing)} not mounted")
                lbl.setStyleSheet("color: #c0392b; font-size: 11px;")
            else:
                last_str = ""
                if last:
                    try:
                        from datetime import datetime as _dt
                        last_str = f" · last sync {_dt.fromisoformat(last).strftime('%Y-%m-%d')}"
                    except ValueError:
                        last_str = f" · last sync {last[:10]}"
                lbl.setText(f"✓ {pair['label']}: {count:,} tracked{last_str}")
                lbl.setStyleSheet("color: #2c7a3d; font-size: 11px;")
        self._update_scan_buttons()

    # ------------------------------------------------------------------
    # Table row builder
    # ------------------------------------------------------------------

    def _row_builder(self, f: sync.SyncFinding) -> list:
        if f.status == "moved":
            path_str = f"{f.rel_path}  →  {f.moved_to_rel_path}"
        else:
            path_str = f.rel_path
        size_str = core.format_bytes(f.size) if f.size else "—"
        return [f.pair_label, f.status.upper(), path_str, size_str]

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def start_scan(self) -> None:
        if not config.SYNC_PAIRS:
            QMessageBox.warning(self, "No Sync Pairs", "No sync pairs configured in config.json.")
            return
        self._start_scan_worker(on_done=self._on_scan_done)

    def _start_scan_worker(self, on_done, label: str = "Scanning…") -> None:
        tracker = self.main_window.sync_tracker

        def scan_fn(tracker, progress_cb=None, file_cb=None, stop_event=None):
            return sync.scan_all(tracker, progress_cb, file_cb, stop_event)

        self._set_running(True, label)
        self.progress.setValue(0)
        self.thread = QThread()
        self.worker = ScanWorker(scan_fn, tracker)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.scan_item.connect(
            lambda p: self._current_file_label.setText(f"⟳  {Path(p).name}")
        )
        self.worker.finished.connect(on_done)
        self.worker.stopped.connect(self._on_scan_stopped)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.stopped.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def _on_scan_done(self, findings: list) -> None:
        self.findings = findings
        self.table.set_findings(findings, self._row_builder)
        self._color_status_cells()
        n = len(findings)
        self.count_label.setText(f"{n} findings")
        self._set_running(False)
        self.apply_btn.setEnabled(bool(findings))
        self._refresh_pair_labels()
        self.main_window.log(f"[Sync] Scan complete: {n} findings.")
        if n == 0:
            msg = "0 findings — already up to date."
        else:
            msg = f"{n} finding{'s' if n != 1 else ''} — review the table and copy as needed."
        QMessageBox.information(self, "Sync — Scan Complete", msg)

    def _color_status_cells(self) -> None:
        """Color the Status column cells by status type."""
        from PyQt6.QtGui import QColor
        displayed = min(len(self.findings), _TABLE_DISPLAY_CAP)
        for row in range(displayed):
            f = self.findings[row]
            color = self.STATUS_COLORS.get(f.status, "#333")
            item = self.table.item(row, 2)  # Status is col 2 (after ✓ and Pair)
            if item:
                item.setForeground(QColor(color))

    def _on_scan_stopped(self) -> None:
        self._set_running(False)
        self.main_window.log("[Sync] Scan stopped.")

    # ------------------------------------------------------------------
    # Copy selected
    # ------------------------------------------------------------------

    def start_copy(self) -> None:
        selected = self.table.get_selected_findings()
        if not selected:
            QMessageBox.information(self, "Nothing Selected", "No items are checked.")
            return

        new_upd = [f for f in selected if f.status in ("new", "updated", "moved")]
        dels = [f for f in selected if f.status == "deleted"]

        msg_parts = []
        if new_upd:
            msg_parts.append(f"Copy {len(new_upd)} file(s) to USB")
        if dels:
            msg_parts.append(
                f"Delete {len(dels)} file(s) from USB "
                "(source was deleted — cannot be undone)"
            )

        reply = QMessageBox.question(
            self, "Confirm Sync",
            "\n".join(msg_parts) + "\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def on_copy_done(ok: int, fail: int, duplicated: list) -> None:
            self._remove_copied_findings(selected)
            self._on_copy_done(ok, fail, duplicated)

        self._run_copy_worker(selected, on_done=on_copy_done)

    def _run_copy_worker(self, findings: list, on_done) -> None:
        tracker = self.main_window.sync_tracker
        self._set_running(True, f"0 / {len(findings):,} files")
        self.thread = QThread()
        self.worker = SyncCopyWorker(findings, tracker)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_copy_progress)
        self.worker.current_file.connect(
            lambda name: self._current_file_label.setText(f"⟳  {name}")
        )
        self.worker.finished.connect(on_done)
        self.worker.stopped.connect(self._on_copy_stopped)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.stopped.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def _on_copy_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.count_label.setText(f"{done:,} / {total:,} files")

    def _on_copy_done(self, ok: int, fail: int, duplicated: list) -> None:
        self._set_running(False)
        self._refresh_pair_labels()
        self.main_window.log(f"[Sync] Copy done: {ok} succeeded, {fail} failed.")
        msg = f"{ok} file(s) copied successfully."
        if fail:
            msg += f"\n{fail} failed — check log."
        if duplicated:
            msg += (
                f"\n\n{len(duplicated)} file(s) were duplicated on USB (moved from source):\n"
                + "\n".join(f"  {p}" for p in duplicated[:8])
                + ("\n  …" if len(duplicated) > 8 else "")
                + "\n\nWould you like to delete these old USB copies now?"
            )
            reply = QMessageBox.question(
                self, "Delete Old USB Copies?",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._delete_old_usb_copies(duplicated)
            else:
                QMessageBox.information(self, "Sync Complete", f"{ok} file(s) copied.")
        else:
            QMessageBox.information(self, "Sync Complete", msg)

    def _delete_old_usb_copies(self, paths: list) -> None:
        deleted = skipped = 0
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
                deleted += 1
            except OSError:
                skipped += 1
        self.main_window.log(f"[Sync] Deleted {deleted} old USB copies, {skipped} skipped.")

    def _on_copy_stopped(self, ok: int, fail: int, duplicated: list) -> None:
        self._set_running(False)
        self._refresh_pair_labels()
        self.main_window.log(f"[Sync] Copy stopped. {ok} succeeded, {fail} failed.")

    def _remove_copied_findings(self, submitted: list) -> None:
        """Remove submitted findings from the table after a copy completes."""
        submitted_ids = {id(f) for f in submitted}
        self.findings = [f for f in self.findings if id(f) not in submitted_ids]
        self.table.set_findings(self.findings, self._row_builder)
        self._color_status_cells()
        n = len(self.findings)
        self.count_label.setText(f"{n} findings" if n else "0 findings")
        self.apply_btn.setEnabled(bool(self.findings))

    # ------------------------------------------------------------------
    # Sync + Convert (chained operation)
    # ------------------------------------------------------------------

    def start_sync_and_convert(self) -> None:
        if not config.SYNC_PAIRS:
            QMessageBox.warning(self, "No Sync Pairs", "No sync pairs configured in config.json.")
            return

        def after_scan(findings: list) -> None:
            self.findings = findings
            self.table.set_findings(findings, self._row_builder)
            self._color_status_cells()
            to_copy = [f for f in findings if f.status in ("new", "updated", "moved")]
            n_total = len(findings)
            n_del = len([f for f in findings if f.status == "deleted"])
            self.count_label.setText(f"{n_total} findings")
            self._set_running(False)
            self._refresh_pair_labels()

            if not to_copy and not n_del:
                self.main_window.log("[Sync] Nothing to sync — already up to date.")
                self._switch_to_phase1()
                return

            msg = f"Found {len(to_copy)} file(s) to copy to USB."
            if n_del:
                msg += f"\n{n_del} deleted-source finding(s) skipped (not auto-deleted)."
            msg += "\n\nCopy now, then run Wav Format scan?"
            reply = QMessageBox.question(
                self, "Sync + Convert",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.apply_btn.setEnabled(bool(findings))
                return

            def after_copy(ok: int, fail: int, duplicated: list) -> None:
                self._remove_copied_findings(to_copy)
                self._on_copy_done(ok, fail, duplicated)
                self.main_window.log("[Sync+Convert] Sync done — switching to Wav Format tab.")
                self._switch_to_phase1()

            self._set_running(True, "1/2 Copying…")
            self._run_copy_worker(to_copy, on_done=after_copy)

        self._start_scan_worker(on_done=after_scan, label="1/2 Syncing…")

    def _switch_to_phase1(self) -> None:
        """Switch to the Wav Format tab and auto-start its scan."""
        self.count_label.setText("2/2 Scanning format…")
        tabs = self.main_window.tabs
        for i in range(tabs.count()):
            if isinstance(tabs.widget(i), Phase1Tab):
                tabs.setCurrentIndex(i)
                tabs.widget(i).start_scan(phase_label="2/2 Scanning format…")
                return

    # ------------------------------------------------------------------
    # Bootstrap (CCC handoff)
    # ------------------------------------------------------------------

    def start_bootstrap(self) -> None:
        if not config.SYNC_PAIRS:
            QMessageBox.warning(self, "No Sync Pairs", "No sync pairs configured in config.json.")
            return

        # Check that USB is mounted — bootstrap must compare against actual USB contents
        missing_usb = [p["label"] for p in config.SYNC_PAIRS if not p["usb"].exists()]
        if missing_usb:
            QMessageBox.warning(
                self, "USB Not Mounted",
                f"Cannot bootstrap: USB path(s) not found for: {', '.join(missing_usb)}\n\n"
                "Bootstrap needs to scan both Mac source and USB to determine\n"
                "which files are already there."
            )
            return

        reply = QMessageBox.question(
            self, "Register Existing USB Files",
            "Scan both Mac source and USB folders to find files already on USB.\n\n"
            "Files found on USB will be registered as synced — they won't be\n"
            "re-copied on the next Scan.\n\n"
            "Files on Mac source but missing from USB will appear as NEW\n"
            "on the next Scan so you can copy them over.\n\n"
            "No files are copied. This replaces any existing tracker data. Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        from datetime import datetime as _dt, timezone as _tz
        synced_at = _dt.now(_tz.utc).isoformat()
        tracker = self.main_window.sync_tracker
        tracker._data.clear()
        tracker._dirty = True

        self._set_running(True, "Scanning USB + source…")
        self.thread = QThread()
        self.worker = BootstrapWorker(tracker, synced_at)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.current_file.connect(
            lambda p: self._current_file_label.setText(f"⟳  {Path(p).name}")
        )
        self.worker.finished.connect(self._on_bootstrap_done)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    def _on_bootstrap_done(self, registered: int, skipped_new: int) -> None:
        self._set_running(False)
        self._refresh_pair_labels()
        self.main_window.log(
            f"[Sync] Bootstrap complete: {registered:,} registered, "
            f"{skipped_new:,} newer files will appear as NEW."
        )
        QMessageBox.information(
            self, "Bootstrap Complete",
            f"{registered:,} files registered as already synced.\n"
            f"{skipped_new:,} newer files will appear as NEW on the next scan."
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(done)

    def _on_error(self, msg: str) -> None:
        self._set_running(False)
        self.main_window.log(f"[Sync] ERROR: {msg}")
        QMessageBox.critical(self, "Sync Error", msg)

    def _request_stop(self) -> None:
        if hasattr(self, "worker"):
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("⏹ Stopping…")

    def _set_running(self, running: bool, label: str = "") -> None:
        self._running = running
        if running:
            # Disable immediately; _update_scan_buttons() restores correct state on stop.
            self.sync_convert_btn.setEnabled(False)
            self.bootstrap_btn.setEnabled(False)
        self.stop_btn.setVisible(running)
        self.stop_btn.setText("⏹ Stop")
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(running)
        self._current_file_label.setVisible(running)
        if running and label:
            self.count_label.setText(label)
        if not running:
            self._current_file_label.setText("")
        # Manages caffeinate + scan_btn + apply_btn across all tabs (including this one)
        self.main_window.set_busy(running)
        # After set_busy re-enables scan_btn, correct it based on USB mount state.
        if not running:
            self._update_scan_buttons()


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
        self.sync_tracker = sync.SyncTracker()

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

        self.eject_btn = QPushButton("⏏ Eject")
        self.eject_btn.setEnabled(False)
        self.eject_btn.setToolTip("Save cache and safely eject the loaded drive")
        self.eject_btn.clicked.connect(self.eject_drive)
        self.eject_btn.setStyleSheet("padding: 4px 10px;")
        top.addWidget(self.eject_btn)

        layout.addLayout(top)

        # --- Splitter: tabs on top, log on bottom ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tabs = QTabWidget()
        self.tabs.addTab(SyncTab(self),         "1. Sync")
        self.tabs.addTab(Phase1Tab(self),       "2. Wav Format")
        self.tabs.addTab(SilenceTab(self),      "3. Silence Remover")
        self.tabs.addTab(FileCleanupTab(self),  "4. File Cleanup")
        self.tabs.addTab(NameCleanupTab(self),  "5. Name Cleanup")
        self.tabs.addTab(StereoMonoTab(self),   "6. Fake Stereo to Mono")
        self.tabs.addTab(Phase6Tab(self),       "7. BPM Detection")
        # SyncTab (index 0) is always enabled; processing tabs need a loaded drive
        for i in range(1, self.tabs.count()):
            self.tabs.setTabEnabled(i, False)
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

        for i in range(self.tabs.count()):
            self.tabs.setTabEnabled(i, True)
        self.report_btn.setEnabled(True)
        self.eject_btn.setEnabled(True)
        cache_root = self.cache.cache_file.parent
        cache_note = f"  (cache at {cache_root})" if cache_root != path else ""
        self.statusBar().showMessage(
            f"Loaded: {path}  |  Cache: {self.cache.size()} entries{cache_note}"
        )
        self.log(f"Loaded drive: {path}")
        self.log(f"Cache: {self.cache.size()} entries — {self.cache.cache_file}")

        # Refresh Sync tab pair labels so button states update when a drive is loaded.
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, SyncTab):
                tab._refresh_pair_labels()
                break

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

    # --- Eject ---

    def eject_drive(self):
        if self._busy:
            QMessageBox.warning(self, "Busy",
                                "A scan or apply is still running. Stop it before ejecting.")
            return
        if self.base_dir is None:
            return

        # Find the mount point (top of /Volumes/X, not a subfolder)
        import sys
        parts = self.base_dir.parts
        if sys.platform == "darwin" and len(parts) >= 3 and parts[1] == "Volumes":
            mount = Path("/") / parts[1] / parts[2]
        else:
            mount = self.base_dir

        reply = QMessageBox.question(
            self, "Eject Drive",
            f"Save cache and eject  {mount} ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Save cache before releasing handle
        if self.cache is not None:
            self.cache.save()
            self.log("Cache saved.")

        # Close logging FileHandlers pointing to the volume — keeps Python off the disk
        import logging as _logging
        root_logger = _logging.getLogger()
        for h in list(root_logger.handlers):
            if isinstance(h, _logging.FileHandler):
                h.close()
                root_logger.removeHandler(h)

        # Release all Python references to the volume
        self.base_dir = None
        self.cache = None
        self.tabs.setEnabled(False)
        self.report_btn.setEnabled(False)
        self.eject_btn.setEnabled(False)
        self.statusBar().showMessage("Ejecting…")

        # Move cwd off the volume so Python doesn't hold it open
        import os
        try:
            os.chdir(Path.home())
        except OSError:
            pass

        # Run diskutil eject
        try:
            res = subprocess.run(
                ["diskutil", "eject", str(mount)],
                capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            )
            if res.returncode == 0:
                self.log(f"Ejected: {mount}")
                self.statusBar().showMessage(f"Ejected {mount.name} — safe to unplug.")
                QMessageBox.information(self, "Ejected",
                                        f"{mount.name} ejected successfully.\nSafe to unplug.")
            else:
                err = (res.stderr or res.stdout or "unknown error").strip()
                self.log(f"Eject failed: {err}")
                self.statusBar().showMessage("Eject failed — see log.")
                QMessageBox.warning(self, "Eject Failed",
                                    f"diskutil eject returned an error:\n\n{err}")
        except (subprocess.TimeoutExpired, OSError) as e:
            self.log(f"Eject error: {e}")
            self.statusBar().showMessage("Eject failed — see log.")
            QMessageBox.warning(self, "Eject Failed", str(e))

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
            if hasattr(tab, 'skip_btn'):
                tab.skip_btn.setEnabled(False if busy else bool(tab.findings))

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
