"""Bundle: given the implementation you picked in an OLD repo (e.g. AuthController.php), collect every file
that code needs from that same repo, so it can be copied into a NEW project in one go:

  classes it uses        Permission::class, `use App\\Models\\Role;`, new Foo()
  methods it calls       $user->hasRole()  -> the file that defines hasRole (app/Models/User.php)
  relative imports       import { redis } from '../lib/redis'   /  from .models import Role
  migrations             a model Role -> the migration that creates `roles`
  wiring                 files that register it: Kernel.php ('role' => EnsureUserHasRole), routes using 'role:'
  packages & env vars    composer/npm/pip packages the code imports, env()/process.env keys it reads

Deterministic and local: it only reads the indexed repos. No LLM, no network."""
from __future__ import annotations

import json
import posixpath
import re

from ..code.chunker import detect_language
from .adapt import adapt
from .blocks import keep_only, top_level
from ..code.sources import get_source
from ..db import rows

MAX_FILES = 12
PHP_SKIP_METHODS = {"json", "user", "only", "attempt", "pluck", "where", "first", "get", "create", "save",
                    "delete", "update", "find", "exists", "isNotEmpty", "intersect", "belongsToMany", "hasMany",
                    "belongsTo", "hasOne", "middleware", "group", "validate", "input", "all", "with", "load",
                    "factory", "getTTL", "logout", "refresh", "parseToken", "getKey", "getEmailForPasswordReset",
                    "line", "action", "subject", "toFile", "resize", "rotate", "webp", "set", "del", "keys",
                    "attach", "detach", "sync", "send", "status", "headers"}


# (pattern in the code, pip package, word that must appear in the copied code) for dependencies that are
# needed but never imported by their own name
IMPLICIT_PY = [
    (r"\bEmailStr\b", "email-validator", "EmailStr"),
    (r"\b(UploadFile|Form\(|OAuth2PasswordRequestForm)\b", "python-multipart", "UploadFile"),
    (r"postgresql(\+psycopg2)?://", "psycopg2-binary", "postgresql"),
    (r"mysql\+pymysql://", "pymysql", "pymysql"),
]

# pip name -> import name, where they differ
PY_IMPORT_NAMES = {"pyjwt": "jwt", "python-jose": "jose", "pillow": "PIL", "beautifulsoup4": "bs4",
                   "opencv-python": "cv2", "pyyaml": "yaml", "python-dotenv": "dotenv", "scikit-learn": "sklearn",
                   "python-multipart": "multipart", "pydantic-settings": "pydantic_settings", "psycopg2-binary": "psycopg2",
                   "psycopg-binary": "psycopg", "uvicorn[standard]": "uvicorn", "google-cloud-storage": "google.cloud",
                   "passlib[bcrypt]": "passlib", "email-validator": "email_validator", "aiosqlite": "aiosqlite"}


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def camel(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in name.split("_"))


GO_DEF = re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+)|(?:var|const)\s+(\w+))", re.M)
RUBY_SKIP_CONSTS = {"ApplicationController", "ApplicationRecord", "ActiveRecord", "ActiveSupport", "ActionController",
                    "Rails", "ENV", "JWT", "String", "Integer", "Hash", "Array", "Time", "Date", "DateTime"}
RUBY_SKIP_METHODS = {"find_by", "where", "create", "save", "update", "destroy", "render", "head", "params", "exists?",
                     "map", "each", "first", "last", "new", "to_s", "to_i", "merge", "dig", "split", "authenticate",
                     "find", "all", "includes", "order", "limit", "permit", "require", "present?", "blank?", "nil?"}


def plural(name: str) -> str:
    low = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return low[:-1] + "ies" if low.endswith("y") else low + ("es" if low.endswith("s") else "s")


