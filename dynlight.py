"""Windows Dynamic Lighting (HID LampArray) mirror — experimental.

Mirrors the strip colour onto motherboard / RAM / keyboard / mouse lighting
via the WinRT LampArray API (the same devices Windows Settings > Dynamic
Lighting controls).

Hard lessons from probing (documented so nobody "simplifies" this away):
- `LampArray.from_id_async()` can block a thread *synchronously* inside the
  native call — `asyncio.wait_for` can NOT cancel it. Every open therefore
  runs in a short-lived daemon thread joined with a timeout; stuck opens are
  abandoned, never awaited.
- HID stacks wedge when several apps (G Hub, Dynamic Lighting background
  controller, us) open the same collections — per-device errors drop that
  device, never the whole feature.
- The steady-state colour path must NEVER block the caller (ambilight loop):
  `push()` only enqueues the latest colour; one persistent worker thread owns
  all LampArray objects and applies at most every 100ms.

Nothing here may ever raise out of the public methods.
"""
from __future__ import annotations
import queue
import threading
import time

try:
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.lights import LampArray
    from winrt.windows.ui import ColorHelper
    _HAVE_WINRT = True
except Exception:
    DeviceInformation = None  # type: ignore
    LampArray = None  # type: ignore
    ColorHelper = None  # type: ignore
    _HAVE_WINRT = False

# LampArray HID interface-class GUID — the AQS-selector overload is not
# projected, so enumeration lists everything and filters on this substring.
_LAMP_IFACE_GUID = "4d1e55b2"
_OPEN_TIMEOUT = 5.0      # abandon stuck opens after this (daemon thread leaks are fine)
_PUSH_MIN_GAP = 0.10     # at most 10 HID writes/sec per worker


def winrt_available() -> bool:
    return _HAVE_WINRT


