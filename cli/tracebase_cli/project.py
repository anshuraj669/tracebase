"""Read the project in the current folder: file tree (respecting .gitignore) and the files most relevant
to a task. Only this context is sent to the Tracebase server; nothing is written here."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pathspec

SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "dist", "build", ".next",
             ".nuxt", "target", ".idea", ".vscode", "storage", "coverage", ".tracebase", ".pytest_cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mp3",
            ".woff", ".woff2", ".ttf", ".eot", ".lock", ".map", ".min.js", ".sqlite", ".db", ".jar", ".class"}
MANIFESTS = {"composer.json", "package.json", "requirements.txt", "pyproject.toml", "go.mod", "Gemfile",
             "pom.xml", "build.gradle", "Cargo.toml", "tsconfig.json"}

# files that wire a feature into an app (routes, middleware registration, entry points, auth config)
WIRING = re.compile(
    r"(^|/)(routes/[^/]+\.php|app/Http/Kernel\.php|bootstrap/app\.php|config/auth\.php|app/Models/User\.php|"
    r"app/Providers/AuthServiceProvider\.php|(src/)?(app|server|index|main)\.(ts|js|mjs)|(src/)?routes?/[^/]+\.(ts|js)|"
    r"settings\.py|urls\.py|main\.py|app\.py|models\.py|cmd/[^/]+/main\.go)$")

TOPICS = [
    (r"log ?in|sign ?in|auth|jwt|token|session|password", ["auth", "login", "user", "session", "token", "guard", "password"]),
    (r"rbac|role|permission|access control|authori[sz]|admin|policy|gate",
     ["role", "permission", "policy", "gate", "user", "middleware", "auth", "admin"]),
    (r"upload|image|file", ["upload", "file", "image", "storage", "media"]),
    (r"payment|stripe|checkout|subscription|webhook", ["payment", "stripe", "checkout", "webhook", "order", "invoice"]),
    (r"mail|email|notif", ["mail", "notification", "notify"]),
]


def _gitignore(root: Path) -> pathspec.PathSpec | None:
    f = root / ".gitignore"
    if f.is_file():
        lines = f.read_text(errors="ignore").splitlines()
        for style in ("gitignore", "gitwildmatch"):  # "gitignore" in pathspec >= 0.12.2
            try:
                return pathspec.PathSpec.from_lines(style, lines)
            except Exception:
                continue
    return None


def scan_tree(root: Path, limit: int = 3000) -> list[str]:
    spec = _gitignore(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                             and not (spec and spec.match_file(f"{'' if rel_dir == '.' else rel_dir + '/'}{d}/")))
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            low = name.lower()
            if any(low.endswith(e) for e in SKIP_EXT) or name.startswith(".env"):
                continue
            if spec and spec.match_file(rel):
                continue
            out.append(rel)
            if len(out) >= limit:
                return out
    return out


def read_text(root: Path, rel: str, max_bytes: int = 200_000) -> str | None:
    p = root / rel
    try:
        if not p.is_file() or p.stat().st_size > max_bytes:
            return None
        data = p.read_bytes()
        if b"\x00" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def keywords(task: str) -> list[str]:
    low = task.lower()
    words = [w for w in re.findall(r"[a-z]{3,}", low) if w not in {"the", "and", "add", "with", "from", "into",
                                                                   "implement", "create", "write", "make", "for",
                                                                   "project", "projects", "search", "this", "function",
                                                                   "like", "use", "using", "based", "previous", "old"}]
    for rx, extra in TOPICS:
        if re.search(rx, low):
            words += extra
    return list(dict.fromkeys(words))


def context(root: Path, task: str = "", max_files: int = 14, budget: int = 60_000) -> dict:
    """{name, tree, files{path: text}}: manifests + wiring files + files whose path matches the task."""
    tree = scan_tree(root)
    kws = keywords(task)

    def score(p: str) -> float:
        name = p.split("/")[-1]
        s = 0.0
        if name in MANIFESTS and p.count("/") == 0:
            s += 10
        if WIRING.search(p):
            s += 4
        low = p.lower()
        s += sum(2.0 for k in kws if k in low)
        if "test" in low or low.startswith("docs/"):
            s -= 3
        return s

    ranked = sorted((p for p in tree if score(p) > 0), key=lambda p: (-score(p), p.count("/"), p))
    # classes the routes/wiring files point at (e.g. BookingController), so the model knows what exists
    by_stem = {}
    for p in tree:
        by_stem.setdefault(Path(p).stem, p)
    referenced = []
    for p in ranked[:max_files]:
        if WIRING.search(p):
            for name in re.findall(r"\b([A-Z]\w+(?:Controller|Handler|Service))\b", read_text(root, p) or ""):
                if name in by_stem and by_stem[name] not in ranked[:max_files] + referenced:
                    referenced.append(by_stem[name])
    ranked = ranked[:max_files] + referenced[:4] + ranked[max_files:]
    max_files += min(len(referenced), 4)
    files, used = {}, 0
    for p in ranked[:max_files * 2]:
        text = read_text(root, p)
        if text is None:
            continue
        text = text if len(text) <= 12_000 else text[:12_000] + "\n[truncated]"
        if used + len(text) > budget:
            continue
        files[p] = text
        used += len(text)
        if len(files) >= max_files:
            break
    return {"name": root.name, "tree": tree, "files": files}
