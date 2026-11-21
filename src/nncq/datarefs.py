"""engine .xdata data cross-reference: which assets instantiate which types, and which assets reference which.

No chunks, no vectors: ~35 s ripgrep pass over a full workspace (392k files / 30 GB), then deltas.
Answers: what data uses this code (type) / what code does this asset use / who references this asset /
most referenced assets. Static (what the files say), name-based.
"""
from __future__ import annotations

import bisect
import collections
import os
import re
import subprocess
import time
from pathlib import Path

from nncq import walk
from nncq.store import Store, rkey

RG_PAT = r'<instance [^>]*?type="[^"]+"|ref="[^"]*/[0-9a-fA-F-]{36}"'
_PY_PAT = re.compile(rb'<instance [^>]*?type="([^"]+)"|ref="([^"]*)/[0-9a-fA-F-]{36}"')
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


# ------------------------------------------------------------------ scanning

def _type_short(t: str) -> str:
    return t.rsplit(".", 1)[-1]


class _Writer:
    def __init__(self, store: Store):
        self.s = store
        self.types = dict(store.q("SELECT name, id FROM data_types"))
        self.assets = dict(store.q("SELECT key, id FROM data_assets"))
        self.dirty_types: set[int] = set()     # for incremental stats after a delta
        self.dirty_assets: set[int] = set()
        self.dirty_files: set[int] = set()

    def _mark_old(self, fid: int) -> None:
        db = self.s.db
        self.dirty_types.update(r[0] for r in db.execute("SELECT type_id FROM data_inst WHERE file_id=?", (fid,)))
        self.dirty_assets.update(r[0] for r in db.execute("SELECT asset_id FROM data_refs WHERE file_id=?", (fid,)))

    def type_id(self, name: str) -> int:
        i = self.types.get(name)
        if i is None:
            i = self.s.db.execute("INSERT INTO data_types(name, short) VALUES(?,?)", (name, _type_short(name))).lastrowid
            self.types[name] = i
        return i

    def asset_id(self, key: str) -> int:
        i = self.assets.get(key)
        if i is None:
            i = self.s.db.execute("INSERT INTO data_assets(key) VALUES(?)", (key,)).lastrowid
            self.assets[key] = i
        return i

    def put(self, path: str, size: int, mtime_ns: int, types: collections.Counter, refs: set[str]) -> None:
        db = self.s.db
        row = db.execute("SELECT id FROM data_files WHERE path=?", (path,)).fetchone()
        if row:
            fid = row[0]
            self._mark_old(fid)
            db.execute("DELETE FROM data_inst WHERE file_id=?", (fid,))
            db.execute("DELETE FROM data_refs WHERE file_id=?", (fid,))
            db.execute("UPDATE data_files SET size=?, mtime_ns=?, instances=? WHERE id=?",
                       (size, mtime_ns, sum(types.values()), fid))
        else:
            fid = db.execute("INSERT INTO data_files(path, rkey, size, mtime_ns, instances) VALUES(?,?,?,?,?)",
                             (path, rkey(path), size, mtime_ns, sum(types.values()))).lastrowid
        tids = {t: self.type_id(t) for t in types}
        aids = [self.asset_id(k) for k in refs]
        db.executemany("INSERT INTO data_inst(type_id, file_id, n) VALUES(?,?,?)",
                       ((tids[t], fid, n) for t, n in types.items()))
        db.executemany("INSERT OR IGNORE INTO data_refs(asset_id, file_id) VALUES(?,?)", ((a, fid) for a in aids))
        self.dirty_types.update(tids.values())
        self.dirty_assets.update(aids)
        self.dirty_files.add(fid)

    def delete(self, path: str) -> None:
        row = self.s.db.execute("SELECT id FROM data_files WHERE path=?", (path,)).fetchone()
        if row:
            self._mark_old(row[0])
            for t in ("data_inst", "data_refs"):
                self.s.db.execute(f"DELETE FROM {t} WHERE file_id=?", (row[0],))
            self.s.db.execute("DELETE FROM data_files WHERE id=?", (row[0],))


