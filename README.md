# no-net-code-query (nncq)

Local code retrieval for AI coding agents. It combines hybrid search (BM25 plus embeddings plus a reranker), AST-aware chunks, a symbol and call graph, and a engine code↔data cross-reference. Everything is served to agents over MCP. Nothing leaves the machine.

> **Status: MVP in progress.** Done: chunking (tree-sitter and engine DSLs), the SQLite store, file enumeration, the `.xdata` data cross-reference, model wrappers, T0 indexing, and the graph views. In progress: the embedding worker, search, the MCP server and the CLI. Commands below describe the intended interface.

---

## What it gives an agent

| MCP tool | Answers |
|---|---|
| `search_code` | "where is X done?" in natural language or with identifiers. Hybrid ranking, returns whole functions or classes |
| `grep_code` | exact text or regex matches (ripgrep, honours `.p4ignore`) |
| `find_symbol` | where a class, function or method is defined |
| `file_outline` | the symbols in a file, with line ranges, without reading the file |
| `find_references` | callers, subclasses, and text mentions of a name |
| `symbol_context`, `calls`, `called_by`, `call_tree`, `call_trace`, `impact`, `type_hierarchy`, `file_context`, `hotspots` | the symbol graph views, [see below](#graph-tools) |
| `read_code` | an exact line range from disk |
| `data_for_code` | engine: which data types / assets use this code type |
| `code_for_data` | engine: which code defines or implements the types this asset uses |
| `asset_info`, `top_assets` | engine: what an asset contains, which assets are referenced most |
| `index_status`, `update_index` | freshness, embedding progress, delta re-index |

### Search and the AST work together

Search results are not just matching text. Every chunk is cut at a function or class boundary and carries its **path, line range, symbol, kind and parent**. Every symbol is also a node in the graph, so an agent can chain tools without reading whole files:

1. `search_code("how are emitter graphs spawned")` returns `EmitterGraphEntity::onCreate` in `EmitterGraphEntity.cpp:120-188`, kind `method`, parent `EmitterGraphEntity`.
2. `call_tree("onCreate", in)` shows who triggers it. `file_outline` shows the rest of the class.
3. `data_for_code("EmitterGraphEntity")` returns `Effect.EmitterGraphEntityData`, used by 3,400 `.xdata` assets, with the top ones listed.
4. `asset_info("EBP_TOY_Stopper_Damaged")` → which types it instantiates and what references it. `code_for_data` leads back to the `.ddf` and C++.

Each step returns a few hundred tokens of structured answer instead of thousands of tokens of raw file text.

### The symbol graph: "how does it know that without a build?"

Usually, to get "who calls this", "what derives from this" or "who includes this header" you need an IDE that has **built** the project: clangd with `compile_commands.json`, IntelliSense with the right configuration, or a SCIP indexer that runs the compiler front-end. For engine that means generated code, millions of lines, and hours of indexing.

nncq gets a useful graph **without compiling anything**, using [tree-sitter](https://tree-sitter.github.io/):

1. **Parse every file on its own.** tree-sitter is an incremental, error-tolerant parser, the same one editors use for syntax highlighting. It turns each file into a concrete syntax tree in milliseconds. It needs no headers, macros, include paths or compiler flags. Broken or half-written code still parses, with the bad region marked.
2. **Walk the tree once and record facts:**
   - *definitions*: class, struct, enum, function, method, namespace, with qualified name (`app::EmitterGraphEntity::onCreate`), parent, line span and signature
   - *calls*: from the enclosing definition to the callee's name (`onCreate → spawnEmitter`)
   - *bases*: `class Bar : public Foo` gives `Bar inherits Foo` (C++, C#, Python, Java, TS/JS, Kotlin, Swift, PHP, Ruby, and engine DDF)
   - *includes*: `#include "Engine/Core/Foo.h"` gives `file includes foo.h`
3. **Join by name in SQLite.** "Callers of X" is an indexed lookup on the callee name. "Derived from X" is the same on base names. Call trees, call paths and impact analysis are breadth-first walks over those joins.
4. **Chunk on the same tree.** Search chunks are cut at the same definition boundaries, so every search hit is already a graph node. That's why search and navigation compose.

The parsers are a few MB each, cached in `cache/tools/tree-sitter`. Parsing runs at about 500 files/s per CPU core, so a full engine tree takes minutes, and a delta after `p4 sync` takes seconds.

**The trade-off is honest:** links are by *name*, not resolved type. A call to `update()` matches every definition named `update`. nncq says when a name is ambiguous and ranks callers that are in the same class, or whose code mentions the class, first. Qualified names and paths narrow it down. For the questions you ask while reading code ("roughly who uses this", "how are A and B related", "what breaks if I change this"), name-level precision is usually enough. It is available on every language and branch the moment the files are on disk.

#### Graph tools

| Tool | What you get |
|---|---|
| `symbol_context` | One-call card for a symbol. For a type: definition(s), bases and base chains, derived types, members, engine data types that use it. For a function: callers (ranked), callees (resolved to definitions when unique), overrides up and down the hierarchy. Plus how many files mention it and whether the name is ambiguous |
| `calls` | Flat "Calls From" list: everything a function calls, in source order, each resolved to its definition when the name is unique |
| `called_by` | Flat "Calls To" list: every caller with file:line. When several functions share the name, rows are ranked same class > code mentions the class > name only |
| `call_tree` | Callers (`in`) or callees (`out`) as an indented tree, N levels deep, cycle-safe |
| `call_trace` | "How does execution get here?" as debugger-style pseudo stacks (innermost frame first, file:line per frame), walked back to entry points (functions nothing calls). Given two functions, it shows how they relate: A reaches B, B reaches A, or a nearest common caller of both. Routes through unambiguous names are listed first, and a link that matched by name alone is tagged `[calls X: N defs]` |
| `impact` | Transitive callers of X, summarised by folder and file: the blast radius before you change a signature |
| `type_hierarchy` | Base chain(s) up and the derived tree down |
| `file_context` | Outline, `#include`s, who includes this file, and which files it calls into or is called from |
| `hotspots` | Static top-N tables per folder: most-called functions, most-derived bases, biggest fan-out, most-included headers, largest functions, uncalled functions (dead-code candidates) |

### Compared with a grep or search skill

A grep skill is good when you know the exact string. It falls short when you don't:

- **Concepts versus strings.** "Where do we throttle streaming when memory is low" matches nothing literally. Embeddings and the reranker find it. BM25 still catches exact identifiers, and the two are fused so neither kind of query suffers.
- **Ranking.** Grep returns every match in file order. In a 1M-file workspace that is hundreds of hits, and the agent pays tokens to read them. Here a cross-encoder reranks the top 40 and the agent sees the best 10.
- **Units of meaning.** Grep returns a line. Here a hit is the whole enclosing function, labelled with its class.
- **Structure.** Grep can't tell you which method a call is in, what a class derives from, or what a file defines. The graph answers that from one index lookup.
- **Data↔code.** Finding which of 392k `.xdata` files use a type costs a 35-second ripgrep pass each time. The cross-reference answers it in milliseconds.
- **Precomputed.** Parsing and embedding happen once, plus small deltas. Queries are index lookups, not tree walks.

`grep_code` is still included, because for an exact string grep is the right tool.

---

## Local only and air-gapped

- The network is used **only during `init`**: pip packages and a one-time download of the pinned model revisions into `cache/models/hf`.
- After that, the Hugging Face stack is forced offline (`HF_HUB_OFFLINE=1`, telemetry off). Models load from the repo folder.
- There are no cloud APIs, no telemetry and no external services. The server binds to `127.0.0.1` only. A non-loopback bind is refused unless `NNCQ_TOKEN` is set.
- For a machine with no internet, copy the models from another checkout with `init.ps1 -SeedModels <hub dir>`. pip still needs a wheel cache or mirror.
- Everything lives inside the clone (`.venv/`, `cache/`, `.env`). Deleting the folder uninstalls it.

## Requirements and cost

- **GPU:** NVIDIA with CUDA 12.8 support. Embedder and reranker are ~1B parameters each, about 2.5 GB VRAM each in bf16, plus working memory. Developed on an RTX 5080 (16 GB). An 8 GB card should work with a smaller `NNCQ_GPU_BATCH_TOKENS`. CPU-only works for queries but is too slow to embed a large codebase.
- **Disk:**

  | What | Size | Notes |
  |---|---|---|
  | `.venv` | 4.6 GB | 4.1 GB of it is CUDA torch |
  | `cache/models` | 9.2 GB | two Nemotron models as published (weights 2.3 GB each) |
  | `cache/tools` | 2.7 GB | pip cache and tree-sitter parsers. The pip cache can be deleted |
  | index | varies | engine data cross-reference: 420 MB for 390k `.xdata`. Vectors: 2 KB per chunk (1024-dim fp16), about 2 GB per million chunks |

- **RAM:** the server keeps vectors in memory (2 KB per chunk). Models load on first query and unload after `NNCQ_MODEL_IDLE_SECONDS`.
- **Time (measured on the sample workspace):**
  - T0 parsing: about 500 files/s per process (7,220 files in 15 s, single process).
  - Full `.xdata` cross-reference: 84 s for 388k files.
  - Embedding throughput: not measured yet. It is the slow part, hours for a large tree, which is why it is tiered and interruptible.

## GPU use during indexing: on demand, interruptible

Embedding keeps the GPU close to fully busy. It is built to stay out of your way:

- **Tiers.**
  - **T0 (CPU):** chunks, BM25, symbols and graph. Searchable within minutes.
  - **T1 (GPU):** short "card" vectors (path, symbol, signature), cheap and fast.
  - **T2 (GPU):** full-chunk vectors.

  Search uses whatever tier each chunk has reached. Recently changed files go first.
- **Pause and resume.** `nncq pause` makes the worker finish its current batch and **exit**, so VRAM is fully released (it runs as its own process). `nncq resume` picks up where it stopped. Progress is saved per batch, so Ctrl+C, a reboot or a crash loses at most one batch.
- **On demand.** `NNCQ_EMBED_TIER=0` disables background embedding. You can also run `nncq work` only when you choose.
- **Polite mode.** `NNCQ_BG_WHEN=idle` embeds only after `NNCQ_IDLE_MINUTES` without keyboard or mouse input. `NNCQ_GPU_MIN_FREE_MB` skips starting while the GPU is busy, e.g. a game or the editor.
- **Queries stay light.** A query is one short embedding plus reranking 40 chunks, a fraction of a second.

---

## Why each tool is used

| Tool | Why |
|---|---|
| **NVIDIA Nemotron embed + rerank 1B (v2)** | Strong on code and long passages. The embedder is Matryoshka-trained (2048 dims, truncated to `NNCQ_EMBED_DIM`) and asymmetric (separate query and document prompts). The reranker is a cross-encoder, which is where most of the precision comes from. Pinned revisions, open weights. |
| **torch (CUDA) + transformers + sentence-transformers** | Runs both models in-process. There's no model server to install or keep running. |
| **SQLite (FTS5)** | One file per workspace holds chunks, BM25 index, vectors, symbols, edges and the data cross-reference. No database server, safe concurrent reads (WAL), part of Python. |
| **numpy** | Brute-force cosine over in-memory vectors. Exact and fast enough for a few million chunks, with nothing extra to tune. |
| **tree-sitter + language pack** | Fast, error-tolerant parsers for 20+ languages. They provide AST chunk boundaries, symbols and call edges without compiling anything. |
| **ripgrep** | Lists 1M+ files in seconds and honours `.p4ignore`/`.gitignore`. Also streams the `.xdata` scan and backs `grep_code`. Installed into the venv from pip. |
| **watchdog** | File-change events for automatic delta re-indexing. |
| **mcp + uvicorn** | The standard MCP server (streamable HTTP and stdio), usable from Copilot CLI, VS Code, Claude Code and others. |

**Not used, and why:**
- **Milvus, Qdrant, Docker:** an extra always-on service. SQLite plus numpy covers this scale.
- **Ollama:** it does not serve the Nemotron models, and it would be another service.
- **LSP/SCIP indexers (clangd, Serena):** they need per-language servers and full builds. For engine C++ that means a working `compile_commands.json` for millions of lines. tree-sitter gives a **name-based** graph without building. The trade-off is that calls are matched by name, not by resolved type. See Limitations.

---

## Design

```
            ripgrep (+.p4ignore) ──► stat ──► changed files
                                               │
      T0 CPU process pool:  tree-sitter / DSL chunker ──► chunks, symbols, edges
                                               │
 SQLite index.db ◄─────────────────────────────┘
   files, chunks(tier), fts(bm25), vecs(fp16), symbols, edges, data_* (xdata xref)
     ▲                         ▲
     │ T1/T2 GPU worker        │ queries
     │ (separate process,      │
     │  pause = exit)          │
                      MCP server (127.0.0.1:8765/mcp, or stdio)
                        search: BM25 ∪ dense ─► RRF fuse ─► rerank ─► top k
                        graph:  symbols / edges by name
                        data:   xdata types ↔ DDF ↔ C++
                        watcher ─► path deltas
```

- **Chunking** (`chunker.py`):
  - Definitions become chunks. A class too big for one chunk is split into a header chunk plus one chunk per member.
  - Code between definitions becomes gap chunks. Very long functions are split into overlapping line windows.
  - engine `.ddf/.adf/.ser/.hubl` use a brace-block chunker. `.ddf` types are qualified by their `module` (`Entity.LayerData`), which is exactly how `.xdata` names them.
- **FTS:** identifiers are also indexed as sub-words, so `spawn` matches `SpawnEntityData` and `spawn_entity`. The symbol column is weighted above the body.
- **Search:**
  - BM25 top 100 and dense top 100 are merged by reciprocal-rank fusion.
  - The top 40 go through the cross-encoder.
  - If no vectors exist yet, it falls back to lexical search and says so.
- **Graph:**
  - Symbols cover each definition: qualified name, kind, parent, lines and signature.
  - Edges are `call` (enclosing symbol → callee name) and `inherits` (type → base).
  - Call hierarchy walks edges by name.
- **engine data** (`datarefs.py`):
  - `.xdata` files are *not* chunked or embedded. One ripgrep pass records the instance types per file and the asset→asset references.
  - Code↔data joins go through type names. `entity Foo` in DDF corresponds to data type `FooData`, and DDF `: Base` edges add subclasses.
  - Asset lookups use reversed-path keys, so any path suffix resolves.
- **Deltas: a sync never forces a rebuild.** Cost follows what changed, not the workspace size:
  - `nncq update` compares on-disk stats with the index. A file is re-parsed only when its SHA-1 changed, so a `p4 sync` that only touches mtimes is cheap.
  - `nncq sync [p4 args]` runs `p4 sync` and re-indexes exactly the files it reports, however many there are.
  - The server's watcher handles edits as you save.
  - The graph is stored per file and resolved by name at query time. A changed header does not re-process the files that include it, and calls between unchanged files stay correct.
  - Chunks whose text did not change keep their vectors, so editing one function in a 3,000-line file re-embeds one chunk. Only that edit loses semantic search until the background worker catches up; keyword and graph search see it immediately.
  - `.xdata` changes update the data cross-reference incrementally, in small transactions (about 700 files/s), so it stays queryable throughout.
  - `nncq index --fresh` rebuilds everything from scratch. It is never needed for correctness; run it overnight if you want a clean slate.
- **Graceful degradation:** tools never block on indexing. When something is stale, a response carries `index_state`: what is degraded, where (folders), since when, what still works, and whether the returned results fall in that area. Cases: a sync in progress or interrupted, changed code not embedded yet, and the semantic index still building. Healthy responses carry nothing extra.
- **Storage layout:**
  ```
  init.ps1, pyproject.toml   repo root (launch + packaging only)
  src/nncq/                  all code
  docs/                      design notes (docs/ideas.md: unbuilt ideas)
  .venv/                     python env            (git-ignored)
  .env                       your settings         (git-ignored, written by init)
  cache/models/hf/           model weights         (git-ignored)
  cache/tools/               pip, parsers, torch   (git-ignored)
  cache/db/<repo>/index.db   one index per workspace
  cache/logs/                server / worker logs
  ```

---

## Quick start (Windows)

```powershell
git clone <this repo> no-net-code-query; cd no-net-code-query
.\init.ps1                       # venv, CUDA torch, models, interactive .env
.venv\Scripts\nncq index C:/work\my-workspace --dirs src/Code   # T0 + data xref
.venv\Scripts\nncq start         # background server; embeds in the background
.venv\Scripts\nncq mcp-config    # prints config for Copilot CLI / VS Code / Claude Code
```

Copilot CLI (`~/.copilot/mcp-config.json`):
```json
{ "mcpServers": { "nncq": { "type": "http", "url": "http://127.0.0.1:8765/mcp", "tools": ["*"] } } }
```

Day to day: `nncq sync` (p4 sync plus delta), `nncq status`, `nncq pause` / `nncq resume`, `nncq stop`.

**Linux / Ubuntu:** nothing in the core is Windows-specific (no Windows-only dependencies, paths stored as `/`). It needs an `init.sh` equivalent of `init.ps1`. Idle detection uses `xprintidle` when available. It has not been tested on Linux yet.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `NNCQ_DEVICE` | `auto` | `cuda`, `cpu`, or `auto` |
| `NNCQ_DTYPE` | `bf16` | `fp32` doubles VRAM for no practical retrieval gain |
| `NNCQ_EMBED_DIM` | `1024` | vector size, up to 2048. Lower uses less RAM and disk |
| `NNCQ_EMBED_TIER` | `2` | `0` no vectors, `1` cards only, `2` cards then full chunks |
| `NNCQ_BG_WHEN` / `NNCQ_IDLE_MINUTES` | `always` / `5` | embed only when idle |
| `NNCQ_GPU_MIN_FREE_MB` | `0` | don't start embedding below this much free VRAM |
| `NNCQ_GPU_BATCH_TOKENS` | `32768` | GPU batch size. Lower it on smaller cards |
| `NNCQ_RERANK` / `NNCQ_RERANK_CANDIDATES` | `true` / `40` | cross-encoder reranking |
| `NNCQ_INDEX_PROCS` | half the cores (max 8) | T0 parser processes |
| `NNCQ_TEXT_LANGS` | `markdown,cmake,make,dockerfile,proto,batch` | non-code formats to index (add `xml,json,yaml,...` if wanted) |
| `NNCQ_EXTRA_EXTENSIONS` | | e.g. `.foo,.bar=cpp` |
| `NNCQ_IGNORE` | | extra ignore globs (on top of `.p4ignore`) |
| `NNCQ_DATA` | `true` | maintain the `.xdata` cross-reference |
| `NNCQ_HOST` / `NNCQ_PORT` / `NNCQ_TOKEN` | `127.0.0.1` / `8765` / | token required for non-loopback |
| `NNCQ_WATCH` | `true` | re-index on file save |

## Limitations

- **The graph is name-based, not type-resolved.** See [the trade-off](#the-symbol-graph-how-does-it-know-that-without-a-build). LSP-precise references would need a buildable project. Possible later via SCIP for languages where that is cheap (C#, Python, TS).
- **Data↔code goes through type names**, not runtime paths. Which asset a running game actually loads is out of scope; see `docs/ideas.md`.
- **One GPU, one machine.** LAN sharing (one indexer, many clients, behind a token) is planned but not built.

## Roadmap

- **Phase 1 (MVP):** indexer, embedding worker, hybrid search, graph tools, MCP server, CLI, `init`.
- **Phase 2:** changelist stamping and CL-range deltas (`docs/ideas.md`), Linux `init.sh`, LAN mode with a token.
- **Editor plugins** (they call the same local server):
  - **Visual Studio:** symbol card, call tree, impact and type hierarchy for the symbol under the caret. A semantic search panel. "What data uses this" for engine types.
  - **editor:** from a selected asset or type, show the code that defines and implements it (`code_for_data`), and jump from code to the assets that use it.
