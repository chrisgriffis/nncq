"""MCP server: search, graph, data and index tools over stdio (an agent launches it) or streamable HTTP.

Lifecycle: the process that holds NNCQ_PORT "owns" the workspace indexes and runs, per registered repo:
  - a start-up catch-up delta (picks up p4 syncs made while nothing was running)
  - a file watcher (debounced path deltas) when NNCQ_WATCH is on
  - a supervisor that starts the GPU embedding worker when chunks lack vectors (not while paused)
  - the status dashboard on http://127.0.0.1:NNCQ_PORT/
stdio (`nncq serve --stdio`, what Copilot launches): the first copy binds the port and becomes the owner. Until
someone opens the page, the port is a bare listening socket; the first request starts the web app in-process on
that socket. Everything lives in the MCP process: when the client exits (stdin closes) the server stops its
worker and exits, taking the dashboard with it; the worker also exits by itself if its parent disappears.
Other stdio copies answer queries only and take over the port if the owner goes away.
HTTP (`nncq serve` / `nncq start`): a standalone server for several clients; MCP at /mcp, dashboard at /.

Every tool runs in a worker thread with its own read connection, so a slow query never blocks others,
and every answer passes through health.annotate (index_state appears only when something is degraded).
Read-only on the workspace: nothing here writes outside cache/.
"""
from __future__ import annotations

import collections
import io
import json
import os
import select
import socket
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

from nncq import ask as asker, config, datarefs, graph, health, indexer, repos, search, sysutil, worker
from nncq.store import Store

INSTRUCTIONS = """Local code index (nothing leaves this machine). Start with ask for any question: it returns a short,
generated map (how the question was read, what the hits cover, one line of facts per hit, how hits call each
other) with refs; then expand(refs) for exact code or structure. search_code gives raw code blocks;
use find_symbol / file_outline / read_code to zoom in; the graph tools (symbol_context, called_by, calls,
call_tree, call_trace, impact, type_hierarchy, file_context, hotspots) answer structural questions without
reading files. Graph links are by name (static, no build): ambiguity is reported. engine: data_for_code,
code_for_data, asset_info, top_assets. If a result has index_state, part of the index is still catching up:
it says what, where and since when; keyword and graph results are still valid."""

_local = threading.local()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_workers: dict[str, object] = {}
_watchers: dict[str, object] = {}
_owner = threading.Event()               # this process holds the port: it indexes, watches, embeds
_stop = threading.Event()
_info = {"mode": "stdio", "started": time.time(), "mcp_url": "", "dashboard": ""}
_events: deque = deque(maxlen=300)        # (time, message) for the dashboard
_calls: deque = deque(maxlen=60)
_tool_stats: dict[str, dict] = {}
_ARG_KEYS = ("question", "query", "refs", "name", "to", "pattern", "path", "asset", "type_name", "what")


def _log(msg: str) -> None:
    _events.append((time.time(), msg))
    print(msg, flush=True)


def _watch(r: repos.Repo) -> None:
    if not config.settings().watch or r.name in _watchers:
        return
    try:
        _watchers[r.name] = _Watch(r)
    except Exception as e:
        _log(f"[{r.name}] watcher not started: {e}")


def _repo(name: str) -> repos.Repo:
    try:
        return repos.get(name)
    except KeyError as e:
        raise ValueError(str(e).strip("'\"")) from None


def _store(r: repos.Repo) -> Store:
    d = getattr(_local, "stores", None)
    if d is None:
        d = _local.stores = {}
    s = d.get(r.name)
    if s is None:
        if not r.db.exists():
            raise ValueError(f"repo '{r.name}' has no index yet: call index_repo or run `nncq index`")
        s = d[r.name] = Store(r.db, cache_mb=64)
    return s


async def _run(fn, *a, **k):
    caller = sys._getframe(1)              # the tool coroutine: its name and arguments feed the dashboard
    tool, args = caller.f_code.co_name, caller.f_locals
    arg = next((str(args[x]) for x in _ARG_KEYS if args.get(x)), "")
    if args.get("repo"):
        arg += f"  (repo {args['repo']})"
    t = time.time()

    def call():
        try:
            return fn(*a, **k)
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"error": f"{type(e).__name__}: {e}"}
    out = await anyio.to_thread.run_sync(call)
    ms = round((time.time() - t) * 1000)
    err = isinstance(out, dict) and "error" in out
    s = _tool_stats.setdefault(tool, {"n": 0, "errors": 0, "ms_total": 0, "last_ms": 0, "last_at": 0})
    s["n"] += 1
    s["errors"] += err
    s["ms_total"] += ms
    s["last_ms"], s["last_at"] = ms, time.time()
    _calls.append({"at": time.time(), "tool": tool, "arg": arg[:200], "ms": ms,
                   **({"error": str(out["error"])[:200]} if err else {})})
    return out


