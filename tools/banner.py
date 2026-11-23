"""Builds docs/img/banner.svg: the title, two rows of feature pills and, faded in the background, the demo dashboard
(docs/img/hero.png, from tools/screens.py). The screenshot is embedded (a JPEG data URI) because GitHub shows SVGs as
images, which never load external files. Re-run after replacing hero.png:

    python tools/banner.py            (needs Pillow)
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
W, H = 1200, 430

ROWS = [  # (label, colour): search and graph | the model explorer
    [("RAG over MCP", "#5aa9ff"), ("AST chunks", "#5aa9ff"), ("calls ⇄ called by", "#4cc38a"),
     ("hybrid search", "#4cc38a"), ("depends ↺ cycles", "#4cc38a"), ("code ↔ data xrefs", "#e5a50a"),
     ("air-gapped", "#e5a50a")],
    [("UML class", "#a78bfa"), ("sequence", "#a78bfa"), ("SysML components & pins", "#a78bfa"),
     ("release / debug deployment", "#c084fc"), ("agent compositions (MCP)", "#f472b6"),
     ("mermaid export", "#2dd4bf"), ("never stale (sync-aware)", "#2dd4bf")],
]


def text_width(s: str, size: float = 13) -> float:
    """Rough Segoe UI advance widths, enough to size a pill."""
    narrow, wide = set("il.,:;'|!()[] ftjI"), set("mwMW&@")
    return sum(size * (0.30 if c in narrow else 0.80 if c in wide else 0.56 if c.isupper() else 0.52) for c in s)


def background() -> str:
    im = Image.open(IMG / "hero.png").convert("RGB")
    scale = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    x, y = (im.width - W) // 2, 0
    im = im.crop((x, y, x + W, y + H)).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=58, optimize=True, progressive=True)
    return base64.b64encode(buf.getvalue()).decode()


def pills(y: int) -> str:
    out = []
    for r, row in enumerate(ROWS):
        x = 50.0
        for label, col in row:
            w = text_width(label) + 24
            yy = y + r * 32
            out.append(f'<rect x="{x:.1f}" y="{yy}" width="{w:.1f}" height="23" rx="11.5" fill="{col}" '
                       f'fill-opacity=".14" stroke="{col}" stroke-opacity=".55"/>'
                       f'<text x="{x + w / 2:.1f}" y="{yy + 16}" text-anchor="middle" fill="{col}" font-size="13">'
                       f'{label.replace("&", "&amp;")}</text>')
            x += w + 9
    return "".join(out)


def main() -> None:
    b64 = background()
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        'font-family="Segoe UI,Helvetica,Arial,sans-serif" role="img" '
        'aria-label="nncq: ask questions, don&#8217;t search the code">'
        '<title>nncq: ask questions, don&#8217;t search the code (click for the full dashboard)</title>'
        f'<defs><clipPath id="c"><rect width="{W}" height="{H}" rx="14"/></clipPath>'
        '<linearGradient id="v" x1="0" x2="0" y1="0" y2="1"><stop offset=".22" stop-color="#0d0f13" stop-opacity=".25"/>'
        '<stop offset=".5" stop-color="#0d0f13" stop-opacity=".84"/><stop offset="1" stop-color="#0d0f13" '
        'stop-opacity=".97"/></linearGradient>'
        '<linearGradient id="g" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#0d0f13" stop-opacity=".6"/>'
        '<stop offset=".6" stop-color="#0d0f13" stop-opacity=".15"/><stop offset="1" stop-color="#0d0f13" '
        'stop-opacity="0"/></linearGradient></defs>'
        f'<g clip-path="url(#c)"><rect width="{W}" height="{H}" fill="#0d0f13"/>'
        f'<image width="{W}" height="{H}" opacity=".55" href="data:image/jpeg;base64,{b64}"/>'
        f'<rect width="{W}" height="{H}" fill="url(#v)"/><rect y="190" width="{W}" height="{H - 190}" fill="url(#g)"/></g>'
        f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="14" fill="none" stroke="#2a2f3d"/>'
        '<circle cx="62" cy="234" r="9" fill="#4cc38a"/><circle cx="62" cy="234" r="15" fill="none" stroke="#4cc38a" '
        'stroke-opacity=".35" stroke-width="2"/>'
        '<text x="88" y="252" fill="#fff" font-size="64" font-weight="700" letter-spacing="1">nncq</text>'
        '<text x="256" y="250" fill="#7d8490" font-size="22">no-net-code-query</text>'
        '<text x="50" y="296" fill="#d7dae0" font-size="21">Ask questions, don&#8217;t search the code. '
        'See its model. Nothing leaves the machine.</text>'
        + pills(318) +
        f'<text x="{W - 22}" y="{H - 14}" text-anchor="end" fill="#d7dae0" fill-opacity=".75" font-size="12">'
        'click for the full dashboard ↗</text></svg>')
    (IMG / "banner.svg").write_text(svg, encoding="utf-8", newline="\n")
    print(f"banner.svg {len(svg) // 1024} KB")


if __name__ == "__main__":
    main()
