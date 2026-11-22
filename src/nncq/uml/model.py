"""Repo-level UML model: components (build targets, Python packages, folders), which files belong to which,
their link / build dependencies, file -> file dependencies (resolved #include and import edges) and build
configurations. Rebuilt in one pass from the index plus the build files (read-only on the workspace); cheap enough
to run after every index update. Per-file facts (members, relations, ...) come from extract.py at parse time.

enabled(store)  UML is on for this index: NNCQ_UML=true, or it was built on demand once (meta uml=1)
build()         the whole pass;  refresh(): after a delta, only what the changed paths can affect
"""
from __future__ import annotations

import json
import posixpath
import time
from collections import Counter, defaultdict
from pathlib import Path

from nncq import config, langs
from nncq.store import Store
from nncq.uml import FACTS_VERSION, buildinfo

MODEL_VERSION = "1"
_TOP_BUILD = ("CMakeLists.txt", "CMakePresets.json", "CMakeUserPresets.json", "pyproject.toml", "setup.py")


def enabled(store: Store) -> bool:
    return store.meta("uml") == "1" or config.settings().uml


def facts_ready(store: Store) -> bool:
    return store.meta("uml_facts") == FACTS_VERSION


def state(store: Store) -> dict:
    """What the UML model has, for status / tools / the dashboard."""
    one = lambda sql: store.q(sql)[0][0]
    built = store.meta("uml_built_at")
    out = {"enabled": enabled(store), "facts": facts_ready(store),
           "built_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(built))) if built else None,
           "components": one("SELECT COUNT(*) FROM components WHERE kind!='external'"),
           "views": one("SELECT COUNT(*) FROM views"), "stale_views": one(
               "SELECT COUNT(*) FROM views WHERE stale=1 OR id IN (SELECT view_id FROM view_dirty)")}
    if not out["facts"] or not built:
        out["note"] = ("UML model not built yet: run `nncq uml init`, the uml_build tool or the dashboard's "
                       "Build button (one CPU re-parse; vectors are kept)")
    return out


def _build_files(store: Store, root: Path) -> list[str]:
    paths = [p for (p,) in store.q("SELECT path FROM files") if buildinfo.is_build_file(p)]
    tops = {p.split("/", 1)[0] for (p,) in store.q("SELECT path FROM files") if "/" in p}
    for d in [""] + sorted(tops):
        for name in _TOP_BUILD:
            rel = f"{d}/{name}" if d else name
            if rel not in paths and (root / rel).is_file():
                paths.append(rel)
    return paths


