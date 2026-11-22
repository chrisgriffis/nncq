"""File enumeration: ripgrep (from the venv) + .p4ignore translated to gitignore syntax + NNCQ_IGNORE globs."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nncq import config

DEFAULT_SKIP = ["!**/.git/**", "!**/.svn/**", "!**/node_modules/**", "!**/__pycache__/**", "!**/.vs/**",
                "!**/.venv/**", "!**/bin/Debug/**", "!**/bin/Release/**", "!**/obj/**"]

# Code only (not data cross-reference scans), case-insensitive; NNCQ_IGNORE_DEFAULTS=false or a profile's
# ignore_defaults=false turns them off. Vendored / third-party, generated code.
CODE_IGNORE_DEFAULTS = [
    "**/External/**", "**/ThirdParty/**", "**/third_party/**", "**/3rdParty/**", "**/vendor/**", "**/extern/**",
    "**/Generated/**", "*.Designer.cs", "*.g.cs", "*.generated.*", "*_generated.*", "*_pb2.py", "*_pb2_grpc.py",
    "*.pb.h", "*.pb.cc", "*.min.js", "*.pyi",
]


def code_ignore(extra: str, profile=None) -> str:
    """NNCQ_IGNORE + the profile's ignores + the defaults above (unless disabled), as one comma list."""
    on = os.environ.get("NNCQ_IGNORE_DEFAULTS", "true").lower() not in ("0", "false", "no", "off")
    if profile is not None and not profile.ignore_defaults:
        on = False
    out = list(CODE_IGNORE_DEFAULTS) if on else []
    out += profile.code_ignore() if profile is not None else []
    return ",".join(out + [x.strip() for x in extra.replace(";", ",").split(",") if x.strip()])


def rg_exe() -> str:
    for c in (Path(sys.executable).parent / "rg.exe", Path(sys.executable).parent / "rg"):
        if c.exists():
            return str(c)
    raise RuntimeError(f"ripgrep not found next to {sys.executable} (re-run init.ps1; nncq only uses its own venv)")


def _p4ignore_to_gitignore(text: str) -> str:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        line = line[1:] if neg else line
        line = line.replace("\\", "/").replace("...", "**")
        if line.startswith("*/"):
            line = "**/" + line[2:]
        out.append(("!" if neg else "") + line)
    return "\n".join(out) + "\n"


def ignore_args(root: Path, extra_ignore: str = "") -> list[str]:
    args = ["--no-require-git", "--hidden"]
    for g in DEFAULT_SKIP:
        args += ["-g", g]
    p4i = root / os.environ.get("P4IGNORE", ".p4ignore")
    if p4i.is_file():
        d = config.DATA_DIR / "ignore"
        d.mkdir(parents=True, exist_ok=True)
        out = d / (hashlib.sha1(str(root).lower().encode()).hexdigest()[:12] + ".gitignore")
        text = _p4ignore_to_gitignore(p4i.read_text(encoding="utf-8", errors="replace"))
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            out.write_text(text, encoding="utf-8")
        args += ["--ignore-file", str(out)]
    for g in filter(None, (x.strip() for x in extra_ignore.replace(";", ",").split(","))):
        args += ["--iglob", "!" + g]
    return args


def top_dirs(root: Path) -> list[str]:
    """Search targets: top-level entries (rg given no path may read stdin when it is not a tty)."""
    return sorted(e.name for e in os.scandir(root) if not e.name.startswith("."))


def list_files(root: Path, globs: list[str], extra_ignore: str = "", dirs: list[str] | None = None) -> list[str]:
    """Relative posix paths of files matching any glob (e.g. '*.cpp'), honouring ignores.
    dirs: restrict to these subdirectories of root (default: everything)."""
    cmd = [rg_exe(), "--files", *ignore_args(root, extra_ignore)]
    for g in globs:
        cmd += ["-g", g]
    targets = [d for d in (dirs or []) if (root / d).is_dir()] if dirs else top_dirs(root)
    if not targets:
        return []
    cmd += targets
    p = subprocess.run(cmd, cwd=root, capture_output=True, stdin=subprocess.DEVNULL)
    if p.returncode not in (0, 1):
        raise RuntimeError(p.stderr.decode(errors="replace")[:500])
    return [ln.replace("\\", "/") for ln in p.stdout.decode("utf-8", "replace").splitlines() if ln]


def _glob_re(pat: str) -> str:
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3; continue
        if pat.startswith("**", i):
            out.append(".*"); i += 2; continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return "".join(out)


class Ignorer:
    """Python-side version of the rg ignore rules, for single paths reported by the watcher / p4 sync."""

    def __init__(self, root: Path, extra_ignore: str = "", dirs: list[str] | None = None):
        lines = [g[1:] for g in DEFAULT_SKIP]
        p4i = root / os.environ.get("P4IGNORE", ".p4ignore")
        if p4i.is_file():
            lines += _p4ignore_to_gitignore(p4i.read_text(encoding="utf-8", errors="replace")).splitlines()
        lines += [g.strip() for g in extra_ignore.replace(";", ",").split(",") if g.strip()]
        self.rules: list[tuple[bool, re.Pattern]] = []
        for ln in lines:
            neg = ln.startswith("!")
            ln = ln[1:] if neg else ln
            ln = ln.rstrip("/")
            anchored = ln.startswith("/") or "/" in ln.strip("/").replace("**/", "")
            body = _glob_re(ln.lstrip("/"))
            rx = ("^" if anchored else r"(?:^|.*/)") + body + "(?:/.*)?$"
            self.rules.append((neg, re.compile(rx, re.I)))
        self.dirs = [d.strip("/").lower() + "/" for d in dirs or []]

    def ignored(self, rel: str) -> bool:
        rel = rel.replace("\\", "/")
        if self.dirs and not any(rel.lower().startswith(d) for d in self.dirs):
            return True
        hit = False
        for neg, rx in self.rules:
            if rx.match(rel):
                hit = not neg
        return hit


def stat_many(root: Path, paths: list[str]) -> dict[str, tuple[int, int]]:
    """path -> (size, mtime_ns); missing files are omitted. Threaded: os.stat releases the GIL."""
    def one(p):
        try:
            st = os.stat(root / p)
            return p, (st.st_size, st.st_mtime_ns)
        except OSError:
            return p, None
    with ThreadPoolExecutor(32) as ex:
        return {p: s for p, s in ex.map(one, paths, chunksize=512) if s is not None}
