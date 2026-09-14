"""
Elf Lantern (wl.smartled / com.easylink.colorful) BLE protocol - PC port.
Reverse-engineered from Elf+Lantern_6.5.08_APKPure.xapk
Service: BluetoothLEService.java, BluetoothUtil.java, EncryptionDecryptionKt.java
"""
from __future__ import annotations
import os
import random

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"

# Device name filters (BluetoothLEService.supportedDevice + Global)
NAME_FILTERS = ("ELK-", "ELK~", "LED LIGHT STRIP", "XSL-", "ELK_*", "CLK-")
ENCRYPTED_MARKER = "ELK-*"

START = 0x7E
END = 0xEF

MODES = [
    "Static Red",
    "Static Blue",
    "Static Green",
    "Static Cyan",
    "Static Yellow",
    "Static Purple",
    "Static White",
    "Three Color Jumping Change",
    "Seven Color Jumping Change",
    "Three Color Cross Fade",
    "Seven Color Cross Fade",
    "Red Gradual Change",
    "Green Gradual Change",
    "Blue Gradual Change",
    "Yellow Gradual Change",
    "Cyan Gradual Change",
    "Purple Gradual Change",
    "White Gradual Change",
    "Red Green Cross Fade",
    "Red Blue Cross Fade",
    "Green Blue Cross Fade",
    "Seven color Stobe Flash",
    "Red Strobe Flash",
    "Green Strobe Flash",
    "Blue Strobe Flash",
    "Yellow Strobe Flash",
    "Cyan Strobe Flash",
    "Purple Strobe Flash",
    "White Strobe Flash",
]

# LightMode in BluetoothUtil (0=ALL,1=RGB,2=W,3=CT,4=LASER)
LIGHT_MODE_ALL = 0
LIGHT_MODE_RGB = 1
LIGHT_MODE_W = 2
LIGHT_MODE_CT = 3
LIGHT_MODE_LASER = 4

EQ_CLASSIC = 0
EQ_SOFT = 1
EQ_DYNAMIC = 2
EQ_DISCO = 3


def _b(v: int) -> int:
    return v & 0xFF


def is_supported_device(name: str | None) -> bool:
    if not name:
        return False
    n = name.strip()
    return (
        n.startswith("ELK-")
        or n.startswith("ELK~")
        or n.startswith("LED LIGHT STRIP")
        or n.startswith("XSL-")
        or n.startswith("ELK-*")
    )


def is_encrypted_device(name: str | None) -> bool:
    return bool(name) and ENCRYPTED_MARKER in name


# ---------- packet builders (all 9 bytes, [7E ... EF]) ----------

def pkt_power(on: bool) -> bytes:
    v = 0x01 if on else 0x00
    return bytes([START, 0x04, 0x04, v, 0x00, v, 0xFF, 0x00, END])


def pkt_brightness(brightness: int, light_mode: int = 0xFF) -> bytes:
    brightness = max(0, min(100, int(brightness)))
    return bytes([START, 0x04, 0x01, _b(brightness), _b(light_mode), 0xFF, 0xFF, 0x00, END])


def pkt_color_static(r: int, g: int, b: int) -> bytes:
    return bytes([START, 0x07, 0x05, 0x03, _b(r), _b(g), _b(b), 0x10, END])


def pkt_color_realtime(r: int, g: int, b: int) -> bytes:
    """Music-amplitude / Ambilight realtime packet. Same as static but flag 0x20.
    This is what the PC Ambilight screen-sync uses."""
    return bytes([START, 0x07, 0x05, 0x03, _b(r), _b(g), _b(b), 0x20, END])


def pkt_mode(mode_index: int) -> bytes:
    if not (0 <= mode_index < len(MODES)):
        raise ValueError(f"mode 0..{len(MODES)-1}")
    return bytes([START, 0x05, 0x03, _b(mode_index + 128), 0x03, 0xFF, 0xFF, 0x00, END])


def pkt_mode_speed(speed: int) -> bytes:
    speed = max(0, min(100, int(speed)))
    return bytes([START, 0x04, 0x02, _b(speed), 0xFF, 0xFF, 0xFF, 0x00, END])


def pkt_single_color_w(value: int) -> bytes:
    value = max(0, min(100, int(value)))
    return bytes([START, 0x05, 0x05, 0x01, _b(value), 0xFF, 0xFF, 0x08, END])


def pkt_cct(warm: int, cold: int) -> bytes:
    warm = max(0, min(100, int(warm)))
    cold = max(0, min(100, int(cold)))
    return bytes([START, 0x06, 0x05, 0x02, _b(warm), _b(cold), 0xFF, 0x08, END])


