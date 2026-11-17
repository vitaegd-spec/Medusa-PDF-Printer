"""Settings persistence and path helpers for MedusaPDF."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields

APP_NAME = "MedusaPDF"
APP_VERSION = "1.1.1"
LEGACY_APP_NAME = "MedussaPDF"   # old (misspelled) name – settings are migrated from it once

FORMATS: dict[str, tuple[str, str]] = {
    # key: (label, extension)
    "pdf": ("PDF document", ".pdf"),
    "tiff": ("TIFF image (multi-page)", ".tif"),
    "jpg": ("JPEG image (one per page)", ".jpg"),
    "docx": ("Word document", ".docx"),
    "txt": ("Plain text", ".txt"),
}

EXT_TO_FORMAT = {".pdf": "pdf", ".tif": "tiff", ".tiff": "tiff", ".jpg": "jpg", ".jpeg": "jpg",
                 ".docx": "docx", ".txt": "txt"}

PDF_QUALITIES: dict[str, str] = {
    "maximum": "Maximum – lossless images, no downsampling",
    "high": "High – prepress (300 dpi images)",
    "standard": "Standard – printer (300 dpi, JPEG)",
    "small": "Small – eBook (150 dpi)",
}

TIFF_MODES: dict[str, str] = {
    "color": "Colour (24-bit, LZW)",
    "gray": "Greyscale (8-bit, LZW)",
    "bw": "Black & white (CCITT G4, fax/archive)",
}

PAPER_SIZES = ["A4", "Letter", "Legal", "A3", "A5", "Executive"]

FILENAME_MODES: dict[str, str] = {
    "smart": "Smart title – same name as the printed document",
    "smart_timestamp": "Smart title + timestamp",
    "timestamp": "Timestamp only",
    "custom": "Custom template",
}

# key: (strftime format, example shown in the UI)
TIMESTAMP_STYLES: dict[str, tuple[str, str]] = {
    "iso": ("%Y-%m-%d_%H-%M-%S", "2026-11-17_15-34-30"),
    "compact": ("%Y%m%d-%H%M%S", "20261117-153430"),
    "readable": ("%Y-%m-%d %H.%M.%S", "2026-11-17 15.34.30"),
    "date": ("%Y-%m-%d", "2026-11-17"),
}


def _env_dir(var: str) -> str:
    root = os.environ.get(var) or os.path.expanduser("~")
    path = os.path.join(root, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def app_data_dir() -> str:
    """%APPDATA%\\MedusaPDF – settings and logs."""
    return _env_dir("APPDATA")


def local_data_dir() -> str:
    """%LOCALAPPDATA%\\MedusaPDF – spool and helper scripts."""
    return _env_dir("LOCALAPPDATA")


def spool_dir() -> str:
    path = os.path.join(local_data_dir(), "spool")
    os.makedirs(path, exist_ok=True)
    return path


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    root = getattr(sys, "_MEIPASS", None) or base_dir()
    return os.path.join(root, rel)


CONFIG_FILE = os.path.join(app_data_dir(), "settings.json")


def _migrate_legacy_settings() -> None:
    """First run after the rename: copy settings from %APPDATA%\\MedussaPDF (old printer name is updated)."""
    if os.path.exists(CONFIG_FILE):
        return
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    legacy = os.path.join(root, LEGACY_APP_NAME, "settings.json")
    try:
        with open(legacy, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("printer_name") == LEGACY_APP_NAME:
            data["printer_name"] = APP_NAME
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


_migrate_legacy_settings()
LOG_FILE = os.path.join(app_data_dir(), "medusa.log")
LOCK_FILE = os.path.join(app_data_dir(), "medusa.lock")


@dataclass
class Settings:
    # Output
    destination: str = ""                 # empty = ask every time
    output_format: str = "pdf"
    filename_mode: str = "smart"          # see FILENAME_MODES
    timestamp_style: str = "iso"          # see TIMESTAMP_STYLES
    timestamp_prefix: str = "Print"       # used by "Timestamp only" and when no title is available
    filename_template: str = "{title}_{timestamp}"
    if_exists: str = "number"             # number | overwrite
    open_file_after: bool = False
    open_folder_after: bool = False
    last_dir: str = ""                    # remembered folder for the Save dialog
    # Quality
    pdf_quality: str = "maximum"
    image_dpi: int = 300
    jpeg_quality: int = 95
    tiff_mode: str = "color"
    # Printer
    printer_name: str = "MedusaPDF"
    port: int = 9100
    paper_size: str = "A4"
    gs_path: str = ""
    # Windows
    start_with_windows: bool = False
    start_minimized: bool = True
    show_notifications: bool = True

    def _clamp(self) -> None:
        if self.output_format not in FORMATS:
            self.output_format = "pdf"
        if self.pdf_quality not in PDF_QUALITIES:
            self.pdf_quality = "maximum"
        if self.tiff_mode not in TIFF_MODES:
            self.tiff_mode = "color"
        if self.if_exists not in ("number", "overwrite"):
            self.if_exists = "number"
        if self.filename_mode not in FILENAME_MODES:
            self.filename_mode = "smart"
        if self.timestamp_style not in TIMESTAMP_STYLES:
            self.timestamp_style = "iso"
        self.timestamp_prefix = str(self.timestamp_prefix or "").strip()
        if self.paper_size not in PAPER_SIZES:
            self.paper_size = "A4"
        self.image_dpi = max(72, min(1200, int(self.image_dpi)))
        self.jpeg_quality = max(10, min(100, int(self.jpeg_quality)))
        self.port = max(1024, min(65535, int(self.port)))
        if not self.filename_template.strip():
            self.filename_template = "{title}_{timestamp}"
        if not self.printer_name.strip():
            self.printer_name = "MedusaPDF"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {f.name for f in fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(s, key, value)
            s._clamp()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return s

    def save(self) -> None:
        self._clamp()
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, CONFIG_FILE)
