"""Privacy: redaction before storage, retention, forget / purge / export / wipe, encryption at rest, API access."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tracebase.config import settings
from tracebase.redact import redact

ADMIN = "admin-secret-for-tests"


# ------------------------------------------------------------------ redaction rules
@pytest.mark.parametrize("raw, expected", [
    ("Login failed for john.doe+x@example.co.uk", "Login failed for [email]"),
    ("from 203.0.113.42 port 443", "from [ip] port 443"),
    ("peer fe80::1ff:fe23:4567:890a closed", "peer [ip] closed"),
    ("POST /reset?token=abc123def&email=a@b.io", "POST /reset?token=[redacted]&email=[email]"),
    ("Authorization: Bearer abc.def-123456789", "Authorization: Bearer [redacted]"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N", "jwt [jwt]"),
    ("card=4111 1111 1111 1111 declined", "card=[card] declined"),
    ("paid with 4111111111111111", "paid with [card]"),
    ('{"password":"hunter2","api_key":"zzz"}', '{"password":"[redacted]","api_key":"[redacted]"}'),
    ("'db_password' => 'p4ss'", "'db_password' => '[redacted]'"),
    ("mysql://app:s3cret@db:3306/shop", "mysql://app:[redacted]@db:3306/shop"),
    ("key sk_live_abcdefghij1234567890 rejected", "key [secret] rejected"),
    ("call +91 98765 43210 now", "call [phone] now"),
    ("Cookie: laravel_session=abc; XSRF-TOKEN=def", "Cookie: [redacted]"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "[private-key]"),
])
def test_redacts_personal_data_and_secrets(raw, expected):
    assert redact(raw) == expected


@pytest.mark.parametrize("keep", [
    'Undefined array key "coupon" in /app/Services/CartService.php:57',
    "SQLSTATE[HY000] [2002] Connection refused",
    "CSRF token mismatch",
    "order 1234567890123456 not found",            # not a card number (fails the Luhn check / prefix)
    "at 2026-11-08T03:00:01+05:30 took 12:30:45",  # timestamps are not IPv6 or phone numbers
    "Cart::add failed, Db::add too",               # PHP static calls are not IPv6
    "listening on 127.0.0.1 and ::1",
    "Payment gateway timeout after 30s (order 88088)",
])
def test_keeps_what_explanations_need(keep):
    assert redact(keep) == keep


def test_redaction_settings(monkeypatch):
    monkeypatch.setattr(settings, "redact_ips", False)
    assert redact("from 203.0.113.42") == "from 203.0.113.42"
    monkeypatch.setattr(settings, "redact_extra", '["CUST-\\\\d{6}"]')
    assert redact("customer CUST-123456 failed") == "customer [redacted] failed"
    monkeypatch.setattr(settings, "redact_logs", False)
    assert redact("x a@b.io") == "x a@b.io"


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def clean(indexed, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "enable_scheduler", False)
    monkeypatch.setattr(settings, "logs_dir", settings.repos_dir.parent / "no-logs-here")
    from tracebase.db import tx
    with tx() as c:
        for t in ("log_events", "error_groups", "log_offsets", "alerts", "users", "audit"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM kv WHERE key IN ('excluded_repos', 'excluded_services')")
    yield
    with tx() as c:
        c.execute("DELETE FROM kv WHERE key IN ('excluded_repos', 'excluded_services')")


def _ingest(lines, service="shop-api"):
    from tracebase.logs.collector import ingest_payload
    return ingest_payload({"service": service, "lines": lines})


def _all_text() -> str:
    from tracebase.db import rows
    out = [json.dumps(r) for t in ("log_events", "error_groups", "alerts") for r in rows(f"SELECT * FROM {t}")]
    return "\n".join(out)


# ------------------------------------------------------------------ redaction on ingest
def test_nothing_personal_reaches_the_database(clean):
    res = _ingest([
        "2026-11-09 10:00:00 [ERROR] Login failed for alice@example.com from 198.51.100.7",
        "2026-11-09 10:00:05 [ERROR] Login failed for bob@example.org from 198.51.100.9",
        "2026-11-09 10:01:00 [ERROR] Stripe charge failed card=4242424242424242 key=sk_live_abcdefghij1234567890",
        "Traceback (most recent call last):",
        '  File "/app/pay.py", line 9, in charge',
        "ValueError: bad token Bearer abcdefghijklmnop for carol@example.net",
    ])
    assert res["events"] == 3
    stored = _all_text()
    for leaked in ("alice@", "bob@", "carol@", "198.51.100", "4242424242424242", "sk_live_", "abcdefghijklmnop"):
        assert leaked not in stored, leaked
    assert "[email]" in stored and "[ip]" in stored and "[card]" in stored
    from tracebase.logs import grouper
    groups = grouper.find_errors()
    assert sum(1 for g in groups if g["title"].startswith("Login failed")) == 1  # one group, not one per user
    assert any(f["path"] == "/app/pay.py" for g in groups for f in g["frames"])  # stack frames still extracted


def test_forgotten_service_logs_are_dropped_on_arrival(clean):
    from tracebase import privacy
    _ingest(["2026-11-09 10:00:00 [ERROR] boom"], service="legacy")
    privacy.forget_service("legacy", exclude=True)
    assert _ingest(["2026-11-09 10:05:00 [ERROR] boom again"], service="legacy")["events"] == 0
    assert "legacy" not in _all_text()
    privacy.restore_service("legacy")
    assert _ingest(["2026-11-09 10:06:00 [ERROR] boom"], service="legacy")["events"] == 1


# ------------------------------------------------------------------ data stored before redaction existed
def test_upgrade_redacts_old_data_and_merges_groups(clean, monkeypatch):
    from tracebase import privacy
    from tracebase.db import kv_set, row, tx
    from tracebase.logs import grouper

    monkeypatch.setattr(settings, "redact_logs", False)  # an older version: stored as-is, one group per email
    _ingest(["2026-11-09 10:00:00 [ERROR] Login failed for alice@example.com",
             "2026-11-09 10:00:01 [ERROR] Login failed for bob@example.com"])
    with tx() as c:
        c.execute("INSERT INTO alerts(kind,severity,title,body,dedupe_key) VALUES('log','warning','x','for alice@example.com','k1')")
        c.execute("UPDATE error_groups SET explanation='{\"cause\": \"alice@example.com\"}'")
    assert len(grouper.find_errors()) == 2
    kv_set("redaction_version", "0")

    monkeypatch.setattr(settings, "redact_logs", True)
    res = privacy.redact_stored()
    assert res["merged_groups"] == 1 and res["log_events"] == 2 and res["alerts"] == 3  # + 2 "new error" alerts
    assert "example.com" not in _all_text()
    groups = grouper.find_errors()
    assert len(groups) == 1 and groups[0]["count"] == 2 and groups[0]["explanation"] is None
    assert row("SELECT COUNT(*) n FROM log_events WHERE group_id=?", (groups[0]["id"],))["n"] == 2
    assert privacy.redact_stored() == {"skipped": "already redacted"}  # once per rules version
    # new events of the same shape land in the merged group
    _ingest(["2026-11-09 10:02:00 [ERROR] Login failed for carol@example.com"])
    assert [g["count"] for g in grouper.find_errors()] == [3]


# ------------------------------------------------------------------ retention
def test_retention_deletes_old_records(clean, monkeypatch):
    from tracebase import privacy
    from tracebase.db import row, tx
    from tracebase.timeutil import iso, now
    from datetime import timedelta

    old, recent = iso(now() - timedelta(days=200)), iso(now() - timedelta(days=1))
    _ingest([f"{old.replace('T', ' ')} [ERROR] ancient failure", f"{recent.replace('T', ' ')} [ERROR] fresh failure"])
    with tx() as c:
        c.execute("INSERT INTO alerts(kind,severity,title,created_at,dedupe_key) VALUES('log','warning','old',"
                  "datetime('now','-200 days'),'old')")
        c.execute("INSERT INTO audit(at,user,action) VALUES(datetime('now','-400 days'),'x','ask')")
        c.execute("INSERT INTO audit(user,action) VALUES('x','ask')")
        c.execute("INSERT INTO log_offsets(path,inode,offset) VALUES('gone.log',1,10)")
    monkeypatch.setattr(settings, "logs_dir", settings.repos_dir.parent)  # exists; gone.log does not
    res = privacy.prune()
    assert res["log_events"] == 1 and res["error_groups"] == 1 and res["audit"] == 1 and res["log_offsets"] == 1
    assert res["alerts"] >= 1
    assert row("SELECT COUNT(*) n FROM error_groups")["n"] == 1
    assert row("SELECT COUNT(*) n FROM audit")["n"] == 1

    monkeypatch.setattr(settings, "log_retention_days", 0)  # 0 = keep forever
    _ingest([f"{old.replace('T', ' ')} [ERROR] ancient failure"])
    assert privacy.prune()["log_events"] == 0


# ------------------------------------------------------------------ forget / purge / export
def test_purge_matching_and_export(clean):
    from tracebase import privacy
    _ingest(["2026-11-09 10:00:00 [ERROR] order ORD-77812 failed: stock",
             "2026-11-09 10:00:01 [ERROR] order ORD-90001 failed: stock",
             "2026-11-09 10:00:02 [INFO] order ORD-77812 retried"], service="orders")
    lines = [json.loads(l) for l in privacy.export(service="orders", match="ORD-77812")]
    assert lines[0]["type"] == "export"
    assert sorted(l["type"] for l in lines[1:]) == ["alert", "error_group", "log_event", "log_event"]

    dry = privacy.purge_matching("ORD-77812", dry_run=True)
    assert dry["would_delete"]["log_events"] == 2
    res = privacy.purge_matching("ORD-77812")
    assert res["deleted"]["log_events"] == 2 and res["deleted"]["error_groups"] == 1
    assert "ORD-77812" not in _all_text() and "ORD-90001" in _all_text()
    with pytest.raises(ValueError):
        privacy.purge_matching("ab")  # too short: would delete almost everything
    assert privacy.purge_matching("100%_x")["deleted"]["log_events"] == 0  # LIKE wildcards are literal


def test_forget_repo_removes_index_and_blocks_access(clean):
    from tracebase import privacy
    from tracebase.code import search
    from tracebase.code.indexer import index_all
    from tracebase.db import row

    repo = "side/expense-tracker"
    assert row("SELECT COUNT(*) n FROM chunks WHERE repo=?", (repo,))["n"] > 0
    try:
        res = privacy.forget_repo(repo)
        assert res["deleted"]["chunks"] > 0
        for t, col in (("chunks", "repo"), ("files", "repo"), ("secrets", "repo"), ("repos", "name")):
            assert row(f"SELECT COUNT(*) n FROM {t} WHERE {col}=?", (repo,))["n"] == 0
        assert not row("SELECT COUNT(*) n FROM chunks_fts WHERE chunks_fts MATCH 'expense'")["n"] or \
            all(r["repo"] != repo for r in search.search("expense tracker")["results"])
        assert "error" in search.read_code(repo, "src/auth/tokens.ts")  # not readable from disk either
        index_all()
        assert row("SELECT COUNT(*) n FROM files WHERE repo=?", (repo,))["n"] == 0  # not re-indexed
    finally:
        privacy.restore_repo(repo)
        index_all()
    assert row("SELECT COUNT(*) n FROM files WHERE repo=?", (repo,))["n"] > 0


# ------------------------------------------------------------------ API
@pytest.fixture()
def api(clean, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    from tracebase.main import app
    return TestClient(app)


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_data_endpoints_are_admin_only_and_audited(api):
    tok = api.post("/api/team/users", json={"name": "ravi", "repos": ["*"]}, headers=_h(ADMIN)).json()["token"]
    for method, url in (("GET", "/api/data/summary"), ("GET", "/api/data/export"), ("POST", "/api/data/prune"),
                        ("DELETE", "/api/data/services/x"), ("DELETE", "/api/data/repos/x")):
        assert api.request(method, url, headers=_h(tok)).status_code == 403, url
    api.post("/api/logs/ingest", params={"service": "orders"}, headers={**_h(ADMIN), "content-type": "text/plain"},
             content=b"2026-11-09 10:00:00 [ERROR] order ORD-55555 failed for x@y.com")
    s = api.get("/api/data/summary", headers=_h(ADMIN)).json()
    assert s["counts"]["log_events"] == 1 and s["redaction"]["enabled"] and s["retention_days"]["log_events"] == 30
    exp = api.get("/api/data/export", params={"service": "orders"}, headers=_h(ADMIN))
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    assert "x@y.com" not in exp.text and "[email]" in exp.text
    r = api.post("/api/data/purge", json={"match": "ORD-55555"}, headers=_h(ADMIN)).json()
    assert r["deleted"]["log_events"] == 1
    audit = api.get("/api/team/audit", headers=_h(ADMIN)).json()
    purge = next(a for a in audit if a["action"] == "data_purge")
    assert "ORD-55555" not in json.dumps(purge)  # the erased value is not kept in the audit log
    assert api.post("/api/data/wipe", json={"confirm": "yes"}, headers=_h(ADMIN)).status_code == 400


def test_health_hides_details_from_visitors(api):
    anon = api.get("/api/health").json()
    assert anon == {"status": "ok", "version": anon["version"], "phase": 3, "team": True}
    full = api.get("/api/health", headers=_h(ADMIN)).json()
    assert "repos_dir" in full and full["privacy"]["redaction"] is True


def test_security_headers_and_ingest_limit(api, monkeypatch):
    r = api.get("/api/stats", headers=_h(ADMIN))
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in api.get("/api/stats", headers={**_h(ADMIN), "Origin": "https://evil.test"}).headers
    monkeypatch.setattr(settings, "max_ingest_mb", 1)
    big = b"2026-11-09 10:00:00 [INFO] x\n" * 50000
    assert api.post("/api/logs/ingest", content=big, headers={**_h(ADMIN), "content-type": "text/plain"}).status_code == 413
    bad = api.post("/api/logs/ingest", content=b"{nope", headers={**_h(ADMIN), "content-type": "application/json"})
    assert bad.status_code == 400


# ------------------------------------------------------------------ encryption at rest
def test_database_encryption(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    from tracebase import db

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_key", "")
    db.reset_for_tests()
    try:
        db.kv_set("probe", "plain-secret-value")  # an existing plain database...
        db.reset_for_tests()
        monkeypatch.setattr(settings, "db_key", "correct horse 'battery'")
        assert db.kv_get("probe") == "plain-secret-value"  # ...is encrypted in place on first open with a key
        db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = (tmp_path / "tracebase.db").read_bytes()
        assert not raw.startswith(b"SQLite format 3") and b"plain-secret-value" not in raw
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.connect(tmp_path / "tracebase.db").execute("SELECT * FROM kv").fetchall()
        db.reset_for_tests()
        monkeypatch.setattr(settings, "db_key", "wrong")
        with pytest.raises(db.DatabaseKeyError):
            db.connect()
    finally:
        db.reset_for_tests()
