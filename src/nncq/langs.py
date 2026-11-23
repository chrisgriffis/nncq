"""File extension -> language, and the tree-sitter node types the chunker treats as definitions."""
from __future__ import annotations

from pathlib import PurePosixPath

AST_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".java": "java", ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift", ".scala": "scala", ".lua": "lua",
    ".sh": "bash", ".bash": "bash", ".ps1": "powershell", ".psm1": "powershell",
    ".c": "c", ".h": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".inl": "cpp", ".ipp": "cpp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hlsl": "hlsl", ".hlsli": "hlsl", ".fx": "hlsl", ".fxh": "hlsl",
    ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl", ".comp": "glsl",
}
TEXT_LANG_BY_EXT = {
    ".md": "markdown", ".rst": "text", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".xml": "xml", ".csproj": "xml", ".props": "xml", ".targets": "xml", ".vcxproj": "xml",
    ".cmake": "cmake", ".sql": "sql", ".bat": "batch", ".cmd": "batch", ".proto": "proto",
    ".gradle": "groovy",
}
FILENAME_LANG = {"CMakeLists.txt": "cmake", "Makefile": "make", "Dockerfile": "dockerfile"}


def build_ext_map(extra: str = "", more: dict[str, str] | None = None) -> dict[str, str]:
    """All indexed extensions. extra: '.foo,.bar=cpp' (no '=lang' -> plain text chunking); more: from a profile."""
    m = {**TEXT_LANG_BY_EXT, **AST_LANG_BY_EXT, **(more or {})}
    for item in filter(None, (x.strip() for x in extra.split(","))):
        ext, _, lang = item.partition("=")
        ext = ext if ext.startswith(".") else "." + ext
        m[ext.lower()] = lang.strip() or "text"
    return m


def lang_for(path: str, ext_map: dict[str, str]) -> str | None:
    p = PurePosixPath(path)
    if p.name in FILENAME_LANG:
        return FILENAME_LANG[p.name]
    return ext_map.get(p.suffix.lower())


def is_ast_lang(lang: str) -> bool:
    return lang in set(AST_LANG_BY_EXT.values())


QUAL_SEP = {"cpp": "::", "c": "::", "rust": "::", "hlsl": "::", "php": "\\"}

FUNC_TYPES = {
    "function_definition", "function_declaration", "function_item", "method_definition",
    "method_declaration", "constructor_declaration", "destructor_declaration", "operator_declaration",
    "conversion_operator_declaration", "generator_function_declaration", "method", "singleton_method",
    "local_function_statement", "function_signature_item", "secondary_constructor",
}
# def node type -> kind. Container kinds are split into header + members when too big for one chunk.
CLASS_KINDS = {
    "class_definition": "class", "class_declaration": "class", "abstract_class_declaration": "class",
    "class_specifier": "class", "class": "class", "object_declaration": "class",
    "struct_specifier": "struct", "struct_declaration": "struct", "struct_item": "struct",
    "record_declaration": "record", "record_struct_declaration": "record",
    "union_specifier": "union", "union_item": "union",
    "interface_declaration": "interface", "protocol_declaration": "interface", "trait_item": "trait",
    "trait_declaration": "trait", "annotation_type_declaration": "interface",
    "enum_declaration": "enum", "enum_specifier": "enum", "enum_item": "enum",
    "impl_item": "impl", "module": "module",
    "namespace_definition": "namespace", "namespace_declaration": "namespace",
    "file_scoped_namespace_declaration": "namespace", "internal_module": "namespace", "mod_item": "module",
}
OTHER_KINDS = {
    "property_declaration": "property", "type_alias_declaration": "type", "type_item": "type",
    "type_declaration": "type", "macro_definition": "macro", "delegate_declaration": "delegate",
    "preproc_function_def": "macro",
}
CONTAINER_KINDS = {"class", "struct", "record", "union", "interface", "trait", "enum", "impl",
                   "module", "namespace"}
CLASSLIKE_KINDS = CONTAINER_KINDS - {"namespace", "module"}
# C/C++ specifiers without a body are type references, not definitions.
NEEDS_BODY = {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier",
              "struct_item", "union_item", "enum_item", "mod_item"}
BODY_TYPES = {"body_statement", "class_body", "declaration_list", "field_declaration_list", "block",
              "enum_body", "interface_body", "statement_block", "enum_variant_list",
              "field_declaration_list", "enumerator_list", "object_body", "protocol_body"}
WRAPPERS = {"template_declaration", "export_statement", "decorated_definition", "ambient_declaration",
            "lexical_declaration", "variable_declaration", "attributed_statement"}
FN_VALUES = {"arrow_function", "function_expression", "function", "class", "generator_function"}

CALL_TYPES = {
    "call", "call_expression", "method_invocation", "invocation_expression", "object_creation_expression",
    "new_expression", "function_call_expression", "member_call_expression", "scoped_call_expression",
    "nullsafe_member_call_expression", "macro_invocation", "method_call_expression", "function_call",
}
CALLEE_FIELDS = ("function", "method", "constructor", "macro", "name", "type")
IDENT_TYPES = {
    "identifier", "field_identifier", "property_identifier", "type_identifier", "constant",
    "simple_identifier", "name", "namespace_identifier", "destructor_name", "operator_name",
    "shorthand_property_identifier", "private_property_identifier", "scoped_identifier_name",
}
DECLARATOR_WRAPPERS = {"function_declarator", "pointer_declarator", "reference_declarator",
                       "parenthesized_declarator", "attributed_declarator", "array_declarator"}
