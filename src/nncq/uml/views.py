"""Auto views: each kind is a handful of SQL queries over symbols / edges / components (model.py, extract.py).

Every generator returns (model, deps, scopes):
  model   the view's JSON model (MODEL_VERSION, stable node / member / edge ids, a source span and the
          absolute path for every element and relationship, raw declaration text): lossless enough to render
          any format and, later, to map diagram edits back to source (docs/ideas.md)
  deps    {(path, ref)} the files / symbols it was made from: a change there marks the view for re-check
  scopes  {path prefix} folders whose new files could change it (new subclass, new component member)

  class        types + members (visibility, static/abstract/virtual, types, params) + inherits / realizes /
               composes / aggregates / associates (role, multiplicity) / depends / instantiates
  object       instances of types: globals and singletons, members holding them, where they are created
  component    SysML-style: components (build targets / packages / folders) with provided ports (their files
               other components use) and required ports (one per component used), connectors between them
  package      folders at a depth and their #include / import dependencies
  deployment   artifacts (executables, libraries, scripts), link dependencies, build configurations
  sequence     from an entry function, calls in source order (bounded depth), lifelines = classes / modules
  depends      dependency graph around a type (graph.type_deps) or a file (path=, graph.file_deps: include / import),
               both directions, grouped by folder, cycles drawn as dashed back edges
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from nncq import graph, langs
from nncq.store import Store
from nncq.uml.extract import multiplicity, type_names

MODEL_VERSION = 1
KINDS = ("class", "object", "component", "package", "deployment", "sequence", "depends")
TYPE_KINDS = ("class", "struct", "record", "union", "interface", "trait", "enum")
MEMBER_KINDS = ("field", "method", "function", "constant", "property", "variable")
FUNCS = ("function", "method")
VIS = {"public": "+", "private": "-", "protected": "#", "internal": "~"}
REL_KINDS = ("composes", "aggregates", "associates")
_COLS = ("s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature, f.path, s.file_id, "
         "s.visibility, s.mods, s.rtype, s.params, s.tparams, s.stereo, s.start_col, s.end_col")


@dataclass
class Sym:
    id: int
    name: str
    qualname: str
    kind: str
    parent: str
    start: int
    end: int
    signature: str
    path: str
    file_id: int
    visibility: str
    mods: str
    rtype: str
    params: str
    tparams: str
    stereo: str
    start_col: int
    end_col: int

    @property
    def modset(self) -> set[str]:
        return set((self.mods or "").split())


def slug(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_")[:60] or "x"


class Ctx:
    """One generation: the store, the workspace root (for absolute paths) and lookup caches."""

    def __init__(self, store: Store):
        self.st = store
        self.root = store.meta("root") or ""
        self.deps: set[tuple[str, str]] = set()
        self.scopes: set[str] = set()
        self._types: dict[str, list[Sym]] = {}
        self._ids: dict[str, str] = {}
        self._used: set[str] = set()

    def q(self, sql: str, args=()) -> list[Sym]:
        return [Sym(*r) for r in self.st.q(f"SELECT {_COLS} FROM symbols s JOIN files f ON f.id=s.file_id " + sql,
                                           args)]

    def abs(self, path: str) -> str:
        return str(Path(self.root) / path) if self.root and path is not None else path

    def ref(self, s: Sym) -> dict:
        self.deps.add((s.path, s.qualname))
        r = {"q": s.qualname, "path": s.path, "abs": self.abs(s.path), "line": s.start, "end": s.end,
             "kind": s.kind}
        if s.start_col is not None:
            r.update(col=s.start_col, end_col=s.end_col)
        return r

    def loc(self, path: str, line: int, col=None) -> dict:
        r = {"path": path, "abs": self.abs(path), "line": line}
        if col is not None:
            r["col"] = col
        return r

    def nid(self, key: str, label: str) -> str:
        """Stable, readable node id: the same key gets the same id across regenerations."""
        if key in self._ids:
            return self._ids[key]
        base = slug(label)
        i = base if base not in self._used else f"{base}_{hashlib.sha1(key.encode()).hexdigest()[:5]}"
        self._used.add(i)
        self._ids[key] = i
        return i

    # ------------------------------------------------------------ lookups
    def types_named(self, name: str) -> list[Sym]:
        if name not in self._types:
            self._types[name] = self.q(f"WHERE s.name=? AND s.kind IN ({','.join('?' * len(TYPE_KINDS))}) "
                                       "ORDER BY length(f.path), f.path", (name, *TYPE_KINDS))
        return self._types[name]

    def type_for(self, name: str, near: str = "") -> Sym | None:
        """The definition a type name refers to: the only one, else the one nearest the referring file."""
        ts = self.types_named(name)
        if len(ts) <= 1:
            return ts[0] if ts else None
        for depth in (3, 2, 1):
            pre = "/".join(near.split("/")[:depth])
            hit = [t for t in ts if pre and t.path.startswith(pre)]
            if hit:
                return hit[0]
        return ts[0]

    def find(self, name: str, path: str = "", kinds=None) -> list[Sym]:
        defs = graph.find_defs(self.st, name, path, set(kinds) if kinds else None)
        if not defs:
            return []
        rows = self.q("WHERE s.id IN (%s)" % ",".join(str(d.id) for d in defs[:50]))
        order = {d.id: i for i, d in enumerate(defs)}
        return sorted(rows, key=lambda r: order.get(r.id, 99))

    def members(self, quals: list[str]) -> list[Sym]:
        if not quals:
            return []
        return self.q(f"WHERE s.parent IN ({','.join('?' * len(quals))}) AND s.kind IN "
                      f"({','.join('?' * len(MEMBER_KINDS))}) ORDER BY s.start_line", (*quals, *MEMBER_KINDS))

    def comp_of(self, path: str) -> str:
        r = self.st.q("SELECT c.name FROM comp_files cf JOIN files f ON f.id=cf.file_id JOIN components c "
                      "ON c.id=cf.comp_id WHERE f.path=?", (path,))
        return r[0][0] if r else ""


def _merge_members(ctx: Ctx, rows: list[Sym]) -> list[dict]:
    """One entry per member: the in-class declaration (visibility, virtual, ...) merged with its definition."""
    out: dict[tuple, dict] = {}
    for m in rows:
        params = re.sub(r"\s+", " ", m.params or "")
        key = (m.name, m.kind if m.kind != "function" else "method", params if m.kind in FUNCS else "")
        e = out.get(key)
        decl = "decl" in m.modset
        if e is None:
            e = out[key] = {"id": slug(m.qualname.replace("~", "dtor_")) + ("" if m.kind not in FUNCS else "_" + hashlib.sha1(
                params.encode()).hexdigest()[:4]), "name": m.name, "kind": "method" if m.kind in FUNCS else m.kind,
                "vis": VIS.get(m.visibility or "", ""), "type": m.rtype or "", "params": params,
                "mods": sorted(m.modset - {"decl"}), "tparams": m.tparams or "", "decl": m.signature or "",
                "ref": ctx.ref(m)}
        elif decl:
            e.update(vis=VIS.get(m.visibility or "", "") or e["vis"], mods=sorted((set(e["mods"]) | m.modset) - {"decl"}),
                     decl=m.signature or e["decl"], type=m.rtype or e["type"])
            e.setdefault("impl", e["ref"])
            e["ref"] = ctx.ref(m)
        else:
            e["impl"] = ctx.ref(m)
            e["mods"] = sorted(set(e["mods"]) | (m.modset - {"decl"}))
    return list(out.values())


def _type_node(ctx: Ctx, t: Sym, with_members: bool) -> dict:
    stereo = [x for x in (t.stereo or "").split(",") if x]
    typ = t.kind if t.kind in ("interface", "enum", "struct", "union", "record", "trait") else "class"
    if "interface" in stereo:
        typ = "interface"
    node = {"id": ctx.nid("t:" + t.qualname, t.name), "type": typ, "label": t.name, "qualname": t.qualname,
            "stereotype": ",".join(s for s in stereo if s != typ), "tparams": t.tparams or "",
            "decl": t.signature or "", "ref": ctx.ref(t), "component": ctx.comp_of(t.path)}
    if with_members:
        node["members"] = _merge_members(ctx, ctx.members([t.qualname]))
    else:
        node["members"] = []
        node["collapsed"] = True
    return node


def _edge(ctx: Ctx, a: str, b: str, kind: str, label: str = "", path: str = "", line: int = 0, col=None,
          **extra) -> dict:
    e = {"id": f"{a}-{kind}-{b}" + (f"-{slug(label)}" if label else ""), "from": a, "to": b, "kind": kind,
         "label": label}
    if path:
        e["ref"] = ctx.loc(path, line, col)
    e.update({k: v for k, v in extra.items() if v not in (None, "")})
    return e


def _file_types(ctx: Ctx, path: str, cap: int) -> list[Sym]:
    """Types declared in a file plus the types whose members it defines (Foo::bar in a .cpp -> Foo)."""
    ctx.deps.add((path, "@file"))
    out = ctx.q(f"WHERE f.path=? AND s.kind IN ({','.join('?' * len(TYPE_KINDS))}) ORDER BY s.start_line LIMIT ?",
                (path, *TYPE_KINDS, cap))
    have = {t.qualname for t in out}
    fam = set(graph.lang_family(ctx.st, (ctx.st.q("SELECT id FROM files WHERE path=?", (path,)) or [[None]])[0][0]))
    same = lambda d: not fam or bool(set(graph.lang_family(ctx.st, d.file_id)) & fam)
    for (parent,) in ctx.st.q("SELECT s.parent FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? "
                              "AND s.parent!='' GROUP BY s.parent ORDER BY MIN(s.start_line) LIMIT 60", (path,)):
        if parent in have:
            continue
        hit = [d for d in ctx.find(parent, kinds={"type"}) if d.kind in TYPE_KINDS
               and graph.norm(d.qualname) == graph.norm(parent) and same(d)][:1]   # exact, same language
        out += [t for t in hit if t.qualname not in have]
        have |= {t.qualname for t in hit}
    return out[:cap]


def _focus_types(ctx: Ctx, subject: str, under: str, cap: int, path: str = "") -> list[Sym]:
    out: list[Sym] = []
    for name in [x.strip() for x in subject.split(",") if x.strip()]:
        hit = [d for d in ctx.find(name, kinds={"type"}) if d.kind in TYPE_KINDS]
        if hit:
            out.append(hit[0])
    if path:
        out += _file_types(ctx, path, cap)
    if under:
        pre = under.replace("\\", "/").strip("/")
        ctx.scopes.add(pre + "/")
        out += ctx.q(f"WHERE f.path LIKE ? AND s.kind IN ({','.join('?' * len(TYPE_KINDS))}) "
                     "ORDER BY f.path, s.start_line LIMIT ?", (pre + "/%", *TYPE_KINDS, cap))
    seen, res = set(), []
    for t in out:
        if t.qualname not in seen:
            seen.add(t.qualname)
            res.append(t)
    return res[:cap]


# ------------------------------------------------------------------ class

def class_view(st: Store, subject: str = "", under: str = "", depth: int = 1, members: str = "focus",
               max_nodes: int = 40, path: str = "") -> tuple[dict, set, set]:
    """path: the types declared in that file (a Python module also gets its module, imports and importers)."""
    ctx = Ctx(st)
    fpath = path = path.replace("\\", "/").strip("/")          # `path` is reused by the edge loops below
    focus = _focus_types(ctx, subject, under, max_nodes, path)
    if not focus:
        raise ValueError(f"no class/struct/interface/enum matches {subject or under or path!r}")
    for t in focus:
        ctx.scopes.add(posixpath.dirname(t.path) + "/")
    types: dict[str, Sym] = {t.qualname: t for t in focus}
    level = {t.qualname: 0 for t in focus}
    frontier = list(focus)
    for d in range(depth):
        nxt = []
        for t in frontier:
            near = []
            for dst, label in st.q("SELECT dst, label FROM edges WHERE src=? AND kind IN "
                                   "('inherits','composes','aggregates','associates')", (t.qualname,)):
                near.append(ctx.type_for(dst, t.path))
            if t.parent:                                  # a nested type: its enclosing type
                fam = set(graph.lang_family(st, t.file_id))
                near += [x for x in ctx.find(t.parent, kinds={"type"}) if x.kind in TYPE_KINDS      # same language:
                         and graph.norm(x.qualname) == graph.norm(t.parent)                            # a C++ namespace
                         and (not fam or fam & set(graph.lang_family(st, x.file_id)))][:1]              # is no Python class
            lw, la = graph._lang_sql(graph.lang_family(st, t.file_id))       # derived types in the same language
            for (src,) in st.q("SELECT DISTINCT e.src FROM edges e JOIN files f ON f.id=e.file_id WHERE e.dst=? "
                               f"AND e.kind='inherits'{lw} LIMIT 12", (t.name, *la)):
                near += [x for x in ctx.q("WHERE s.qualname=? AND s.kind IN (%s) LIMIT 1" % ",".join(
                    "?" * len(TYPE_KINDS)), (src, *TYPE_KINDS))]
            for n in near:
                if n is not None and n.qualname not in types and len(types) < max_nodes:
                    types[n.qualname] = n
                    level[n.qualname] = d + 1
                    nxt.append(n)
        frontier = nxt
    nodes = {q: _type_node(ctx, t, members == "all" or (members == "focus" and level[q] == 0))
             for q, t in types.items()}
    by_short: dict[str, list[str]] = defaultdict(list)
    for q, t in types.items():
        by_short[t.name].append(q)

    def target(name: str, near: str) -> str | None:
        qs = by_short.get(name)
        if not qs:
            return None
        if len(qs) == 1:
            return qs[0]
        t = ctx.type_for(name, near)
        return t.qualname if t is not None and t.qualname in qs else qs[0]
    edges: dict[str, dict] = {}
    quals = list(types)
    ph = ",".join("?" * len(quals))
    strong: set[tuple[str, str]] = set()
    for src, dst, kind, label, role, line, col, path in st.q(
            f"SELECT e.src, e.dst, e.kind, e.label, e.role, e.line, e.col, f.path FROM edges e JOIN files f "
            f"ON f.id=e.file_id WHERE e.src IN ({ph}) AND e.kind IN ('inherits','composes','aggregates','associates') "
            "ORDER BY e.line", quals):
        tq = target(dst, path)
        if tq is None or tq == src:
            continue
        a, b = nodes[src]["id"], nodes[tq]["id"]
        if kind == "inherits":
            k = "realizes" if nodes[tq]["type"] == "interface" and nodes[src]["type"] != "interface" else "inherits"
            e = _edge(ctx, a, b, k, "", path, line, col)
        else:
            e = _edge(ctx, a, b, kind, role or "", path, line, col, mult_to=multiplicity(label or ""),
                      type=label or "")
        edges.setdefault(e["id"], e)
        strong.add((a, b))
    for q, t in types.items():                            # nested types hang off their enclosing type
        if t.parent in nodes and t.parent != q:
            e = _edge(ctx, nodes[t.parent]["id"], nodes[q]["id"], "contains", "nested", t.path, t.start)
            edges.setdefault(e["id"], e)
    mq = [m for q in quals for m in [q]]
    for src, dst, kind, line, col, path, parent in st.q(
            f"SELECT e.src, e.dst, e.kind, e.line, e.col, f.path, s.parent FROM edges e JOIN files f ON f.id=e.file_id "
            f"JOIN symbols s ON s.qualname=e.src AND s.file_id=e.file_id WHERE s.parent IN ({ph}) "
            "AND e.kind IN ('depends','instantiates','call') LIMIT 20000", mq):
        tq = target(dst, path)
        if tq is None or tq == parent or parent not in nodes:
            continue
        if kind == "call" and not dst[:1].isupper():
            continue
        a, b = nodes[parent]["id"], nodes[tq]["id"]
        if (a, b) in strong:
            continue
        k = "depends" if kind == "depends" else "instantiates"
        e = _edge(ctx, a, b, k, "«create»" if k == "instantiates" else "", path, line, col)
        if not any(x["from"] == a and x["to"] == b and x["kind"] in ("depends", "instantiates") and
                   (x["kind"] == k or k == "depends") for x in edges.values()):
            edges.setdefault(e["id"], e)
    extra_nodes: list[dict] = []
    if fpath:
        extra_nodes = _module_context(ctx, fpath, [nodes[t.qualname] for t in focus if t.path == fpath], edges)
    title = f"Classes: {subject or under or fpath}"
    model = {"version": MODEL_VERSION, "kind": "class", "title": title, "subject": subject or under or fpath,
             "layout": {"direction": "TB"}, "nodes": list(nodes.values()) + extra_nodes, "edges": list(edges.values()),
             "params": {"depth": depth, "members": members, "under": under, **({"path": fpath} if fpath else {})}}
    return model, ctx.deps, ctx.scopes


def _module_context(ctx: Ctx, path: str, declared: list[dict], edges: dict, cap: int = 8) -> list[dict]:
    """A Python module as a node (its top-level functions as members) containing its classes, plus the modules it
    imports and the modules importing it."""
    row = ctx.st.q("SELECT id, lang FROM files WHERE path=?", (path,))
    if not row or row[0][1] != "python":
        return []
    fid = row[0][0]

    def name(p: str) -> str:
        stem = posixpath.splitext(posixpath.basename(p))[0]
        return posixpath.basename(posixpath.dirname(p)) or stem if stem == "__init__" else stem

    def node(p: str, members: list | None = None) -> dict:
        return {"id": ctx.nid("m:" + p, "mod_" + name(p)), "type": "module", "label": name(p),
                "ref": ctx.loc(p, 1), "members": members or []}
    top = ctx.q("WHERE f.path=? AND s.parent='' AND s.kind IN ('function','constant','variable') "
                "ORDER BY s.start_line LIMIT 40", (path,))
    me = node(path, _merge_members(ctx, top))
    out = [me]
    for n in declared:
        e = _edge(ctx, me["id"], n["id"], "contains")
        edges.setdefault(e["id"], e)
    for sql, outgoing in (("SELECT f.path FROM file_deps d JOIN files f ON f.id=d.dst_id WHERE d.src_id=? "
                           "ORDER BY d.n DESC, f.path LIMIT ?", True),
                          ("SELECT f.path FROM file_deps d JOIN files f ON f.id=d.src_id WHERE d.dst_id=? "
                           "ORDER BY d.n DESC, f.path LIMIT ?", False)):
        for (p,) in ctx.st.q(sql, (fid, cap)):
            if p == path:
                continue
            o = node(p)
            if all(x["id"] != o["id"] for x in out):
                out.append(o)
            e = _edge(ctx, me["id"], o["id"], "imports") if outgoing else _edge(ctx, o["id"], me["id"], "imports")
            edges.setdefault(e["id"], e)
    return out


# ------------------------------------------------------------------ object

def object_view(st: Store, subject: str = "", under: str = "", max_nodes: int = 40) -> tuple[dict, set, set]:
    ctx = Ctx(st)
    focus = _focus_types(ctx, subject, under, 12)
    if not focus:
        raise ValueError(f"no type matches {subject or under!r}")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for t in focus:
        ctx.scopes.add(posixpath.dirname(t.path) + "/")
        tn = _type_node(ctx, t, False)
        nodes[tn["id"]] = tn

        def inst(key, name, stereo, ref, note="", slots=None):
            if len(nodes) >= max_nodes:
                return None
            nd = {"id": ctx.nid(key, f"{name or 'anon'}_{t.name}"), "type": "instance",
                  "label": f"{name} : {t.name}" if name else f":{t.name}",
                  "instance_of": tn["id"], "stereotype": stereo, "ref": ref, "slots": slots or [], "note": note}
            nodes.setdefault(nd["id"], nd)
            edges.append(_edge(ctx, nd["id"], tn["id"], "instance_of", ""))
            return nd
        if "singleton" in (t.stereo or ""):
            inst("single:" + t.qualname, "", "singleton", ctx.ref(t), "single shared instance")
        for v in ctx.q("WHERE s.kind='variable' AND s.rtype LIKE ? LIMIT 30", (f"%{t.name}%",)):
            if t.name in type_names(v.rtype or ""):
                inst("var:" + v.qualname + v.path, v.name, "static" if "static" in v.modset else "global", ctx.ref(v),
                     slots=[f"type = {v.rtype}"])
        for f in ctx.q("WHERE s.kind='field' AND s.rtype LIKE ? LIMIT 30", (f"%{t.name}%",)):
            if t.name not in type_names(f.rtype or "") or f.parent == t.qualname:
                continue
            owner = ctx.type_for(re.split(r"::|\.", f.parent)[-1], f.path)
            nd = inst("field:" + f.qualname, f.name, "member", ctx.ref(f), slots=[f"type = {f.rtype}",
                                                                                f"multiplicity = {multiplicity(f.rtype)}"])
            if nd is None:
                break
            if owner is not None:
                on = _type_node(ctx, owner, False)
                nodes.setdefault(on["id"], on)
                edges.append(_edge(ctx, on["id"], nd["id"], "composes", f.name, f.path, f.start))
        sites = st.q("SELECT e.src, e.role, e.label, e.line, e.col, f.path FROM edges e JOIN files f ON f.id=e.file_id "
                     "WHERE e.dst=? AND e.kind='instantiates' ORDER BY f.path, e.line LIMIT 40", (t.name,))
        if not sites:
            sites = [(s, "", "", ln, None, p) for s, ln, p in st.q(
                "SELECT e.src, e.line, f.path FROM edges e JOIN files f ON f.id=e.file_id WHERE e.dst=? "
                "AND e.kind='call' AND e.src!=? ORDER BY f.path, e.line LIMIT 20", (t.name, t.qualname))]
        for src, role, label, line, col, path in sites:
            if src and src.startswith(t.qualname):
                stereo = "static local" if (role or "").startswith("static ") else "created by own class"
            else:
                stereo = "static local" if (role or "").startswith("static ") else "created"
            name = (role or "").replace("static ", "")
            nd = inst(f"site:{path}:{line}:{src}", name, stereo, ctx.loc(path, line, col),
                      note=f"in {src or '(file level)'}")
            if nd is None:
                break
            ctx.deps.add((path, src))
    model = {"version": MODEL_VERSION, "kind": "object", "title": f"Instances: {subject or under}",
             "subject": subject or under, "layout": {"direction": "LR"}, "nodes": list(nodes.values()),
             "edges": edges, "params": {"under": under}}
    return model, ctx.deps, ctx.scopes


# ------------------------------------------------------------------ component (SysML)

MAX_OVERVIEW_SCRIPTS = 20       # a workspace / folder component overview drops script components beyond this


def _comp_rows(st: Store, under: str, subject: str) -> list[tuple]:
    rows = st.q("SELECT c.id, c.name, c.kind, c.dir, c.source, c.line, c.info, "
                "(SELECT COUNT(*) FROM comp_files cf WHERE cf.comp_id=c.id) FROM components c "
                "WHERE c.kind NOT IN ('external')")
    if under:
        pre = under.replace("\\", "/").strip("/")
        rows = [r for r in rows if (r[3] or "") == pre or (r[3] or "").startswith(pre + "/")]
    return rows


def component_view(st: Store, subject: str = "", under: str = "", externals: bool = False,
                   max_ports: int = 6, path: str = "") -> tuple[dict, set, set]:
    """path: focus on the components a build file defines, else the component owning that file."""
    ctx = Ctx(st)
    if not st.q("SELECT 1 FROM components LIMIT 1"):
        raise ValueError("no components yet: build the UML model first (uml_build / nncq uml init)")
    rows = _comp_rows(st, under, subject)
    ctx.scopes.add((under.replace("\\", "/").strip("/") + "/") if under else "")
    dep_rows = st.q("SELECT cs.comp_id, cd.comp_id, fd.kind, fd.dst_id, f.path, SUM(fd.n) FROM file_deps fd "
                    "JOIN comp_files cs ON cs.file_id=fd.src_id JOIN comp_files cd ON cd.file_id=fd.dst_id "
                    "JOIN files f ON f.id=fd.dst_id WHERE cs.comp_id!=cd.comp_id GROUP BY cs.comp_id, cd.comp_id, fd.dst_id")
    keep = {r[0] for r in rows}
    path = path.replace("\\", "/").strip("/")
    sid: set = set()
    if subject:
        sid = {r[0] for r in rows if r[1].lower() == subject.lower()} or {
            r[0] for r in st.q("SELECT id FROM components WHERE name=? COLLATE NOCASE", (subject,))}
        if not sid:
            raise ValueError(f"no component named {subject!r}")
    elif path:
        sid = {r[0] for r in st.q("SELECT id FROM components WHERE source=? AND kind!='external'", (path,))} or {
            r[0] for r in st.q("SELECT cf.comp_id FROM comp_files cf JOIN files f ON f.id=cf.file_id WHERE f.path=?",
                               (path,))}
        if not sid:
            raise ValueError(f"no component defines or owns {path!r}")
        ctx.deps.add((path, "@file"))
    if sid:
        nb = {a for a, b, *_ in dep_rows if b in sid} | {b for a, b, *_ in dep_rows if a in sid}
        nb |= {r[0] for r in st.q("SELECT c2.id FROM comp_deps d JOIN components c2 ON c2.name=d.dst "
                                  f"WHERE d.src_id IN ({','.join(map(str, sid))})")}
        keep = sid | nb
        rows = [r for r in st.q("SELECT c.id, c.name, c.kind, c.dir, c.source, c.line, c.info, "
                                "(SELECT COUNT(*) FROM comp_files cf WHERE cf.comp_id=c.id) FROM components c")
                if r[0] in keep and (externals or r[2] != "external")]
    byid = {r[0]: r for r in rows}
    hidden = 0
    if not sid:
        scripts = [r for r in rows if r[2] == "script"]
        if len(scripts) > MAX_OVERVIEW_SCRIPTS:      # runnable .py files: hundreds in a big repo swamp the overview
            hidden = len(scripts)
            rows = [r for r in rows if r[2] != "script"]
            byid = {r[0]: r for r in rows}
    used: dict[int, Counter] = defaultdict(Counter)        # provider -> file -> consumer count
    pairs: dict[tuple[int, int], dict[str, int]] = defaultdict(dict)
    for a, b, kind, fid, fp, n in dep_rows:
        if a in byid and b in byid:
            used[b][fp] += 1
            pairs[(a, b)][fp] = pairs[(a, b)].get(fp, 0) + n
    nodes, edges = [], []
    port_of: dict[tuple[int, str], str] = {}
    nid = {}
    for cid, name, kind, d, source, line, info, nfiles in rows:
        i = ctx.nid("c:" + name, name)
        nid[cid] = i
        ports = []
        top = [p for p, _ in used[cid].most_common()]
        for p in top[:max_ports]:
            iface = st.q("SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? AND s.kind IN "
                         f"({','.join('?' * len(TYPE_KINDS))}) AND (s.parent='' OR s.parent NOT LIKE '%::%' "
                         "OR 1) ORDER BY s.start_line LIMIT 1", (p, *TYPE_KINDS))
            pid = f"{i}_p{len(ports)}"
            port_of[(cid, p)] = pid
            ports.append({"id": pid, "name": posixpath.basename(p), "dir": "provided",
                          "iface": iface[0][0] if iface else "", "ref": ctx.loc(p, 1),
                          "consumers": used[cid][p]})
            ctx.deps.add((p, "@file"))
        if len(top) > max_ports:
            pid = f"{i}_pmore"
            for p in top[max_ports:]:
                port_of[(cid, p)] = pid
            ports.append({"id": pid, "name": f"+{len(top) - max_ports} more", "dir": "provided", "iface": "",
                          "files": top[max_ports:max_ports + 30]})
        providers = sorted({b for (a, b) in pairs if a == cid})
        links = st.q("SELECT d.dst, d.kind, d.scope FROM comp_deps d WHERE d.src_id=?", (cid,))
        link_names = {x[0] for x in links}
        for b in providers:
            ports.append({"id": f"{i}_r{b}", "name": f"needs {byid[b][1]}", "dir": "required",
                          "iface": byid[b][1]})
        for dst, lk, scope in links:
            other = next((r for r in byid.values() if r[1] == dst), None)
            if other is not None and other[0] not in providers and other[0] != cid:
                ports.append({"id": f"{i}_r{other[0]}", "name": f"links {dst}", "dir": "required", "iface": dst,
                              "scope": scope})
        info_d = json.loads(info or "{}")
        ref = {"component": name, "dir": d, "abs": ctx.abs(d) if d is not None else None}
        if source:
            ref.update(ctx.loc(source, line or 1))
            ref["component"] = name
            ctx.deps.add((source, "@build"))
        nodes.append({"id": i, "type": "external" if kind == "external" else "component", "label": name,
                      "stereotype": kind if kind != "library" else f"library {info_d.get('type', '')}".strip(),
                      "ref": ref, "ports": ports, "info": {"files": nfiles, **({"links": sorted(link_names)}
                                                                              if link_names else {})}})
    for (a, b), files in pairs.items():
        for p, n in sorted(files.items(), key=lambda x: -x[1]):
            edges.append(_edge(ctx, nid[a], nid[b], "connects", posixpath.basename(p) if port_of.get((b, p), "")
                               .endswith(tuple("0123456789")) else "", from_port=f"{nid[a]}_r{b}",
                               to_port=port_of.get((b, p)), n=n))
        # one connector per provided port: collapse duplicates into the +more port
    seen, ded = set(), []
    for e in edges:
        k = (e["from_port"], e.get("to_port"))
        if k in seen:
            continue
        seen.add(k)
        ded.append(e)
    edges = ded
    for cid in byid:
        for dst, lk, scope in st.q("SELECT dst, kind, scope FROM comp_deps WHERE src_id=?", (cid,)):
            other = next((r for r in byid.values() if r[1] == dst), None)
            if other is not None and other[0] != cid and (cid, other[0]) not in pairs:
                edges.append(_edge(ctx, nid[cid], nid[other[0]], "connects", f"{lk} {scope}".strip(),
                                   from_port=f"{nid[cid]}_r{other[0]}"))
    model = {"version": MODEL_VERSION, "kind": "component",
             "title": f"Components: {subject or path or under or 'workspace'}", "subject": subject or path or under,
             "layout": {"direction": "LR"}, "nodes": nodes, "edges": edges,
             **({"notes": [{"text": f"{hidden} scripts not shown: open a folder (under) or a component to see them"}]}
                if hidden else {}),
             "params": {"under": under, "externals": externals, "max_ports": max_ports, **({"path": path} if path else {})}}
    return model, ctx.deps, ctx.scopes


# ------------------------------------------------------------------ package

def package_view(st: Store, subject: str = "", under: str = "", depth: int = 2) -> tuple[dict, set, set]:
    ctx = Ctx(st)
    pre = (under or subject).replace("\\", "/").strip("/")
    ctx.scopes.add(pre + "/" if pre else "")
    base = pre.count("/") + 1 if pre else 0
    cut = lambda p: "/".join(p.split("/")[:base + depth]) if p.count("/") >= base + depth else posixpath.dirname(p)
    files = [(fid, p) for fid, p, lang in st.q("SELECT f.id, f.path, f.lang FROM files f WHERE f.path LIKE ?",
                                                (pre + "/%" if pre else "%",)) if lang and langs.is_ast_lang(lang)]
    pkg = {fid: cut(p) for fid, p in files if cut(p)}
    count = Counter(pkg.values())
    deps: Counter = Counter()
    kinds: dict[tuple, set] = defaultdict(set)
    if pkg:
        for a, b, kind, n in st.q("SELECT src_id, dst_id, kind, n FROM file_deps"):
            pa, pb = pkg.get(a), pkg.get(b)
            if pa and pb and pa != pb:
                deps[(pa, pb)] += n
                kinds[(pa, pb)].add(kind)
    nodes = []
    ids = {}
    for p, n in sorted(count.items()):
        ids[p] = ctx.nid("p:" + p, p)
        group = "/".join(p.split("/")[:base + 1]) if depth > 1 else ""
        nodes.append({"id": ids[p], "type": "package", "label": (p[len(pre) + 1:] if pre else p) or p or "(root)",
                      "ref": {"path": p, "abs": ctx.abs(p)}, "info": {"files": n}, "group": group})
    edges = [_edge(ctx, ids[a], ids[b], "imports" if kinds[(a, b)] == {"import"} else "depends",
                   f"{'/'.join(sorted(kinds[(a, b)]))} x{n}", n=n) for (a, b), n in deps.most_common(300)]
    groups = sorted({x["group"] for x in nodes if x["group"]})
    model = {"version": MODEL_VERSION, "kind": "package", "title": f"Packages: {pre or 'workspace'}",
             "subject": pre, "layout": {"direction": "LR"}, "nodes": nodes, "edges": edges,
             "groups": [{"id": "g_" + slug(g), "label": g, "nodes": [x["id"] for x in nodes if x["group"] == g]}
                        for g in groups if g],
             "params": {"depth": depth, "under": pre}}
    for x in nodes:
        x.pop("group")
    return model, ctx.deps, ctx.scopes


# ------------------------------------------------------------------ deployment

def deployment_view(st: Store, subject: str = "", under: str = "", externals: bool = True) -> tuple[dict, set, set]:
    ctx = Ctx(st)
    pre = (under or "").replace("\\", "/").strip("/")
    ctx.scopes.add(pre + "/" if pre else "")
    arts = st.q("SELECT id, name, kind, dir, source, line, info FROM components WHERE kind IN "
                "('library','executable','script','package','external') ORDER BY kind, name")
    if pre:
        arts = [a for a in arts if a[2] == "external" or (a[3] or "").startswith(pre)]
    if subject:
        sid = [a for a in arts if a[1].lower() == subject.lower()]
        if not sid:
            raise ValueError(f"no artifact named {subject!r}")
        names = {sid[0][1]} | {d for (d,) in st.q("SELECT dst FROM comp_deps WHERE src_id=?", (sid[0][0],))} | {
            n for (n,) in st.q("SELECT c.name FROM comp_deps d JOIN components c ON c.id=d.src_id WHERE d.dst=?",
                               (sid[0][1],))}
        arts = [a for a in arts if a[1] in names]
    links = st.q("SELECT c.name, d.dst, d.kind, d.scope FROM comp_deps d JOIN components c ON c.id=d.src_id")
    linked = {d for _, d, _, _ in links}
    nodes, edges, ids = [], [], {}
    for cid, name, kind, d, source, line, info in arts:
        if kind == "external" and (not externals or name not in linked):
            continue
        if kind == "package" and not json.loads(info or "{}").get("scripts"):
            continue
        info_d = json.loads(info or "{}")
        ids[name] = ctx.nid("a:" + name, name)
        ref = {"component": name, "dir": d, "abs": ctx.abs(d) if d is not None else None}
        if source:
            ref.update(ctx.loc(source, line or 1))
            ctx.deps.add((source, "@build"))
        typ = info_d.get("type", "")
        art = {"executable": f"{name}{'.exe' if typ == 'EXECUTABLE' else ''}", "script": name}.get(kind, name)
        nodes.append({"id": ids[name], "type": "external" if kind == "external" else "artifact", "label": art,
                      "stereotype": {"library": f"library {typ}".strip(), "executable": "executable",
                                     "script": "script", "package": "python package"}.get(kind, "external"),
                      "ref": ref, "group": "g_" + slug(d.split("/")[0]) if d else "g_external", "info": info_d})
    for src, dst, kind, scope in links:
        if src in ids and dst in ids:
            edges.append(_edge(ctx, ids[src], ids[dst], "depends" if kind != "runs" else "deploys",
                               f"{kind} {scope}".strip()))
    cfg = st.q("SELECT name, kind, source, info FROM build_configs ORDER BY kind, name")
    groups = defaultdict(list)
    for x in nodes:
        groups[x["group"]].append(x["id"])
    for name, kind, source, info in cfg:
        if kind == "option":
            continue
        i = ctx.nid(f"cfg:{kind}:{name}", "cfg_" + name)
        nodes.append({"id": i, "type": "config", "label": name, "stereotype": kind, "note": info or "",
                      **({"ref": ctx.loc(source, 1)} if source else {})})
        groups["g_toolchains" if kind == "toolchain" else "g_configs"].append(i)
    opts = [f"{n}: {i}" for n, k, _, i in cfg if k == "option"]
    labels = {"g_configs": "Build configurations", "g_toolchains": "Toolchains", "g_external": "External libraries"}
    model = {"version": MODEL_VERSION, "kind": "deployment", "title": f"Deployment: {subject or pre or 'workspace'}",
             "subject": subject or pre, "layout": {"direction": "LR"}, "nodes": nodes, "edges": edges,
             "groups": [{"id": g, "label": labels.get(g, g[2:]), "nodes": v} for g, v in groups.items()],
             "notes": [{"text": "Build options: " + "; ".join(opts[:20])}] if opts else [],
             "params": {"under": pre, "externals": externals}}
    for x in nodes:
        x.pop("group", None)
    return model, ctx.deps, ctx.scopes


# ------------------------------------------------------------------ sequence

def sequence_view(st: Store, subject: str, path: str = "", depth: int = 3, max_messages: int = 60,
                  external: bool = False) -> tuple[dict, set, set]:
    ctx = Ctx(st)
    entry = [d for d in ctx.find(subject, path, kinds=FUNCS) if d.kind in FUNCS]
    entry = [d for d in entry if "decl" not in d.modset] or entry
    if not entry:
        raise ValueError(f"no function or method named {subject!r}")
    e0 = entry[0]
    langs = graph.lang_family(st, e0.file_id)
    parts: dict[str, dict] = {}
    msgs: list[dict] = []
    amb = graph._ambiguity(st)
    lw, la = graph._lang_sql(langs)

    def lifeline(d: Sym) -> str:
        t = ctx.type_for(re.split(r"::|\.", d.parent)[-1], d.path) if d.parent else None
        if t is not None and (t.qualname == d.parent or d.parent.endswith(t.qualname) or t.qualname.endswith(d.parent)):
            key, label, ref = "t:" + t.qualname, t.name, ctx.ref(t)
        else:
            mod = d.parent if d.parent else posixpath.basename(d.path)
            key, label, ref = "m:" + mod, mod, ctx.loc(d.path, 1)
        i = ctx.nid(key, label)
        if i not in parts:
            parts[i] = {"id": i, "type": "lifeline", "label": label, "ref": ref}
        return i

    def body(qual: str) -> Sym | None:
        rows = [r for r in ctx.q("WHERE s.qualname=? AND s.kind IN ('function','method')", (qual,))
                if "decl" not in r.modset]
        return rows[0] if rows else None

    def methods_of(tq: str, name: str, near: str, hops: int = 3) -> Sym | None:
        for _ in range(hops):
            r = body(f"{tq}{'::' if '::' in tq or not '.' in tq else '.'}{name}") or next(
                (x for x in ctx.q("WHERE s.name=? AND s.parent=? AND s.kind IN ('function','method')", (name, tq))
                 if "decl" not in x.modset), None)
            if r is None:
                r = next(iter(ctx.q("WHERE s.name=? AND s.parent=? AND s.kind IN ('function','method') LIMIT 1",
                                    (name, tq))), None)
            if r is not None:
                return r
            b = st.q("SELECT dst FROM edges WHERE src=? AND kind='inherits' LIMIT 1", (tq,))
            t = ctx.type_for(b[0][0], near) if b else None
            if t is None:
                return None
            tq = t.qualname
        return None

    def var_type(fn: Sym, var: str) -> str:
        cls = fn.parent
        if cls:
            f = ctx.q("WHERE s.parent=? AND s.name=? AND s.kind='field' LIMIT 1", (cls, var))
            if f and type_names(f[0].rtype or ""):
                return type_names(f[0].rtype)[0]
        for dst, in st.q("SELECT dst FROM edges WHERE src=? AND kind IN ('instantiates','var','depends') "
                         "AND (role=? OR role=?) LIMIT 1", (fn.qualname, var, "static " + var)):
            return dst
        g = ctx.q("WHERE s.name=? AND s.kind='variable' LIMIT 1", (var,))
        if g and type_names(g[0].rtype or ""):
            return type_names(g[0].rtype)[0]
        if ctx.types_named(var):
            return var
        return ""

    def resolve(fn: Sym, dst: str, role: str) -> tuple[Sym | None, Sym | None]:
        """(callee definition, type it is created from: constructor calls)."""
        if not role or role == "this":
            if fn.parent:
                m = methods_of(fn.parent, dst, fn.path)
                if m is not None:
                    return m, None
            t = ctx.type_for(dst, fn.path) if dst[:1].isupper() else None
            if t is not None:
                return None, t
            if role == "this":
                return None, None
            rows = st.q(f"SELECT DISTINCT s.qualname FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? "
                        f"AND s.kind IN ('function','method') {lw} LIMIT 3", (dst, *la))
            if len(rows) == 1:
                return body(rows[0][0]), None
            return None, None
        if role.endswith("::"):
            scope = role[:-2].split("::")[-1]
            t = ctx.type_for(scope, fn.path)
            if t is not None:
                return methods_of(t.qualname, dst, fn.path), None
            rows = st.q("SELECT DISTINCT qualname FROM symbols WHERE name=? AND qualname LIKE ? "
                        "AND kind IN ('function','method') LIMIT 2", (dst, f"%{role}{dst}"))
            return (body(rows[0][0]) if len(rows) == 1 else None), None
        var = re.split(r"\.|->|::", role)[0]
        tn = var_type(fn, var)
        t = ctx.type_for(tn, fn.path) if tn else None
        if t is not None:
            return methods_of(t.qualname, dst, fn.path), None
        return None, None

    caller = {"id": "caller", "type": "actor", "label": "caller"}
    first = lifeline(e0)
    msgs.append({"id": "m0", "from": "caller", "to": first, "label": f"{e0.name}()", "kind": "call",
                 "ref": ctx.ref(e0), "depth": 0, "target": {"q": e0.qualname, "path": e0.path}})
    stack: list[str] = []

    def walk(fn: Sym, d: int) -> None:
        stack.append(fn.qualname)
        here = lifeline(fn)
        for dst, role, line, col in st.q("SELECT dst, role, line, col FROM edges WHERE kind='call' AND src=? "
                                         "AND file_id=? ORDER BY rowid", (fn.qualname, fn.file_id)):
            if len(msgs) >= max_messages:
                break
            if graph.MACRO.match(dst) or len(dst) < 3 or graph._call_count(st, dst) > graph.UTILITY_CALLS:
                continue
            callee, created = resolve(fn, dst, role or "")
            loc = ctx.loc(fn.path, line, col)
            if created is not None:
                to = lifeline(Sym(0, created.name, created.qualname + "::" + created.name, "method",
                                  created.qualname, 0, 0, "", created.path, created.file_id, "", "", "", "", "", "",
                                  None, None))
                msgs.append({"id": f"m{len(msgs)}", "from": here, "to": to, "label": f"«create» {created.name}",
                             "kind": "create", "ref": loc, "depth": d + 1,
                             "target": {"q": created.qualname, "path": created.path}})
                continue
            if callee is None:
                if external and amb(dst) == 0:
                    i = ctx.nid("x:external", "external")
                    parts.setdefault(i, {"id": i, "type": "lifeline", "label": "(not indexed)", "external": True})
                    msgs.append({"id": f"m{len(msgs)}", "from": here, "to": i, "label": f"{dst}()", "kind": "call",
                                 "ref": loc, "depth": d + 1})
                continue
            to = lifeline(callee)
            msgs.append({"id": f"m{len(msgs)}", "from": here, "to": to, "label": f"{dst}()",
                         "kind": "self" if to == here else "call", "ref": loc, "depth": d + 1,
                         "target": {"q": callee.qualname, "path": callee.path, "line": callee.start}})
            ctx.deps.add((callee.path, callee.qualname))
            if callee.id and d + 1 < depth and callee.qualname not in stack:
                before = len(msgs)
                walk(callee, d + 1)
                if before < len(msgs) < max_messages and to != here:
                    msgs.append({"id": f"m{len(msgs)}", "from": to, "to": here, "label": "", "kind": "reply",
                                 "depth": d + 1})
        stack.pop()
    walk(e0, 0)
    ctx.deps.add((e0.path, e0.qualname))
    model = {"version": MODEL_VERSION, "kind": "sequence", "title": f"Sequence: {e0.qualname}",
             "subject": e0.qualname, "layout": {}, "nodes": [caller] + list(parts.values()), "edges": [],
             "messages": msgs, "params": {"depth": depth, "path": path or e0.path, "max_messages": max_messages},
             **({"cut": f"stopped at {max_messages} messages"} if len(msgs) >= max_messages else {})}
    return model, ctx.deps, ctx.scopes


def depends_view(st: Store, subject: str = "", path: str = "", depth: int = 2, max_nodes: int = 15) -> tuple[dict, set, set]:
    """subject = a type (or member: its owner type) -> its type dependencies; path = a file -> its include / import
    dependencies. depth = hops each way, max_nodes = neighbours per node each way."""
    ctx = Ctx(st)
    path = path.replace("\\", "/").strip("/")
    if subject:
        g = graph.type_deps(st, subject, "both", max(1, min(depth, 3)), max_nodes)
    elif path:
        g = graph.file_deps(st, path, "both", max(1, min(depth, 3)), max_nodes)
    else:
        raise ValueError("depends needs subject (a type) or path (a file)")
    if g.get("error") or not g.get("nodes"):
        raise ValueError(g.get("error") or f"no dependencies for {subject or path!r}")
    files = not subject
    ids, nodes, groups = {}, [], defaultdict(list)
    for n in g["nodes"]:
        i = ids[n["id"]] = ctx.nid("d:" + n["id"], n["label"])
        if files:
            ref = ctx.loc(n["path"], 1)
            ctx.deps.add((n["path"], "@file"))
        else:
            s = [x for x in ctx.q("WHERE s.qualname=? AND f.path=? LIMIT 1", (n["id"], n["path"]))]
            ref = ctx.ref(s[0]) if s else ctx.loc(n["path"], n["line"])
        typ = "module" if files else ("interface" if n["kind"] in ("interface", "trait") else n["kind"]
                                       if n["kind"] in ("struct", "enum", "union", "record") else "class")
        nodes.append({"id": i, "type": typ, "label": n["label"], "ref": ref,
                      "stereotype": "" if files else n["kind"], **({"root": True} if n["id"] == g["root"] else {}),
                      **({"note": "cycle"} if n.get("cycle") else {})})
        folder = posixpath.dirname(n["path"] or "")
        groups[folder].append(i)
        ctx.scopes.add(folder + "/" if folder else "")
    edges = []
    for e in g["edges"]:
        if e["from"] in ids and e["to"] in ids:
            lab = e["kind"] + (f" x{e['n']}" if (e.get("n") or 1) > 1 else "") + (" ↺" if e.get("cycle") else "")
            edges.append(_edge(ctx, ids[e["from"]], ids[e["to"]], e["kind"], lab, e.get("path") or "", e.get("line") or 0,
                               **({"cycle": True} if e.get("cycle") else {})))
    root = next(n for n in g["nodes"] if n["id"] == g["root"])
    more = sum((g.get("more_out") or {}).values()) + sum((g.get("more_in") or {}).values())
    notes = ([{"text": f"{g['cycles']} cycle(s): dashed edges"}] if g.get("cycles") else []) + \
        ([{"text": f"+{more} more neighbours not shown (cap {max_nodes} per node)"}] if more else [])
    model = {"version": MODEL_VERSION, "kind": "depends", "title": f"Depends ({'file' if files else 'type'}): {root['label']}",
             "subject": g["root"], "layout": {"direction": "LR"}, "nodes": nodes, "edges": edges, "notes": notes,
             "groups": [{"id": "g_" + slug(k or "root"), "label": k or "(root)", "nodes": v} for k, v in groups.items()],
             "params": {"depth": depth, "mode": "include" if files else "type", "cycles": g.get("cycles", 0),
                        **({"path": path} if files else {})}}
    return model, ctx.deps, ctx.scopes


GENERATORS = {"class": class_view, "object": object_view, "component": component_view, "package": package_view,
              "deployment": deployment_view, "sequence": sequence_view, "depends": depends_view}


def generate(st: Store, kind: str, subject: str = "", **opts) -> tuple[dict, set, set]:
    if kind not in GENERATORS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    fn = GENERATORS[kind]
    import inspect
    names = set(inspect.signature(fn).parameters)
    return fn(st, subject, **{k: v for k, v in opts.items() if k in names and v not in (None, "")})
