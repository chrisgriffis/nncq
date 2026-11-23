"""Pass reports: what came down during one pass, as Markdown for the operations log."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from orbitkit_tools.decoders import DecodeStatus, Decoder, make_decoder
from orbitkit_tools.frames import Packet, read_archive, read_hex_replay
from orbitkit_tools.passes import Pass


@dataclass
class PassReport:
    satellite: str
    protocol: str
    statuses: Counter = field(default_factory=Counter)
    apids: Counter = field(default_factory=Counter)
    bytes_received: int = 0

    @property
    def frames(self) -> int:
        return sum(self.statuses.values())

    @property
    def frame_error_rate(self) -> float:
        return self.statuses[DecodeStatus.BAD_CRC] / self.frames if self.frames else 0.0

    def add_packet(self, packet: Packet) -> None:
        self.apids[packet.apid] += 1
        self.bytes_received += len(packet.data)

    def to_markdown(self, planned: Pass | None = None) -> str:
        lines = [f"# Pass report: {self.satellite}", ""]
        if planned:
            lines += [f"- window: {planned.duration_s:.0f} s, max elevation {planned.max_elevation_deg:.0f} deg", ""]
        lines += [f"- protocol: {self.protocol}", f"- frames: {self.frames}",
                  f"- frame error rate: {self.frame_error_rate:.1%}", f"- payload bytes: {self.bytes_received}", "",
                  "| APID | packets |", "|---|---|"]
        lines += [f"| 0x{apid:03X} | {n} |" for apid, n in sorted(self.apids.items())]
        return "\n".join(lines) + "\n"


def report_from_replay(path: Path, satellite: str, protocol: str = "ccsds") -> PassReport:
    """Re-decode a hex replay file offline and summarise it."""
    decoder: Decoder = make_decoder(protocol)
    rep = PassReport(satellite, decoder.protocol)
    for raw in read_hex_replay(path):
        res = decoder.decode(raw)
        rep.statuses[res.status] += 1
        for p in res.packets:
            rep.add_packet(p)
    return rep


def report_from_archive(path: Path, satellite: str) -> PassReport:
    rep = PassReport(satellite, "archive")
    for p in read_archive(path):
        rep.statuses[DecodeStatus.OK] += 1
        rep.add_packet(p)
    return rep