def scan_full(store: Store, root: Path, extra_ignore: str = "", log=print) -> dict:
    """Rebuild the whole cross-reference with one ripgrep pass."""
    t0 = time.time()
    cmd = [walk.rg_exe(), *walk.ignore_args(root, extra_ignore), "-o", "--null", "--no-line-number",
           "--with-filename", "-g", "*.xdata", RG_PAT, *walk.top_dirs(root)]
    proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=1 << 20)
    nfiles = 0
    with store.tx() as db:
        for t in ("data_inst", "data_refs", "data_files", "data_assets", "data_types"):
            db.execute(f"DELETE FROM {t}")
        w = _Writer(store)
        cur, types, refs = None, collections.Counter(), set()

        def flush():
            nonlocal nfiles
            if cur is None:
                return
            try:
                st = os.stat(root / cur)
            except OSError:
                return
            w.put(cur, st.st_size, st.st_mtime_ns, types, refs)
            nfiles += 1
            if nfiles % 50000 == 0:
                log(f"  data: {nfiles} files ({time.time() - t0:.0f}s)")

        for line in proc.stdout:
            path, _, m = line.rstrip(b"\r\n").partition(b"\0")
            p = path.decode("utf-8", "replace").replace("\\", "/")
            if p != cur:
                flush()
                cur, types, refs = p, collections.Counter(), set()
            if m.startswith(b"<instance"):
                i = m.rfind(b'type="')
                types[m[i + 6:m.rfind(b'"')].decode("utf-8", "replace")] += 1
            else:
                refs.add(m[5:m.rfind(b"/")].decode("utf-8", "replace").lower())
        flush()
        proc.wait()
        _refresh_stats(store)
        store.set_meta("data_scanned_at", time.time())
    return {"files": nfiles, "seconds": round(time.time() - t0, 1)}


DELTA_BATCH = 2000   # .xdata files per write transaction, so readers and other writers are never blocked long


def scan_paths(store: Store, root: Path, paths: list[str], log=print) -> int:
    """Delta: (re)scan the given relative .xdata paths; missing ones are removed. Cost is proportional to
    len(paths) only (stats are updated incrementally), so even a huge sync never triggers a full rebuild."""
    paths = [p.replace("\\", "/") for p in paths if p.lower().endswith(".xdata")]
    t0 = time.time()
    for i in range(0, len(paths), DELTA_BATCH):
        with store.tx():
            w = _Writer(store)
            for p in paths[i:i + DELTA_BATCH]:
                fp = root / p
                try:
                    st = fp.stat()
                    data = fp.read_bytes()
                except OSError:
                    w.delete(p)
                    continue
                types, refs = collections.Counter(), set()
                for t, r in _PY_PAT.findall(data):
                    if t:
                        types[t.decode("utf-8", "replace")] += 1
                    else:
                        refs.add(r.decode("utf-8", "replace").lower())
                w.put(p, st.st_size, st.st_mtime_ns, types, refs)
            _refresh_dirty(store, w)
        if len(paths) > DELTA_BATCH:
            log(f"  data: {min(i + DELTA_BATCH, len(paths))}/{len(paths)} files ({time.time() - t0:.0f}s)")
    return len(paths)


def update(store: Store, root: Path, extra_ignore: str = "", log=print) -> dict:
    """Stat-compare every .xdata against the table; rescan changed/new, drop removed.
    Only the very first build does a full scan; after that, any amount of change is applied as a delta."""
    if store.meta("data_scanned_at") is None:
        return scan_full(store, root, extra_ignore, log)
    t0 = time.time()
    on_disk = walk.stat_many(root, walk.list_files(root, ["*.xdata"], extra_ignore))
    known = {p: (s, m) for p, s, m in store.q("SELECT path, size, mtime_ns FROM data_files")}
    changed = [p for p, st in on_disk.items() if known.get(p) != st]
    removed = [p for p in known if p not in on_disk]
    scan_paths(store, root, changed + removed, log)
    return {"changed": len(changed), "removed": len(removed), "seconds": round(time.time() - t0, 1)}


