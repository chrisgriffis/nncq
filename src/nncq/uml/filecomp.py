"""The default diagram for a file: a generated composition (key composition:file:<path>, origin auto) whose frame
is the file and whose groups are what it contains or defines. Built on demand only (a file picked in the dashboard,
uml_view(path=...)); catalog.file_composition stores it and regenerates it lazily when it went stale.

  «namespace» frames   nesting for the types / functions / globals below, by namespace (or outer type)
  Types                classes declared here or whose members this file defines (Foo::bar in a .cpp), with members
  Functions            free functions, with their key calls (in-file targets as connectors, others under Calls)
  Globals              top-level variables / constants
  Related types        types outside the file its types inherit / compose / use (from the file's class view)
  Includes / imports   files it depends on        Included by   files depending on it (capped)
  Component            the build target owning it, or those this build file defines
  Diagrams             sub-views: the file's class view, sequences from its busiest functions
Empty groups are omitted; big ones end with a '+N more' note.
"""
from __future__ import annotations

import posixpath
import re
from typing import Callable

from nncq import graph
from nncq.store import Store
from nncq.uml import views as V

CAP = 12
_SEP = re.compile(r"::|\.")


def key(path: str) -> str:
    return "composition:file:" + path


def spec(st: Store, path: str, sub: Callable[..., str | None]) -> dict:
    """The composition spec for a file. sub(kind, subject, **opts) -> stored view key (generated / refreshed on
    demand) or None; used for the file's sub-diagrams, which become members."""
    row = st.q("SELECT id, lang FROM files WHERE path=?", (path,))
    if not row:
        raise ValueError(f"file not indexed: {path!r}")
    fid = row[0][0]
    ctx = V.Ctx(st)
    members: list[dict] = []
    groups: list[dict] = []
    conns: list[dict] = []
    ids: dict[str, str] = {}

    def mid(ref: str, hint: str) -> str:
        if ref not in ids:
            base = ("m_" + V.slug(hint))[:50]
            i, n = base, 1
            while i in ids.values():
                n += 1
                i = f"{base}_{n}"
            ids[ref] = i
        return ids[ref]

    def add(group: str, label: str, refs: list[tuple[str, str]], parent: str = "", auto_members=False,
            outside=False) -> list[str]:
        """refs: [(element id, label hint)]; returns the member ids (capped, '+N more' note)."""
        if not refs:
            return []
        out = []
        for ref, hint in refs[:CAP]:
            if ref in ids:
                continue
            m = {"id": mid(ref, hint), "ref": ref}
            if auto_members:
                m["members"] = "auto"
            if outside:
                m["outside"] = True
            members.append(m)
            out.append(m["id"])
        if len(refs) > CAP:
            more = f"{group}_more"
            members.append({"id": more, "bespoke": {"type": "note", "label": f"+{len(refs) - CAP} more"}})
            out.append(more)
        if out:
            groups.append({"id": group, "label": label, "members": out, **({"parent": parent} if parent else {})})
        return out

    # what the file defines, by namespace
    types = V._file_types(ctx, path, 60)
    tq = {t.qualname for t in types}
    syms = ctx.q("WHERE f.path=? ORDER BY s.start_line", (path,))
    funcs = [s for s in syms if s.kind == "function" and s.parent not in tq and "decl" not in s.modset
             and not any(s.parent == t or s.parent.endswith(("::" + t, "." + t)) for t in tq)]
    funcs = [s for s in funcs if not st.q("SELECT 1 FROM symbols x WHERE x.qualname=? AND x.kind IN (%s) LIMIT 1"
                                          % ",".join("?" * len(V.TYPE_KINDS)), (s.parent, *V.TYPE_KINDS))]
    owners = (*V.TYPE_KINDS, *V.FUNCS)                     # members and function-local statics are not globals
    glob = [s for s in syms if s.kind in ("variable", "constant") and not st.q(
        "SELECT 1 FROM symbols x WHERE x.qualname=? AND x.kind IN (%s) LIMIT 1" % ",".join("?" * len(owners)),
        (s.parent, *owners))]

    def ns(q: str) -> str:
        parts = _SEP.split(q)
        return "::".join(parts[:-1]) if len(parts) > 1 else ""
    spaces = sorted({ns(t.qualname) for t in types} | {ns(f.qualname) for f in funcs} | {ns(g.qualname) for g in glob})
    for space in spaces:
        parent = ""
        if space:
            parent = "ns_" + V.slug(space)
            groups.append({"id": parent, "label": ("«type» " if space in tq else "«namespace» ") + space,
                           "members": []})
        sfx = ("_" + V.slug(space)) if space else ""
        add("types" + sfx, "Types", [("sym:" + t.qualname, t.name) for t in types if ns(t.qualname) == space],
            parent, auto_members=True)
        add("functions" + sfx, "Functions", [(f"sym:{f.qualname}@{path}", f.name) for f in funcs
                                            if ns(f.qualname) == space], parent)
        add("globals" + sfx, "Globals", [(f"sym:{g.qualname}@{path}", g.name) for g in glob
                                        if ns(g.qualname) == space], parent)
    groups[:] = [g for g in groups if g["members"] or any(x.get("parent") == g["id"] for x in groups)]

    # key calls of the free functions
    callees: list[tuple[str, str]] = []
    for f in funcs[:CAP]:
        for dst, n in st.q("SELECT dst, COUNT(*) c FROM edges WHERE src=? AND file_id=? AND kind='call' GROUP BY dst "
                           "ORDER BY c DESC, MIN(line) LIMIT 4", (f.qualname, fid)):
            d = next((x for x in graph.find_defs(st, dst) if x.kind in V.FUNCS), None)
            if d is None:
                continue
            ref = f"sym:{d.qualname}@{d.path}" if d.path == path else "sym:" + d.qualname
            if ref not in ids and d.path != path and all(r != ref for r, _ in callees):
                callees.append((ref, d.name))
            conns.append((f"sym:{f.qualname}@{path}", ref, "calls" + (f" x{n}" if n > 1 else "")))
    add("calls", "Calls (other files)", callees, outside=True)

    # the file's class view: its types' relations to types outside the file
    class_key = sub("class", "", path=path) if types else None
    if class_key:
        r = st.q("SELECT model FROM views WHERE key=?", (class_key,))
        import json
        model = json.loads(r[0][0]) if r else {"nodes": [], "edges": []}
        q_of = {n["id"]: n.get("qualname") for n in model.get("nodes", []) if n.get("qualname")}
        rel = [(q_of[e["from"]], q_of[e["to"]], e["kind"]) for e in model.get("edges", [])
               if e["from"] in q_of and e["to"] in q_of]
        outside = []
        for a, b, _ in rel:
            for q in (a, b):
                if q not in tq and ("sym:" + q) not in ids and all(x != "sym:" + q for x, _ in outside):
                    outside.append(("sym:" + q, _SEP.split(q)[-1]))
        add("related", "Related types (other files)", outside, outside=True)
        conns += [("sym:" + a, "sym:" + b, k) for a, b, k in rel]

    # dependencies, dependents, component
    deps = [p for (p,) in st.q("SELECT f.path FROM file_deps d JOIN files f ON f.id=d.dst_id WHERE d.src_id=? "
                               "ORDER BY d.n DESC, f.path", (fid,)) if p != path]
    users = [p for (p,) in st.q("SELECT f.path FROM file_deps d JOIN files f ON f.id=d.src_id WHERE d.dst_id=? "
                                "ORDER BY d.n DESC, f.path", (fid,)) if p != path]
    add("includes", "Includes / imports", [("file:" + p, posixpath.basename(p)) for p in deps], outside=True)
    add("users", "Included / imported by", [("file:" + p, posixpath.basename(p)) for p in users], outside=True)
    comps = [n for (n,) in st.q("SELECT name FROM components WHERE source=? AND kind!='external' ORDER BY name",
                                (path,))]
    comps += [n for (n,) in st.q("SELECT c.name FROM comp_files cf JOIN components c ON c.id=cf.comp_id "
                                 "WHERE cf.file_id=?", (fid,)) if n not in comps]
    add("component", "Defines (build targets)" if st.q("SELECT 1 FROM components WHERE source=? LIMIT 1", (path,))
        else "Component", [("comp:" + n, n) for n in comps])

    # sub-diagrams: the class view, sequences from the busiest free functions
    views = [("view:" + class_key, "class view")] if class_key else []
    busy = sorted(funcs, key=lambda f: -len([c for c in conns if c[0] == f"sym:{f.qualname}@{path}"]))
    for f in busy[:2]:
        if any(c[0] == f"sym:{f.qualname}@{path}" for c in conns):
            k = sub("sequence", f.qualname)
            if k:
                views.append(("view:" + k, "sequence " + f.name))
    add("diagrams", "Diagrams", views)

    connectors = []
    for a, b, label in conns:
        if a in ids and b in ids and ids[a] != ids[b]:
            kind = label.split(" ")[0]
            c = {"from": ids[a], "to": ids[b], "kind": kind, "label": label[len(kind):].strip()}
            if c not in connectors:
                connectors.append(c)
    return {"id": "file:" + path, "name": posixpath.basename(path) or path, "group": "files", "auto_file": path, "members": members, "groups": groups, "connectors": connectors}
