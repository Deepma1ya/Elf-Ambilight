"""Start-with-Windows via HKCU Run key (stdlib only)."""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

from proc import run_hidden

try:
    import winreg
except ImportError:  # non-Windows
    winreg = None  # type: ignore

APP_NAME = "Elf-Ambilight"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _entry_script() -> Path:
    """The .py entry point actually running (webapp.py or legacy app.py)."""
    here = Path(__file__).resolve().parent
    try:
        argv0 = Path(sys.argv[0]).resolve()
        if argv0.suffix.lower() == ".py" and argv0.is_file():
            return argv0
    except Exception:
        pass
    return here / "app.py"


def _app_cmd(minimized: bool) -> str:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" --minimized' if minimized else f'"{exe}"'
    script = _entry_script()
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
# Sentinel far in the past: uptime is often < 300 s when this app is launched
# at logon, so 0.0 would make the first query return a bogus cached False.
_TASK_CACHE_T: float = -1e9
_TASK_CACHE_V: bool = False


def _is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_elevated() -> bool:
    return _is_elevated()


def task_result_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "elf_task_result.json"


def clear_task_result():
    try:
        task_result_path().unlink(missing_ok=True)
    except Exception:
        pass


def write_task_result(payload: dict):
    try:
        task_result_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_task_result():
    try:
        p = task_result_path()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def enable_high_priority(on: bool, minimized: bool = False, retries: int = 3):
    """Single-launcher model: the task and the registry Run entry are
    mutually exclusive — only one may launch the app at logon, otherwise
    Windows starts it twice.

    The task is created *before* the Run entry is removed so a failure never
    leaves the app with no startup at all."""
    if on:
        set_task(True, minimized=minimized, retries=retries)
        set_startup(False)  # task becomes the ONLY launcher
    else:
        set_task(False)
        set_startup(True, minimized=minimized)  # restore normal launcher


def disable_all_startup():
    for fn in (lambda: set_task(False), lambda: set_startup(False)):
        try:
            fn()
        except Exception:
            pass


def relaunch_elevated_task(enable: bool, restore_normal: bool = True):
    """Relaunch self via UAC prompt to flip the task. The elevated copy
    handles --task on|off and exits without GUI. Raises if the user
    denies the prompt or the launch fails.

    restore_normal=False is used when the user is turning startup off
    entirely: deleting the task must not re-create the Run entry."""
    import ctypes
    flag = "on" if enable else "off"
    extra = "" if (enable or restore_normal) else " --no-restore"
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, f"--task {flag}{extra}"
    else:
        exe, params = sys.executable, f'"{_entry_script()}" --task {flag}{extra}'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    except OSError as e:
        raise RuntimeError("admin approval needed (prompt denied)") from e
    if int(rc or 0) <= 32:
        code = int(rc or 0)
        if code == 5:  # SE_ERR_ACCESSDENIED
            raise RuntimeError("admin approval was denied")
        raise RuntimeError(f"elevation failed (code {code})")


def invalidate_task_cache():
    global _TASK_CACHE_T
    _TASK_CACHE_T = -1e9


def task_enabled(force: bool = False) -> bool:
    """Cached task query — avoids spawning schtasks on every state refresh.

    The UI polls state after each action; without this cache every click
    would launch several console processes (hidden, but wasteful). Pass
    force=True after a change (especially one made by an elevated helper
    process) or the answer stays stale for up to 5 minutes."""
    global _TASK_CACHE_T, _TASK_CACHE_V
    now = time.monotonic()
    if not force and (now - _TASK_CACHE_T) < 300.0:
        return _TASK_CACHE_V
    try:
        r = run_hidden(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _TASK_CACHE_T, _TASK_CACHE_V = now, ok
    return ok


def set_task(enabled: bool, minimized: bool = False, retries: int = 3):
    """Create/delete the ONLOGON scheduled task. Creating with /RL HIGHEST
    requires elevation — raises RuntimeError with a plain message if denied."""
    global _TASK_CACHE_T, _TASK_CACHE_V
    invalidate_task_cache()
    if enabled:
        if not _is_elevated():
            raise RuntimeError(
                "needs admin once: right-click the app → Run as administrator, "
                "then enable High priority startup")
        cmd = _app_cmd(minimized)
        last = ""
        for _ in range(max(1, retries)):
            r = run_hidden(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd,
                 "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
                timeout=30)
            if r.returncode == 0:
                _TASK_CACHE_T, _TASK_CACHE_V = time.monotonic(), True
                return
            last = ((r.stderr or "") + " " + (r.stdout or "")).strip()
            time.sleep(0.6)
        detail = last.splitlines()
        raise RuntimeError("; ".join(detail[:2]) or "schtasks create failed")
    else:
        for _ in range(max(1, retries)):
            r = run_hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           timeout=30)
            if r.returncode == 0 or "cannot find" in ((r.stderr or "") +
                                                      (r.stdout or "")).lower():
                _TASK_CACHE_T, _TASK_CACHE_V = time.monotonic(), False
                return
            time.sleep(0.6)
        detail = ((r.stderr or "") + " " + (r.stdout or "")).strip().splitlines()
        raise RuntimeError("; ".join(detail[:2]) or "schtasks delete failed")