class DynLight:
    """Owns WinRT LampArray handles on a worker thread. Thread-safe."""

    def __init__(self):
        self.enabled = False
        self.lamp_count = 0
        self.device_names: list[str] = []
        self.last_error = ""
        self.last_scan_ms = 0.0
        self._lamps: list = []          # (name, LampArray) — worker thread only
        self._queue: queue.Queue = queue.Queue(maxsize=1)  # latest (r,g,b)
        self._refresh_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- lifecycle ----
    def start(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_evt.clear()
        self._worker = threading.Thread(target=self._worker_run,
                                        name="dynlight", daemon=True)
        self._worker.start()

    def stop(self):
        self._stop_evt.set()
        self._refresh_evt.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ---- public API (never raises, never blocks the caller) ----
    def push(self, r: int, g: int, b: int):
        """Mirror a colour. No-op unless enabled with lamps. Non-blocking."""
        if not self.enabled or not _HAVE_WINRT:
            return
        try:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
            self._queue.put_nowait((max(0, min(255, int(r))),
                                    max(0, min(255, int(g))),
                                    max(0, min(255, int(b)))))
        except Exception:
            pass

    def request_refresh(self):
        """Re-enumerate devices on the worker thread."""
        self.start()
        self._refresh_evt.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled,
                    "available": _HAVE_WINRT,
                    "lamps": self.lamp_count,
                    "devices": list(self.device_names),
                    "error": self.last_error,
                    "scan_ms": self.last_scan_ms}

    # ---- worker ----
    def _worker_run(self):
        import asyncio
        try:
            asyncio.run(self._worker_main())
        except Exception:
            pass

    async def _worker_main(self):
        import asyncio
        self._do_refresh()
        while not self._stop_evt.is_set():
            if self._refresh_evt.is_set():
                self._refresh_evt.clear()
                self._do_refresh()
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                continue
            if not self.enabled:
                continue
            self._apply(item)
            # drain bursts — only the latest colour matters
            try:
                while True:
                    nxt = self._queue.get_nowait()
                    if nxt is None:
                        break
                    item = nxt
                    self._apply(item)
            except queue.Empty:
                pass
            await asyncio.sleep(0)

    def _do_refresh(self):
        t0 = time.monotonic()
        lamps: list = []
        names: list[str] = []
        err = ""
        # run enumeration in an abandonable thread (it can wedge, too)
        box: dict = {}

        def _enum():
            try:
                import asyncio as _aio

                async def _wrap():  # noqa: ANN202
                    return await DeviceInformation.find_all_async()  # type: ignore[attr-defined]

                box["infos"] = _aio.run(_wrap())
            except Exception as e:  # noqa: BLE001
                box["err"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=_enum, daemon=True)
        th.start()
        th.join(20.0)
        if th.is_alive():
            err = "enumeration hung (HID stack busy — try Refresh later)"
            infos = []
        else:
            err = box.get("err", "")
            infos = box.get("infos", [])
        if not err:
            try:
                cands = [i for i in (infos or [])
                         if _LAMP_IFACE_GUID in i.id.lower()
                         and not i.id.endswith("\\KBD")]
                # de-dupe by HID path (one zone per collection is still one open)
                seen: set[str] = set()
                uniq = []
                for i in cands:
                    key = i.id.split("#")[1] if "#" in i.id else i.id
                    if key not in seen:
                        seen.add(key)
                        uniq.append(i)
                # parallel opens under one overall deadline: stuck opens are
                # abandoned, so a busy HID stack costs seconds, not minutes
                results: dict = {}

                def _open_one(idx: int, dev_id: str):
                    la = self._open_abandonable(dev_id)
                    if la is not None:
                        results[idx] = la

                ths = []
                for idx, i in enumerate(uniq[:16]):  # cap: zones add up
                    th = threading.Thread(target=_open_one, args=(idx, i.id),
                                          daemon=True)
                    ths.append(th)
                    th.start()
                deadline = time.monotonic() + 15.0
                for th in ths:
                    th.join(max(0.1, deadline - time.monotonic()))
                for idx, i in enumerate(uniq[:16]):
                    la = results.get(idx)
                    if la is None:
                        continue
                    try:
                        n = int(la.lamp_count)
                    except Exception:  # noqa: BLE001
                        continue
                    if n > 0:
                        lamps.append((i.name or "LampArray", la))
                        names.append(i.name or "LampArray")
                if not lamps and uniq:
                    err = (f"found {len(uniq)} lighting device(s) but all are busy — "
                           "yield them first (G Hub: enable Windows Dynamic Lighting, "
                           "or close RGB apps), then Refresh")
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {str(e)[:120]}"
        with self._lock:
            self._lamps = lamps
            self.device_names = names
            self.lamp_count = sum(int(la.lamp_count) for _, la in lamps)
            self.last_error = err
            self.last_scan_ms = (time.monotonic() - t0) * 1000.0
        # drop any stale queued colour after re-enumeration
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _open_abandonable(self, dev_id: str):
        """Open one LampArray, abandoning the attempt after _OPEN_TIMEOUT.

        The native call can block synchronously, so this runs in a throwaway
        daemon thread joined with a timeout. Returns None on any failure.
        """
        box: dict = {}

        def _open():
            try:
                import asyncio as _aio

                async def _wrap():  # noqa: ANN202
                    return await LampArray.from_id_async(dev_id)  # type: ignore[attr-defined]

                box["la"] = _aio.run(_wrap())
            except Exception as e:  # noqa: BLE001
                box["err"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=_open, daemon=True)
        th.start()
        th.join(_OPEN_TIMEOUT)
        if th.is_alive():
            return None
        return box.get("la")

    def _apply(self, item):
        r, g, b = item
        try:
            color = ColorHelper.from_argb(255, r, g, b)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return
        bad: list = []
        for name, la in list(self._lamps):
            try:
                n = int(la.lamp_count)
                for idx in range(n):
                    la.set_color(color, idx)
            except Exception:  # noqa: BLE001
                bad.append((name, la))
        if bad:
            with self._lock:
                self._lamps = [p for p in self._lamps if p not in bad]
                self.lamp_count = sum(int(la.lamp_count) for _, la in self._lamps)
        if self._lamps:
            time.sleep(_PUSH_MIN_GAP)