class _Repo:
    def __init__(self, repo: str) -> None:
        self.name = repo
        self.src = get_source(repo)
        self.paths = [r["path"] for r in rows("SELECT path FROM files WHERE repo=?", (repo,))]
        self._cache: dict[str, str] = {}

    def read(self, path: str) -> str:
        if path not in self._cache:
            self._cache[path] = (self.src.read(path) if self.src else None) or ""
        return self._cache[path]

    def find_class(self, name: str) -> str | None:
        cands = [p for p in self.paths if _stem(p) == name and not p.startswith(("vendor/", "node_modules/"))]
        cands.sort(key=lambda p: (not p.startswith(("app/", "src/")), len(p)))
        return cands[0] if cands else None

    def find_method(self, method: str, lang: str = "php") -> str | None:
        like, rx = {
            "ruby": (f"%def %{method}%", rf"^\s*def\s+(self\.)?{re.escape(method)}(?![\w?!])"),
        }.get(lang, (f"%function {method}(%", rf"function\s+{re.escape(method)}\s*\("))
        for r in rows("SELECT path, content FROM chunks WHERE repo=? AND content LIKE ? LIMIT 8", (self.name, like)):
            if re.search(rx, r["content"], re.M):
                return r["path"]
        return None

    def go_module(self) -> str | None:
        m = re.search(r"(?m)^module\s+(\S+)", self.read("go.mod"))
        return m.group(1) if m else None

    def go_package_files(self, rel_dir: str) -> list[str]:
        return sorted(p for p in self.paths if posixpath.dirname(p) == rel_dir and p.endswith(".go")
                      and not p.endswith("_test.go"))

    def find_ruby_const(self, const: str) -> str | None:
        """Rails autoloading: Authorizable -> app/**/authorizable.rb, Admin::Report -> admin/report.rb."""
        rel = "/".join(snake(part) for part in const.split("::")) + ".rb"
        cands = [p for p in self.paths if p == rel or p.endswith("/" + rel)]
        cands.sort(key=lambda p: (not p.startswith(("app/", "lib/")), len(p)))
        return cands[0] if cands else None

    def resolve_module(self, from_path: str, module: str) -> str | None:
        """Python absolute import (`from services.auth import x`) -> file in this repo, if it is one."""
        rel = module.replace(".", "/")
        bases = [""]
        d = posixpath.dirname(from_path)
        while d:
            bases.append(d)
            d = posixpath.dirname(d)
        paths = set(self.paths)
        for b in bases:
            for cand in (f"{rel}.py", f"{rel}/__init__.py"):
                full = posixpath.join(b, cand) if b else cand
                if full in paths:
                    return full
        return None

    def resolve_import(self, from_path: str, spec: str) -> str | None:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
        for p in self.paths:
            if p == target or posixpath.splitext(p)[0] == target or posixpath.splitext(p)[0] == target + "/index":
                return p
        return None


def _names(spec: str) -> set[str] | None:
    """Imported names from `a, b as c` / `(a,\n b)` / `{ a, b }`; None = the whole module."""
    spec = spec.strip().strip("(){} \n")
    if not spec or "*" in spec:
        return None
    return {n.strip().split(" as ")[0].strip() for n in spec.replace("\n", " ").split(",") if n.strip()}


def _dependencies(repo: _Repo, path: str, text: str) -> list[tuple[str, str, set[str] | None]]:
    """(path, reason, names needed or None for the whole file) of files in the same repo that `path` needs."""
    out: list[tuple[str, str, set[str] | None]] = []
    if path.endswith(".php"):
        names = set(re.findall(r"\b([A-Z][A-Za-z0-9_]+)::class", text))
        names |= set(re.findall(r"^use App\\(?:[A-Za-z0-9_]+\\)*([A-Za-z0-9_]+);", text, re.M))
        names |= set(re.findall(r"\bnew\s+([A-Z][A-Za-z0-9_]+)\s*\(", text))
        for n in sorted(names):
            p = repo.find_class(n)
            if p and p != path:
                out.append((p, f"{n} is used by {_stem(path)}", None))
        own = set(re.findall(r"function\s+(\w+)\s*\(", text))
        for m in sorted(set(re.findall(r"->([a-zA-Z_]\w*)\(", text)) - PHP_SKIP_METHODS - own):
            p = repo.find_method(m)
            if p and p != path:
                out.append((p, f"defines {m}(), called by {_stem(path)}", None))
    if path.endswith(".go"):
        out += _go_dependencies(repo, path, text)
    if path.endswith(".java"):
        out += _java_dependencies(repo, path, text)
    if path.endswith(".rb"):
        out += _ruby_dependencies(repo, path, text)
    # JS / TS relative imports
    for what, spec in re.findall(r"""import\s+(?:type\s+)?([^'";]*?)\s*from\s*['"](\.{1,2}/[^'"]+)['"]""", text):
        p = repo.resolve_import(path, spec)
        if p and p != path:
            named = re.search(r"\{([^}]*)\}", what)
            default = what.split("{")[0].strip(" ,")
            out.append((p, f"imported by {_stem(path)}", None if default or not named else _names(named.group(1))))
    for spec in re.findall(r"""require\(\s*['"](\.{1,2}/[^'"]+)['"]""", text):
        p = repo.resolve_import(path, spec)
        if p and p != path:
            out.append((p, f"imported by {_stem(path)}", None))
    # Python: absolute (from services.auth import X) and relative (from .models import X)
    if path.endswith(".py"):
        for mod, spec in re.findall(r"^from\s+(\.*[\w.]*)\s+import\s+(\([^)]*\)|[^\n]+)", text, re.M):
            if mod.startswith("."):
                base = posixpath.dirname(path)
                for _ in range(len(mod) - len(mod.lstrip(".")) - 1):
                    base = posixpath.dirname(base)
                rel = mod.lstrip(".").replace(".", "/")
                cand = posixpath.join(base, rel) if rel else base
                p = next((x for x in repo.paths if x in (cand + ".py", cand + "/__init__.py")), None)
            else:
                p = repo.resolve_module(path, mod)
            if p and p != path:
                out.append((p, f"imported by {_stem(path)}", _names(spec)))
        for mod in re.findall(r"^import\s+([A-Za-z_][\w.]*)", text, re.M):
            p = repo.resolve_module(path, mod)
            if p and p != path:
                out.append((p, f"imported by {_stem(path)}", None))
    return out


