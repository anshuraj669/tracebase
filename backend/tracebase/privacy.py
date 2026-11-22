"""Data lifecycle: what is stored, how long it is kept, and how to export or delete it.

- Retention: log lines, error groups, alerts and the audit log are deleted after a set number of days.
- Forget a repo: its code index, embeddings and secrets findings are deleted, and it is no longer indexed or
  readable (until restored). Forget a service: its log lines, errors and alerts are deleted (and, if asked,
  new logs for it are dropped on arrival).
- Purge matching: delete every log line, error and alert containing a string (a customer id, an email...),
  for erasure requests.
- Export: everything stored about a service (or matching a string) as NDJSON.
- Redaction upgrade: data stored before redaction existed (or under older rules) is redacted once at startup.
All deletes overwrite the freed pages (secure_delete) and truncate the write-ahead log.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Iterator

from . import redact
from .config import settings
from .db import checkpoint, connect, encrypted, kv_get, kv_set, open_private, row, rows, size_bytes, tx
from .timeutil import iso, now

log = logging.getLogger(__name__)

TABLES = ("repos", "files", "chunks", "secrets", "log_events", "error_groups", "alerts", "users", "audit")


# ------------------------------------------------------------------ exclusions (forgotten repos / services)
def _get_list(key: str) -> list[str]:
    try:
        return sorted(set(json.loads(kv_get(key) or "[]")))
    except ValueError:
        return []


def _set_list(key: str, items: list[str]) -> None:
    kv_set(key, json.dumps(sorted(set(items))))


def excluded_repos() -> list[str]:
    return _get_list("excluded_repos")


def excluded_services() -> list[str]:
    return _get_list("excluded_services")


def is_excluded_repo(name: str, excluded: list[str] | None = None) -> bool:
    ex = excluded if excluded is not None else excluded_repos()
    return any(name == e or name.startswith(e + "/") for e in ex)


# ------------------------------------------------------------------ retention
def prune() -> dict:
    """Delete records older than their retention period. Returns how many rows were deleted per kind."""
    out = {"log_events": 0, "error_groups": 0, "alerts": 0, "audit": 0, "log_offsets": 0}
    t = now()
    with tx() as c:
        if settings.log_retention_days > 0:
            out["log_events"] = c.execute("DELETE FROM log_events WHERE ts < ?",
                                          (iso(t - timedelta(days=settings.log_retention_days)),)).rowcount
        if settings.error_retention_days > 0:
            out["error_groups"] = c.execute(
                "DELETE FROM error_groups WHERE last_seen < ? AND NOT EXISTS"
                " (SELECT 1 FROM log_events e WHERE e.group_id = error_groups.id)",
                (iso(t - timedelta(days=settings.error_retention_days)),)).rowcount
        # alerts.created_at and audit.at are SQLite CURRENT_TIMESTAMP values (UTC, "YYYY-MM-DD HH:MM:SS")
        if settings.alert_retention_days > 0:
            out["alerts"] = c.execute("DELETE FROM alerts WHERE created_at < datetime('now', ?)",
                                      (f"-{settings.alert_retention_days} days",)).rowcount
        if settings.audit_retention_days > 0:
            out["audit"] = c.execute("DELETE FROM audit WHERE at < datetime('now', ?)",
                                     (f"-{settings.audit_retention_days} days",)).rowcount
        if settings.logs_dir.exists():
            gone = [r["path"] for r in c.execute("SELECT path FROM log_offsets").fetchall()
                    if not (settings.logs_dir / r["path"]).exists()]
            c.executemany("DELETE FROM log_offsets WHERE path=?", [(p,) for p in gone])
            out["log_offsets"] = len(gone)
    if any(out.values()):
        checkpoint()
        log.info("retention: deleted %s", out)
    return out


# ------------------------------------------------------------------ forget / purge
def _delete_repo_rows(c, name: str) -> dict:
    like = name + "/%"
    ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE repo=? OR repo LIKE ?", (name, like))]
    c.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
    n = {"chunks": len(ids)}
    n["files"] = c.execute("DELETE FROM files WHERE repo=? OR repo LIKE ?", (name, like)).rowcount  # chunks cascade
    c.execute("DELETE FROM chunks WHERE repo=? OR repo LIKE ?", (name, like))
    n["secrets"] = c.execute("DELETE FROM secrets WHERE repo=? OR repo LIKE ?", (name, like)).rowcount
    n["repos"] = c.execute("DELETE FROM repos WHERE name=? OR name LIKE ?", (name, like)).rowcount
    return n


def _optimize_fts() -> None:
    with tx() as c:  # merge FTS segments so deleted text is rewritten out of the index
        c.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")


def forget_repo(name: str) -> dict:
    """Delete a repo's index (code chunks, embeddings, file list, secrets findings) and stop indexing it.
    The folder on disk is not touched: remove it there too, or `restore_repo` to index it again."""
    from .code import indexer
    from .embeddings import vector_index

    name = name.strip().strip("/")
    if not name:
        raise ValueError("repo name required")
    _set_list("excluded_repos", excluded_repos() + [name])  # first, so a running indexer doesn't re-add it
    with indexer.lock():
        with tx() as c:
            n = _delete_repo_rows(c, name)
    _optimize_fts()
    vector_index.mark_dirty()
    checkpoint()
    return {"repo": name, "deleted": n, "excluded": True}


def restore_repo(name: str) -> dict:
    name = name.strip().strip("/")
    before = excluded_repos()
    if name not in before:
        raise KeyError(name)
    _set_list("excluded_repos", [e for e in before if e != name])
    return {"repo": name, "excluded": False, "note": "it will be indexed again on the next run"}


def forget_service(service: str, exclude: bool = False) -> dict:
    """Delete a log service's events, error groups and alerts. With exclude, new logs for it are dropped too."""
    service = service.strip()
    if not service:
        raise ValueError("service name required")
    if exclude:
        _set_list("excluded_services", excluded_services() + [service])
    with tx() as c:
        n = {"log_events": c.execute("DELETE FROM log_events WHERE service=?", (service,)).rowcount,
             "error_groups": c.execute("DELETE FROM error_groups WHERE service=?", (service,)).rowcount,
             "alerts": c.execute("DELETE FROM alerts WHERE service=?", (service,)).rowcount}
    checkpoint()
    return {"service": service, "deleted": n, "excluded": exclude}


