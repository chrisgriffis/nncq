"""Frames and packets as read back from a pass archive (.okp) or a hex replay file."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


class FrameKind(enum.Enum):
    UNKNOWN = 0
    TELEMETRY = 1
    TELECOMMAND = 2
    IDLE = 3
    BEACON = 4


@dataclass(frozen=True)
class FrameHeader:
    spacecraft_id: int
    virtual_channel: int
    sequence: int

    @property
    def kind(self) -> FrameKind:
        return FrameKind.IDLE if self.virtual_channel == 7 else FrameKind.TELEMETRY

    @classmethod
    def parse(cls, body: bytes) -> "FrameHeader":
        return cls(((body[0] & 0x3F) << 4) | (body[1] >> 4), (body[1] >> 1) & 0x07, body[2])


@dataclass
class Packet:
    apid: int
    sequence_count: int
    data: bytes
    time_ms: int = 0

    IDLE_APID = 0x7FF

    @property
    def idle(self) -> bool:
        return self.apid == self.IDLE_APID


@dataclass
class Frame:
    header: FrameHeader
    payload: bytes
    received_at_ms: int = 0
    snr_db: float = 0.0
    packets: list[Packet] = field(default_factory=list)

    @property
    def kind(self) -> FrameKind:
        return self.header.kind


def read_hex_replay(path: Path) -> Iterator[bytes]:
    """One raw frame per line, hex encoded: the same format LoopbackRadio::load reads."""
    for line in path.read_text(encoding="ascii").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield bytes.fromhex(line)


def read_archive(path: Path) -> Iterator[Packet]:
    """Packets from a FileSink archive: [apid:2][len:2][data:len]... followed by a CRC-32 trailer."""
    blob = path.read_bytes()
    body, at = blob[:-4], 0
    while at + 4 <= len(body):
        apid = int.from_bytes(body[at:at + 2], "big")
        size = int.from_bytes(body[at + 2:at + 4], "big")
        yield Packet(apid, 0, body[at + 4:at + 4 + size])
        at += 4 + size
