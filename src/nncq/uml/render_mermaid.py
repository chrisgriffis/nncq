"""Mermaid text for a view model. Node ids become n_<id> (Mermaid has reserved words); the dashboard maps them back.

  class, object      classDiagram (instances as classes labelled 'name : Type' with «instance»-like stereotypes)
  sequence           sequenceDiagram
  component          flowchart: a component with ports is a subgraph; provided ports are circles ((x)),
                     required ports are trapezoids [/x\\]; connectors run port -> port
  package, deployment, composition and anything else
                     flowchart: frames (groups, compositions, embedded diagrams) are subgraphs
"""
from __future__ import annotations

import re

_SHAPE = {"component": ('["', '"]'), "package": ('[/"', '"/]'), "artifact": ('[["', '"]]'),
          "config": ('{{"', '"}}'), "external": ('[("', '")]'), "actor": ('(["', '"])'), "note": ('>"', '"]'),
          "composition": ('[/"', '"\\]'), "view": ('[\\"', '"/]'), "port": ('(("', '"))'),
          "lifeline": ('["', '"]'), "instance": ('["', '"]')}
_ARROW = {"inherits": "-->", "realizes": "-.->", "composes": "--o", "aggregates": "--o", "associates": "-->",
          "depends": "-.->", "depends-on": "-.->", "uses": "-.->", "imports": "-.->", "includes": "-.->",
          "instance_of": "-.->", "deploys": "-.->", "instantiates": "-.->", "flows-to": "==>", "connects": "---",
          "contains": "--o", "calls": "-->", "link": "---"}


# fills by element kind (dark, readable text in light viewers too); the dashboard's list badges use the same hues
PALETTE = {"class": ("#1b2a44", "#5aa9ff"), "interface": ("#2a1f44", "#b28cff"), "enum": ("#3a2d10", "#e5a50a"),
           "struct": ("#13332e", "#3cc6b0"), "function": ("#14301f", "#4cc38a"), "instance": ("#13332e", "#5fd4c0"),
           "component": ("#1e2633", "#8fa3bf"), "port": ("#1e2633", "#8fa3bf"), "package": ("#262033", "#a58bd6"),
           "config": ("#2d2a18", "#c9b458"), "release": ("#14301f", "#4cc38a"), "debug": ("#3a2d08", "#e5a50a"),
           "external": ("#17191d", "#5a6375"), "module": ("#232a33", "#7f8ea3"), "view": ("#1d2a44", "#5aa9ff"),
           "composition": ("#33203a", "#d68be6"), "note": ("#2b2a14", "#8a7f3c"), "block": ("#262b36", "#8b95a7")}
_ALIAS = {"union": "struct", "record": "struct", "trait": "interface", "lifeline": "function", "method": "function",
          "artifact": "component", "actor": "block", "part": "block", "data": "block", "state": "block",
          "usecase": "block"}


EDGE_HUE = {"inherits": "interface", "realizes": "interface", "composes": "class", "aggregates": "struct",
            "associates": "component", "depends": "package", "instantiates": "function", "var": "config",
            "include": "module", "import": "module"}


def kind_of(n: dict) -> str:
    """Palette key of a node: its type, abstract types as interfaces, configurations by release / debug."""
    t = _ALIAS.get(n.get("type") or "", n.get("type") or "class")
    st = (n.get("stereotype") or "").lower()
    if t == "class" and ("abstract" in st or "interface" in st):
        t = "interface"
    if t == "config":
        lab = (n.get("label") or "").lower()
        t = "debug" if "debug" in lab else "release" if "release" in lab or "relwith" in lab else "config"
    return t if t in PALETTE else "block"


def _style(fill: str, stroke: str, extra: str = "") -> str:
    return f"fill:{fill},stroke:{stroke},color:#d7dae0{extra}"


def nid(i: str) -> str:
    return "n_" + re.sub(r"\W", "_", str(i))


def _q(s: str) -> str:
    return str(s or "").replace('"', "#quot;").replace("\n", " ").replace(";", ",")[:160]


def _plain(s: str) -> str:
    return re.sub(r"[;#%{}\"`]", "", str(s or "")).replace("\n", " ")[:120]


def _member(m: dict) -> str:
    t = lambda s: _plain(s).replace("<", "~").replace(">", "~").replace("*", "∗").replace(":", "꞉")
    mods = set(m.get("mods") or [])
    if m.get("kind") == "method":
        txt = f"{m.get('vis', '')}{t(m['name'])}({t(m.get('params', ''))[:60]})" + (f" {t(m['type'])}" if m.get("type") else "")
        if mods & {"pure", "abstract"}:
            txt += "*"
        elif "static" in mods:
            txt += "$"
    else:
        txt = f"{m.get('vis', '')}{t(m['type']) + ' ' if m.get('type') else ''}{t(m['name'])}"
        if "static" in mods:
            txt += "$"
    return txt


