"""Agent-authored views and compositions: schema, validation, binding to the index, materialization.

Element ids (stable, how anything in the model is referenced):
  sym:<qualname>[@<path>]   a symbol (class, function, field, ...)      comp:<name>   component / artifact
  port:<component>/<port>   a component pin (provided: file name; required: 'needs <component>')
  pkg:<folder>   file:<path>   cfg:<build configuration>   composition:<id>   view:<key>

A view (diagram) references generated elements by id and may add bespoke nodes (no ref). A composition is a
semantic model element, not a diagram: named members (generated elements, other compositions, views, bespoke
parts) + typed connectors between them. Either may reference the other (cycle-checked). Bindings are re-checked
when the files they come from change: members that changed or vanished are flagged in a staleness report, never
dropped.
"""
from __future__ import annotations

import re

from nncq import graph
from nncq.store import Store
from nncq.uml import views as V

SCHEMES = ("sym:", "comp:", "port:", "pkg:", "file:", "cfg:", "composition:", "view:")
NODE_TYPES = ("class", "interface", "struct", "enum", "union", "record", "trait", "instance", "component", "port",
              "package", "artifact", "config", "external", "lifeline", "actor", "module", "function", "note", "block",
              "part", "composition", "view", "state", "usecase", "data")
EDGE_KINDS = ("inherits", "realizes", "composes", "aggregates", "associates", "depends", "instantiates", "calls",
              "imports", "includes", "connects", "deploys", "contains", "uses", "flows-to", "depends-on",
              "instance_of", "link", "other")
CONNECTOR_KINDS = ("contains", "uses", "flows-to", "depends-on", "realizes", "connects", "other")
MESSAGE_KINDS = ("call", "reply", "create", "async", "self", "note")
VIEW_KINDS = V.KINDS
_ID = re.compile(r"^[A-Za-z0-9][\w.\-]{0,79}$")

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "ref": {"type": "string", "description": "element id: " + " | ".join(s + "..." for s in SCHEMES)},
        "port": {"type": "object", "required": ["name"], "properties": {
            "id": {"type": "string"}, "name": {"type": "string"},
            "dir": {"enum": ["provided", "required", "inout"]}, "iface": {"type": "string"}}},
        "bespoke": {"type": "object", "required": ["label"], "properties": {
            "type": {"enum": list(NODE_TYPES)}, "label": {"type": "string"}, "description": {"type": "string"},
            "stereotype": {"type": "string"}, "ports": {"type": "array", "items": {"$ref": "#/$defs/port"}}}},
    },
    "oneOf": [
        {"title": "view", "type": "object", "required": ["kind", "title", "nodes"], "properties": {
            "key": {"type": "string", "pattern": _ID.pattern, "description": "stable id; same key = update"},
            "kind": {"enum": list(VIEW_KINDS)}, "title": {"type": "string"}, "description": {"type": "string"},
            "group": {"type": "string"}, "layout": {"type": "object", "properties": {
                "direction": {"enum": ["LR", "TB", "RL", "BT"]}}},
            "nodes": {"type": "array", "items": {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "string", "pattern": r"^[A-Za-z_]\w{0,59}$"}, "ref": {"$ref": "#/$defs/ref"},
                "label": {"type": "string"}, "type": {"enum": list(NODE_TYPES)}, "stereotype": {"type": "string"},
                "members": {"description": "'auto' (from the index) | 'none' | [{name, kind, type, vis, params}]"},
                "ports": {"type": "array", "items": {"$ref": "#/$defs/port"}},
                "expand": {"type": "boolean", "description": "composition: draw its members inside a frame"},
                "note": {"type": "string"}}}},
            "edges": {"type": "array", "items": {"type": "object", "required": ["from", "to", "kind"], "properties": {
                "from": {"type": "string"}, "to": {"type": "string"}, "kind": {"enum": list(EDGE_KINDS)},
                "label": {"type": "string"}, "mult_from": {"type": "string"}, "mult_to": {"type": "string"},
                "from_port": {"type": "string"}, "to_port": {"type": "string"}}}},
            "auto_edges": {"type": "boolean", "description": "add index relations between bound nodes"},
            "messages": {"type": "array", "items": {"type": "object", "required": ["from", "to", "label"],
                                                    "properties": {"from": {"type": "string"}, "to": {"type": "string"},
                                                                   "label": {"type": "string"},
                                                                   "kind": {"enum": list(MESSAGE_KINDS)},
                                                                   "ref": {"$ref": "#/$defs/ref"}}}},
            "groups": {"type": "array", "items": {"type": "object", "required": ["id", "nodes"], "properties": {
                "id": {"type": "string"}, "label": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "string"}}}}},
            "notes": {"type": "array", "items": {"type": "object", "required": ["text"], "properties": {
                "text": {"type": "string"}, "on": {"type": "string"}}}}}},
        {"title": "composition", "type": "object", "required": ["id", "name", "members"], "properties": {
            "id": {"type": "string", "pattern": _ID.pattern, "description": "stable id; same id = update"},
            "name": {"type": "string"}, "description": {"type": "string"}, "author": {"enum": ["agent", "user"]},
            "group": {"type": "string"},
            "members": {"type": "array", "items": {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "string", "pattern": r"^[A-Za-z_]\w{0,59}$"}, "ref": {"$ref": "#/$defs/ref"},
                "bespoke": {"$ref": "#/$defs/bespoke"}, "role": {"type": "string"},
                "expand": {"type": "boolean", "description": "composition / view member: draw inside a frame"}}}},
            "connectors": {"type": "array", "items": {"type": "object", "required": ["from", "to", "kind"],
                                                      "properties": {"from": {"type": "string"},
                                                                     "to": {"type": "string"},
                                                                     "kind": {"enum": list(CONNECTOR_KINDS)},
                                                                     "label": {"type": "string"}}}}}},
    ],
}

