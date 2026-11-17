"""Locating and running Ghostscript (free, AGPL – https://ghostscript.com)."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import winreg

CREATE_NO_WINDOW = 0x08000000
GS_TIMEOUT_SECONDS = 900
DOWNLOAD_URL = "https://ghostscript.com/releases/gsdnld.html"


class GhostscriptError(Exception):
    pass


def _version_key(path: str) -> tuple:
    m = re.search(r"gs(\d+(?:\.\d+)*)", path, re.IGNORECASE)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def _from_registry() -> list[str]:
    found = []
    for root_key in (r"SOFTWARE\GPL Ghostscript", r"SOFTWARE\Artifex\GPL Ghostscript",
                     r"SOFTWARE\Artifex Ghostscript"):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_key, 0, winreg.KEY_READ | view) as key:
                    i = 0
                    while True:
                        try:
                            ver = winreg.EnumKey(key, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(key, ver) as sub:
                                dll, _ = winreg.QueryValueEx(sub, "GS_DLL")
                            folder = os.path.dirname(dll)
                            for exe in ("gswin64c.exe", "gswin32c.exe"):
                                p = os.path.join(folder, exe)
                                if os.path.isfile(p):
                                    found.append(p)
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def find_ghostscript(custom_path: str = "") -> str:
    """Return the path to the newest console Ghostscript (gswin64c.exe), or ''."""
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    candidates: list[str] = []
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if not root:
            continue
        for exe in ("gswin64c.exe", "gswin32c.exe"):
            candidates += glob.glob(os.path.join(root, "gs", "gs*", "bin", exe))
    candidates += _from_registry()
    candidates = [c for c in dict.fromkeys(candidates) if os.path.isfile(c)]
    if candidates:
        return sorted(candidates, key=_version_key, reverse=True)[0]
    for exe in ("gswin64c.exe", "gswin32c.exe"):
        p = shutil.which(exe)
        if p:
            return p
    return ""


def gs_version(gs: str) -> str:
    try:
        r = subprocess.run([gs, "--version"], capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode(errors="replace").strip()
    except Exception:
        return ""


def run_gs(gs: str, args: list[str], timeout: int = GS_TIMEOUT_SECONDS) -> None:
    cmd = [gs, "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dQUIET"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise GhostscriptError("Ghostscript timed out")
    except OSError as e:
        raise GhostscriptError(f"Could not start Ghostscript: {e}")
    if r.returncode != 0:
        text = (r.stderr or r.stdout or b"").decode(errors="replace").strip().splitlines()
        tail = " | ".join(line.strip() for line in text[-4:] if line.strip())
        raise GhostscriptError(f"Ghostscript failed (code {r.returncode}): {tail or 'no details'}")
