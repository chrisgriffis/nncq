"""Background embedding worker (GPU). Runs as its own process so pausing frees all VRAM: it exits.

Order: every chunk gets a cheap "card" vector first (tier 1: path, kind, symbol, first lines), newest files
first; then, with NNCQ_EMBED_TIER=2, full-chunk vectors (tier 2) replace them. Each batch commits on its
own, so Ctrl+C, a reboot or `nncq pause` loses at most one batch. A chunk replaced or deleted while its
batch was on the GPU is skipped at write time (ids are never reused).

Yields the GPU (unloads the model and waits) when NNCQ_BG_WHEN=idle and the user is active, or when
free VRAM is below NNCQ_GPU_MIN_FREE_MB. Holds its next batch while a search is using the GPU (searches mark
cache/db/query.busy), so query latency stays ~1 s while embedding runs. Status: cache/db/<repo>/worker.json.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

from nncq import config, sysutil
from nncq.repos import Repo
from nncq.search import card_text, full_text
from nncq.store import Store

CARD_BATCH = 256          # small batches: a search waits for at most one batch (~1-2 s) before it gets the GPU
FULL_BATCH = 48
QUERY_QUIET_S = 3.0       # hold the next batch until no search has used the GPU for this long


def _wait_for_queries(save) -> None:
    waited = False
    while True:
        try:
            age = time.time() - config.QUERY_BUSY_FILE.stat().st_mtime
        except OSError:
            return
        if age >= QUERY_QUIET_S:
            return
        if not waited:
            save(state="yielding the GPU to a search")
            waited = True
        time.sleep(QUERY_QUIET_S - age + 0.05)


def _parser_running(store: Store) -> bool:
    """A T0 build / delta is still writing chunks (indexer._begin records it with its pid)."""
    raw = store.meta("sync")
    try:
        return bool(raw) and sysutil.pid_alive(json.loads(raw).get("pid", 0))
    except ValueError:
        return False


def status_file(repo: Repo):
    return repo.db.parent / "worker.json"


def _claim(repo: Repo):
    """One worker per repo: an OS lock on worker.lock, held until the process exits (worker.json alone races)."""
    f = open(repo.db.parent / "worker.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def read_status(repo: Repo) -> dict | None:
    f = status_file(repo)
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    s["alive"] = sysutil.pid_alive(s.get("pid", 0))
    return s


def paused() -> bool:
    try:
        return bool(json.loads(config.CONTROL_FILE.read_text(encoding="utf-8")).get("paused"))
    except (OSError, ValueError):
        return False


def set_paused(flag: bool) -> None:
    config.CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONTROL_FILE.write_text(json.dumps({"paused": flag, "at": time.time()}), encoding="utf-8")


def pending(store: Store, target: int) -> dict:
    t = dict(store.q("SELECT tier, COUNT(*) FROM chunks GROUP BY tier"))
    return {"cards_pending": t.get(0, 0) if target >= 1 else 0,
            "full_pending": (t.get(0, 0) + t.get(1, 0)) if target >= 2 else 0,
            "total": sum(t.values())}


def _yield_reason(s: config.Settings, loaded: bool) -> str:
    if s.bg_when == "idle":
        idle = sysutil.idle_seconds()
        if idle is not None and idle < s.idle_minutes * 60:
            return f"waiting for {s.idle_minutes:g} min without input (NNCQ_BG_WHEN=idle)"
    if s.gpu_min_free_mb and not loaded:
        free = sysutil.gpu_free_mb()
        if free is not None and free < s.gpu_min_free_mb:
            return f"GPU busy ({free} MB free < NNCQ_GPU_MIN_FREE_MB={s.gpu_min_free_mb})"
    return ""


def run(repo: Repo, once: bool = False, max_batches: int = 0, log=print, parent: int = 0) -> dict:
    """parent: pid of the server that started this worker; the worker exits when it is gone (no orphans)."""
    s = config.settings()
    if s.embed_tier <= 0:
        return {"state": "disabled (NNCQ_EMBED_TIER=0)"}
    lock = _claim(repo)
    if lock is None:
        cur = read_status(repo) or {}
        return {"state": f"already running (pid {cur.get('pid', '?')})"}
    from nncq.models import Embedder
    emb = Embedder(idle=0)
    store = Store(repo.db, cache_mb=64)
    st = {"pid": os.getpid(), "repo": repo.name, "state": "starting", "started": time.time(), "embedded": 0}

    def save(**kw):
        st.update(kw, updated=time.time())
        tmp = status_file(repo).with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, status_file(repo))

    batches = 0
    t_rate, n_rate = time.time(), 0
    try:
        while True:
            if parent and not sysutil.pid_alive(parent):
                save(state="exited (server gone)")
                log(f"server (pid {parent}) is gone: exiting")
                break
            if paused():
                save(state="paused")
                log("paused: exiting (VRAM released)")
                break
            why = _yield_reason(s, emb.model is not None)
            if why:
                emb.unload()
                save(state=why)
                if once:
                    break
                for _ in range(30):
                    if parent and not sysutil.pid_alive(parent):
                        break
                    time.sleep(1)
                continue
            rows = []
            _wait_for_queries(save)
            if s.embed_tier >= 1:
                tier, size, fmt = 1, CARD_BATCH, card_text
                rows = store.q("SELECT c.id, f.path, c.kind, c.symbol, c.text FROM chunks c JOIN files f ON f.id=c.file_id "
                               "WHERE c.tier=0 ORDER BY c.prio DESC LIMIT ?", (size,))
            if not rows and s.embed_tier >= 2:
                tier, size, fmt = 2, FULL_BATCH, full_text
                rows = store.q("SELECT c.id, f.path, c.kind, c.symbol, c.text FROM chunks c JOIN files f ON f.id=c.file_id "
                               "WHERE c.tier=1 ORDER BY c.prio DESC LIMIT ?", (size,))
            if not rows:
                if _parser_running(store):
                    save(state="caught up: waiting for the parser to commit more chunks", **pending(store, s.embed_tier))
                    time.sleep(3)
                    continue
                save(state="done", **pending(store, s.embed_tier))
                log("nothing left to embed")
                break
            v = emb.encode_documents([fmt(p, k or "", sym or "", txt or "") for _, p, k, sym, txt in rows])
            v16 = v.astype(np.float16)
            wrote = 0
            with store.tx() as db:
                for (cid, *_), vec in zip(rows, v16):
                    if db.execute("UPDATE chunks SET tier=? WHERE id=? AND tier<?", (tier, cid, tier)).rowcount:
                        db.execute("INSERT OR REPLACE INTO vecs(chunk_id, tier, v) VALUES(?,?,?)",
                                   (cid, tier, vec.tobytes()))
                        wrote += 1
            batches += 1
            n_rate += len(rows)
            el = time.time() - t_rate
            rate = n_rate / el if el > 0 else 0
            pend = pending(store, s.embed_tier)
            left = pend["cards_pending"] / rate if tier == 1 and rate else (pend["full_pending"] / rate if rate else 0)
            save(state=f"embedding tier {tier} ({'cards' if tier == 1 else 'full chunks'})",
                 embedded=st["embedded"] + wrote, chunks_per_s=round(rate, 1),
                 eta_this_tier_min=round(left / 60, 1), **pend)
            if batches % 20 == 1:
                log(f"tier {tier}: +{wrote} ({rate:.0f} chunks/s) pending cards={pend['cards_pending']} "
                    f"full={pend['full_pending']}")
            if el > 120:
                t_rate, n_rate = time.time(), 0
            if max_batches and batches >= max_batches:
                save(state="stopped (batch limit)")
                break
    except KeyboardInterrupt:
        save(state="interrupted")
    finally:
        emb.unload()
        store.close()
        if st.get("state", "").startswith("embedding"):
            save(state="exited")
        lock.close()
    return st
