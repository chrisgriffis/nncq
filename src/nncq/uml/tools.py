"""MCP tools for UML views and compositions, registered by server.build() (register()). Read tools are cached by
answers.py like every other tool; writes (upsert / delete / build) bump the cache."""
from __future__ import annotations

from mcp.server.fastmcp import Context

from nncq import answers, indexer
from nncq.uml import catalog, model, protocol

READ_TOOLS = {"uml_protocol", "uml_find", "uml_view", "uml_views"}
WEB_TOOLS = READ_TOOLS | {"uml_build", "uml_delete"}

INSTRUCTIONS = """UML: uml_view(kind=class|object|component|package|deployment|sequence, subject) draws a diagram
from the index (ascii for chat, markdown for files); uml_views lists stored diagrams (also by symbol); compose your
own diagrams and compositions with uml_upsert (schema + rules: uml_protocol); uml_find gives element ids."""


def _ready(st) -> dict | None:
    if not model.facts_ready(st) or not st.meta("uml_built_at"):
        return {"error": "UML model not built for this index", "fix": "call uml_build (CPU, minutes), then retry",
                **model.state(st)}
    return None


def register(mcp, run, with_, job, owned) -> None:
    """run / with_ / job / owned: server._run, server._with, server._job, the ownership check."""

    @mcp.tool()
    async def uml_protocol() -> dict:
        """How to compose diagrams and compositions: element id scheme, rules, JSON schema, examples. Authoritative:
        read it before uml_upsert."""
        return await run(protocol.protocol)

    @mcp.tool()
    async def uml_find(name: str, repo: str = "") -> dict:
        """Element ids (sym:/comp:/port:/pkg:/cfg:/composition:/view:) for a name, to reference in uml_upsert."""
        return await run(with_, repo, lambda r, st: _ready(st) or catalog.find(st, name))

    @mcp.tool()
    async def uml_view(kind: str = "", subject: str = "", key: str = "", format: str = "ascii", depth: int = 0,
                       under: str = "", path: str = "", repo: str = "") -> dict:
        """A diagram. Generated from the index: kind = class | object | component | package | deployment | sequence |
        depends (dependency graph: subject = a type, or path = a file for its include / import graph),
        subject = type / component / entry function name, under = folder filter, depth = relation hops (class,
        sequence: call depth; package: folder depth), path = a file (class: the types declared in it; component:
        the components it defines or belongs to). No kind (or kind=default): the best diagram for a subject and / or
        path (type -> class, function -> sequence, file -> the classes it declares or defines members of, else its
        component; folder -> packages; nothing -> overview), generated on demand, stale ones regenerated.
        Stored: key = a view key or composition:<id>.
        format: ascii (chat) | markdown (```mermaid + element links) | mermaid | json (model with source spans).
        Stale diagrams carry report.staleness (changed / missing / moved members)."""
        opts = {k: v for k, v in (("depth", depth), ("under", under), ("path", path)) if v}
        return await run(with_, repo, lambda r, st: _ready(st) or catalog.view(st, kind, subject, key, format, **opts))

    @mcp.tool()
    async def uml_views(group_by: str = "kind", symbol: str = "", kind: str = "", origin: str = "",
                        repo: str = "") -> dict:
        """Stored diagrams and compositions, grouped by kind | group (component / folder) | origin (auto, agent,
        user). symbol: only those containing that symbol / component / composition."""
        return await run(with_, repo, lambda r, st: catalog.list_views(st, group_by, symbol, kind, origin))

    @mcp.tool()
    async def uml_upsert(spec: dict | str, repo: str = "", model: str = "", ctx: Context = None) -> dict:
        """Create or replace a diagram (key) or composition (id) from JSON per uml_protocol. Returns its key,
        unbound refs and edges the index does not confirm. model: pass your model id (e.g. claude-opus-5.5):
        recorded with the MCP client as the item's provenance."""
        client = _client(ctx)

        def f(r, st):
            out = catalog.upsert(st, spec, client, model)
            answers.bump()
            return out
        return await run(with_, repo, f)

    @mcp.tool()
    async def uml_delete(key: str, repo: str = "") -> dict:
        """Delete a stored diagram or composition (composition:<id>). Diagrams referencing it get flagged."""
        def f(r, st):
            out = catalog.delete(st, key)
            answers.bump()
            return out
        return await run(with_, repo, f)

    @mcp.tool()
    async def uml_build(repo: str = "") -> dict:
        """Build or refresh the UML model (members, relations, components, build configurations) in the background:
        a one-time CPU re-parse when the index predates it. Poll index_status (uml)."""
        def f():
            from nncq import repos
            if (o := owned()) is not None:
                return o
            try:
                r = repos.get(repo)
            except KeyError as e:
                return {"error": str(e).strip("'\"")}
            return {"repo": r.name, **job(r, "uml build", lambda log: build(r, log))}
        return await run(f)


def _client(ctx) -> str:
    """The MCP client as name/version: from the session's initialize request, else the HTTP User-Agent (stateless
    HTTP has no session), else ''."""
    try:
        ci = ctx.session.client_params.clientInfo
        if ci and ci.name:
            return ci.name + (f"/{ci.version}" if ci.version else "")
    except Exception:
        pass
    try:
        req = ctx.request_context.request
        ua = req.headers.get("user-agent", "") if req is not None else ""
        return ua.split()[0] if ua else ""
    except Exception:
        return ""


def build(r, log=print) -> dict:
    """Turn UML on for this index and bring it up to date (re-parse once if the facts are missing)."""
    st = indexer.open_store(r)
    try:
        st.set_meta("uml", "1")
    finally:
        st.close()
    res = indexer.update(r, log=log)
    st = indexer.open_store(r)
    try:
        if "uml" not in res and not res.get("skipped"):
            res["uml"] = model.build(st, r.root, log)
        res["state"] = model.state(st)
    finally:
        st.close()
    return res
