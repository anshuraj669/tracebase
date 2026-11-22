"""Find the developer's own past implementations of a feature ("login", "role based access control")
across every indexed repo, preferring code in the same language/framework as the new project.
Purely local: hybrid search over the index, no LLM and no network."""
from __future__ import annotations

import re

from ..code.search import search
from ..db import row
from .stack import describe, repo_stack

# Common features -> extra search queries, so a short request still finds every relevant piece.
FEATURES: list[tuple[str, list[str]]] = [
    (r"\b(log ?in|sign ?in|authenticat\w*|auth)\b", [
        "login authenticate user credentials", "issue token after login", "logout invalidate session token",
        "current authenticated user me"]),
    (r"\b(jwt|refresh token|access token|bearer)\b", [
        "jwt sign verify token", "refresh token rotate", "respond with token expires"]),
    (r"\b(rbac|roles?|permissions?|access control|authori[sz]\w*|admin only|guard)\b", [
        "check user role middleware", "user has role permission", "roles permissions tables migration",
        "authorize gate policy permission", "require role route protection"]),
    (r"\b(password reset|forgot password|reset password)\b", [
        "send password reset email", "reset password token link"]),
    (r"\b(upload|image|thumbnail|resize)\b", ["resize uploaded image thumbnail", "store uploaded file"]),
    (r"\b(stripe|payment|checkout|webhook|subscription)\b", [
        "stripe webhook signature verify", "create checkout payment", "subscription invoice paid"]),
    (r"\b(e-?mail|mail|notification)\b", ["send email notification", "mail transport template"]),
    (r"\b(coupon|discount|promo)\b", ["apply coupon discount cart total"]),
    (r"\b(rate ?limit|throttl\w*)\b", ["rate limit requests throttle"]),
    (r"\b(cache|redis)\b", ["redis cache get set ttl"]),
]

# Words that describe the request, not the code ("search my projects and copy the login logic here").
FILLER = re.compile(
    r"\b(search|find|look(ing)? (for|up)|in|from|my|our|the|a|an|old(er)?|previous|past|projects?|repos?|"
    r"repositor(y|ies)|code(base)?|copy|paste|bring|reuse|take|get|write|put|add|implement|create|make|it|"
    r"here|there|this|that|new|current|and|then|to|into|for|of|me|please|logic|function(ality)?|feature|"
    r"like|i|did|before|used|same|way|one)\b", re.I)


TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|[._-](test|spec)\.[a-z]+$", re.I)


def clean_task(task: str) -> str:
    core = re.sub(r"\s+", " ", FILLER.sub(" ", task)).strip(" ,.?!")
    return core or task.strip()


def expand(task: str) -> list[str]:
    core = clean_task(task)
    queries = [core]
    low = task.lower()
    for rx, qs in FEATURES:
        if re.search(rx, low):
            queries += qs
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


def find_examples(task: str, stack: dict | None = None, limit: int = 8, exclude_repo: str | None = None) -> dict:
    stack = stack or {}
    queries = expand(task)
    langs = set(stack.get("languages") or [])
    primary = stack.get("primary")

    wants_tests = bool(re.search(r"\btests?\b|\bspec\b", task, re.I))
    acc: dict[tuple, dict] = {}
    for qi, q in enumerate(queries):
        weight = 1.0 if qi == 0 else 0.85
        for r in search(q, limit=12)["results"]:
            if exclude_repo and (r["repo"] == exclude_repo or r["repo"].endswith("/" + exclude_repo)):
                continue
            if r["kind"] in ("file", "module") and r["language"] in ("json", "yaml", "dotenv", "markdown", "text"):
                continue
            key = (r["repo"], r["path"], r["lines"][0])
            hit = acc.setdefault(key, {**r, "score": 0.0, "hits": 0})
            hit["score"] = max(hit["score"], r["score"] * weight)
            hit["hits"] += 1

    ranked = []
    for h in acc.values():
        s = h["score"] + 0.04 * min(h["hits"], 5)
        if langs and h["language"] in langs:
            s *= 1.25
        if primary and repo_stack(h["repo"]).get("primary") == primary:
            s *= 1.2
        if not wants_tests and TEST_PATH.search(h["path"]):
            s *= 0.55  # copying a feature means its code, not its tests
        ranked.append((s, h))
    ranked.sort(key=lambda x: -x[0])

    out, per_repo = [], {}
    for s, h in ranked:
        if per_repo.get(h["repo"], 0) >= 4:
            continue
        per_repo[h["repo"]] = per_repo.get(h["repo"], 0) + 1
        ch = row("SELECT content FROM chunks WHERE repo=? AND path=? AND start_line=? LIMIT 1",
                 (h["repo"], h["path"], h["lines"][0]))
        out.append({
            "id": len(out) + 1, "repo": h["repo"], "path": h["path"], "lines": h["lines"],
            "symbol": h["symbol"], "kind": h["kind"], "language": h["language"],
            "framework": repo_stack(h["repo"]).get("primary"), "score": round(min(s, 1.5), 2),
            "code": (ch["content"] if ch else h["preview"])[:3500],
        })
        if len(out) >= limit:
            break
    return {"task": task, "query": clean_task(task), "queries": queries, "examples": out,
            "stack_text": describe(stack) if stack else ""}
