# Design ideas (not implemented)

## Deep index for selected data files

By default the data files matched by a profile's `[[xref]]` rules are not chunked or embedded. `datarefs.py` only
keeps a static cross-reference: types per data file and file→file references. On a tree with a few hundred
thousand data files that is tens of seconds per full scan.

Idea: let a user opt specific data files into a full index.
- **Candidates:** the most-referenced data files (`hot_data`, optionally rolled up by folder).
- **Selection:** pick ranks, paths or globs and keep the list in a table. The indexer treats matching files as code, using the xref rule's name as the lang.
- **Chunking per data file:**
  - one embedded summary chunk (primary type, contained types, named instances, references)
  - one condensed chunk per instance (type, non-numeric field values, refs)
- **Symbols:** one per instance.
- **Edges:** `instance_of` (data type), `data_ref`, and, where the profile declares extra patterns for them, wiring between instances (event / link / property connections given as source and target refs).
- **Scale:** at about 20 instances per data file, a full deep index of a few hundred thousand files is millions of chunks. Only opt-in is viable.

## Usage-driven ranking

Ranking data files or code by what people actually use. Static references are a fixed baseline. Real usage needs runtime or editor signals:
- MCP tool-call activity (files queried, read, returned)
- Perforce `opened`/submitted files (editors that check data files out)
- editor event watching

A separate collector would write its own tables, and ranking would consult them (e.g. filter or boost heavy hitters, raise background embedding priority).

Dropped for the MVP: low value relative to the tracking cost, and editor usage needs editor hooks.

## Changelist stamping (phase 2)

The index tracks the working tree on disk, not a changelist, and every update converges to whatever is on disk.
Phase 2 would also stamp each update with the workspace's have-changelist
(`p4 -ztag changes -m1 -s submitted "//<client>/...#have"`). Then `status` could report "index = CL A,
workspace = CL B" and catch-up could be phrased as "delta A..B" (`p4 files //<client>/...@A,@B`)
instead of a full stat sweep. Useful mainly when the stat sweep becomes slow or when sharing an index
across machines synced to the same CL.

## Round-trip UML (diagram edits -> source changes)

Not built. The UML model is kept lossless enough for it (`nncq/uml`):
- every element and relationship has a stable id (`sym:<qualname>[@path]`, `comp:`, `port:`, ...; member ids
  derived from qualname + parameter hash) and a source span: file, start/end line and column (`symbols.start_col`
  / `end_col`, `edges.col`), plus the absolute path;
- raw declaration text per element (`decl`: the member / type declaration as written) and the parsed parts
  (visibility, mods, rtype, params, tparams);
- `MODEL_VERSION` in every model, `FACTS_VERSION` for the parse; views keep the spec (what was asked / authored)
  apart from the materialized model.

Extension point: a `uml/apply.py` that diffs an edited view model against the stored one and turns each change into
a span edit (rename member -> replace name token at its span, add field -> insert after the last field
declaration of that visibility, change multiplicity -> rewrite the declared type), shown as a patch for the user
to apply through the dashboard editor (vcs.py: p4 edit / git stage). Spans go stale exactly when the staleness
report says so, which is the guard against editing moved code.

## Background graph jobs (pinned, phase 2)

Rule: the indexing crawl records cheap per-file facts while it is already reading each file. Graph views
run on demand as SQLite queries. Only whole-program work that is too slow even on demand runs as a
background job, and its results are stamped with the index generation they were computed from.

Per-file facts to record during the crawl (one pass, free):
- Constructor calls (`new X`, `X x(...)`, `X{...}`) as call edges to X. Without these, constructors look
  like entry points and traces stop there.
- Function references that are not calls (`&Foo::bar`, `bind(...)`, delegate/event `+=`, Lua callbacks)
  as `ref` edges. These remove most false "uncalled" results and let traces pass through callbacks.
- `virtual`/`override`/`final` flags on methods, so an override can be linked to its base method.
- Lambdas attributed to their enclosing function, not to the namespace / file level.

Background-job candidates (each is O(whole graph) and only useful as a precomputed table):
- Transitive caller count per function (blast-radius ranking for every symbol).
- Strongly connected components (recursion and include cycles).
- Whole-repo entry points and dead-code list (needs the ref edges above to be trustworthy).
- Centrality / "core API" ranking (PageRank-style over call + include edges).
