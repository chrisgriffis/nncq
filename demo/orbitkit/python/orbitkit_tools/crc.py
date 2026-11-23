"""CRC-16/CCITT-FALSE (frames) and CRC-32 (archives), bit-compatible with orbitkit::core::Crc."""
from __future__ import annotations

import zlib

_TABLE = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if _c & 0x8000 else (_c << 1)
    _TABLE.append(_c & 0xFFFF)


def ccitt16(data: bytes, seed: int = 0xFFFF) -> int:
    crc = seed
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def crc32(data: bytes, seed: int = 0) -> int:
    return zlib.crc32(data, seed) & 0xFFFFFFFF


def check(frame: bytes) -> bool:
    """True when the frame's last two bytes are the CRC-16 of the rest (a corrupted frame fails)."""
    if len(frame) < 3:
        return False
    return ccitt16(frame[:-2]) == int.from_bytes(frame[-2:], "big")


def seal(body: bytes) -> bytes:
    """body + its CRC-16, as the spacecraft sends it."""
    return body + ccitt16(body).to_bytes(2, "big")
