---
name: nncq-uml-modeler
description: Use when nncq uml_* MCP tools are available and the user wants a diagram (class, object, component, package, deployment, sequence, depends) or to model / group parts of the code (compositions).
---

# nncq UML modeler

Authoritative schema, element-id scheme, rules, examples: call `uml_protocol`. This file is a summary.

## Kinds
- view = diagram (presentation). Generated (`origin: auto`) or authored (`agent` / `user`).
- composition = semantic model element: members + connectors (+ groups). Not a diagram. Element id `composition:<id>`.
- Either may reference the other. Reference cycles are rejected.

## Discover
- `uml_views(group_by=kind|group|origin)`; `uml_views(symbol=X)`: stored views / compositions containing X. Check before generating or authoring.
- `uml_find(name)`: element ids. Schemes: `sym:<qualname>[@path]` `comp:` `port:<comp>/<file>|<comp>/needs <comp>` `pkg:` `file:` `cfg:` `composition:` `view:`.
- `uml_view(key=..., format=json)`: model with stable ids, source spans, absolute paths.

## Generate
`uml_view(kind, subject, under, depth, path, format)`. Stored as auto views; regenerated when their sources change.
- class: members, nested types (`contains` / `nested`), inheritance / realization, composes / aggregates / associates (role, multiplicity), dependencies. `path` = a file: the types it declares or defines members of.
- object: globals, singletons, members holding instances, creation sites.
- component: SysML blocks with pins (provided = files others include, required = `needs <comp>`), connectors. `path` = a build file or source: its target(s).
- package: folders + include / import dependencies. deployment: executables, libraries, scripts, link deps, configurations (Debug / Release, presets), toolchains, options.
- sequence: calls from an entry function in source order, receivers typed through members / locals / qualifiers; lifelines and messages link to their elements. `depth` = call depth.
- depends: `subject` = type (type dependencies) or `path` = file (includes / imports), both directions, cycles dashed.
- No kind (or `default`): best diagram for `subject` / `path`: type -> class, function -> sequence, file -> file composition, folder -> packages, nothing -> components.
- File composition: key `composition:file:<path>`, origin auto, on demand only: per-namespace types (members) / functions (+ key calls) / globals, related types, includes, included-by, component, the file's sub-diagrams. Rebuilt when stale (parts first). Never hand-edit; to customize, upsert a copy under your own id.

## Author
`uml_upsert(spec, model=<your model id>)`. Provenance = MCP client + model + time: the dashboard shows an `mcp-agent` badge (details on hover), exports end with a footer. Same `key` (view) / `id` (composition) = replace.
```json
{"id":"ground-segment","name":"Ground segment","members":[
  {"id":"tlm","ref":"comp:orbit_telemetry"},{"id":"pipe","ref":"sym:orbitkit::telemetry::Pipeline"},
  {"id":"flow","ref":"view:downlink-path"},
  {"id":"sat","bespoke":{"type":"block","label":"Satellite"}}],
 "connectors":[{"from":"sat","to":"tlm","kind":"flows-to","label":"UHF downlink"}],
 "groups":[{"id":"station","label":"Station computer","members":["tlm","pipe"]}]}
```
`groups` draw sub-frames (optional `parent` = enclosing group id).
View: `{"key","kind","title","nodes":[{"id","ref"|"label","members":"auto"|"none","expand"}],"edges":[{"from","to","kind","label"}],"auto_edges":true}`.
Node / member ids: `[A-Za-z_]\w*`, unique per object. Keys / ids: `[A-Za-z0-9][\w.-]*`, no `:`.

## Rules
- List / find before creating. Upsert by key / id; never duplicate.
- Prefer `ref` to generated elements. Bespoke only for what the index lacks; always labeled, minimal.
- After upsert: fix `unbound` refs; `unverified_edges` = not in the index (ok for intent, not for facts).
- Stale: `report.staleness[]` per member: changed | missing | moved (`now_at`). Members are kept; repair by re-upsert with corrected refs. `stale: pending` re-checks on next read.
- `uml_delete(key)`: an auto view comes back on the next request; an agent / user item is gone for good, and items referencing it are flagged (`referenced_by_now_flagged`).
- Output: `ascii` in chat (plain views only, capped); `markdown` for files / PRs (pure mermaid block + element links to the dashboard, revisioned; the page flags outdated links); `json` for edits.
- No `uml_build` unless a tool says the model is not built.
