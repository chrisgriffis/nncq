"""Command line: nncq <command>. Run `nncq -h` for the list."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from nncq import config


def _p(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    else:
        print(obj)


def _repo(name: str):
    from nncq import repos
    try:
        return repos.get(name)
    except KeyError as e:
        raise SystemExit(str(e).strip("'\""))


def _store(name: str):
    from nncq.store import Store
    r = _repo(name)
    if not r.db.exists():
        raise SystemExit(f"repo '{r.name}' is not indexed yet: nncq index <path>")
    return r, Store(r.db, cache_mb=64)


def _server() -> dict | None:
    from nncq import sysutil
    try:
        s = json.loads(config.SERVER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if sysutil.pid_alive(s.get("pid", 0)) else None


def _spawn_worker(r) -> str:
    from nncq import sysutil, worker
    ws = worker.read_status(r)
    if ws and ws["alive"]:
        return f"embedding worker already running (pid {ws['pid']})"
    if config.settings().embed_tier <= 0:
        return "embedding disabled (NNCQ_EMBED_TIER=0)"
    p = sysutil.spawn([sys.executable, "-m", "nncq", *config.home_args(), "work", "--repo", r.name],
                      config.LOG_DIR / f"worker-{r.name}.log", detach=True)
    return f"embedding worker started in the background (pid {p.pid}); `nncq status`, `nncq pause`"


# ------------------------------------------------------------------ commands

def cmd_init(a):
    from nncq import init
    return init.main(a)


def cmd_index(a):
    from nncq import indexer, repos
    if a.path:
        r = repos.add(Path(a.path), a.name, [d for d in a.dirs.split(",") if d.strip()] if a.dirs is not None else None)
    else:
        r = _repo(a.name)
    started = [a.no_embed or bool(_server())]

    def log(msg):
        print(msg, flush=True)
        if not started[0] and "files (" in msg:     # first batches committed: embed alongside the parse
            started[0] = True
            print(_spawn_worker(r), flush=True)
    res = indexer.update(r, full=a.fresh, data=False if a.no_data else None, log=log)
    _p(res)
    if not started[0]:
        print(_spawn_worker(r))
    elif _server():
        print("server running: it starts the embedding worker")
    return 0


def cmd_update(a):
    from nncq import indexer
    _p(indexer.update(_repo(a.repo)))
    return 0


def cmd_work(a):
    from nncq import worker
    _p(worker.run(_repo(a.repo), once=a.once, max_batches=a.max_batches, parent=a.parent))
    return 0


def cmd_serve(a):
    from nncq import server
    server.serve(stdio=a.stdio)
    return 0


def cmd_start(a):
    from nncq import sysutil
    s = _server()
    if s:
        print(f"already running ({s.get('mode', 'http')}, pid {s['pid']}): MCP {s['url'] or 'stdio'}, dashboard {s.get('dashboard', '-')}")
        return 0
    p = sysutil.spawn([sys.executable, "-m", "nncq", *config.home_args(), "serve"], config.LOG_DIR / "server.log", detach=True)
    for _ in range(60):
        time.sleep(0.5)
        s = _server()
        if s:
            print(f"started: {s['url']} (pid {s['pid']}); log: {config.LOG_DIR / 'server.log'}")
            return 0
        if p.poll() is not None:
            break
    print(f"server did not start; see {config.LOG_DIR / 'server.log'}")
    return 1


def cmd_stop(a):
    from nncq import repos, sysutil, worker
    s = _server()
    if s and s.get("mode") == "stdio" and not a.force:
        print(f"server (pid {s['pid']}) is owned by an MCP client over stdio and exits with it; --force to kill anyway")
    elif s:
        sysutil.kill(s["pid"])
        print(f"stopped server (pid {s['pid']})")
    for r in repos.load().values():
        ws = worker.read_status(r)
        if ws and ws["alive"] and not (s and s.get("mode") == "stdio" and not a.force):
            sysutil.kill(ws["pid"])
            print(f"stopped embedding worker for {r.name} (pid {ws['pid']})")
    return 0


def cmd_status(a):
    from nncq import health, indexer, repos, worker
    from nncq.store import Store
    s = _server()
    print(f"server: {s.get('mode', 'http')} pid {s['pid']}; MCP {s['url'] or '(stdio)'}; dashboard {s.get('dashboard', '-')}"
          if s else "server: not running")
    print("embedding: off (lite mode)" if config.settings().lite else
          f"embedding: {'PAUSED' if worker.paused() else 'enabled'} (tier {config.settings().embed_tier})")
    for r in (repos.load().values() if not a.repo else [_repo(a.repo)]):
        st = indexer.status(r)
        ws = worker.read_status(r)
        if ws:
            st["worker"] = {k: v for k, v in ws.items() if k in ("alive", "state", "embedded", "chunks_per_s",
                                                                   "eta_this_tier_min", "cards_pending", "full_pending")}
        if r.db.exists():
            db = Store(r.db, cache_mb=8)
            try:
                hs, _ = health.compute(db, config.settings().embed_tier)
            finally:
                db.close()
            if hs:
                st["index_state"] = hs["degraded"]
        _p(st)
    return 0


def cmd_pause(a):
    from nncq import worker
    worker.set_paused(True)
    print("paused: the embedding worker finishes its batch and exits (VRAM released)")
    return 0


def cmd_resume(a):
    from nncq import repos, worker
    worker.set_paused(False)
    print("resumed")
    if not _server():
        for r in (repos.load().values() if not a.repo else [_repo(a.repo)]):
            if r.db.exists():
                print(f"{r.name}: {_spawn_worker(r)}")
    return 0


def cmd_search(a):
    from nncq import search
    r, st = _store(a.repo)
    try:
        res = search.search(st, a.query, a.k, a.path, a.lang, max_chars=a.chars)
    finally:
        st.close()
    if a.json:
        _p(res)
        return 0
    print(f"[{res.get('mode')}] {res.get('ms', 0)} ms" + (f"  ({res['note']})" if res.get("note") else ""))
    for i, x in enumerate(res["results"], 1):
        print(f"\n{i}. {x['path']}:{x['lines']}  {x['kind']} {x['symbol']}  ({x['score']})")
        print("   " + "\n   ".join(x["code"].splitlines()[:a.lines]))
    return 0


def cmd_graph(a):
    from nncq import graph, health, search
    r, st = _store(a.repo)
    v, x = a.view, a.arg
    try:
        fn = {
            "context": lambda: graph.symbol_context(st, x, a.path),
            "calls": lambda: graph.calls(st, x, a.path),
            "called-by": lambda: graph.called_by(st, x, a.path, a.k),
            "tree": lambda: graph.call_tree(st, x, a.direction, a.depth, path=a.path),
            "trace": lambda: graph.call_trace(st, x, a.frm, a.depth if a.depth != 3 else 8),
            "impact": lambda: graph.impact(st, x, a.depth),
            "hierarchy": lambda: graph.type_hierarchy(st, x),
            "file": lambda: graph.file_context(st, x),
            "outline": lambda: search.file_outline(st, x),
            "hotspots": lambda: graph.hotspots(st, x or "called", a.path, a.k),
            "symbol": lambda: search.find_symbol(st, x, path=a.path),
            "refs": lambda: search.find_references(st, x, a.path, a.k),
        }[v]
        res = fn()
        health.annotate(res, st, None, config.settings().embed_tier, semantic=False)
    finally:
        st.close()
    if isinstance(res, dict) and "tree" in res:
        print(res.pop("tree"))
    _p(res)
    return 0


def cmd_data(a):
    from nncq import datarefs
    r, st = _store(a.repo)
    try:
        res = {
            "top": lambda: datarefs.hot_data(st, a.k, a.arg or "", a.group),
            "file": lambda: datarefs.data_file(st, a.arg, a.k),
            "uses": lambda: datarefs.data_for_code(st, a.arg, a.k),
            "code": lambda: datarefs.code_for_data(st, a.arg, a.k),
        }[a.view]()
    finally:
        st.close()
    _p(res)
    return 0


def cmd_profile(a):
    from nncq import profiles
    if a.action == "list":
        for p in profiles.load_all().values():
            print(f"{p.name:16} {p.root or '(workspace not found on this machine)'}  {p.path}")
            if p.root is None:
                for c in p.roots:
                    print(f"{'':16}   {c}: {p.accepts(profiles.candidate(c)) or 'ok'}")
        return 0
    if a.action == "new":
        from nncq import init
        return 0 if init.profile_wizard(a.name, a.path, ask=sys.stdin.isatty()) else 1
    if a.action == "show":
        p = profiles.get(a.name)
        if p is None:
            raise SystemExit(f"no profile {a.name!r} in {config.PROFILES_DIR}")
        print(p.path.read_text(encoding="utf-8"))
        print(f"# workspace on this machine: {p.root or 'not found'}")
        return 0
    if a.action == "export":
        names = [a.name] if a.name else list(profiles.load_all())
        for n in names:
            print(profiles.export(n, Path(a.path) if a.path else None))
        return 0
    if a.action == "import":
        if not a.name:
            raise SystemExit("nncq profile import <file> [--as name] [--force]")
        print(profiles.import_(Path(a.name), a.as_name, a.force))
        return 0
    raise SystemExit(f"unknown action {a.action}")


def cmd_agents(a):
    from nncq import agents
    t = [x.strip() for x in a.targets.split(",") if x.strip()] or None
    return agents.run(t, ask=sys.stdin.isatty())


def cmd_mcp_config(a):
    s = config.settings()
    url = f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}/mcp"
    hdr = {"Authorization": f"Bearer {s.token}"} if s.token else None
    exe = str(Path(sys.executable).parent / ("nncq.exe" if sys.platform == "win32" else "nncq"))
    http = {"type": "http", "url": url, "tools": ["*"], **({"headers": hdr} if hdr else {})}
    args = [*config.home_args(), "serve", "--stdio"]
    stdio = {"type": "local", "command": exe, "args": args, "tools": ["*"]}
    print("# Copilot CLI (recommended): ~/.copilot/mcp-config.json  (or: nncq init --copilot)")
    print("# Copilot starts nncq over stdio; nncq, its embedding worker and the dashboard exit with Copilot.")
    _p({"mcpServers": {"nncq": stdio}})
    print("\n# VS Code: .vscode/mcp.json")
    _p({"servers": {"nncq": {"type": "stdio", "command": exe, "args": args}}})
    print("\n# Claude Code:  claude mcp add --scope user nncq -- " + " ".join([exe, *args]))
    print("\n# Visual Studio (Copilot agent mode): %USERPROFILE%\\.mcp.json")
    _p({"servers": {"nncq": {"type": "stdio", "command": exe, "args": args}}})
    print("\n# Or let nncq do it, MCP server plus the nncq-user skill:  nncq agents")
    print(f"\n# Dashboard: open http://127.0.0.1:{s.port}/ while an MCP client has nncq running (starts on first visit)")
    print("\n# Shared HTTP server instead (run `nncq start` yourself; several clients can attach):")
    _p({"mcpServers": {"nncq": http}})
    return 0


def cmd_repos(a):
    from nncq import repos
    if a.remove:
        repos.remove(a.remove)
    for r in repos.load().values():
        print(f"{r.name:20} {r.root}" + (f"  [{', '.join(r.dirs)}]" if r.dirs else "")
              + ("" if r.db.exists() else "  (not indexed)"))
    return 0


def cmd_doctor(a):
    from nncq import sysutil, walk
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else '!!'}] {what}" + (f": {detail}" if detail else ""))
    line(sys.version_info[:2] >= (3, 11), "python", sys.version.split()[0])
    if config.settings().lite:
        line(True, "mode", "lite: torch, models and GPU not needed")
    else:
        _doctor_models(line)
    try:
        line(True, "ripgrep", walk.rg_exe())
    except Exception as e:
        line(False, "ripgrep", str(e))
    if not config.settings().lite:
        free = sysutil.gpu_free_mb()
        line(True, "gpu free", f"{free} MB" if free is not None else "unknown (nvidia-smi not found)")
    s = _server()
    line(True, "server", s["url"] if s else "not running (nncq start)")
    return 0 if ok else 1


def _doctor_models(line):
    from nncq import models
    try:
        import torch
        line(True, "torch", f"{torch.__version__}, cuda={torch.cuda.is_available()}"
             + (f" ({torch.cuda.get_device_name(0)})" if torch.cuda.is_available() else ""))
    except Exception as e:
        line(False, "torch", str(e))
    miss = models.models_present()
    line(not miss, "models", "present" if not miss else f"missing {miss}: nncq init")


# ------------------------------------------------------------------ parser

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nncq", description="Local code index for AI agents (no network).")
    ap.add_argument("--home", default="", help="nncq home: its profiles/, cache/db, logs and .env (default: the nncq "
                                               "folder; the bundled demo is demo/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="first-time setup: .env, models, parsers, workspace, Copilot config")
    p.add_argument("--seed-models", default="", help="HF hub cache that already has the models (copy, don't download)")
    p.add_argument("--yes", action="store_true", help="accept defaults")
    p.add_argument("--workspace", default="", help="workspace root to index (read-only)")
    p.add_argument("--dirs", default=None, help="code subfolders, comma-separated (empty = everything)")
    p.add_argument("--name", default="", help="profile / index name (default: folder name)")
    p.add_argument("--profile", default="", help="use this existing profile (profiles/<name>)")
    p.add_argument("--index", action="store_true", help="start indexing at the end")
    p.add_argument("--copilot", action="store_true", help="same as --agents copilot")
    p.add_argument("--agents", default="", help="set up these agents without asking: copilot,claude,vs")
    p.add_argument("--lite", action="store_true", help="lite mode: no torch, no models, no GPU (keyword + graph)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("agents", help="set up agents: MCP server + nncq-user skill (Copilot CLI, Claude Code, VS)")
    p.add_argument("--targets", default="", help="copilot,claude,vs (default: detect and ask)")
    p.set_defaults(fn=cmd_agents)

    p = sub.add_parser("index", help="register a workspace and build/refresh its index")
    p.add_argument("path", nargs="?", default="")
    p.add_argument("--name", default="")
    p.add_argument("--dirs", default=None, help="comma-separated code subfolders, e.g. src,tools")
    p.add_argument("--fresh", action="store_true", help="re-parse everything (never needed for correctness)")
    p.add_argument("--no-data", action="store_true", help="skip the profile's data cross-reference")
    p.add_argument("--no-embed", action="store_true", help="don't start the embedding worker afterwards")
    p.set_defaults(fn=cmd_index)

    for name, fn, h in (("update", cmd_update, "re-index files changed on disk, e.g. after a pull or sync (stat delta)"),):
        p = sub.add_parser(name, help=h)
        p.add_argument("--repo", default="")
        p.set_defaults(fn=fn)

    p = sub.add_parser("work", help="run the embedding worker in the foreground")
    p.add_argument("--repo", default="")
    p.add_argument("--once", action="store_true")
    p.add_argument("--max-batches", type=int, default=0)
    p.add_argument("--parent", type=int, default=0, help="exit when this process exits (no orphans)")
    p.set_defaults(fn=cmd_work)

    p = sub.add_parser("serve", help="run the MCP server in the foreground")
    p.add_argument("--stdio", action="store_true")
    p.set_defaults(fn=cmd_serve)

    for name, fn, h in (("start", cmd_start, "start the shared HTTP MCP server in the background"),
                        ("mcp-config", cmd_mcp_config, "print MCP client configuration"),
                        ("doctor", cmd_doctor, "check the installation")):
        sub.add_parser(name, help=h).set_defaults(fn=fn)
    p = sub.add_parser("stop", help="stop the server and embedding workers")
    p.add_argument("--force", action="store_true", help="also kill a server owned by an MCP client over stdio")
    p.set_defaults(fn=cmd_stop)

    for name, fn, h in (("status", cmd_status, "index, embedding and server status"),
                        ("pause", cmd_pause, "pause embedding (frees VRAM)"),
                        ("resume", cmd_resume, "resume embedding")):
        p = sub.add_parser(name, help=h)
        p.add_argument("--repo", default="")
        p.set_defaults(fn=fn)

    p = sub.add_parser("search", help="hybrid search from the command line")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--path", default="")
    p.add_argument("--lang", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--lines", type=int, default=6, help="lines of code shown per hit")
    p.add_argument("--chars", type=int, default=1500)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("graph", help="symbol graph views")
    p.add_argument("view", choices=["context", "calls", "called-by", "tree", "trace", "impact", "hierarchy",
                                    "file", "outline", "hotspots", "symbol", "refs"])
    p.add_argument("arg", nargs="?", default="")
    p.add_argument("--from", dest="frm", default="", help="trace: the other function")
    p.add_argument("--path", default="")
    p.add_argument("--direction", default="in")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("-k", type=int, default=30)
    p.add_argument("--repo", default="")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("data", help="data cross-reference (profiles with [[xref]] rules)")
    p.add_argument("view", choices=["top", "file", "uses", "code"])
    p.add_argument("arg", nargs="?", default="")
    p.add_argument("-k", type=int, default=25)
    p.add_argument("--group", type=int, default=0)
    p.add_argument("--repo", default="")
    p.set_defaults(fn=cmd_data)

    p = sub.add_parser("profile", help="codebase profiles: list | new | show | export | import")
    p.add_argument("action", choices=["list", "new", "show", "export", "import"])
    p.add_argument("name", nargs="?", default="", help="profile name (import: the archive file)")
    p.add_argument("path", nargs="?", default="", help="new: workspace root; export: output file/dir")
    p.add_argument("--as", dest="as_name", default="", help="import under another name")
    p.add_argument("--force", action="store_true", help="import: overwrite an existing profile")
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("repos", help="list registered workspaces")
    p.add_argument("--remove", default="")
    p.set_defaults(fn=cmd_repos)

    from nncq.uml import cli as uml_cli
    uml_cli.add_parser(sub)

    a = ap.parse_args(argv)
    if a.home:
        home = Path(a.home).expanduser()
        if not home.is_dir():
            raise SystemExit(f"--home {a.home}: not a directory")
        config.use_home(home)
    for f in (sys.stdout, sys.stderr):
        if hasattr(f, "reconfigure"):
            f.reconfigure(encoding="utf-8", errors="replace")
    return a.fn(a) or 0
