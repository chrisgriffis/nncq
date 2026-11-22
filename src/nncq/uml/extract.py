"""UML facts per file, collected on the chunker's tree-sitter walk (chunker._Ast with uml=True).

Adds symbols: member fields, C++ member declarations (methods declared in a class body, defined elsewhere:
mods 'decl'), enumerators (kind constant), globals of a user type (kind variable). Fills per symbol: visibility,
mods, rtype, params, tparams, stereo and the column span. Adds edges (see schema.py):
  composes | aggregates | associates   class -> member type   value / unique_ptr / optional: composes,
                                       shared_ptr: aggregates, raw pointer / reference / weak_ptr: associates
                                       Python: created in the class (self.x = Foo()) composes, passed in
                                       (self.x = param: Foo) aggregates, annotation only associates
  depends                              function -> parameter / return types
  instantiates | var                   function (or global) -> type of a value it creates / a pointer it holds
                                       (role = the variable: lets sequence diagrams type `x.run()`)
  import                               Python imports
and the receiver of every call as the call edge's role. Name-based like the rest of the graph.
"""
from __future__ import annotations

import re
from collections import defaultdict

from nncq import langs as L
from nncq.chunker import Edge, Symbol

C_LANGS = {"c", "cpp", "hlsl", "cuda", "objc"}
FUNCS = {"function", "method"}
MOD_WORDS = {"static", "virtual", "inline", "explicit", "constexpr", "consteval", "extern", "mutable", "override",
             "final", "abstract", "sealed", "async", "readonly", "volatile", "friend", "public", "private",
             "protected", "internal", "synchronized", "native", "open", "suspend", "const", "thread_local"}
_MOD_NODES = {"storage_class_specifier", "virtual_specifier", "function_specifier", "explicit_function_specifier",
              "modifiers", "modifier", "accessibility_modifier", "visibility_modifier", "virtual"}
PRIMITIVES = set("""void bool char wchar_t char8_t char16_t char32_t short int long float double signed unsigned auto
size_t ssize_t ptrdiff_t intptr_t uintptr_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t
string wstring string_view wstring_view u8string nullptr_t byte std nullptr decltype typename const volatile struct
class enum union template function any Any None NoneType str bytes bytearray object int float complex type bool
Callable Union Literal TypeVar Self self cls this var let number boolean undefined null never unknown symbol
String Object Integer Long Double Float Boolean Character Void dynamic""".split())
COMPOSE_WRAP = {"unique_ptr", "UniquePtr", "TUniquePtr", "scoped_ptr", "optional", "Optional", "array", "Array"}
AGGREGATE_WRAP = {"shared_ptr", "SharedPtr", "TSharedPtr", "RefPtr", "ComPtr", "intrusive_ptr", "Ref", "Rc", "Arc"}
ASSOC_WRAP = {"weak_ptr", "WeakPtr", "TWeakPtr", "observer_ptr", "reference_wrapper", "Weak", "WeakRef"}
MANY_WRAP = {"vector", "list", "deque", "set", "multiset", "map", "multimap", "unordered_map", "unordered_set",
             "unordered_multimap", "array", "span", "queue", "stack", "priority_queue", "forward_list", "Vector",
             "List", "Array", "TArray", "TMap", "TSet", "QList", "QVector", "QMap", "Dict", "dict", "Sequence",
             "Iterable", "Iterator", "Generator", "tuple", "Tuple", "Set", "FrozenSet", "frozenset", "Mapping",
             "MutableMapping", "MutableSequence", "DefaultDict", "defaultdict", "Deque", "ArrayList", "HashMap",
             "HashSet", "IList", "IEnumerable", "ICollection", "IDictionary", "Dictionary", "Collection", "Map"}
OPT_WRAP = {"optional", "Optional", "Nullable"}
WRAPPER_NAMES = COMPOSE_WRAP | AGGREGATE_WRAP | ASSOC_WRAP | MANY_WRAP | OPT_WRAP | {
    "pair", "variant", "atomic", "function", "Pair", "Variant", "Type", "ClassVar", "Final", "Annotated", "Awaitable",
    "Coroutine", "AsyncIterator", "Promise", "Task", "Future", "future", "promise", "allocator", "less", "hash"}
