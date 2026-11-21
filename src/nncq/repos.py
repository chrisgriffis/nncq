"""Registered workspaces. Each gets its own SQLite index at cache/db/<name>/index.db.

Registry = cache/db/repos.json (written by `nncq index <path>`) plus NNCQ_REPOS from .env
(name=path;name2=path2, optionally name=path|SubDir1,SubDir2 to index only those subdirectories).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from nncq import config


@dataclass
class Repo:
    name: str
    root: Path
    dirs: list[str] = field(default_factory=list)   # code subdirectories ([] = whole root)

    @property
    def db(self) -> Path:
        return config.DATA_DIR / self.name / "index.db"

    def to_json(self) -> dict:
        return {"root": str(self.root), "dirs": self.dirs}


def _from_env() -> dict[str, Repo]:
    out = {}
    for item in filter(None, (x.strip() for x in config.settings().repos.split(";"))):
        name, _, rest = item.partition("=")
        path, _, dirs = rest.partition("|")
        if name.strip() and path.strip():
            out[name.strip()] = Repo(name.strip(), Path(path.strip()),
                                     [d.strip().replace("\\", "/") for d in dirs.split(",") if d.strip()])
    return out


def load() -> dict[str, Repo]:
    out = {}
    if config.REPOS_FILE.exists():
        for name, v in json.loads(config.REPOS_FILE.read_text(encoding="utf-8")).items():
            out[name] = Repo(name, Path(v["root"]), v.get("dirs", []))
    out.update(_from_env())
    return out


def _save(repos: dict[str, Repo]) -> None:
    env = _from_env()
    data = {n: r.to_json() for n, r in repos.items() if n not in env}
    config.REPOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REPOS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def default_name(root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", root.resolve().name).strip("-").lower() or "repo"


def add(root: Path, name: str = "", dirs: list[str] | None = None) -> Repo:
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    repos = load()
    for r in repos.values():
        if r.root.resolve() == root and not name:
            name = r.name
    name = name or default_name(root)
    old = repos.get(name)
    if dirs is None:
        dirs = old.dirs if old else []
    repos[name] = Repo(name, root, [d.strip("/\\").replace("\\", "/") for d in dirs])
    _save(repos)
    return repos[name]


def remove(name: str) -> None:
    repos = load()
    repos.pop(name, None)
    _save(repos)


def get(name: str = "") -> Repo:
    """By name; else the only repo; else the repo containing the current directory."""
    repos = load()
    if name:
        if name not in repos:
            raise KeyError(f"unknown repo '{name}' (have: {', '.join(repos) or 'none'})")
        return repos[name]
    if len(repos) == 1:
        return next(iter(repos.values()))
    if not repos:
        raise KeyError("no repos indexed yet: run `nncq index <path>`")
    cwd = Path.cwd().resolve()
    for r in repos.values():
        if cwd == r.root or r.root in cwd.parents:
            return r
    raise KeyError(f"several repos ({', '.join(repos)}): pass repo=<name>")