def build(store: Store, root: Path, log=print) -> dict:
    t0 = time.time()
    info = buildinfo.parse(root, _build_files(store, root))
    files = store.q("SELECT id, path, lang FROM files")
    code = [(fid, p, lang) for fid, p, lang in files if lang and langs.is_ast_lang(lang)]
    by_path = {p: fid for fid, p, _ in files}
    comps: list[dict] = []
    names: dict[str, int] = {}

    def add(name, kind, d, source="", line=0, info_=None) -> int:
        comps.append({"name": name, "kind": kind, "dir": d, "source": source, "line": line, "info": info_ or {}})
        names.setdefault(name, len(comps) - 1)
        return len(comps) - 1

    primary: dict[str, int] = {}
    assign: dict[int, int] = {}
    for t in info.targets.values():
        if t.kind == "external":
            continue
        i = add(t.name, t.kind, t.dir, t.source, t.line, {"type": t.type, "includes": t.includes,
                                                          "sources": len(t.sources)})
        if t.dir not in primary or (t.kind == "library" and comps[primary[t.dir]]["kind"] != "library"):
            primary[t.dir] = i
        for s in t.sources:
            if s in by_path:
                assign.setdefault(by_path[s], i)
    for t in info.targets.values():          # headers in a target's own include folders belong to it
        if t.kind == "external":
            continue
        i = names[t.name]
        for dirs in t.includes.values():
            for d in dirs:
                if not d or not comps[i]["dir"] or not d.startswith(comps[i]["dir"]):
                    continue
                pre = d.rstrip("/") + "/"
                for fid, p, _ in code:
                    if p.startswith(pre) and fid not in assign:
                        assign[fid] = i
    for name, d, source, extra in info.packages:
        i = add(name, "package", d, source, 0, extra)
        primary.setdefault(d, i)
        for script, entry in (extra.get("scripts") or {}).items():
            add(script, "script", d, source, 0, {"entry": entry, "package": name})
    pkg_dirs = sorted({posixpath.dirname(p) for _, p, lang in code if lang == "python" and p.endswith("/__init__.py")})
    for d in pkg_dirs:
        if posixpath.dirname(d) in pkg_dirs or d in primary:
            continue
        primary[d] = add(posixpath.basename(d) or d, "package", d, d + "/__init__.py")
    fallback: dict[str, int] = {}
    for fid, p, lang in code:
        if fid in assign:
            continue
        d = posixpath.dirname(p)
        while True:
            if d in primary:
                assign[fid] = primary[d]
                break
            if not d:
                break
            d = posixpath.dirname(d)
        if fid in assign:
            continue
        parts = p.split("/")
        d = "/".join(parts[:2]) if len(parts) > 2 else parts[0] if len(parts) > 1 else ""
        if d not in fallback:
            nm = d or "(root)"
            fallback[d] = add(nm if nm not in names else d, "module" if lang == "python" else "directory", d)
        assign[fid] = fallback[d]
    externals = sorted({info.resolve(n) for t in info.targets.values() for n, _ in t.links} - set(info.targets)
                       - {c["name"] for c in comps})
    for n in externals:
        add(n, "external", None)
    for t in info.targets.values():
        if t.kind == "external" and t.name not in names:
            add(t.name, "external", None, t.source, t.line, {"type": "IMPORTED"})
    scripts = [p for (p,) in store.q("SELECT DISTINCT f.path FROM chunks c JOIN files f ON f.id=c.file_id WHERE "
                                     "f.lang='python' AND c.text LIKE '%\\_\\_name\\_\\_%\\_\\_main\\_\\_%' ESCAPE '\\' "
                                     "LIMIT 500")]
    deps = _file_deps(store, code, assign)
    with store.tx() as db:
        for t in ("components", "comp_files", "comp_deps", "build_configs", "file_deps"):
            db.execute(f"DELETE FROM {t}")
        ids = {}
        for i, c in enumerate(comps):
            ids[i] = db.execute("INSERT INTO components(name, kind, dir, source, line, info) VALUES(?,?,?,?,?,?)",
                                (c["name"], c["kind"], c["dir"], c["source"], c["line"],
                                 json.dumps(c["info"]))).lastrowid
        for p in scripts:
            fid = by_path[p]
            ci = db.execute("INSERT INTO components(name, kind, dir, source, line, info) VALUES(?,?,?,?,?,?)",
                            (posixpath.basename(p), "script", posixpath.dirname(p), p, 0,
                             json.dumps({"entry": p, "main": True}))).lastrowid
            if fid in assign:
                db.execute("INSERT INTO comp_deps(src_id, dst, kind, scope) VALUES(?,?,?,?)",
                           (ci, comps[assign[fid]]["name"], "runs", ""))
        db.executemany("INSERT OR REPLACE INTO comp_files(file_id, comp_id) VALUES(?,?)",
                       ((fid, ids[i]) for fid, i in assign.items()))
        for t in info.targets.values():
            if t.name in names and t.kind != "external":
                src = ids[names[t.name]]
                db.executemany("INSERT INTO comp_deps(src_id, dst, kind, scope) VALUES(?,?,?,?)",
                               [(src, info.resolve(n), "link", s) for n, s in dict.fromkeys(t.links)]
                               + [(src, info.resolve(n), "build", "") for n in dict.fromkeys(t.deps)])
        db.executemany("INSERT INTO file_deps(src_id, dst_id, kind, n) VALUES(?,?,?,?)",
                       ((a, b, k, n) for (a, b, k), n in deps.items()))
        seen = set()
        for row in info.configs:
            if row[:3] not in seen:
                seen.add(row[:3])
                db.execute("INSERT INTO build_configs(name, kind, source, info) VALUES(?,?,?,?)", row)
        store.set_meta("uml_built_at", time.time())
        store.set_meta("uml_model", MODEL_VERSION)
        store.set_meta("uml", "1")
    kinds = Counter(c["kind"] for c in comps)
    out = {"components": dict(kinds), "files_assigned": len(assign), "file_deps": len(deps),
           "configs": len(seen), "seconds": round(time.time() - t0, 2)}
    log(f"  uml model: {out}")
    return out


