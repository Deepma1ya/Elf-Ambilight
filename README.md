# Elf-Ambilight

Modern Windows control for BLE LED strips — colors, effects, schedules, and real-time screen-sync ambient lighting. Embedded browser UI (WebView2, no external browser), single EXE, no install needed.

**Download:** [Latest release](https://github.com/Deepma1ya/Elf-Ambilight/releases/latest) — grab `Elf-Ambilight.exe` and double-click.

## Features

- **Devices** — BLE scan/connect, named groups, auto-connect and auto-reconnect on startup.
- **Colors** — interactive HSV wheel, eyedropper, harmony swatches, hex/RGB/HSL, 24 presets, scenes, live-send while dragging.
- **Calibration** — guided white-balance helper, per-channel gains, gamma, temperature, saturation/ceiling, channel order, per-primary matrix calibration with live preview, and named profiles.
- **Effects** — 29 built-in strip effects with searchable categories, brightness and speed control.
- **Command palette** — `Ctrl+K` to jump to any page, run actions, or apply presets; `Ctrl+S` saves, `Alt+1–7` switches pages.
- **Schedule** — on-board strip timers plus PC-side schedules (power, color, mode).
- **Ambilight** — screen-sync lighting with center/full capture, average/dominant/vibrant/brightest sampling, exact-duration crossfade (0–10 s), built-in and custom presets, and an idle eco mode. The window hides while syncing so the strip never mirrors its own UI (toggle in Capture). Capture runs at 5 Hz in a background thread with a capture-free fade loop, so Windows stays smooth while it runs.
- **Settings** — launch at startup, Bluetooth helpers, system-tray behavior, and a Light / Dark / System theme switch.
- **Windows Dynamic Lighting (experimental)** — mirror the strip color to PC gear (motherboard / RAM / keyboard / mouse) via the LampArray API. Enable in Setup. One app drives each lamp, so yield lighting in vendor apps first (e.g. G Hub: enable its Windows Dynamic Lighting support) — no need to close them.

Edits are staged until you press **Save** (sidebar, or `Ctrl+S`). Switching tabs or closing with unsaved changes offers Save / Discard / Cancel, and Discard restores the strip's last saved state.

## Quick start

1. Download `Elf-Ambilight.exe` from [Releases](https://github.com/Deepma1ya/Elf-Ambilight/releases/latest).
2. Double-click to run (first launch creates `%appdata%\Elf-Ambilight\config.json`).
3. On the **Devices** page, scan and connect your strip, then add it to a group.

Supported strips advertise as `ELK-*`, `LED LIGHT STRIP`, `XSL-*`, or `CLK-*`.

## Build from source

Requires Python 3.12+ on Windows 10/11 with Bluetooth LE.

```bat
pip install -r requirements.txt
python webapp.py
```

To build the EXE (same command the release uses):

```bat
build_exe.bat
```

Output: `dist\Elf-Ambilight.exe`.

## Project structure

| File | Purpose |
|---|---|
| `webapp.py` | WebUI backend — pywebview window, JS bridge, tray, schedules, all logic |
| `webui/` | Embedded frontend — `index.html` + `styles.css` + `app.js` (Neon OLED theme) |
| `app.py` | legacy customtkinter UI (kept as fallback; the release EXE uses the WebUI) |
| `ambilight.py` | Screen capture (dxcam/mss), sampling, time-based crossfade engine |
| `protocol.py` | BLE protocol — 9-byte `7E…EF` packets, 29 modes, ELK auto-encrypt |
| `ble_manager.py` | Bleak connection manager (scan, connect, throttled writes) |
| `config.py` | Settings in `%appdata%\Elf-Ambilight\config.json` (migrates legacy copies) |
| `bt_helper.py` | Bluetooth service check / enable helpers |
| `startup_helper.py` | `HKCU\...\Run` autostart entry |
| `single_instance.py` | Named-mutex singleton guard |
| `icon.ico` | App icon (window, tray, taskbar, EXE) |

## Protocol notes

- Service `0000fff0`, characteristic `0000fff3`, 9-byte packets framed `7E…EF`.
- `ELK-*` devices auto-encrypt payloads (see `protocol.py`).
- Realtime color packets are write-without-response for minimal latency.

## Settings

Everything persists in `%appdata%\Elf-Ambilight\config.json` — groups, device names, calibration, last color/brightness/mode, recents, timers, schedules, ambilight, and flags. Delete it to reset to defaults.
