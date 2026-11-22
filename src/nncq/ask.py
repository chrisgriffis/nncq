"""ask / expand: a compact, generated answer map for a question. No LLM: everything is derived from the index.

ask(question) returns, in a few KB instead of pages of code:
  interpreted  how the question was read: search terms (with stem expansions), how rare each is in the code,
               how many hits each reached, words ignored, how much of the code the meaning search covers, and
               the code's own vocabulary for the topic (identifiers recurring across hits, rare elsewhere)
  portfolio    what the result set is: areas (folders) and counts, kinds, which channel found the hits
               (keyword / meaning / both), match strength, and where each channel led on its own
  hits         one line of facts per hit: signature, doc comment, meaningful callees (plumbing filtered),
               callers, bases/derived/members for types, the 1-2 lines that best match, and why it ranked
  links        how the hits relate: direct calls, one-hop calls via another function, shared classes
  next         concrete follow-ups (expand, call_trace, narrower asks)

expand(refs, what) digs into hits: exact code, symbol context, callers, callees or the file outline.
Refs are chunk ids ("c123"): short and stateless, valid until that file is re-indexed.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from pathlib import Path

from nncq import config, graph, search
from nncq.repos import Repo
from nncq.store import Store, split_ident

DOCS = {"section", "text", "module_doc"}
FILE_LEVEL = {"namespace", "module"}
AREA_DEPTH = 4
UTILITY_CALLS = graph.UTILITY_CALLS
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{5,}\b")
_COMMENT = re.compile(r'^\s*(?:///?[<!]?|/\*+!?|\*(?!/)|#(?!\s*(?:include|define|if|ifdef|ifndef|endif|else|elif|'
                      r'pragma|import|undef|region|endregion)\b)|--+|"""|\'\'\'|<!--)\s?')
_JUNK = re.compile(r"^[\s=\-*/#~_+<>!]*$|copyright|licen[cs]e|\(c\)|all rights reserved|^\s*@", re.I)
_MACRO = graph.MACRO
_SENT = re.compile(r"(?<=[a-z0-9)\]])[.!?](\s|$)")
_KW = set(("return const static void class struct public private protected virtual override inline unsigned "
           "nullptr template typename namespace default include define").split())

_ndocs: dict[str, tuple[float, int]] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ question -> terms

def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ers", "er", "ies", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            base = w[:-len(suf)] + ("y" if suf == "ies" else "")
            if len(base) > 4 and base[-1] == base[-2] and base[-1] not in "lsz":
                base = base[:-1]
            return base
    return w


def _terms(question: str) -> tuple[list[dict], list[str], str]:
    toks = _WORD.findall(question)
    ignored = [t for t in toks if t.lower() in search._STOP]
    keep = [t for t in toks if t.lower() not in search._STOP] or toks
    terms, alts = [], []
    for t in dict.fromkeys(keep):
        low, parts = t.lower(), split_ident(t)
        pre = _stem(low) if not parts and t.isalpha() else low
        terms.append({"word": t, "exact": low, "parts": parts, "prefix": pre if pre != low and len(pre) >= 4 else ""})
        if len(low) > 1:
            alts.append(f'"{low}"')
        alts += [f'"{p}"' for p in parts]
        if terms[-1]["prefix"]:
            alts.append(f'"{terms[-1]["prefix"]}"*')
    return terms, ignored, " OR ".join(dict.fromkeys(alts))


def _needles(t: dict) -> list[str]:
    return [t["exact"], *t["parts"], *([t["prefix"]] if t["prefix"] else [])]


def _matched(t: dict, hay: str) -> bool:
    return any(n in hay for n in _needles(t))


def _vocab(store: Store) -> bool:
    try:
        store.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.nncq_vocab USING fts5vocab(main, fts, row)")
        return True
    except Exception:
        return False


def _total_docs(store: Store) -> int:
    key = str(store.path)
    with _lock:
        hit = _ndocs.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    n = store.q("SELECT COUNT(*) FROM chunks")[0][0]
    with _lock:
        _ndocs[key] = (time.time(), n)
    return n


def _df(store: Store, term: str, prefix: bool = False) -> int:
    if prefix:
        r = store.q("SELECT SUM(doc) FROM temp.nncq_vocab WHERE term >= ? AND term < ?", (term, term + "\uffff"))
    else:
        r = store.q("SELECT doc FROM temp.nncq_vocab WHERE term = ?", (term,))
    return int(r[0][0] or 0) if r else 0


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%" if x >= 0.1 else f"{x * 100:.1f}%" if x >= 0.001 else f"{x * 100:.2f}%"


