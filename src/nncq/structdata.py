"""Structured data files (XML / JSON / YAML) as symbols, chunks and references, via tree-sitter.

Opt-in: only when the formats are kept (NNCQ_TEXT_LANGS or a profile's text_langs list xml / json / yaml).

  symbols  JSON / YAML keys (`key`), XML elements (`element`), list items named by a name/id/key field (`item`);
           qualified by their path: build.targets.ThermalTest
  chunks   along that structure (a key too big for one chunk: its header, then its children)
  edges    ref      a value (or XML tag) shaped like a code name: Foo::Bar, pkg.mod.Cls, ThermalTest, run_suite
                    -> callers of that name list the data that names it (any language)
           include  a value shaped like a file path (src/a.cpp, ..\\x.h) -> file_context includes / included_by
Plain words and prose are not references. Data symbols are not types and never resolve calls (graph.DATA_KINDS).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nncq import langs as L
from nncq.chunker import Chunk, Edge, FileResult, Symbol, _parser, windows

MAX_SYMBOLS = 3000
MAX_DEPTH = 12
MAX_REFS = 5000
NAME_KEYS = {"name", "id", "key", "title", "label"}
_IDENT = re.compile(r"^[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*$")
_PART = re.compile(r"[A-Za-z_][\w-]*")
_SEP = re.compile(r"::|\.|\\")
_VAR = re.compile(r"^(?:\$\([^)]*\)|\$\{[^}]*\}|%[^%]*%)[\\/]*")
_FILE = re.compile(r"^[\w.+-]+\.([A-Za-z][\w+]{0,7})$")
_WORD = re.compile(r"\w{2,}")
_EXTS = {e.lstrip(".").lower() for e in (*L.AST_LANG_BY_EXT, *L.TEXT_LANG_BY_EXT)} | {"c", "h", "dll", "lib", "so"}


@dataclass
class _N:
    name: str
    kind: str
    s: int                      # 0-based rows
    e: int
    kids: list["_N"] = field(default_factory=list)
    refs: list[tuple[str, str, int, str]] = field(default_factory=list)    # (dst, kind, line, label)


def _clean(name: str) -> str:
    """A key as a qualname part: the qualname separators (. :: \\) would split it."""
    return _SEP.sub("_", name.strip())[:120]


def refs_of(value: str, line: int) -> list[tuple[str, str, int, str]]:
    """References in one scalar value: [(dst, kind, line, label)]."""
    v = value.strip().strip("\"'")
    if not 3 <= len(v) <= 300:
        return []
    if re.search(r"[;,]", v):
        return [r for part in re.split(r"[;,]", v) if part.strip() for r in refs_of(part, line)]
    if re.search(r"\s", v) or "://" in v or "*" in v:
        return []
    p = re.sub(r"/{2,}", "/", _VAR.sub("", v).replace("\\", "/"))
    base = p.rsplit("/", 1)[-1]
    m = _FILE.match(base)
    if m and (m.group(1).lower() in _EXTS or "/" in p):
        return [(base.lower(), "include", line, p)]
    if _IDENT.match(v):
        last = _SEP.split(v)[-1]
        if len(last) >= 3 and ("::" in v or "." in v or "_" in last.strip("_") or re.search(r"[a-z][A-Z]", last)):
            return [(last, "ref", line, v)]
    return []


def _code_shaped(name: str) -> bool:
    return bool(_IDENT.match(name)) and len(name) >= 3 and bool("_" in name.strip("_") or re.search(r"[a-z][A-Z]", name))


# ------------------------------------------------------------------ per format: node -> _N trees

def _rows(n) -> tuple[int, int]:
    s, e = n.start_point[0], n.end_point[0]
    if e > s and n.end_point[1] == 0:
        e -= 1
    return s, e


class _Build:
    def __init__(self):
        self.count = 0

    def node(self, name: str, kind: str, n, depth: int) -> _N | None:
        if depth > MAX_DEPTH or self.count >= MAX_SYMBOLS or not name:
            return None
        self.count += 1
        s, e = _rows(n)
        return _N(_clean(name), kind, s, e)


def _text(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


# JSON

def _json_str(n) -> str | None:
    if n is None or n.type != "string":
        return None
    return "".join(_text(c) for c in n.named_children) if n.named_children else ""


def _json(n, into: _N, b: _Build, depth: int) -> None:
    if n is None:
        return
    if n.type == "document":
        for c in n.named_children:
            _json(c, into, b, depth)
    elif n.type == "object":
        for p in n.named_children:
            if p.type != "pair":
                continue
            k, v = _json_str(p.child_by_field_name("key")), p.child_by_field_name("value")
            me = b.node(k or "", "key", p, depth)
            if me is not None:
                into.kids.append(me)
            _json(v, me or into, b, depth + 1)
    elif n.type == "array":
        for it in n.named_children:
            nm = ""
            if it.type == "object":
                for p in it.named_children:
                    if p.type == "pair" and (_json_str(p.child_by_field_name("key")) or "").lower() in NAME_KEYS:
                        nm = _json_str(p.child_by_field_name("value")) or ""
                        break
            me = b.node(nm, "item", it, depth) if nm else None
            if me is not None:
                into.kids.append(me)
            _json(it, me or into, b, depth + 1)
    elif n.type == "string":
        into.refs += refs_of(_json_str(n) or "", n.start_point[0] + 1)


# YAML

_Y_WRAP = {"stream", "document", "block_node", "flow_node"}
_Y_SCALAR = {"plain_scalar", "double_quote_scalar", "single_quote_scalar"}


def _y_unwrap(n):
    while n is not None and n.type in _Y_WRAP:
        n = next((c for c in n.named_children if c.type not in ("tag", "anchor", "comment")), None)
    return n


def _y_scalar(n) -> str | None:
    n = _y_unwrap(n)
    return _text(n).strip().strip("\"'") if n is not None and n.type in _Y_SCALAR else None


def _yaml(n, into: _N, b: _Build, depth: int) -> None:
    n = _y_unwrap(n)
    if n is None:
        return
    if n.type in ("block_mapping", "flow_mapping"):
        for p in n.named_children:
            if p.type not in ("block_mapping_pair", "flow_pair"):
                continue
            k = _y_scalar(p.child_by_field_name("key"))
            me = b.node(k or "", "key", p, depth)
            if me is not None:
                into.kids.append(me)
            _yaml(p.child_by_field_name("value"), me or into, b, depth + 1)
    elif n.type in ("block_sequence", "flow_sequence"):
        for it in n.named_children:
            body = _y_unwrap(it.named_children[0]) if it.type == "block_sequence_item" and it.named_children else _y_unwrap(it)
            nm = ""
            if body is not None and body.type in ("block_mapping", "flow_mapping"):
                for p in body.named_children:
                    if (_y_scalar(p.child_by_field_name("key")) or "").lower() in NAME_KEYS:
                        nm = _y_scalar(p.child_by_field_name("value")) or ""
                        break
            me = b.node(nm, "item", it, depth) if nm else None
            if me is not None:
                into.kids.append(me)
            _yaml(body, me or into, b, depth + 1)
    elif n.type in _Y_SCALAR:
        into.refs += refs_of(_text(n), n.start_point[0] + 1)


# XML

def _xml(n, into: _N, b: _Build, depth: int) -> None:
    for c in n.named_children:
        if c.type == "element":
            tag = next((t for t in c.named_children if t.type in ("STag", "EmptyElemTag")), None)
            tname = next((_text(x) for x in tag.named_children if x.type == "Name"), "") if tag is not None else ""
            attrs = []
            for a in (tag.named_children if tag is not None else []):
                if a.type == "Attribute":
                    an = next((_text(x) for x in a.named_children if x.type == "Name"), "")
                    av = next((_text(x).strip("\"'") for x in a.named_children if x.type == "AttValue"), "")
                    attrs.append((an, av, a.start_point[0] + 1))
            label = next((v for k, v, _ in attrs if k.lower() in NAME_KEYS and _IDENT.match(v) and not _SEP.search(v)), "")
            me = b.node(label or tname, "element", c, depth)
            tgt = me or into
            if me is not None:
                into.kids.append(me)
            if _code_shaped(tname):
                tgt.refs.append((tname, "ref", c.start_point[0] + 1, tname))
            for _, v, ln in attrs:
                tgt.refs += refs_of(v, ln)
            body = next((x for x in c.named_children if x.type == "content"), None)
            if body is not None:
                _xml(body, tgt, b, depth + 1)
        elif c.type == "CharData":
            raw = _text(c)
            t = raw.strip()
            if t:
                lead = raw[:len(raw) - len(raw.lstrip())]
                into.refs += refs_of(t, c.start_point[0] + 1 + lead.count("\n"))
        elif c.type in ("content", "prolog"):
            _xml(c, into, b, depth)


_BUILDERS = {"json": _json, "yaml": _yaml, "xml": _xml}


# ------------------------------------------------------------------ _N trees -> FileResult

def parse(path: str, data: bytes, lines: list[str], lang: str, max_chars: int) -> FileResult:
    r = FileResult(lang, "data")
    tree = _parser(lang).parse(data)
    root = _N("", "file", 0, len(lines) - 1)
    b = _Build()
    if lang == "xml":
        _xml(tree.root_node, root, b, 0)
    else:
        _BUILDERS[lang](tree.root_node, root, b, 0)
    seen: set[tuple[str, str, str]] = set()

    def emit(n: _N, parents: list[str]):
        qual = ".".join(parents + [n.name]) if n.name else ""
        if n.name:
            r.symbols.append(Symbol(n.name, qual, n.kind, ".".join(parents), n.s + 1, n.e + 1,
                                    lines[n.s].strip()[:200] if n.s < len(lines) else ""))
        for dst, kind, line, label in n.refs:
            k = (qual, dst, kind)
            if k not in seen and len(seen) < MAX_REFS:
                seen.add(k)
                r.edges.append(Edge(qual, dst, kind, line, label))
        kp = parents + [n.name] if n.name else parents
        for c in n.kids:
            emit(c, kp)

    emit(root, [])

    def chunks(n: _N, s: int, e: int, parents: list[str]):
        qual = ".".join(parents + [n.name]) if n.name else ""
        kind, par = (n.kind, ".".join(parents)) if n.name else ("module", "")
        size = sum(len(lines[i]) for i in range(s, e + 1))
        if size <= max_chars or not n.kids:
            gap(s, e, qual, kind, par)
            return
        kp = parents + [n.name] if n.name else parents
        cur = s
        for c in n.kids:
            if c.s < cur or c.e > e:
                continue
            gap(cur, c.s - 1, qual, kind, par)
            chunks(c, c.s, c.e, kp)
            cur = c.e + 1
        gap(cur, e, qual, kind, par)

    def gap(s, e, sym, kind, par):
        if e < s:
            return
        for a, z in windows(lines, s, e, max_chars):
            c = Chunk(a + 1, z + 1, sym, kind, par, "".join(lines[a:z + 1]))
            if _WORD.search(c.text):
                r.chunks.append(c)

    chunks(root, 0, len(lines) - 1, [])
    return r