def _go_imports(text: str) -> list[tuple[str, str]]:
    """[(alias, import path)] of a Go file."""
    specs = re.findall(r'^import\s+(\w+\s+)?"([^"]+)"', text, re.M)
    for block in re.findall(r"^import\s*\((.*?)^\)", text, re.M | re.S):
        specs += re.findall(r'^\s*(\w+\s+)?"([^"]+)"', block, re.M)
    return [((a or "").strip() or ip.rstrip("/").split("/")[-1], ip) for a, ip in specs]


def _go_defs(text: str) -> set[str]:
    return {next(g for g in m.groups() if g) for m in GO_DEF.finditer(text)}


def _go_dependencies(repo: _Repo, path: str, text: str) -> list[tuple[str, str, set[str] | None]]:
    out = []
    mod = repo.go_module()
    if mod:
        for alias, ip in _go_imports(text):
            if ip != mod and not ip.startswith(mod + "/"):
                continue
            used = set(re.findall(rf"\b{re.escape(alias)}\.([A-Z]\w*)", text))
            files = repo.go_package_files(ip[len(mod):].lstrip("/"))
            hit = [f for f in files if used & _go_defs(repo.read(f))] or files
            out += [(f, f"package {alias} is used by {_stem(path)}", None) for f in hit]
    # same package: identifiers defined in sibling files
    own = _go_defs(text)
    for f in repo.go_package_files(posixpath.dirname(path)):
        if f != path and any(re.search(rf"\b{re.escape(n)}\b", text) for n in _go_defs(repo.read(f)) - own):
            out.append((f, f"same package, used by {_stem(path)}", None))
    return out


def _java_dependencies(repo: _Repo, path: str, text: str) -> list[tuple[str, str, set[str] | None]]:
    out = []
    for fq in re.findall(r"^import\s+([\w.]+\.[A-Z]\w*)\s*;", text, re.M):
        rel = fq.replace(".", "/") + ".java"
        p = next((x for x in repo.paths if x.endswith("/" + rel) or x == rel), None)
        if p and p != path:
            out.append((p, f"{fq.rsplit('.', 1)[1]} is used by {_stem(path)}", None))
    d = posixpath.dirname(path)
    for name in sorted(set(re.findall(r"\b([A-Z]\w+)\b", re.sub(r"(?m)^import .*$", "", text)))):
        p = posixpath.join(d, name + ".java")
        if p != path and p in set(repo.paths):
            out.append((p, f"{name} (same package) is used by {_stem(path)}", None))
    return out


