"""SQLite storage (metadata + FTS5 full-text search + embedding blobs)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------- Code ----------
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    language TEXT,
    mtime REAL,
    size INTEGER,
    sha TEXT,
    UNIQUE(repo, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    language TEXT,
    symbol TEXT,
    kind TEXT,
    start_line INTEGER,
    end_line INTEGER,
    content TEXT,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_repo_path ON chunks(repo, path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    symbol, path, content, tokens,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS repos (
    name TEXT PRIMARY KEY,
    root TEXT,
    kind TEXT,
    files INTEGER DEFAULT 0,
    chunks INTEGER DEFAULT 0,
    languages TEXT,
    indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS secrets (
    id INTEGER PRIMARY KEY,
    repo TEXT, path TEXT, line INTEGER,
    severity TEXT, rule TEXT, preview TEXT,
    found_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(repo, path, line, rule)
);

-- ---------- Team mode ----------
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'member',
    repos TEXT NOT NULL DEFAULT '["*"]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    user TEXT, action TEXT, repo TEXT, detail TEXT
);

-- ---------- Log Detective ----------
CREATE TABLE IF NOT EXISTS error_groups (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    service TEXT,
    level TEXT,
    title TEXT,
    normalized TEXT,
    count INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'new',
    sample TEXT,
    stack TEXT,
    frames TEXT,
    explanation TEXT
);
CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    level TEXT,
    service TEXT,
    message TEXT,
    source TEXT,
    group_id INTEGER REFERENCES error_groups(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON log_events(ts);
CREATE INDEX IF NOT EXISTS idx_log_service ON log_events(service, ts);
CREATE INDEX IF NOT EXISTS idx_log_group ON log_events(group_id, ts);
CREATE TABLE IF NOT EXISTS log_offsets (
    path TEXT PRIMARY KEY,
    inode INTEGER,
    offset INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    kind TEXT,
    severity TEXT,
    service TEXT,
    title TEXT,
    body TEXT,
    link TEXT,
    read INTEGER DEFAULT 0,
    dedupe_key TEXT UNIQUE
);

-- ---------- Shared ----------
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def connect() -> sqlite3.Connection:
    """One connection per thread; schema created lazily."""
    path = str(settings.db_path)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _init_lock:
        if path not in _initialized:
            conn.executescript(SCHEMA)
            _initialized.add(path)
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn, _local.path = conn, path
    return conn


def reset_for_tests() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _initialized.clear()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rows(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def row(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    r = connect().execute(sql, params).fetchone()
    return dict(r) if r else None


def kv_get(key: str, default: str | None = None) -> str | None:
    r = row("SELECT value FROM kv WHERE key=?", (key,))
    return r["value"] if r else default


def kv_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
