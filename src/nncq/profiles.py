"""Codebase profiles: everything specific to one codebase lives in profiles/<name>/profile.toml (git-ignored;
profiles/_template is the documented, shared example). The core stays codebase-agnostic.

A profile says
  - how to find its workspace on this machine ([workspace]: candidate roots checked against markers / git remote,
    or NNCQ_WORKSPACE_<NAME>). Workspaces live anywhere outside the nncq folder, never inside it.
  - which folders to index and what to ignore
  - extra file extensions, brace-block definition languages ([[dsl]])
  - data cross-reference rules ([[xref]]): regexes over data files naming the types they instantiate and the
    files they reference (see datarefs.py)
  - notes appended to the MCP server instructions ([mcp])
Each profile is one index: cache/db/<name>/index.db.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from nncq import config

DIR = config.ROOT / "profiles"
TEMPLATE = "_template"

DSL_KEYWORDS = ["class", "struct", "enum", "interface", "valuetype", "component", "service", "type", "method",
                "namespace", "delegate", "event", "union", "flags", "module", "function", "entity", "contract"]
DSL_CONTAINERS = ["class", "struct", "interface", "valuetype", "component", "service", "namespace", "union",
                  "entity", "contract", "type"]


@dataclass(frozen=True)
class Dsl:
    lang: str
    extensions: tuple[str, ...]
    keywords: tuple[str, ...] = tuple(DSL_KEYWORDS)
    containers: tuple[str, ...] = tuple(DSL_CONTAINERS)
    module_keyword: str = ""        # `module X;` qualifies the declarations that follow (X.Foo)

    def spec(self) -> dict:
        """Picklable form handed to chunker worker processes."""
        return {"keywords": self.keywords, "containers": self.containers, "module_keyword": self.module_keyword}


@dataclass(frozen=True)
class Xref:
    name: str
    glob: str                       # data files, e.g. *.xml
    instance: str = ""              # regex with (?P<type>...): a type the file instantiates
    reference: str = ""             # regex with (?P<ref>...): a path/key the file references (no extension)
    label: str = "data file"
    type_suffix: str = ""           # data type naming convention: code Foo <-> data FooData

    @property
    def pattern(self) -> str:
        return "|".join(p for p in (self.instance, self.reference) if p)

    def matches(self, path: str) -> bool:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1].lower(), self.glob.lower())


@dataclass
class Profile:
    name: str
    path: Path                      # the profile.toml
    description: str = ""
    roots: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    git_remote: str = ""
    vcs: str = ""                   # git | p4 | none ("" = detect at run time, vcs.detect)
    dirs: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    ignore_defaults: bool = True
    extensions: dict[str, str] = field(default_factory=dict)
    text_langs: list[str] | None = None
    dsls: list[Dsl] = field(default_factory=list)
    xrefs: list[Xref] = field(default_factory=list)
    mcp_notes: str = ""

    # ---------------------------------------------------------------- workspace link
    def accepts(self, root: Path) -> str:
        """'' if root looks like this profile's workspace, else why not."""
        if not root.is_dir():
            return f"not a directory: {root}"
        if nested(root):
            return f"{root} overlaps the nncq folder {config.ROOT}; workspaces live outside it"
        miss = [m for m in self.markers if not (root / m).exists()]
        if miss:
            return f"missing marker(s) {', '.join(miss)} under {root}"
        if self.git_remote and (root / ".git").exists():
            url = git_remote(root)
            if url and self.git_remote.lower() not in url.lower():
                return f"git remote {url} does not contain {self.git_remote!r}"
        return ""

    @cached_property
    def root(self) -> Path | None:
        """This machine's workspace: NNCQ_WORKSPACE_<NAME>, else the first candidate root that is accepted."""
        env = os.environ.get("NNCQ_WORKSPACE_" + re.sub(r"\W", "_", self.name).upper(), "")
        for c in ([env] if env else []) + self.roots:
            p = Path(os.path.expandvars(c)).expanduser()
            if not self.accepts(p):
                return p.resolve()
        return None

    # ---------------------------------------------------------------- indexing knobs
    def code_ignore(self) -> list[str]:
        return list(self.ignore)

    def dsl_for(self, lang: str) -> Dsl | None:
        return next((d for d in self.dsls if d.lang == lang), None)

    def ext_map(self) -> dict[str, str]:
        m = dict(self.extensions)
        for d in self.dsls:
            m.update({e: d.lang for e in d.extensions})
        return m

    def xref_for(self, path: str) -> Xref | None:
        return next((x for x in self.xrefs if x.matches(path)), None)


