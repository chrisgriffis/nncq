"""What every tool answer passes through on its way out (agents over MCP and the dashboard alike).

cache   Identical read-only calls (tool + arguments) are answered from memory for NNCQ_CACHE_MINUTES, until the
        index changes (the watcher, a delta or a build bumps the generation). ask is keyed without k: a cached
        answer with more hits serves any smaller k. Embedding progress does not invalidate: the TTL bounds that.
budget  MCP clients spill or cut long tool results (Copilot CLI writes anything above ~20 KB to a temp file),
        so answers above NNCQ_MAX_RESPONSE_KB are trimmed at their largest list or multi-line text field.
        The rest is kept here under a cursor and `paged` says how to get it: more(cursor=...). Sizes are
        measured as the client receives them (JSON, indent 2).
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict

from nncq import config

CACHED = {"ask", "expand", "search_code", "find_symbol", "file_outline", "find_references", "symbol_context",
          "calls", "called_by", "call_tree", "call_trace", "impact", "type_hierarchy", "file_context", "hotspots",
          "data_for_code", "code_for_data", "data_file", "hot_data"}
CACHED |= {"uml_protocol", "uml_find", "uml_view", "uml_views"}       # uml writes bump the cache
MAX_ENTRIES = 256
MAX_CURSORS = 200

_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple[float, int, dict]] = OrderedDict()
_cursors: OrderedDict[str, dict] = OrderedDict()
_gen = [0]
stats = {"hits": 0, "misses": 0, "paged": 0}


def bump() -> None:
    """The index changed: forget cached answers (cursors stay valid, they hold what was already answered)."""
    with _lock:
        _gen[0] += 1
        _cache.clear()


def _ttl() -> float:
    return config.settings().cache_minutes * 60


def key(tool: str, args: dict) -> tuple | None:
    if tool not in CACHED or _ttl() <= 0:
        return None
    a = {k: v for k, v in args.items() if not (tool == "ask" and k == "k")}
    return tool, json.dumps(a, sort_keys=True, default=str)


def get(k: tuple | None, args: dict) -> dict | None:
    if k is None:
        return None
    with _lock:
        e = _cache.get(k)
        if e is None or time.time() - e[0] > _ttl() or e[1] != _gen[0]:
            stats["misses"] += 1
            return None
        out = e[2]
        if k[0] == "ask":
            want = int(args.get("k") or 8)
            if out.get("_k", 0) < want:
                stats["misses"] += 1
                return None
            out = _slice_ask(out, want)
        _cache.move_to_end(k)
        stats["hits"] += 1
        return out


def put(k: tuple | None, args: dict, out) -> None:
    if k is None or not isinstance(out, dict) or "error" in out:
        return
    if k[0] == "ask":
        out = {**out, "_k": int(args.get("k") or 8)}
    with _lock:
        _cache[k] = (time.time(), _gen[0], out)
        _cache.move_to_end(k)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)


def _slice_ask(out: dict, k: int) -> dict:
    hits = out.get("hits", [])
    if len(hits) <= k:
        return out
    keep = {h["ref"] for h in hits[:k]}
    o = {**out, "hits": hits[:k]}
    if "links" in o:
        o["links"] = [x for x in o["links"]
                      if all(r in keep for r in (w for w in x.replace(",", " ").split() if w[:1] == "c" and w[1:].isdigit()))]
    if isinstance(o.get("portfolio"), dict):
        p = dict(o["portfolio"])
        p["shown"] = f"{k} of " + str(p.get("shown", "")).split(" of ", 1)[-1]
        p["areas"] = [a for a in (p.get("areas") or []) if any(r in a for r in keep)]
        o["portfolio"] = p
    return o


def public(out):
    return {k: v for k, v in out.items() if k != "_k"} if isinstance(out, dict) and "_k" in out else out


# ------------------------------------------------------------------ budget

def size(o) -> int:
    return len(json.dumps(o, ensure_ascii=False, indent=2, default=str).encode())


def _cursor(tool: str, field: str, value, offset: int, base: int) -> str:
    """value[offset:] is what is left; base = how many came before it in the original answer."""
    c = secrets.token_hex(4)
    with _lock:
        _cursors[c] = {"tool": tool, "field": field, "value": value, "offset": offset, "base": base}
        while len(_cursors) > MAX_CURSORS:
            _cursors.popitem(last=False)
    stats["paged"] += 1
    return c


def _lines(v) -> list[str] | None:
    return v.split("\n") if isinstance(v, str) and v.count("\n") >= 2 else None


def fit(out, tool: str, budget: int | None = None, offsets: dict | None = None):
    """out trimmed to the byte budget; trimmed fields are listed in out['paged'] with a more() cursor."""
    if budget is None:
        budget = config.settings().max_response_kb * 1024
    if budget <= 0 or not isinstance(out, dict) or size(out) <= budget:
        return out
    out = dict(out)
    paged = dict(out.pop("paged", {}) or {})
    for _ in range(4):
        cands = [(size(v), f) for f, v in out.items()
                 if (isinstance(v, list) and len(v) > 1) or _lines(v) or (isinstance(v, list) and v
                                                                         and isinstance(v[0], dict))]
        if not cands:
            break
        _, f = max(cands)
        v = out[f]
        base = (offsets or {}).get(f, 0)
        parts = v if isinstance(v, list) else _lines(v)
        join = (lambda xs: xs) if isinstance(v, list) else (lambda xs: "\n".join(xs))
        out[f] = join([])
        room = budget - size({**out, "paged": {**paged, f: "x" * 90}})
        n, used = 0, 0
        for p in parts:
            s = size(p) + 4
            if used + s > room and n:
                break
            used += s
            n += 1
        if n == 1 and isinstance(v, list) and isinstance(parts[0], dict) and size(parts[0]) > room:
            parts = [fit(parts[0], tool, max(room, 2048))]        # one huge element: page inside it
        out[f] = join(parts[:n])
        if n < len(v if isinstance(v, list) else parts):
            total = len(v) if isinstance(v, list) else len(_lines(v))
            unit = "" if isinstance(v, list) else " lines"
            c = _cursor(tool, f, v, n, base + n)
            paged[f] = f"{base + 1}-{base + n} of {base + total}{unit}; rest: more(cursor='{c}')"
        if size({**out, "paged": paged}) <= budget:
            break
    if paged:
        out["paged"] = paged
    return out


def more(cursor: str) -> dict:
    with _lock:
        c = _cursors.get(cursor.strip().strip("'\""))
    if c is None:
        return {"error": "unknown or expired cursor: repeat the original call (answers are cached, so it is cheap)"}
    v, off = c["value"], c["offset"]
    rest = v[off:] if isinstance(v, list) else "\n".join(_lines(v)[off:])
    out = {"tool": c["tool"], "field": c["field"], "from": c["base"] + 1, c["field"]: rest}
    return fit(out, c["tool"], offsets={c["field"]: c["base"]})
