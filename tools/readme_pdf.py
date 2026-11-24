"""README.md as a fully rendered PDF: GitHub-like styling, local images, Mermaid diagrams drawn, <details> opened,
page numbers. Drives headless Edge (or Chrome) over the DevTools protocol, reusing tools/screens.py's client.

    python tools/readme_pdf.py [--out README.pdf] [--paper letter|a4]

markdown-it-py and Pygments come with nncq's dependencies (via rich). Mermaid uses the pinned local build
(`nncq uml init` fetches it); without it the two diagrams print as their source text.
"""
from __future__ import annotations

import argparse
import base64
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screens import BROWSERS, ROOT, Browser  # noqa: E402

from markdown_it import MarkdownIt  # noqa: E402

PAPER = {"letter": (8.5, 11.0), "a4": (8.27, 11.69)}
CSS = """
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.5;
       color: #1f2328; margin: 0; }
h1, h2 { border-bottom: 1px solid #d1d9e0; padding-bottom: .3em; } h1 { font-size: 1.9em; }
h2 { font-size: 1.45em; margin-top: 1.6em; } h1, h2, h3, h4 { break-after: avoid; line-height: 1.25; }
a { color: #0969da; text-decoration: none; }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 88%; background: #eff1f3; padding: .15em .35em;
       border-radius: 5px; }
pre { background: #f6f8fa; padding: 10px 12px; border-radius: 6px; font-size: 8.6pt; line-height: 1.4;
      white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
pre code, .hl pre { background: none; padding: 0; font-size: inherit; }
.hl { background: #f6f8fa; border-radius: 6px; } .hl pre { margin: 0; padding: 10px 12px; }
pre.mermaid { background: none; text-align: center; white-space: normal; }
table { border-collapse: collapse; margin: 1em 0; font-size: 9.5pt; }
th, td { border: 1px solid #d1d9e0; padding: 5px 10px; vertical-align: top; } th { background: #f6f8fa; }
tr { break-inside: avoid; }
img { max-width: 100%; height: auto; break-inside: avoid; }
blockquote { border-left: .25em solid #d1d9e0; color: #59636e; margin: 0; padding: 0 1em; }
"""
# open every <details>, draw Mermaid, then wait for images; window.done tells the driver it can print
SCRIPT = """
document.querySelectorAll('details').forEach(d => d.open = true);
window.done = false;
(async () => {
  try { if (window.mermaid) { mermaid.initialize({startOnLoad: false, theme: 'default'});
                              await mermaid.run({querySelector: 'pre.mermaid'}); } } catch (e) { console.error(e); }
  await Promise.all([...document.images].map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; })));
  window.done = true;
})();
"""
FOOTER = ('<div style="font-size:8px;width:100%;text-align:center;color:#888">{title} · '
          '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')


def mermaid_js() -> Path | None:
    try:
        from nncq.uml import vendor
        return vendor.path()
    except Exception:
        return None


def code_block(code: str, lang: str, _attrs) -> str:
    if lang == "mermaid":
        return f'<pre class="mermaid">{html.escape(code)}</pre>'
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import TextLexer, get_lexer_by_name
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except Exception:
            lexer = TextLexer()
        return highlight(code, lexer, HtmlFormatter(cssclass="hl"))
    except ImportError:
        return ""                                       # markdown-it's plain <pre><code>


def anchor(m: re.Match) -> str:
    """GitHub-style heading ids so the README's in-page links work in the PDF."""
    text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
    slug = re.sub(r"[^\w\- ]", "", text.lower()).strip().replace(" ", "-")
    return f'<h{m.group(1)} id="{slug}">{m.group(2)}</h{m.group(1)}>'


def page(src: Path) -> str:
    md = MarkdownIt("commonmark", {"html": True, "highlight": code_block}).enable(["table", "strikethrough"])
    body = re.sub(r"<h([1-6])>(.*?)</h\1>", anchor, md.render(src.read_text(encoding="utf-8")))
    try:
        from pygments.formatters import HtmlFormatter
        css = CSS + HtmlFormatter().get_style_defs(".hl")
    except ImportError:
        css = CSS
    mjs = mermaid_js()
    return (f'<!doctype html><html><head><meta charset="utf-8"><base href="{src.parent.as_uri()}/">'
            f"<title>{html.escape(src.stem)}</title><style>{css}</style></head><body>{body}"
            + (f'<script src="{mjs.as_uri()}"></script>' if mjs else "")
            + f"<script>{SCRIPT}</script></body></html>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", default=str(ROOT / "README.md"))
    ap.add_argument("--out", default=str(ROOT / "README.pdf"))
    ap.add_argument("--paper", choices=sorted(PAPER), default="letter")
    ap.add_argument("--browser", default="")
    ap.add_argument("--port", type=int, default=9334)
    a = ap.parse_args()
    exe = a.browser or next((x for x in BROWSERS if Path(x).is_file()), "")
    if not exe:
        raise SystemExit("no Edge / Chrome found: pass --browser")
    src, out = Path(a.src).resolve(), Path(a.out).resolve()
    tmp = ROOT / "cache" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    doc = tmp / "readme-pdf.html"
    doc.write_text(page(src), encoding="utf-8")
    if not mermaid_js():
        print("   ! no local Mermaid build (nncq uml init): diagrams print as source", file=sys.stderr)

    b = Browser(exe, a.port, tmp / "readme-pdf-browser", 1200, 1000, 1)
    try:
        b.call("Emulation.clearDeviceMetricsOverride")
        b.goto(doc.as_uri(), settle=0)
        if not b.until("window.done === true", 60):
            raise SystemExit("the page did not finish rendering")
        broken = b.js("[...document.images].filter(i => !i.naturalWidth).map(i => i.getAttribute('src'))")
        for s in broken or []:
            print(f"   ! image not found: {s}", file=sys.stderr)
        w, h = PAPER[a.paper]
        data = b.call("Page.printToPDF", paperWidth=w, paperHeight=h, marginTop=0.63, marginBottom=0.63,
                      marginLeft=0.55, marginRight=0.55, printBackground=True, displayHeaderFooter=True,
                      headerTemplate="<span></span>",
                      footerTemplate=FOOTER.format(title=html.escape(src.stem)))["data"]
    finally:
        b.close()
        doc.unlink(missing_ok=True)
    out.write_bytes(base64.b64decode(data))
    print(f"   {out}  {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
