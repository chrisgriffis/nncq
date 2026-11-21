"""Symbol-graph views over the index (no GPU, pure SQLite). All matching is by name: a call to `update`
links to every definition named `update`. Where that is ambiguous, results say so and rank the likely
targets first (same class, or the caller's code mentions the class).

  symbol_context   one-call "neighbourhood" card: definition(s), bases/derived/members or
                   callers/callees/overrides, engine data types, how widely it is mentioned
  calls            flat 'Calls From' table: what this function calls, in source order
  called_by        flat 'Calls To' table: every caller with file:line, ranked when the name is ambiguous
  call_tree        callers (in) or callees (out), N levels, as an indented tree
  call_trace       static 'how does execution get here': pseudo call stacks back to entry points;
                   with frm: how two functions relate (A->B, B->A, shared callers)
  impact           transitive callers of X, summarised by folder and file (blast radius of a change)
  type_hierarchy   base chain(s) up, derived tree down
  file_context     outline + includes / included-by + which files it calls into / is called from
  hotspots         static top-N tables: most called, most derived, biggest fan-out, most included,
                   largest functions, uncalled functions (dead-code candidates)
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

from nncq.store import Store

_SEP = re.compile(r"::|\.|\\")
FUNCS = {"function", "method"}
NON_TYPES = FUNCS | {"namespace", "module", "field", "variable", "constant", "macro", "property", "module_doc"}


def short(name: str) -> str:
    return _SEP.split(name.strip())[-1]


def norm(q: str) -> str:
    return _SEP.sub(".", q or "").lower()


def is_type(kind: str) -> bool:
    return kind not in NON_TYPES


@dataclass
class Def:
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

    def loc(self) -> str:
        return f"{self.path}:{self.start}"

    def line(self) -> str:
        return f"{self.qualname} [{self.kind}] {self.path}:{self.start}-{self.end}"


def _defs_named(store: Store, name: str, limit: int = 2000) -> list[Def]:
    return [Def(*r) for r in store.q(
        "SELECT s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature, f.path, s.file_id "
        "FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? COLLATE NOCASE LIMIT ?", (name, limit))]


def find_defs(store: Store, name: str, path: str = "", kinds: set[str] | None = None) -> list[Def]:
    """Definitions matching a short or qualified name (Foo, app::Foo::bar, Entity.LayerData), best first."""
    s = short(name)
    rows = _defs_named(store, s)
    if _SEP.search(name.strip()):
        want = norm(name)
        rows = [r for r in rows if norm(r.qualname) == want or norm(r.qualname).endswith("." + want)] or rows
    if path:
        p = path.replace("\\", "/").lower()
        rows = [r for r in rows if p in r.path.lower()] or rows
    if kinds:
        rows = [r for r in rows if (r.kind in kinds) or ("type" in kinds and is_type(r.kind))] or rows
    rows.sort(key=lambda r: (r.name != s, 0 if is_type(r.kind) else 1 if r.kind in FUNCS else 2,
                             r.path.count("/"), r.path))
    return rows


def _group(rows: list[Def]) -> "OrderedDict[str, list[Def]]":
    g: OrderedDict[str, list[Def]] = OrderedDict()
    for r in rows:
        g.setdefault(norm(r.qualname), []).append(r)
    return g


def _not_found(store: Store, name: str) -> dict:
    s = short(name)
    sug = [q for (q,) in store.q("SELECT DISTINCT qualname FROM symbols WHERE name LIKE ? LIMIT 12", (s + "%",))]
    return {"error": f"no definition named {name!r}", "did_you_mean": sug}


def _chunk_text(store: Store, file_id: int, line: int) -> str:
    r = store.q("SELECT text FROM chunks WHERE file_id=? AND start_line<=? AND end_line>=? LIMIT 1",
                (file_id, line, line))
    return r[0][0] if r else ""


# ------------------------------------------------------------------ edges by name

def callers(store: Store, target: Def | None, name: str, limit: int, ambiguous: bool) -> tuple[list[dict], int, int]:
    """Call sites of `name`, grouped by calling symbol. Ranked: same class > mentions class > by name."""
    s = short(name)
    total, files = store.q("SELECT COUNT(*), COUNT(DISTINCT file_id) FROM edges WHERE dst=? AND kind='call'", (s,))[0]
    rows = store.q("SELECT e.src, e.file_id, f.path, MIN(e.line), COUNT(*) FROM edges e JOIN files f ON f.id=e.file_id "
                   "WHERE e.dst=? AND e.kind='call' GROUP BY e.src, e.file_id ORDER BY COUNT(*) DESC LIMIT 400", (s,))
    out = []
    cls = short(target.parent) if target and target.parent else ""
    for i, (src, fid, path, line, n) in enumerate(rows):
        conf = "by name"
        if target and cls and ambiguous and i < 200:
            src_parent = norm(src).rsplit(".", 1)[0] if "." in norm(src) else ""
            if src_parent and src_parent == norm(target.parent):
                conf = "same class"
            elif re.search(rf"\b{re.escape(cls)}\b", _chunk_text(store, fid, line)):
                conf = "mentions " + cls
        elif not ambiguous:
            conf = "unique name"
        out.append({"caller": src or "(file level)", "at": f"{path}:{line}", "calls": n, "match": conf})
    rank = lambda c: 0 if c["match"] in ("same class", "unique name") else 1 if c["match"].startswith("mentions") else 2
    out.sort(key=lambda c: (rank(c), -c["calls"]))
    return out[:limit], total, files


def callees(store: Store, quals: list[str], limit: int) -> list[str]:
    if not quals:
        return []
    ph = ",".join("?" * len(quals))
    rows = store.q(f"SELECT dst, COUNT(*), MIN(line) FROM edges WHERE kind='call' AND src IN ({ph}) "
                   f"GROUP BY dst ORDER BY MIN(line) LIMIT ?", (*quals, limit))
    out = []
    for dst, n, line in rows:
        ds = [d for d in _defs_named(store, dst, 50) if d.name == dst]
        kinds = {norm(d.qualname) for d in ds}
        tag = f" x{n}" if n > 1 else ""
        if len(kinds) == 1:
            out.append(f"L{line} {dst}{tag} -> {ds[0].qualname} {ds[0].loc()}")
        elif kinds:
            out.append(f"L{line} {dst}{tag} -> {len(kinds)} definitions (ambiguous)")
        else:
            out.append(f"L{line} {dst}{tag} (not in index: library / macro / generated)")
    return out


def bases_of(store: Store, quals: list[str]) -> list[tuple[str, str, int]]:
    """(short, as written, line) of direct bases."""
    if not quals:
        return []
    ph = ",".join("?" * len(quals))
    seen, out = set(), []
    for dst, label, line in store.q(f"SELECT dst, label, line FROM edges WHERE kind='inherits' AND src IN ({ph})", quals):
        if dst not in seen:
            seen.add(dst)
            out.append((dst, label or dst, line))
    return out


def derived_of(store: Store, name: str, limit: int = 500) -> list[tuple[str, str, int]]:
    """(qualname, path, line) of types that list `name` as a base."""
    return store.q("SELECT e.src, f.path, e.line FROM edges e JOIN files f ON f.id=e.file_id "
                   "WHERE e.dst=? AND e.kind='inherits' LIMIT ?", (short(name), limit))


def _type_quals(store: Store, name: str) -> list[str]:
    return [d.qualname for d in find_defs(store, name, kinds={"type"}) if is_type(d.kind)][:20]


def base_chains(store: Store, quals: list[str], depth: int = 10) -> list[list[str]]:
    chains, stack = [], [([], quals)]
    while stack and len(chains) < 20:
        path, qs = stack.pop()
        bs = bases_of(store, qs)
        if not bs or len(path) >= depth:
            if path:
                chains.append(path)
            continue
        for b, label, _ in bs:
            if label in path:
                continue
            stack.append((path + [label], _type_quals(store, b)))
    return chains


def derived_closure(store: Store, name: str, depth: int = 8, cap: int = 5000) -> set[str]:
    seen, frontier = set(), {short(name)}
    for _ in range(depth):
        nxt = set()
        for n in frontier:
            for src, _, _ in derived_of(store, n):
                s = short(src)
                if src not in seen:
                    seen.add(src)
                    nxt.add(s)
        frontier = nxt
        if not frontier or len(seen) > cap:
            break
    return seen


# ------------------------------------------------------------------ views

def symbol_context(store: Store, name: str, path: str = "", limit: int = 12) -> dict:
    rows = find_defs(store, name, path)
    if not rows:
        # Lua `local M = {}` (one-line table) or similar: no definition, but members point at it as parent
        mem = store.q("SELECT s.name, s.kind, s.qualname, f.path, s.start_line FROM symbols s JOIN files f "
                      "ON f.id=s.file_id WHERE s.parent=? ORDER BY f.path, s.start_line LIMIT 200", (name,))
        if mem:
            by: dict[str, list[str]] = {}
            for n, k, _, _, _ in mem:
                by.setdefault(k, []).append(n)
            return {"symbol": name, "kind": "container (no definition indexed, e.g. a Lua table)",
                    "defined_at": sorted({f"{m[3]}" for m in mem})[:6], "members": by}
        return _not_found(store, name)
    groups = _group(rows)
    prim = next(iter(groups.values()))
    p = prim[0]
    quals = sorted({r.qualname for r in prim})
    ambiguous = len(groups) > 1
    out: dict = {"symbol": p.qualname, "kind": p.kind, "parent": p.parent or None,
                 "defined_at": [f"{r.path}:{r.start}-{r.end}" for r in prim[:6]], "signature": p.signature}
    if ambiguous:
        out["same_name_elsewhere"] = [g[0].line() for k, g in list(groups.items())[1:limit + 1]]
        out["note"] = (f"{len(groups)} different definitions are named '{p.name}'. Links are by name; "
                       "pass a qualified name or path to pick one.")
    if is_type(p.kind):
        bs = bases_of(store, quals)
        if bs:
            out["bases"] = [label for _, label, _ in bs]
            chains = base_chains(store, quals)
            if any(len(c) > 1 for c in chains):
                out["base_chains"] = [" -> ".join(c) for c in chains[:6]]
        d = derived_of(store, p.name)
        if d:
            out["derived"] = [f"{q} {pth}:{ln}" for q, pth, ln in d[:limit]]
            out["derived_total"] = len(derived_closure(store, p.name))
        members = store.q("SELECT name, kind FROM symbols WHERE parent IN (" + ",".join("?" * len(quals)) +
                          ") ORDER BY start_line", quals)
        if members:
            by: dict[str, list[str]] = {}
            for n, k in members:
                if n not in by.setdefault(k, []):
                    by[k].append(n)
            out["members"] = {k: v[:60] for k, v in by.items()}
        if store.q("SELECT 1 FROM data_types LIMIT 1"):
            from nncq import datarefs
            dt = datarefs.data_for_code(store, p.qualname if "." in p.qualname else p.name, k=1)["data_types"]
            if dt:
                out["data_types"] = [f"{t['type']} ({t['via']}, {t['files']} .xdata files)" for t in dt[:6]]
    if p.kind in FUNCS or not is_type(p.kind):
        cs, total, files = callers(store, p, p.name, limit, ambiguous)
        if total:
            out["callers"] = [f"{c['caller']}  {c['at']}" + (f" x{c['calls']}" if c["calls"] > 1 else "")
                              + ("" if c["match"] in ("unique name", "by name") and not ambiguous
                                 else f"  [{c['match']}]") for c in cs]
            out["call_sites_total"] = f"{total} calls in {files} files"
        ce = callees(store, quals, limit * 3)
        if ce:
            out["calls"] = ce
        if p.parent:
            fam = {short(x) for x in derived_closure(store, p.parent, 6)}
            for chain in base_chains(store, [p.parent], 8):
                fam.update(short(x) for x in chain)
            fam.discard(short(p.parent))
            if fam:
                ov = [d for d in _defs_named(store, p.name, 500)
                      if d.name == p.name and d.parent and short(d.parent) in fam]
                if ov:
                    out["overrides_and_overridden"] = [f"{d.qualname} {d.loc()}" for d in ov[:limit]]
    n = store.q("SELECT COUNT(*) FROM (SELECT DISTINCT c.file_id FROM fts JOIN chunks c ON c.id=fts.rowid "
                "WHERE fts MATCH ? LIMIT 5000)", (f'body:"{p.name}"',))[0][0]
    out["mentioned_in_files"] = n if n < 5000 else "5000+"
    return out


def _pick_func(store: Store, name: str, path: str) -> tuple[list[Def], list[str]]:
    """Definitions of the best-matching function, plus other same-named qualnames (for disambiguation)."""
    rows = find_defs(store, name, path, FUNCS)
    g = _group([r for r in rows if r.kind in FUNCS] or rows)
    keys = list(g)
    return (g[keys[0]] if keys else []), [g[k][0].line() for k in keys[1:15]]


def calls(store: Store, name: str, path: str = "", limit: int = 200) -> dict:
    """Flat 'Calls From' table: every call `name` makes, in source order, resolved to a definition when unique."""
    defs, others = _pick_func(store, name, path)
    if not defs:
        return _not_found(store, name)
    rows = callees(store, sorted({d.qualname for d in defs}), limit)
    out = {"function": defs[0].qualname, "defined_at": [d.loc() for d in defs[:4]], "calls": rows,
           "count": len(rows), "note": "static, name-based; calls through pointers/delegates/virtual "
                                       "dispatch show the name called, not the runtime target"}
    if others:
        out["other_definitions_with_this_name"] = others + ["(pass path= or a qualified name to pick one)"]
    return out


def called_by(store: Store, name: str, path: str = "", limit: int = 200) -> dict:
    """Flat 'Calls To' table: every function that calls `name`, one row per caller and file, best match first."""
    defs, others = _pick_func(store, name, path)
    target = defs[0] if defs else None
    rows, total, files = callers(store, target, name, limit, bool(others))
    out = {"function": target.qualname if target else name,
           "defined_at": [d.loc() for d in defs[:4]] or "(no definition indexed: library, macro or generated)",
           "callers": [f"{c['caller']}  {c['at']}" + (f"  x{c['calls']}" if c["calls"] > 1 else "")
                       + ("" if c["match"] in ("unique name", "by name") and not others else f"  [{c['match']}]")
                       for c in rows],
           "shown": len(rows), "call_sites_total": total, "files_total": files}
    if others:
        out["note"] = (f"{len(others) + 1} functions share the name {short(name)!r}; rows are ranked "
                       "[same class] > [mentions <class>] > [by name]. Lower ranks may call a different one.")
        out["other_definitions_with_this_name"] = others
    return out


def call_tree(store: Store, name: str, direction: str = "in", depth: int = 3, limit: int = 10,
              path: str = "") -> dict:
    rows = find_defs(store, name, path, {"function", "method"})
    root = rows[0].qualname if rows else name
    lines = [f"{root}" + (f"  {rows[0].loc()}" if rows else "  (no definition indexed)")]
    seen = {short(root)}
    budget = [200]

    def walk_in(n: str, d: int, ind: str):
        for c in callers(store, None, n, limit, False)[0]:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            s = short(c["caller"])
            rep = s in seen
            lines.append(f"{ind}<- {c['caller']}  {c['at']}" + (f" x{c['calls']}" if c["calls"] > 1 else "")
                         + ("  (seen)" if rep else ""))
            if not rep and d + 1 < depth and c["caller"] != "(file level)":
                seen.add(s)
                walk_in(s, d + 1, ind + "   ")

    def walk_out(quals: list[str], d: int, ind: str):
        ph = ",".join("?" * len(quals))
        for dst, n, line, fpath in store.q(
                f"SELECT e.dst, COUNT(*), MIN(e.line), f.path FROM edges e JOIN files f ON f.id=e.file_id "
                f"WHERE e.kind='call' AND e.src IN ({ph}) GROUP BY e.dst ORDER BY MIN(e.line) LIMIT ?", (*quals, limit)):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            ds = [x for x in _defs_named(store, dst, 50) if x.name == dst and x.kind in FUNCS]
            where = ds[0].loc() if len({norm(x.qualname) for x in ds}) == 1 else \
                f"{len(ds)} defs" if ds else "not indexed"
            rep = dst in seen
            lines.append(f"{ind}-> {dst}  ({where}; called {fpath}:{line})" + ("  (seen)" if rep else ""))
            if ds and not rep and d + 1 < depth:
                seen.add(dst)
                walk_out(sorted({x.qualname for x in ds})[:5], d + 1, ind + "   ")

    if direction.startswith("in"):
        walk_in(short(root), 0, "  ")
    else:
        quals = sorted({r.qualname for r in rows if norm(r.qualname) == norm(root)}) or [root]
        walk_out(quals, 0, "  ")
    note = "name-based: callers of any definition with this name" if direction.startswith("in") else \
        "name-based: callee names resolved to definitions when unique"
    return {"tree": "\n".join(lines), "note": note, "truncated": budget[0] <= 0}


def _bfs_callers(store: Store, start: str, max_depth: int, cap: int = 6000):
    """Backward BFS over call edges from `start` (short name).
    dist[s]  hops from s down to start
    via[s]   [(callee short, caller qual, call site)] on shortest routes
    roots    expanded names nothing in the index calls (entry points: ticks, handlers, bindings, main)"""
    dist = {start: 0}
    via: dict[str, list[tuple[str, str, str]]] = {}
    roots: set[str] = set()
    frontier = [start]
    for d in range(1, max_depth + 1):
        nxt = []
        for n in frontier:
            rows = store.q("SELECT e.src, f.path, MIN(e.line) FROM edges e JOIN files f ON f.id=e.file_id "
                           "WHERE e.dst=? AND e.kind='call' AND e.src!='' GROUP BY e.src LIMIT 300", (n,))
            if not rows and n != start:
                roots.add(n)
            for src_q, path, line in rows:
                s = short(src_q)
                if s not in dist:
                    dist[s] = d
                    nxt.append(s)
                if dist[s] == d and len(via.setdefault(s, [])) < 8:
                    via[s].append((n, src_q, f"{path}:{line}"))
        frontier = nxt
        if not frontier or len(dist) > cap:
            break
    return dist, via, roots, set(frontier)


def _routes(via, frm: str, to: str, k: int, amb=None) -> list[list[tuple[str, str, str]]]:
    """Up to k shortest routes frm -> ... -> to as [(caller qual, call site, callee short)].
    With amb (name -> definition count), routes through unambiguous names come first."""
    out: list = []
    want = k if amb is None else k * 6

    def go(cur, acc):
        if len(out) >= want:
            return
        if cur == to:
            out.append(acc)
            return
        for nxt, q, at in via.get(cur, []):
            go(nxt, acc + [(q, at, nxt)])
    go(frm, [])
    if amb is not None:
        out.sort(key=lambda r: sum(amb(c) > 1 for _, _, c in r))
    return out[:k]


def _stack(route, leaf: str, leaf_loc: str, cut: bool = False, amb=None) -> list[str]:
    """Debugger-style pseudo stack: #0 = innermost callee, last frame = outermost caller.
    A frame tagged [name: N defs] calls a name with N definitions, so that link may be a different function."""
    frames = [f"#0  {leaf}  {leaf_loc}"]
    for i, (q, at, callee) in enumerate(reversed(route), 1):
        n = amb(callee) if amb else 1
        frames.append(f"#{i}  {q}  {at}" + (f"  [calls {callee}: {n} defs]" if n > 1 else ""))
    if cut:
        frames.append("    ... (more callers above; raise max_depth)")
    return frames


def _ambiguity(store: Store):
    cache: dict[str, int] = {}

    def amb(name: str) -> int:
        if name not in cache:
            cache[name] = store.q("SELECT COUNT(DISTINCT qualname) FROM symbols WHERE name=? AND kind IN "
                                  "('function','method')", (name,))[0][0]
        return cache[name]
    return amb


def _is_ctor(via, name: str) -> bool:
    q = via.get(name, [("", "", "")])[0][1]
    parts = _SEP.split(q)
    return len(parts) >= 2 and parts[-1] == parts[-2]


def _loc(store: Store, name: str) -> str:
    d = find_defs(store, name, kinds=FUNCS)
    return d[0].loc() if d else "(no definition indexed)"


def call_trace(store: Store, to: str, frm: str = "", max_depth: int = 8, k: int = 6) -> dict:
    """Static "how does execution get here": pseudo call stacks (innermost frame first) ending in `to`.

    to only:   walk callers back to entry points (names nothing calls) and return up to k stacks,
               one per entry point, shortest first; stacks that hit max_depth are marked as cut.
    frm + to:  how the two relate: frm reaches to, to reaches frm, or the nearest shared callers.
    """
    st = short(to)
    note = "static, name-based: each frame calls the frame below it by name; no runtime/type info"
    dist, via, roots, frontier = _bfs_callers(store, st, max_depth)
    amb = _ambiguity(store)
    if not frm:
        loc = _loc(store, to)
        ends = sorted(roots, key=lambda r: (dist[r], r))
        cut = sorted(frontier - roots, key=lambda r: r)
        stacks = []
        for r in ends[:k]:
            for route in _routes(via, r, st, 1, amb):
                stacks.append(_stack(route, st, loc, amb=amb))
        for r in cut[:max(0, k - len(stacks))]:
            for route in _routes(via, r, st, 1, amb):
                stacks.append(_stack(route, st, loc, cut=True, amb=amb))
        out = {"target": to, "defined_at": loc, "stacks": stacks,
               "entry_points_total": len(roots), "callers_explored": len(dist) - 1, "note": note}
        if roots:
            out["entry_points"] = [f"{r}  ({dist[r]} hops)" + ("  (constructor: built via new/declaration, "
                                   "not traced further)" if _is_ctor(via, r) else "") for r in ends[:30]]
        if not stacks:
            out["result"] = "nothing in the index calls this (entry point, callback, virtual, or script/reflection use)"
        return out

    sf = short(frm)
    locs = {sf: _loc(store, frm), st: _loc(store, to)}
    out: dict = {"from": frm, "to": to, "note": note}
    dist_f, via_f, _, _ = _bfs_callers(store, sf, max_depth)
    if sf in dist and sf != st:
        out["from_reaches_to"] = [_stack(r, st, locs[st], amb=amb) for r in _routes(via, sf, st, k, amb)]
    if st in dist_f and sf != st:
        out["to_reaches_from"] = [_stack(r, sf, locs[sf], amb=amb) for r in _routes(via_f, st, sf, k, amb)]
    common = sorted((dist_f[s] + dist[s], max(dist_f[s], dist[s]), s) for s in dist_f.keys() & dist.keys()
                    if s not in (sf, st))
    if common:
        shared = []
        for _, _, s in common:
            if len(shared) >= k:
                break
            # same short name is not enough: the top frame must be the same function in the same file
            ra, rb = _routes(via_f, s, sf, 8, amb), _routes(via, s, st, 8, amb)
            pair = next(((x, y) for x in ra for y in rb
                         if x[0][0] == y[0][0] and x[0][1].rsplit(":", 1)[0] == y[0][1].rsplit(":", 1)[0]), None)
            if pair:
                shared.append({"caller": pair[0][0][0], "stack_to_from": _stack(pair[0], sf, locs[sf], amb=amb),
                               "stack_to_to": _stack(pair[1], st, locs[st], amb=amb)})
        if shared:
            out["shared_callers"] = shared
    if not any(x in out for x in ("from_reaches_to", "to_reaches_from", "shared_callers")):
        out["result"] = (f"no static relation within {max_depth} hops "
                         f"(explored {len(dist_f)} callers of {sf}, {len(dist)} of {st})")
    return out


def impact(store: Store, name: str, depth: int = 3, group_depth: int = 3, limit: int = 25) -> dict:
    """Everything that (transitively) calls `name`: the blast radius of changing it."""
    frontier, seen = {short(name)}, set()
    per_level, files, folder = [], Counter(), Counter()
    for _ in range(depth):
        nxt, level = set(), 0
        for n in frontier:
            for src_q, path in store.q("SELECT DISTINCT e.src, f.path FROM edges e JOIN files f ON f.id=e.file_id "
                                       "WHERE e.dst=? AND e.kind='call' LIMIT 2000", (n,)):
                key = (src_q, path)
                if key in seen:
                    continue
                seen.add(key)
                level += 1
                files[path] += 1
                folder["/".join(path.split("/")[:group_depth])] += 1
                if src_q:
                    nxt.add(short(src_q))
        per_level.append(level)
        frontier = nxt - {short(name)}
        if not frontier or len(seen) > 20000:
            break
    return {"symbol": name, "callers_per_level": per_level, "caller_symbols": len(seen), "files": len(files),
            "by_folder": [f"{k}/  {v}" for k, v in folder.most_common(limit)],
            "top_files": [f"{k}  {v}" for k, v in files.most_common(limit)],
            "note": "name-based over-approximation: every caller of any definition with the same name"}


def type_hierarchy(store: Store, name: str, depth: int = 6, limit: int = 60) -> dict:
    quals = _type_quals(store, name) or [name]
    up = [" -> ".join([quals[0]] + c) for c in base_chains(store, quals, depth)]
    lines, budget = [quals[0]], [limit]

    def down(n: str, d: int, ind: str, seen: set):
        for src, path, line in sorted(derived_of(store, n), key=lambda r: r[0]):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            lines.append(f"{ind}{src}  {path}:{line}")
            s = short(src)
            if s not in seen and d + 1 < depth:
                down(s, d + 1, ind + "  ", seen | {s})

    down(short(quals[0]), 0, "  ", {short(quals[0])})
    return {"type": quals[0], "bases": up or ["(no bases)"], "derived_tree": "\n".join(lines),
            "derived_total": len(derived_closure(store, quals[0])), "truncated": budget[0] <= 0}


def resolve_file(store: Store, path: str) -> tuple[int, str] | list[str] | None:
    p = path.replace("\\", "/").strip().lstrip("./")
    r = store.q("SELECT id, path FROM files WHERE path=? COLLATE NOCASE", (p,))
    if not r:
        r = store.q("SELECT id, path FROM files WHERE path LIKE ? ORDER BY length(path) LIMIT 10", ("%/" + p,))
    if not r:
        r = store.q("SELECT id, path FROM files WHERE path LIKE ? ORDER BY length(path) LIMIT 10", ("%" + p + "%",))
    if len(r) == 1:
        return r[0]
    return [x[1] for x in r] or None


def file_context(store: Store, path: str, limit: int = 15) -> dict:
    f = resolve_file(store, path)
    if f is None:
        return {"error": f"no indexed file matches {path!r}"}
    if isinstance(f, list):
        return {"error": f"{path!r} is ambiguous", "candidates": f}
    fid, fpath = f
    syms = store.q("SELECT name, qualname, kind, parent, start_line, end_line FROM symbols WHERE file_id=? "
                   "ORDER BY start_line", (fid,))
    outline = []
    for name, q, kind, parent, s, e in syms[:200]:
        ind = "  " * min(norm(parent).count(".") + 1 if parent else 0, 4)
        outline.append(f"L{s}-{e} {ind}{kind} {q}")
    out: dict = {"path": fpath, "outline": outline}
    if len(syms) > 200:
        out["outline_truncated"] = len(syms)
    inc = [l or d for d, l in store.q("SELECT dst, label FROM edges WHERE file_id=? AND kind='include' ORDER BY line", (fid,))]
    if inc:
        out["includes"] = inc
    base = fpath.rsplit("/", 1)[-1].lower()
    by = [p for p, label in store.q("SELECT DISTINCT f.path, e.label FROM edges e JOIN files f ON f.id=e.file_id "
                                    "WHERE e.dst=? AND e.kind='include' LIMIT 5000", (base,))
          if fpath.lower().endswith((label or base).lower().lstrip("./"))]
    if by:
        out["included_by"] = {"files": len(by), "top": sorted(by)[:limit]}
    # coupling through calls (names with <=3 definitions, to keep it meaningful)
    out_files: Counter = Counter()
    for dst, n in store.q("SELECT dst, COUNT(*) FROM edges WHERE file_id=? AND kind='call' GROUP BY dst LIMIT 400", (fid,)):
        ds = {(x.path) for x in _defs_named(store, dst, 10) if x.name == dst}
        if 0 < len(ds) <= 3:
            for p in ds:
                if p != fpath:
                    out_files[p] += n
    if out_files:
        out["calls_into"] = [f"{p}  {n}" for p, n in out_files.most_common(limit)]
    names = sorted({n for n, _, k, _, _, _ in syms if k in FUNCS})[:300]
    in_files: Counter = Counter()
    for i in range(0, len(names), 200):
        part = names[i:i + 200]
        for p, n in store.q(f"SELECT f.path, COUNT(*) FROM edges e JOIN files f ON f.id=e.file_id WHERE e.kind='call' "
                            f"AND e.dst IN ({','.join('?' * len(part))}) AND e.file_id!=? GROUP BY f.path",
                            (*part, fid)):
            in_files[p] += n
    if in_files:
        out["called_from"] = [f"{p}  {n}" for p, n in in_files.most_common(limit)]
    return out


HOTSPOTS = ("called", "derived", "fanout", "included", "largest", "uncalled")


def hotspots(store: Store, what: str = "called", under: str = "", k: int = 30) -> dict:
    """Static top-N tables. under: path prefix restricting where the symbols are defined."""
    if what not in HOTSPOTS:
        return {"error": f"what must be one of {', '.join(HOTSPOTS)}"}
    pre = under.replace("\\", "/").strip("/")
    fwhere, fargs = ("AND f.path LIKE ?", [pre + "/%"]) if pre else ("", [])
    rows: list[str] = []
    if what in ("called", "derived"):
        ek = "call" if what == "called" else "inherits"
        kinds = "s.kind IN ('function','method')" if what == "called" else \
            "s.kind NOT IN ('function','method','namespace','module','field','variable','constant','macro')"
        for dst, n, nf in store.q(
                f"SELECT e.dst, COUNT(*), COUNT(DISTINCT e.file_id) FROM edges e WHERE e.kind='{ek}' AND e.dst IN "
                f"(SELECT s.name FROM symbols s JOIN files f ON f.id=s.file_id WHERE {kinds} {fwhere}) "
                f"GROUP BY e.dst ORDER BY COUNT(*) DESC LIMIT ?", (*fargs, k * 2)):
            if len(dst) < 3:
                continue
            nd = store.q("SELECT COUNT(DISTINCT qualname) FROM symbols WHERE name=?", (dst,))[0][0]
            rows.append(f"{dst}  {n} {'calls' if ek == 'call' else 'subtypes'} in {nf} files"
                        + (f"  ({nd} definitions share this name)" if nd > 1 else ""))
    elif what == "fanout":
        for src, n, nd, path in store.q(
                f"SELECT e.src, COUNT(*), COUNT(DISTINCT e.dst), f.path FROM edges e JOIN files f ON f.id=e.file_id "
                f"WHERE e.kind='call' AND e.src!='' {fwhere} GROUP BY e.src ORDER BY COUNT(DISTINCT e.dst) DESC LIMIT ?",
                (*fargs, k)):
            rows.append(f"{src}  calls {nd} distinct ({n} sites)  {path}")
    elif what == "included":
        for dst, nf in store.q("SELECT dst, COUNT(DISTINCT file_id) FROM edges WHERE kind='include' "
                               "GROUP BY dst ORDER BY 2 DESC LIMIT ?", (k * 3 if pre else k,)):
            hit = store.q("SELECT path FROM files WHERE path LIKE ? " + ("AND path LIKE ?" if pre else "") + " LIMIT 1",
                          ("%/" + dst, *([pre + "/%"] if pre else [])))
            if pre and not hit:
                continue
            rows.append(f"{dst}  included by {nf} files" + (f"  ({hit[0][0]})" if hit else ""))
            if len(rows) >= k:
                break
    elif what == "largest":
        for q, kind, path, s, e in store.q(
                f"SELECT s.qualname, s.kind, f.path, s.start_line, s.end_line FROM symbols s JOIN files f ON f.id=s.file_id "
                f"WHERE s.kind IN ('function','method') {fwhere} ORDER BY s.end_line - s.start_line DESC LIMIT ?",
                (*fargs, k)):
            rows.append(f"{q}  {e - s + 1} lines  {path}:{s}")
    else:   # uncalled
        for q, name, parent, path, s in store.q(
                f"SELECT s.qualname, s.name, s.parent, f.path, s.start_line FROM symbols s JOIN files f ON f.id=s.file_id "
                f"WHERE s.kind IN ('function','method') {fwhere} "
                f"AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst=s.name AND e.kind='call') "
                f"AND s.name NOT LIKE 'operator%' AND s.name NOT LIKE '~%' AND s.name NOT LIKE 'test%' "
                f"AND s.name NOT IN ('main','WinMain','wmain','__init__','Main') ORDER BY f.path, s.start_line LIMIT ?",
                (*fargs, k * 2)):
            if parent and short(parent) == name:      # constructor
                continue
            rows.append(f"{q}  {path}:{s}")
        rows = rows[:k]
    notes = {"uncalled": "no call by this name anywhere in the index. Could still be virtual overrides, callbacks, "
                         "reflection, macro/script use or exports",
             "called": "counts are by name (all definitions sharing a name are pooled)"}
    return {"what": what, "under": pre or "(everything)", "rows": rows, "note": notes.get(what, "")}
