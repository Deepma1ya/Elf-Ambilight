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

GEN_NAMES = (
    "rainbow", "wave", "pulse", "chase", "aurora", "twinkle", "fire",
    "candle", "ocean", "police", "lightning", "heartbeat", "disco", "sunrise",
)
GEN_LABELS = {
    "rainbow": "Rainbow sweep",
    "wave": "Rainbow wave",
    "pulse": "Breathing pulse",
    "chase": "Colour chase",
    "aurora": "Aurora drift",
    "twinkle": "Twinkle sparks",
    "fire": "Fire flicker",
    "candle": "Candle glow",
    "ocean": "Ocean wave",
    "police": "Police lights",
    "lightning": "Lightning storm",
    "heartbeat": "Heartbeat",
    "disco": "Disco party",
    "sunrise": "Sunrise",
}


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb((h % 360.0) / 360.0,
                                  max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


def _mix(rgb: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    k = max(0.0, min(1.0, k))
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


def ffmpeg_path() -> str | None:
    """Our per-user install first, then PATH/common folders, then imageio."""
    try:
        import ffmpeg_setup  # type: ignore
        p, _ = ffmpeg_setup.find_ffmpeg()
        if p:
            return p
    except Exception:
        pass
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
        self.speed = 1.0
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
              base_color=(255, 255, 255), speed: float = 1.0) -> bool:
        self.stop()
        self.mode = mode
        self.fps = max(1.0, min(60.0, float(fps)))
        self.brightness = max(1, min(100, int(brightness)))
        self.speed = max(0.25, min(3.0, float(speed)))
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

    def set_tuning(self, fps=None, brightness=None, speed=None):
        """Live changes while playing (safe: plain attribute writes)."""
        try:
            if fps is not None:
                self.fps = max(1.0, min(60.0, float(fps)))
            if brightness is not None:
                self.brightness = max(1, min(100, int(brightness)))
            if speed is not None:
                self.speed = max(0.25, min(3.0, float(speed)))
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "name": self.name,
            "frames": self.frames,
            "fps": self.fps,
            "brightness": self.brightness,
            "speed": self.speed,
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
                period = 1.0 / max(1.0, self.fps)   # live FPS changes
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
        """Time-based animations. `speed` scales the effect tempo live and is
        independent of FPS; brightness is applied per frame."""
        t = 0.0
        last = time.monotonic()
        spark = 0.0
        flash = 0.0
        base = self.base_color
        while not self._stop.is_set():
            now = time.monotonic()
            step = now - last
            last = now
            if step <= 0.0 or step > 0.5:
                step = 1.0 / max(1.0, self.fps)
            t += step * max(0.05, float(self.speed))

            if name == "rainbow":
                rgb = _hsv(t * 60.0, 1.0, 1.0)
            elif name == "wave":
                v = 0.55 + 0.45 * math.sin(t * 3.0)
                rgb = _hsv(t * 28.0, 0.92, max(0.18, v))
            elif name == "pulse":
                k = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(t * 2.2))
                rgb = _mix(base, k)
            elif name == "chase":
                k = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(t * 4.0))
                rgb = _mix(base, k)
            elif name == "aurora":
                h = 150.0 + 55.0 * math.sin(t * 0.35) + 18.0 * math.sin(t * 0.9)
                v = 0.62 + 0.38 * (0.5 + 0.5 * math.sin(t * 0.7 + 1.2))
                rgb = _hsv(h, 0.88, v)
            elif name == "twinkle":
                spark = max(0.0, spark - step * 3.2)
                if random.random() < step * 5.0:
                    spark = 1.0
                k = 0.06 + 0.94 * spark
                rgb = (int(255 * k), int(238 * k), int(205 * k))
            elif name == "fire":
                g = 70 + int(90 * (0.5 + 0.5 * math.sin(t * 7.0 + random.random() * 6.28)))
                b = 10 + int(25 * random.random())
                rgb = (255, g, b)
            elif name == "candle":
                f = (0.72 + 0.16 * math.sin(t * 3.1)
                     + 0.08 * math.sin(t * 8.7 + 1.3)
                     + random.uniform(-0.05, 0.05))
                k = max(0.35, min(1.0, f))
                rgb = (255, int(150 * k), int(55 * k))
            elif name == "ocean":
                h = 190.0 + 25.0 * math.sin(t * 1.3)
                v = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 1.7))
                rgb = _hsv(h, 0.85, v)
            elif name == "police":
                phase = (t / 0.42) % 2.0
                k = 0.35 + 0.65 * (0.5 + 0.5 * math.cos(phase * math.pi * 2.0))
                rgb = ((int(255 * k), 0, int(30 * k)) if phase < 1.0
                       else (int(10 * k), int(40 * k), int(255 * k)))
            elif name == "lightning":
                flash = max(0.0, flash - step * 6.0)
                if random.random() < step * 1.6:
                    flash = 1.0
                k = min(1.0, flash)
                rgb = (int(40 + 200 * k), int(55 + 190 * k), int(90 + 165 * k))
            elif name == "heartbeat":
                p = t % 1.0
                k = 0.14
                if p < 0.10:
                    k = 1.0 - (p / 0.10) * 0.15
                elif p < 0.18:
                    k = 0.35
                elif p < 0.30:
                    k = 0.85 - ((p - 0.18) / 0.12) * 0.45
                rgb = (int(255 * k), int(16 * k), int(28 * k))
            elif name == "disco":
                beat = int(t / 0.33)
                rnd = random.Random(beat * 7919)
                v = 0.75 + 0.25 * math.sin(t * 9.0)
                rgb = _hsv(rnd.random() * 360.0, 1.0, max(0.4, v))
            elif name == "sunrise":
                cyc = (t / 12.0) % 1.0
                rgb = _hsv(8.0 + 42.0 * cyc, max(0.05, 1.0 - 0.75 * cyc),
                           0.35 + 0.65 * cyc)
            else:
                rgb = base
            yield _scale(rgb, self.brightness)
