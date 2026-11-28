"""Connected projects: apps that send their logs and errors to Tracebase themselves.

Each project gets a write-only ingest key (tbk_...). The key can only POST logs to /api/logs/ingest, and only under
the project's own name (its service): it can't read anything, so it is safe in a browser bundle or a server's
environment. Keys are stored as SHA-256 hashes and shown once; a leaked key is replaced with rotate()."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time

from .db import row, rows, tx

PLATFORMS = ("browser", "node", "python", "logs", "other")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
PREFIX = "tbk_"

# a runaway client (an error in a render loop) must not flood the store: per project, per minute
EVENTS_PER_MINUTE = 6000
_window: dict[str, tuple[int, int]] = {}
_lock = threading.Lock()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _new_key() -> str:
    return PREFIX + secrets.token_urlsafe(24)


def is_key(token: str) -> bool:
    return token.startswith(PREFIX)


def _public(p: dict) -> dict:
    return {k: p[k] for k in ("name", "platform", "key_hint", "created_by", "created_at", "last_event_at", "events")}


def create(name: str, platform: str, created_by: str) -> tuple[dict, str]:
    name = name.strip().lower()
    if not NAME.match(name):
        raise ValueError("use 2-63 lowercase letters, digits, '.', '-' or '_', starting with a letter or digit")
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of: {', '.join(PLATFORMS)}")
    key = _new_key()
    with tx() as c:
        if c.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise FileExistsError(name)
        c.execute("INSERT INTO projects(name, platform, key_hash, key_hint, created_by) VALUES(?,?,?,?,?)",
                  (name, platform, _hash(key), key[:10], created_by))
    return get(name), key


def get(name: str) -> dict | None:
    p = row("SELECT * FROM projects WHERE name=?", (name,))
    return _public(p) if p else None


def list_all() -> list[dict]:
    out = [_public(p) for p in rows("SELECT * FROM projects ORDER BY name")]
    counts = {r["service"]: r for r in rows(
        "SELECT service, SUM(status IN ('new','regressed')) open, COUNT(*) groups, MAX(last_seen) last_error "
        "FROM error_groups GROUP BY service")}
    for p in out:
        c = counts.get(p["name"]) or {}
        p.update({"open_errors": c.get("open") or 0, "error_types": c.get("groups") or 0,
                  "last_error_at": c.get("last_error")})
    return out


def rotate(name: str) -> str:
    key = _new_key()
    with tx() as c:
        if c.execute("UPDATE projects SET key_hash=?, key_hint=? WHERE name=?", (_hash(key), key[:10], name)).rowcount == 0:
            raise KeyError(name)
    return key


def delete(name: str) -> None:
    with tx() as c:
        if c.execute("DELETE FROM projects WHERE name=?", (name,)).rowcount == 0:
            raise KeyError(name)


def from_key(key: str) -> dict | None:
    p = row("SELECT name, platform FROM projects WHERE key_hash=?", (_hash(key),))
    return dict(p) if p else None


def allow(name: str, n: int) -> bool:
    """Rate limit: False when this project already sent EVENTS_PER_MINUTE events in the current minute."""
    minute = int(time.time() // 60)
    with _lock:
        m, used = _window.get(name, (minute, 0))
        if m != minute:
            used = 0
        if used + n > EVENTS_PER_MINUTE:
            _window[name] = (minute, used)
            return False
        _window[name] = (minute, used + n)
        return True


def record(name: str, events: int) -> None:
    with tx() as c:
        c.execute("UPDATE projects SET events=events+?, last_event_at=CURRENT_TIMESTAMP WHERE name=?", (events, name))
