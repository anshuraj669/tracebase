"""Normalise + fingerprint error messages so thousands of identical errors become one group;
store events; compute rates vs. baseline and raise anomaly alerts."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from .. import team
from ..alerts import raise_alert
from ..db import connect, row, rows, tx
from ..timeutil import iso, now, parse_since, parse_ts
from .parser import Event, extract_frames

GROUP_LEVELS = {"ERROR", "CRITICAL", "WARNING"}

_NORMALISERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b(evt|ch|pi|cus|sub|in|req|txn|ord|usr)_[A-Za-z0-9]+(\.\.\.)?"), r"\1_<id>"),
    (re.compile(r"(?<=[=:#/ ])\d+(?=[\s,)/\]]|$)"), "<n>"),
    (re.compile(r"(?<![\w\[])\d{3,}(?![\w\]])"), "<n>"),
    (re.compile(r"'[^']{24,}'"), "'<str>'"),
    (re.compile(r"\"[^\"]{24,}\""), "\"<str>\""),
    (re.compile(r"\s+"), " "),
]


def normalise(message: str) -> str:
    m = message.strip()
    for rx, rep in _NORMALISERS:
        m = rx.sub(rep, m)
    return m[:300]


def fingerprint(ev: Event) -> tuple[str, str, list[dict]]:
    frames = extract_frames(ev.text)
    norm = normalise(ev.message)
    top = frames[0] if frames else None
    key = f"{ev.service}|{norm}|{top.path.rsplit('/', 1)[-1] + ':' + (top.func or '') if top else ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:16], norm, [f.as_dict() for f in frames]


def ingest_events(events: list[Event], source: str = "") -> dict:
    """Store events, update groups. Returns stats and newly created group ids."""
    if not events:
        return {"events": 0, "errors": 0, "new_groups": []}
    new_groups: list[int] = []
    regressed: list[int] = []
    err_buckets: dict[str, dict[datetime, int]] = defaultdict(lambda: defaultdict(int))
    counts = defaultdict(int)
    conn = connect()
    with tx() as c:
        cache: dict[str, int] = {}
        for ev in events:
            ts = ev.ts or now()
            group_id = None
            if ev.level in GROUP_LEVELS:
                fp, norm, frames = fingerprint(ev)
                gid = cache.get(fp)
                if gid is None:
                    g = c.execute("SELECT id, status FROM error_groups WHERE fingerprint=?", (fp,)).fetchone()
                    if g is None:
                        gid = c.execute(
                            "INSERT INTO error_groups(fingerprint,service,level,title,normalized,count,first_seen,last_seen,"
                            "status,sample,stack,frames) VALUES(?,?,?,?,?,0,?,?, 'new', ?, ?, ?)",
                            (fp, ev.service, ev.level, _title(ev.message), norm, iso(ts), iso(ts),
                             ev.message[:2000], "\n".join(ev.stack)[:8000], json.dumps(frames)),
                        ).lastrowid
                        new_groups.append(gid)
                    else:
                        gid = g["id"]
                        if g["status"] == "resolved":
                            c.execute("UPDATE error_groups SET status='regressed' WHERE id=?", (gid,))
                            regressed.append(gid)
                    cache[fp] = gid
                group_id = gid
                c.execute(
                    "UPDATE error_groups SET count=count+1, first_seen=MIN(first_seen, ?), last_seen=MAX(last_seen, ?),"
                    " stack=CASE WHEN stack='' OR stack IS NULL THEN ? ELSE stack END WHERE id=?",
                    (iso(ts), iso(ts), "\n".join(ev.stack)[:8000], gid))
                counts["errors"] += 1
                if ev.level in ("ERROR", "CRITICAL"):
                    b = ts.replace(minute=ts.minute - ts.minute % 10, second=0)
                    err_buckets[ev.service][b] += 1
            c.execute("INSERT INTO log_events(ts,level,service,message,source,group_id) VALUES(?,?,?,?,?,?)",
                      (iso(ts), ev.level, ev.service, ev.text[:4000], source or ev.source, group_id))
            counts["events"] += 1
    conn.commit()
    for gid in new_groups:
        g = row("SELECT * FROM error_groups WHERE id=?", (gid,))
        if g and g["level"] in ("ERROR", "CRITICAL"):
            raise_alert("log", "warning", f"New error type in {g['service']}: E-{gid}",
                        g["title"], f"/logs/E-{gid}", dedupe_key=f"newgroup:{gid}", service=g["service"])
    for gid in regressed:
        g = row("SELECT * FROM error_groups WHERE id=?", (gid,))
        raise_alert("log", "critical", f"E-{gid} is back in {g['service']} (it was marked resolved)", g["title"],
                    f"/logs/E-{gid}", dedupe_key=f"regressed:{gid}:{g['last_seen'][:13]}", service=g["service"])
    spikes = []
    for svc, buckets in err_buckets.items():
        for b, n in sorted(buckets.items()):
            if n >= 10:
                st = check_spike(svc, b + timedelta(minutes=10))
                if st:
                    spikes.append(st)
    return {"events": counts["events"], "errors": counts["errors"], "new_groups": new_groups, "spikes": spikes}


def _title(msg: str) -> str:
    t = msg.strip().splitlines()[0] if msg.strip() else "(empty message)"
    return t[:160]


def rate_stats(service: str | None, at: datetime | None = None, window_min: int = 10, baseline_h: int = 24) -> dict:
    """Error rate in the window ending at `at` vs. the baseline in the preceding `baseline_h` hours."""
    if at is None:
        r = row("SELECT MAX(ts) t FROM log_events" + (" WHERE service=?" if service else ""), (service,) if service else ())
        at = parse_ts(r["t"]) if r and r["t"] else now()
    w_start = at - timedelta(minutes=window_min)
    b_start = w_start - timedelta(hours=baseline_h)
    svc_sql = " AND service=?" if service else ""
    p = (service,) if service else ()
    q = "SELECT COUNT(*) n FROM log_events WHERE level IN ('ERROR','CRITICAL') AND ts>? AND ts<=?" + svc_sql
    win = row(q, (iso(w_start), iso(at), *p))["n"]
    base = row(q, (iso(b_start), iso(w_start), *p))["n"]
    # baseline only over the span we actually have data for
    first = row("SELECT MIN(ts) t FROM log_events WHERE 1=1" + svc_sql, p)
    first_dt = parse_ts(first["t"]) if first and first["t"] else w_start
    span_min = max(60.0, (w_start - max(b_start, first_dt)).total_seconds() / 60)
    base_rate = base / span_min
    win_rate = win / window_min
    return {"service": service, "window_end": iso(at), "window_minutes": window_min,
            "errors_in_window": win, "rate_per_min": round(win_rate, 3),
            "baseline_per_min": round(base_rate, 3),
            "ratio": round(win_rate / max(base_rate, 0.01), 1)}


def check_spike(service: str, at: datetime) -> dict | None:
    st = rate_stats(service, at)
    if st["errors_in_window"] >= 10 and st["rate_per_min"] >= max(5 * st["baseline_per_min"], 1.0):
        top = row("SELECT g.id, g.title, COUNT(*) n FROM log_events e JOIN error_groups g ON g.id=e.group_id"
                  " WHERE e.service=? AND e.ts>? AND e.ts<=? GROUP BY g.id ORDER BY n DESC LIMIT 1",
                  (service, iso(at - timedelta(minutes=10)), iso(at)))
        hour_key = st["window_end"][:13]
        started = row("SELECT MIN(ts) t FROM log_events WHERE service=? AND level IN ('ERROR','CRITICAL') AND ts>?",
                      (service, iso(at - timedelta(minutes=10))))
        body = (f"error rate {st['rate_per_min']}/min (baseline {st['baseline_per_min']}/min) -- started {started['t'][11:] if started and started['t'] else '?'}")
        if top:
            body += f"\nTop error: E-{top['id']} \"{top['title'][:80]}\""
        title = (f"{service}: error rate {st['ratio']}x above normal" if st["baseline_per_min"] >= 0.05
                 else f"{service}: error spike, {st['rate_per_min']:g}/min (normally almost none)")
        raise_alert("anomaly", "critical", title, body,
                    f"/logs/E-{top['id']}" if top else f"/logs?service={service}", dedupe_key=f"spike:{service}:{hour_key}",
                    service=service)
        return st
    return None


def find_errors(service: str | None = None, since: str | None = None, level: str | None = None,
                status: str | None = None, q: str | None = None, limit: int = 50) -> list[dict]:
    ref = None
    r = row("SELECT MAX(ts) t FROM log_events")
    latest = parse_ts(r["t"]) if r and r["t"] else None
    # Use real "now", but if logs are older (replayed/demo data), anchor relative ranges to the latest log time.
    if latest and latest < now() - timedelta(hours=6):
        ref = latest + timedelta(minutes=1)
    since_dt = parse_since(since, ref) if since else None
    where, params = ["1=1"], []
    scope, sp = team.service_filter("g.service")
    if scope:
        where.append(scope[5:])
        params += sp
    if service:
        where.append("(g.service = ? OR g.service LIKE ?)")
        params += [service, f"%{service}%"]
    if level:
        where.append("g.level = ?")
        params.append(level.upper())
    if status:
        where.append("g.status = ?")
        params.append(status)
    if q:
        where.append("(g.title LIKE ? OR g.normalized LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if since_dt:
        where.append("g.last_seen >= ?")
        params.append(iso(since_dt))
        sql = (f"SELECT g.*, (SELECT COUNT(*) FROM log_events e WHERE e.group_id=g.id AND e.ts>=?) AS window_count,"
               f" (SELECT MIN(e.ts) FROM log_events e WHERE e.group_id=g.id AND e.ts>=?) AS window_first"
               f" FROM error_groups g WHERE {' AND '.join(where)} ORDER BY window_count DESC LIMIT ?")
        out = rows(sql, (iso(since_dt), iso(since_dt), *params, limit))
    else:
        out = rows(f"SELECT g.* FROM error_groups g WHERE {' AND '.join(where)} ORDER BY g.last_seen DESC, g.count DESC LIMIT ?",
                   (*params, limit))
    spiking = _spiking_groups()
    for g in out:
        g["frames"] = json.loads(g["frames"] or "[]")
        g["code"] = f"E-{g['id']}"
        g["spike"] = g["id"] in spiking and g["status"] != "resolved"
        g.pop("stack", None)
    return out


def _spiking_groups() -> set[int]:
    out = set()
    for r in rows("SELECT link FROM alerts WHERE kind='anomaly' AND read=0 AND link LIKE '/logs/E-%'"):
        try:
            out.add(int(r["link"].rsplit("E-", 1)[1]))
        except ValueError:
            pass
    return out


def get_group(gid: int) -> dict | None:
    g = row("SELECT * FROM error_groups WHERE id=?", (gid,))
    if not g:
        return None
    g["frames"] = json.loads(g["frames"] or "[]")
    g["code"] = f"E-{gid}"
    g["samples"] = rows("SELECT ts, level, service, message, source FROM log_events WHERE group_id=? ORDER BY ts DESC LIMIT 5", (gid,))
    g["timeline"] = rows(
        "SELECT substr(ts,1,13)||':00' AS bucket, COUNT(*) n FROM log_events WHERE group_id=? GROUP BY bucket ORDER BY bucket", (gid,))
    return g


def set_status(gid: int, status: str) -> None:
    with tx() as c:
        c.execute("UPDATE error_groups SET status=? WHERE id=?", (status, gid))


def services() -> list[dict]:
    scope, p = team.service_filter("service")
    return rows("SELECT service, COUNT(*) events, SUM(level IN ('ERROR','CRITICAL')) errors, MAX(ts) last_ts"
                f" FROM log_events WHERE 1=1{scope} GROUP BY service ORDER BY errors DESC", tuple(p))


def timeline(service: str | None = None, hours: int = 48) -> list[dict]:
    r = row("SELECT MAX(ts) t FROM log_events" + (" WHERE service=?" if service else ""), (service,) if service else ())
    end = parse_ts(r["t"]) if r and r["t"] else now()
    start = end - timedelta(hours=hours)
    scope, sp = team.service_filter("service")
    q = ("SELECT substr(ts,1,15)||'0' AS bucket, SUM(level IN ('ERROR','CRITICAL')) errors, SUM(level='WARNING') warnings,"
         f" COUNT(*) total FROM log_events WHERE ts>=?{scope}")
    p: list = [iso(start), *sp]
    if service:
        q += " AND service=?"
        p.append(service)
    return rows(q + " GROUP BY bucket ORDER BY bucket", tuple(p))


def prune(days: int) -> int:
    cutoff = iso(now() - timedelta(days=days))
    with tx() as c:
        n = c.execute("DELETE FROM log_events WHERE ts < ?", (cutoff,)).rowcount
    return n