def pkt_rgbw(rgb_on: bool, w_on: bool, cct_on: bool, light_mode: int = 0) -> bytes:
    flags = 0xE0 if rgb_on else 0x00
    if w_on:
        flags |= 0x10
    if light_mode in (0, 1):
        onoff = 0x01 if rgb_on else 0x00
    elif light_mode == 2:
        onoff = 0x01 if w_on else 0x00
    else:
        onoff = 0x01 if cct_on else 0x00
    return bytes([START, 0x04, 0x04, _b(flags), _b(light_mode), _b(onoff), 0xFF, 0x00, END])


def _weeks_byte(weeks_7bit: int, on: bool) -> int:
    return (_b(weeks_7bit) & 0x7F) | (0x80 if on else 0x00)


def pkt_timing(hour: int, minute: int, second: int, weeks_7bit: int, on: bool, slot: int) -> bytes:
    """slot 0 = timer1 (on), 1 = timer2 (off). weeks bit0=Mon..bit6=Sun."""
    return bytes([
        START, 0x08, 0x82,
        _b(hour), _b(minute), _b(second),
        _b(slot), _b(_weeks_byte(weeks_7bit, on)), END,
    ])


def pkt_system_time(hour: int, minute: int, second: int, weekday_0sun: int) -> bytes:
    return bytes([START, 0x07, 0x83, _b(hour), _b(minute), _b(second), _b(weekday_0sun), 0xFF, END])


def pkt_pin_sequence(p1: int, p2: int, p3: int) -> bytes:
    return bytes([START, 0x06, 0x81, _b(p1), _b(p2), _b(p3), 0xFF, 0x00, END])


def pkt_ext_mic_onoff(on: bool) -> bytes:
    return bytes([START, 0x04, 0x07, 0x01 if on else 0x00, 0xFF, 0xFF, 0xFF, 0x00, END])


def pkt_ext_mic_eq(eq_mode: int) -> bytes:
    return bytes([START, 0x05, 0x03, _b(eq_mode + 128), 0x04, 0xFF, 0xFF, 0x00, END])


def pkt_ext_mic_sensitive(value: int) -> bytes:
    value = max(0, min(100, int(value)))
    return bytes([START, 0x04, 0x06, _b(value), 0xFF, 0xFF, 0xFF, 0x00, END])


# ---------- encryption (com.szelk.ledlamppro.ble.EncryptionDecryptionKt) ----------
# Only for devices whose advertised name contains "ELK-*" and only when
# packet[2] (CMD) is 1, 3 or 4. Then header 7E->AA, tail EF->55, 9B -> 21B.

_PRESET_KEY = bytes([42, 127, 193, 148, 51, 222, 69, 224, 139, 17, 92, 166, 9, 242, 125, 184])


def _keystream(rand12: bytes) -> bytes:
    out = bytearray(9)
    for i in range(9):
        a = (rand12[i] * 27) & 0xFF
        b = (rand12[(i + 3) % 12] + 55) & 0xFF
        c = (rand12[(i + 7) % 12] >> 2) & 0xFF
        d = (i * 85) & 0xFF
        out[i] = (a ^ b ^ c ^ d) & 0xFF
    return bytes(out)


def encrypt_packet(plain9: bytes, rand12: bytes | None = None) -> bytes:
    if len(plain9) != 9:
        raise ValueError("plain must be 9 bytes")
    if rand12 is None:
        rand12 = os.urandom(12)
    if len(rand12) != 12:
        raise ValueError("rand must be 12 bytes")
    ks = _keystream(rand12)
    ct = bytearray(21)
    for i in range(9):
        ct[i] = (plain9[i] ^ ks[i]) & 0xFF
    for i in range(12):
        ct[9 + i] = (rand12[i] ^ _PRESET_KEY[i % 16]) & 0xFF
    return bytes(ct)


def maybe_encrypt(packet9: bytes, device_name: str | None) -> bytes:
    """Apply ELK-* encryption rule. Returns 9B normally, 21B if encrypted."""
    if len(packet9) != 9:
        raise ValueError("packet must be 9 bytes")
    if is_encrypted_device(device_name) and packet9[2] in (0x01, 0x03, 0x04):
        mutated = bytearray(packet9)
        mutated[0] = 0xAA
        mutated[8] = 0x55
        return encrypt_packet(bytes(mutated))
    return packet9


def hex_str(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)
