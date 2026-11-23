"""Run the bundled demo: index demo/orbitkit, serve it, seed the model once, open the dashboard.

    demo\\demo.cmd            Windows: double-click (first run sets up ..\\.venv)
    demo/demo.sh             Linux / macOS
    python demo/run.py [--fresh] [--no-seed] [--no-browser]

Before indexing it runs the asset step of `nncq init` (tree-sitter parsers, the pinned sha256-verified Mermaid build
for the dashboard's diagrams), which only touches the network when they are missing.

demo/ is an nncq home of its own: every nncq command below runs with `--home demo`, so the profile
(demo/profiles/orbitkit), settings (demo/.env: lite mode, port 8766), index and logs (demo/cache) all stay in this
folder and never touch the main install's profiles or indexes. Ctrl+C, or closing the window, stops the server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "orbitkit"
# dashboard state to open on: Model explorer on the class view of python/orbitkit_tools/frames.py, Symbol explorer on
# DecodeResult in Decoder.h, "encoder" in the ask box, symbol tab, panes not synced
START = ("#m=%2Fuml%2Fview%2Fview%253Aclass%253A*%257Cpath%253Dpython%252Forbitkit_tools%252Fframes.py"
         "&s=orbitkit%3A%3Atelemetry%3A%3ADecodeResult"
         "&p=libs%2Ftelemetry%2Finclude%2Forbitkit%2Ftelemetry%2FDecoder.h&q=encoder&v=sym&sync=0")


def nncq(*args: str) -> list[str]:
    return [sys.executable, "-m", "nncq", "--home", str(HERE), *args]


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "health", timeout=2) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fresh", action="store_true", help="re-parse everything and seed the model again")
    ap.add_argument("--no-seed", action="store_true", help="skip the generated diagrams / agent-made items")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))      # killed: still stop the server (finally below)
    from nncq import config
    config.use_home(HERE)
    url = f"http://127.0.0.1:{config.settings().port}/"

    h = health(url)
    if h and REPO in h.get("repos", []):
        print(f"== demo already running: {url}")
        if not a.no_browser:
            webbrowser.open(url + START)
        return 0

    from nncq import init
    init.assets()           # parsers + pinned Mermaid build (what `nncq init` fetches); no-op once present
    print(f"== indexing {HERE / REPO}")
    if subprocess.call(nncq("index", "--name", REPO, "--no-embed", *(["--fresh"] if a.fresh else []))):
        return 1

    print(f"== starting the demo server on {url}  (Ctrl+C stops it)")
    srv = subprocess.Popen(nncq("serve"))
    try:
        for _ in range(120):
            if health(url):
                break
            if srv.poll() is not None:
                print("!! server exited; see the messages above")
                return 1
            time.sleep(0.5)
        else:
            print("!! server did not answer within 60 s")
            srv.terminate()
            return 1

        marker = config.DATA_DIR / REPO / "demo-seeded"
        if not a.no_seed and (a.fresh or not marker.exists()):
            print("== seeding the model (generated diagrams, one agent-made view and composition)")
            import seed_model
            if asyncio.run(seed_model.main(url + "mcp", config.CACHE / "seed-out")) == 0:
                marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

        print(f"== demo ready: {url}{START}")
        if not a.no_browser:
            webbrowser.open(url + START)
        return srv.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if srv.poll() is None:
            srv.terminate()
            try:
                srv.wait(10)
            except subprocess.TimeoutExpired:
                srv.kill()


if __name__ == "__main__":
    raise SystemExit(main())
