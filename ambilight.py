"""Screen-capture -> average color -> realtime BLE packets (Ambilight).

Performance design (60fps capable):
- One mss instance per capture thread (thread-local), reused across frames.
  Creating mss.mss() per frame re-enumerates monitors (~5-15ms) — avoided.
- Averaging via PIL.ImageStat (C loop), not a Python per-pixel loop.
- Sends stay delta-gated: 60fps loop does NOT mean 60 BLE writes/sec.
"""
from __future__ import annotations
import asyncio
import threading
import time
from typing import Callable, Optional

from protocol import pkt_color_realtime


class Ambilight:
    def __init__(self, ble, get_targets, fps: float = 30.0, smooth: float = 0.35,
                 min_delta: int = 6, brightness: int = 100,
                 capture_mode: str = "center"):
        """
        ble: ElfBLE instance
        get_targets: callable() -> list[str] (connected MACs to drive)
        fps: loop rate, 1..60 (capture+average only; BLE sends are delta-gated)
        smooth: 0..1 exponential smoothing (higher = snappier)
        min_delta: skip send if r/g/b each changed less than this
        brightness: 0..100 scale applied to captured color
        capture_mode: 'center' (middle 50%, ~10ms, 60fps capable) or
                      'full' (whole screen, ~35ms at 1440p, use <=25fps)
        """
        self.ble = ble
        self.get_targets = get_targets
        self.calibrate: Optional[Callable[[int, int, int], tuple[int, int, int]]] = None
        self.fps = max(1.0, min(60.0, fps))
        self.smooth = max(0.05, min(1.0, smooth))
        self.min_delta = max(0, min_delta)
        self.brightness = max(1, min(100, brightness))
        self.capture_mode = capture_mode if capture_mode in ("center", "full") else "center"
        self.sample_mode = "average"  # average | dominant | vibrant | brightest
        self.last_stats: dict[str, tuple[int, int, int]] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_color = (0, 0, 0)
        self._sm = [0.0, 0.0, 0.0]
        self.last_send = 0.0
        self.frames = 0
        self.sends = 0
        self.last_capture_ms = 0.0
        self._tls = threading.local()

    def start(self):
        """Must be called from inside the asyncio loop thread (create_task
        needs a running loop in the current thread). Use the app's
        AsyncRunner.submit(_begin_async()) from UI threads."""
        if self._task and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "Ambilight.start() called without a running event loop; "
                "schedule it on the BLE loop thread instead")
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except Exception:
                self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    # ---- capture ----
    def _thread_sct(self):
        """Per-thread reused mss instance (mss is not thread-safe to share)."""
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            import mss
            sct = mss.MSS()
            self._tls.sct = sct
        return sct

    def _capture_box(self, sct):
        mon = sct.monitors[1]
        if self.capture_mode == "full":
            return mon
        w, h = mon["width"], mon["height"]
        return {"left": mon["left"] + w // 4, "top": mon["top"] + h // 4,
                "width": w // 2, "height": h // 2}

    @staticmethod
    def _analyze_small(img):
        """One pass over a tiny image -> all sampling candidates.

        average:   mean color (classic ambilight)
        dominant:  most frequent color (12-bit quantized histogram peak)
        vibrant:   pixel maximizing saturation*brightness (neon pop)
        brightest: pixel with max r+g+b (highlights/flashes)
        """
        from PIL import Image
        w, h = img.size
        if w > 640:  # fast box pre-shrink; BILINEAR finish below stays accurate
            img = img.reduce(8 if w > 1280 else 4)
            w, h = img.size
        th = max(1, round(h * 48 / max(1, w)))
        resample = getattr(Image, "Resampling", Image).BILINEAR
        small = img.resize((48, th), resample) if (w, h) != (48, th) else img
        px = list(small.getdata())
        n = max(1, len(px))
        sr = sg = sb = 0
        hist: dict[int, int] = {}
        dom_n = 0
        dom = (0, 0, 0)
        vib_s = -1
        vib = (0, 0, 0)
        bri_v = -1
        bri = (0, 0, 0)
        for (r, g, b) in px:
            sr += r
            sg += g
            sb += b
            key = ((r & 0xF0) << 4) | (g & 0xF0) | (b >> 4)
            c = hist.get(key, 0) + 1
            hist[key] = c
            if c > dom_n:
                dom_n = c
                dom = (((key >> 8) & 15) * 17, ((key >> 4) & 15) * 17, (key & 15) * 17)
            mx = r if r >= g and r >= b else (g if g >= b else b)
            mn = r if r <= g and r <= b else (g if g <= b else b)
            vs = (mx - mn) * mx
            if vs > vib_s:
                vib_s = vs
                vib = (r, g, b)
            br = r + g + b
            if br > bri_v:
                bri_v = br
                bri = (r, g, b)
        return {
            "average": (sr // n, sg // n, sb // n),
            "dominant": dom,
            "vibrant": vib,
            "brightest": bri,
        }

    def _grab_stats(self) -> dict[str, tuple[int, int, int]]:
        from PIL import Image
        t0 = time.perf_counter()
        sct = self._thread_sct()
        shot = sct.grab(self._capture_box(sct))
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        stats = self._analyze_small(img)
        self.last_stats = stats
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        return stats

    def capture_average(self) -> tuple[int, int, int]:
        """Grab screen region, return average (r,g,b)."""
        return self._grab_stats()["average"]

    def capture_selected(self) -> tuple[int, int, int]:
        """Grab screen region, return the active sample-mode candidate."""
        stats = self._grab_stats()
        return stats.get(self.sample_mode, stats["average"])

    def _apply_brightness(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        k = self.brightness / 100.0
        return (int(r * k), int(g * k), int(b * k))

    async def _loop(self):
        period = 1.0 / max(1.0, min(60.0, self.fps))
        loop = asyncio.get_running_loop()
        while self._running:
            t0 = time.monotonic()
            try:
                r, g, b = await loop.run_in_executor(None, self.capture_selected)
                self.frames += 1
                # smooth
                s = self.smooth
                self._sm[0] += (r - self._sm[0]) * s
                self._sm[1] += (g - self._sm[1]) * s
                self._sm[2] += (b - self._sm[2]) * s
                sr, sg, sb = (int(self._sm[0]), int(self._sm[1]), int(self._sm[2]))
                sr, sg, sb = self._apply_brightness(sr, sg, sb)
                if self.calibrate is not None:
                    try:
                        sr, sg, sb = self.calibrate(sr, sg, sb)
                    except Exception:
                        pass
                lr, lg, lb = self.last_color
                if (abs(sr - lr) >= self.min_delta or abs(sg - lg) >= self.min_delta
                        or abs(sb - lb) >= self.min_delta):
                    targets = [a for a in self.get_targets() if self.ble.is_connected(a)]
                    if targets:
                        pkt = pkt_color_realtime(sr, sg, sb)
                        await self.ble.write_many(targets, pkt, response=False)
                        self.sends += 1
                        self.last_color = (sr, sg, sb)
                        self.last_send = time.monotonic()
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.25)
            dt = time.monotonic() - t0
            await asyncio.sleep(max(0.0, period - dt))
        self._running = False
