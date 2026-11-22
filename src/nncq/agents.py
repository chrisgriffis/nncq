"""`nncq agents`: wire nncq into the agents on this machine, the same way for everyone.

For each target it does two things:
  mcp    register nncq as a stdio MCP server (the agent starts it; it exits with the agent)
  skill  install nncq-user.skill.md, which teaches the agent to investigate with several small asks and
         answer with one compiled digest (the MCP server instructions carry a short version of the same)

Targets:
  copilot  Copilot CLI     ~/.copilot/mcp-config.json          ~/.copilot/skills/nncq-user/SKILL.md
  claude   Claude Code     `claude mcp add --scope user ...`   ~/.claude/skills/nncq-user/SKILL.md
  vs       Visual Studio   ~/.mcp.json (global MCP servers)    no global skill location: printed hint

Existing config files are merged, never replaced; a .bak-nncq copy is kept before the first change.
Only files in the user's home are touched; the indexed workspace is never written to.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nncq import config

SKILL_NAME = "nncq-user"
SKILL_SRC = config.ROOT / "nncq-user.skill.md"
TARGETS = ("copilot", "claude", "vs")
LABEL = {"copilot": "Copilot CLI", "claude": "Claude Code", "vs": "Visual Studio (Copilot agent mode)"}


def _home() -> Path:
    return Path(os.environ.get("NNCQ_AGENT_HOME") or Path.home())


def exe() -> str:
    return str(Path(sys.executable).parent / ("nncq.exe" if os.name == "nt" else "nncq"))


def _merge_json(path: Path, section: str, entry: dict) -> str:
    """Put entry under data[section]["nncq"]. Returns what happened."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return f"!! {path} is not valid JSON; add this under \"{section}\" yourself:\n   \"nncq\": {json.dumps(entry)}"
    if data.get(section, {}).get("nncq") == entry:
        return f"already configured ({path})"
    if path.exists() and not path.with_name(path.name + ".bak-nncq").exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-nncq"))
    data.setdefault(section, {})["nncq"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"added nncq to {path}"


def _skill(dest_root: Path) -> str:
    if not SKILL_SRC.exists():
        return f"!! skill source missing: {SKILL_SRC}"
    dst = dest_root / SKILL_NAME / "SKILL.md"
    new = SKILL_SRC.read_text(encoding="utf-8")
    if dst.exists() and dst.read_text(encoding="utf-8") == new:
        return f"skill up to date ({dst})"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new, encoding="utf-8")
    return f"skill installed ({dst})"


def detected() -> list[str]:
    h = _home()
    out = []
    if (h / ".copilot").is_dir() or shutil.which("copilot"):
        out.append("copilot")
    if (h / ".claude").is_dir() or shutil.which("claude"):
        out.append("claude")
    pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)")]
    if os.name == "nt" and any(p and (Path(p) / "Microsoft Visual Studio").is_dir() for p in pf):
        out.append("vs")
    return out


def setup(target: str) -> list[str]:
    h, cmd = _home(), exe()
    if target == "copilot":
        entry = {"type": "local", "command": cmd, "args": ["serve", "--stdio"], "tools": ["*"]}
        return [_merge_json(h / ".copilot" / "mcp-config.json", "mcpServers", entry),
                _skill(h / ".copilot" / "skills"), "restart Copilot CLI or run /mcp"]
    if target == "claude":
        out = []
        claude = shutil.which("claude")
        add = ["mcp", "add", "--scope", "user", "nncq", "--", cmd, "serve", "--stdio"]
        if claude and os.environ.get("NNCQ_AGENT_HOME") is None:   # redirected home (tests): never call the real CLI
            got = subprocess.run([claude, "mcp", "get", "nncq"], capture_output=True, text=True)
            if got.returncode == 0:
                out.append("MCP server already registered (claude mcp get nncq)")
            else:
                r = subprocess.run([claude, *add], capture_output=True, text=True)
                out.append("MCP server registered (user scope)" if r.returncode == 0 else
                           f"!! claude mcp add failed: {(r.stderr or r.stdout).strip()}")
        else:
            out.append("run: claude " + " ".join(add))
        return out + [_skill(h / ".claude" / "skills")]
    if target == "vs":
        entry = {"type": "stdio", "command": cmd, "args": ["serve", "--stdio"]}
        return [_merge_json(h / ".mcp.json", "servers", entry),
                "skill: Visual Studio has no global skill folder; paste nncq-user.skill.md into your Copilot "
                f"custom instructions, or attach it in chat ({SKILL_SRC})",
                "restart Visual Studio; enable the nncq tools in the Copilot Chat tool picker (agent mode)"]
    raise ValueError(f"unknown agent target {target!r}; one of {', '.join(TARGETS)}")


def run(targets: list[str] | None, ask: bool) -> int:
    """targets None = detect, and ask per target when interactive (otherwise do nothing)."""
    if targets is None:
        found = detected()
        if not ask:
            if found:
                print(f"== agents: found {', '.join(LABEL[t] for t in found)}; set up with: nncq agents "
                      f"--targets {','.join(found)}")
            return 0
        targets = [t for t in found if input(f"  Set up {LABEL[t]} (MCP server + nncq-user skill)? [Y/n]: ")
                   .strip().lower() in ("", "y", "yes")]
    bad = 0
    for t in targets:
        print(f"== {LABEL.get(t, t)}")
        try:
            for line in setup(t):
                print("   " + line)
                bad += line.startswith("!!")
        except ValueError as e:
            print(f"   !! {e}")
            bad += 1
    return 1 if bad else 0
