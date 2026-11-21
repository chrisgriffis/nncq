"""`nncq init`: first-time setup. Writes .env, puts the models in cache/models/hf (download or copy from
--seed-models), pre-fetches the tree-sitter parsers (so nothing touches the network afterwards), registers the
workspace to index and adds nncq to Copilot CLI."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from nncq import config

QUESTIONS = [
    ("NNCQ_PORT", "MCP server port", "8765"),
    ("NNCQ_EMBED_TIER", "Embeddings: 2 = cards then full chunks (best), 1 = cards only (faster), 0 = none", "2"),
    ("NNCQ_BG_WHEN", "Embed in the background: always | idle (only when you are away from the keyboard)", "always"),
    ("NNCQ_GPU_MIN_FREE_MB", "Leave the GPU alone when it has less free VRAM than this (0 = ignore)", "0"),
    ("NNCQ_DATA", "engine: build the .xdata code<->data cross-reference (true/false)", "true"),
    ("NNCQ_WATCH", "Watch indexed folders and re-index on save (true/false)", "true"),
]


def _write_env(yes: bool) -> None:
    cur = config.read_env_file()
    if config.ENV_FILE.exists() and yes:
        print(f"== keeping {config.ENV_FILE}")
        return
    print("== settings (Enter = default; edit .env later; .env.example lists everything)")
    vals = {}
    for k, q, d in QUESTIONS:
        dv = cur.get(k, d)
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


def _copilot(a) -> None:
    """Add nncq to Copilot CLI as a stdio server: Copilot starts it and it exits with Copilot."""
    cfg = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")) / "mcp-config.json"
    exe = Path(sys.executable).parent / ("nncq.exe" if os.name == "nt" else "nncq")
    entry = {"type": "local", "command": str(exe), "args": ["serve", "--stdio"], "tools": ["*"]}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except ValueError:
        print(f"!! {cfg} is not valid JSON; add this yourself:\n   \"nncq\": {json.dumps(entry)}")
        return
    if data.get("mcpServers", {}).get("nncq") == entry:
        print(f"== Copilot CLI: already configured ({cfg})")
        return
    ok = a.copilot or (not a.yes and sys.stdin.isatty()
                       and input(f"  Add nncq to Copilot CLI ({cfg})? [Y/n]: ").strip().lower() in ("", "y", "yes"))
    if not ok:
        return
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_suffix(".json.bak-nncq"))
    data.setdefault("mcpServers", {})["nncq"] = entry
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"== Copilot CLI: added nncq (stdio) to {cfg}; restart Copilot or run /mcp")


def main(a) -> int:
    for d in (config.DATA_DIR, config.LOG_DIR, config.TOOLS_DIR, config.HF_HOME):
        d.mkdir(parents=True, exist_ok=True)
    _write_env(a.yes)
    os.environ.update({k: v for k, v in config.read_env_file().items() if v})
    ok = _models(a.seed_models)
    _parsers()
    r = _workspace(a)
    _copilot(a)
    if r is not None and (a.index or (not a.yes and sys.stdin.isatty() and not r.db.exists() and input(
            "  Start indexing now (CPU parse, GPU embedding alongside)? [Y/n]: ").strip().lower() in ("", "y", "yes"))):
        from nncq import cli
        cli.main(["index", "--name", r.name])
    print("\nNext:\n  .venv\\Scripts\\nncq index                 (build / refresh the index; embedding runs alongside)"
          "\n  copilot                                    (starts nncq itself; dashboard on http://127.0.0.1:"
          f"{config.settings().port}/)"
          "\n  .venv\\Scripts\\nncq mcp-config            (for other agents)")
    return 0 if ok else 1
