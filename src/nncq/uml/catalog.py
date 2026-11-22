"""Stored views and compositions (table `views`; kind 'composition' for compositions), their dependencies and
staleness, the render cache and lookups.

Staleness only where something changed: store.replace_file / delete_file mark the views whose view_refs path
or view_scope folder matches (schema.touch). On next access:
  auto views     regenerated; if the result differs, stored with a report of what was added / removed
  agent views,   every reference is re-resolved and fingerprinted: changed | missing | moved members are listed in
  compositions   `staleness` (members are never dropped; bespoke ones are never touched); views and compositions
                 that reference this one are marked for re-check in turn.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import time
from collections import defaultdict

from nncq import graph, langs
from nncq.store import Store
from nncq.uml import protocol as P
from nncq.uml import render_ascii, render_mermaid
from nncq.uml import views as V

FORMATS = ("ascii", "mermaid", "markdown", "json")
_COLS = "id, key, kind, title, origin, grp, spec, model, hash, stale, report, created, updated, prov"


def _hash(model: dict) -> str:
    return hashlib.sha1(json.dumps(model, sort_keys=True, default=str).encode()).hexdigest()[:16]


def element_of(row: dict) -> str:
    return row["key"] if row["kind"] == "composition" else "view:" + row["key"]


def _row(st: Store, key: str) -> dict | None:
    key = key.strip()
    if key.startswith("view:"):
        key = key[5:]
    r = st.q(f"SELECT {_COLS} FROM views WHERE key=?", (key,))
    if not r and not key.startswith("composition:"):
        r = st.q(f"SELECT {_COLS} FROM views WHERE key=?", ("composition:" + key,))
    if not r:
        return None
    d = dict(zip(_COLS.split(", "), r[0]))
    d["dirty"] = bool(st.q("SELECT 1 FROM view_dirty WHERE view_id=?", (d["id"],)))
    return d


def _save(st: Store, key: str, kind: str, title: str, origin: str, grp: str, spec: dict, model: dict,
          refs: list[tuple[str, str, str]], scopes: set | list, stale: int = 0, report: dict | None = None,
          prov: dict | None = None) -> int:
    now = time.time()
    with st.tx() as db:
        old = db.execute("SELECT id, created FROM views WHERE key=?", (key,)).fetchone()
        args = (kind, title, origin, grp, json.dumps(spec), json.dumps(model), _hash(model), stale,
                json.dumps(report) if report else None, now)
        if old:
            vid = old[0]
            db.execute("UPDATE views SET kind=?, title=?, origin=?, grp=?, spec=?, model=?, hash=?, stale=?, report=?, "
                       "updated=? WHERE id=?", (*args, vid))
            if prov is not None:
                db.execute("UPDATE views SET prov=? WHERE id=?", (json.dumps(prov), vid))
        else:
            vid = db.execute("INSERT INTO views(kind, title, origin, grp, spec, model, hash, stale, report, updated, key,"
                             " created, prov) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (*args, key, now, json.dumps(prov) if prov else None)).lastrowid
        for t in ("view_refs", "view_scope", "view_render", "view_dirty"):
            db.execute(f"DELETE FROM {t} WHERE view_id=?", (vid,))
        db.executemany("INSERT INTO view_refs(view_id, path, ref, fp) VALUES(?,?,?,?)",
                       ((vid, p, r, fp) for p, r, fp in dict.fromkeys(refs)))
        db.executemany("INSERT INTO view_scope(view_id, prefix) VALUES(?,?)", ((vid, s) for s in set(scopes)))
        elem = key if kind == "composition" else "view:" + key
        db.execute("INSERT OR IGNORE INTO view_dirty(view_id) SELECT view_id FROM view_refs WHERE ref=? AND view_id!=?",
                   (elem, vid))
    return vid


# ------------------------------------------------------------------ auto views

def auto_key(kind: str, subject: str = "", **opts) -> str:
    extra = "&".join(f"{k}={v}" for k, v in sorted(opts.items()) if v not in (None, "", False))
    return f"{kind}:{subject or '*'}" + (f"|{extra}" if extra else "")


def _group_of(st: Store, model: dict) -> str:
    if model["kind"] in ("component", "package", "deployment"):
        return model.get("params", {}).get("under") or "workspace"
    for n in model.get("nodes", []):
        if n.get("component"):
            return n["component"]
        path = (n.get("ref") or {}).get("path")
        if path:
            r = st.q("SELECT c.name FROM comp_files cf JOIN files f ON f.id=cf.file_id JOIN components c "
                     "ON c.id=cf.comp_id WHERE f.path=?", (path,))
            if r:
                return r[0][0]
    return ""


def _diff(old: dict, new: dict) -> dict:
    a = {n["id"]: n.get("label") for n in old.get("nodes", [])}
    b = {n["id"]: n.get("label") for n in new.get("nodes", [])}
    out = {"added": [b[k] for k in b.keys() - a.keys()][:30], "removed": [a[k] for k in a.keys() - b.keys()][:30]}
    e_old = {e["id"] for e in old.get("edges", [])} | {m.get("label", "") + m["to"] for m in old.get("messages", [])}
    eb = {e["id"] for e in new.get("edges", [])} | {m.get("label", "") + m["to"] for m in new.get("messages", [])}
    out["relations_added"], out["relations_removed"] = len(eb - e_old), len(e_old - eb)
    return {k: v for k, v in out.items() if v}


GEN_VERSION = 2             # bump when generators change their output: stored auto views of older ones regenerate


def auto(st: Store, kind: str, subject: str = "", **opts) -> dict:
    """Generate an auto view, or return the stored one when nothing it depends on changed."""
    key = auto_key(kind, subject, **opts)
    r = _row(st, key)
    if r and not r["dirty"] and json.loads(r["spec"] or "{}").get("gen") == GEN_VERSION:
        return r
    model, deps, scopes = V.generate(st, kind, subject, **opts)
    refs = [(p, P.norm_ref(q) if not q.startswith("@") else "file:" + p, "") for p, q in deps]
    spec = {"kind": kind, "subject": subject, "opts": opts, "gen": GEN_VERSION}
    if r and r["hash"] == _hash(model):
        st.db.execute("DELETE FROM view_dirty WHERE view_id=?", (r["id"],))
        st.db.execute("UPDATE views SET spec=? WHERE id=?", (json.dumps(spec), r["id"]))
        return _row(st, key)
    report = {"regenerated": time.strftime("%Y-%m-%d %H:%M:%S"), **_diff(json.loads(r["model"]), model)} if r else None
    _save(st, key, kind, model["title"], "auto", _group_of(st, model), spec,
          model, refs, scopes, 0, report)
    return _row(st, key)


# ------------------------------------------------------------------ agent views and compositions

def _cycle(st: Store, start: str, refs: list[str]) -> list[str] | None:
    """Path back to `start` through stored views/compositions, if the new references would close a loop."""
    stack = [(r, [start, r]) for r in refs]
    seen = set()
    while stack:
        ref, path = stack.pop()
        if ref == start:
            return path
        if ref in seen:
            continue
        seen.add(ref)
        row = _row(st, ref if ref.startswith("composition:") else ref[5:])
        if row and row["spec"]:
            stack += [(x, path + [x]) for x in P.composition_refs(json.loads(row["spec"]))]
    return None


def _who(client: str = "", model: str = "") -> dict:
    return {"client": client or "unknown", "model": model or "unknown", "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def provenance(r: dict) -> dict | None:
    """Who made a stored view / composition over MCP (none for generated ones): a short tag client:model of its
    creator and a detail line with versions, times and the last editor."""
    if r.get("origin") == "auto":
        return None
    p = json.loads(r.get("prov") or "null") or {}
    c, u = p.get("created_by") or {}, p.get("updated_by") or {}
    name = lambda w: (w.get("client") or "unknown").split("/")[0]
    tag = f"{name(c)}:{c.get('model') or 'unknown'}" if c else "unknown"
    made = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created") or 0))
    detail = (f"made via MCP by {c.get('client', 'unknown')} · model {c.get('model', 'unknown')} · {c.get('at') or made}"
              if c else f"made via MCP (client and model not recorded) · {made}")
    if u and (u.get("client"), u.get("model")) != (c.get("client"), c.get("model")):
        detail += f"\nlast edited by {u.get('client', 'unknown')} · model {u.get('model', 'unknown')} · {u.get('at', '')}"
    elif u and u.get("at") != c.get("at"):
        detail += f"\nupdated {u.get('at', '')}"
    return {"tag": tag, "detail": detail, **({"created_by": c} if c else {}), **({"updated_by": u} if u else {})}


def upsert(st: Store, obj: dict | str, client: str = "", llm: str = "") -> dict:
    """client / llm: provenance of an MCP call (the MCP client's name/version, the model id the agent passed)."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            return {"error": f"not JSON: {e}"}
    what, errs = P.validate(obj)
    if errs:
        return {"error": "invalid " + (what or "object"), "errors": errs[:30],
                "hint": "call uml_protocol for the schema and examples"}
    if what == "composition":
        key, elem = "composition:" + obj["id"], "composition:" + obj["id"]
    else:
        key = obj.get("key") or V.slug(obj["title"]).lower()
        if _row(st, key) and _row(st, key)["origin"] == "auto":
            return {"error": f"key {key!r} belongs to an auto view; pick another key"}
        elem = "view:" + key
    loop = _cycle(st, elem, P.composition_refs(obj))
    if loop:
        return {"error": "reference cycle: " + " -> ".join(loop)}
    existed = _row(st, key) is not None
    refs, unbound, scopes = P.bind(st, obj, what)
    model = P.materialize_composition(st, obj) if what == "composition" else P.materialize_view(st, obj)
    report = {"unbound": unbound} if unbound else None
    origin = obj.get("author", "agent") if what == "composition" else "agent"
    grp = obj.get("group") or _group_of(st, model)
    title = obj.get("name") if what == "composition" else obj["title"]
    old = _row(st, key)
    who = _who(client, llm)
    created = (json.loads(old["prov"] or "null") or {}).get("created_by") if old else None
    prov = {"created_by": created or who, "updated_by": who}
    _save(st, key, "composition" if what == "composition" else obj["kind"], title, origin, grp, obj, model,
          refs, scopes, 0, report, prov)
    out = {"key": key, "element": elem, "kind": what if what == "composition" else obj["kind"],
           "result": "updated" if existed else "created", "nodes": len(model["nodes"]), "edges": len(model["edges"])}
    if unbound:
        out["unbound"] = unbound
    ver = P.verify_edges(st, obj, what)
    if ver:
        out["unverified_edges"] = ver
    return out


def delete(st: Store, key: str) -> dict:
    r = _row(st, key)
    if r is None:
        return {"error": f"no view or composition {key!r}"}
    users = [k for (k,) in st.q("SELECT v.key FROM view_refs r JOIN views v ON v.id=r.view_id WHERE r.ref=?",
                                (element_of(r),))]
    with st.tx() as db:
        for t in ("view_refs", "view_scope", "view_render", "view_dirty"):
            db.execute(f"DELETE FROM {t} WHERE view_id=?", (r["id"],))
        db.execute("DELETE FROM views WHERE id=?", (r["id"],))
        db.execute("INSERT OR IGNORE INTO view_dirty(view_id) SELECT view_id FROM view_refs WHERE ref=?",
                   (element_of(r),))
    return {"deleted": r["key"], **({"referenced_by_now_flagged": users} if users else {})}


def file_composition(st: Store, path: str) -> dict:
    """The file's composition (filecomp.py), generated on demand: fresh -> reused; dirty (a file it depends on
    changed, or a part was regenerated) -> its parts are refreshed first (auto views regenerate when dirty),
    then it is rebuilt. Its refs point at its parts and the file, so later staleness propagates to it."""
    from nncq.uml import filecomp
    path = path.replace("\\", "/").strip("/")
    k = filecomp.key(path)
    r = _row(st, k)
    if r and not r["dirty"]:
        return r

    def sub(kind: str, subject: str = "", **opts) -> str | None:
        try:
            return auto(st, kind, subject, **opts)["key"]
        except (ValueError, KeyError):
            return None
    spec = filecomp.spec(st, path, sub)
    if not spec["members"]:
        raise ValueError(f"nothing to show for {path!r}")
    refs, unbound, scopes = P.bind(st, spec, "composition")
    refs.append((path, "file:" + path, ""))
    model = P.materialize_composition(st, spec)
    if r and r["hash"] == _hash(model):
        st.db.execute("DELETE FROM view_dirty WHERE view_id=?", (r["id"],))
        return _row(st, k)
    report = {"regenerated": time.strftime("%Y-%m-%d %H:%M:%S"), **_diff(json.loads(r["model"]), model)} if r else None
    _save(st, k, "composition", spec["name"], "auto", "files", spec, model, refs, scopes, 0, report)
    return _row(st, k)


def check(st: Store, r: dict) -> dict:
    """Re-validate a view marked by a file change (see module doc). Returns the fresh row."""
    if not r["dirty"]:
        return r
    spec = json.loads(r["spec"] or "{}")
    if r["origin"] == "auto":
        try:
            if r["kind"] == "composition":
                return file_composition(st, spec["auto_file"])
            return auto(st, spec["kind"], spec.get("subject", ""), **spec.get("opts", {}))
        except ValueError as e:
            report = {"regenerated": time.strftime("%Y-%m-%d %H:%M:%S"), "error": str(e)}
            with st.tx() as db:
                db.execute("UPDATE views SET stale=1, report=? WHERE id=?", (json.dumps(report), r["id"]))
                db.execute("DELETE FROM view_dirty WHERE view_id=?", (r["id"],))
            return _row(st, r["key"])
    ctx = V.Ctx(st)
    owners = defaultdict(list)
    for m in spec.get("members") or spec.get("nodes") or []:
        if isinstance(m, dict) and m.get("ref"):
            owners[m["ref"]].append(m["id"])
    flags = []
    refs = []
    for path, ref, fp in st.q("SELECT path, ref, fp FROM view_refs WHERE view_id=?", (r["id"],)):
        b = P.resolve(ctx, ref)
        if b is None:
            flags.append({"members": owners.get(ref, []), "ref": ref, "status": "missing",
                          "was_at": path or None})
            refs.append((path, ref, fp))
            continue
        refs.append((b.get("path") or path, ref, fp))
        if path and b.get("path") and b["path"] != path and ref.startswith("sym:"):
            flags.append({"members": owners.get(ref, []), "ref": ref, "status": "moved", "now_at":
                          f"{b['path']}:{b['line']}", "was_at": path})
        elif fp and b["fp"] != fp:
            flags.append({"members": owners.get(ref, []), "ref": ref, "status": "changed",
                          "now_at": f"{b['path']}:{b['line']}" if b.get("path") else None})
        elif b.get("stale"):
            flags.append({"members": owners.get(ref, []), "ref": ref, "status": "nested stale"})
    what = "composition" if r["kind"] == "composition" else "view"
    model = P.materialize_composition(st, spec) if what == "composition" else P.materialize_view(st, spec)
    by_member = {mid: f["status"] for f in flags for mid in f["members"]}
    for n in model["nodes"]:
        if n["id"] in by_member:
            n["stale"] = by_member[n["id"]]
    old = json.loads(r["report"] or "{}")
    report = {**({"unbound": old["unbound"]} if old.get("unbound") else {}),
              **({"staleness": flags, "checked": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "repair": "re-upsert with corrected refs (uml_upsert); members are kept until you do"}
                 if flags else {})}
    stale = 1 if flags else 0
    with st.tx() as db:
        db.execute("UPDATE views SET model=?, hash=?, stale=?, report=? WHERE id=?",
                   (json.dumps(model), _hash(model), stale, json.dumps(report) if report else None, r["id"]))
        db.execute("DELETE FROM view_render WHERE view_id=?", (r["id"],))
        db.execute("DELETE FROM view_dirty WHERE view_id=?", (r["id"],))
        if stale != r["stale"] or model != json.loads(r["model"] or "{}"):
            db.execute("INSERT OR IGNORE INTO view_dirty(view_id) SELECT view_id FROM view_refs WHERE ref=?",
                       (element_of(r),))
    return _row(st, r["key"])


# ------------------------------------------------------------------ reading

def node_element(n: dict) -> str:
    """Stable element id of a rendered node (sym:/comp:/pkg:/cfg:/composition:/view:), '' for bespoke ones."""
    if n.get("element"):
        return n["element"]
    ref = n.get("ref") or {}
    if ref.get("q"):
        return f"sym:{ref['q']}"
    if ref.get("component"):
        return f"comp:{ref['component']}"
    if n.get("type") == "package" and ref.get("path"):
        return f"pkg:{ref['path']}"
    if n.get("type") == "config":
        return f"cfg:{n.get('label')}"
    return ""


def markdown(st: Store, r: dict, model: dict) -> str:
    """Header comment + pure Mermaid block + a list of the elements shown, each a link to its dashboard page.
    Links carry the view revision and the element's content hash; the page warns when they no longer match.
    Nothing here contacts the server."""
    from urllib.parse import quote, urlencode
    from nncq import config
    from nncq.uml import FACTS_VERSION
    try:
        repo = st.path.parent.name
        s = config.settings()
        host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
        base = f"http://{host}:{s.port}/views"
    except Exception:
        repo, base = "", "http://127.0.0.1:8765/views"
    vrev = r["hash"] or _hash(model)
    rows, links = [], {}

    def sub_link(kind: str, q: str, label: str, loc: dict | None) -> str:
        url = f"{base}#/uml/{kind}/{quote('sym:' + q, safe='')}?{urlencode({'view': r['key'], 'v': vrev, 'repo': repo})}"
        where = f" `{loc['path']}:{loc.get('line', 1)}`" if loc and loc.get("path") else ""
        return f"[{label.replace('[', '(').replace(']', ')')}]({url}){where}"
    for n in model.get("nodes", []):
        try:
            el = node_element(n)
            label = (n.get("label") or n["id"]).replace("[", "(").replace("]", ")")
            if not el:
                rows.append(f"- {label} (bespoke{': ' + n['note'] if n.get('note') else ''})")
                continue
            q = urlencode({"view": r["key"], "v": vrev, "h": _hash(n)[:10], "repo": repo})
            url = f"{base}#/uml/{quote(n.get('type') or 'element')}/{quote(el, safe='')}?{q}"
            links[n["id"]] = url
            loc = (n.get("ref") or {})
            at = f" `{loc['path']}:{loc.get('line', 1)}`" if loc.get("path") else ""
            rows.append(f"- [{label}]({url}) {n.get('type', '')}{at}" + (" (stale)" if n.get("stale") else ""))
            ms = [m for m in (n.get("members") or []) if (m.get("ref") or {}).get("q")]     # members, under their type
            rows += [f"  - {sub_link('method' if m.get('kind') == 'method' else 'member', m['ref']['q'], m['name'] + ('()' if m.get('kind') == 'method' else ''), m.get('impl') or m['ref'])}"
                     for m in ms[:15]] + ([f"  - … {len(ms) - 15} more"] if len(ms) > 15 else [])
        except Exception:
            rows.append(f"- {n.get('label') or n.get('id')}")
    calls = {}
    for m in model.get("messages", []):                     # sequence: every called method
        t = m.get("target") or {}
        if t.get("q") and t["q"] not in calls:
            calls[t["q"]] = sub_link("method", t["q"], t["q"], t)
    if calls:
        rows += ["", "Calls:", ""] + [f"- {x}" for x in calls.values()]
    try:
        mm = render_mermaid.render(model, links)
    except Exception:
        mm = render_mermaid.render(model)
    p = provenance(r)
    if p:
        mm += f"\n%% made via mcp · {p['tag']}"
    out = [f"<!-- nncq uml export: generator={V.MODEL_VERSION}.{FACTS_VERSION} repo={repo} view={r['key']} "
           f"view_rev={vrev} model_hash={_hash(model)} -->", f"### {model.get('title', r['key'])}", "",
           "```mermaid", mm, "```", ""]
    view_link = f"{base}#/uml/view/{quote(element_of(r), safe='')}?{urlencode({'view': r['key'], 'v': vrev, 'repo': repo})}"
    out += [f"Elements ([open this view]({view_link})):", ""] + rows
    if p:
        out += ["", f"_made via mcp · {p['tag']}_"]
    return "\n".join(out) + "\n"


RENDER_VERSION = 2          # bump when a renderer's output changes: cached renders of older versions are ignored


def render(st: Store, r: dict, fmt: str) -> str:
    key = f"{fmt}@{RENDER_VERSION}"
    hit = st.q("SELECT text FROM view_render WHERE view_id=? AND fmt=?", (r["id"], key))
    if hit:
        return hit[0][0]
    model = json.loads(r["model"])
    p = provenance(r)
    if fmt == "ascii":
        text = render_ascii.render(model) + (f"\n-- via mcp · {p['tag']}" if p else "")
    elif fmt == "mermaid":
        text = render_mermaid.render(model)
    elif fmt == "markdown":
        text = markdown(st, r, model)
    else:
        return json.dumps(model)
    st.db.execute("DELETE FROM view_render WHERE view_id=? AND fmt LIKE ? AND fmt!=?", (r["id"], fmt + "%", key))
    st.db.execute("INSERT OR REPLACE INTO view_render(view_id, fmt, text) VALUES(?,?,?)", (r["id"], key, text))
    return text


def summary(r: dict) -> dict:
    rep = json.loads(r["report"] or "null")
    out = {"key": r["key"], "element": element_of(r), "kind": r["kind"], "title": r["title"], "origin": r["origin"],
           "group": r["grp"] or "", "rev": r["hash"], "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"] or 0))}
    if r["dirty"]:
        out["stale"] = "pending re-check (files it depends on changed)"
    elif r["stale"]:
        out["stale"] = True
    if rep:
        out["report"] = rep
    if (p := provenance(r)) is not None:
        out["provenance"] = {"tag": p["tag"], "detail": p["detail"]}
    return out


