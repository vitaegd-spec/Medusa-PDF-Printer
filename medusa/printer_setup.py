"""Install / remove the MedusaPDF Windows printer (elevated PowerShell)."""
from __future__ import annotations

import os
import shutil

import pywintypes
import win32con
import win32event
import win32print
import win32process
from win32com.shell import shell, shellcon

from .config import local_data_dir, resource_path

SCRIPT_TIMEOUT_MS = 180_000
ERROR_CANCELLED = 1223


class SetupError(Exception):
    pass


def printer_installed(name: str) -> bool:
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return any(p[2].lower() == name.lower() for p in win32print.EnumPrinters(flags))
    except pywintypes.error:
        return False


def _staged_script(name: str) -> str:
    """Copy the bundled script to %LOCALAPPDATA% so the elevated process can always read it."""
    src = resource_path(os.path.join("scripts", name))
    if not os.path.isfile(src):
        raise SetupError(f"Missing script: {src}")
    folder = os.path.join(local_data_dir(), "scripts")
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, name)
    shutil.copyfile(src, dst)
    return dst


def run_elevated(script_name: str, params: dict[str, str | int]) -> tuple[int, str]:
    """Run a PowerShell script as administrator (UAC prompt). Returns (exit code, log text)."""
    script = _staged_script(script_name)
    log_file = os.path.join(local_data_dir(), "scripts", os.path.splitext(script_name)[0] + ".log")
    try:
        os.remove(log_file)
    except OSError:
        pass
    arg_text = " ".join(f'-{k} "{v}"' for k, v in params.items())
    cmd = f'-NoProfile -ExecutionPolicy Bypass -File "{script}" {arg_text} -LogFile "{log_file}"'
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas",
                                    lpFile="powershell.exe", lpParameters=cmd, nShow=win32con.SW_HIDE)
    except pywintypes.error as e:
        if e.winerror == ERROR_CANCELLED:
            raise SetupError("Administrator permission was cancelled")
        raise SetupError(f"Could not start PowerShell: {e.strerror}")
    handle = info["hProcess"]
    try:
        if win32event.WaitForSingleObject(handle, SCRIPT_TIMEOUT_MS) != win32event.WAIT_OBJECT_0:
            raise SetupError("Printer setup timed out")
        code = win32process.GetExitCodeProcess(handle)
    finally:
        handle.Close()
    text = ""
    try:
        with open(log_file, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read().strip()
    except OSError:
        pass
    return code, text


def install_printer(name: str, port: int, paper: str) -> tuple[int, str]:
    return run_elevated("install_printer.ps1", {"PrinterName": name, "Port": port, "PaperSize": paper})


def uninstall_printer(name: str) -> tuple[int, str]:
    return run_elevated("uninstall_printer.ps1", {"PrinterName": name})