def _class(model: dict) -> list[str]:
    out = ["classDiagram", f"  direction {(model.get('layout') or {}).get('direction', 'TB')}"]
    for n in model["nodes"]:
        i = nid(n["id"])
        label = _q(n.get("label") or n["id"])
        if n.get("tparams"):
            label += "~" + _plain(n["tparams"]).replace("<", "").replace(">", "").replace(" ", "")[:30] + "~"
        out.append(f'  class {i}["{label}"]')
        st = [s for s in (n.get("stereotype") or "").split(",") if s]
        typ = n.get("type", "class")
        if typ not in ("class", "struct", "instance"):
            st.insert(0, typ)
        if n.get("stale"):
            st.append(f"stale: {n['stale']}")
        if st:
            out.append(f"  <<{_plain(', '.join(dict.fromkeys(st)))}>> {i}")
        ms = n.get("members") or []
        for m in ms[:25]:
            out.append(f"  {i} : {_member(m)}")
        if len(ms) > 25:
            out.append(f"  {i} : ... {len(ms) - 25} more")
        for s in (n.get("slots") or [])[:4]:
            out.append(f"  {i} : {_plain(s).replace(':', '꞉')}")
        if n.get("note") and n.get("type") in ("instance", "note", "block") :
            out.append(f'  note for {i} "{_q(n["note"])}"')
        f, s = PALETTE[kind_of(n)]
        out.append(f"  style {i} " + (_style("#3a2d08", "#e5a50a", ",stroke-dasharray:6 3") if n.get("stale")
                                       else _style(f, s)))
    for e in model.get("edges", []):
        a, b, k = nid(e["from"]), nid(e["to"]), e["kind"]
        lab = _plain(e.get("label") or "").replace(":", "")
        lab = f" : {lab}" if lab else ""
        mt = f' "{_plain(e["mult_to"])}"' if e.get("mult_to") else ""
        mf = f'"{_plain(e["mult_from"])}" ' if e.get("mult_from") else ""
        if k == "inherits":
            out.append(f"  {b} <|-- {a}")
        elif k == "realizes":
            out.append(f"  {b} <|.. {a}")
        elif k in ("composes", "contains"):
            out.append(f"  {a} {mf}*--{mt} {b}{lab}")
        elif k == "aggregates":
            out.append(f"  {a} {mf}o--{mt} {b}{lab}")
        elif k in ("associates", "calls", "flows-to", "connects", "link"):
            out.append(f"  {a} {mf}-->{mt} {b}{lab}")
        else:
            out.append(f"  {a} ..> {b}{lab or ' : ' + k}")
    for x in model.get("notes", []):
        out.append(f'  note "{_q(x["text"])}"' if not x.get("on") else f'  note for {nid(x["on"])} "{_q(x["text"])}"')
    return out


def _sequence(model: dict) -> list[str]:
    out = ["sequenceDiagram"]
    for n in model["nodes"]:
        kw = "actor" if n.get("type") == "actor" else "participant"
        out.append(f"  {kw} {nid(n['id'])} as {_plain(n.get('label') or n['id'])}"
                   + (" (stale)" if n.get("stale") else ""))
    for m in model.get("messages", []):
        a, b = nid(m["from"]), nid(m["to"])
        arrow = {"reply": "-->>", "async": "-)", "create": "->>"}.get(m.get("kind"), "->>")
        if m.get("kind") == "note":
            out.append(f"  Note over {a}: {_plain(m['label'])}")
        else:
            out.append(f"  {a}{arrow}{b}: {_plain(m.get('label') or ' ')}")
    if model.get("cut"):
        out.append(f"  Note over {nid(model['nodes'][0]['id'])}: {_plain(model['cut'])}")
    return out