MAKERS = {"make_unique", "make_shared", "MakeUnique", "MakeShared", "MakeShareable", "NewObject", "allocate_shared"}
_TOKEN = re.compile(r"[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*")
_SINGLETON = re.compile(r"(?i)^(get_?)?(instance|singleton|shared\w*|default\w*|the\w+)$")


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _one(s: str, cap: int = 200) -> str:
    return re.sub(r"\s+", " ", s).strip()[:cap]


def type_names(written: str) -> list[str]:
    """User type names in a type as written: 'std::vector<std::shared_ptr<ns::Item>>' -> ['Item']."""
    out: list[str] = []
    for tok in _TOKEN.findall((written or "").replace('"', " ").replace("'", " ")):
        short = re.split(r"\s*::\s*", tok)[-1]
        if (len(short) < 2 or short in PRIMITIVES or short in WRAPPER_NAMES or short.isupper()
                or tok.startswith("std") and short.islower() or short in out):
            continue
        out.append(short)
    return out


def multiplicity(written: str) -> str:
    w = written or ""
    toks = set(re.findall(r"\w+", w))
    arr = any(t not in WRAPPER_NAMES for t in re.findall(r"(\w+)\s*\[", w))      # C array, not Optional[...]
    if toks & MANY_WRAP or arr:
        return "*"
    if toks & OPT_WRAP or "*" in w or toks & ASSOC_WRAP:
        return "0..1"
    return "1"


def relation(written: str) -> tuple[str, str]:
    """(composes | aggregates | associates, multiplicity) for a member declared with this type."""
    toks = set(re.findall(r"\w+", written or ""))
    if "*" in written or "&" in written or toks & ASSOC_WRAP:
        kind = "associates"
    elif toks & AGGREGATE_WRAP:
        kind = "aggregates"
    else:
        kind = "composes"
    return kind, multiplicity(written)


def _words(n) -> set[str]:
    out: set[str] = set()
    if n is None:
        return out
    for c in n.children:
        if not c.is_named and c.type in MOD_WORDS:
            out.add(c.type)
        elif c.type in _MOD_NODES:
            out.update(w for w in re.findall(r"\w+", _t(c)) if w in MOD_WORDS)
    return out


def _unwrap(d):
    """C/C++ declarator -> (function_declarator or None, pointer/ref marks, name node, array suffix)."""
    marks, arr = "", ""
    for _ in range(10):
        if d is None:
            break
        if d.type == "function_declarator":
            return d, marks, None, arr
        if d.type in ("field_identifier", "identifier", "destructor_name", "operator_name", "qualified_identifier",
                      "type_identifier", "operator_cast"):
            return None, marks, d, arr
        if d.type == "pointer_declarator":
            marks += "*"
        elif d.type == "reference_declarator":
            marks += "&&" if "&&" in _t(d)[:3] else "&"
        elif d.type == "array_declarator":
            arr += "[" + _t(d.child_by_field_name("size")) + "]"
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in d.named_children if c.type.endswith("declarator") or c.type in (
                "field_identifier", "identifier", "qualified_identifier", "destructor_name", "operator_name")), None)
        d = nxt
    return None, marks, None, arr


def _strip_parens(s: str) -> str:
    s = _one(s, 400)
    return s[1:-1].strip() if s[:1] == "(" and s[-1:] == ")" else s


def _vis_from_name(name: str) -> str:
    if name.startswith("__") and not name.endswith("__"):
        return "private"
    return "protected" if name.startswith("_") and not name.endswith("__") else "public"


