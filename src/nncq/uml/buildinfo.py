"""Build files -> components, artifacts, link dependencies and build configurations. Read-only, best effort:
CMake is evaluated without running it (no if() logic: every branch counts, variables expanded in file order).

  CMakeLists.txt / *.cmake  add_library / add_executable (artifacts + their sources), target_sources,
                            target_link_libraries (PUBLIC/PRIVATE/INTERFACE), target_include_directories,
                            add_dependencies, ALIAS, option(), CMAKE_BUILD_TYPE / CMAKE_CONFIGURATION_TYPES,
                            toolchain files (set CMAKE_SYSTEM_NAME / CMAKE_<LANG>_COMPILER)
  CMakePresets.json         configure / build presets
  pyproject.toml, setup.py  Python distribution name and console scripts
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

_SCOPES = {"PUBLIC", "PRIVATE", "INTERFACE"}
_LIB_TYPES = {"STATIC", "SHARED", "MODULE", "OBJECT", "INTERFACE", "UNKNOWN"}
_FLAGS = {"EXCLUDE_FROM_ALL", "WIN32", "MACOSX_BUNDLE", "GLOBAL", "SYSTEM", "BEFORE", "AFTER"}
_LINK_KW = {"debug", "optimized", "general", "LINK_PUBLIC", "LINK_PRIVATE", "LINK_INTERFACE_LIBRARIES"}
DEFAULT_CONFIGS = ("Debug", "Release", "RelWithDebInfo", "MinSizeRel")


@dataclass
class Target:
    name: str
    kind: str                     # library | executable | external
    type: str                     # STATIC | SHARED | ... | EXECUTABLE | IMPORTED
    dir: str                      # repo-relative folder of the CMakeLists that declares it
    source: str                   # declaring file
    line: int
    sources: list[str] = field(default_factory=list)       # repo-relative paths
    includes: dict[str, list[str]] = field(default_factory=dict)   # scope -> dirs
    links: list[tuple[str, str]] = field(default_factory=list)     # (name, scope)
    deps: list[str] = field(default_factory=list)


@dataclass
class BuildInfo:
    targets: dict[str, Target] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    configs: list[tuple[str, str, str, str]] = field(default_factory=list)   # (name, kind, source, info)
    packages: list[tuple[str, str, str, dict]] = field(default_factory=list)  # (name, dir, source, info)
    projects: list[tuple[str, str]] = field(default_factory=list)            # (name, dir)

    def resolve(self, name: str) -> str:
        return self.aliases.get(name, name)


def _strip_comments(text: str) -> str:
    text = re.sub(r"#\[(=*)\[.*?\]\1\]", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = []
    for line in text.split("\n"):
        q, cut = False, len(line)
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                q = not q
            elif ch == "#" and not q:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def commands(text: str):
    """(command lowercase, [args], line) for every CMake command invocation."""
    text = _strip_comments(text)
    for m in re.finditer(r"(?m)^[ \t]*([A-Za-z_]\w*)[ \t]*\(", text):
        i, depth, q = m.end(), 1, False
        while i < len(text) and depth:
            ch = text[i]
            if ch == '"' and text[i - 1] != "\\":
                q = not q
            elif not q and ch == "(":
                depth += 1
            elif not q and ch == ")":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        args = [a[1:-1] if a.startswith('"') else a for a in re.findall(r'"(?:[^"\\]|\\.)*"|[^\s()"]+', body)]
        yield m.group(1).lower(), args, text.count("\n", 0, m.start()) + 1


def _norm(p: str) -> str:
    return posixpath.normpath(p.replace("\\", "/")) if p else p


class _CMake:
    def __init__(self, info: BuildInfo):
        self.info = info
        self.vars: dict[str, str] = {}

    def expand(self, s: str, depth: int = 0) -> str:
        if "${" not in s or depth > 8:
            return s
        return self.expand(re.sub(r"\$\{(\w+)\}", lambda m: self.vars.get(m.group(1), ""), s), depth + 1)

    def path(self, s: str, d: str) -> str | None:
        """Repo-relative path for a CMake path argument; None when outside the repo or a generator expression."""
        s = self.expand(s)
        if not s or "$<" in s or "$" in s:
            return None
        if s == "@" or s.startswith("@/"):
            p = _norm(s[2:] or ".")
        elif re.match(r"^([A-Za-z]:)?/", s):
            return None
        else:
            p = _norm(posixpath.join(d, s))
        return None if p.startswith("..") else ("" if p == "." else p)

    def run(self, rel: str, text: str) -> None:
        d = posixpath.dirname(rel)
        here = "@/" + d if d else "@"
        self.vars.update(CMAKE_CURRENT_LIST_DIR=here, CMAKE_CURRENT_SOURCE_DIR=here, PROJECT_SOURCE_DIR=here,
                         CMAKE_SOURCE_DIR="@", PROJECT_NAME=self.vars.get("PROJECT_NAME", ""))
        info = self.info
        is_toolchain = "toolchain" in rel.lower()
        for cmd, args, line in commands(text):
            if not args:
                continue
            a0 = self.expand(args[0])
            if cmd == "set":
                vals = [x for x in args[1:] if x not in ("PARENT_SCOPE", "FORCE")]
                if "CACHE" in vals:
                    vals = vals[:vals.index("CACHE")]
                self.vars[a0] = ";".join(self.expand(v) for v in vals)
                if a0 in ("CMAKE_SYSTEM_NAME", "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_TOOLCHAIN_FILE"):
                    is_toolchain = True
                if a0 == "CMAKE_CONFIGURATION_TYPES":
                    for c in self.vars[a0].split(";"):
                        if c:
                            info.configs.append((c, "config", rel, f"CMAKE_CONFIGURATION_TYPES ({rel}:{line})"))
                elif a0 == "CMAKE_BUILD_TYPE" and self.vars[a0]:
                    info.configs.append((self.vars[a0], "config", rel, f"CMAKE_BUILD_TYPE ({rel}:{line})"))
            elif cmd == "list" and len(args) > 2 and args[0].upper() == "APPEND":
                v = self.expand(args[1])
                self.vars[v] = ";".join(x for x in [self.vars.get(v, "")] + [self.expand(x) for x in args[2:]] if x)
            elif cmd == "project":
                self.vars["PROJECT_NAME"] = a0
                info.projects.append((a0, d))
            elif cmd == "option":
                info.configs.append((a0, "option", rel, f"{args[1] if len(args) > 1 else ''} "
                                                        f"[default {args[2] if len(args) > 2 else 'OFF'}]".strip()))
            elif cmd in ("add_library", "add_executable"):
                rest = [self.expand(x) for x in args[1:]]
                if "ALIAS" in rest:
                    info.aliases[a0] = rest[rest.index("ALIAS") + 1] if rest.index("ALIAS") + 1 < len(rest) else a0
                    continue
                typ = next((x for x in rest if x in _LIB_TYPES), "STATIC" if cmd == "add_library" else "EXECUTABLE")
                kind = "library" if cmd == "add_library" else "executable"
                if "IMPORTED" in rest:
                    kind, typ = "external", "IMPORTED"
                t = info.targets.get(a0) or Target(a0, kind, typ, d, rel, line)
                info.targets[a0] = t
                t.sources += self._files(rest, d)
            elif cmd == "target_sources" and a0 in info.targets:
                info.targets[a0].sources += self._files([self.expand(x) for x in args[1:]], d)
            elif cmd == "target_link_libraries" and a0 in info.targets:
                scope = "PRIVATE"
                for x in args[1:]:
                    for v in self.expand(x).split(";"):
                        if v in _SCOPES:
                            scope = v
                        elif v and v not in _LINK_KW and "$<" not in v and not v.startswith("-"):
                            info.targets[a0].links.append((v, scope))
            elif cmd == "target_include_directories" and a0 in info.targets:
                scope = "PRIVATE"
                for x in args[1:]:
                    for v in self.expand(x).split(";"):
                        if v in _SCOPES:
                            scope = v
                        elif v not in _FLAGS:
                            p = self.path(v, d)
                            if p is not None:
                                info.targets[a0].includes.setdefault(scope, []).append(p)
            elif cmd == "add_dependencies" and a0 in info.targets:
                info.targets[a0].deps += [self.expand(x) for x in args[1:]]
        if is_toolchain and rel.endswith(".cmake"):
            name = posixpath.basename(rel)[:-6]
            info.configs.append((name, "toolchain", rel, ", ".join(
                f"{k}={self.vars[k]}" for k in ("CMAKE_SYSTEM_NAME", "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER",
                                                "CMAKE_CXX_STANDARD") if self.vars.get(k)) or "toolchain file"))

    def _files(self, args: list[str], d: str) -> list[str]:
        out = []
        for a in args:
            for v in a.split(";"):
                if v in _LIB_TYPES or v in _FLAGS or v in _SCOPES or v == "IMPORTED":
                    continue
                p = self.path(v, d)
                if p and "." in posixpath.basename(p):
                    out.append(p)
        return out


def _presets(rel: str, text: str, info: BuildInfo) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    for kind in ("configurePresets", "buildPresets", "testPresets"):
        for p in data.get(kind, []) or []:
            if isinstance(p, dict) and p.get("name") and not p.get("hidden"):
                cv = p.get("cacheVariables") or {}
                bt = cv.get("CMAKE_BUILD_TYPE")
                bt = bt.get("value") if isinstance(bt, dict) else bt
                desc = ", ".join(x for x in (p.get("displayName") or "", p.get("generator") or "",
                                             f"CMAKE_BUILD_TYPE={bt}" if bt else "",
                                             f"configuration={p['configuration']}" if p.get("configuration") else "")
                                 if x)
                info.configs.append((p["name"], "preset:" + kind[:-7], rel, desc))


def _python(rel: str, text: str, info: BuildInfo) -> None:
    d = posixpath.dirname(rel)
    if rel.endswith("pyproject.toml"):
        try:
            import tomllib
            data = tomllib.loads(text)
        except Exception:
            return
        proj = data.get("project") or data.get("tool", {}).get("poetry") or {}
        if proj.get("name"):
            scripts = dict(proj.get("scripts") or {})
            scripts.update((proj.get("gui-scripts") or {}))
            info.packages.append((proj["name"], d, rel, {"scripts": scripts, "version": proj.get("version", "")}))
    else:
        m = re.search(r"\bname\s*=\s*['\"]([\w.\-]+)['\"]", text)
        if m:
            scripts = re.findall(r"['\"]([\w.\-]+)\s*=\s*([\w.:]+)['\"]", text)
            info.packages.append((m.group(1), d, rel, {"scripts": dict(scripts)}))


def is_build_file(path: str) -> bool:
    b = posixpath.basename(path).lower()
    return b in ("cmakelists.txt", "cmakepresets.json", "cmakeuserpresets.json", "pyproject.toml", "setup.py") \
        or b.endswith(".cmake")


def parse(root: Path, paths: list[str]) -> BuildInfo:
    """paths: repo-relative build files (indexed ones plus top-level ones found on disk)."""
    info = BuildInfo()
    cm = _CMake(info)
    order = sorted(set(paths), key=lambda p: (posixpath.basename(p).lower() != "cmakelists.txt" and
                                              p.lower().endswith(".cmake") and "toolchain" not in p.lower(),
                                              p.count("/"), p))
    for rel in order:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        b = posixpath.basename(rel).lower()
        if b in ("cmakepresets.json", "cmakeuserpresets.json"):
            _presets(rel, text, info)
        elif b in ("pyproject.toml", "setup.py"):
            _python(rel, text, info)
        elif "toolchain" in rel.lower() and b.endswith(".cmake"):
            _CMake(info).run(rel, text)            # toolchains don't leak variables into the project
        else:
            cm.run(rel, text)
    if info.targets and not any(k == "config" or k.startswith("preset") for _, k, _, _ in info.configs):
        info.configs += [(c, "config", "", "CMake default configuration (no presets / CMAKE_CONFIGURATION_TYPES)")
                         for c in DEFAULT_CONFIGS]
    return info
