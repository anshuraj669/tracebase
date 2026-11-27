# SPDX-License-Identifier: Apache-2.0
"""Turn a suspect flag off in Flagsmith (only after a person approves it), then watch whether the error stops.

Nothing here runs on its own: a rollback starts from an explicit request (the UI's button, `tracebase why --rollback`,
or an AI agent's tool call that the person confirmed). The follow-up check runs on the scheduler: when the watch
window has passed with no new occurrences, the error group is marked resolved and an alert says the rollback worked.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import alerts, team
from ..config import settings
from ..db import row, tx
from ..timeutil import iso, now, parse_ts
from . import store
from .flagsmith import Flagsmith, FlagsmithError

log = logging.getLogger(__name__)
GRACE = timedelta(seconds=45)  # SDKs cache flags and log lines arrive late: errors this soon after still count as "before"


class RollbackError(RuntimeError):
    pass


def _errors_between(gid: int, start: str, end: str | None = None) -> int:
    q = "SELECT COUNT(*) n FROM log_events WHERE group_id=? AND ts>=?" + (" AND ts<?" if end else "")
    return row(q, (gid, start, end) if end else (gid, start))["n"]


def apply(gid: int, change_id: int, approved_by: str | None = None, client: Flagsmith | None = None) -> dict:
    """Turn off the flag state that `change_id` turned on. The caller has already asked the person."""
    ch = store.get_change(change_id)
    if not ch:
        raise RollbackError(f"flag change {change_id} not found")
    if ch.get("provider") != "flagsmith" or not ch.get("feature_id") or not ch.get("environment_id"):
        raise RollbackError("only Flagsmith flags with a known feature and environment can be turned off from Tracebase")
    active = [r for r in store.rollbacks(gid, "watching") if r["change_id"] == change_id]
    if active:
        return {"ok": True, "already": True, "rollback": active[0]}
    fs = client or Flagsmith()
    try:
        state = fs.find_state(ch["environment_id"], ch["feature_id"], ch.get("segment_id"))
        was_on = bool(state.get("enabled"))
        if was_on:
            fs.set_enabled(state, False)
    except FlagsmithError as e:
        with tx() as c:
            c.execute("INSERT INTO flag_rollbacks(created_at, group_id, change_id, feature, environment, segment,"
                      " approved_by, status, note) VALUES(?,?,?,?,?,?,?,'failed',?)",
                      (iso(now()), gid, change_id, ch["feature"], ch["environment"], ch.get("segment"),
                       approved_by, str(e)[:500]))
        raise RollbackError(str(e)) from e
    at = now()
    before = _errors_between(gid, iso(at - timedelta(minutes=settings.flag_verify_minutes)), iso(at))
    with tx() as c:
        cur = c.execute("INSERT INTO flag_rollbacks(created_at, group_id, change_id, feature, environment, segment,"
                        " feature_state_id, approved_by, status, errors_before, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (iso(at), gid, change_id, ch["feature"], ch["environment"], ch.get("segment"), state["id"],
                         approved_by, "watching", before, None if was_on else "flag was already off in Flagsmith"))
        rid = cur.lastrowid
    # our own change goes on the timeline too (the webhook that follows is de-duplicated by its content, not this row)
    store.record({**{k: ch.get(k) for k in store.COLS}, "ts": iso(at), "enabled_before": was_on, "enabled_after": False,
                  "value_before": None, "value_after": None, "rollout_before": None, "rollout_after": None,
                  "summary": "turned off (rollback from Tracebase)", "changed_by": approved_by or team.current().name,
                  "source": "tracebase"})
    team.log("flag_rollback", None, error=f"E-{gid}", feature=ch["feature"], environment=ch["environment"])
    return {"ok": True, "rollback": row("SELECT * FROM flag_rollbacks WHERE id=?", (rid,)),
            "watch_minutes": settings.flag_verify_minutes}


def verify() -> list[dict]:
    """Scheduler job: settle rollbacks whose watch window has passed."""
    done = []
    for r in store.rollbacks(status="watching"):
        start = parse_ts(r["created_at"])
        if not start or now() < start + timedelta(minutes=settings.flag_verify_minutes):
            continue
        after = _errors_between(r["group_id"], iso(start + GRACE))
        worked = after == 0
        status = "worked" if worked else "did_not_help"
        with tx() as c:
            c.execute("UPDATE flag_rollbacks SET status=?, errors_after=?, checked_at=? WHERE id=?",
                      (status, after, iso(now()), r["id"]))
            if worked:
                c.execute("UPDATE error_groups SET status='resolved' WHERE id=? AND status IN ('new','regressed','known')",
                          (r["group_id"],))
        g = row("SELECT service FROM error_groups WHERE id=?", (r["group_id"],)) or {}
        if worked:
            alerts.raise_alert("flag_rollback", "info", f"E-{r['group_id']} stopped after turning off '{r['feature']}'",
                       f"No new occurrences in the {settings.flag_verify_minutes} minutes after the flag "
                       f"'{r['feature']}' was turned off in {r['environment']} ({r['errors_before']} in the minutes before). "
                       "Marked resolved by the rollback.",
                       f"/logs/E-{r['group_id']}", dedupe_key=f"flag_rollback:{r['id']}", service=g.get("service"))
        else:
            alerts.raise_alert("flag_rollback", "warning", f"E-{r['group_id']} continues after turning off '{r['feature']}'",
                       f"{after} new occurrences after the flag '{r['feature']}' was turned off: the flag was not the "
                       "(only) cause. The flag is still off; turn it back on in Flagsmith if you want it.",
                       f"/logs/E-{r['group_id']}", dedupe_key=f"flag_rollback:{r['id']}", service=g.get("service"))
        done.append({**r, "status": status, "errors_after": after})
    return done
