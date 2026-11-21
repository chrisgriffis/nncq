"""OS-specific bits (Windows + Linux): user idle time, GPU free memory, detached processes."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input; None when unknown (then 'idle' mode never waits)."""
    if WINDOWS:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0
    if shutil.which("xprintidle") and os.environ.get("DISPLAY"):
        try:
            return int(subprocess.check_output(["xprintidle"], timeout=5)) / 1000.0
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def gpu_free_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.check_output([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                      timeout=10, stdin=subprocess.DEVNULL)
        return int(out.decode().split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def spawn(cmd: list[str], log: Path, detach: bool) -> subprocess.Popen:
    """Start a background process with output appended to log. detach: survives this process."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")
    kw: dict = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if WINDOWS:
        flags = subprocess.CREATE_NO_WINDOW
        if detach:
            flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = flags
    elif detach:
        kw["start_new_session"] = True
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        return subprocess.Popen(cmd, env=env, **kw)
    finally:
        out.close()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259                        # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid: int) -> None:
    if WINDOWS:
        subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
