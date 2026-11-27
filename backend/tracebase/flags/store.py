# SPDX-License-Identifier: Apache-2.0
"""Flag changes and rollbacks in the Tracebase database, and the Flagsmith webhook payload turned into rows."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

from ..db import row, rows, tx
from ..timeutil import iso, now, parse_ts

# ------------------------------------------------------------------ webhook
def valid_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Flagsmith signs the exact JSON body with HMAC-SHA256 and sends the hex digest in X-Flagsmith-Signature."""
    if not signature:
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip())


def _rollout(state: dict | None) -> list[dict]:
    """Multivariate percentage split of a feature state: [{"value": "v2", "percent": 50}, ...]."""
    out = []
    for mv in (state or {}).get("multivariate_feature_state_values") or []:
        opt = mv.get("multivariate_feature_option") or {}
        out.append({"value": opt.get("value"), "percent": mv.get("percentage_allocation")})
    return out


def _fmt_rollout(r: list[dict]) -> str:
    return ", ".join(f"{x['value']} {x['percent']:g}%" for x in r if x.get("percent") is not None)


def describe(enabled_before: bool | None, enabled_after: bool | None, value_before: Any, value_after: Any,
             rollout_before: list[dict], rollout_after: list[dict], deleted: bool = False) -> str:
    """One line a person reads: 'turned on', 'value "a" -> "b"', 'rollout v2 10% -> v2 50%'."""
    if deleted:
        return "deleted"
    parts = []
    if enabled_before is None and enabled_after is not None:
        parts.append("created " + ("on" if enabled_after else "off"))
    elif enabled_before != enabled_after:
        parts.append("turned on" if enabled_after else "turned off")
    if value_before != value_after and not (value_before in (None, "") and value_after in (None, "")):
        parts.append(f"value {json.dumps(value_before)} -> {json.dumps(value_after)}")
    if rollout_before != rollout_after and (rollout_before or rollout_after):
        parts.append(f"rollout {_fmt_rollout(rollout_before) or 'none'} -> {_fmt_rollout(rollout_after) or 'none'}")
    return "; ".join(parts) or "saved (no change)"


def parse_flagsmith(payload: dict) -> dict | None:
    """A Flagsmith environment webhook (FLAG_UPDATED / FLAG_DELETED) as a flag_changes row, or None to ignore."""
    event = payload.get("event_type")
    data = payload.get("data") or {}
    if event not in ("FLAG_UPDATED", "FLAG_DELETED"):
        return None
    new, old = data.get("new_state") or {}, data.get("previous_state") or {}
    state = new or old
    feature = state.get("feature") or {}
    if not feature.get("name"):
        return None
    env = state.get("environment") or {}
    project = feature.get("project") or {}
    seg = ((state.get("feature_segment") or {}).get("segment")) or {}
    deleted = event == "FLAG_DELETED"
    eb = old.get("enabled") if old else None
    ea = None if deleted else new.get("enabled")
    vb = old.get("feature_state_value") if old else None
    va = None if deleted else new.get("feature_state_value")
    rb, ra = _rollout(old), ([] if deleted else _rollout(new))
    changed_by = data.get("changed_by")
    if isinstance(changed_by, dict):
        changed_by = changed_by.get("email") or changed_by.get("name")
    ts = parse_ts(data.get("timestamp")) or now()
    return {
        "ts": iso(ts), "provider": "flagsmith",
        "project": project.get("name"), "project_id": project.get("id"),
        "environment": env.get("name"), "environment_id": env.get("id"),
        "feature": feature["name"], "feature_id": feature.get("id"),
        "segment": seg.get("name"), "segment_id": seg.get("id"),
        "identity": state.get("identity_identifier"),
        "enabled_before": eb, "enabled_after": ea,
        "value_before": None if vb is None else json.dumps(vb), "value_after": None if va is None else json.dumps(va),
        "rollout_before": json.dumps(rb) if rb else None, "rollout_after": json.dumps(ra) if ra else None,
        "summary": describe(eb, ea, vb, va, rb, ra, deleted),
        "changed_by": changed_by, "source": "webhook",
    }


COLS = ("ts", "provider", "project", "project_id", "environment", "environment_id", "feature", "feature_id", "segment",
        "segment_id", "identity", "enabled_before", "enabled_after", "value_before", "value_after", "rollout_before",
        "rollout_after", "summary", "changed_by", "source")


def record(change: dict) -> dict | None:
    """Store one change (idempotent: Flagsmith retries webhooks). Returns the stored row, or None for a duplicate."""
    key = hashlib.sha256(json.dumps({k: change.get(k) for k in COLS if k != "source"}, sort_keys=True,
                                    default=str).encode()).hexdigest()[:32]
    vals = [change.get(k) for k in COLS]
    if change.get("source") == "webhook" and change.get("enabled_after") is False:
        # Flagsmith's webhook for a rollback Tracebase just made: already on the timeline as Tracebase's change
        ts = parse_ts(change["ts"]) or now()
        if row("SELECT id FROM flag_changes WHERE source='tracebase' AND feature=? AND environment_id IS ? AND segment_id IS ?"
               " AND ts BETWEEN ? AND ?", (change["feature"], change.get("environment_id"), change.get("segment_id"),
                                          iso(ts - timedelta(minutes=2)), iso(ts + timedelta(minutes=2)))):
            return None
    with tx() as c:
        cur = c.execute(f"INSERT OR IGNORE INTO flag_changes({','.join(COLS)}, dedupe_key) VALUES({','.join('?' * len(COLS))}, ?)",
                        (*vals, key))
        if not cur.rowcount:
            return None
        cid = cur.lastrowid
        # explanations cached before this change arrived may now be missing a suspect
        ts = parse_ts(change["ts"]) or now()
        c.execute("UPDATE error_groups SET explanation=NULL WHERE last_seen >= ?", (iso(ts - timedelta(minutes=2)),))
    return get_change(cid)


def get_change(cid: int) -> dict | None:
    r = row("SELECT * FROM flag_changes WHERE id=?", (cid,))
    return _decode(r) if r else None


def _decode(r: dict) -> dict:
    r = dict(r)
    for k in ("value_before", "value_after", "rollout_before", "rollout_after"):
        if r.get(k) is not None:
            try:
                r[k] = json.loads(r[k])
            except ValueError:
                pass
    for k in ("enabled_before", "enabled_after"):
        if r.get(k) is not None:
            r[k] = bool(r[k])
    r.pop("dedupe_key", None)
    return r


def history(feature: str | None = None, environment: str | None = None, since: str | None = None,
            until: str | None = None, limit: int = 100) -> list[dict]:
    where, p = ["1=1"], []
    if feature:
        where.append("feature=?")
        p.append(feature)
    if environment:
        where.append("environment=?")
        p.append(environment)
    if since:
        where.append("ts>=?")
        p.append(since)
    if until:
        where.append("ts<=?")
        p.append(until)
    return [_decode(r) for r in rows(f"SELECT * FROM flag_changes WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
                                     (*p, limit))]


def rollbacks(group_id: int | None = None, status: str | None = None) -> list[dict]:
    where, p = ["1=1"], []
    if group_id is not None:
        where.append("group_id=?")
        p.append(group_id)
    if status:
        where.append("status=?")
        p.append(status)
    return rows(f"SELECT * FROM flag_rollbacks WHERE {' AND '.join(where)} ORDER BY id DESC", tuple(p))
