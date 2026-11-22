"""`nncq init`: first-time setup. Writes .env, puts the models in cache/models/hf (download or copy from
--seed-models), pre-fetches the tree-sitter parsers (so nothing touches the network afterwards), registers the
workspace to index and sets up the agents on this machine (MCP server + nncq-user skill, see agents.py).

Profiles: full (default) = embeddings + reranker on an NVIDIA GPU; lite (--lite, NNCQ_PROFILE=lite) = no torch, no
models, no GPU: keyword search, symbol graph and .xdata cross-reference only. An index built in lite works unchanged
in full: switch the profile, install the gpu extra, and the worker embeds what is already there."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from nncq import config

QUESTIONS = [
    ("NNCQ_PORT", "MCP server port", "8765"),
    ("NNCQ_PROFILE", "Profile: full = embeddings + reranker (NVIDIA GPU) | lite = keyword + graph only, no GPU", "full"),
    ("NNCQ_EMBED_TIER", "Embeddings: 2 = cards then full chunks (best), 1 = cards only (faster), 0 = none", "2"),
    ("NNCQ_BG_WHEN", "Embed in the background: always | idle (only when you are away from the keyboard)", "always"),
    ("NNCQ_GPU_MIN_FREE_MB", "Leave the GPU alone when it has less free VRAM than this (0 = ignore)", "0"),
    ("NNCQ_DATA", "engine: build the .xdata code<->data cross-reference (true/false)", "true"),
    ("NNCQ_WATCH", "Watch indexed folders and re-index on save (true/false)", "true"),
]


GPU_ONLY = {"NNCQ_EMBED_TIER", "NNCQ_BG_WHEN", "NNCQ_GPU_MIN_FREE_MB"}


def _set_env(key: str, val: str) -> None:
    cur = config.read_env_file()
    if cur.get(key) == val:
        return
    lines = config.ENV_FILE.read_text(encoding="utf-8").splitlines() if config.ENV_FILE.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(key + "=")] + [f"{key}={val}"]
    config.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_env(yes: bool, lite: bool = False) -> None:
    cur = config.read_env_file()
    if config.ENV_FILE.exists() and yes:
        print(f"== keeping {config.ENV_FILE}")
        return
    print("== settings (Enter = default; edit .env later; .env.example lists everything)")
    vals = {}
    for k, q, d in QUESTIONS:
        dv = "lite" if (k == "NNCQ_PROFILE" and lite) else cur.get(k, d)
        if k in GPU_ONLY and vals.get("NNCQ_PROFILE") == "lite":
            continue
        v = dv if yes or not sys.stdin.isatty() else (input(f"  {q} [{dv}]: ").strip() or dv)
        vals[k] = v
    keep = {k: v for k, v in cur.items() if k not in vals}
    lines = ["# nncq settings (see .env.example for all options)"]
    lines += [f"{k}={v}" for k, v in {**vals, **keep}.items()]
    config.ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   wrote {config.ENV_FILE}")


def _models(seed: str) -> bool:
    from nncq import models
    miss = models.models_present()
    if not miss:
        print("== models: present")
        return True
    hub = config.HF_HOME / "hub"
    if seed:
        src = Path(seed)
        src = src / "hub" if (src / "hub").is_dir() else src
        for name in miss:
            d = "models--" + name.replace("/", "--")
            if (src / d).is_dir():
                print(f"== copying {name} from {src}")
                shutil.copytree(src / d, hub / d, dirs_exist_ok=True)
        miss = models.models_present()
        if not miss:
            return True
        print(f"   not in seed: {miss}")
    print(f"== downloading {', '.join(miss)} (~5 GB, the only network step)")
    if models.fetch() != 0:
        print("!! model download failed; re-run `nncq init` or pass --seed-models <hf cache>")
        return False
    return not models.models_present()


def _parsers() -> bool:
    from nncq.langs import AST_LANG_BY_EXT
    import tree_sitter_language_pack as tslp
    tslp.configure(tslp.PackConfig(cache_dir=str(config.TS_CACHE)))
    bad = []
    for lang in sorted(set(AST_LANG_BY_EXT.values())):
        try:
            tslp.get_parser(lang)
        except Exception as e:
            bad.append(f"{lang} ({type(e).__name__})")
    print("== parsers: " + ("ready" if not bad else f"unavailable: {', '.join(bad)} (those files use line windows)"))
    return True


def _workspace(a) -> "object | None":
    """Register the workspace to index (read-only: nncq never writes into it)."""
    from nncq import repos
    cur = list(repos.load().values())
    ask = not a.yes and sys.stdin.isatty()
    path = a.workspace or (input(f"  Workspace root to index [{cur[0].root if cur else 'skip'}]: ").strip()
                           if ask else "") or (str(cur[0].root) if cur else "")
    if not path:
        print("== workspace: none registered (later: nncq index <path>)")
        return None
    root = Path(path).expanduser()
    if not root.is_dir():
        print(f"!! not a directory: {root}")
        return None
    old = next((r for r in cur if r.root.resolve() == root.resolve()), None)
    dv = ",".join(old.dirs) if old else ""
    dirs = a.dirs if a.dirs is not None else (
        input(f"  Code folders under it, comma-separated (empty = everything) [{dv}]: ").strip() or dv if ask else dv)
    name = a.name or (old.name if old else repos.default_name(root))
    r = repos.add(root, name, [d for d in dirs.split(",") if d.strip()])
    print(f"== workspace: {r.name} = {r.root}" + (f" [{', '.join(r.dirs)}]" if r.dirs else " (everything)")
          + "\n   read-only: files are listed and read; the index lives in cache/db/" + r.name)
    return r


def main(a) -> int:
    for d in (config.DATA_DIR, config.LOG_DIR, config.TOOLS_DIR, config.HF_HOME):
        d.mkdir(parents=True, exist_ok=True)
    _write_env(a.yes, a.lite)
    if a.lite:
        _set_env("NNCQ_PROFILE", "lite")
    os.environ.update({k: v for k, v in config.read_env_file().items() if v})
    lite = config.settings().lite
    if lite:
        print("== profile: lite (no torch, no models, no GPU): keyword search + symbol graph + data cross-reference")
        ok = True
    else:
        ok = _models(a.seed_models)
    _parsers()
    r = _workspace(a)
    from nncq import agents
    targets = [t for t in (a.agents or "").split(",") if t.strip()] or (["copilot"] if a.copilot else None)
    agents.run(targets, ask=not a.yes and sys.stdin.isatty())
    what = "CPU parse" if lite else "CPU parse, GPU embedding alongside"
    if r is not None and (a.index or (not a.yes and sys.stdin.isatty() and not r.db.exists() and input(
            f"  Start indexing now ({what})? [Y/n]: ").strip().lower() in ("", "y", "yes"))):
        from nncq import cli
        cli.main(["index", "--name", r.name])
    print("\nNext:\n  .venv\\Scripts\\nncq index                 (build / refresh the index"
          + ("" if lite else "; embedding runs alongside") + ")"
          "\n  copilot                                    (starts nncq itself; dashboard on http://127.0.0.1:"
          f"{config.settings().port}/)"
          "\n  .venv\\Scripts\\nncq agents               (set up Copilot CLI / Claude Code / Visual Studio again)"
          "\n  .venv\\Scripts\\nncq mcp-config            (for other agents)")
    return 0 if ok else 1
