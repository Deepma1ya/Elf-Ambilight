Elf-Ambilight — modern PC control for BLE LED strips (Python + customtkinter)
==============================================================================

RUN:
  Elf-Ambilight.exe         (in E:\RGB PC PORT) — double-click, no install needed
  or:  python app.py        (needs: pip install -r requirements.txt)

SETTINGS FILE:
  %appdata%\Elf-Ambilight\config.json  (auto-created on first launch;
  an old config.json next to the app is migrated automatically once).
  Groups, device names, last connected devices, calibration, last color /
  brightness / mode / speed / last-sent, recents, timers, schedules,
  ambilight, flags. Delete it to reset to defaults.

REALTIME + SAVE:
  Sliders send to the strip LIVE while you drag (toggle "Live" on Color page).
  Edits are staged until you press Save (sidebar, or Ctrl+S).
  Switching tabs (or closing) with unsaved changes pops up Save / Discard /
  Cancel. Discard restores the full saved light state on the strip
  (brightness + color or mode/speed, whichever was showing).

PAGES (left sidebar, click the color dot anytime to jump to Color):
  Devices, Color, Tune, Modes, Timer, Ambi, Setup — see previous README
  sections (kept): groups, HSL/RGB/hex/presets/recents, per-color R/G/B
  sliders + matrix calibration, 29 modes, on-board timers + PC schedules,
  multi-mode ambilight + presets, startup/BT/tray settings.

AMBILIGHT: fixed start (was silently dead — create_task from UI thread),
  plus "Start at app startup" checkbox on the Ambi page (retries until a
  device is present, ~2 min).

BLUETOOTH OFF: sidebar BT / BT! / BT? + prompt dialog.
TRAY: close always minimizes to tray (opt-out in Setup). --minimized works.
STARTUP: Setup writes HKCU...\Run ("Elf-Ambilight").

BLE: service 0000fff0 / char 0000fff3, 9-byte packets 7E...EF, ELK-* auto-encrypt.

SOURCE: Elf-Ambilight\  BUILD: Elf-Ambilight\build_exe.bat
GIT:    https://github.com/Deepma1ya/Elf-Ambilight.git