def nested(root: Path) -> bool:
    try:
        a, b = root.resolve(), config.ROOT.resolve()
    except OSError:
        return False
    return a == b or b in a.parents or a in b.parents


def git_remote(root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ext(e: str) -> str:
    return (e if e.startswith(".") else "." + e).lower()


def parse(path: Path) -> Profile:
    d = tomllib.loads(path.read_text(encoding="utf-8"))
    ws, ix, mcp = d.get("workspace", {}), d.get("index", {}), d.get("mcp", {})
    name = d.get("name") or path.parent.name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"{path}: profile name {name!r} must be letters, digits, _ . -")
    vc = str(ws.get("vcs", "") or "").strip().lower()
    if vc and vc not in ("git", "p4", "none"):
        raise ValueError(f"{path}: [workspace] vcs = {vc!r}: use git, p4 or none (or leave it out to detect)")
    dsls = [Dsl(x["lang"], tuple(_ext(e) for e in x.get("extensions", [])),
                tuple(x.get("keywords", DSL_KEYWORDS)), tuple(x.get("containers", DSL_CONTAINERS)),
                x.get("module_keyword", "")) for x in d.get("dsl", [])]
    xrefs = []
    for x in d.get("xref", []):
        xr = Xref(x.get("name", "data"), x["glob"], x.get("instance", ""), x.get("reference", ""),
                  x.get("label", "data file"), x.get("type_suffix", ""))
        for rx, grp in ((xr.instance, "type"), (xr.reference, "ref")):
            if rx and f"(?P<{grp}>" not in rx:
                raise ValueError(f"{path}: xref {xr.name}: regex {rx!r} needs a (?P<{grp}>...) group")
            if rx:
                re.compile(rx)
        xrefs.append(xr)
    return Profile(
        name=name, path=path, description=d.get("description", ""),
        roots=list(ws.get("roots", [])), markers=list(ws.get("markers", [])), git_remote=ws.get("git_remote", ""), vcs=vc,
        dirs=[x.strip("/\\").replace("\\", "/") for x in ws.get("dirs", [])],
        ignore=list(ws.get("ignore", [])), ignore_defaults=bool(ws.get("ignore_defaults", True)),
        extensions={_ext(k): v or "text" for k, v in ix.get("extensions", {}).items()},
        text_langs=ix.get("text_langs"), dsls=dsls, xrefs=xrefs, mcp_notes=mcp.get("notes", "").strip())


def load_all() -> dict[str, Profile]:
    out = {}
    if DIR.is_dir():
        for f in sorted(DIR.glob("*/profile.toml")):
            if f.parent.name == TEMPLATE or f.parent.name.startswith("."):
                continue
            p = parse(f)
            out[p.name] = p
    return out


def get(name: str) -> Profile | None:
    return load_all().get(name)


# ---------------------------------------------------------------- writing (nncq init / nncq profile new)

def _q(s: str) -> str:
    return "'" + s.replace("'", "") + "'" if "'" not in s else '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _list(xs) -> str:
    return "[" + ", ".join(_q(str(x)) for x in xs) + "]"


