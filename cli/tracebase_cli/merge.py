"""Deterministic merging of code copied from an old project into a file that already exists in the new one.
No AI: functions/methods are found with brace matching (PHP, JS, TS, Go, Java…) or indentation (Python).

  add_functions()  add the methods/functions the new file doesn't have yet (+ their missing imports)
  insert_lines()   put wiring lines (e.g. a middleware alias) next to the same neighbour line they had before
"""
from __future__ import annotations

import re

FUNC_START = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:export|default|public|protected|private|static|async|final|abstract|override)\s+)*"
    r"(?:function\s+(?P<name>\w+)\s*\(|(?:const|let|var)\s+(?P<arrow>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)",
    re.M)
IMPORT_LINE = re.compile(r"^(use\s|import\s|from\s\S+\s+import\s|const\s+\w+\s*=\s*require\(|<\?php)")
PY_START = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?(?:def|class)\s+(?P<name>\w+)", re.M)
GO_START = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*[\[(]", re.M)
JAVA_START = re.compile(
    r"^(?P<indent>[ \t]+)(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)+"
    r"(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+(?P<name>\w+)\s*\(", re.M)
RUBY_START = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>(?:self\.)?[\w?!=]+)", re.M)


def _brace_end(text: str, start: int) -> int | None:
    """Index just after the brace that closes the first '{' at/after `start` (strings & comments skipped)."""
    i, n, depth, seen = start, len(text), 0, False
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            q = ch
            i += 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("//", i) or (ch == "#" and not text.startswith("#[", i)):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 1
        elif ch == "{":
            depth += 1
            seen = True
        elif ch == "}":
            depth -= 1
            if seen and depth == 0:
                return i + 1
        elif ch == ";" and not seen:
            return None  # declaration without a body (interface / abstract)
        i += 1
    return None


def _leading_comments(text: str, start: int) -> int:
    """Move `start` back over the docblock / comments / decorators / attributes directly above it."""
    lines_before = text[:start].split("\n")
    lines_before.pop()  # partial current line
    keep = len(lines_before)
    in_block = False
    for j in range(len(lines_before) - 1, -1, -1):
        t = lines_before[j].strip()
        if in_block:
            keep = j
            if t.startswith("/*"):
                in_block = False
            continue
        if t.endswith("*/"):
            keep = j
            in_block = not t.startswith("/*")
            continue
        if t.startswith(("//", "#", "@", "*")) and not t.startswith("#!"):
            keep = j
            continue
        break
    return len("\n".join(lines_before[:keep])) + (1 if keep else 0)


def functions(text: str, lang: str) -> dict[str, tuple[int, int]]:
    """name -> (start, end) offsets, including the docblock above it."""
    out: dict[str, tuple[int, int]] = {}
    if lang == "python":
        lines = text.splitlines(keepends=True)
        offs = [0]
        for ln in lines:
            offs.append(offs[-1] + len(ln))
        for m in PY_START.finditer(text):
            ind = len(m.group("indent"))
            li = text.count("\n", 0, m.start())
            end_li = li + 1
            while end_li < len(lines):
                ln = lines[end_li]
                if ln.strip() and len(ln) - len(ln.lstrip()) <= ind and not ln.lstrip().startswith((")", "]")):
                    break
                end_li += 1
            while end_li > li + 1 and not lines[end_li - 1].strip():
                end_li -= 1
            out.setdefault(m.group("name"), (_leading_comments(text, m.start()), offs[end_li]))
        return out
    if lang == "ruby":
        lines = text.splitlines(keepends=True)
        offs = [0]
        for ln in lines:
            offs.append(offs[-1] + len(ln))
        for m in RUBY_START.finditer(text):
            ind = m.group("indent")
            li = text.count("\n", 0, m.start())
            end_li = next((j for j in range(li + 1, len(lines)) if re.match(rf"^{re.escape(ind)}end\b", lines[j])), None)
            if end_li is not None:
                out.setdefault(m.group("name"), (_leading_comments(text, m.start()), offs[end_li + 1]))
        return out
    starts = {"go": GO_START, "java": JAVA_START, "kotlin": JAVA_START}.get(lang, FUNC_START)
    for m in starts.finditer(text):
        name = m.group("name") or (m.groupdict().get("arrow"))
        end = _brace_end(text, m.end())
        if not end:
            continue
        if end < len(text) and text[end] == ";":
            end += 1
        out.setdefault(name, (_leading_comments(text, m.start()), end))
    return out


def _go_specs(text: str) -> list[str]:
    specs = re.findall(r'^import\s+((?:\w+\s+)?"[^"]+")', text, re.M)
    for block in re.findall(r"^import\s*\((.*?)^\)", text, re.M | re.S):
        specs += [s.strip() for s in re.findall(r'^\s*((?:\w+\s+)?"[^"]+")', block, re.M)]
    return specs


def imports(text: str, lang: str) -> list[str]:
    if lang == "go":
        return [f"import {s}" for s in _go_specs(text)]
    if lang in ("java", "kotlin"):
        return re.findall(r"^import\s+(?:static\s+)?[\w.*]+;?", text, re.M)
    if lang == "ruby":
        return re.findall(r"""^require(?:_relative)?\s+['"][^'"]+['"]""", text, re.M)
    if lang == "php":
        return re.findall(r"^use\s+[\w\\]+(?:\s+as\s+\w+)?\s*;", text, re.M)  # top-level only (no indent)
    if lang == "python":
        return re.findall(r"^(?:from\s+\S+\s+import\s+[^\n]+|import\s+[^\n]+)", text, re.M)
    return re.findall(r"^import\s[^\n]*?(?:from\s+)?['\"][^'\"]+['\"];?|^const\s+\w+\s*=\s*require\([^)]*\);?", text, re.M)


def _insert_go_imports(text: str, new: list[str]) -> str:
    have = {s.split()[-1] for s in _go_specs(text)}
    specs = [n.removeprefix("import").strip() for n in new]
    specs = [s for s in specs if s.split()[-1] not in have]
    if not specs:
        return text
    block = re.search(r"^import\s*\((.*?)^\)", text, re.M | re.S)
    if block:
        indent = re.search(r"^([ \t]+)\S", block.group(1), re.M)
        ind = indent.group(1) if indent else "\t"
        at = block.end() - 1
        return text[:at] + "".join(f"{ind}{s}\n" for s in specs) + text[at:]
    single = list(re.finditer(r'^import\s+(?:\w+\s+)?"[^"]+"\s*$', text, re.M))
    if single:  # turn single imports into a block
        olds = [m.group(0).removeprefix("import").strip() for m in single]
        start, end = single[0].start(), single[-1].end()
        return text[:start] + "import (\n" + "".join(f"\t{s}\n" for s in olds + specs) + ")" + text[end:]
    m = re.search(r"^package\s+\w+\s*\n", text, re.M)
    at = m.end() if m else 0
    return text[:at] + "\nimport (\n" + "".join(f"\t{s}\n" for s in specs) + ")\n" + text[at:]


def _insert_imports(text: str, new: list[str], lang: str) -> str:
    if not new:
        return text
    if lang == "go":
        return _insert_go_imports(text, new)
    existing = list(re.finditer("|".join(re.escape(i) for i in imports(text, lang)) or r"(?!x)x", text))
    block = "\n".join(new)
    if existing:
        at = existing[-1].end()
        return text[:at] + "\n" + block + text[at:]
    if lang in ("java", "kotlin"):
        m = re.search(r"^package\s+[^;\n]+;?\s*\n", text, re.M)
        if m:
            return text[:m.end()] + "\n" + block + "\n" + text[m.end():]
    if lang == "php":
        m = re.search(r"^namespace\s+[^;]+;\s*\n", text, re.M) or re.search(r"^<\?php\s*\n", text)
        if m:
            return text[:m.end()] + "\n" + block + "\n" + text[m.end():]
    return block + "\n" + text


def add_functions(existing: str, incoming: str, lang: str, only: list[str] | None = None) -> tuple[str, list[str]]:
    """Copy the functions/methods of `incoming` that `existing` lacks. Returns (new text, added names)."""
    have = functions(existing, lang)
    theirs = functions(incoming, lang)
    names = [n for n in theirs if n not in have and (only is None or n in only)]
    if not names:
        return existing, []
    chunks = []
    for n in names:
        s, e = theirs[n]
        chunks.append(incoming[s:e].rstrip("\n"))
    body = "\n\n".join(chunks)
    # put them inside the class when there is one (before its final closing brace), else at the end
    if lang == "ruby" and re.search(r"^(class|module)\s+\w", existing, re.M):
        close = existing.rstrip().rfind("\nend")
        inner = "\n".join(("  " + ln if ln.strip() and not ln.startswith("  ") else ln) for ln in body.split("\n"))
        out = existing[:close].rstrip("\n") + "\n\n" + inner + existing[close:]
    elif lang not in ("python", "go") and re.search(
            r"^\s*(?:(?:public|protected|private|final|abstract|static|export|default)\s+)*class\s+\w+", existing, re.M):
        close = existing.rstrip().rfind("}")
        out = existing[:close].rstrip("\n") + "\n\n" + body + "\n" + existing[close:]
    else:
        out = existing.rstrip("\n") + "\n\n" + body + "\n"
    # imports the copied code needs
    copied = "\n".join(chunks)
    wanted = []
    have_imports = {i.strip() for i in imports(existing, lang)}
    for imp in imports(incoming, lang):
        if imp.strip() in have_imports:
            continue
        if lang == "go":
            spec = imp.removeprefix("import").strip().split()
            names_in = [spec[0]] if len(spec) == 2 else [spec[-1].strip('"').rstrip("/").split("/")[-1].split(".")[0]]
        elif lang in ("java", "kotlin", "php"):
            names_in = re.findall(r"(\w+)\s*;?\s*$", imp.split(" as ")[-1])
        elif lang == "ruby":
            names_in = [re.sub(r"\W", "", imp.split()[-1]).upper()]  # require 'jwt' -> JWT
        else:
            names_in = re.findall(r"\b(\w+)\b", re.sub(r"from\s+['\"].*$|^import|require\(.*\)", "", imp))
        if any(re.search(rf"\b{re.escape(x)}\b", copied, re.I if lang == "ruby" else 0)
               for x in names_in if x not in ("import", "from", "const")):
            wanted.append(imp.strip())
    return _insert_imports(out, wanted, lang), names


def insert_lines(existing: str, snippets: list[dict]) -> tuple[str, list[str], list[dict]]:
    """Insert wiring lines next to the neighbour line they had in the old file.
    Returns (new text, inserted lines, snippets that must be added by hand)."""
    text = existing
    added, manual = [], []
    for sn in snippets:
        line = sn["line"].rstrip()
        if line.strip() in {ln.strip() for ln in text.splitlines()}:
            continue
        if sn["kind"] == "import":
            t = line.strip()
            lang = "php" if t.startswith("use ") else \
                "go" if re.match(r'^import\s+(\w+\s+)?"[^"]+"$', t) else \
                "java" if re.match(r"^import\s+(static\s+)?[\w.*]+;$", t) else \
                ("python" if re.match(r"^(from\s+\S+\s+import|import\s+[\w.]+\s*$)", t) else "js")
            if lang == "go" and any(t.removeprefix("import").strip().split()[-1] == s.split()[-1] for s in _go_specs(text)):
                continue
            text = _insert_imports(text, [t], lang)
            added.append(line.strip())
            continue
        lines = text.split("\n")
        if sn["kind"] == "nested":
            # inside a block (route group, func main, routes.draw do): only next to the same neighbour line
            idx = None
            for nb in sn.get("before") or []:  # nearest neighbour first
                if not nb.strip() or IMPORT_LINE.match(nb.strip()):
                    continue
                idx = next((i for i, ln in enumerate(lines) if ln.strip() == nb.strip()), None)
                if idx is not None:
                    break
            if idx is None:
                manual.append(sn)
                continue
            lines.insert(idx + 1, line)
            text = "\n".join(lines)
            added.append(line.strip())
            continue
        anchor = None
        for nb in sn.get("before") or []:
            if sn["kind"] == "statement" and IMPORT_LINE.match(nb.strip()):
                break  # code goes after the imports, not between them
            idx = next((i for i, ln in enumerate(lines) if ln.strip() == nb.strip()), None)
            if idx is not None:
                anchor = idx
                break
        if anchor is not None:
            lines.insert(anchor + 1, line)
        elif sn["kind"] == "statement":
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", line, ""]
        else:
            manual.append(sn)
            continue
        text = "\n".join(lines)
        added.append(line.strip())
    return text, added, manual


RUBY_MACRO = re.compile(r"^([ \t]+)(has_secure_password|has_many|has_one|belongs_to|has_and_belongs_to_many)\b[^\n]*", re.M)


def add_ruby_macros(existing: str, incoming: str, used_by: str) -> tuple[str, list[str]]:
    """Rails class declarations the copied code relies on: `has_secure_password` (gives .authenticate),
    `has_many :roles` (gives .roles). Added after the class line when something uses what they provide."""
    added = []
    for m in RUBY_MACRO.finditer(incoming):
        line = m.group(0).strip()
        if line in existing:
            continue
        name = m.group(2)
        provides = ["authenticate", "password"] if name == "has_secure_password" else \
            re.findall(r":(\w+)", line)[:1]
        if any(re.search(rf"[.&]\s*{re.escape(w)}\b|\b{re.escape(w)}\.", used_by) for w in provides):
            added.append(line)
    if not added:
        return existing, []
    cls = re.search(r"^class\s+[^\n]+\n", existing, re.M)
    if not cls:
        return existing, []
    at = cls.end()
    return existing[:at] + "".join(f"  {ln}\n" for ln in added) + existing[at:], added
