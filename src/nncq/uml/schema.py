"""UML tables and the symbols/edges columns UML needs. No imports from nncq: store.py imports this.

symbols  + visibility (public|protected|private), mods ('static virtual pure override const decl ...'),
           rtype (return / field / variable type as written), params, tparams (template / generic params),
           stereo ('interface', 'abstract', 'singleton', 'dataclass', comma-joined),
           start_col / end_col (0-based columns of the definition span: with the lines, an exact source span)
edges    + role (member or variable name; for calls the receiver: 'm_conn', 'Foo::', 'self.dev'), col
           kinds besides call | inherits | include:
           composes | aggregates | associates   class -> member type (label = type as written)
           depends                              function -> parameter / return type
           instantiates | var                   function or global -> type it creates / holds by pointer
           import                               file or function -> module (Python)
components / comp_files / comp_deps / file_deps / build_configs   repo-level model (model.py, from build files
           and resolved #include / import edges)
views / view_refs / view_scope / view_dirty / view_render   stored views and what they depend on
"""
from __future__ import annotations

SYMBOL_COLUMNS = (("visibility", "TEXT"), ("mods", "TEXT"), ("rtype", "TEXT"), ("params", "TEXT"),
                  ("tparams", "TEXT"), ("stereo", "TEXT"), ("start_col", "INT"), ("end_col", "INT"))
EDGE_COLUMNS = (("role", "TEXT"), ("col", "INT"))

UML_SCHEMA = """
CREATE TABLE IF NOT EXISTS components(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT, dir TEXT, source TEXT, line INT, info TEXT);
CREATE INDEX IF NOT EXISTS components_name ON components(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS comp_files(file_id INTEGER PRIMARY KEY, comp_id INT NOT NULL);
CREATE INDEX IF NOT EXISTS comp_files_comp ON comp_files(comp_id);
CREATE TABLE IF NOT EXISTS comp_deps(src_id INT NOT NULL, dst TEXT NOT NULL, kind TEXT, scope TEXT);
CREATE INDEX IF NOT EXISTS comp_deps_src ON comp_deps(src_id);
CREATE TABLE IF NOT EXISTS file_deps(src_id INT NOT NULL, dst_id INT NOT NULL, kind TEXT NOT NULL, n INT,
  PRIMARY KEY(src_id, dst_id, kind)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS file_deps_dst ON file_deps(dst_id);
CREATE TABLE IF NOT EXISTS build_configs(name TEXT, kind TEXT, source TEXT, info TEXT);

CREATE TABLE IF NOT EXISTS views(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, title TEXT, origin TEXT NOT NULL,
  grp TEXT, spec TEXT, model TEXT, hash TEXT, stale INT NOT NULL DEFAULT 0, report TEXT,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS view_refs(view_id INT NOT NULL, path TEXT, ref TEXT, fp TEXT);
CREATE INDEX IF NOT EXISTS view_refs_path ON view_refs(path);
CREATE INDEX IF NOT EXISTS view_refs_view ON view_refs(view_id);
CREATE INDEX IF NOT EXISTS view_refs_ref ON view_refs(ref COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS view_scope(view_id INT NOT NULL, prefix TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS view_dirty(view_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS view_render(view_id INT NOT NULL, fmt TEXT NOT NULL, text TEXT, PRIMARY KEY(view_id, fmt));
"""


def migrate(db) -> bool:
    """Add missing columns/tables (schema 1 -> 2). True when columns were added (old rows lack UML facts)."""
    added = False
    for table, cols in (("symbols", SYMBOL_COLUMNS), ("edges", EDGE_COLUMNS)):
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for c, typ in cols:
            if c not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {c} {typ}")
                added = True
    db.executescript(UML_SCHEMA)
    return added


def touch(db, path: str) -> None:
    """A file was re-parsed or removed: views that depend on it get re-checked on next access, and their cached
    renders are dropped. Views elsewhere are not touched. Caller holds the write transaction."""
    if db.execute("SELECT 1 FROM views LIMIT 1").fetchone() is None:
        return
    db.execute("INSERT OR IGNORE INTO view_dirty(view_id) SELECT view_id FROM view_refs WHERE path=? "
               "UNION SELECT view_id FROM view_scope WHERE substr(?, 1, length(prefix))=prefix", (path, path))
    db.execute("DELETE FROM view_render WHERE view_id IN (SELECT view_id FROM view_dirty)")