def _ruby_dependencies(repo: _Repo, path: str, text: str) -> list[tuple[str, str, set[str] | None]]:
    out = []
    code = re.sub(r"(?m)#.*$", "", text)
    for spec in re.findall(r"""require_relative\s+['"]([^'"]+)['"]""", code):
        p = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec + ("" if spec.endswith(".rb") else ".rb")))
        if p in set(repo.paths):
            out.append((p, f"required by {_stem(path)}", None))
    own = set(re.findall(r"^\s*(?:class|module)\s+([\w:]+)", code, re.M))
    consts = set(re.findall(r"(?<![\w:])([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)*(?:::[A-Z]\w*)*)", re.sub(r"'[^']*'|\"[^\"]*\"", "", code)))
    for c in sorted(consts - own - RUBY_SKIP_CONSTS):
        p = repo.find_ruby_const(c)
        if p and p != path:
            out.append((p, f"{c} is used by {_stem(path)}", None))
    # associations name models by symbol: has_many :roles -> Role
    for assoc in re.findall(r"^\s*(?:has_many|has_one|belongs_to|has_and_belongs_to_many)\s+:(\w+)", code, re.M):
        model = camel(assoc[:-3] + "y" if assoc.endswith("ies") else assoc.rstrip("s") if assoc.endswith("s") else assoc)
        p = repo.find_ruby_const(model)
        if p and p != path and model not in own:
            out.append((p, f"{model} is associated with {_stem(path)}", None))
    own_defs = set(re.findall(r"^\s*def\s+(?:self\.)?([\w?!=]+)", code, re.M))
    called = set(re.findall(r"\.([a-z_]\w*[?!]?)", code)) | set(re.findall(r"before_action\s+:([\w?!]+)", code))
    for m in sorted(called - own_defs - RUBY_SKIP_METHODS):
        p = repo.find_method(m, "ruby")
        if p and p != path:
            out.append((p, f"defines {m}, called by {_stem(path)}", None))
    return out


def _depths(text: str) -> list[int]:
    """Bracket depth at the start of each line (strings and comments ignored, roughly)."""
    depths, d = [], 0
    for line in text.splitlines():
        depths.append(d)
        clean = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|//.*$|#.*$", "", line)
        d += clean.count("{") + clean.count("(") + clean.count("[") - clean.count("}") - clean.count(")") - clean.count("]")
        d = max(d, 0)
    return depths


def _trim_import(t: str, needles: list[str]) -> str:
    """`from routers import auth, voices, tts` -> `from routers import auth` (only what we copy)."""
    m = re.match(r"^(from\s+\S+\s+import\s+)\(?([^()]+?)\)?\s*$", t)
    if m:
        keep = [n.strip() for n in m.group(2).split(",") if n.strip().split(" as ")[0] in needles]
        return m.group(1) + ", ".join(keep) if keep else t
    m = re.match(r"^(import\s*\{)([^}]*)(\}\s*from\s*['\"][^'\"]+['\"];?)$", t)
    if m:
        keep = [n.strip() for n in m.group(2).split(",") if n.strip().split(" as ")[0] in needles]
        return f"{m.group(1)} {', '.join(keep)} {m.group(3)}" if keep else t
    return t


def snippets(text: str, needles: list[str], aliases: list[str] | None = None) -> list[dict]:
    """Lines of a wiring file that mention one of `needles`, with what they need to be re-inserted elsewhere:
    kind "import"    an import / use line (trimmed to the needed names)
    kind "entry"     an array/list entry ('role' => X::class,): insert next to the same neighbour line
    kind "statement" a complete top-level statement: can be appended
    kind "nested"    inside a block (e.g. a route group): shown, not inserted automatically"""
    lines = text.splitlines()
    depths = _depths(text)
    base = min((d for d, ln in zip(depths, lines) if ln.strip()), default=0)
    out = []
    for i, ln in enumerate(lines):
        code = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "''", ln)  # a name inside a string doesn't count…
        if not any(re.search(rf"\b{re.escape(n)}\b", code) for n in needles) and \
                not any(re.search(rf"['\"]{re.escape(a)}[:'\"#]", ln) for a in aliases or []):  # …except 'role:' aliases
            continue
        t = ln.strip()
        if t.startswith(("#", "//", "*", "/*")):
            continue
        indent = len(ln) - len(ln.lstrip())
        if re.match(r"^use\s|^import\s|^from\s\S+\s+import\s", t):
            kind = "import"
            ln = ln[:indent] + _trim_import(t, needles)
        elif t.endswith(",") and re.search(r"=>|:", t):
            kind = "entry"
        elif depths[i] <= base and indent == 0 and t.count("(") == t.count(")") and \
                (t.endswith(";") or not t.endswith((":", "{", "(", "[", ",", "\\"))):
            kind = "statement"
        else:
            kind = "nested"
        before = [lines[j] for j in range(i - 1, max(-1, i - 4), -1) if lines[j].strip()][:3]
        ctx_open = next((lines[j].strip() for j in range(i - 1, -1, -1) if depths[j] < depths[i] and lines[j].strip()), None)
        if ctx_open is None and indent:  # indentation-based block (Python): the enclosing def / with / if
            ctx_open = next((lines[j].strip() for j in range(i - 1, -1, -1)
                             if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) < indent), None)
        out.append({"line": ln, "kind": kind, "before": before, "inside": ctx_open if kind == "nested" else None})
    return out


