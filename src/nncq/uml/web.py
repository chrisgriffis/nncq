"""The Model explorer (/views; embedded in the dashboard as /views?embed=1): stored diagrams and compositions
grouped, rendered interactively with the vendored Mermaid (vendor.py; ASCII + Mermaid text when absent), an overview
minimap, node details with click-through to related elements, callers / callees and source, deep links from exported
Markdown (#/uml/<type>/<element>?view=&v=&h=) and default diagrams for a target (#/uml/default?s=&p=), Markdown
export / preview. Custom views and compositions are authored over MCP only (uml_upsert). Local-machine actions
(loopback only, explicit clicks): reveal in folder, open in the editor, edit + save a source file with git / p4
buttons (vcs.py), write the .md export to cache/exports and open it in VS Code. Never reachable from MCP."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from nncq import config, repos, vcs
from nncq.uml import vendor


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return config.is_loopback(config.settings().host) and config.is_loopback(host or "")


def _repo(name: str):
    rs = repos.load()
    if name:
        return repos.get(name)
    if not rs:
        raise KeyError("no repo registered")
    return next(iter(rs.values()))


def _inside(r, path: str) -> Path:
    p = (Path(r.root) / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not p.is_relative_to(Path(r.root).resolve()):
        raise ValueError("path is outside the workspace")
    return p


def routes(tool) -> list[Route]:
    """tool(name, args) -> awaitable dict: the server's web_tool (same tools agents use)."""

    async def page(request: Request):
        return HTMLResponse(PAGE.replace("__MERMAID__", "1" if vendor.path() else "0"))

    async def mermaid_js(request: Request):
        p = vendor.path()
        if p is None:
            return Response("mermaid not vendored: nncq uml init", status_code=404)
        return FileResponse(p, media_type="text/javascript", headers={"Cache-Control": "max-age=86400"})

    async def local(request: Request):
        """POST /api/uml/local/<op> {repo, path, ...}: reveal | open | read | save | vcs | export_md {key, open}."""
        if not _loopback(request):
            return JSONResponse({"error": "local actions are only available on a loopback host"}, status_code=403)
        import anyio
        try:
            a = await request.json()
        except ValueError:
            a = {}
        op = request.path_params["op"]
        try:
            return JSONResponse(await anyio.to_thread.run_sync(_local, op, a))
        except (KeyError, ValueError, OSError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"})

    async def build(request: Request):
        return JSONResponse(await tool("uml_build", {"repo": request.query_params.get("repo", "")}))

    return [Route("/views", page), Route("/vendor/mermaid.min.js", mermaid_js),
            Route("/api/uml/local/{op}", local, methods=["POST"]), Route("/api/uml/build", build, methods=["POST"])]


def _local(op: str, a: dict) -> dict:
    r = _repo(a.get("repo", ""))
    if op == "export_md":
        return _export_md(r, a.get("key", ""), a.get("open", ""))
    p = _inside(r, a.get("path", ""))
    rel = p.relative_to(Path(r.root).resolve()).as_posix()
    if op == "reveal":
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(p.parent)])
        return {"ok": True, "revealed": str(p)}
    if op == "open":
        if sys.platform == "win32":
            os.startfile(str(p))                     # noqa: S606 - explicit user click, file inside the workspace
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(p)])
        return {"ok": True, "opened": str(p)}
    if op == "read":
        data = p.read_bytes()
        return {"path": rel, "abs": str(p), "text": data.decode("utf-8", "replace"),
                "sha1": hashlib.sha1(data).hexdigest(), "vcs": vcs.for_root(r.root).status(rel),
                "readonly": not os.access(p, os.W_OK)}
    if op == "save":
        cur = p.read_bytes()
        if hashlib.sha1(cur).hexdigest() != a.get("sha1"):
            return {"error": "the file changed on disk since it was opened: reload before saving"}
        if not os.access(p, os.W_OK):
            return {"error": "file is read-only (p4: check it out first)"}
        text = a.get("text", "")
        if b"\r\n" in cur and "\r\n" not in text:
            text = text.replace("\n", "\r\n")
        p.write_bytes(text.encode("utf-8"))
        return {"ok": True, "path": rel, "sha1": hashlib.sha1(p.read_bytes()).hexdigest(),
                "vcs": vcs.for_root(r.root).status(rel)}
    if op == "vcs":
        v = vcs.for_root(r.root)
        what = a.get("op", "status")
        if what not in ("status", "edit", "add", "stage", "revert"):
            return {"error": f"unknown vcs op {what!r}"}
        out = getattr(v, what)(rel)
        return {**v.info(), **(out if isinstance(out, dict) else {"result": out})}
    return {"error": f"unknown op {op!r}"}


def _code_exe() -> str | None:
    """VS Code's command line launcher: `code` on PATH, else the per-user install."""
    import shutil
    exe = shutil.which("code")
    if exe:
        return exe
    for c in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
              Path(os.environ.get("ProgramFiles", "")) / "Microsoft VS Code" / "bin" / "code.cmd"):
        if c.is_file():
            return str(c)
    return None


