"""Team mode: one shared Tracebase for an agency or team, with per-person tokens, repo access and an audit log.

Off by default (one developer on their own machine). Set TRACEBASE_ADMIN_TOKEN to turn it on: every /api call
then needs `Authorization: Bearer <token>`, either the admin token or a member token created with
`tracebase team add`. A member only sees the repos their patterns allow ("freelance/*", "clinic-app"): search,
ask, read, copy and the dashboard are all filtered. The same patterns cover log services (Log Detective):
"shop-api" = the shop-api repo and its logs, errors and alerts. Tokens are stored as SHA-256 hashes, never in clear."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .config import settings
from .db import row, rows, tx


@dataclass(frozen=True)
class User:
    name: str
    role: str                      # admin | member
    patterns: tuple[str, ...] = ("*",)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


LOCAL = User("local", "admin")
_current: ContextVar[User] = ContextVar("tracebase_user", default=LOCAL)


def enabled() -> bool:
    return bool(settings.admin_token)


def current() -> User:
    return _current.get()


def set_current(user: User):
    return _current.set(user)


# ------------------------------------------------------------------ repo access
def _matches(repo: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(repo, p) or repo.endswith("/" + p) or fnmatch.fnmatchcase(repo.split("/")[-1], p)
               for p in patterns)


def can(repo: str) -> bool:
    u = current()
    return u.is_admin or "*" in u.patterns or _matches(repo, u.patterns)


def allowed_repos() -> list[str] | None:
    """Repo names the current user may see, or None for all of them."""
    u = current()
    if u.is_admin or "*" in u.patterns:
        return None
    return [r["name"] for r in rows("SELECT name FROM repos") if _matches(r["name"], u.patterns)]


def sql_filter(column: str = "repo") -> tuple[str, list]:
    """(' AND column IN (...)', params) restricting a query to the current user's repos ('' when unrestricted)."""
    names = allowed_repos()
    if names is None:
        return "", []
    if not names:
        return " AND 1=0", []
    return f" AND {column} IN ({','.join('?' * len(names))})", list(names)


# Log Detective: a member's patterns cover log services too ("shop-api" = the shop-api repo AND its logs)
def can_service(service: str | None) -> bool:
    u = current()
    return u.is_admin or "*" in u.patterns or bool(service and _matches(service, u.patterns))


def allowed_services() -> list[str] | None:
    u = current()
    if u.is_admin or "*" in u.patterns:
        return None
    names = {r["service"] for r in rows("SELECT DISTINCT service FROM log_events")} | \
            {r["service"] for r in rows("SELECT DISTINCT service FROM error_groups")}
    return sorted(s for s in names if s and _matches(s, u.patterns))


def service_filter(column: str = "service") -> tuple[str, list]:
    names = allowed_services()
    if names is None:
        return "", []
    if not names:
        return " AND 1=0", []
    return f" AND {column} IN ({','.join('?' * len(names))})", list(names)


def require_service(service: str | None) -> None:
    if not can_service(service):
        raise HTTPException(404, "not found")


# ------------------------------------------------------------------ members and tokens
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return "tb_" + secrets.token_urlsafe(24)


def add_user(name: str, patterns: list[str], role: str = "member") -> str:
    if role not in ("member", "admin"):
        raise ValueError("role must be member or admin")
    if not name or not all(c.isalnum() or c in "-_.@" for c in name):
        raise ValueError("name may contain letters, digits and - _ . @")
    token = _new_token()
    with tx() as c:
        c.execute("INSERT INTO users(name, token_hash, role, repos) VALUES(?,?,?,?)",
                  (name, _hash(token), role, json.dumps(patterns or ["*"])))
    return token


def rotate(name: str) -> str:
    token = _new_token()
    with tx() as c:
        if c.execute("UPDATE users SET token_hash=? WHERE name=?", (_hash(token), name)).rowcount == 0:
            raise KeyError(name)
    return token


def update_repos(name: str, patterns: list[str]) -> None:
    with tx() as c:
        if c.execute("UPDATE users SET repos=? WHERE name=?", (json.dumps(patterns or ["*"]), name)).rowcount == 0:
            raise KeyError(name)


def remove_user(name: str) -> None:
    with tx() as c:
        if c.execute("DELETE FROM users WHERE name=?", (name,)).rowcount == 0:
            raise KeyError(name)


def list_users() -> list[dict]:
    out = rows("SELECT name, role, repos, created_at, last_seen FROM users ORDER BY name")
    for u in out:
        u["repos"] = json.loads(u["repos"] or '["*"]')
    return out


def from_token(token: str) -> User | None:
    if settings.admin_token and hmac.compare_digest(token, settings.admin_token):
        return User("admin", "admin")
    r = row("SELECT name, role, repos FROM users WHERE token_hash=?", (_hash(token),))
    if not r:
        return None
    with tx() as c:
        c.execute("UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE name=?", (r["name"],))
    return User(r["name"], r["role"], tuple(json.loads(r["repos"] or '["*"]')))


# ------------------------------------------------------------------ audit log
def log(action: str, repo: str | None = None, **detail) -> None:
    """Record who did what (only in team mode: a single developer doesn't need an audit trail)."""
    if not enabled():
        return
    with tx() as c:
        c.execute("INSERT INTO audit(user, action, repo, detail) VALUES(?,?,?,?)",
                  (current().name, action, repo, json.dumps(detail, ensure_ascii=False)[:4000]))


def audit(user: str | None = None, action: str | None = None, limit: int = 100) -> list[dict]:
    q, p = "SELECT at, user, action, repo, detail FROM audit WHERE 1=1", []
    if user:
        q, p = q + " AND user=?", p + [user]
    if action:
        q, p = q + " AND action=?", p + [action]
    out = rows(q + " ORDER BY id DESC LIMIT ?", tuple(p + [limit]))
    for e in out:
        e["detail"] = json.loads(e["detail"] or "{}")
    return out


# ------------------------------------------------------------------ FastAPI dependency
OPEN = ("/api/health",)


async def authenticate(request: Request) -> None:
    """Runs before every route (async, so the user set here is visible to sync endpoints in the threadpool)."""
    path = request.url.path
    if not enabled():
        set_current(LOCAL)
        return
    if not path.startswith("/api/") or path in OPEN:
        set_current(User("anonymous", "member", ()))
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""  # never in the URL: it would end up in logs
    user = from_token(token) if token else None
    if not user:
        raise HTTPException(401, "Tracebase is in team mode: send `Authorization: Bearer <token>` "
                                 "(run `tracebase login <token>`)")
    set_current(user)


def require_admin() -> None:
    if not current().is_admin:
        raise HTTPException(403, "admin only")


def require_repo(repo: str | None) -> None:
    if repo and not can(repo):
        raise HTTPException(404, f"unknown repository: {repo}")