RULES = """Rules:
- Upsert by key (views) / id (compositions): the same key replaces; never create a near-duplicate. List first.
- Reference generated elements by id (sym:/comp:/port:/pkg:/file:/cfg:/composition:). Get ids from
  uml_find, uml_view(format=json) or uml_views(symbol=...). A node or member without ref is bespoke.
- Bespoke: only what the index cannot provide (concepts, external actors, notes). Label every one.
- members:"auto" pulls fields/methods from the index; auto_edges:true adds index relations between bound nodes.
- Composition (semantic grouping) and view (diagram) are separate kinds. Either may reference the other:
  composition member {"id":"d","ref":"view:<key>"}; view node {"id":"x","ref":"composition:<id>","expand":true}.
  Compositions nest; reference cycles are rejected.
- After upsert check `unbound` (ref did not resolve) and `unverified_edges` (not found in the index: fine for
  intent, wrong for facts). Stale views/compositions carry `staleness`: per member changed|missing|moved;
  repair by re-upserting with corrected refs.
- Output: format=ascii for chat, markdown for files/PRs (pure ```mermaid block + element list linking to the
  dashboard; links carry revisions, the page flags outdated exports), json for edits."""

EXAMPLE_VIEW = {"key": "device-connect", "kind": "class", "title": "How devices connect",
                "nodes": [{"id": "mgr", "ref": "sym:servicehost::ConnectionManager", "members": "auto"},
                          {"id": "conn", "ref": "sym:AndroidConnection"},
                          {"id": "usb", "type": "external", "label": "USB stack", "note": "libusb"}],
                "edges": [{"from": "mgr", "to": "conn", "kind": "uses", "label": "opens"},
                          {"from": "conn", "to": "usb", "kind": "depends"}], "auto_edges": True}
EXAMPLE_COMPOSITION = {"id": "device-access", "name": "Device access", "description": "everything between CLI and device",
                       "members": [{"id": "cli", "ref": "comp:servicehostCLI"},
                                   {"id": "lib", "ref": "comp:servicehost"},
                                   {"id": "farm", "bespoke": {"type": "block", "label": "Device farm",
                                                               "description": "physical devices on USB hubs"}}],
                       "connectors": [{"from": "cli", "to": "lib", "kind": "uses"},
                                      {"from": "lib", "to": "farm", "kind": "flows-to", "label": "adb / usb"}]}


