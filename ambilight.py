"""Screen-capture -> sampled color -> crossfaded realtime BLE packets (Ambilight).

Performance design (imperceptible: sampling decoupled from fading):
- Screen is sampled at ~5Hz in a low-priority background thread (2Hz eco
  when still) — a 19-42ms GDI/DXGI grab 5x/sec instead of 20-60x/sec is
  what keeps Windows feeling perfectly smooth.
- Fading/interpolation runs capture-free at FPS rate (cheap lerp + tiny
  BLE packet only while the colour is actually moving; near-zero when idle).
- One mss/dxcam instance reused (no re-enumeration); dxcam duplication
  runs at the sample rate, not the fade rate.
- Low-res ~57×32 thumbnails (1.8k px), C-speed ImageStat averaging.
- Privacy: downscaled immediately, never saved.
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
                 crossfade: bool = True, use_dxcam: bool = True,
                 sample_hz: float = 5.0):
        """
        ble: ElfBLE instance
        get_targets: callable() -> list[str] (connected MACs to drive)
        fps: fade/render rate, 1..60 (capture-free lerp — cheap, can be high)
        smooth: crossfade DURATION in seconds, 0.0..10.0 (0 = instant,
                2.0 = gradual 2-second fade at FPS rate). Kept name for compat.
        min_delta: skip send if r/g/b each changed less than this (direct mode)
        brightness: 0..100 scale applied to captured color
        capture_mode: 'center' or 'full'
        update_interval: min seconds between strip updates — DIRECT mode only.
                         Ignored when crossfade is on.
        crossfade: True = time-based gradual fade, False = instant jump
        sample_hz: screen sampling rate for the background sampler thread
                   (default 5Hz, 2Hz eco when still). The expensive grab runs
                   here — never in the fade loop — so Windows stays silky.
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
        self.sample_hz = max(1.0, min(15.0, sample_hz))
        # optional Windows Dynamic Lighting mirror (DynLight, never blocks)
        self.dynlight = None
        # capture monitor: 1-based mss index (1 = primary), 0 = all monitors
        self.ambi_monitor = 1
        # latest sampled screen colour (written by sampler thread, read by loop)
        self._sampled_target: tuple[int, int, int] = (0, 0, 0)
        self._sampler_thread: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._sampler_cam = None  # dxcam instance owned by the sampler thread
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
        # fresh sampler state so the first fade converges immediately
        self._dom_ema_init = False
        self._sampled_target = tuple(self.last_color)  # type: ignore
        self._still = 0
        self._prev_thumb = None
        self.eco = False
        self._sampler_stop.clear()
        th = self._sampler_thread
        if th is None or not th.is_alive():
            self._sampler_thread = threading.Thread(
                target=self._sampler_run, name="ambi-sampler", daemon=True)
            self._sampler_thread.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        self._sampler_stop.set()
        th = self._sampler_thread
        if th is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, th.join, 3.0)
            except Exception:
                pass
            self._sampler_thread = None
        # sampler thread stops its own dxcam on exit; defensive stop here too
        try:
            cam = self._sampler_cam
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
        except Exception:
            pass
        self._sampler_cam = None
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except Exception:
                self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    # ---- background sampler (the only place that touches the screen) ----
    def _sampler_run(self):
        """Low-rate screen sampling on a background thread.

        A 19-42ms grab at 5Hz (~100-200ms of CPU per second, bursty) is
        invisible; the same grab at 20-60Hz in the fade loop (~40% of a
        core + compositor stalls) is what made Windows feel laggy.
        """
        try:  # keep the sampler out of the way of games / the compositor
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), -2)  # LOWEST
        except Exception:
            pass
        still_needed = max(1, int(round(self.sample_hz * 2.0)))  # 2s still → eco
        while not self._sampler_stop.is_set():
            t0 = time.monotonic()
            try:
                self._sample_once(still_needed)
            except Exception:
                pass
            # eco: still screen → 2Hz; moving screen → sample_hz
            interval = 0.50 if self._still > still_needed else 1.0 / max(1.0, self.sample_hz)
            dt = time.monotonic() - t0
            self._sampler_stop.wait(max(0.0, interval - dt))
        # release Desktop Duplication promptly so no GPU/compositor state lingers
        try:
            cam = self._sampler_cam
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
        except Exception:
            pass

    def _sample_once(self, still_needed: int):
        """One capture → analyze → EMA → publish. Sampler thread only."""
        t0 = time.perf_counter()
        stats = self._grab_stats()
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        # still-screen detection (exact thumbnail match)
        if self._thumb_bytes == self._prev_thumb and self._prev_thumb:
            self._still += 1
        else:
            self._still = 0
            self._prev_thumb = self._thumb_bytes
        self.eco = self._still > still_needed
        # --- temporal target lock (prevents flicker on mixed screens) ---
        raw_c = stats.get(self.sample_mode, stats["average"])
        raw = (float(raw_c[0]), float(raw_c[1]), float(raw_c[2]))
        ts = 0.25  # EMA factor (lower = more stable)
        if not self._dom_ema_init:
            self._dom_ema = raw
            self._dom_target = raw
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
        self._sampled_target = (int(self._dom_target[0]),
                                int(self._dom_target[1]),
                                int(self._dom_target[2]))

    # ---- capture ----
    def _thread_sct(self):
        """Per-thread reused mss instance (mss is not thread-safe to share)."""
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            import mss
            sct = mss.MSS()
            self._tls.sct = sct
        return sct

    def _thread_dxcam(self, box, ox: int = 0, oy: int = 0):
        """Sampler-thread dxcam (Desktop Duplication) on the primary output.
        Duplication runs at the *sample* rate (5Hz), not the fade rate.
        Region is relative to the output origin (fixes non-zero primaries).
        Other monitors use the mss fallback — dxcam's singleton cannot
        reliably hop outputs, and 5Hz mss is still cheap."""
        try:
            import dxcam  # type: ignore
        except Exception:
            return None
        region = (int(box["left"] - ox), int(box["top"] - oy),
                  int(box["left"] - ox + box["width"]),
                  int(box["top"] - oy + box["height"]))
        target_fps = int(max(5, min(15, self.sample_hz)))
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
        self._sampler_cam = cam
        return cam

    def _capture_box(self, sct):
        """Capture box + owning monitor for the selected monitor index.

        Returns (box, mon, use_dxcam_ok): index 0 = all monitors (mss only,
        dxcam cannot span outputs), 1-based = that mss monitor (clamped).
        """
        try:
            mons = sct.monitors
        except Exception:
            mons = []
        idx = max(0, int(self.ambi_monitor))
        if not mons:
            mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
            return ({"left": 480, "top": 270, "width": 960, "height": 540},
                    mon, False)
        if idx <= 0:
            mon = mons[0]  # bounding box of all monitors
            return (dict(mon), mon, False)
        if idx >= len(mons):
            idx = 1
        mon = mons[idx]
        if self.capture_mode == "full":
            return (dict(mon), mon, idx == 1)
        w, h = mon["width"], mon["height"]
        box = {"left": mon["left"] + w // 4, "top": mon["top"] + h // 4,
               "width": w // 2, "height": h // 2}
        return (box, mon, idx == 1)

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

        # fast path: average-only (most common) — C-speed ImageStat, no
        # Python per-pixel loop, no histogram. ~0.06ms vs ~0.36ms.
        if self.sample_mode == "average":
            from PIL import ImageStat
            m = ImageStat.Stat(small).mean
            avg = (int(m[0]), int(m[1]), int(m[2]))
            # thumb already stored for eco; fill all keys for the preview
            return {"average": avg, "dominant": avg, "vibrant": avg, "brightest": avg}

        px = list(small.getdata())
        n = max(1, len(px))
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
        box, mon, dx_ok = self._capture_box(sct)
        # GPU path: dxcam Desktop Duplication; single resize straight to the
        # thumbnail size (no intermediate 50px step). mss fallback never
        # flickers (SRCCOPY without CAPTUREBLT) but costs a GDI readback —
        # which is exactly why sampling runs at 5Hz in the background.
        if self.use_dxcam and dx_ok:
            try:
                cam = self._thread_dxcam(box, int(mon.get("left", 0)),
                                         int(mon.get("top", 0)))
                if cam is not None:
                    frame = cam.get_latest_frame()
                    if frame is not None:
                        bh = max(1, int(box["height"]))
                        bw = max(1, int(box["width"]))
                        th = 32
                        tw = max(1, round(bw * th / max(1, bh)))
                        h, w = frame.shape[:2]
                        img_full = Image.frombytes("RGB", (w, h), frame.tobytes(), "raw", "BGRX")
                        resample = getattr(Image, "Resampling", Image).BILINEAR
                        img = img_full.resize((tw, th), resample)
                        stats = self._analyze_small(img)
                        self.last_stats = stats
                        return stats
            except Exception:
                pass
        # fallback: mss at native res then downscale in _analyze_small
        shot = sct.grab(box)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        stats = self._analyze_small(img)
        self.last_stats = stats
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
        """Capture-free fade loop. Reads the sampler's latest colour, walks
        the strip toward it, sends on change. No grabs, no resizes, no
        executor hops — a few microseconds per frame, so Windows never
        feels it. When idle (eco + fade settled) it naps at 4Hz."""
        period = 1.0 / max(1.0, min(60.0, self.fps))
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
                self.frames += 1
                st = self._sampled_target
                sr, sg, sb = self._apply_brightness(st[0], st[1], st[2])
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
                        raw = self.get_targets()
                        targets = [a for a in raw if self.ble.is_connected(a)]
                        if targets:
                            pkt = pkt_color_realtime(*cur)
                            await self.ble.write_many(targets, pkt, response=False)
                            self.sends += 1
                            self._last_sent = cur
                            self.last_send = time.monotonic()
                        try:  # PC strip follows only when it is in the group
                            from dynlight import VIRTUAL_ADDR
                            if self.dynlight is not None and VIRTUAL_ADDR in raw:
                                self.dynlight.push(*cur)
                        except Exception:
                            pass
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
                        raw = self.get_targets()
                        targets = [a for a in raw if self.ble.is_connected(a)]
                        if targets:
                            pkt = pkt_color_realtime(*cur)
                            await self.ble.write_many(targets, pkt, response=False)
                            self.sends += 1
                            self._last_sent = cur
                            self.last_send = time.monotonic()
                            self._fade_target = cur
                            self._fade_t0 = now
                        try:  # PC strip follows only when it is in the group
                            from dynlight import VIRTUAL_ADDR
                            if self.dynlight is not None and VIRTUAL_ADDR in raw:
                                self.dynlight.push(*cur)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.25)
            dt = time.monotonic() - t0
            # idle nap: eco still-screen + fade settled + nothing new to send
            # → 4Hz instead of full FPS. Wakes instantly: any new sample
            # restarts the fade and the next frame sends again.
            try:
                settled = (self.last_color == self._fade_target
                           and self.last_color == self._last_sent)
            except Exception:
                settled = False
            if self.eco and settled:
                await asyncio.sleep(max(0.0, 0.25 - dt))
            else:
                await asyncio.sleep(max(0.0, period - dt))
        self._running = False
