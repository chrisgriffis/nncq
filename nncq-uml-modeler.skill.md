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
`uml_view(kind, subject, under, depth, path, format)`. kind: class | object | component | package | deployment | sequence. subject: type | component | entry function. `path` = a file: class -> the types it declares or defines members of; component -> its build target(s). Stored and reused; regenerated when its sources change.
- No kind = the default diagram for a target: type -> class view, function -> sequence, folder (`path`) -> packages, nothing -> component overview, any file (`path`) -> its **file composition**.
- File composition: key `composition:file:<path>` (also `uml_view(key="file:<path>")`), origin auto, made on demand only. Groups: per namespace types (with members) / functions (+ key calls) / globals, related types, includes, included-by, component, the file's sub-diagrams. Rebuilt when stale (parts first); never hand-edit it: to customize, upsert a copy under your own id. ASCII refuses it (use markdown).

## Author
`uml_upsert(spec)`. Same `key` (view) / `id` (composition) = replace.
```json
{"id":"device-access","name":"Device access","members":[
  {"id":"lib","ref":"comp:DeviceLib"},{"id":"api","ref":"sym:ns::DeviceApi"},
  {"id":"seq","ref":"view:connect-flow"},
  {"id":"hw","bespoke":{"type":"block","label":"Hardware"}}],
 "connectors":[{"from":"lib","to":"hw","kind":"flows-to","label":"usb"}],
 "groups":[{"id":"core","label":"Core","members":["lib","api"]}]}
```
Composition `groups` draw sub-frames (optional `parent` = enclosing group id).
View: `{"key","kind","title","nodes":[{"id","ref"|"label","members":"auto"|"none","expand"}],"edges":[{"from","to","kind","label"}],"auto_edges":true}`.
Node/member ids: `[A-Za-z_]\w*`, unique per object. Keys/ids: `[A-Za-z0-9][\w.-]*`, no `:`.

## Rules
- List/find before creating. Upsert by key/id; never duplicate.
- Prefer `ref` to generated elements. Bespoke only for what the index lacks; always labeled, minimal.
- After upsert: fix `unbound` refs; `unverified_edges` = not in the index (ok for intent, not for facts).
- Stale: `report.staleness[]` per member: changed | missing | moved (`now_at`). Members are kept; repair by re-upsert with corrected refs. `stale: pending` re-checks on next read.
- Output: `ascii` in chat (plain views only, capped); `markdown` for files (pure mermaid block + element links to the dashboard, revisioned; outdated links are flagged by the page); `json` for edits.
- No `uml_build` unless a tool says the model is not built.
