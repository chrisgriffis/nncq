"""Hybrid search (BM25 + dense + rerank) and the lookup tools agents use around it.

search_code      BM25 top-N and dense top-N, fused by reciprocal rank, top candidates reranked by the
                 Nemotron cross-encoder. Uses whatever vectors exist; with none it is lexical and says so.
grep_code        ripgrep over the repo (same ignores as indexing)
find_symbol      definitions by name / qualified name / prefix
file_outline     the symbols of one file, with line ranges
find_references  every line mentioning an identifier (FTS narrows to chunks, then exact word match)
read_code        an exact line range from disk (confined to the repo root)

Vectors are fp16 rows in SQLite (vecs). The server keeps them in one numpy matrix per index, loads new rows
incrementally by seq, and reloads fully when many rows are stale.
"""
from __future__ import annotations

import contextlib
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from nncq import config, graph, walk
from nncq.repos import Repo
from nncq.store import Store, split_ident

RRF_K = 60
LEX_N = 100
DENSE_N = 100
_STOP = {"the", "a", "an", "how", "is", "are", "where", "what", "does", "do", "to", "of", "in", "for", "and",
         "or", "with", "on", "by", "this", "that", "be", "it", "find", "code", "which", "when", "who", "get"}
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


# ------------------------------------------------------------------ text fed to the embedder
# No line numbers: a chunk that only moved keeps its vector (store.replace_file reuses it).

def card_text(path: str, kind: str, symbol: str, text: str) -> str:
    head = "\n".join(text.splitlines()[:8])[:500]
    return f"{path}\n{kind} {symbol}\n{head}"


def full_text(path: str, kind: str, symbol: str, text: str) -> str:
    return f"{path}\n{kind} {symbol}\n{text}"


# ------------------------------------------------------------------ dense matrix cache