REGISTRATION = re.compile(r"(^|/)(app/Http/Kernel\.php|bootstrap/app\.php|app/Providers/[^/]+\.php|routes?/[^/]+|"
                          r"urls\.py|main\.py|app\.py|server\.(ts|js|py)|app\.(ts|js)|index\.(ts|js)|asgi\.py|wsgi\.py|"
                          r"main\.go|router\.go|routes\.go|config/routes\.rb|app/controllers/application_controller\.rb)$")
ENTRY = re.compile(r"(^|/)(routes?/[^/]+|urls\.py|main\.py|app\.py|server\.(ts|js|py)|app\.(ts|js)|index\.(ts|js)|asgi\.py|wsgi\.py|"
                   r"main\.go|router\.go|routes\.go|config/routes\.rb)$")
CONSUMER = re.compile(r"(^|/)(routers?|controllers?|Controllers|handlers?|api|views|endpoints)/|(^|/)views\.py$", re.I)


def _symbol_refs(symbol: str | None, main: list[str], repo: _Repo) -> list[str]:
    """Patterns a consumer must contain: AuthService.login / AuthService::login / login( for a picked symbol,
    else the class names defined in the picked files."""
    if symbol and symbol not in ("<module>", "<file>"):
        cls, _, meth = symbol.rpartition("::")
        if cls and meth:
            return [rf"\b{re.escape(cls)}(\.|::|->){re.escape(meth)}\b", rf"->{re.escape(meth)}\("]
        return [rf"\b{re.escape(symbol)}\s*\("]
    names = {c for p in main for c in re.findall(r"^\s*(?:export\s+)?(?:final\s+)?class\s+(\w+)", repo.read(p), re.M)}
    return [rf"\b{re.escape(n)}\b" for n in names]


def _consumers(repo: _Repo, picked: dict[str, str], main: list[str], symbol: str | None) -> list[tuple[str, str, list[str]]]:
    """Files that USE the picked implementation (e.g. the router exposing AuthService.login as an endpoint)."""
    refs = _symbol_refs(symbol, main, repo)
    if not refs:
        return []
    out = []
    for p in repo.paths:
        if p in picked or not CONSUMER.search(p) or len(out) >= 2:
            continue
        text = repo.read(p)
        if any(re.search(r, text) for r in refs):
            out.append((p, f"calls {symbol or _stem(main[0])} (its endpoint / controller)", refs))
    return out


def _entry_setup(repo: _Repo, picked: dict[str, str], needs: dict[str, set[str] | None]) -> set[str]:
    """Setup functions (init_db, configure_logging…) that the old entry point imports from a file we copy only
    partially: keep them in that file, and wire their calls in too."""
    found: set[str] = set()
    entries = [p for p in repo.paths if ENTRY.search(p) and p not in picked]
    for f, names in needs.items():
        if names is None or not f.endswith(".py"):
            continue
        module = f[:-3].replace("/", ".").removesuffix(".__init__")
        defs = {n for n, _, _ in top_level(repo.read(f), "python")}
        for e in entries:
            for spec in re.findall(rf"^from\s+{re.escape(module)}\s+import\s+(\([^)]*\)|[^\n]+)", repo.read(e), re.M):
                extra = (_names(spec) or set()) & defs
                extra -= names
                if extra:
                    needs[f] = names | extra
                    names = needs[f]
                    found |= extra
    return found


