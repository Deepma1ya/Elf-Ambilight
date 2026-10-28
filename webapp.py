"""Elf-Ambilight WebUI backend — embedded browser UI (pywebview + WebView2).

Same engine as the desktop app (BLE, calibration, ambilight, schedules,
startup task, tray); only the view layer is HTML/CSS/JS in webui/.
No external browser is ever opened — the page renders inside our window.
"""
from __future__ import annotations
import asyncio
import ctypes
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

APP_NAME = "Elf-Ambilight"
APP_VERSION = "1.2.0"

from protocol import (
    MODES, pkt_power, pkt_brightness, pkt_color_static, pkt_mode,
    pkt_mode_speed, pkt_single_color_w, pkt_cct, pkt_rgbw,
    pkt_timing, pkt_system_time, pkt_pin_sequence,
)
from ble_manager import ElfBLE
from ambilight import Ambilight
from config import Config, CONFIG_PATH
from bt_helper import bt_service_running, try_enable_bluetooth, open_bluetooth_settings
import app_rules
import media_player
from startup_helper import (startup_enabled, set_startup, task_enabled,
                            is_elevated, relaunch_elevated_task,
                            enable_high_priority)

COLOR_PRESETS = [
    (255, 255, 255), (255, 250, 250), (255, 255, 240), (255, 253, 208),
    (255, 180, 100), (255, 215, 0), (255, 165, 0), (255, 127, 80),
    (255, 0, 0), (255, 0, 127), (255, 192, 203), (255, 105, 180),
    (170, 60, 255), (75, 0, 130), (0, 120, 255), (135, 206, 235),
    (0, 255, 255), (0, 128, 128), (0, 255, 0), (0, 180, 0),
    (34, 139, 34), (128, 128, 0), (165, 42, 42), (255, 218, 185),
]
AMBI_BUILTINS = {
    "Movie": {"fps": 30, "smooth": 2.0, "brightness": 80, "min_delta": 8,
              "interval": 0.80, "mode": "center", "sample": "average"},
    "Game": {"fps": 60, "smooth": 0.4, "brightness": 100, "min_delta": 3,
             "interval": 0.05, "mode": "center", "sample": "vibrant"},
    "Chill": {"fps": 15, "smooth": 3.0, "brightness": 60, "min_delta": 10,
              "interval": 1.00, "mode": "center", "sample": "average"},
    "Party": {"fps": 30, "smooth": 1.0, "brightness": 100, "min_delta": 2,
              "interval": 0.30, "mode": "center", "sample": "brightest"},
    "Balanced": {"fps": 60, "smooth": 0.2, "brightness": 100, "min_delta": 9,
                 "interval": 0.12, "mode": "full", "sample": "vibrant",
                 "crossfade": True, "dxcam": True},
}
BRI_CH = {"ALL": 0, "RGB": 1, "W": 2, "CT": 3, "MODE": 0xFF}
DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _webui_file(name: str) -> str:
    try:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            p = Path(sys._MEIPASS) / "webui" / name  # type: ignore[attr-defined]
            if p.is_file():
                return str(p)
    except Exception:
        pass
    return str(Path(__file__).with_name("webui") / name)