def protocol() -> dict:
    return {"element_ids": {s[:-1]: d for s, d in zip(SCHEMES, (
        "symbol by qualified name, optional @path", "component / build artifact by name",
        "component pin: comp/<file> (provided) or comp/needs <component> (required)", "folder",
        "indexed file", "build configuration / toolchain / preset", "composition by id", "stored view by key"))},
        "rules": RULES, "schema": SCHEMA, "example_view": EXAMPLE_VIEW, "example_composition": EXAMPLE_COMPOSITION,
        "tools": {"uml_find": "element ids by name (symbols, components, compositions, views)",
                  "uml_view": "auto view (kind+subject) or stored one (key); format ascii|markdown|mermaid|json",
                  "uml_views": "list, grouped (kind|group|origin), or which views contain a symbol",
                  "uml_upsert": "store a view or composition (JSON per schema)", "uml_delete": "remove by key/id",
                  "uml_build": "build / refresh the model"}}


# ------------------------------------------------------------------ validation

def validate(obj: dict) -> tuple[str, list[str]]:
    """('view' | 'composition', errors)."""
    if not isinstance(obj, dict):
        return "", ["expected a JSON object"]
    errs: list[str] = []
    if "members" in obj and "kind" not in obj:
        if not _ID.match(str(obj.get("id", ""))):
            errs.append("id: required, letters/digits/._- (max 80)")
        if not obj.get("name"):
            errs.append("name: required")
        ids = _ids(obj.get("members"), "members", errs)
        for i, m in enumerate(obj.get("members") or []):
            if isinstance(m, dict) and not m.get("ref") and not isinstance(m.get("bespoke"), dict):
                errs.append(f"members[{i}]: needs ref (generated / composition) or bespoke {{type, label}}")
            elif isinstance(m, dict) and isinstance(m.get("bespoke"), dict) and not m["bespoke"].get("label"):
                errs.append(f"members[{i}].bespoke.label: required")
            if isinstance(m, dict) and m.get("ref") and not str(m["ref"]).startswith(SCHEMES):
                errs.append(f"members[{i}].ref: must start with one of {', '.join(SCHEMES)}")
        for i, c in enumerate(obj.get("connectors") or []):
            _edge_check(c, i, ids, CONNECTOR_KINDS, "connectors", errs)
        if obj.get("author", "agent") not in ("agent", "user"):
            errs.append("author: agent | user")
        return "composition", errs
    if obj.get("kind") not in VIEW_KINDS:
        errs.append(f"kind: one of {', '.join(VIEW_KINDS)}")
    if not obj.get("title"):
        errs.append("title: required")
    if obj.get("key") is not None and not _ID.match(str(obj["key"])):
        errs.append("key: letters/digits/._- (max 80), no ':'")
    ids = _ids(obj.get("nodes"), "nodes", errs)
    for i, n in enumerate(obj.get("nodes") or []):
        if isinstance(n, dict):
            if n.get("ref") and not str(n["ref"]).startswith(SCHEMES):
                errs.append(f"nodes[{i}].ref: must start with one of {', '.join(SCHEMES)}")
            if n.get("type") and n["type"] not in NODE_TYPES:
                errs.append(f"nodes[{i}].type: one of {', '.join(NODE_TYPES)}")
            if not n.get("ref") and not n.get("label"):
                errs.append(f"nodes[{i}]: bespoke node needs a label")
    for i, e in enumerate(obj.get("edges") or []):
        _edge_check(e, i, ids, EDGE_KINDS, "edges", errs)
    for i, m in enumerate(obj.get("messages") or []):
        if not isinstance(m, dict) or m.get("from") not in ids or m.get("to") not in ids:
            errs.append(f"messages[{i}]: from/to must be node ids")
        elif m.get("kind", "call") not in MESSAGE_KINDS:
            errs.append(f"messages[{i}].kind: one of {', '.join(MESSAGE_KINDS)}")
    for i, g in enumerate(obj.get("groups") or []):
        if not isinstance(g, dict) or not g.get("id") or any(x not in ids for x in g.get("nodes") or []):
            errs.append(f"groups[{i}]: id + nodes (node ids)")
    return "view", errs


