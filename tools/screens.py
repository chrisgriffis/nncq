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
import urllib.parse
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


# ---------------------------------------------------------------------------------------------- page helpers

QUESTION = "where are frames with a bad CRC dropped"
FRAME = "document.querySelector('#mframe').contentDocument"


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def views_hash(key: str) -> str:
    return "#/uml/view/" + q(key if key.startswith("composition:") else "view:" + key)


def dash(b: Browser, base: str, hash_: str = "", panes: dict | None = None, wait: str = "", settle: float = 2.5):
    """The main page with fresh storage, optional pane widths, then a hash."""
    fresh(b)
    b.goto(base + "/", 0.5)
    b.js("localStorage.clear();sessionStorage.clear();" + (
        f"localStorage.setItem('nncq.panes',{json.dumps(json.dumps(panes))});" if panes else ""))
    b.goto(base + "/" + hash_, settle)
    if wait:
        b.until(wait, 20)
        time.sleep(1.0)


def fresh(b: Browser) -> None:
    """A new document (no state left over from the previous scene) and the mouse out of the way."""
    b.goto("about:blank", 0.2)
    b.mouse(1, 1)


def views(b: Browser, base: str, key: str = "", settle: float = 2.5, wait: str = "document.querySelector('#out svg')"):
    fresh(b)
    b.goto(base + "/views" + (views_hash(key) if key else ""), settle)
    if wait:
        b.until(wait, 20)
        time.sleep(1.2)


def tip(b: Browser, target: str, text_expr: str, dx: int = 8, dy: int = 22) -> None:
    """Headless browsers do not paint native title tooltips: draw one (same text) next to the hovered element."""
    b.js(f"""(()=>{{const e={target};if(!e)return;const r=e.getBoundingClientRect();const t=document.createElement('div');
      t.className='shot-tip';t.textContent={text_expr};
      Object.assign(t.style,{{position:'fixed',left:(r.left+{dx})+'px',top:(r.bottom+{dy - 18})+'px',zIndex:99,
        background:'#f4f4f4',color:'#111',font:'12px Segoe UI,system-ui,sans-serif',padding:'3px 7px',
        border:'1px solid #767676',boxShadow:'2px 2px 3px #0006',whiteSpace:'pre',pointerEvents:'none'}});
      document.body.appendChild(t)}})()""")


def union(b: Browser, *exprs: str, pad: int = 0) -> dict:
    rs = [r for r in (b.rect(e) for e in exprs) if r]
    x0, y0 = min(r["x"] for r in rs), min(r["y"] for r in rs)
    x1, y1 = max(r["x"] + r["width"] for r in rs), max(r["y"] + r["height"] for r in rs)
    return {"x": max(0, x0 - pad), "y": max(0, y0 - pad), "width": x1 - x0 + 2 * pad, "height": y1 - y0 + 2 * pad,
            "scale": 1}


# ---------------------------------------------------------------------------------------------- scenes

def scene_hero(b: Browser, base: str):
    """Three panes: an answered question, a symbol in the Symbol explorer, its diagram in the Model explorer."""
    b.resize(1920, 1080, 1)
    dash(b, base, f"#q={q(QUESTION)}&s={q('orbitkit::telemetry::FramedDecoder::decode')}",
         {"w": {"dash": 33, "sym": 31, "model": 36}}, wait=f"{FRAME}.querySelector('#out svg')", settle=3)
    b.js("const d=document.querySelector('#dashb');d.scrollTop=document.querySelector('#s-search').offsetTop-d.offsetTop-6", 0.6)
    b.shot("hero")


def scene_ask(b: Browser, base: str):
    """An answer with a path filter: how the question was read, the portfolio, the files; filtered panels outlined."""
    b.resize(1700, 1500, 1.25)
    dash(b, base, f"#q={q(QUESTION)}&qp=libs/telemetry", {"w": {"dash": 44, "sym": 28, "model": 28}},
         wait="document.querySelector('#answer .frow')")
    b.js("const d=document.querySelector('#dashb');d.scrollTop=document.querySelector('#s-search').offsetTop-d.offsetTop-6", 0.5)
    b.shot("ask", union(b, "document.querySelector('#s-search')", "document.querySelector('#answer')", pad=6))


def _sym(b: Browser, base: str, sym: str, mode: str = "outline", w=(26, 44, 30), size=(1700, 1100, 1.25)):
    b.resize(*size)
    dash(b, base, f"#s={q(sym)}", {"w": {"dash": w[0], "sym": w[1], "model": w[2]}},
         wait="document.querySelector('#scard .t')")
    b.js(f"document.querySelector('#tmode a[data-m={mode}]').click()", 1.8)


