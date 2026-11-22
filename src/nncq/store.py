"""One SQLite file per indexed workspace: code chunks + FTS5 + vectors + symbol graph + data xref.

Code (tiered, see worker.py):
  files, chunks (tier 0 = no vector, 1 = card vector, 2 = full vector), fts (bm25), vecs, symbols, edges
engine data cross-reference (datarefs.py, no chunks/vectors):
  data_files, data_types, data_inst (type x file), data_assets (reference targets), data_refs
UML (nncq/uml/schema.py): extra symbol/edge columns, components, views (+ what they depend on, for staleness)
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from nncq.chunker import FileResult
from nncq.uml import schema as uml_schema

SCHEMA_VERSION = "2"     # 2: UML columns on symbols/edges + UML tables (nncq/uml/schema.py), migrated in place

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT, size INT, mtime_ns INT, sha1 TEXT,
  prio REAL, indexed_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INT NOT NULL, start_line INT, end_line INT,
  symbol TEXT, kind TEXT, parent TEXT, text TEXT, tier INT NOT NULL DEFAULT 0, prio REAL);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS chunks_tier ON chunks(tier, prio DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  path, symbol, body, content='', contentless_delete=1, tokenize="unicode61 tokenchars '_'");
CREATE TABLE IF NOT EXISTS vecs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, chunk_id INT UNIQUE NOT NULL, tier INT, v BLOB);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, file_id INT NOT NULL, name TEXT, qualname TEXT, kind TEXT, parent TEXT,
  start_line INT, end_line INT, signature TEXT);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_qual ON symbols(qualname COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS symbols_parent ON symbols(parent);
CREATE TABLE IF NOT EXISTS edges(file_id INT NOT NULL, src TEXT, dst TEXT, kind TEXT, line INT, label TEXT);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS edges_file ON edges(file_id);

CREATE TABLE IF NOT EXISTS data_files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, rkey TEXT NOT NULL, size INT, mtime_ns INT,
  inbound INT NOT NULL DEFAULT 0, instances INT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS data_files_rkey ON data_files(rkey);
CREATE INDEX IF NOT EXISTS data_files_inbound ON data_files(inbound DESC);
CREATE TABLE IF NOT EXISTS data_types(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, short TEXT NOT NULL,
  files INT NOT NULL DEFAULT 0, instances INT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS data_types_short ON data_types(short COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS data_inst(type_id INT, file_id INT, n INT, PRIMARY KEY(type_id, file_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS data_inst_file ON data_inst(file_id);
CREATE TABLE IF NOT EXISTS data_assets(id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, inbound INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS data_refs(asset_id INT, file_id INT, PRIMARY KEY(asset_id, file_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS data_refs_file ON data_refs(file_id);
"""

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_ident(word: str) -> list[str]:
    parts = [p for seg in word.split("_") for p in _CAMEL.findall(seg)]
    return [p.lower() for p in parts if len(p) > 1] if len(parts) > 1 else []


def fts_body(text: str) -> str:
    """Code + sub-tokens of compound identifiers, so 'spawn' finds SpawnEntityData and spawn_entity."""
    extra = {p for w in set(_IDENT.findall(text)) for p in split_ident(w)}
    return text + ("\n" + " ".join(sorted(extra)) if extra else "")


def rkey(path: str) -> str:
    """Reversed lowercase path without extension: suffix lookups become indexed prefix ranges."""
    p = path.replace("\\", "/").lower()
    if p.endswith(".xdata"):
        p = p[:-4]
    return p[::-1]