def _ids(items, what: str, errs: list[str]) -> set[str]:
    if not isinstance(items, list) or not items:
        errs.append(f"{what}: non-empty list")
        return set()
    ids: set[str] = set()
    for i, n in enumerate(items):
        nid = n.get("id") if isinstance(n, dict) else None
        if not nid or not re.fullmatch(r"[A-Za-z_]\w{0,59}", str(nid)):
            errs.append(f"{what}[{i}].id: required, [A-Za-z_][A-Za-z0-9_]*")
        elif nid in ids:
            errs.append(f"{what}[{i}].id: duplicate {nid!r}")
        else:
            ids.add(nid)
    return ids


def _edge_check(e, i, ids, kinds, what, errs) -> None:
    if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
        errs.append(f"{what}[{i}]: from/to must be ids in this object")
    elif e.get("kind") not in kinds:
        errs.append(f"{what}[{i}].kind: one of {', '.join(kinds)}")


# ------------------------------------------------------------------ binding

def element_id(kind: str, name: str, path: str = "") -> str:
    return {"sym": f"sym:{name}" + (f"@{path}" if path else "")}.get(kind, f"{kind}:{name}")


def norm_ref(ref: str) -> str:
    """Generator deps (raw qualnames / '@file') -> element ids."""
    return ref if ref.startswith(SCHEMES) else f"sym:{ref}"


def resolve(ctx: V.Ctx, ref: str) -> dict | None:
    """Element id -> {id, type, label, path, line, fp, ...}; None when it no longer exists."""
    import hashlib
    st = ctx.st
    h = lambda *xs: hashlib.sha1("\x1f".join(str(x) for x in xs).encode()).hexdigest()[:12]
    scheme, _, rest = ref.partition(":")
    if scheme == "sym":
        q, _, path = rest.partition("@")
        rows = ctx.q("WHERE s.qualname=?" + (" AND f.path=?" if path else "") + " ORDER BY "
                     "CASE WHEN s.mods LIKE '%decl%' THEN 1 ELSE 0 END, length(f.path)", (q, path) if path else (q,))
        if not rows:
            d = [x for x in ctx.find(q, path) if graph.norm(x.qualname).endswith(graph.norm(q))]
            if not d:
                return None
            rows = d
            moved = bool(path)
        else:
            moved = False
        s = next((r for r in rows if r.kind in V.TYPE_KINDS), rows[0])
        mem = st.q("SELECT name, kind, rtype, params, visibility FROM symbols WHERE parent=? AND file_id=? "
                   "ORDER BY name, params", (s.qualname, s.file_id))
        bases = st.q("SELECT dst FROM edges WHERE src=? AND kind='inherits' AND file_id=? ORDER BY dst",
                     (s.qualname, s.file_id))
        return {"id": ref, "sym": s, "type": s.kind, "label": s.name, "path": s.path, "line": s.start,
                "moved_to": s.path if moved else None,
                "fp": h(s.kind, s.signature, s.rtype, s.params, s.mods, s.visibility, s.stereo, mem, bases)}
    if scheme == "comp":
        r = st.q("SELECT id, name, kind, dir, source, line FROM components WHERE name=?", (rest,))
        if not r:
            return None
        cid, name, kind, d, source, line = r[0]
        return {"id": ref, "type": "external" if kind == "external" else "component", "label": name, "kind": kind,
                "path": source or d or "", "line": line or 1, "dir": d, "fp": h(name, kind, d, source)}
    if scheme == "port":
        comp, _, port = rest.partition("/")
        r = st.q("SELECT id, dir FROM components WHERE name=?", (comp,))
        if not r:
            return None
        if port.startswith("needs "):
            ok = st.q("SELECT 1 FROM components WHERE name=?", (port[6:],))
        else:
            ok = st.q("SELECT f.path FROM comp_files cf JOIN files f ON f.id=cf.file_id WHERE cf.comp_id=? AND "
                      "(f.path=? OR f.path LIKE ?)", (r[0][0], port, "%/" + port))
        if not ok:
            return None
        return {"id": ref, "type": "port", "label": port, "component": comp, "path": ok[0][0] if ok and
                isinstance(ok[0][0], str) else (r[0][1] or ""), "line": 1, "fp": h(comp, port)}
    if scheme in ("pkg", "file"):
        p = rest.strip("/")
        if scheme == "file":
            r = st.q("SELECT path FROM files WHERE path=?", (p,))
        else:
            r = st.q("SELECT path FROM files WHERE path LIKE ? LIMIT 1", (p + "/%",))
        if not r:
            return None
        return {"id": ref, "type": "package" if scheme == "pkg" else "artifact", "label": p.rsplit("/", 1)[-1] or p,
                "path": p, "line": 1, "fp": h(scheme, p)}
    if scheme == "cfg":
        r = st.q("SELECT name, kind, source, info FROM build_configs WHERE name=? LIMIT 1", (rest,))
        if not r:
            return None
        return {"id": ref, "type": "config", "label": rest, "path": r[0][2] or "", "line": 1,
                "fp": h(*r[0]), "stereotype": r[0][1]}
    if scheme in ("composition", "view"):
        r = st.q("SELECT key, title, hash, stale, kind FROM views WHERE key=?",
                 (ref if scheme == "composition" else rest,))
        if not r or (r[0][4] == "composition") != (scheme == "composition"):
            return None
        return {"id": ref, "type": "composition" if scheme == "composition" else "view", "label": r[0][1],
                "path": "", "line": 0, "fp": h(r[0][2], r[0][3]), "stale": bool(r[0][3])}
    return None