def restore_service(service: str) -> dict:
    before = excluded_services()
    if service not in before:
        raise KeyError(service)
    _set_list("excluded_services", [e for e in before if e != service])
    return {"service": service, "excluded": False}


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


MATCH_GROUPS = ("title LIKE ? ESCAPE '\\' OR sample LIKE ? ESCAPE '\\' OR stack LIKE ? ESCAPE '\\'"
                " OR explanation LIKE ? ESCAPE '\\'")


def purge_matching(text: str, service: str | None = None, dry_run: bool = False) -> dict:
    """Delete every log line, error group and alert containing `text` (erasure requests: a customer id, an email)."""
    if len(text.strip()) < 4:
        raise ValueError("give at least 4 characters to match (this deletes data)")
    p = _like(text.strip())
    svc_sql, svc_p = (" AND service=?", (service,)) if service else ("", ())
    queries = {
        "log_events": ("log_events", "message LIKE ? ESCAPE '\\'", (p,)),
        "error_groups": ("error_groups", f"({MATCH_GROUPS})", (p, p, p, p)),
        "alerts": ("alerts", "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')", (p, p)),
    }
    out = {}
    with tx() as c:
        for key, (table, where, params) in queries.items():
            sql = f"FROM {table} WHERE {where}{svc_sql}"
            if dry_run:
                out[key] = c.execute(f"SELECT COUNT(*) {sql}", (*params, *svc_p)).fetchone()[0]
            else:
                out[key] = c.execute(f"DELETE {sql}", (*params, *svc_p)).rowcount
    if not dry_run:
        checkpoint()
    return {"match": text, "service": service, "dry_run": dry_run, ("would_delete" if dry_run else "deleted"): out}


def wipe(keep_team: bool = True) -> dict:
    """Delete everything Tracebase stored (the index is rebuilt from the repos folder on the next run)."""
    from .code import indexer
    from .embeddings import vector_index

    tables = ["chunks_fts", "chunks", "files", "repos", "secrets", "log_events", "error_groups", "log_offsets",
              "alerts", "kv"]
    if not keep_team:
        tables += ["users", "audit"]
    keep_kv = {k: kv_get(k) for k in ("excluded_repos", "excluded_services", "redaction_version")}
    with indexer.lock():
        with tx() as c:
            for t in tables:
                c.execute(f"DELETE FROM {t}")
            for k, v in keep_kv.items():
                if v is not None:
                    c.execute("INSERT INTO kv(key,value) VALUES(?,?)", (k, v))
    vector_index.mark_dirty()
    conn = connect()
    conn.commit()
    conn.execute("VACUUM")
    checkpoint()
    return {"wiped": tables}


# ------------------------------------------------------------------ export
def export(service: str | None = None, match: str | None = None) -> Iterator[str]:
    """NDJSON: one {"type": ..., ...} object per line, for a service and/or the records containing `match`."""
    where, p = ["1=1"], []
    if service:
        where.append("service=?")
        p.append(service)
    meta = {"type": "export", "service": service, "match": match, "exported_at": iso(now()),
            "redaction": settings.redact_logs}
    yield json.dumps(meta) + "\n"
    lp = list(p) + ([_like(match)] if match else [])
    gp = list(p) + ([_like(match)] * 4 if match else [])
    ap = list(p) + ([_like(match)] * 2 if match else [])
    w = " AND ".join(where)
    specs = [
        ("error_group", f"SELECT * FROM error_groups WHERE {w}" + (f" AND ({MATCH_GROUPS})" if match else ""), gp),
        ("alert", f"SELECT * FROM alerts WHERE {w}" + (" AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
                                                        if match else ""), ap),
        ("log_event", f"SELECT * FROM log_events WHERE {w}" + (" AND message LIKE ? ESCAPE '\\'" if match else "")
         + " ORDER BY ts", lp),
    ]
    # Its own connection: a streamed response is read from several worker threads, one batch at a time.
    conn = open_private()
    try:
        for kind, sql, params in specs:
            cur = conn.execute(sql, tuple(params))
            while batch := cur.fetchmany(500):
                for r in batch:
                    yield json.dumps({"type": kind, **dict(r)}, ensure_ascii=False, default=str) + "\n"
    finally:
        conn.close()


