"""Split source files into meaningful chunks (functions, classes, methods) using tree-sitter,
with a heuristic fallback for languages tree-sitter can't parse."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

EXT_LANG = {
    ".py": "python", ".php": "php", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".go": "go",
    ".rb": "ruby", ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".kt": "kotlin", ".swift": "swift", ".scala": "scala", ".sh": "bash",
    ".bash": "bash", ".sql": "sql", ".vue": "vue", ".dart": "dart", ".lua": "lua",
    ".html": "html", ".css": "css", ".scss": "scss", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".toml": "toml", ".md": "markdown", ".xml": "xml", ".ini": "ini",
    ".env": "dotenv", ".tf": "hcl", ".gradle": "groovy", ".blade.php": "php",
}
SPECIAL_NAMES = {"Dockerfile": "dockerfile", "Makefile": "make", ".env": "dotenv", "docker-compose.yml": "yaml"}

# Languages we chunk structurally with tree-sitter.
TS_LANGS = {"python", "php", "javascript", "typescript", "tsx", "java", "go", "ruby", "rust", "c", "cpp",
            "csharp", "kotlin", "scala", "swift", "bash", "dart", "lua"}

FUNC_TYPES = {
    "function_definition", "function_declaration", "method_declaration", "method_definition",
    "function_item", "method", "singleton_method", "constructor_declaration", "function_signature",
    "arrow_function", "generator_function_declaration", "func_literal", "local_function",
}
CLASS_TYPES = {
    "class_definition", "class_declaration", "interface_declaration", "trait_declaration",
    "enum_declaration", "struct_item", "impl_item", "trait_item", "enum_item", "class",
    "module", "struct_specifier", "class_specifier", "object_declaration", "type_declaration",
    "abstract_class_declaration", "record_declaration",
}
MAX_CHUNK_LINES = 120
SMALL_CLASS_LINES = 60


@dataclass
class Chunk:
    symbol: str
    kind: str
    start_line: int  # 1-based inclusive
    end_line: int
    content: str


def detect_language(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    if name.startswith(".env"):
        return "dotenv"
    if name.endswith(".blade.php"):
        return "php"
    if "." not in name:
        return None
    return EXT_LANG.get("." + name.rsplit(".", 1)[-1].lower())


_parsers: dict[str, object] = {}


def _parser(lang: str):
    if lang in _parsers:
        return _parsers[lang]
    try:
        from tree_sitter_language_pack import get_parser

        p = get_parser(lang)  # type: ignore[arg-type]
    except Exception as e:  # pragma: no cover
        log.warning("no tree-sitter grammar for %s (%s); using heuristic chunking", lang, str(e)[:120])
        p = None
    _parsers[lang] = p
    return p


def _node_name(node, src: bytes) -> str | None:
    n = node.child_by_field_name("name")
    if n is None and node.type == "arrow_function":
        parent = node.parent
        if parent is not None and parent.type in ("variable_declarator", "pair", "assignment_expression", "public_field_definition"):
            n = parent.child_by_field_name("name") or parent.child_by_field_name("key") or parent.child_by_field_name("left")
    if n is None and node.type in ("function_definition", "function_declaration"):
        d = node.child_by_field_name("declarator")
        while d is not None and d.child_by_field_name("declarator") is not None:
            d = d.child_by_field_name("declarator")
        n = d
    if n is None and node.type == "impl_item":
        n = node.child_by_field_name("type")
    if n is None:
        return None
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")[:120]


def _window(lines: list[str], start: int, end: int, symbol: str, kind: str) -> list[Chunk]:
    """Split [start,end] (0-based inclusive) into windows of MAX_CHUNK_LINES."""
    out = []
    s = start
    part = 1
    while s <= end:
        e = min(end, s + MAX_CHUNK_LINES - 1)
        sym = symbol if (s == start and e == end) else f"{symbol} (part {part})"
        out.append(Chunk(sym, kind, s + 1, e + 1, "\n".join(lines[s:e + 1])))
        s = e + 1 - 10 if e < end else e + 1  # 10-line overlap
        part += 1
    return out


def chunk_tree_sitter(text: str, lang: str) -> list[Chunk] | None:
    parser = _parser(lang)
    if parser is None:
        return None
    src = text.encode("utf-8", "replace")
    try:
        tree = parser.parse(src)
    except Exception:
        return None
    lines = text.split("\n")
    chunks: list[Chunk] = []
    covered = [False] * len(lines)

    def mark(a: int, b: int) -> None:
        for i in range(a, min(b + 1, len(covered))):
            covered[i] = True

    def visit(node, owner: str | None) -> None:
        t = node.type
        if not node.is_named:  # keyword tokens such as PHP's `class`
            return
        if t in CLASS_TYPES:
            name = _node_name(node, src) or t
            full = f"{owner}::{name}" if owner else name
            a, b = node.start_point[0], node.end_point[0]
            if b - a + 1 <= SMALL_CLASS_LINES:
                chunks.append(Chunk(full, "class", a + 1, b + 1, "\n".join(lines[a:b + 1])))
                mark(a, b)
                return
            # Large class: header chunk + recurse into members.
            first_child_line = b
            for c in node.children:
                if c.type in ("declaration_list", "class_body", "block", "body", "field_declaration_list"):
                    for m in c.children:
                        if m.type in FUNC_TYPES or m.type in CLASS_TYPES:
                            first_child_line = min(first_child_line, m.start_point[0])
            head_end = max(a, min(first_child_line - 1, a + 40))
            chunks.append(Chunk(full, "class", a + 1, head_end + 1, "\n".join(lines[a:head_end + 1])))
            mark(a, head_end)
            for c in node.children:
                visit(c, full)
            return
        if t in FUNC_TYPES:
            name = _node_name(node, src)
            if t == "arrow_function" and name is None:
                for c in node.children:
                    visit(c, owner)
                return
            name = name or "<anonymous>"
            full = f"{owner}::{name}" if owner else name
            a, b = node.start_point[0], node.end_point[0]
            # include leading comments / decorators / docblocks
            lead = 0
            while (a > 0 and lead < 15 and not covered[a - 1]
                   and lines[a - 1].strip().startswith(("@", "#[", "/**", "/*", "*", "*/", "///", "\"\"\""))):
                a -= 1
                lead += 1
            chunks.extend(_window(lines, a, b, full, "method" if owner else "function"))
            mark(a, b)
            return
        for c in node.children:
            visit(c, owner)

    for top in tree.root_node.children:  # never treat the root (e.g. Python's `module`) as a class
        visit(top, None)
    chunks.extend(_gap_chunks(lines, covered))
    chunks.sort(key=lambda c: c.start_line)
    return chunks


def _gap_chunks(lines: list[str], covered: list[bool]) -> list[Chunk]:
    out: list[Chunk] = []
    i = 0
    n = len(lines)
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not covered[j + 1]:
            j += 1
        body = "\n".join(lines[i:j + 1])
        if body.strip() and len(body.strip()) > 20:
            out.extend(_window(lines, i, j, "<module>", "module"))
        i = j + 1
    return out


_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public|private|protected|static|async|final|abstract|\s)*"
    r"(?:def|function|func|fn|class|interface|trait|struct|enum|sub|proc)\s+([A-Za-z_][\w:]*)"
)


def chunk_fallback(text: str) -> list[Chunk]:
    lines = text.split("\n")
    if not lines:
        return []
    starts: list[tuple[int, str]] = []
    for i, l in enumerate(lines):
        m = _SYMBOL_RE.match(l)
        if m:
            starts.append((i, m.group(1)))
    chunks: list[Chunk] = []
    if not starts:
        return _window(lines, 0, len(lines) - 1, "<file>", "file") if text.strip() else []
    if starts[0][0] > 0:
        chunks.extend(_window(lines, 0, starts[0][0] - 1, "<module>", "module"))
    for k, (s, name) in enumerate(starts):
        e = (starts[k + 1][0] - 1) if k + 1 < len(starts) else len(lines) - 1
        chunks.extend(_window(lines, s, e, name, "block"))
    return [c for c in chunks if c.content.strip()]


def chunk_file(text: str, lang: str | None) -> list[Chunk]:
    if lang in TS_LANGS:
        res = chunk_tree_sitter(text, lang)
        if res is not None:
            return [c for c in res if c.content.strip()]
    return chunk_fallback(text)
