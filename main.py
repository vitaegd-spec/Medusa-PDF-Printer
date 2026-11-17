"""MedusaPDF – free virtual PDF printer (PDF / TIFF / JPG / DOCX / TXT)."""
import os
import sys


def main() -> int:
    # Windowed (pythonw / PyInstaller --windowed) builds have no console streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    app = QApplication(sys.argv)
    app.setApplicationName("MedusaPDF")
    app.setQuitOnLastWindowClosed(False)

    if sys.platform != "win32":
        QMessageBox.critical(None, "MedusaPDF", "MedusaPDF only runs on Windows.")
        return 1

    from medusa.config import LOCK_FILE

    lock = QLockFile(LOCK_FILE)
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, "MedusaPDF", "MedusaPDF is already running – look for its icon in the system tray.")
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(None, "MedusaPDF", "No system tray detected – MedusaPDF will run as a normal window.")

    from medusa.logger import setup_logging
    from medusa.ui import MainWindow

    emitter = setup_logging()
    window = MainWindow(emitter, launched_minimized="--minimized" in sys.argv)  # noqa: F841
    rc = app.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
