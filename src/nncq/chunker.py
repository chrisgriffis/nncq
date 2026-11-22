"""Split a file into retrieval chunks and extract its symbols and graph edges.

Three strategies, picked by language:
  ast    tree-sitter: chunks at function/class boundaries, containers too big for one chunk are split
         into a header chunk + one chunk per member; symbols for every definition; call edges.
  dsl    C-like definition languages declared by a codebase profile ([[dsl]] in profile.toml): brace-matched
         blocks at the declaration keywords the profile lists (class/struct/enum/service/...). With a
         module_keyword, `module X;` qualifies the declarations that follow (X.Foo).
  text   everything else: line windows (markdown: by heading).

Data files covered by a profile's [[xref]] rules are not chunked; datarefs.py cross-references them.
Pure python + tree-sitter, no torch: runs in worker processes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from nncq import langs as L


@dataclass
class Chunk:
    start_line: int          # 1-based, inclusive
    end_line: int
    symbol: str              # qualified name of the enclosing definition ('' at file level)
    kind: str
    parent: str
    text: str


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    parent: str
    start_line: int
    end_line: int
    signature: str
    visibility: str = ""     # UML facts (uml/extract.py, when parsed with uml=True)
    mods: str = ""
    rtype: str = ""
    params: str = ""
    tparams: str = ""
    stereo: str = ""
    start_col: int = -1      # -1 = unknown
    end_col: int = -1


@dataclass
class Edge:
    src: str                 # qualname of the source symbol ('' = file level)
    dst: str                 # call: callee identifier; inherits: base short name; include: basename (lower)
    kind: str                # call | inherits | include (+ UML kinds, see uml/schema.py)
    line: int
    label: str = ""          # inherits: base as written; include: path as written
    role: str = ""           # UML: member / variable name; call: receiver
    col: int = -1


@dataclass
class FileResult:
    lang: str
    strategy: str
    chunks: list[Chunk] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    error: str = ""


_WORD = re.compile(r"\w{2,}")
_parsers: dict = {}
_BASE_CLAUSES = {"base_class_clause", "base_list", "superclass", "super_interfaces", "class_heritage",
                 "extends_clause", "implements_clause", "extends_type_clause", "delegation_specifiers",
                 "delegation_specifier", "inheritance_specifier", "type_inheritance_clause",
                 "base_clause", "class_interface_clause"}
_TYPEISH = {"type_identifier", "identifier", "qualified_identifier", "qualified_type_identifier",
            "scoped_type_identifier", "generic_name", "generic_type", "template_type", "qualified_name",
            "attribute", "user_type", "member_expression", "scoped_identifier", "nested_type_identifier",
            "simple_identifier", "constant", "scope_resolution", "name"}


def _parser(lang: str):
    p = _parsers.get(lang)
    if p is None:
        import tree_sitter_language_pack as tslp
        from nncq import config
        tslp.configure(tslp.PackConfig(cache_dir=str(config.TS_CACHE)))
        p = _parsers[lang] = tslp.get_parser(lang)
    return p


_PP_LANGS = {"c", "cpp", "hlsl", "csharp", "objc", "cuda"}
_PP_COND = re.compile(rb"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_PP_ZERO = re.compile(rb"^(?:0|false)\b")
_NOT_CR = re.compile(rb"[^\r]")


def flatten_pp(data: bytes) -> tuple[bytes, set[int]]:
    """Keep one branch of every #if/#elif/#else (the first, or the #else of `#if 0`), blank the rest and the
    directives themselves, as ctags does. Bytes and line numbers are unchanged; only the parser sees this.
    Without it, two alternative function headers or an unbalanced brace inside #if branches turns a whole
    C++ file into one tree-sitter ERROR node: wrong extents, lost symbols, calls attributed to file level.
    Returns the parser input and the 0-based rows of code that was blanked (the branches not taken)."""
    lines = data.split(b"\n")
    skipped: set[int] = set()
    out: list[bytes] = []
    stack: list[list[bool]] = []          # [enclosing branch active, a branch of this #if was taken]
    active, i = True, 0
    while i < len(lines):
        m = _PP_COND.match(lines[i])
        if not m:
            if active:
                out.append(lines[i])
            else:
                out.append(_NOT_CR.sub(b" ", lines[i]))
                skipped.add(i)
            i += 1
            continue
        j = i
        while lines[j].rstrip(b"\r").endswith(b"\\") and j + 1 < len(lines):
            j += 1
        kw = m.group(1)
        if kw in (b"if", b"ifdef", b"ifndef"):
            take = not (kw == b"if" and _PP_ZERO.match(m.group(2).strip()))
            stack.append([active, take])
            active = active and take
        elif kw in (b"elif", b"else") and stack:
            outer, taken = stack[-1]
            active = outer and not taken
            stack[-1][1] = taken or active
        elif kw == b"endif" and stack:
            active = stack.pop()[0]
        out.extend(_NOT_CR.sub(b" ", lines[k]) for k in range(i, j + 1))
        i = j + 1
    return b"\n".join(out), skipped


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def process(path: str, data: bytes, lang: str, max_chars: int = 3000, dsl: dict | None = None,
            uml: bool = False) -> FileResult:
    """dsl: the profile's spec for this language (profiles.Dsl.spec()) when it is a brace-block DSL.
    uml: also record the UML facts (members, types, stereotypes, call receivers) on the same walk."""
    text = decode(data)
    lines = text.splitlines(keepends=True) or [""]
    try:
        if dsl is not None:
            return _dsl(path, lines, lang, max_chars, dsl)
        if L.is_ast_lang(lang):
            return _Ast(path, data, lines, lang, max_chars, uml).run()
    except Exception as e:  # a parser failure must never lose the file: fall back to windows
        r = _text(lines, lang, max_chars)
        r.error = f"{type(e).__name__}: {e}"
        return r
    return _text(lines, lang, max_chars)


# ---------------------------------------------------------------- line windows

def windows(lines: list[str], s: int, e: int, max_chars: int, overlap: int = 2) -> list[tuple[int, int]]:
    """0-based inclusive row ranges covering s..e, each at most ~max_chars."""
    out, start, size = [], s, 0
    for i in range(s, e + 1):
        size += len(lines[i])
        if size > max_chars and i > start:
            out.append((start, i - 1))
            start = max(start + 1, i - overlap)
            size = sum(len(lines[j]) for j in range(start, i + 1))
    out.append((start, e))
    return out


def _mk(lines, s, e, symbol, kind, parent) -> Chunk:
    return Chunk(s + 1, e + 1, symbol, kind, parent, "".join(lines[s:e + 1]))


def _text(lines: list[str], lang: str, max_chars: int) -> FileResult:
    r = FileResult(lang, "text")
    if lang == "markdown":
        heads = [i for i, ln in enumerate(lines) if ln.startswith("#")] or [0]
        if heads[0] != 0:
            heads.insert(0, 0)
        heads.append(len(lines))
        for a, b in zip(heads, heads[1:]):
            title = lines[a].lstrip("#").strip() if lines[a].startswith("#") else ""
            for s, e in windows(lines, a, b - 1, max_chars):
                c = _mk(lines, s, e, title, "section", "")
                if _WORD.search(c.text):
                    r.chunks.append(c)
        return r
    for s, e in windows(lines, 0, len(lines) - 1, max_chars):
        c = _mk(lines, s, e, "", "text", "")
        if _WORD.search(c.text):
            r.chunks.append(c)
    return r


# ---------------------------------------------------------------- tree-sitter

def _txt(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


@dataclass
class _Def:
    node: object             # the definition node itself
    span: object             # node whose range the chunk covers (wrapper, e.g. template/export)
    name: str
    kind: str
    scope: list[str]         # extra qualifiers from the name itself (C++ Foo::Bar::baz)
    body: object


class _Ast:
    def __init__(self, path, data, lines, lang, max_chars, uml=False):
        self.path, self.data, self.lines, self.lang, self.max = path, data, lines, lang, max_chars
        self.sep = L.QUAL_SEP.get(lang, ".")
        self.r = FileResult(lang, "ast")
        self.uml = uml
        if uml:
            from nncq.uml.extract import Extractor
        self.x = Extractor(self) if uml else None

    def run(self) -> FileResult:
        data, skipped = flatten_pp(self.data) if self.lang in _PP_LANGS and b"#" in self.data else (self.data, set())
        tree = _parser(self.lang).parse(data)
        root = tree.root_node
        self._chunk_scope(list(root.children), [], "", "file", None)
        self._collect(root)
        if skipped:
            self._recover(skipped)
        if self.x is not None:
            self.x.finish()
        if not self.r.chunks:
            self.r.chunks = _text(self.lines, self.lang, self.max).chunks
        return self.r

    def _recover(self, skipped: set[int]) -> None:
        """Definitions and calls that only exist in the #if branches flatten_pp dropped (other platforms):
        taken from a parse of the original text and attached to the enclosing definition of the main parse."""
        other = _Ast(self.path, self.data, self.lines, self.lang, self.max, self.uml)
        other._collect(_parser(self.lang).parse(self.data).root_node)
        have = {(s.name, s.start_line) for s in self.r.symbols}
        scopes = [(s.start_line, s.end_line, s.qualname) for s in self.r.symbols]

        def enclosing(line: int) -> str:
            best = min(((e - a, q) for a, e, q in scopes if a <= line <= e), default=None)
            return best[1] if best else ""
        added = []
        for s in other.r.symbols:
            if s.start_line - 1 in skipped and (s.name, s.start_line) not in have:
                parent = enclosing(s.start_line)
                q = self.sep.join(x for x in (parent, s.name) if x)
                added.append(replace(s, qualname=q, parent=parent))
        self.r.symbols += added
        scopes += [(s.start_line, s.end_line, s.qualname) for s in added]
        self.r.edges += [replace(e, src=enclosing(e.line)) for e in other.r.edges if e.line - 1 in skipped]

    # -- definitions
    def def_info(self, n) -> _Def | None:
        t = n.type
        kind = None
        name_node = None
        if t in L.FUNC_TYPES:
            kind = "function"
            name_node = n.child_by_field_name("name")
            if name_node is None:
                name_node = self._declarator_name(n.child_by_field_name("declarator"))
        elif t in L.CLASS_KINDS:
            if t in L.NEEDS_BODY and self._body(n) is None:
                return None
            kind = L.CLASS_KINDS[t]
            if t == "impl_item":
                name_node = n.child_by_field_name("type")
            else:
                name_node = n.child_by_field_name("name")
        elif t in L.OTHER_KINDS:
            kind = L.OTHER_KINDS[t]
            if t == "type_declaration":
                spec = next((c for c in n.named_children if c.type in ("type_spec", "type_alias")), None)
                name_node = spec.child_by_field_name("name") if spec is not None else None
            else:
                name_node = n.child_by_field_name("name")
        elif t == "variable_declarator":
            v = n.child_by_field_name("value")
            if v is None or v.type not in L.FN_VALUES:
                return None
            kind = "class" if v.type == "class" else "function"
            name_node = n.child_by_field_name("name")
        elif t == "assignment_statement" and self.lang == "lua":
            # M.spawn = function(...) end  /  local Foo = {}   (first target only)
            vl = next((c for c in n.named_children if c.type == "variable_list"), None)
            el = next((c for c in n.named_children if c.type == "expression_list"), None)
            v = el.named_children[0] if el is not None and el.named_child_count else None
            if vl is None or not vl.named_child_count or v is None:
                return None
            if v.type == "function_definition":
                kind = "function"
            elif v.type == "table_constructor" and v.end_point[0] > v.start_point[0]:
                kind = "table"
            else:
                return None
            name_node = vl.named_children[0]
        else:
            return None
        if name_node is None:
            if kind == "namespace":
                name = "(anonymous)"
            else:
                return None
        else:
            name = re.sub(r"\s+", " ", re.sub(r"\s*::\s*", "::", _txt(name_node).strip()))
        if not name or len(name) > 200:
            return None
        scope: list[str] = []
        if "::" in name and self.lang in ("cpp", "c", "hlsl", "rust"):
            parts = [p for p in name.split("::") if p]
            name, scope = parts[-1], parts[:-1]
        elif self.lang == "lua" and re.search(r"[.:]", name):
            parts = [p for p in re.split(r"[.:]", name) if p]
            if not parts or not all(re.fullmatch(r"\w+", p) for p in parts):
                return None
            name, scope = parts[-1], parts[:-1]
        return _Def(n, n, name, kind, scope, self._body(n))

    def _body(self, n):
        b = n.child_by_field_name("body")
        if b is None:
            b = next((c for c in n.named_children if c.type in L.BODY_TYPES), None)
        return b

    def _declarator_name(self, d):
        depth = 0
        while d is not None and d.type in L.DECLARATOR_WRAPPERS and depth < 8:
            d = d.child_by_field_name("declarator")
            depth += 1
        return d

    def as_def(self, n) -> _Def | None:
        d = self.def_info(n)
        if d is not None:
            return d
        if n.type in L.WRAPPERS:
            for c in n.named_children:
                inner = self.as_def(c)
                if inner is not None:
                    inner.span = n
                    return inner
        return None

    def _has_def(self, n, depth=0) -> bool:
        if depth > 3:
            return False
        for c in n.named_children:
            if self.def_info(c) is not None or self._has_def(c, depth + 1):
                return True
        return False

    # -- chunking
    def _flatten(self, children):
        for ch in children:
            d = self.as_def(ch)
            if d is not None:
                yield ("def", ch, d)
            elif ch.named_child_count and ch.end_point[0] > ch.start_point[0] and self._has_def(ch):
                yield from self._flatten(list(ch.children))
            elif ch.is_named:                     # bare `{` `}` `;` tokens would duplicate their whole line
                yield ("gap", ch, None)

    def _chunk_scope(self, children, parents, ctx_sym, ctx_kind, header):
        gap: list[tuple[int, int, bool]] = []
        if header:
            gap.append((header[0], header[1], False))
        parent = self.sep.join(parents)
        for what, node, d in self._flatten(children):
            s, e = node.start_point[0], node.end_point[0]
            if node.end_point[1] == 0 and e > s:          # preprocessor nodes end at column 0 of the next line
                e -= 1
            if what == "gap":
                gap.append((s, e, node.type in ("comment", "line_comment", "block_comment")))
                continue
            start = s
            while gap and gap[-1][2] and gap[-1][1] >= start - 1:   # leading doc comments
                start = gap.pop()[0]
            self._flush_gap(gap, ctx_sym, ctx_kind, parent)
            gap = []
            self._emit_def(d, start, e, parents)
        self._flush_gap(gap, ctx_sym, ctx_kind, parent)

    def _flush_gap(self, gap, sym, kind, parent):
        if not gap:
            return
        s = min(g[0] for g in gap)
        e = max(g[1] for g in gap)
        for a, b in windows(self.lines, s, e, self.max):
            c = _mk(self.lines, a, b, sym, kind if sym else "module", parent if sym else "")
            if _WORD.search(c.text):
                self.r.chunks.append(c)

    def _emit_def(self, d: _Def, start: int, end: int, parents: list[str]):
        names = parents + d.scope + [d.name]
        qual = self.sep.join(names)
        parent = self.sep.join(parents + d.scope)
        size = sum(len(self.lines[i]) for i in range(start, min(end, len(self.lines) - 1) + 1))
        scope_only = d.kind in ("namespace", "module") and d.body is not None and self._has_def(d.body)
        if size <= self.max and not scope_only:
            self.r.chunks.append(_mk(self.lines, start, end, qual, d.kind, parent))
            return
        if d.kind in L.CONTAINER_KINDS and d.body is not None:
            hb = d.body.start_point[0]
            self._chunk_scope(list(d.body.children), names, qual, d.kind, None if scope_only else (start, hb))
            return
        for a, b in windows(self.lines, start, end, self.max):
            self.r.chunks.append(_mk(self.lines, a, b, qual, d.kind, parent))

    def _signature(self, row: int) -> str:
        """First line of the definition; joins following lines while it is only a return type / template
        header / attribute (`void` on its own line, then `Foo::bar(...)`)."""
        out = []
        for i in range(row, min(row + 4, len(self.lines))):
            s = self.lines[i].strip()
            out.append(s)
            if "(" in s or "{" in s or ":" in s.replace("::", "") or len(" ".join(out)) > 120:
                break
        return " ".join(x for x in out if x)[:200]

    # -- symbols + call edges over the whole tree
    def _collect(self, root):
        stack = [(root, (), "", "")]   # node, parent names, enclosing qualname, enclosing kind
        while stack:
            n, parents, encl, encl_kind = stack.pop()
            d = self.def_info(n) if n.is_named else None
            if d is not None:
                names = parents + tuple(d.scope) + (d.name,)
                kind = d.kind
                if kind == "function" and (encl_kind in L.CLASSLIKE_KINDS or d.scope):
                    kind = "method"
                qual = self.sep.join(names)
                span = n.parent if n.parent is not None and n.parent.type in L.WRAPPERS else n
                sig = self._signature(n.start_point[0])
                sym = Symbol(d.name, qual, kind, self.sep.join(names[:-1]), span.start_point[0] + 1,
                             n.end_point[0] + 1, sig)
                self.r.symbols.append(sym)
                parents, encl, encl_kind = names, qual, kind
                if kind in L.CLASSLIKE_KINDS:
                    for short, full, line in self._bases(n):
                        self.r.edges.append(Edge(qual, short, "inherits", line, full))
                if self.x is not None:
                    self.x.on_def(n, span, sym)
            elif n.type in L.CALL_TYPES:
                callee = self._callee(n)
                if callee:
                    self.r.edges.append(Edge(encl, callee, "call", n.start_point[0] + 1,
                                             role=self.x.on_call(n, encl, callee) if self.x is not None else "",
                                             col=n.start_point[1] if self.x is not None else -1))
            elif n.type == "preproc_include":
                p = n.child_by_field_name("path")
                inc = _txt(p).strip().strip('"<>').replace("\\", "/") if p is not None else ""
                if inc:
                    self.r.edges.append(Edge("", inc.rsplit("/", 1)[-1].lower(), "include",
                                             n.start_point[0] + 1, inc))
            elif self.x is not None:
                self.x.on_node(n, encl, encl_kind)
            for c in reversed(n.children):
                if c.named_child_count or c.is_named:
                    stack.append((c, parents, encl, encl_kind))

    def _bases(self, n) -> list[tuple[str, str, int]]:
        """(short name, as written, line) of the base types in a class-like definition."""
        out: list[tuple[str, str, int]] = []
        clauses = [c for c in n.children if c.type in _BASE_CLAUSES]
        for f in ("superclasses", "superclass", "interfaces"):
            c = n.child_by_field_name(f)
            if c is not None and c not in clauses:
                clauses.append(c)
        for c in clauses:
            self._base_names(c, out, 0)
        return out

    def _base_names(self, n, out, depth) -> None:
        for c in n.named_children:
            if c.type in _TYPEISH:
                full = _txt(c).strip()
                short = re.split(r"::|\.", re.sub(r"<.*", "", full, flags=re.S))[-1].strip()
                if re.fullmatch(r"\w+", short or ""):
                    out.append((short, full[:200], c.start_point[0] + 1))
            elif c.type != "keyword_argument" and depth < 4:
                self._base_names(c, out, depth + 1)

    def _callee(self, n) -> str:
        fn = None
        for f in L.CALLEE_FIELDS:
            fn = n.child_by_field_name(f)
            if fn is not None:
                break
        if fn is None:
            named = n.named_children
            fn = named[0] if named else None
        return self._ident(fn, 0)

    def _ident(self, n, depth) -> str:
        if n is None or depth > 6:
            return ""
        if n.type in L.IDENT_TYPES or (n.is_named and n.named_child_count == 0 and n.child_count == 0):
            t = _txt(n)
            return t if len(t) <= 120 and re.match(r"^[\w~$@]+$", t) else ""
        for f in ("name", "field", "property", "attribute", "method"):
            c = n.child_by_field_name(f)
            if c is not None:
                return self._ident(c, depth + 1)
        named = n.named_children
        if not named:
            return ""
        if n.type in ("template_function", "template_method", "generic_name", "generic_function"):
            return self._ident(named[0], depth + 1)
        return self._ident(named[-1], depth + 1)


# ---------------------------------------------------------------- brace-block definition languages (profile DSLs)

_DSL_RX: dict[tuple, re.Pattern] = {}


def _dsl_decl(keywords) -> re.Pattern:
    key = tuple(keywords)
    rx = _DSL_RX.get(key)
    if rx is None:
        rx = _DSL_RX[key] = re.compile(
            r"^[ \t]*(?:\[[^\]]*\][ \t]*)*(?:@\w+[ \t]*)*"
            r"(?:[a-z]+[ \t]+)*?"
            r"(" + "|".join(re.escape(k) for k in key) + r")[ \t]+([\w.:]+)(.*)$")
    return rx


def _strip_code(line: str, in_block: bool) -> tuple[str, bool]:
    """Line with comments and string literals blanked (for brace counting)."""
    out, i, n = [], 0, len(line)
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                return "".join(out), True
            i, in_block = j + 2, False
            continue
        ch = line[i]
        if line.startswith("//", i):
            break
        if line.startswith("/*", i):
            in_block, i = True, i + 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == "\\" else 1
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block


def _dsl(path: str, lines: list[str], lang: str, max_chars: int, spec: dict) -> FileResult:
    r = FileResult(lang, "dsl")
    decl = _dsl_decl(spec["keywords"])
    containers = set(spec["containers"])
    module_kw = spec.get("module_keyword") or ""
    clean, blk = [], False
    for ln in lines:
        c, blk = _strip_code(ln, blk)
        clean.append(c)

    def block_end(start: int) -> int:
        depth, seen = 0, False
        for i in range(start, len(clean)):
            for ch in clean[i]:
                if ch == "{":
                    depth, seen = depth + 1, True
                elif ch == "}":
                    depth -= 1
                    if seen and depth == 0:
                        return i
            if not seen and i > start and clean[i].strip().endswith(";"):
                return i
            if not seen and i - start > 3:
                return start
        return len(clean) - 1

    module: list[str] = []      # `module X;` (spec module_keyword) qualifies the types that follow

    def scope(s: int, e: int, parents: list[str], ctx_sym: str, ctx_kind: str):
        gap_s = s
        i = s
        while i <= e:
            m = decl.match(lines[i])
            if not m or clean[i].strip() == "":
                i += 1
                continue
            kw, name = m.group(1), m.group(2)
            if kw in ("module", "namespace", module_kw) and clean[i].rstrip().endswith(";"):
                if module_kw and kw == module_kw:
                    module[:] = [name]
                i += 1
                continue
            end = min(block_end(i), e)
            start = i
            while start > gap_s and (lines[start - 1].lstrip().startswith(("//", "[", "@", "*", "/*"))):
                start -= 1
            _gap(gap_s, start - 1, ctx_sym, ctx_kind, parents)
            qual = ".".join((parents or module) + [name])
            r.symbols.append(Symbol(name.split(".")[-1], qual, kw, ".".join(parents), start + 1, end + 1,
                                    lines[i].strip()[:200]))
            base = re.match(r"\s*:\s*([\w.:]+)", m.group(3) or "")
            if base:
                full = base.group(1)
                r.edges.append(Edge(qual, re.split(r"::|\.", full)[-1], "inherits", i + 1, full))
            size = sum(len(lines[k]) for k in range(start, end + 1))
            if size > max_chars and kw in containers and end > i:
                body = next((k for k in range(i, end + 1) if "{" in clean[k]), i)
                r.chunks.append(_mk(lines, start, body, qual, kw, ".".join(parents)))
                scope(body + 1, end, parents + [name], qual, kw)
            else:
                for a, b in windows(lines, start, end, max_chars):
                    r.chunks.append(_mk(lines, a, b, qual, kw, ".".join(parents)))
            gap_s = i = end + 1
        _gap(gap_s, e, ctx_sym, ctx_kind, parents)

    def _gap(s, e, sym, kind, parents):
        if e < s:
            return
        for a, b in windows(lines, s, e, max_chars):
            c = _mk(lines, a, b, sym, kind if sym else "module", ".".join(parents[:-1]) if sym else "")
            if _WORD.search(c.text):
                r.chunks.append(c)

    scope(0, len(lines) - 1, [], "", "file")
    return r
