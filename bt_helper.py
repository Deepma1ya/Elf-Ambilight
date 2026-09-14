"""Bluetooth system helpers (no extra deps): service check, enable attempt,
settings shortcut, and error sniffing for 'BT is off' conditions."""
from __future__ import annotations
import os
import subprocess

BT_OFF_HINTS = (
    "turned off", "radio", "powered off", "power off", "not powered",
    "unavailable", "disabled", "not ready", "not initialized",
    "no bluetooth", "bluetooth off", "adapter not found",
)


def looks_like_bt_off(err: Exception | str) -> bool:
    msg = str(err).lower()
    return any(h in msg for h in BT_OFF_HINTS)


def _run_hidden(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    kw: dict = {"capture_output": True, "text": True, "timeout": timeout}
    try:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass
    return subprocess.run(args, **kw)


def bt_service_running() -> bool | None:
    """True = bthserv running, False = stopped, None = unknown."""
    try:
        r = _run_hidden(["sc", "query", "bthserv"], timeout=10)
        out = (r.stdout or "").upper()
        if "RUNNING" in out:
            return True
        if "STOPPED" in out or "FAILED" in out:
            return False
        return None
    except Exception:
        return None


def try_enable_bluetooth() -> bool:
    """Best-effort: re-enable disabled Bluetooth PnP devices (needs admin),
    then start bthserv. Returns True if anything claims success."""
    ok = False
    ps = (
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Status -ne 'OK'} | "
        "ForEach-Object { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction SilentlyContinue }"
    )
    try:
        _run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
        ok = True
    except Exception:
        pass
    try:
        r = _run_hidden(["sc", "start", "bthserv"], timeout=20)
        if "START_PENDING" in (r.stdout or "").upper() or "RUNNING" in (r.stdout or "").upper():
            ok = True
    except Exception:
        pass
    return ok


def open_bluetooth_settings():
    try:
        os.startfile("ms-settings:bluetooth")  # type: ignore[attr-defined]
    except Exception:
        pass
