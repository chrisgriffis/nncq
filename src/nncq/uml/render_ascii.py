"""Plain-text diagrams sized for an agent's prompt window (~110 columns): boxes for types and components, a
column layout for sequences, a tree plus relation list for everything else. '(!)' marks stale members."""
from __future__ import annotations

import re

WIDTH = 110
_ARROW = {"inherits": "--|>", "realizes": "..|>", "composes": "*-->", "aggregates": "o-->", "associates": "--->",
          "depends": "..>", "depends-on": "..>", "uses": "..>", "imports": "..>", "includes": "..>",
          "instantiates": "..> «create»", "instance_of": "..> «instanceOf»", "connects": "----", "flows-to": "==>",
          "contains": "<>--", "calls": "--->", "deploys": "..> «deploy»", "link": "----"}


def _box(title: list[str], sections: list[list[str]], cap: int = 52) -> list[str]:
    rows = [r[:cap] for r in title] + [x[:cap] for s in sections for x in s]
    w = max([len(r) for r in rows] + [8])
    line = "+" + "-" * (w + 2) + "+"
    out = [line] + [f"| {r[:cap]:<{w}} |" for r in title]
    for s in sections:
        out.append(line)
        out += [f"| {x[:cap]:<{w}} |" for x in s]
    return out + [line]


def _pack(boxes: list[list[str]]) -> list[str]:
    out, row, used = [], [], 0
    for b in boxes + [None]:
        bw = len(b[0]) if b else 0
        if b is None or (row and used + bw + 2 > WIDTH):
            h = max(len(x) for x in row) if row else 0
            for i in range(h):
                out.append("  ".join((x[i] if i < len(x) else " " * len(x[0])) for x in row).rstrip())
            out.append("")
            row, used = [], 0
        if b is not None:
            row.append(b)
            used += bw + 2
    return out


def _pnames(params: str) -> str:
    out = []
    for p in re.split(r",(?![^<>()\[\]]*[>)\]])", params or ""):
        p = p.split("=")[0].strip()
        nm = p.split(":")[0].strip() if ":" in p.replace("::", "") else (re.findall(r"\w+", p) or [""])[-1]
        if nm and nm not in ("self", "cls", "void"):
            out.append(nm)
    return ", ".join(out)


def _member(m: dict) -> str:
    mods = set(m.get("mods") or [])
    flag = " {abstract}" if mods & {"pure", "abstract"} else " {static}" if "static" in mods else ""
    if m.get("kind") == "method":
        return f"{m.get('vis') or ' '} {m['name']}({_pnames(m.get('params', ''))[:40]})" + (f" : {m['type']}" if m.get("type") else "") + flag
    return f"{m.get('vis') or ' '} {m['name']}" + (f" : {m['type']}" if m.get("type") else "") + flag


def _label(n: dict) -> str:
    return (n.get("label") or n["id"]) + (" (!)" if n.get("stale") else "")


def _relations(model: dict, names: dict) -> list[str]:
    out = []
    for e in model.get("edges", []):
        a = names.get(e.get("from_frame") or e["from"], e["from"])
        b = names.get(e.get("to_frame") or e["to"], e["to"])
        if e.get("from_port"):
            a += "." + e["from_port"].rsplit("_", 1)[-1]
        extra = " ".join(x for x in (e.get("label") or "", f"[{e['mult_to']}]" if e.get("mult_to") else "",
                                     f"x{e['n']}" if e.get("n") else "") if x)
        out.append(f"  {a} {_ARROW.get(e['kind'], '--' + e['kind'] + '-->')} {b}" + (f"   {extra}" if extra else ""))
    return out


def _class(model: dict) -> list[str]:
    boxes = []
    for n in model["nodes"]:
        st = [s for s in (n.get("stereotype") or "").split(",") if s]
        if n.get("type") not in (None, "class", "struct", "instance"):
            st.insert(0, n["type"])
        title = ([f"<<{', '.join(dict.fromkeys(st))}>>"] if st else []) + [_label(n)]
        ms = n.get("members") or []
        fields = [_member(m) for m in ms if m.get("kind") != "method"][:12]
        meths = [_member(m) for m in ms if m.get("kind") == "method"][:16]
        more = len(ms) - len(fields) - len(meths)
        secs = [s for s in (fields, meths + ([f"  ... {more} more"] if more > 0 else [])) if s]
        secs += [[s] for s in (n.get("slots") or [])[:3]]
        if n.get("note"):
            secs.append([n["note"]])
        boxes.append(_box(title, secs))
    return _pack(boxes)