def _export_md(r, key: str, open_with: str = "") -> dict:
    """Write a view's Markdown export to cache/exports/<repo>/<key>.md (overwritten) and optionally open it in
    VS Code. The file name is sanitized and confined to the exports folder."""
    import re
    from nncq.store import Store
    from nncq.uml import catalog
    st = Store(r.db, cache_mb=16)
    try:
        out = catalog.get(st, key, "markdown")
    finally:
        st.close()
    if "error" in out:
        return {"error": out["error"]}
    base = (config.CACHE / "exports" / re.sub(r"[^\w.-]+", "_", r.name)).resolve()
    dest = (base / (re.sub(r"[^\w.-]+", "_", out["key"]).strip("._")[:120] + ".md")).resolve()
    if not dest.is_relative_to(base):
        return {"error": "bad export name"}
    base.mkdir(parents=True, exist_ok=True)
    dest.write_text(out["markdown"], encoding="utf-8", newline="\n")
    res = {"ok": True, "path": str(dest), "hint": "Ctrl+Shift+V for preview (Mermaid needs the VS Code extension "
                                                    "'Markdown Preview Mermaid Support')"}
    if open_with == "vscode":
        exe = _code_exe()
        if exe is None:
            return {**res, "ok": False, "error": f"VS Code not found (no `code` on PATH, no per-user install); the "
                                                 f"file is at {dest}"}
        subprocess.Popen([exe, str(dest)], shell=exe.lower().endswith(".cmd"))    # noqa: S603 - explicit user click
    return res


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nncq · model explorer</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d0f13;--panel:#151821;--panel2:#0b0d11;--line:#232733;--fg:#d7dae0;--dim:#7d8490;--acc:#5aa9ff;--ok:#4cc38a;--warn:#e5a50a;--bad:#ef5b5b;font:13px/1.4 ui-sans-serif,Segoe UI,system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column}
button,select,input,textarea{font:inherit;color:var(--fg)}
button{background:#1d2230;border:1px solid var(--line);border-radius:6px;padding:3px 9px;cursor:pointer}button:hover{border-color:var(--acc)}
select,input,textarea{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 7px}
a{color:var(--acc);cursor:pointer;text-decoration:none}.dim{color:var(--dim)}.mono{font-family:ui-monospace,Cascadia Mono,Consolas,monospace;font-size:12px}
header{display:flex;gap:10px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--line)}header b{font-size:15px}header .sp{flex:1}
main{flex:1;display:grid;grid-template-columns:300px minmax(0,1fr) 360px;min-height:0}
.col{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}.col:last-child{border-right:0;border-left:1px solid var(--line)}
.sec{padding:8px 10px;border-bottom:1px solid var(--line)}.sec h4{margin:0 0 6px;font-size:11px;text-transform:uppercase;color:var(--dim);letter-spacing:.04em}
.scroll{overflow:auto;flex:1;min-height:0}
.grp{padding:6px 10px 2px;font-size:11px;color:var(--dim);text-transform:uppercase}
.vi{padding:4px 10px 4px 16px;cursor:pointer;display:flex;gap:6px;align-items:center;white-space:nowrap;overflow:hidden}.vi:hover{background:#1b1f29}.vi.on{background:#1d2a44}
.vi .t{overflow:hidden;text-overflow:ellipsis;flex:1}
.b{font-size:10px;padding:0 6px;border-radius:8px;background:#232733;color:var(--dim);flex:none}.b.w{background:#3a2d08;color:var(--warn)}.b.x{background:#3a1414;color:#ff8a8a}
.form{display:grid;grid-template-columns:auto 1fr;gap:5px 8px;align-items:center}
.vh{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}.vh .t{font-weight:600;font-size:14px}
.tabs{display:flex;background:var(--panel2);border-radius:7px;padding:2px}.tabs a{padding:2px 9px;border-radius:5px;color:var(--dim);font-size:12px}.tabs a.on{background:#1d2a44;color:var(--fg)}
#canvas{flex:1;overflow:auto;position:relative;background:var(--panel2)}#canvas svg{max-width:none;display:block;margin:10px}
#canvas pre{margin:0;padding:12px;font:12px/1.35 ui-monospace,Cascadia Mono,Consolas,monospace;white-space:pre}
#corner{display:flex;gap:4px;flex-wrap:wrap}.vh button{padding:2px 8px;font-size:12px;white-space:nowrap}body.embed .vh{gap:5px;padding:6px 8px}body.embed .vh button{padding:1px 6px;font-size:11px}body.embed .tabs a{padding:2px 6px}
.hl rect,.hl circle,.hl polygon,.hl path.basic{stroke:var(--acc)!important;stroke-width:3px!important}
#canvas g[data-n]{cursor:pointer}#canvas{cursor:grab}#canvas.panning{cursor:grabbing;user-select:none}#canvas.panning *{pointer-events:none}
.card{padding:10px 12px;border-bottom:1px solid var(--line)}.card .t{font-size:14px;font-weight:600;word-break:break-all}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 8px;font-size:12px;margin-top:6px}.kv span:nth-child(odd){color:var(--dim);text-align:right}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}.chip{font-size:11px;padding:2px 8px;border-radius:10px;background:#1a1e28;cursor:pointer}.chip:hover{color:var(--acc)}
.acts{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
pre.code{margin:0;padding:8px;font:12px/1.45 ui-monospace,Cascadia Mono,Consolas,monospace;white-space:pre;overflow:auto;max-height:46vh;background:var(--panel2)}
pre.code .hl{background:#2b2a14;display:block}
#notice{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:#3a2d08;color:#ffd987;border:1px solid var(--warn);padding:9px 16px;border-radius:9px;z-index:50;display:none;box-shadow:0 6px 30px #000a}
.modal{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;z-index:40}.modal.on{display:flex}
.mbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;width:min(1100px,94vw);height:min(760px,90vh);display:flex;flex-direction:column}
.mbox .mh{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line)}.mbox textarea{flex:1;margin:8px;resize:none;font:12px/1.45 ui-monospace,Cascadia Mono,Consolas,monospace;white-space:pre;tab-size:4}
.mbox .mf{padding:6px 12px;border-top:1px solid var(--line);font-size:12px}
.err{color:var(--bad)}
.emb{display:none!important}body.embed .emb{display:inline-block!important}body.embed .noemb{display:none!important}
#mstate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:0 1 auto}header>*{flex:none}header>#mstate{flex:0 1 auto}
body.embed header{padding:6px 10px;gap:8px}body.embed header b{font-size:13px}
#bpin{filter:grayscale(1);opacity:.45;padding:1px 6px}#bpin.on{filter:none;opacity:1;border-color:var(--acc);background:#1d2a44}
.hb{padding:1px 8px;color:var(--dim)}.hb:hover{color:var(--fg)}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #262b36;border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}@keyframes sp{to{transform:rotate(360deg)}}
.hint{margin-top:8px;font-size:11px;color:var(--dim)}.hint .mono{font-size:11px;color:#9fb3cc}
#mmbox{position:relative;height:170px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;overflow:hidden;cursor:grab}
#mmbox svg{position:absolute;left:0;top:0;pointer-events:none;max-width:none}#mmv{position:absolute;border:1.5px solid var(--acc);background:#5aa9ff1f;border-radius:2px;pointer-events:none}
.mdv{flex:1;overflow:auto;margin:8px;padding:6px 18px;background:var(--bg);border:1px solid var(--line);border-radius:6px;font-size:14px;line-height:1.55}
.mdv h1,.mdv h2,.mdv h3,.mdv h4{margin:.8em 0 .4em;font-weight:600}.mdv h3{font-size:17px}.mdv ul{padding-left:22px}.mdv code{font:12px ui-monospace,Cascadia Mono,Consolas,monospace;background:#ffffff12;padding:1px 4px;border-radius:4px}
.mdv pre{background:var(--panel2);padding:10px;border-radius:6px;overflow:auto}.mdv pre code{background:none;padding:0}.mdv .mmd{background:var(--panel2);padding:10px;border-radius:6px;overflow:auto}.mdv .mmd svg{max-width:none}
.mdv .cm{color:#5f6b7a;font:11px ui-monospace,Consolas,monospace}
pre .k,.mdv .k{color:#c792ea}pre .s,.mdv .s{color:#c3e88d}pre .c,.mdv .c{color:#5f6b7a;font-style:italic}pre .n,.mdv .n{color:#f78c6c}pre .pp,.mdv .pp{color:#89ddff}pre .h{color:#82aaff;font-weight:600}pre .ty{color:#ffcb6b}
.b.k-class{background:#1b2a44;color:#7cc0ff}.b.k-object{background:#13332e;color:#5fd4c0}.b.k-sequence{background:#14301f;color:#6fdc9e}.b.k-component{background:#26303f;color:#a9bbd6}
.b.k-file{background:#232a33;color:#b8c6d8;border:1px solid #3d4a5c}.b.o-agent{background:#f2f4f7;color:#000;font-weight:600}.b.o-user{background:#d7dae0;color:#111}.b.o-auto{background:#26303f;color:#a9bbd6}
.b.k-package{background:#262033;color:#c3a8f0}.b.k-deployment{background:#2d2a18;color:#e0cc6e}.b.k-composition{background:#33203a;color:#e39ff0}
.form input,.form select{min-width:0;width:100%}
@media (max-width:1100px){main{grid-template-columns:minmax(170px,24%) minmax(0,1fr) minmax(220px,30%)}}
@media (max-width:760px){main{grid-template-columns:minmax(150px,30%) minmax(0,1fr);grid-template-rows:minmax(0,1fr) minmax(120px,36%)}
 main>.col:last-child{grid-column:1/-1;border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<div id="notice"></div>
<header><b><span class="noemb">nncq · model explorer</span><span class="emb">Model explorer</span></b><a href="./" class="dim noemb">← dashboard</a>
 <button id="bpin" class="emb" title="pin: keep this diagram when the Symbol explorer moves (unpinned, the Model explorer follows it)">📌</button>
 <select id="repo" title="index"></select><span id="mstate" class="dim"></span><span class="sp"></span>
 <button id="bbuild" title="Re-derive the UML model from the index (only stale/changed parts); cached views regenerate on next open">⟳ Refresh</button>
 <a id="bsolo" class="emb hb" href="views" target="_blank" title="open the Model explorer on its own page">↗</a><button id="bcol" class="emb hb" title="collapse the Model explorer">–</button></header>
<main>
 <div class="col">
  <div class="sec"><h4>Overview <span id="mmz" class="dim" style="text-transform:none;letter-spacing:0"></span></h4>
   <div id="mmbox" title="the open diagram, scaled: click or drag to pan"><div class="dim" style="padding:8px;font-size:11px">no diagram</div></div>
   <div class="hint">Custom views: ask your agent (MCP <span class="mono">uml_protocol</span> / <span class="mono">uml_upsert</span>)</div></div>
  <div class="sec" style="display:flex;gap:6px"><input id="filter" placeholder="filter" style="flex:1"><select id="gb"><option value="kind">by kind</option><option value="group">by group</option><option value="origin">by origin</option></select></div>
  <div class="scroll" id="list"><div class="dim" style="padding:12px">loading…</div></div>
 </div>
 <div class="col" style="min-width:0">
  <div class="vh"><span class="t" id="vt">No diagram open</span><span id="vb"></span><span class="sp" style="flex:1"></span>
   <div class="tabs" id="tabs"><a data-t="diagram" class="on">Diagram</a><a data-t="ascii">ASCII</a><a data-t="mermaid">Mermaid</a><a data-t="json">JSON</a><a data-t="report">Report</a></div>
   <span id="corner"><button id="zo" title="zoom out (wheel, key -)">−</button><button id="zi" title="zoom in (wheel, key +)">+</button><button id="zf" title="fit (double-click the background, key 0)">fit</button><button id="z1" title="actual size (key 1)">100%</button><button id="bmd" title="Markdown export: Mermaid + element links"><span class="noemb">Generate </span>.md</button><button id="bpv" title="preview the exported Markdown as a Markdown viewer renders it">Preview<span class="noemb"> .md</span></button><button id="bvs" title="write the .md to cache/exports and open it in VS Code (Ctrl+Shift+V previews it; Mermaid needs the 'Markdown Preview Mermaid Support' extension)"><span class="noemb">Open in </span>VS Code</button></span><button id="bdel" title="delete this stored view / composition">Delete</button></div>
  <div id="canvas"><div id="out"><div class="dim" style="padding:20px">Pick a diagram or generate one.</div></div></div>
 </div>
 <div class="col"><div class="scroll" id="side"><div class="dim" style="padding:14px">Click a node for details.</div></div></div>
</main>
<div class="modal" id="mod"><div class="mbox"><div class="mh"><b id="mt"></b><span class="sp" style="flex:1"></span><span id="mbtns"></span><button id="mclose">Close</button></div>
 <textarea id="mtext" spellcheck="false"></textarea><div id="mview" class="mdv" hidden></div><div class="mf" id="mstat"></div></div></div>
<script>
const QS=new URLSearchParams(location.search);
if(QS.get('debug')==='1'){const E=window.__errs=[];addEventListener('error',e=>E.push(String(e.message)));addEventListener('unhandledrejection',e=>E.push('rejection: '+String(e.reason&&e.reason.message||e.reason)))}
const EMBED=QS.has('embed');if(EMBED)document.body.classList.add('embed');
/* embedded in the dashboard (same origin): the host page's API for cross-navigation, collapsing and hash mirroring */
const host=()=>{try{return EMBED&&parent!==window&&parent.nncqHost||null}catch(e){return null}};
const MERMAID=__MERMAID__===1;const tok=QS.get('token');const H=tok?{Authorization:'Bearer '+tok}:{};
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let repo='',cur=null,tab='diagram',zoom=1,selN=null,mm=0;
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
async function T(name,args){const j=await api('api/tool/'+name,{...args,...(repo?{repo}:{})});if(j&&j.error)throw new Error(j.error+(j.fix?' — '+j.fix:''));return j}
const L=(op,a)=>api('api/uml/local/'+op,{...a,repo});
function notice(t){const n=$('#notice');n.textContent=t;n.style.display='block';setTimeout(()=>document.addEventListener('click',()=>n.style.display='none',{once:true,capture:true}),50)}
const MCFG={startOnLoad:false,theme:'dark',securityLevel:'strict',maxTextSize:500000,flowchart:{htmlLabels:true},
 themeVariables:{darkMode:true,background:'#0b0d11',primaryColor:'#1b2a44',primaryTextColor:'#d7dae0',primaryBorderColor:'#5aa9ff',lineColor:'#8b95a7',
  secondaryColor:'#14301f',tertiaryColor:'#151821',clusterBkg:'#ffffff07',clusterBorder:'#3d4658',fontFamily:'Segoe UI, system-ui, sans-serif',fontSize:'13px',
  noteBkgColor:'#2b2a14',noteTextColor:'#e8e2b0',actorBkg:'#14301f',actorBorder:'#4cc38a',signalColor:'#8b95a7',signalTextColor:'#d7dae0'}};
if(MERMAID){const s=document.createElement('script');s.src='vendor/mermaid.min.js';s.onload=()=>{mermaid.initialize(MCFG);if(cur)draw()};document.head.appendChild(s)}

/* ---------- repos, model state, list ---------- */
async function init(){try{const r=await fetch('api/status',{headers:H});const d=await r.json();const rs=(d.repos||[]).map(x=>x.name);
  repo=localStorage.getItem('nncq.repo')||rs[0]||'';if(!rs.includes(repo))repo=rs[0]||'';
  $('#repo').innerHTML=rs.map(n=>`<option ${n===repo?'selected':''}>${esc(n)}</option>`).join('');
  const st=((d.repos||[]).find(x=>x.name===repo)||{}).status||{};const u=st.uml||{};
  $('#mstate').textContent=u.built_at?`model ${u.built_at} · ${u.components} components · ${u.views} stored${u.stale_views?` · ${u.stale_views} stale`:''}`:(u.note||'UML model not built');}catch(e){}
 await list();route()}
$('#repo').onchange=e=>{repo=e.target.value;localStorage.setItem('nncq.repo',repo);init()};
const pinned=()=>localStorage.getItem('nncq.mpin')==='1';
function pinUi(){$('#bpin').classList.toggle('on',pinned())}
$('#bpin').onclick=()=>{localStorage.setItem('nncq.mpin',pinned()?'0':'1');pinUi()};pinUi();
$('#bcol').onclick=()=>{const h=host();if(h)h.collapse('model')};
$('#bsolo').onclick=()=>{$('#bsolo').href='views'+(tok?'?token='+encodeURIComponent(tok):'')+location.hash};
$('#bbuild').onclick=async()=>{const b=$('#bbuild');b.disabled=true;b.innerHTML='<span class="spin"></span> Refreshing…';
 const done=()=>{b.disabled=false;b.textContent='⟳ Refresh';init()};
 try{const j=await api('api/uml/build?repo='+encodeURIComponent(repo));$('#mstate').textContent=j.error||('refresh: '+(j.reason||j.state||'started'));if(j.error)return done()}catch(e){return done()}
 const t0=Date.now();const tick=async()=>{try{const d=await (await fetch('api/status',{headers:H,cache:'no-store'})).json();const r=(d.repos||[]).find(x=>x.name===repo)||{};
   if(r.job&&r.job.state==='running'&&Date.now()-t0<6e5)return setTimeout(tick,2000)}catch(e){}done()};setTimeout(tick,1500)};
async function list(){let d;try{d=await T('uml_views',{group_by:$('#gb').value})}catch(e){$('#list').innerHTML=`<div class="err" style="padding:10px">${esc(e.message)}</div>`;return}
 const f=$('#filter').value.toLowerCase();let h='';
 for(const [g,vs] of Object.entries(d.groups||{})){const xs=vs.filter(v=>!f||(v.title+v.key).toLowerCase().includes(f));if(!xs.length)continue;
  h+=`<div class="grp">${esc(g)} · ${xs.length}</div>`+xs.map(v=>`<div class="vi ${cur&&cur.key===v.key?'on':''}" data-k="${esc(v.key)}" title="${esc(v.key)}">${kindBadge(v)}${originBadge(v)}<span class="t">${esc(v.title)}</span>${v.stale?`<span class="b w" title="${esc(typeof v.stale==='string'?v.stale:'stale: see Report')}">stale</span>`:''}</div>`).join('')}
 $('#list').innerHTML=h||'<div class="dim" style="padding:12px">No stored diagrams yet: pick a symbol or file (its default diagram is made on demand), or let an agent compose one (uml_upsert).</div>'}
$('#filter').oninput=list;$('#gb').onchange=list;
$('#list').onclick=e=>{const v=e.target.closest('[data-k]');if(v)go(v.dataset.k)};

/* ---------- open / draw ---------- */
const isFile=v=>String(v.key||'').startsWith('composition:file:');
const originBadge=v=>v.origin==='auto'?'<span class="b o-auto" title="generated from the index, on demand; regenerated when its sources change">auto</span>':`<span class="b o-${esc(v.origin)}" title="${esc(v.provenance?v.provenance.tag+'\n'+v.provenance.detail:'made via MCP')}">${v.origin==='agent'?'mcp-agent':esc(v.origin)}</span>`;
const kindBadge=v=>isFile(v)?'<span class="b k-file" title="file composition (generated on demand)">file</span>':`<span class="b k-${esc(v.kind)}">${esc(v.kind)}</span>`;
function go(key,q){location.hash='#/uml/view/'+encodeURIComponent(key.startsWith('composition:')?key:'view:'+key)+(q||'')}
async function open(key){try{const d=await T('uml_view',{key,format:'json'});show(d);return d}catch(e){return null}}
function show(d){cur=d;selN=null;$('#vt').textContent=d.title;
 $('#vb').innerHTML=`${kindBadge(d)} ${originBadge(d)}`+(d.stale||(d.report&&d.report.staleness)?' <span class="b w">stale</span>':'')+(d.report&&d.report.unbound?' <span class="b x">unbound refs</span>':'');

 document.querySelectorAll('#list .vi').forEach(x=>x.classList.toggle('on',x.dataset.k===d.key));draw()}
document.querySelectorAll('#tabs a').forEach(a=>a.onclick=()=>{tab=a.dataset.t;document.querySelectorAll('#tabs a').forEach(x=>x.classList.toggle('on',x===a));draw()});
async function text(fmt){try{return (await T('uml_view',{key:cur.key,format:fmt}))[fmt==='ascii'?'diagram':fmt]}catch(e){return e.message}}
async function draw(){if(!cur)return;const o=$('#out');
 mini();
 if(tab==='json'){o.innerHTML=`<pre>${hlc(JSON.stringify(cur.spec?{spec:cur.spec,model:cur.model}:cur.model,null,1),'json')}</pre>`;return}
 if(tab==='report'){o.innerHTML=`<pre>${hlc(JSON.stringify(cur.report||{status:'up to date'},null,1),'json')}</pre>`;return}
 if(tab==='ascii'||(tab==='diagram'&&!MERMAID)){o.innerHTML=`<pre>${esc(await text('ascii'))}</pre>`+(MERMAID?'':'<div class="dim" style="padding:8px 12px">Mermaid is not vendored (nncq uml init fetches it once): showing ASCII.</div>');nodeLinks();return}
 const src=await text('mermaid');if(tab==='mermaid'){o.innerHTML=`<pre>${hlc(src,'mermaid')}</pre>`;return}
 if(!window.mermaid){o.innerHTML='<div class="dim" style="padding:20px">loading renderer…</div>';return}
 try{const {svg}=await mermaid.render('mm'+(++mm),src);o.innerHTML=svg;restoreView();wire();mini()}catch(e){o.innerHTML=`<div class="err" style="padding:12px">Mermaid: ${esc(e.message||e)}</div><pre>${esc(src)}</pre>`}}
function zoomTo(z){zoom=z;const s=$('#out svg');if(s){const w=s.viewBox&&s.viewBox.baseVal?s.viewBox.baseVal.width:s.getBBox().width;s.style.width=(w*z)+'px';s.style.maxWidth='none';s.removeAttribute('height')}mmView()}
/* ---------- overview: a scaled live copy of the diagram with the visible area; click / drag pans ---------- */
let mmk=1;
function mini(){const box=$('#mmbox'),s=$('#out svg');if(!s||tab!=='diagram'){box.innerHTML='<div class="dim" style="padding:8px;font-size:11px">'+(cur?'overview: Diagram tab only':'no diagram')+'</div>';$('#mmz').textContent='';return}
 const vb=s.viewBox&&s.viewBox.baseVal,w=vb&&vb.width||s.getBBox().width,h=vb&&vb.height||s.getBBox().height,bw=box.clientWidth,bh=box.clientHeight;
 const k=Math.min(bw/w,bh/h),c=s.cloneNode(true),id='mmini';c.id=id;c.removeAttribute('data-n');
 c.querySelectorAll('style').forEach(x=>x.textContent=x.textContent.split('#'+s.id).join('#'+id));c.querySelectorAll('[data-n]').forEach(x=>x.removeAttribute('data-n'));
 c.style.width=(w*k)+'px';c.style.height=(h*k)+'px';c.style.left=((bw-w*k)/2)+'px';c.style.top=((bh-h*k)/2)+'px';
 box.innerHTML='';box.appendChild(c);box.insertAdjacentHTML('beforeend','<div id="mmv"></div>');mmk=k;mmView()}
function mmView(){const s=$('#out svg'),v=$('#mmv'),c=$('#mmini');if(!s||!v||!c)return;const cv=$('#canvas'),sr=s.getBoundingClientRect(),cr=cv.getBoundingClientRect();
 const sc=c.clientWidth/Math.max(1,sr.width);
 const x=(cr.left-sr.left)*sc,y=(cr.top-sr.top)*sc,w=cv.clientWidth*sc,h=cv.clientHeight*sc,ox=parseFloat(c.style.left),oy=parseFloat(c.style.top);
 const L=Math.max(0,x),T=Math.max(0,y),R=Math.min(c.clientWidth,x+w),B=Math.min(c.clientHeight,y+h);
 Object.assign(v.style,{left:(ox+L)+'px',top:(oy+T)+'px',width:Math.max(4,R-L)+'px',height:Math.max(4,B-T)+'px'});$('#mmz').textContent=Math.round(zoom*100)+'%'}
function mmPan(ev){const s=$('#out svg'),c=$('#mmini');if(!s||!c)return;const cv=$('#canvas'),br=c.getBoundingClientRect(),sc=s.getBoundingClientRect().width/Math.max(1,c.clientWidth);
 const px=(ev.clientX-br.left)*sc,py=(ev.clientY-br.top)*sc,off=s.getBoundingClientRect(),cr=cv.getBoundingClientRect();
 cv.scrollLeft+=px-(cr.left-off.left)-cv.clientWidth/2;cv.scrollTop+=py-(cr.top-off.top)-cv.clientHeight/2;mmView()}
$('#mmbox').addEventListener('pointerdown',e=>{if(!$('#mmini'))return;e.preventDefault();const b=$('#mmbox');b.setPointerCapture(e.pointerId);mmPan(e);
 const mv=ev=>mmPan(ev),up=()=>{b.removeEventListener('pointermove',mv);b.removeEventListener('pointerup',up)};b.addEventListener('pointermove',mv);b.addEventListener('pointerup',up)});
$('#canvas').addEventListener('scroll',()=>{mmView();if(!pan||panned)saveView()});addEventListener('resize',()=>{if(cur&&tab==='diagram')mini()});
/* ---------- pan / zoom on the SVG itself (width from its viewBox: crisp at any zoom); state kept per view ---------- */
const VS={},CV=$('#canvas');
function svgSize(){const s=$('#out svg'),vb=s.viewBox&&s.viewBox.baseVal;return {w:vb&&vb.width||s.getBBox().width,h:vb&&vb.height||s.getBBox().height}}
function saveView(){if(cur&&$('#out svg'))VS[cur.key]={z:zoom,x:CV.scrollLeft,y:CV.scrollTop}}
function fit(){if(!$('#out svg'))return;const {w,h}=svgSize();zoomTo(Math.max(.05,Math.min(2,(CV.clientWidth-24)/w,(CV.clientHeight-24)/h)));CV.scrollLeft=0;CV.scrollTop=0;saveView();mmView()}
function restoreView(){const v=cur&&VS[cur.key];if(!v||!CV.clientWidth)return fit();zoomTo(v.z);CV.scrollLeft=v.x;CV.scrollTop=v.y;mmView()}
function zoomAt(z,mx,my){const s=$('#out svg');if(!s)return;z=Math.max(.05,Math.min(8,z));const r=CV.getBoundingClientRect(),sr=s.getBoundingClientRect();
 mx=mx??r.left+CV.clientWidth/2;my=my??r.top+CV.clientHeight/2;const ox=sr.left-r.left+CV.scrollLeft,oy=sr.top-r.top+CV.scrollTop;
 const px=(CV.scrollLeft+mx-r.left-ox)/zoom,py=(CV.scrollTop+my-r.top-oy)/zoom;zoomTo(z);
 CV.scrollLeft=px*z+ox-(mx-r.left);CV.scrollTop=py*z+oy-(my-r.top);saveView();mmView()}
$('#zi').onclick=()=>zoomAt(zoom*1.25);$('#zo').onclick=()=>zoomAt(zoom/1.25);$('#zf').onclick=fit;$('#z1').onclick=()=>zoomAt(1);
CV.addEventListener('wheel',e=>{if(tab!=='diagram'||!$('#out svg'))return;e.preventDefault();zoomAt(zoom*Math.pow(1.0015,-e.deltaY),e.clientX,e.clientY)},{passive:false});
let pan=null,panned=false;     /* drag pans; a drag of 5px or more is a pan, so node clicks still work */
CV.addEventListener('pointerdown',e=>{if(e.button!==0||tab!=='diagram'||!$('#out svg'))return;pan={x:e.clientX,y:e.clientY,sx:CV.scrollLeft,sy:CV.scrollTop,id:e.pointerId};panned=false});
CV.addEventListener('pointermove',e=>{if(!pan)return;const dx=e.clientX-pan.x,dy=e.clientY-pan.y;if(!panned&&Math.hypot(dx,dy)<5)return;
 if(!panned){panned=true;try{CV.setPointerCapture(pan.id)}catch(x){}CV.classList.add('panning')}CV.scrollLeft=pan.sx-dx;CV.scrollTop=pan.sy-dy});
const endPan=()=>{pan=null;CV.classList.remove('panning')};CV.addEventListener('pointerup',endPan);CV.addEventListener('pointercancel',endPan);
CV.addEventListener('click',e=>{if(panned){e.stopPropagation();e.preventDefault();panned=false}},true);
CV.addEventListener('dblclick',e=>{if(tab==='diagram'&&!e.target.closest('[data-n]')){e.preventDefault();fit()}});
document.addEventListener('keydown',e=>{if(tab!=='diagram'||/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)||$('#mod').classList.contains('on')||e.ctrlKey||e.metaKey||e.altKey)return;
 const k=e.key;if(k==='+'||k==='=')zoomAt(zoom*1.25);else if(k==='-'||k==='_')zoomAt(zoom/1.25);else if(k==='0')fit();else if(k==='1')zoomAt(1);else return;e.preventDefault()});
function nodeOf(raw){if(!cur)return null;const id=raw.replace(/^n_/,'');const ns=cur.model.nodes;let n=ns.find(x=>x.id===id);
 if(!n)n=ns.find(x=>(x.ports||[]).some(p=>p.id===id));return n}
function wire(){const svg=$('#out svg');if(!svg)return;
 svg.querySelectorAll('[id]').forEach(el=>{const m=/(?:^|-)(n_[A-Za-z0-9_]+?)(?:-\d+)?$/.exec(el.id);if(!m)return;const n=nodeOf(m[1]);if(!n)return;
  el.setAttribute('data-n',n.id);el.addEventListener('click',ev=>{ev.stopPropagation();pick(n.id)})});
 if(cur.kind==='sequence')svg.querySelectorAll('text.actor,.actor-box text,text').forEach(t=>{const n=cur.model.nodes.find(x=>x.label===t.textContent.trim());if(n){t.style.cursor='pointer';t.addEventListener('click',()=>pick(n.id))}})}
function nodeLinks(){const pre=$('#out pre');if(!pre||!cur)return;pre.addEventListener('click',()=>{const s=getSelection().toString().trim();const n=cur.model.nodes.find(x=>x.label===s);if(n)pick(n.id)})}
function hl(id){document.querySelectorAll('#out .hl').forEach(x=>x.classList.remove('hl'));document.querySelectorAll(`#out [data-n="${CSS.escape(id)}"]`).forEach(x=>x.classList.add('hl'))}

/* ---------- node details: element, relations, callers / callees, source, local actions ---------- */
function loc(n){const r=n.ref||{};return r.path?{path:r.path,line:r.line||1,abs:r.abs,end:r.end}:null}
function elemId(n){if(n.element)return n.element;const r=n.ref||{};if(r.q)return 'sym:'+r.q;if(r.component)return 'comp:'+r.component;if(n.type==='package'&&r.path)return 'pkg:'+r.path;return ''}
async function pick(id){const n=cur.model.nodes.find(x=>x.id===id);if(!n)return;selN=id;hl(id);const l=loc(n),el=elemId(n);
 const rels=(cur.model.edges||[]).filter(e=>e.from===id||e.to===id).map(e=>{const o=e.from===id?e.to:e.from;const on=cur.model.nodes.find(x=>x.id===o);
  return `<span class="chip" data-go="${esc(o)}">${e.from===id?'→':'←'} ${esc(e.kind)} <b>${esc(on?on.label:o)}</b>${e.label?' · '+esc(e.label):''}</span>`}).join('');
 const msgs=(cur.model.messages||[]).filter(m=>m.from===id||m.to===id).slice(0,30).map(m=>`<span class="chip" data-msg="${esc(m.id)}">${m.from===id?'→':'←'} ${esc(m.label||m.kind)}</span>`).join('');
 const mem=(n.members||[]).slice(0,60).map(m=>`<span class="chip" data-mem="${esc(m.id)}">${esc(m.vis||'')}${esc(m.name)}${m.kind==='method'?'()':''}</span>`).join('');
 const sub=el.startsWith('composition:')||el.startsWith('view:')?`<span class="chip" data-open="${esc(el)}">open ${el.startsWith('view:')?'diagram':'composition'}</span>`:'';
 $('#side').innerHTML=`<div class="card"><div class="t">${esc(n.label)}</div><div class="kv">
   <span>type</span><span>${esc(n.type||'')}${n.stereotype?' «'+esc(n.stereotype)+'»':''}</span>${el?`<span>element</span><span class="mono">${esc(el)}</span>`:'<span>element</span><span class="dim">bespoke (only in this diagram / composition)</span>'}
   ${l?`<span>file</span><span class="mono" title="${esc(l.abs||'')}">${esc(l.abs||l.path)}:${l.line}</span>`:''}${n.stale?`<span>stale</span><span class="err">${esc(n.stale)}</span>`:''}${n.note?`<span>note</span><span>${esc(n.note)}</span>`:''}${n.decl?`<span>decl</span><span class="mono">${esc(n.decl)}</span>`:''}</div>
  <div class="acts">${l?`<button data-a="src">Source</button><button data-a="reveal" title="show in Explorer (this machine)">View in folder</button><button data-a="open">Open in editor</button><button data-a="edit" title="edit and save, with git / p4">Edit file</button>`:''}${(n.ref||{}).q?`<a class="chip" data-symx="${esc(n.ref.q)}" data-symp="${esc(n.ref.path||'')}" href="./#s=${encodeURIComponent(n.ref.q)}&p=${encodeURIComponent(n.ref.path||'')}&v=sym" title="open ${esc(n.ref.q)} in the Symbol explorer">Symbol explorer</a>`:''}${sub}</div>
  ${rels?`<div class="chips">${rels}</div>`:''}${msgs?`<div class="chips">${msgs}</div>`:''}${mem?`<div class="chips">${mem}</div>`:''}</div><div id="more"></div>`;
 $('#side').dataset.id=id;if((n.ref||{}).q)symbolCard(n.ref.q,n.ref.path)}
async function symbolCard(q,path){const box=$('#more');box.innerHTML='<div class="dim" style="padding:10px">…</div>';
 try{const d=await T('symbol_context',{name:q,path:path||''});if($('#more')!==box)return;
  const ch=(xs,lab)=>xs&&xs.length?`<div class="dim" style="margin-top:6px;font-size:11px">${lab}</div><div class="chips">${xs.slice(0,14).map(s=>`<span class="chip" data-sym="${esc(String(s).split(/\s+/)[0])}" title="${esc(s)}">${esc(String(s).split(/\s+/)[0])}</span>`).join('')}</div>`:'';
  const calls=(d.calls||[]).map(s=>{const m=/-> (\S+) /.exec(s);return m?m[1]:null}).filter(Boolean);
  box.innerHTML=`<div class="card">${ch(d.bases,'bases')}${ch((d.derived||[]),'derived')}${ch(d.callers,'called by')}${ch(calls,'calls')}</div>`}catch(e){box.innerHTML=''}}
$('#side').onclick=async e=>{const sx=e.target.closest('[data-symx]');const h=sx&&host();
 if(h&&!e.ctrlKey&&!e.shiftKey&&!e.metaKey){e.preventDefault();h.openSym(sx.dataset.symx,sx.dataset.symp);return}
 const t=e.target.closest('[data-go],[data-a],[data-mem],[data-sym],[data-open],[data-msg]');if(!t)return;
 if(t.dataset.go)return pick(t.dataset.go);
 if(t.dataset.open)return go(t.dataset.open.replace(/^view:/,''));
 const n=cur.model.nodes.find(x=>x.id===$('#side').dataset.id);
 if(t.dataset.msg){const m=cur.model.messages.find(x=>x.id===t.dataset.msg);if(m&&m.ref)return source(m.ref.path,m.ref.line);return}
 if(t.dataset.mem){const m=(n.members||[]).find(x=>x.id===t.dataset.mem);const r=(m&&(m.impl||m.ref))||{};if(r.path)source(r.path,r.line,r.end);return}
 if(t.dataset.sym){const s=t.dataset.sym;const hit=cur.model.nodes.find(x=>(x.ref||{}).q===s||x.label===s);if(hit)return pick(hit.id);
  try{const f=await T('find_symbol',{name:s});const m=/^(\S+) \[(\w+)\] (.+):(\d+)-(\d+)/.exec((f.definitions||[])[0]||'');if(m){symbolCard(m[1],m[3]);source(m[3],+m[4],+m[5])}}catch(x){}return}
 const l=loc(n);if(!l)return;const a=t.dataset.a;
 if(a==='src')return source(l.path,l.line,l.end);
 if(a==='edit')return editFile(l.path,l.line);
 const j=await L(a,{path:l.path});if(j.error)notice(j.error)};
async function source(path,line,end){const box=$('#more')||$('#side');const a=Math.max(1,(line||1)-6),b=end&&end-line<200?end+3:(line||1)+50;
 try{const d=await T('read_code',{path,start:a,end:b});const lg=langOf(path),rows=String(d.code||'').split('\n').map(r=>{const m=/^(\d+): ?(.*)$/.exec(r);return m?`<span class="${+m[1]===line?'hl':''}"><span class="c" style="font-style:normal">${m[1].padStart(5)}</span>  ${hlc(m[2],lg)}</span>`:esc(r)}).join('\n');
  box.innerHTML=`<div class="card"><div class="mono dim">${esc(path)}:${line}</div></div><pre class="code">${rows}</pre>`}catch(e){box.innerHTML=`<div class="err" style="padding:10px">${esc(e.message)}</div>`}}

/* ---------- editors: source file (+ git / p4) and diagram JSON ---------- */
let ed=null;
function modal(title,txt,btns,stat,html){$('#mt').textContent=title;$('#mtext').value=txt;$('#mtext').hidden=html!=null;$('#mview').hidden=html==null;
 if(html!=null){$('#mview').innerHTML=html;$('#mview').scrollTop=0}$('#mbtns').innerHTML=btns;$('#mstat').innerHTML=stat||'';$('#mod').classList.add('on')}
$('#mclose').onclick=()=>{$('#mod').classList.remove('on');ed=null};
async function editFile(path,line){const d=await L('read',{path});if(d.error)return notice(d.error);ed={type:'file',path,sha1:d.sha1};
 const v=d.vcs||{},isP4=v.vcs==='p4',isGit=v.vcs==='git';
 const b=`<button data-e="save">Save</button>`+(isP4?`<button data-v="edit">p4 edit</button><button data-v="add">p4 add</button><button data-v="revert">p4 revert</button>`:'')+(isGit?`<button data-v="stage">git stage</button><button data-v="revert">git revert</button>`:'');
 modal(d.abs,d.text,b,vstat(v,d.readonly));const ta=$('#mtext');const pos=d.text.split('\n').slice(0,Math.max(0,(line||1)-1)).join('\n').length;ta.focus();ta.setSelectionRange(pos,pos);ta.scrollTop=Math.max(0,((line||1)-8)*17)}
const vstat=(v,ro)=>`${esc(v.vcs||'no vcs')}: ${esc(v.state||'')}${ro?' · <span class="err">read-only</span>':''}${v.error||v.note?' · '+esc(v.error||v.note):''}`;
$('#mbtns').onclick=async e=>{const t=e.target.closest('button');if(!t||!ed)return;
 if(ed.type==='file'){if(t.dataset.e==='save'){const j=await L('save',{path:ed.path,text:$('#mtext').value,sha1:ed.sha1});if(j.error){$('#mstat').innerHTML=`<span class="err">${esc(j.error)}</span>`;return}ed.sha1=j.sha1;$('#mstat').innerHTML='saved · '+vstat(j.vcs||{});return}
  const j=await L('vcs',{path:ed.path,op:t.dataset.v});$('#mstat').innerHTML=j.error?`<span class="err">${esc(j.error)}</span>`:'ok · '+vstat(j.status||j);
  if(!j.error&&t.dataset.v==='revert'){const d=await L('read',{path:ed.path});if(!d.error){$('#mtext').value=d.text;ed.sha1=d.sha1}}return}
};
$('#bdel').onclick=async()=>{if(!cur||!confirm('Delete '+cur.key+'?'))return;try{await T('uml_delete',{key:cur.key});cur=null;$('#out').innerHTML='';list()}catch(e){notice(e.message)}};
$('#bmd').onclick=async()=>{if(!cur)return;const md=await text('markdown');ed=null;
 modal(cur.key+'.md',md,'<button id="cp">Copy</button><button id="dl">Download</button>','Same export as MCP uml_view(format=markdown): Mermaid with element links + link list.',`<pre class="code" style="max-height:none;margin:0">${hlc(md,'md')}</pre>`);
 $('#cp').onclick=async()=>{try{await navigator.clipboard.writeText(md);$('#mstat').textContent='copied'}catch(e){$('#mtext').select();document.execCommand('copy');$('#mstat').textContent='copied'}};
 $('#dl').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown'}));a.download=cur.key.replace(/[^\w.-]+/g,'_')+'.md';a.click()}};

/* ---------- light syntax colouring: C / C++ / Python / CMake / JSON / Mermaid / Markdown (no network) ---------- */
const KW={c:'if else for while do switch case default break continue return goto try catch throw new delete class struct enum union namespace using typedef template typename public private protected virtual override final static const constexpr inline extern volatile mutable explicit friend operator sizeof this nullptr true false void bool char int float double long short unsigned signed auto decltype noexcept',
 py:'def class return if elif else for while in not and or is None True False import from as with try except finally raise pass break continue lambda yield async await global nonlocal del assert self',
 cmake:'if elseif else endif foreach endforeach function endfunction macro endmacro set unset option project add_library add_executable target_link_libraries target_include_directories target_compile_definitions target_compile_options target_sources find_package include add_subdirectory message install PUBLIC PRIVATE INTERFACE STATIC SHARED',
 mermaid:'flowchart graph subgraph end classDiagram sequenceDiagram class direction style classDef click href participant actor note Note over link cssClass LR TB RL BT _blank',json:'true false null'};
const KWS={};for(const k in KW)KWS[k]=new Set(KW[k].split(' '));
const langOf=p=>/\.pyi?$/i.test(p)?'py':/(^|\/)CMakeLists\.txt$|\.cmake$/i.test(p)?'cmake':/\.md$/i.test(p)?'md':/\.json$/i.test(p)?'json':'c';
function hl1(line,lang){const cm={c:'//',py:'#',cmake:'#',mermaid:'%%'}[lang];let ci=-1;
 if(cm){let q=null;for(let j=0;j<line.length;j++){const ch=line[j];if(q){if(ch==='\\'){j++;continue}if(ch===q)q=null}else if(ch==='"'||(ch==="'"&&lang!=='cmake'&&lang!=='mermaid'))q=ch;else if(line.startsWith(cm,j)){ci=j;break}}}
 const code=ci>=0?line.slice(0,ci):line,kw=KWS[lang]||new Set();let out='',i=0,m;
 const re=/("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)|(\b\d[\w.]*\b)|([A-Za-z_][\w]*)|(#\s*\w+|\$\{[^}]*\}|-->|-\.->|--o|<\|--|\.\.>|==>)/g;
 while((m=re.exec(code))){out+=esc(code.slice(i,m.index));const t=m[0];
  if(m[1]){const key=lang==='json'&&/^\s*:/.test(code.slice(m.index+t.length));out+=`<span class="${key?'pp':'s'}">${esc(t)}</span>`}
  else if(m[2])out+=`<span class="n">${esc(t)}</span>`;
  else if(m[3])out+=kw.has(t)?`<span class="k">${t}</span>`:(lang==='c'&&/^[A-Z]\w*[a-z]\w*$/.test(t)?`<span class="ty">${t}</span>`:esc(t));
  else out+=(lang==='py'&&t[0]==='#')?esc(t):`<span class="pp">${esc(t)}</span>`;i=m.index+t.length}
 out+=esc(code.slice(i));if(ci>=0)out+=`<span class="c">${esc(line.slice(ci))}</span>`;return out}
function hlc(text,lang){const ls=String(text??'').replace(/\r/g,'').split('\n');if(lang!=='md')return ls.map(l=>hl1(l,lang)).join('\n');
 let fence=null;return ls.map(l=>{if(/^\s*```/.test(l)){fence=fence?null:(l.trim().slice(3)||'text');return `<span class="k">${esc(l)}</span>`}
  if(fence)return hl1(l,fence==='mermaid'?'mermaid':fence==='python'?'py':fence==='json'?'json':'c');
  if(/^\s*<!--/.test(l))return `<span class="c">${esc(l)}</span>`;if(/^#{1,6}\s/.test(l))return `<span class="h">${esc(l)}</span>`;
  return esc(l).replace(/^(\s*[-*]\s)/,'<span class="k">$1</span>').replace(/`([^`]+)`/g,'<span class="s">`$1`</span>').replace(/\[([^\]]+)\]\(([^)]+)\)/g,'[<span class="h">$1</span>](<span class="pp">$2</span>)')}).join('\n')}

/* ---------- Preview .md: the export rendered the way a Markdown viewer would (headings, lists, links, mermaid) ---------- */
function mdInline(t){return esc(t).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*([^*]+)\*/g,'$1<i>$2</i>')
 .replace(/(^|[^\w])_([^_]+)_(?=[^\w]|$)/g,'$1<i>$2</i>').replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(x,a,b)=>`<a href="${b}" target="_blank" rel="noopener">${a}</a>`)}
function mdHtml(md){const out=[],ls=String(md).replace(/\r/g,'').split('\n');let i=0,list=false,mmd=0;
 const close=()=>{if(list){out.push('</ul>');list=false}};
 while(i<ls.length){const l=ls[i];
  if(/^\s*```/.test(l)){close();const lang=l.trim().slice(3).trim(),body=[];i++;while(i<ls.length&&!/^\s*```/.test(ls[i]))body.push(ls[i++]);i++;
   out.push(lang==='mermaid'?`<div class="mmd" data-src="${esc(body.join('\n'))}" id="mdm${++mmd}"><span class="dim">rendering…</span></div>`:`<pre><code>${hlc(body.join('\n'),lang==='python'?'py':lang||'c')}</code></pre>`);continue}
  if(/^\s*<!--/.test(l)){close();let t=l;while(!/-->/.test(t)&&i+1<ls.length)t+='\n'+ls[++i];out.push(`<div class="cm">${esc(t)}</div>`);i++;continue}
  const h=/^(#{1,6})\s+(.*)$/.exec(l);if(h){close();out.push(`<h${h[1].length}>${mdInline(h[2])}</h${h[1].length}>`);i++;continue}
  const li=/^\s*[-*]\s+(.*)$/.exec(l);if(li){if(!list){out.push('<ul>');list=true}out.push(`<li>${mdInline(li[1])}</li>`);i++;continue}
  close();if(l.trim())out.push(`<p>${mdInline(l)}</p>`);i++}
 close();return out.join('\n')}
async function mdMermaid(box){for(const d of box.querySelectorAll('.mmd[data-src]')){try{mermaid.initialize({...MCFG,securityLevel:'antiscript'});
  const {svg}=await mermaid.render('pv'+(++mm),d.dataset.src);d.innerHTML=svg}catch(e){d.innerHTML=`<span class="err">Mermaid: ${esc(e.message||e)}</span>`}
  finally{mermaid.initialize(MCFG)}}}
$('#bpv').onclick=async()=>{if(!cur)return;const md=await text('markdown');ed=null;
 modal('Preview: '+cur.key+'.md',md,'<button id="cp">Copy .md</button>','Rendered like a Markdown preview; links to this page open here, others in a new tab.',mdHtml(md));
 $('#cp').onclick=async()=>{try{await navigator.clipboard.writeText(md);$('#mstat').textContent='copied'}catch(e){$('#mstat').textContent='copy failed: '+e.message}};
 if(window.mermaid)mdMermaid($('#mview'))};
$('#mview').addEventListener('click',e=>{const a=e.target.closest('a');if(!a)return;const href=a.getAttribute('href')||a.getAttribute('xlink:href')||'';const k=href.indexOf('/views#');
 if(k>=0&&href.startsWith(location.origin)){e.preventDefault();$('#mod').classList.remove('on');location.hash=href.slice(k+6)}},true);
$('#bvs').onclick=async()=>{if(!cur)return;const j=await L('export_md',{key:cur.key,open:'vscode'});
 notice(j.error?j.error:`Opened ${j.path} in VS Code: Ctrl+Shift+V for preview (Mermaid needs the "Markdown Preview Mermaid Support" extension).`)};

/* ---------- deep links: #/uml/<type>/<element>?view=<key>&v=<view rev>&h=<element rev>&repo=<repo> ----------
   #/uml/default?s=<symbol>&p=<file or folder> (or none: the workspace overview): the best diagram for that target */
let seq=0;
function useRepo(r){if(r&&r!==repo&&[...$('#repo').options].some(o=>o.value===r)){repo=r;$('#repo').value=r}}
async function dflt(s,p){const my=++seq;$('#out').innerHTML='<div class="dim" style="padding:20px"><span class="spin"></span> generating…</div>';
 $('#vt').textContent=s||p||'Workspace';$('#vb').innerHTML='';
 try{const d=await T('uml_view',{kind:'default',subject:s||'',path:p||'',format:'json'});if(my!==seq)return;show(d);
  const sn=s&&String(s).split(/::|\./).pop();
  const n=s&&(d.model.nodes.find(x=>(x.ref||{}).q===s)||d.model.nodes.find(x=>(x.ref||{}).q&&String(x.ref.q).split(/::|\./).pop()===sn&&x.type!=='module'));
  if(n)pick(n.id);else $('#side').innerHTML=`<div class="dim" style="padding:14px">Default diagram for ${esc(s||p||'the workspace')} (${esc(d.kind)}). Click a node for details.</div>`;
  if(!document.querySelector(`#list .vi[data-k="${CSS.escape(d.key)}"]`))list()}
 catch(e){if(my===seq){cur=null;$('#vt').textContent='No diagram';$('#out').innerHTML=`<div class="err" style="padding:20px">${esc(e.message)}</div>`}}}
async function route(){const h=host();if(h&&h.modelHash)h.modelHash(location.hash);
 const dm=/^#\/uml\/default\/?(?:\?(.*))?$/.exec(location.hash);
 if(!location.hash||location.hash==='#'||dm){const p=new URLSearchParams(dm&&dm[1]||'');useRepo(p.get('repo'));return dflt(p.get('s')||p.get('c')||'',p.get('p')||'')}
 const m=/^#\/uml\/([^/]+)\/([^?]*)(?:\?(.*))?$/.exec(location.hash);if(!m)return;
 const p=new URLSearchParams(m[3]||''),el=decodeURIComponent(m[2]);seq++;
 useRepo(p.get('repo'));
 let key=p.get('view')||'';if(m[1]==='view'&&!key)key=el.replace(/^view:/,'');
 if(m[1]==='view'&&!p.get('view'))key=el.startsWith('composition:')?el:el.replace(/^view:/,'');
 if(!key){const x=/^(sym|comp|pkg):(.+)$/.exec(el);if(x)return x[1]==='pkg'?dflt('',x[2]):dflt(x[2],'')}
 const d=key?await open(key):null;let stale=false;
 if(!d){stale=true;if(el.startsWith('view:')||el.startsWith('composition:'))await open(el.replace(/^view:/,''))}
 else{if(p.get('v')&&p.get('v')!==d.rev)stale=true;
  if(m[1]!=='view'){const n=d.model.nodes.find(x=>elemId(x)===el);if(n){pick(n.id);if(p.get('h')&&d.node_revs&&d.node_revs[n.id]!==p.get('h'))stale=true}else stale=true}}
 if(stale)notice('This link came from an outdated export: regenerate the diagram.')}
window.addEventListener('hashchange',route);
init();
</script></body></html>
"""
