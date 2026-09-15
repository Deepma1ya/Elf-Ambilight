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


# ── High priority startup: Task Scheduler logon task ───────────────
# A GUI app cannot run before login (session 0 has no desktop), so this is
# the earliest real mechanism: an ONLOGON task with highest privileges and
# no delay fires ahead of HKCU Run entries and other startup apps.
TASK_NAME = "Elf-Ambilight"


def _is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_elevated() -> bool:
    return _is_elevated()


def relaunch_elevated_task(enable: bool):
    """Relaunch self via UAC prompt to flip the task. The elevated copy
    handles --task on|off and exits without GUI. Raises if the user
    denies the prompt or the launch fails."""
    import ctypes
    flag = "on" if enable else "off"
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, f"--task {flag}"
    else:
        script = Path(__file__).resolve().parent / "app.py"
        exe, params = sys.executable, f'"{script}" --task {flag}'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    except OSError as e:
        raise RuntimeError("admin approval needed (prompt denied)") from e
    if int(rc or 0) <= 32:
        raise RuntimeError(f"elevation failed (code {int(rc or 0)})")


def task_enabled() -> bool:
    try:
        import subprocess
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def set_task(enabled: bool, minimized: bool = False):
    """Create/delete the ONLOGON scheduled task. Creating with /RL HIGHEST
    requires elevation — raises RuntimeError with a plain message if denied."""
    import subprocess
    if enabled:
        if not _is_elevated():
            raise RuntimeError(
                "needs admin once: right-click the app → Run as administrator, "
                "then enable High priority startup")
        cmd = _app_cmd(minimized)
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd,
             "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError("; ".join(detail[:2]) or "schtasks create failed")
    else:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 and "cannot find" not in (r.stderr or "").lower():
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError("; ".join(detail[:2]) or "schtasks delete failed")
