"""Hidden subprocess helper.

Elf-Ambilight is built with --windowed, so any console helper it launches
(schtasks, sc, powershell) would otherwise flash a terminal window. Every
system call must go through run_hidden().
"""
from __future__ import annotations

import subprocess

CREATE_NO_WINDOW = 0x08000000
SW_HIDE = 0


def run_hidden(args, timeout: float = 15, **kwargs):
    """subprocess.run with no console window and no flashing.

    capture_output/text default to True; pass overrides as kwargs.
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", timeout)
    kwargs.setdefault("startupinfo", si)
    try:
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    except Exception:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(args, **kwargs)
