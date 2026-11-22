"""Plain-English explanation of an error group: likely cause, impact, suggested fix.
Uses the LLM when available; falls back to a built-in rule book so it always answers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .. import team
from ..code import git
from ..code.search import read_code, resolve_path
from ..db import rows, tx
from ..llm import LLMUnavailable, llm
from ..timeutil import iso, parse_ts
from .grouper import get_group, rate_stats

RULES = [
    (r"connection refused|could not connect|ECONNREFUSED|\[2002\]|Connection reset|server has gone away",
     "The service could not reach a dependency (database/cache/API) — it was down, restarting, or the host/port is wrong.",
     ["Check whether the target container/server was restarted or stopped at that time.",
      "Add retry with exponential back-off around the connection."]),
    (r"undefined (index|array key|offset)|KeyError|undefined property|Cannot read propert(y|ies) of undefined|is not defined|NoneType",
     "Code reads a key/property that is missing from the input — the input shape changed or an optional field was not supplied.",
     ["Use a safe default (e.g. `$x['key'] ?? null`, `dict.get()`, optional chaining `?.`).",
      "Validate the request payload before use."]),
    (r"NullPointer|null reference|on null|of null|undefined method .* on null",
     "A value that was expected to exist is null — usually a lookup returned nothing.",
     ["Guard against the null case and return a clear error.", "Check the query/lookup that produced the value."]),
    (r"signature (mismatch|verification failed)|No signatures found|Invalid signature",
     "Webhook signature verification failed — the signing secret differs between sender and receiver, or the raw body was modified before verification.",
     ["Confirm the webhook signing secret in the environment matches the provider dashboard.",
      "Verify the signature against the raw request body (before JSON parsing/middleware)."]),
    (r"timed? ?out|timeout|deadline exceeded|ETIMEDOUT|504",
     "An operation took longer than its timeout — a slow query, slow upstream, or network issue.",
     ["Find the slow query/endpoint in the logs around that time.", "Add indexes or raise the timeout with back-off."]),
    (r"out of memory|OOM|memory exhausted|Allowed memory size|heap out of memory",
     "The process ran out of memory.", ["Process data in batches/streams.", "Raise the memory limit only after fixing the leak."]),
    (r"permission denied|EACCES|forbidden|403",
     "The process lacks permission on a file or resource.", ["Check file ownership/mode and the user the container runs as."]),
    (r"no such file|ENOENT|not found|FileNotFound|404",
     "A file, route or resource that the code expects does not exist.", ["Check deploy artifacts and paths."]),
    (r"deadlock|lock wait timeout",
     "Two transactions blocked each other in the database.", ["Keep transactions short and lock rows in a consistent order."]),
    (r"duplicate entry|unique constraint|UniqueViolation|23505|1062",
     "An insert violated a unique constraint — a double submission or a race condition.",
     ["Use upsert / idempotency keys.", "Catch the constraint error and return a friendly message."]),
    (r"too many connections|1040|remaining connection slots",
     "The database ran out of connections.", ["Use a connection pool and close connections.", "Raise max_connections if needed."]),
    (r"SQLSTATE|syntax error|QueryException|ProgrammingError",
     "A database query failed.", ["Inspect the failing SQL and recent migrations."]),
]


def code_context(frames: list[dict], service: str | None, radius: int = 4) -> list[dict]:
    """Resolve stack frames to files in indexed repos and return the surrounding lines."""
    out = []
    for f in frames[:4]:
        cands = [c for c in resolve_path(f["path"], service) if team.can(c["repo"])]
        if not cands:
            continue
        c = cands[0]
        snippet = read_code(c["repo"], c["path"], f["line"], f["line"], context=radius)
        if "error" in snippet:
            continue
        out.append({"repo": c["repo"], "path": c["path"], "line": f["line"], "func": f.get("func"),
                    "frame_path": f["path"], "snippet": snippet["lines"]})
    return out


def introduced_by(code: list[dict], first_seen: str | None) -> dict | None:
    """git blame on the failing line: the commit that last changed it, and how long before the first error it landed."""
    first = parse_ts(first_seen)
    for c in code[:2]:
        try:
            h = git.history(c["repo"], c["path"], line=c["line"])
        except Exception:
            continue
        if "error" in h or not h.get("commits"):
            continue
        cm = h["commits"][0]
        try:
            when = datetime.strptime(cm["date"], "%Y-%m-%d %H:%M:%S %z").astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        before = int((first - when).total_seconds() // 60) if first else None
        return {"repo": c["repo"], "path": c["path"], "line": c["line"], "sha": cm["sha"], "author": cm["author"],
                "date": when.strftime("%Y-%m-%d %H:%M"), "message": cm["message"], "minutes_before_first_error": before}
    return None


def _ago(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} minutes"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours"
    return f"{minutes // 1440} days"


EVENT_HINTS = r"(shut ?down|shutting down|restart|stopp(ed|ing)|starting|started|ready for connections|killed|OOM|deploy|backup|dump|migrat|SIGTERM|exited|unhealthy)"


def nearby_events(g: dict, minutes: int = 5) -> list[dict]:
    """Notable events in *other* services around the time the error group started (restarts, deploys, backups)."""
    dt = parse_ts(g.get("first_seen"))
    if not dt:
        return []
    cand = rows("SELECT ts, level, service, message FROM log_events WHERE ts BETWEEN ? AND ? AND service != ? ORDER BY ts LIMIT 400",
                (iso(dt - timedelta(minutes=minutes)), iso(dt + timedelta(minutes=45)), g["service"]))
    out, seen = [], set()
    for e in cand:
        if not team.can_service(e["service"]):  # a member only sees services they have access to
            continue
        if re.search(EVENT_HINTS, e["message"], re.I) and (e["service"], e["message"][:60]) not in seen:
            seen.add((e["service"], e["message"][:60]))
            out.append({**e, "message": e["message"][:200]})
    return out[:8]


def _heuristic(g: dict, code: list[dict], stats: dict, events: list[dict] | None = None, commit: dict | None = None) -> dict:
    text = f"{g['title']}\n{g.get('sample') or ''}\n{g.get('stack') or ''}"
    cause, fixes = "Not enough information for a confident diagnosis — inspect the sample lines and stack trace.", []
    for pat, c, f in RULES:
        if re.search(pat, text, re.I):
            cause, fixes = c, list(f)
            break
    events = events or []
    stop = next((e for e in events if re.search(r"shut ?down|shutting down|stopp|SIGTERM|killed|exited", e["message"], re.I)), None)
    if stop:
        back = next((e for e in events if e["ts"] > stop["ts"] and re.search(r"ready|started|starting", e["message"], re.I)), None)
        trig = next((e for e in events if re.search(r"backup|dump", e["message"], re.I)), None)
        cause += (f" At {stop['ts'][11:19]} the '{stop['service']}' service logged \"{stop['message'][:90]}\""
                  + (f" and came back at {back['ts'][11:19]}" if back else "") + ".")
        if trig:
            cause += f" This was triggered by \"{trig['message'][:90]}\" ({trig['service']}) — the backup stops the container to take a dump."
            fixes.insert(0, "Use a live dump instead of stopping the container (mysqldump --single-transaction), "
                            "or point the app at a replica during the backup window.")
    if code:
        c = code[0]
        bad = next((l for l in c["snippet"] if l["n"] == c["line"]), None)
        if bad:
            fix = suggest_line_fix(bad["text"])
            if fix:
                fixes.insert(0, f"In {c['repo']}/{c['path']}:{c['line']} change `{bad['text'].strip()}` to `{fix}`")
            else:
                fixes.append(f"Look at {c['repo']}/{c['path']}:{c['line']} (`{bad['text'].strip()[:80]}`).")
    if commit and commit["minutes_before_first_error"] is not None and 0 <= commit["minutes_before_first_error"] <= 7 * 1440:
        cause += (f" {commit['path']}:{commit['line']} was last changed in commit {commit['sha']} \"{commit['message'][:80]}\""
                  f" ({commit['author']}, {commit['date']}), {_ago(commit['minutes_before_first_error'])} before the first"
                  " error: the likely culprit.")
        fixes.append(f"Review what commit {commit['sha']} changed in {commit['repo']} (git show {commit['sha']}).")
    dur = _duration(g)
    impact = (f"1 occurrence at {g['first_seen'][:16].replace('T', ' ')}." if g["count"] == 1 else
              f"{g['count']} occurrences between {g['first_seen'][11:16]} and {g['last_seen'][11:16]}{dur}.")
    normal = baseline_before(g["service"], g["first_seen"])
    if normal is not None:
        impact += f" Before it started, {g['service']} logged {normal:.1f} errors/hour."
    return {"cause": cause, "impact": impact, "fix": fixes, "source": "rules"}


def suggest_line_fix(line: str) -> str | None:
    s = line.strip()
    m = re.match(r"^(\$\w+\s*=\s*)(\$\w+(?:\[['\"][\w-]+['\"]\])+)\s*;(.*)$", s)
    if m:
        return f"{m.group(1)}{m.group(2)} ?? null;"
    m = re.match(r"^(\w+\s*=\s*)(\w+)\[['\"](\w+)['\"]\](.*)$", s)
    if m and not s.startswith(("$", "const", "let", "var")):
        return f"{m.group(1)}{m.group(2)}.get('{m.group(3)}'){m.group(4)}"
    return None


def baseline_before(service: str, first_seen: str | None, hours: int = 24) -> float | None:
    """Errors per hour in this service in the `hours` before the group first appeared (the 'normal' rate)."""
    first = parse_ts(first_seen)
    if not first:
        return None
    lo = iso(first - timedelta(hours=hours))
    r = rows("SELECT COUNT(*) n, MIN(ts) t0 FROM log_events WHERE service=? AND ts>=? AND ts<?", (service, lo, iso(first)))[0]
    t0 = parse_ts(r["t0"])
    if not t0:
        return None  # no history before it: no honest baseline
    errs = rows("SELECT COUNT(*) n FROM log_events WHERE service=? AND level IN ('ERROR','CRITICAL') AND ts>=? AND ts<?",
                (service, lo, iso(first)))[0]["n"]
    return errs / max((first - t0).total_seconds() / 3600, 1.0)


def _duration(g: dict) -> str:
    a, b = parse_ts(g["first_seen"]), parse_ts(g["last_seen"])
    if a and b and b > a:
        mins = int((b - a).total_seconds() // 60)
        return f" (~{mins} minutes)" if mins else ""
    return ""


def explain_group(gid: int, refresh: bool = False) -> dict:
    g = get_group(gid)
    if not g:
        return {"error": f"error group E-{gid} not found"}
    # explanations are cached for unrestricted users only: a member's may not use code/services they can't see
    shared = team.allowed_repos() is None and team.allowed_services() is None
    if g.get("explanation") and not refresh and shared:
        try:
            cached = json.loads(g["explanation"])
            cached["cached"] = True
            return cached
        except json.JSONDecodeError:
            pass
    code = code_context(g["frames"], g["service"])
    stats = rate_stats(g["service"], parse_ts(g["last_seen"]))
    events = nearby_events(g)
    commit = introduced_by(code, g["first_seen"])
    result = _heuristic(g, code, stats, events, commit)
    if llm.available():
        ctx = {
            "group": {k: g[k] for k in ("code", "service", "level", "title", "count", "first_seen", "last_seen", "status")},
            "sample_lines": [s["message"][:600] for s in g["samples"][:3]],
            "stack_trace": (g.get("stack") or "")[:2500],
            "code_at_frames": [{"location": f"{c['repo']}/{c['path']}:{c['line']}",
                                "code": "\n".join(f"{'>>' if l['n'] == c['line'] else '  '} {l['n']} {l['text']}" for l in c["snippet"])}
                               for c in code],
            "events_in_other_services_near_first_error": events,
            "commit_that_last_changed_the_failing_line": commit,
            "error_rate": stats,
            "rule_based_hint": result["cause"],
        }
        prompt = (
            "Explain this production error group for a developer. Reply ONLY with JSON: "
            '{"cause": "...", "impact": "...", "fix": ["step 1", "step 2"]}. Be specific. Only cite file:line '
            "locations that appear in code_at_frames — never invent paths. Use events in other services "
            "to explain WHY it happened when they line up in time. Keep each field under 60 words.\n\n"
            + json.dumps(ctx, default=str)[:9000]
        )
        try:
            raw = llm.complete(prompt)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            known = {f"{c['path']}:{c['line']}" for c in code} | {f"{c['path'].rsplit('/', 1)[-1]}:{c['line']}" for c in code}
            cited = re.findall(r"([\w./-]+\.\w{1,5}):(\d+)", json.dumps(data))
            invented = [f"{p}:{l}" for p, l in cited if not any(f"{p}:{l}".endswith(k) or k.endswith(f"{p}:{l}") for k in known)]
            if data.get("cause") and not invented:
                fix = data.get("fix")
                llm_fix = [str(f) for f in fix] if isinstance(fix, list) else [str(fix)] if fix else []
                # Concrete rule-derived fixes (exact line change, live-dump advice) always come first.
                concrete = [f for f in result["fix"] if f.startswith(("In ", "Use a live dump"))]
                cause = str(data["cause"])
                if "backup" in result["cause"] and "backup" not in cause.lower():
                    cause += " " + result["cause"][result["cause"].find("At "):] if "At " in result["cause"] else ""
                if commit and commit["sha"] in result["cause"] and commit["sha"] not in cause:
                    i = result["cause"].find(f"{commit['path']}:{commit['line']} was last changed")
                    cause += (" " + result["cause"][i:]) if i >= 0 else ""
                result = {"cause": cause.strip(), "impact": result["impact"],  # numbers come from the data, never the LLM
                          "fix": (concrete + [f for f in llm_fix if f not in concrete])[:4] or result["fix"], "source": "llm"}
        except (LLMUnavailable, json.JSONDecodeError, ValueError):
            pass
    result.update({"group": g["code"], "code": code, "events": events, "stats": stats, "commit": commit})
    if shared:
        with tx() as c:
            c.execute("UPDATE error_groups SET explanation=? WHERE id=?", (json.dumps(result, default=str), gid))
    return result
