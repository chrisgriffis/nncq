"""Pass planning from predicted visibility windows: pick which passes to work when several overlap."""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path


class PassPhase(enum.Enum):
    UPCOMING = "upcoming"
    ACQUIRING = "acquiring"
    TRACKING = "tracking"
    FADING = "fading"
    DONE = "done"


@dataclass(frozen=True)
class GroundSite:
    name: str
    latitude_deg: float
    longitude_deg: float
    altitude_m: float = 0.0


@dataclass(frozen=True)
class Pass:
    satellite: str
    aos_ms: int
    los_ms: int
    max_elevation_deg: float
    downlink_hz: float
    priority: int = 0

    EDGE_MS = 30_000

    @property
    def duration_s(self) -> float:
        return (self.los_ms - self.aos_ms) / 1000

    def phase(self, now_ms: int) -> PassPhase:
        if now_ms < self.aos_ms:
            return PassPhase.UPCOMING
        if now_ms > self.los_ms:
            return PassPhase.DONE
        if now_ms < self.aos_ms + self.EDGE_MS:
            return PassPhase.ACQUIRING
        if now_ms > self.los_ms - self.EDGE_MS:
            return PassPhase.FADING
        return PassPhase.TRACKING

    def overlaps(self, other: "Pass") -> bool:
        return self.aos_ms < other.los_ms and other.aos_ms < self.los_ms


class PassPlanner:
    """Greedy plan: highest priority first, then highest elevation; overlapping passes lose."""

    def __init__(self, site: GroundSite, min_elevation_deg: float = 10.0):
        self.site = site
        self.min_elevation_deg = min_elevation_deg

    def plan(self, candidates: list[Pass]) -> list[Pass]:
        usable = [p for p in candidates if p.max_elevation_deg >= self.min_elevation_deg]
        chosen: list[Pass] = []
        for p in sorted(usable, key=lambda p: (-p.priority, -p.max_elevation_deg)):
            if not any(p.overlaps(c) for c in chosen):
                chosen.append(p)
        return sorted(chosen, key=lambda p: p.aos_ms)

    def conflicts(self, candidates: list[Pass]) -> list[tuple[Pass, Pass]]:
        out = []
        for i, a in enumerate(candidates):
            out += [(a, b) for b in candidates[i + 1:] if a.overlaps(b)]
        return out


def load_satellites(path: Path) -> dict[str, dict]:
    """config/satellites.json: name -> {norad, downlink_hz, protocol, priority}."""
    return {s["name"]: s for s in json.loads(path.read_text(encoding="utf-8"))["satellites"]}