def _refresh_dirty(store: Store, w: _Writer) -> None:
    """Recompute stats only for the types, assets and files a delta touched."""
    db = store.db
    if w.dirty_types:
        ids = list(w.dirty_types)
        db.executemany("UPDATE data_types SET (files, instances) = (SELECT COUNT(*), COALESCE(SUM(n), 0) "
                       "FROM data_inst WHERE type_id=?) WHERE id=?", ((i, i) for i in ids))
        db.executemany("DELETE FROM data_types WHERE id=? AND files=0", ((i,) for i in ids))
    files = set(w.dirty_files)
    for aid in w.dirty_assets:
        n = db.execute("SELECT COUNT(*) FROM data_refs WHERE asset_id=?", (aid,)).fetchone()[0]
        row = db.execute("SELECT key FROM data_assets WHERE id=?", (aid,)).fetchone()
        if not row:
            continue
        if n:
            db.execute("UPDATE data_assets SET inbound=? WHERE id=?", (n, aid))
        else:
            db.execute("DELETE FROM data_assets WHERE id=?", (aid,))
        rk = row[0][::-1]
        files.update(r[0] for r in db.execute("SELECT id FROM data_files WHERE rkey=? OR (rkey>=? AND rkey<?)",
                                              (rk, rk + "/", rk + "0")))
    for fid in files:
        row = db.execute("SELECT path FROM data_files WHERE id=?", (fid,)).fetchone()
        if not row:
            continue
        keys = _keys_for_path(row[0])
        ph = ",".join("?" * len(keys))
        n = db.execute(f"SELECT COALESCE(SUM(inbound), 0) FROM data_assets WHERE key IN ({ph})", keys).fetchone()[0]
        db.execute("UPDATE data_files SET inbound=? WHERE id=?", (n, fid))


def _refresh_stats(store: Store) -> None:
    """Full recompute (used by scan_full only)."""
    db = store.db
    db.execute("UPDATE data_types SET files=0, instances=0")
    db.execute("UPDATE data_types SET (files, instances) = (SELECT COUNT(*), SUM(n) FROM data_inst WHERE type_id=data_types.id) "
               "WHERE id IN (SELECT DISTINCT type_id FROM data_inst)")
    db.execute("DELETE FROM data_types WHERE files=0")
    db.execute("UPDATE data_assets SET inbound=0")
    db.executemany("UPDATE data_assets SET inbound=? WHERE id=?",
                   ((n, a) for a, n in db.execute("SELECT asset_id, COUNT(*) FROM data_refs GROUP BY asset_id").fetchall()))
    db.execute("DELETE FROM data_assets WHERE inbound=0")
    # file inbound = inbound of the asset key(s) the file path ends with (content layers: same key, several files)
    files = sorted(db.execute("SELECT rkey, id FROM data_files").fetchall())
    keys = [f[0] for f in files]
    inbound: dict[int, int] = collections.Counter()
    for key, n in db.execute("SELECT key, inbound FROM data_assets"):
        rk = key[::-1]
        i = bisect.bisect_left(keys, rk)
        while i < len(keys) and keys[i].startswith(rk):
            if len(keys[i]) == len(rk) or keys[i][len(rk)] == "/":
                inbound[files[i][1]] += n
            i += 1
    db.execute("UPDATE data_files SET inbound=0")
    db.executemany("UPDATE data_files SET inbound=? WHERE id=?", ((n, f) for f, n in inbound.items()))


# ------------------------------------------------------------------ lookups

def resolve(store: Store, q: str, limit: int = 20) -> list[tuple[int, str, int]]:
    """Asset path / name / reference key -> [(file_id, path, inbound)]: suffix match, then substring."""
    q = q.strip().replace("\\", "/").strip("/")
    rk = rkey(q)
    rows = store.q("SELECT id, path, inbound FROM data_files WHERE rkey=? OR (rkey>=? AND rkey<?) "
                   "ORDER BY inbound DESC LIMIT ?", (rk, rk + "/", rk + "0", limit))
    if not rows:
        rows = store.q("SELECT id, path, inbound FROM data_files WHERE path LIKE ? ORDER BY inbound DESC LIMIT ?",
                       (f"%{q.removesuffix('.xdata')}%", limit))
    return rows