def bind(st: Store, obj: dict, what: str) -> tuple[list[tuple[str, str, str]], list[dict], list[str]]:
    """[(path, element id, fp)] of everything referenced, unbound refs, scopes (component folders)."""
    ctx = V.Ctx(st)
    refs, unbound, scopes = [], [], []
    items = obj.get("members" if what == "composition" else "nodes") or []
    extra = [m.get("ref") for m in obj.get("messages") or [] if isinstance(m, dict) and m.get("ref")]
    for ref, owner in [(n.get("ref"), n.get("id")) for n in items if n.get("ref")] + [(r, "message") for r in extra]:
        b = resolve(ctx, ref)
        if b is None:
            unbound.append({"id": owner, "ref": ref})
            refs.append(("", ref, ""))
            continue
        refs.append((b.get("path") or "", ref, b["fp"]))
        if b.get("dir"):
            scopes.append(b["dir"].rstrip("/") + "/")
        elif ref.startswith("port:"):
            r = st.q("SELECT dir FROM components WHERE name=?", (ref[5:].split("/", 1)[0],))
            if r and r[0][0]:
                scopes.append(r[0][0].rstrip("/") + "/")
        elif ref.startswith("pkg:"):
            scopes.append(ref[4:].strip("/") + "/")
    return refs, unbound, scopes


def composition_refs(obj: dict) -> list[str]:
    """composition:/view: element ids an object references (for cycle checks)."""
    return [m["ref"] for m in (obj.get("members") or []) + (obj.get("nodes") or [])
            if isinstance(m, dict) and str(m.get("ref", "")).startswith(("composition:", "view:"))]


# ------------------------------------------------------------------ materialization (spec -> renderable model)

