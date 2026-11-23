"""Reproducible README screenshots: drives headless Edge (or Chrome) over the DevTools protocol against a running
nncq dashboard and writes cropped PNGs to docs/img/.

    nncq init --profile _demo --lite --yes && nncq index --name orbitkit && nncq start
    python tools/screens.py --url http://127.0.0.1:8765 [--only hero,ask] [--scale 1.5]

Each scene sets the page state (URL hash, clicks, typing), waits for it to render and captures an element or a
clip. Pillow (optional) quantises the PNGs to keep them small. Stdlib only otherwise: the websocket client below
is the minimum CDP needs (text frames, client masking).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"
BROWSERS = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            "/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium"]


# ---------------------------------------------------------------------------------------------- CDP plumbing

class Socket:
    """Just enough RFC 6455 for CDP: masked text frames out, (possibly fragmented) frames in."""

    def __init__(self, url: str):
        host, path = url[len("ws://"):].split("/", 1)
        h, p = host.rsplit(":", 1)
        self.s = socket.create_connection((h, int(p)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, obj) -> None:
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytearray([0x81])
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                                  else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError("devtools socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> dict:
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            msg += self._read(n)
            if b0 & 0x80:
                return json.loads(msg)


class Browser:
    def __init__(self, exe: str, port: int, profile: Path, width: int, height: int, scale: float):
        shutil.rmtree(profile, ignore_errors=True)
        self.proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                                      "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile}",
                                      f"--window-size={width},{height}", f"--remote-debugging-port={port}",
                                      "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("the browser did not open its DevTools port")
        self.ws = Socket(page["webSocketDebuggerUrl"])
        self.i = 0
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.resize(width, height, scale)

    def call(self, method: str, **params) -> dict:
        self.i += 1
        self.ws.send({"id": self.i, "method": method, "params": params})
        while True:
            m = self.ws.recv()
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def resize(self, width: int, height: int, scale: float) -> None:
        self.width, self.height, self.scale = width, height, scale
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale,
                  mobile=False)

    def js(self, expr: str, wait: float = 0.0):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(f"js error in {expr[:80]!r}: {d.get('exception', {}).get('description', d)}")
        if wait:
            time.sleep(wait)
        return r.get("result", {}).get("value")

    def goto(self, url: str, settle: float = 1.5) -> None:
        self.call("Page.navigate", url=url)
        self.until("document.readyState === 'complete'", 20)
        time.sleep(settle)

    def until(self, cond: str, timeout: float = 15.0) -> bool:
        expr = f"(()=>{{try{{return !!({cond})}}catch(e){{return false}}}})()"
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.15)
        print(f"   ! timed out waiting for: {cond[:120]}", file=sys.stderr)
        return False

    def rect(self, expr: str, pad: int = 0) -> dict | None:
        """Page box of the element (or {x,y,width,height} object) the JS expression returns."""
        r = self.js(f"""(()=>{{const e={expr};if(!e)return null;
            const b=e.getBoundingClientRect?e.getBoundingClientRect():e;
            return {{x:b.x+scrollX,y:b.y+scrollY,width:b.width,height:b.height}}}})()""")
        if not r:
            return None
        return {"x": max(0, r["x"] - pad), "y": max(0, r["y"] - pad),
                "width": r["width"] + 2 * pad, "height": r["height"] + 2 * pad, "scale": 1}

    def shot(self, name: str, clip: dict | None = None) -> Path:
        params = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = {k: (round(v, 1) if k != "scale" else v) for k, v in clip.items()}
        data = base64.b64decode(self.call("Page.captureScreenshot", **params)["data"])
        path = OUT / f"{name}.png"
        path.write_bytes(optimise(data))
        print(f"   {path.name}  {path.stat().st_size // 1024} KB")
        return path

    def mouse(self, x: float, y: float, kind: str = "mouseMoved", button: str = "none", clicks: int = 0) -> None:
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, clickCount=clicks)

    def hover(self, expr: str) -> bool:
        r = self.js(f"""(()=>{{const e={expr};if(!e)return null;const b=e.getBoundingClientRect();
            return [b.x+b.width/2,b.y+b.height/2]}})()""")
        if not r:
            return False
        self.mouse(r[0] - 30, r[1] - 30)
        self.mouse(*r)
        return True

    def close(self) -> None:
        try:
            self.call("Browser.close")
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def optimise(png: bytes) -> bytes:
    """Quantise to a 256-colour palette when Pillow is available (UI screenshots lose nothing visible)."""
    try:
        from PIL import Image
    except ImportError:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGB")
    q = im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    q.save(buf, "PNG", optimize=True)
    return buf.getvalue() if buf.tell() < len(png) else png
