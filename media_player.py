"""Continuous RGB playback for Studio: media files and generated animations.

Frames are decoded at a small size, averaged to one colour, then streamed to
the strip through a callback supplied by the backend (calibration + BLE +
Dynamic Lighting live there).

Video files use the system ffmpeg (or imageio-ffmpeg if installed).
GIF / WebP / APNG use Pillow and always work.
"""
from __future__ import annotations

import colorsys
import math
import os
import random
import shutil
import subprocess
import threading
import time

FRAME_W, FRAME_H = 64, 36
FRAME_BYTES = FRAME_W * FRAME_H * 3
IMAGE_EXT = (".gif", ".webp", ".apng", ".png", ".jpg", ".jpeg", ".bmp")
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".wmv")

GEN_NAMES = ("rainbow", "pulse", "chase", "fire", "ocean")
GEN_LABELS = {
    "rainbow": "Rainbow sweep",
    "pulse": "Breathing pulse",
    "chase": "Colour chase",
    "fire": "Fire flicker",
    "ocean": "Ocean wave",
}


def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg  # type: ignore
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _avg(buf: bytes) -> tuple[int, int, int]:
    # Fast average without numpy dependency in this hot path.
    n = len(buf) // 3
    if n <= 0:
        return (0, 0, 0)
    # sample every 7th pixel for speed; colours are flat at this size
    step = 3 * max(1, n // 900)
    r = g = b = 0
    count = 0
    for i in range(0, n * 3 - 2, step):
        r += buf[i]
        g += buf[i + 1]
        b += buf[i + 2]
        count += 1
    if count == 0:
        return (0, 0, 0)
    return (r // count, g // count, b // count)


def _scale(rgb: tuple[int, int, int], brightness: int) -> tuple[int, int, int]:
    k = max(0, min(100, brightness)) / 100.0
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


class MediaPlayer:
    def __init__(self, send, targets, base_color=(255, 255, 255)):
        self.send = send                    # callable(r,g,b)
        self.targets = targets              # callable() -> list[str] | bool
        self.base_color = base_color
        self.fps = 30.0
        self.brightness = 100
        self.loop = True
        self.mode = ""
        self.name = ""
        self.running = False
        self.frames = 0
        self.last_color = (0, 0, 0)
        self.error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    # ---------- lifecycle ----------
    def start(self, mode: str, path: str = "", fps: float = 30.0,
              brightness: int = 100, loop: bool = True,
              base_color=(255, 255, 255)) -> bool:
        self.stop()
        self.mode = mode
        self.fps = max(1.0, min(60.0, float(fps)))
        self.brightness = max(1, min(100, int(brightness)))
        self.loop = bool(loop)
        self.base_color = base_color or (255, 255, 255)
        self.name = (GEN_LABELS.get(mode.replace("gen:", ""), "") if mode.startswith("gen:")
                     else os.path.basename(path))
        self.path = path
        self.frames = 0
        self.error = ""
        self.last_color = (0, 0, 0)
        if mode == "file":
            if not path or not os.path.isfile(path):
                self.error = "File not found."
                return False
            ext = os.path.splitext(path)[1].lower()
            if ext in VIDEO_EXT and not ffmpeg_path():
                self.error = ("Video needs ffmpeg. Install ffmpeg and add it to PATH, "
                              "or use an animated GIF/WebP.")
                return False
        elif mode.startswith("gen:"):
            if mode.split(":", 1)[1] not in GEN_NAMES:
                self.error = "Unknown animation."
                return False
        else:
            self.error = "Unknown source."
            return False
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, name="studio-player", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.running = False
        try:
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.5)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass
        self._proc = None
        th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(1.5)
            except Exception:
                pass
        self._thread = None

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "name": self.name,
            "frames": self.frames,
            "fps": self.fps,
            "brightness": self.brightness,
            "loop": self.loop,
            "color": list(self.last_color),
            "error": self.error,
        }

    # ---------- loop ----------
    def _run(self):
        try:
            if self.mode == "file":
                src = self._file_frames()
            else:
                src = self._gen_frames(self.mode.split(":", 1)[1])
            period = 1.0 / self.fps
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    rgb = next(src)
                except StopIteration:
                    if self.loop:
                        src = (self._file_frames() if self.mode == "file"
                               else self._gen_frames(self.mode.split(":", 1)[1]))
                        continue
                    break
                if rgb is None:
                    break
                if self._stop.is_set():
                    break
                self.last_color = rgb
                self.frames += 1
                try:
                    self.send(rgb[0], rgb[1], rgb[2])
                except Exception:
                    pass
                dt = time.monotonic() - t0
                self._stop.wait(max(0.0, period - dt))
        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False

    # ---------- sources ----------
    def _file_frames(self):
        ext = os.path.splitext(self.path)[1].lower()
        if ext in IMAGE_EXT:
            yield from self._image_frames()
        else:
            yield from self._video_frames()

    def _image_frames(self):
        from PIL import Image
        img = Image.open(self.path)
        n = getattr(img, "n_frames", 1)
        for i in range(n):
            if self._stop.is_set():
                return
            try:
                img.seek(i)
            except EOFError:
                break
            frame = img.convert("RGB").resize((FRAME_W, FRAME_H))
            rgb = _avg(frame.tobytes())
            yield _scale(rgb, self.brightness)

    def _video_frames(self):
        exe = ffmpeg_path()
        if not exe:
            raise RuntimeError("ffmpeg not found")
        cmd = [
            exe, "-v", "error", "-i", self.path,
            "-vf", f"scale={FRAME_W}:{FRAME_H}",
            "-r", f"{self.fps:g}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        flags = 0x08000000  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            startupinfo=si, creationflags=flags, bufsize=FRAME_BYTES * 2)
        try:
            while not self._stop.is_set():
                buf = self._read_exact(self._proc.stdout, FRAME_BYTES)
                if not buf or len(buf) < FRAME_BYTES:
                    break
                yield _scale(_avg(buf), self.brightness)
        finally:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            chunk = stream.read(n - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _gen_frames(self, name: str):
        t = 0.0
        dt = 1.0 / self.fps
        base = self.base_color
        while not self._stop.is_set():
            if name == "rainbow":
                h = (t * 60.0) % 360.0
                r, g, b = colorsys.hsv_to_rgb(h / 360.0, 1.0, 1.0)
                rgb = (int(r * 255), int(g * 255), int(b * 255))
            elif name == "pulse":
                k = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(t * 2.2))
                rgb = (int(base[0] * k), int(base[1] * k), int(base[2] * k))
            elif name == "chase":
                k = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(t * 4.0))
                rgb = (int(base[0] * k), int(base[1] * k), int(base[2] * k))
            elif name == "fire":
                r = 255
                g = 70 + int(90 * (0.5 + 0.5 * math.sin(t * 7.0 + random.random())))
                b = 10 + int(25 * random.random())
                rgb = (r, g, b)
            elif name == "ocean":
                h = (190 + 25 * math.sin(t * 1.3)) % 360
                v = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 1.7))
                r, g, b = colorsys.hsv_to_rgb(h / 360.0, 0.85, v)
                rgb = (int(r * 255), int(g * 255), int(b * 255))
            else:
                rgb = base
            yield _scale(rgb, self.brightness)
            t += dt
