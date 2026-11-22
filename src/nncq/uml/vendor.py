"""A pinned Mermaid build for the dashboard, fetched once (by `nncq init` / `nncq uml init`) into
cache/tools/mermaid/<version>/ and verified by sha256; the dashboard serves it locally, so diagrams render with
no network. Without it the dashboard shows ASCII + Mermaid text instead."""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from nncq import config

VERSION = "11.17.2"
SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"
URLS = (f"https://cdn.jsdelivr.net/npm/mermaid@{VERSION}/dist/mermaid.min.js",
        f"https://unpkg.com/mermaid@{VERSION}/dist/mermaid.min.js")
_ok: dict[str, bool] = {}


def target() -> Path:
    return config.TOOLS_DIR / "mermaid" / VERSION / "mermaid.min.js"


def path() -> Path | None:
    """The verified local file, or None."""
    p = target()
    if not p.is_file():
        return None
    key = f"{p}:{p.stat().st_mtime_ns}"
    if key not in _ok:
        _ok[key] = hashlib.sha256(p.read_bytes()).hexdigest() == SHA256
    return p if _ok[key] else None


def fetch(log=print) -> bool:
    if path() is not None:
        log(f"== mermaid {VERSION}: present")
        return True
    p = target()
    p.parent.mkdir(parents=True, exist_ok=True)
    for url in URLS:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
        except OSError as e:
            log(f"   mermaid: {url}: {e}")
            continue
        if hashlib.sha256(data).hexdigest() != SHA256:
            log(f"   mermaid: {url}: sha256 mismatch, not used")
            continue
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        log(f"== mermaid {VERSION}: verified and stored in {p.parent} (dashboard diagrams, no network later)")
        return True
    log("!! mermaid not fetched: the dashboard shows ASCII and Mermaid text instead (retry: nncq uml init)")
    return False
