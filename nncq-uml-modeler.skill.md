---
name: nncq-uml-modeler
description: Use when nncq uml_* MCP tools are available and the user wants a diagram (class, object, component, package, deployment, sequence) or to model/group parts of the code (compositions).
---

# nncq UML modeler

Authoritative schema, element-id scheme, rules, examples: call `uml_protocol`. This file is a summary.

## Kinds
- view = diagram (presentation). Generated (`origin: auto`) or authored (`agent`/`user`).
- composition = semantic model element: members + connectors. Not a diagram. Element id `composition:<id>`.
- Either may reference the other. Cycles rejected.

## Discover
- `uml_views(group_by=kind|group|origin)`; `uml_views(symbol=X)`: stored views/compositions containing X.
- `uml_find(name)`: element ids. Schemes: `sym:<qualname>[@path]` `comp:` `port:<comp>/<file>|<comp>/needs <comp>` `pkg:` `file:` `cfg:` `composition:` `view:`.
- `uml_view(key=..., format=json)`: model with stable ids, source spans, absolute paths.

## Generate
`uml_view(kind, subject, under, depth, format)`. kind: class | object | component | package | deployment | sequence. subject: type | component | entry function. Stored and reused; regenerated when its sources change.

## Author
`uml_upsert(spec)`. Same `key` (view) / `id` (composition) = replace.
```json
{"id":"device-access","name":"Device access","members":[
  {"id":"lib","ref":"comp:DeviceLib"},{"id":"api","ref":"sym:ns::DeviceApi"},
  {"id":"seq","ref":"view:connect-flow"},
  {"id":"hw","bespoke":{"type":"block","label":"Hardware"}}],
 "connectors":[{"from":"lib","to":"hw","kind":"flows-to","label":"usb"}]}
```
View: `{"key","kind","title","nodes":[{"id","ref"|"label","members":"auto"|"none","expand"}],"edges":[{"from","to","kind","label"}],"auto_edges":true}`.
Node/member ids: `[A-Za-z_]\w*`, unique per object. Keys/ids: `[A-Za-z0-9][\w.-]*`, no `:`.

## Rules
- List/find before creating. Upsert by key/id; never duplicate.
- Prefer `ref` to generated elements. Bespoke only for what the index lacks; always labeled, minimal.
- After upsert: fix `unbound` refs; `unverified_edges` = not in the index (ok for intent, not for facts).
- Stale: `report.staleness[]` per member: changed | missing | moved (`now_at`). Members are kept; repair by re-upsert with corrected refs. `stale: pending` re-checks on next read.
- Output: `ascii` in chat (plain views only, capped); `markdown` for files (pure mermaid block + element links to the dashboard, revisioned; outdated links are flagged by the page); `json` for edits.
- No `uml_build` unless a tool says the model is not built.
