"""Dashboard Views page (/views): stored diagrams and compositions grouped, rendered interactively with the
vendored Mermaid (vendor.py; ASCII + Mermaid text when absent), node details with click-through to related
elements, callers / callees and source, deep links from exported Markdown (#/uml/<type>/<element>?view=&v=&h=),
"Generate .md". Local-machine actions (loopback only, explicit clicks): reveal in folder, open in the editor,
edit + save a source file with git / p4 buttons (vcs.py), edit a diagram's JSON. Never reachable from MCP."""
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

from nncq import answers, config, repos, vcs
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
        """POST /api/uml/local/<op> {repo, path, ...}: reveal | open | read | save | vcs | upsert."""
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
    if op == "upsert":
        from nncq.store import Store
        from nncq.uml import catalog
        spec = a.get("spec")
        if isinstance(spec, dict) and "members" in spec and "kind" not in spec:
            spec.setdefault("author", "user")
        st = Store(r.db, cache_mb=16)
        try:
            out = catalog.upsert(st, spec)
        finally:
            st.close()
        answers.bump()
        return out
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


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nncq · views</title>
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
#corner{position:absolute;right:10px;top:8px;display:flex;gap:6px;z-index:2}
.hl rect,.hl circle,.hl polygon,.hl path.basic{stroke:var(--acc)!important;stroke-width:3px!important}
#canvas g[data-n]{cursor:pointer}
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
</style></head><body>
<div id="notice"></div>
<header><b>nncq · views</b><a href="./" class="dim">← dashboard</a><select id="repo" title="index"></select><span id="mstate" class="dim"></span><span class="sp"></span>
 <button id="bbuild" title="build / refresh the UML model (CPU)">Build UML model</button></header>
<main>
 <div class="col">
  <div class="sec"><h4>New diagram from the index</h4><form class="form" id="fnew">
   <span class="dim">kind</span><select id="nk"><option>class</option><option>object</option><option>component</option><option>package</option><option>deployment</option><option>sequence</option></select>
   <span class="dim">subject</span><input id="ns" placeholder="type / component / function">
   <span class="dim">under</span><input id="nu" placeholder="folder filter (optional)">
   <span class="dim">depth</span><input id="nd" type="number" min="0" max="6" value="" placeholder="default">
   <span></span><button>Generate</button></form></div>
  <div class="sec" style="display:flex;gap:6px"><input id="filter" placeholder="filter" style="flex:1"><select id="gb"><option value="kind">by kind</option><option value="group">by group</option><option value="origin">by origin</option></select></div>
  <div class="scroll" id="list"><div class="dim" style="padding:12px">loading…</div></div>
 </div>
 <div class="col" style="min-width:0">
  <div class="vh"><span class="t" id="vt">No diagram open</span><span id="vb"></span><span class="sp" style="flex:1"></span>
   <div class="tabs" id="tabs"><a data-t="diagram" class="on">Diagram</a><a data-t="ascii">ASCII</a><a data-t="mermaid">Mermaid</a><a data-t="json">JSON</a><a data-t="report">Report</a></div>
   <button id="bedit" title="edit this diagram / composition as JSON">Edit</button><button id="bdel">Delete</button></div>
  <div id="canvas"><div id="corner"><button id="zo">−</button><button id="zi">+</button><button id="bmd" title="Markdown export: Mermaid + element links">Generate .md</button></div><div id="out"><div class="dim" style="padding:20px">Pick a diagram or generate one.</div></div></div>
 </div>
 <div class="col"><div class="scroll" id="side"><div class="dim" style="padding:14px">Click a node for details.</div></div></div>
</main>
<div class="modal" id="mod"><div class="mbox"><div class="mh"><b id="mt"></b><span class="sp" style="flex:1"></span><span id="mbtns"></span><button id="mclose">Close</button></div>
 <textarea id="mtext" spellcheck="false"></textarea><div class="mf" id="mstat"></div></div></div>