def _component(model: dict) -> list[str]:
    boxes = []
    for n in model["nodes"]:
        prov = [f"o {p['name']}" + (f" ({p['consumers']})" if p.get("consumers") else "") for p in n.get("ports", [])
                if p.get("dir") != "required"]
        req = [f"( {p['name']}" for p in n.get("ports", []) if p.get("dir") == "required"]
        title = [f"<<{n.get('stereotype') or n.get('type')}>>", _label(n)]
        secs = [s for s in (prov, req) if s]
        boxes.append(_box(title, secs or [["(no ports)"]], 44))
    return _pack(boxes)


def _tree(model: dict) -> list[str]:
    nodes = {n["id"]: n for n in model["nodes"]}
    groups = model.get("groups") or []
    placed = set()
    out = []

    def line(n: dict, ind: str) -> str:
        st = n.get("stereotype") or (n.get("type") if n.get("type") not in ("package",) else "")
        loc = (n.get("ref") or {}).get("path") or ""
        files = (n.get("info") or {}).get("files")
        return f"{ind}- {'«' + st + '» ' if st else ''}{_label(n)}" + (f"  ({files} files)" if files else "") + \
            (f"  {loc}" if loc and loc != n.get("label") else "") + (f"  -- {n['note']}" if n.get("note") else "")

    def grp(g: dict, ind: str) -> None:
        out.append(f"{ind}[{g.get('label') or g['id']}]")
        for x in g.get("nodes", []):
            if x in nodes:
                placed.add(x)
                out.append(line(nodes[x], ind + "  "))
        for c in groups:
            if c.get("parent") == g["id"]:
                grp(c, ind + "  ")
    known = {g["id"] for g in groups}
    for g in groups:
        if not g.get("parent") or g["parent"] not in known:
            grp(g, "")
    rest = [n for n in model["nodes"] if n["id"] not in placed]
    out += [line(n, "") for n in rest]
    return out


def _sequence(model: dict) -> list[str]:
    parts = model["nodes"]
    col = {p["id"]: i for i, p in enumerate(parts)}
    msgs = model.get("messages", [])
    w = max(14, min(34, max([len(m.get("label", "")) + 6 for m in msgs] + [len(p["label"]) + 2 for p in parts])))
    x = [i * w + w // 2 for i in range(len(parts))]
    width = len(parts) * w
    head = [" "] * width
    for p, cx in zip(parts, x):
        lab = _label(p)[:w - 2]
        s = max(0, cx - len(lab) // 2)
        head[s:s + len(lab)] = lab
    life = [" "] * width
    for cx in x:
        life[cx] = "|"
    out = ["".join(head).rstrip(), "".join(life).rstrip()]
    for m in msgs:
        row = life[:]
        a, b = col.get(m["from"]), col.get(m["to"])
        if a is None or b is None:
            continue
        lab = m.get("label", "")
        if m.get("kind") == "reply":
            lab = lab or "return"
        if a == b:
            t = f"|--+ {lab}"[:w + 10]
            row[x[a]:x[a] + len(t)] = t
            out.append("".join(row[:max(width, x[a] + len(t))]).rstrip())
            continue
        lo, hi = sorted((x[a], x[b]))
        dash = "." if m.get("kind") in ("reply", "async") else "-"
        for i in range(lo + 1, hi):
            row[i] = dash
        if b > a:
            row[hi - 1] = ">"
        else:
            row[lo + 1] = "<"
        t = f" {lab} "[:hi - lo - 3]
        s = lo + 2
        row[s:s + len(t)] = t
        out.append("".join(row).rstrip())
    if model.get("cut"):
        out.append(f"... {model['cut']}")
    return out


def render(model: dict) -> str:
    k = model.get("kind")
    names = {n["id"]: n.get("label") or n["id"] for n in model["nodes"]}
    for n in model["nodes"]:
        for p in n.get("ports") or []:
            names[p["id"]] = f"{names[n['id']]}.{p['name']}"
    for g in model.get("groups") or []:
        names[g["id"]] = g.get("label") or g["id"]
    out = [f"{model.get('title', '')}", ""]
    if k == "sequence":
        out += _sequence(model)
        return "\n".join(out)
    if k in ("class", "object") and not model.get("groups"):
        out += _class(model)
    elif k == "component":
        out += _component(model)
    else:
        out += _tree(model)
    rel = _relations(model, names)
    if rel:
        out += ["Relations:"] + rel[:200] + ([f"  ... {len(rel) - 200} more"] if len(rel) > 200 else [])
    for x in model.get("notes", []):
        out.append(f"Note{' on ' + names.get(x['on'], x['on']) if x.get('on') else ''}: {x['text']}")
    stale = [n for n in model["nodes"] if n.get("stale")]
    if stale:
        out.append("(!) stale: " + ", ".join(f"{_label(n)} {n['stale']}" for n in stale))
    return "\n".join(out)
