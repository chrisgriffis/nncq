"""UML / SysML views over the index: class, object, component, package, deployment and sequence diagrams.

  schema.py          tables + migration (symbol/edge columns, components, views and their staleness refs)
  extract.py         per-file UML facts on the chunker's tree-sitter walk (members, visibility, types, relations)
  buildinfo.py       build files -> components, artifacts, link deps, build configurations (CMake, Python)
  model.py           repo-level model pass + the queries every view is made of
  views.py           auto views generated from the model (one SQL-backed function per kind)
  protocol.py        agent-authored views: JSON schema, validation, binding to the index
  catalog.py         stored views: upsert / list / by symbol / delete, staleness, render cache
  render_mermaid.py  Mermaid text (classDiagram, sequenceDiagram, flowchart for SysML/package/deployment)
  render_ascii.py    plain-text diagrams for an agent's prompt window
  tools.py           MCP tools;  web.py: the dashboard's Views page;  vendor.py: pinned local Mermaid build
"""
from __future__ import annotations

FACTS_VERSION = "2"     # bump when extract.py changes what it records: files are re-parsed once