def ascii_ok(r: dict, model: dict) -> bool:
    """ASCII covers plain views of ordinary elements: not compositions, nor views showing compositions/diagrams."""
    return r["kind"] != "composition" and not any(n.get("type") in ("composition", "view") or
                                                  str(n.get("element", "")).startswith(("composition:", "view:"))
                                                  for n in model.get("nodes", [])) \
        and not any(str(g.get("element", "")).startswith(("composition:", "view:")) for g in model.get("groups", []))


def get(st: Store, key: str, fmt: str = "ascii") -> dict:
    r = _row(st, key)
    if r is None:
        return {"error": f"no view or composition {key!r}", "hint": "uml_views lists them"}
    r = check(st, r)
    if fmt == "ascii" and not ascii_ok(r, json.loads(r["model"])):
        return {"error": "ASCII is for plain views; this one is or contains a composition / diagram",
                "hint": "use format=markdown (or mermaid)", "key": r["key"], "element": element_of(r)}
    out = summary(r)
    out.pop("stale", None) if not r["stale"] else None
    if fmt == "json":
        out["model"] = json.loads(r["model"])
        out["node_revs"] = {n["id"]: _hash(n)[:10] for n in out["model"].get("nodes", [])}
        if r["origin"] != "auto":
            out["spec"] = json.loads(r["spec"])
    else:
        out[{"ascii": "diagram", "mermaid": "mermaid", "markdown": "markdown"}.get(fmt, "diagram")] = render(st, r, fmt)
    return out