<script>
const MERMAID=__MERMAID__===1;const tok=new URLSearchParams(location.search).get('token');const H=tok?{Authorization:'Bearer '+tok}:{};
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let repo='',cur=null,tab='diagram',zoom=1,selN=null,mm=0;
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
async function T(name,args){const j=await api('api/tool/'+name,{...args,...(repo?{repo}:{})});if(j&&j.error)throw new Error(j.error+(j.fix?' — '+j.fix:''));return j}
const L=(op,a)=>api('api/uml/local/'+op,{...a,repo});
function notice(t){const n=$('#notice');n.textContent=t;n.style.display='block';setTimeout(()=>document.addEventListener('click',()=>n.style.display='none',{once:true,capture:true}),50)}
if(MERMAID){const s=document.createElement('script');s.src='vendor/mermaid.min.js';s.onload=()=>{mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'strict',maxTextSize:500000,flowchart:{htmlLabels:true}});if(cur)draw()};document.head.appendChild(s)}

/* ---------- repos, model state, list ---------- */
async function init(){try{const r=await fetch('api/status',{headers:H});const d=await r.json();const rs=(d.repos||[]).map(x=>x.name);
  repo=localStorage.getItem('nncq.repo')||rs[0]||'';if(!rs.includes(repo))repo=rs[0]||'';
  $('#repo').innerHTML=rs.map(n=>`<option ${n===repo?'selected':''}>${esc(n)}</option>`).join('');
  const st=((d.repos||[]).find(x=>x.name===repo)||{}).status||{};const u=st.uml||{};
  $('#mstate').textContent=u.built_at?`model ${u.built_at} · ${u.components} components · ${u.views} stored${u.stale_views?` · ${u.stale_views} stale`:''}`:(u.note||'UML model not built');}catch(e){}
 await list();route()}
$('#repo').onchange=e=>{repo=e.target.value;localStorage.setItem('nncq.repo',repo);init()};
$('#bbuild').onclick=async()=>{$('#bbuild').disabled=true;const j=await api('api/uml/build?repo='+encodeURIComponent(repo));$('#mstate').textContent=j.error||('build: '+(j.reason||'started — watch the dashboard console'));setTimeout(()=>{$('#bbuild').disabled=false;init()},4000)};
async function list(){let d;try{d=await T('uml_views',{group_by:$('#gb').value})}catch(e){$('#list').innerHTML=`<div class="err" style="padding:10px">${esc(e.message)}</div>`;return}
 const f=$('#filter').value.toLowerCase();let h='';
 for(const [g,vs] of Object.entries(d.groups||{})){const xs=vs.filter(v=>!f||(v.title+v.key).toLowerCase().includes(f));if(!xs.length)continue;
  h+=`<div class="grp">${esc(g)} · ${xs.length}</div>`+xs.map(v=>`<div class="vi ${cur&&cur.key===v.key?'on':''}" data-k="${esc(v.key)}" title="${esc(v.key)}"><span class="b">${esc(v.kind)}</span><span class="t">${esc(v.title)}</span>${v.origin!=='auto'?`<span class="b">${esc(v.origin)}</span>`:''}${v.stale?`<span class="b w" title="${esc(typeof v.stale==='string'?v.stale:'stale: see Report')}">stale</span>`:''}</div>`).join('')}
 $('#list').innerHTML=h||'<div class="dim" style="padding:12px">No stored diagrams yet. Generate one above, or let an agent compose one (uml_upsert).</div>'}
$('#filter').oninput=list;$('#gb').onchange=list;
$('#list').onclick=e=>{const v=e.target.closest('[data-k]');if(v)go(v.dataset.k)};
$('#fnew').onsubmit=async e=>{e.preventDefault();const a={kind:$('#nk').value,subject:$('#ns').value.trim(),under:$('#nu').value.trim(),format:'json'};if($('#nd').value)a.depth=+$('#nd').value;
 $('#out').innerHTML='<div class="dim" style="padding:20px">generating…</div>';try{const d=await T('uml_view',a);show(d);list()}catch(x){$('#out').innerHTML=`<div class="err" style="padding:20px">${esc(x.message)}</div>`}};

/* ---------- open / draw ---------- */
function go(key,q){location.hash='#/uml/view/'+encodeURIComponent(key.startsWith('composition:')?key:'view:'+key)+(q||'')}
async function open(key){try{const d=await T('uml_view',{key,format:'json'});show(d);return d}catch(e){return null}}
function show(d){cur=d;selN=null;zoom=1;$('#vt').textContent=d.title;
 $('#vb').innerHTML=`<span class="b">${esc(d.kind)}</span> <span class="b">${esc(d.origin)}</span>`+(d.stale||(d.report&&d.report.staleness)?' <span class="b w">stale</span>':'')+(d.report&&d.report.unbound?' <span class="b x">unbound refs</span>':'');
 $('#bedit').style.display=$('#bdel').style.display=d.origin==='auto'?'none':'';if(d.origin==='auto')$('#bdel').style.display='';
 document.querySelectorAll('#list .vi').forEach(x=>x.classList.toggle('on',x.dataset.k===d.key));draw()}
