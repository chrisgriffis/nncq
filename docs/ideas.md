# Design ideas (not implemented)

## Deep index for selected `.xdata` assets

By default `.xdata` data is not chunked or embedded. `datarefs.py` only keeps a static cross-reference:
types per asset and asset→asset references. That's about 35 s per full scan over 392k files (30 GB).

Idea: let a user opt specific assets into a full index.
- **Candidates:** the most-referenced assets (`top_assets`, optionally rolled up by folder).
- **Selection:** pick ranks, paths or globs and keep the list in a table. The indexer treats matching files as code, using lang `xdata`.
- **Chunking per asset:**
  - one embedded summary chunk (primary type, contained types, named instances, events, references)
  - one condensed chunk per instance (type, non-numeric field values, refs)
- **Symbols:** one per instance.
- **Edges:** `instance_of` (data type), `asset_ref`, and schematic `event`/`link`/`property` connections parsed from `EventConnections`/`LinkConnections`/`PropertyConnections` (Source/Target refs + SourceEvent/TargetEvent names).
- **Scale:** in samples, a `.xdata` averages about 20 instances, so a full deep index would be about 8M chunks. Only opt-in is viable.

## Usage-driven ranking

Ranking assets or code by what people actually use. Static references are a fixed baseline. Real usage needs runtime or editor signals:
- MCP tool-call activity (files queried, read, returned)
- Perforce `opened`/submitted files (editor checks `.xdata` out)
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
- Lambdas attributed to their enclosing function, not to `fb` / file level.

Background-job candidates (each is O(whole graph) and only useful as a precomputed table):
- Transitive caller count per function (blast-radius ranking for every symbol).
- Strongly connected components (recursion and include cycles).
- Whole-repo entry points and dead-code list (needs the ref edges above to be trustworthy).
- Centrality / "core API" ranking (PageRank-style over call + include edges).
