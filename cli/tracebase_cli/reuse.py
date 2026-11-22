"""Turn a bundle (the picked implementation + what it needs, from an old repo) into concrete file changes for
the project in the current folder. Shared by `tracebase copy` and the MCP tools. No AI, no network
(besides the local Tracebase server)."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import merge, project

LANG_BY_EXT = {".php": "php", ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
               ".jsx": "javascript", ".mjs": "javascript", ".go": "go", ".java": "java", ".kt": "kotlin",
               ".rb": "ruby", ".cs": "csharp", ".rs": "rust"}


@dataclass
class Change:
    target: str
    kind: str                 # create | merge | wire | same | manual | deps | env
    reason: str
    source: str               # repo/path it comes from
    new_text: str | None = None
    old_text: str | None = None
    added: list[str] = field(default_factory=list)
    manual: list[dict] = field(default_factory=list)


class CopyError(RuntimeError):
    pass


def safe_target(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise CopyError(f"refusing to write outside the project: {rel}")
    return target


def _lang(path: str) -> str:
    return LANG_BY_EXT.get(Path(path).suffix.lower(), "")


def needed_methods(incoming: str, lang: str, used_by: str) -> list[str]:
    """Methods of `incoming` that the rest of the copied code calls, plus what those call on $this/this/self."""
    fns = merge.functions(incoming, lang)
    want = [n for n in fns if re.search(rf"(->|\.|::|&\.)\s*{re.escape(n)}(?:\s*\(|(?![\w?!=])(?=[\s,)\]}};]|$))", used_by, re.M)]
    i = 0
    while i < len(want):
        s, e = fns[want[i]]
        body = incoming[s:e]
        for n in fns:
            if n not in want and re.search(rf"(\$this->|this\.|self\.){re.escape(n)}\b", body):
                want.append(n)
        i += 1
    return want


def plan_changes(root: Path, bundle: dict, only_new: bool = False) -> list[Change]:
    files = bundle["files"]
    changes: list[Change] = []
    for f in files:
        rel = f["target"]
        src = f"{bundle['repo']}/{f['path']}"
        safe_target(root, rel)
        current = project.read_text(root, rel) if (root / rel).exists() else None
        if current is None and f.get("role") == "wiring":
            # an entry point (main.py, routes file…) this project doesn't have: never copy the old one whole
            changes.append(Change(rel, "manual", f["reason"], src,
                                  manual=[s for s in f.get("snippets") or [] if s["kind"] != "import"]))
            continue
        if current is None:
            changes.append(Change(rel, "create", f["reason"], src, new_text=f["content"]))
            continue
        if only_new:
            continue
        if f.get("role") == "wiring":
            snips = f.get("snippets") or []
            text, added, manual = merge.insert_lines(current, snips)
            if rel.endswith(".go"):  # Go won't compile a declared-but-unused variable: leave those to the developer
                code = re.sub(r'"(?:\\.|[^"\\])*"|`[^`]*`|//[^\n]*', '""', text)
                unused = [ln for ln in added if (m := re.match(r"(\w+)\s*:=", ln))
                          and len(re.findall(rf"\b{re.escape(m.group(1))}\b", code)) == 1]
                if unused:
                    keep = [sn for sn in snips if sn["line"].strip() not in unused]
                    text, added, manual = merge.insert_lines(current, keep)
                    manual += [dict(sn, kind="nested") for sn in snips if sn["line"].strip() in unused]
                # …nor an unused import
                body = re.sub(r"(?ms)^import\s*\(.*?^\)|^import\s+[^\n]+", "", text)
                dead = [ln for ln in added if ln.startswith("import ") and not re.search(
                    rf"\b{re.escape(ln.split()[-1].strip(chr(34)).rsplit('/', 1)[-1])}\.", body)]
                if dead:
                    keep = [sn for sn in snips if sn["line"].strip() not in dead and sn["line"].strip() not in unused]
                    text, added, manual2 = merge.insert_lines(current, keep)
                    manual = manual2 + [dict(sn, kind="nested") for sn in snips
                                        if sn["line"].strip() in dead or sn["line"].strip() in unused]
            kind = "wire" if added else ("manual" if manual else "same")
            changes.append(Change(rel, kind, f["reason"], src, text if added else None, current, added, manual))
            continue
        lang = _lang(rel)
        if not lang:
            changes.append(Change(rel, "same" if current == f["content"] else "manual", f["reason"], src))
            continue
        others = "\n".join(x["content"] for x in files if x is not f)
        only = None if f.get("role") == "picked" else needed_methods(f["content"], lang, others)
        text, added = merge.add_functions(current, f["content"], lang, only=only)
        if lang == "ruby":
            text, macros = merge.add_ruby_macros(text, f["content"], others + "\n" + text)
            added += macros
        changes.append(Change(rel, "merge" if added else "same", f["reason"], src,
                              text if added else None, current, added))
    changes = _drop_unmerged_associations(changes)
    if not only_new:
        written = "\n".join(c.new_text or "" for c in changes)
        changes += manifest_changes(root, bundle, written)
        env = env_example_change(root, bundle, written)
        if env:
            changes.append(env)
    return changes


def _drop_unmerged_associations(changes: list[Change]) -> list[Change]:
    """Rails: Role came along only because the OLD User `has_many :roles`. If that association isn't merged
    into this project's User, Role (and its migration) aren't needed."""
    drop: set[str] = set()
    for c in changes:
        if not c.target.endswith(".rb") or c.kind not in ("merge", "same"):
            continue
        model = Path(c.target).stem
        final = c.new_text if c.new_text is not None else (c.old_text or "")
        for d in changes:
            m = re.match(rf"^(\w+) is associated with {re.escape(model)}\b", d.reason)
            if m and d.kind == "create":
                table = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", m.group(1)).lower()
                if not re.search(rf":{table}s?\b", final):
                    drop.add(d.target)
                    drop |= {x.target for x in changes if x.kind == "create" and
                             re.match(rf"^creates the \w+ table for {re.escape(Path(d.target).stem)}$", x.reason)}
    return [c for c in changes if c.target not in drop]


# ------------------------------------------------------------------ dependency files and .env.example
INSTALL_AFTER = {"package.json": "npm install", "composer.json": "composer update", "requirements.txt":
                 "pip install -r requirements.txt", "Gemfile": "bundle install", "pom.xml": "mvn compile"}


def _json_indent(text: str) -> int:
    m = re.search(r"\n( +)\"", text)
    return len(m.group(1)) if m else 2


def _add_json_deps(text: str, key: str, deps: dict[str, str]) -> str:
    data = json.loads(text or "{}")
    data.setdefault(key, {}).update(deps)
    return json.dumps(data, indent=_json_indent(text), ensure_ascii=False) + "\n"


def manifest_changes(root: Path, bundle: dict, written: str) -> list[Change]:
    """Add the packages the copied code uses to this project's dependency file (never installs anything)."""
    pkgs = packages_needed(bundle, written)
    by_file: dict[str, list[dict]] = {}
    for p in pkgs:
        target = {"package.json": "package.json", "composer.json": "composer.json", "python": "requirements.txt",
                  "Gemfile": "Gemfile", "pom.xml": "pom.xml"}.get(p["manager"])
        if target and (root / target).is_file():
            by_file.setdefault(target, []).append(p)
    out = []
    for target, items in by_file.items():
        old = project.read_text(root, target) or ""
        try:
            if target == "package.json":
                new = _add_json_deps(old, "dependencies", {p["name"]: p["version"] for p in items})
            elif target == "composer.json":
                new = _add_json_deps(old, "require", {p["name"]: p["version"] for p in items})
            elif target == "requirements.txt":
                new = old.rstrip("\n") + ("\n" if old.strip() else "") + "\n".join(p["version"] for p in items) + "\n"
            elif target == "pom.xml":
                close = old.rfind("</dependencies>")
                if close < 0:
                    continue
                ind = (re.search(r"\n([ \t]*)<dependency>", old) or re.search(r"\n([ \t]*)</dependencies>", old)).group(1)
                ind = ind if "<dependency>" in old else ind + "  "
                xml = "".join(f"{ind}{p['xml']}\n" for p in items)
                line_start = old.rfind("\n", 0, close) + 1
                new = old[:line_start] + xml + old[line_start:]
            else:  # Gemfile
                new = old.rstrip("\n") + "\n" + "\n".join(p.get("line") or f"gem '{p['name']}'" for p in items) + "\n"
        except ValueError:
            continue  # unreadable manifest: leave it to the developer
        if new != old:
            for p in items:
                p["added_to"] = target
            out.append(Change(target, "deps", f"packages the copied code uses (then run `{INSTALL_AFTER[target]}`)",
                              bundle["repo"], new, old, [p["name"] for p in items]))
    return out


