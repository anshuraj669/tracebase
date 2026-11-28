# SPDX-License-Identifier: Apache-2.0
"""Which flag changes could have caused an error group, and is the failing code the code that checks the flag?

A flag change is a suspect when it happened shortly before the errors started (the current burst: an error that
comes back after a rollback or a quiet half hour is a new incident) or within a couple of minutes after them:
clocks and SDK caches drift). Suspects are ranked by how close in time they were and by how strongly the flag is
tied to the failing code:

  guards    a line of the stack trace runs after a flag check (the flag name in quotes) in the same function
  call_path the flag is checked in a file of the stack trace
  service   the flag is checked somewhere in the failing service's repo
"""
from __future__ import annotations

import re
from datetime import timedelta

from .. import team
from ..db import row, rows
from ..timeutil import iso, parse_ts
from . import store
from .rollback import GRACE

GAP = timedelta(minutes=30)   # this long without the error, and its next occurrence starts a new incident
BEFORE = timedelta(hours=6)   # how far back a flag change can still explain a new error
AFTER = timedelta(minutes=2)  # clock skew between Flagsmith and the logs, SDK polling delay
LINK_WEIGHT = {"guards": 3.0, "call_path": 2.0, "service": 1.0, None: 0.0}
LINK_TEXT = {
    "guards": "the failing line is inside code that checks this flag",
    "call_path": "this flag is checked in a file on the failing stack trace",
    "service": "this flag is checked in the failing service's code",
}


COMMENT = re.compile(r"\s*(//|#|\*|/\*|<!--|--)")


