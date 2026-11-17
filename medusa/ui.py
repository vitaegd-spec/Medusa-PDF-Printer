"""Main window (Status + Settings tabs), save-dialog handling and system tray integration."""
from __future__ import annotations

import html
import logging
import os
import subprocess
import threading
from collections import deque
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from . import printer_setup, startup
from .config import (APP_NAME, APP_VERSION, EXT_TO_FORMAT, FILENAME_MODES, FORMATS, LOG_FILE, PAPER_SIZES,
                     PDF_QUALITIES, TIFF_MODES, TIMESTAMP_STYLES, Settings, resource_path)
from .converter import ConversionWorker, PrintJob, make_filename, write_test_job

PREVIEW_TITLE = "Microsoft Word - Invoice 42.docx"
from .ghostscript import DOWNLOAD_URL, find_ghostscript, gs_version
from .listener import ListenerSignals, PrintListener

log = logging.getLogger("medusa")

LEVEL_COLORS = {"WARNING": "#d08700", "ERROR": "#e05252", "CRITICAL": "#e05252"}
OK_COLOR, BAD_COLOR = "#2e9e5b", "#e05252"

STYLE = """
QMainWindow, QWidget { font-size: 10pt; }
QGroupBox { font-weight: 600; border: 1px solid palette(mid); border-radius: 8px; margin-top: 12px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton { padding: 5px 12px; border-radius: 6px; }
QPushButton#primary { font-weight: 600; }
QLabel[class="muted"] { color: #808080; }
QPlainTextEdit { font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt; }
"""


def _row(*widgets) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for item in widgets:
        if isinstance(item, int):
            lay.addStretch(item)
        else:
            lay.addWidget(item)
    return w


def _status(label: QLabel, ok: bool, text: str) -> None:
    label.setText(("● " if ok else "✖ ") + text)
    label.setStyleSheet(f"color: {OK_COLOR if ok else BAD_COLOR}; font-weight: 600;")


class _TaskSignals(QObject):
    done = Signal(str, int, str, str)  # action, exit code, script log, error