class Extractor:
    def __init__(self, ast):
        self.a, self.lang, self.r, self.sep = ast, ast.lang, ast.r, ast.sep
        self.vis: dict[int, str] = {}                 # start byte of a class-body member -> access
        self.fields: dict[str, Symbol] = {}           # Python fields already seen (self.x assigned many times)
        self.ptypes: dict[str, dict[str, str]] = {}   # function qualname -> parameter -> type as written

    # ------------------------------------------------------------ definitions
    def on_def(self, n, span, sym: Symbol) -> None:
        sym.start_col, sym.end_col = span.start_point[1], n.end_point[1]
        if span.type == "template_declaration":
            sym.tparams = _strip_parens(_t(span.child_by_field_name("parameters")).strip("<>"))
        elif n.child_by_field_name("type_parameters") is not None:
            sym.tparams = _one(_t(n.child_by_field_name("type_parameters"))).strip("<>[]")
        if sym.kind in FUNCS:
            self._function(n, span, sym)
        elif sym.kind in L.CLASSLIKE_KINDS:
            self._class(n, span, sym)

    def _decorators(self, span) -> list[str]:
        if span is None or span.type != "decorated_definition":
            return []
        return [re.split(r"[(\s]", _t(c).lstrip("@").strip())[0] for c in span.named_children if c.type == "decorator"]

    def _function(self, n, span, sym: Symbol) -> None:
        mods = _words(n)
        rt, params_node = "", n.child_by_field_name("parameters")
        if self.lang in C_LANGS:
            fd, marks, _, _ = _unwrap(n.child_by_field_name("declarator"))
            t = n.child_by_field_name("type")
            if t is not None:
                quals = " ".join(_t(c) for c in n.children if c.type == "type_qualifier")
                rt = _one(f"{quals} {_t(t)}{marks}")
            if fd is not None:
                params_node = fd.child_by_field_name("parameters")
                mods |= _words(fd) | {"const" for c in fd.children if c.type == "type_qualifier" and _t(c) == "const"}
            mods |= {"default" for c in n.children if c.type == "default_method_clause"}
            mods |= {"delete" for c in n.children if c.type == "delete_method_clause"}
        elif self.lang == "python":
            rt = _one(_t(n.child_by_field_name("return_type"))).strip("\"'")
            for d in self._decorators(span):
                last = d.rsplit(".", 1)[-1]
                mods |= {"staticmethod": {"static"}, "classmethod": {"static", "classmethod"},
                         "property": {"property"}, "abstractmethod": {"abstract"}, "setter": {"property"},
                         "cached_property": {"property"}, "overload": {"overload"}}.get(last, set())
            if any(c.type == "async" for c in n.children):
                mods.add("async")
        else:
            for f in ("type", "return_type", "returns", "result"):
                rt = _one(_t(n.child_by_field_name(f))).lstrip(":").strip()
                if rt:
                    break
        cls_short = re.split(r"::|\.", sym.parent)[-1] if sym.parent else ""
        if sym.name == cls_short or sym.name in ("__init__", "constructor"):
            mods.add("ctor")
        elif sym.name.startswith("~"):
            mods.add("dtor")
        sym.rtype, sym.params = rt, _strip_parens(_t(params_node))
        acc = self.vis.get(span.start_byte, "") or next((w for w in ("public", "protected", "private", "internal")
                                                          if w in mods), "")
        if not acc and self.lang == "python" and sym.kind == "method":
            acc = _vis_from_name(sym.name)
        sym.visibility = acc
        sym.mods = " ".join(sorted(mods - {"public", "protected", "private", "internal"}))
        self._depends(sym, params_node, rt, n.start_point[0] + 1)

    def _params(self, params_node) -> list[tuple[str, str]]:
        """(name, type as written) per parameter."""
        out = []
        if params_node is None:
            return out
        for p in params_node.named_children:
            t = p.child_by_field_name("type")
            name = ""
            if self.lang in C_LANGS:
                _, marks, nn, arr = _unwrap(p.child_by_field_name("declarator"))
                quals = " ".join(_t(c) for c in p.children if c.type == "type_qualifier")
                written = _one(f"{quals} {_t(t)}{marks}{arr}") if t is not None else ""
                name = _t(nn)
            else:
                written = _one(_t(t)).lstrip(":").strip()
                nn = p.child_by_field_name("name") or p.child_by_field_name("pattern") or next(
                    (c for c in p.named_children if c.type == "identifier"), None)
                name = _t(nn) if p.type != "identifier" else _t(p)
            out.append((name, written))
        return out

    def _depends(self, sym: Symbol, params_node, rt: str, line: int) -> None:
        ps = self._params(params_node)
        if self.lang == "python":
            self.ptypes[sym.qualname] = {n: w for n, w in ps if n and w}
        own = {re.split(r"::|\.", sym.parent)[-1]} if sym.parent else set()
        own |= {x.strip().split()[-1] for x in sym.tparams.split(",") if x.strip()}
        seen: set[str] = set()
        for role, written in [("return", rt)] + ps:
            for tn in type_names(written):
                if tn not in own and tn not in seen:
                    seen.add(tn)
                    self.r.edges.append(Edge(sym.qualname, tn, "depends", line, written[:200], role or "param"))

    def _class(self, n, span, sym: Symbol) -> None:
        mods = _words(n)
        st = [s for s in sym.stereo.split(",") if s]
        if self.lang == "python":
            st += [d for d in self._decorators(span) if d.rsplit(".", 1)[-1] == "dataclass"][:1]
        if "abstract" in mods:
            st.append("abstract")
        sym.stereo = ",".join(dict.fromkeys(st))
        sym.mods = " ".join(sorted(mods - {"public", "protected", "private", "internal"}))
        sym.visibility = self.vis.get(span.start_byte, "") or next(
            (w for w in ("public", "protected", "private", "internal") if w in mods), "")
        body = self.a._body(n)
        if body is None:
            return
        if self.lang in C_LANGS:
            if n.type == "enum_specifier":
                self._enumerators(body, sym)
            else:
                self._cpp_members(n, body, sym)
        elif self.lang != "python":
            self._generic_members(body, sym)

    def _enumerators(self, body, cls: Symbol) -> None:
        for e in body.named_children[:300]:
            if e.type == "enumerator":
                nm = _t(e.child_by_field_name("name"))
                if nm:
                    self.r.symbols.append(Symbol(nm, cls.qualname + self.sep + nm, "constant", cls.qualname,
                                                 e.start_point[0] + 1, e.end_point[0] + 1, _one(_t(e)), "public",
                                                 start_col=e.start_point[1], end_col=e.end_point[1]))

    def _cpp_members(self, n, body, cls: Symbol) -> None:
        acc = "private" if n.type == "class_specifier" else "public"
        for c in body.children:
            if c.type == "access_specifier":
                acc = _t(c).strip()
                continue
            if not c.is_named:
                continue
            self.vis[c.start_byte] = acc
            inner, tp = c, ""
            if c.type == "template_declaration":
                tp = _strip_parens(_t(c.child_by_field_name("parameters")).strip("<>"))
                inner = next((x for x in c.named_children if x.type in (
                    "field_declaration", "declaration", "function_definition")), c)
                self.vis[inner.start_byte] = acc
            if inner.type in ("field_declaration", "declaration"):
                self._cpp_member(inner, c, cls, acc, tp)

    def _cpp_member(self, d, wrap, cls: Symbol, acc: str, tp: str) -> None:
        t = d.child_by_field_name("type")
        if t is not None and t.type in ("class_specifier", "struct_specifier", "union_specifier", "enum_specifier"):
            base = _t(t.child_by_field_name("name"))
        else:
            base = _t(t) if t is not None else ""
        quals = " ".join(_t(c) for c in d.children if c.type == "type_qualifier")
        base = _one(f"{quals} {base}")
        mods = _words(d)
        line, sig = wrap.start_point[0] + 1, _one(_t(d))
        for dec in d.children_by_field_name("declarator"):
            fd, marks, nn, arr = _unwrap(dec)
            if fd is not None:
                name = _t(fd.child_by_field_name("declarator")).split("::")[-1].strip()
                if not name:
                    continue
                m = set(mods) | _words(fd) | {"decl"}
                m |= {"const" for c in fd.children if c.type == "type_qualifier" and _t(c) == "const"}
                if _t(d.child_by_field_name("default_value")).strip() == "0":
                    m |= {"pure", "abstract"}
                m |= {"default" for c in d.children if c.type == "default_method_clause"}
                m |= {"delete" for c in d.children if c.type == "delete_method_clause"}
                if name == cls.name:
                    m.add("ctor")
                elif name.startswith("~"):
                    m.add("dtor")
                s = Symbol(name, cls.qualname + self.sep + name, "method", cls.qualname, line, d.end_point[0] + 1,
                           sig, acc, " ".join(sorted(m - {"public", "protected", "private"})),
                           _one(base + marks) if base else "", _strip_parens(_t(fd.child_by_field_name("parameters"))),
                           tp, start_col=wrap.start_point[1], end_col=d.end_point[1])
                self.r.symbols.append(s)
                self._depends(s, fd.child_by_field_name("parameters"), s.rtype, line)
            elif nn is not None:
                name = _t(nn)
                written = _one(base + marks + arr)
                s = Symbol(name, cls.qualname + self.sep + name, "field", cls.qualname, line, d.end_point[0] + 1, sig,
                           acc, " ".join(sorted(mods - {"virtual", "public", "protected", "private"})), written,
                           start_col=wrap.start_point[1], end_col=d.end_point[1])
                self.r.symbols.append(s)
                kind, _ = relation(written)
                for tn in type_names(written):
                    if tn != cls.name:
                        self.r.edges.append(Edge(cls.qualname, tn, kind, line, written, name, nn.start_point[1]))

    def _generic_members(self, body, cls: Symbol) -> None:
        for c in body.named_children:
            if c.type not in ("field_declaration", "public_field_definition", "field_definition",
                              "event_field_declaration", "property_signature"):
                continue
            mods = _words(c)
            vd = next((x for x in c.named_children if x.type == "variable_declaration"), c)
            t = vd.child_by_field_name("type")
            written = _one(_t(t)).lstrip(":").strip()
            names = [x.child_by_field_name("name") or next((y for y in x.named_children if y.type in (
                "identifier", "property_identifier")), None)
                for x in vd.named_children if x.type == "variable_declarator"]
            if not names:
                names = [c.child_by_field_name("name") or c.child_by_field_name("property")]
            acc = next((w for w in ("public", "protected", "private", "internal") if w in mods), "")
            for nn in names:
                name = _t(nn)
                if not name:
                    continue
                self.r.symbols.append(Symbol(name, cls.qualname + self.sep + name, "field", cls.qualname,
                                             c.start_point[0] + 1, c.end_point[0] + 1, _one(_t(c)), acc,
                                             " ".join(sorted(mods - {"public", "protected", "private", "internal"})),
                                             written, start_col=c.start_point[1], end_col=c.end_point[1]))
                kind, _ = relation(written)
                for tn in type_names(written):
                    if tn != cls.name:
                        self.r.edges.append(Edge(cls.qualname, tn, kind, c.start_point[0] + 1, written, name))

    # ------------------------------------------------------------ calls
    def on_call(self, n, encl: str, callee: str) -> str:
        line, col = n.start_point[0] + 1, n.start_point[1]
        if n.type in ("new_expression", "object_creation_expression"):
            t = n.child_by_field_name("type") or n.child_by_field_name("constructor")
            tn = type_names(_t(t))
            if tn:
                self.r.edges.append(Edge(encl, tn[0], "instantiates", line, _one(_t(t)), self._binding(n), col))
            return ""
        if callee in MAKERS:
            fn = n.child_by_field_name("function")
            args = self._find(fn, "template_argument_list")
            tn = type_names(_t(args))
            if tn:
                self.r.edges.append(Edge(encl, tn[0], "instantiates", line, _one(_t(args)).strip("<>"),
                                         self._binding(n), col))
        return self.receiver(n)

    def _find(self, n, typ, depth=0):
        if n is None or depth > 4:
            return None
        if n.type == typ:
            return n
        for c in n.named_children:
            r = self._find(c, typ, depth + 1)
            if r is not None:
                return r
        return None

    def receiver(self, n) -> str:
        fn = n.child_by_field_name("function")
        obj = n.child_by_field_name("object")
        if obj is None and fn is not None:
            if fn.type in ("qualified_identifier", "scoped_identifier"):
                sc = fn.child_by_field_name("scope") or fn.child_by_field_name("path")
                s = _one(_t(sc))
                return s + "::" if s and re.fullmatch(r"[\w:]+", s) else ""
            for f in ("argument", "object", "expression", "operand", "value"):
                obj = fn.child_by_field_name(f)
                if obj is not None:
                    break
        if obj is None:
            return ""
        s = re.sub(r"\s+", "", _t(obj))
        if s in ("this", "self", "(*this)", "*this"):
            return "this"
        s = re.sub(r"^(this|self)(->|\.)", "", s)
        return s if len(s) <= 60 and re.fullmatch(r"[\w.:]+(?:->[\w.:]+)*", s) else ""

    def _binding(self, n) -> str:
        p = n.parent
        while p is not None and p.type in ("parenthesized_expression", "await"):
            p = p.parent
        if p is None:
            return ""
        if p.type == "init_declarator":
            _, _, nn, _ = _unwrap(p.child_by_field_name("declarator"))
            return _t(nn)
        if p.type in ("assignment_expression", "assignment", "variable_declarator"):
            left = p.child_by_field_name("left") or p.child_by_field_name("name")
            s = re.sub(r"^(this|self)(->|\.)", "", _t(left).strip())
            return s if re.fullmatch(r"[\w.]+", s or "-") else ""
        return ""

    # ------------------------------------------------------------ other nodes
    def on_node(self, n, encl: str, encl_kind: str) -> None:
        if self.lang in C_LANGS:
            if n.type == "declaration" and encl_kind not in L.CLASSLIKE_KINDS:
                self._cpp_declaration(n, encl, encl_kind)
        elif self.lang == "python":
            if n.type == "import_statement":
                for c in n.children_by_field_name("name"):
                    mod = _t(c.child_by_field_name("name") if c.type == "aliased_import" else c)
                    self.r.edges.append(Edge(encl, mod, "import", n.start_point[0] + 1, _one(_t(n))))
            elif n.type == "import_from_statement":
                mod = _t(n.child_by_field_name("module_name"))
                if mod:
                    self.r.edges.append(Edge(encl, mod, "import", n.start_point[0] + 1, _one(_t(n))))
            elif n.type == "assignment":
                self._py_assign(n, encl, encl_kind)

    def _cpp_declaration(self, n, encl: str, encl_kind: str) -> None:
        t = n.child_by_field_name("type")
        if t is None:
            return
        if t.type in ("class_specifier", "struct_specifier", "union_specifier", "enum_specifier"):
            base = _t(t.child_by_field_name("name"))
        else:
            base = _t(t)
        if base in ("auto", "decltype(auto)"):
            return
        quals = " ".join(_t(c) for c in n.children if c.type == "type_qualifier")
        base = _one(f"{quals} {base}")
        mods = _words(n)
        line = n.start_point[0] + 1
        for dec in n.children_by_field_name("declarator"):
            fd, marks, nn, arr = _unwrap(dec)
            if fd is not None or nn is None:
                continue
            written = _one(base + marks + arr)
            tns = type_names(written)
            if not tns:
                continue
            name = _t(nn)
            if encl_kind in FUNCS:
                self.r.edges.append(Edge(encl, tns[0], "var" if marks else "instantiates", line, written,
                                         ("static " if "static" in mods else "") + name, nn.start_point[1]))
            else:
                q = self.sep.join(x for x in (encl, name) if x)
                self.r.symbols.append(Symbol(name, q, "variable", encl, line, n.end_point[0] + 1, _one(_t(n)), "",
                                             " ".join(sorted(mods)), written, start_col=n.start_point[1],
                                             end_col=n.end_point[1]))
                if not marks:
                    self.r.edges.append(Edge(q, tns[0], "instantiates", line, written, name, nn.start_point[1]))

    def _py_assign(self, n, encl: str, encl_kind: str) -> None:
        left, right, ann = (n.child_by_field_name(f) for f in ("left", "right", "type"))
        if left is None:
            return
        created = ""
        if right is not None and right.type == "call":
            callee = _t(right.child_by_field_name("function")).rsplit(".", 1)[-1]
            if callee[:1].isupper() and not callee.isupper() and re.fullmatch(r"\w+", callee):
                created = callee
        written = _one(_t(ann)).strip("\"'") if ann is not None else created
        line = n.start_point[0] + 1
        if left.type == "attribute" and _t(left.child_by_field_name("object")) == "self" and encl_kind == "method":
            rel = "composes" if created and not ann else None
            if not written and right is not None and right.type == "identifier":
                written = self.ptypes.get(encl, {}).get(_t(right), "")
                rel = "aggregates" if written else None
            self._py_field(encl.rsplit(".", 1)[0], _t(left.child_by_field_name("attribute")), written, n, rel)
        elif left.type == "identifier" and encl_kind in L.CLASSLIKE_KINDS:
            self._py_field(encl, _t(left), written, n, "composes" if created and not ann else None, static=ann is None)
        elif left.type == "identifier" and encl_kind in FUNCS:
            if created:
                self.r.edges.append(Edge(encl, created, "instantiates", line, created, _t(left), n.start_point[1]))
        elif left.type == "identifier" and written and encl_kind not in FUNCS:
            name = _t(left)
            q = self.sep.join(x for x in (encl, name) if x)
            self.r.symbols.append(Symbol(name, q, "variable", encl, line, n.end_point[0] + 1, _one(_t(n)),
                                         _vis_from_name(name), "", written, start_col=n.start_point[1],
                                         end_col=n.end_point[1]))
            if created:
                self.r.edges.append(Edge(q, created, "instantiates", line, created, name, n.start_point[1]))

    def _py_field(self, cls: str, name: str, written: str, n, rel: str | None, static: bool = False) -> None:
        if not name or not cls:
            return
        q = f"{cls}.{name}"
        s = self.fields.get(q)
        if s is None:
            s = self.fields[q] = Symbol(name, q, "field", cls, n.start_point[0] + 1, n.end_point[0] + 1, _one(_t(n)),
                                        _vis_from_name(name), "static" if static else "", "",
                                        start_col=n.start_point[1], end_col=n.end_point[1])
            self.r.symbols.append(s)
        if not written or s.rtype:
            return
        s.rtype = written
        kind = rel or relation(written)[0].replace("composes", "associates")
        for tn in type_names(written):
            if tn != cls.rsplit(".", 1)[-1]:
                self.r.edges.append(Edge(cls, tn, kind, n.start_point[0] + 1, written, name, n.start_point[1]))

    # ------------------------------------------------------------ whole file
    def finish(self) -> None:
        """Stereotypes that need all members: interface / abstract (pure virtuals, ABC, Protocol), singleton."""
        members: dict[str, list[Symbol]] = defaultdict(list)
        for s in self.r.symbols:
            if s.parent:
                members[s.parent].append(s)
        bases: dict[str, set[str]] = defaultdict(set)
        statics: dict[str, set[str]] = defaultdict(set)      # function -> types it holds in a static local
        for e in self.r.edges:
            if e.kind == "inherits":
                bases[e.src].add(e.dst)
            elif e.kind == "instantiates" and e.role.startswith("static "):
                statics[e.src].add(e.dst)
        for c in self.r.symbols:
            if c.kind not in L.CLASSLIKE_KINDS or c.kind == "enum":
                continue
            ms = members.get(c.qualname, [])
            mods = lambda m: set(m.mods.split())
            meths = [m for m in ms if m.kind == "method" and not mods(m) & {"ctor", "dtor"}]
            pure = [m for m in meths if mods(m) & {"pure", "abstract"}]
            data = [m for m in ms if m.kind == "field" and "static" not in mods(m)]
            st = [s for s in c.stereo.split(",") if s]
            bs = bases.get(c.qualname, set())
            if c.kind != "interface" and ("Protocol" in bs or (pure and len(pure) == len(meths) and not data
                                                                and self.lang in C_LANGS)):
                st.append("interface")
            elif pure or bs & {"ABC", "ABCMeta"}:
                st.append("abstract")
            if any(("static" in mods(m) and ((m.kind == "method" and _SINGLETON.match(m.name)
                                              and (c.name in m.rtype or not m.rtype))
                                             or (m.kind == "field" and (c.name in type_names(m.rtype)
                                                                        or m.name.lstrip("_").lower() in ("instance", "singleton")))))
                   or c.name in statics.get(m.qualname, ()) for m in ms):
                st.append("singleton")
            c.stereo = ",".join(dict.fromkeys(st))
