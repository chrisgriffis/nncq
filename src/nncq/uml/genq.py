"""The Model explorer's diagram generation queue. Generating a diagram on a big index can take a minute (more while
the embedder keeps the GPU and disk busy), so the page queues its requests here instead of firing them all at the
server: one diagram at a time on its own thread, newest request first (what you just clicked is what you wait for),
with pause / play / stop (interrupt the running one, pause) / eject (stop and clear) and per-item cancel. A stopped
generation is cut short by a sqlite progress handler: its next query step fails. In memory, per server process; MCP uml_view calls bypass it."""
from __future__ import annotations

import itertools
import threading
import time
import traceback

KEEP_DONE = 50

_cv = threading.Condition()
_ids = itertools.count(1)
_queue: list[dict] = []             # waiting, next first
_running: dict | None = None
_done: dict[int, dict] = {}         # finished / cancelled / failed, oldest first (the page reads its results here)
_paused = False
_thread: threading.Thread | None = None


def _label(kind: str, subject: str, path: str) -> str:
    what = subject or path or "workspace overview"
    return what if kind == "default" else f"{kind}: {what}"


def _pub(it: dict | None) -> dict | None:
    if it is None:
        return None
    out = {k: it[k] for k in ("id", "repo", "kind", "subject", "path", "label", "state") if k in it}
    if it.get("started"):
        out["secs"] = round((it.get("finished") or time.time()) - it["started"], 1)
    for k in ("key", "error"):
        if it.get(k):
            out[k] = it[k]
    return out


def _finish(it: dict, state: str, **kv) -> None:
    it.update(state=state, finished=time.time(), **kv)
    _done[it["id"]] = it
    while len(_done) > KEEP_DONE:
        del _done[next(iter(_done))]


def add(repo: str, kind: str = "", subject: str = "", path: str = "", wait: float = 0.0) -> dict:
    """Queue a diagram (kind '' = the default one for subject / path); the same request already queued moves to the
    front, one already running is joined. wait: seconds to block for it (fast diagrams answer in one round trip)."""
    global _thread
    kind = kind or "default"
    sig = (repo, kind, subject, path)
    with _cv:
        it = _running if _running is not None and _running["sig"] == sig else None
        if it is None:
            it = next((x for x in _queue if x["sig"] == sig), None)
            if it is not None:
                _queue.remove(it)
            else:
                it = {"id": next(_ids), "sig": sig, "repo": repo, "kind": kind, "subject": subject, "path": path,
                      "label": _label(kind, subject, path), "state": "queued", "added": time.time()}
            _queue.insert(0, it)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_work, daemon=True, name="uml-genq")
            _thread.start()
        _cv.notify_all()
        end = time.time() + wait
        while it["id"] not in _done and (left := end - time.time()) > 0:
            _cv.wait(left)
        return _pub(it)


def control(op: str, item: int = 0) -> dict:
    """play | pause | stop (interrupt the running one, pause) | eject (stop, clear the queue, play) | cancel item."""
    global _paused
    with _cv:
        if op == "play":
            _paused = False
        elif op == "pause":
            _paused = True
        elif op in ("stop", "eject"):
            _interrupt()
            _paused = op == "stop"
            if op == "eject":
                for it in _queue:
                    _finish(it, "cancelled")
                _queue.clear()
        elif op == "cancel":
            if _running is not None and _running["id"] == item:
                _interrupt()
            elif (it := next((x for x in _queue if x["id"] == item), None)) is not None:
                _queue.remove(it)
                _finish(it, "cancelled")
            else:
                return {"error": f"no queued or running diagram {item}"}
        else:
            return {"error": f"unknown op {op!r}: play, pause, stop, eject, cancel"}
        _cv.notify_all()
    return state()


def state() -> dict:
    with _cv:
        return {"paused": _paused, "running": _pub(_running), "queued": [_pub(x) for x in _queue],
                "done": {str(k): _pub(v) for k, v in _done.items()}}


def _interrupt() -> None:
    """Under _cv: cancel the running item (its Store's progress handler fails the next query step)."""
    if _running is not None:
        _running["cancel"] = True


def _work() -> None:
    global _running
    from nncq import answers, repos
    from nncq.store import Store
    from nncq.uml import catalog, tools
    while True:
        with _cv:
            while _paused or not _queue:
                _cv.wait()
            it = _running = _queue.pop(0)
            it.update(state="running", started=time.time())
            _cv.notify_all()
        out: dict = {}
        try:
            r = repos.get(it["repo"]) if it["repo"] else next(iter(repos.load().values()))
            st = Store(r.db, cache_mb=64)
            st.db.set_progress_handler(lambda: bool(it.get("cancel")), 2000)
            try:
                n0 = st.q("SELECT COUNT(*) FROM views")[0][0]
                out = tools._ready(st) or (
                    catalog.default_view(st, it["subject"], it["path"], "json") if it["kind"] == "default"
                    else catalog.view(st, it["kind"], it["subject"], "", "json",
                                      **({"path": it["path"]} if it["path"] else {})))
                it["new"] = st.q("SELECT COUNT(*) FROM views")[0][0] != n0
            finally:
                st.close()
        except Exception as e:
            if not it.get("cancel"):
                traceback.print_exc()
            out = {"error": f"{type(e).__name__}: {e}"}
        with _cv:
            if it.get("cancel"):
                _finish(it, "cancelled")
            elif "error" in out:
                _finish(it, "error", error=str(out["error"]) + (f" ({out['fix']})" if out.get("fix") else ""))
            else:
                _finish(it, "done", key=out.get("key", ""))
            _running = None
            _cv.notify_all()
        if it["state"] == "done" and it.get("new"):
            answers.bump()          # a new stored view: the stored list (uml_views) must show it