class MainWindow(QMainWindow):
    request_convert = Signal(object, object)

    def __init__(self, log_emitter, launched_minimized: bool = False):
        super().__init__()
        self.settings = Settings.load()
        self.pending: deque[tuple[str, str]] = deque()
        self.prompting = False
        self.jobs_active = 0
        self.saved_total = 0
        self.failed_total = 0
        self._quitting = False
        self._tray_hint_shown = False
        self._setup_running = False

        icon = QIcon(resource_path(os.path.join("assets", "medusa.ico")))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.app_icon = icon
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.setWindowIcon(icon)
        self.resize(900, 680)
        self.setStyleSheet(STYLE)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_status_tab(), "Status")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.setCentralWidget(self.tabs)

        self._build_tray()
        log_emitter.message.connect(self._append_log)

        # Conversion worker thread
        self.worker_thread = QThread(self)
        self.worker = ConversionWorker()
        self.worker.moveToThread(self.worker_thread)
        self.request_convert.connect(self.worker.convert)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker_thread.start()

        # Setup task signals
        self.task_signals = _TaskSignals()
        self.task_signals.done.connect(self._on_setup_done)

        # Listener
        self.listener_signals = ListenerSignals()
        self.listener_signals.job_received.connect(self._on_job_received)
        self.listener_signals.state_changed.connect(self._on_listener_state)
        self.listener = PrintListener(self.settings.port, self.listener_signals)

        self._load_settings_into_form()
        log.info("%s %s started.", APP_NAME, APP_VERSION)
        self.listener.start()
        self._refresh_status()

        if self.settings.start_with_windows:
            try:
                startup.set_startup(True)
            except OSError as e:
                log.warning("Could not update Windows startup entry: %s", e)

        first_run = not printer_setup.printer_installed(self.settings.printer_name) or not find_ghostscript(self.settings.gs_path)
        if first_run or not (launched_minimized or self.settings.start_minimized):
            self.show()

    # =================================================================== UI build
    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.lbl_listener = QLabel()
        self.lbl_printer = QLabel()
        self.lbl_gs = QLabel()
        self.lbl_gs.setWordWrap(True)
        self.lbl_output = QLabel()
        self.lbl_output.setWordWrap(True)
        self.lbl_format = QLabel()
        self.lbl_counts = QLabel()
        rows = [("Listener", self.lbl_listener), ("Printer", self.lbl_printer), ("Ghostscript", self.lbl_gs),
                ("Save to", self.lbl_output), ("Format / quality", self.lbl_format), ("This session", self.lbl_counts)]
        for r, (title, widget) in enumerate(rows):
            t = QLabel(title + ":")
            t.setProperty("class", "muted")
            grid.addWidget(t, r, 0)
            grid.addWidget(widget, r, 1)
        grid.setColumnStretch(1, 1)
        outer.addWidget(box)

        self.btn_install = QPushButton("Install printer")
        self.btn_install.setObjectName("primary")
        self.btn_install.clicked.connect(self._install_printer)
        self.btn_remove = QPushButton("Remove printer")
        self.btn_remove.clicked.connect(self._remove_printer)
        btn_prefs = QPushButton("Printer preferences")
        btn_prefs.clicked.connect(self._printer_preferences)
        btn_test = QPushButton("Test page")
        btn_test.clicked.connect(self._test_page)
        btn_out = QPushButton("Open output folder")
        btn_out.clicked.connect(self._open_output_folder)
        self.btn_gs_dl = QPushButton("Get Ghostscript")
        self.btn_gs_dl.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DOWNLOAD_URL)))
        outer.addWidget(_row(self.btn_install, self.btn_remove, btn_prefs, 1, self.btn_gs_dl, btn_test, btn_out))

        logbox = QGroupBox("Activity log")
        lv = QVBoxLayout(logbox)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lv.addWidget(self.log_view)
        btn_clear = QPushButton("Clear view")
        btn_clear.clicked.connect(self.log_view.clear)
        btn_logfile = QPushButton("Open log file")
        btn_logfile.clicked.connect(lambda: self._open_path(LOG_FILE))
        lv.addWidget(_row(1, btn_clear, btn_logfile))
        outer.addWidget(logbox, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        # --- Output
        g1 = QGroupBox("Output")
        f1 = QFormLayout(g1)
        self.ed_dest = QLineEdit()
        self.ed_dest.setPlaceholderText("Empty = ask where to save every time")
        b_browse = QPushButton("Browse…")
        b_browse.clicked.connect(self._browse_dest)
        b_clear = QPushButton("Clear")
        b_clear.clicked.connect(self.ed_dest.clear)
        f1.addRow("Default destination:", _row(self.ed_dest, b_browse, b_clear))
        self.cb_format = QComboBox()
        for key, (label, ext) in FORMATS.items():
            self.cb_format.addItem(f"{label}  ({ext})", key)
        f1.addRow("Output format:", _row(self.cb_format, 1))
        self.cb_naming = QComboBox()
        for key, label in FILENAME_MODES.items():
            self.cb_naming.addItem(label, key)
        f1.addRow("File naming:", _row(self.cb_naming, 1))
        self.cb_stamp = QComboBox()
        for key, (_fmt, example) in TIMESTAMP_STYLES.items():
            self.cb_stamp.addItem(example, key)
        self.ed_prefix = QLineEdit()
        self.ed_prefix.setPlaceholderText("optional, e.g. Print")
        self.ed_prefix.setMaximumWidth(180)
        self.ed_prefix.setToolTip("Used for 'Timestamp only', and whenever the printing app sends no document name.")
        f1.addRow("Timestamp style:", _row(self.cb_stamp, QLabel("   Prefix:"), self.ed_prefix, 1))
        self.ed_template = QLineEdit()
        self.ed_template.setToolTip("Tokens: {title} {timestamp} {date} {time} {datetime} {user}")
        self.lbl_tokens = QLabel("Tokens: {title}  {timestamp}  {date}  {time}  {datetime}  {user}")
        self.lbl_tokens.setProperty("class", "muted")
        f1.addRow("Custom template:", self.ed_template)
        f1.addRow("", self.lbl_tokens)
        self.lbl_preview = QLabel()
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f1.addRow("Preview:", self.lbl_preview)
        for sig in (self.cb_naming.currentIndexChanged, self.cb_stamp.currentIndexChanged,
                    self.cb_format.currentIndexChanged):
            sig.connect(self._update_name_preview)
        for ed in (self.ed_prefix, self.ed_template):
            ed.textChanged.connect(self._update_name_preview)
        self.cb_exists = QComboBox()
        self.cb_exists.addItem("Add a number – e.g. Invoice (2).pdf", "number")
        self.cb_exists.addItem("Overwrite the existing file", "overwrite")
        f1.addRow("If file exists:", _row(self.cb_exists, 1))
        self.chk_open_file = QCheckBox("Open the file after saving")
        self.chk_open_folder = QCheckBox("Open the folder after saving")
        f1.addRow("", _row(self.chk_open_file, self.chk_open_folder, 1))
        outer.addWidget(g1)

        # --- Quality
        g2 = QGroupBox("Quality")
        f2 = QFormLayout(g2)
        self.cb_quality = QComboBox()
        for key, label in PDF_QUALITIES.items():
            self.cb_quality.addItem(label, key)
        f2.addRow("PDF quality:", _row(self.cb_quality, 1))
        self.sp_dpi = QSpinBox()
        self.sp_dpi.setRange(72, 1200)
        self.sp_dpi.setSingleStep(50)
        self.sp_dpi.setSuffix(" dpi")
        f2.addRow("TIFF / JPG resolution:", _row(self.sp_dpi, 1))
        self.sp_jpeg = QSpinBox()
        self.sp_jpeg.setRange(10, 100)
        self.sp_jpeg.setSuffix(" %")
        f2.addRow("JPG quality:", _row(self.sp_jpeg, 1))
        self.cb_tiff = QComboBox()
        for key, label in TIFF_MODES.items():
            self.cb_tiff.addItem(label, key)
        f2.addRow("TIFF colour:", _row(self.cb_tiff, 1))
        outer.addWidget(g2)

        # --- Printer & engine
        g3 = QGroupBox("Printer && engine")
        f3 = QFormLayout(g3)
        self.ed_printer = QLineEdit()
        f3.addRow("Printer name:", self.ed_printer)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1024, 65535)
        self.cb_paper = QComboBox()
        self.cb_paper.addItems(PAPER_SIZES)
        f3.addRow("Local port:", _row(self.sp_port, QLabel("   Default paper:"), self.cb_paper, 1))
        self.ed_gs = QLineEdit()
        self.ed_gs.setPlaceholderText("Auto-detect (C:\\Program Files\\gs\\…\\bin\\gswin64c.exe)")
        b_gs = QPushButton("Browse…")
        b_gs.clicked.connect(self._browse_gs)
        f3.addRow("Ghostscript:", _row(self.ed_gs, b_gs))
        note = QLabel("Changing the printer name, port or paper size requires “Install printer” again.")
        note.setProperty("class", "muted")
        f3.addRow("", note)
        outer.addWidget(g3)

        # --- Windows
        g4 = QGroupBox("Windows")
        v4 = QVBoxLayout(g4)
        self.chk_startup = QCheckBox("Start MedusaPDF when Windows starts (needed for printing to work)")
        self.chk_minimized = QCheckBox("Start minimized to the system tray")
        self.chk_notify = QCheckBox("Show tray notifications when files are saved / fail")
        for c in (self.chk_startup, self.chk_minimized, self.chk_notify):
            v4.addWidget(c)
        outer.addWidget(g4)

        outer.addStretch(1)
        b_revert = QPushButton("Revert")
        b_revert.clicked.connect(self._load_settings_into_form)
        b_save = QPushButton("Save settings")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save_settings)
        outer.addWidget(_row(1, b_revert, b_save))
        return page

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.app_icon, self)
        menu = QMenu()
        menu.addAction(QAction("Open MedusaPDF", self, triggered=self._show_window))
        menu.addSeparator()
        menu.addAction(QAction("Open output folder", self, triggered=self._open_output_folder))
        menu.addAction(QAction("Test page", self, triggered=self._test_page))
        menu.addSeparator()
        menu.addAction(QAction("Exit", self, triggered=self._quit))
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    # =================================================================== settings form
    def _load_settings_into_form(self) -> None:
        s = self.settings
        self.ed_dest.setText(s.destination)
        self.cb_format.setCurrentIndex(max(0, self.cb_format.findData(s.output_format)))
        self.cb_naming.setCurrentIndex(max(0, self.cb_naming.findData(s.filename_mode)))
        self.cb_stamp.setCurrentIndex(max(0, self.cb_stamp.findData(s.timestamp_style)))
        self.ed_prefix.setText(s.timestamp_prefix)
        self.ed_template.setText(s.filename_template)
        self.cb_exists.setCurrentIndex(max(0, self.cb_exists.findData(s.if_exists)))
        self.chk_open_file.setChecked(s.open_file_after)
        self.chk_open_folder.setChecked(s.open_folder_after)
        self.cb_quality.setCurrentIndex(max(0, self.cb_quality.findData(s.pdf_quality)))
        self.sp_dpi.setValue(s.image_dpi)
        self.sp_jpeg.setValue(s.jpeg_quality)
        self.cb_tiff.setCurrentIndex(max(0, self.cb_tiff.findData(s.tiff_mode)))
        self.ed_printer.setText(s.printer_name)
        self.sp_port.setValue(s.port)
        self.cb_paper.setCurrentText(s.paper_size)
        self.ed_gs.setText(s.gs_path)
        self.chk_startup.setChecked(s.start_with_windows)
        self.chk_minimized.setChecked(s.start_minimized)
        self.chk_notify.setChecked(s.show_notifications)
        self._update_name_preview()

    def _form_naming_settings(self) -> Settings:
        s = replace(self.settings)
        s.filename_mode = self.cb_naming.currentData() or "smart"
        s.timestamp_style = self.cb_stamp.currentData() or "iso"
        s.timestamp_prefix = self.ed_prefix.text().strip()
        s.filename_template = self.ed_template.text().strip() or "{title}_{timestamp}"
        s.output_format = self.cb_format.currentData() or "pdf"
        return s

    def _update_name_preview(self, *_args) -> None:
        if not hasattr(self, "lbl_preview"):
            return
        s = self._form_naming_settings()
        mode = s.filename_mode
        self.ed_template.setEnabled(mode == "custom")
        self.lbl_tokens.setEnabled(mode == "custom")
        ext = FORMATS[s.output_format][1]
        named = make_filename(s, PREVIEW_TITLE) + ext
        untitled = make_filename(s, "") + ext
        self.lbl_preview.setText(f"“{PREVIEW_TITLE}”  →  <b>{html.escape(named)}</b><br>"
                                 f"<span style='color:#808080'>No document name sent  →  {html.escape(untitled)}</span>")

    def _save_settings(self) -> None:
        s = replace(self.settings)
        dest = self.ed_dest.text().strip()
        s.destination = os.path.normpath(dest) if dest else ""
        s.output_format = self.cb_format.currentData()
        s.filename_mode = self.cb_naming.currentData()
        s.timestamp_style = self.cb_stamp.currentData()
        s.timestamp_prefix = self.ed_prefix.text().strip()
        s.filename_template = self.ed_template.text().strip()
        s.if_exists = self.cb_exists.currentData()
        s.open_file_after = self.chk_open_file.isChecked()
        s.open_folder_after = self.chk_open_folder.isChecked()
        s.pdf_quality = self.cb_quality.currentData()
        s.image_dpi = self.sp_dpi.value()
        s.jpeg_quality = self.sp_jpeg.value()
        s.tiff_mode = self.cb_tiff.currentData()
        s.printer_name = self.ed_printer.text().strip()
        s.port = self.sp_port.value()
        s.paper_size = self.cb_paper.currentText()
        s.gs_path = self.ed_gs.text().strip()
        s.start_with_windows = self.chk_startup.isChecked()
        s.start_minimized = self.chk_minimized.isChecked()
        s.show_notifications = self.chk_notify.isChecked()

        errors = []
        if s.destination and not os.path.isdir(s.destination):
            try:
                os.makedirs(s.destination, exist_ok=True)
            except OSError:
                errors.append("The default destination folder does not exist and could not be created.")
        if s.gs_path and not os.path.isfile(s.gs_path):
            errors.append("The Ghostscript path does not point to a file.")
        if s.filename_mode == "custom" and not s.filename_template:
            errors.append("Enter a custom file name template, e.g. {title}_{timestamp}.")
        if any(c in '\\/:*?"<>|' for c in s.timestamp_prefix):
            errors.append("The timestamp prefix contains characters that are not allowed in file names.")
        if not s.printer_name or any(c in '\\/,!' for c in s.printer_name):
            errors.append("Printer name is empty or contains invalid characters.")
        if errors:
            QMessageBox.warning(self, APP_NAME, "\n".join(errors))
            return

        try:
            startup.set_startup(s.start_with_windows)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not update Windows startup setting:\n{e}")
        try:
            s.save()
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not save settings:\n{e}")
            return

        old = self.settings
        self.settings = s
        if s.port != old.port:
            self.listener.port = s.port
            self.listener.start()
        if (s.port, s.printer_name, s.paper_size) != (old.port, old.printer_name, old.paper_size):
            log.warning("Printer name / port / paper changed – click “Install printer” to apply.")
        log.info("Settings saved. Format: %s, naming: %s, destination: %s.", FORMATS[s.output_format][0],
                 FILENAME_MODES[s.filename_mode].split(" – ")[0], s.destination or "ask every time")
        self._refresh_status()

    def _browse_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Default destination folder", self.ed_dest.text() or "")
        if path:
            self.ed_dest.setText(os.path.normpath(path))

    def _browse_gs(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate Ghostscript (gswin64c.exe)", r"C:\Program Files\gs",
                                              "Ghostscript console (gswin64c.exe gswin32c.exe);;Programs (*.exe)")
        if path:
            self.ed_gs.setText(os.path.normpath(path))

    # =================================================================== jobs
    def _on_job_received(self, ps_path: str, title: str) -> None:
        self.pending.append((ps_path, title))
        self._process_pending()

    def _process_pending(self) -> None:
        if self.prompting:
            return
        while self.pending:
            ps_path, title = self.pending.popleft()
            s = self.settings
            if s.destination:
                ext = FORMATS[s.output_format][1]
                target = os.path.join(s.destination, make_filename(s, title) + ext)
                job = PrintJob(ps_path, title, s.output_format, target, overwrite=(s.if_exists == "overwrite"))
            else:
                self.prompting = True
                try:
                    choice = self._ask_target(title)
                finally:
                    self.prompting = False
                if choice is None:
                    log.info("Save cancelled – job '%s' discarded.", title or "untitled")
                    try:
                        os.remove(ps_path)
                    except OSError:
                        pass
                    continue
                fmt, target = choice
                job = PrintJob(ps_path, title, fmt, target, overwrite=True)  # dialog already confirmed overwrite
            self.jobs_active += 1
            self._refresh_status()
            self.request_convert.emit(job, replace(self.settings))

    def _ask_target(self, title: str) -> tuple[str, str] | None:
        s = self.settings
        filters = [f"{label} (*{ext})" for label, ext in FORMATS.values()]
        keys = list(FORMATS.keys())
        idx = keys.index(s.output_format)
        start_dir = s.last_dir if s.last_dir and os.path.isdir(s.last_dir) else os.path.expanduser("~\\Documents")
        name = make_filename(s, title) + FORMATS[s.output_format][1]

        dlg = QFileDialog(None, f"{APP_NAME} – Save as", os.path.join(start_dir, name))
        dlg.setWindowIcon(self.app_icon)
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.setFileMode(QFileDialog.FileMode.AnyFile)
        dlg.setNameFilters(filters)
        dlg.selectNameFilter(filters[idx])
        dlg.setDefaultSuffix(FORMATS[s.output_format][1].lstrip("."))
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        def on_filter(selected: str) -> None:
            if selected in filters:
                ext = FORMATS[keys[filters.index(selected)]][1]
                dlg.setDefaultSuffix(ext.lstrip("."))
                current = dlg.selectedFiles()
                if current:
                    stem = os.path.splitext(os.path.basename(current[0]))[0]
                    dlg.selectFile(stem + ext)

        dlg.filterSelected.connect(on_filter)
        QTimer.singleShot(0, lambda: (dlg.raise_(), dlg.activateWindow()))
        if self.settings.show_notifications:
            self.tray.showMessage(APP_NAME, "Choose where to save your printed document.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)
        if dlg.exec() != QDialog.DialogCode.Accepted.value or not dlg.selectedFiles():
            return None

        path = os.path.normpath(dlg.selectedFiles()[0])
        ext = os.path.splitext(path)[1].lower()
        fmt = EXT_TO_FORMAT.get(ext)
        if fmt is None:
            sel = dlg.selectedNameFilter()
            fmt = keys[filters.index(sel)] if sel in filters else s.output_format
            path += FORMATS[fmt][1]
        self.settings.last_dir = os.path.dirname(path)
        try:
            self.settings.save()
        except OSError:
            pass
        return fmt, path

    def _on_job_done(self, title: str, paths: list) -> None:
        self.jobs_active = max(0, self.jobs_active - 1)
        self.saved_total += 1
        self._refresh_status()
        s = self.settings
        if s.show_notifications:
            what = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} pages"
            self.tray.showMessage(APP_NAME, f"Saved: {what}", QSystemTrayIcon.MessageIcon.Information, 4000)
        if s.open_file_after and paths:
            try:
                os.startfile(paths[0])
            except OSError as e:
                log.warning("Could not open file: %s", e)
        if s.open_folder_after and paths:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(paths[0])])

    def _on_job_failed(self, title: str, error: str) -> None:
        self.jobs_active = max(0, self.jobs_active - 1)
        self.failed_total += 1
        self._refresh_status()
        if self.settings.show_notifications:
            self.tray.showMessage(APP_NAME, f"Could not save '{title}':\n{error}",
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)

    def _test_page(self) -> None:
        log.info("Creating test page…")
        self._on_job_received(write_test_job(), "MedusaPDF Test Page")

    # =================================================================== printer setup
    def _run_setup(self, action: str) -> None:
        if self._setup_running:
            return
        self._setup_running = True
        self.btn_install.setEnabled(False)
        self.btn_remove.setEnabled(False)
        s = self.settings
        log.info("%s printer '%s' – approve the Windows admin prompt…",
                 "Installing" if action == "install" else "Removing", s.printer_name)

        def task() -> None:
            try:
                if action == "install":
                    code, text = printer_setup.install_printer(s.printer_name, s.port, s.paper_size)
                else:
                    code, text = printer_setup.uninstall_printer(s.printer_name)
                self.task_signals.done.emit(action, code, text, "")
            except Exception as e:
                self.task_signals.done.emit(action, -1, "", str(e))

        threading.Thread(target=task, daemon=True).start()

    def _install_printer(self) -> None:
        self._run_setup("install")

    def _remove_printer(self) -> None:
        if QMessageBox.question(self, APP_NAME, f"Remove the '{self.settings.printer_name}' printer?") \
                == QMessageBox.StandardButton.Yes:
            self._run_setup("remove")

    def _on_setup_done(self, action: str, code: int, text: str, error: str) -> None:
        self._setup_running = False
        self.btn_install.setEnabled(True)
        self.btn_remove.setEnabled(True)
        for line in text.splitlines():
            if line.strip():
                log.info("  setup: %s", line.strip())
        if error:
            log.error("Printer %s failed: %s", action, error)
        elif code == 0:
            log.info("Printer %s completed.", "installation" if action == "install" else "removal")
        else:
            log.error("Printer %s failed (exit code %d).", action, code)
        self._refresh_status()

    def _printer_preferences(self) -> None:
        name = self.settings.printer_name
        if not printer_setup.printer_installed(name):
            QMessageBox.information(self, APP_NAME, "Install the printer first.")
            return
        subprocess.Popen(["rundll32.exe", "printui.dll,PrintUIEntry", "/e", "/n", name])

    # =================================================================== status
    def _on_listener_state(self, ok: bool, message: str) -> None:
        _status(self.lbl_listener, ok, message)

    def _refresh_status(self) -> None:
        s = self.settings
        _status(self.lbl_listener, self.listener.listening,
                f"Listening on 127.0.0.1:{s.port}" if self.listener.listening else f"Not listening on port {s.port}")
        installed = printer_setup.printer_installed(s.printer_name)
        _status(self.lbl_printer, installed,
                f"'{s.printer_name}' installed" if installed else f"'{s.printer_name}' not installed – click Install printer")
        gs = find_ghostscript(s.gs_path)
        if gs:
            ver = gs_version(gs)
            _status(self.lbl_gs, True, f"{ver + '  –  ' if ver else ''}{gs}")
        else:
            _status(self.lbl_gs, False, "Not found – install Ghostscript (free) or set its path in Settings")
        self.btn_gs_dl.setVisible(not gs)
        self.lbl_output.setText(s.destination or "<i>Ask every time</i>")
        self.lbl_format.setText(f"{FORMATS[s.output_format][0]}  ·  {PDF_QUALITIES[s.pdf_quality]}")
        busy = f",  {self.jobs_active} converting…" if self.jobs_active else ""
        self.lbl_counts.setText(f"{self.saved_total} saved, {self.failed_total} failed{busy}")
        self.tray.setToolTip(f"{APP_NAME} – {'ready' if self.listener.listening else 'not listening'}"
                             f"\n{self.saved_total} saved this session")

    def _append_log(self, level: str, text: str) -> None:
        safe = html.escape(text)
        color = LEVEL_COLORS.get(level)
        self.log_view.appendHtml(f'<span style="color:{color}">{safe}</span>' if color else safe)

    # =================================================================== window / tray
    def _open_output_folder(self) -> None:
        path = self.settings.destination or self.settings.last_dir
        if not path:
            QMessageBox.information(self, APP_NAME, "No output folder yet – set a default destination in Settings.")
            return
        self._open_path(path)

    def _open_path(self, path: str) -> None:
        path = os.path.normpath(path)
        if not os.path.exists(path):
            QMessageBox.information(self, APP_NAME, f"Not found:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, str(e))

    def _show_window(self) -> None:
        self._refresh_status()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "Still running in the system tray – it must keep running to receive print jobs.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)

    def _quit(self) -> None:
        if self.jobs_active:
            if QMessageBox.question(self, APP_NAME, "A document is still being converted. Exit anyway once it finishes?") \
                    != QMessageBox.StandardButton.Yes:
                return
        self._quitting = True
        log.info("MedusaPDF exiting.")
        self.listener.stop()
        self.worker_thread.quit()
        self.worker_thread.wait(30000)
        self.tray.hide()
        self.close()
        QApplication.quit()