# ------------------------------------------------------------------ per-hit facts

def _area(path: str) -> tuple[str, str]:
    parts = path.split("/")
    dirs = parts[:-1][:AREA_DEPTH]
    area = "/".join(dirs) or "."
    return area, path[len(area) + 1:] if dirs else path


def _sentence(lines: list[str]) -> str:
    text = " ".join(x.strip() for x in lines if x.strip())
    text = re.sub(r"\s+", " ", text).strip()
    m = _SENT.search(text, 20)
    if m:
        text = text[:m.start() + 1]
    return text[:220] + ("…" if len(text) > 220 else "")


def _comment_block(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        m = _COMMENT.match(ln)
        if not m:
            break
        body = ln[m.end():].strip().rstrip("*/").strip().strip('"').strip()
        if body and not _JUNK.search(body):
            out.append(body)
    return out


def _doc(store: Store, root: Path, it: dict, files: dict) -> str:
    lines = it["text"].replace("\r", "").split("\n")
    if it["kind"] in DOCS:
        body = [x for x in lines[1:] if x.strip() and not x.lstrip().startswith(("#", "```", "|", "<", "---"))]
        return _sentence(body[:4])
    head = _comment_block(lines)
    if not head and len(lines) > 1 and lines[1].strip().startswith(('"""', "'''")):
        head = _comment_block(lines[1:])
    if head:
        return _sentence(head[:6])

    def above(path: str, line: int) -> list[str]:
        if path not in files:
            try:
                files[path] = (root / path).read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                files[path] = []
        src = files[path]
        blk = []
        for i in range(line - 2, max(-1, line - 16), -1):
            if i >= len(src) or not _COMMENT.match(src[i]):
                break
            blk.insert(0, src[i])
        return _comment_block(blk)

    head = above(it["path"], it["a"])
    if not head and it["sym"]:
        for p, ln in store.q("SELECT f.path, s.start_line FROM symbols s JOIN files f ON f.id=s.file_id "
                             "WHERE s.qualname=? COLLATE NOCASE AND s.qualname=? AND s.file_id<>? LIMIT 2",
                             (it["sym"], it["sym"], it["fid"])):
            head = above(p, ln)
            if head:
                break
    return _sentence(head[:6]) if head else ""


def _peek(it: dict, terms: list[dict], n: int = 2) -> list[str]:
    lines = it["text"].replace("\r", "").split("\n")
    scored = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i == 0 or len(s) < 4 or _JUNK.search(s):
            continue
        low = s.lower()
        hit = sum(_matched(t, low) for t in terms)
        if hit:
            scored.append((-hit, i, s))
    best = sorted(scored)[:n]
    return [f"{it['a'] + i}: {s[:140]}" for _, i, s in sorted(best, key=lambda x: x[1])]


def _func_facts(store: Store, it: dict) -> None:
    rows = store.q("SELECT dst, MIN(line) FROM edges WHERE kind='call' AND src=? AND file_id=? GROUP BY dst "
                   "ORDER BY MIN(line)", (it["sym"], it["fid"]))
    it["_callees"] = {d: ln for d, ln in rows}
    names = [d for d, _ in rows if len(d) > 2 and not _MACRO.match(d)]
    if names:
        ph = ",".join("?" * len(names))
        common = {d for d, c in store.q(f"SELECT dst, COUNT(*) FROM edges WHERE kind='call' AND dst IN ({ph}) "
                                        f"GROUP BY dst", names) if c > UTILITY_CALLS}
        dom = [d for d in names if d not in common]
        hidden = len(rows) - len(dom)
        if dom:
            it["calls"] = ", ".join(dom[:8]) + (f" (+{len(dom) - 8})" if len(dom) > 8 else "") + \
                (f"; {hidden} plumbing calls hidden" if hidden else "")
    nm = graph.short(it["sym"])
    total, srcs = store.q("SELECT COUNT(*), COUNT(DISTINCT src) FROM edges WHERE kind='call' AND dst=?", (nm,))[0]
    if total:
        ndefs = store.q("SELECT COUNT(DISTINCT qualname) FROM symbols WHERE name=? COLLATE NOCASE AND name=?",
                        (nm, nm))[0][0]
        top = []
        if total <= 5000:
            cls = graph.norm(it["parent"] or "")
            rows = store.q("SELECT src, COUNT(*) n FROM edges WHERE kind='call' AND dst=? GROUP BY src "
                           "ORDER BY n DESC LIMIT 60", (nm,))
            rows.sort(key=lambda r: (not (cls and graph.norm(r[0] or "").startswith(cls + ".")), -r[1]))
            top = [graph.short(r[0] or "(file level)") if graph.norm(r[0] or "").startswith(cls + ".") and cls
                   else (r[0] or "(file level)") for r in rows[:3]]
        amb = f"; by name, {ndefs} definitions share it" if ndefs > 1 else ""
        it["callers"] = (", ".join(top) + (f" (+{srcs - len(top)})" if srcs > len(top) else "") if top
                         else f"{srcs} callers") + f" [{total} call sites{amb}]"


def _type_facts(store: Store, it: dict) -> None:
    bases = [d for (d,) in store.q("SELECT DISTINCT dst FROM edges WHERE kind='inherits' AND src=? LIMIT 6",
                                   (it["sym"],))]
    if bases:
        it["bases"] = ", ".join(bases)
    nder = store.q("SELECT COUNT(DISTINCT src) FROM edges WHERE kind='inherits' AND dst=?",
                   (graph.short(it["sym"]),))[0][0]
    if nder:
        it["derived"] = nder
    mem = store.q("SELECT kind, COUNT(*) FROM symbols WHERE parent=? GROUP BY kind", (it["sym"],))
    if mem:
        it["members"] = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in sorted(mem, key=lambda x: -x[1])[:4])


