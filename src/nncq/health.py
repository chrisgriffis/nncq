"""Graceful degradation: say when, where and how results are incomplete, instead of blocking.

Every search / graph / data response passes through annotate(). When the index is healthy it adds nothing.
Otherwise it adds `index_state`: a short list of what is degraded, where (folders), since when, and what
still works, plus `affects_these_results` when returned paths fall in a degraded area.

States (cheap to compute, cached for a few seconds):
  sync running      a delta (a pull or sync, file changes, catch-up) is being applied: results there may mix revisions
  sync interrupted  the process applying it died: those files stay stale until the next update / server start
  not embedded      chunks with no vector yet: found by keyword and graph, not by meaning
  card-only         chunks with a summary vector but not a full one: semantic ranking is coarser there

divergence() is the just-in-time check per result: the files a result cites are stat'ed against what the index
recorded; changed or missing ones are listed under `divergent` (and re-indexed in the background by the server).
"""
from __future__ import annotations

import json
import time
from collections import Counter

from nncq import sysutil
from nncq.store import Store

_TTL = 3.0
_cache: dict[str, tuple[float, dict | None, list[str]]] = {}
_DETAIL_MAX = 50_000     # above this many pending chunks, report a percentage instead of folders


def _when(ts: float) -> str:
    age = time.time() - ts
    hm = time.strftime("%H:%M", time.localtime(ts))
    return f"since {hm}, {age / 60:.0f} min ago" if age < 86400 else time.strftime("since %Y-%m-%d %H:%M", time.localtime(ts))


def _folders(paths: Counter, depth: int = 4, k: int = 5) -> tuple[list[str], list[str]]:
    c: Counter = Counter()
    for p, n in paths.items():
        c["/".join(p.split("/")[:depth])] += n
    top = c.most_common(k)
    return [f"{f} ({n})" for f, n in top] + ([f"+{len(c) - k} more folders"] if len(c) > k else []), list(c)


def compute(store: Store, embed_tier: int = 2) -> tuple[dict | None, list[str]]:
    """(state or None, degraded folder prefixes). state["semantic_from"] = index of the first item that only
    concerns semantic (vector) search; graph/data tools drop those items."""
    items, prefixes = [], []
    raw = store.meta("sync")
    if raw:
        s = json.loads(raw)
        alive = sysutil.pid_alive(s.get("pid", 0))
        where = ", ".join(s.get("where", []))
        prefixes += [w.rsplit(" (", 1)[0] for w in s.get("where", []) if not w.startswith("+")]
        if alive:
            items.append(f"{s['source']} being applied {_when(s['started'])}: {s['done']}/{s['total']} files done "
                         f"in {where}. Results there may mix old and new revisions until it finishes.")
        else:
            items.append(f"{s['source']} interrupted (started {_when(s['started'])[6:]}), {s['done']}/{s['total']} "
                         f"files applied in {where}. The rest stay stale until the next update or server start.")
    sync_items, sync_prefixes = len(items), list(prefixes)
    tiers = dict(store.q("SELECT tier, COUNT(*) FROM chunks GROUP BY tier"))
    total = sum(tiers.values()) or 1
    t0, t1 = tiers.get(0, 0), tiers.get(1, 0)
    if embed_tier <= 0:
        pass                                          # embeddings off (lite / tier 0): not a degraded state
    elif t0 == total:
        items.append("semantic index not built yet: search is keyword + graph only (exact names and words work, "
                     "'find code that does X' phrasing will be weaker).")
    elif t0:
        if t0 > _DETAIL_MAX:
            items.append(f"semantic index {100 * (total - t0) / total:.0f}% built ({t0} chunks pending): pending "
                         "code is found by keyword and graph, not yet by meaning.")
        else:
            rows = store.q("SELECT f.path, COUNT(*), MIN(f.indexed_at) FROM chunks c JOIN files f ON f.id=c.file_id "
                           "WHERE c.tier=0 GROUP BY c.file_id")
            where, pre = _folders(Counter({p: n for p, n, _ in rows}))
            prefixes += pre
            since = min(r[2] for r in rows)
            items.append(f"{t0} changed chunks in {len(rows)} files not embedded yet ({_when(since)}) in "
                         f"{', '.join(where)}: found by keyword and graph, not yet by meaning.")
    if embed_tier >= 2 and t1 and t1 > 0.02 * total:
        items.append(f"{100 * t1 / total:.0f}% of chunks have only a summary vector so far: semantic ranking "
                     "is coarser for them until the full pass finishes.")
    if not items:
        return None, []
    return {"degraded": items, "checked_at": time.strftime("%H:%M:%S"),
            "_sync": (sync_items, sync_prefixes)}, prefixes


def state(store: Store, embed_tier: int = 2) -> tuple[dict | None, list[str]]:
    key = str(store.path)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1], hit[2]
    st, pre = compute(store, embed_tier)
    _cache[key] = (time.time(), st, pre)
    return st, pre


_PATH_KEYS = ("path", "file")
_DIV_MAX = 40


def result_paths(obj, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """Repo-relative file paths a tool result cites (keys 'path' / 'file', a few levels deep)."""
    out = [] if out is None else out
    if depth > 4 or len(out) >= _DIV_MAX:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _PATH_KEYS and isinstance(v, str) and v and v not in out:
                out.append(v)
            elif isinstance(v, (dict, list)):
                result_paths(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            result_paths(v, out, depth + 1)
    return out


def divergence(store: Store, root, paths: list[str]) -> list[dict]:
    """Files whose on-disk size/mtime no longer match the index (edited, synced or deleted since indexed)."""
    import os
    from pathlib import Path
    out = []
    for p in dict.fromkeys(paths[:_DIV_MAX]):
        row = store.db.execute("SELECT size, mtime_ns FROM files WHERE path=?", (p,)).fetchone()
        if row is None:
            continue
        try:
            stt = os.stat(Path(root) / p)
        except OSError:
            out.append({"path": p, "state": "missing on disk"})
            continue
        if (stt.st_size, stt.st_mtime_ns) != (row[0], row[1]):
            out.append({"path": p, "state": "changed on disk since indexed"})
    return out


def annotate(result: dict, store: Store, paths: list[str] | None = None, embed_tier: int = 2,
             semantic: bool = True) -> dict:
    """Attach index_state to a tool result when (and only when) something relevant is degraded.
    semantic=False (graph / data / lookup tools): only sync state matters, not embedding progress."""
    st, pre = state(store, embed_tier)
    if st is None:
        return result
    n_sync, sync_pre = st["_sync"]
    items = st["degraded"] if semantic else st["degraded"][:n_sync]
    if not items:
        return result
    if not semantic:
        pre = sync_pre
    out = {"degraded": items, "checked_at": st["checked_at"]}
    if paths and pre:
        hit = [p for p in paths if any(p.startswith(x) for x in pre)]
        out["affects_these_results"] = f"{len(hit)} of {len(paths)} results are in a degraded area" if hit else "no"
    result["index_state"] = out
    return result