def _keys_for_path(path: str) -> list[str]:
    parts = path.lower().removesuffix(".xdata").split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def asset_info(store: Store, q: str, k: int = 25) -> dict:
    """Types instantiated, outgoing references, incoming referrers for one asset."""
    rows = resolve(store, q, 10)
    if not rows:
        return {"error": f"no .xdata asset matches {q!r}"}
    fid, path, inbound = rows[0]
    types = store.q("SELECT t.name, i.n FROM data_inst i JOIN data_types t ON t.id=i.type_id WHERE i.file_id=? "
                    "ORDER BY i.n DESC", (fid,))
    out = [r[0] for r in store.q("SELECT a.key FROM data_refs r JOIN data_assets a ON a.id=r.asset_id "
                                 "WHERE r.file_id=? ORDER BY a.key LIMIT ?", (fid, k))]
    keys = _keys_for_path(path)
    ph = ",".join("?" * len(keys))
    referrers = [r[0] for r in store.q(
        f"SELECT DISTINCT f.path FROM data_assets a JOIN data_refs r ON r.asset_id=a.id JOIN data_files f ON f.id=r.file_id "
        f"WHERE a.key IN ({ph}) ORDER BY f.inbound DESC LIMIT ?", (*keys, k))]
    return {"path": path, "inbound_refs": inbound, "types": [{"type": t, "count": n} for t, n in types],
            "references": out, "referenced_by": referrers,
            "other_matches": [r[1] for r in rows[1:]]}


def _type_rows(store: Store, where: str, args) -> list[tuple[int, str, str, int, int]]:
    return store.q(f"SELECT id, name, short, files, instances FROM data_types WHERE {where}", args)


def data_for_code(store: Store, name: str, k: int = 15) -> dict:
    """What data uses this code: data types named by / used in the code symbol, their subclasses, and top assets.

    Matching (name-based): the data type itself (Entity.LayerData / LayerData), the runtime convention
    Foo -> FooData, and any data type name that appears in the symbol's code.
    """
    name = name.strip()
    base = re.split(r"::|\.", name)[-1]
    found: dict[int, dict] = {}

    def add(rows, via):
        for tid, tname, short, files, inst in rows:
            found.setdefault(tid, {"type": tname, "via": via, "files": files, "instances": inst})

    add(_type_rows(store, "name=? COLLATE NOCASE OR short=? COLLATE NOCASE OR short=? COLLATE NOCASE",
                   (name, base, base + "Data")), "name")
    syms = store.q("SELECT s.file_id, s.start_line, s.end_line, s.qualname, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
                   "WHERE (s.name=? COLLATE NOCASE OR s.qualname=? COLLATE NOCASE) AND f.lang!='xdata' LIMIT 10", (name, name))
    idents: set[str] = set()
    for fid, s, e, _, _ in syms:
        for (txt,) in store.q("SELECT text FROM chunks WHERE file_id=? AND end_line>=? AND start_line<=? LIMIT 20",
                              (fid, s, e)):
            idents.update(_IDENT.findall(txt))
    idents = list(idents)
    for i in range(0, len(idents), 500):
        part = idents[i:i + 500]
        add(_type_rows(store, f"short IN ({','.join('?' * len(part))})", part), "used in code")
    # subclasses declared in .ddf: `class Foo : Bar` -> data typed Foo is also Bar data
    frontier = {f["type"] for f in found.values()}
    seen = set(frontier)
    for _ in range(6):
        nxt = set()
        for t in frontier:
            for (src,) in store.q("SELECT DISTINCT src FROM edges WHERE kind='inherits' AND (dst=? OR dst=?)",
                                  (t, _type_short(t))):
                if src not in seen:
                    seen.add(src)
                    nxt.add(src)
        for t in nxt:
            add(_type_rows(store, "name=? OR (short=? AND name NOT LIKE '%.%')", (t, t)), "subclass")
        frontier = nxt
        if not frontier:
            break
    results = []
    via_rank = {"name": 0, "subclass": 1, "used in code": 2}
    for tid, info in sorted(found.items(), key=lambda kv: (via_rank[kv[1]["via"]], -kv[1]["files"]))[:40]:
        info["top_assets"] = [{"path": p, "instances": n, "inbound_refs": ib} for p, n, ib in store.q(
            "SELECT f.path, i.n, f.inbound FROM data_inst i JOIN data_files f ON f.id=i.file_id WHERE i.type_id=? "
            "ORDER BY f.inbound DESC, i.n DESC LIMIT ?", (tid, k))]
        results.append(info)
    return {"query": name, "code_symbols": [f"{s[4]}:{s[1]} {s[3]}" for s in syms], "data_types": results}