document.querySelectorAll('#tabs a').forEach(a=>a.onclick=()=>{tab=a.dataset.t;document.querySelectorAll('#tabs a').forEach(x=>x.classList.toggle('on',x===a));draw()});
async function text(fmt){try{return (await T('uml_view',{key:cur.key,format:fmt}))[fmt==='ascii'?'diagram':fmt]}catch(e){return e.message}}
async function draw(){if(!cur)return;const o=$('#out');
 if(tab==='json'){o.innerHTML=`<pre>${esc(JSON.stringify(cur.spec?{spec:cur.spec,model:cur.model}:cur.model,null,1))}</pre>`;return}
 if(tab==='report'){o.innerHTML=`<pre>${esc(JSON.stringify(cur.report||{status:'up to date'},null,1))}</pre>`;return}
 if(tab==='ascii'||(tab==='diagram'&&!MERMAID)){o.innerHTML=`<pre>${esc(await text('ascii'))}</pre>`+(MERMAID?'':'<div class="dim" style="padding:8px 12px">Mermaid is not vendored (nncq uml init fetches it once): showing ASCII.</div>');nodeLinks();return}
 const src=await text('mermaid');if(tab==='mermaid'){o.innerHTML=`<pre>${esc(src)}</pre>`;return}
 if(!window.mermaid){o.innerHTML='<div class="dim" style="padding:20px">loading renderer…</div>';return}
 try{const {svg}=await mermaid.render('mm'+(++mm),src);o.innerHTML=svg;zoomTo(zoom);wire()}catch(e){o.innerHTML=`<div class="err" style="padding:12px">Mermaid: ${esc(e.message||e)}</div><pre>${esc(src)}</pre>`}}