# ------------------------------------------------------------------ ask

def _strength(top: float, reranked: bool) -> str:
    if not reranked and config.settings().lite:
        return "unrated (lite: keyword ranking, no reranker): verify the top hits before relying on them"
    if not reranked:
        return "unknown (no reranker): order is keyword/meaning fusion only"
    if top >= 2:
        return f"strong (best rerank {top:+.1f})"
    if top >= -2:
        return f"fair (best rerank {top:+.1f})"
    return f"weak (best rerank {top:+.1f}): nothing clearly answers this; treat hits as leads, try code vocabulary"


def _led_to(store: Store, lst: list, n: int = 100) -> str:
    ids = [cid for cid, _ in lst[:n]]
    if not ids:
        return ""
    ph = ",".join("?" * len(ids))
    areas = Counter(_area(p)[0] for (p,) in store.q(
        f"SELECT f.path FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id IN ({ph})", ids))
    tot = sum(areas.values())
    return ", ".join(f"{a} {c * 100 // tot}%" for a, c in areas.most_common(3))


def ask(store: Store, root: Path, question: str, k: int = 8, path: str = "", lang: str = "",
        vec_cover: float | None = None) -> dict:
    t0 = time.time()
    terms, ignored, expr = _terms(question)
    c = search.candidates(store, question, k, path, lang, fts_expr=expr, pool=max(40, k * 4))
    cands = c["cands"]
    if not cands:
        return {"question": question, "hits": [], "note": "no matches", "interpreted": {"terms": [t["word"] for t in terms]}}
    lex_r = {cid: i + 1 for i, (cid, _) in enumerate(c["lex"])}
    den_r = {cid: i + 1 for i, (cid, _) in enumerate(c["dense"])}
    scores = c["scores"]

    items, by_key, by_text = [], {}, {}
    for cid, p, a, b, sym, kind, parent, text, lg, fid in cands:
        key = (p, sym) if sym and kind not in FILE_LEVEL else (p, a)
        h = hash(text.strip())
        if h in by_text:
            by_text[h].setdefault("also", []).append(p)
            continue
        if key in by_key:            # another window of the same symbol: count it, keep the best one's text/lines
            by_key[key]["windows"] = by_key[key].get("windows", 1) + 1
            continue
        it = {"cid": cid, "path": p, "a": a, "b": b, "sym": sym or "", "kind": kind, "parent": parent, "text": text,
              "lang": lg, "fid": fid, "score": scores.get(cid, 0.0), "lex": lex_r.get(cid), "dense": den_r.get(cid)}
        by_key[key] = by_text[h] = it
        items.append(it)
    shown = items[:k]

    files: dict[str, list[str]] = {}
    low_hay = {id(it): (it["text"] + " " + it["path"] + " " + it["sym"]).lower() for it in shown}
    for it in shown:
        it["doc"] = _doc(store, root, it, files)
        if it["kind"] in graph.FUNCS:
            _func_facts(store, it)
        elif it["kind"] not in DOCS and it["kind"] not in FILE_LEVEL and graph.is_type(it["kind"]):
            _type_facts(store, it)
        it["peek"] = _peek(it, terms)
        hay = low_hay[id(it)]
        it["matched"] = [t["word"] for t in terms if _matched(t, hay)]

    # ---- interpretation
    have_vocab = _vocab(store)
    ndocs = _total_docs(store) or 1
    if vec_cover is None:
        vec_cover = min(1.0, search.vectors(store).n / ndocs)
    tinfo = []
    for t in terms:
        s = t["word"]
        if t["prefix"]:
            s += f" -> also {t['prefix']}*"
        if t["parts"]:
            s += f" -> parts {' '.join(t['parts'])}"
        if have_vocab:
            df = _df(store, t["prefix"], True) if t["prefix"] else _df(store, t["exact"])
            s += f" · {'~' if t['prefix'] else ''}{_pct(df / ndocs)} of chunks" if df else " · not in the code vocabulary"
        s += f" · in {sum(t['word'] in it['matched'] for it in shown)}/{len(shown)} hits"
        tinfo.append(s)
    qwords = {n for t in terms for n in _needles(t)}
    vc = Counter()
    for it in items[:20]:
        vc.update({w for w in _IDENT.findall(it["text"]) if not _MACRO.match(w) and w.lower() not in _KW
                   and (any(ch.isupper() for ch in w[1:]) or "_" in w.strip("_"))})
    voc = []
    for w, n in vc.most_common(120):
        if n < 3 or w.lower() in qwords:
            continue
        df = _df(store, w.lower()) if have_vocab else 0
        voc.append((n * (1.0 / (1 + df / ndocs * 200)), w))
    vocab = [w for _, w in sorted(voc, reverse=True)[:8]]
    interp = {"terms": tinfo}
    if ignored:
        interp["ignored"] = ", ".join(ignored)
    if c["dense"]:
        interp["meaning"] = "embedded the whole question and compared it with code meaning" + \
            (f" ({_pct(vec_cover)} of chunks have vectors so far; the rest is found by keyword only)"
             if vec_cover is not None and vec_cover < 0.999 else "")
    elif config.settings().lite:
        interp["meaning"] = "off (lite mode: keywords + graph; use the code's words, see code_vocabulary)"
    else:
        interp["meaning"] = "not used: " + "; ".join(c["notes"] or ["no vectors"])
    if vocab:
        interp["code_vocabulary"] = ", ".join(vocab)

    # ---- portfolio
    areas: dict[str, list[dict]] = {}
    for it in shown:
        areas.setdefault(_area(it["path"])[0], []).append(it)
    kinds = Counter("doc" if it["kind"] in DOCS else "file-level" if it["kind"] in FILE_LEVEL else it["kind"]
                    for it in shown)
    both = sum(1 for it in shown if it["lex"] and it["dense"])
    lex_only = sum(1 for it in shown if it["lex"] and not it["dense"])
    den_only = sum(1 for it in shown if it["dense"] and not it["lex"])
    top = max((it["score"] for it in shown), default=0.0)
    port = {"shown": f"{len(shown)} of {len(items)} distinct candidates ({len(cands)} chunks "
                     f"{'reranked' if c.get('reranked') else 'ranked'})",
            "areas": [f"{a}: {len(v)} ({', '.join('c' + str(x['cid']) for x in v)})"
                      for a, v in sorted(areas.items(), key=lambda kv: -len(kv[1]))],
            "kinds": ", ".join(f"{n} {k}" for k, n in kinds.most_common()),
            "found_by": f"keyword+meaning {both}, meaning only {den_only}, keyword only {lex_only}",
            "strength": _strength(top, c.get("reranked", False))}
    if c["lex"]:
        port["keyword_led_to"] = _led_to(store, c["lex"])
    if c["dense"]:
        port["meaning_led_to"] = _led_to(store, c["dense"])

    # ---- links between hits
    links = []
    funcs = [it for it in shown if "_callees" in it]
    by_short: dict[str, list[dict]] = {}
    for it in shown:
        if it["sym"] and it["kind"] not in DOCS | FILE_LEVEL:
            by_short.setdefault(graph.short(it["sym"]), []).append(it)
    lab = lambda it: f"c{it['cid']} {graph.short(it['sym'])}"
    for A in funcs:
        for dst, ln in A["_callees"].items():
            for B in by_short.get(dst, []):
                if B is not A:
                    links.append(f"{lab(A)} calls {lab(B)} (line {ln})")
    for B in funcs:
        callers_b = {src for (src,) in store.q("SELECT DISTINCT src FROM edges WHERE kind='call' AND dst=? LIMIT 400",
                                                (graph.short(B["sym"]),)) if src}
        for A in funcs:
            if A is B or any(lab(A) + " calls " + lab(B) in x for x in links):
                continue
            via = [s for s in callers_b if graph.short(s) in A["_callees"] and s != A["sym"]]
            if via:
                links.append(f"{lab(A)} -> {via[0]} -> {lab(B)} (one hop, by name)")
    par = Counter(it["parent"] for it in shown if it["parent"] and it["kind"] in graph.FUNCS)
    for p, n in par.items():
        if n > 1:
            links.append(f"{', '.join('c' + str(it['cid']) for it in shown if it['parent'] == p)} are members of {p}")
    for it in shown:
        for b in (it.get("bases") or "").split(", "):
            for B in by_short.get(graph.short(b), []) if b else []:
                links.append(f"{lab(it)} derives from {lab(B)}")

    # ---- hits
    hits = []
    for rank, it in enumerate(shown, 1):
        area, rel = _area(it["path"])
        why = []
        if it["lex"]:
            why.append(f"keyword #{it['lex']}")
        if it["dense"]:
            why.append(f"meaning #{it['dense']}")
        if c.get("reranked"):
            why.append(f"rerank {it['score']:+.1f}")
        if it["matched"]:
            why.append("terms: " + " ".join(it["matched"]))
        h = {"ref": f"c{it['cid']}", "rank": rank,
             "sym": it["sym"] if it["kind"] not in FILE_LEVEL else f"(file level of {Path(it['path']).name})",
             "kind": it["kind"], "at": f"{area} / {rel}:{it['a']}-{it['b']}"}
        ext = _extent(store, it["fid"], it["sym"], it["a"], it["b"]) if it["kind"] not in FILE_LEVEL else None
        if ext and ext[0] and ext[0] != it["sym"]:
            h["sig"] = re.sub(r"\s+", " ", ext[0])[:180]
        if ext and (ext[1] < it["a"] - 3 or ext[2] > it["b"]):
            h["window"] = f"part of {graph.short(it['sym'])} {ext[1]}-{ext[2]}" + \
                (f"; {it['windows']} windows matched" if it.get("windows", 1) > 1 else "")
        for f in ("doc", "calls", "callers", "bases", "derived", "members", "peek"):
            if it.get(f):
                h[f] = it[f]
        h["why"] = " · ".join(why)
        if it.get("also"):
            h["also_at"] = it["also"][:3]
        hits.append(h)

    nxt = [f"expand(refs='{','.join(h['ref'] for h in hits[:3])}') for exact code; what='context'|'callers'|"
           f"'callees'|'outline' for structure"]
    best_fn = next((it for it in shown if it["kind"] in graph.FUNCS), None)
    if best_fn:
        nxt.append(f"call_trace(to='{best_fn['sym']}') for how execution reaches it")
    if top < -2 and vocab:
        nxt.append(f"ask again using the code's words, e.g. '{' '.join(vocab[:3])}'")
    if areas and len(areas) > 2:
        big = max(areas.items(), key=lambda kv: len(kv[1]))
        if len(big[1]) >= 2:
            nxt.append(f"narrow: ask(..., path='{big[0]}')")

    out = {"question": question, "interpreted": interp, "portfolio": port, "hits": hits}
    if links:
        out["links"] = list(dict.fromkeys(links))[:12]
    out["next"] = nxt
    if c["notes"]:
        out["note"] = "; ".join(c["notes"])
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def _extent(store: Store, fid: int, sym: str, a: int, b: int) -> tuple[str, int, int] | None:
    """(signature, start, end) of the definition a chunk belongs to (a chunk may be one window of a long one)."""
    if not sym:
        return None
    r = store.q("SELECT signature, start_line, end_line FROM symbols WHERE file_id=? AND qualname=? "
                "AND start_line<=? AND end_line>=? ORDER BY end_line-start_line LIMIT 1", (fid, sym, b, a))
    return r[0] if r else None