def flag_refs(name: str, repos: list[str] | None = None, limit: int = 40) -> list[dict]:
    """Places in the indexed code that check a flag: its name in quotes, e.g. is_enabled('new_checkout')."""
    pat = re.compile(r"""(["'])""" + re.escape(name) + r"\1")
    q = "SELECT repo, path, symbol, start_line, end_line, content FROM chunks WHERE content LIKE ?"
    p: list = [f"%{name}%"]
    if repos:
        q += f" AND repo IN ({','.join('?' * len(repos))})"
        p += repos
    out = []
    for c in rows(q + " LIMIT 400", tuple(p)):
        if not team.can(c["repo"]):
            continue
        for i, line in enumerate((c["content"] or "").splitlines()):
            if pat.search(line) and not COMMENT.match(line):
                out.append({"repo": c["repo"], "path": c["path"], "symbol": c["symbol"], "line": c["start_line"] + i,
                            "start_line": c["start_line"], "end_line": c["end_line"], "text": line.strip()[:160]})
        if len(out) >= limit:
            break
    seen, uniq = set(), []
    for r in out:  # chunks can overlap (a class and its methods): keep each line once, the narrowest chunk
        k = (r["repo"], r["path"], r["line"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def _service_repos(service: str) -> list[str]:
    s = service.lower()
    return [r["name"] for r in rows("SELECT name FROM repos")
            if r["name"].lower() == s or r["name"].lower().endswith("/" + s) or s in r["name"].lower().split("/")[-1]]


def link(name: str, code: list[dict], service: str | None) -> tuple[str | None, dict | None]:
    """How strongly a flag is tied to the failing code: (kind, the flag check that ties it)."""
    refs = flag_refs(name)
    if not refs:
        return None, None
    for c in code:  # a frame of the stack runs after a flag check in the same function: the flag guards it
        inside = [r for r in refs if r["repo"] == c["repo"] and r["path"] == c["path"]
                  and r["start_line"] <= r["line"] <= c["line"] <= r["end_line"]]
        if inside:
            return "guards", max(inside, key=lambda r: (r["line"], -(r["end_line"] - r["start_line"])))
    files = {(c["repo"], c["path"]) for c in code}
    for r in refs:
        if (r["repo"], r["path"]) in files:
            return "call_path", r
    repos = set(_service_repos(service)) if service else set()
    for r in refs:
        if r["repo"] in repos:
            return "service", r
    return None, refs[0]


def incident_start(g: dict):
    """When the current burst of this error started: its first occurrence after the last rollback that stopped it,
    or after a quiet gap. An error that comes back is a new incident, with its own suspects."""
    first = parse_ts(g.get("first_seen"))
    if not g.get("id"):
        return first
    floor = None
    # the last rollback that stopped this error, before its latest occurrence: what came after is a new incident
    rb = row("SELECT MAX(created_at) t FROM flag_rollbacks WHERE group_id=? AND status='worked' AND created_at<?",
             (g["id"], g.get("last_seen") or ""))
    if rb and rb["t"]:
        after = iso(parse_ts(rb["t"]) + GRACE)  # stragglers (SDK caches, late log lines) still belong to the old one
        nxt = row("SELECT MIN(ts) t FROM log_events WHERE group_id=? AND ts>?", (g["id"], after))
        if nxt and nxt["t"]:
            floor = nxt["t"]
    q = "SELECT ts FROM log_events WHERE group_id=?" + (" AND ts>=?" if floor else "") + " ORDER BY ts DESC LIMIT 20000"
    ts = [parse_ts(r["ts"]) for r in rows(q, (g["id"], floor) if floor else (g["id"],))]
    ts = [t for t in ts if t]
    if not ts:
        return first
    start = ts[0]
    for t in ts[1:]:
        if start - t > GAP:
            break
        start = t
    return start


def _turned_off_since(ch: dict) -> bool:
    """A later change (a rollback, or someone in the dashboard) already turned this flag off again."""
    last = row("SELECT enabled_after FROM flag_changes WHERE feature=? AND IFNULL(environment_id, -1)=IFNULL(?, -1) "
               "AND IFNULL(segment, '')=IFNULL(?, '') AND IFNULL(identity, '')=IFNULL(?, '') AND (ts>? OR (ts=? AND id>?)) "
               "ORDER BY ts DESC, id DESC LIMIT 1",
               (ch["feature"], ch.get("environment_id"), ch.get("segment"), ch.get("identity"), ch["ts"], ch["ts"], ch["id"]))
    return bool(last) and last["enabled_after"] == 0


def for_group(g: dict, code: list[dict]) -> list[dict]:
    """Flag changes that are suspects for the current incident of this error group, best first."""
    first = incident_start(g)
    if not first:
        return []
    started = iso(first)
    changes = store.history(since=iso(first - BEFORE), until=iso(first + AFTER), limit=200)
    out = []
    for ch in changes:
        if ch.get("source") == "tracebase":  # Tracebase's own rollbacks are fixes, not causes
            continue
        if ch["summary"] == "saved (no change)":
            continue
        minutes = int((first - parse_ts(ch["ts"])).total_seconds() // 60)
        kind, ref = link(ch["feature"], code, g.get("service"))
        # closeness in time: 1.0 right before the error, fading over the 6-hour window
        closeness = max(0.0, 1.0 - max(minutes, 0) / (BEFORE.total_seconds() / 60))
        risky = 1.0 if (ch.get("enabled_after") or ch.get("rollout_after") or ch.get("value_after") is not None) else 0.5
        score = round((closeness * 2 + LINK_WEIGHT[kind]) * risky, 3)
        out.append({"change": ch, "minutes_before_first_error": minutes, "incident_start": started, "link": kind,
                    "link_reason": LINK_TEXT.get(kind), "flag_check": ref, "score": score,
                    "already_off": (off := _turned_off_since(ch)),
                    "can_roll_back": bool(ch.get("enabled_after")) and ch.get("provider") == "flagsmith" and not off})
    # the same flag changed several times: keep its latest change before the error (a change just after the errors
    # started, such as someone turning it off again, must not hide the one that caused them)
    best: dict[tuple, dict] = {}
    order = lambda s: (s["minutes_before_first_error"] >= 0, s["change"]["ts"])
    for s in out:
        k = (s["change"]["feature"], s["change"]["environment"], s["change"].get("segment"))
        if k not in best or order(s) > order(best[k]):
            best[k] = s
    ranked = sorted(best.values(), key=lambda s: -s["score"])
    # without a code link, only a change right before the error is worth naming
    return [s for s in ranked if s["link"] or 0 <= s["minutes_before_first_error"] <= 30][:3]


def sentence(s: dict) -> str:
    ch = s["change"]
    where = ch.get("environment") or "an environment"
    if ch.get("segment"):
        where += f" (segment {ch['segment']})"
    who = f" by {ch['changed_by']}" if ch.get("changed_by") else ""
    m = s["minutes_before_first_error"]
    when = (f"{m} minute{'s' if m != 1 else ''} before the errors started" if m > 0 else
            "in the minute the errors started" if m == 0 else f"{-m} minute{'s' if m != -1 else ''} after the errors started")
    text = f"Feature flag '{ch['feature']}' was {ch['summary']} in {where}{who} {when}"
    fc = s.get("flag_check")
    if s["link"] == "guards" and fc:
        text += f", and the failing code runs only behind that flag ({fc['path']}:{fc['line']}: `{fc['text'][:70]}`)"
    elif s["link"] and fc:
        text += f"; {s['link_reason']} ({fc['path']}:{fc['line']})"
    return text + "."
