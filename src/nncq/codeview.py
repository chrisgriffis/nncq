"""The code view both pages share (dashboard and Model explorer), inlined into each page (CODEVIEW_JS):

  NC.hl / NC.text      light syntax colouring: C / C++ / C# / JS, Python, Lua, CMake, JSON, Mermaid, Markdown
  NC.code / NC.fill    numbered source lines; NC.fill renders at once, then asks /api/code/links for the symbol
                       links of those lines and re-renders with them: calls, types, members, includes / imports
                       become clickable `.sym` spans (same class and handler as symbol names in search hits),
                       definitions get anchors; ambiguous ones open a candidate list
  NC.editor            plain-text editing with line numbers: Save (Ctrl+S) through /api/uml/local/save (loopback),
                       git / p4 buttons, then a delta re-index of the file; Cancel returns to the view

links() computes the spans server-side from the index (edges with line / column, symbol definitions).
"""
from __future__ import annotations

import posixpath

import json

from nncq import graph
from nncq.store import Store
from nncq.uml.render_mermaid import PALETTE

_LINK_KINDS = ("call", "include", "import", "inherits", "composes", "aggregates", "associates", "depends",
               "instantiates", "var")


def links(st: Store, path: str, start: int = 1, end: int = 1 << 30, cap: int = 2000) -> dict:
    """Symbol links for lines start..end of a file: [{line, col, name, kind, sym, path, to_line} | {..., file} |
    {..., candidates: [{sym, path, to_line}]} | {..., def: true}]. name is the identifier to mark on that line."""
    row = st.q("SELECT id FROM files WHERE path=?", (path,))
    if not row:
        return {"path": path, "links": []}
    fid = row[0][0]
    fam = graph.lang_family(st, fid)
    out: list[dict] = []
    seen: set[tuple] = set()
    for q, name, kind, line in st.q("SELECT qualname, name, kind, start_line FROM symbols WHERE file_id=? AND "
                                    "start_line BETWEEN ? AND ?", (fid, start, end)):
        out.append({"line": line, "name": name, "kind": kind, "def": True, "sym": q, "path": path, "to_line": line})
        seen.add((line, name))
    deps = [p for (p,) in st.q("SELECT f.path FROM file_deps d JOIN files f ON f.id=d.dst_id WHERE d.src_id=?", (fid,))]
    memo: dict[tuple, dict | None] = {}

    def target(kind: str, dst: str, src: str) -> dict | None:
        k = (kind, dst, graph._parent_short(src) if kind == "call" else "")
        if k in memo:
            return memo[k]
        res = None
        if kind in ("include", "import"):
            d = dst.strip("\"'<> ").replace("\\", "/").lower()          # include edges are stored lower-case
            stem = d.lstrip(".").replace(".", "/") if kind == "import" else d
            hit = [p for p in deps if (pl := p.lower()).endswith("/" + stem) or pl == stem or pl.endswith("/" + stem + ".py")
                   or pl.endswith("/" + stem + "/__init__.py")]
            if hit:
                res = {"file": hit[0]}
        else:
            defs = graph._defs_exact(st, graph.short(dst), fam) if kind == "call" else \
                [x for x in graph.find_defs(st, dst, kinds={"type"}) if graph.is_type(x.kind)
                 and (not fam or set(graph.lang_family(st, x.file_id)) & set(fam))]
            groups: dict[str, graph.Def] = {}
            for x in defs:
                if kind == "call" and x.kind not in graph.FUNCS and not graph.is_type(x.kind):
                    continue
                g = graph.norm(x.qualname)
                if g not in groups or (groups[g].signature or "").endswith(";"):
                    groups[g] = x
            own = graph._parent_short(src)
            mine = [x for x in groups.values() if own and graph._parent_short(x.qualname) == own] if kind == "call" else []
            pick = list(groups.values()) if len(groups) == 1 else mine if len(mine) == 1 else []
            if pick:
                res = {"sym": pick[0].qualname, "path": pick[0].path, "to_line": pick[0].start, "def_kind": pick[0].kind}
            elif groups:
                res = {"candidates": [{"sym": x.qualname, "path": x.path, "to_line": x.start}
                                      for x in list(groups.values())[:10]], "total": len(groups)}
        memo[k] = res
        return res
    for src, dst, kind, line, col in st.q(
            f"SELECT src, dst, kind, line, col FROM edges WHERE file_id=? AND line BETWEEN ? AND ? AND kind IN "
            f"({','.join('?' * len(_LINK_KINDS))}) ORDER BY line LIMIT ?", (fid, start, end, *_LINK_KINDS, cap)):
        name = posixpath.basename(dst.strip("\"'<> ")) if kind == "include" else \
            dst.rsplit(".", 1)[-1].lstrip(".") if kind == "import" else graph.short(dst)
        if not name or (line, name) in seen:
            continue
        t = target(kind, dst, src or "")
        if t is None:
            continue
        if kind == "include":
            name = posixpath.basename(t["file"])                 # as written in the source, not lower-cased
        seen.add((line, name))
        out.append({"line": line, "col": col, "name": name, "kind": kind, **t})
    return {"path": path, "links": out}