def _code_under(st: Store, folder: str) -> bool:
    return any(lang and langs.is_ast_lang(lang) for (lang,) in st.q(
        "SELECT DISTINCT lang FROM files WHERE path LIKE ?", (folder + "/%" if folder else "%",)))


def default_targets(st: Store, subject: str = "", path: str = "") -> list[tuple[str, str, dict]]:
    """Best-guess diagrams for what a user is looking at, most specific first: a type -> its class view, a function
    -> a sequence from it, a component -> its component view, any file -> its file composition (filecomp.py; else
    its component), a folder -> its packages, nothing -> the workspace's components (else deployment, packages)."""
    p = (path or "").replace("\\", "/").strip("/")
    out: list[tuple[str, str, dict]] = []
    if subject:
        d = next(iter(graph.find_defs(st, subject, p)), None)
        if d is not None:
            if d.kind in V.TYPE_KINDS:
                out.append(("class", d.qualname, {}))
            elif d.kind in V.FUNCS:
                out.append(("sequence", d.qualname, {}))
                if d.parent and any(x.kind in V.TYPE_KINDS for x in graph.find_defs(st, d.parent)):
                    out.append(("class", d.parent, {}))       # a method that calls nothing: its class
            elif d.parent and any(x.kind in V.TYPE_KINDS for x in graph.find_defs(st, d.parent)):
                out.append(("class", d.parent, {}))
            p = d.path
        elif st.q("SELECT 1 FROM components WHERE name=? COLLATE NOCASE", (subject,)):
            out.append(("component", subject, {}))
    folder = p
    if p and st.q("SELECT 1 FROM files WHERE path=?", (p,)):
        out += [("file", "", {"path": p}), ("component", "", {"path": p})]
        folder = posixpath.dirname(p)
    if p:
        while folder and not _code_under(st, folder):
            folder = posixpath.dirname(folder)
        if folder:
            out.append(("package", "", {"under": folder}))
    return out + [("component", "", {}), ("deployment", "", {}), ("package", "", {})]