class Vectors:
    def __init__(self, db: Path, dim: int):
        self.db, self.dim = db, dim
        self.mat = np.zeros((0, dim), dtype=np.float16)
        self.ids = np.zeros(0, dtype=np.int64)
        self.row_of: dict[int, int] = {}
        self.n = 0
        self.last_seq = 0
        self.checked = 0.0
        self.lock = threading.Lock()

    def _grow(self, need: int) -> None:
        if need <= len(self.mat):
            return
        cap = max(need, int(len(self.mat) * 1.5) + 4096)
        m = np.zeros((cap, self.dim), dtype=np.float16)
        m[:self.n] = self.mat[:self.n]
        i = np.zeros(cap, dtype=np.int64)
        i[:self.n] = self.ids[:self.n]
        self.mat, self.ids = m, i

    def refresh(self, store: Store) -> None:
        with self.lock:
            if time.time() - self.checked < 3:
                return
            self.checked = time.time()
            live = store.q("SELECT COUNT(*) FROM vecs")[0][0]
            if self.n and live < 0.8 * self.n:
                self.__init__(self.db, self.dim)
                self.checked = time.time()
            while True:
                rows = store.q("SELECT seq, chunk_id, v FROM vecs WHERE seq>? ORDER BY seq LIMIT 50000",
                               (self.last_seq,))
                if not rows:
                    break
                self._grow(self.n + len(rows))
                for seq, cid, v in rows:
                    a = np.frombuffer(v, dtype=np.float16)
                    if a.shape[0] != self.dim:
                        continue
                    r = self.row_of.get(cid)
                    if r is None:
                        r = self.n
                        self.row_of[cid] = r
                        self.ids[r] = cid
                        self.n += 1
                    self.mat[r] = a
                self.last_seq = rows[-1][0]

    def top(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        with self.lock:
            if not self.n:
                return []
            q = q.astype(np.float32)
            scores = np.empty(self.n, dtype=np.float32)
            for i in range(0, self.n, 65536):
                j = min(self.n, i + 65536)
                scores[i:j] = self.mat[i:j].astype(np.float32) @ q
            k = min(k, self.n)
            idx = np.argpartition(-scores, k - 1)[:k]
            idx = idx[np.argsort(-scores[idx])]
            return [(int(self.ids[i]), float(scores[i])) for i in idx]


_vectors: dict[str, Vectors] = {}
_models: dict[str, object] = {}
_mlock = threading.Lock()


def vectors(store: Store) -> Vectors:
    key = str(store.path)
    with _mlock:
        v = _vectors.get(key)
        if v is None:
            v = _vectors[key] = Vectors(store.path, config.settings().embed_dim)
    v.refresh(store)
    return v


def _model(name: str):
    from nncq import models
    with _mlock:
        if name not in _models:
            _models[name] = models.Embedder() if name == "embed" else models.Reranker()
        return _models[name]


def models_ready() -> bool:
    from nncq import models
    return not models.models_present()


@contextlib.contextmanager
def _gpu_turn():
    """Mark a query's GPU use so the background worker holds its next batch (see worker._wait_for_queries)."""
    def touch():
        try:
            config.QUERY_BUSY_FILE.touch()
        except OSError:
            pass
    touch()
    try:
        yield
    finally:
        touch()


# ------------------------------------------------------------------ lexical

def fts_query(q: str) -> str:
    toks = [t for t in _TOKEN.findall(q)]
    keep = [t for t in toks if t.lower() not in _STOP] or toks
    terms: list[str] = []
    for t in keep:
        terms.append(t.lower())
        terms += split_ident(t)
    seen, out = set(), []
    for t in terms:
        if t not in seen and len(t) > 1:
            seen.add(t)
            out.append(f'"{t}"')
    return " OR ".join(out)


def lexical(store: Store, q: str, n: int) -> list[tuple[int, float]]:
    m = fts_query(q)
    if not m:
        return []
    try:
        return store.q("SELECT rowid, bm25(fts, 3.0, 6.0, 1.0) AS s FROM fts WHERE fts MATCH ? ORDER BY s LIMIT ?",
                       (m, n))
    except Exception:
        return []


# ------------------------------------------------------------------ search

def _filter_sql(path: str, lang: str) -> tuple[str, list]:
    where, args = [], []
    if path:
        p = path.replace("\\", "/").strip()
        where.append("f.path LIKE ?" if ("*" in p or "?" in p) else "f.path LIKE ?")
        args.append(p.replace("*", "%").replace("?", "_") if ("*" in p or "?" in p) else f"%{p}%")
    if lang:
        where.append("f.lang IN (%s)" % ",".join("?" * len(lang.split(","))))
        args += [x.strip().lower() for x in lang.split(",")]
    return (" AND " + " AND ".join(where)) if where else "", args


def search(store: Store, query: str, k: int = 10, path: str = "", lang: str = "", max_chars: int = 1500,
           rerank: bool | None = None) -> dict:
    s = config.settings()
    t0 = time.time()
    wide = bool(path or lang)
    lex = lexical(store, query, LEX_N * (5 if wide else 1))
    dense: list[tuple[int, float]] = []
    notes = []
    vec = vectors(store)
    if vec.n and models_ready():
        try:
            with _gpu_turn():
                qv = _model("embed").encode_query(query)
            dense = vec.top(qv, DENSE_N * (10 if wide else 1))
        except Exception as e:                       # GPU busy / OOM: keep going lexically
            notes.append(f"semantic search unavailable ({type(e).__name__}); keyword results only")
    elif not vec.n:
        notes.append("no vectors yet: keyword results only")
    fused: dict[int, float] = {}
    for lst in (lex, dense):
        for r, (cid, _) in enumerate(lst):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
    if not fused:
        return {"query": query, "results": [], "mode": "none", "note": "no matches"}
    ids = sorted(fused, key=lambda c: -fused[c])[:max(s.rerank_candidates, k) * (4 if wide else 1)]
    fsql, fargs = _filter_sql(path, lang)
    ph = ",".join("?" * len(ids))
    rows = {r[0]: r for r in store.q(
        f"SELECT c.id, f.path, c.start_line, c.end_line, c.symbol, c.kind, c.parent, c.text, f.lang "
        f"FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id IN ({ph}){fsql}", (*ids, *fargs))}
    cands = [rows[c] for c in ids if c in rows][:max(s.rerank_candidates, k)]
    mode = "hybrid" if dense else "lexical"
    do_rr = (s.rerank if rerank is None else rerank) and len(cands) > 1 and models_ready()
    scores = {r[0]: fused[r[0]] for r in cands}
    if do_rr:
        try:
            with _gpu_turn():
                sc = _model("rerank").score(query, [full_text(r[1], r[5], r[4], r[7]) for r in cands])
            scores = {r[0]: x for r, x in zip(cands, sc)}
            mode += "+rerank"
        except Exception as e:
            notes.append(f"reranker unavailable ({type(e).__name__}); fused order used")
    cands.sort(key=lambda r: -scores[r[0]])
    res = []
    for r in cands[:k]:
        text = r[7]
        res.append({"path": r[1], "lines": f"{r[2]}-{r[3]}", "symbol": r[4] or "(file level)", "kind": r[5],
                    "parent": r[6] or None, "score": round(scores[r[0]], 4),
                    "code": text if len(text) <= max_chars else text[:max_chars] + f"\n... [{len(text) - max_chars} more chars: read_code]"})
    out = {"query": query, "mode": mode, "results": res, "ms": int((time.time() - t0) * 1000)}
    if notes:
        out["note"] = "; ".join(notes)
    return out


# ------------------------------------------------------------------ lookups

def grep_code(repo: Repo, pattern: str, glob: str = "", fixed: bool = False, ignore_case: bool = False,
              max_results: int = 100) -> dict:
    s = config.settings()
    cmd = [walk.rg_exe(), "-n", "--no-heading", "--max-columns", "300", "--max-columns-preview",
           *walk.ignore_args(repo.root, s.ignore)]
    if fixed:
        cmd.append("-F")
    if ignore_case:
        cmd.append("-i")
    for g in filter(None, (x.strip() for x in glob.split(","))):
        cmd += ["-g", g]
    targets = [d for d in repo.dirs if (repo.root / d).is_dir()] if repo.dirs else walk.top_dirs(repo.root)
    cmd += ["-e", pattern, "--", *targets]
    out, n, truncated = [], 0, False
    proc = subprocess.Popen(cmd, cwd=repo.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL)
    t0 = time.time()
    try:
        for line in proc.stdout:
            n += 1
            if n > max_results:
                truncated = True
                break
            out.append(line.decode("utf-8", "replace").rstrip().replace("\\", "/", 1))
            if time.time() - t0 > 30:
                truncated = True
                break
    finally:
        proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.wait()
    res = {"pattern": pattern, "matches": out, "truncated": truncated}
    if err and not out:
        res["error"] = err[:500]
    return res


def find_symbol(store: Store, name: str, kind: str = "", path: str = "", limit: int = 30) -> dict:
    rows = graph.find_defs(store, name, path)
    how = "exact"
    if not rows:
        how = "prefix"
        rows = [graph.Def(*r) for r in store.q(
            "SELECT s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature, f.path, s.file_id "
            "FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name LIKE ? ORDER BY length(s.name) LIMIT 300",
            (graph.short(name) + "%",))]
    if not rows:
        how = "substring"
        rows = [graph.Def(*r) for r in store.q(
            "SELECT s.id, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature, f.path, s.file_id "
            "FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name LIKE ? ORDER BY length(s.name) LIMIT 300",
            ("%" + graph.short(name) + "%",))]
    if kind:
        ks = {x.strip() for x in kind.split(",")}
        rows = [r for r in rows if r.kind in ks or ("type" in ks and graph.is_type(r.kind))]
    return {"name": name, "match": how, "total": len(rows),
            "definitions": [r.line() + (f"\n    {r.signature}" if r.signature else "") for r in rows[:limit]]}


def file_outline(store: Store, path: str) -> dict:
    f = graph.resolve_file(store, path)
    if f is None:
        return {"error": f"no indexed file matches {path!r}"}
    if isinstance(f, list):
        return {"error": f"{path!r} is ambiguous", "candidates": f}
    fid, fpath = f
    rows = store.q("SELECT kind, qualname, parent, start_line, end_line, signature FROM symbols WHERE file_id=? "
                   "ORDER BY start_line", (fid,))
    lang, err = store.q("SELECT lang, error FROM files WHERE id=?", (fid,))[0]
    depth: dict[str, int] = {}
    out = []
    for kind, q, parent, s, e, sig in rows:
        d = depth.get(parent, -1) + 1 if parent else 0
        depth[q] = d
        out.append(f"{'  ' * d}{s}-{e} {kind} {q}" + (f"  | {sig}" if sig and sig != q else ""))
    res = {"path": fpath, "lang": lang, "symbols": out}
    if err:
        res["note"] = err
    return res


def find_references(store: Store, name: str, path: str = "", limit: int = 80) -> dict:
    ident = graph.short(name)
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])")
    fsql, fargs = _filter_sql(path, "")
    rows = store.q(f"SELECT f.path, c.start_line, c.text FROM fts JOIN chunks c ON c.id=fts.rowid "
                   f"JOIN files f ON f.id=c.file_id WHERE fts MATCH ?{fsql} LIMIT 5000",
                   (f'"{ident.lower()}"', *fargs))
    hits, seen = [], set()
    for p, start, text in rows:
        for i, line in enumerate(text.splitlines()):
            if word.search(line):
                key = (p, start + i)
                if key not in seen:
                    seen.add(key)
                    hits.append((p, start + i, line.strip()[:200]))
    hits.sort()
    defs = {(d.path, d.start) for d in graph.find_defs(store, ident)}
    out = [f"{p}:{n}: {t}" + ("   <- definition" if (p, n) in defs else "") for p, n, t in hits[:limit]]
    files = len({h[0] for h in hits})
    derived = len(graph.derived_of(store, ident, 1000))
    calls = store.q("SELECT COUNT(*) FROM edges WHERE dst=? AND kind='call'", (ident,))[0][0]
    return {"name": ident, "lines_total": len(hits), "files": files, "shown": len(out),
            "call_sites": calls, "subclasses": derived, "references": out,
            "note": "exact word matches in indexed code (case-sensitive); calls/subclasses counted by name"}


def read_code(repo: Repo, path: str, start: int = 1, end: int = 0, max_lines: int = 400) -> dict:
    p = path.replace("\\", "/").strip()
    root = repo.root.resolve()
    fp = (root / p).resolve()
    if root not in fp.parents and fp != root:
        return {"error": "path is outside the repository"}
    if not fp.is_file():
        st = Store(repo.db, readonly=True, cache_mb=8)
        try:
            f = graph.resolve_file(st, p)
        finally:
            st.close()
        if isinstance(f, tuple):
            fp, p = (root / f[1]).resolve(), f[1]
        else:
            return {"error": f"no such file: {p}", "candidates": f or []}
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    end = len(lines) if end <= 0 else min(end, len(lines))
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    return {"path": p, "lines": f"{start}-{end}", "total_lines": len(lines), "code": body}