class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class Backend:
    """All app state + engine. The Api class below is the JS bridge."""

    def __init__(self):
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        self.runner = AsyncRunner()
        self.ble = ElfBLE()
        self.cfg = Config.load()
        self.cfg.save()
        self.window = None
        self._dirty: set[str] = set()
        self._manual_disc: set[str] = set()
        self._ever_connected = False
        self._boot_retries = 6
        self._sched_fired: dict[int, str] = {}
        self._bt_prompted = False
        self._bt_state = "unknown"
        self.found: dict = {}
        self._quitting = False
        self.ambilight = Ambilight(self.ble, self._ambi_targets,
                                   fps=self.cfg.ambi_fps,
                                   smooth=self.cfg.ambi_smooth,
                                   min_delta=self.cfg.ambi_min_delta,
                                   brightness=self.cfg.ambi_brightness,
                                   capture_mode=self.cfg.ambi_mode,
                                   update_interval=self.cfg.ambi_interval,
                                   crossfade=self.cfg.ambi_crossfade,
                                   use_dxcam=self.cfg.ambi_use_dxcam,
                                   white_floor=self.cfg.ambi_white_floor)
        self.ambilight.sample_mode = self.cfg.ambi_sample
        self.ambilight.calibrate = self.cfg.cal_apply
        try:
            self.ambilight.ambi_monitor = max(0, int(self.cfg.ambi_monitor))
        except Exception:
            pass
        from dynlight import DynLight
        self.dynlight = DynLight()
        self.dynlight.enabled = bool(self.cfg.dynlight_enabled)
        self.ambilight.dynlight = self.dynlight
        self._tray = None
        self._hidden_for_sync = False
        self._ambi_rule_owner = None
        self._rule_prev: set[int] = set()
        self.media = None
        from collections import deque
        self._events = deque()
        self._monitors_cache = None
        self._monitors_t = 0.0

    # ── push → JS ──
    # Thread-safe event queue drained by frontend poll() — never
    # evaluate_js from worker threads (COM UI-thread violations).
    def push(self, evt: dict):
        try:
            q = self._events
            q.append(evt)
            while len(q) > 200:
                q.popleft()
        except Exception:
            pass

    def log(self, msg: str):
        try:
            self.push({"t": "log", "msg": str(msg)[:300]})
        except Exception:
            pass

    # ── engine helpers (ported 1:1 from the desktop app) ──
    def _targets(self) -> list[str]:
        addrs = [a for a in self.cfg.targets() if self.ble.is_connected(a)]
        try:
            from dynlight import VIRTUAL_ADDR
            if (VIRTUAL_ADDR in self.cfg.targets()
                    and VIRTUAL_ADDR not in addrs
                    and self.dynlight.lamp_count > 0):
                addrs.append(VIRTUAL_ADDR)
        except Exception:
            pass
        return addrs or self.ble.connected_addresses()

    def _ambi_targets(self):
        return self._targets()

    def _send_pkt(self, pkt, what):
        addrs = self._targets()
        try:
            from dynlight import VIRTUAL_ADDR
            want_dyn = VIRTUAL_ADDR in addrs
        except Exception:
            want_dyn = False
        ble_addrs = [a for a in addrs if a != VIRTUAL_ADDR]
        if not ble_addrs and not want_dyn:
            self.log("No device connected.")
            return
        note = ""
        dyn_rgb = None
        if (len(pkt) == 9 and pkt[0] == 0x7E and pkt[1] == 0x07
                and pkt[2] == 0x05 and pkt[3] == 0x03 and pkt[7] in (0x10, 0x20)):
            r, g, b = pkt[4], pkt[5], pkt[6]
            cr, cg, cb = self.cfg.cal_apply(r, g, b)
            if (cr, cg, cb) != (r, g, b):
                pkt = bytes([pkt[0], pkt[1], pkt[2], pkt[3], cr, cg, cb, pkt[7], pkt[8]])
                note = f" cal({cr},{cg},{cb})"
            dyn_rgb = (cr, cg, cb)
        if dyn_rgb is not None and want_dyn:
            try:
                self.dynlight.push(*dyn_rgb)
            except Exception:
                pass

        async def _do():
            if not ble_addrs:
                return
            res = await self.ble.write_many(ble_addrs, pkt)
            fails = [a for a, ok, _ in res if not ok]
            if fails:
                raise RuntimeError(f"write failed: {fails}")
        dest = f"{len(ble_addrs)} dev" if ble_addrs else "PC Lighting"
        if ble_addrs and want_dyn and dyn_rgb is not None:
            dest += " + PC"
        self.log(f"TX {what}{note} → {dest}")
        try:
            fut = self.runner.submit(_do())
            def _cb(f):
                try:
                    f.result(timeout=60)
                except Exception as e:
                    self.log(f"Error: {e}")
            fut.add_done_callback(_cb)
        except Exception as e:
            self.log(f"Error: {e}")

    def _mark_dirty(self, page: str = "settings"):
        self._dirty.add(page)

    # ── state snapshot for the frontend ──
    def get_state(self) -> dict:
        try:
            mons = self._list_monitors()
        except Exception:
            mons = [[1, "Monitor 1 (primary)"]]
        connected = self.ble.connected_addresses()
        found = [{"addr": d.address, "name": d.name, "rssi": d.rssi,
                  "connected": self.ble.is_connected(d.address)}
                 for d in sorted(self.found.values(), key=lambda d: -d.rssi)]
        members = []
        try:
            for a in self.cfg.targets():
                members.append({"addr": a,
                                "name": self.cfg.names.get(a, "?"),
                                "connected": self.ble.is_connected(a)})
        except Exception:
            pass
        return {
            "ver": APP_VERSION,
            "cfg": asdict(self.cfg),
            "cfg_path": str(CONFIG_PATH),
            "dirty": bool(self._dirty),
            "found": found,
            "connected": connected,
            "groups": {k: list(v) for k, v in self.cfg.groups.items()},
            "selected_group": self.cfg.selected_group,
            "group_members": members,
            "remembered_text": self._remembered_text(),
            "startup_on": bool(startup_enabled() or task_enabled()),
            "task_on": bool(task_enabled()),
            "bt_state": self._bt_state,
            "dyn_lamps": int(getattr(self.dynlight, "lamp_count", 0) or 0),
            "dyn_on": bool(self.cfg.dynlight_enabled),
            "ambi_running": bool(self.ambilight.running),
            "media": (self.media.snapshot() if self.media is not None else {
                "running": False, "mode": "", "name": "", "frames": 0,
                "fps": float(self.cfg.media_fps),
                "brightness": int(self.cfg.media_brightness),
                "loop": bool(self.cfg.media_loop), "color": [0, 0, 0], "error": "",
            }),
            "media_last_file": self.cfg.media_last_file,
            "monitors": mons,
            "mode_names": list(MODES),
            "recent": [list(c) for c in self.cfg.recent_colors],
            "color_presets": [list(c) for c in COLOR_PRESETS],
            "tune_profiles": dict(self.cfg.cal_tune_presets or {}),
            "ambi_custom": dict(self.cfg.ambi_custom_presets or {}),
            "t1": {"h": self.cfg.t1_hour, "m": self.cfg.t1_min,
                   "on": self.cfg.t1_on, "days": list(self.cfg.t1_days)},
            "t2": {"h": self.cfg.t2_hour, "m": self.cfg.t2_min,
                   "on": self.cfg.t2_on, "days": list(self.cfg.t2_days)},
            "w": 50,
        }

    def _remembered_text(self):
        if not self.cfg.last_addresses:
            return "Remembered: none (connect a device to remember it)"
        parts = [f"{self.cfg.names.get(a, '?')} {a}" for a in self.cfg.last_addresses]
        return "Remembered: " + "  ·  ".join(parts)

    def _list_monitors(self):
        """Monitor list is cached for 30 s: creating an MSS instance on every
        state refresh costs ~0.5 ms and allocates a graphics context."""
        now = time.monotonic()
        if self._monitors_cache is not None and (now - self._monitors_t) < 30.0:
            return self._monitors_cache
        items = [[0, "All monitors"]]
        try:
            import mss
            with mss.mss() as sct:
                for i, m in enumerate(sct.monitors[1:], start=1):
                    tag = " (primary)" if i == 1 else ""
                    items.append([i, "Monitor %d%s %dx%d" % (i, tag, m["width"], m["height"])])
        except Exception:
            items.append([1, "Monitor 1 (primary)"])
        self._monitors_cache = items
        self._monitors_t = now
        return items

    # ── background loops ──
    def auto_connect(self):
        async def _do():
            out = []
            for a in self.cfg.last_addresses[:]:
                try:
                    ok = await self.ble.connect(a, self.cfg.names.get(a, ""))
                    if ok:
                        self._ever_connected = True
                        self.cfg.touch_last(a)
                        self.log(f"Connected {a}")
                        out.append(a)
                except Exception as e:
                    self.log(f"Connect {a}: {e}")
                    if looks_bt_off(e):
                        break
            return out

        def _done(fut):
            try:
                fut.result(timeout=120)
            except Exception as e:
                self.log(f"Auto-connect: {e}")
            self._save_conn_state()
            self._push_conn()
            self._maybe_sync_time_startup()
            self._ambi_autostart(6)
        try:
            self.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            self.log(f"Auto-connect: {e}")
        self.log("Auto-connecting remembered devices ...")

    def _save_conn_state(self):
        try:
            self._save_keys({"names": dict(self.cfg.names),
                             "last_addresses": list(self.cfg.last_addresses)})
        except Exception:
            pass

    def _save_keys(self, keys: dict):
        try:
            d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        d.update(keys)
        try:
            CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _push_conn(self):
        try:
            self.push({"t": "conn",
                       "connected": self.ble.connected_addresses(),
                       "names": dict(self.cfg.names),
                       "last": list(self.cfg.last_addresses)})
        except Exception:
            pass

    def _maybe_sync_time_startup(self):
        if not self.cfg.sync_time_on_startup:
            return
        if not self.ble.connected_addresses():
            return
        try:
            now = datetime.now()
            wd = (now.weekday() + 1) % 7
            self._send_pkt(pkt_system_time(now.hour, now.minute, now.second, wd),
                           f"startup clock sync {now.strftime('%H:%M:%S')}")
        except Exception as e:
            self.log(f"Clock sync failed: {e}")

    def watchdog(self):
        while not self._quitting:
            try:
                connected = self.ble.connected_addresses()
                candidates = [a for a in self.cfg.last_addresses if a not in self._manual_disc]
                if self.cfg.auto_connect and not connected and candidates:
                    if self._ever_connected:
                        if self.cfg.auto_reconnect:
                            self.log("Connection lost — reconnecting ...")
                            self.auto_connect()
                    elif self._boot_retries > 0:
                        self._boot_retries -= 1
                        self.log(f"Startup connect retry ({self._boot_retries} left) ...")
                        self.auto_connect()
                self._check_schedules()
            except Exception:
                pass
            try:
                self._bt_refresh()
            except Exception:
                pass
            for _ in range(15):
                if self._quitting:
                    break
                time.sleep(1)

    def _bt_refresh(self):
        try:
            st = bt_service_running()
        except Exception:
            st = None
        self._bt_state = "on" if st else ("off" if st is False else "unknown")
        try:
            self.push({"t": "bt", "state": self._bt_state})
        except Exception:
            pass

    # ── schedules ──
    def _check_schedules(self):
        if not self.cfg.schedules:
            return
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        for idx, s in enumerate(self.cfg.schedules):
            try:
                if not s.get("enabled"):
                    continue
                if s.get("hour") != now.hour or s.get("min") != now.minute:
                    continue
                days = s.get("days", [True] * 7)
                if not days[now.weekday()]:
                    continue
                if self._sched_fired.get(idx) == stamp:
                    continue
                self._sched_fired[idx] = stamp
                self._fire_schedule(s)
            except Exception as e:
                self.log(f"Schedule error: {e}")

    def _fire_schedule(self, s):
        name = s.get("name", "Schedule")
        action = s.get("action", "power_on")
        if not self.ble.connected_addresses():
            self.log(f"Schedule '{name}': skipped (no device).")
            return
        self.log(f"Schedule '{name}' firing: {action}.")
        if action == "power_on":
            self._send_pkt(pkt_power(True), f"sched '{name}' ON")
        elif action == "power_off":
            self._send_pkt(pkt_power(False), f"sched '{name}' OFF")
        elif action == "color":
            r, g, b = self.cfg.last_r, self.cfg.last_g, self.cfg.last_b
            self.cfg.last_sent = "color"
            self._send_pkt(pkt_color_static(r, g, b), f"sched '{name}' color")
        elif action == "mode":
            m = max(0, min(28, int(s.get("mode", 10))))
            self.cfg.last_sent = "mode"
            self.cfg.last_mode = m
            self._send_pkt(pkt_mode(m), f"sched '{name}' mode {m}")

    # ── Studio: continuous RGB playback ──
    def _media_send(self, r: int, g: int, b: int):
        try:
            c = self.cfg
            cr, cg, cb = c.cal_apply(int(r), int(g), int(b))
        except Exception:
            cr, cg, cb = int(r), int(g), int(b)
        try:
            targets = self._targets()
        except Exception:
            targets = []
        try:
            from dynlight import VIRTUAL_ADDR
        except Exception:
            VIRTUAL_ADDR = "dynlight://pc"
        ble = [a for a in targets if a != VIRTUAL_ADDR and self.ble.is_connected(a)]
        if ble:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.ble.write_many(ble, pkt_color_realtime(cr, cg, cb), response=False),
                    self.runner.loop)
            except Exception:
                pass
        if VIRTUAL_ADDR in targets:
            try:
                if self.dynlight is not None:
                    self.dynlight.push(cr, cg, cb)
            except Exception:
                pass

    def media_start(self, mode: str, path: str = "", fps: float = 30.0,
                    brightness: int = 100, loop: bool = True, color=None):
        if self.ambilight.running:
            self.ambi_stop()
        try:
            base = tuple(int(x) for x in (color or [self.cfg.last_r, self.cfg.last_g, self.cfg.last_b])[:3])
        except Exception:
            base = (255, 255, 255)
        mp = media_player.MediaPlayer(self._media_send, self._targets, base_color=base)
        if not mp.start(mode, path, fps, brightness, loop, base):
            return {"ok": False, "error": mp.error or "Could not start playback.",
                    "state": self.get_state()}
        if self.media is not None:
            self.media.stop()
        self.media = mp
        c = self.cfg
        c.media_fps = float(mp.fps)
        c.media_brightness = int(mp.brightness)
        c.media_loop = bool(mp.loop)
        if mode == "file":
            c.media_last_file = path
        self._save_keys({
            "media_fps": c.media_fps, "media_brightness": c.media_brightness,
            "media_loop": c.media_loop, "media_last_file": c.media_last_file,
        })
        self.log(f"Studio: playing {mp.name} at {mp.fps:.0f}fps.")
        try:
            self.push({"t": "media_state", "media": mp.snapshot()})
        except Exception:
            pass
        return {"ok": True, "state": self.get_state(), "media": mp.snapshot()}

    def media_stop(self):
        if self.media is not None:
            self.media.stop()
            self.log("Studio: playback stopped.")
        snap = self.media.snapshot() if self.media else None
        self.media = None
        try:
            self.push({"t": "media_state",
                       "media": snap or {"running": False}})
        except Exception:
            pass
        return self.get_state()

    def media_tick_loop(self):
        while not self._quitting:
            try:
                if self.media is not None:
                    snap = self.media.snapshot()
                    self.push({"t": "media_tick", "media": snap})
                    if not snap.get("running"):
                        self.log(f"Studio: finished {snap.get('name') or 'playback'}.")
                        self.media = None
                        self.push({"t": "media_state", "media": snap})
            except Exception:
                pass
            for _ in range(5):
                if self._quitting:
                    break
                time.sleep(0.1)

    # ── ambilight lifecycle ──
    def apply_ambi_preset(self, name: str) -> bool:
        """Apply a built-in or custom Ambilight preset to config + engine."""
        from config import Config as _Cfg
        p = AMBI_BUILTINS.get(name)
        if p is None:
            p = (self.cfg.ambi_custom_presets or {}).get(name)
        if not p:
            return False
        has_xf = "crossfade" in p
        has_dx = "dxcam" in p
        has_wf = "white_floor" in p
        p = _Cfg._clean_ambi_preset(dict(p))
        c = self.cfg
        e = self.ambilight
        e.fps = c.ambi_fps = p["fps"]
        e.smooth = c.ambi_smooth = p["smooth"]
        e.brightness = c.ambi_brightness = p["brightness"]
        e.min_delta = c.ambi_min_delta = p["min_delta"]
        e.update_interval = c.ambi_interval = p["interval"]
        e.capture_mode = c.ambi_mode = p["mode"]
        e.sample_mode = c.ambi_sample = p["sample"]
        if has_wf:
            c.ambi_white_floor = p["white_floor"]
            e.white_floor = float(p["white_floor"])
        if has_xf:
            e.crossfade = c.ambi_crossfade = p["crossfade"]
        if has_dx:
            want = p["dxcam"]
            if want:
                try:
                    import dxcam  # noqa
                except Exception:
                    want = False
                    self.log("Preset: dxcam not installed — GPU capture stays OFF.")
            e.use_dxcam = c.ambi_use_dxcam = want
        return True

    # ── per-app launch rules ──
    def _rule_exe(self, rule: dict) -> str:
        exe = str(rule.get("exe", "")).strip().lower()
        if exe and not exe.endswith(".exe"):
            exe += ".exe"
        return exe

    def _check_app_rules(self):
        rules = self.cfg.app_rules or []
        if not rules:
            self._rule_prev = set()
            return
        try:
            running = app_rules.running_exes()
        except Exception:
            return
        active = set()
        for i, r in enumerate(rules):
            if not r.get("enabled"):
                continue
            exe = self._rule_exe(r)
            if exe and exe in running:
                active.add(i)
        prev = self._rule_prev
        for i in active - prev:
            self._fire_app_rule(i, rules[i])
        for i in prev - active:
            self._end_app_rule(i, rules[i], active)
        self._rule_prev = active

    def _fire_app_rule(self, idx: int, rule: dict):
        exe = self._rule_exe(rule)
        if self.ambilight.running:
            self.log(f"Rule: {exe} is running — Ambilight already on.")
            return
        name = str(rule.get("preset") or "").strip()
        if name:
            if self.apply_ambi_preset(name):
                self.log(f"Rule: applied preset '{name}' for {exe}.")
            else:
                self.log(f"Rule: preset '{name}' not found for {exe}.")
        self.log(f"Rule: {exe} started — turning Ambilight on.")
        self.ambi_start(owner=exe)

    def _end_app_rule(self, idx: int, rule: dict, active_now: set):
        if not rule.get("auto_stop"):
            return
        exe = self._rule_exe(rule)
        if self._ambi_rule_owner != exe:
            return
        if active_now:  # another rule app is still running
            return
        if self.ambilight.running:
            self.log(f"Rule: {exe} closed — turning Ambilight off.")
            self.ambi_stop()

    def app_rule_loop(self):
        while not self._quitting:
            try:
                self._check_app_rules()
            except Exception:
                pass
            for _ in range(20):
                if self._quitting:
                    break
                time.sleep(0.1)

    def _set_window_visible(self, visible: bool) -> bool:
        """Show/hide the app window without touching WebView from a worker."""
        try:
            w = self.window
            if w is not None:
                if visible:
                    w.show()
                else:
                    w.hide()
                return True
        except Exception:
            pass
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(
                None, f"{APP_NAME} v{APP_VERSION}")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5 if visible else 0)
                if visible:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                return True
        except Exception:
            pass
        return False

    def ambi_start(self, owner=None) -> bool:
        if self.media is not None and self.media.running:
            self.media_stop()
        try:
            import mss  # noqa
            from PIL import Image  # noqa
        except ImportError:
            self.log("Missing deps: pip install mss pillow")
            return False
        if not self._targets():
            self.log("No device connected.")
            return False
        # Hide our own window first: otherwise the capture region is the app
        # UI and the strip mirrors its own colours.
        if self.cfg.ambi_hide_window and not self._hidden_for_sync:
            self._hidden_for_sync = True
            if not self._set_window_visible(False):
                self._hidden_for_sync = False
                self.log("Could not hide the window — Ambilight may mirror its own UI.")
            time.sleep(0.2)  # let the compositor remove the window
            try:
                if self._tray is not None:
                    self._tray.notify(
                        "Ambilight is running. Restore the window from this tray icon.",
                        APP_NAME)
            except Exception:
                pass
        try:
            fut = self.runner.submit(self._ambi_begin_async())
            fut.result(timeout=10)
        except Exception as e:
            self.log(f"Ambilight failed to start: {e}")
            return False
        self.log(f"Ambilight started @ {self.ambilight.fps:.0f}fps "
                 f"({self.ambilight.capture_mode}/{self.ambilight.sample_mode})")
        self._ambi_rule_owner = owner
        try:
            self.push({"t": "ambi_state", "running": True})
        except Exception:
            pass
        return True

    async def _ambi_begin_async(self):
        self.ambilight.start()

    def ambi_stop(self):
        try:
            self.runner.submit(self.ambilight.stop())
        except Exception:
            pass
        self.log("Ambilight stopped.")
        self._ambi_rule_owner = None
        if self._hidden_for_sync:
            self._hidden_for_sync = False
            self._set_window_visible(True)
        try:
            self.push({"t": "ambi_state", "running": False})
        except Exception:
            pass

    def ambi_tick_loop(self):
        while not self._quitting:
            try:
                if self.ambilight.running:
                    r, g, b = self.ambilight.last_color
                    self.push({"t": "ambi_tick",
                               "color": [int(r), int(g), int(b)],
                               "stats": {k: [int(x) for x in v]
                                         for k, v in (self.ambilight.last_stats or {}).items()},
                               "frames": int(self.ambilight.frames),
                               "sends": int(self.ambilight.sends),
                               "cap": round(float(self.ambilight.last_capture_ms), 1),
                               "eco": bool(self.ambilight.eco)})
            except Exception:
                pass
            for _ in range(10):
                if self._quitting:
                    break
                time.sleep(0.1)

    def _ambi_autostart(self, tries: int = 6):
        def _run(n):
            if self._quitting:
                return
            try:
                if not self.cfg.ambi_on_startup or self.ambilight.running:
                    return
                if not self._targets():
                    if n > 0:
                        threading.Timer(20.0, _run, args=(n - 1,)).start()
                    else:
                        self.log("Ambilight autostart: no device, giving up.")
                    return
                self.ambi_start()
            except Exception as e:
                self.log(f"Ambilight autostart: {e}")
        threading.Timer(6.0, _run, args=(tries,)).start()

    # ── tray ──
    def ensure_tray(self):
        try:
            import pystray
            from PIL import Image
            p = self._icon_path()
            img = Image.open(p).resize((64, 64)) if p else None
            if img is None:
                raise RuntimeError("no icon")
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("Show", self._tray_show, default=True),
                pystray.MenuItem("Stop Ambilight", self._tray_stop_ambi),
                pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = pystray.Icon("Elf-Ambilight", img,
                                      f"{APP_NAME} v{APP_VERSION}", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception as e:
            self.log(f"Tray unavailable: {e}")

    @staticmethod
    def _icon_path():
        cands = []
        try:
            cands.append(Path(__file__).with_name("icon.ico"))
        except Exception:
            pass
        try:
            cands.append(Path(sys.executable).with_name("icon.ico"))
            cands.append(Path(sys.executable).parent / "icon.ico")
        except Exception:
            pass
        try:
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                cands.append(Path(sys._MEIPASS) / "icon.ico")  # type: ignore[attr-defined]
        except Exception:
            pass
        for p in cands:
            try:
                if p.is_file():
                    return str(p)
            except Exception:
                continue
        return None

    def _tray_show(self, *a):
        try:
            w = self.window
            if w is not None:
                w.show()
        except Exception:
            pass
        if self._hidden_for_sync:
            self._hidden_for_sync = False
            self.log("Window restored — Ambilight now includes the app window in its capture.")

    def _tray_quit(self, *a):
        self.begin_quit(force=True)

    def _tray_stop_ambi(self, *a):
        try:
            self.ambi_stop()
        except Exception:
            pass

    def begin_quit(self, force=False):
        self._quitting = True
        try:
            self.ambilight._running = False
            if self.ambilight._task is not None:
                try:
                    self.ambilight._task.cancel()
                except Exception:
                    pass
            try:
                cam = getattr(self.ambilight._tls, "dxcam", None)
                if cam is not None:
                    cam.stop()
            except Exception:
                pass
        except Exception:
            pass
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        try:
            self.runner.loop.call_soon_threadsafe(self.runner.loop.stop)
        except Exception:
            pass
        try:
            import os
            os._exit(0)
        except Exception:
            pass


def looks_bt_off(e: Exception) -> bool:
    try:
        from bt_helper import looks_like_bt_off
        return bool(looks_like_bt_off(e))
    except Exception:
        return False


_BACKEND = None


def _B():
    """Module-level backend ref (kept OFF the Api instance:
    pywebview recursively enumerates js_api attributes, and an embedded
    Backend/Window graph explodes bridge generation)."""
    assert _BACKEND is not None
    return _BACKEND


class Api:
    """pywebview js_api — every method returns JSON-safe values only."""

    def __init__(self):
        pass

    # ── state ──
    def get_state(self):
        return _B().get_state()

    def ui_booted(self, info):
        """Health report from the frontend after first render (diagnostics)."""
        try:
            _B()._ui_booted = dict(info or {})
            _B().log("WebUI rendered: %s" % ", ".join(
                f"{k}={v}" for k, v in (info or {}).items()))
        except Exception:
            pass
        return True

    def poll(self):
        """Drain queued backend events (frontend polls every 500ms)."""
        try:
            q = _B()._events
            out = list(q)
            q.clear()
            return out
        except Exception:
            return []

    def save(self):
        try:
            _B().cfg.save()
            _B()._dirty.clear()
            _B().log("Settings saved to config.json.")
        except Exception as e:
            _B().log(f"Save failed: {e}")
        return _B().get_state()

    def discard(self):
        b = _B()
        dirty = set(b._dirty)
        try:
            b._dirty.clear()
            b.cfg = Config.load()
            b.ambilight.calibrate = b.cfg.cal_apply
            try:
                b.ambilight.white_floor = float(b.cfg.ambi_white_floor)
            except Exception:
                pass
        except Exception as e:
            b.log(f"Revert failed: {e}")
        # Put the saved light state back on the strip — reloading the UI alone
        # would leave the hardware showing the discarded colour.
        if dirty & {"color", "tune", "modes"}:
            try:
                c = b.cfg
                b._send_pkt(pkt_brightness(c.last_brightness),
                            f"revert brightness {c.last_brightness}")
                if c.last_sent == "mode" or (
                        "modes" in dirty and not (dirty & {"color", "tune"})):
                    b._send_pkt(pkt_mode(c.last_mode),
                                f"revert mode {c.last_mode} {MODES[c.last_mode]}")
                    b._send_pkt(pkt_mode_speed(c.last_speed),
                                f"revert speed {c.last_speed}")
                else:
                    b._send_pkt(pkt_color_static(c.last_r, c.last_g, c.last_b),
                                f"revert RGB({c.last_r},{c.last_g},{c.last_b})")
            except Exception as e:
                b.log(f"Revert send failed: {e}")
        b.log("Discarded — reverted to last save.")
        return b.get_state()

    def quit(self):
        if _B()._dirty:
            return {"dirty": True}
        _B().begin_quit(force=True)
        return {"dirty": False}

    def quit_force(self):
        _B().begin_quit(force=True)
        return True

    # ── devices ──
    def scan_start(self, timeout):
        b = _B()
        b.log(f"Scanning {timeout}s ...")

        async def _do():
            return await b.ble.scan(timeout=float(timeout or 5))

        def _done(fut):
            try:
                devs = fut.result(timeout=120)
                for d in devs:
                    b.found[d.address] = d
                    b.cfg.remember_name(d.address, d.name)
                b._save_conn_state()
                b.log(f"Found {len(devs)} device(s).")
                b.push({"t": "found", "devices": [
                    {"addr": d.address, "name": d.name, "rssi": d.rssi,
                     "connected": b.ble.is_connected(d.address)} for d in devs]})
                if float(timeout or 5) <= 3.0:
                    # quick scan also auto-connects remembered hits
                    async def _qc():
                        for a in b.cfg.last_addresses[:]:
                            if b.ble.is_connected(a) or a in b._manual_disc:
                                continue
                            if a in [d.address for d in devs]:
                                try:
                                    await b.ble.connect(a, b.cfg.names.get(a, ""))
                                    b._ever_connected = True
                                    b.log(f"Connected {a}")
                                except Exception:
                                    pass
                    try:
                        b.runner.submit(_qc()).add_done_callback(
                            lambda f: b._push_conn())
                    except Exception:
                        pass
            except Exception as e:
                b.log(f"Scan failed: {e}")
        try:
            b.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            b.log(f"Scan failed: {e}")
        return True

    def connect_all(self):
        b = _B()
        if not b.found:
            b.log("No devices found. Scan first.")
            return b.get_state()

        async def _do():
            for d in list(b.found.values()):
                if not b.ble.is_connected(d.address):
                    try:
                        await b.ble.connect(d.address, d.name)
                        b._manual_disc.discard(d.address)
                        b.cfg.touch_last(d.address)
                        b.cfg.remember_name(d.address, d.name)
                        b._ever_connected = True
                        b.log(f"Connected {d.name} {d.address}")
                    except Exception as e:
                        b.log(f"Connect {d.address}: {e}")
            b._save_conn_state()

        def _done(fut):
            try:
                fut.result(timeout=180)
            except Exception as e:
                b.log(f"Connect: {e}")
            b._push_conn()
            b._maybe_sync_time_startup()
            b._ambi_autostart(6)
        try:
            b.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            b.log(f"Connect: {e}")
        return True

    def connect_one(self, addr):
        b = _B()

        async def _do():
            try:
                nm = b.cfg.names.get(addr, "")
                if not nm and addr in b.found:
                    nm = b.found[addr].name
                await b.ble.connect(addr, nm)
                b._manual_disc.discard(addr)
                b.cfg.touch_last(addr)
                if nm:
                    b.cfg.remember_name(addr, nm)
                b._ever_connected = True
                b.log(f"Connected {addr}")
            except Exception as e:
                b.log(f"Connect {addr}: {e}")
            b._save_conn_state()

        def _done(fut):
            b._push_conn()
        try:
            b.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            b.log(f"Connect: {e}")
        return True

    def disconnect_all(self):
        b = _B()
        addrs = b.ble.connected_addresses()
        b._manual_disc.update(addrs)
        b._boot_retries = 0

        async def _do():
            for a in addrs:
                try:
                    await b.ble.disconnect(a)
                    b.log(f"Disconnected {a}")
                except Exception:
                    pass

        def _done(fut):
            b._push_conn()
        try:
            b.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            b.log(f"Disconnect: {e}")
        return True

    def disconnect_one(self, addr):
        b = _B()
        b._manual_disc.add(addr)
        b._boot_retries = 0

        async def _do():
            try:
                await b.ble.disconnect(addr)
                b.log(f"Disconnected {addr}")
            except Exception:
                pass

        def _done(fut):
            b._push_conn()
        try:
            b.runner.submit(_do()).add_done_callback(_done)
        except Exception as e:
            b.log(f"Disconnect: {e}")
        return True

    def forget_remembered(self):
        b = _B()
        b.cfg.last_addresses.clear()
        b._save_keys({"last_addresses": []})
        b.log("Forgot remembered devices.")
        return b.get_state()

    # ── groups ──
    def _groups_changed(self, msg):
        b = _B()
        b._save_keys({"groups": {k: list(v) for k, v in b.cfg.groups.items()},
                      "selected_group": b.cfg.selected_group,
                      "names": dict(b.cfg.names),
                      "last_addresses": list(b.cfg.last_addresses)})
        b._mark_dirty("devices")
        b.log(msg)
        return b.get_state()

    def group_add_many(self, addrs):
        b = _B()
        g = b.cfg.selected_group
        grp = b.cfg.groups.setdefault(g, [])
        n = 0
        for addr in addrs or []:
            if addr in grp:
                continue
            grp.append(addr)
            name = b.cfg.names.get(addr, "")
            if not name and addr in b.found:
                name = b.found[addr].name
                b.cfg.remember_name(addr, name)
            n += 1
        return self._groups_changed(f"Added {n} to '{g}'." if n else f"Already in '{g}'.")

    def group_remove(self, addr):
        b = _B()
        g = b.cfg.selected_group
        grp = b.cfg.groups.get(g, [])
        if addr in grp:
            grp.remove(addr)
        return self._groups_changed(f"Removed {addr} from '{g}'.")

    def group_select(self, name):
        b = _B()
        if name in b.cfg.groups:
            b.cfg.selected_group = name
        return self._groups_changed(f"Group '{b.cfg.selected_group}' selected.")

    def group_new(self, name):
        b = _B()
        name = (name or "").strip()[:24] or f"Group {len(b.cfg.groups) + 1}"
        if name not in b.cfg.groups:
            b.cfg.groups[name] = []
        b.cfg.selected_group = name
        return self._groups_changed(f"Group '{name}' created.")

    def group_rename(self, new):
        b = _B()
        old = b.cfg.selected_group
        new = (new or "").strip()[:24]
        if not new or new == old:
            return b.get_state()
        if new in b.cfg.groups:
            b.log(f"'{new}' already exists.")
            return b.get_state()
        b.cfg.groups[new] = b.cfg.groups.pop(old)
        b.cfg.selected_group = new
        return self._groups_changed(f"Renamed '{old}' to '{new}'.")

    def group_delete(self):
        b = _B()
        old = b.cfg.selected_group
        if len(b.cfg.groups) <= 1:
            b.log("Cannot delete the last group.")
            return b.get_state()
        b.cfg.groups.pop(old, None)
        b.cfg.selected_group = next(iter(b.cfg.groups))
        return self._groups_changed(f"Deleted group '{old}'.")

    # ── basic sends ──
    def power(self, on):
        _B()._send_pkt(pkt_power(bool(on)), f"power {'ON' if on else 'OFF'}")
        return True

    def set_color(self, r, g, b, record=True):
        c = _B().cfg
        c.last_r, c.last_g, c.last_b = int(r), int(g), int(b)
        c.last_sent = "color"
        if record:
            c.add_recent(int(r), int(g), int(b))
        _B()._mark_dirty("color")
        _B()._send_pkt(pkt_color_static(int(r), int(g), int(b)),
                         f"static RGB({int(r)},{int(g)},{int(b)})")
        return _B().get_state()

    def set_brightness(self, v, ch="ALL"):
        c = _B().cfg
        v = max(0, min(100, int(v)))
        c.last_brightness = v
        _B()._mark_dirty("color")
        code = BRI_CH.get(str(ch).upper(), 0)
        _B()._send_pkt(pkt_brightness(v, code), f"brightness {v}")
        return _B().get_state()

    def set_w(self, v):
        _B()._mark_dirty("color")
        _B()._send_pkt(pkt_single_color_w(max(0, min(100, int(v)))), "single-W")
        return _B().get_state()

    def set_cct(self, warm, cold):
        _B()._mark_dirty("color")
        _B()._send_pkt(pkt_cct(max(0, min(255, int(warm))), max(0, min(255, int(cold)))), "cct")
        return _B().get_state()

    def set_rgbw(self, rgb, w, cct, ch="ALL"):
        _B()._mark_dirty("color")
        code = BRI_CH.get(str(ch).upper(), 0)
        _B()._send_pkt(pkt_rgbw(bool(rgb), bool(w), bool(cct), code), "rgbw-state")
        return True

    def set_pins(self, p1, p2, p3):
        _B()._mark_dirty("color")
        try:
            a = max(1, min(6, int(p1)))
            bb = max(1, min(6, int(p2)))
            cc = max(1, min(6, int(p3)))
        except Exception:
            a, bb, cc = 1, 2, 3
        _B()._send_pkt(pkt_pin_sequence(a, bb, cc), "pins")
        return True

    def set_pref(self, key, val):
        c = _B().cfg
        if hasattr(c, key):
            setattr(c, key, val)
            _B()._mark_dirty("settings")
            if key == "live_send":
                _B().log(f"Live send {'ON' if val else 'OFF'}.")
        return _B().get_state()

    def set_theme(self, name):
        """Persist the UI theme immediately (not a staged setting)."""
        name = str(name).lower()
        if name not in ("light", "dark", "system"):
            name = "light"
        _B().cfg.ui_theme = name
        _B()._save_keys({"ui_theme": name})
        return {"theme": name}

    # ── tune ──
    def tune_set(self, kind, payload):
        c = _B().cfg
        try:
            if kind == "gains":
                c.cal_enabled = bool(payload.get("enabled", c.cal_enabled))
                c.cal_gain_r = max(0.2, min(2.0, float(payload.get("r", c.cal_gain_r))))
                c.cal_gain_g = max(0.2, min(2.0, float(payload.get("g", c.cal_gain_g))))
                c.cal_gain_b = max(0.2, min(2.0, float(payload.get("b", c.cal_gain_b))))
                c.cal_gamma = max(0.3, min(3.0, float(payload.get("gamma", c.cal_gamma))))
                c.cal_gamma_r = max(0.3, min(3.0, float(payload.get("gamma_r", c.cal_gamma_r))))
                c.cal_gamma_g = max(0.3, min(3.0, float(payload.get("gamma_g", c.cal_gamma_g))))
                c.cal_gamma_b = max(0.3, min(3.0, float(payload.get("gamma_b", c.cal_gamma_b))))
                c.cal_temp = max(-100, min(100, int(payload.get("temp", c.cal_temp))))
                c.cal_order = str(payload.get("order", c.cal_order))
                c.cal_sat = max(0.0, min(2.0, float(payload.get("sat", c.cal_sat))))
                c.cal_limit = max(0.1, min(1.0, float(payload.get("limit", c.cal_limit))))
            elif kind == "prim":
                attr = str(payload.get("attr", ""))
                vals = [max(0, min(255, int(x))) for x in list(payload.get("vals", []))[:3]]
                if attr in ("prim_r", "prim_g", "prim_b", "white_pt") and len(vals) == 3:
                    setattr(c, attr, vals)
        except Exception:
            pass
        _B()._mark_dirty("tune")
        if c.live_send and kind in ("gains", "prim"):
            r, g, b = c.last_r, c.last_g, c.last_b
            _B()._send_pkt(pkt_color_static(r, g, b), f"tune preview RGB({r},{g},{b})")
        return _B().get_state()

    def tune_reset(self):
        c = _B().cfg
        c.cal_enabled = True
        c.cal_gain_r = c.cal_gain_g = c.cal_gain_b = 1.0
        c.cal_gamma = 1.0
        c.cal_gamma_r = c.cal_gamma_g = c.cal_gamma_b = 1.0
        c.cal_temp = 0
        c.cal_order = "RGB"
        c.cal_sat = 1.0
        c.cal_limit = 1.0
        _B()._mark_dirty("tune")
        _B().log("Calibration reset (unsaved — press Save to keep).")
        return _B().get_state()

    def cal_preview(self, r, g, b):
        try:
            cr, cg, cb = _B().cfg.cal_apply(int(r), int(g), int(b), apply_order=False)
            return {"req": [int(r), int(g), int(b)], "out": [cr, cg, cb]}
        except Exception:
            return None

    def wb_show(self):
        _B()._send_pkt(pkt_color_static(255, 255, 255), "white balance ref")
        return True

    def favorite_add(self, r, g, b):
        c = _B().cfg
        col = [max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))]
        favs = [f for f in (c.favorites or []) if f != col]
        favs.insert(0, col)
        c.favorites = favs[:12]
        _B()._save_keys({"favorites": c.favorites})
        _B().log(f"Colour saved to favourites: {tuple(col)}")
        return _B().get_state()

    def favorite_remove(self, r, g, b):
        c = _B().cfg
        col = [int(r), int(g), int(b)]
        c.favorites = [f for f in (c.favorites or []) if f != col]
        _B()._save_keys({"favorites": c.favorites})
        return _B().get_state()

    def capture_screen_color(self):
        """One-shot screen colour sample (centre 50%) for the match-screen tool."""
        b = _B()
        try:
            if b.ambilight.running:
                return {"rgb": list(b.ambilight.last_color), "live": True}
        except Exception:
            pass
        try:
            import mss
            from PIL import Image, ImageStat
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                w, h = mon["width"], mon["height"]
                box = {"left": mon["left"] + w // 4, "top": mon["top"] + h // 4,
                       "width": max(1, w // 2), "height": max(1, h // 2)}
                shot = sct.grab(box)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                img = img.resize((57, 32))
                m = ImageStat.Stat(img).mean
                return {"rgb": [int(m[0]), int(m[1]), int(m[2])], "live": False}
        except Exception as e:
            b.log(f"Screen match failed: {e}")
            return {"rgb": None, "live": False}

    def wb_nudge(self, kind):
        c = _B().cfg
        w = list(c.white_pt)
        if kind == "dim":
            w = [min(255, x + 12) for x in w]
            msg = "brightened"
        else:
            idx = {"red": 0, "green": 1, "blue": 2}.get(str(kind), 2)
            w[idx] = max(30, w[idx] - 15)
            msg = f"less {kind}"
        c.white_pt = w
        _B()._mark_dirty("tune")
        _B()._send_pkt(pkt_color_static(255, 255, 255), "white balance ref")
        _B().log(f"White balance {msg} -> {tuple(w)} (unsaved — press Save).")
        return _B().get_state()

    def _tune_snapshot(self):
        c = _B().cfg
        return {"gain_r": float(c.cal_gain_r), "gain_g": float(c.cal_gain_g),
                "gain_b": float(c.cal_gain_b), "gamma": float(c.cal_gamma),
                "gamma_r": float(c.cal_gamma_r), "gamma_g": float(c.cal_gamma_g),
                "gamma_b": float(c.cal_gamma_b),
                "temp": int(c.cal_temp), "order": str(c.cal_order),
                "sat": float(c.cal_sat), "limit": float(c.cal_limit),
                "prim_r": list(c.prim_r), "prim_g": list(c.prim_g),
                "prim_b": list(c.prim_b), "white_pt": list(c.white_pt),
                "enabled": bool(c.cal_enabled)}

    def tune_prof_apply(self, name):
        from config import Config as _Cfg
        p = _Cfg._clean_tune_profile(dict((_B().cfg.cal_tune_presets or {}).get(name, {})))
        c = _B().cfg
        c.cal_enabled = p["enabled"]
        c.cal_gain_r, c.cal_gain_g, c.cal_gain_b = p["gain_r"], p["gain_g"], p["gain_b"]
        c.cal_gamma, c.cal_temp, c.cal_order = p["gamma"], p["temp"], p["order"]
        c.cal_gamma_r = p.get("gamma_r", 1.0)
        c.cal_gamma_g = p.get("gamma_g", 1.0)
        c.cal_gamma_b = p.get("gamma_b", 1.0)
        c.cal_sat, c.cal_limit = p["sat"], p["limit"]
        c.prim_r, c.prim_g, c.prim_b = list(p["prim_r"]), list(p["prim_g"]), list(p["prim_b"])
        c.white_pt = list(p["white_pt"])
        _B()._mark_dirty("tune")
        _B().log(f"Tune profile '{name}' staged (unsaved).")
        return _B().get_state()

    def tune_prof_save(self, name):
        name = (name or "").strip()[:24]
        if not name:
            return _B().get_state()
        _B().cfg.cal_tune_presets[name] = self._tune_snapshot()
        _B()._save_keys({"cal_tune_presets": _B().cfg.cal_tune_presets})
        _B().log(f"Tune profile '{name}' saved.")
        return _B().get_state()

    def tune_prof_rename(self, old, new):
        new = (new or "").strip()[:24]
        d = _B().cfg.cal_tune_presets or {}
        if old in d and new and new not in d:
            d[new] = d.pop(old)
            _B()._save_keys({"cal_tune_presets": d})
            _B().log(f"Tune profile '{old}' renamed to '{new}'.")
        return _B().get_state()

    def tune_prof_delete(self, name):
        (_B().cfg.cal_tune_presets or {}).pop(name, None)
        _B()._save_keys({"cal_tune_presets": _B().cfg.cal_tune_presets})
        _B().log(f"Tune profile '{name}' deleted.")
        return _B().get_state()

    # ── modes ──
    def send_mode(self, idx):
        c = _B().cfg
        c.last_mode = max(0, min(28, int(idx)))
        c.last_sent = "mode"
        _B()._mark_dirty("modes")
        _B()._send_pkt(pkt_mode(c.last_mode), f"mode {c.last_mode} {MODES[c.last_mode]}")
        return _B().get_state()

    def send_speed(self, v):
        c = _B().cfg
        c.last_speed = max(0, min(100, int(v)))
        _B()._mark_dirty("modes")
        _B()._send_pkt(pkt_mode_speed(c.last_speed), f"speed {c.last_speed}")
        return _B().get_state()

    # ── timing ──
    def send_timer(self, slot, h, m, on, days):
        c = _B().cfg
        h = max(0, min(23, int(h)))
        m = max(0, min(59, int(m)))
        days = [bool(d) for d in list(days)[:7]]
        while len(days) < 7:
            days.append(True)
        if int(slot) == 0:
            c.t1_hour, c.t1_min, c.t1_on, c.t1_days = h, m, bool(on), days
        else:
            c.t2_hour, c.t2_min, c.t2_on, c.t2_days = h, m, bool(on), days
        w = 0
        for i, d in enumerate(days):
            if d:
                w |= (1 << i)
        _B()._mark_dirty("timing")
        _B()._send_pkt(pkt_timing(h, m, 0, w, bool(on), int(slot)),
                         f"timer{int(slot)+1} {h:02d}:{m:02d} {'on' if on else 'off'}")
        return _B().get_state()

    def sync_time(self):
        now = datetime.now()
        wd = (now.weekday() + 1) % 7
        _B()._send_pkt(pkt_system_time(now.hour, now.minute, now.second, wd),
                         f"time {now.strftime('%H:%M:%S')}")
        return True

    def sched_add(self):
        _B().cfg.schedules.append(Config.blank_schedule(len(_B().cfg.schedules) + 1))
        _B()._mark_dirty("timing")
        return _B().get_state()

    def sched_del(self, idx):
        try:
            del _B().cfg.schedules[int(idx)]
        except Exception:
            pass
        _B()._mark_dirty("timing")
        return _B().get_state()

    def sched_set(self, idx, key, val):
        try:
            s = _B().cfg.schedules[int(idx)]
            if key in ("name", "action", "mode", "enabled"):
                s[key] = val
                _B().cfg.schedules[int(idx)] = Config._clean_schedule(s)
                _B()._mark_dirty("timing")
        except Exception:
            pass
        return _B().get_state()

    def sched_time(self, idx, h, m):
        try:
            s = _B().cfg.schedules[int(idx)]
            s["hour"] = max(0, min(23, int(h)))
            s["min"] = max(0, min(59, int(m)))
            _B()._mark_dirty("timing")
        except Exception:
            pass
        return _B().get_state()

    def sched_day(self, idx, day, val):
        try:
            s = _B().cfg.schedules[int(idx)]
            days = list(s.get("days", [True] * 7))
            days[int(day)] = bool(val)
            s["days"] = days
            _B()._mark_dirty("timing")
        except Exception:
            pass
        return _B().get_state()

    # ── ambilight ──
    def ambi_start(self):
        _B().ambi_start()
        return _B().get_state()

    def ambi_stop(self):
        _B().ambi_stop()
        return _B().get_state()

    def ambi_auto(self, on):
        _B().cfg.ambi_on_startup = bool(on)
        _B()._save_keys({"ambi_on_startup": _B().cfg.ambi_on_startup})
        _B().log("Ambilight at startup " + ("ON." if on else "OFF."))
        return _B().get_state()

    def set_ambi_hide(self, on):
        _B().cfg.ambi_hide_window = bool(on)
        _B()._save_keys({"ambi_hide_window": bool(on)})
        _B().log("Hide window while syncing " + ("ON." if on else "OFF."))
        return _B().get_state()

    # ── Studio playback ──
    def media_pick(self):
        try:
            import webview
            w = _B().window
            if w is None:
                return ""
            res = w.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=(
                    "Media (*.gif;*.webp;*.apng;*.png;*.jpg;*.jpeg;*.bmp;*.mp4;*.mkv;*.avi;*.mov;*.webm;*.m4v)",
                    "Animations (*.gif;*.webp;*.apng)",
                    "Video (*.mp4;*.mkv;*.avi;*.mov;*.webm;*.m4v)",
                    "All files (*.*)",
                ))
            if res:
                return res[0] if isinstance(res, (list, tuple)) else str(res)
        except Exception as e:
            _B().log(f"File picker failed: {e}")
        return ""

    def media_start(self, mode, path="", fps=30, brightness=100, loop=True, color=None):
        res = _B().media_start(mode, path, fps, brightness, loop, color)
        return res

    def media_stop(self):
        return _B().media_stop()

    # ── per-app Ambilight rules ──
    def list_apps(self):
        try:
            return app_rules.visible_window_exes()
        except Exception:
            return []

    def app_rule_add(self, exe, preset="", auto_stop=True):
        b = _B()
        exe = str(exe or "").strip().lower().replace("\\", "/").split("/")[-1]
        if exe and not exe.endswith(".exe"):
            exe += ".exe"
        if not exe:
            return b.get_state()
        if any(r.get("exe") == exe for r in (b.cfg.app_rules or [])):
            b.log(f"Rule for {exe} already exists.")
            return b.get_state()
        b.cfg.app_rules.append({
            "exe": exe[:80], "preset": str(preset or "")[:40],
            "auto_stop": bool(auto_stop), "enabled": True,
        })
        b._save_keys({"app_rules": b.cfg.app_rules})
        b._rule_prev = set()
        b.log(f"Rule added: start Ambilight when {exe} runs"
              + (f" (preset '{preset}')." if preset else " (current settings)."))
        return b.get_state()

    def app_rule_update(self, idx, patch):
        b = _B()
        try:
            r = b.cfg.app_rules[int(idx)]
            if "preset" in patch:
                r["preset"] = str(patch["preset"] or "")[:40]
            if "auto_stop" in patch:
                r["auto_stop"] = bool(patch["auto_stop"])
            if "enabled" in patch:
                r["enabled"] = bool(patch["enabled"])
            b._save_keys({"app_rules": b.cfg.app_rules})
        except Exception:
            pass
        return b.get_state()

    def app_rule_delete(self, idx):
        b = _B()
        try:
            removed = b.cfg.app_rules.pop(int(idx))
            b._save_keys({"app_rules": b.cfg.app_rules})
            b._rule_prev = set()
            b.log(f"Rule removed: {removed.get('exe', '?')}.")
        except Exception:
            pass
        return b.get_state()

    def ambi_set(self, p):
        c = _B().cfg
        e = _B().ambilight
        try:
            e.fps = c.ambi_fps = max(1.0, min(60.0, float(p.get("fps", c.ambi_fps))))
            e.smooth = c.ambi_smooth = max(0.0, min(10.0, float(p.get("smooth", c.ambi_smooth))))
            e.brightness = c.ambi_brightness = max(1, min(100, int(p.get("brightness", c.ambi_brightness))))
            e.min_delta = c.ambi_min_delta = max(0, min(30, int(p.get("min_delta", c.ambi_min_delta))))
            c.ambi_white_floor = max(0, min(100, int(p.get("white_floor", c.ambi_white_floor))))
            e.white_floor = float(c.ambi_white_floor)
            e.update_interval = c.ambi_interval = max(0.01, min(5.0, float(p.get("interval", c.ambi_interval))))
            mode = str(p.get("mode", c.ambi_mode))
            e.capture_mode = c.ambi_mode = "full" if mode == "full" else "center"
            sample = str(p.get("sample", c.ambi_sample)).lower()
            e.sample_mode = c.ambi_sample = sample if sample in (
                "average", "dominant", "vibrant", "brightest") else "average"
            e.ambi_monitor = c.ambi_monitor = max(0, int(p.get("monitor", c.ambi_monitor)))
            e.crossfade = c.ambi_crossfade = bool(p.get("crossfade", c.ambi_crossfade))
            e.use_dxcam = c.ambi_use_dxcam = bool(p.get("dxcam", c.ambi_use_dxcam))
        except Exception:
            pass
        _B()._mark_dirty("ambi")
        return _B().get_state()

    def ambi_preset(self, name):
        if not _B().apply_ambi_preset(name):
            return _B().get_state()
        _B()._mark_dirty("ambi")
        _B().log(f"Ambilight preset '{name}' staged (unsaved).")
        return _B().get_state()

    def ambi_custom_apply(self, name):
        return self.ambi_preset(name)

    def ambi_custom_save(self, name):
        name = (name or "").strip()[:24]
        if not name:
            return _B().get_state()
        c = _B().cfg
        c.ambi_custom_presets[name] = {
            "fps": int(round(c.ambi_fps)), "smooth": round(c.ambi_smooth, 2),
            "brightness": int(c.ambi_brightness), "min_delta": int(c.ambi_min_delta),
            "interval": round(c.ambi_interval, 3),
            "white_floor": int(c.ambi_white_floor),
            "mode": c.ambi_mode, "sample": c.ambi_sample,
            "crossfade": bool(c.ambi_crossfade), "dxcam": bool(c.ambi_use_dxcam)}
        _B()._save_keys({"ambi_custom_presets": c.ambi_custom_presets})
        _B().log(f"Ambilight preset '{name}' saved.")
        return _B().get_state()

    def ambi_custom_rename(self, old, new):
        new = (new or "").strip()[:24]
        d = _B().cfg.ambi_custom_presets or {}
        if old in d and new and new not in d:
            d[new] = d.pop(old)
            _B()._save_keys({"ambi_custom_presets": d})
            _B().log(f"Ambilight preset '{old}' renamed to '{new}'.")
        return _B().get_state()

    def ambi_custom_delete(self, name):
        (_B().cfg.ambi_custom_presets or {}).pop(name, None)
        _B()._save_keys({"ambi_custom_presets": _B().cfg.ambi_custom_presets})
        _B().log(f"Ambilight preset '{name}' deleted.")
        return _B().get_state()

    # ── setup ──
    def set_startup(self, on):
        on = bool(on)
        b = _B()
        try:
            if on:
                if task_enabled():
                    set_startup(False)
                    b.log("Start with Windows ON (high priority task).")
                else:
                    set_startup(True, minimized=b.cfg.start_minimized)
                    b.log("Start with Windows ON (normal).")
            else:
                set_startup(False)
                if task_enabled():
                    if is_elevated():
                        from startup_helper import set_task
                        set_task(False)
                    else:
                        relaunch_elevated_task(False)
                        b.log("Waiting for admin approval ...")
                        threading.Timer(4.0, lambda: b.push(
                            {"t": "state", "state": b.get_state()})).start()
                b.log("Start with Windows OFF (all auto-start removed).")
        except Exception as e:
            b.log(f"Startup: {e}")
        return b.get_state()

    def set_task(self, on):
        want = bool(on)
        b = _B()
        if not want and not task_enabled():
            pass
        elif want and not (startup_enabled() or task_enabled()):
            b.log("Turn on 'Start app with Windows' first.")
            return b.get_state()
        try:
            if is_elevated():
                enable_high_priority(want, minimized=b.cfg.start_minimized)
                b.log("High priority startup " + (
                    "ON (task only — normal entry removed)."
                    if want else "OFF (normal startup restored)."))
            else:
                relaunch_elevated_task(want)
                b.log("Waiting for admin approval ...")
                def _poll_left(tries=4):
                    if tries <= 0 or b._quitting:
                        try:
                            b.push({"t": "state", "state": b.get_state()})
                        except Exception:
                            pass
                        return
                    try:
                        if task_enabled() == want:
                            try:
                                b.push({"t": "state", "state": b.get_state()})
                            except Exception:
                                pass
                            b.log("High priority startup " + ("ON." if want else "OFF."))
                            return
                    except Exception:
                        pass
                    threading.Timer(2.0, _poll_left, args=(tries - 1,)).start()
                threading.Timer(4.0, _poll_left).start()
        except Exception as e:
            b.log(f"High priority startup: {e}")
        return b.get_state()

    def set_dyn(self, on):
        _B().cfg.dynlight_enabled = bool(on)
        _B()._save_keys({"dynlight_enabled": _B().cfg.dynlight_enabled})
        _B().dynlight.enabled = bool(on)
        if on:
            try:
                _B().dynlight.request_refresh()
            except Exception:
                pass
        _B().log("Dynamic Lighting mirror " + ("ON." if on else "OFF."))
        return _B().get_state()

    def dyn_refresh(self):
        try:
            _B().dynlight.request_refresh()
            _B().log("Dynamic Lighting refresh requested.")
        except Exception as e:
            _B().log(f"Refresh: {e}")

        def _late():
            time.sleep(3)
            try:
                _B().push({"t": "dyn",
                             "lamps": int(getattr(_B().dynlight, "lamp_count", 0) or 0)})
            except Exception:
                pass
        threading.Thread(target=_late, daemon=True).start()
        return True

    def bt_refresh(self):
        threading.Thread(target=_B()._bt_refresh, daemon=True).start()
        return True

    def bt_enable(self):
        def _w():
            try:
                try_enable_bluetooth()
            except Exception as e:
                _B().log(f"BT enable: {e}")
            _B()._bt_refresh()
        threading.Thread(target=_w, daemon=True).start()
        return True

    def bt_settings(self):
        try:
            open_bluetooth_settings()
        except Exception as e:
            _B().log(f"BT settings: {e}")
        return True

    def open_config_folder(self):
        try:
            import os
            os.startfile(str(CONFIG_PATH.parent))  # type: ignore[attr-defined]
        except Exception as e:
            _B().log(f"Open folder: {e}")
        return True


def main():
    if "--task" in sys.argv:
        try:
            on = sys.argv[sys.argv.index("--task") + 1].lower() == "on"
        except Exception:
            print("usage: --task on|off")
            return
        try:
            enable_high_priority(on, minimized=bool(Config.load().start_minimized))
            print(f"high priority {'ON' if on else 'OFF'}")
        except Exception as e:
            print(f"task setup failed: {e}")
        return
    if "--allow-multi" not in sys.argv:
        try:
            from single_instance import acquire
        except Exception:
            acquire = None  # type: ignore
        if acquire is not None and not acquire():
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Elf-Ambilight is already running.\nCheck the system tray.",
                    "Elf-Ambilight", 0x40)
            except Exception:
                pass
            return
    import webview
    back = Backend()
    global _BACKEND
    _BACKEND = back
    api = Api()
    hidden = "--minimized" in sys.argv or "--tray" in sys.argv
    win = webview.create_window(
        f"{APP_NAME} v{APP_VERSION}",
        url=_webui_file("index.html"),
        js_api=api,
        width=1180, height=780,
        min_size=(940, 620),
        hidden=hidden,
    )
    back.window = win
    selftest = "--selftest" in sys.argv
    if selftest:
        def _selftest_write(payload: dict):
            bases = [os.environ.get("LOCALAPPDATA"), os.environ.get("TEMP"),
                     r"E:\RGB PC PORT"]
            for base in bases:
                if not base:
                    continue
                try:
                    p = Path(base) / "elf_selftest.json"
                    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    return str(p)
                except Exception:
                    continue
            return None

        _selftest_write({"stage": "main reached", "version": APP_VERSION})
        t_start = time.monotonic()

        def _selftest():
            try:
                ok = False
                for _ in range(120):
                    if getattr(back, "_ui_booted", None):
                        ok = True
                        break
                    time.sleep(0.5)
                report = {
                    "stage": "finished",
                    "ok": ok,
                    "ui": getattr(back, "_ui_booted", None),
                    "version": APP_VERSION,
                    "webui": _webui_file("index.html"),
                    "private_mode": False,
                    "boot_seconds": round(time.monotonic() - t_start, 2),
                }
                try:
                    import app_rules
                    report["app_rules"] = len(app_rules.running_exes()) > 0
                except Exception as e:
                    report["app_rules_error"] = str(e)[:200]
                try:
                    import media_player
                    report["ffmpeg"] = bool(media_player.ffmpeg_path())
                except Exception as e:
                    report["media_error"] = str(e)[:200]
                _selftest_write(report)
            finally:
                back.begin_quit(force=True)
        threading.Thread(target=_selftest, daemon=True).start()

    def on_closing():
        # close → tray unless user chose quit-on-close
        try:
            if back.cfg.quit_on_close or back._quitting:
                return True
            win.hide()
            back.log("Minimized to tray.")
            return False
        except Exception:
            return True

    try:
        win.events.closing += on_closing
    except Exception:
        pass
    back.ensure_tray()
    if back.cfg.bt_on_startup:
        threading.Thread(target=lambda: (try_enable_bluetooth()
                                         if bt_service_running() is False else None),
                         daemon=True).start()
    threading.Thread(target=back.watchdog, daemon=True).start()
    threading.Thread(target=back.ambi_tick_loop, daemon=True).start()
    threading.Thread(target=back.app_rule_loop, daemon=True).start()
    threading.Thread(target=back.media_tick_loop, daemon=True).start()
    if back.dynlight.enabled:
        threading.Timer(6.0, lambda: back.dynlight.request_refresh()).start()
    if back.cfg.auto_connect and back.cfg.last_addresses:
        threading.Timer(0.8, back.auto_connect).start()
    back._bt_refresh()
    threading.Timer(6.0, lambda: back._ambi_autostart(6)).start()
    webview.start()
    back.begin_quit(force=True)


if __name__ == "__main__":
    main()