class Store:
    def __init__(self, path: Path, readonly: bool = False, cache_mb: int = 256):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self.path.as_posix()}?mode=ro" if readonly else f"file:{self.path.as_posix()}"
        self.db = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=30000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            if uml_schema.migrate(self.db) and self.meta("schema") is not None:
                self.db.execute("DELETE FROM meta WHERE key='uml_facts'")    # old rows: re-parse before UML views
            if self.meta("schema") != SCHEMA_VERSION:
                self.set_meta("schema", SCHEMA_VERSION)
        self.db.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
        self.db.execute("PRAGMA temp_store=MEMORY")

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def q(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()

    def meta(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    # ------------------------------------------------------------ code files
    def file_stats(self) -> dict[str, tuple[int, int, str]]:
        return {p: (s, m, h) for p, s, m, h in self.db.execute("SELECT path, size, mtime_ns, sha1 FROM files")}

    def _clear_file(self, db, file_id: int) -> None:
        ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
        db.executemany("DELETE FROM fts WHERE rowid=?", ((i,) for i in ids))
        db.executemany("DELETE FROM vecs WHERE chunk_id=?", ((i,) for i in ids))
        db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM edges WHERE file_id=?", (file_id,))

    def replace_file(self, path: str, size: int, mtime_ns: int, sha1: str, prio: float, r: FileResult) -> None:
        """Caller holds a transaction (tx()). Chunks start at tier 0 (lexical/graph only)."""
        db = self.db
        uml_schema.touch(db, path)
        row = db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        keep: dict[tuple, tuple[int, bytes]] = {}
        if row:
            fid = row[0]
            # unchanged chunks keep their vectors: a one-line edit re-embeds one chunk, not the file
            for sym, kind, parent, text, tier, v in db.execute(
                    "SELECT c.symbol, c.kind, c.parent, c.text, v.tier, v.v FROM chunks c JOIN vecs v ON v.chunk_id=c.id "
                    "WHERE c.file_id=?", (fid,)):
                keep[(sym, kind, parent, text)] = (tier, v)
            self._clear_file(db, fid)
            db.execute("UPDATE files SET lang=?, size=?, mtime_ns=?, sha1=?, prio=?, indexed_at=?, error=? WHERE id=?",
                       (r.lang, size, mtime_ns, sha1, prio, time.time(), r.error or None, fid))
        else:
            fid = db.execute("INSERT INTO files(path, lang, size, mtime_ns, sha1, prio, indexed_at, error) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (path, r.lang, size, mtime_ns, sha1, prio, time.time(), r.error or None)).lastrowid
        for c in r.chunks:
            cid = db.execute("INSERT INTO chunks(file_id, start_line, end_line, symbol, kind, parent, text, prio) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (fid, c.start_line, c.end_line, c.symbol, c.kind, c.parent, c.text, prio)).lastrowid
            old = keep.pop((c.symbol, c.kind, c.parent, c.text), None)
            if old:
                db.execute("UPDATE chunks SET tier=? WHERE id=?", (old[0], cid))
                db.execute("INSERT INTO vecs(chunk_id, tier, v) VALUES(?,?,?)", (cid, old[0], old[1]))
            db.execute("INSERT INTO fts(rowid, path, symbol, body) VALUES(?,?,?,?)",
                       (cid, path, c.symbol + " " + " ".join(split_ident(c.symbol.split(":")[-1].split(".")[-1])),
                        fts_body(c.text)))
        db.executemany("INSERT INTO symbols(file_id, name, qualname, kind, parent, start_line, end_line, signature, "
                       "visibility, mods, rtype, params, tparams, stereo, start_col, end_col) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ((fid, s.name, s.qualname, s.kind, s.parent, s.start_line, s.end_line, s.signature,
                         s.visibility or None, s.mods or None, s.rtype or None, s.params or None, s.tparams or None,
                         s.stereo or None, s.start_col if s.start_col >= 0 else None,
                         s.end_col if s.end_col >= 0 else None) for s in r.symbols))
        db.executemany("INSERT INTO edges(file_id, src, dst, kind, line, label, role, col) VALUES(?,?,?,?,?,?,?,?)",
                       ((fid, e.src, e.dst, e.kind, e.line, e.label or None, e.role or None,
                         e.col if e.col >= 0 else None) for e in r.edges))

    def touch_file(self, path: str, size: int, mtime_ns: int) -> None:
        """Content unchanged (same sha1) but stat changed, e.g. p4 sync of an identical revision."""
        self.db.execute("UPDATE files SET size=?, mtime_ns=? WHERE path=?", (size, mtime_ns, path))

    def delete_file(self, path: str) -> None:
        row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        if row:
            uml_schema.touch(self.db, path)
            self._clear_file(self.db, row[0])
            self.db.execute("DELETE FROM files WHERE id=?", (row[0],))

    def counts(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]
        tiers = dict(self.q("SELECT tier, COUNT(*) FROM chunks GROUP BY tier"))
        return {"files": one("SELECT COUNT(*) FROM files"), "chunks": sum(tiers.values()),
                "chunks_by_tier": {str(k): v for k, v in sorted(tiers.items())},
                "symbols": one("SELECT COUNT(*) FROM symbols"), "edges": one("SELECT COUNT(*) FROM edges"),
                "data_files": one("SELECT COUNT(*) FROM data_files"),
                "data_types": one("SELECT COUNT(*) FROM data_types")}