def codeview_js() -> str:
    """The shared script with the diagram palette filled in (NC.pal: the same fills as Mermaid exports)."""
    return CODEVIEW_JS.replace("__PALETTE__", json.dumps(PALETTE))


CODEVIEW_JS = r"""
/* ---------- shared code view (nncq/codeview.py): colouring, symbol links, editor ---------- */
const NC=(()=>{const E=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
 const KW={c:'if else for while do switch case default break continue return goto try catch throw finally new delete class struct enum union namespace using typedef template typename public private protected virtual override final static const constexpr inline extern volatile mutable explicit friend operator sizeof this nullptr true false void bool char int float double long short unsigned signed auto decltype noexcept var let function interface abstract sealed readonly get set ref out base is foreach internal string object async await import export from',
  py:'def class return if elif else for while in not and or is None True False import from as with try except finally raise pass break continue lambda yield async await global nonlocal del assert self',
  lua:'local function end then elseif nil and or not in repeat until return if else for while do break true false self',
  cmake:'if elseif else endif foreach endforeach function endfunction macro endmacro set unset option project add_library add_executable target_link_libraries target_include_directories target_compile_definitions target_compile_options target_sources find_package include add_subdirectory message install PUBLIC PRIVATE INTERFACE STATIC SHARED',
  mermaid:'flowchart graph subgraph end classDiagram sequenceDiagram class direction style classDef click href participant actor note Note over link cssClass LR TB RL BT _blank',json:'true false null'};
 const KS={};for(const k in KW)KS[k]=new Set(KW[k].split(' '));
 const langOf=p=>/\.pyi?$/i.test(p)?'py':/\.lua$/i.test(p)?'lua':/(^|\/)CMakeLists\.txt$|\.cmake$/i.test(p)?'cmake':/\.md$/i.test(p)?'md':/\.json$/i.test(p)?'json':'c';
 const tip=l=>l.candidates?`${l.total||l.candidates.length} candidates: click to choose`:l.file?`open ${l.file}`:`${l.def?'definition: ':''}${l.def_kind||l.kind||''} ${l.sym||''}\n${l.path||''}:${l.to_line||''}`;
 const span=(l,inner)=>`<span class="sym nc${l.def?' def':''}${l.candidates?' amb':''}" data-l="${E(JSON.stringify(l))}" title="${E(tip(l))}">${inner}</span>`;
 /* one line: lk = {name: link} for this line (identifiers, and string tokens holding an include) */
 function hl(line,lang,lk){const cm={c:'//',py:'#',lua:'--',cmake:'#',mermaid:'%%'}[lang];let ci=-1;
  if(cm){let q=null;for(let j=0;j<line.length;j++){const ch=line[j];if(q){if(ch==='\\'){j++;continue}if(ch===q)q=null}else if(ch==='"'||(ch==="'"&&lang!=='cmake'&&lang!=='mermaid'))q=ch;else if(line.startsWith(cm,j)){ci=j;break}}}
  const code=ci>=0?line.slice(0,ci):line,kw=KS[lang]||new Set();let out='',i=0,m;
  const re=/("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|<[\w./-]+\.h\w*>)|(\b\d[\w.]*\b)|([A-Za-z_~][\w]*)|(#\s*\w+|\$\{[^}]*\}|-->|-\.->|--o|<\|--|\.\.>|==>)/g;
  while((m=re.exec(code))){out+=E(code.slice(i,m.index));const t=m[0];
   if(m[1]){const key=lang==='json'&&/^\s*:/.test(code.slice(m.index+t.length));const inc=lk&&Object.values(lk).find(l=>l.file&&t.includes(l.name));
    const h=`<span class="${key?'pp':'s'}">${E(t)}</span>`;out+=inc?span(inc,h):h}
   else if(m[2])out+=`<span class="n">${E(t)}</span>`;
   else if(m[3]){const l=lk&&lk[t.replace(/^~/,'')]||lk&&lk[t];const h=kw.has(t)?`<span class="k">${t}</span>`:(lang==='c'&&/^[A-Z]\w*[a-z]\w*$/.test(t)?`<span class="ty">${t}</span>`:E(t));out+=l?span(l,h):h}
   else out+=(lang==='py'&&t[0]==='#')?E(t):`<span class="pp">${E(t)}</span>`;i=m.index+t.length}
  out+=E(code.slice(i));if(ci>=0)out+=`<span class="c">${E(line.slice(ci))}</span>`;return out}
 /* whole texts without line numbers: JSON, Mermaid, Markdown (fences coloured by their language) */
 function text(t,lang){const ls=String(t??'').replace(/\r/g,'').split('\n');if(lang!=='md')return ls.map(l=>hl(l,lang)).join('\n');
  let fence=null;return ls.map(l=>{if(/^\s*```/.test(l)){fence=fence?null:(l.trim().slice(3)||'text');return `<span class="k">${E(l)}</span>`}
   if(fence)return hl(l,fence==='mermaid'?'mermaid':fence==='python'?'py':fence==='json'?'json':'c');
   if(/^\s*<!--/.test(l))return `<span class="c">${E(l)}</span>`;if(/^#{1,6}\s/.test(l))return `<span class="h">${E(l)}</span>`;
   return E(l).replace(/^(\s*[-*]\s)/,'<span class="k">$1</span>').replace(/`([^`]+)`/g,'<span class="s">`$1`</span>').replace(/\[([^\]]+)\]\(([^)]+)\)/g,'[<span class="h">$1</span>](<span class="pp">$2</span>)')}).join('\n')}
 /* numbered lines; mark = a line or a Set of lines; links = [{line, name, ...}] */
 function code(t,start,path,mark,links){const lang=langOf(path),many=mark instanceof Set,by={};
  for(const l of links||[])(by[l.line]=by[l.line]||{})[l.name]=by[l.line][l.name]&&by[l.line][l.name].def?by[l.line][l.name]:l;
  return String(t).replace(/\r/g,'').split('\n').map((l,i)=>{const no=start+i,on=many?mark.has(no):no===mark;
   return `<span class="l${on?' hl':''}" data-ln="${no}"${!many&&on?' id="mk"':''}><span class="ln">${no}</span>${hl(l,lang,by[no])}</span>`}).join('')}
 const numbered=c=>{const rows=String(c).replace(/\r/g,'').split('\n');const m=/^(\d+): /.exec(rows[0]||'');return {start:m?+m[1]:1,text:rows.map(r=>r.replace(/^\d+: /,'')).join('\n')}};
 let api=null,onPick=null;
 /* render into el now, then again with links once /api/code/links answers (cached per range) */
 const LC=new Map();
 async function fill(el,t,start,path,mark,o){el.innerHTML=code(t,start,path,mark);if(!api||!path)return;const n=String(t).split('\n').length,k=path+'|'+start+'|'+n;
  try{let p=LC.get(k);if(!p){p=api('api/code/links',{path,start,end:start+n-1});LC.set(k,p);if(LC.size>200)LC.delete(LC.keys().next().value)}
   const r=await p;if(r&&r.links&&r.links.length&&el.isConnected){const sc=el.parentElement?el.parentElement.scrollTop:0;el.innerHTML=code(t,start,path,mark,r.links);if(el.parentElement)el.parentElement.scrollTop=sc}}catch(e){LC.delete(k)}}
 function menu(l,x,y){let m=document.getElementById('ncmenu');if(!m){m=document.createElement('div');m.id='ncmenu';document.body.appendChild(m)}
  m.innerHTML=`<div class="msg">${E(l.name)}: ${l.total||l.candidates.length} candidates</div>`+l.candidates.map((c,i)=>`<a data-i="${i}" title="${E(c.path)}:${c.to_line}"><b>${E(c.sym)}</b> <span class="dim">${E(String(c.path).split('/').pop())}:${c.to_line}</span></a>`).join('');
  m.style.display='block';m.style.left=Math.min(x,innerWidth-380)+'px';m.style.top=Math.min(y+8,innerHeight-40)+'px';
  m.onclick=e=>{const a=e.target.closest('[data-i]');if(!a)return;m.style.display='none';onPick&&onPick({...l.candidates[+a.dataset.i],kind:l.kind},e)}}
 document.addEventListener('click',e=>{const s=e.target.closest('.sym.nc');const m=document.getElementById('ncmenu');
  if(!s){if(m&&!e.target.closest('#ncmenu'))m.style.display='none';return}
  e.preventDefault();e.stopPropagation();let l;try{l=JSON.parse(s.dataset.l)}catch(x){return}
  if(l.candidates)return menu(l,e.clientX,e.clientY);onPick&&onPick(l,e)},true);
 const css=document.createElement('style');css.textContent=`.sym.nc{cursor:pointer;border-bottom:1px dotted #5a6375}.sym.nc:hover{color:var(--acc,#5aa9ff);border-bottom-color:currentColor}
  .sym.nc.def{border-bottom:1px solid #3d4658}.sym.nc.amb{border-bottom-style:dashed}
  #ncmenu{position:fixed;z-index:60;background:#151821;border:1px solid #232733;border-radius:6px;padding:4px 0;font-size:12px;box-shadow:0 6px 20px #0008;max-height:50vh;overflow:auto;display:none}
  #ncmenu a{display:block;padding:4px 12px;cursor:pointer;white-space:nowrap;color:#d7dae0}#ncmenu a:hover{background:#ffffff14}#ncmenu .msg{padding:4px 12px;color:#7d8490}
  .nce{display:flex;flex-direction:column;height:100%;min-height:240px}.nce-bar{display:flex;gap:6px;align-items:center;padding:6px 10px;border-bottom:1px solid #232733;flex-wrap:wrap;font-size:12px}
  .nce-bar .sp{flex:1}.nce-bar .st{color:#7d8490}.nce-body{flex:1;display:flex;min-height:0;background:#0b0d11}
  .nce-gut{margin:0;padding:8px 8px 8px 0;width:56px;text-align:right;color:#475060;overflow:hidden;font:12px/1.5 ui-monospace,Cascadia Mono,Consolas,monospace;user-select:none;flex:none}
  .nce textarea{flex:1;resize:none;border:0;outline:none;background:transparent;color:#d7dae0;padding:8px;font:12px/1.5 ui-monospace,Cascadia Mono,Consolas,monospace;white-space:pre;tab-size:4;overflow:auto}`;
 document.head.appendChild(css);
 /* plain-text editor: Save (Ctrl+S) via the loopback save op, git / p4, then the server re-indexes the file */
 async function editor(box,o){const L=(op,a)=>api('api/uml/local/'+op,{...a,repo:o.repo||''});const d=await L('read',{path:o.path});
  if(d.error){box.innerHTML=`<div class="err" style="padding:10px">${E(d.error)}</div>`;return}
  let sha1=d.sha1,dirty=false;const v=d.vcs||{},isP4=v.vcs==='p4',isGit=v.vcs==='git';
  const vst=(v,ro)=>`${E(v.vcs||'no vcs')}: ${E(v.state||'')}${ro?' · <span class="err">read-only'+(isP4?' (p4 edit to check out)':'')+'</span>':''}${v.error||v.note?' · '+E(v.error||v.note):''}`;
  box.innerHTML=`<div class="nce"><div class="nce-bar"><b class="mono">${E(o.path)}</b><span class="st">${vst(v,d.readonly)}</span><span class="sp"></span>
   ${isP4?'<button data-v="edit">p4 edit</button><button data-v="add">p4 add</button><button data-v="revert">p4 revert</button>':''}${isGit?'<button data-v="stage">git stage</button><button data-v="revert">git revert</button>':''}
   <button data-e="save" title="Ctrl+S">Save</button><button data-e="cancel" title="back to the view (Esc)">Cancel</button></div>
   <div class="nce-body"><pre class="nce-gut"></pre><textarea spellcheck="false"></textarea></div></div>`;
  const ta=box.querySelector('textarea'),gut=box.querySelector('.nce-gut'),st=box.querySelector('.st');ta.value=d.text;
  const nums=()=>{const n=ta.value.split('\n').length;if(gut.dataset.n!=n){gut.dataset.n=n;gut.textContent=Array.from({length:n},(_,i)=>i+1).join('\n')}};nums();
  ta.addEventListener('input',()=>{dirty=true;nums()});ta.addEventListener('scroll',()=>{gut.scrollTop=ta.scrollTop});
  const pos=d.text.split('\n').slice(0,Math.max(0,(o.line||1)-1)).join('\n').length;ta.focus();ta.setSelectionRange(pos,pos);ta.scrollTop=Math.max(0,((o.line||1)-8)*18);
  const save=async()=>{const j=await L('save',{path:o.path,text:ta.value,sha1});if(j.error){st.innerHTML=`<span class="err">${E(j.error)}</span>`;return false}
   sha1=j.sha1;dirty=false;LC.clear();st.innerHTML='saved · re-indexing · '+vst(j.vcs||{});o.onSaved&&o.onSaved();return true};
  const close=()=>{if(dirty&&!confirm('Discard unsaved changes?'))return;o.onClose&&o.onClose()};
  ta.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();save()}else if(e.key==='Escape'){e.preventDefault();close()}
   else if(e.key==='Tab'){e.preventDefault();const a=ta.selectionStart;ta.setRangeText('\t',a,ta.selectionEnd,'end');dirty=true}});
  box.querySelector('.nce-bar').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;
   if(b.dataset.e==='save')return save();if(b.dataset.e==='cancel')return close();
   const j=await L('vcs',{path:o.path,op:b.dataset.v});st.innerHTML=j.error?`<span class="err">${E(j.error)}</span>`:'ok · '+vst(j.status||j);
   if(!j.error&&b.dataset.v==='revert'){const r=await L('read',{path:o.path});if(!r.error){ta.value=r.text;sha1=r.sha1;dirty=false;nums()}}})}
 /* pan / zoom an SVG inside a scroll box: wheel zooms at the cursor (the SVG's own width: crisp), drag pans;
    box.dataset.panned is set while a drag just happened, so click handlers can ignore it */
 function panzoom(box,svg){if(!svg)return;const vb=svg.viewBox&&svg.viewBox.baseVal,w0=vb&&vb.width||svg.getBBox().width;let z=Math.min(1,(box.clientWidth-16)/w0||1);
  const set=v=>{z=Math.max(.1,Math.min(6,v));svg.style.width=(w0*z)+'px';svg.style.maxWidth='none';svg.removeAttribute('height')};set(z);
  box.addEventListener('wheel',e=>{e.preventDefault();const r=box.getBoundingClientRect(),sr=svg.getBoundingClientRect(),px=(e.clientX-sr.left)/z,py=(e.clientY-sr.top)/z,z1=z*Math.pow(1.0015,-e.deltaY);
   set(z1);const sr2=svg.getBoundingClientRect();box.scrollLeft+=sr2.left+px*z-e.clientX;box.scrollTop+=sr2.top+py*z-e.clientY},{passive:false});
  let p=null;box.addEventListener('pointerdown',e=>{if(e.button)return;p={x:e.clientX,y:e.clientY,l:box.scrollLeft,t:box.scrollTop,m:false};delete box.dataset.panned});
  box.addEventListener('pointermove',e=>{if(!p)return;const dx=e.clientX-p.x,dy=e.clientY-p.y;if(!p.m&&Math.hypot(dx,dy)<5)return;p.m=true;box.dataset.panned='1';box.scrollLeft=p.l-dx;box.scrollTop=p.t-dy});
  const end=()=>{if(p&&p.m)setTimeout(()=>delete box.dataset.panned,0);p=null};box.addEventListener('pointerup',end);box.addEventListener('pointerleave',end)}
 return {hl,text,code,fill,numbered,langOf,editor,panzoom,pal:__PALETTE__,set api(f){api=f},set onPick(f){onPick=f}}})();
"""