def _node_for(ctx: V.Ctx, n: dict, kind: str, b: dict | None, depth: int, out_groups: list, out_nodes: list,
              out_edges: list, seen: set) -> dict:
    nid = n["id"]
    node = {"id": nid, "label": n.get("label") or (b or {}).get("label") or nid, "type": n.get("type") or "",
            "stereotype": n.get("stereotype", ""), "note": n.get("note") or n.get("description", ""),
            "element": n.get("ref", ""), "bespoke": not n.get("ref")}
    if n.get("ref") and b is None:
        node.update(type=node["type"] or "note", unbound=True)
        return node
    if b is None:
        node["type"] = node["type"] or "block"
        node["ports"] = [{"id": f"{nid}_{V.slug(p.get('id') or p['name'])}", **p} for p in n.get("ports") or []]
        return node
    loc = ctx.loc(b["path"], b["line"]) if b.get("path") else {}
    sym = b.get("sym")
    if sym is not None and sym.kind in V.TYPE_KINDS:
        want = n.get("members", "auto" if kind in ("class", "object") else "none")
        tn = V._type_node(ctx, sym, want == "auto")
        if isinstance(want, list):
            tn["members"] = [{"id": V.slug(m.get("name", "m")), "name": m.get("name", ""), "kind": m.get("kind", "field"),
                              "vis": m.get("vis", ""), "type": m.get("type", ""), "params": m.get("params", ""),
                              "mods": m.get("mods", [])} for m in want if isinstance(m, dict)]
        tn.update(id=nid, label=n.get("label") or tn["label"], element=n["ref"], note=node["note"])
        if n.get("type"):
            tn["type"] = n["type"]
        if n.get("stereotype"):
            tn["stereotype"] = n["stereotype"]
        return tn
    if sym is not None:
        node.update(type=node["type"] or ("lifeline" if kind == "sequence" else "function" if sym.kind in V.FUNCS
                                          else sym.kind), ref=ctx.ref(sym), qualname=sym.qualname,
                    decl=sym.signature or "")
        return node
    node.update(type=node["type"] or b["type"], ref={**loc, **({"dir": b["dir"]} if b.get("dir") else {})})
    if b.get("stereotype") and not node["stereotype"]:
        node["stereotype"] = b["stereotype"]
    if b["type"] == "component" and kind == "component" and "ports" not in n:
        try:
            m, _, _ = V.component_view(ctx.st, b["label"], externals=False)
            src = next((x for x in m["nodes"] if x["label"] == b["label"]), None)
            if src:
                node["ports"] = [{**p, "id": f"{nid}_{p['id'].rsplit('_', 1)[-1]}"} for p in src["ports"]]
                node["stereotype"] = node["stereotype"] or src.get("stereotype", "")
        except ValueError:
            pass
    elif n.get("ports"):
        node["ports"] = [{"id": f"{nid}_{V.slug(p.get('id') or p['name'])}", **p} for p in n["ports"]]
    if b["type"] == "view":
        node["type"] = "view"
        node["stereotype"] = node["stereotype"] or "diagram"
        if n.get("expand") and depth < 3 and n["ref"] not in seen:
            import json
            r = ctx.st.q("SELECT model FROM views WHERE key=?", (n["ref"][5:],))
            sub = json.loads(r[0][0]) if r and r[0][0] else None
            if sub:
                gid = f"{nid}__frame"
                pre = nid + "__"
                out_groups.append({"id": gid, "label": f"«diagram» {sub.get('title', '')}", "nodes": [],
                                   "parent": None, "element": n["ref"]})
                for x in sub.get("nodes", []):
                    y = {**x, "id": pre + x["id"], "members": [], "collapsed": bool(x.get("members"))}
                    y.pop("ports", None)
                    out_nodes.append(y)
                    out_groups[-1]["nodes"].append(y["id"])
                for e in sub.get("edges", []):
                    out_edges.append({**e, "id": pre + e["id"], "from": pre + e["from"], "to": pre + e["to"],
                                      "from_port": None, "to_port": None})
                node["expanded_into"] = gid
        return node
    if b["type"] == "composition":
        node["type"] = "composition"
        if n.get("expand") and depth < 3 and n["ref"] not in seen:
            r = ctx.st.q("SELECT spec FROM views WHERE key=?", (n["ref"],))
            if r:
                import json
                sub = json.loads(r[0][0])
                inner = materialize_composition(ctx.st, sub, depth + 1, prefix=nid + "__", seen=seen | {n["ref"]})
                out_nodes.extend(inner["nodes"])
                out_groups.extend(inner["groups"])          # its first group is its frame (parent None)
                out_edges.extend(inner["edges"])
                node["expanded_into"] = inner["groups"][0]["id"]
    return node


