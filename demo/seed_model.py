"""Seed the bundled demo's model through the MCP server, the way an agent would: generate a set of diagrams and
store one agent-authored composition and view (provenance: the MCP client name and the model id below), then
write their ASCII / Markdown exports (demo/docs holds the ones the README quotes).

run.py calls this once per index, after the server is up. By hand, against a running demo server:
    python demo/seed_model.py [--url http://127.0.0.1:8766/mcp] [--out demo/cache/seed-out]

Re-running replaces the stored items (upsert by key / id). The script introduces itself as the MCP client
"copilot-cli" (client info and User-Agent: stateless HTTP sessions record the latter) with the model id below, so the
stored items carry the same provenance an agent session would leave.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

HERE = Path(__file__).resolve().parent          # demo/
CLIENT = types.Implementation(name="copilot-cli", version="demo")
MODEL = "claude-opus-5.5"

COMPOSITION = {
    "id": "ground-segment",
    "name": "Ground segment",
    "description": "From the satellite's downlink to mission control and the pass archive",
    "members": [
        {"id": "sat", "bespoke": {"type": "block", "label": "Satellite", "description": "cubesat in low Earth orbit"}},
        {"id": "link", "ref": "comp:orbit_link"},
        {"id": "sched", "ref": "comp:orbit_sched"},
        {"id": "tlm", "ref": "comp:orbit_telemetry"},
        {"id": "app", "ref": "comp:groundd"},
        {"id": "tools", "ref": "comp:orbitkit-tools"},
        {"id": "flow", "ref": "view:downlink-path"},
        {"id": "mcc", "bespoke": {"type": "actor", "label": "Mission control", "description": "operators' software"}},
        {"id": "archive", "bespoke": {"type": "block", "label": "Pass archive (.okp)"}},
    ],
    "connectors": [
        {"from": "sat", "to": "link", "kind": "flows-to", "label": "UHF downlink"},
        {"from": "link", "to": "tlm", "kind": "flows-to", "label": "raw frames"},
        {"from": "sched", "to": "link", "kind": "uses", "label": "points, retunes"},
        {"from": "sched", "to": "tlm", "kind": "uses", "label": "runs each tick"},
        {"from": "app", "to": "sched", "kind": "uses"},
        {"from": "tlm", "to": "mcc", "kind": "flows-to", "label": "UDP packets"},
        {"from": "tlm", "to": "archive", "kind": "flows-to", "label": "FileSink"},
        {"from": "tools", "to": "archive", "kind": "uses", "label": "re-decodes, reports"},
    ],
    "groups": [
        {"id": "station", "label": "Station computer (C++)", "members": ["link", "sched", "tlm", "app"]},
        {"id": "offline", "label": "Offline (Python)", "members": ["tools"]},
    ],
}

VIEW = {
    "key": "downlink-path",
    "kind": "class",
    "title": "How a frame becomes packets",
    "nodes": [
        {"id": "pipe", "ref": "sym:orbitkit::telemetry::Pipeline", "members": "auto"},
        {"id": "radio", "ref": "sym:orbitkit::link::IRadio"},
        {"id": "dec", "ref": "sym:orbitkit::telemetry::IDecoder"},
        {"id": "framed", "ref": "sym:orbitkit::telemetry::FramedDecoder", "members": "auto"},
        {"id": "ccsds", "ref": "sym:orbitkit::telemetry::CcsdsDecoder"},
        {"id": "crc", "ref": "sym:orbitkit::core::Crc"},
        {"id": "sink", "ref": "sym:orbitkit::telemetry::ISink"},
        {"id": "sdr", "type": "external", "label": "SDR driver", "note": "vendor library, optional"},
    ],
    "edges": [
        {"from": "framed", "to": "crc", "kind": "uses", "label": "check()"},
        {"from": "radio", "to": "sdr", "kind": "depends", "label": "samples"},
    ],
    "auto_edges": True,
}

# (file name, uml_view arguments): diagrams generated from the index and stored as auto views
GENERATED = [
    ("sequence-main", {"kind": "sequence", "subject": "main", "path": "apps/groundd/main.cpp", "depth": 2}),
    ("sequence-tick", {"kind": "sequence", "subject": "orbitkit::sched::Scheduler::tick", "depth": 2}),
    ("class-pipeline", {"kind": "class", "subject": "orbitkit::telemetry::Pipeline"}),
    ("class-decoders", {"kind": "class", "subject": "orbitkit::telemetry::FramedDecoder"}),
    ("component", {"kind": "component"}),
    ("component-telemetry", {"kind": "component", "subject": "orbit_telemetry"}),
    ("package-libs", {"kind": "package", "under": "libs"}),
    ("deployment", {"kind": "deployment"}),
    ("depends-scheduler", {"kind": "depends", "subject": "orbitkit::sched::Scheduler"}),
    ("depends-tracker-h", {"kind": "depends", "path": "libs/sched/include/orbitkit/sched/Tracker.h"}),
    ("object-pipeline", {"kind": "object", "subject": "orbitkit::telemetry::Pipeline"}),
    ("file-pipeline-cpp", {"path": "libs/telemetry/src/Pipeline.cpp"}),
]


def text(r) -> dict:
    t = "\n".join(c.text for c in r.content if hasattr(c, "text"))
    try:
        return json.loads(t)
    except ValueError:
        return {"text": t}


async def main(url: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    async with streamablehttp_client(url, headers={"User-Agent": f"{CLIENT.name}/{CLIENT.version}"}) as (rd, wr, _):
        async with ClientSession(rd, wr, client_info=CLIENT) as s:
            await s.initialize()

            async def call(tool: str, **args) -> dict:
                d = text(await s.call_tool(tool, args))
                if "error" in d:
                    nonlocal bad
                    bad += 1
                    print(f"!! {tool} {json.dumps(args)[:120]}: {d['error']}")
                return d

            for name, args in GENERATED:
                fmts = (("ascii", "txt", "diagram"),) if "kind" in args else ()     # file compositions: md only
                for fmt, ext, field in fmts + (("markdown", "md", "markdown"),):
                    d = await call("uml_view", format=fmt, **args)
                    if field in d:
                        (out / f"{name}.{ext}").write_text(d[field], encoding="utf-8", newline="\n")
                print(f"   {name}: {d.get('key', '?')}")
            for spec in (VIEW, COMPOSITION):
                d = await call("uml_upsert", spec=json.dumps(spec), model=MODEL)
                print(f"   upsert {d.get('key')}: unbound={d.get('unbound')} unverified={d.get('unverified_edges')}")
            for key, name in (("downlink-path", "agent-view"), ("composition:ground-segment", "agent-composition")):
                d = await call("uml_view", key=key, format="markdown")
                if "markdown" in d:
                    (out / f"{name}.md").write_text(d["markdown"], encoding="utf-8", newline="\n")
            d = await call("uml_view", key="downlink-path", format="ascii")
            if "diagram" in d:
                (out / "agent-view.txt").write_text(d["diagram"], encoding="utf-8", newline="\n")
            d = await call("uml_views", group_by="origin")
            (out / "views.json").write_text(json.dumps(d, indent=1), encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    ap.add_argument("--out", default=str(HERE / "cache" / "seed-out"))
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.url, Path(a.out))))
