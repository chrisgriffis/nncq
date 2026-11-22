"""`nncq uml ...`: build the model, draw / list / export / upsert / delete diagrams and compositions."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    p = sub.add_parser("uml", help="UML / SysML diagrams: init (build the model), view, list, show, upsert, delete")
    p.add_argument("action", choices=["init", "view", "list", "show", "upsert", "delete", "find", "protocol"])
    p.add_argument("args", nargs="*", help="view: KIND [SUBJECT] | show/delete: KEY | upsert: FILE.json | find: NAME")
    p.add_argument("--repo", default="")
    p.add_argument("--format", default="ascii", choices=["ascii", "markdown", "mermaid", "json"])
    p.add_argument("--under", default="", help="folder filter")
    p.add_argument("--depth", type=int, default=0)
    p.add_argument("--path", default="", help="sequence: which definition of the entry function")
    p.add_argument("--group", default="kind", help="list: kind | group | origin")
    p.add_argument("--symbol", default="", help="list: only views containing this symbol")
    p.add_argument("-o", "--out", default="", help="write the result to this file instead of stdout")
    p.add_argument("--no-vendor", action="store_true", help="init: don't fetch the pinned Mermaid build")
    p.set_defaults(fn=run)


def _emit(a, text: str) -> None:
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {a.out}")
    else:
        print(text)


def run(a) -> int:
    from nncq import repos
    from nncq.store import Store
    from nncq.uml import catalog, protocol, tools, vendor
    try:
        r = repos.get(a.repo)
    except KeyError as e:
        raise SystemExit(str(e).strip("'\""))
    if a.action == "protocol":
        _emit(a, json.dumps(protocol.protocol(), indent=1))
        return 0
    if a.action == "init":
        res = tools.build(r, log=lambda m: print(m, flush=True))
        print(json.dumps(res.get("state", res), indent=1, default=str))
        if not a.no_vendor:
            vendor.fetch()
        return 0
    if not r.db.exists():
        raise SystemExit(f"repo '{r.name}' is not indexed yet: nncq index <path>")
    st = Store(r.db, cache_mb=64)
    try:
        if a.action == "list":
            out = catalog.list_views(st, a.group, a.symbol)
        elif a.action == "find":
            out = catalog.find(st, " ".join(a.args))
        elif a.action == "upsert":
            if not a.args:
                raise SystemExit("usage: nncq uml upsert FILE.json  ('-' = stdin)")
            src = sys.stdin.read() if a.args[0] == "-" else Path(a.args[0]).read_text(encoding="utf-8")
            out = catalog.upsert(st, src)
        elif a.action == "delete":
            out = catalog.delete(st, a.args[0] if a.args else "")
        else:
            err = tools._ready(st)
            if err and a.action == "view":
                raise SystemExit(f"{err['error']}: run `nncq uml init`")
            if a.action == "show":
                out = catalog.get(st, a.args[0] if a.args else "", a.format)
            else:
                if not a.args:
                    raise SystemExit("usage: nncq uml view KIND [SUBJECT]   (class|object|component|package|"
                                     "deployment|sequence)")
                opts = {k: v for k, v in (("depth", a.depth), ("under", a.under), ("path", a.path)) if v}
                try:
                    out = catalog.view(st, a.args[0], " ".join(a.args[1:]), "", a.format, **opts)
                except ValueError as e:
                    out = {"error": str(e)}
            for f in ("diagram", "markdown", "mermaid"):
                if f in out:
                    _emit(a, out[f])
                    rest = {k: v for k, v in out.items() if k != f}
                    print(json.dumps(rest, indent=1, ensure_ascii=False, default=str), file=sys.stderr)
                    return 0
    finally:
        st.close()
    _emit(a, json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 1 if isinstance(out, dict) and "error" in out else 0
