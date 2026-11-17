"""Logging: rotating file in %APPDATA%\\MedusaPDF plus a Qt bridge for the UI log view."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, Signal

from .config import LOG_FILE


class LogEmitter(QObject):
    message = Signal(str, str)  # levelname, formatted text


class _QtHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter):
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emitter.message.emit(record.levelname, self.format(record))
        except Exception:
            pass


def setup_logging() -> LogEmitter:
    logger = logging.getLogger("medusa")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"))

    emitter = LogEmitter()
    qt_handler = _QtHandler(emitter)
    qt_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    logger.addHandler(file_handler)
    logger.addHandler(qt_handler)
    return emitter