# ------------------------------------------------------------------ summary
def summary() -> dict:
    counts = {t: row(f"SELECT COUNT(*) n FROM {t}")["n"] for t in TABLES}
    span = row("SELECT MIN(ts) oldest, MAX(ts) newest FROM log_events")
    return {
        "database": {"path": str(settings.db_path), "size_bytes": size_bytes(), "encrypted": encrypted()},
        "counts": counts,
        "logs": {"oldest": span["oldest"], "newest": span["newest"]},
        "services": rows("SELECT service, COUNT(*) events FROM log_events GROUP BY service ORDER BY events DESC"),
        "retention_days": {"log_events": settings.log_retention_days, "error_groups": settings.error_retention_days,
                           "alerts": settings.alert_retention_days, "audit": settings.audit_retention_days},
        "redaction": {"enabled": settings.redact_logs, "ips": settings.redact_ips,
                      "custom_patterns": bool(settings.redact_extra.strip()), "version": redact.VERSION},
        "excluded": {"repos": excluded_repos(), "services": excluded_services()},
        "outbound": {"alert_webhook": bool(settings.alert_webhook),
                     "llm": {"provider": settings.llm_provider, "base_url": settings.llm_base_url}},
    }


# ------------------------------------------------------------------ redaction of data stored earlier
def redact_stored(force: bool = False) -> dict:
    """Apply the current redaction rules to everything already stored (once per rules version).
    Error groups whose fingerprint changes are merged, and cached explanations are dropped (rebuilt on demand)."""
    if not settings.redact_logs:
        return {"skipped": "redaction is off"}
    done = kv_get("redaction_version")
    if not force and done and int(done) >= redact.VERSION:
        return {"skipped": "already redacted"}
    from .logs.grouper import group_key

    out = {"log_events": 0, "error_groups": 0, "merged_groups": 0, "alerts": 0}
    conn = connect()
    with tx() as c:
        cur = conn.execute("SELECT id, message FROM log_events")
        while batch := cur.fetchmany(2000):
            upd = [(m2, r["id"]) for r in batch if (m2 := redact.redact(r["message"])) != r["message"]]
            c.executemany("UPDATE log_events SET message=? WHERE id=?", upd)
            out["log_events"] += len(upd)
        keep_by_fp: dict[str, int] = {}
        updates: list[tuple] = []
        for g in rows("SELECT * FROM error_groups ORDER BY id"):
            title, sample, stack = redact.redact(g["title"]), redact.redact(g["sample"]), redact.redact(g["stack"])
            fp, norm = group_key(g["service"], sample, json.loads(g["frames"] or "[]"))
            keep = keep_by_fp.get(fp)
            if keep is not None:  # two groups only differed by redacted data (an email, an IP): merge them
                c.execute("UPDATE log_events SET group_id=? WHERE group_id=?", (keep, g["id"]))
                c.execute("UPDATE error_groups SET count=count+?, first_seen=MIN(first_seen, ?),"
                          " last_seen=MAX(last_seen, ?) WHERE id=?", (g["count"], g["first_seen"], g["last_seen"], keep))
                c.execute("DELETE FROM error_groups WHERE id=?", (g["id"],))
                out["merged_groups"] += 1
                continue
            keep_by_fp[fp] = g["id"]
            if (title, sample, stack, fp) != (g["title"], g["sample"], g["stack"], g["fingerprint"]):
                updates.append((fp, title, norm, sample, stack, g["id"]))
        # fingerprints are UNIQUE: park the changed ones first so a swap never collides with an old value
        c.executemany("UPDATE error_groups SET fingerprint='tmp:'||id WHERE id=?", [(u[-1],) for u in updates])
        c.executemany("UPDATE error_groups SET fingerprint=?, title=?, normalized=?, sample=?, stack=?,"
                      " explanation=NULL WHERE id=?", updates)
        out["error_groups"] = len(updates)
        for a in rows("SELECT id, title, body FROM alerts"):
            t2, b2 = redact.redact(a["title"]), redact.redact(a["body"])
            if (t2, b2) != (a["title"], a["body"]):
                c.execute("UPDATE alerts SET title=?, body=? WHERE id=?", (t2, b2, a["id"]))
                out["alerts"] += 1
        c.execute("INSERT INTO kv(key,value) VALUES('redaction_version', ?) ON CONFLICT(key) DO UPDATE"
                  " SET value=excluded.value", (str(redact.VERSION),))
    if any(out.values()):
        conn.execute("VACUUM")  # rewrite the file so the old, unredacted pages are gone
        checkpoint()
        log.warning("redacted personal data stored by an earlier version: %s", out)
    return out
