---
name: nncq-user
description: Use when nncq MCP tools (ask, expand, call_trace, called_by, impact, data_for_code, ...) are available and the user asks how code works, what calls what, what breaks if X changes, or how code and data files relate.
---

# nncq: investigate quietly, answer plainly

Answers are small, cached and deterministic. Make many narrow calls quietly, then answer. Never read whole files.

## Loop
1. Split the question into 2-5 sub-questions, one concept each.
2. `ask` each, in parallel. Add `path=` once you know the area.
3. Check `portfolio.strength`. `weak`: re-ask using `interpreted.code_vocabulary`, not a bigger `k`. `unrated (lite)`: keyword-only ranking, verify more.
4. Connect the best symbols: `call_trace(to, frm)`, `called_by`, `calls`, `symbol_context`, `impact`, `type_hierarchy`.
5. `expand(refs)` only the 2-4 hits your conclusion rests on.
6. Stop when new calls stop changing the answer.

## Tool choice
- Exact name: `find_symbol`, `symbol_context`. Exact string or regex: `grep_code`. Everything else starts with `ask`.
- Structure without code: `expand(refs, what=context|callers|callees|outline)`, `file_context`.
- Code <-> data files (only when the workspace profile has data rules): `data_for_code`, `code_for_data`, `data_file`, `hot_data`.
- Diagrams: `uml_view(kind, subject)` (ascii in chat, markdown for files). Composing views / compositions: `uml_protocol`.
- `more(cursor)` only if the trimmed part matters.

## Graph caveats
- Links are by name. `N defs` or "ambiguous" means the edge may point elsewhere: `expand` before asserting it.
- Jobs, callbacks, virtuals and script bindings break traces. Don't invent the missing hop.
- `(test)` callers are not the production path. "Uncalled" is a candidate, not proof of dead code.
- `index_state` present: semantic ranking is stale there; keyword and graph results still hold.

## Answering
- Tool use is invisible. Don't announce it, narrate it, or mention nncq. Answer like someone who already knows the code.
- Lead with the answer: the mechanism, in plain words, as short as the question allows.
- Name a file or function only where it helps the user act or look; `path:line`, never `c123` refs.
- No chains, tables or evidence dumps unless asked. Offer more depth in one line instead.
- Mention a gap only if it changes the answer (e.g. a trail that goes cold at a callback).
