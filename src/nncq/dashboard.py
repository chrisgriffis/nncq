"""Status dashboard: one self-contained page (no CDN, works air-gapped) that polls /api/status.

Served by the MCP server process itself (server.py), so it lives and dies with it: in stdio mode the owning
MCP process keeps a bare listening socket on NNCQ_PORT and starts this web app on the first request.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


def routes(snapshot, control, tool=None) -> list[Route]:
    """snapshot() -> dict for /api/status; control(action, repo) -> dict for POST /api/<action>;
    tool(name, args) -> awaitable dict for POST /api/tool/<name> (the search and symbol pages)."""
    async def page(request: Request):
        return HTMLResponse(HTML)

    async def status(request: Request):
        import anyio
        return JSONResponse(await anyio.to_thread.run_sync(snapshot))

    async def act(request: Request):
        import anyio
        action, repo = request.path_params["action"], request.query_params.get("repo", "")
        return JSONResponse(await anyio.to_thread.run_sync(control, action, repo))

    async def call(request: Request):
        try:
            args = await request.json()
        except ValueError:
            args = {}
        if tool is None or not isinstance(args, dict):
            return JSONResponse({"error": "tools are not available"}, status_code=400)
        return JSONResponse(await tool(request.path_params["name"], args))

    return [Route("/", page), Route("/dashboard", page), Route("/api/status", status),
            Route("/api/tool/{name}", call, methods=["POST"]), Route("/api/{action}", act, methods=["POST"])]


def app(snapshot, control, tool=None, extra: list[Route] | None = None) -> Starlette:
    return Starlette(routes=routes(snapshot, control, tool) + (extra or []))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nncq</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d0f13;--panel:#151821;--panel2:#0b0d11;--line:#232733;--fg:#d7dae0;--dim:#7d8490;--acc:#5aa9ff;--ok:#4cc38a;
--warn:#e5a50a;--bad:#ef5b5b;--t0:#262b36;--t1:#3d7bd9;--t2:#4cc38a;font:13px/1.4 ui-sans-serif,Segoe UI,system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column;overflow:hidden}
button,select,input{font:inherit;color:var(--fg)}
button{background:#1d2230;border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}button:hover{border-color:var(--acc)}
select{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:4px 6px}
.dim{color:var(--dim)}.mono{font-family:ui-monospace,Cascadia Mono,Consolas,monospace;font-size:12px}.err{color:var(--bad)}
#banner{display:none;background:#3a1414;color:#ffb4b4;padding:7px 16px}
svg{display:block}
/* three panes: dashboard | Symbol explorer | Model explorer (iframe of /views?embed=1); collapsible, resizable */
#page{flex:1;min-height:0;display:flex;padding:8px 6px}
.xp{position:relative;display:flex;flex-direction:column;min-width:240px;min-height:0;overflow:hidden}
.xp.col{flex:0 0 28px!important;min-width:0;cursor:pointer;background:none!important;border:0!important}
.xp.col>:not(.strip){position:absolute!important;left:-99999px;top:0;width:900px;height:700px;visibility:hidden}
.strip{display:none;flex:1;writing-mode:vertical-rl;align-items:center;justify-content:flex-start;gap:8px;padding:10px 0;background:var(--panel);border:1px solid var(--line);
 border-radius:10px;color:var(--dim);font-size:12px;letter-spacing:.03em;user-select:none}.strip:before{content:'»';font-size:13px}
.xp.col>.strip{display:flex}.xp.col:hover>.strip{color:var(--fg);border-color:var(--acc)}
.xs{flex:0 0 8px;cursor:col-resize;position:relative}.xs:hover:after,.xs.on:after{content:'';position:absolute;left:3px;top:0;bottom:0;width:2px;background:var(--acc);border-radius:1px}
.xs.off{cursor:default;pointer-events:none}body.drag{cursor:col-resize;user-select:none}body.drag iframe{pointer-events:none}
.xc{padding:0 7px;line-height:18px;color:var(--dim);flex:none}.xc:hover{color:var(--fg)}
.pt{font-size:12px;font-weight:600;white-space:nowrap}
#left{background:none}#left>.xh{display:flex;align-items:center;gap:8px;padding:3px 4px 7px 6px}#left>.xh .sp{flex:1}
#dashb{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:12px;padding:0 4px 4px 2px;container-type:inline-size}
#mpane{background:var(--panel);border:1px solid var(--line);border-radius:10px}#mpane.col{background:none;border:0}
#mframe{flex:1;border:0;width:100%;height:100%;background:var(--bg);border-radius:10px}
#dbg{position:fixed;left:0;bottom:0;max-width:60vw;max-height:30vh;overflow:auto;font:10px monospace;background:#000c;color:#9f9;z-index:99;white-space:pre-wrap}
@container (max-width:1040px){#status{flex-wrap:wrap}#status .gc{flex:1 1 100%}.sbar{flex-wrap:wrap}.sbar .qwrap{flex:1 1 calc(100% - 76px)}.sside{flex:1 1 100%}}
@container (max-width:700px){.rb{flex-wrap:wrap}.stages{flex:1 1 200px}.mid{min-width:200px}}
.fill{width:0;min-width:100%}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
/* status */
#status{display:flex;gap:12px;align-items:stretch}
.rcol{display:flex;flex-direction:column;gap:12px;flex:1}.rcol>.rcard{flex:1;display:flex;flex-direction:column}.rcard>.rb{flex:1}
.rcard.pick{cursor:pointer}.rcard.sel{border-color:#2d4a77}
.top{display:flex;align-items:center;gap:10px;margin-bottom:6px;white-space:nowrap}.top b{font-size:14px}.top .sp{flex:1}
.top .mono{color:var(--dim);overflow:hidden;text-overflow:ellipsis}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);display:inline-block;box-shadow:0 0 6px var(--ok);flex:none}
.dot.off{background:var(--bad);box-shadow:0 0 6px var(--bad)}
.lt{display:inline-block;width:10px;height:10px;border-radius:50%;flex:none}
.ib{padding:0 7px;font-size:12px;line-height:20px;border-radius:5px}
.rb{display:flex;align-items:center;gap:20px}
.mid{display:flex;flex-direction:column;gap:8px;min-width:230px}
.bigs{display:flex;gap:22px;align-items:baseline;white-space:nowrap}.bigs b{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.bigs small{color:var(--dim);margin-left:6px;font-size:12px}
.stages{display:flex;flex-direction:column;gap:10px;width:220px}
.stage .lb{display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px;white-space:nowrap;gap:10px}.stage .lb span:last-child{color:var(--dim)}
.stage .lb b{color:var(--fg)}
.track{height:10px;border-radius:5px;background:var(--t0);overflow:hidden}
.fill-b{height:100%;border-radius:inherit;transition:width .9s ease}
.active{background-image:repeating-linear-gradient(-45deg,rgba(255,255,255,.2) 0 6px,transparent 6px 12px);background-size:17px 17px;animation:mv .7s linear infinite}
@keyframes mv{to{background-position:17px 0}}
.ring circle{transition:stroke-dasharray .9s ease,stroke-dashoffset .9s ease}
.pulse{animation:pl 1.6s ease-in-out infinite}@keyframes pl{50%{opacity:.25}}
.gc{display:flex;flex-direction:column;gap:8px;align-items:center;flex:0 0 var(--side);box-sizing:border-box}.gc>.gb{flex:1}.gc .top{align-self:stretch}
.rings{display:flex;gap:14px}
.rg{display:flex;flex-direction:column;align-items:center;gap:3px}.cap{font-size:12px;color:var(--dim);white-space:nowrap;text-align:center}.cap b{color:var(--fg);font-weight:600}
.spark{display:flex;flex-direction:column;gap:2px}.chart .gl{stroke:#fff;stroke-opacity:.07}.chart .tk,.chart .ax{stroke:#fff;stroke-opacity:.3}.chart .tl{fill:#8b95a7;font-size:7.5px;font-family:ui-monospace,Consolas,monospace}.spark.ck{cursor:pointer}.spark.ck:hover .cap{color:var(--fg)}
.slab{display:flex;flex-direction:column;align-items:center;gap:5px}.lg{display:flex;gap:9px;font-size:11px;color:var(--dim)}.lg span{display:flex;align-items:center;gap:4px}.lg i{width:8px;height:8px;border-radius:50%;background:var(--t0);border:1px solid #3a4150}.sst{font-size:12px;font-weight:700;padding:2px 9px;border-radius:9px;letter-spacing:.02em;text-transform:uppercase;white-space:nowrap}
.sst.ok{background:#12351f;color:var(--ok)}.sst.warn{background:#3a2d08;color:var(--warn)}.sst.bad{background:#3a1414;color:#ff8a8a;animation:pl 1.6s ease-in-out infinite}
.sst.t1{background:#10283f;color:#7cc0ff}.sst.dim{background:var(--t0);color:var(--dim)}
.gb{display:flex;gap:18px;align-items:center;justify-content:center}.gside{display:flex;flex-direction:column;gap:12px}
.gc .mods{flex-direction:column;gap:5px}.md{width:9px;height:9px;border-radius:50%;border:1.5px solid #5a6375;box-sizing:border-box}.md.on{background:var(--ok);border-color:var(--ok);box-shadow:0 0 6px var(--ok)}.gc .mods small{color:#6b7486}.gc .mods span.on{color:var(--fg)}
.mods{display:flex;gap:14px;font-size:12px;color:var(--dim)}.mods span{display:flex;align-items:center;gap:5px}
#console{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 12px;height:180px;overflow:auto;font:12px/1.5 ui-monospace,Cascadia Mono,Consolas,monospace}
/* search */
:root{--side:390px}
.sbar{display:flex;gap:12px;align-items:stretch}.sbar>*,.sside>*{box-sizing:border-box;height:44px}
.sside{flex:0 0 var(--side);display:flex;gap:8px}.sside #qpath{flex:1;font-size:13px;padding:10px}.sside select{width:58px}.sbar .askb{flex:0 0 64px}
.sbar input[type=text]{flex:1;min-width:0;font-size:16px;padding:10px 14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;outline:none}
.sbar input[type=text]:focus{border-color:var(--acc)}
.recent{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.recent:empty{display:none}
.recent a{font-size:11px;padding:2px 8px;border-radius:10px;background:#1a1e28;color:var(--dim);cursor:pointer}.recent a:hover{color:var(--fg)}.recent a.cur{background:#1d2a44;color:var(--acc)}
.qwrap{flex:1;min-width:0;position:relative;display:flex}.qwrap input{flex:1}
#qx{position:absolute;right:8px;top:50%;transform:translateY(-50%);color:var(--dim);cursor:pointer;font-size:18px;padding:0 6px;display:none}#qx:hover{color:var(--fg)}
.blk{margin-top:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.blk>summary{list-style:none;cursor:default;padding:7px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px}
.blk>summary::-webkit-details-marker{display:none}.blk:not([open])>summary>.tw{transform:rotate(-90deg)}
.blk>summary .h{font-weight:600;font-size:12px}
.bb{padding:2px 12px 10px;display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:12px;align-items:center}
.bb>.lab{color:var(--dim);font-size:11px;text-align:right;white-space:nowrap}
.st{font-size:11px;font-weight:600;padding:1px 8px;border-radius:9px;text-transform:uppercase}
.st.strong{background:#12351f;color:var(--ok)}.st.fair{background:#3a2d08;color:var(--warn)}.st.weak{background:#3a1414;color:#ff8a8a}
.tchip{font-size:11px;padding:2px 8px;border-radius:10px;background:#1a1e28;white-space:nowrap}.tchip i{font-style:normal;color:var(--dim)}
.tchip.ig{color:var(--dim);text-decoration:line-through;background:none;padding:2px 3px}
.tchip.v{cursor:pointer;color:var(--acc)}.tchip.v:hover{background:#1d2a44}
.fb{display:flex;height:10px;border-radius:5px;overflow:hidden;width:220px;background:var(--t0)}.fb i{display:block;height:100%}
.leg{display:flex;gap:12px;font-size:11px;color:var(--dim);flex-wrap:wrap}.leg b{color:var(--fg);font-weight:600}.leg i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}
.abar{display:grid;grid-template-columns:minmax(0,max-content) 1fr auto;gap:3px 8px;align-items:center;font-size:11px}
.abar .an{cursor:pointer;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}.abar .an:hover{color:var(--acc)}
.abar .ab{height:7px;border-radius:4px;background:var(--acc);min-width:4px}
.lnk{font-size:11px;color:var(--dim);line-height:1.6}.lnk .r{color:var(--acc);cursor:pointer}.lnk .r:hover{text-decoration:underline}
.files{max-height:46vh;overflow:auto;border-top:1px solid var(--line)}
.frow{display:flex;gap:8px;align-items:center;padding:5px 12px;cursor:pointer;border-bottom:1px solid #1c2029;min-width:0;font-size:12px}
.frow:hover{background:#1b1f29}.frow.on{background:#1d2a44}
.frow .fn{font-weight:600;white-space:nowrap}.frow .fd{color:var(--dim);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left;min-width:60px;flex:1 1 0}
.frow .fs{display:flex;gap:4px;flex:0 1 auto;min-width:0;overflow:hidden}.frow .fs span{font-size:11px;padding:0 6px;border-radius:8px;background:#232733;white-space:nowrap}
.rk{width:44px;height:5px;background:var(--t0);border-radius:3px;overflow:hidden;flex:none}.rk i{display:block;height:100%;background:var(--ok)}
.hit{border-bottom:1px solid var(--line)}.tw:before{content:'';border-left:5px solid transparent;border-right:5px solid transparent;border-top:7px solid currentColor}.tw{color:#a9b0bc;font-size:0;flex:none;width:20px;height:20px;display:inline-flex;align-items:center;justify-content:center;border-radius:4px;cursor:pointer;transition:transform .15s;user-select:none;margin-left:-6px}.tw:hover{color:var(--fg);background:#1b1f29}.blk>summary>.tw{margin-right:-4px}
.hit.cl .tw{transform:rotate(-90deg)}.hit.cl>.facts,.hit.cl>pre,.hit.cl>.more{display:none}.hit.cl>.hh{padding-bottom:8px}
.rzh{position:absolute;left:0;right:0;bottom:0;height:8px;cursor:ns-resize;z-index:3}.rzh:hover,.rzh.on{background:linear-gradient(transparent 5px,var(--acc) 5px,var(--acc) 7px,transparent 7px)}
.rzh::after{content:'';position:absolute;right:3px;bottom:2px;width:9px;height:9px;background:linear-gradient(135deg,transparent 45%,#5a6375 45%,#5a6375 55%,transparent 55%,transparent 70%,#5a6375 70%,#5a6375 80%,transparent 80%)}
#s-symbols.idle{height:auto!important}#s-symbols.idle>.rzh{display:none}details:not([open])>.rzh{display:none}.bb{overflow:auto}.hit .hh{display:flex;gap:8px;align-items:center;padding:8px 12px 2px;flex-wrap:wrap}
.hit .hh .sym{font-weight:600;cursor:pointer}.hit .hh .sym:hover{color:var(--acc)}
.hit .facts{padding:2px 12px 6px;font-size:12px;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
.hit .facts .lab{color:var(--dim);font-size:11px;text-align:right}
.hit .sig{font-family:ui-monospace,Cascadia Mono,Consolas,monospace;font-size:12px;color:#b8c0cc;white-space:pre-wrap;word-break:break-word}
.why{display:flex;gap:4px;flex-wrap:wrap}.why span{font-size:10px;padding:0 6px;border-radius:8px;background:#232733;color:var(--dim);white-space:nowrap}
.pause{font-size:12px;padding:2px 10px;border-radius:6px;background:#2a2f3b;border:1px solid var(--line);color:var(--fg);cursor:pointer;white-space:nowrap}.pause:hover{border-color:var(--acc)}
.pause.p{background:#3a2d08;color:var(--warn)}
.res{margin-bottom:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.rh{display:flex;gap:10px;align-items:center;padding:7px 12px;border-bottom:1px solid var(--line);min-width:0}
.rh .sym{font-weight:600;cursor:pointer;white-space:nowrap}.rh .sym:hover{color:var(--acc)}
.rh .path{color:var(--dim);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left;flex:1}
.kind{font-size:10px;text-transform:uppercase;padding:1px 6px;border-radius:4px;background:#1d2a44;color:var(--acc);white-space:nowrap}
.score{width:70px;height:6px;background:var(--t0);border-radius:3px;overflow:hidden;flex:none}.score i{display:block;height:100%;background:var(--ok)}
.more{padding:4px 12px;font-size:11px;color:var(--acc);cursor:pointer;border-top:1px solid var(--line)}
pre.code{margin:0;padding:8px 0;overflow:auto;max-height:340px;font:12px/1.5 ui-monospace,Cascadia Mono,Consolas,monospace;background:var(--panel2);tab-size:4}
pre.code.tall{max-height:none}
.code span.l{display:block;padding-right:12px}.code span.l.hl{background:#2b2a14}
.code .ln{display:inline-block;width:56px;padding-right:12px;text-align:right;color:#475060;user-select:none}
/* element kinds: same hues as the Model explorer's diagrams */
.kind.k-class,.badge.k-class{background:#1b2a44;color:#7cc0ff}.kind.k-interface,.badge.k-interface,.kind.k-trait,.badge.k-trait{background:#2a1f44;color:#c9a8ff}
.kind.k-enum,.badge.k-enum{background:#3a2d10;color:#f0c050}.kind.k-struct,.badge.k-struct,.kind.k-union,.badge.k-union,.kind.k-record,.badge.k-record{background:#13332e;color:#5fd4c0}
.kind.k-function,.badge.k-function,.kind.k-method,.badge.k-method{background:#14301f;color:#6fdc9e}.kind.k-field,.badge.k-field,.kind.k-variable,.badge.k-variable,.kind.k-constant,.badge.k-constant,.kind.k-property,.badge.k-property{background:#2d2a18;color:#e0cc6e}
.kind.k-namespace,.badge.k-namespace,.kind.k-module,.badge.k-module{background:#262033;color:#c3a8f0}
.k{color:#c792ea}.s{color:#c3e88d}.c{color:#5f6b7a;font-style:italic}.n{color:#f78c6c}.pp{color:#89ddff}
/* symbols */
#s-symbols{height:min(300px,32vh);flex:none}#s-symbols.idle{height:auto}
#vsym>#s-symbols{border:0;border-bottom:1px solid var(--line);border-radius:0;background:none}
#vsym>.scard{flex:none;max-height:34vh;overflow:auto}#vsym>.cpath{flex:none}#vsym>.pb{min-height:80px}
#sform .recent{flex-basis:100%;margin-top:0}
#right>.vb{flex:1;min-height:0;display:flex;flex-direction:column}#right>.vb[hidden]{display:none}
.navb{display:flex}.navb a{padding:0 8px;font-size:18px;line-height:22px;color:var(--dim);cursor:pointer;border-radius:5px}.navb a:hover{color:var(--fg);background:#1b1f29}
#vtitle a{color:var(--acc);cursor:pointer;text-decoration:none}#vtitle a:hover{text-decoration:underline}
#pmenu{position:fixed;z-index:50;background:var(--panel,#151821);border:1px solid var(--line,#232733);border-radius:6px;padding:4px 0;font-size:12px;box-shadow:0 6px 20px #0008}#pmenu a{display:block;padding:5px 14px;cursor:pointer;white-space:nowrap}#pmenu a:hover{background:#ffffff14}#pmenu .msg{padding:5px 14px;color:var(--dim,#7d8490)}
#vtitle{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left;flex:1;min-width:0}
.pane{background:var(--panel);border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.pane.l{flex:0 0 40%;min-width:0}.pane.r{flex:1;min-width:0}
.pane .ph{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line);align-items:center;flex-wrap:wrap}
.pane .pb{overflow:auto;flex:1;min-height:0;padding:6px 8px}
.pane input[type=text]{flex:1;min-width:120px;padding:6px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;outline:none}
.seg{display:flex;background:var(--panel2);border-radius:7px;padding:2px}.seg a{padding:3px 10px;border-radius:5px;color:var(--dim);cursor:pointer;font-size:12px}
.seg a.on{background:#1d2a44;color:var(--fg)}
ul.tree{list-style:none;margin:0;padding-left:16px}ul.tree.root{padding-left:0}
.tn{display:flex;align-items:center;gap:6px;padding:2px 5px;border-radius:5px;cursor:pointer;white-space:nowrap;min-width:0}
.tn:hover{background:#1b1f29}.tn.cur{background:#1d2a44}
.car{width:12px;flex:none;color:var(--dim);text-align:center;font-size:10px}
.tn .nm{overflow:hidden;text-overflow:ellipsis}.tn .nm b{font-weight:600}.tn .q{color:var(--dim)}
.tn .f{color:var(--dim);font-size:11px;margin-left:auto;padding-left:10px;overflow:hidden;text-overflow:ellipsis;direction:rtl;min-width:0}
.badge{font-size:10px;padding:0 6px;border-radius:8px;background:#232733;color:var(--dim);flex:none}
.badge.w{background:#3a2d08;color:var(--warn)}
.rr{color:var(--acc);opacity:0;flex:none;padding:0 3px}.tn:hover .rr{opacity:1}
.scard{padding:10px 12px;border-bottom:1px solid var(--line)}.scard:empty{display:none}.scard .t{font-size:15px;font-weight:600;word-break:break-all}
.scard .sig{font-family:ui-monospace,Cascadia Mono,Consolas,monospace;font-size:12px;color:#b8c0cc;margin:6px 0;white-space:pre-wrap}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.chip{font-size:11px;padding:2px 8px;border-radius:10px;background:#1a1e28;color:var(--dim);cursor:pointer}
.chip:hover{color:var(--fg)}.chip b{color:var(--fg);font-weight:600}
.empty{padding:20px;color:var(--dim);text-align:center}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--t0);border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.cpath{padding:6px 12px;font-size:11px;color:var(--dim);border-bottom:1px solid var(--line);display:flex;gap:8px}.cpath:empty{display:none}
</style></head><body>
<div id="banner"></div>
<div id="page">
 <section class="xp" id="left" data-pane="dash">
 <div class="xh"><b class="pt">Dashboard</b><span class="sp"></span><button class="xc" data-col="dash" title="collapse the dashboard (its strip expands it again)">–</button></div>
 <div id="dashb">
 <div id="status"><div class="card empty"><span class="spin"></span></div></div>

 <section class="fill" id="s-search">
  <form class="sbar" onsubmit="event.preventDefault();doSearch()">
   <button class="askb">Ask</button><div class="qwrap"><input type="text" id="q" placeholder="Ask about the code: how / where / why…, or an identifier   ( / )" autocomplete="off"><a id="qx" title="clear (Esc)">×</a></div>
   <div class="sside"><input type="text" id="qpath" placeholder="path filter">
   <select id="qk" title="hits"><option>5</option><option selected>8</option><option>12</option><option>20</option></select></div></form>
  <div class="recent" id="recent"></div><div id="answer"></div>
 </section>

 <section class="fill"><div id="console" data-rz="console" data-rzmin="60"></div></section>
 </div>
 <div class="strip" title="expand the dashboard">Dashboard</div>
 </section>
 <div class="xs" data-i="0" title="drag to resize · double-click to reset"></div>

 <section id="right" class="pane xp" data-pane="sym">
  <div class="ph"><b class="pt">Symbol explorer</b><span class="navb"><a onclick="history.back()" title="back (Alt+←, mouse back)">‹</a><a onclick="history.forward()" title="forward (Alt+→)">›</a></span><div class="seg" id="vtab"><a data-v="hits">Active search</a><a data-v="sym">Symbol</a></div><a id="mlink" href="views" title="show this symbol / file in the Model explorer (UML / SysML diagrams; ctrl-click: on its own page)" style="font-size:12px;color:var(--acc);white-space:nowrap">Model explorer</a><span id="vtitle" class="mono"></span><button class="xc" data-col="sym" title="collapse the Symbol explorer (its strip expands it again)">–</button></div>
  <div class="vb pb" id="vhits" style="padding:0"><div class="empty">Ask something, then pick a file.</div></div>
  <div class="vb" id="vsym" hidden>
   <form class="ph" id="sform" onsubmit="event.preventDefault();findSym($('#sq').value)">
    <input type="text" id="sq" placeholder="Symbol: name or ns::Class::method" autocomplete="off"><button>Find</button>
    <div class="seg" id="tmode"><a data-m="callers">Callers</a><a data-m="callees">Callees</a><a data-m="outline">File outline</a><a data-m="types">Type tree</a></div>
    <div class="recent" id="srecent"></div></form>
   <div class="scard" id="scard"></div>
   <section class="pane idle" id="s-symbols" data-rz="symbols" data-rzmin="80"><div class="pb" id="tree"><div class="empty">Find a symbol, or click one in search hits.</div></div></section>
   <div class="cpath" id="cpath"></div><div class="pb" style="padding:0"><pre class="code tall" id="sview"></pre></div></div>
  <div class="strip" title="expand the Symbol explorer">Symbol explorer</div>
 </section>
 <div class="xs" data-i="1" title="drag to resize · double-click to reset"></div>

 <section class="xp" id="mpane" data-pane="model"><iframe id="mframe" title="Model explorer"></iframe><div class="strip" title="expand the Model explorer">Model explorer</div></section>
</div>

<script>
const QS=new URLSearchParams(location.search);
if(QS.get('debug')==='1'){const E=window.__errs=[];addEventListener('error',e=>E.push(String(e.message)));addEventListener('unhandledrejection',e=>E.push('rejection: '+String(e.reason&&e.reason.message||e.reason)))}
const tok=QS.get('token');const H=tok?{Authorization:'Bearer '+tok}:{};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n=x=>x==null?'–':Number(x).toLocaleString();
const k=x=>x==null?'–':x>=1e6?(x/1e6).toFixed(x>=1e7?0:2)+'M':x>=1e4?Math.round(x/1e3)+'k':Number(x).toLocaleString();
const dur=m=>m==null?'–':m<1?Math.round(m*60)+'s':m<90?Math.round(m)+' min':(m/60).toFixed(1)+' h';
const pct=f=>{const p=f*100;return p.toFixed(p>0&&p<10?1:0)+'%'};
const base=p=>String(p||'').split('/').pop();
let last=null,fails=0,timer=null,sel=localStorage.getItem('nncq.repo')||'',multi=false;

/* ---------- in-place DOM update: keeps elements, so CSS transitions and animations run smoothly ---------- */
function morph(a,b){if(a.nodeType!==b.nodeType||a.nodeName!==b.nodeName){a.replaceWith(b);return}
 if(a.nodeType===3){if(a.data!==b.data)a.data=b.data;return}if(a.nodeType!==1)return;
 for(const x of [...a.attributes])if(!b.hasAttribute(x.name))a.removeAttribute(x.name);
 for(const x of [...b.attributes])if(a.getAttribute(x.name)!==x.value)a.setAttribute(x.name,x.value);
 kids(a,b)}
function kids(a,b){const ac=[...a.childNodes],bc=[...b.childNodes];
 bc.forEach((c,i)=>i<ac.length?morph(ac[i],c):a.appendChild(c));for(let i=bc.length;i<ac.length;i++)ac[i].remove()}
function patch(el,html){const t=document.createElement(el.tagName);t.innerHTML=html;kids(el,t)}

/* ---------- visuals ---------- */
function ring(segs,size,big,sub,glow){const r=42,C=2*Math.PI*r;let off=0,arcs='';
 for(const [f,c] of segs){const L=Math.max(0,Math.min(1,f||0))*C;
  arcs+=`<circle cx="50" cy="50" r="${r}" fill="none" stroke="${c}" stroke-width="9" stroke-dasharray="${L.toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 50 50)"/>`;off+=L}
 return `<div class="rg"><svg class="ring" viewBox="0 0 100 100" width="${size}" height="${size}"><circle cx="50" cy="50" r="${r}" fill="none" stroke="var(--t0)" stroke-width="9"/>${arcs}<circle class="${glow?'pulse':''}" cx="50" cy="50" r="49" fill="none" stroke="${glow||'transparent'}" stroke-width="1.2"/><text x="50" y="57" text-anchor="middle" fill="var(--fg)" font-size="${size>100?20:21}" font-weight="600">${big}</text></svg>${sub?`<div class="cap">${sub}</div>`:''}</div>`}
/* line chart in real pixels: faint grid, ticks on both axes, tiny numbers (y: 0/mid/max, x: time ago) */
function area(pts,id,color,h,win,max,pw){const W=pw||300,L=24,B=11,T=3,R=3,pwid=W-L-R,ph=h-T,H=h+B,now=Date.now()/1e3;pts=(pts||[]).filter(p=>p[0]>now-win);
 const raw=max||Math.max(1,...pts.map(p=>p[1]))*1.15,mag=Math.pow(10,Math.floor(Math.log10(raw))),mx=max||[1,2,2.5,5,10].map(x=>x*mag).find(x=>x>=raw);
 const X=t=>(L+(t-(now-win))/win*pwid).toFixed(1),Y=v=>(T+ph-Math.min(v,mx)/mx*ph).toFixed(1);
 const fy=v=>v>=1e3?(v/1e3).toFixed(v%1e3?1:0)+'k':String(+v.toFixed(1)),ft=s=>s<=0?'now':s>=3600?'-'+(s/3600).toFixed(s%3600?1:0)+'h':'-'+Math.round(s/60)+'m';
 let g='';for(const f of [0,.5,1]){const y=Y(mx*f);g+=`<line x1="${L}" x2="${W-R}" y1="${y}" y2="${y}" class="gl"/><line x1="${L-3}" x2="${L}" y1="${y}" y2="${y}" class="tk"/><text x="${L-5}" y="${(+y+3).toFixed(1)}" text-anchor="end" class="tl">${fy(mx*f)}</text>`}
 const NS=[6,5,4,3].find(n=>[60,120,300,600,900,1800,3600,7200,10800,21600].includes(win/n))||4;
 for(let i=0;i<=NS;i++){const x=(L+i/NS*pwid).toFixed(1);g+=`<line x1="${x}" x2="${x}" y1="${T}" y2="${T+ph}" class="gl"/><line x1="${x}" x2="${x}" y1="${T+ph}" y2="${T+ph+3}" class="tk"/><text x="${x}" y="${H-1}" text-anchor="${i?i<NS?'middle':'end':'start'}" class="tl">${pwid/NS<28&&(NS-i)%2?'':ft(win*(1-i/NS))}</text>`}
 g+=`<line x1="${L}" x2="${L}" y1="${T}" y2="${T+ph}" class="ax"/><line x1="${L}" x2="${W-R}" y1="${T+ph}" y2="${T+ph}" class="ax"/>`;
 if(pts.length&&pts[0][0]>now-win+1)pts=[[now-win,0],[pts[0][0],0],...pts];
 let body='';if(pts.length>=2){const xy=pts.map(([t,v])=>X(t)+','+Y(v)).join(' ');
  body=`<polygon fill="url(#${id})" points="${X(pts[0][0])},${T+ph} ${xy} ${X(pts[pts.length-1][0])},${T+ph}"/><polyline fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" points="${xy}"/>`}
 return `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".45"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${g}${body}</svg>`}
/* charts: click cycles the window; past the live window they use the saved per-minute history (survives restarts) */
const WINS=JSON.parse(localStorage.getItem('nncq.win')||'{}');
function chart(d,key,id,color,h,short,max,pw,label){const ws=[short,21600,86400],w=ws.includes(WINS[key])?WINS[key]:short,
 pts=w>short?(d.history_24h||{})[key]:(d.history||{})[key],tl=w>=3600?w/3600+' h':w/60+' min';
 return `<div class="spark ck" data-k="${esc(key)}" data-w="${ws.join(',')}" title="click: ${ws.map(x=>x>=3600?x/3600+' h':x/60+' min').join(' → ')}">${area(pts,id,color,h,w,max,pw)}<div class="cap">${label} · last ${tl}</div></div>`}
document.addEventListener('click',e=>{const c=e.target.closest('.spark.ck');if(!c)return;const ws=c.dataset.w.split(',').map(Number),k=c.dataset.k,cur=ws.includes(WINS[k])?WINS[k]:ws[0];
 WINS[k]=ws[(ws.indexOf(cur)+1)%ws.length];localStorage.setItem('nncq.win',JSON.stringify(WINS));if(last)render(last)});
/* drag the bottom edge (grip at bottom-right) to resize vertically; double-click resets; sizes persist */
const RZ=JSON.parse(localStorage.getItem('nncq.rz')||'{}');
function rzApply(root){(root||document).querySelectorAll('[data-rz]').forEach(el=>{const k=el.dataset.rz,host=el.classList.contains('pane')?el:el.parentElement;
 if(RZ[k]){el.style.height=RZ[k]+'px';el.style.maxHeight='none'}
 if(!host.querySelector(`:scope>.rzh[data-for="${k}"]`)){host.style.position='relative';host.insertAdjacentHTML('beforeend',`<div class="rzh" data-for="${k}" title="drag to resize · double-click to reset"></div>`)}})}
document.addEventListener('pointerdown',e=>{const hd=e.target.closest('.rzh');if(!hd)return;const el=document.querySelector(`[data-rz="${hd.dataset.for}"]`);if(!el)return;
 e.preventDefault();const y0=e.clientY,h0=el.getBoundingClientRect().height,min=+el.dataset.rzmin||80;hd.setPointerCapture(e.pointerId);hd.classList.add('on');
 const mv=ev=>{const v=Math.max(min,Math.round(h0+ev.clientY-y0));el.style.height=v+'px';el.style.maxHeight='none';RZ[el.dataset.rz]=v};
 const up=()=>{hd.removeEventListener('pointermove',mv);hd.removeEventListener('pointerup',up);hd.classList.remove('on');localStorage.setItem('nncq.rz',JSON.stringify(RZ))};
 hd.addEventListener('pointermove',mv);hd.addEventListener('pointerup',up)});
document.addEventListener('dblclick',e=>{const hd=e.target.closest('.rzh');if(!hd)return;const el=document.querySelector(`[data-rz="${hd.dataset.for}"]`);
 if(el){el.style.height='';el.style.maxHeight=''}delete RZ[hd.dataset.for];localStorage.setItem('nncq.rz',JSON.stringify(RZ))});
function stage(label,f,active,color,extra){const p=Math.max(0,Math.min(1,f||0));
 return `<div class="stage"><div class="lb"><span>${label}</span><span>${extra||''} <b>${p>=.9995?'✓':pct(p)}</b></span></div><div class="track"><div class="fill-b ${active?'active':''}" style="width:${(p*100).toFixed(2)}%;background:${color}"></div></div></div>`}
const light=(c,act,done,t)=>`<i class="lt ${act?'pulse':''}" title="${t}" style="background:${act||done?c:'var(--t0)'}"></i>`;

/* ---------- status row: one card per index + the GPU ---------- */
function repoCard(r,d,i){const st=r.status||{},w=r.embedding||{},tr=st.chunks_by_tier||{},tot=st.chunks||0,t1=+(tr['1']||0),t2=+(tr['2']||0);
 const live=!!w.alive,ws=w.state||'',full=live&&/tier 2|full/i.test(ws),cards=live&&!full&&/tier 1|card/i.test(ws);
 const sem=tot?(t1+t2)/tot:0,fl=tot?t2/tot:0,pz=st.parse,pf=pz&&pz.total?pz.done/pz.total:st.indexed?1:0,parsing=!!pz&&pf<1;
 const rate=live&&w.chunks_per_s!=null?k(Math.round(w.chunks_per_s)):null,s=d.server||{},paused=r.paused||d.paused;
 const cls=`card rcard${multi?' pick':''}${multi&&r.name===sel?' sel':''}`;
 const dg=((st.index_state||{}).degraded||[]),dgs=dg.join('\n'),werr=w.error||(/error|crash|fail/i.test(ws)?ws:'');
 const eta=live&&w.eta_this_tier_min?' · ~'+dur(w.eta_this_tier_min)+' left':'',sync=pz&&pz.total?`${k(pz.done)}/${k(pz.total)} files`:'',
  where=pz&&pz.where?'\nwhere: '+pz.where.join(', '):'';
 /* status enum (1-3 words); hover says what to do */
 const stl=r.error?['broken','bad','Index unreadable. Restart the server; re-index if it persists.\n'+r.error]
  :!tot?['not indexed','dim','Run index_repo.']
  :/interrupted/.test(dgs)?['stale','warn','An update died part-way: some files return old code. Restart the server or run update_index.\n\n'+dgs]
  :/being applied/.test(dgs)?['updating','t1','Re-reading changed files ('+sync+'). Wait.'+where]
  :werr?['embed failed','bad','Semantic search stopped growing; text search still works. Restart the server.\n'+werr]
  :paused&&sem<.9995?['paused','dim','Embedding paused. Resume when the GPU is free.']
  :live&&sem<.9995?['embedding','t1','Semantic coverage growing'+eta+'. Wait.']
  :sem<.9995?['partial','dim','Embedding not running; restart the server to resume.']
  :['ready','ok','Text and semantic search cover all code.'];
 const lab=`<span class="sst ${stl[1]}" title="${esc(stl[2])}">${esc(stl[0])}</span><div class="lg"><span><i style="background:var(--t2)"></i>full</span><span><i style="background:var(--t1)"></i>semantic</span><span><i></i>text</span></div>`;
 return `<div class="${cls}" data-repo="${esc(r.name)}"><div class="top"><span class="dot cdot" title="${esc(s.mode)} · pid ${s.pid} · up ${dur((s.uptime_s||0)/60)}"></span><b>${esc(r.name)}</b><span class="mono" title="${esc(st.root)}">${esc(st.root||'')}</span><span class="sp"></span>
  ${light('var(--warn)',parsing,pf>=1,'parse')}${light('var(--t1)',cards,sem>=.9995,'card vectors')}${light('var(--t2)',full,fl>=.9995,'full vectors')}
  <button class="pause ${paused?'p':''}" onclick="event.stopPropagation();toggle()" title="${paused?'resume embedding':'pause embedding (frees the GPU)'}">${paused?'▶ resume':'❚❚ pause'}</button></div>
 <div class="rb"><div class="slab">${ring([[fl,'var(--t2)'],[tot?t1/tot:0,'var(--t1)']],124,pct(sem),'',live?'var(--acc)':'')}${lab}</div>
  <div class="mid"><div class="bigs"><div><b>${rate??(paused?'paused':'idle')}</b>${rate!=null?'<small>chunks / s</small>':''}</div>
   ${live?`<div><b>${dur(w.eta_this_tier_min)}</b><small>to all ${full?'full vectors':'cards'}</small></div>`:''}</div>
   ${chart(d,'rate:'+r.name,'ga'+i,'#5aa9ff',46,900,0,250,'embedding rate')}</div>
  <div class="stages">${stage('Parse · keyword + graph',pf,parsing,'var(--warn)',parsing?k(pz.done)+' / '+k(pz.total):'')}${stage('Card vectors · meaning',sem,cards,'var(--t1)',k(t1+t2))}${stage('Full vectors · fine ranking',fl,full,'var(--t2)',k(t2))}</div></div></div>`}
function gpuCard(d){const g=d.gpu||{},u=g.util||0,v=g.mem_total?g.mem_used/g.mem_total:0;
 return `<div class="card gc"><div class="top"><b>GPU</b><span class="dim">${esc(g.name||'not available')}</span></div>
  <div class="gb"><div class="rings"><div title="GPU utilisation">${ring([[u/100,'var(--ok)']],92,u+'%','GPU load',u>50?'var(--ok)':'')}</div>
   <div title="video memory in use (all apps)">${ring([[v,v>.9?'var(--bad)':'var(--t1)']],92,Math.round(v*100)+'%',g.mem_total?`VRAM <b>${(g.mem_used/1024).toFixed(1)}</b> / ${(g.mem_total/1024).toFixed(0)} GB`:'VRAM','')}</div></div>
  <div class="gside"><div class="mods">${Object.entries(d.models||{}).map(([nm,m])=>`<span class="${m.loaded?'on':''}" title="${m.loaded?'loaded on '+esc(m.device)+' · unloads in '+dur(m.unload_in_s/60):'not loaded: loads on the first search'}"><i class="md ${m.loaded?'on':''}"></i>${esc(nm.split(' ')[0])}${m.loaded?'':' <small>off</small>'}</span>`).join('')}</div>
   ${chart(d,'gpu_util','gu','#4cc38a',30,300,100,130,'load')}</div></div></div>`}
function render(d){last=d;const rs=d.repos||[];multi=rs.length>1;if(!rs.some(r=>r.name===sel))sel=(rs[0]||{}).name||'';
 patch($('#status'),`<div class="rcol">${rs.map((r,i)=>repoCard(r,d,i)).join('')||'<div class="card empty">no index registered</div>'}</div>${gpuCard(d)}`);
 patch($('#console'),(d.events||[]).slice().reverse().map(e=>`<div><span class="dim">${new Date(e[0]*1e3).toLocaleTimeString()}</span> ${esc(e[1])}</div>`).join('')||'<div class="dim">quiet</div>')}
$('#status').addEventListener('click',e=>{const c=e.target.closest('[data-repo]');if(c&&multi){sel=c.dataset.repo;localStorage.setItem('nncq.repo',sel);if(last)render(last)}});
async function poll(){clearTimeout(timer);try{const r=await fetch('api/status',{headers:H,cache:'no-store'});if(!r.ok)throw new Error(r.status);
  render(await r.json());fails=0;$('#banner').style.display='none'}
 catch(e){fails++;document.querySelectorAll('.cdot').forEach(x=>x.classList.add('off'));$('#banner').style.display='block';
  $('#banner').textContent='nncq is not answering. It runs inside its MCP client (e.g. Copilot) and exits with it; this page reconnects when it is back.'}
 const busy=last&&(last.repos||[]).some(r=>(r.job&&r.job.state==='running')||(r.embedding&&r.embedding.alive));
 timer=setTimeout(poll,document.hidden?15000:fails?Math.min(30000,2000*fails):busy?1500:4000)}
async function toggle(){await fetch('api/'+(last&&last.paused?'resume':'pause'),{method:'POST',headers:H});poll()}

/* ---------- tool calls (same read-only tools the agents use) ---------- */
/* results are cached (15 min in memory; asks also for the tab's lifetime), so back/forward redraws instantly */
const TC=new Map();
function T(name,args){const body=JSON.stringify({...args,...(multi&&sel?{repo:sel}:{})}),key=name+body,c=TC.get(key);
 if(c&&Date.now()-c.t<9e5)return c.p;
 const ss=name==='ask'&&sessionStorage.getItem('nncq.c:'+key);
 const p=ss?Promise.resolve(JSON.parse(ss)):(async()=>{const r=await fetch('api/tool/'+name,{method:'POST',headers:{...H,'Content-Type':'application/json'},body});
  const j=await r.json();if(j&&j.error)throw new Error(j.error);return j})();
 TC.set(key,{p,t:Date.now()});if(TC.size>300)TC.delete(TC.keys().next().value);
 p.then(j=>{if(name==='ask'&&!ss)try{sessionStorage.setItem('nncq.c:'+key,JSON.stringify(j))}catch(e){}},()=>TC.delete(key));
 return p}
/* navigation: a new ask or symbol is a history entry; picking a file/tab updates the current one */
let nav=false;
function setHash(o,push){const p=new URLSearchParams(location.hash.slice(1));for(const [a,b] of Object.entries(o))b!=null&&b!==''?p.set(a,b):p.delete(a);
 const u='#'+p.toString();if(u===(location.hash||'#'))return;history[push&&!nav?'pushState':'replaceState'](null,'',u)}
async function applyHash(){nav=true;try{const p=new URLSearchParams(location.hash.slice(1));
  const q=p.get('q')||'',qp=p.get('qp')||'',kk=p.get('k')||'8';
  if(!q){if(ans||$('#q').value)home(true)}
  else if(!ans||ans._key!==q+'\n'+qp+'\n'+kk){$('#q').value=q;$('#qpath').value=qp;$('#qk').value=kk;await doSearch()}
  const f=+(p.get('f')||0);if(ans&&ans.files&&ans.files[f]&&fsel!==f)openFile(f);
  const s=p.get('s')||'';if(s&&(!cur||cur.req!==s))await openSym(s,p.get('p')||'');
  const v=p.get('v');if(v&&v!==view)setView(v)}finally{nav=false}}
window.addEventListener('popstate',applyHash);

/* ---------- code rendering with light highlighting ---------- */
const KW=new Set(('if else for while do switch case default break continue return goto try catch throw finally new delete class struct enum union namespace using typedef template typename public private protected virtual override final static const constexpr inline extern volatile mutable explicit friend operator sizeof this nullptr true false void bool char int float double long short unsigned signed auto decltype '+
 'local function end then elseif nil and or not in repeat until self def import from as pass lambda None True False with yield async await var let interface abstract sealed readonly get set ref out base is foreach internal string object').split(' '));
function hl(line,lang){const cm=lang==='lua'?'--':lang==='py'?'#':'//';let out='',i=0;
 const re=/("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)|(\b\d[\w.]*\b)|([A-Za-z_]\w*)|(#\s*\w+)/g;let m;
 const ci=(()=>{let q=null;for(let j=0;j<line.length;j++){const ch=line[j];if(q){if(ch==='\\'){j++;continue}if(ch===q)q=null}else if(ch==='"'||ch==="'")q=ch;else if(line.startsWith(cm,j))return j}return -1})();
 const code=ci>=0?line.slice(0,ci):line;
 while((m=re.exec(code))){out+=esc(code.slice(i,m.index));const s=m[0];
  out+=m[1]?`<span class="s">${esc(s)}</span>`:m[2]?`<span class="n">${esc(s)}</span>`:m[3]?(KW.has(s)?`<span class="k">${s}</span>`:esc(s)):(lang!=='py'?`<span class="pp">${esc(s)}</span>`:esc(s));i=m.index+s.length}
 out+=esc(code.slice(i));if(ci>=0)out+=`<span class="c">${esc(line.slice(ci))}</span>`;return out}
const langOf=p=>/\.lua$/i.test(p)?'lua':/\.py$/i.test(p)?'py':'c';
function codeHtml(text,start,path,mark){const lang=langOf(path);
 const many=mark instanceof Set;return String(text).replace(/\r/g,'').split('\n').map((l,i)=>{const no=start+i,on=many?mark.has(no):no===mark;return `<span class="l${on?' hl':''}"${!many&&on?' id="mk"':''}><span class="ln">${no}</span>${hl(l,lang)}</span>`}).join('')}
function numbered(code){const rows=String(code).replace(/\r/g,'').split('\n');const m=/^(\d+): /.exec(rows[0]||'');const start=m?+m[1]:1;
 return {start,text:rows.map(r=>r.replace(/^\d+: /,'')).join('\n')}}

/* ---------- search: ask -> how it was read + what came back, then files; a file opens in the right viewer ---------- */
let ans=null,fsel=-1,view='hits';
const lastName=s=>String(s||'').split(/::|\./).pop();
const fullPath=at=>String(at).replace(' / ','/').replace(/:\d+-\d+$/,'');
const span=at=>{const m=/:(\d+)-(\d+)$/.exec(at);return m?[+m[1],+m[2]]:[1,1]};
function recent(){const r=JSON.parse(localStorage.getItem('nncq.recent')||'[]');
 const cq=$('#q').value.trim();$('#recent').innerHTML=r.map(q=>`<a data-q="${esc(q)}" class="${q===cq?'cur':''}">${esc(q)}</a>`).join('')}
$('#recent').addEventListener('click',e=>{const a=e.target.closest('[data-q]');if(a){$('#q').value=a.dataset.q;doSearch()}});
$('#q').addEventListener('input',()=>{$('#qx').style.display=$('#q').value||ans?'block':'none'});
$('#q').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();home()}});
$('#qx').addEventListener('click',home);
function home(quiet){$('#q').value='';$('#qpath').value='';$('#qx').style.display='none';ans=null;fsel=-1;$('#answer').innerHTML='';setHash({q:'',qp:'',k:'',f:''},true);recent();
 $('#vhits').innerHTML='<div class="empty">Ask something, then pick a file.</div>';if(view==='hits')vtitle('');if(quiet!==true)$('#q').focus()}
function setView(v){view=v;setHash({v:v==='hits'?'':v});$('#mlink').href='views'+dhash(curTarget());$('#vhits').hidden=v!=='hits';$('#vsym').hidden=v!=='sym';
 document.querySelectorAll('#vtab a').forEach(a=>a.classList.toggle('on',a.dataset.v===v));
 vtitle(v==='hits'?(ans&&fsel>=0?ans.files[fsel].p:''):(cur?cur.path:''))}
function vtitle(t){$('#vtitle').innerHTML=t?`<a data-file="${esc(t)}" title="open / view in folder / copy path">&lrm;${esc(t)}</a>`:''}
function fileMenu(path,x,y){let m=$('#pmenu');if(!m){m=document.createElement('div');m.id='pmenu';document.body.appendChild(m)}
 m.innerHTML='<a data-op="open">Open in editor</a><a data-op="reveal">View in folder</a><a data-op="copy">Copy path</a>';
 m.style.display='block';m.style.left=Math.min(x,innerWidth-180)+'px';m.style.top=(y+6)+'px';
 m.onclick=async e=>{const op=(e.target.closest('[data-op]')||{}).dataset?.op;if(!op)return;e.stopPropagation();
  if(op==='copy'){navigator.clipboard.writeText(path);m.style.display='none';return}
  try{const r=await fetch('api/uml/local/'+op,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo:sel,path})}).then(r=>r.json());
   if(r.error){m.innerHTML='<div class="msg">'+esc(r.error)+'</div>';return}}catch(err){m.innerHTML='<div class="msg">'+esc(String(err))+'</div>';return}
  m.style.display='none'}}
document.addEventListener('click',e=>{const a=e.target.closest('#vtitle [data-file]');const m=$('#pmenu');
 if(a){e.preventDefault();e.stopPropagation();fileMenu(a.dataset.file,e.clientX,e.clientY);return}
 if(m&&!e.target.closest('#pmenu'))m.style.display='none'});
$('#vtab').addEventListener('click',e=>{const a=e.target.closest('[data-v]');if(a)setView(a.dataset.v)});

async function doSearch(){const q=$('#q').value.trim(),qp=$('#qpath').value.trim(),kk=$('#qk').value;if(!q)return home();
 const r=[q,...JSON.parse(localStorage.getItem('nncq.recent')||'[]').filter(x=>x!==q)].slice(0,12);localStorage.setItem('nncq.recent',JSON.stringify(r));
 setHash({q,qp,k:kk==='8'?'':kk,f:''},true);$('#qx').style.display='block';recent();
 $('#answer').innerHTML='<div class="blk" style="padding:8px 12px"><span class="spin"></span> reading the question…</div>';
 const mine={};ans=mine;
 try{const d=await T('ask',{question:q,k:+kk,path:qp});if(ans!==mine)return;d._key=q+'\n'+qp+'\n'+kk;
  const fm=new Map();(d.hits||[]).forEach(h=>{const p=fullPath(h.at);if(!fm.has(p))fm.set(p,[]);fm.get(p).push(h)});
  d.files=[...fm.entries()].map(([p,hs])=>({p,hs}));ans=d;fsel=-1;drawAnswer();if(d.files.length)openFile(0)}
 catch(e){if(ans!==mine)return;ans=null;$('#answer').innerHTML=`<div class="blk err" style="padding:8px 12px">${esc(e.message)}</div>`}}

function drawAnswer(){const d=ans,ip=d.interpreted||{},pf=d.portfolio||{},hits=d.hits||[];
 const stw=(/^(strong|fair|weak)/.exec(pf.strength||'')||[])[1]||'';
 const terms=(ip.terms||[]).map(t=>{const m=/^(\S+)(?: -> also (\S+))?(?: -> parts (.+?))?(?: · (.+?))? · in (\d+)\/(\d+) hits$/.exec(t);
  if(!m)return `<span class="tchip">${esc(t)}</span>`;const ext=[m[2],m[3]].filter(Boolean).join(' ');
  return `<span class="tchip" title="${esc(t)}"><b>${esc(m[1])}</b>${ext?` <i>${esc(ext)}</i>`:''} <i>${esc((m[4]||'').replace(' of chunks',' of code'))} · in ${m[5]}/${m[6]}</i></span>`}).join('');
 const ign=(ip.ignored||'').split(', ').filter(Boolean).map(w=>`<span class="tchip ig" title="ignored">${esc(w)}</span>`).join('');
 const voc=(ip.code_vocabulary||'').split(', ').filter(Boolean).map(w=>`<span class="tchip v" data-ask="${esc(w)}" title="ask with this word">${esc(w)}</span>`).join(' ');
 const fbm=/keyword\+meaning (\d+), meaning only (\d+), keyword only (\d+)/.exec(pf.found_by||'')||[0,0,0,0];
 const [b,mo,ko]=[+fbm[1],+fbm[2],+fbm[3]],tot=Math.max(1,b+mo+ko),C1='var(--ok)',C2='var(--acc)',C3='var(--warn)';
 const fb=`<div class="fb"><i style="width:${b/tot*100}%;background:${C1}"></i><i style="width:${mo/tot*100}%;background:${C2}"></i><i style="width:${ko/tot*100}%;background:${C3}"></i></div>
  <div class="leg"><span><i style="background:${C1}"></i>both <b>${b}</b></span><span><i style="background:${C2}"></i>meaning <b>${mo}</b></span><span><i style="background:${C3}"></i>keyword <b>${ko}</b></span></div>`;
 const areas=(pf.areas||[]).map(a=>/^(.+): (\d+) \((.*)\)$/.exec(a)).filter(Boolean),amx=Math.max(1,...areas.map(a=>+a[2]));
 const ab=areas.map(a=>`<span class="an" data-area="${esc(a[1])}" title="narrow to ${esc(a[1])}">&lrm;${esc(a[1])}</span><span class="ab" style="width:${+a[2]/amx*100}%"></span><span class="dim">${a[2]}</span>`).join('');
 const refs=s=>esc(s).replace(/\bc(\d+)\b/g,'<span class="r" data-ref="c$1">c$1</span>');
 const links=(d.links||[]).map(l=>`<div>${refs(l)}</div>`).join('');
 const led=[pf.keyword_led_to&&'keyword → '+esc(pf.keyword_led_to),pf.meaning_led_to&&'meaning → '+esc(pf.meaning_led_to)].filter(Boolean).join('<br>');
 const notes=[pf.strength,ip.meaning,d.note].filter(Boolean).map(esc).join('<br>');
 const files=d.files.map((f,i)=>{const best=Math.min(...f.hs.map(h=>h.rank)),bn=base(f.p),dir=f.p.slice(0,Math.max(0,f.p.length-bn.length-1));
  return `<div class="frow ${i===fsel?'on':''}" data-f="${i}"><span class="rk" title="best rank #${best}"><i style="width:${Math.max(8,100-(best-1)*100/Math.max(1,hits.length))}%"></i></span>
   <span class="fn">${esc(bn)}</span><span class="fs">${f.hs.map(h=>`<span title="${esc(h.sym)}">${esc(lastName(h.sym))}</span>`).join('')}</span>
   <span class="fd" title="${esc(f.p)}">&lrm;${esc(dir)}</span></div>`}).join('');
 $('#answer').innerHTML=`<details class="blk" open><summary><span class="tw" title="roll up / unroll">▾</span>${stw?`<span class="st ${stw}" title="${esc(pf.strength)}">${stw}</span>`:''}<span class="h">Read as</span>${terms}${ign}
   <span class="dim" style="margin-left:auto">${esc((pf.shown||'').replace(/ \(.*\)/,''))} · ${n(d.ms)} ms</span></summary>
  <div class="bb" data-rz="readas" data-rzmin="60">${voc?`<span class="lab">code says</span><div>${voc}</div>`:''}
   <span class="lab">found by</span><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">${fb}</div>
   ${ab?`<span class="lab">areas</span><div class="abar">${ab}</div>`:''}
   ${pf.kinds?`<span class="lab">kinds</span><div>${esc(pf.kinds)}</div>`:''}
   ${links?`<span class="lab">links</span><div class="lnk">${links}</div>`:''}
   ${led?`<span class="lab">leads</span><div class="lnk">${led}</div>`:''}
   ${notes?`<span class="lab">notes</span><div class="lnk">${notes}</div>`:''}</div></details>
  <details class="blk" open><summary><span class="tw" title="roll up / unroll">▾</span><span class="h">Files</span><span class="dim">${d.files.length} files · ${hits.length} hits · pick one to open it on the right</span></summary>
   <div class="files" data-rz="files" data-rzmin="80">${files||'<div class="empty">nothing found</div>'}</div></details>`}
$('#answer').addEventListener('click',e=>{
 const v=e.target.closest('[data-ask]');if(v){$('#q').value=v.dataset.ask;doSearch();return}
 const a=e.target.closest('[data-area]');if(a){$('#qpath').value=a.dataset.area;doSearch();return}
 const r=e.target.closest('[data-ref]');if(r){const i=ans.files.findIndex(f=>f.hs.some(h=>h.ref===r.dataset.ref));if(i>=0){target('sym');openFile(i,r.dataset.ref)}return}
 const f=e.target.closest('[data-f]');if(f){target('sym');openFile(+f.dataset.f)}});

async function openFile(i,ref){const d=ans;fsel=i;setHash({f:i||''});document.querySelectorAll('#answer .frow').forEach(x=>x.classList.toggle('on',+x.dataset.f===i));
 const f=d.files[i];setView('hits');follow({p:f.p});
 $('#vhits').innerHTML=f.hs.map(h=>{const [a,b]=span(h.at);
  const facts=[['signature',h.sig&&`<span class="sig">${esc(h.sig)}</span>`],['doc',h.doc&&esc(h.doc)],['calls',h.calls&&esc(h.calls)],['callers',h.callers&&esc(h.callers)],
   ['bases',h.bases&&esc(h.bases)],['members',h.members&&esc(h.members)+(h.derived?` · ${h.derived} derived`:'')],['also at',h.also_at&&esc(h.also_at.join(', '))]].filter(x=>x[1]);
  return `<div class="hit" id="h-${h.ref}"><div class="hh"><span class="hc tw" title="roll up / unroll">▾</span><span class="dim" style="font-size:11px">#${h.rank}</span><span class="kind k-${esc(h.kind)}">${esc(h.kind)}</span>
    <span class="sym" data-sym="${esc(h.sym)}" data-path="${esc(f.p)}" title="open in the symbol view">${short(h.sym)}</span><span class="dim mono">${a}-${b}</span>
    <span class="why">${String(h.why||'').split(' · ').filter(Boolean).map(w=>`<span>${esc(w)}</span>`).join('')}</span></div>
   ${facts.length?`<div class="facts">${facts.map(([l,v])=>`<span class="lab">${l}</span><div>${v}</div>`).join('')}</div>`:''}
   <pre class="code tall" data-ref="${h.ref}"><span class="spin" style="margin:4px 12px"></span></pre></div>`}).join('');
 $('#vhits').scrollTop=0;if(ref){const el=$('#h-'+ref);if(el)el.scrollIntoView({block:'start'})}
 const peeks=new Set(f.hs.flatMap(h=>(h.peek||[]).map(p=>+(/^(\d+):/.exec(p)||[])[1]).filter(Boolean)));
 try{const x=await T('expand',{refs:f.hs.map(h=>h.ref).join(','),what:'code'});if(ans!==d||fsel!==i)return;
  for(const it of x.items||[]){const el=$(`#vhits pre[data-ref="${it.ref}"]`);if(!el)continue;
   if(it.error||!it.code){el.innerHTML=`<span class="err" style="padding:0 12px">${esc(it.error||'no code')}</span>`;continue}
   const c=numbered(it.code);el.innerHTML=codeHtml(c.text,c.start,f.p,peeks);
   const m=/start=(\d+), end=(\d+)/.exec(it.more||'');
   if(m)el.insertAdjacentHTML('afterend',`<div class="more" data-p="${esc(f.p)}" data-a="${m[1]}" data-b="${m[2]}">show the remaining ${+m[2]-+m[1]+1} lines</div>`)}}
 catch(e){$('#vhits').insertAdjacentHTML('afterbegin',`<div class="err" style="padding:6px 12px">${esc(e.message)}</div>`)}}
$('#vhits').addEventListener('click',async e=>{const s=e.target.closest('.sym[data-sym]');if(s&&s.dataset.sym){openSym(s.dataset.sym,s.dataset.path);return}
 const hc=e.target.closest('.hit>.hh>.tw');if(hc){hc.closest('.hit').classList.toggle('cl');return}
 const m=e.target.closest('.more');if(!m)return;m.innerHTML='<span class="spin"></span>';
 const d=await T('read_code',{path:m.dataset.p,start:+m.dataset.a,end:+m.dataset.b});const c=numbered(d.code);
 m.previousElementSibling.insertAdjacentHTML('beforeend',codeHtml(c.text,c.start,d.path));m.remove()});

/* ---------- symbol explorer ---------- */
let tmode=localStorage.getItem('nncq.tmode')||'callers',cur=null,nodes=[],selNode=null;
const reDef=/^(\S+) \[(\w+)\] (.+):(\d+)-(\d+)\s*\n?\s*(.*)$/s,reCaller=/^(\S+)\s{2,}(.+):(\d+)$/,
 reCall=/^L(\d+) (\S+)(?: x(\d+))? (?:-> (.*)|\((.*)\))$/,reOut=/^(\s*)(\d+)-(\d+) (\S+) (\S+)\s+\|\s?(.*)$/;
function short(sym){const p=String(sym).split(/::|\./);const l=p.pop();return (p.length?`<span class="q">${esc(p.slice(-1)[0])}::</span>`:'')+`<b>${esc(l)}</b>`}
function mk(o){o.id=nodes.length;nodes.push(o);return o}
function tnode(nd){const car=nd.leaf?'':nd.open?'▾':'▸';const lab=nd.label||short(nd.sym);
 return `<li><div class="tn ${selNode===nd.id?'cur':''}" data-id="${nd.id}"><span class="car" data-t="${nd.id}">${nd.loading?'<span class="spin"></span>':car}</span><span class="nm" title="${esc(nd.sym||'')}">${lab}</span>
  ${nd.badge?`<span class="badge ${nd.bw?'w':'k-'+esc(nd.badge)}">${esc(nd.badge)}</span>`:''}${nd.sym&&nd.path&&!nd.noroot?`<span class="rr" data-r="${nd.id}" title="explore this symbol">◎</span>`:''}<span class="f" title="${esc(nd.path||'')}">${nd.path?'&lrm;'+esc(nd.path)+(nd.line?':'+nd.line:''):''}</span></div>
  ${nd.open&&nd.kids?`<ul class="tree">${nd.kids.map(tnode).join('')||'<li class="dim" style="padding:2px 22px">none</li>'}</ul>`:''}</li>`}
function drawTree(){const roots=nodes.filter(x=>x.root);$('#tree').innerHTML=roots.length?`<ul class="tree root">${roots.map(tnode).join('')}</ul>`:'<div class="empty">Find a symbol, or click one in search hits.</div>'}
function ancestors(nd){const s=new Set();for(let p=nd;p;p=p.parent!=null?nodes[p.parent]:null)s.add(p.sym);return s}
async function expand(nd){if(nd.leaf)return;if(nd.kids){nd.open=!nd.open;drawTree();return}
 nd.loading=true;drawTree();
 try{nd.kids=await nd.load(nd)}catch(e){nd.kids=[mk({label:`<span class="err">${esc(e.message)}</span>`,leaf:true})]}
 nd.kids.forEach(c=>c.parent=nd.id);const anc=ancestors(nd);nd.kids.forEach(c=>{if(c.sym&&anc.has(c.sym)&&!c.leaf){c.leaf=true;c.badge='recursion';c.bw=true}});
 nd.loading=false;nd.open=true;drawTree()}
const loadCallers=async nd=>{const d=await T('called_by',{name:nd.sym,path:nd.dpath||'',limit:80});
 const out=(d.callers||[]).map(s=>{const m=reCaller.exec(s);return m?mk({sym:m[1],path:m[2],line:+m[3],dpath:m[2],load:loadCallers}):mk({label:esc(s),leaf:true})});
 if(d.call_sites_total&&d.shown&&d.call_sites_total>d.shown)out.push(mk({label:`<span class="dim">… ${d.call_sites_total-d.shown} more call sites</span>`,leaf:true}));return out};
const loadCallees=async nd=>{const d=await T('calls',{name:nd.sym,path:nd.dpath||''});const site=(d.defined_at||[])[0]||nd.dpath||'';const sp=site.replace(/:\d+(-\d+)?$/,'');
 return (d.calls||[]).map(s=>{const m=reCall.exec(s);if(!m)return mk({label:esc(s),leaf:true});const [,ln,name,times,to,ext]=m;
  if(ext)return mk({label:`<span class="dim">${esc(name)}</span>`,leaf:true,badge:'external',path:sp,line:+ln,site:true});
  const amb=/^(\d+) definitions \(ambiguous\)/.exec(to||'');
  if(amb)return mk({sym:name,label:`<b>${esc(name)}</b>`,badge:amb[1]+' candidates',bw:true,path:sp,line:+ln,site:true,noroot:true,load:loadCands});
  const t=/^(\S+) (.+):(\d+)$/.exec(to||'');
  return t?mk({sym:t[1],path:t[2],line:+t[3],dpath:t[2],badge:times?'×'+times:'',callLine:+ln,callPath:sp,load:loadCallees}):mk({label:esc(s),leaf:true})})};
const loadCands=async nd=>{const d=await T('find_symbol',{name:nd.sym});
 return (d.definitions||[]).map(s=>{const m=reDef.exec(s);return m?mk({sym:m[1],path:m[3],line:+m[4],dpath:m[3],badge:m[2],load:loadCallees}):mk({label:esc(s),leaf:true})})};
async function outlineTree(path){const d=await T('file_outline',{path});const roots=[],stack=[];
 for(const s of d.symbols||[]){const m=reOut.exec(s);if(!m)continue;const dep=m[1].length/2;
  const nd=mk({sym:m[5],path:d.path,line:+m[2],end:+m[3],dpath:d.path,badge:m[4],kids:[],open:true,leaf:false});
  while(stack.length>dep)stack.pop();(stack.length?stack[stack.length-1].kids:roots).push(nd);if(stack.length)nd.parent=stack[stack.length-1].id;stack.push(nd)}
 for(const x of nodes)if(x.kids&&!x.kids.length&&x.badge){x.leaf=true;delete x.kids}
 return roots}
async function typeTree(sym){const d=await T('type_hierarchy',{name:sym});const out=[];
 for(const b of d.bases||[])b.split(' -> ').slice(1).forEach((c,i)=>out.push(mk({sym:c,label:`<span class="dim">${i?'↑':'base'}</span> ${short(c)}`,leaf:true})));
 const lines=String(d.derived_tree||'').split('\n').filter(Boolean),stack=[],roots=[];
 for(const l of lines){const dep=(l.match(/^\s*/)[0].length)/2;const nm=l.trim().replace(/^[└├│─\s]+/,'');
  const nd=mk({sym:nm,kids:[],open:true});while(stack.length>dep)stack.pop();(stack.length?stack[stack.length-1].kids:roots).push(nd);stack.push(nd)}
 for(const x of nodes)if(x.kids&&!x.kids.length){x.leaf=true}
 if(roots[0])roots[0].label=`${short(roots[0].sym)} <span class="dim">derived types: ${n(d.derived_total)}${d.truncated?' (truncated)':''}</span>`;
 return [...out,...roots]}
async function buildTree(){nodes=[];selNode=null;if(!cur){drawTree();return}
 $('#tree').innerHTML='<div class="empty"><span class="spin"></span></div>';
 try{if(tmode==='callers'||tmode==='callees'){const r=mk({root:true,sym:cur.sym,path:cur.path,line:cur.line,dpath:cur.path,noroot:true,load:tmode==='callers'?loadCallers:loadCallees});
   r.label=`${short(cur.sym)} <span class="dim">${tmode==='callers'?'is called by':'calls'}</span>`;drawTree();await expand(r)}
  else if(tmode==='outline'){(await outlineTree(cur.path)).forEach(r=>r.root=true);const hit=nodes.find(x=>x.sym===cur.sym);if(hit)selNode=hit.id;drawTree();
   const el=$('#tree .tn.cur');if(el)el.scrollIntoView({block:'nearest'})}
  else{(await typeTree(cur.sym)).forEach(r=>r.root=true);drawTree()}}
 catch(e){$('#tree').innerHTML=`<div class="empty err">${esc(e.message)}</div>`}}
function segs(){document.querySelectorAll('#tmode a').forEach(a=>a.classList.toggle('on',a.dataset.m===tmode))}
$('#tmode').addEventListener('click',e=>{const a=e.target.closest('[data-m]');if(!a)return;tmode=a.dataset.m;localStorage.setItem('nncq.tmode',tmode);segs();buildTree()});
$('#tree').addEventListener('click',async e=>{const r=e.target.closest('[data-r]');if(r){const nd=nodes[+r.dataset.r];openSym(nd.sym,nd.dpath||nd.path);return}
 const t=e.target.closest('[data-t]');const row=e.target.closest('.tn');if(!row)return;const nd=nodes[+row.dataset.id];
 if(t&&!nd.leaf){expand(nd);return}
 selNode=nd.id;drawTree();
 if(nd.callPath&&tmode==='callees')show(nd.callPath,nd.callLine);else if(nd.path)show(nd.path,nd.line,nd.end);
 else if(nd.sym&&tmode==='types')openSym(nd.sym)});
$('#tree').addEventListener('dblclick',e=>{const row=e.target.closest('.tn');if(!row)return;const nd=nodes[+row.dataset.id];if(nd.sym&&!nd.noroot)openSym(nd.sym,nd.dpath||nd.path)});
async function show(path,line,end){setView('sym');$('#cpath').innerHTML=`<span class="mono">${esc(path)}</span><span>${line?':'+line:''}</span>`;$('#sview').innerHTML='<span class="spin"></span>';
 try{const a=Math.max(1,(line||1)-15),b=end&&end-line<300?end+8:(line||1)+60;const d=await T('read_code',{path,start:a,end:b});const c=numbered(d.code);
  $('#sview').innerHTML=codeHtml(c.text,c.start,d.path,line);const mkr=$('#mk'),box=$('#sview').parentElement;
  if(mkr)box.scrollTop=mkr.offsetTop-box.clientHeight/2}
 catch(e){$('#sview').innerHTML=`<span class="err">${esc(e.message)}</span>`}}
function chips(list,label){if(!list||!list.length)return'';return `<div class="chips"><span class="dim" style="font-size:11px;padding:2px 0">${label}</span>${list.slice(0,12).map(s=>{const m=reCaller.exec(s)||reCall.exec(s);
 const sym=m?(reCaller.test(s)?m[1]:((/^(\S+) /.exec(m[4]||'')||[])[1]||m[2])):s.split(' ')[0];return `<span class="chip" data-s="${esc(sym)}" title="${esc(s)}">${short(sym)}</span>`}).join('')}${list.length>12?`<span class="dim" style="font-size:11px">+${list.length-12}</span>`:''}</div>`}
$('#scard').addEventListener('click',e=>{const c=e.target.closest('[data-s]');if(c)openSym(c.dataset.s,c.dataset.p||'')});
function findSym(q){q=(q||'').trim();if(q)openSym(q)}
async function openSym(name,path,again){if(!nav)target('sym');$('#sq').value=name;setHash({s:name,p:path||''},!again);setView('sym');
 $('#scard').innerHTML='<span class="spin"></span>';
 try{let d;try{d=await T('symbol_context',{name,path:path||''})}catch(e){d={error:e.message}}
  if(d.error||!d.defined_at){const f=await T('find_symbol',{name});const defs=(f.definitions||[]).map(s=>reDef.exec(s)).filter(Boolean);
   if(defs.length===1)return openSym(defs[0][1],defs[0][3],true);
   $('#scard').innerHTML=`<div class="t">${esc(name)}</div><div class="dim">${defs.length?defs.length+' definitions: pick one':'not found'+(d.error?': '+esc(d.error):'')}</div>
    <div class="chips">${defs.map(m=>`<span class="chip" data-s="${esc(m[1])}" data-p="${esc(m[3])}" title="${esc(m[3])}:${m[4]}"><b>${esc(m[1])}</b> <span class="dim">${esc(m[2])} · ${esc(base(m[3]))}</span></span>`).join('')}</div>`;return}
  const at=String(d.defined_at[0]),m=/^(.+):(\d+)(?:-(\d+))?$/.exec(at),p=m?m[1]:at,a=m?+m[2]:1,b=m&&m[3]?+m[3]:a;
  cur={sym:d.symbol||name,req:name,path:p,line:a,end:b,kind:d.kind};$('#s-symbols').classList.remove('idle');setView('sym');
  if(!nav)srecentAdd(cur.sym,p);else srecent();follow({s:cur.sym,p});
  const facts=[d.call_sites_total&&`<span class="chip"><b>${esc(d.call_sites_total)}</b></span>`,d.mentioned_in_files!=null&&`<span class="chip">mentioned in <b>${n(d.mentioned_in_files)}</b> files</span>`,
   d.defined_at.length>1&&`<span class="chip">${d.defined_at.length} definitions</span>`].filter(Boolean).join('');
  $('#scard').innerHTML=`<div class="t">${short(cur.sym)} <span class="kind k-${esc(d.kind||'')}">${esc(d.kind||'')}</span></div>
   ${d.parent?`<div class="dim" style="font-size:11px">in <span class="chip" data-s="${esc(d.parent)}">${esc(d.parent)}</span></div>`:''}
   ${d.signature?`<div class="sig">${esc(d.signature)}</div>`:''}<div class="chips">${facts}</div>
   ${chips(d.bases,'bases')}${chips(d.derived,'derived')}${chips(d.callers,'callers')}${chips((d.calls||[]).filter(s=>!/ambiguous|not in index/.test(s)),'calls')}`;
  show(p,a,b);buildTree()}
 catch(e){$('#scard').innerHTML=`<span class="err">${esc(e.message)}</span>`}}

/* ---------- panes: dashboard | Symbol explorer | Model explorer; collapsible to a strip, resizable, persisted ---------- */
const PANES=['dash','sym','model'],PDEF={dash:36,sym:30,model:34};
const PS=(()=>{let s={};try{s=JSON.parse(localStorage.getItem('nncq.panes'))||{}}catch(e){}return {w:{...PDEF,...(s.w||{})},c:{dash:false,sym:false,model:false,...(s.c||{})}}})();
if(QS.has('collapse')){const l=QS.get('collapse').split(',');PANES.forEach(p=>PS.c[p]=l.includes(p))}   /* test hook: ?collapse=sym,model */
const paneEl=p=>document.querySelector(`.xp[data-pane="${p}"]`);
function layout(){PANES.forEach(p=>{const el=paneEl(p);el.classList.toggle('col',!!PS.c[p]);el.style.flex=PS.c[p]?'':`${PS.w[p]} 1 0px`});
 document.querySelectorAll('.xs').forEach(s=>{const i=+s.dataset.i;s.classList.toggle('off',!!PS.c[PANES[i]]||!PANES.slice(i+1).some(p=>!PS.c[p]))});
 localStorage.setItem('nncq.panes',JSON.stringify(PS))}
function setCol(p,c){if(!!PS.c[p]===c)return;PS.c[p]=c;layout();if(!c&&p==='model')catchUp()}
/* an action aimed at an explorer: expanded -> just navigate it; collapsed -> it swaps with the other explorer,
   or, when both are collapsed, with the dashboard pane */
function target(t,quiet){if(!PS.c[t])return;const o=t==='sym'?'model':'sym';
 if(!PS.c[o])PS.c[o]=true;else PS.c.dash=true;PS.c[t]=false;layout();if(t==='model'&&!quiet)catchUp()}
document.addEventListener('click',e=>{const b=e.target.closest('.xc[data-col]');if(b){setCol(b.dataset.col,true);return}
 const x=e.target.closest('.xp.col');if(x)setCol(x.dataset.pane,false)});
document.addEventListener('pointerdown',e=>{const s=e.target.closest('.xs');if(!s||s.classList.contains('off'))return;const i=+s.dataset.i;
 const L=PANES.slice(0,i+1).reverse().find(p=>!PS.c[p]),R=PANES.slice(i+1).find(p=>!PS.c[p]);if(!L||!R)return;
 e.preventDefault();const a=paneEl(L).getBoundingClientRect().width,b=paneEl(R).getBoundingClientRect().width,sum=PS.w[L]+PS.w[R],x0=e.clientX,MIN=240;
 s.setPointerCapture(e.pointerId);s.classList.add('on');document.body.classList.add('drag');
 const mv=ev=>{const nl=Math.max(MIN,Math.min(a+b-MIN,a+ev.clientX-x0));PS.w[L]=sum*nl/(a+b);PS.w[R]=sum-PS.w[L];layout()};
 const up=()=>{s.removeEventListener('pointermove',mv);s.removeEventListener('pointerup',up);s.classList.remove('on');document.body.classList.remove('drag')};
 s.addEventListener('pointermove',mv);s.addEventListener('pointerup',up)});
document.addEventListener('dblclick',e=>{if(e.target.closest('.xs')){PS.w={...PDEF};layout()}});

/* ---------- the Model explorer: /views?embed=1 in an iframe; follows the Symbol explorer unless pinned ---------- */
const fr=$('#mframe');let frReady=false,frPend=null,missed=false,lastT=null,skipFollow=false,noFollow=false;
const mpinned=()=>localStorage.getItem('nncq.mpin')==='1';
function dhash(t){const p=new URLSearchParams();if(t&&t.s)p.set('s',t.s);if(t&&t.p)p.set('p',t.p);if(multi&&sel)p.set('repo',sel);const q=p.toString();return '#/uml/default'+(q?'?'+q:'')}
function mnav(h,push){if(!frReady){frPend=h;return}const w=fr.contentWindow;if(w.location.hash===h)return;
 if(push)w.location.hash=h;else w.location.replace(w.location.pathname+w.location.search+h)}
fr.addEventListener('load',()=>{frReady=true;if(frPend){const h=frPend;frPend=null;mnav(h)}});
function curTarget(){return view==='sym'&&cur?{s:cur.sym,p:cur.path}:view==='hits'&&ans&&ans.files&&fsel>=0?{p:ans.files[fsel].p}:null}
function follow(t){lastT=t;$('#mlink').href='views'+dhash(curTarget()||t);if(skipFollow||noFollow||mpinned())return;
 if(PS.c.model){missed=true;return}missed=false;mnav(dhash(t))}
function catchUp(){if(missed&&lastT&&!mpinned()){missed=false;mnav(dhash(lastT))}}
$('#mlink').addEventListener('click',e=>{if(e.ctrlKey||e.metaKey||e.shiftKey||e.button)return;e.preventDefault();
 target('model',true);missed=false;mnav(dhash(curTarget()),true)});
/* called by the embedded Model explorer (same origin) */
window.nncqHost={openSym(q,p){target('sym');noFollow=true;Promise.resolve(openSym(q,p||'')).finally(()=>{noFollow=false})},
 collapse(p){setCol(p,true)},modelHash(h){setHash({m:h&&h!=='#'?h.replace(/^#/,''):''})}};
function srecent(){const r=JSON.parse(localStorage.getItem('nncq.srecent')||'[]');
 $('#srecent').innerHTML=r.map(x=>`<a data-s="${esc(x.s)}" data-p="${esc(x.p||'')}" class="${cur&&cur.sym===x.s?'cur':''}" title="${esc(x.s)}${x.p?' · '+esc(x.p):''}">${esc(String(x.s).split(/::|\./).slice(-2).join('::'))}</a>`).join('')}
function srecentAdd(s,p){const r=[{s,p},...JSON.parse(localStorage.getItem('nncq.srecent')||'[]').filter(x=>x.s!==s)].slice(0,10);
 localStorage.setItem('nncq.srecent',JSON.stringify(r));srecent()}
$('#srecent').addEventListener('click',e=>{const a=e.target.closest('[data-s]');if(a)openSym(a.dataset.s,a.dataset.p)});
function debugPanel(){const d=document.createElement('div');d.id='dbg';document.body.appendChild(d);
 setInterval(()=>{let f={};try{const w=fr.contentWindow,q=s=>w.document.querySelectorAll(s);
   f={hash:w.location.hash,title:(w.document.querySelector('#vt')||{}).textContent,svg:q('#out svg').length,nodes:q('#out [data-n]').length,
    list:q('#list .vi').length,err:[...q('#out .err')].map(x=>x.textContent).join(' | '),errs:w.__errs||[],embed:w.document.body.classList.contains('embed')}}catch(e){f={x:String(e)}}
  d.textContent='DBG '+JSON.stringify({errs:window.__errs,c:PS.c,w:PS.w,view,sym:cur&&cur.sym,hash:location.hash,frame:f})},300)}

/* ---------- start ---------- */
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
document.addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|SELECT/.test(document.activeElement.tagName)){e.preventDefault();setCol('dash',false);$('#q').focus()}});
document.addEventListener('click',e=>{const s=e.target.closest('summary');if(s&&!e.target.closest('.tw'))e.preventDefault()},true);new MutationObserver(()=>rzApply($('#answer'))).observe($('#answer'),{childList:true});rzApply();segs();recent();srecent();layout();
{const h0=new URLSearchParams(location.hash.slice(1)),m0=(h0.get('m')||'').replace(/^#/,''),s0=h0.get('s')||'';
 const nt=(performance.getEntriesByType('navigation')[0]||{}).type;
 if(nt!=='reload'&&nt!=='back_forward'){if(m0&&!s0)target('model',true);else if(s0&&!m0)target('sym',true)}
 skipFollow=!!m0;const h1=m0?'#'+m0:s0&&!mpinned()?dhash({s:s0,p:h0.get('p')||''}):'';frPend=null;
 fr.src='views?embed=1'+(tok?'&token='+encodeURIComponent(tok):'')+(QS.get('debug')==='1'?'&debug=1':'')+h1}
if(QS.get('debug')==='1')debugPanel();
setView(new URLSearchParams(location.hash.slice(1)).get('v')||'hits');
applyHash().finally(()=>{skipFollow=false});
poll();
</script></body></html>
"""