# ------------------------------------------------------------------ expand

def _resolve(store: Store, ref: str) -> dict | None:
    ref = ref.strip()
    m = re.fullmatch(r"c(\d+)", ref)
    if m:
        r = store.q("SELECT f.path, c.start_line, c.end_line, c.symbol, c.kind, c.file_id FROM chunks c JOIN files f "
                    "ON f.id=c.file_id WHERE c.id=?", (int(m.group(1)),))
        if not r:
            return None
        loc = dict(zip(("path", "a", "b", "sym", "kind"), r[0][:5]))
        ext = _extent(store, r[0][5], loc["sym"], loc["a"], loc["b"]) if loc["kind"] not in FILE_LEVEL else None
        if ext and (ext[1] < loc["a"] or ext[2] > loc["b"]):
            loc["ext"] = (min(ext[1], loc["a"]), max(ext[2], loc["b"]))
        return loc
    m = re.fullmatch(r"(.+?):(\d+)(?:-(\d+))?", ref)
    if m and "/" in ref:
        a = int(m.group(2))
        loc = {"path": m.group(1).replace("\\", "/"), "a": a, "b": int(m.group(3) or a + 40), "sym": "", "kind": ""}
        r = store.q("SELECT s.qualname, s.kind, s.start_line, s.end_line FROM symbols s JOIN files f ON f.id=s.file_id "
                    "WHERE f.path=? AND s.start_line<=? AND s.end_line>=? AND s.kind NOT IN ('namespace', 'module') "
                    "ORDER BY s.end_line - s.start_line LIMIT 1", (loc["path"], a, a))
        if r:       # name the enclosing symbol, so context/callers/callees work on a line range too
            loc.update(sym=r[0][0], kind=r[0][1], span=(r[0][2], r[0][3]))
        return loc
    d = graph.find_defs(store, ref)
    if d:
        return {"path": d[0].path, "a": d[0].start, "b": d[0].end, "sym": d[0].qualname, "kind": d[0].kind}
    return None


