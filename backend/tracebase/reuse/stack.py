"""Detect a project's stack (languages + frameworks) from its manifest files and file list.
Used both for the developer's *new* project (manifests sent by the CLI) and for the indexed
past repos, so examples from the same framework can be preferred."""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import Callable

MANIFESTS = ("composer.json", "package.json", "requirements.txt", "pyproject.toml", "go.mod",
             "Gemfile", "pom.xml", "build.gradle", "Cargo.toml")

EXT_LANG = {
    ".php": "php", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".py": "python", ".go": "go", ".rb": "ruby", ".java": "java", ".kt": "kotlin",
    ".rs": "rust", ".cs": "csharp", ".vue": "vue", ".svelte": "svelte",
}

# (manifest, regex on its text, framework name)
FRAMEWORK_RULES = [
    ("composer.json", r'"laravel/framework"', "laravel"),
    ("composer.json", r'"symfony/framework-bundle"', "symfony"),
    ("composer.json", r'"tymon/jwt-auth"', "jwt-auth"),
    ("composer.json", r'"spatie/laravel-permission"', "spatie-permission"),
    ("package.json", r'"next"\s*:', "nextjs"),
    ("package.json", r'"@nestjs/core"', "nestjs"),
    ("package.json", r'"express"\s*:', "express"),
    ("package.json", r'"fastify"\s*:', "fastify"),
    ("package.json", r'"koa"\s*:', "koa"),
    ("package.json", r'"react"\s*:', "react"),
    ("package.json", r'"vue"\s*:', "vue"),
    ("package.json", r'"prisma"|"@prisma/client"', "prisma"),
    ("package.json", r'"mongoose"', "mongoose"),
    ("package.json", r'"jsonwebtoken"', "jsonwebtoken"),
    ("requirements.txt", r"(?im)^django\b", "django"),
    ("requirements.txt", r"(?im)^flask\b", "flask"),
    ("requirements.txt", r"(?im)^fastapi\b", "fastapi"),
    ("pyproject.toml", r"(?i)\bdjango\b", "django"),
    ("pyproject.toml", r"(?i)\bflask\b", "flask"),
    ("pyproject.toml", r"(?i)\bfastapi\b", "fastapi"),
    ("go.mod", r"gin-gonic/gin", "gin"),
    ("go.mod", r"labstack/echo", "echo"),
    ("go.mod", r"go-chi/chi", "chi"),
    ("go.mod", r"gofiber/fiber", "fiber"),
    ("Gemfile", r"['\"]sinatra['\"]", "sinatra"),
    ("Gemfile", r"['\"]rails['\"]", "rails"),
    ("pom.xml", r"spring-boot", "spring"),
    ("build.gradle", r"spring-boot", "spring"),
]
# frameworks that define the *shape* of a project (used for "same framework" matching)
PRIMARY = {"laravel", "symfony", "nextjs", "nestjs", "express", "fastify", "koa", "django", "flask",
           "fastapi", "gin", "echo", "chi", "fiber", "rails", "sinatra", "spring"}


def detect(paths: list[str], read: Callable[[str], str | None]) -> dict:
    """paths: project-relative file paths; read(path) -> file text or None."""
    langs = Counter()
    for p in paths:
        m = re.search(r"\.[A-Za-z0-9]+$", p)
        if m and m.group(0).lower() in EXT_LANG:
            langs[EXT_LANG[m.group(0).lower()]] += 1
    frameworks: list[str] = []
    present = {p for p in paths if p.split("/")[-1] in MANIFESTS and p.count("/") <= 1}
    for manifest, rx, name in FRAMEWORK_RULES:
        for p in present:
            if p.split("/")[-1] != manifest:
                continue
            text = read(p) or ""
            if re.search(rx, text) and name not in frameworks:
                frameworks.append(name)
    if "package.json" in {p.split("/")[-1] for p in present} and not langs:
        langs["javascript"] += 1
    if langs.get("typescript") and langs.get("javascript"):
        langs["typescript"] += langs.pop("javascript")
    return {
        "languages": [l for l, _ in langs.most_common(4)],
        "frameworks": frameworks,
        "primary": next((f for f in frameworks if f in PRIMARY), None),
    }


def describe(stack: dict) -> str:
    parts = []
    if stack.get("primary"):
        parts.append(stack["primary"])
    parts += [f for f in stack.get("frameworks", []) if f != stack.get("primary")]
    langs = ", ".join(stack.get("languages", [])) or "unknown language"
    return f"{langs}" + (f" ({', '.join(parts)})" if parts else "")


@lru_cache(maxsize=256)
def _repo_stack_cached(repo: str, stamp: str) -> dict:
    from ..code.sources import get_source
    from ..db import rows

    paths = [r["path"] for r in rows("SELECT path FROM files WHERE repo=?", (repo,))]
    src = get_source(repo)
    # manifests are not always indexed (e.g. json can be skipped), so also probe the source directly
    found = {m for m in MANIFESTS if src and src.read(m) is not None}
    return detect(sorted(set(paths) | found), lambda p: src.read(p) if src else None)


def repo_stack(repo: str) -> dict:
    """Stack of an indexed repo (cached until the repo is re-indexed)."""
    from ..db import row

    r = row("SELECT indexed_at FROM repos WHERE name=?", (repo,))
    return _repo_stack_cached(repo, (r or {}).get("indexed_at") or "")