def scene_code(b: Browser, base: str):
    """The code view: every known symbol is a link (definition, call, type), with its target on hover."""
    _sym(b, base, "orbitkit::telemetry::FramedDecoder::decode")
    link = ("[...document.querySelectorAll('#sview .sym')].find(e=>e.textContent==='check'&&"
            "e.closest('.l').dataset.ln==='36')")
    b.hover(link)
    tip(b, link, f"({link}).title")
    r = union(b, "document.querySelector('#cpath')", "document.querySelector('#sview .l[data-ln=\"42\"]')")
    b.shot("code", {**r, "y": r["y"] - 4, "height": r["height"] + 8})


def scene_editor(b: Browser, base: str):
    """Edit in place: the code view turns into an editor with the workspace's VCS actions."""
    _sym(b, base, "orbitkit::telemetry::Pipeline::run")
    b.js("document.querySelector('#cpath a.ced').click()", 2)
    b.shot("editor", union(b, "document.querySelector('#cpath')", "document.querySelector('#cpath').parentElement.lastElementChild", pad=2))


def scene_outline(b: Browser, base: str):
    """Symbol explorer: the card of a symbol and the outline of its file."""
    _sym(b, base, "orbitkit::telemetry::FramedDecoder", "outline")
    b.shot("symbols-outline", union(b, "document.querySelector('#right .ph')", "document.querySelector('#tree')"))


def scene_callers(b: Browser, base: str):
    """Symbol explorer: callers of Crc::check, expanded up to main."""
    _sym(b, base, "orbitkit::core::Crc::check", "callers")
    for _ in range(6):
        b.js("""(()=>{const n=[...document.querySelectorAll('#tree .tn')].filter(x=>{const c=x.querySelector('.car');
             return c&&c.textContent==='\u25b8'&&!/TEST|test/.test(x.textContent)});if(n.length)n[n.length-1].querySelector('.car').click()})()""", 1.2)
    b.shot("symbols-callers", union(b, "document.querySelector('#sform')", "document.querySelector('#tree')"))


def scene_depends(b: Browser, base: str):
    """Symbol explorer: Depends of a type, as a graph thumbnail and as an outline that stops at cycles."""
    _sym(b, base, "orbitkit::sched::Tracker", "depends", size=(1700, 1300, 1.25))
    b.until("document.querySelector('#dthumb svg')", 15)
    time.sleep(1)
    b.shot("symbols-depends", union(b, "document.querySelector('#sform')", "document.querySelector('#tree')"))
    b.js("document.querySelector('#tree a[data-dv=outline]').click()", 1.5)
    for _ in range(3):
        b.js("""(()=>{const c=[...document.querySelectorAll('#tree .tn')].filter(x=>/Scheduler/.test(x.textContent)&&
             x.querySelector('.car')&&x.querySelector('.car').textContent==='\u25b8');if(c.length)c[0].querySelector('.car').click()})()""", 1.2)
    b.shot("symbols-depends-outline", union(b, "document.querySelector('#sform')", "document.querySelector('#tree')"))


def _canvas(b: Browser, pad: int = 0) -> dict:
    return union(b, "document.querySelector('#vt')", "document.querySelector('#out')", pad=pad)


def scene_class(b: Browser, base: str):
    """Model explorer: a class view (members, nested type, composition / aggregation), a picked node's details,
    and the inheritance tooltip of a type."""
    b.resize(1800, 1050, 1.25)
    views(b, base, "class:orbitkit::telemetry::Pipeline")
    b.js("[...document.querySelectorAll('#out g.node[data-n]')].find(g=>/Pipeline/.test(g.textContent)).dispatchEvent(new MouseEvent('click',{bubbles:true}))", 1.5)
    b.shot("model-class", union(b, "document.querySelector('#vt')", "document.querySelector('#out')", "document.querySelector('#side')"))


def scene_hierarchy(b: Browser, base: str):
    """Hovering a type in any diagram shows its inheritance (bases up, derived types down)."""
    b.resize(1500, 1000, 1.25)
    views(b, base, "class:orbitkit::telemetry::FramedDecoder")
    b.hover("[...document.querySelectorAll('#out g.node[data-n]')].find(g=>/^\\W*abstract\\W*FramedDecoder/.test(g.textContent.trim()))")
    b.until("getComputedStyle(document.querySelector('#htip')).display!=='none'", 6)
    time.sleep(0.8)
    b.shot("model-hierarchy", union(b, "document.querySelector('#vt')", "document.querySelector('#out')"))