def default_view(st: Store, subject: str = "", path: str = "", fmt: str = "json") -> dict:
    """The first default target that yields a non-empty diagram (generated on demand, stored as an auto view)."""
    tried = []
    for kind, subj, opts in default_targets(st, subject, path):
        try:
            r = file_composition(st, opts["path"]) if kind == "file" else auto(st, kind, subj, **opts)
        except (ValueError, KeyError) as e:
            tried.append(f"{kind} {subj or opts or 'workspace'}: {e}")
            continue
        m = json.loads(r["model"])
        if not m.get("nodes") or (kind == "sequence" and len(m.get("messages") or []) <= 1):
            tried.append(f"{kind} {subj or opts or 'workspace'}: empty")
            continue
        return {**get(st, r["key"], fmt), "default_for": {"subject": subject, "path": path, "kind": kind}}
    return {"error": "no diagram for this target", "tried": tried}


def view(st: Store, kind: str = "", subject: str = "", key: str = "", fmt: str = "ascii", **opts) -> dict:
    if fmt not in FORMATS:
        return {"error": f"format: one of {', '.join(FORMATS)}"}
    if key:
        return get(st, key, fmt)
    if kind == "default" or not kind:
        return default_view(st, subject, opts.get("path", "") or opts.get("under", ""), fmt)
    if kind not in V.KINDS:
        return {"error": f"kind: one of {', '.join(V.KINDS)}, default (or pass key= for a stored view / composition)"}
    r = auto(st, kind, subject, **opts)
    return get(st, r["key"], fmt)