def write(name: str, root: Path, dirs: list[str], ignore: list[str], description: str = "",
          markers: list[str] | None = None, remote: str = "", vcs: str = "") -> Path:
    """Create profiles/<name>/profile.toml from the template, filled in for this workspace."""
    if nested(root):
        raise ValueError(f"{root} overlaps the nncq folder; pick a workspace outside {config.ROOT}")
    dst = DIR / name / "profile.toml"
    tmpl = (DIR / TEMPLATE / "profile.toml").read_text(encoding="utf-8")
    head, _, rest = tmpl.partition("# ---- end of [workspace]")
    ws = (f"name = {_q(name)}\ndescription = {_q(description)}\n\n[workspace]\n"
          f"roots = {_list([str(root).replace(chr(92), '/')])}\n"
          f"markers = {_list(markers or [])}\n"
          f"git_remote = {_q(remote)}\n"
          f"vcs = {_q(vcs)}\n"
          f"dirs = {_list(dirs)}\n"
          f"ignore = {_list(ignore)}\n"
          "ignore_defaults = true\n")
    body = rest if rest else ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(ws + "# ---- end of [workspace]" + body, encoding="utf-8")
    parse(dst)
    return dst


def add_root(p: Profile, root: Path) -> None:
    """Remember another location of the workspace (another machine / drive) in the profile."""
    text = p.path.read_text(encoding="utf-8")
    entry = str(root).replace("\\", "/")
    if entry in p.roots:
        return
    new = _list([entry] + p.roots)
    text, n = re.subn(r"(?m)^roots\s*=\s*\[[^\]]*\]", lambda _: f"roots = {new}", text, count=1)
    if not n:
        text = text.replace("[workspace]\n", f"[workspace]\nroots = {new}\n", 1)
    p.path.write_text(text, encoding="utf-8")


def suggest(root: Path) -> dict:
    """Look at a workspace (read-only) and suggest markers, code folders, ignores and a git remote."""
    tops = sorted(e.name for e in os.scandir(root) if not e.name.startswith("."))
    vendored = re.compile(r"(?i)^(contrib|external|extern|third[_-]?party|3rdparty|vendor|deps|packages|"
                          r"node_modules|build|out|bin|obj)$")
    subdirs = [t for t in tops if (root / t).is_dir()]
    dirs = [t for t in subdirs if not vendored.match(t)]
    markers = [m for m in ("CMakeLists.txt", "pyproject.toml", "package.json", ".p4ignore", "README.md",
                           "Cargo.toml", "go.mod") if (root / m).is_file()][:2]
    url = git_remote(root) if (root / ".git").exists() else ""
    remote = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) if url else ""
    return {"dirs": [], "skipped": [t for t in subdirs if vendored.match(t)], "all": dirs,
            "markers": markers, "remote": remote}

# ---------------------------------------------------------------- archive (nncq profile export / import)

ARCHIVE_DIR = config.ROOT / "archive"
ARCHIVE_EXT = ".nncqprofile"


def export(name: str, dest: Path | None = None) -> Path:
    """profiles/<name>/ -> one compressed file (zip, deflate level 9) in archive/ by default."""
    import time
    import zipfile
    src = DIR / name
    if not (src / "profile.toml").is_file():
        raise ValueError(f"no profile {name!r} under {DIR}")
    dest = Path(dest) if dest else ARCHIVE_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}{ARCHIVE_EXT}"
    if dest.is_dir():
        dest = dest / f"{name}{ARCHIVE_EXT}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("nncq-profile.json", __import__("json").dumps({"name": name, "format": 1}))
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f"{name}/{f.relative_to(src).as_posix()}")
    return dest


def import_(archive: Path, name: str = "", force: bool = False) -> Path:
    """Unpack an exported profile into profiles/<name>/ (name defaults to the one it was exported as)."""
    import json
    import zipfile
    with zipfile.ZipFile(archive) as z:
        meta = json.loads(z.read("nncq-profile.json"))
        old = meta["name"]
        name = name or old
        dst = DIR / name
        if (dst / "profile.toml").exists() and not force:
            raise ValueError(f"profile {name!r} exists ({dst}); pass --force to overwrite")
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(old + "/"):
                continue
            rel = Path(info.filename[len(old) + 1:])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe path in archive: {info.filename}")
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(info))
    toml = dst / "profile.toml"
    if name != old:
        text = toml.read_text(encoding="utf-8")
        toml.write_text(re.sub(r"(?m)^name\s*=.*$", f"name = {_q(name)}", text, count=1), encoding="utf-8")
    parse(toml)
    return dst