def materialize_view(st: Store, spec: dict) -> dict:
    ctx = V.Ctx(st)
    kind = spec["kind"]
    nodes, edges, groups = [], [], []
    extra_nodes: list = []
    bound: dict[str, dict] = {}
    frame: dict[str, str] = {}
    for n in spec.get("nodes") or []:
        b = resolve(ctx, n["ref"]) if n.get("ref") else None
        if b is not None:
            bound[n["id"]] = b
        node = _node_for(ctx, n, kind, b, 0, groups, extra_nodes, edges, set())
        if node.get("expanded_into"):
            frame[n["id"]] = node["expanded_into"]        # drawn as its frame
            continue
        nodes.append(node)
    nodes += extra_nodes
    for i, e in enumerate(spec.get("edges") or []):
        edges.append({"id": f"e{i}", "from": e["from"], "to": e["to"], "kind": e["kind"], "label": e.get("label", ""),
                      **{k: e[k] for k in ("mult_from", "mult_to", "from_port", "to_port") if e.get(k)},
                      "authored": True})
    if spec.get("auto_edges"):
        edges += _auto_edges(st, spec, bound, {e["from"] + e["to"] for e in edges})
    for e in edges:
        for end in ("from", "to"):
            if frame.get(e[end]):
                e[end + "_frame"] = frame[e[end]]
    for g in spec.get("groups") or []:
        groups.append({"id": g["id"], "label": g.get("label", g["id"]), "nodes": list(g["nodes"])})
    msgs = []
    for i, m in enumerate(spec.get("messages") or []):
        mm = {"id": f"m{i}", "from": m["from"], "to": m["to"], "label": m["label"], "kind": m.get("kind", "call"),
              "depth": 1}
        if m.get("ref"):
            b = resolve(ctx, m["ref"])
            if b:
                mm["ref"] = ctx.loc(b["path"], b["line"])
        msgs.append(mm)
    return {"version": V.MODEL_VERSION, "kind": kind, "title": spec["title"], "subject": spec.get("description", ""),
            "layout": spec.get("layout") or {"direction": "LR" if kind != "class" else "TB"}, "nodes": nodes,
            "edges": edges, "messages": msgs, "groups": groups,
            "notes": [{"text": x["text"], "on": x.get("on", "")} for x in spec.get("notes") or []]}


def _auto_edges(st: Store, spec: dict, bound: dict[str, dict], have: set[str]) -> list[dict]:
    syms = {nid: b["sym"] for nid, b in bound.items() if b.get("sym") is not None and b["sym"].kind in V.TYPE_KINDS}
    out = []
    if syms:
        try:
            m, _, _ = V.class_view(st, ",".join(s.qualname for s in syms.values()), depth=0, members="none")
        except ValueError:
            m = {"nodes": [], "edges": []}
        q2id = {n["qualname"]: n["id"] for n in m["nodes"]}
        back = {q2id.get(s.qualname): nid for nid, s in syms.items()}
        for e in m["edges"]:
            a, b = back.get(e["from"]), back.get(e["to"])
            if a and b and a + b not in have:
                out.append({**e, "id": f"auto_{a}_{e['kind']}_{b}", "from": a, "to": b})
    comps = {nid: b["label"] for nid, b in bound.items() if b["type"] in ("component", "external")}
    if len(comps) > 1:
        ids = {v: k for k, v in comps.items()}
        for a, b, n in st.q("SELECT c1.name, c2.name, SUM(fd.n) FROM file_deps fd JOIN comp_files s ON s.file_id=fd.src_id "
                            "JOIN comp_files d ON d.file_id=fd.dst_id JOIN components c1 ON c1.id=s.comp_id JOIN "
                            "components c2 ON c2.id=d.comp_id WHERE c1.id!=c2.id GROUP BY c1.name, c2.name"):
            if a in ids and b in ids and ids[a] + ids[b] not in have:
                out.append({"id": f"auto_{ids[a]}_{ids[b]}", "from": ids[a], "to": ids[b], "kind": "depends",
                            "label": f"x{n}"})
        for a, b, k in st.q("SELECT c.name, d.dst, d.kind FROM comp_deps d JOIN components c ON c.id=d.src_id"):
            if a in ids and b in ids and ids[a] + ids[b] not in have:
                out.append({"id": f"auto_{ids[a]}_{k}_{ids[b]}", "from": ids[a], "to": ids[b], "kind": "depends",
                            "label": k})
    return out