def code_for_data(store: Store, q: str, k: int = 8) -> dict:
    """What code does this asset (or data type) use: per type, its .ddf definition, the runtime class
    (FooData -> Foo), and code that mentions the type."""
    rows = resolve(store, q, 5) if not re.fullmatch(r"[\w.]+", q) or "/" in q else []
    if rows:
        fid, path, _ = rows[0]
        types = store.q("SELECT t.name, t.short, i.n FROM data_inst i JOIN data_types t ON t.id=i.type_id "
                        "WHERE i.file_id=? ORDER BY i.n DESC LIMIT 30", (fid,))
    else:
        path = ""
        types = [(n, s, 0) for _, n, s, _, _ in _type_rows(store, "name=? COLLATE NOCASE OR short=? COLLATE NOCASE", (q, q))]
        if not types:
            return {"error": f"no asset or data type matches {q!r}"}
    out = []
    code_paths: list[str] = []
    for tname, short, n in types:
        names = [short] + ([short[:-4]] if short.endswith("Data") and len(short) > 4 else [])
        ph = ",".join("?" * len(names))
        defs = store.q(f"SELECT f.path, s.start_line, s.kind, s.qualname FROM symbols s JOIN files f ON f.id=s.file_id "
                       f"WHERE (s.name IN ({ph}) OR s.qualname=? COLLATE NOCASE) AND f.lang!='xdata' "
                       f"AND s.kind NOT IN ('function','method') ORDER BY (s.qualname=?) DESC LIMIT ?",
                       (*names, tname, tname, k))
        users = store.q("SELECT DISTINCT f.path FROM fts JOIN chunks c ON c.id=fts.rowid JOIN files f ON f.id=c.file_id "
                        "WHERE fts MATCH ? AND f.lang!='xdata' LIMIT ?", (f'body:"{short}"', k * 3))
        code_paths += [d[0] for d in defs]
        out.append({"type": tname, "instances": n,
                    "definitions": [f"{p}:{ln} [{kd}] {qn}" for p, ln, kd, qn in defs],
                    "mentioned_in": [u[0] for u in users][:k]})
    return {"asset": path, "types": out}


# ------------------------------------------------------------------ most referenced

def top_assets(store: Store, k: int = 50, under: str = "", group_depth: int = 0) -> list[dict]:
    """Most-referenced assets (static inbound reference count); group_depth rolls up to folders."""
    args: list = []
    where = "inbound>0"
    if under:
        where += " AND path LIKE ?"
        args.append(under.replace("\\", "/").rstrip("/") + "/%")
    if not group_depth:
        return [{"rank": i + 1, "path": p, "inbound_refs": ib, "instances": n, "kb": sz // 1024}
                for i, (p, ib, n, sz) in enumerate(store.q(
                    f"SELECT path, inbound, instances, size FROM data_files WHERE {where} "
                    f"ORDER BY inbound DESC LIMIT ?", (*args, k)))]
    agg: dict[str, list] = {}
    for p, ib in store.q(f"SELECT path, inbound FROM data_files WHERE {where}", args):
        key = "/".join(p.split("/")[:group_depth]) + "/"
        g = agg.setdefault(key, [0, 0])
        g[0] += ib
        g[1] += 1
    top = sorted(agg.items(), key=lambda kv: -kv[1][0])[:k]
    return [{"rank": i + 1, "path": key, "inbound_refs": ib, "assets": n} for i, (key, (ib, n)) in enumerate(top)]
