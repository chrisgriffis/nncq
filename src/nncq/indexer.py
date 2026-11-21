"""T0 indexing: files -> chunks + FTS + symbols + edges (CPU only, usable immediately).
Vectors are added later by worker.py.

Full / stat-delta:  update(repo)          enumerate with ripgrep, stat, re-chunk new/changed, drop removed
Path delta:         update_paths(repo, p) for the watcher (exact file list)
The engine .xdata cross-reference (datarefs.py) is refreshed alongside when NNCQ_DATA is on.
Read-only on the workspace: files are only listed, stat'ed and read; everything is written under cache/.

Sync cost is proportional to what changed, never to the workspace (sync with P4V / p4 as usual):
  - the watcher hands over exactly the paths that changed; at start-up a stat pass finds the rest
  - content-identical files (same sha1) are just re-stamped
  - the graph is name-based per file, so a changed header does not re-process the files that include it
  - chunks whose text did not change keep their vectors (store.replace_file), so only edited code re-embeds
  - .xdata stats are updated incrementally; no amount of change triggers a full data rescan
While a delta is being applied, meta 'sync' records what/where/since when (health.py reports it in responses).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from nncq import chunker, config, datarefs, langs, sysutil, walk
from nncq.repos import Repo
from nncq.store import Store

BATCH = 256          # files per write transaction
POOL_MIN = 64        # below this many files, chunk in-process (pool start-up costs more)


def ext_map() -> dict[str, str]:
    """Code (tree-sitter languages + engine DSLs) + the text formats listed in NNCQ_TEXT_LANGS."""
    s = config.settings()
    keep = {x.strip() for x in s.text_langs.split(",") if x.strip()}
    extra = {("." + e.strip().lstrip(".").partition("=")[0]).lower()
             for e in s.extra_extensions.split(",") if e.strip()}
    m = langs.build_ext_map(s.extra_extensions)
    return {e: l for e, l in m.items()
            if langs.is_ast_lang(l) or l in chunker.DSL_LANGS or l in keep or e in extra}


def globs(m: dict[str, str]) -> list[str]:
    keep = {x.strip() for x in config.settings().text_langs.split(",")}
    return sorted({"*" + e for e in m} | {n for n, l in langs.FILENAME_LANG.items() if l in keep})


def lang_of(path: str, m: dict[str, str]) -> str | None:
    lang = langs.lang_for(path, m)
    if lang and path.rsplit("/", 1)[-1] in langs.FILENAME_LANG:
        keep = {x.strip() for x in config.settings().text_langs.split(",")}
        return lang if lang in keep else None
    return lang


def open_store(repo: Repo) -> Store:
    st = Store(repo.db)
    if st.meta("root") != str(repo.root):
        st.set_meta("root", str(repo.root))
    return st


# ------------------------------------------------------------- per-file work (runs in pool processes)

def _work(job):
    root, rel, lang, max_chars, max_bytes, known_sha1 = job
    try:
        data = (Path(root) / rel).read_bytes()
    except OSError as e:
        return rel, "gone", str(e), None
    sha1 = hashlib.sha1(data).hexdigest()
    if sha1 == known_sha1:
        return rel, "same", sha1, None
    if len(data) > max_bytes:
        return rel, "new", sha1, chunker.FileResult(lang, "skipped", error=f"larger than {max_bytes // 1024} KB")
    if b"\0" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return rel, "new", sha1, chunker.FileResult(lang, "skipped", error="binary")
    return rel, "new", sha1, chunker.process(rel, data, lang, max_chars)


def folders(paths, depth: int = 4, k: int = 6) -> list[str]:
    """'src/Code/Engine/Entity (812)' style summary of where a set of paths lives."""
    c = Counter("/".join(p.split("/")[:depth]) for p in paths)
    out = [f"{f} ({n})" for f, n in c.most_common(k)]
    if len(c) > k:
        out.append(f"+{len(c) - k} more folders")
    return out


def _other_parser(store: Store) -> int:
    """pid of another live process that is writing chunks into this index (T0 build / catch-up), else 0."""
    try:
        pid = json.loads(store.meta("sync") or "{}").get("pid", 0)
    except ValueError:
        return 0
    return pid if pid and pid != os.getpid() and sysutil.pid_alive(pid) else 0


def _begin(store: Store, source: str, paths: list[str]) -> dict:
    state = {"source": source, "started": time.time(), "pid": os.getpid(), "total": len(paths), "done": 0,
             "where": folders(paths)}
    store.set_meta("sync", json.dumps(state))
    return state


def _end(store: Store) -> None:
    store.db.execute("DELETE FROM meta WHERE key='sync'")


def _apply(store: Store, root: Path, todo: list[tuple[str, str, tuple[int, int]]],
           known: dict[str, tuple[int, int, str]], log, label: str, state: dict | None = None) -> dict:
    """todo: [(rel, lang, (size, mtime_ns))]. Chunk them (pool if many) and write in batches."""
    s = config.settings()
    jobs = [(str(root), rel, lang, s.chunk_chars, s.max_file_kb * 1024, (known.get(rel) or (0, 0, None))[2])
            for rel, lang, _ in todo]
    stat = {rel: st for rel, _, st in todo}
    out = {"changed": 0, "same": 0, "gone": 0, "errors": 0}
    t0 = last = time.time()
    pending: list = []

    def flush():
        with store.tx():
            for rel, what, sha1, r in pending:
                size, mtime = stat[rel]
                if what == "same":
                    store.touch_file(rel, size, mtime)
                elif what == "gone":
                    store.delete_file(rel)
                else:
                    store.replace_file(rel, size, mtime, sha1, mtime / 1e9, r)
                    out["errors"] += bool(r.error)
                out[what if what != "new" else "changed"] += 1
            if state is not None:
                state["done"] += len(pending)
                store.set_meta("sync", json.dumps(state))
        pending.clear()

    if len(jobs) < POOL_MIN or s.index_procs <= 1:
        results = map(_work, jobs)
        pool = None
    else:
        pool = ProcessPoolExecutor(s.index_procs)
        results = pool.map(_work, jobs, chunksize=16)
    try:
        for i, res in enumerate(results, 1):
            pending.append(res)
            if len(pending) >= BATCH:
                flush()
            if time.time() - last > 10:
                last = time.time()
                log(f"  {label}: {i}/{len(jobs)} files ({i / (last - t0):.0f}/s)")
        if pending:
            flush()
    finally:
        if pool:
            pool.shutdown(cancel_futures=True)
    out["seconds"] = round(time.time() - t0, 1)
    return out


# ------------------------------------------------------------- entry points

def update(repo: Repo, full: bool = False, data: bool | None = None, log=print) -> dict:
    """Bring the index in line with disk (first run = full build). full: re-chunk everything."""
    s = config.settings()
    t0 = time.time()
    store = open_store(repo)
    try:
        other = _other_parser(store)
        if other:
            log(f"[{repo.name}] another indexer (pid {other}) is running; it covers this")
            return {"skipped": f"another indexer (pid {other}) is running"}
        m = ext_map()
        files = [f for f in walk.list_files(repo.root, globs(m), walk.code_ignore(s.ignore), repo.dirs)
                 if lang_of(f, m)]
        log(f"[{repo.name}] {len(files)} code files under {repo.root}"
            + (f" ({', '.join(repo.dirs)})" if repo.dirs else ""))
        stats = walk.stat_many(repo.root, files)
        known = {} if full else store.file_stats()
        todo = [(p, lang_of(p, m), st) for p, st in stats.items() if (known.get(p) or (None, None))[:2] != st]
        removed = [p for p in store.file_stats() if p not in stats]
        state = _begin(store, "full build" if not known else "catch-up", [x[0] for x in todo] + removed)
        if removed:
            with store.tx():
                for p in removed:
                    store.delete_file(p)
        res = _apply(store, repo.root, todo, known, log, "code", state) if todo else {"changed": 0}
        res["removed"] = len(removed)
        store.set_meta("indexed_at", time.time())
        log(f"[{repo.name}] code: {res}")
        if s.data if data is None else data:
            log(f"[{repo.name}] data cross-reference (.xdata) ...")
            d = datarefs.update(store, repo.root, s.ignore, log)
            log(f"[{repo.name}] data: {d}")
            res["data"] = d
        _end(store)
        res["seconds_total"] = round(time.time() - t0, 1)
        return res
    finally:
        store.close()


def update_paths(repo: Repo, paths: list[str], store: Store | None = None, log=print,
                 source: str = "file changes") -> dict:
    """Delta for an explicit list of repo-relative paths (watcher events)."""
    s = config.settings()
    own = store is None
    store = store or open_store(repo)
    try:
        m = ext_map()
        ign = walk.Ignorer(repo.root, s.ignore)
        code_ign = walk.Ignorer(repo.root, walk.code_ignore(s.ignore), repo.dirs)
        rels = sorted({p.replace("\\", "/") for p in paths})
        xdata = [p for p in rels if p.lower().endswith(".xdata") and not ign.ignored(p)]
        code = [p for p in rels if lang_of(p, m) and not code_ign.ignored(p)]
        want = set(code)
        known = {p: v for p, v in store.file_stats().items() if p in want}
        stats = walk.stat_many(repo.root, code)
        todo = [(p, lang_of(p, m), st) for p, st in stats.items() if (known.get(p) or (None, None))[:2] != st]
        gone = [p for p in code if p not in stats and p in known]
        if not (todo or gone or xdata):
            return {"changed": 0}
        busy = _other_parser(store)                # a build is running: write the delta, leave its progress marker
        state = None if busy else _begin(store, source, [x[0] for x in todo] + gone + (xdata if s.data else []))
        try:
            if gone:
                with store.tx():
                    for p in gone:
                        store.delete_file(p)
            res = _apply(store, repo.root, todo, known, log, "code", state) if todo else {"changed": 0}
            res["removed"] = len(gone)
            if xdata and s.data:
                res["data_files"] = datarefs.scan_paths(store, repo.root, xdata, log)
        finally:
            if not busy:
                _end(store)
        return res
    finally:
        if own:
            store.close()


def status(repo: Repo) -> dict:
    if not repo.db.exists():
        return {"repo": repo.name, "root": str(repo.root), "indexed": False}
    st = Store(repo.db, cache_mb=8)
    try:
        c = st.counts()
        c.update(repo=repo.name, root=str(repo.root), dirs=repo.dirs, indexed=True,
                 vectors=st.q("SELECT COUNT(*) FROM vecs")[0][0],
                 file_errors=st.q("SELECT COUNT(*) FROM files WHERE error IS NOT NULL")[0][0],
                 indexed_at=_ts(st.meta("indexed_at")), data_scanned_at=_ts(st.meta("data_scanned_at")),
                 db_mb=round(sum(f.stat().st_size for f in repo.db.parent.glob("index.db*")) / 2**20))
        return c
    finally:
        st.close()


def _ts(v) -> str | None:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v))) if v else None
