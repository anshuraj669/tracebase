"""Incremental code indexer: discover repos → chunk changed files → SQLite + FTS5 + vectors."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone

from ..db import connect, rows, tx
from ..embeddings import embedder, to_blob, vector_index
from ..textutil import identifier_text
from . import secrets
from .chunker import chunk_file, detect_language
from .sources import Source, discover, sha1

log = logging.getLogger(__name__)
_lock = threading.Lock()
status: dict = {"running": False, "last_run": None, "last_stats": None, "current": None}


@contextmanager
def lock(timeout: float = 600):
    """Hold the indexer lock (privacy deletes wait for a running index pass, so it can't re-add what they remove)."""
    got = _lock.acquire(timeout=timeout)
    try:
        yield
    finally:
        if got:
            _lock.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _delete_file(c, file_id: int) -> None:
    ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
    c.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
    c.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    c.execute("DELETE FROM files WHERE id=?", (file_id,))


def index_repo(name: str, src: Source, force: bool = False) -> dict:
    conn = connect()
    existing = {r["path"]: r for r in rows("SELECT id, path, mtime, size, sha FROM files WHERE repo=?", (name,))}
    seen: set[str] = set()
    stats = Counter()
    for fi in src.files():
        lang = detect_language(fi.path)
        if lang is None:
            continue
        seen.add(fi.path)
        old = existing.get(fi.path)
        if old and not force and old["mtime"] == fi.mtime and old["size"] == fi.size:
            stats["unchanged"] += 1
            continue
        text = src.read(fi.path)
        if text is None:
            continue
        digest = sha1(text)
        with tx() as c:
            if old and old["sha"] == digest and not force:
                c.execute("UPDATE files SET mtime=?, size=? WHERE id=?", (fi.mtime, fi.size, old["id"]))
                stats["unchanged"] += 1
                continue
            if old:
                _delete_file(c, old["id"])
            cur = c.execute("INSERT INTO files(repo,path,language,mtime,size,sha) VALUES(?,?,?,?,?,?)",
                            (name, fi.path, lang, fi.mtime, fi.size, digest))
            file_id = cur.lastrowid
            for ch in chunk_file(text, lang):
                emb_text = f"{ch.symbol} {fi.path}\n{ch.content[:3000]}"
                vec = embedder.embed(emb_text)
                cid = c.execute(
                    "INSERT INTO chunks(file_id,repo,path,language,symbol,kind,start_line,end_line,content,embedding)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (file_id, name, fi.path, lang, ch.symbol, ch.kind, ch.start_line, ch.end_line, ch.content, to_blob(vec)),
                ).lastrowid
                c.execute("INSERT INTO chunks_fts(rowid,symbol,path,content,tokens) VALUES(?,?,?,?,?)",
                          (cid, ch.symbol, fi.path, ch.content, identifier_text(f"{ch.symbol} {fi.path} {ch.content}")))
                stats["chunks"] += 1
            # secrets scan for this file
            c.execute("DELETE FROM secrets WHERE repo=? AND path=?", (name, fi.path))
            for f in secrets.scan_text(fi.path, text):
                c.execute("INSERT OR IGNORE INTO secrets(repo,path,line,severity,rule,preview) VALUES(?,?,?,?,?,?)",
                          (name, fi.path, f["line"], f["severity"], f["rule"], f["preview"]))
        stats["indexed"] += 1
    removed = [p for p in existing if p not in seen]
    if removed:
        with tx() as c:
            for p in removed:
                _delete_file(c, existing[p]["id"])
                c.execute("DELETE FROM secrets WHERE repo=? AND path=?", (name, p))
        stats["removed"] = len(removed)
    langs = rows("SELECT language, COUNT(*) n FROM files WHERE repo=? GROUP BY language ORDER BY n DESC", (name,))
    totals = connect().execute(
        "SELECT (SELECT COUNT(*) FROM files WHERE repo=?), (SELECT COUNT(*) FROM chunks WHERE repo=?)", (name, name)
    ).fetchone()
    with tx() as c:
        c.execute(
            "INSERT INTO repos(name,root,kind,files,chunks,languages,indexed_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET root=excluded.root, kind=excluded.kind, files=excluded.files,"
            " chunks=excluded.chunks, languages=excluded.languages, indexed_at=excluded.indexed_at",
            (name, str(src.root), src.kind, totals[0], totals[1],
             json.dumps({l["language"]: l["n"] for l in langs}), _now()),
        )
    if stats["indexed"] or stats.get("removed"):
        vector_index.mark_dirty()
    conn.commit()
    return dict(stats)


def index_all(force: bool = False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "indexer already running"}
    try:
        status["running"] = True
        t0 = time.time()
        from ..privacy import excluded_repos, is_excluded_repo
        ex = excluded_repos()
        repos = {n: s for n, s in discover().items() if not is_excluded_repo(n, ex)}
        result: dict = {"repos": {}}
        for name, src in repos.items():
            status["current"] = name
            try:
                result["repos"][name] = index_repo(name, src, force=force)
            except Exception as e:
                log.exception("indexing %s failed", name)
                result["repos"][name] = {"error": str(e)}
        # drop repos that disappeared
        gone = [r["name"] for r in rows("SELECT name FROM repos") if r["name"] not in repos]
        if gone:
            with tx() as c:
                for g in gone:
                    for f in c.execute("SELECT id FROM files WHERE repo=?", (g,)).fetchall():
                        _delete_file(c, f[0])
                    c.execute("DELETE FROM repos WHERE name=?", (g,))
                    c.execute("DELETE FROM secrets WHERE repo=?", (g,))
            vector_index.mark_dirty()
            result["removed_repos"] = gone
        result["took_s"] = round(time.time() - t0, 2)
        status["last_run"], status["last_stats"] = _now(), result
        return result
    finally:
        status["running"], status["current"] = False, None
        _lock.release()
