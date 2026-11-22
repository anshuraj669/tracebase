"""Top-level blocks (functions, classes, constants) of Python / JS / TS files, so only the parts of a file that
are actually needed get copied: `from models.db_models import User` copies User (and what User uses),
not every model in that file."""
from __future__ import annotations

import re

PY_TOP = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)|^(\w+)\s*(?::[^=\n]+)?=(?!=)", re.M)
JS_TOP = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+(\w+)|class\s+(\w+)|(?:const|let|var)\s+(\w+)|"
    r"(?:interface|type|enum)\s+(\w+))", re.M)


def _js_end(text: str, start: int) -> int:
    """End of a JS/TS top-level statement starting at `start` (brace/paren aware, strings skipped)."""
    i, n, depth = start, len(text), 0
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            q = ch
            i += 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 1
        elif ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth == 0 and ch == "}":
                j = i + 1
                while j < n and text[j] in " \t":
                    j += 1
                if j >= n or text[j] in "\n;":
                    return j + 1 if j < n and text[j] == ";" else j
        elif ch == ";" and depth == 0:
            return i + 1
        elif ch == "\n" and depth == 0 and i > start:
            nxt = text[i + 1:i + 2]
            if nxt and nxt not in " \t\n.?:)]}" and not text[start:i].rstrip().endswith(("=", "=>", ",", "(", "+", "&&", "||")):
                return i
        i += 1
    return n


def _leading(text: str, start: int) -> int:
    """Include decorators / docblocks / comments directly above a block."""
    lines = text[:start].split("\n")[:-1]
    k = len(lines)
    while k and (lines[k - 1].strip().startswith(("@", "#", "//", "*", "/*", "/**")) or lines[k - 1].strip().endswith("*/")):
        k -= 1
    return len("\n".join(lines[:k])) + (1 if k else 0)


def top_level(text: str, lang: str) -> list[tuple[str, int, int]]:
    """[(name, start, end)] of top-level definitions, in file order."""
    out: list[tuple[str, int, int]] = []
    if lang == "python":
        starts = [(m.group(1) or m.group(2), m.start()) for m in PY_TOP.finditer(text)]
        lines_end = len(text)
        for idx, (name, s) in enumerate(starts):
            # block ends where the next non-indented, non-comment line starts
            m = re.compile(r"^(?=[^\s)\]}])", re.M)
            e = lines_end
            for mm in m.finditer(text, text.find("\n", s) + 1 if text.find("\n", s) >= 0 else lines_end):
                e = mm.start()
                break
            out.append((name, _leading(text, s), e))
        return out
    if lang in ("javascript", "typescript", "tsx", "jsx"):
        for m in JS_TOP.finditer(text):
            name = next(g for g in m.groups() if g)
            out.append((name, _leading(text, m.start()), _js_end(text, m.start())))
    return out


def keep_only(text: str, lang: str, names: set[str], refs: list[str] | None = None) -> str:
    """Remove top-level definitions that are not in `names` (or don't mention one of `refs`), keeping
    everything they use from the same file. Imports and other module-level code stay."""
    blocks = top_level(text, lang)
    if not blocks:
        return text
    by_name = {n: (s, e) for n, s, e in blocks}
    want = {n for n in names if n in by_name}
    if refs:
        want |= {n for n, s, e in blocks if any(re.search(rf"\b{re.escape(r)}\b", text[s:e]) for r in refs)}
    if not want:
        return text
    changed = True
    while changed:
        changed = False
        for n in list(want):
            s, e = by_name[n]
            body = text[s:e]
            for other in by_name:
                if other not in want and re.search(rf"\b{re.escape(other)}\b", body):
                    want.add(other)
                    changed = True
    # definitions nothing needs are removed; imports and other module-level statements stay
    drop = sorted(((s, e) for n, s, e in blocks if n not in want), reverse=True)
    out = text
    for s, e in drop:
        out = out[:s] + out[e:]
    if drop:
        out = prune_imports(out, lang)
    return re.sub(r"\n{4,}", "\n\n\n", out).rstrip() + "\n"


PY_FROM = re.compile(r"^from\s+(\S+)\s+import\s+(\([^)]*\)|[^\n]+)\n?", re.M)
PY_IMPORT = re.compile(r"^import\s+([\w.]+)(?:\s+as\s+(\w+))?[ \t]*\n?", re.M)
JS_NAMED = re.compile(r"""^import\s+(type\s+)?\{([^}]*)\}\s*from\s*(['"][^'"]+['"]);?[ \t]*\n?""", re.M)
JS_DEFAULT = re.compile(r"""^import\s+(\w+)\s+from\s*(['"][^'"]+['"]);?[ \t]*\n?""", re.M)


def prune_imports(text: str, lang: str) -> str:
    """Drop imported names nothing in the file uses anymore (after removing blocks)."""
    def used(name: str, body: str) -> bool:
        return re.search(rf"(?<![\w.]){re.escape(name)}\b", body) is not None

    if lang == "python":
        def fix_from(m: re.Match) -> str:
            body = text[:m.start()] + text[m.end():]
            names = [n.strip() for n in m.group(2).strip("()\n ").replace("\n", " ").split(",") if n.strip()]
            keep = [n for n in names if n == "*" or used(n.split(" as ")[-1].strip(), body)]
            if m.group(1) == "__future__":
                return m.group(0)
            return f"from {m.group(1)} import {', '.join(keep)}\n" if keep else ""

        def fix_import(m: re.Match) -> str:
            body = text[:m.start()] + text[m.end():]
            name = m.group(2) or m.group(1).split(".")[0]
            return m.group(0) if used(name, body) else ""
        text = PY_FROM.sub(fix_from, text)
        return PY_IMPORT.sub(fix_import, text)
    if lang in ("javascript", "typescript", "tsx", "jsx"):
        def fix_named(m: re.Match) -> str:
            body = text[:m.start()] + text[m.end():]
            names = [n.strip() for n in m.group(2).split(",") if n.strip()]
            keep = [n for n in names if used(n.split(" as ")[-1].strip(), body)]
            return f"import {m.group(1) or ''}{{ {', '.join(keep)} }} from {m.group(3)};\n" if keep else ""

        def fix_default(m: re.Match) -> str:
            body = text[:m.start()] + text[m.end():]
            return m.group(0) if used(m.group(1), body) else ""
        text = JS_NAMED.sub(fix_named, text)
        return JS_DEFAULT.sub(fix_default, text)
    return text