def expand(repo: Repo, store: Store, refs: str, what: str = "code", max_lines: int = 160) -> dict:
    out = []
    for ref in [r for r in re.split(r"[,\s]+", refs) if r][:6]:
        loc = _resolve(store, ref)
        if not loc:
            out.append({"ref": ref, "error": "unknown ref (re-run ask: the file may have been re-indexed)"})
            continue
        ext = loc.get("ext")
        if what == "code" and ext and ext[1] - ext[0] < max_lines:   # a window of a short symbol: show all of it
            loc["a"], loc["b"], ext = ext[0], ext[1], None
        e = {"ref": ref, "sym": loc["sym"], "kind": loc["kind"], "at": f"{loc['path']}:{loc['a']}-{loc['b']}"}
        if what == "code":
            end = min(loc["b"], loc["a"] + max_lines - 1)
            rc = search.read_code(repo, loc["path"], loc["a"], end)
            e["code"] = rc.get("code") or rc.get("error")
            last = max(loc["b"], ext[1]) if ext else loc["b"]
            span = loc.get("span")
            if span and not ext and (span[0] < loc["a"] or span[1] > loc["b"]):
                e["window"] = f"inside {graph.short(loc['sym'])} {span[0]}-{span[1]}"
            if ext:
                e["window"] = f"part of {graph.short(loc['sym'])} {ext[0]}-{ext[1]}"
                if ext[0] < loc["a"]:
                    e["before"] = f"read_code(path='{loc['path']}', start={ext[0]}, end={loc['a'] - 1})"
            if end < last:
                e["more"] = f"read_code(path='{loc['path']}', start={end + 1}, end={last})"
        elif what == "context":
            e.update(graph.symbol_context(store, loc["sym"], loc["path"]))
        elif what == "callers":
            e.update(graph.called_by(store, loc["sym"], loc["path"], 40))
        elif what == "callees":
            e.update(graph.calls(store, loc["sym"], loc["path"]))
        elif what == "outline":
            e.update(search.file_outline(store, loc["path"]))
        else:
            e["error"] = "what: code | context | callers | callees | outline"
        out.append(e)
    return {"what": what, "items": out}
