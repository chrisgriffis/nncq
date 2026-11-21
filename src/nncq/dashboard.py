"""Status dashboard: one self-contained page (no CDN, works air-gapped) that polls /api/status.

Served by the MCP server process itself (server.py), so it lives and dies with it: in stdio mode the owning
MCP process keeps a bare listening socket on NNCQ_PORT and starts this web app on the first request.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


def routes(snapshot, control) -> list[Route]:
    """snapshot() -> dict for /api/status; control(action, repo) -> dict for POST /api/<action>."""
    async def page(request: Request):
        return HTMLResponse(HTML)

    async def status(request: Request):
        import anyio
        return JSONResponse(await anyio.to_thread.run_sync(snapshot))

    async def act(request: Request):
        import anyio
        action, repo = request.path_params["action"], request.query_params.get("repo", "")
        return JSONResponse(await anyio.to_thread.run_sync(control, action, repo))

    return [Route("/", page), Route("/dashboard", page), Route("/api/status", status),
            Route("/api/{action}", act, methods=["POST"])]


def app(snapshot, control, extra: list[Route] | None = None) -> Starlette:
    return Starlette(routes=routes(snapshot, control) + (extra or []))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nncq</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262a33;--fg:#d7dae0;--dim:#8b919c;--acc:#5aa9ff;--ok:#4cc38a;
--warn:#e5a50a;--bad:#ef5b5b;--t0:#3a3f4b;--t1:#3d7bd9;--t2:#4cc38a;font:13px/1.45 ui-sans-serif,Segoe UI,system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg)}
header{display:flex;gap:16px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
header h1{font-size:15px;margin:0;font-weight:600}header .dim{flex:1}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px}
.dot.off{background:var(--bad)}.dot.stale{background:var(--warn)}
main{padding:16px 20px;display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(440px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;min-width:0}
.card.wide{grid-column:1/-1}.card h2{font-size:13px;margin:0 0 10px;font-weight:600;display:flex;gap:8px;align-items:baseline}
.card h2 small{color:var(--dim);font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dim{color:var(--dim)}.mono{font-family:ui-monospace,Cascadia Mono,Consolas,monospace;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.stat b{display:block;font-size:17px;font-weight:600}.stat span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.bar{height:10px;border-radius:5px;background:var(--t0);display:flex;overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;transition:width .6s}
.legend{display:flex;gap:14px;font-size:11px;color:var(--dim)}.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.row{display:flex;justify-content:space-between;gap:12px;padding:3px 0}.row>span:last-child{text-align:right}
.pill{border-radius:10px;padding:1px 8px;font-size:11px;background:#232733;color:var(--dim)}
.pill.on{background:#16351f;color:var(--ok)}.pill.busy{background:#13294a;color:var(--acc)}.pill.warn{background:#3a2d08;color:var(--warn)}
.warnbox{border-left:3px solid var(--warn);background:#221c0a;padding:6px 10px;margin:8px 0;border-radius:3px}
.warnbox div+div{margin-top:4px}
table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase}td.r,th.r{text-align:right}
td.q{max-width:280px;overflow:hidden;text-overflow:ellipsis}.err{color:var(--bad)}
.log{max-height:260px;overflow:auto;white-space:pre-wrap}.log div{padding:1px 0}
button{background:#232733;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--acc)}svg{display:block}
#banner{display:none;background:#3a1414;color:#ffb4b4;padding:8px 20px}
</style></head><body>
<header><h1><span id="dot" class="dot"></span>nncq</h1><span class="dim" id="hdr"></span>
<button id="pausebtn" onclick="toggle()">…</button></header>
<div id="banner"></div>
<main id="main"></main>
<script>
const tok=new URLSearchParams(location.search).get('token');const H=tok?{Authorization:'Bearer '+tok}:{};
const hist={};let last=null,fails=0,timer=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n=x=>x==null?'–':Number(x).toLocaleString();
const ago=t=>{if(!t)return'–';const s=Math.max(0,Date.now()/1e3-t);return s<60?Math.round(s)+'s ago':s<3600?Math.round(s/60)+'m ago':(s/3600).toFixed(1)+'h ago'};
const dur=m=>m==null?'–':m<1?Math.round(m*60)+'s':m<90?Math.round(m)+' min':(m/60).toFixed(1)+' h';
function spark(a,w=160,h=28){if(!a||a.length<2)return'';const mx=Math.max(...a,1);
 const p=a.map((v,i)=>`${(i/(a.length-1)*w).toFixed(1)},${(h-2-v/mx*(h-4)).toFixed(1)}`).join(' ');
 return `<svg width="${w}" height="${h}"><polyline fill="none" stroke="var(--acc)" stroke-width="1.5" points="${p}"/></svg>`}
function gpuCard(d){const g=d.gpu||{},m=d.models||{};let h=`<div class="card"><h2>GPU &amp; models <small>${esc(g.name||'')}</small></h2>`;
 if(g.mem_total){const u=g.mem_used/g.mem_total*100;h+=`<div class="row"><span>VRAM</span><span>${n(g.mem_used)} / ${n(g.mem_total)} MB · util ${g.util}%</span></div>
 <div class="bar"><i style="width:${u}%;background:${u>90?'var(--bad)':'var(--t1)'}"></i></div>`}else h+=`<div class="dim">nvidia-smi not available</div>`;
 for(const [k,v] of Object.entries(m)){h+=`<div class="row"><span>${esc(k)}</span><span>${v.loaded?`<span class="pill on">loaded · ${esc(v.device)}</span> <span class="dim">used ${ago(v.last_used)}, unloads in ${dur(v.unload_in_s/60)}</span>`:'<span class="pill">not loaded</span> <span class="dim">loads on first search</span>'}</span></div>`}
 if(d.process&&d.process.cuda_mb!=null)h+=`<div class="row dim"><span>this process (torch)</span><span>${n(d.process.cuda_mb)} MB</span></div>`;
 return h+'</div>'}
function repoCard(r){const st=r.status||{},w=r.embedding,t=st.chunks_by_tier||{},tot=st.chunks||0;
 const t1=+(t['1']||0),t2=+(t['2']||0),t0=tot-t1-t2,p=x=>tot?x/tot*100:0;
 let h=`<div class="card wide"><h2>${esc(r.name)} <small class="mono">${esc(st.root)}${st.dirs&&st.dirs.length?' ['+esc(st.dirs.join(', '))+']':''}</small></h2>`;
 if(!st.indexed)return h+'<div class="dim">not indexed yet</div></div>';
 h+=`<div class="grid">${[['files',st.files],['chunks',tot],['symbols',st.symbols],['call/inherit edges',st.edges],['.xdata files',st.data_files],['data types',st.data_types],['index size',st.db_mb!=null?n(st.db_mb)+' MB':null],['indexed',st.indexed_at]].map(([k,v])=>`<div class="stat"><b>${typeof v==='number'?n(v):esc(v??'–')}</b><span>${k}</span></div>`).join('')}</div>`;
 h+=`<div class="row"><span>Semantic index</span><span>${tot?Math.round(p(t2))+'% full · '+Math.round(p(t1+t2))+'% searchable by meaning':''}</span></div>
 <div class="bar"><i style="width:${p(t2)}%;background:var(--t2)"></i><i style="width:${p(t1)}%;background:var(--t1)"></i></div>
 <div class="legend"><span><i style="background:var(--t2)"></i>full vectors ${n(t2)}</span><span><i style="background:var(--t1)"></i>card vectors ${n(t1)}</span><span><i style="background:var(--t0)"></i>keyword + graph only ${n(t0)}</span></div>`;
 if(w){const k=r.name;(hist[k]=hist[k]||[]);if(w.alive&&w.chunks_per_s!=null&&(!hist[k].t||hist[k].t!==w.updated)){hist[k].push(w.chunks_per_s);hist[k].t=w.updated;if(hist[k].length>90)hist[k].shift()}
  const cls=!w.alive?'':/yield/.test(w.state)?'warn':'busy';
  h+=`<div class="row" style="margin-top:10px"><span>Embedding worker ${w.alive?'<span class="dim">pid '+w.pid+'</span>':''}</span><span><span class="pill ${cls}">${esc(w.alive?w.state:'stopped · '+(w.state||''))}</span></span></div>`;
  if(w.alive)h+=`<div class="row"><span>${spark(hist[k])}</span><span>${n(w.chunks_per_s)} chunks/s · this tier done in ${dur(w.eta_this_tier_min)}<br><span class="dim">cards pending ${n(w.cards_pending)} · full pending ${n(w.full_pending)}</span></span></div>`}
 if(r.paused)h+=`<div class="row"><span>Embedding</span><span class="pill warn">paused (VRAM released)</span></div>`;
 if(r.job){const j=r.job;h+=`<div class="row" style="margin-top:8px"><span>Job: ${esc(j.kind)}</span><span><span class="pill ${j.state==='running'?'busy':/fail/.test(j.state)?'warn':'on'}">${esc(j.state)}</span> <span class="dim">since ${esc(j.started)}</span></span></div>`;
  if(j.state==='running'&&j.log&&j.log.length)h+=`<div class="mono dim">${esc(j.log[j.log.length-1])}</div>`}
 if(r.watcher)h+=`<div class="row"><span>File watcher</span><span>${r.watcher.pending?'<span class="pill busy">'+r.watcher.pending+' changed paths waiting</span>':'<span class="pill on">watching</span>'}</span></div>`;
 const deg=(st.index_state||{}).degraded||[];if(deg.length)h+=`<div class="warnbox">${deg.map(x=>'<div>'+esc(x)+'</div>').join('')}</div>`;
 return h+'</div>'}
function toolsCard(d){const t=Object.entries(d.tools||{}).sort((a,b)=>b[1].last_at-a[1].last_at);
 let h=`<div class="card"><h2>Tools <small>${t.length?'':'no calls yet'}</small></h2>`;
 if(t.length)h+=`<table><tr><th>tool</th><th class="r">calls</th><th class="r">avg</th><th class="r">last</th><th>when</th></tr>${t.map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td class="r">${n(v.n)}${v.errors?` <span class="err">(${v.errors} err)</span>`:''}</td><td class="r">${n(Math.round(v.ms_total/v.n))} ms</td><td class="r">${n(v.last_ms)} ms</td><td class="dim">${ago(v.last_at)}</td></tr>`).join('')}</table>`;
 return h+'</div>'}
function callsCard(d){const c=(d.calls||[]).slice().reverse();
 return `<div class="card"><h2>Recent calls</h2>${c.length?`<table>${c.slice(0,18).map(x=>`<tr><td class="dim">${ago(x.at)}</td><td class="mono">${esc(x.tool)}</td><td class="q mono" title="${esc(x.arg)}">${esc(x.arg)}</td><td class="r ${x.error?'err':''}">${x.error?'error':n(x.ms)+' ms'}</td></tr>`).join('')}</table>`:'<div class="dim">none yet</div>'}</div>`}
function logCard(d){return `<div class="card wide"><h2>Activity</h2><div class="log mono">${(d.events||[]).slice().reverse().map(e=>`<div><span class="dim">${new Date(e[0]*1e3).toLocaleTimeString()}</span> ${esc(e[1])}</div>`).join('')||'<div class="dim">quiet</div>'}</div></div>`}
function render(d){last=d;const s=d.server;
 $('#hdr').textContent=`${s.mode} · pid ${s.pid} · up ${dur(s.uptime_s/60)} · ${s.owner?'owns indexing':'standby (queries only)'}${s.mcp_url?' · MCP '+s.mcp_url:''}`;
 $('#pausebtn').textContent=d.paused?'Resume embedding':'Pause embedding';
 $('#main').innerHTML=(d.repos||[]).map(repoCard).join('')+gpuCard(d)+toolsCard(d)+callsCard(d)+logCard(d)}
async function poll(){clearTimeout(timer);try{const r=await fetch('api/status',{headers:H,cache:'no-store'});if(!r.ok)throw new Error(r.status);
  render(await r.json());fails=0;$('#banner').style.display='none';$('#dot').className='dot'}
 catch(e){fails++;$('#dot').className='dot off';$('#banner').style.display='block';
  $('#banner').textContent='nncq is not answering. It runs inside its MCP client (e.g. Copilot) and exits with it; this page reconnects when it is back.'}
 const busy=last&&(last.repos||[]).some(r=>(r.job&&r.job.state==='running')||(r.embedding&&r.embedding.alive));
 timer=setTimeout(poll,document.hidden?15000:fails?Math.min(30000,2000*fails):busy?1500:4000)}
async function toggle(){await fetch('api/'+(last&&last.paused?'resume':'pause'),{method:'POST',headers:H});poll()}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});poll();
</script></body></html>"""
