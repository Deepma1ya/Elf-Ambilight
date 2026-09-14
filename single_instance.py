"""Single-instance guard (named mutex). Prevents two copies fighting over
the same BLE strips and clobbering the shared config.json."""
from __future__ import annotations
import ctypes

_lock_handle = None


def acquire(app_id: str = "ElfAmbilightSingleton") -> bool:
    """True if this process owns the instance lock, False if one is running."""
    global _lock_handle
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + app_id)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(h)
            return False
        _lock_handle = h  # kept alive for process lifetime
        return True
    except Exception:
        return True  # fail-open: never brick launch on API issues


def release():
    global _lock_handle
    try:
        if _lock_handle is not None:
            ctypes.windll.kernel32.CloseHandle(_lock_handle)
    except Exception:
        pass
    finally:
        _lock_handle = None
