"""Start-with-Windows via HKCU Run key (stdlib only)."""
from __future__ import annotations
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None  # type: ignore

APP_NAME = "Elf-Ambilight"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _app_cmd(minimized: bool) -> str:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" --minimized' if minimized else f'"{exe}"'
    script = Path(__file__).resolve().parent / "app.py"
    base = f'"{sys.executable}" "{script}"'
    return f"{base} --minimized" if minimized else base


def startup_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool, minimized: bool = False):
    if winreg is None:
        raise RuntimeError("startup option is Windows-only")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _app_cmd(minimized))
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
