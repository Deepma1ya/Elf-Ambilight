# Changelog

All notable changes to Elf-Ambilight are documented here.
This project follows [Semantic Versioning](https://semver.org/) and the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [1.5.0] — 2026-10-28

### Added

- **Nine more Studio animations** — 14 in total: Rainbow wave, Aurora drift,
  Twinkle sparks, Candle glow, Police lights, Lightning storm, Heartbeat,
  Disco party and Sunrise join the original five.
- **Animation speed slider** (0.25×–3×). Tempo is scaled live and is
  independent of FPS. FPS and brightness now also apply live while playing,
  and all three persist between launches.
- Playback controls moved below the Studio tabs, so they are visible on both
  the File and Animations tabs.

### Changed

- Animation timing uses real elapsed time, so effects keep the same tempo at
  any FPS, including after changing speed mid-playback.

## [1.4.0] — 2026-10-28

### Added

- **One-click FFmpeg in Studio** — downloads the official static Windows build
  (~80 MB), installs `ffmpeg.exe` under
  `%LOCALAPPDATA%\Elf-Ambilight\ffmpeg`, verifies it runs, and appends that
  folder to your **user PATH**. No admin or UAC required, with live download
  progress in the card. If FFmpeg is already on the PC but missing from PATH,
  the same button becomes **Add to PATH**.

### Fixed

- **Studio animations sent no data to the strip.** `pkt_color_realtime` was
  never imported in the backend, so every frame raised a silently swallowed
  `NameError` and no BLE write was ever scheduled. Verified now by the frozen
  self-test (`media_pkt`, `media_frames`, `media_send`).
- **Studio output can no longer flood the BLE stack.** Frames are coalesced
  through a single drain at ~30 Hz with at most one write in flight (latest
  frame wins), matching Ambilight's proven pacing. 60 fps media stays smooth
  instead of queueing minutes of stale packets; write failures are logged
  instead of ignored.
- **Now-playing preview lag.** Progress was pushed at 2 Hz and polled every
  450 ms, so the preview jumped between colours. Ticks now run at ~12 Hz,
  the bridge polls every ~90 ms while Studio is playing, only the preview
  element is updated, and colours cross-fade over 90 ms.

## [1.3.0] — 2026-10-28

The big one: the desktop window is now an embedded WebView2 interface, with
Studio playback, per-app Ambilight rules and a rebuilt color engine.

### Added

- **Studio** page — stream media to the strip continuously: video (MP4, MKV,
  AVI, MOV, WebM, M4V, FLV, WMV via the system FFmpeg), animated GIF / WebP /
  APNG, still images, and five built-in generators (Rainbow sweep, Breathing
  pulse, Colour chase, Fire flicker, Ocean wave). FPS (1–60), brightness,
  loop and a live output preview with frame counter.
- **Per-app Ambilight rules** — start Ambilight automatically when a chosen
  app runs (VLC, Media Player, a game, anything with a window or typed exe
  name), optionally stop it when that app closes. Pick a preset per rule;
  enable, edit or delete rules inline. Checked every 2 s with a native
  process snapshot, edge-triggered so it never fights manual control.
- **Connected now** device list, including strips connected automatically,
  with per-device Add and Disconnect.
- Live colour wheel: colours stream to the strip while dragging, no Send
  click, plus eyedropper, harmony swatches, saved favourites, scenes, random
  colour and screen-match.
- Command palette (`Ctrl+K`), keyboard shortcuts (`Alt+1–8`), and
  Light / Dark / System themes stored in the config.
- Per-channel gamma, white-balance presets and named tune profiles.
- Animated sidebar navigation and page transitions, with reduced-motion
  support.

### Changed

- The UI is now a single embedded WebView2 window (pywebview); no external
  browser is ever opened.
- New bulb app icon with rounded corners, rendered at every Windows size
  (16–256 px).
- Ambilight hides the app window while syncing by default (toggle in
  Capture) so the strip never mirrors its own UI, and seeds the first frame
  from screen content.
- Studio playback and Ambilight are mutually exclusive: starting one stops
  the other, and both share the same calibration and target pipeline.
- BLE writes are throttled and state pushes are batched, so the UI stays
  smooth while lighting is live.

### Fixed

- **High priority startup switch reverted to off after approving UAC.** The
  task check was cached for five minutes, so the post-elevation refresh read
  the pre-UAC answer; the cache sentinel also returned a bogus "off" during
  the first five minutes of Windows uptime, exactly when an autostart app
  runs. Task state is now force-refreshed after elevation, the switch holds
  its position with a "Waiting for Windows approval…" hint, and UAC denial
  or `schtasks` errors are reported instead of failing silently.
- Task creation now retries, and the scheduled task is created before the
  Run entry is removed, so a failed elevation can no longer leave the app
  with no autostart at all.
- Turning "Start with Windows" off no longer re-adds the Run entry from the
  elevated helper.
- Discard now restores the saved colour and brightness on the strip, not
  just in the UI.
- Colour-wheel drag previews no longer flood the recently-used list.
- Ambilight no longer captures the app's own window colours.
- Auto-connected strips now appear on the Devices page immediately.

## [1.1.0] — 2026-09-15

### Added

- PC Lighting as a virtual strip, with Ambilight monitor selection.
- Windows Dynamic Lighting mirror (experimental LampArray support).
- Groups that work: per-light add/remove, checkboxes, Add checked.
- Cinematic theme, glowing colour wheel and ambient bias-glow.
- High-priority startup via an ONLOGON Task Scheduler entry with UAC
  auto-elevation.
- Group rename, save fixes and the version shown in title bars.

### Fixed

- Group membership and save behaviour when editing multiple lights.

## [1.0.0] — 2026-09-15

### Added

- First release: Windows BLE LED strip controller with screen-sync Ambient
  lighting, colour calibration, schedules and a system-tray menu.
- Single-file EXE, no installer required.

[1.5.0]: https://github.com/Deepma1ya/Elf-Ambilight/releases/tag/v1.5.0
[1.4.0]: https://github.com/Deepma1ya/Elf-Ambilight/releases/tag/v1.4.0
[1.3.0]: https://github.com/Deepma1ya/Elf-Ambilight/releases/tag/v1.3.0
[1.1.0]: https://github.com/Deepma1ya/Elf-Ambilight/releases/tag/v1.1.0
[1.0.0]: https://github.com/Deepma1ya/Elf-Ambilight/releases/tag/v1.0.0