def _wiring(repo: _Repo, picked: dict[str, str], modules: set[str], setup: set[str] | None = None) -> list[tuple[str, str, list[dict]]]:
    """Registration files that mention the picked classes, the modules we copy (FastAPI include_router,
    Express app.use), or the aliases those classes are registered under (Laravel 'role' => X::class)."""
    classes = {_stem(p) for p in picked if len(_stem(p)) > 3 and _stem(p)[:1].isupper()}
    classes |= {camel(_stem(p)) for p in picked if p.endswith(".rb") and "/migrate/" not in p}
    go_pkgs = {posixpath.basename(posixpath.dirname(p)) for p in picked if p.endswith(".go") and "/" in p}
    # Rails: sessions_controller.rb is routed as 'sessions#create' / resources :sessions
    rb_routes = {_stem(p)[:-len("_controller")] for p in picked if p.endswith("_controller.rb")}
    if not classes and not modules and not go_pkgs:
        return []
    out: list[tuple[str, str, list[dict]]] = []
    aliases: dict[str, str] = {}
    candidates = [p for p in repo.paths if p not in picked and REGISTRATION.search(p)]
    for p in candidates:
        text = repo.read(p)
        for c in classes:
            for a in re.findall(rf"""['"]([\w.-]+)['"]\s*=>\s*[\\\w]*\b{re.escape(c)}::class""", text):
                aliases[a] = c
    for p in candidates:
        text = repo.read(p)
        code = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "''", text)
        hit = [c for c in classes if re.search(rf"\b{re.escape(c)}\b", code)]
        calls = [f for f in setup or () if re.search(rf"\b{re.escape(f)}\s*\(", code)]
        mods = [m for m in modules if re.search(rf"\b{re.escape(m)}\.(router|routes|urls|bp|blueprint)\b|"
                                                rf"app\.use\([^)]*\b{re.escape(m)}\b|include\(['\"][\w.]*{re.escape(m)}", text)]
        used = [a for a in aliases if re.search(rf"""['"]{re.escape(a)}[:'"]""", text)]
        if p.endswith(".go"):
            mods += [g for g in go_pkgs if re.search(rf"\b{re.escape(g)}\.[A-Z]\w*", code)]
        if p.endswith(".rb"):
            used += [r for r in rb_routes if re.search(rf"""['"]{re.escape(r)}#|resources?\s+:{re.escape(r)}\b""", text)]
        if hit or mods or used or calls:
            if hit:
                why = f"registers / uses {', '.join(hit)}"
            elif mods or calls:
                why = f"registers {', '.join(mods + [c + '()' for c in calls])}"
            elif used[0] in aliases:
                why = f"applies the '{used[0]}' middleware ({aliases[used[0]]})"
            else:
                why = f"routes to {used[0]}"
            snips = snippets(text, hit + mods + calls, used)
            if p.endswith(".go"):
                snips = [{"line": f'import "{ip}"', "kind": "import", "before": [], "inside": None}
                         for alias, ip in _go_imports(text) if alias in mods] + snips
            out.append((p, why, snips))
    return out


