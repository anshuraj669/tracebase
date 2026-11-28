"""SQLite storage (metadata + FTS5 full-text search + embedding blobs).

Encryption at rest: set TRACEBASE_DB_KEY (or TRACEBASE_DB_KEY_FILE) and the database is opened with SQLCipher
(AES-256), so a copied disk, volume or backup is unreadable without the key. An existing plain database is
encrypted in place on first start with a key. Without a key, plain SQLite is used: protect the disk instead
(LUKS, an encrypted EBS volume). Deleted rows are overwritten (secure_delete), so purged data does not linger
in free pages.
"""
from __future__ import annotations

import logging
import os
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
-- sign-in sessions (Continue with Google): a token per browser, stored hashed, expiring
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_name TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE,
    via TEXT NOT NULL DEFAULT 'google',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);
-- connected projects: apps that send their logs and errors with a write-only ingest key (tbk_...)
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,              -- also the service its logs are stored under
    platform TEXT NOT NULL DEFAULT 'other',
    key_hash TEXT NOT NULL UNIQUE,
    key_hint TEXT NOT NULL,                 -- the first characters, to recognise a key
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_event_at TEXT,
    events INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    user TEXT, action TEXT, repo TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);

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
CREATE INDEX IF NOT EXISTS idx_groups_last_seen ON error_groups(last_seen);
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
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);

-- ---------- Feature flags (flags/) ----------
CREATE TABLE IF NOT EXISTS flag_changes (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    provider TEXT DEFAULT 'flagsmith',
    project TEXT, project_id INTEGER,
    environment TEXT, environment_id INTEGER,
    feature TEXT NOT NULL, feature_id INTEGER,
    segment TEXT, segment_id INTEGER,
    identity TEXT,
    enabled_before INTEGER, enabled_after INTEGER,
    value_before TEXT, value_after TEXT,
    rollout_before TEXT, rollout_after TEXT,
    summary TEXT,
    changed_by TEXT,
    source TEXT DEFAULT 'webhook',
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_flag_changes_ts ON flag_changes(ts);
CREATE INDEX IF NOT EXISTS idx_flag_changes_feature ON flag_changes(feature, ts);
CREATE TABLE IF NOT EXISTS flag_rollbacks (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    group_id INTEGER,
    change_id INTEGER,
    feature TEXT, environment TEXT, segment TEXT,
    feature_state_id INTEGER,
    approved_by TEXT,
    status TEXT DEFAULT 'watching',     -- watching | worked | did_not_help | failed
    errors_before INTEGER, errors_after INTEGER,
    checked_at TEXT,
    note TEXT
);

-- ---------- Shared ----------
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

log = logging.getLogger(__name__)
_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()
PLAIN_HEADER = b"SQLite format 3\x00"


class DatabaseKeyError(RuntimeError):
    pass


def _driver():
    if not settings.db_key:
        return sqlite3
    try:
        from sqlcipher3 import dbapi2  # pip install sqlcipher3-binary (Linux x86-64) or sqlcipher3 (+ libsqlcipher)
    except ImportError as e:
        raise DatabaseKeyError(
            "TRACEBASE_DB_KEY is set but SQLCipher is not installed. Install `sqlcipher3-binary` (Linux x86-64) "
            "or `sqlcipher3` with libsqlcipher, or use the Docker image, which includes it.") from e
    return dbapi2


def _quote(key: str) -> str:
    return "'" + key.replace("'", "''") + "'"


def _is_plain(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == PLAIN_HEADER
    except OSError:
        return False


def _encrypt_existing(path: str) -> None:
    """A plain database + a key: copy it into an encrypted database, then swap the files (once)."""
    drv = _driver()
    tmp = path + ".encrypting"
    for f in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    src = sqlite3.connect(path)
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src.close()
    conn = drv.connect(path)
    conn.execute(f"ATTACH DATABASE {_quote(tmp)} AS enc KEY {_quote(settings.db_key)}")
    conn.execute("SELECT sqlcipher_export('enc')")
    conn.execute("DETACH DATABASE enc")
    conn.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for f in (path + "-wal", path + "-shm"):
        if os.path.exists(f):
            os.remove(f)  # held the old plain pages
    log.warning("database encrypted with TRACEBASE_DB_KEY: %s (old plain copies in backups are still readable)", path)


def _open(path: str):
    drv = _driver()
    if settings.db_key and _is_plain(path):
        _encrypt_existing(path)
    conn = drv.connect(path, timeout=30, check_same_thread=False)
    if settings.db_key:
        conn.execute(f"PRAGMA key = {_quote(settings.db_key)}")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except drv.DatabaseError as e:
        conn.close()
        if settings.db_key:
            raise DatabaseKeyError(f"cannot open {path}: wrong TRACEBASE_DB_KEY (or the file is damaged)") from e
        raise DatabaseKeyError(f"cannot open {path}: it is encrypted, set TRACEBASE_DB_KEY") from e
    conn.row_factory = drv.Row
    conn.execute("PRAGMA secure_delete=ON")  # deleted rows are zeroed, not left in free pages
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# columns added after a table first shipped: (table, column, declaration)
COLUMNS = [("users", "email", "TEXT")]


def _add_columns(conn) -> None:
    for table, col, decl in COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(lower(email)) WHERE email IS NOT NULL")
    conn.commit()


def open_private():
    """A separate connection (same key and settings), for long reads that must not share the thread's connection."""
    connect()  # schema + one-time encryption first
    return _open(str(settings.db_path))


def encrypted() -> bool:
    return bool(settings.db_key)


def connect() -> sqlite3.Connection:
    """One connection per thread; schema created lazily."""
    path = str(settings.db_path)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        conn = _open(path)
        if path not in _initialized:
            conn.executescript(SCHEMA)
            _add_columns(conn)
            _initialized.add(path)
            for f in (path, path + "-wal", path + "-shm"):
                try:
                    os.chmod(f, 0o600)
                except OSError:
                    pass
    _local.conn, _local.path = conn, path
    return conn


def checkpoint() -> None:
    """Fold the write-ahead log into the database and truncate it (after a purge: no old pages left in the WAL)."""
    try:
        connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:  # pragma: no cover (busy readers: retried on the next purge)
        log.warning("wal checkpoint failed: %s", e)


def size_bytes() -> int:
    p = str(settings.db_path)
    return sum(os.path.getsize(f) for f in (p, p + "-wal") if os.path.exists(f))


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
