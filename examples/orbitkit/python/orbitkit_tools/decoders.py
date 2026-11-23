"""Frame decoders: the Python twins of telemetry::IDecoder and its subclasses, for offline re-decoding."""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field

from orbitkit_tools import crc
from orbitkit_tools.frames import Frame, FrameHeader, FrameKind, Packet


class DecodeStatus(enum.Enum):
    OK = "ok"
    NO_SYNC = "no-sync"
    BAD_CRC = "bad-crc"
    TRUNCATED = "truncated"
    UNSUPPORTED = "unsupported"


@dataclass
class DecodeResult:
    status: DecodeStatus
    frame: Frame | None = None
    packets: list[Packet] = field(default_factory=list)


class Decoder(abc.ABC):
    protocol = "abstract"

    @abc.abstractmethod
    def decode(self, raw: bytes) -> DecodeResult: ...


class FramedDecoder(Decoder):
    """Sync marker search and CRC check; subclasses decode the body."""

    def __init__(self, sync: bytes, frame_size: int):
        self.sync = sync
        self.frame_size = frame_size
        self.crc_failures = 0

    def decode(self, raw: bytes) -> DecodeResult:
        at = raw.find(self.sync)
        if at < 0:
            return DecodeResult(DecodeStatus.NO_SYNC)
        body = raw[at + len(self.sync):at + len(self.sync) + self.frame_size]
        if len(body) < self.frame_size:
            return DecodeResult(DecodeStatus.TRUNCATED)
        if not crc.check(body):
            self.crc_failures += 1
            return DecodeResult(DecodeStatus.BAD_CRC)
        return self.decode_body(body[:-2])

    @abc.abstractmethod
    def decode_body(self, body: bytes) -> DecodeResult: ...


class CcsdsDecoder(FramedDecoder):
    protocol = "ccsds-tm"
    ASM = bytes.fromhex("1ACFFC1D")

    def __init__(self, frame_size: int = 223):
        super().__init__(self.ASM, frame_size)

    def decode_body(self, body: bytes) -> DecodeResult:
        header = FrameHeader.parse(body)
        frame = Frame(header, body[6:])
        if header.kind is not FrameKind.IDLE:
            frame.packets = list(self.packets(frame.payload))
        return DecodeResult(DecodeStatus.OK, frame, frame.packets)

    @staticmethod
    def packets(data: bytes):
        at = 0
        while at + 6 <= len(data):
            apid = ((data[at] & 0x07) << 8) | data[at + 1]
            seq = ((data[at + 2] & 0x3F) << 8) | data[at + 3]
            size = int.from_bytes(data[at + 4:at + 6], "big") + 1
            if at + 6 + size > len(data):
                return
            if apid != Packet.IDLE_APID:
                yield Packet(apid, seq, data[at + 6:at + 6 + size])
            at += 6 + size


class Ax25Decoder(FramedDecoder):
    protocol = "ax25"

    def __init__(self, callsign: str = ""):
        super().__init__(b"\x7e\x7e\x7e\x7e", 256)
        self.callsign = callsign

    @staticmethod
    def read_callsign(field_: bytes) -> str:
        return "".join(chr(b >> 1) for b in field_[:6]).strip()

    def decode_body(self, body: bytes) -> DecodeResult:
        if len(body) < 16:
            return DecodeResult(DecodeStatus.TRUNCATED)
        if self.callsign and self.read_callsign(body[7:13]) != self.callsign:
            return DecodeResult(DecodeStatus.UNSUPPORTED)
        frame = Frame(FrameHeader(0, 0, 0), body[16:])
        frame.packets = [Packet(0x0A0, 0, frame.payload)]
        return DecodeResult(DecodeStatus.OK, frame, frame.packets)


class BeaconDecoder(Ax25Decoder):
    protocol = "ax25-beacon"

    def decode_body(self, body: bytes) -> DecodeResult:
        res = super().decode_body(body)
        for p in res.packets:
            p.apid = 0x100
        return res

    @staticmethod
    def fields(text: str) -> dict[str, str]:
        """'KST1>BCN: batt=7.9V temp=12C' -> {'batt': '7.9V', 'temp': '12C'}"""
        _, _, rest = text.partition(":")
        return dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)


def make_decoder(protocol: str) -> Decoder:
    """'ccsds', 'ax25:<callsign>' or 'beacon:<callsign>', like telemetry::makeDecoder."""
    kind, _, arg = protocol.partition(":")
    if kind == "ax25":
        return Ax25Decoder(arg)
    if kind == "beacon":
        return BeaconDecoder(arg)
    return CcsdsDecoder()
