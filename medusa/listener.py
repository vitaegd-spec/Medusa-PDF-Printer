"""Loopback TCP listener – the MedusaPDF Windows printer sends its PostScript here (RAW / port 9100 style)."""
from __future__ import annotations

import itertools
import logging
import os
import socket
import threading
import time

from PySide6.QtCore import QObject, Signal

from .config import spool_dir
from .converter import extract_title

log = logging.getLogger("medusa")

IDLE_TIMEOUT_SECONDS = 120
MIN_JOB_BYTES = 64


class ListenerSignals(QObject):
    job_received = Signal(str, str)   # spooled .ps path, document title
    state_changed = Signal(bool, str)  # listening?, message


class PrintListener:
    def __init__(self, port: int, signals: ListenerSignals):
        self.port = port
        self.signals = signals
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._counter = itertools.count(1)

    @property
    def listening(self) -> bool:
        return self._running

    def start(self) -> bool:
        self.stop()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(8)
        except OSError as e:
            msg = f"Cannot listen on 127.0.0.1:{self.port} – {e.strerror or e}. Another program may be using this port."
            log.error(msg)
            self.signals.state_changed.emit(False, msg)
            return False
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="medusa-listener", daemon=True)
        self._thread.start()
        log.info("Listening for print jobs on 127.0.0.1:%d.", self.port)
        self.signals.state_changed.emit(True, f"Listening on 127.0.0.1:{self.port}")
        return True

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _accept_loop(self) -> None:
        sock = self._sock
        while self._running and sock is not None:
            try:
                conn, _ = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        if self._running:
            self._running = False
            self.signals.state_changed.emit(False, "Listener stopped unexpectedly")

    def _handle(self, conn: socket.socket) -> None:
        path = os.path.join(spool_dir(), f"job_{time.strftime('%Y%m%d-%H%M%S')}_{next(self._counter)}.ps")
        size = 0
        try:
            conn.settimeout(IDLE_TIMEOUT_SECONDS)
            with open(path, "wb") as fh:
                while True:
                    chunk = conn.recv(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
        except socket.timeout:
            log.warning("Print job connection went idle – using the %d bytes received.", size)
        except OSError as e:
            log.error("Error receiving print job: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

        if size < MIN_JOB_BYTES:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        title = extract_title(path)
        log.info("Received print job '%s' (%.1f KB).", title or "untitled", size / 1024)
        self.signals.job_received.emit(path, title)
