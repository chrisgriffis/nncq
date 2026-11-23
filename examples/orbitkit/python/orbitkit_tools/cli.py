"""orbit-tools: command line entry point (see pyproject.toml [project.scripts])."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orbitkit_tools import __version__
from orbitkit_tools.passes import GroundSite, Pass, PassPlanner, load_satellites
from orbitkit_tools.report import report_from_archive, report_from_replay


def cmd_report(a) -> int:
    path = Path(a.file)
    rep = report_from_archive(path, a.satellite) if path.suffix == ".okp" else \
        report_from_replay(path, a.satellite, a.protocol)
    sys.stdout.write(rep.to_markdown())
    return 0 if rep.frame_error_rate < 0.5 else 2


def cmd_plan(a) -> int:
    sats = load_satellites(Path(a.satellites))
    windows = json.loads(Path(a.windows).read_text(encoding="utf-8"))
    passes = [Pass(w["satellite"], w["aos_ms"], w["los_ms"], w["max_el"], sats[w["satellite"]]["downlink_hz"],
                   sats[w["satellite"]].get("priority", 0)) for w in windows]
    planner = PassPlanner(GroundSite(a.site, a.lat, a.lon), a.min_el)
    for p in planner.plan(passes):
        print(f"{p.satellite:12} aos={p.aos_ms} dur={p.duration_s:5.0f}s max_el={p.max_elevation_deg:4.1f}")
    for x, y in planner.conflicts(passes):
        print(f"conflict: {x.satellite} / {y.satellite}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orbit-tools", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="summarise a pass archive (.okp) or hex replay file")
    p.add_argument("file")
    p.add_argument("--satellite", default="unknown")
    p.add_argument("--protocol", default="ccsds")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("plan", help="choose passes to work from predicted windows")
    p.add_argument("windows", help="JSON list of {satellite, aos_ms, los_ms, max_el}")
    p.add_argument("--satellites", default="config/satellites.json")
    p.add_argument("--site", default="station")
    p.add_argument("--lat", type=float, default=0.0)
    p.add_argument("--lon", type=float, default=0.0)
    p.add_argument("--min-el", type=float, default=10.0)
    p.set_defaults(fn=cmd_plan)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
