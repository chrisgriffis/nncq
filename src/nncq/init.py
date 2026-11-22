"""`nncq init`: first-time setup. Writes .env, puts the models in cache/models/hf (download or copy from
--seed-models), pre-fetches the tree-sitter parsers (so nothing touches the network afterwards), registers the
workspace to index as a codebase profile (profiles/<name>/profile.toml, see profiles.py) and sets up the agents on
this machine (MCP server + nncq-user skill, see agents.py).

Modes: full (default) = embeddings + reranker on an NVIDIA GPU; lite (--lite, NNCQ_MODE=lite) = no torch, no models,
no GPU: keyword search, symbol graph and data cross-reference only. An index built in lite works unchanged in full:
switch the mode, install the gpu extra, and the worker embeds what is already there."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from nncq import config

QUESTIONS = [
    ("NNCQ_PORT", "MCP server port", "8765"),
    ("NNCQ_MODE", "Mode: full = embeddings + reranker (NVIDIA GPU) | lite = keyword + graph only, no GPU", "full"),
    ("NNCQ_EMBED_TIER", "Embeddings: 2 = cards then full chunks (best), 1 = cards only (faster), 0 = none", "2"),
    ("NNCQ_BG_WHEN", "Embed in the background: always | idle (only when you are away from the keyboard)", "always"),
    ("NNCQ_GPU_MIN_FREE_MB", "Leave the GPU alone when it has less free VRAM than this (0 = ignore)", "0"),
    ("NNCQ_DATA", "Build the data<->code cross-reference for profiles that declare [[xref]] rules (true/false)", "true"),
    ("NNCQ_WATCH", "Watch indexed folders and re-index on save (true/false)", "true"),
]


GPU_ONLY = {"NNCQ_EMBED_TIER", "NNCQ_BG_WHEN", "NNCQ_GPU_MIN_FREE_MB"}
QUESTIONS.append(("NNCQ_UML", "Build the UML/SysML model at index time (classes, components, packages; else later "
                              "with `nncq uml init`) (true/false)", "true"))


def _uml() -> None:
    """The pinned Mermaid build for the dashboard's diagrams (the only network step for UML)."""
    from nncq.uml import vendor
    vendor.fetch()


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
        dv = "lite" if (k == "NNCQ_MODE" and lite) else cur.get(k, cur.get("NNCQ_PROFILE", d) if k == "NNCQ_MODE" else d)
        if k in GPU_ONLY and vals.get("NNCQ_MODE") == "lite":
            continue
        v = dv if yes or not sys.stdin.isatty() else (input(f"  {q} [{dv}]: ").strip() or dv)
        vals[k] = v
    keep = {k: v for k, v in cur.items() if k not in vals and k != "NNCQ_PROFILE"}
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


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def profile_wizard(name: str, path: str, ask: bool, dirs: str | None = None) -> "object | None":
    """Link a codebase profile to its workspace on this machine, creating the profile when there is none.
    A new profile gets the workspace part (roots, markers, git remote, dirs, ignores) filled in; DSLs, extra
    extensions and [[xref]] data rules are edited in profiles/<name>/profile.toml (the template documents them)."""
    from nncq import profiles, repos
    p = profiles.get(name) if name else None
    root = Path(os.path.expandvars(path)).expanduser() if path else None
    if p is not None:
        if root is not None:
            why = p.accepts(root)
            if why:
                print(f"!! {root} is not the '{p.name}' workspace: {why}")
                return None
            if p.root is None or p.root != root.resolve():
                profiles.add_root(p, root.resolve())
                print(f"   remembered {root} in {p.path}")
            p = profiles.get(p.name)
        if p.root is None:
            print(f"!! profile '{p.name}': workspace not found on this machine. Pass its path "
                  f"(nncq profile new {p.name} <path>) or set NNCQ_WORKSPACE_{p.name.upper()}")
            for c in p.roots:
                print(f"     {c}: {p.accepts(Path(os.path.expandvars(c)).expanduser())}")
            return None
        r = repos.get(p.name)
        print(f"== workspace: profile '{p.name}' -> {r.root}" + (f" [{', '.join(r.dirs)}]" if r.dirs else ""))
        return r
    if root is None and ask:
        root = Path(input("  Workspace root to index (outside this folder) [skip]: ").strip() or ".").expanduser()
        root = None if str(root) == "." else root
    if root is None:
        print("== workspace: none (later: nncq profile new <name> <path>)")
        return None
    if not root.is_dir():
        print(f"!! not a directory: {root}")
        return None
    if profiles.nested(root):
        print(f"!! {root} overlaps {config.ROOT}: workspaces live outside the nncq folder")
        return None
    sg = profiles.suggest(root)
    name = name or repos.default_name(root)
    if ask:
        name = input(f"  Profile name [{name}]: ").strip() or name
    dv = ",".join(sg["dirs"])
    if dirs is None:
        dirs = (input(f"  Code folders, comma-separated (empty = everything) [{dv}]: ").strip() or dv) if ask else dv
    ig = ",".join(f"{s}/**" for s in sg["skipped"])
    ig = (input(f"  Ignore (globs, comma-separated) [{ig}]: ").strip() or ig) if ask else ig
    dst = profiles.write(name, root.resolve(), _split(dirs), _split(ig), markers=sg["markers"], remote=sg["remote"])
    print(f"== profile '{name}' written: {dst}\n   workspace {root.resolve()}"
          + (f" [{dirs}]" if dirs else " (everything)") + (f", ignoring {ig}" if ig else "")
          + "\n   add file types, DSLs and [[xref]] data rules there (documented in profiles/_template/profile.toml)")
    return repos.get(name)


def _workspace(a) -> "object | None":
    """Pick or create the codebase profile to index (read-only: nncq never writes into the workspace)."""
    from nncq import profiles
    ask = not a.yes and sys.stdin.isatty()
    name = a.profile or a.name
    if not name and not a.workspace:
        found = profiles.load_all()
        if found:
            print("== profiles: " + ", ".join(f"{n} ({p.root or 'workspace not found'})" for n, p in found.items()))
            dv = next((n for n, p in found.items() if p.root), "")
            name = (input(f"  Profile to use, or 'new' [{dv or 'new'}]: ").strip() or dv or "new") if ask else dv
            name = "" if name == "new" else name
    return profile_wizard(name, a.workspace, ask, a.dirs)


def main(a) -> int:
    for d in (config.DATA_DIR, config.LOG_DIR, config.TOOLS_DIR, config.HF_HOME):
        d.mkdir(parents=True, exist_ok=True)
    _write_env(a.yes, a.lite)
    if a.lite:
        _set_env("NNCQ_MODE", "lite")
    os.environ.update({k: v for k, v in config.read_env_file().items() if v})
    lite = config.settings().lite
    if lite:
        print("== mode: lite (no torch, no models, no GPU): keyword search + symbol graph + data cross-reference")
        ok = True
    else:
        ok = _models(a.seed_models)
    _parsers()
    _uml()
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