def _model(b: Browser, base: str, key: str, name: str, size=(1700, 1000, 1.25), zoom: int = 0, full=False):
    b.resize(*size)
    views(b, base, key)
    for _ in range(abs(zoom)):
        b.js(f"document.querySelector('#{'zi' if zoom > 0 else 'zo'}').click()", 0.4)
    time.sleep(0.6)
    b.shot(name, None if full else _canvas(b))


def scene_sequence(b: Browser, base: str):
    _model(b, base, "sequence:orbitkit::sched::Scheduler::tick|depth=2", "model-sequence", (2300, 1350, 1))


def scene_component(b: Browser, base: str):
    _model(b, base, "component:orbit_telemetry", "model-component", (1500, 950, 1.25))


def scene_deployment(b: Browser, base: str):
    _model(b, base, "deployment:*", "model-deployment", (2300, 1150, 1))


def scene_filecomp(b: Browser, base: str):
    _model(b, base, "composition:file:libs/telemetry/src/Pipeline.cpp", "model-file", (2300, 1000, 1))


def scene_dependsgraph(b: Browser, base: str):
    _model(b, base, "depends:*|path=libs/sched/include/orbitkit/sched/Tracker.h", "model-depends", (1900, 1100, 1))


def scene_agent(b: Browser, base: str):
    """An agent-made composition: mcp-agent badge with its provenance, groups, bespoke parts, a nested diagram."""
    b.resize(1700, 1050, 1.25)
    views(b, base, "composition:ground-segment")
    badge = "document.querySelector('#vt').parentElement.querySelector('.b.o-agent')"
    b.hover(badge)
    tip(b, badge, f"({badge}).title")
    b.shot("model-agent", {"x": 0, "y": 0, "width": 1240, "height": 1050, "scale": 1})


def scene_minimap(b: Browser, base: str):
    """Pan / zoom: zoomed in, the overview minimap shows where you are; toolbar and key bindings."""
    b.resize(1600, 900, 1.25)
    views(b, base, "component:*")
    for _ in range(4):
        b.js("document.querySelector('#zi').click()", 0.3)
    time.sleep(0.8)
    b.shot("model-minimap", {"x": 0, "y": 0, "width": 1240, "height": 560, "scale": 1})


def scene_preview(b: Browser, base: str):
    """Preview .md: the Markdown export rendered like an editor preview, Mermaid included."""
    b.resize(1500, 1100, 1.25)
    views(b, base, "class:orbitkit::telemetry::FramedDecoder")
    b.js("document.querySelector('#bpv').click()", 3)
    b.until("document.querySelector('#mod svg, #mview svg')", 15)
    time.sleep(1)
    b.shot("model-preview", union(b, "document.querySelector('#mod').firstElementChild || document.querySelector('#mod')", pad=0))


def scene_status(b: Browser, base: str):
    """Index status in lite mode."""
    b.resize(1500, 900, 1.5)
    dash(b, base, "", {"w": {"dash": 40, "sym": 30, "model": 30}}, wait="document.querySelector('#status .ring, #status svg')")
    b.shot("status", union(b, "document.querySelector('#status')", pad=4))


SCENES = {"hero": scene_hero, "ask": scene_ask, "code": scene_code, "editor": scene_editor, "outline": scene_outline,
          "callers": scene_callers, "depends": scene_depends, "class": scene_class, "hierarchy": scene_hierarchy, "sequence": scene_sequence,
          "component": scene_component, "deployment": scene_deployment, "filecomp": scene_filecomp,
          "dependsgraph": scene_dependsgraph, "agent": scene_agent, "minimap": scene_minimap,
          "preview": scene_preview, "status": scene_status}


def main() -> int:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--only", default="", help="comma-separated scene names (default: all)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"))
    ap.add_argument("--browser", default="")
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()
    OUT = Path(a.out)
    OUT.mkdir(parents=True, exist_ok=True)
    exe = a.browser or next((x for x in BROWSERS if Path(x).is_file()), "")
    if not exe:
        raise SystemExit("no Edge / Chrome found: pass --browser")
    names = [n for n in a.only.split(",") if n] or list(SCENES)
    b = Browser(exe, a.port, ROOT / "cache" / "tmp" / "screens-browser", 1600, 1000, 1)
    try:
        for n in names:
            print(f"== {n}")
            SCENES[n](b, a.url.rstrip("/"))
    finally:
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