def _flow(model: dict) -> list[str]:
    out = [f"flowchart {(model.get('layout') or {}).get('direction', 'LR')}"]
    groups = model.get("groups") or []
    in_group = {x: g["id"] for g in groups for x in g.get("nodes", [])}
    nodes = {n["id"]: n for n in model["nodes"]}
    lines: dict[str | None, list[str]] = {}
    classes: list[str] = []

    def node_lines(n: dict, ind: str) -> list[str]:
        i = nid(n["id"])
        st = [s for s in (n.get("stereotype") or "").split(",") if s]
        typ = n.get("type", "")
        if typ in ("class", "interface", "struct", "enum", "block", "part", "function", "module") and typ not in st:
            st.insert(0, typ)
        head = (f"«{_q(', '.join(st))}»<br/>" if st else "") + _q(n.get("label") or n["id"])
        ms = [] if n.get("collapsed") else n.get("members") or []
        if ms:
            head += "<br/>" + "<br/>".join(_q(_member(m)).replace("#", "﹟") for m in ms[:10]) + \
                (f"<br/>… +{len(ms) - 10} more" if len(ms) > 10 else "")
        if n.get("stale"):
            head += f"<br/>(stale: {_q(n['stale'])})"
        if n.get("bespoke"):
            classes.append(f"  class {i} bespoke")
        ports = n.get("ports") or []
        if ports:
            f, s = PALETTE[kind_of(n)]
            classes.append(f"  style {i} {_style(f, s)}")
            res = [f'{ind}subgraph {i}["{head}"]', f"{ind}  direction TB"]
            for p in ports:
                pi = nid(p["id"])
                if p.get("dir") == "required":
                    res.append(f'{ind}  {pi}[/"{_q(p["name"])}"\\]')
                    classes.append(f"  class {pi} required")
                else:
                    res.append(f'{ind}  {pi}(("{_q(p["name"])}"))')
                    classes.append(f"  class {pi} provided")
            res.append(f"{ind}end")
            return res
        a, b = _SHAPE.get(typ, ('["', '"]'))
        classes.append(f"  class {i} k_{'ext' if n.get('outside') else kind_of(n)}")
        if n.get("stale"):
            classes.append(f"  class {i} stale")
        return [f"{ind}{i}{a}{head}{b}"]

    children: dict[str | None, list[dict]] = {}
    for g in groups:
        children.setdefault(g.get("parent"), []).append(g)

    depth: dict[str, int] = {}

    def group_lines(g: dict, ind: str) -> list[str]:
        d = depth[g["id"]] = depth.get(g.get("parent") or "", -1) + 1
        lab = g.get("label") or ""
        frame = ("#5aa9ff0d", "#5aa9ff66") if lab.startswith("«composition»") else \
            ("#a58bd612", "#a58bd666") if lab.startswith("«namespace»") else \
            ("#d68be60d", "#d68be655") if lab.startswith("«diagram»") else (f"#ffffff{6 + 3 * min(d, 4):02x}", "#3d4658")
        classes.append(f"  style {nid(g['id'])} fill:{frame[0]},stroke:{frame[1]},color:#9aa3b0")
        res = [f'{ind}subgraph {nid(g["id"])}["{_q(g.get("label") or g["id"])}"]']
        for x in g.get("nodes", []):
            if x in nodes:
                res += node_lines(nodes[x], ind + "  ")
        for c in children.get(g["id"], []):
            res += group_lines(c, ind + "  ")
        return res + [f"{ind}end"]
    for n in model["nodes"]:
        if n["id"] not in in_group:
            out += node_lines(n, "  ")
    known = {g["id"] for g in groups}
    for g in groups:
        if not g.get("parent") or g["parent"] not in known:
            out += group_lines(g, "  ")
    for e in model.get("edges", []):
        a = nid(e.get("from_port") or e.get("from_frame") or e["from"])
        b = nid(e.get("to_port") or e.get("to_frame") or e["to"])
        arrow = "-.->" if e.get("cycle") else _ARROW.get(e["kind"], "-->")
        lab = " ".join(x for x in (e.get("label") or "", f"x{e['n']}" if e.get("n") and e["kind"] == "connects"
                                   else "") if x)
        if e["kind"] in ("inherits", "realizes", "instance_of", "contains", "flows-to", "uses", "depends-on") and \
                e["kind"] not in lab:
            lab = (e["kind"] + " " + lab).strip()
        out.append(f'  {a} {arrow}|"{_q(lab)}"| {b}' if lab else f"  {a} {arrow} {b}")
    if model.get("kind") == "depends":                        # edge colour = relation kind, same hues as the nodes
        for i, e in enumerate(model.get("edges", [])):
            f, s = PALETTE.get(EDGE_HUE.get(e["kind"], "block"), PALETTE["block"])
            out.append(f"  linkStyle {i} stroke:{s},color:{s}" + (",stroke-dasharray:5 4" if e.get("cycle") else ""))
        roots = [n for n in model["nodes"] if n.get("root")]
        classes += [f"  class {nid(n['id'])} root" for n in roots]
    for i, x in enumerate(model.get("notes", [])):
        out.append(f'  note_{i}>"{_q(x["text"])}"]')
        if x.get("on"):
            out.append(f"  note_{i} -.- {nid(x['on'])}")
    out += [f"  classDef k_{k} {_style(f, s)}" for k, (f, s) in PALETTE.items()]
    out += ["  classDef k_ext fill:#15171b,stroke:#4a5160,color:#8b95a7,stroke-dasharray:3 3",
            "  classDef provided fill:#12351f,stroke:#4cc38a,color:#d7dae0",
            "  classDef required fill:#3a2d08,stroke:#e5a50a,color:#d7dae0",
            "  classDef bespoke stroke-dasharray:4 3",
            "  classDef stale fill:#3a2d08,stroke:#e5a50a,stroke-width:2px,stroke-dasharray:6 3",
            "  classDef root stroke-width:3px"]
    out += list(dict.fromkeys(classes))
    return out


def render(model: dict, links: dict[str, str] | None = None) -> str:
    """links: node id -> URL, emitted as click / link directives (Markdown export; renderers may ignore them)."""
    k = model.get("kind")
    if k == "sequence":
        lines = _sequence(model)
        lines += [f"  link {nid(i)}: open @ {u}" for i, u in (links or {}).items()]
    else:
        lines = _class(model) if k in ("class", "object") and not model.get("groups") else _flow(model)
        ported = {n["id"] for n in model["nodes"] if n.get("ports") and k != "class"}
        lines += [f'  click {nid(i)} href "{u}" _blank' for i, u in (links or {}).items() if i not in ported]
    return "\n".join(lines)