def _with(repo: str, fn, paths_of=None, semantic: bool = False):
    r = _repo(repo)
    st = _store(r)
    out = fn(r, st)
    if isinstance(out, dict) and "error" not in out:
        paths = paths_of(out) if paths_of else None
        health.annotate(out, st, paths, config.settings().embed_tier, semantic=semantic)
    return out


# ------------------------------------------------------------------ background jobs (HTTP mode)

def _job(r: repos.Repo, kind: str, fn) -> dict:
    with _jobs_lock:
        j = _jobs.get(r.name)
        if j and j["state"] == "running":
            return {"started": False, "reason": f"{j['kind']} already running since "
                                                 f"{time.strftime('%H:%M:%S', time.localtime(j['started']))}"}
        j = _jobs[r.name] = {"kind": kind, "state": "running", "started": time.time(), "log": []}

    def log(msg):
        j["log"] = (j["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-30:]
        _log(msg if msg.startswith(f"[{r.name}]") else f"[{r.name}] {msg}")

    def go():
        try:
            j["result"] = fn(log)
            j["state"] = "done"
        except Exception as e:
            traceback.print_exc()
            j["state"] = f"failed: {type(e).__name__}: {e}"
        j["finished"] = time.time()
    threading.Thread(target=go, daemon=True, name=f"job-{r.name}").start()
    return {"started": True, "job": kind, "repo": r.name, "progress": "call index_status"}


def _ensure_worker(r: repos.Repo) -> str:
    s = config.settings()
    if s.embed_tier <= 0:
        return "embedding disabled (NNCQ_EMBED_TIER=0)"
    if worker.paused():
        return "paused (resume with update_index mode=resume or `nncq resume`)"
    ws = worker.read_status(r)
    if ws and ws["alive"]:
        return f"running (pid {ws['pid']})"
    if not r.db.exists():
        return "no index yet"
    st = Store(r.db, cache_mb=8)
    try:
        p = worker.pending(st, s.embed_tier)
    finally:
        st.close()
    if not (p["cards_pending"] or p["full_pending"]):
        return "idle (all chunks embedded)"
    proc = sysutil.spawn([sys.executable, "-m", "nncq", "work", "--repo", r.name, "--parent", str(os.getpid())],
                         config.LOG_DIR / f"worker-{r.name}.log", detach=False)
    _workers[r.name] = proc
    _log(f"[{r.name}] embedding worker started (pid {proc.pid})")
    return f"started (pid {proc.pid})"


def _supervisor():
    while not _stop.wait(20):
        _gpu()
        for r in repos.load().values():
            try:
                _ensure_worker(r)                  # also during a build: it embeds what T0 has committed so far
                ws = worker.read_status(r)
                if ws and ws.get("alive"):         # keep the dashboard charts filled even when nobody watches
                    _sample("rate:" + r.name, ws.get("updated") or time.time(), ws.get("chunks_per_s"))
            except Exception:
                traceback.print_exc()


class _Watch:
    """watchdog observer per repo; paths are debounced and applied as one update_paths delta."""

    def __init__(self, r: repos.Repo):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        self.r = r
        self.exts = set(indexer.ext_map()) | {".xdata"}
        self.pending: set[str] = set()
        self.last = 0.0
        self.lock = threading.Lock()
        root = os.path.normcase(str(r.root.resolve())) + os.sep
        me = self

        class H(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.is_directory:
                    return
                for p in (getattr(ev, "src_path", ""), getattr(ev, "dest_path", "")):
                    if not p:
                        continue
                    p = os.fsdecode(p)
                    if os.path.splitext(p)[1].lower() not in me.exts:
                        continue
                    n = os.path.normcase(p)
                    if n.startswith(root):
                        with me.lock:
                            me.pending.add(p[len(root):].replace("\\", "/"))
                            me.last = time.time()
        self.obs = Observer()
        self.obs.schedule(H(), str(r.root), recursive=True)
        self.obs.daemon = True
        self.obs.start()
        threading.Thread(target=self._loop, daemon=True, name=f"watch-{r.name}").start()

    def _loop(self):
        deb = config.settings().watch_debounce
        while True:
            time.sleep(1)
            with self.lock:
                if not self.pending or time.time() - self.last < deb:
                    continue
                batch, self.pending = sorted(self.pending), set()
            j = _jobs.get(self.r.name)
            if j and j["state"] == "running":
                with self.lock:
                    self.pending.update(batch)      # a build/sync is applying; retry after it
                continue
            try:
                st = Store(self.r.db, cache_mb=32)
                try:
                    res = indexer.update_paths(self.r, batch, st, log=lambda m: None, source="file changes")
                finally:
                    st.close()
                if res.get("changed") or res.get("removed") or res.get("data_files"):
                    _log(f"[{self.r.name}] watcher: {len(batch)} paths -> {res}")
            except Exception:
                traceback.print_exc()


# ------------------------------------------------------------------ tools

def build(stdio: bool = False) -> FastMCP:
    s = config.settings()
    mcp = FastMCP("nncq", instructions=INSTRUCTIONS, host=s.host, port=s.port, stateless_http=True,
                  json_response=True, log_level="WARNING")
    if not config.is_loopback(s.host):
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    global _mcp
    _mcp = mcp
    paths_of_results = lambda o: [x["path"] for x in o.get("results", [])]

    @mcp.tool()
    async def ask(question: str, k: int = 8, path: str = "", lang: str = "", repo: str = "") -> dict:
        """Start here for any question about the code ("how does X work", "where is Y decided").
        Returns a compact map, not code: how the question was interpreted (terms, stems, the code's own
        vocabulary for it), what the result set covers (areas, kinds, match strength, which search channel found
        what), one line of facts per hit (signature, doc comment, meaningful calls, callers, best-matching lines,
        why it ranked) and how the hits call each other. Dig in with expand(refs) using the hits' refs.
        path: substring/glob filter on file path. lang: e.g. 'cpp,csharp'."""
        return await _run(_with, repo, lambda r, st: asker.ask(st, r.root, question, k, path, lang),
                          lambda o: [h["at"].replace(" / ", "/").rsplit(":", 1)[0] for h in o.get("hits", [])],
                          True)

    @mcp.tool()
    async def expand(refs: str, what: str = "code", repo: str = "") -> dict:
        """Dig into hits from ask. refs: comma-separated refs (c123), symbol names, or path:start-end.
        what: code (exact current lines) | context (symbol card) | callers | callees | outline (file symbols)."""
        return await _run(_with, repo, lambda r, st: asker.expand(r, st, refs, what))

    @mcp.tool()
    async def search_code(query: str, k: int = 10, path: str = "", lang: str = "", repo: str = "") -> dict:
        """Raw hybrid code search (keywords + meaning + reranker): whole functions/classes as code blocks.
        Prefer ask for questions; use this when you want the code text of the top matches directly.
        path: substring or glob filter on file path (e.g. 'Engine/Entity' or '*.ddf'). lang: e.g. 'cpp,csharp'."""
        return await _run(_with, repo, lambda r, st: search.search(st, query, k, path, lang), paths_of_results,
                          True)

    @mcp.tool()
    async def grep_code(pattern: str, glob: str = "", fixed: bool = False, ignore_case: bool = False,
                        max_results: int = 100, repo: str = "") -> dict:
        """Exact text / regex search with ripgrep over the repo (honours .p4ignore). glob: e.g. '*.cpp,*.h'."""
        return await _run(lambda: search.grep_code(_repo(repo), pattern, glob, fixed, ignore_case, max_results))

    @mcp.tool()
    async def find_symbol(name: str, kind: str = "", path: str = "", repo: str = "") -> dict:
        """Where a class/function/method/etc. is defined. name: short or qualified (app::Foo::bar, Entity.LayerData).
        kind: optional filter, e.g. 'class,struct' or 'type' or 'method'."""
        return await _run(_with, repo, lambda r, st: search.find_symbol(st, name, kind, path))

    @mcp.tool()
    async def file_outline(path: str, repo: str = "") -> dict:
        """The symbols in a file with line ranges and signatures, without reading the file. Path suffix is enough."""
        return await _run(_with, repo, lambda r, st: search.file_outline(st, path))

    @mcp.tool()
    async def find_references(name: str, path: str = "", limit: int = 80, repo: str = "") -> dict:
        """Every line mentioning an identifier (exact word), plus call-site and subclass counts."""
        return await _run(_with, repo, lambda r, st: search.find_references(st, name, path, limit))

    @mcp.tool()
    async def read_code(path: str, start: int = 1, end: int = 0, repo: str = "") -> dict:
        """Exact lines from disk (numbered). end=0 means to end of file (max 400 lines per call)."""
        return await _run(lambda: search.read_code(_repo(repo), path, start, end))

    @mcp.tool()
    async def symbol_context(name: str, path: str = "", repo: str = "") -> dict:
        """One-call card for a symbol: definition, bases/derived/members (types) or callers/callees/overrides
        (functions), engine data types that use it, how widely it is mentioned."""
        return await _run(_with, repo, lambda r, st: graph.symbol_context(st, name, path))

    @mcp.tool()
    async def calls(name: str, path: str = "", repo: str = "") -> dict:
        """'Calls From': everything a function calls, in source order, resolved to definitions when unique."""
        return await _run(_with, repo, lambda r, st: graph.calls(st, name, path))

    @mcp.tool()
    async def called_by(name: str, path: str = "", limit: int = 60, repo: str = "") -> dict:
        """'Calls To': every caller with file:line, ranked (same class > mentions the class > name only)."""
        return await _run(_with, repo, lambda r, st: graph.called_by(st, name, path, limit))

    @mcp.tool()
    async def call_tree(name: str, direction: str = "in", depth: int = 3, path: str = "", repo: str = "") -> dict:
        """Callers (direction='in') or callees ('out') as an indented tree, N levels."""
        return await _run(_with, repo, lambda r, st: graph.call_tree(st, name, direction, depth, path=path))

    @mcp.tool()
    async def call_trace(to: str, frm: str = "", max_depth: int = 8, repo: str = "") -> dict:
        """Static 'how does execution get here': debugger-style pseudo call stacks ending in `to`, walked back to
        entry points. With frm: how two functions relate (frm reaches to, to reaches frm, or shared callers)."""
        return await _run(_with, repo, lambda r, st: graph.call_trace(st, to, frm, max_depth))

    @mcp.tool()
    async def impact(name: str, depth: int = 3, repo: str = "") -> dict:
        """Blast radius of changing a function: transitive callers summarised by folder and file."""
        return await _run(_with, repo, lambda r, st: graph.impact(st, name, depth))

    @mcp.tool()
    async def type_hierarchy(name: str, repo: str = "") -> dict:
        """Base chain(s) up and derived-type tree down for a class/struct/DDF type."""
        return await _run(_with, repo, lambda r, st: graph.type_hierarchy(st, name))

    @mcp.tool()
    async def file_context(path: str, repo: str = "") -> dict:
        """A file's outline, includes / included-by, and which files it calls into / is called from."""
        return await _run(_with, repo, lambda r, st: graph.file_context(st, path))

    @mcp.tool()
    async def hotspots(what: str = "called", under: str = "", k: int = 30, repo: str = "") -> dict:
        """Static top-N tables. what: called | derived | fanout | included | largest | uncalled.
        under: path prefix restricting where the symbols are defined."""
        return await _run(_with, repo, lambda r, st: graph.hotspots(st, what, under, k))

    @mcp.tool()
    async def data_for_code(name: str, k: int = 15, repo: str = "") -> dict:
        """engine: which .xdata data types/assets use this code type (entity Foo -> FooData, subclasses too)."""
        return await _run(_with, repo, lambda r, st: datarefs.data_for_code(st, name, k))

    @mcp.tool()
    async def code_for_data(asset: str, k: int = 8, repo: str = "") -> dict:
        """engine: the code (.ddf / C++ / C#) that defines and implements the types an asset uses."""
        return await _run(_with, repo, lambda r, st: datarefs.code_for_data(st, asset, k))

    @mcp.tool()
    async def asset_info(asset: str, k: int = 25, repo: str = "") -> dict:
        """engine: what a .xdata asset instantiates, what it references and what references it. Any path suffix."""
        return await _run(_with, repo, lambda r, st: datarefs.asset_info(st, asset, k))

    @mcp.tool()
    async def top_assets(k: int = 50, under: str = "", group_depth: int = 0, repo: str = "") -> dict:
        """engine: most-referenced assets (static inbound refs); group_depth > 0 rolls up to folders."""
        return await _run(_with, repo, lambda r, st: {"assets": datarefs.top_assets(st, k, under, group_depth)})

    @mcp.tool()
    async def index_status(repo: str = "") -> dict:
        """Index freshness: files/chunks/symbols, embedding progress and ETA, running jobs, degraded areas."""
        def f():
            names = [repo] if repo else list(repos.load())
            out = {}
            for n in names:
                r = _repo(n)
                st = indexer.status(r)
                ws = worker.read_status(r)
                if ws:
                    st["embedding"] = {k: v for k, v in ws.items() if k not in ("repo",)}
                st["embedding_paused"] = worker.paused()
                j = _jobs.get(r.name)
                if j:
                    st["job"] = {"kind": j["kind"], "state": j["state"],
                                 "started": time.strftime("%H:%M:%S", time.localtime(j["started"])),
                                 "log": j["log"][-8:], **({"result": j["result"]} if "result" in j else {})}
                if r.db.exists():
                    s2 = Store(r.db, cache_mb=8)
                    try:
                        hs, _ = health.compute(s2, config.settings().embed_tier)
                    finally:
                        s2.close()
                    if hs:
                        hs.pop("_sync", None)
                        st["index_state"] = hs
                out[n] = st
            return out if not repo else out[repo]
        return await _run(f)

    def _owned() -> dict | None:
        if _owner.is_set():
            return None
        return {"error": "another nncq process owns indexing for these workspaces "
                         f"({_owner_desc()}); queries work here, indexing requests go to it"}

    @mcp.tool()
    async def update_index(repo: str = "", mode: str = "delta") -> dict:
        """Bring the index up to date or control embedding. Returns immediately; poll index_status.
        mode: delta (re-index files changed on disk, e.g. after a p4 sync) | embed (start the GPU worker now) |
        pause (stop embedding, free VRAM) | resume. Never writes to the workspace."""
        def f():
            r = _repo(repo)
            if mode == "pause":
                worker.set_paused(True)
                return {"paused": True, "note": "the worker finishes its batch and exits"}
            if mode not in ("delta", "embed", "resume"):
                return {"error": f"unknown mode {mode!r} (delta | embed | pause | resume)"}
            if mode == "resume":
                worker.set_paused(False)
            if (o := _owned()) is not None:
                return {**o, "paused": False} if mode == "resume" else o
            if mode == "delta":
                return _job(r, "delta", lambda log: indexer.update(r, log=log))
            return {"paused": worker.paused(), "worker": _ensure_worker(r)}
        return await _run(f)

    @mcp.tool()
    async def index_repo(path: str, name: str = "", dirs: str = "", fresh: bool = False) -> dict:
        """Register a workspace and build (or refresh) its index in the background: parsing, symbols, graph
        and the .xdata cross-reference first (CPU, minutes), then embeddings on the GPU. Read-only on the workspace.
        dirs: comma-separated subfolders to index for code (e.g. 'src/Code'); empty = everything."""
        def f():
            if (o := _owned()) is not None:
                return o
            r = repos.add(Path(path), name, [d for d in dirs.split(",") if d.strip()] if dirs else None)
            first = not r.db.exists()

            def go(log):
                res = indexer.update(r, full=fresh, log=log)
                _watch(r)
                log(f"embedding worker: {_ensure_worker(r)}")
                return res
            return {"repo": r.name, "root": str(r.root), "dirs": r.dirs,
                    **_job(r, "full build" if first or fresh else "delta", go)}
        return await _run(f)
    return mcp


# ------------------------------------------------------------------ dashboard state

_gpu_cache: dict = {"at": 0.0}
_mcp: FastMCP | None = None
WEB_TOOLS = {"ask", "expand", "search_code", "grep_code", "find_symbol", "file_outline", "find_references", "read_code",
             "symbol_context", "calls", "called_by", "call_tree", "call_trace", "impact", "type_hierarchy",
             "file_context", "hotspots", "data_for_code", "code_for_data", "asset_info", "top_assets"}


async def web_tool(name: str, args: dict) -> dict:
    """The dashboard's search / symbol pages call the same (read-only) MCP tools agents use."""
    if name not in WEB_TOOLS or _mcp is None:
        return {"error": f"not available from the web page: {name}"}
    try:
        return await _mcp._tool_manager.call_tool(name, args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
_repo_cache: dict[str, tuple[float, dict]] = {}
_LIVE_N = 600                                     # ~15 min of live samples at the page's poll rate
_hist: dict[str, collections.deque] = {}          # rolling series for the dashboard charts: [(t, value), ...]
# Charts survive restarts: samples are averaged per minute into a small CSV (t,key,value), reloaded at start.
HIST_FILE = config.DATA_DIR / "history.csv"
HIST_KEEP_S = 7 * 86400
_hist_min: dict[str, list] = {}                   # key -> [minute, sum, count] being averaged
_hist_long: dict[str, collections.deque] = {}     # key -> per-minute averages [(t, v)], up to HIST_KEEP_S
_hist_lock = threading.Lock()
_hist_loaded = False


def _long(key: str) -> collections.deque:
    return _hist_long.setdefault(key, collections.deque(maxlen=HIST_KEEP_S // 60))


def _hist_buckets(win_s: int = 86400, n: int = 240) -> dict[str, list]:
    """Per-minute history (plus the minute in progress) averaged into n buckets over the last win_s."""
    now = time.time()
    w = win_s / n
    out = {}
    with _hist_lock:
        for key, d in _hist_long.items():
            pts = [p for p in d if p[0] >= now - win_s]
            m = _hist_min.get(key)
            if m and m[2]:
                pts.append((m[0] * 60 + 30, m[1] / m[2]))
            b: dict[int, list] = {}
            for t, v in pts:
                s = b.setdefault(int((t - (now - win_s)) // w), [0.0, 0])
                s[0] += v
                s[1] += 1
            if b:
                out[key] = [(round(now - win_s + (i + .5) * w), round(s / c, 2)) for i, (s, c) in sorted(b.items())]
    return out


def _hist_load() -> None:
    """Seed the charts with the saved per-minute averages and drop rows older than HIST_KEEP_S."""
    global _hist_loaded
    with _hist_lock:
        if _hist_loaded:
            return
        _hist_loaded = True
        try:
            rows = HIST_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        cut, keep = time.time() - HIST_KEEP_S, []
        for line in rows:
            try:
                t, key, v = line.split(",", 2)
                t, v = float(t), float(v)
            except ValueError:
                continue
            if t >= cut:
                keep.append(line)
                _long(key).append((t, v))
        for key, d in _hist_long.items():            # low-fi seed so the short charts show the time before a restart
            live = _hist.setdefault(key, collections.deque(maxlen=_LIVE_N))
            for p in d:
                if p[0] >= time.time() - 1800:
                    live.append(p)
        if len(keep) < len(rows):
            tmp = HIST_FILE.with_suffix(".tmp")
            tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8", newline="\n")
            os.replace(tmp, HIST_FILE)


def _sample(key: str, t: float, v) -> None:
    if not _hist_loaded:
        _hist_load()
    d = _hist.setdefault(key, collections.deque(maxlen=_LIVE_N))
    if v is None or (d and d[-1][0] >= t):
        return
    d.append((round(t, 1), v))
    minute = int(t // 60)
    with _hist_lock:
        m = _hist_min.get(key)
        if m and m[0] != minute and m[2]:
            avg = float(f"{m[1] / m[2]:.4g}")
            _long(key).append((m[0] * 60 + 30, avg))
            try:
                with open(HIST_FILE, "a", encoding="utf-8", newline="\n") as f:
                    f.write(f"{m[0] * 60 + 30},{key},{avg:g}\n")
            except OSError:
                pass
        if not m or m[0] != minute:
            _hist_min[key] = m = [minute, 0.0, 0]
        m[1] += float(v)
        m[2] += 1


def _owner_desc() -> str:
    try:
        s = json.loads(config.SERVER_FILE.read_text(encoding="utf-8"))
        return f"pid {s['pid']}, {s.get('mode', 'http')}, dashboard {s.get('dashboard', '')}"
    except (OSError, ValueError, KeyError):
        return "it holds port %d" % config.settings().port


def _gpu() -> dict:
    if time.time() - _gpu_cache["at"] > 3:
        _gpu_cache.update(sysutil.gpu_info() or {}, at=time.time())
        g = _gpu_cache
        if g.get("mem_total"):
            _sample("gpu_util", g["at"], g.get("util"))
            _sample("gpu_mem", g["at"], round(g["mem_used"] / g["mem_total"] * 100, 1))
    return {k: v for k, v in _gpu_cache.items() if k != "at"}


def _repo_snapshot(r: repos.Repo) -> dict:
    """Index counts are cached briefly (they scan big tables); worker/job state is always fresh."""
    hit = _repo_cache.get(r.name)
    busy = (_jobs.get(r.name) or {}).get("state") == "running"
    if not hit or time.time() - hit[0] > (5 if busy else 15):
        st = indexer.status(r)
        if r.db.exists():
            s2 = Store(r.db, cache_mb=8)
            try:
                st["chunks_by_tier"] = {str(k): v for k, v in
                                        s2.q("SELECT tier, COUNT(*) FROM chunks GROUP BY tier")}
                hs, _ = health.compute(s2, config.settings().embed_tier)
                try:
                    st["parse"] = json.loads(s2.meta("sync") or "null")
                except ValueError:
                    st["parse"] = None
            finally:
                s2.close()
            if hs:
                hs.pop("_sync", None)
                st["index_state"] = hs
        hit = _repo_cache[r.name] = (time.time(), st)
    st = dict(hit[1])
    out = {"name": r.name, "status": st, "paused": worker.paused()}
    ws = worker.read_status(r)
    if ws:
        out["embedding"] = {k: v for k, v in ws.items() if k != "repo"}
        if ws.get("alive"):
            _sample("rate:" + r.name, ws.get("updated") or time.time(), ws.get("chunks_per_s"))
        if ws.get("alive") and ws.get("total"):       # live tier split from the worker between count refreshes
            t2 = ws["total"] - ws.get("full_pending", 0)
            t1 = ws.get("full_pending", 0) - ws.get("cards_pending", 0)
            st["chunks_by_tier"] = {"1": t1, "2": t2, "0": ws.get("cards_pending", 0)}
            st["chunks"] = ws["total"]
    j = _jobs.get(r.name)
    if j:
        out["job"] = {"kind": j["kind"], "state": j["state"],
                      "started": time.strftime("%H:%M:%S", time.localtime(j["started"])), "log": j["log"][-5:]}
    w = _watchers.get(r.name)
    if w:
        out["watcher"] = {"pending": len(w.pending)}
    return out


def snapshot() -> dict:
    models = {}
    for key, label in (("embed", "embedder (queries)"), ("rerank", "reranker")):
        m = search._models.get(key)
        loaded = bool(m is not None and m.model is not None)
        models[label] = {"loaded": loaded, "device": getattr(m, "device", None) if loaded else None,
                         "last_used": getattr(m, "last_used", 0) or None,
                         "unload_in_s": max(0, m.idle - (time.time() - m.last_used)) if loaded and m.idle else None}
    proc = {}
    if "torch" in sys.modules:
        torch = sys.modules["torch"]
        try:
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                proc["cuda_mb"] = round(torch.cuda.memory_reserved() / 2**20)
        except Exception:
            pass
    out_repos = []
    for r in repos.load().values():
        try:
            out_repos.append(_repo_snapshot(r))
        except Exception as e:
            out_repos.append({"name": r.name, "status": {"root": str(r.root), "indexed": False}, "error": str(e)})
    return {"server": {"pid": os.getpid(), "mode": _info["mode"], "owner": _owner.is_set(),
                       "uptime_s": round(time.time() - _info["started"]), "mcp_url": _info["mcp_url"]},
            "paused": worker.paused(), "repos": out_repos, "gpu": _gpu(), "models": models, "process": proc,
            "tools": _tool_stats, "calls": list(_calls), "events": list(_events)[-80:],
            "history": {k: list(v) for k, v in _hist.items()}, "history_24h": _hist_buckets()}


def control(action: str, repo: str = "") -> dict:
    if action == "pause":
        worker.set_paused(True)
        _log("embedding paused from the dashboard")
        return {"paused": True}
    if action == "resume":
        worker.set_paused(False)
        _log("embedding resumed from the dashboard")
        if _owner.is_set():
            for r in ([repos.get(repo)] if repo else repos.load().values()):
                if r.db.exists():
                    _ensure_worker(r)
        return {"paused": False}
    return {"error": f"unknown action {action!r}"}


# ------------------------------------------------------------------ ownership, lifecycle

def _bind(host: str, port: int) -> socket.socket | None:
    """The listening socket doubles as the single-owner lock (exclusive on Windows, no SO_REUSEADDR)."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        return None
    sock.set_inheritable(False)
    return sock


def _token_gate(app, token: str):
    if not token:
        return app
    from starlette.responses import JSONResponse

    async def gated(scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            hdr = dict(scope.get("headers") or [])
            q = scope.get("query_string", b"").decode()
            if hdr.get(b"authorization", b"").decode() != f"Bearer {token}" and f"token={token}" not in q:
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)
    return gated


def _health_route():
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health_route(request):
        return JSONResponse({"ok": True, "pid": os.getpid(), "mode": _info["mode"], "owner": _owner.is_set(),
                             "repos": list(repos.load())})
    return Route("/health", health_route)


def _uvicorn(app, sock: socket.socket) -> None:
    import uvicorn
    cfg = uvicorn.Config(app, log_level="warning", lifespan="on" if _info["mode"] == "http" else "off")
    uvicorn.Server(cfg).run(sockets=[sock])


def _lazy_dashboard(sock: socket.socket) -> None:
    """Hold the port with a bare socket; start the web app on it at the first connection (in this process)."""
    while not _stop.is_set():
        r, _, _ = select.select([sock], [], [], 1.0)
        if r:
            break
    else:
        return
    from nncq import dashboard
    _log(f"dashboard: first request, starting web server on {_info['dashboard']}")
    app = _token_gate(dashboard.app(snapshot, control, web_tool, [_health_route()]), config.settings().token)
    try:
        _uvicorn(app, sock)
    except Exception:
        traceback.print_exc()


def _become_owner(sock: socket.socket | None) -> None:
    s = config.settings()
    _owner.set()
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    _info["dashboard"] = f"http://{host}:{s.port}/"
    config.SERVER_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SERVER_FILE.write_text(json.dumps({"pid": os.getpid(), "mode": _info["mode"], "url": _info["mcp_url"],
                                              "dashboard": _info["dashboard"], "started": time.time()}),
                                  encoding="utf-8")
    for r in repos.load().values():
        if r.db.exists():
            _job(r, "catch-up", lambda log, r=r: indexer.update(r, log=log))
            _watch(r)
    threading.Thread(target=_supervisor, daemon=True, name="supervisor").start()
    if sock is not None:
        threading.Thread(target=_lazy_dashboard, args=(sock,), daemon=True, name="dashboard").start()
    _log(f"owner (pid {os.getpid()}, {_info['mode']}): indexing, watcher, embedding; dashboard {_info['dashboard']}")


def _claim_loop() -> None:
    """stdio: become the owner now if the port is free, else stand by and take over when it frees up."""
    s = config.settings()
    announced = False
    while not _stop.is_set():
        sock = _bind(s.host, s.port)
        if sock is not None:
            _become_owner(sock)
            return
        if not announced:
            _log(f"standby: port {s.port} is held by another nncq ({_owner_desc()}); answering queries only")
            announced = True
        _stop.wait(15)


def _shutdown() -> None:
    _stop.set()
    for name, proc in list(_workers.items()):
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    for w in list(_watchers.values()):
        try:
            w.obs.stop()
        except Exception:
            pass
    try:
        cur = json.loads(config.SERVER_FILE.read_text(encoding="utf-8"))
        if cur.get("pid") == os.getpid():
            config.SERVER_FILE.unlink()
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ entry points

def _serve_stdio(mcp: FastMCP) -> None:
    """MCP owns the real stdout; anything else printed (ours, libraries, C code) goes to the log file."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    proto = os.fdopen(os.dup(1), "wb")
    logf = open(config.LOG_DIR / "server.log", "a", encoding="utf-8", buffering=1)
    os.dup2(logf.fileno(), 1)
    sys.stdout = logf
    _log(f"--- nncq stdio MCP server, pid {os.getpid()}")
    threading.Thread(target=_claim_loop, daemon=True, name="claim").start()

    async def main():
        from mcp.server.stdio import stdio_server
        out = anyio.wrap_file(io.TextIOWrapper(proto, encoding="utf-8"))
        async with stdio_server(stdout=out) as (rd, wr):
            await mcp._mcp_server.run(rd, wr, mcp._mcp_server.create_initialization_options())
    try:
        anyio.run(main)
    finally:
        _log(f"MCP client went away: shutting down (pid {os.getpid()})")
        _shutdown()
        logf.flush()
        os._exit(0)                 # daemon threads, watchers, the web server: all gone with the process


def serve(stdio: bool = False) -> None:
    s = config.settings()
    if not config.is_loopback(s.host) and not s.token:
        raise SystemExit("NNCQ_TOKEN is required when NNCQ_HOST is not loopback")
    _info["mode"] = "stdio" if stdio else "http"
    mcp = build(stdio)
    if stdio:
        _serve_stdio(mcp)
        return
    sock = _bind(s.host, s.port)
    if sock is None:
        raise SystemExit(f"port {s.port} is in use ({_owner_desc()}). Stop that first, or set NNCQ_PORT.")
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    _info["mcp_url"] = f"http://{host}:{s.port}/mcp"
    _become_owner(None)
    from nncq import dashboard
    app = mcp.streamable_http_app()
    app.router.routes.extend([_health_route(), *dashboard.routes(snapshot, control, web_tool)])
    print(f"nncq MCP server on {_info['mcp_url']}, dashboard {_info['dashboard']}", flush=True)
    try:
        _uvicorn(_token_gate(app, s.token), sock)
    finally:
        _shutdown()