def _migrations(repo: _Repo, picked: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for p in list(picked):
        if not re.search(r"(^|/)(Models?|models?|entities)/", p):
            continue
        pl = plural(_stem(p))
        for m in repo.paths:
            if "migrat" in m and re.search(rf"[_/]{pl}(_|\.|$)", m) and m not in picked:
                out.append((m, f"creates the {pl} table for {_stem(p)}"))
    return out


def _packages(repo: _Repo, texts: str, project_manifests: dict[str, str]) -> list[dict]:
    """Packages from the old repo's manifest that the copied code uses and the new project lacks."""
    low = texts.lower()
    out = []
    for manifest, keys in (("composer.json", ("require",)), ("package.json", ("dependencies", "devDependencies"))):
        raw = repo.read(manifest)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        have = project_manifests.get(manifest, "")
        for k in keys:
            for name, ver in (data.get(k) or {}).items():
                if name == "php" or name.startswith("ext-") or f'"{name}"' in have:
                    continue
                tokens = [t for t in re.split(r"[/@]", name) if len(t) > 2]
                needles = {name.lower()} | {t.lower() for t in tokens[:1]} | {t.lower().replace("-", "") for t in tokens}
                if any(n in low for n in needles if len(n) > 3):
                    tool = "composer require" if manifest == "composer.json" else "npm install"
                    out.append({"manager": manifest, "name": name, "version": ver, "install": f"{tool} {name}",
                                "module": name.split("/")[0] if manifest == "composer.json" else name})
    have_py = (project_manifests.get("requirements.txt", "") + project_manifests.get("pyproject.toml", "")).lower()
    py_reqs = [ln.strip() for ln in repo.read("requirements.txt").splitlines()]
    py_reqs += re.findall(r"""^\s*["']([A-Za-z0-9_.\-\[\]]+[^"']*)["'],?\s*$""", repo.read("pyproject.toml"), re.M)
    for line in py_reqs:
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=~!\[; ]", line, 1)[0].strip()
        if not name or re.search(rf"(^|[\s\"'])({re.escape(name.lower())})([\s<>=~!\[;\"']|$)", have_py):
            continue
        mod = PY_IMPORT_NAMES.get(name.lower(), name.lower().replace("-", "_"))
        if re.search(rf"^\s*(import|from)\s+{re.escape(mod)}\b", texts, re.M | re.I):
            out.append({"manager": "python", "name": name, "module": mod, "version": line, "install": f"pip install {name}"})
    out += _go_packages(repo, texts, project_manifests.get("go.mod", ""))
    out += _ruby_packages(repo, texts, project_manifests.get("Gemfile", ""))
    out += _maven_packages(repo, texts, project_manifests.get("pom.xml", ""))
    # packages that code needs without importing them by name
    for rx, name, mod in IMPLICIT_PY:
        if re.search(rx, texts) and name.lower() not in have_py and not any(o["name"].lower() == name.lower() for o in out):
            out.append({"manager": "python", "name": name, "module": mod, "version": name,
                        "install": f"pip install {name}", "implicit": True})
    return out


def _go_packages(repo: _Repo, texts: str, have: str) -> list[dict]:
    gomod = repo.read("go.mod")
    reqs = re.findall(r"^\s*(?:require\s+)?([\w.\-]+\.[a-z]{2,}/[\w.\-/]+)\s+(v[\w.\-+]+)", gomod, re.M)
    imports = set(re.findall(r'"([\w.\-]+\.[a-z]{2,}/[^"]+)"', texts))
    out = []
    for mod, ver in reqs:
        if mod in have or not any(i == mod or i.startswith(mod + "/") for i in imports):
            continue
        out.append({"manager": "go.mod", "name": mod, "version": ver, "module": mod, "install": f"go get {mod}@{ver}"})
    return out


IMPLICIT_RB = [(r"\bhas_secure_password\b", "bcrypt")]


def _ruby_packages(repo: _Repo, texts: str, have: str) -> list[dict]:
    out = []
    code = re.sub(r"(?m)#.*$", "", texts)
    for line, name in re.findall(r"""^(\s*gem\s+['"]([\w\-]+)['"][^\n]*)""", repo.read("Gemfile"), re.M):
        if name in ("rails",) or re.search(rf"""gem\s+['"]{re.escape(name)}['"]""", have):
            continue
        used = re.search(rf"""require\s+['"]{re.escape(name)}['"]|\b{re.escape(camel(name.replace('-', '_')))}\b|"""
                         rf"""\b{re.escape(name.upper())}\b""", code)
        implicit = any(re.search(rx, code) and gem == name for rx, gem in IMPLICIT_RB)
        if used or implicit:
            trigger = next((re.search(rx, code).group(0) for rx, gem in IMPLICIT_RB if gem == name and re.search(rx, code)), name)
            out.append({"manager": "Gemfile", "name": name, "version": line.strip(), "module": trigger if implicit else name,
                        "line": line.strip(), "install": f"bundle add {name}", "implicit": implicit})
    return out


def _maven_packages(repo: _Repo, texts: str, have: str) -> list[dict]:
    out = []
    pom = re.sub(r"(?s)<parent>.*?</parent>", "", repo.read("pom.xml"))
    imports = re.findall(r"^import\s+(?:static\s+)?([\w.]+)", texts, re.M)
    for dep in re.findall(r"(?s)<dependency>(.*?)</dependency>", pom):
        g = re.search(r"<groupId>([^<]+)", dep)
        a = re.search(r"<artifactId>([^<]+)", dep)
        v = re.search(r"<version>([^<]+)", dep)
        if not g or not a or f"<artifactId>{a.group(1)}</artifactId>" in have:
            continue
        if any(i.startswith(g.group(1) + ".") for i in imports):
            xml = f"<dependency><groupId>{g.group(1)}</groupId><artifactId>{a.group(1)}</artifactId>" + \
                  (f"<version>{v.group(1)}</version>" if v else "") + "</dependency>"
            out.append({"manager": "pom.xml", "name": f"{g.group(1)}:{a.group(1)}", "version": v.group(1) if v else "",
                        "module": g.group(1), "xml": xml, "install": "add the dependency to pom.xml"})
    return out


def _env_vars(texts: str) -> list[str]:
    found = re.findall(r"""env\(\s*['"]([A-Z][A-Z0-9_]+)['"]|process\.env\.([A-Z][A-Z0-9_]+)|"""
                       r"""os\.environ(?:\.get)?[\[(]\s*['"]([A-Z][A-Z0-9_]+)['"]|[gG]etenv\(\s*['"]([A-Z][A-Z0-9_]+)['"]|"""
                       r"""ENV(?:\.fetch\(|\[)\s*['"]([A-Z][A-Z0-9_]+)['"]""",
                       texts)
    return sorted({v for g in found for v in g if v})


OLD_MANIFESTS = ("composer.json", "package.json", "pyproject.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile")


def bundle(repo_name: str, paths: list[str], project: dict | None = None, depth: int = 3,
           symbol: str | None = None, renames: list[tuple[str, str]] | None = None) -> dict:
    """Everything needed to copy `paths` (optionally: the `symbol` picked in it) from `repo_name`."""
    repo = _Repo(repo_name)
    if not repo.src:
        return {"error": f"unknown repository: {repo_name}"}
    project = project or {}
    tree = set(project.get("tree") or [])
    picked: dict[str, str] = {}
    needs: dict[str, set[str] | None] = {}   # file -> names needed from it (None = whole file)
    refs: dict[str, list[str]] = {}          # consumer file -> only blocks matching these patterns
    for p in paths:
        if repo.read(p):
            picked[p] = "the implementation you picked"
            needs[p] = None
    if not picked:
        return {"error": f"file not found in {repo_name}: {', '.join(paths)}"}
    main = list(picked)

    def text_of(p: str) -> str:
        t = repo.read(p)
        lang = detect_language(p)
        if p in refs:
            return keep_only_patterns(t, lang, refs[p])
        if needs.get(p) is not None:
            return keep_only(t, lang, needs[p])
        return t

    def follow(frontier: list[str]) -> None:
        for _ in range(depth):
            nxt = []
            for p in frontier:
                for dep, why, names in _dependencies(repo, p, text_of(p)):
                    if dep in picked:
                        if needs.get(dep) is not None and dep not in main:
                            needs[dep] = None if names is None else needs[dep] | names
                        continue
                    if len(picked) < MAX_FILES:
                        picked[dep] = why
                        needs[dep] = names
                        nxt.append(dep)
            for dep, why in _migrations(repo, picked):
                if dep not in picked and len(picked) < MAX_FILES:
                    picked[dep] = why
                    needs[dep] = None
            frontier = nxt
            if not frontier:
                break

    follow(main)
    consumers = []
    for dep, why, pats in _consumers(repo, picked, main, symbol):
        if dep not in picked and len(picked) < MAX_FILES:
            picked[dep] = why
            needs[dep] = None
            refs[dep] = pats
            consumers.append(dep)
    follow(consumers)
    setup = _entry_setup(repo, picked, needs)
    wiring: dict[str, list[dict]] = {}
    modules = {_stem(p) for p in consumers}
    for dep, why, snips in _wiring(repo, picked, modules, setup):
        if dep not in picked and len(picked) < MAX_FILES + 2:
            picked[dep] = why
            wiring[dep] = snips

    files = []
    for p, why in picked.items():
        text = text_of(p)
        partial = text != repo.read(p)
        files.append({"path": p, "target": p, "reason": why + (" (only the parts needed)" if partial else ""),
                      "language": detect_language(p), "exists_in_project": p in tree, "content": text,
                      "role": "wiring" if p in wiring and (p in tree or ENTRY.search(p)) else
                              ("picked" if p in main else "dependency"),
                      "snippets": wiring.get(p, [])})
    # make it fit the new project: its name, the developer's renames, Go module / Java package
    old_ctx = {"name": repo_name, "tree": repo.paths, "files": {m: repo.read(m) for m in OLD_MANIFESTS}}
    new_ctx = {"name": project.get("name") or "", "tree": list(tree), "files": project.get("files") or {}}
    adapted = adapt(files, old_ctx, new_ctx, renames) if project or renames else []
    for f in files:
        f["exists_in_project"] = f["target"] in tree
    alltext = "\n".join(f["content"] for f in files if f["role"] != "wiring")
    manifests = {k: v for k, v in (project.get("files") or {}).items() if k in ("composer.json", "package.json",
                                                                                 "requirements.txt", "pyproject.toml",
                                                                                 "go.mod", "Gemfile", "pom.xml")}
    return {"repo": repo_name, "files": files, "packages": _packages(repo, alltext, manifests),
            "env": _env_vars(alltext), "adapted": adapted}


def keep_only_patterns(text: str, lang: str, patterns: list[str]) -> str:
    """Keep the top-level blocks matching any regex in `patterns` (+ what they use)."""
    blocks = top_level(text, lang)
    hits = {n for n, s, e in blocks if any(re.search(p, text[s:e]) for p in patterns)}
    return keep_only(text, lang, hits) if hits else text
