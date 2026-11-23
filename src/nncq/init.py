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
import re
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


def assets() -> None:
    """Install-wide, non-interactive setup shared by every home: tree-sitter parsers and the pinned Mermaid build.
    Idempotent (present = no network). nncq init runs it; so does the bundled demo (demo/run.py) on every start."""
    _parsers()
    _uml()


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _path(s: str) -> Path | None:
    """A typed or pasted path; surrounding quotes (Explorer's "Copy as path") and whitespace are dropped."""
    s = (s or "").strip().strip("\"'").strip()
    return Path(os.path.expandvars(s)).expanduser() if s else None


def _new_root(root: Path) -> str:
    """'' if root can be a new profile's workspace, else why not."""
    from nncq import profiles
    if not root.is_dir():
        return f"not a directory: {root}"
    if profiles.nested(root):
        return f"{root} overlaps {config.ROOT}: workspaces live outside the nncq folder"
    return ""


def _ask_dir(prompt: str, check) -> Path | None:
    """Ask until check(path) passes ('') or the answer is empty (skip)."""
    while True:
        root = _path(input(prompt))
        if root is None:
            return None
        why = check(root)
        if not why:
            return root
        print(f"    {why}  (Enter to skip)")


def _ask_name(dv: str) -> str:
    from nncq import profiles
    while True:
        n = input(f"  Profile name, the label for this codebase [{dv}]: ").strip() or dv
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", n):
            print("    letters, digits, _ . - only")
        elif profiles.get(n) is not None:
            print(f"    profile '{n}' exists; pick another name")
        else:
            return n


def profile_wizard(name: str, path: str, ask: bool, dirs: str | None = None) -> "object | None":
    """Link a codebase profile to its workspace on this machine, creating the profile when there is none.
    A new profile gets the workspace part (roots, markers, git remote, dirs, ignores) filled in; DSLs, extra
    extensions and [[xref]] data rules are edited in profiles/<name>/profile.toml (the template documents them)."""
    from nncq import profiles, repos
    p = profiles.get(name) if name else None
    root = _path(path)
    if p is not None:
        if root is not None:
            why = p.accepts(root)
            if why:
                print(f"!! {root} is not the '{p.name}' workspace: {why}")
                root = _ask_dir(f"  Where is the '{p.name}' workspace on this machine? [skip]: ", p.accepts) if ask else None
                if root is None:
                    return None
        elif p.root is None and ask:
            print(f"   profile '{p.name}': workspace not found on this machine (looked in {', '.join(p.roots) or 'nowhere'})")
            root = _ask_dir(f"  Where is the '{p.name}' workspace on this machine? [skip]: ", p.accepts)
        if root is not None:
            if p.root is None or p.root != root.resolve():
                profiles.add_root(p, root.resolve())
                print(f"   remembered {root.resolve()} in {p.path}")
            p = profiles.get(p.name)
        if p.root is None:
            print(f"!! profile '{p.name}': workspace not found on this machine. Pass its path "
                  f"(nncq profile new {p.name} <path>) or set NNCQ_WORKSPACE_{p.name.upper()}")
            for c in p.roots:
                print(f"     {c}: {p.accepts(profiles.candidate(c))}")
            return None
        r = repos.get(p.name)
        print(f"== workspace: profile '{p.name}' -> {r.root}" + (f" [{', '.join(r.dirs)}]" if r.dirs else ""))
        return r
    if root is not None and _new_root(root):
        print(f"!! {_new_root(root)}")
        root = None
        if not ask:
            return None
    if root is None and ask:
        root = _ask_dir("  Workspace root to index (outside this folder) [skip]: ", _new_root)
    if root is None:
        print("== workspace: none (later: nncq profile new <name> <path>)")
        return None
    sg = profiles.suggest(root)
    name = name or repos.default_name(root)
    desc = ""
    if ask:
        name = _ask_name(name)
        desc = input("  Description, one line (optional) []: ").strip()
    dv = ",".join(sg["dirs"])
    if dirs is None:
        dirs = (input(f"  Code folders, comma-separated (empty = everything) [{dv}]: ").strip() or dv) if ask else dv
    ig = ",".join(f"{s}/**" for s in sg["skipped"])
    ig = (input(f"  Ignore (globs, comma-separated) [{ig}]: ").strip() or ig) if ask else ig
    from nncq import vcs as vcs_mod
    vc = vcs_mod.detect(root.resolve())
    if ask:
        while True:
            a = (input(f"  Version control (git / p4 / none) [{vc}]: ").strip().lower() or vc)
            if a in vcs_mod.KINDS:
                vc = a
                break
            print("    git, p4 or none")
    dst = profiles.write(name, root.resolve(), _split(dirs), _split(ig), description=desc, markers=sg["markers"],
                         remote=sg["remote"], vcs=vc)
    print(f"== profile '{name}' written: {dst}\n   workspace {root.resolve()}"
          + (f" [{dirs}]" if dirs else " (everything)") + (f", ignoring {ig}" if ig else "") + f", vcs {vc}"
          + "\n   add file types, DSLs and [[xref]] data rules there (documented in profiles/_template/profile.toml)")
    return repos.get(name)


def _pick_profile(found: dict) -> str:
    """Ask for an existing profile, 'new', or an exported profile file to import; returns the profile name or ''."""
    from nncq import profiles
    archives = sorted(profiles.archive_dir().glob("*" + profiles.ARCHIVE_EXT)) if profiles.archive_dir().is_dir() else []
    if found:
        print("== profiles: " + ", ".join(f"{n} ({p.root or 'workspace not found'})" for n, p in found.items()))
    if archives:
        print("== exported profiles you can import: " + ", ".join(str(f) for f in archives))
    dv = next((n for n, p in found.items() if p.root), "") or "new"
    while True:
        ans = input(f"  Profile to use, 'new', or a {profiles.ARCHIVE_EXT} file to import [{dv}]: ").strip() or dv
        if ans == "new":
            return ""
        if ans in found:
            return ans
        f = _path(ans)
        if f is not None and (f.is_file() or ans.lower().endswith(profiles.ARCHIVE_EXT)):
            try:
                dst = profiles.import_(f)
            except (OSError, ValueError, KeyError) as e:
                print(f"    cannot import {f}: {e}")
                continue
            print(f"   imported {dst}")
            return dst.name
        print(f"    no profile '{ans}'" + (f"; choose {', '.join(found)}, " if found else "; type ")
              + f"'new' or a {profiles.ARCHIVE_EXT} file")


def _workspace(a) -> "object | None":
    """Pick, import or create the codebase profile to index (read-only: nncq never writes into the workspace)."""
    from nncq import profiles
    ask = not a.yes and sys.stdin.isatty()
    name = a.profile or a.name
    if not name and not a.workspace:
        found = profiles.load_all()
        if ask:
            name = _pick_profile(found)
        else:
            name = next((n for n, p in found.items() if p.root), "")
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
    assets()
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
