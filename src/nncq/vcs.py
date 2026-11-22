"""Version control for a workspace: detect git or Perforce and wrap the few operations nncq needs.

    v = vcs.for_root(root)        # Git | Perforce | NoVcs (never raises)
    v.status(path)                # {'vcs', 'tracked', 'state', ...}   state: clean|modified|added|deleted|untracked|
                                  #   opened:<action> (p4)|unknown
    v.have_rev(path)              # git: blob id at HEAD; p4: '#<haveRev>'; None when not tracked / unknown
    v.edit(path) / v.add(path) / v.revert(path) / v.stage(path)

nncq is read-only on the workspace; the mutating calls exist only for explicit user clicks in the dashboard
(loopback only, never from MCP). git / p4 are external programs: NNCQ_GIT / NNCQ_P4, the venv's Scripts folder,
the usual install folders, then PATH. When missing, operations return {'error': 'git not found ...'}.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TIMEOUT = 20


def _exe(name: str) -> str | None:
    env = os.environ.get(f"NNCQ_{name.upper()}")
    if env and Path(env).is_file():
        return env
    win = sys.platform == "win32"
    exe = name + (".exe" if win else "")
    cands = [Path(sys.executable).parent / exe]
    if win:
        pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        cands += [Path(p) / sub / exe for p in pf if p for sub in (("Git/cmd", "Git/bin") if name == "git"
                                                                   else ("Perforce", "Perforce/DVCS"))]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which(name)


def _run(args: list[str], cwd: Path, timeout: int = _TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", f"{type(e).__name__}: {e}"


class NoVcs:
    kind = "none"

    def __init__(self, root: Path, why: str = "no git or Perforce workspace detected"):
        self.root, self.why = Path(root), why

    def info(self) -> dict:
        return {"vcs": self.kind, "root": str(self.root), "note": self.why}

    def status(self, path: str) -> dict:
        return {"vcs": self.kind, "tracked": False, "state": "unknown", "note": self.why}

    def have_rev(self, path: str) -> str | None:
        return None

    def _no(self, *_a) -> dict:
        return {"error": self.why}
    edit = add = revert = stage = _no


class _Base(NoVcs):
    exe_name = ""

    def __init__(self, root: Path):
        super().__init__(root)
        self.exe = _exe(self.exe_name)

    def _abs(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"{path} is outside the workspace")
        return p

    def _call(self, *args: str) -> tuple[int, str, str]:
        if not self.exe:
            return -1, "", f"{self.exe_name} not found (set NNCQ_{self.exe_name.upper()} or install it)"
        return _run([self.exe, *args], self.root)

    def info(self) -> dict:
        return {"vcs": self.kind, "root": str(self.root), "exe": self.exe or f"{self.exe_name} not found"}

    def _result(self, rc: int, out: str, err: str, path: str) -> dict:
        res = {"vcs": self.kind, "ok": rc == 0, "output": (out + err).strip()[:2000]}
        if rc == 0:
            res["status"] = self.status(path)
        else:
            res["error"] = (err or out).strip()[:500] or f"exit {rc}"
        return res


class Git(_Base):
    kind, exe_name = "git", "git"

    def status(self, path: str) -> dict:
        rc, out, err = self._call("status", "--porcelain=v1", "--ignored", "--", str(self._abs(path)))
        if rc != 0:
            return {"vcs": "git", "tracked": False, "state": "unknown", "error": (err or out).strip()[:300]}
        code = out[:2] if out else ""
        state = {"": "clean", "??": "untracked", "!!": "ignored"}.get(code) or (
            "added" if "A" in code else "deleted" if "D" in code else "modified")
        return {"vcs": "git", "tracked": code not in ("??", "!!"), "state": state, "staged": bool(code[:1].strip())
                and code not in ("??", "!!")}

    def have_rev(self, path: str) -> str | None:
        rel = self._abs(path).relative_to(self.root.resolve()).as_posix()
        rc, out, _ = self._call("rev-parse", f"HEAD:{rel}")
        return out.strip() if rc == 0 and out.strip() else None

    def edit(self, path: str) -> dict:
        return {"vcs": "git", "ok": True, "note": "git: files are editable without checkout",
                "status": self.status(path)}

    def stage(self, path: str) -> dict:
        return self._result(*self._call("add", "--", str(self._abs(path))), path)
    add = stage

    def revert(self, path: str) -> dict:
        p = str(self._abs(path))
        if self.status(path)["state"] == "untracked":
            return {"vcs": "git", "ok": False, "error": "untracked file: nothing to revert to"}
        return self._result(*self._call("restore", "--staged", "--worktree", "--source=HEAD", "--", p), path)


class Perforce(_Base):
    kind, exe_name = "p4", "p4"

    def _fstat(self, path: str) -> dict:
        rc, out, err = self._call("-ztag", "fstat", str(self._abs(path)))
        d: dict[str, str] = {}
        for line in out.splitlines():
            if line.startswith("... "):
                k, _, v = line[4:].partition(" ")
                d.setdefault(k, v)
        if rc != 0 or not d:
            d["_error"] = (err or out).strip()[:300]
        return d

    def status(self, path: str) -> dict:
        d = self._fstat(path)
        if "_error" in d:
            return {"vcs": "p4", "tracked": False, "state": "untracked" if "no such file" in d["_error"] else "unknown",
                    "note": d["_error"]}
        st = f"opened:{d['action']}" if d.get("action") else "clean"
        if d.get("haveRev") and d.get("headRev") and d["haveRev"] != d["headRev"]:
            st += f" (have #{d['haveRev']} of #{d['headRev']})"
        return {"vcs": "p4", "tracked": "headRev" in d or "action" in d, "state": st, "depot": d.get("depotFile"),
                "have": d.get("haveRev"), "head": d.get("headRev")}

    def have_rev(self, path: str) -> str | None:
        h = self._fstat(path).get("haveRev")
        return f"#{h}" if h else None

    def edit(self, path: str) -> dict:
        return self._result(*self._call("edit", str(self._abs(path))), path)

    def add(self, path: str) -> dict:
        return self._result(*self._call("add", str(self._abs(path))), path)
    stage = add

    def revert(self, path: str) -> dict:
        return self._result(*self._call("revert", str(self._abs(path))), path)


def _up(root: Path):
    p = root.resolve()
    yield p
    yield from p.parents


def for_root(root: str | Path) -> NoVcs:
    """The workspace's VCS. git: a .git above the root; p4: a P4CONFIG file above it, else `p4 where` succeeds."""
    root = Path(root)
    try:
        if any((d / ".git").exists() for d in _up(root)):
            return Git(root)
        cfg = os.environ.get("P4CONFIG") or ".p4config"
        if any((d / cfg).is_file() for d in _up(root)):
            return Perforce(root)
        p4 = _exe("p4")
        if p4 and _run([p4, "where", str(root / "...")], root, 8)[0] == 0:
            return Perforce(root)
    except OSError as e:
        return NoVcs(root, f"VCS detection failed: {e}")
    return NoVcs(root)