def materialize_composition(st: Store, spec: dict, depth: int = 0, prefix: str = "", seen: set | None = None) -> dict:
    """A composition as a renderable model: one frame (group) holding its members, connectors as edges."""
    ctx = V.Ctx(st)
    seen = seen or {"composition:" + spec["id"]}
    nodes, edges, groups, extra = [], [], [], []
    frame = prefix + "frame_" + V.slug(spec["id"])
    exp: dict[str, str] = {}
    for m in spec.get("members") or []:
        n = {"id": prefix + m["id"], "ref": m.get("ref"), **(m.get("bespoke") or {})}
        if m.get("role"):
            n["note"] = (n.get("note", "") + " role: " + m["role"]).strip()
        if m.get("ref", "").startswith(("composition:", "view:")):
            n["expand"] = m.get("expand", depth < 2 and m["ref"].startswith("composition:"))
        b = resolve(ctx, m["ref"]) if m.get("ref") else None
        node = _node_for(ctx, n, "composition", b, depth, groups, extra, edges, seen)
        if node.get("type") in V.TYPE_KINDS or node.get("type") == "interface":
            node["members"] = []
            node["collapsed"] = True
        if node.get("expanded_into"):
            exp[node["id"]] = node["expanded_into"]
        else:
            nodes.append(node)
    nodes += extra
    for g in groups:
        if g.get("parent") is None:
            g["parent"] = frame
    member_ids = [n["id"] for n in nodes if not any(n["id"] in g["nodes"] for g in groups)]
    groups.insert(0, {"id": frame, "label": f"«composition» {spec.get('name', spec['id'])}", "nodes": member_ids,
                      "element": "composition:" + spec["id"], "parent": None})
    for i, c in enumerate(spec.get("connectors") or []):
        e = {"id": f"{prefix}c{i}", "from": prefix + c["from"], "to": prefix + c["to"], "kind": c["kind"],
             "label": c.get("label", ""), "authored": True}
        for end in ("from", "to"):
            if e[end] in exp:
                e[end + "_frame"] = exp[e[end]]
        edges.append(e)
    return {"version": V.MODEL_VERSION, "kind": "composition", "title": spec.get("name", spec["id"]),
            "subject": spec.get("description", ""), "layout": {"direction": "LR"}, "nodes": nodes, "edges": edges,
            "groups": groups, "messages": [], "notes": [{"text": spec["description"], "on": ""}]
            if spec.get("description") and depth == 0 else []}


def verify_edges(st: Store, spec: dict, what: str) -> list[str]:
    """Authored edges between bound symbols that the index does not show (intent, or a wrong claim)."""
    kinds = {"inherits": ("inherits",), "realizes": ("inherits",), "composes": ("composes",),
             "aggregates": ("aggregates", "composes"), "associates": ("associates", "aggregates", "composes"),
             "calls": ("call",), "instantiates": ("instantiates", "call"), "depends": None}
    nodes = {n["id"]: n.get("ref", "") for n in (spec.get("members") if what == "composition" else spec.get("nodes")) or []}
    out = []
    for e in (spec.get("connectors") if what == "composition" else spec.get("edges")) or []:
        a, b = nodes.get(e["from"], ""), nodes.get(e["to"], "")
        ks = kinds.get(e["kind"], "-")
        if ks == "-" or ks is None or not a.startswith("sym:") or not b.startswith("sym:"):
            continue
        src, dst = a[4:].split("@")[0], graph.short(b[4:].split("@")[0])
        ph = ",".join("?" * len(ks))
        hit = st.q(f"SELECT 1 FROM edges WHERE dst=? AND kind IN ({ph}) AND (src=? OR src LIKE ?) LIMIT 1",
                   (dst, *ks, src, src + "%"))
        if not hit:
            out.append(f"{e['from']} -{e['kind']}-> {e['to']}")
    return out
