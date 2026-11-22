"""Alerts: stored in SQLite and shown in the UI / `tracebase alerts`.

Optionally pushed to a webhook YOU configure (TRACEBASE_ALERT_WEBHOOK: a self-hosted ntfy, Slack, ...).
Off by default, so nothing leaves the machine unless you ask for it."""
from __future__ import annotations

import logging
import threading

import httpx

from . import team
from .config import settings
from .db import rows, tx

log = logging.getLogger(__name__)


def raise_alert(kind: str, severity: str, title: str, body: str = "", link: str = "", dedupe_key: str | None = None,
                service: str | None = None) -> bool:
    with tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO alerts(kind,severity,service,title,body,link,dedupe_key) VALUES(?,?,?,?,?,?,?)",
            (kind, severity, service, title, body, link, dedupe_key),
        )
        created = cur.rowcount > 0
    if created and settings.alert_webhook:
        threading.Thread(target=_push, args=(title, body), daemon=True).start()
    return created


def _push(title: str, body: str) -> None:
    try:
        httpx.post(settings.alert_webhook, json={"text": f"{title}\n{body}", "title": title, "message": body}, timeout=10)
    except Exception as e:  # pragma: no cover
        log.warning("alert webhook failed: %s", e)


def list_alerts(unread_only: bool = False, limit: int = 50) -> list[dict]:
    scope, p = team.service_filter("service")
    q = "SELECT id, datetime(created_at, 'localtime') AS created_at, kind, severity, service, title, body, link, read FROM alerts WHERE 1=1" + (" AND read=0" if unread_only else "") + scope + " ORDER BY id DESC LIMIT ?"
    return rows(q, tuple(p + [limit]))


def mark_read(alert_id: int | None = None) -> None:
    scope, p = team.service_filter("service")
    with tx() as c:
        if alert_id is None:
            c.execute("UPDATE alerts SET read=1 WHERE 1=1" + scope, tuple(p))
        else:
            c.execute("UPDATE alerts SET read=1 WHERE id=?" + scope, tuple([alert_id] + p))
