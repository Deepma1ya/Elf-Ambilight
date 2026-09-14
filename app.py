"""Elf-Ambilight — Modern customtkinter UI for BLE LED strips."""
from __future__ import annotations
import asyncio
import colorsys
import json
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, messagebox
from typing import Optional

import customtkinter as ctk

from protocol import (
    MODES, pkt_power, pkt_brightness, pkt_color_static, pkt_color_realtime,
    pkt_mode, pkt_mode_speed, pkt_single_color_w, pkt_cct, pkt_rgbw,
    pkt_timing, pkt_system_time, pkt_pin_sequence, hex_str,
)
from ble_manager import ElfBLE, FoundDevice
from ambilight import Ambilight
from config import Config, ORDERS, CONFIG_PATH
from bt_helper import (
    bt_service_running, try_enable_bluetooth, open_bluetooth_settings,
    looks_like_bt_off,
)
from startup_helper import startup_enabled, set_startup

# ── Theme ──────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#0f1117"
SIDEBAR = "#161922"
CARD = "#1c2030"
CARD_HOVER = "#242940"
ACCENT = "#5b8def"
ACCENT_DIM = "#3a5fa0"
GREEN = "#4ade80"
RED = "#f87171"
YELLOW = "#fbbf24"
FG = "#e2e8f0"
MUTED = "#64748b"
ENTRY_BG = "#0d1017"

PAD = 12


# ── Async runner ───────────────────────────────────────────────────────
class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


# ── Color helpers ──────────────────────────────────────────────────────
def rgb_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"