function zoomTo(z){zoom=z;const s=$('#out svg');if(s){const w=s.viewBox&&s.viewBox.baseVal?s.viewBox.baseVal.width:s.getBBox().width;s.style.width=(w*z)+'px';s.style.maxWidth='none';s.removeAttribute('height')}}
$('#zi').onclick=()=>zoomTo(zoom*1.25);$('#zo').onclick=()=>zoomTo(zoom/1.25);
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
  <div class="acts">${l?`<button data-a="src">Source</button><button data-a="reveal" title="show in Explorer (this machine)">View in folder</button><button data-a="open">Open in editor</button><button data-a="edit" title="edit and save, with git / p4">Edit file</button>`:''}${(n.ref||{}).q?`<a class="chip" href="./#s=${encodeURIComponent(n.ref.q)}&p=${encodeURIComponent(n.ref.path||'')}&v=sym" title="symbol explorer">explorer</a>`:''}${sub}</div>
  ${rels?`<div class="chips">${rels}</div>`:''}${msgs?`<div class="chips">${msgs}</div>`:''}${mem?`<div class="chips">${mem}</div>`:''}</div><div id="more"></div>`;
 $('#side').dataset.id=id;if((n.ref||{}).q)symbolCard(n.ref.q,n.ref.path)}
async function symbolCard(q,path){const box=$('#more');box.innerHTML='<div class="dim" style="padding:10px">…</div>';
 try{const d=await T('symbol_context',{name:q,path:path||''});if($('#more')!==box)return;
  const ch=(xs,lab)=>xs&&xs.length?`<div class="dim" style="margin-top:6px;font-size:11px">${lab}</div><div class="chips">${xs.slice(0,14).map(s=>`<span class="chip" data-sym="${esc(String(s).split(/\s+/)[0])}" title="${esc(s)}">${esc(String(s).split(/\s+/)[0])}</span>`).join('')}</div>`:'';
  const calls=(d.calls||[]).map(s=>{const m=/-> (\S+) /.exec(s);return m?m[1]:null}).filter(Boolean);
  box.innerHTML=`<div class="card">${ch(d.bases,'bases')}${ch((d.derived||[]),'derived')}${ch(d.callers,'called by')}${ch(calls,'calls')}</div>`}catch(e){box.innerHTML=''}}
$('#side').onclick=async e=>{const t=e.target.closest('[data-go],[data-a],[data-mem],[data-sym],[data-open],[data-msg]');if(!t)return;
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
 try{const d=await T('read_code',{path,start:a,end:b});const rows=String(d.code||'').split('\n').map(r=>{const m=/^(\d+): ?(.*)$/.exec(r);return m?`<span class="${+m[1]===line?'hl':''}">${m[1].padStart(5)}  ${esc(m[2])}</span>`:esc(r)}).join('\n');
  box.innerHTML=`<div class="card"><div class="mono dim">${esc(path)}:${line}</div></div><pre class="code">${rows}</pre>`}catch(e){box.innerHTML=`<div class="err" style="padding:10px">${esc(e.message)}</div>`}}

/* ---------- editors: source file (+ git / p4) and diagram JSON ---------- */
let ed=null;
function modal(title,txt,btns,stat){$('#mt').textContent=title;$('#mtext').value=txt;$('#mbtns').innerHTML=btns;$('#mstat').innerHTML=stat||'';$('#mod').classList.add('on')}
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
 if(ed.type==='spec'&&t.dataset.e==='save'){let spec;try{spec=JSON.parse($('#mtext').value)}catch(x){$('#mstat').innerHTML=`<span class="err">JSON: ${esc(x.message)}</span>`;return}
  const j=await L('upsert',{spec});if(j.error){$('#mstat').innerHTML=`<span class="err">${esc(j.error)}</span><br>${esc((j.errors||[]).join('; '))}`;return}
  $('#mstat').textContent=`${j.result}: ${j.key}`+(j.unbound?` · unbound: ${j.unbound.map(x=>x.ref).join(', ')}`:'');await list();open(j.key)}};
$('#bedit').onclick=()=>{if(!cur||cur.origin==='auto')return;ed={type:'spec'};modal('Edit '+cur.key+' (uml_protocol schema)',JSON.stringify(cur.spec,null,2),'<button data-e="save">Save</button>','Saved as author=user for compositions.')};
$('#bdel').onclick=async()=>{if(!cur||!confirm('Delete '+cur.key+'?'))return;try{await T('uml_delete',{key:cur.key});cur=null;$('#out').innerHTML='';list()}catch(e){notice(e.message)}};
$('#bmd').onclick=async()=>{if(!cur)return;const md=await text('markdown');ed=null;
 modal(cur.key+'.md',md,'<button id="cp">Copy</button><button id="dl">Download</button>','Same export as MCP uml_view(format=markdown): Mermaid with element links + link list.');
 $('#cp').onclick=async()=>{try{await navigator.clipboard.writeText(md);$('#mstat').textContent='copied'}catch(e){$('#mtext').select();document.execCommand('copy');$('#mstat').textContent='copied'}};
 $('#dl').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown'}));a.download=cur.key.replace(/[^\w.-]+/g,'_')+'.md';a.click()}};

/* ---------- deep links: #/uml/<type>/<element>?view=<key>&v=<view rev>&h=<element rev>&repo=<repo> ---------- */
async function route(){const m=/^#\/uml\/([^/]+)\/([^?]*)(?:\?(.*))?$/.exec(location.hash);if(!m)return;
 const p=new URLSearchParams(m[3]||''),el=decodeURIComponent(m[2]);
 if(p.get('repo')&&p.get('repo')!==repo&&[...$('#repo').options].some(o=>o.value===p.get('repo'))){repo=p.get('repo');$('#repo').value=repo}
 let key=p.get('view')||'';if(m[1]==='view'&&!key)key=el.replace(/^view:/,'');
 if(m[1]==='view'&&!p.get('view'))key=el.startsWith('composition:')?el:el.replace(/^view:/,'');
 const d=key?await open(key):null;let stale=false;
 if(!d){stale=true;if(el.startsWith('view:')||el.startsWith('composition:'))await open(el.replace(/^view:/,''))}
 else{if(p.get('v')&&p.get('v')!==d.rev)stale=true;
  if(m[1]!=='view'){const n=d.model.nodes.find(x=>elemId(x)===el);if(n){pick(n.id);if(p.get('h')&&d.node_revs&&d.node_revs[n.id]!==p.get('h'))stale=true}else stale=true}}
 if(stale)notice('This link came from an outdated export: regenerate the diagram.')}
window.addEventListener('hashchange',route);
init();
</script></body></html>
"""
