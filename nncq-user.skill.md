---
name: nncq-user
description: Use when nncq MCP tools (ask, expand, call_trace, called_by, impact, data_for_code, ...) are available and the user asks how code works, what calls what, what breaks if X changes, or how code and engine data relate.
---

# nncq: investigate, then compile

Answers are small, cached and deterministic. Make many narrow calls, then write one digest. Never paste tool output; never read whole files.

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
- engine: `data_for_code`, `code_for_data`, `asset_info`.
- `more(cursor)` only if the trimmed part matters.

## Graph caveats
- Links are by name. `N defs` or "ambiguous" means the edge may point elsewhere: `expand` before asserting it.
- Jobs, callbacks, virtuals and script bindings break traces. Report the gap; don't invent the hop.
- `(test)` callers are not the production path. "Uncalled" is a candidate, not proof of dead code.
- `index_state` present: semantic ranking is stale there; keyword and graph results still hold.

## Digest
1. Answer: the mechanism in 2-5 sentences.
2. Chain: `file:line Symbol -> ...`; mark unverified hops `(inferred)`.
3. Key locations: 3-8 lines of `path:line Symbol - role`.
4. Gaps: name-only links, cold trails, weak matches, `index_state`.

Cite `path:line`, never `c123` refs.
