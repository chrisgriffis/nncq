---
name: nncq-user
description: How to investigate a codebase through the nncq MCP server (tools ask, expand, call_trace, symbol_context, called_by, impact, data_for_code, ...). Use whenever nncq tools are available and the user asks how code works, where something is decided, what calls what, what breaks if X changes, or how code and engine data relate. Turns one user question into several cheap nncq queries, verifies the load-bearing leads, and returns one compiled digest instead of raw tool output.
---

# nncq: investigate, then compile

nncq is a local index (BM25 + embeddings + reranker + a name-based symbol graph). It is built to be
asked MANY SMALL QUESTIONS. Each answer is a few KB, deterministic (no LLM runs on the server), and
cached until the index changes, so repeats and overlapping asks are nearly free.

The user does not want tool output. They want a digest you compiled from an investigation:

    one user question -> decompose -> 3-10 nncq calls (parallel where independent)
                      -> verify the few facts the answer rests on -> one written answer

If you answer from a single `ask`, or if you fall back to reading whole files, you are using it wrong:
the first wastes the index, the second wastes your context.

---

## 1. The loop

1. **Decompose.** Split the user question into 2-5 sub-questions that each name ONE concept:
   entry point, decision point, data structure, consumer, failure path. "How does streaming throttle
   under memory pressure" -> (a) where memory pressure is measured, (b) where streaming requests are
   scheduled/dropped, (c) what connects them.
2. **Survey.** One `ask` per sub-question, issued in parallel. Default `k` (8) is fine; use `path=`
   as soon as you know the area. Do not start with `grep_code` or `search_code`.
3. **Triage each answer** (section 3). Weak match -> re-ask in the code's own words, do not just raise `k`.
4. **Connect.** Use the graph on the 1-3 best symbols: `symbol_context`, `call_trace(to, frm)`,
   `called_by`, `calls`, `type_hierarchy`, `impact`. This is where "how does X work" is usually
   answered: the hits form a chain.
5. **Verify.** `expand(refs)` ONLY for the hits your conclusion depends on (typically 2-4). Read the
   code, confirm the claim. The map is a lead sheet, not evidence.
6. **Stop** when every claim in your answer is either verified code or explicitly flagged as inferred.
   Do not keep searching to be thorough; stop when new calls stop changing the answer.
7. **Compile** the digest (section 6).

Rough budget for a "how does X work" question: 2-4 `ask`, 2-4 graph calls, 1-2 `expand`. For a
"what breaks if I change X": `symbol_context` + `impact` + `called_by` + 1 `expand`. For "where is Y":
often 1 `ask` or `find_symbol` is enough; do not inflate simple lookups.

---

## 2. Which tool, when

| Need | Tool | Not |
|---|---|---|
| Any conceptual question, first move | `ask` | `search_code`, `grep_code` |
| Exact code for hits you will cite | `expand(refs='c12,c40')` | `read_code` on whole files |
| Structure of a hit | `expand(refs, what='context'\|'callers'\|'callees'\|'outline')` | reading the file |
| Known exact identifier | `find_symbol`, `symbol_context` | `ask` |
| Known exact string / regex / log message | `grep_code` | `ask` |
| "How does execution get here" / "how are A and B related" | `call_trace(to='B', frm='A')` | chained `called_by` by hand |
| Blast radius before a signature change | `impact`, then `called_by` | `find_references` alone |
| What a file is / who includes it | `file_context`, `file_outline` | `read_code` |
| Code <-> engine data | `data_for_code`, `code_for_data`, `asset_info` | grepping `.xdata` |
| Trimmed answer (`paged` present) | `more(cursor)` only if the cut part matters | re-asking with larger `k` |
| Lines beyond an expanded window | the `read_code` call `expand` suggests | reading the whole file |

`search_code` returns whole code blocks: use it only when you already know you want the code text of
the top matches and `ask` + `expand` would be two steps for nothing.

---

## 3. Reading an `ask` answer

Read these fields before the hits:

- **`portfolio.strength`**: `strong` -> trust the ranking; `fair` -> top hits are probably right, verify;
  `weak` -> nothing clearly answers; the hits are leads at best. Never present a weak result as the answer.
  `unrated (lite ...)` -> the server runs the lite profile (keywords + graph, no embeddings or reranker).
  Ranking is keyword-only: phrase asks with likely identifiers, lean on `code_vocabulary` and the graph,
  and verify more of the top hits before relying on them.
- **`interpreted`**: per-term frequency in the code. A term at ~0% means the codebase uses another word.
  The **code vocabulary** list is the fix: re-ask with those identifiers.