def list_views(st: Store, group_by: str = "kind", symbol: str = "", kind: str = "", origin: str = "") -> dict:
    rows = [dict(zip(_COLS.split(", "), x)) for x in st.q(f"SELECT {_COLS} FROM views ORDER BY kind, title")]
    dirty = {v for (v,) in st.q("SELECT view_id FROM view_dirty")}
    for x in rows:
        x["dirty"] = x["id"] in dirty
    if symbol:
        s = graph.short(symbol)
        ids = {v for (v,) in st.q("SELECT DISTINCT view_id FROM view_refs WHERE ref=? OR ref LIKE ? OR ref LIKE ? "
                                  "OR ref LIKE ? OR ref=? OR ref=? OR ref LIKE ?",
                                  ("sym:" + symbol, f"sym:%::{s}", f"sym:%.{s}", f"sym:{symbol}@%", "comp:" + symbol,
                                   "composition:" + symbol, f"port:{symbol}/%"))}
        rows = [x for x in rows if x["id"] in ids]
    if kind:
        rows = [x for x in rows if x["kind"] == kind]
    if origin:
        rows = [x for x in rows if x["origin"] == origin]
    key = {"kind": "kind", "group": "grp", "component": "grp", "origin": "origin"}.get(group_by, "kind")
    groups: dict[str, list] = defaultdict(list)
    for x in rows:
        s = summary(x)
        s.pop("report", None)
        groups[x[key] or "(none)"].append(s)
    return {"total": len(rows), "grouped_by": group_by, "groups": dict(groups),
            **({"symbol": symbol} if symbol else {})}


