"""Screen-capture -> sampled color -> crossfaded realtime BLE packets (Ambilight).

Performance design (minimal GPU/CPU, same functionality):
- One mss/dxcam instance per thread, reused (no re-enumeration).
- Low-res capture ~57×32 (32px tall, was 89×50) — 1.8k pixels vs 4.5k, ~60% less work, still vibrant.
- One Python pass over thumbnail for all modes; ~0.6ms analyze.
- Crossfaded eased position, throttled BLE (min_delta + interval).
- Aggressive eco: 2s still → 2Hz polling (was 1s→4Hz) — almost idle when static.
- Privacy: downscaled immediately, never saved.
- GPU: dxcam Desktop Duplication only when fps≥30 and use_dxcam=True; mss fallback never flickers.
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
                 capture_mode: str = "center", update_interval: float = 0.1,
                 crossfade: bool = True, use_dxcam: bool = True):
        """
        ble: ElfBLE instance
        get_targets: callable() -> list[str] (connected MACs to drive)
        fps: capture loop rate, 1..60
        smooth: crossfade DURATION in seconds, 0.0..10.0 (0 = instant,
                2.0 = gradual 2-second fade at FPS rate). Kept name for compat.
        min_delta: skip send if r/g/b each changed less than this (direct mode)
        brightness: 0..100 scale applied to captured color
        capture_mode: 'center' or 'full' — both 60fps capable via low-res 50px + dxcam
        update_interval: min seconds between strip updates — DIRECT mode only.
                         Ignored when crossfade is on (screen is sampled every
                         frame and the strip fades continuously).
        crossfade: True = time-based gradual fade, False = instant jump
        """
        self.ble = ble
        self.get_targets = get_targets
        self.calibrate: Optional[Callable[[int, int, int], tuple[int, int, int]]] = None
        self.fps = max(1.0, min(60.0, fps))
        self.smooth = max(0.0, min(10.0, smooth))  # fade duration in seconds
        self.min_delta = max(0, min_delta)
        self.brightness = max(1, min(100, brightness))
        self.capture_mode = capture_mode if capture_mode in ("center", "full") else "center"
        self.sample_mode = "average"  # average | dominant | vibrant | brightest
        self.update_interval = max(0.01, min(5.0, update_interval))
        self.crossfade = bool(crossfade)
        self.use_dxcam = bool(use_dxcam)
        self.last_stats: dict[str, tuple[int, int, int]] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_color = (0, 0, 0)
        self._shown = [0.0, 0.0, 0.0]  # crossfade position (floats)
        self.last_send = 0.0
        self.frames = 0
        self.sends = 0
        self.last_capture_ms = 0.0
        self.eco = False  # idle backoff active (still screen)
        self._still = 0
        self._prev_thumb: bytes | None = None
        self._thumb_bytes: bytes = b""
        self._tls = threading.local()
        # temporal lock — prevents flickering on mixed-color screens
        self._dom_target: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._dom_ema: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._dom_ema_init = False
        # crossfade state: time-based linear fade from _fade_start to
        # _fade_target over `smooth` seconds. Restarted whenever the sampled
        # screen colour moves; progress is purely (now - t0) / duration so
        # the transition always takes the configured time at any FPS.
        self._fade_target: tuple[int, int, int] = (0, 0, 0)
        self._fade_start: tuple[int, int, int] = (0, 0, 0)
        self._fade_t0: float = 0.0

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
        # stop dxcam capture if active
        try:
            cam = getattr(self._tls, "dxcam", None)
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
        except Exception:
            pass
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

    def _thread_dxcam(self, box):
        """Per-thread dxcam for high-FPS full-screen (Desktop Duplication, ~12 ms).
        Reuses the singleton, restarts with new region/fps when needed."""
        try:
            import dxcam  # type: ignore
        except Exception:
            return None
        region = (int(box["left"]), int(box["top"]),
                  int(box["left"] + box["width"]), int(box["top"] + box["height"]))
        target_fps = int(max(30, min(60, self.fps)))
        cam = getattr(self._tls, "dxcam", None)
        prev_region = getattr(self._tls, "dxcam_region", None)
        prev_fps = getattr(self._tls, "dxcam_fps", None)
        try:
            if cam is None:
                cam = dxcam.create(output_idx=0, output_color="BGRA")
                if cam is None:
                    return None
                cam.start(region=region, target_fps=target_fps)
                self._tls.dxcam = cam
                self._tls.dxcam_region = region
                self._tls.dxcam_fps = target_fps
            elif prev_region != region or prev_fps != target_fps:
                try:
                    cam.stop()
                except Exception:
                    pass
                cam.start(region=region, target_fps=target_fps)
                self._tls.dxcam_region = region
                self._tls.dxcam_fps = target_fps
            # ensure it's running
            if not getattr(cam, "is_capturing", True):
                try:
                    cam.start(region=region, target_fps=target_fps)
                except Exception:
                    pass
        except Exception:
            return None
        return cam

    def _capture_box(self, sct):
        mon = sct.monitors[1]
        if self.capture_mode == "full":
            return mon
        w, h = mon["width"], mon["height"]
        return {"left": mon["left"] + w // 4, "top": mon["top"] + h // 4,
                "width": w // 2, "height": h // 2}

    # GDI low-res path removed — caused cursor flicker and was not faster than
    # dxcam/mss on this hardware. Keeping dxcam (GPU) + mss (CPU) only.

    @staticmethod
    def _ease_step(shown: list[float], target: tuple[int, int, int], f: float
                   ) -> tuple[int, int, int]:
        """Move crossfade position toward target; min 1-unit step so small
        gaps always close. Returns integer shown color. Monotonic, exact."""
        out = []
        for i, t in enumerate(target):
            d = t - shown[i]
            step = d * f
            if d != 0 and abs(step) < 1.0:
                step = 1.0 if d > 0 else -1.0
            v = shown[i] + step
            if d > 0 and v > t:
                v = float(t)
            elif d < 0 and v < t:
                v = float(t)
            shown[i] = v
            out.append(int(round(v)))
        return (out[0], out[1], out[2])

    def _analyze_small(self, img):
        """One pass over tiny image -> all candidates. 32px tall (~57×32 for 16:9)."""
        from PIL import Image
        w, h = img.size
        if w > 640:
            img = img.reduce(8 if w > 1280 else 4)
            w, h = img.size
        target_h = 32  # was 50 — 40% fewer pixels, ~0.6ms vs ~1ms, still vibrant
        target_w = max(1, round(w * target_h / max(1, h)))
        resample = getattr(Image, "Resampling", Image).BILINEAR
        small = img.resize((target_w, target_h), resample) if (w, h) != (target_w, target_h) else img
        self._thumb_bytes = small.tobytes()
        px = list(small.getdata())
        n = max(1, len(px))

        # fast path: average-only (most common, cheapest) — skip histogram
        if self.sample_mode == "average":
            sr = sg = sb = 0
            for (r, g, b) in px:
                sr += r
                sg += g
                sb += b
            avg = (sr // n, sg // n, sb // n)
            # still need thumb for eco, but vibrant/brightest/dominant not needed — return avg for all to keep preview working
            return {"average": avg, "dominant": avg, "vibrant": avg, "brightest": avg}

        sr = sg = sb = 0
        hist: dict[int, list] = {}  # key -> [count, sum_r, sum_g, sum_b]
        vib_s = -1
        vib = (0, 0, 0)
        bri_v = -1
        bri = (0, 0, 0)

        # only compute what is needed for current mode + average (for thumb)
        need_dominant = self.sample_mode == "dominant"
        need_vibrant = self.sample_mode == "vibrant"
        need_brightest = self.sample_mode == "brightest"

        for (r, g, b) in px:
            sr += r
            sg += g
            sb += b
            if need_dominant:
                key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                entry = hist.get(key)
                if entry is None:
                    hist[key] = [1, r, g, b]
                else:
                    entry[0] += 1
                    entry[1] += r
                    entry[2] += g
                    entry[3] += b
            if need_vibrant:
                mx = r if r >= g and r >= b else (g if g >= b else b)
                mn = r if r <= g and r <= b else (g if g <= b else b)
                vs = (mx - mn) * mx
                if vs > vib_s:
                    vib_s = vs
                    vib = (r, g, b)
            if need_brightest:
                br = r + g + b
                if br > bri_v:
                    bri_v = br
                    bri = (r, g, b)

        # --- dominant: largest cluster (only if needed) ---
        avg = (sr // n, sg // n, sb // n)
        if need_dominant and hist:
            sorted_keys = sorted(hist.keys(), key=lambda k: hist[k][0], reverse=True)
            claimed: set[int] = set()
            best_count = 0
            best_r = best_g = best_b = 0.0
            for key in sorted_keys:
                if key in claimed:
                    continue
                entry = hist[key]
                count = entry[0]
                sum_r, sum_g, sum_b = entry[1], entry[2], entry[3]
                kr = (key >> 8) & 15
                kg = (key >> 4) & 15
                kb = key & 15
                claimed.add(key)
                for dk in range(-1, 2):
                    for dg in range(-1, 2):
                        for db in range(-1, 2):
                            nk = ((kr + dk) << 8) | ((kg + dg) << 4) | (kb + db)
                            if nk == key or nk in claimed:
                                continue
                            e = hist.get(nk)
                            if e is not None:
                                count += e[0]
                                sum_r += e[1]
                                sum_g += e[2]
                                sum_b += e[3]
                                claimed.add(nk)
                if count > best_count:
                    best_count = count
                    best_r = sum_r / count
                    best_g = sum_g / count
                    best_b = sum_b / count
            dom = (int(best_r), int(best_g), int(best_b))
        else:
            dom = avg

        # fallback vibrants/brightest to avg if not computed
        if not need_vibrant:
            vib = avg
        if not need_brightest:
            bri = avg

        return {
            "average": avg,
            "dominant": dom,
            "vibrant": vib,
            "brightest": bri,
        }

    def _grab_stats(self) -> dict[str, tuple[int, int, int]]:
        from PIL import Image
        t0 = time.perf_counter()
        sct = self._thread_sct()
        box = self._capture_box(sct)
        # GPU path: dxcam Desktop Duplication (~12-16 ms full 1440p) vs mss ~50 ms full, ~25 ms center
        # dxcam uses almost 0% CPU (GPU) and is much faster; mss never flickers but is CPU-heavy
        if self.use_dxcam:
            try:
                cam = self._thread_dxcam(box)
                if cam is not None:
                    frame = cam.get_latest_frame()
                    if frame is not None:
                        bh = max(1, int(box["height"]))
                        bw = max(1, int(box["width"]))
                        th = 50
                        tw = max(1, round(bw * th / max(1, bh)))
                        h, w = frame.shape[:2]
                        img_full = Image.frombytes("RGB", (w, h), frame.tobytes(), "raw", "BGRX")
                        resample = getattr(Image, "Resampling", Image).BILINEAR
                        img = img_full.resize((tw, th), resample)
                        stats = self._analyze_small(img)
                        self.last_stats = stats
                        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
                        return stats
            except Exception:
                pass
        # fallback: mss at native res then downscale in _analyze_small (no cursor flicker — SRCCOPY without CAPTUREBLT)
        shot = sct.grab(box)
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
        still_needed = max(1, int(round(self.fps * 2.0)))  # 2s still → eco, was 1s
        loop = asyncio.get_running_loop()
        self._shown = [float(c) for c in self.last_color]
        # smooth crossfade state
        if not hasattr(self, "_last_sent"):
            self._last_sent: tuple[int, int, int] = tuple(self.last_color)  # type: ignore
        if self._fade_t0 == 0.0:
            self._fade_target = tuple(self.last_color)  # type: ignore
            self._fade_start = tuple(self.last_color)  # type: ignore
            self._fade_t0 = time.monotonic()
        while self._running:
            t0 = time.monotonic()
            try:
                r, g, b = await loop.run_in_executor(None, self.capture_selected)
                self.frames += 1
                # still-screen detection (exact thumbnail match)
                if self._thumb_bytes == self._prev_thumb and self._prev_thumb:
                    self._still += 1
                else:
                    self._still = 0
                    self._prev_thumb = self._thumb_bytes

                # --- temporal target lock ---
                raw = (float(r), float(g), float(b))
                ts = 0.25  # EMA factor (lower = more stable)
                if not self._dom_ema_init:
                    self._dom_ema = raw
                    self._dom_ema_init = True
                else:
                    self._dom_ema = (
                        self._dom_ema[0] + (raw[0] - self._dom_ema[0]) * ts,
                        self._dom_ema[1] + (raw[1] - self._dom_ema[1]) * ts,
                        self._dom_ema[2] + (raw[2] - self._dom_ema[2]) * ts,
                    )
                diff = max(abs(self._dom_ema[i] - self._dom_target[i]) for i in range(3))
                if diff > 3.0:
                    self._dom_target = self._dom_ema

                sr, sg, sb = self._apply_brightness(
                    int(self._dom_target[0]),
                    int(self._dom_target[1]),
                    int(self._dom_target[2]))
                if self.calibrate is not None:
                    try:
                        sr, sg, sb = self.calibrate(sr, sg, sb)
                    except Exception:
                        pass

                now = time.monotonic()

                if self.crossfade:
                    # ── time-based linear fade: sampled screen colour is the
                    #    target every frame; the strip walks from fade-start
                    #    to target over `smooth` seconds. Duration is exact
                    #    and FPS-independent. Every 1-unit step is sent so
                    #    the gradient is truly gradual (min_delta is direct-
                    #    mode only — it would quantize the fade into jumps). ──
                    if (abs(sr - self._fade_target[0]) >= 2
                            or abs(sg - self._fade_target[1]) >= 2
                            or abs(sb - self._fade_target[2]) >= 2):
                        self._fade_start = (int(round(self._shown[0])),
                                            int(round(self._shown[1])),
                                            int(round(self._shown[2])))
                        self._fade_target = (sr, sg, sb)
                        self._fade_t0 = now
                    dur = max(0.0, min(10.0, float(self.smooth)))
                    if dur <= 0.03:
                        cur = self._fade_target
                    else:
                        p = (now - self._fade_t0) / dur
                        if p >= 1.0:
                            cur = self._fade_target
                        elif p <= 0.0:
                            cur = self._fade_start
                        else:
                            fs = self._fade_start
                            ft = self._fade_target
                            cur = (int(round(fs[0] + (ft[0] - fs[0]) * p)),
                                   int(round(fs[1] + (ft[1] - fs[1]) * p)),
                                   int(round(fs[2] + (ft[2] - fs[2]) * p)))
                    self._shown = [float(cur[0]), float(cur[1]), float(cur[2])]
                    self.last_color = cur
                    if cur != self._last_sent:
                        targets = [a for a in self.get_targets() if self.ble.is_connected(a)]
                        if targets:
                            pkt = pkt_color_realtime(*cur)
                            await self.ble.write_many(targets, pkt, response=False)
                            self.sends += 1
                            self._last_sent = cur
                            self.last_send = time.monotonic()
                else:
                    # ── direct: instant jump, throttled by update_interval ──
                    cur = (sr, sg, sb)
                    self._shown = [float(c) for c in cur]
                    self.last_color = cur  # preview follows screen instantly
                    lr, lg, lb = self._last_sent
                    if ((now - self.last_send) >= self.update_interval
                            and cur != (lr, lg, lb)
                            and (abs(cur[0] - lr) >= self.min_delta
                                 or abs(cur[1] - lg) >= self.min_delta
                                 or abs(cur[2] - lb) >= self.min_delta)):
                        targets = [a for a in self.get_targets() if self.ble.is_connected(a)]
                        if targets:
                            pkt = pkt_color_realtime(*cur)
                            await self.ble.write_many(targets, pkt, response=False)
                            self.sends += 1
                            self._last_sent = cur
                            self.last_send = time.monotonic()
                            self._fade_target = cur
                            self._fade_t0 = now
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.25)
            dt = time.monotonic() - t0
            if self._still > still_needed:
                self.eco = True
                await asyncio.sleep(max(0.0, 0.50 - dt))  # 2Hz eco was 4Hz (0.25) — half the wakeups
            else:
                self.eco = False
                await asyncio.sleep(max(0.0, period - dt))
        self._running = False