- **`portfolio` areas / channels**: hits spread over many unrelated folders = ambiguous question; narrow
  with `path=`. Keyword-only hits on a conceptual question = the meaning index may be incomplete
  (check `index_state`).
- **`links`**: A calls B, A reaches B via one hop, same class, derives from. Often the actual answer.
- **`next`**: concrete follow-ups the server computed. Usually good; you are not obliged to follow them.
- **`index_state`** (only present when degraded): says what is stale, where, since when. Keyword and
  graph results are still valid; semantic ranking in that area is not. Tell the user if it touches
  the area of the answer.

---

## 4. The graph is name-based: know its failure modes

Links are by NAME, not resolved type (tree-sitter, no build). nncq reports ambiguity; respect it.

- `[calls X: N defs]`, `same class; N defs`, or "name is ambiguous" -> the edge may be to a different
  `X`. Verify with `expand` before stating "A calls B".
- Virtual dispatch, callbacks, jobs, delegates, function pointers, Lua/script bindings: the trail goes
  cold. `call_trace` says so. Report it as a gap, do not invent the missing hop.
- Constructors and references-not-calls (`&Foo::bar`, `bind`) are not edges yet: "uncalled" in
  `hotspots` is a candidate list, not proof of dead code.
- `#if` branches: the first branch is parsed; others are recovered. Platform-specific code can be
  under-represented.
- Test code is tagged `(test)` and ranked last. Do not cite a test as the production path.

---

## 5. Efficiency rules (you are paying in context)

- Parallelise independent calls (several `ask`s, or `symbol_context` on several symbols) in one turn.
- Repeats are cached: re-calling with the same args is free; `ask` with smaller `k` than a cached one is free.
- Do not `expand` every hit. Expand what you will cite.
- Do not `more(cursor)` by reflex. The first page holds the highest-ranked items.
- Do not re-derive what a tool already said (e.g. grepping for callers after `called_by`).
- Use `path=` / `lang=` filters early; they are the cheapest precision you can buy.
- If two asks return the same top hits, the sub-questions were not distinct; merge and move on.
- If nncq clearly fails on something (weak strength twice, cold trail), say so and fall back to
  `grep_code` / `read_code` for that one gap, not for the whole investigation.

---

## 6. The digest (what the user gets)

Write for a reader who has not seen any tool output. Structure:

1. **Answer** - 2-5 sentences. The mechanism, not a list of files.
2. **Path / chain** - the key flow as `file:line Symbol -> file:line Symbol -> ...`, innermost last.
   Only verified hops; mark inferred hops `(inferred)`.
3. **Key locations** - 3-8 bullets, `path:line Symbol - what it does in this story`.
4. **Data side** (engine only, if relevant) - data types / top assets from `data_for_code`.
5. **Confidence and gaps** - what was verified by reading code, what rests on name-based links,
   where trails went cold, any `index_state` caveat, `weak` matches.
6. **Next** (optional) - one or two follow-up questions worth asking, phrased for the user.

Rules: cite `path:line`, not `c123` refs (refs are internal and expire on re-index). Quote code only
when a few lines carry the point. No tool names in the prose unless the user asked how you found it.

---

## 7. Worked example (shape, not literal output)

User: "How do emitter graphs get spawned, and what data drives them?"

Turn 1 (parallel):
- `ask("where are emitter graphs created or spawned")`
- `ask("emitter graph entity data fields")`

Triage: first is `strong`, top hit `EmitterGraphEntity::onCreate`; links show
`onCreate -> spawnEmitter`. Second is `fair`, top hit a `.ddf` type `Effect.EmitterGraphEntityData`.

Turn 2 (parallel):
- `call_trace(to='EmitterGraphEntity::onCreate')`
- `data_for_code('EmitterGraphEntity')`
- `expand(refs='<onCreate ref>,<spawnEmitter ref>')`

Turn 3: compile. Answer with the chain (entity creation -> onCreate -> spawnEmitter), the `.ddf`
type and how many assets use it, plus a gap note if `call_trace` went cold at a factory/virtual.

Total: 5 calls, ~20-30 KB read, zero whole files opened.

---

## 8. Anti-patterns

- One `ask`, paste the hits, done.
- `ask` with `k=30` instead of a better question.
- `read_code` of a 3,000-line file to "get context".
- Stating "A calls B" from a name-only edge without checking.
- Treating `weak` hits as the answer.
- `grep_code` as the first move on a conceptual question.
- Following every `next` suggestion and every `paged` cursor.
- Reporting tool output structure (portfolio, interpreted, refs) to the user instead of a digest.