def find(st: Store, name: str, limit: int = 20) -> dict:
    """Element ids for a name: symbols, components, pins, folders, configurations, compositions, views."""
    out = []
    like = f"%{name}%"
    for d in graph.find_defs(st, name)[:limit]:
        if not any(x["id"] == f"sym:{d.qualname}" for x in out):
            out.append({"id": f"sym:{d.qualname}", "kind": d.kind, "at": f"{d.path}:{d.start}"})
    for n, k, d in st.q("SELECT name, kind, dir FROM components WHERE name LIKE ? LIMIT ?", (like, limit)):
        out.append({"id": f"comp:{n}", "kind": k, "at": d or ""})
    for n, k in st.q("SELECT DISTINCT name, kind FROM build_configs WHERE name LIKE ? LIMIT 10", (like,)):
        out.append({"id": f"cfg:{n}", "kind": k})
    for k, kind, t in st.q("SELECT key, kind, title FROM views WHERE title LIKE ? OR key LIKE ? LIMIT ?",
                           (like, like, limit)):
        out.append({"id": k if kind == "composition" else "view:" + k, "kind": kind, "title": t})
    dirs = sorted({p.rsplit("/", 1)[0] for (p,) in st.q("SELECT path FROM files WHERE path LIKE ? LIMIT 400",
                                                        (f"%{name}%/%",)) if "/" in p})
    out += [{"id": f"pkg:{d}", "kind": "folder"} for d in dirs if name.lower() in d.rsplit("/", 1)[-1].lower()][:10]
    return {"name": name, "elements": out,
            "note": "ports: port:<component>/<file> (provided) | port:<component>/needs <component> (required)"}