def hsl_to_rgb(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return int(r * 255), int(g * 255), int(b * 255)


def rgb_to_hsl(r, g, b):
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    return h * 360, s * 100, l * 100


# ── App ────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Elf-Ambilight")
        self.geometry("1020x700")
        self.minsize(800, 550)
        self.configure(fg_color=BG)

        self.runner = AsyncRunner()
        self.ble = ElfBLE()
        self.cfg = Config.load()
        self.cfg.save()  # ensure config.json exists in app folder
        self._manual_disc: set[str] = set()
        self._ever_connected = False
        self._dirty: set[str] = set()
        self._current_page = ""
        self._debounce: dict[str, str] = {}
        self._sched_fired: dict[int, str] = {}
        self._bt_prompted = False
        self._bt_state: str = "unknown"
        self._tray = None
        self.found: dict[str, FoundDevice] = {}
        self.ambilight = Ambilight(self.ble, self._ambi_targets)
        self.ambilight.calibrate = self.cfg.cal_apply
        self._color = (self.cfg.last_r, self.cfg.last_g, self.cfg.last_b)

        self._page_builders = {
            "devices": self._page_devices,
            "color": self._page_color,
            "tune": self._page_tune,
            "modes": self._page_modes,
            "timing": self._page_timing,
            "ambi": self._page_ambi,
            "settings": self._page_settings,
        }
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._build_sidebar()
        self._build_pages()
        self._show_page("devices")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-s>", lambda _e: self._save_all())
        self.bind("<Control-S>", lambda _e: self._save_all())

        if "--minimized" in sys.argv or "--tray" in sys.argv:
            self.withdraw()
        self._ensure_tray()

        if self.cfg.bt_on_startup:
            threading.Thread(target=self._bt_startup_enable, daemon=True).start()
        if self.cfg.auto_connect and self.cfg.last_addresses:
            self.after(800, self._auto_connect)
        self.after(15000, self._watchdog)
        self.after(3000, self._bt_refresh_threaded)
        self.after(6000, lambda: self._ambi_autostart())

    def _bt_startup_enable(self):
        try:
            if bt_service_running() is False:
                self._log("Bluetooth off at startup — trying to enable ...")
                try_enable_bluetooth()
        except Exception:
            pass

    # ── Sidebar ────────────────────────────────────────────────────
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=60, fg_color=SIDEBAR, corner_radius=0)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        self._page_btns: dict[str, ctk.CTkButton] = {}
        pages = [
            ("devices", "Devices"),
            ("color", "Color"),
            ("tune", "Tune"),
            ("modes", "Modes"),
            ("timing", "Timer"),
            ("ambi", "Ambi"),
            ("settings", "Setup"),
        ]
        for key, label in pages:
            b = ctk.CTkButton(
                sb, text=label, width=56, height=34, corner_radius=10,
                fg_color="transparent", hover_color=CARD_HOVER,
                text_color=MUTED,
                font=ctk.CTkFont(size=11),
                command=lambda k=key: self._show_page(k),
            )
            b.pack(pady=2, padx=2)
            self._page_btns[key] = b

        # bottom info strip: live color dot + connections + BT
        info = ctk.CTkFrame(sb, fg_color="transparent")
        info.pack(side="bottom", pady=(0, 4))
        self.side_dot = ctk.CTkLabel(info, text="", width=40, height=14,
                                     fg_color=rgb_hex(*self._color), corner_radius=7,
                                     cursor="hand2")
        self.side_dot.pack(pady=2)
        self.side_dot.bind("<Button-1>", lambda _e: self._show_page("color"))
        self.conn_label = ctk.CTkLabel(
            info, text="0", text_color=GREEN,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.conn_label.pack()
        self.bt_dot = ctk.CTkLabel(info, text="BT?", text_color=MUTED,
                                   font=ctk.CTkFont(size=10, weight="bold"))
        self.bt_dot.pack()

        # power + save buttons at bottom
        ctk.CTkButton(
            sb, text="OFF", width=56, height=24, corner_radius=8,
            fg_color=RED, hover_color="#b91c1c",
            command=lambda: self._send_power(False),
        ).pack(side="bottom", pady=1)
        ctk.CTkButton(
            sb, text="ON", width=56, height=24, corner_radius=8,
            fg_color=GREEN, hover_color="#2ea44f",
            command=lambda: self._send_power(True),
        ).pack(side="bottom", pady=1)
        self.save_btn = ctk.CTkButton(
            sb, text="Save", width=56, height=28, corner_radius=8,
            fg_color=ACCENT, hover_color="#4a7dd4",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save_all,
        )
        self.save_btn.pack(side="bottom", pady=(1, 4))

    PAGE_TITLES = {"devices": "Devices", "color": "Color", "tune": "Tune Colors",
                   "modes": "Modes", "timing": "Timing", "ambi": "Ambilight",
                   "settings": "Settings"}

    def _show_page(self, name: str):
        if name == self._current_page:
            return
        if self._dirty:
            names = ", ".join(self.PAGE_TITLES.get(k, k) for k in sorted(self._dirty))
            self._unsaved_popup(
                f"Unsaved changes on: {names}",
                on_save=lambda: (self._save_all(), self._do_show(name)),
                on_discard=lambda: (self._discard_all(), self._do_show(name)),
            )
            return
        self._do_show(name)

    def _do_show(self, name: str):
        self._current_page = name
        for k, b in self._page_btns.items():
            b.configure(fg_color=ACCENT_DIM if k == name else "transparent",
                        text_color=FG if k == name else MUTED)
        self._ensure_page(name)
        for k, f in self._pages.items():
            if k != name:
                f.pack_forget()
        self._pages[name].pack(in_=self._content, fill="both", expand=True, padx=PAD, pady=PAD)

    def _unsaved_popup(self, msg: str, on_save, on_discard):
        top = ctk.CTkToplevel(self)
        top.title("Unsaved changes")
        top.geometry("360x170")
        top.configure(fg_color=BG)
        top.transient(self)
        top.grab_set()
        try:
            top.after(50, top.focus_force)
        except Exception:
            pass
        ctk.CTkLabel(top, text=msg, wraplength=320, text_color=FG,
                      font=ctk.CTkFont(size=13)).pack(pady=(16, 4))
        ctk.CTkLabel(top, text="Save keeps the new values. Discard reverts to the last save.",
                      wraplength=320, text_color=MUTED, font=ctk.CTkFont(size=11)).pack(pady=(0, 10))
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="Save", corner_radius=8, width=90,
                       command=lambda: (top.destroy(), on_save())).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Discard", corner_radius=8, width=90,
                       fg_color="#443333", hover_color="#663333",
                       command=lambda: (top.destroy(), on_discard())).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Cancel", corner_radius=8, width=90,
                       fg_color="transparent", border_width=1,
                       command=top.destroy).pack(side="left", padx=4)

    # ── Save / dirty ─────────────────────────────────────────────
    def _mark_dirty(self, page: str):
        self._dirty.add(page)
        self._refresh_save_btn()

    def _refresh_save_btn(self):
        try:
            n = len(self._dirty)
            self.save_btn.configure(text=f"Save ({n})" if n else "Save")
        except Exception:
            pass

    def _save_all(self):
        try:
            self.cfg.save()
            self._dirty.clear()
            self._refresh_save_btn()
            self._log("Settings saved to config.json.")
        except Exception as e:
            self._log(f"Save failed: {e}")

    def _save_keys(self, keys: dict):
        """Persist only the given keys to config.json (keeps staged edits staged)."""
        try:
            d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        d.update(keys)
        try:
            CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _save_conn_state(self):
        self._save_keys({"names": dict(self.cfg.names),
                         "last_addresses": list(self.cfg.last_addresses)})

    def _save_devices_state(self):
        self._save_keys({"groups": {k: list(v) for k, v in self.cfg.groups.items()},
                         "selected_group": self.cfg.selected_group,
                         "names": dict(self.cfg.names),
                         "last_addresses": list(self.cfg.last_addresses)})

    def _clear_debounces(self):
        for key in list(self._debounce):
            try:
                self.after_cancel(self._debounce.pop(key))
            except Exception:
                pass

    def _revert_strip(self, dirty_pages: set[str]):
        """Restore the strip to the saved light state: brightness + whichever
        of color/mode was showing when last saved (tracked via last_sent)."""
        try:
            if not self._targets():
                return
            do_color = bool({"color", "tune"} & dirty_pages)
            do_modes = bool({"modes"} & dirty_pages)
            if not (do_color or do_modes):
                return
            saved = self.cfg
            self._send_pkt(pkt_brightness(saved.last_brightness),
                            f"revert brightness {saved.last_brightness}")
            if saved.last_sent == "mode" or (do_modes and not do_color):
                self._send_pkt(pkt_mode(saved.last_mode),
                                f"revert mode {saved.last_mode} {MODES[saved.last_mode]}")
                self._send_pkt(pkt_mode_speed(saved.last_speed),
                                f"revert speed {saved.last_speed}")
            else:
                r, g, b = saved.last_r, saved.last_g, saved.last_b
                self._send_pkt(pkt_color_static(r, g, b), f"revert RGB({r},{g},{b})")
        except Exception as e:
            self._log(f"Revert send failed: {e}")

    def _discard_all(self):
        dirty_pages = set(self._dirty)
        self._clear_debounces()  # staged live-sends must not fire after revert
        self._dirty.clear()
        self._refresh_save_btn()
        try:
            self.cfg = Config.load()
            self.ambilight.calibrate = self.cfg.cal_apply
            self._color = (self.cfg.last_r, self.cfg.last_g, self.cfg.last_b)
            self._rebuild_pages()
        except Exception as e:
            self._log(f"Revert failed: {e}")
        finally:
            # navigation must never get stuck, even if rebuild had an issue
            try:
                self._do_show(self._current_page or "devices")
            except Exception:
                pass
        self._revert_strip(dirty_pages)
        self._log("Discarded — reverted to last save.")

    def _rebuild_pages(self):
        # destroy the old content frame wholesale (pages + leaked spacers),
        # then rebuild fresh — never stack multiple content frames.
        old = getattr(self, "_content", None)
        try:
            if old is not None and str(old.winfo_exists()) == "1":
                old.destroy()
        except Exception:
            pass
        self._pages = {}
        self._build_pages()

    def _debounced(self, key: str, delay_ms: int, fn):
        old = self._debounce.pop(key, None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
        self._debounce[key] = self.after(delay_ms, lambda: (self._debounce.pop(key, None), fn()))

    # ── Pages container (lazy: devices now, rest on first show) ──
    def _build_pages(self):
        self._content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self._content.pack(side="left", fill="both", expand=True)
        self._pages = {}
        self._ensure_page("devices")

    def _ensure_page(self, name: str):
        if name not in self._pages:
            self._pages[name] = self._page_builders[name]()
        return self._pages[name]

    # ── Helpers ────────────────────────────────────────────────────
    def _log(self, msg: str):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            if hasattr(self, "_log_box"):
                self._log_box.configure(state="normal")
                self._log_box.insert("end", f"[{ts}] {msg}\n")
                try:  # trim: keep last ~300 lines so long sessions stay fast
                    end = self._log_box.index("end-1c")
                    nlines = int(str(end).split(".")[0])
                    if nlines > 400:
                        self._log_box.delete("1.0", f"{nlines - 300}.0")
                except Exception:
                    pass
                self._log_box.see("end")
                self._log_box.configure(state="disabled")
        except Exception:
            pass

    def _run_async(self, coro, ok="", err="Error", on_err=None):
        fut = self.runner.submit(coro)

        def _poll():
            if fut.done():
                try:
                    fut.result(timeout=60)
                    if ok:
                        self._log(ok)
                except Exception as e:
                    self._log(f"{err}: {e}")
                    if on_err is not None:
                        try:
                            on_err(e)
                        except Exception:
                            pass
                self._refresh_status()
            else:
                self.after(200, _poll)
        _poll()

    def _targets(self) -> list[str]:
        addrs = [a for a in self.cfg.targets() if self.ble.is_connected(a)]
        return addrs or self.ble.connected_addresses()

    @staticmethod
    def _get_int(var, default: int) -> int:
        try:
            return int(float(var.get()))
        except Exception:
            return default

    # NOTE: CTkLabel.bind() already forwards to its inner canvas+label,
    # so plain .bind() on a swatch/dot IS enough — do not double-bind.

    def _send_pkt(self, pkt, what):
        addrs = self._targets()
        if not addrs:
            self._log("No device connected.")
            return
        # calibration
        note = ""
        if len(pkt) == 9 and pkt[0] == 0x7E and pkt[1] == 0x07 and pkt[2] == 0x05 and pkt[3] == 0x03 and pkt[7] in (0x10, 0x20):
            r, g, b = pkt[4], pkt[5], pkt[6]
            cr, cg, cb = self.cfg.cal_apply(r, g, b)
            if (cr, cg, cb) != (r, g, b):
                pkt = bytes([pkt[0], pkt[1], pkt[2], pkt[3], cr, cg, cb, pkt[7], pkt[8]])
                note = f" cal({cr},{cg},{cb})"

        async def _do():
            res = await self.ble.write_many(addrs, pkt)
            fails = [a for a, ok, _ in res if not ok]
            if fails:
                raise RuntimeError(f"write failed: {fails}")
        self._log(f"TX {what}{note} → {len(addrs)} dev")
        self._run_async(_do())

    def _send_power(self, on):
        self._send_pkt(pkt_power(on), f"power {'ON' if on else 'OFF'}")

    def _refresh_status(self):
        n = len(self.ble.connected_addresses())
        self.conn_label.configure(text=str(n), text_color=GREEN if n > 0 else RED)

    def _really_quit(self):
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
            self.destroy()
        except Exception:
            pass

    def _on_close(self):
        if self._dirty:
            names = ", ".join(self.PAGE_TITLES.get(k, k) for k in sorted(self._dirty))
            self._unsaved_popup(
                f"Unsaved changes on: {names}",
                on_save=lambda: (self._save_all(), self._really_quit()),
                on_discard=lambda: self._really_quit(),
            )
            return
        if self.cfg.quit_on_close:
            self._really_quit()
            return
        self.withdraw()  # always to tray; icon restores the window

    # ── Tray ─────────────────────────────────────────────────────
    def _tray_icon_image(self):
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            r, g, b = self._color
            d.rounded_rectangle([6, 6, 58, 58], radius=14, fill=(r, g, b, 255))
            return img
        except Exception:
            return None

    def _ensure_tray(self):
        try:
            import pystray
            img = self._tray_icon_image()
            if img is None:
                return
            menu = pystray.Menu(
                pystray.MenuItem("Show", self._tray_show, default=True),
                pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = pystray.Icon("Elf-Ambilight", img, "Elf-Ambilight", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception as e:
            self._log(f"Tray unavailable: {e}")

    def _tray_show(self, *a):
        try:
            self.after(0, self._tray_restore)
        except Exception:
            pass

    def _tray_restore(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _tray_quit(self, *a):
        try:
            self.after(0, self._on_close)
        except Exception:
            pass

    # ── BT status / prompt ───────────────────────────────────────
    def _bt_refresh_threaded(self):
        def _work():
            try:
                st = bt_service_running()
            except Exception:
                st = None
            try:
                self.after(0, lambda: self._bt_refresh_done(st))
            except Exception:
                pass  # app closing

        threading.Thread(target=_work, daemon=True).start()

    def _bt_refresh_done(self, running):
        self._bt_state = "on" if running else ("off" if running is False else "unknown")
        try:
            if running:
                self.bt_dot.configure(text="BT", text_color=GREEN)
            elif running is False:
                self.bt_dot.configure(text="BT!", text_color=RED)
            else:
                self.bt_dot.configure(text="BT?", text_color=MUTED)
        except Exception:
            pass
        try:
            if hasattr(self, "bt_status_lbl"):
                self.bt_status_lbl.configure(
                    text=f"Bluetooth: {'ON' if running else ('OFF' if running is False else 'unknown')}",
                    text_color=GREEN if running else (RED if running is False else MUTED),
                )
        except Exception:
            pass
        if running is False and not self._bt_prompted:
            self._bt_prompted = True
            self._bt_prompt(auto=True)

    def _bt_prompt(self, auto=False):
        top = ctk.CTkToplevel(self)
        top.title("Bluetooth is off")
        top.geometry("380x200")
        top.configure(fg_color=BG)
        top.transient(self)
        top.grab_set()
        ctk.CTkLabel(top, text="Bluetooth appears to be OFF",
                      font=ctk.CTkFont(size=15, weight="bold"), text_color=FG).pack(pady=(16, 4))
        ctk.CTkLabel(top, text="Turn Bluetooth on so the app can find your strip.",
                      wraplength=330, text_color=MUTED).pack(pady=(0, 10))
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="BT Settings", corner_radius=8, width=100,
                       command=open_bluetooth_settings).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Try turn on", corner_radius=8, width=100,
                       command=lambda: threading.Thread(
                           target=self._bt_try_enable, args=(top,), daemon=True).start()
                       ).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Dismiss", corner_radius=8, width=80,
                       fg_color="transparent", border_width=1,
                       command=top.destroy).pack(side="left", padx=4)

    def _bt_try_enable(self, dlg=None):
        try:
            self._log("Trying to enable Bluetooth ...")
            try_enable_bluetooth()
        except Exception as e:
            self._log(f"Enable BT failed: {e}")
        self._bt_refresh_threaded()
        if dlg is not None:
            try:
                self.after(0, dlg.destroy)
            except Exception:
                pass

    def _bt_check_error(self, err):
        if looks_like_bt_off(err):
            self._log("Bluetooth looks OFF — showing prompt.")
            try:
                self.after(0, lambda: self._bt_prompt())
            except Exception:
                pass
            return True
        return False

    # ── Auto-connect / watchdog ────────────────────────────────────
    async def _try_connect(self, addr: str):
        if self.ble.is_connected(addr) or addr in self._manual_disc:
            return
        await self.ble.connect(addr, self.cfg.names.get(addr, ""))
        self.cfg.touch_last(addr)
        self._ever_connected = True
        self._log(f"Auto-connected {self.cfg.names.get(addr, '?')} {addr}")

    def _auto_connect(self):
        async def _do():
            import asyncio as _aio
            res = await _aio.gather(
                *(self._try_connect(a) for a in self.cfg.last_addresses[:]),
                return_exceptions=True,
            )
            return res

        def _on_done(fut):
            try:
                res = fut.result()
                for r in res or []:
                    if isinstance(r, Exception) and self._bt_check_error(r):
                        break
            except Exception as e:
                self._log(f"Auto-connect: {e}")
                self._bt_check_error(e)
            self._save_conn_state()
            self._refresh_status()
            try:
                self._refresh_devices()
                self._refresh_groups()
            except Exception:
                pass
            self._maybe_sync_time_startup()
            self._ambi_autostart()

        fut = self.runner.submit(_do())

        def _poll():
            if fut.done():
                _on_done(fut)
            else:
                self.after(300, _poll)
        _poll()
        self._log("Auto-connecting remembered devices ...")

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
            self._log(f"Clock sync failed: {e}")

    def _watchdog(self):
        try:
            if (self.cfg.auto_reconnect and self._ever_connected
                    and not self.ble.connected_addresses()
                    and [a for a in self.cfg.last_addresses if a not in self._manual_disc]):
                self._log("Connection lost — reconnecting ...")
                self._auto_connect()
            self._check_schedules()
        finally:
            self.after(15000, self._watchdog)
            self.after(15000, self._bt_refresh_threaded)

    def _ambi_targets(self):
        return self._targets()

    # ════════════════════════════════════════════════════════════════
    # DEVICES PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_devices(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")

        # header
        h = ctk.CTkFrame(f, fg_color="transparent")
        h.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(h, text="Devices", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(side="left")
        self.dev_status = ctk.CTkLabel(h, text="", text_color=MUTED,
                                        font=ctk.CTkFont(size=12))
        self.dev_status.pack(side="right")

        # buttons row
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(row, text="Scan 5s", corner_radius=8, width=90,
                       command=self._scan).pack(side="left", padx=2)
        ctk.CTkButton(row, text="Quick", corner_radius=8, width=60,
                       fg_color=ACCENT_DIM, command=self._quick_scan).pack(side="left", padx=2)
        ctk.CTkButton(row, text="Connect", corner_radius=8, width=80,
                       command=self._connect_selected).pack(side="left", padx=2)
        ctk.CTkButton(row, text="Disconnect", corner_radius=8, width=90,
                       fg_color="#443333", hover_color="#663333",
                       command=self._disconnect_selected).pack(side="left", padx=2)
        ctk.CTkButton(row, text="+ Group", corner_radius=8, width=70,
                       command=self._add_to_group).pack(side="left", padx=(14, 2))

        # device list
        self.dev_frame = ctk.CTkScrollableFrame(f, fg_color=CARD, corner_radius=10,
                                                  height=200)
        self.dev_frame.pack(fill="both", expand=True, pady=(0, 8))
        self.dev_labels: list[ctk.CTkFrame] = []

        # groups
        g_frame = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        g_frame.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(g_frame, text="Group", font=ctk.CTkFont(size=12, weight="bold"),
                      text_color=MUTED).pack(side="left", padx=(0, 6))
        self.group_var = ctk.StringVar(value=self.cfg.selected_group)
        self.group_menu = ctk.CTkOptionMenu(
            g_frame, variable=self.group_var, values=list(self.cfg.groups.keys()),
            width=160, corner_radius=8,
            command=lambda _: self._select_group(),
        )
        self.group_menu.pack(side="left", padx=4)
        ctk.CTkButton(g_frame, text="New", corner_radius=8, width=50,
                       command=self._new_group).pack(side="left", padx=4)
        ctk.CTkButton(g_frame, text="Remove", corner_radius=8, width=60,
                       fg_color="#443333", command=self._remove_from_group).pack(side="left", padx=4)

        # group members
        self.members_frame = ctk.CTkScrollableFrame(f, fg_color=CARD, corner_radius=10,
                                                      height=80)
        self.members_frame.pack(fill="x")

        # log
        ctk.CTkLabel(f, text="Log", font=ctk.CTkFont(size=12, weight="bold"),
                      text_color=MUTED).pack(anchor="w", pady=(8, 2))
        self._log_box = ctk.CTkTextbox(f, height=80, fg_color=ENTRY_BG,
                                         text_color="#94a3b8", font=ctk.CTkFont(family="Consolas", size=11),
                                         corner_radius=8)
        self._log_box.pack(fill="x")
        self._log_box.configure(state="disabled")

        self._refresh_devices()
        self._refresh_groups()
        return f

    def _refresh_devices(self):
        for w in self.dev_labels:
            w.destroy()
        self.dev_labels.clear()

        all_addrs = set(self.cfg.last_addresses) | set(self.found.keys()) | set(self.ble.connected_addresses())
        for addr in all_addrs:
            name = self.cfg.names.get(addr, self.found.get(addr, FoundDevice("", addr)).name)
            connected = self.ble.is_connected(addr)
            row = ctk.CTkFrame(self.dev_frame, fg_color=CARD_HOVER if connected else "transparent",
                                corner_radius=6, height=32)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            ctk.CTkLabel(row, text="\u25cf" if connected else "\u25cb",
                          text_color=GREEN if connected else MUTED,
                          font=ctk.CTkFont(size=14), width=20).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=name, text_color=FG,
                          font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=addr, text_color=MUTED,
                          font=ctk.CTkFont(family="Consolas", size=10)).pack(side="left", padx=8)
            self.dev_labels.append(row)

    def _refresh_groups(self):
        self.group_menu.configure(values=list(self.cfg.groups.keys()))
        self.group_var.set(self.cfg.selected_group)
        for w in self.members_frame.winfo_children():
            w.destroy()
        for addr in self.cfg.targets():
            name = self.cfg.names.get(addr, "?")
            connected = self.ble.is_connected(addr)
            r = ctk.CTkFrame(self.members_frame, fg_color="transparent", height=24)
            r.pack(fill="x")
            ctk.CTkLabel(r, text=f"\u25cf {name}  {addr}" if connected else f"\u25cb {name}  {addr}",
                          text_color=GREEN if connected else MUTED,
                          font=ctk.CTkFont(size=11)).pack(anchor="w")
        self.dev_status.configure(
            text=f"Connected: {len(self.ble.connected_addresses())}  ·  "
                 f"Group: {self.cfg.selected_group}  ·  "
                 f"Auto: {'ON' if self.cfg.auto_connect else 'OFF'}"
        )

    def _scan(self):
        self._log("Scanning 5s ...")

        async def _do():
            return await self.ble.scan(timeout=5.0)

        def _on_done(fut):
            try:
                devs = fut.result()
                for d in devs:
                    self.found[d.address] = d
                    self.cfg.remember_name(d.address, d.name)
                self._save_conn_state()
                self._log(f"Found {len(devs)} device(s).")
                self._refresh_devices()
            except Exception as e:
                self._log(f"Scan failed: {e}")
                self._bt_check_error(e)

        fut = self.runner.submit(_do())

        def _poll():
            if fut.done():
                _on_done(fut)
            else:
                self.after(300, _poll)
        _poll()

    def _quick_scan(self):
        self._log("Quick scan 3s + auto-connect ...")

        async def _do():
            devs = await self.ble.scan(timeout=3.0)
            for d in devs:
                self.found[d.address] = d
                self.cfg.remember_name(d.address, d.name)
            self._save_conn_state()
            for a in self.cfg.last_addresses[:]:
                if self.ble.is_connected(a) or a in self._manual_disc:
                    continue
                if a in [d.address for d in devs]:
                    try:
                        await self.ble.connect(a, self.cfg.names.get(a, ""))
                        self._ever_connected = True
                        self._log(f"Connected {a}")
                    except Exception:
                        pass
            return devs

        self._run_async(_do(), ok="Quick scan done.", on_err=self._bt_check_error)

    def _sel_addr(self) -> Optional[str]:
        sel = self.dev_frame.winfo_children()
        for w in sel:
            # try to get from found or last_addresses
            pass
        # fallback: pick first in found
        if self.found:
            return next(iter(self.found))
        return None

    def _connect_selected(self):
        if not self.found:
            self._log("No devices found. Scan first.")
            return
        # connect all found not yet connected
        async def _do():
            for d in list(self.found.values()):
                if not self.ble.is_connected(d.address):
                    try:
                        await self.ble.connect(d.address, d.name)
                        self._manual_disc.discard(d.address)
                        self.cfg.touch_last(d.address)
                        self.cfg.remember_name(d.address, d.name)
                        self._ever_connected = True
                        self._log(f"Connected {d.name} {d.address}")
                    except Exception as e:
                        self._log(f"Connect {d.address}: {e}")
                        self._bt_check_error(e)
            self._save_conn_state()
        self._run_async(_do(), on_err=self._bt_check_error)

    def _disconnect_selected(self):
        addrs = self.ble.connected_addresses()
        self._manual_disc.update(addrs)

        async def _do():
            for a in addrs:
                await self.ble.disconnect(a)
                self._log(f"Disconnected {a}")
        self._run_async(_do())

    def _add_to_group(self):
        if not self.found:
            return
        for addr in list(self.found.keys()):
            g = self.cfg.selected_group
            if addr not in self.cfg.groups[g]:
                self.cfg.groups[g].append(addr)
                self.cfg.remember_name(addr, self.found[addr].name)
        self._save_devices_state()
        self._refresh_groups()
        self._log(f"Added devices to '{self.cfg.selected_group}'.")

    def _select_group(self):
        self.cfg.selected_group = self.group_var.get()
        self._save_devices_state()
        self._refresh_groups()

    def _new_group(self):
        top = ctk.CTkToplevel(self)
        top.title("New Group")
        top.geometry("300x120")
        top.configure(fg_color=BG)
        top.grab_set()
        ctk.CTkLabel(top, text="Group name:", fg_color="transparent").pack(pady=(12, 4))
        ent = ctk.CTkEntry(top, width=240, corner_radius=8)
        ent.pack(pady=4)
        ent.focus()

        def _ok():
            n = ent.get().strip()
            if n and n not in self.cfg.groups:
                self.cfg.groups[n] = []
                self.cfg.selected_group = n
                self._save_devices_state()
                self._refresh_groups()
            top.destroy()

        ctk.CTkButton(top, text="Create", corner_radius=8, command=_ok).pack(pady=6)

    def _remove_from_group(self):
        g = self.cfg.selected_group
        addrs = self.cfg.groups.get(g, [])
        if addrs:
            self.cfg.groups[g] = addrs[:-1]
            self._save_devices_state()
            self._refresh_groups()

    # ════════════════════════════════════════════════════════════════
    # COLOR PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_color(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        body = ctk.CTkScrollableFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True)

        ctk.CTkLabel(body, text="Color", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 8))

        top = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        top.pack(fill="x", pady=(0, 8))

        # Color preview
        self.c_prev = ctk.CTkLabel(top, text="", width=100, height=60,
                                    fg_color=rgb_hex(*self._color), corner_radius=10)
        self.c_prev.pack(side="left", padx=(0, 12))

        # Hex + RGB info
        info = ctk.CTkFrame(top, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        self.c_hex = ctk.CTkLabel(info, text=rgb_hex(*self._color),
                                   font=ctk.CTkFont(family="Consolas", size=16, weight="bold"),
                                   text_color=FG)
        self.c_hex.pack(anchor="w")
        self.c_rgb = ctk.CTkLabel(info, text=f"{self._color[0]}, {self._color[1]}, {self._color[2]}",
                                   text_color=MUTED, font=ctk.CTkFont(size=12))
        self.c_rgb.pack(anchor="w")

        # Live toggle + Send button
        self.live_var = ctk.BooleanVar(value=self.cfg.live_send)
        ctk.CTkCheckBox(top, text="Live", variable=self.live_var, corner_radius=4,
                         font=ctk.CTkFont(size=12),
                         command=self._live_toggled).pack(side="right", padx=4)
        ctk.CTkButton(top, text="Send", corner_radius=8, width=80,
                       command=self._send_color).pack(side="right", padx=4)

        # Hex entry
        hex_row = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        hex_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(hex_row, text="Hex", font=ctk.CTkFont(size=12, weight="bold"),
                      text_color=MUTED).pack(side="left", padx=(0, 6))
        self.hex_entry = ctk.CTkEntry(hex_row, width=120, corner_radius=8,
                                       placeholder_text="#FF8040")
        self.hex_entry.pack(side="left", padx=4)
        self.hex_entry.bind("<Return>", lambda _: self._apply_hex())
        ctk.CTkButton(hex_row, text="Apply", corner_radius=8, width=60,
                       command=self._apply_hex).pack(side="left", padx=4)

        # HSL sliders
        hsl = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        hsl.pack(fill="x", pady=(0, 8))

        h, s, l = rgb_to_hsl(*self._color)
        self.h_var = ctk.DoubleVar(value=h)
        self.s_var = ctk.DoubleVar(value=s)
        self.l_var = ctk.DoubleVar(value=l)

        for label, var, lo, hi, fmt in [
            ("Hue", self.h_var, 0, 360, "{:.0f}"),
            ("Sat", self.s_var, 0, 100, "{:.0f}%"),
            ("Lit", self.l_var, 0, 100, "{:.0f}%"),
        ]:
            row = ctk.CTkFrame(hsl, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=30, text_color=MUTED,
                          font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkSlider(row, from_=lo, to=hi, variable=var, width=280,
                           command=lambda _: self._hsl_changed()).pack(side="left", padx=8, fill="x", expand=True)
            lbl = ctk.CTkLabel(row, text=fmt.format(var.get()), width=50,
                                font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
            lbl.pack(side="left")
            setattr(self, f"_hsl_{label.lower()}_lbl", lbl)

        # RGB sliders
        rgb_f = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        rgb_f.pack(fill="x", pady=(0, 8))
        self.r_var = ctk.IntVar(value=self._color[0])
        self.g_var = ctk.IntVar(value=self._color[1])
        self.b_var = ctk.IntVar(value=self._color[2])
        for label, var, col in [("R", self.r_var, RED), ("G", self.g_var, GREEN), ("B", self.b_var, ACCENT)]:
            row = ctk.CTkFrame(rgb_f, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=20, text_color=col,
                          font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
            ctk.CTkSlider(row, from_=0, to=255, variable=var, width=280,
                           command=lambda _: self._rgb_changed()).pack(side="left", padx=8, fill="x", expand=True)
            lbl = ctk.CTkLabel(row, text=str(var.get()), width=40,
                                font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
            lbl.pack(side="left")
            setattr(self, f"_rgb_{label.lower()}_lbl", lbl)

        # Presets
        pre = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        pre.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(pre, text="Presets", text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 6))
        prow = ctk.CTkFrame(pre, fg_color="transparent")
        prow.pack(fill="x")
        presets = [
            ("White", 255, 255, 255), ("Snow", 255, 250, 250), ("Ivory", 255, 255, 240),
            ("Cream", 255, 253, 208), ("Warm", 255, 180, 100), ("Gold", 255, 215, 0),
            ("Orange", 255, 165, 0), ("Coral", 255, 127, 80), ("Red", 255, 0, 0),
            ("Rose", 255, 0, 127), ("Pink", 255, 192, 203), ("Hot Pink", 255, 105, 180),
            ("Purple", 170, 60, 255), ("Indigo", 75, 0, 130), ("Blue", 0, 120, 255),
            ("Sky", 135, 206, 235), ("Cyan", 0, 255, 255), ("Teal", 0, 128, 128),
            ("Lime", 0, 255, 0), ("Green", 0, 180, 0), ("Forest", 34, 139, 34),
            ("Olive", 128, 128, 0), ("Brown", 165, 42, 42), ("Peach", 255, 218, 185),
        ]
        for name, r, g, b in presets:
            ctk.CTkButton(
                prow, text="", width=30, height=24, corner_radius=6,
                fg_color=rgb_hex(r, g, b), hover_color=rgb_hex(r, g, b),
                command=lambda r=r, g=g, b=b: self._set_color(r, g, b),
            ).pack(side="left", padx=2, pady=2)

        # Recent colors
        rec = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        rec.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(rec, text="Recent", text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        self.recent_frame = ctk.CTkFrame(rec, fg_color="transparent")
        self.recent_frame.pack(fill="x")
        self._refresh_recent()

        # Brightness + W/CCT
        bri = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        bri.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(bri, text="Brightness", text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        b_row = ctk.CTkFrame(bri, fg_color="transparent")
        b_row.pack(fill="x")
        self.bri_var = ctk.IntVar(value=self.cfg.last_brightness)
        ctk.CTkSlider(b_row, from_=0, to=100, variable=self.bri_var, width=240,
                       command=lambda _: self._bri_changed()).pack(side="left", padx=(0, 8))
        self.bri_lbl = ctk.CTkLabel(b_row, text=str(self.cfg.last_brightness),
                                     font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED, width=40)
        self.bri_lbl.pack(side="left")
        ctk.CTkButton(b_row, text="Send", corner_radius=8, width=60,
                       command=self._send_bri).pack(side="left", padx=8)
        self.chan_var = ctk.StringVar(value="ALL")
        ctk.CTkOptionMenu(b_row, variable=self.chan_var, values=["ALL", "RGB", "W", "CT"],
                           width=80, corner_radius=8).pack(side="left", padx=4)

        # W / CCT
        w_row = ctk.CTkFrame(bri, fg_color="transparent")
        w_row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(w_row, text="W", text_color=MUTED).pack(side="left")
        self.w_var = ctk.IntVar(value=50)
        ctk.CTkSlider(w_row, from_=0, to=100, variable=self.w_var, width=100).pack(side="left", padx=4)
        ctk.CTkButton(w_row, text="Send W", corner_radius=8, width=60,
                       command=lambda: self._send_pkt(pkt_single_color_w(self.w_var.get()), "single-W")).pack(side="left", padx=4)
        ctk.CTkLabel(w_row, text="Warm", text_color=MUTED).pack(side="left", padx=(12, 2))
        self.warm_var = ctk.IntVar(value=50)
        ctk.CTkSlider(w_row, from_=0, to=100, variable=self.warm_var, width=80).pack(side="left")
        ctk.CTkLabel(w_row, text="Cold", text_color=MUTED).pack(side="left", padx=(8, 2))
        self.cold_var = ctk.IntVar(value=50)
        ctk.CTkSlider(w_row, from_=0, to=100, variable=self.cold_var, width=80).pack(side="left")
        ctk.CTkButton(w_row, text="CCT", corner_radius=8, width=50,
                       command=lambda: self._send_pkt(pkt_cct(self.warm_var.get(), self.cold_var.get()), "cct")).pack(side="left", padx=4)

        # Channel on/off + pins
        ch = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        ch.pack(fill="x")
        c_row = ctk.CTkFrame(ch, fg_color="transparent")
        c_row.pack(fill="x")
        self.rgb_on = ctk.BooleanVar(value=True)
        self.w_on = ctk.BooleanVar(value=True)
        self.cct_on = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c_row, text="RGB", variable=self.rgb_on, corner_radius=4).pack(side="left", padx=6)
        ctk.CTkCheckBox(c_row, text="W", variable=self.w_on, corner_radius=4).pack(side="left", padx=6)
        ctk.CTkCheckBox(c_row, text="CCT", variable=self.cct_on, corner_radius=4).pack(side="left", padx=6)
        ctk.CTkButton(c_row, text="Send state", corner_radius=8, width=80,
                       command=self._send_rgbw).pack(side="left", padx=8)
        ctk.CTkLabel(c_row, text="Pins", text_color=MUTED).pack(side="left", padx=(12, 4))
        self.p1 = ctk.IntVar(value=1)
        self.p2 = ctk.IntVar(value=2)
        self.p3 = ctk.IntVar(value=3)
        for v in (self.p1, self.p2, self.p3):
            ctk.CTkEntry(c_row, width=40, textvariable=v, corner_radius=6).pack(side="left", padx=2)
        ctk.CTkButton(c_row, text="Pins", corner_radius=8, width=50,
                       command=self._send_pins).pack(side="left", padx=4)

        return f

    def _stage_color(self, add_recent=False):
        r, g, b = self._color
        self.cfg.last_r, self.cfg.last_g, self.cfg.last_b = r, g, b
        self.cfg.last_sent = "color"
        if add_recent:
            self.cfg.add_recent(r, g, b)
            self._refresh_recent()
        self._mark_dirty("color")

    def _live_color(self):
        """Debounced realtime send while dragging sliders."""
        if not self.cfg.live_send:
            return
        self.cfg.last_sent = "color"
        self._debounced("color", 150, lambda: (
            self._send_pkt(pkt_color_static(*self._color), f"live RGB{self._color}")))

    def _send_color(self):
        self._stage_color(add_recent=True)
        r, g, b = self._color
        try:  # refresh tray icon on explicit sends only, not every slider tick
            if self._tray is not None:
                self._tray.icon = self._tray_icon_image()
        except Exception:
            pass
        self._send_pkt(pkt_color_static(r, g, b), f"static RGB({r},{g},{b})")

    def _live_toggled(self):
        self.cfg.live_send = bool(self.live_var.get())
        self._save_keys({"live_send": self.cfg.live_send})
        self._log(f"Live send {'ON' if self.cfg.live_send else 'OFF'}.")

    def _bri_changed(self):
        v = self.bri_var.get()
        self.cfg.last_brightness = v
        self._mark_dirty("color")
        self.bri_lbl.configure(text=str(v))
        if self.cfg.live_send:
            self._debounced("bri", 150, lambda: self._send_bri(send_only=True))

    def _send_bri(self, send_only=False):
        ch = {"ALL": 0, "RGB": 1, "W": 2, "CT": 3}[self.chan_var.get()]
        v = self.bri_var.get()
        if not send_only:
            self.cfg.last_brightness = v
            self._mark_dirty("color")
        self.bri_lbl.configure(text=str(v))
        self._send_pkt(pkt_brightness(v, ch), f"brightness {v}")

    def _send_rgbw(self):
        ch = {"ALL": 0, "RGB": 1, "W": 2, "CT": 3}[self.chan_var.get()]
        self._send_pkt(pkt_rgbw(self.rgb_on.get(), self.w_on.get(), self.cct_on.get(), ch), "rgbw-state")

    def _send_pins(self):
        p1 = max(1, min(6, self._get_int(self.p1, 1)))
        p2 = max(1, min(6, self._get_int(self.p2, 2)))
        p3 = max(1, min(6, self._get_int(self.p3, 3)))
        self._send_pkt(pkt_pin_sequence(p1, p2, p3), "pins")

    def _set_color(self, r, g, b):
        self._color = (r, g, b)
        self._sync_color_ui()
        self._send_color()

    def _apply_hex(self):
        txt = self.hex_entry.get().strip().lstrip("#")
        if len(txt) == 6:
            try:
                r = int(txt[0:2], 16)
                g = int(txt[2:4], 16)
                b = int(txt[4:6], 16)
                self._set_color(r, g, b)
            except ValueError:
                pass

    def _hsl_changed(self):
        h, s, l = self.h_var.get(), self.s_var.get(), self.l_var.get()
        r, g, b = hsl_to_rgb(h, s, l)
        self._color = (r, g, b)
        self.r_var.set(r)
        self.g_var.set(g)
        self.b_var.set(b)
        self._update_color_labels()
        self._stage_color()
        self._live_color()

    def _rgb_changed(self):
        r, g, b = self.r_var.get(), self.g_var.get(), self.b_var.get()
        self._color = (r, g, b)
        h, s, l = rgb_to_hsl(r, g, b)
        self.h_var.set(h)
        self.s_var.set(s)
        self.l_var.set(l)
        self._update_color_labels()
        self._stage_color()
        self._live_color()

    def _update_color_labels(self):
        r, g, b = self._color
        hx = rgb_hex(r, g, b)
        self.c_prev.configure(fg_color=hx)
        self.c_hex.configure(text=hx)
        self.c_rgb.configure(text=f"{r}, {g}, {b}")
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, hx)
        self._hsl_hue_lbl.configure(text=f"{self.h_var.get():.0f}")
        self._hsl_sat_lbl.configure(text=f"{self.s_var.get():.0f}%")
        self._hsl_lit_lbl.configure(text=f"{self.l_var.get():.0f}%")
        self._rgb_r_lbl.configure(text=str(r))
        self._rgb_g_lbl.configure(text=str(g))
        self._rgb_b_lbl.configure(text=str(b))
        try:
            self.side_dot.configure(fg_color=hx)
        except Exception:
            pass
        try:
            self._cal_refresh_preview()
        except Exception:
            pass

    def _sync_color_ui(self):
        r, g, b = self._color
        self.r_var.set(r)
        self.g_var.set(g)
        self.b_var.set(b)
        h, s, l = rgb_to_hsl(r, g, b)
        self.h_var.set(h)
        self.s_var.set(s)
        self.l_var.set(l)
        self._update_color_labels()

    def _refresh_recent(self):
        for w in self.recent_frame.winfo_children():
            w.destroy()
        for c in self.cfg.recent_colors[:16]:
            r, g, b = c
            ctk.CTkButton(
                self.recent_frame, text="", width=24, height=20, corner_radius=4,
                fg_color=rgb_hex(r, g, b), hover_color=rgb_hex(r, g, b),
                command=lambda r=r, g=g, b=b: self._set_color(r, g, b),
            ).pack(side="left", padx=2, pady=2)

    # ════════════════════════════════════════════════════════════════
    # TUNE PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_tune(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        body = ctk.CTkScrollableFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Tune Colors", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(body, text="White looks blue? Lower Blue gain (try 70-85%) or tune White "
                                "below. Per-color sliders remap pure R/G/B/W. Edits stage until Save.",
                      text_color=MUTED, wraplength=700).pack(anchor="w", pady=(0, 8))

        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=(0, 8))

        self.cal_en = ctk.BooleanVar(value=self.cfg.cal_enabled)
        ctk.CTkCheckBox(card, text="Calibration enabled", variable=self.cal_en,
                         corner_radius=4, command=self._cal_changed).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(card, text="Reset", corner_radius=8, width=60,
                       fg_color="#443333", command=self._cal_reset).pack(anchor="e")
        ctk.CTkButton(card, text="Test White", corner_radius=8, width=80,
                       command=lambda: self._cal_test(255, 255, 255)).pack(anchor="e", pady=(0, 8))

        self._cal_vars: dict[str, ctk.DoubleVar] = {}
        self._cal_lbls: dict[str, ctk.CTkLabel] = {}
        for key, col in [("R", RED), ("G", GREEN), ("B", ACCENT)]:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=f"{key} gain", width=60, text_color=col,
                          font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            var = ctk.DoubleVar(value=getattr(self.cfg, f"cal_gain_{key.lower()}") * 100)
            ctk.CTkSlider(row, from_=20, to=200, variable=var, width=350,
                           command=lambda _: self._cal_changed()).pack(side="left", padx=8, fill="x", expand=True)
            lbl = ctk.CTkLabel(row, text=f"{var.get():.0f}%", width=50,
                                font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
            lbl.pack(side="left")
            self._cal_vars[key] = var
            self._cal_lbls[key] = lbl

        extra = ctk.CTkFrame(card, fg_color="transparent")
        extra.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(extra, text="Gamma", width=60, text_color=MUTED).pack(side="left")
        self._gamma_var = ctk.DoubleVar(value=self.cfg.cal_gamma)
        ctk.CTkSlider(extra, from_=0.3, to=3.0, variable=self._gamma_var, width=160,
                       command=lambda _: self._cal_changed()).pack(side="left", padx=4)
        self._gamma_lbl = ctk.CTkLabel(extra, text=f"{self.cfg.cal_gamma:.2f}", width=50,
                                        font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        self._gamma_lbl.pack(side="left")
        ctk.CTkLabel(extra, text="Warm \u2194 Cool", text_color=MUTED).pack(side="left", padx=(14, 4))
        self._temp_var = ctk.IntVar(value=self.cfg.cal_temp)
        ctk.CTkSlider(extra, from_=-100, to=100, variable=self._temp_var, width=140,
                       command=lambda _: self._cal_changed()).pack(side="left", padx=4)
        self._temp_lbl = ctk.CTkLabel(extra, text=str(self.cfg.cal_temp), width=40,
                                       font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        self._temp_lbl.pack(side="left")
        ctk.CTkLabel(extra, text="Order", text_color=MUTED).pack(side="left", padx=(14, 4))
        self._order_var = ctk.StringVar(value=self.cfg.cal_order)
        ctk.CTkOptionMenu(extra, variable=self._order_var, values=list(ORDERS),
                           width=70, corner_radius=8,
                           command=lambda _: self._cal_changed()).pack(side="left")

        # per-color calibration: full R/G/B sliders per target color.
        # Each row tunes what the strip receives for pure R/G/B/W requests.
        pcal = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        pcal.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(pcal, text="Per-color output (sliders or click swatch, Show tests on strip)",
                      text_color=MUTED, font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 6))
        self._prim_rows: dict[str, dict] = {}
        self._prim_vars: dict[str, dict[str, ctk.IntVar]] = {}
        self._prim_lbls: dict[str, dict[str, ctk.CTkLabel]] = {}
        for label, attr, pure, col in [
            ("Red", "prim_r", (255, 0, 0), RED),
            ("Green", "prim_g", (0, 255, 0), GREEN),
            ("Blue", "prim_b", (0, 0, 255), ACCENT),
            ("White", "white_pt", (255, 255, 255), FG),
        ]:
            box = ctk.CTkFrame(pcal, fg_color="transparent")
            box.pack(fill="x", pady=3)
            head = ctk.CTkFrame(box, fg_color="transparent")
            head.pack(fill="x")
            ctk.CTkLabel(head, text=label, width=50, text_color=col,
                          font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            cur = list(getattr(self.cfg, attr))
            sw = ctk.CTkLabel(head, text="", width=56, height=24,
                              fg_color=rgb_hex(*cur), corner_radius=6)
            sw.pack(side="left", padx=6)
            hx = ctk.CTkLabel(head, text=rgb_hex(*cur), width=70,
                              font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
            hx.pack(side="left")
            ctk.CTkButton(head, text="Show", corner_radius=8, width=56,
                           command=lambda p=pure: self._send_pkt(
                               pkt_color_static(*p), f"show {p}")).pack(side="left", padx=6)
            sw.bind("<Button-1>", lambda _e, a=attr: self._prim_pick(a))
            self._prim_rows[attr] = {"sw": sw, "hex": hx}
            self._prim_vars[attr] = {}
            self._prim_lbls[attr] = {}
            for comp, cname in (("r", "R"), ("g", "G"), ("b", "B")):
                srow = ctk.CTkFrame(box, fg_color="transparent")
                srow.pack(fill="x", pady=1)
                ctk.CTkLabel(srow, text=cname, width=44, text_color=MUTED,
                              font=ctk.CTkFont(size=11)).pack(side="left")
                var = ctk.IntVar(value=cur[0] if comp == "r" else (cur[1] if comp == "g" else cur[2]))
                ctk.CTkSlider(srow, from_=0, to=255, variable=var, width=280,
                               command=lambda _, a=attr: self._prim_slider(a)
                               ).pack(side="left", padx=8, fill="x", expand=True)
                lab = ctk.CTkLabel(srow, text=str(var.get()), width=36,
                                    font=ctk.CTkFont(family="Consolas", size=11),
                                    text_color=MUTED)
                lab.pack(side="left")
                self._prim_vars[attr][comp] = var
                self._prim_lbls[attr][comp] = lab
        ctk.CTkButton(pcal, text="Reset colors", corner_radius=8, width=100,
                       fg_color="#443333", command=self._prim_reset).pack(anchor="e", pady=(6, 0))

        # preview
        prev = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
        prev.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(prev, text="Preview (requested \u2192 sent)", text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 6))
        pr = ctk.CTkFrame(prev, fg_color="transparent")
        pr.pack(fill="x")
        self._prev_req = ctk.CTkLabel(pr, text="", width=100, height=40,
                                       fg_color=rgb_hex(*self._color), corner_radius=8)
        self._prev_req.pack(side="left", padx=4)
        ctk.CTkLabel(pr, text="\u2192", text_color=MUTED, font=ctk.CTkFont(size=18)).pack(side="left", padx=4)
        self._prev_out = ctk.CTkLabel(pr, text="", width=100, height=40,
                                       fg_color=rgb_hex(*self.cfg.cal_apply(*self._color)), corner_radius=8)
        self._prev_out.pack(side="left", padx=4)
        self._prev_txt = ctk.CTkLabel(pr, text="", font=ctk.CTkFont(family="Consolas", size=11),
                                       text_color=MUTED)
        self._prev_txt.pack(side="left", padx=8)
        trow = ctk.CTkFrame(prev, fg_color="transparent")
        trow.pack(fill="x", pady=(8, 0))
        for name, (tr, tg, tb) in [
            ("White", (255, 255, 255)), ("Gray", (128, 128, 128)),
            ("Red", (255, 0, 0)), ("Green", (0, 255, 0)), ("Blue", (0, 80, 255)),
            ("Yellow", (255, 220, 0)), ("Cyan", (0, 220, 220)), ("Purple", (170, 60, 255)),
        ]:
            b = ctk.CTkButton(
                trow, text="", width=34, height=24, corner_radius=6,
                fg_color=rgb_hex(tr, tg, tb), hover_color=rgb_hex(tr, tg, tb),
                command=lambda r=tr, g=tg, b=tb: self._cal_test(r, g, b),
            )
            b.pack(side="left", padx=2)
        self._cal_refresh_preview()

        return f

    def _cal_changed(self):
        self.cfg.cal_enabled = self.cal_en.get()
        for k in ("R", "G", "B"):
            setattr(self.cfg, f"cal_gain_{k.lower()}", max(0.2, min(2.0, self._cal_vars[k].get() / 100.0)))
            self._cal_lbls[k].configure(text=f"{self._cal_vars[k].get():.0f}%")
        self.cfg.cal_gamma = max(0.3, min(3.0, self._gamma_var.get()))
        self.cfg.cal_temp = max(-100, min(100, int(self._temp_var.get())))
        self.cfg.cal_order = self._order_var.get()
        self._gamma_lbl.configure(text=f"{self.cfg.cal_gamma:.2f}")
        self._temp_lbl.configure(text=str(self.cfg.cal_temp))
        self._mark_dirty("tune")
        self._cal_refresh_preview()
        if self.cfg.live_send:
            self._debounced("tune", 200,
                             lambda: self._send_current_color(f"tune preview RGB{self._color}"))

    def _send_current_color(self, what: str):
        self.cfg.last_sent = "color"
        r, g, b = self._color
        self._send_pkt(pkt_color_static(r, g, b), what)

    def _cal_refresh_preview(self):
        r, g, b = self._color
        cr, cg, cb = self.cfg.cal_apply(r, g, b)
        self._prev_req.configure(fg_color=rgb_hex(r, g, b))
        self._prev_out.configure(fg_color=rgb_hex(cr, cg, cb))
        self._prev_txt.configure(text=f"({r},{g},{b}) \u2192 ({cr},{cg},{cb})")

    def _cal_reset(self):
        self.cfg.cal_enabled = True
        self.cfg.cal_gain_r = 1.0
        self.cfg.cal_gain_g = 1.0
        self.cfg.cal_gain_b = 1.0
        self.cfg.cal_gamma = 1.0
        self.cfg.cal_temp = 0
        self.cfg.cal_order = "RGB"
        self.cal_en.set(True)
        for k in ("R", "G", "B"):
            self._cal_vars[k].set(100)
        self._gamma_var.set(1.0)
        self._temp_var.set(0)
        self._order_var.set("RGB")
        self._cal_changed()
        self._log("Calibration reset (unsaved — press Save to keep).")

    def _cal_test(self, r, g, b):
        self._set_color(r, g, b)

    def _prim_pick(self, attr: str):
        cur = list(getattr(self.cfg, attr, [255, 255, 255]))
        initial = rgb_hex(*cur)
        try:
            picked = colorchooser.askcolor(color=initial, title=f"Calibrate {attr}")
        except Exception as e:
            self._log(f"Picker: {e}")
            return
        if picked and picked[0]:
            r, g, b = (max(0, min(255, int(v))) for v in picked[0])
            setattr(self.cfg, attr, [r, g, b])
            self._mark_dirty("tune")
            self._prim_refresh_row(attr)
            self._cal_refresh_preview()
            self._log(f"{attr} -> #{r:02X}{g:02X}{b:02X} (unsaved — press Save to keep).")

    def _prim_slider(self, attr: str):
        try:
            v = self._prim_vars[attr]
            triple = [max(0, min(255, int(v[c].get()))) for c in ("r", "g", "b")]
            setattr(self.cfg, attr, triple)
            self._mark_dirty("tune")
            self._prim_refresh_row(attr)
            self._cal_refresh_preview()
            if self.cfg.live_send:
                self._debounced("tune", 200,
                                 lambda: self._send_current_color(f"tune live RGB{self._color}"))
        except Exception:
            pass

    def _prim_refresh_row(self, attr: str):
        try:
            r, g, b = getattr(self.cfg, attr)
            self._prim_rows[attr]["sw"].configure(fg_color=rgb_hex(r, g, b))
            self._prim_rows[attr]["hex"].configure(text=rgb_hex(r, g, b))
            for comp, val in (("r", r), ("g", g), ("b", b)):
                self._prim_vars[attr][comp].set(val)
                self._prim_lbls[attr][comp].configure(text=str(val))
        except Exception:
            pass

    def _prim_reset(self):
        self.cfg.prim_r = [255, 0, 0]
        self.cfg.prim_g = [0, 255, 0]
        self.cfg.prim_b = [0, 0, 255]
        self.cfg.white_pt = [255, 255, 255]
        self._mark_dirty("tune")
        for a in ("prim_r", "prim_g", "prim_b", "white_pt"):
            self._prim_refresh_row(a)
        self._cal_refresh_preview()
        self._log("Per-color calibration reset (unsaved).")

    # ════════════════════════════════════════════════════════════════
    # MODES PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_modes(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        ctk.CTkLabel(f, text="Modes", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 8))

        left = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.mode_list = ctk.CTkScrollableFrame(left, fg_color="transparent", height=400)
        self.mode_list.pack(fill="both", expand=True)
        self._mode_buttons: list[ctk.CTkButton] = []
        for i, m in enumerate(MODES):
            b = ctk.CTkButton(
                self.mode_list, text=f"{i:02d}  {m}", anchor="w",
                fg_color="transparent", hover_color=CARD_HOVER,
                text_color=FG,
                font=ctk.CTkFont(size=12), height=30, corner_radius=6,
                command=lambda idx=i: self._send_mode(idx),
            )
            b.pack(fill="x", pady=1)
            self._mode_buttons.append(b)

        right = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10, width=220)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        ctk.CTkLabel(right, text="Speed", text_color=MUTED).pack(anchor="w", pady=(8, 2))
        self.speed_var = ctk.IntVar(value=self.cfg.last_speed)
        self.speed_lbl = ctk.CTkLabel(right, text=str(self.cfg.last_speed),
                                       font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        ctk.CTkSlider(right, from_=0, to=100, variable=self.speed_var, width=180,
                       command=lambda _: self._speed_changed()).pack()
        self.speed_lbl.pack()
        ctk.CTkButton(right, text="Send Speed", corner_radius=8,
                       command=self._send_speed).pack(fill="x", pady=4)

        ctk.CTkLabel(right, text="Brightness", text_color=MUTED).pack(anchor="w", pady=(12, 2))
        self.mbri_var = ctk.IntVar(value=self.cfg.last_brightness)
        self.mbri_lbl = ctk.CTkLabel(right, text=str(self.cfg.last_brightness),
                                      font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        ctk.CTkSlider(right, from_=0, to=100, variable=self.mbri_var, width=180,
                       command=lambda _: self._mbri_changed()).pack()
        self.mbri_lbl.pack()
        ctk.CTkButton(right, text="Send Brightness", corner_radius=8,
                       command=lambda: self._send_pkt(pkt_brightness(self.mbri_var.get()), "mode-bri")).pack(fill="x", pady=4)

        return f

    def _send_mode(self, idx):
        self.cfg.last_mode = idx
        self.cfg.last_sent = "mode"
        self._mark_dirty("modes")
        self._send_pkt(pkt_mode(idx), f"mode {idx} {MODES[idx]}")

    def _speed_changed(self):
        v = self.speed_var.get()
        self.cfg.last_speed = v
        self._mark_dirty("modes")
        self.speed_lbl.configure(text=str(v))
        if self.cfg.live_send:
            self._debounced("speed", 200, lambda: self._send_speed(send_only=True))

    def _mbri_changed(self):
        v = self.mbri_var.get()
        self.cfg.last_brightness = v
        self._mark_dirty("modes")
        self.mbri_lbl.configure(text=str(v))
        if self.cfg.live_send:
            self._debounced("mbri", 200, lambda:
                self._send_pkt(pkt_brightness(self.mbri_var.get()), "mode-bri live"))

    def _send_speed(self, send_only=False):
        v = self.speed_var.get()
        if not send_only:
            self.cfg.last_speed = v
            self._mark_dirty("modes")
        self.speed_lbl.configure(text=str(v))
        self._send_pkt(pkt_mode_speed(v), f"speed {v}")

    # ════════════════════════════════════════════════════════════════
    # TIMING PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_timing(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        ctk.CTkLabel(f, text="Timing", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 8))

        self._t1_h = ctk.IntVar(value=self.cfg.t1_hour)
        self._t1_m = ctk.IntVar(value=self.cfg.t1_min)
        self._t1_on = ctk.BooleanVar(value=self.cfg.t1_on)
        self._t1_days = [ctk.BooleanVar(value=d) for d in self.cfg.t1_days]
        self._timer_card(f, "Timer 1", self._t1_h, self._t1_m, self._t1_on, self._t1_days, 0)

        self._t2_h = ctk.IntVar(value=self.cfg.t2_hour)
        self._t2_m = ctk.IntVar(value=self.cfg.t2_min)
        self._t2_on = ctk.BooleanVar(value=self.cfg.t2_on)
        self._t2_days = [ctk.BooleanVar(value=d) for d in self.cfg.t2_days]
        self._timer_card(f, "Timer 2", self._t2_h, self._t2_m, self._t2_on, self._t2_days, 1)

        ctk.CTkButton(f, text="Sync PC Clock to Strip", corner_radius=8, width=180,
                       command=self._sync_time).pack(anchor="w", pady=8)

        # ── PC-side schedules ──
        sh = ctk.CTkFrame(f, fg_color="transparent")
        sh.pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(sh, text="Schedules (PC-side — PC must be on)",
                      font=ctk.CTkFont(size=16, weight="bold"), text_color=FG).pack(side="left")
        ctk.CTkButton(sh, text="+ Add", corner_radius=8, width=70,
                       command=self._sched_add).pack(side="right")
        self.sched_list = ctk.CTkFrame(f, fg_color="transparent")
        self.sched_list.pack(fill="x")
        self._render_schedules()
        return f

    SCHED_ACTIONS = {"Power ON": "power_on", "Power OFF": "power_off",
                     "Send color": "color", "Send mode": "mode"}
    SCHED_ACTIONS_R = {v: k for k, v in SCHED_ACTIONS.items()}

    def _render_schedules(self):
        for w in self.sched_list.winfo_children():
            w.destroy()
        if not self.cfg.schedules:
            ctk.CTkLabel(self.sched_list, text="No schedules — add one to automate lights.",
                          text_color=MUTED).pack(anchor="w", pady=4)
            return
        for idx, s in enumerate(self.cfg.schedules):
            card = ctk.CTkFrame(self.sched_list, fg_color=CARD, corner_radius=10)
            card.pack(fill="x", pady=(0, 6))
            r1 = ctk.CTkFrame(card, fg_color="transparent")
            r1.pack(fill="x")
            en = ctk.BooleanVar(value=s.get("enabled", True))
            ctk.CTkCheckBox(r1, text="", variable=en, width=24, corner_radius=4,
                             command=lambda i=idx, v=en: self._sched_set(i, "enabled", bool(v.get()))
                             ).pack(side="left", padx=(2, 0))
            nm = ctk.StringVar(value=s.get("name", f"Schedule {idx+1}"))
            ctk.CTkEntry(r1, width=130, textvariable=nm, corner_radius=6,
                         placeholder_text="Name").pack(side="left", padx=4)
            nm.trace_add("write", lambda *a, i=idx, v=nm: self._sched_set(i, "name", v.get()))
            hv = ctk.StringVar(value=str(s.get("hour", 7)))
            mv = ctk.StringVar(value=str(s.get("min", 0)))
            ctk.CTkEntry(r1, width=36, textvariable=hv, corner_radius=6).pack(side="left")
            ctk.CTkLabel(r1, text=":", text_color=MUTED).pack(side="left")
            ctk.CTkEntry(r1, width=36, textvariable=mv, corner_radius=6).pack(side="left", padx=(0, 4))
            hv.trace_add("write", lambda *a, i=idx, v=hv: self._sched_time(i, "hour", v.get()))
            mv.trace_add("write", lambda *a, i=idx, v=mv: self._sched_time(i, "min", v.get()))
            av = ctk.StringVar(value=self.SCHED_ACTIONS_R.get(s.get("action", "power_on"), "Power ON"))
            ctk.CTkOptionMenu(r1, variable=av, values=list(self.SCHED_ACTIONS.keys()),
                              width=110, corner_radius=8,
                              command=lambda _c, i=idx, v=av: self._sched_set(
                                  i, "action", self.SCHED_ACTIONS[v.get()])).pack(side="left", padx=4)
            mdv = ctk.StringVar(value=str(s.get("mode", 10)))
            ctk.CTkEntry(r1, width=40, textvariable=mdv, corner_radius=6).pack(side="left")
            ctk.CTkLabel(r1, text="mode#", text_color=MUTED, font=ctk.CTkFont(size=10)).pack(side="left", padx=(2, 0))
            mdv.trace_add("write", lambda *a, i=idx, v=mdv: self._sched_time(i, "mode", v.get(), lo=0, hi=28))
            ctk.CTkButton(r1, text="✕", corner_radius=8, width=32,
                           fg_color="#443333", hover_color="#663333",
                           command=lambda i=idx: self._sched_del(i)).pack(side="right", padx=2)
            r2 = ctk.CTkFrame(card, fg_color="transparent")
            r2.pack(fill="x", pady=(2, 0))
            for di, dn in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
                dv = ctk.BooleanVar(value=bool(s.get("days", [True]*7)[di]))
                ctk.CTkCheckBox(r2, text=dn, variable=dv, width=40, corner_radius=4,
                                 font=ctk.CTkFont(size=10),
                                 command=lambda i=idx, d=di, v=dv: self._sched_day(i, d, bool(v.get()))
                                 ).pack(side="left", padx=1)

    def _sched_add(self):
        self.cfg.schedules.append(Config.blank_schedule(len(self.cfg.schedules) + 1))
        self._mark_dirty("timing")
        self._render_schedules()

    def _sched_del(self, idx):
        if 0 <= idx < len(self.cfg.schedules):
            del self.cfg.schedules[idx]
            self._mark_dirty("timing")
            self._render_schedules()

    def _sched_set(self, idx, key, val):
        if 0 <= idx < len(self.cfg.schedules):
            self.cfg.schedules[idx][key] = val
            self._mark_dirty("timing")

    def _sched_time(self, idx, key, val, lo=0, hi=59):
        try:
            v = max(0 if key != "hour" else 0, min(hi, int(float(val))))
            if key == "hour":
                v = max(0, min(23, v))
            if 0 <= idx < len(self.cfg.schedules):
                self.cfg.schedules[idx][key] = v
                self._mark_dirty("timing")
        except Exception:
            pass

    def _sched_day(self, idx, day, val):
        if 0 <= idx < len(self.cfg.schedules):
            days = list(self.cfg.schedules[idx].get("days", [True] * 7))
            if len(days) == 7:
                days[day] = val
                self.cfg.schedules[idx]["days"] = days
                self._mark_dirty("timing")

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
                self._log(f"Schedule error: {e}")

    def _fire_schedule(self, s):
        name = s.get("name", "Schedule")
        action = s.get("action", "power_on")
        if not self.ble.connected_addresses():
            self._log(f"Schedule '{name}': skipped (no device).")
            return
        self._log(f"Schedule '{name}' firing: {action}.")
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

    def _timer_card(self, parent, title, h_var, m_var, on_var, days_vars, slot):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=(0, 8))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                      text_color=FG).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row, text="H", text_color=MUTED).pack(side="left")
        ctk.CTkEntry(row, width=40, textvariable=h_var, corner_radius=6).pack(side="left", padx=2)
        ctk.CTkLabel(row, text="M", text_color=MUTED).pack(side="left")
        ctk.CTkEntry(row, width=40, textvariable=m_var, corner_radius=6).pack(side="left", padx=2)
        ctk.CTkCheckBox(row, text="ON", variable=on_var, corner_radius=4).pack(side="left", padx=8)
        for i, nm in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
            ctk.CTkCheckBox(row, text=nm, variable=days_vars[i], width=36,
                             corner_radius=4, font=ctk.CTkFont(size=10)).pack(side="left", padx=1)
        ctk.CTkButton(row, text="Send", corner_radius=8, width=50,
                       command=lambda: self._send_timer(h_var, m_var, on_var, days_vars, slot)).pack(side="left", padx=8)

    def _weeks(self, days_vars):
        w = 0
        for i, v in enumerate(days_vars):
            if v.get():
                w |= (1 << i)
        return w

    def _send_timer(self, h_var, m_var, on_var, days_vars, slot):
        h = max(0, min(23, self._get_int(h_var, 0)))
        m = max(0, min(59, self._get_int(m_var, 0)))
        on = bool(on_var.get())
        if slot == 0:
            self.cfg.t1_hour, self.cfg.t1_min, self.cfg.t1_on = h, m, on
            self.cfg.t1_days = [bool(v.get()) for v in days_vars]
        else:
            self.cfg.t2_hour, self.cfg.t2_min, self.cfg.t2_on = h, m, on
            self.cfg.t2_days = [bool(v.get()) for v in days_vars]
        self._mark_dirty("timing")
        self._send_pkt(pkt_timing(h, m, 0, self._weeks(days_vars), on, slot),
                        f"timer{slot+1} {h:02d}:{m:02d} {'on' if on else 'off'}")

    def _sync_time(self):
        now = datetime.now()
        wd = (now.weekday() + 1) % 7
        self._send_pkt(pkt_system_time(now.hour, now.minute, now.second, wd),
                        f"time {now.strftime('%H:%M:%S')}")

    # ════════════════════════════════════════════════════════════════
    # AMBILIGHT PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_ambi(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        ctk.CTkLabel(f, text="Ambilight", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(f, text="Screen average \u2192 realtime BLE packets. Calibration applied. "
                              "Center 50% is fast enough for 60fps; Full needs \u226425fps.",
                      text_color=MUTED, wraplength=700).pack(anchor="w", pady=(0, 8))

        row = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        row.pack(fill="x", pady=(0, 8))
        self.ambi_btn = ctk.CTkButton(row, text="Start Ambilight", corner_radius=8, width=130,
                                       fg_color=GREEN, hover_color="#2ea44f",
                                       command=self._toggle_ambi)
        self.ambi_btn.pack(side="left", padx=4)
        self._ambi_auto_var = ctk.BooleanVar(value=self.cfg.ambi_on_startup)
        ctk.CTkCheckBox(row, text="Start at app startup", variable=self._ambi_auto_var,
                         corner_radius=4, font=ctk.CTkFont(size=11),
                         command=self._ambi_auto_toggled).pack(side="left", padx=8)
        self._ambi_preview = ctk.CTkLabel(row, text="", width=60, height=36,
                                           fg_color="#000000", corner_radius=8)
        self._ambi_preview.pack(side="left", padx=12)
        self._ambi_stat = ctk.CTkLabel(row, text="idle",
                                        font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
        self._ambi_stat.pack(side="left", padx=8)

        mrow = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        mrow.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(mrow, text="Capture area", text_color=MUTED).pack(side="left", padx=(0, 6))
        self._ambi_mode_var = ctk.StringVar(
            value="Center 50% (fast)" if self.cfg.ambi_mode == "center" else "Full screen (slow)")
        ctk.CTkOptionMenu(mrow, variable=self._ambi_mode_var,
                           values=["Center 50% (fast)", "Full screen (slow)"],
                           width=170, corner_radius=8,
                           command=lambda _: self._ambi_refresh()).pack(side="left")

        srow = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        srow.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(srow, text="Sampling", text_color=MUTED).pack(side="left", padx=(0, 6))
        self._ambi_sample_var = ctk.StringVar(value=self.cfg.ambi_sample.capitalize())
        ctk.CTkOptionMenu(srow, variable=self._ambi_sample_var,
                           values=["Average", "Dominant", "Vibrant", "Brightest"],
                           width=130, corner_radius=8,
                           command=lambda _: self._ambi_refresh()).pack(side="left")
        ctk.CTkLabel(srow, text="avg=mean · dom=most common · vib=neon pop · bri=highlights",
                      text_color=MUTED, font=ctk.CTkFont(size=10)).pack(side="left", padx=8)

        prow = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        prow.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(prow, text="Presets", text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=(0, 8))
        for pname in ["Movie", "Game", "Chill", "Party"]:
            ctk.CTkButton(prow, text=pname, corner_radius=8, width=70,
                           fg_color=ACCENT_DIM,
                           command=lambda p=pname: self._ambi_preset(p)).pack(side="left", padx=2)

        cand = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        cand.pack(fill="x", pady=(0, 8))
        self._cand_sw: dict[str, ctk.CTkLabel] = {}
        for key, label in [("average", "avg"), ("dominant", "dom"),
                           ("vibrant", "vib"), ("brightest", "bri")]:
            box = ctk.CTkFrame(cand, fg_color="transparent")
            box.pack(side="left", padx=6)
            ctk.CTkLabel(box, text=label, text_color=MUTED,
                          font=ctk.CTkFont(size=10)).pack()
            sw = ctk.CTkLabel(box, text="", width=44, height=22,
                              fg_color="#000000", corner_radius=6)
            sw.pack()
            self._cand_sw[key] = sw

        grid = ctk.CTkFrame(f, fg_color=CARD, corner_radius=10)
        grid.pack(fill="x")
        self._ambi_vars: dict[str, ctk.DoubleVar] = {}
        self._ambi_lbls: dict[str, ctk.CTkLabel] = {}
        for i, (txt, key, lo, hi) in enumerate([
            ("FPS (1-60)", "fps", 1, 60),
            ("Smoothing", "smooth", 0.05, 1.0),
            ("Brightness", "brightness", 1, 100),
            ("Min Delta", "min_delta", 0, 30),
        ]):
            row = ctk.CTkFrame(grid, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=txt, width=80, text_color=MUTED).pack(side="left")
            var = ctk.DoubleVar(value=getattr(self.cfg, f"ambi_{key}"))
            ctk.CTkSlider(row, from_=lo, to=hi, variable=var, width=300,
                           command=lambda _: self._ambi_refresh()).pack(side="left", padx=8, fill="x", expand=True)
            lbl = ctk.CTkLabel(row, text=f"{var.get():.0f}", width=50,
                                font=ctk.CTkFont(family="Consolas", size=11), text_color=MUTED)
            lbl.pack(side="left")
            self._ambi_vars[key] = var
            self._ambi_lbls[key] = lbl

        return f

    def _ambi_mode(self) -> str:
        try:
            return "full" if self._ambi_mode_var.get().startswith("Full") else "center"
        except Exception:
            return "center"

    AMBI_PRESETS = {
        "Movie": {"fps": 30, "smooth": 0.25, "brightness": 80, "min_delta": 8,
                  "mode": "center", "sample": "average"},
        "Game": {"fps": 60, "smooth": 0.60, "brightness": 100, "min_delta": 3,
                 "mode": "center", "sample": "vibrant"},
        "Chill": {"fps": 15, "smooth": 0.15, "brightness": 60, "min_delta": 10,
                  "mode": "center", "sample": "average"},
        "Party": {"fps": 60, "smooth": 0.80, "brightness": 100, "min_delta": 2,
                  "mode": "center", "sample": "brightest"},
    }

    def _ambi_preset(self, name: str):
        p = self.AMBI_PRESETS.get(name)
        if not p:
            return
        self._ambi_vars["fps"].set(p["fps"])
        self._ambi_vars["smooth"].set(p["smooth"])
        self._ambi_vars["brightness"].set(p["brightness"])
        self._ambi_vars["min_delta"].set(p["min_delta"])
        self._ambi_mode_var.set("Center 50% (fast)" if p["mode"] == "center" else "Full screen (slow)")
        self._ambi_sample_var.set(str(p["sample"]).capitalize())
        self._ambi_refresh()
        self._log(f"Ambilight preset '{name}' staged (unsaved).")

    def _ambi_sample(self) -> str:
        try:
            s = self._ambi_sample_var.get().lower()
            return s if s in ("average", "dominant", "vibrant", "brightest") else "average"
        except Exception:
            return "average"

    def _ambi_refresh(self, save: bool = True):
        for k, lbl in self._ambi_lbls.items():
            v = self._ambi_vars[k].get()
            lbl.configure(text=f"{v:.0f}")
        if save:
            self.cfg.ambi_fps = max(1.0, min(60.0, float(self._ambi_vars["fps"].get())))
            self.cfg.ambi_smooth = float(self._ambi_vars["smooth"].get())
            self.cfg.ambi_brightness = int(self._ambi_vars["brightness"].get())
            self.cfg.ambi_min_delta = int(self._ambi_vars["min_delta"].get())
            self.cfg.ambi_mode = self._ambi_mode()
            self.cfg.ambi_sample = self._ambi_sample()
            self._mark_dirty("ambi")

    def _toggle_ambi(self):
        if self.ambilight.running:
            self.runner.submit(self.ambilight.stop())
            self.ambi_btn.configure(text="Start Ambilight", fg_color=GREEN, hover_color="#2ea44f")
            self._log("Ambilight stopped.")
            return
        if not self._targets():
            self._log("No device connected.")
            return
        try:
            import mss
            from PIL import Image
        except ImportError:
            self._log("Missing deps: pip install mss pillow")
            return
        self.ambilight.fps = max(1.0, min(60.0, self._ambi_vars["fps"].get()))
        self.ambilight.smooth = self._ambi_vars["smooth"].get()
        self.ambilight.brightness = int(self._ambi_vars["brightness"].get())
        self.ambilight.min_delta = int(self._ambi_vars["min_delta"].get())
        self.ambilight.capture_mode = self._ambi_mode()
        self.ambilight.sample_mode = self._ambi_sample()
        try:
            # start() needs the running loop: schedule it ON the loop thread
            self.runner.submit(self._ambi_begin_async()).result(timeout=10)
        except Exception as e:
            self._log(f"Ambilight failed to start: {e}")
            return
        self.ambi_btn.configure(text="Stop Ambilight", fg_color=RED, hover_color="#b91c1c")
        self._log(f"Ambilight started @ {self.ambilight.fps:.0f}fps "
                  f"({self.ambilight.capture_mode}/{self.ambilight.sample_mode})")
        self._ambi_tick()

    async def _ambi_begin_async(self):
        self.ambilight.start()

    def _ambi_auto_toggled(self):
        self.cfg.ambi_on_startup = bool(self._ambi_auto_var.get())
        self._save_keys({"ambi_on_startup": self.cfg.ambi_on_startup})
        self._log("Ambilight at startup " + ("ON." if self.cfg.ambi_on_startup else "OFF."))

    def _ambi_autostart(self, tries: int = 6):
        try:
            if not self.cfg.ambi_on_startup or self.ambilight.running:
                return
            if not self._targets():
                if tries > 0:
                    self.after(20000, lambda: self._ambi_autostart(tries - 1))
                else:
                    self._log("Ambilight autostart: no device, giving up.")
                return
            self._ensure_page("ambi")
            self._toggle_ambi()
        except Exception as e:
            self._log(f"Ambilight autostart: {e}")

    def _ambi_tick(self):
        if not self.ambilight.running:
            self._ambi_stat.configure(text="idle")
            self.ambi_btn.configure(text="Start Ambilight", fg_color=GREEN, hover_color="#2ea44f")
            return
        r, g, b = self.ambilight.last_color
        self._ambi_preview.configure(fg_color=rgb_hex(r, g, b))
        self._ambi_stat.configure(
            text=f"f={self.ambilight.frames} s={self.ambilight.sends} "
                 f"cap={self.ambilight.last_capture_ms:.1f}ms rgb=({r},{g},{b})")
        try:
            for key, sw in self._cand_sw.items():
                cr, cg, cb = self.ambilight.last_stats.get(key, (0, 0, 0))
                sw.configure(fg_color=rgb_hex(cr, cg, cb))
        except Exception:
            pass
        self.ambilight.fps = max(1.0, min(60.0, self._ambi_vars["fps"].get()))
        self.ambilight.smooth = self._ambi_vars["smooth"].get()
        self.ambilight.brightness = int(self._ambi_vars["brightness"].get())
        self.ambilight.min_delta = int(self._ambi_vars["min_delta"].get())
        self.ambilight.capture_mode = self._ambi_mode()
        self.ambilight.sample_mode = self._ambi_sample()
        self._ambi_refresh(save=False)
        self.after(500, self._ambi_tick)


    # ════════════════════════════════════════════════════════════════
    # SETTINGS PAGE
    # ════════════════════════════════════════════════════════════════
    def _page_settings(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self._content, fg_color="transparent")
        ctk.CTkLabel(f, text="Settings", font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=FG).pack(anchor="w", pady=(0, 8))
        body = ctk.CTkScrollableFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True)

        def _card(title):
            c = ctk.CTkFrame(body, fg_color=CARD, corner_radius=10)
            c.pack(fill="x", pady=(0, 8))
            ctk.CTkLabel(c, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                          text_color=MUTED).pack(anchor="w", pady=(0, 6))
            return c

        def _toggle(parent, text, initial, on_change):
            v = ctk.BooleanVar(value=initial)
            ctk.CTkCheckBox(parent, text=text, variable=v, corner_radius=4,
                             command=lambda: on_change(bool(v.get()))).pack(anchor="w", pady=2)
            return v

        # ── Startup ──
        sc = _card("Windows startup")
        self._startup_var = ctk.BooleanVar(value=startup_enabled())
        ctk.CTkCheckBox(sc, text="Start app with Windows", variable=self._startup_var,
                         corner_radius=4,
                         command=self._startup_toggled).pack(anchor="w", pady=2)
        _toggle(sc, "Start minimized to tray", self.cfg.start_minimized,
                lambda on: (setattr(self.cfg, "start_minimized", on),
                            self._save_keys({"start_minimized": on}),
                            self._refresh_startup_cmd()))

        # ── Bluetooth ──
        bc = _card("Bluetooth")
        self.bt_status_lbl = ctk.CTkLabel(bc, text="Bluetooth: checking ...", text_color=MUTED,
                                           font=ctk.CTkFont(size=12, weight="bold"))
        self.bt_status_lbl.pack(anchor="w", pady=(0, 4))
        _toggle(bc, "Try to enable Bluetooth automatically at startup", self.cfg.bt_on_startup,
                lambda on: (setattr(self.cfg, "bt_on_startup", on),
                            self._save_keys({"bt_on_startup": on})))
        brow = ctk.CTkFrame(bc, fg_color="transparent")
        brow.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(brow, text="Refresh", corner_radius=8, width=80,
                       command=self._bt_refresh_threaded).pack(side="left", padx=2)
        ctk.CTkButton(brow, text="Turn on now", corner_radius=8, width=100,
                       command=lambda: threading.Thread(
                           target=self._bt_try_enable, daemon=True).start()).pack(side="left", padx=2)
        ctk.CTkButton(brow, text="BT Settings", corner_radius=8, width=100,
                       fg_color="transparent", border_width=1,
                       command=open_bluetooth_settings).pack(side="left", padx=2)

        # ── Startup actions ──
        ac = _card("After launch")
        _toggle(ac, "Auto-connect remembered devices", self.cfg.auto_connect,
                lambda on: (setattr(self.cfg, "auto_connect", on),
                            self._save_keys({"auto_connect": on})))
        _toggle(ac, "Auto-reconnect if connection drops", self.cfg.auto_reconnect,
                lambda on: (setattr(self.cfg, "auto_reconnect", on),
                            self._save_keys({"auto_reconnect": on})))
        _toggle(ac, "Sync PC clock to strip after connect", self.cfg.sync_time_on_startup,
                lambda on: (setattr(self.cfg, "sync_time_on_startup", on),
                            self._save_keys({"sync_time_on_startup": on})))

        # ── Window ──
        wc = _card("Window")
        ctk.CTkLabel(wc, text="Closing the window minimizes to the system tray.",
                      text_color=MUTED, wraplength=650).pack(anchor="w", pady=(0, 2))
        _toggle(wc, "Close button quits the app instead (skip tray)", self.cfg.quit_on_close,
                lambda on: (setattr(self.cfg, "quit_on_close", on),
                            self._save_keys({"quit_on_close": on})))

        # ── Remembered ──
        rc = _card("Remembered devices")
        self._remembered_lbl = ctk.CTkLabel(
            rc, text=self._remembered_text(), text_color=MUTED, wraplength=650)
        self._remembered_lbl.pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(rc, text="Forget remembered", corner_radius=8, width=140,
                       fg_color="#443333",
                       command=self._forget_remembered).pack(anchor="w")

        # ── Config file ──
        cc = _card("Config file")
        ctk.CTkLabel(cc, text=str(CONFIG_PATH), text_color=MUTED,
                      font=ctk.CTkFont(family="Consolas", size=11)).pack(anchor="w", pady=(0, 4))
        crow = ctk.CTkFrame(cc, fg_color="transparent")
        crow.pack(fill="x")
        ctk.CTkButton(crow, text="Open folder", corner_radius=8, width=100,
                       command=self._open_config_folder).pack(side="left", padx=2)
        ctk.CTkButton(crow, text="Save now", corner_radius=8, width=100,
                       command=self._save_all).pack(side="left", padx=2)
        return f

    def _refresh_startup_cmd(self):
        try:
            if startup_enabled():
                set_startup(True, minimized=self.cfg.start_minimized)
        except Exception as e:
            self._log(f"Startup update: {e}")

    def _startup_toggled(self):
        try:
            set_startup(bool(self._startup_var.get()), minimized=self.cfg.start_minimized)
            self._log("Start with Windows " + ("ON." if self._startup_var.get() else "OFF."))
        except Exception as e:
            self._log(f"Startup: {e}")
            try:
                self._startup_var.set(startup_enabled())
            except Exception:
                pass

    def _remembered_text(self):
        if not self.cfg.last_addresses:
            return "Remembered: none (connect a device to remember it)"
        parts = [f"{self.cfg.names.get(a, '?')} {a}" for a in self.cfg.last_addresses]
        return "Remembered: " + "  ·  ".join(parts)

    def _forget_remembered(self):
        self.cfg.last_addresses.clear()
        self._save_keys({"last_addresses": []})
        try:
            self._remembered_lbl.configure(text=self._remembered_text())
            self._refresh_devices()
            self._refresh_groups()
        except Exception:
            pass
        self._log("Forgot remembered devices.")

    def _open_config_folder(self):
        try:
            import os as _os
            _os.startfile(str(CONFIG_PATH.parent))  # type: ignore[attr-defined]
        except Exception as e:
            self._log(f"Open folder: {e}")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