def _file_deps(store: Store, code, assign, only: set[int] | None = None) -> Counter:
    """(src file, dst file, include|import) -> count, for #include / import edges that resolve to an indexed file.
    A basename shared by several files is resolved by the path as written, then same folder / component / area."""
    by_base: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for fid, p, _ in code:
        by_base[posixpath.basename(p).lower()].append((fid, p.lower()))
    path_of = {fid: p for fid, p, _ in code}
    top = lambda p: "/".join(p.split("/")[:2])
    out: Counter = Counter()

    def pick(src: int, cands: list[tuple[int, str]]) -> int | None:
        if len(cands) == 1:
            return cands[0][0]
        sp = path_of.get(src, "").lower()
        for key in (lambda c: posixpath.dirname(c[1]) == posixpath.dirname(sp),
                    lambda c: assign.get(c[0]) is not None and assign.get(c[0]) == assign.get(src),
                    lambda c: top(c[1]) == top(sp)):
            hit = [c for c in cands if key(c)]
            if len(hit) == 1:
                return hit[0][0]
        return None
    sel = f" AND e.file_id IN ({','.join(str(int(x)) for x in only)})" if only is not None else ""
    for src, dst, label in store.q("SELECT e.file_id, e.dst, e.label FROM edges e WHERE e.kind='include'" + sel):
        cands = by_base.get(dst)
        if not cands or src not in path_of:
            continue
        lab = (label or dst).lower().replace("\\", "/").lstrip("./")
        exact = [c for c in cands if c[1] == lab or c[1].endswith("/" + lab)]
        tgt = pick(src, exact or cands)
        if tgt is not None and tgt != src:
            out[(src, tgt, "include")] += 1
    mods: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for fid, p, lang in code:
        if lang != "python":
            continue
        stem = p[:-3] if p.endswith(".py") else p
        if stem.endswith("/__init__"):
            stem = stem[:-9]
        parts = stem.split("/")
        for i in range(len(parts)):
            mods[".".join(parts[i:])].append((fid, p.lower()))
    for src, dst in store.q("SELECT e.file_id, e.dst FROM edges e JOIN files f ON f.id=e.file_id "
                            "WHERE e.kind='import' AND f.lang='python'" + sel):
        if src not in path_of or not dst:
            continue
        if dst.startswith("."):
            n = len(dst) - len(dst.lstrip("."))
            base = posixpath.dirname(path_of[src])
            for _ in range(n - 1):
                base = posixpath.dirname(base)
            cands = mods.get(".".join(x for x in (base.replace("/", "."), dst.lstrip(".")) if x), [])
        else:
            cands = mods.get(dst, []) or (mods.get(dst.rsplit(".", 1)[0], []) if "." in dst else [])
        tgt = pick(src, cands) if cands else None
        if tgt is not None and tgt != src:
            out[(src, tgt, "import")] += 1
    return out


def refresh(store: Store, root: Path, changed: list[str], added_or_removed: bool, log=print) -> dict | None:
    """After a delta: the model only depends on build files, the file set and #include/import edges, so a plain
    edit just re-resolves the changed files' dependencies; build-file edits and new / removed files rebuild."""
    if added_or_removed or any(buildinfo.is_build_file(p) for p in changed) or store.meta("uml_built_at") is None:
        return build(store, root, log)
    code = [(fid, p, lang) for fid, p, lang in store.q("SELECT id, path, lang FROM files")
            if lang and langs.is_ast_lang(lang)]
    ch = set(changed)
    want = {fid for fid, p, _ in code if p in ch}
    if not want:
        return None
    deps = _file_deps(store, code, dict(store.q("SELECT file_id, comp_id FROM comp_files")), want)
    with store.tx() as db:
        db.executemany("DELETE FROM file_deps WHERE src_id=?", ((f,) for f in want))
        db.executemany("INSERT OR REPLACE INTO file_deps(src_id, dst_id, kind, n) VALUES(?,?,?,?)",
                       ((a, b, k, n) for (a, b, k), n in deps.items()))
    return {"file_deps_refreshed": len(want)}