def env_example_change(root: Path, bundle: dict, written: str) -> Change | None:
    """Document the env vars the copied code reads in .env.example (empty values; .env is never touched)."""
    missing = env_missing(root, bundle, written)
    if not missing:
        return None
    target = ".env.example"
    old = project.read_text(root, target) if (root / target).is_file() else None
    head = (old.rstrip("\n") + "\n\n") if old and old.strip() else ""
    new = head + f"# used by code copied from {bundle['repo']}\n" + "\n".join(f"{k}=" for k in missing) + "\n"
    return Change(target, "env", "env vars the copied code reads (fill them in your .env)", bundle["repo"], new, old,
                  missing)


def private_dir(root: Path) -> Path:
    """.tracebase/ in the project (backups, pending plans), never committed."""
    d = root / ".tracebase"
    d.mkdir(exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


def apply(root: Path, change: Change, backup_dir: Path) -> str:
    if change.new_text is None:
        return "skipped"
    target = safe_target(root, change.target)
    note = "created"
    if target.exists():
        private_dir(root)
        dest = backup_dir / change.target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
        note = "updated"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(change.new_text)
    return note


def packages_needed(bundle: dict, written: str) -> list[dict]:
    """Packages from the old project that the code actually written here uses."""
    low = written.lower()
    out = []
    for p in bundle.get("packages", []):
        name = p["name"].lower()
        needles = {name, (p.get("module") or name).lower()}   # import name, e.g. "jwt" for PyJWT
        if "/" in name and not name.startswith("@"):
            needles.add(name.split("/")[0])          # composer vendor, e.g. "tymon" for tymon/jwt-auth
        if p.get("implicit") and (p.get("module") or "").lower() in low or \
                any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", low) for n in needles):
            out.append(p)
    return out


def env_missing(root: Path, bundle: dict, written: str) -> list[str]:
    keys = [k for k in bundle.get("env", []) if k in written]
    have = ""
    for name in (".env.example", ".env.sample", ".env"):
        p = root / name
        if p.is_file():
            have += p.read_text(errors="ignore")
    return [k for k in keys if not re.search(rf"^{re.escape(k)}=", have, re.M)]
