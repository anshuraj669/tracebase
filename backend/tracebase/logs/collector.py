"""Log collection: (a) files dropped into the logs folder (tailed incrementally),
(b) Docker container logs on the NAS, (c) HTTP ingest (see api)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from ..config import settings
from ..db import kv_get, kv_set, row, tx
from .grouper import ingest_events
from .parser import parse_text

log = logging.getLogger(__name__)
LOG_EXT = {".log", ".txt", ".json", ".jsonl", ".out", ".err"}
_lock = threading.Lock()


def collect_files(root: Path | None = None) -> dict:
    root = root or settings.logs_dir
    stats = {"files": 0, "events": 0, "errors": 0, "new_groups": 0}
    if not root.exists() or not _lock.acquire(blocking=False):
        return stats
    try:
        for p in sorted(root.rglob("*")):
            if not p.is_file() or (p.suffix.lower() not in LOG_EXT and not p.name.endswith(".log.1")):
                continue
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            off = row("SELECT inode, offset FROM log_offsets WHERE path=?", (rel,))
            start = 0
            if off and off["inode"] == st.st_ino and off["offset"] <= st.st_size:
                start = off["offset"]
            if start >= st.st_size:
                continue
            with open(p, "rb") as f:
                f.seek(start)
                data = f.read()
            # only consume complete lines
            cut = data.rfind(b"\n")
            if cut == -1:
                continue
            chunk = data[: cut + 1]
            events = parse_text(chunk.decode("utf-8", "replace"), source=rel)
            res = ingest_events(events, source=rel)
            with tx() as c:
                c.execute("INSERT INTO log_offsets(path,inode,offset) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET"
                          " inode=excluded.inode, offset=excluded.offset", (rel, st.st_ino, start + len(chunk)))
            stats["files"] += 1
            stats["events"] += res["events"]
            stats["errors"] += res["errors"]
            stats["new_groups"] += len(res["new_groups"])
    finally:
        _lock.release()
    return stats


def docker_available() -> bool:
    return os.environ.get("TRACEBASE_DOCKER_LOGS", "0") == "1" and shutil.which("docker") is not None


def collect_docker() -> dict:
    if not docker_available():
        return {"enabled": False}
    stats = {"containers": 0, "events": 0}
    try:
        names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15).stdout.split()
    except Exception as e:
        return {"error": str(e)}
    for name in names:
        since = kv_get(f"docker_since:{name}") or "10m"
        try:
            r = subprocess.run(["docker", "logs", "--timestamps", "--since", since, name],
                               capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        text = r.stdout + r.stderr
        lines = text.splitlines()
        if lines:
            last_ts = lines[-1].split(" ", 1)[0]
            events = parse_text(text, source=f"docker:{name}", default_service=name)
            stats["events"] += ingest_events(events, source=f"docker:{name}")["events"]
            if last_ts[:4].isdigit():
                kv_set(f"docker_since:{name}", last_ts)
        stats["containers"] += 1
    return stats


def ingest_payload(payload: dict | str, service: str | None = None) -> dict:
    """HTTP ingest: raw text, {"lines": [...]} or {"events": [{ts, level, message, service, stack}]}.
    In team mode a member may only send logs for services their patterns cover (PermissionError otherwise)."""
    from .. import team
    from ..timeutil import parse_ts
    from .parser import Event, norm_level

    if isinstance(payload, str):
        events = parse_text(payload, source="api", default_service=service or "app")
    else:
        svc = payload.get("service") or service or "app"
        if "lines" in payload:
            events = parse_text("\n".join(payload["lines"]), source="api", default_service=svc)
        else:
            events = []
            for e in payload.get("events", []):
                stack = e.get("stack") or []
                events.append(Event(parse_ts(e.get("ts") or e.get("timestamp")), norm_level(e.get("level")),
                                    e.get("service") or svc, str(e.get("message", "")),
                                    stack.splitlines() if isinstance(stack, str) else list(stack), "api"))
    denied = sorted({e.service for e in events if not team.can_service(e.service)})
    if denied:
        raise PermissionError("not allowed to send logs for: " + ", ".join(denied))
    return ingest_events(events, source="api")
