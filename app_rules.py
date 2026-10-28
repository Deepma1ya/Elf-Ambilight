"""Detect running apps for Ambilight launch rules (no external deps).

Two views are offered:
  running_exes()       — every process name (lowercase basenames)
  visible_window_exes() — processes that own a visible titled window (picker)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


def running_exes() -> set[str]:
    """Lowercase executable names of every running process."""
    names: set[str] = set()
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (0, -1):
            return names
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if k32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    exe = (entry.szExeFile or "").strip().lower()
                    if exe:
                        names.add(exe)
                    if not k32.Process32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return names


def _exe_for_pid(pid: int) -> str:
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = w.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def visible_window_exes(limit: int = 120) -> list[str]:
    """Executables that currently own a visible window (best picker list)."""
    out: set[str] = set()
    try:
        user32 = ctypes.windll.user32
        CB = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def _cb(hwnd, _l):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                pid = w.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                exe = _exe_for_pid(int(pid.value))
                if exe and exe not in ("explorer.exe", "applicationframehost.exe",
                                       "elf-ambilight.exe"):
                    out.add(exe)
            except Exception:
                pass
            return True

        user32.EnumWindows(CB(_cb), 0)
    except Exception:
        pass
    return sorted(out)[:limit]
