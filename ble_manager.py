"""Async BLE manager for Elf-Ambilight strips (Bleak, Windows 10/11)."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field

from protocol import SERVICE_UUID, CHAR_UUID, is_supported_device, maybe_encrypt, hex_str

try:
    from bleak import BleakScanner, BleakClient
    from bleak.exc import BleakError
except ImportError:  # pragma: no cover
    BleakScanner = None  # type: ignore
    BleakClient = None  # type: ignore
    BleakError = Exception  # type: ignore


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: int = 0


@dataclass
class _Conn:
    client: object
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ElfBLE:
    """Keeps one BleakClient per MAC. All writes go to CHAR_UUID."""

    def __init__(self):
        self._conns: dict[str, _Conn] = {}
        self._names: dict[str, str] = {}

    # ---- scan ----
    async def scan(self, timeout: float = 8.0) -> list[FoundDevice]:
        if BleakScanner is None:
            raise RuntimeError("bleak not installed (pip install -r requirements.txt)")
        found: dict[str, FoundDevice] = {}

        def _cb(device, _adv):
            name = device.name or ""
            if is_supported_device(name):
                addr = device.address
                prev = found.get(addr)
                rssi = getattr(_adv, "rssi", 0) or 0
                if prev is None or rssi > prev.rssi:
                    found[addr] = FoundDevice(name=name, address=addr, rssi=rssi)

        scanner = BleakScanner(detection_callback=_cb)  # type: ignore
        await scanner.start()
        try:
            await asyncio.sleep(timeout)
        finally:
            await scanner.stop()
        devs = sorted(found.values(), key=lambda d: -d.rssi)
        for d in devs:
            self._names[d.address] = d.name
        return devs

    # ---- connect ----
    async def connect(self, address: str, name: str = "", timeout: float = 15.0) -> bool:
        if address in self._conns:
            try:
                existing = self._conns[address].client
                if bool(getattr(existing, "is_connected", False)):
                    return True
            except Exception:
                pass
            await self.disconnect(address)
        if name:
            self._names[address] = name
        client = BleakClient(address, timeout=timeout)  # type: ignore
        await client.connect()
        # best-effort MTU / service discovery happens inside bleak
        self._conns[address] = _Conn(client=client, name=self._names.get(address, name))
        return True

    async def disconnect(self, address: str):
        c = self._conns.pop(address, None)
        if c is not None:
            try:
                await c.client.disconnect()  # type: ignore
            except Exception:
                pass

    async def disconnect_all(self):
        for addr in list(self._conns.keys()):
            await self.disconnect(addr)

    def is_connected(self, address: str) -> bool:
        c = self._conns.get(address)
        if not c:
            return False
        try:
            return bool(c.client.is_connected)  # type: ignore
        except Exception:
            return False

    def connected_addresses(self) -> list[str]:
        return [a for a in list(self._conns.keys()) if self.is_connected(a)]

    # ---- write ----
    async def write(self, address: str, packet9: bytes, response: bool = False):
        """Write one 9-byte protocol packet (auto-encrypts for ELK-* devices)."""
        c = self._conns.get(address)
        if c is None or not self.is_connected(address):
            raise RuntimeError(f"not connected: {address}")
        payload = maybe_encrypt(packet9, c.name or self._names.get(address, ""))
        async with c.lock:
            try:
                await c.client.write_gatt_char(CHAR_UUID, payload, response=response)  # type: ignore
            except Exception as e:
                # one retry without response flip
                try:
                    await c.client.write_gatt_char(CHAR_UUID, payload, response=not response)  # type: ignore
                except Exception:
                    raise e

    async def write_many(self, addresses: list[str], packet9: bytes, response: bool = False,
                         concurrency: int = 4):
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(addr: str):
            async with sem:
                try:
                    await self.write(addr, packet9, response=response)
                    return (addr, True, "")
                except Exception as e:
                    return (addr, False, str(e))

        return await asyncio.gather(*[_one(a) for a in addresses])
