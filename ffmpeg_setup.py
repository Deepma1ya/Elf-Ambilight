"""One-click FFmpeg for Studio video — per-user, no admin required.

The app ships without FFmpeg to stay ~34 MB. When video playback is needed,
this module downloads an official static Windows build, keeps only
`ffmpeg.exe` under %LOCALAPPDATA%\\Elf-Ambilight\\ffmpeg\\bin, verifies it
runs, and appends that folder to the *user* PATH (HKCU\\Environment), then
tells Windows via WM_SETTINGCHANGE. No elevation, no system-wide changes.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from proc import run_hidden

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Elf-Ambilight"
INSTALL_DIR = APP_DIR / "ffmpeg"
BIN_DIR = INSTALL_DIR / "bin"
LOCAL_FFMPEG = BIN_DIR / "ffmpeg.exe"

# Official static Windows builds, tried in order.
SOURCES = (
    ("gyan.dev essentials",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("BtbN win64-gpl",
     "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
     "ffmpeg-master-latest-win64-gpl.zip"),
)
USER_AGENT = "Elf-Ambilight (+https://github.com/Deepma1ya/Elf-Ambilight)"
COMMON_PATHS = (
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "ffmpeg" / "bin" / "ffmpeg.exe",
)


def _norm(p: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(
            os.path.expandvars(str(p).strip().strip('"'))))
    except Exception:
        return ""


def _user_path() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            try:
                val, _ = winreg.QueryValueEx(k, "Path")
                return str(val or "")
            except FileNotFoundError:
                return ""
    except Exception:
        return ""


def path_dirs() -> list[str]:
    """User PATH + process PATH, as raw entries."""
    raw = _user_path().split(";") + os.environ.get("PATH", "").split(";")
    return [d.strip() for d in raw if d.strip()]


def on_path(folder) -> bool:
    want = _norm(str(folder))
    return bool(want) and any(_norm(d) == want for d in path_dirs())


def find_ffmpeg() -> tuple[str, str]:
    """(exe, source) — our install first, then PATH, then common folders."""
    try:
        if LOCAL_FFMPEG.is_file():
            return str(LOCAL_FFMPEG), "app"
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p:
        return p, "path"
    for c in COMMON_PATHS:
        try:
            if c.is_file():
                return str(c), "common"
        except Exception:
            continue
    return "", ""


def info() -> dict:
    exe, source = find_ffmpeg()
    return {
        "ready": bool(exe),
        "exe": exe,
        "source": source,
        "on_path": bool(exe) and on_path(Path(exe).parent),
        "install_dir": str(INSTALL_DIR),
    }


def _broadcast_env_change():
    try:
        import ctypes
        from ctypes import wintypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = wintypes.DWORD()
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                                   "Environment", SMTO_ABORTIFHUNG, 3000,
                                   ctypes.byref(result))
    except Exception:
        pass


def add_to_user_path(folder, key_name: str = r"Environment") -> bool:
    """Append `folder` to HKCU\\Environment\\Path once. No admin needed.

    key_name exists for tests (a throwaway subkey); production always uses
    the real user Environment key. Returns True when the value changed."""
    folder = str(folder).strip().strip('"')
    if not folder:
        return False
    real = key_name == r"Environment"
    if real and on_path(folder):
        return False
    try:
        import winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_name, 0,
                                 winreg.KEY_READ | winreg.KEY_SET_VALUE)
        with key:
            try:
                cur, typ = winreg.QueryValueEx(key, "Path")
                typ = winreg.REG_EXPAND_SZ if typ == winreg.REG_EXPAND_SZ else winreg.REG_SZ
            except FileNotFoundError:
                cur, typ = "", winreg.REG_EXPAND_SZ
            cur = str(cur or "").strip()
            if any(_norm(d) == _norm(folder) for d in cur.split(";") if d.strip()):
                return False
            new = (cur + ";" + folder) if cur and not cur.endswith(";") else (cur + folder)
            winreg.SetValueEx(key, "Path", 0, typ, new)
        if real:
            # Make it visible to this process (and every child) immediately.
            env = os.environ.get("PATH", "")
            if not any(_norm(d) == _norm(folder) for d in env.split(";")):
                os.environ["PATH"] = (env + ";" + folder) if env else folder
            _broadcast_env_change()
        return True
    except Exception:
        return False


def add_existing_to_path():
    """Put the already-found ffmpeg on PATH (e.g. a loose C:\\ffmpeg copy)."""
    exe, _ = find_ffmpeg()
    if not exe:
        return None
    add_to_user_path(Path(exe).parent)
    return info()


def _version(exe: Path) -> str:
    try:
        r = run_hidden([str(exe), "-version"], timeout=25)
        if getattr(r, "returncode", 1) == 0:
            line = (r.stdout or "").strip().splitlines()
            return line[0].strip() if line else "ffmpeg"
    except Exception:
        pass
    return ""


def extract_ffmpeg(zip_path, dest_bin, progress=None) -> tuple[str, str]:
    """Install ffmpeg.exe from a downloaded archive into `dest_bin`.

    Writes to ffmpeg.new.exe, verifies it runs, then swaps it into place so
    an interrupted install can never leave a broken binary behind."""
    progress = progress or (lambda stage, pct, msg: None)
    dest_bin = Path(dest_bin)
    dest_bin.mkdir(parents=True, exist_ok=True)
    member = ""
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            low = n.lower().replace("\\", "/")
            if low.endswith("/bin/ffmpeg.exe") or low == "ffmpeg.exe":
                member = n
                break
        if not member:
            raise RuntimeError("ffmpeg.exe not found in the archive")
        tmp_exe = dest_bin / "ffmpeg.new.exe"
        with z.open(member) as src, open(tmp_exe, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    progress("verify", 100, "Checking the downloaded binary …")
    ver = _version(tmp_exe)
    if not ver:
        try:
            tmp_exe.unlink()
        except Exception:
            pass
        raise RuntimeError("the downloaded ffmpeg.exe did not run")
    target = dest_bin / "ffmpeg.exe"
    os.replace(tmp_exe, target)
    return str(target), ver


def download_and_install(progress=None) -> dict:
    """Download, verify, install and expose ffmpeg.exe. Raises on failure.

    progress(stage, pct, msg) is called during download/extract; pct is
    -1 when the server does not report a size."""
    progress = progress or (lambda stage, pct, msg: None)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="elf_ffmpeg_"))
    errors: list[str] = []
    try:
        for name, url in SOURCES:
            zip_path = tmp / "ffmpeg.zip"
            try:
                progress("download", 0, f"Contacting {name} …")
                req = Request(url, headers={"User-Agent": USER_AGENT})
                with urlopen(req, timeout=45) as resp, open(zip_path, "wb") as out:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    last = -2
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        got += len(chunk)
                        pct = int(got * 100 / total) if total else -1
                        if pct != last:
                            last = pct
                            mb = got / 1048576
                            msg = (f"Downloading {mb:.0f} MB of {total / 1048576:.0f} MB"
                                   if total else f"Downloading {mb:.0f} MB")
                            progress("download", pct, msg)
                if not zip_path.is_file() or zip_path.stat().st_size < (5 << 20):
                    raise RuntimeError("download was incomplete")

                progress("extract", 100, "Extracting ffmpeg.exe …")
                exe, ver = extract_ffmpeg(zip_path, BIN_DIR, progress)

                progress("path", 100, "Adding to your user PATH …")
                add_to_user_path(BIN_DIR)
                progress("done", 100, ver)
                return {"ok": True, "exe": exe, "version": ver,
                        "source": "app", "on_path": on_path(BIN_DIR)}
            except Exception as e:
                errors.append(f"{name}: {e}")
                try:
                    zip_path.unlink()
                except Exception:
                    pass
        raise RuntimeError("; ".join(errors[-2:]))
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
