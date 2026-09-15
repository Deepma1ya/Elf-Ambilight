"""All settings in %appdata%/Elf-Ambilight/config.json (migrates legacy copies)."""
from __future__ import annotations
import json
import os
import shutil
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_DIR = Path(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
) / "Elf-Ambilight"
CONFIG_PATH = APP_DIR / "config.json"


def _legacy_paths() -> list[Path]:
    """Older locations of config.json (app folder / exe folder)."""
    here = Path(__file__).resolve().parent
    paths = [here / "config.json"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent / "config.json"
        if exe_dir != paths[0]:
            paths.append(exe_dir)
    return paths


def migrate_legacy_config():
    """Copy the first legacy config.json found into the appdata folder."""
    try:
        if CONFIG_PATH.exists():
            return
        for legacy in _legacy_paths():
            try:
                if legacy.exists() and legacy.is_file():
                    APP_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(legacy, CONFIG_PATH)
                    return
            except Exception:
                continue
    except Exception:
        pass

ORDERS = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")


@dataclass
class Config:
    # groups
    groups: dict[str, list[str]] = field(default_factory=lambda: {"Default": []})
    selected_group: str = "Default"
    names: dict[str, str] = field(default_factory=dict)

    # connection
    auto_connect: bool = True
    auto_reconnect: bool = True
    last_addresses: list[str] = field(default_factory=list)

    # calibration
    cal_enabled: bool = True
    cal_gain_r: float = 1.0
    cal_gain_g: float = 1.0
    cal_gain_b: float = 1.0
    cal_gamma: float = 1.0
    cal_temp: int = 0
    cal_order: str = "RGB"
    # per-color primaries: RGB triples the strip must receive for pure
    # red / green / blue requests (pick until the strip looks right).
    prim_r: list[int] = field(default_factory=lambda: [255, 0, 0])
    prim_g: list[int] = field(default_factory=lambda: [0, 255, 0])
    prim_b: list[int] = field(default_factory=lambda: [0, 0, 255])
    # white point: exact triple sent for a white request.
    white_pt: list[int] = field(default_factory=lambda: [255, 255, 255])

    # last UI state
    last_r: int = 255
    last_g: int = 255
    last_b: int = 255
    last_brightness: int = 100
    last_mode: int = 10
    last_speed: int = 50
    last_sent: str = "color"  # what the strip shows: "color" | "mode"
    recent_colors: list[list[int]] = field(default_factory=list)

    # UI prefs
    quit_on_close: bool = False  # False = close minimizes to tray
    start_minimized: bool = False
    live_send: bool = True

    # startup behavior
    bt_on_startup: bool = False
    sync_time_on_startup: bool = False
    ambi_on_startup: bool = False

    # PC-side schedules: [{name,hour,min,days[7 bool],action,mode,enabled}]
    # action: "power_on" | "power_off" | "color" | "mode"
    schedules: list[dict] = field(default_factory=list)

    # ambilight
    ambi_fps: float = 30.0
    ambi_smooth: float = 0.35
    ambi_brightness: int = 100
    ambi_min_delta: int = 6
    ambi_mode: str = "center"  # "center" or "full" — both 60fps via low-res
    ambi_sample: str = "average"  # average | dominant | vibrant | brightest
    ambi_interval: float = 0.1  # fade window / update every (s), now 0.01-2.0
    ambi_crossfade: bool = True  # smooth fade at FPS rate vs instant jump
    ambi_use_dxcam: bool = True  # DXGI Desktop Duplication (GPU, ~15ms) vs mss (CPU, ~50ms full)

    # timers
    t1_hour: int = 7
    t1_min: int = 0
    t1_on: bool = True
    t1_days: list[bool] = field(default_factory=lambda: [True] * 7)
    t2_hour: int = 22
    t2_min: int = 0
    t2_on: bool = True
    t2_days: list[bool] = field(default_factory=lambda: [True] * 7)

    def targets(self) -> list[str]:
        return list(self.groups.get(self.selected_group, []))

    def remember_name(self, addr: str, name: str):
        if name:
            self.names[addr] = name

    def touch_last(self, addr: str):
        if addr in self.last_addresses:
            self.last_addresses.remove(addr)
        self.last_addresses.insert(0, addr)
        self.last_addresses = self.last_addresses[:8]

    def forget_last(self, addr: str):
        if addr in self.last_addresses:
            self.last_addresses.remove(addr)

    def add_recent(self, r: int, g: int, b: int):
        self.recent_colors = [[r, g, b]] + [
            c for c in self.recent_colors if c != [r, g, b]
        ][:15]

    @staticmethod
    def _rgb3(v) -> tuple[int, int, int]:
        try:
            r, g, b = (max(0, min(255, int(x))) for x in list(v)[:3])
            return r, g, b
        except Exception:
            return 255, 255, 255

    def _matrix_apply(self, r: int, g: int, b: int) -> tuple[float, float, float]:
        """Primary-matrix + white-balance. Identity when primaries are pure
        and white_pt is (255,255,255), so old configs behave identically."""
        Rp = self._rgb3(self.prim_r)
        Gp = self._rgb3(self.prim_g)
        Bp = self._rgb3(self.prim_b)
        Wp = self._rgb3(self.white_pt)
        out = [0.0, 0.0, 0.0]
        for c in range(3):
            out[c] = (r * Rp[c] + g * Gp[c] + b * Bp[c]) / 255.0
            col_sum = Rp[c] + Gp[c] + Bp[c]
            out[c] *= (Wp[c] / col_sum) if col_sum > 0 else 1.0
        return out[0], out[1], out[2]

    def cal_apply(self, r: int, g: int, b: int, *, apply_order: bool = True) -> tuple[int, int, int]:
        if not self.cal_enabled:
            return max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
        rf, gf, bf = self._matrix_apply(r, g, b)
        # gains / temp / gamma on logical channels (before wire order)
        r1 = rf * max(0.2, min(2.0, self.cal_gain_r))
        g1 = gf * max(0.2, min(2.0, self.cal_gain_g))
        b1 = bf * max(0.2, min(2.0, self.cal_gain_b))
        t = max(-100, min(100, self.cal_temp)) / 100.0
        if t > 0:
            r1 *= 1.0 + 0.25 * t
            b1 *= 1.0 - 0.20 * t
        elif t < 0:
            r1 = max(0, r1 * (1.0 + 0.20 * t))
            b1 *= 1.0 - 0.25 * t
        gm = max(0.3, min(3.0, self.cal_gamma))
        if abs(gm - 1.0) > 1e-6:
            r1 = 255.0 * ((max(0, r1) / 255.0) ** gm)
            g1 = 255.0 * ((max(0, g1) / 255.0) ** gm)
            b1 = 255.0 * ((max(0, b1) / 255.0) ** gm)
        if not apply_order:
            def cl(v):
                return max(0, min(255, int(round(v))))
            return cl(r1), cl(g1), cl(b1)
        ch = {"R": r1, "G": g1, "B": b1}
        o = self.cal_order.upper() if self.cal_order.upper() in ORDERS else "RGB"
        r0, g0, b0 = ch[o[0]], ch[o[1]], ch[o[2]]
        def cl(v):
            return max(0, min(255, int(round(v))))
        return cl(r0), cl(g0), cl(b0)

    def save(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _clean_schedule(s: dict) -> dict:
        days = s.get("days", [True] * 7)
        if not isinstance(days, list) or len(days) != 7:
            days = [True] * 7
        days = [bool(d) for d in days]
        action = s.get("action", "power_on")
        if action not in ("power_on", "power_off", "color", "mode"):
            action = "power_on"
        return {
            "name": str(s.get("name", "Schedule")),
            "hour": max(0, min(23, int(s.get("hour", 7)))),
            "min": max(0, min(59, int(s.get("min", 0)))),
            "days": days,
            "action": action,
            "mode": max(0, min(28, int(s.get("mode", 10)))),
            "enabled": bool(s.get("enabled", True)),
        }

    @staticmethod
    def blank_schedule(n: int) -> dict:
        return Config._clean_schedule({"name": f"Schedule {n}"})

    @classmethod
    def load(cls) -> "Config":
        migrate_legacy_config()
        try:
            if CONFIG_PATH.exists():
                d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                c = cls()
                for k, v in d.items():
                    if hasattr(c, k):
                        setattr(c, k, v)
                if not c.groups:
                    c.groups = {"Default": []}
                if c.selected_group not in c.groups:
                    c.selected_group = next(iter(c.groups))
                c.recent_colors = [c for c in c.recent_colors if isinstance(c, list) and len(c) == 3]
                c.schedules = [cls._clean_schedule(s) for s in (c.schedules or []) if isinstance(s, dict)]
                for attr, default in (("prim_r", (255, 0, 0)), ("prim_g", (0, 255, 0)),
                                      ("prim_b", (0, 0, 255)), ("white_pt", (255, 255, 255))):
                    v = getattr(c, attr, default)
                    try:
                        v = [max(0, min(255, int(x))) for x in list(v)[:3]]
                        if len(v) != 3:
                            raise ValueError
                    except Exception:
                        v = list(default)
                    setattr(c, attr, v)
                # migrate legacy close_to_tray (True=to tray) to quit_on_close
                if "close_to_tray" in d and "quit_on_close" not in d:
                    try:
                        c.quit_on_close = not bool(d["close_to_tray"])
                    except Exception:
                        pass
                if getattr(c, "ambi_mode", "center") not in ("center", "full"):
                    c.ambi_mode = "center"
                if getattr(c, "ambi_sample", "average") not in (
                        "average", "dominant", "vibrant", "brightest"):
                    c.ambi_sample = "average"
                if getattr(c, "last_sent", "color") not in ("color", "mode"):
                    c.last_sent = "color"
                try:
                    c.ambi_fps = max(1.0, min(60.0, float(c.ambi_fps)))
                except Exception:
                    c.ambi_fps = 30.0
                try:
                    c.ambi_interval = max(0.01, min(5.0, float(c.ambi_interval)))
                except Exception:
                    c.ambi_interval = 0.1
                try:
                    c.ambi_crossfade = bool(c.ambi_crossfade)
                except Exception:
                    c.ambi_crossfade = True
                try:
                    c.ambi_use_dxcam = bool(c.ambi_use_dxcam)
                except Exception:
                    c.ambi_use_dxcam = True
                return c
        except Exception:
            pass
        return cls()
