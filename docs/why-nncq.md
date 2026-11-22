# Why nncq, if you already have Copilot in Visual Studio with Visual Assist

**Short answer:** nncq does not replace either of them. Visual Assist is an index for *you*, clicking through code. Copilot is the *agent*. Copilot cannot use Visual Assist's index, so when you ask it a question it falls back to text search and reading files. nncq gives the agent its own index, with some of what Visual Assist gives you plus things neither has. It plugs into Copilot in Visual Studio over MCP, so the result is "Copilot + nncq", next to Visual Assist, not instead of it.

---

## What each one is

**Visual Assist (VA)**
- Its own fast C/C++ parser. No build needed. Find Symbol, Find References, Go To Implementation, outline, refactoring.
- Interactive and one step at a time: you click, it answers, you click again.
- Scoped to what is loaded in the solution. Large codebases often generate solutions per configuration, each with a subset of the tree.
- Its database is not available to Copilot. The agent cannot ask VA anything.

**Copilot in Visual Studio (agent mode)**
- The model plus a set of tools: workspace and text search, file reads, IntelliSense-backed symbol lookups, the terminal, and any MCP servers you add.
- Its semantic code index is aimed at repositories hosted on GitHub or Azure DevOps. A Perforce workspace gets text search plus whatever the agent reads.
- IntelliSense on a solution this size is partial and depends on configuration. The agent sees what IntelliSense has, when it has it.
- Every tool result lands in the model's context. Grep across a large tree returns hundreds of lines, and reading files to understand them costs thousands of tokens each.

**nncq**
- An index of the tree on disk, not of the solution: C++, C#, HLSL, Lua, Python, your own definition languages (declared in a codebase profile) and more, including the code that never makes it into a generated `.sln`.
- A call and type graph built from tree-sitter, no build needed: callers, callees, call traces, blast radius, type hierarchy, includes.
- A code<->data cross-reference, when the codebase profile declares data rules: which data files use a type, and which code a given data file needs.
- Search that returns whole functions and classes, ranked. Full mode adds embeddings and a reranker for questions that share no words with the code.
- Answers sized for an agent: `ask` returns a short map of where the answer is (a few KB), and the agent opens only what it needs.
- Local only: one index per person, on their machine, matching their synced changelist. nncq adds no new place your code goes (no server, no cloud, no shared index).

---

## Question by question

| Question | VA (you) | Copilot alone | Copilot + nncq |
|---|---|---|---|
| Where is `Foo` defined? | Best: instant, precise | OK: search, then read | OK: `find_symbol` |
| Who calls `Foo::bar`, precisely? | Best: parser-aware references | Grep: every `bar`, unranked | Good: name-based, ranked by class, ambiguity flagged |
| How does execution get from the main loop to X? | Manual, one level per click | Many greps and reads; often gives up | One `call_trace`: pseudo stacks with `file:line` |
| What breaks if I change this signature? | Manual | Grep and guess | `impact`: callers grouped by folder |
| "Where do we throttle streaming under memory pressure?" (no known names) | No help | Guesses words to grep | Full: semantic search + reranker. Lite: keywords, then re-ask using the code's vocabulary |
| Which data files use this type? Which code does this data file need? | No | No (a grep over a large data tree takes tens of seconds per pass, unranked) | `data_for_code` / `code_for_data` in milliseconds |
| Code outside the loaded solution (tools, shaders, scripts, custom DSLs, other configs) | Not indexed | Grep only | Indexed, graph included |
| Exact overload or template resolution | Better than nncq | IntelliSense, when configured | Weaker: links are by name |
| Rename, refactor, edit | Yes | Yes | No (read-only) |
| Why is it slow at runtime? | No | No | No: static code only. It can list what the cost depends on; a profiler says which part fires |

---

## Why the agent needs its own index

The deciding factor is context, not cleverness. An agent answers a research question by spending tool calls, and every result costs context it can't reuse:

- **Copilot alone** on "how does the number of open connections affect request latency": grep for guessed words, get 300 hits, read 4-6 files (several thousand tokens each), and stop when the budget runs out. What it finds depends on which words it guessed.
- **Copilot + nncq**: 3-4 narrow `ask`s (a few KB each), a `call_trace` and an `impact` on the best symbols, then `expand` on the 2-3 functions the answer rests on. For the same budget the agent has considered far more of the codebase, and every claim points to a `file:line`.

The `nncq-user` skill (installed by `nncq agents`) tells the agent to work this way: break the question into several small asks, follow the best hits through the graph, verify, then write one digest. Copilot without it tends to do one search and start reading files.

---

## Where nncq is worse

- **Links are by name, not resolved type.** A call to `update()` matches every `update`. nncq ranks the callers and says when a name is ambiguous, but VA is more precise for "exactly which overload".
- **Static only.** Jobs, callbacks, virtual dispatch and script bindings break call traces. nncq reports where a trail goes cold instead of inventing the missing hop, but it cannot fill it.
- **Read-only.** No refactoring or edits; that stays in VA and Copilot.
- **Lite has no semantic search.** Questions that share no words with the code are weaker. The graph tools, the cross-reference and `ask` / `expand` work the same.

---

## Recommended setup

1. Keep Visual Assist for your own navigation and refactoring.
2. Install nncq **lite** (`init.ps1 -Lite`): about 150 MB, no GPU, the index builds in minutes.
3. `nncq agents --targets vs` (and `copilot`, `claude` if you use them): registers nncq in `%USERPROFILE%\.mcp.json` and installs the skill where the agent supports one. In Visual Studio, enable the nncq tools in the Copilot Chat tool picker (agent mode), and paste `nncq-user.skill.md` into your custom instructions.
4. Ask research questions in plain words. Expect a mechanism plus `file:line` locations and a list of gaps, not a guess.
5. Upgrade one machine to **full** (GPU) if questions with no shared words with the code keep coming back weak.

---

## How to check this rather than take it on faith

Pick 30-40 questions with known answer locations (concept questions, "who calls", "what breaks if", code<->data). Run each through Copilot alone, Copilot + nncq lite, and Copilot + nncq full. Score:

- whether the key `file:line` locations were found;
- tokens spent;
- tool calls made;
- wrong claims stated with confidence.

If lite does not beat Copilot alone, the problem is how the agent uses nncq (skill, response size), not the index. If full does not beat lite, the GPU is not worth it for your questions.
