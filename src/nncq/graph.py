"""Symbol-graph views over the index (no GPU, pure SQLite). All matching is by name: a call to `update`
links to every definition named `update`. Where that is ambiguous, results say so and rank the likely
targets first (same class, or the caller's code mentions the class).

  symbol_context   one-call "neighbourhood" card: definition(s), bases/derived/members or
                   callers/callees/overrides, data types that use it (profile data xref), how widely it is mentioned
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
MACRO = re.compile(r"^[A-Z0-9_]+$")
UTILITY_CALLS = 1500     # names called from more places than this are plumbing (get, size, assert, log...)
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


def _defs_exact(store: Store, name: str, langs: list[str] | None = None, limit: int = 500) -> list[Def]:
    """Definitions spelled exactly `name` (callee resolution: EndFrame in C# is not endFrame in C++)."""
    lw, la = _lang_sql(langs or [])
    return [Def(*r) for r in store.q(
        "SELECT s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature, f.path, s.file_id "
        "FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? COLLATE NOCASE AND s.name=?" + lw + " LIMIT ?",
        (name, name, *la, limit))]


_TEST_PATH = re.compile(r"(^|/)(unit_?)?tests?(/|$)|(^|/)tests?[^/]*$|[_.]tests?\.[a-z]+$|(^|/)mocks?(/|$)", re.I)
_TEST_NAME = re.compile(r"^(TEST(_[A-Z]+)?|[A-Z_]*MOCK[A-Z_]*|test_\w+)$")


def is_test(path: str, caller: str = "") -> bool:
    """Test or mock code: listed after production callers, which are what 'how does this get called' means."""
    return bool(_TEST_PATH.search(path or "") or _TEST_NAME.match(short(caller or "")))


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


_FAMILIES = [{"c", "cpp", "objc", "cuda"}, {"javascript", "typescript", "tsx"}]


def lang_family(store: Store, file_id: int | None) -> list[str]:
    """Languages whose calls can reach a definition in this file: a C++ `render` is not called from Python.
    [] = no restriction (unknown file)."""
    if file_id is None:
        return []
    r = store.q("SELECT lang FROM files WHERE id=?", (file_id,))
    lang = r[0][0] if r else None
    if not lang:
        return []
    return sorted(next((f for f in _FAMILIES if lang in f), {lang}))


def _lang_sql(langs: list[str], col: str = "f.lang") -> tuple[str, tuple]:
    return (f" AND {col} IN ({','.join('?' * len(langs))})", tuple(langs)) if langs else ("", ())


def _chunk_text(store: Store, file_id: int, line: int) -> str:
    r = store.q("SELECT text FROM chunks WHERE file_id=? AND start_line<=? AND end_line>=? LIMIT 1",
                (file_id, line, line))
    return r[0][0] if r else ""


def _line_text(store: Store, file_id: int, line: int) -> str:
    r = store.q("SELECT text, start_line FROM chunks WHERE file_id=? AND start_line<=? AND end_line>=? LIMIT 1",
                (file_id, line, line))
    if not r:
        return ""
    lines = r[0][0].split("\n")
    i = line - r[0][1]
    return lines[i] if 0 <= i < len(lines) else ""


def own_call(store: Store, file_id: int, line: int, name: str, cls: str = "") -> bool | None:
    """Is `name(...)` on this line a call on the caller's own class (plain, this->, Base::, or a receiver named
    after the class: gameRenderer->, g_gameRenderer.)? False: only on other objects (m_pool->name());
    None: the call is not on this line."""
    found = None
    named = cls.lower()
    for m in re.finditer(rf"\b{re.escape(name)}\s*(<[^<>()]*>)?\s*\(", _line_text(store, file_id, line)):
        pre = m.string[:m.start()].rstrip()
        if not (pre.endswith("->") or (pre.endswith(".") and not pre.endswith(".."))):
            return True
        if re.search(r"\bthis\s*(->|\.)$", pre):
            return True
        recv = re.search(r"(\w+)\s*(->|\.)$", pre)
        if named and recv and re.sub(r"^(m_|s_|g_|p_)", "", recv.group(1).lower()) == named:
            return True
        found = False
    return found


# ------------------------------------------------------------------ edges by name

def callers(store: Store, target: Def | None, name: str, limit: int, ambiguous: bool,
            langs: list[str] | None = None, path: str = "") -> tuple[list[dict], int, int]:
    """Call sites of `name`, grouped by calling symbol. Ranked: same class > mentions class > by name.
    langs: caller languages (default: the target's language family). path: substring filter on caller files."""
    s = short(name)
    if langs is None:
        langs = lang_family(store, target.file_id) if target else []
    lw, la = _lang_sql(langs)
    if path:
        lw += " AND f.path LIKE ?"
        la += (f"%{path.replace(chr(92), '/')}%",)
    base = "FROM edges e JOIN files f ON f.id=e.file_id WHERE e.dst=? AND e.kind='call'" + lw
    total, files = store.q(f"SELECT COUNT(*), COUNT(DISTINCT e.file_id) {base}", (s, *la))[0]
    grp = f"SELECT e.src, e.file_id, f.path, MIN(e.line), COUNT(*) {base}"
    cls = short(target.parent) if target and target.parent else ""
    rows = []
    if cls and ambiguous:      # same-class callers first, whatever their call counts (render() has thousands)
        rows = store.q(f"{grp} AND e.src LIKE ? GROUP BY e.src, e.file_id LIMIT 200", (s, *la, f"%{cls}%"))
    seen = {(r[0], r[1]) for r in rows}
    rows += [r for r in store.q(f"{grp} GROUP BY e.src, e.file_id ORDER BY COUNT(*) DESC LIMIT 400", (s, *la))
             if (r[0], r[1]) not in seen]
    out = []
    want = norm(target.parent) if target and target.parent else ""
    # classes with their own `name`: their unqualified calls go to that one, not to the target
    owners = ({_parent_short(d.qualname) for d in _defs_exact(store, s, langs, 2000)} - {"", cls}
              if cls and ambiguous else set())
    for i, (src, fid, path_, line, n) in enumerate(rows):
        if owners and _parent_short(src) in owners:
            continue
        conf = "by name"
        if target and cls and ambiguous:
            src_parent = norm(src).rsplit(".", 1)[0] if "." in norm(src) else ""
            if src_parent and (src_parent == want or src_parent.endswith("." + want) or want.endswith("." + src_parent)):
                # same class, unless the call is on another object (m_pool->update())
                conf = "by name" if own_call(store, fid, line, s, cls) is False else "same class"
            elif i < 200 and re.search(rf"\b{re.escape(cls)}\b", _chunk_text(store, fid, line)):
                conf = "mentions " + cls
        elif not ambiguous:
            conf = "unique name"
        out.append({"caller": src or "(file level)", "at": f"{path_}:{line}", "calls": n, "match": conf,
                    "test": is_test(path_, src)})
    rank = lambda c: 0 if c["match"] in ("same class", "unique name") else 1 if c["match"].startswith("mentions") else 2
    out.sort(key=lambda c: (c["test"], rank(c), -c["calls"]))
    return out[:limit], total, files


def caller_rows(cs: list[dict]) -> list[dict]:
    """callers() rows as structured entries for UIs: {sym, path, line, times, match, by_name, test}."""
    out = []
    for c in cs:
        path, _, line = c["at"].rpartition(":")
        out.append({"sym": c["caller"], "path": path, "line": int(line) if line.isdigit() else 0, "times": c["calls"],
                    "match": c["match"], "by_name": c["match"] == "by name", "test": bool(c["test"])})
    return out


def callees(store: Store, quals: list[str], limit: int, langs: list[str] | None = None,
            rows_out: list | None = None) -> list[str]:
    """Calls made by `quals`, resolved in the caller's language family; a shared name defined in the caller's
    own class resolves to that one. Macros and plumbing (called from > UTILITY_CALLS places) are one line.
    rows_out: also collect structured rows {line, name, times, sym, path, def_line, note, candidates, external}."""
    rows_out = [] if rows_out is None else rows_out
    if not quals:
        return []
    ph = ",".join("?" * len(quals))
    rows = store.q(f"SELECT dst, COUNT(*), MIN(line), GROUP_CONCAT(file_id || ':' || line) FROM edges "
                   f"WHERE kind='call' AND src IN ({ph}) GROUP BY dst ORDER BY MIN(line)", tuple(quals))
    own = {_parent_short(q) for q in quals} - {""}
    out, minor = [], []
    for dst, n, line, sites in rows:
        tag = f" x{n}" if n > 1 else ""
        if MACRO.match(dst) or len(dst) < 3 or _call_count(store, dst) > UTILITY_CALLS:
            minor.append(dst + tag)
            continue
        if len(out) >= limit:
            continue
        ds = _defs_exact(store, dst, langs)
        groups = {norm(d.qualname): d for d in ds}
        mine = [d for d in groups.values() if _parent_short(d.qualname) in own]
        if mine and len(groups) > 1 and not any(
                own_call(store, int(f), int(ln), dst, _parent_short(mine[0].qualname)) is not False
                for f, ln in (x.split(":") for x in sites.split(",")[:8])):
            mine = []       # every call is on another object: m_pool->endFrame() is not our endFrame
        row = {"line": line, "name": dst, "times": n}
        if len(groups) == 1:
            d = next(iter(groups.values()))
            out.append(f"L{line} {dst}{tag} -> {d.qualname} {d.loc()}")
            row.update(sym=d.qualname, path=d.path, def_line=d.start)
        elif mine:
            out.append(f"L{line} {dst}{tag} -> {mine[0].qualname} {mine[0].loc()}  (same class; {len(groups)} defs)")
            row.update(sym=mine[0].qualname, path=mine[0].path, def_line=mine[0].start, note="same class",
                       candidates=len(groups))
        elif groups:
            out.append(f"L{line} {dst}{tag} -> {len(groups)} definitions (ambiguous)")
            row.update(candidates=len(groups))
        else:
            out.append(f"L{line} {dst}{tag} (not in index: library / macro / generated)")
            row["external"] = True
        rows_out.append(row)
    if minor:
        out.append(f"+ {len(minor)} macros/plumbing, not resolved: {', '.join(minor[:40])}"
                   + (" ..." if len(minor) > 40 else ""))
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
                out["data_types"] = [f"{t['type']} ({t['via']}, {t['files']} data files)" for t in dt[:6]]
    if p.kind in FUNCS or not is_type(p.kind):
        cs, total, files = callers(store, p, p.name, limit, ambiguous)
        if total:
            out["callers"] = [f"{c['caller']}  {c['at']}" + (f" x{c['calls']}" if c["calls"] > 1 else "")
                              + ("" if c["match"] in ("unique name", "by name") and not ambiguous
                                 else f"  [{c['match']}]") + ("  (test)" if c["test"] else "") for c in cs]
            out["callers_list"] = caller_rows(cs)
            out["call_sites_total"] = f"{total} calls in {files} files"
        rows: list = []
        ce = callees(store, quals, limit * 3, lang_family(store, p.file_id), rows)
        if ce:
            out["calls"] = ce
            out["calls_list"] = rows
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
    struct: list = []
    rows = callees(store, sorted({d.qualname for d in defs}), limit, lang_family(store, defs[0].file_id), struct)
    out = {"function": defs[0].qualname, "defined_at": [d.loc() for d in defs[:4]], "calls": rows, "calls_list": struct,
           "count": len(rows), "note": "static, name-based; calls through pointers/delegates/virtual "
                                       "dispatch show the name called, not the runtime target"}
    if others:
        out["other_definitions_with_this_name"] = others + ["(pass path= or a qualified name to pick one)"]
    return out


def called_by(store: Store, name: str, path: str = "", limit: int = 200, caller_path: str = "") -> dict:
    """Flat 'Calls To' table: every function that calls `name`, one row per caller and file, best match first.
    path picks the definition; caller_path keeps only callers in matching files. Callers are limited to the
    definition's language family (C/C++ together, JS/TS together)."""
    defs, others = _pick_func(store, name, path)
    target = defs[0] if defs else None
    ndefs = _ambiguity(store)(short(name))
    rows, total, files = callers(store, target, name, limit, bool(others) or ndefs > 1, path=caller_path)
    out = {"function": target.qualname if target else name,
           "defined_at": [d.loc() for d in defs[:4]] or "(no definition indexed: library, macro or generated)",
           "callers": [f"{c['caller']}  {c['at']}" + (f"  x{c['calls']}" if c["calls"] > 1 else "")
                       + ("" if c["match"] == "unique name" else f"  [{c['match']}]")
                       + ("  (test)" if c["test"] else "")
                       for c in rows],
           "callers_list": caller_rows(rows), "shown": len(rows), "call_sites_total": total, "files_total": files}
    if others or ndefs > 1:
        out["note"] = (f"{max(ndefs, len(others) + 1)} functions share the name {short(name)!r}; rows are ranked "
                       "[same class] > [mentions <class>] > [by name]. Lower ranks may call a different one.")
    if others:
        out["other_definitions_with_this_name"] = others
    return out


def call_tree(store: Store, name: str, direction: str = "in", depth: int = 3, limit: int = 10,
              path: str = "") -> dict:
    rows = find_defs(store, name, path, {"function", "method"})
    root = rows[0].qualname if rows else name
    lines = [f"{root}" + (f"  {rows[0].loc()}" if rows else "  (no definition indexed)")]
    seen = {short(root)}
    budget = [120]
    langs = lang_family(store, rows[0].file_id) if rows else []
    lw, la = _lang_sql(langs)
    skipped = Counter()

    amb = _ambiguity(store)

    def walk_in(n: str, d: int, ind: str, tgt: Def | None):
        # tgt: the definition reached; for shared names its own class's callers come first
        for c in callers(store, tgt, n, limit, amb(n) > 1, langs)[0]:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            s = short(c["caller"])
            rep = s in seen
            lines.append(f"{ind}<- {c['caller']}  {c['at']}" + (f" x{c['calls']}" if c["calls"] > 1 else "")
                         + ("  [same class]" if c["match"] == "same class" else "")
                         + ("  (test)" if c["test"] else "") + ("  (seen)" if rep else ""))
            if not rep and d + 1 < depth and c["caller"] != "(file level)":
                seen.add(s)
                p = c["at"].rsplit(":", 1)[0]
                nd = [x for x in find_defs(store, c["caller"], kinds=FUNCS) if x.path == p]
                walk_in(s, d + 1, ind + "   ", nd[0] if nd else None)

    def walk_out(quals: list[str], d: int, ind: str):
        ph = ",".join("?" * len(quals))
        shown = 0
        for dst, n, line, fpath in store.q(
                f"SELECT e.dst, COUNT(*), MIN(e.line), f.path FROM edges e JOIN files f ON f.id=e.file_id "
                f"WHERE e.kind='call' AND e.src IN ({ph}) GROUP BY e.dst ORDER BY MIN(e.line)", quals):
            if budget[0] <= 0 or shown >= limit:
                return
            if MACRO.match(dst) or len(dst) < 3:
                skipped["macros"] += 1
                continue
            if _call_count(store, dst) > UTILITY_CALLS:
                skipped["plumbing"] += 1
                continue
            budget[0] -= 1
            shown += 1
            ds = [x for x in _defs_exact(store, dst, langs) if x.kind in FUNCS]
            nq = len({norm(x.qualname) for x in ds})
            where = ds[0].loc() if nq == 1 else f"{nq} defs, not expanded" if ds else "not indexed"
            rep = dst in seen
            lines.append(f"{ind}-> {dst}  ({where}; called {fpath}:{line})" + ("  (seen)" if rep else ""))
            if nq == 1 and not rep and d + 1 < depth:
                seen.add(dst)
                walk_out(sorted({x.qualname for x in ds}), d + 1, ind + "   ")

    if direction.startswith("in"):
        walk_in(short(root), 0, "  ", rows[0] if rows else None)
    else:
        quals = sorted({r.qualname for r in rows if norm(r.qualname) == norm(root)}) or [root]
        walk_out(quals, 0, "  ")
    note = ("name-based: callers of any definition with this name; the definition's own class first, "
            "test code last") if direction.startswith("in") else \
        ("name-based: callee names resolved to definitions when unique; ambiguous names are listed, not expanded"
         + (f"; hidden: {', '.join(f'{v} {k}' for k, v in skipped.items())} (macros; plumbing called from "
            f">{UTILITY_CALLS} places)" if skipped else ""))
    out = {"tree": "\n".join(lines), "note": note}
    if langs:
        out["languages"] = ", ".join(langs)
    if budget[0] <= 0:
        out["cut"] = "node budget reached: lower depth or start from a deeper function"
    return out


_counts: dict[tuple[str, str], int] = {}


def _call_count(store: Store, name: str) -> int:
    k = (str(store.path), name)
    if k not in _counts:
        if len(_counts) > 50000:
            _counts.clear()
        _counts[k] = store.q("SELECT COUNT(*) FROM edges WHERE dst=? AND kind='call'", (name,))[0][0]
    return _counts[k]


def _parent_short(q: str) -> str:
    p = _SEP.split(q or "")
    return p[-2] if len(p) >= 2 else ""


def _bfs_callers(store: Store, start: str, max_depth: int, cap: int = 6000, langs: list[str] | None = None,
                 cls: str = ""):
    """Backward BFS over call edges from `start` (short name).
    dist[s]  hops from s down to start
    via[s]   [(callee short, caller qual, call site)] on shortest routes
    roots    expanded names nothing in the index calls (entry points: ticks, handlers, bindings, main)"""
    lw, la = _lang_sql(langs or [])
    hint = {start: cls}          # class of the definition we reached a name through: its own class calls it first
    dist = {start: 0}
    via: dict[str, list[tuple[str, str, str]]] = {}
    roots: set[str] = set()
    frontier = [start]
    for d in range(1, max_depth + 1):
        nxt = []
        for n in frontier:
            h = hint.get(n, "")
            rows = store.q("SELECT e.src, f.path, MIN(e.line) FROM edges e JOIN files f ON f.id=e.file_id "
                           "WHERE e.dst=? AND e.kind='call' AND e.src!=''" + lw + " GROUP BY e.src "
                           "ORDER BY (e.src LIKE ?) DESC LIMIT 300", (n, *la, f"%{h}%" if h else ""))
            if not rows and n != start:
                roots.add(n)
            for src_q, path, line in rows:
                s = short(src_q)
                if s not in dist:
                    dist[s] = d
                    nxt.append(s)
                    hint[s] = _parent_short(src_q)
                if dist[s] == d and len(via.setdefault(s, [])) < 8:
                    via[s].append((n, src_q, f"{path}:{line}"))
        frontier = nxt
        if not frontier or len(dist) > cap:
            break
    return dist, via, roots, set(frontier)


def _doubtful(route, amb, leaf_q: str) -> int:
    """Hops that call a shared name from outside the callee's class (may be a different function)."""
    n = 0
    for i, (q, _, callee) in enumerate(route):
        callee_q = route[i + 1][0] if i + 1 < len(route) else leaf_q
        if amb(callee) > 1 and not (_parent_short(q) and _parent_short(q) == _parent_short(callee_q)):
            n += 1
    return n


def _routes(via, frm: str, to: str, k: int, amb=None, leaf_q: str = "") -> list[list[tuple[str, str, str]]]:
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
        out.sort(key=lambda r: _doubtful(r, amb, leaf_q))
    return out[:k]


def _stack(route, leaf: str, leaf_loc: str, cut: bool = False, amb=None, leaf_q: str = "") -> list[str]:
    """Debugger-style pseudo stack: #0 = innermost callee, last frame = outermost caller.
    A frame tagged [name: N defs] calls a name with N definitions, so that link may be a different function."""
    frames = [f"#0  {leaf}  {leaf_loc}"]
    for i, (q, at, callee) in enumerate(reversed(route), 1):
        n = amb(callee) if amb else 1
        j = len(route) - i
        callee_q = route[j + 1][0] if j + 1 < len(route) else leaf_q
        same = n > 1 and _parent_short(q) and _parent_short(q) == _parent_short(callee_q)
        frames.append(f"#{i}  {q}  {at}" + (f"  [calls {callee}: {n} defs, same class]" if same
                                            else f"  [calls {callee}: {n} defs]" if n > 1 else ""))
    if cut:
        frames.append("    ... (more callers above; raise max_depth)")
    return frames


def _ambiguity(store: Store):
    cache: dict[str, int] = {}

    def amb(name: str) -> int:
        if name not in cache:
            cache[name] = store.q("SELECT COUNT(DISTINCT qualname) FROM symbols WHERE name=? COLLATE NOCASE AND name=? "
                                  "AND kind IN ('function','method')", (name, name))[0][0]
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
    td = find_defs(store, to, kinds=FUNCS)
    langs = lang_family(store, td[0].file_id) if td else []
    tq = td[0].qualname if td else to
    dist, via, roots, frontier = _bfs_callers(store, st, max_depth, langs=langs, cls=_parent_short(tq))
    amb = _ambiguity(store)
    if not frm:
        loc = _loc(store, to)
        # soft entry points: shared names whose only callers are other classes' functions of that name
        # (jobs, callbacks, virtuals): the static trail goes cold there, the stack above is probably wrong
        callers_of: dict[str, list[str]] = {}
        for lst in via.values():
            for callee, q, _ in lst:
                callers_of.setdefault(callee, []).append(q)
        soft = set()
        for r in sorted((r for r in dist if 0 < dist[r] <= 4 and r not in roots), key=lambda r: (dist[r], r))[:300]:
            own = _parent_short(via[r][0][1]) if via.get(r) else ""
            if own and amb(r) > 1 and not any(_parent_short(q) == own for q in callers_of.get(r, [])):
                soft.add(r)
        best = {}                       # entry point -> its most certain route (fewest doubtful hops)
        for r in sorted(roots | frontier | soft, key=lambda r: (dist[r], r))[:150]:
            rs = _routes(via, r, st, 1, amb, tq)
            if rs:
                best[r] = rs[0]
        # production stacks first: a trail through test/mock code answers "who tests this", not "how does it run"
        rank = lambda r: (any(is_test(at.rsplit(":", 1)[0], q) for q, at, _ in best[r]),
                          _doubtful(best[r], amb, tq), r not in roots, dist[r], r)
        ends = sorted((r for r in roots if r in best), key=rank)
        stacks = []
        for r in sorted(best, key=rank)[:k]:
            fr = _stack(best[r], st, loc, cut=r in frontier and r not in roots and r not in soft, amb=amb, leaf_q=tq)
            if r in soft:
                fr.append(f"    ... {r} is called only by same-named functions of other classes "
                          f"({len(set(callers_of.get(r, [])))}): likely reached via a job, callback or virtual")
            stacks.append(fr)
        ends += sorted(roots - set(best), key=lambda r: (dist[r], r))
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
    fd = find_defs(store, frm, kinds=FUNCS)
    fq = fd[0].qualname if fd else frm
    dist_f, via_f, _, _ = _bfs_callers(store, sf, max_depth, langs=langs, cls=_parent_short(fq))
    if sf in dist and sf != st:
        out["from_reaches_to"] = [_stack(r, st, locs[st], amb=amb, leaf_q=tq)
                                  for r in _routes(via, sf, st, k, amb, tq)]
    if st in dist_f and sf != st:
        out["to_reaches_from"] = [_stack(r, sf, locs[sf], amb=amb, leaf_q=fq)
                                  for r in _routes(via_f, st, sf, k, amb, fq)]
    common = sorted((dist_f[s] + dist[s], max(dist_f[s], dist[s]), s) for s in dist_f.keys() & dist.keys()
                    if s not in (sf, st))
    if common:
        shared = []
        for _, _, s in common:
            if len(shared) >= k:
                break
            # same short name is not enough: the top frame must be the same function in the same file
            ra, rb = _routes(via_f, s, sf, 8, amb, fq), _routes(via, s, st, 8, amb, tq)
            pair = next(((x, y) for x in ra for y in rb
                         if x[0][0] == y[0][0] and x[0][1].rsplit(":", 1)[0] == y[0][1].rsplit(":", 1)[0]), None)
            if pair:
                shared.append({"caller": pair[0][0][0],
                               "stack_to_from": _stack(pair[0], sf, locs[sf], amb=amb, leaf_q=fq),
                               "stack_to_to": _stack(pair[1], st, locs[st], amb=amb, leaf_q=tq)})
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
    typed = _type_quals(store, name)
    quals = typed or [name]
    up = [" -> ".join([quals[0]] + c) for c in base_chains(store, quals, depth)]
    lines, budget, derived = [quals[0]], [limit], []

    def down(n: str, d: int, ind: str, seen: set):
        for src, path, line in sorted(derived_of(store, n), key=lambda r: r[0]):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            lines.append(f"{ind}{src}  {path}:{line}")
            derived.append({"type": src, "path": path, "line": line, "depth": d + 1})
            s = short(src)
            if s not in seen and d + 1 < depth:
                down(s, d + 1, ind + "  ", seen | {s})

    down(short(quals[0]), 0, "  ", {short(quals[0])})
    at = next((x for x in find_defs(store, quals[0], kinds={"type"}) if is_type(x.kind)), None) if typed else None
    return {"type": quals[0], "is_type": bool(typed), **({"path": at.path, "line": at.start} if at else {}),
            "bases": up or ["(no bases)"], "derived_tree": "\n".join(lines), "derived": derived,
            "derived_total": len(derived_closure(store, quals[0])), "truncated": budget[0] <= 0}


DEP_KINDS = ("inherits", "composes", "aggregates", "associates", "depends", "instantiates", "var")


def _type_near(store: Store, name: str, near: str, fam: list[str]) -> Def | None:
    """The type definition a short type name in `near` most likely means: same language family, nearest folder."""
    ts = [d for d in _defs_exact(store, short(name), fam) if is_type(d.kind)]
    if len(ts) <= 1:
        return ts[0] if ts else None
    for depth in (4, 3, 2, 1):
        pre = "/".join(near.split("/")[:depth])
        hit = [t for t in ts if pre and t.path.startswith(pre)]
        if hit:
            return hit[0]
    return ts[0]


def type_deps(store: Store, name: str, direction: str = "both", depth: int = 1, limit: int = 40) -> dict:
    """Type dependency graph around a type (a member's owner type when given a member): every relation the index
    records between types (inheritance, composition / aggregation / association, member / parameter / return types,
    instantiation, held variables), outgoing ("this depends on") and incoming ("depends on this"), as nodes + edges
    {from, to, kind, n, path, line, cycle}; capped per node (more_out / more_in), cycles marked (see _dep_walk)."""
    defs = [d for d in find_defs(store, name, kinds={"type"}) if is_type(d.kind)]
    if not defs:
        d0 = next(iter(find_defs(store, name)), None)
        if d0 is not None and d0.parent:
            defs = [d for d in find_defs(store, d0.parent, kinds={"type"}) if is_type(d.kind)
                    and norm(d.qualname) == norm(d0.parent)]
    if not defs:
        return {**_not_found(store, name), "nodes": [], "edges": []}
    root = defs[0]
    ph = ",".join("?" * len(DEP_KINDS))

    def out_of(t: Def) -> list[tuple]:
        fam = lang_family(store, t.file_id)
        rows = store.q(f"SELECT e.dst, e.kind, COUNT(*), MIN(e.line), f.path FROM edges e JOIN files f ON f.id=e.file_id "
                       f"WHERE e.kind IN ({ph}) AND (e.src=? OR e.src IN (SELECT qualname FROM symbols WHERE parent=?)) "
                       f"GROUP BY e.dst, e.kind", (*DEP_KINDS, t.qualname, t.qualname))
        out = []
        for dst, kind, n, line, path in rows:
            d = _type_near(store, dst, path, fam)
            if d is not None and d.qualname != t.qualname:
                out.append((d, kind, n, path, line))
        return out

    def in_of(t: Def) -> list[tuple]:
        fam = lang_family(store, t.file_id)
        lw, la = _lang_sql(fam)
        rows = store.q(f"SELECT e.src, e.kind, COUNT(*), MIN(e.line), f.path FROM edges e JOIN files f ON f.id=e.file_id "
                       f"WHERE e.kind IN ({ph}) AND e.dst=?{lw} GROUP BY e.src, e.kind LIMIT 2000",
                       (*DEP_KINDS, t.name, *la))
        agg: dict[tuple, list] = {}
        means: dict[str, Def | None] = {}
        for src, kind, n, line, path in rows:
            if path not in means:
                means[path] = _type_near(store, t.name, path, fam)
            if means[path] is not None and norm(means[path].qualname) != norm(t.qualname):
                continue                                 # that name means another type over there
            s = next(iter(store.q("SELECT s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, "
                                  "s.signature, f.path, s.file_id FROM symbols s JOIN files f ON f.id=s.file_id "
                                  "WHERE s.qualname=? AND f.path=? LIMIT 1", (src, path))), None)
            if s is None:
                continue
            d = Def(*s)
            if not is_type(d.kind) and d.parent:
                owner = [x for x in find_defs(store, d.parent, kinds={"type"}) if is_type(x.kind)
                         and norm(x.qualname) == norm(d.parent)]
                d = owner[0] if owner else d
            if d.qualname == t.qualname:
                continue
            k = (d.qualname, kind)
            if k in agg:
                agg[k][2] += n
            else:
                agg[k] = [d, kind, n, path, line]
        return [tuple(v) for v in agg.values()]

    defs_by: dict[str, Def] = {root.qualname: root}

    def neighbors(q: str, way: str) -> list[tuple]:
        t = defs_by[q]
        out = []
        for d, kind, n, path, line in (out_of(t) if way == "out" else in_of(t)):
            defs_by.setdefault(d.qualname, d)
            out.append((d.qualname, kind, n, path, line, {"id": d.qualname, "label": d.name, "kind": d.kind,
                                                          "path": d.path, "line": d.start}))
        return out
    g = _dep_walk(root.qualname, {"id": root.qualname, "label": root.name, "kind": root.kind, "path": root.path,
                                  "line": root.start}, neighbors, direction, depth, limit)
    return {"type": root.qualname, "kind": root.kind, "path": root.path, "line": root.start, **g}


def _dep_walk(root: str, root_info: dict, neighbors, direction: str, depth: int, limit: int) -> dict:
    """Breadth-first dependency walk shared by type_deps / file_deps. A node found going out keeps going out, one found
    coming in keeps coming in (both from the root). An edge back to a node on its own path is a cycle: marked
    (edge cycle: true), counted, never expanded. Per node at most `limit` neighbours each way (more_out / more_in)."""
    nodes = {root: {**root_info, "depth": 0}}
    edges: dict[tuple, dict] = {}
    more = {"out": {}, "in": {}}
    cycles = 0
    path_of = {(root, "out"): frozenset([root]), (root, "in"): frozenset([root])}
    frontier = [(root, 0, w) for w in (("out", "in") if direction == "both" else (direction,))]
    while frontier:
        t, lvl, way = frontier.pop(0)
        grouped: dict[str, list] = {}
        for row in neighbors(t, way):
            grouped.setdefault(row[0], []).append(row)
        keep = sorted(grouped.values(), key=lambda xs: -sum(x[2] for x in xs))
        if len(keep) > limit:
            more[way][t] = len(keep) - limit
        anc = path_of[(t, way)]
        for xs in keep[:limit]:
            o = xs[0][0]
            cyc = o in anc
            nodes.setdefault(o, {**xs[0][5], "depth": lvl + 1})
            for _, kind, n, path, line, _info in xs:
                a, b = (t, o) if way == "out" else (o, t)
                edges.setdefault((a, b, kind), {"from": a, "to": b, "kind": kind, "n": n, "path": path, "line": line,
                                                **({"cycle": True} if cyc else {})})
            if cyc:
                cycles += 1
                nodes[o]["cycle"] = True if o != root else nodes[o].get("cycle", False)
            elif (o, way) not in path_of:
                path_of[(o, way)] = anc | {o}
                if lvl + 1 < depth:
                    frontier.append((o, lvl + 1, way))
    return {"root": root, "direction": direction, "depth": depth, "nodes": list(nodes.values()),
            "edges": list(edges.values()), "cycles": cycles,
            **({"more_out": more["out"]} if more["out"] else {}), **({"more_in": more["in"]} if more["in"] else {})}


def file_deps(store: Store, path: str, direction: str = "both", depth: int = 1, limit: int = 40) -> dict:
    """Include / import dependency graph around a file: what it includes (out), who includes it (in), transitively by
    depth, with the same caps and cycle marking as type_deps. Nodes {id: path, label, kind: file, folder, component}."""
    f = resolve_file(store, path)
    if f is None:
        return {"error": f"no indexed file matches {path!r}", "nodes": [], "edges": []}
    if isinstance(f, list):
        return {"error": f"{path!r} is ambiguous", "candidates": f, "nodes": [], "edges": []}
    fid, fpath = f
    ids: dict[str, int] = {fpath: fid}
    comp = {}

    def info(pth: str) -> dict:
        if pth not in comp:
            r = store.q("SELECT c.name FROM comp_files cf JOIN files f ON f.id=cf.file_id JOIN components c "
                        "ON c.id=cf.comp_id WHERE f.path=?", (pth,))
            comp[pth] = r[0][0] if r else ""
        return {"id": pth, "label": pth.rsplit("/", 1)[-1], "kind": "file", "path": pth, "line": 1,
                "folder": pth.rsplit("/", 1)[0] if "/" in pth else "", "component": comp[pth]}

    def neighbors(pth: str, way: str) -> list[tuple]:
        a, b = ("src_id", "dst_id") if way == "out" else ("dst_id", "src_id")
        out = []
        for oid, opath, kind, n in store.q(f"SELECT f.id, f.path, d.kind, d.n FROM file_deps d JOIN files f ON f.id=d.{b} "
                                            f"WHERE d.{a}=?", (ids[pth],)):
            ids.setdefault(opath, oid)
            out.append((opath, kind, n or 1, pth if way == "out" else opath, 0, info(opath)))
        return out
    g = _dep_walk(fpath, info(fpath), neighbors, direction, depth, limit)
    return {"file": fpath, **g}


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
            nd = store.q("SELECT COUNT(DISTINCT qualname) FROM symbols WHERE name=? COLLATE NOCASE AND name=?",
                         (dst, dst))[0][0]
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
