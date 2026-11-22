"""Log Detective: parsing, grouping, spikes, explanations linked to code + git history, team scoping."""
import pytest
from fastapi.testclient import TestClient

from tracebase.config import settings
from tracebase.logs.grouper import normalise
from tracebase.logs.parser import extract_frames, parse_text
from tracebase.timeutil import parse_since, parse_ts

ADMIN = "admin-secret-for-tests"


@pytest.fixture()
def logs(indexed, monkeypatch):
    """The demo logs (seeded next to the demo repos) collected into a clean log store."""
    monkeypatch.setattr(settings, "logs_dir", settings.repos_dir.parent / "logs")
    monkeypatch.setattr(settings, "admin_token", "")
    from tracebase.db import tx
    with tx() as c:
        for t in ("log_events", "error_groups", "log_offsets", "alerts"):
            c.execute(f"DELETE FROM {t}")
    from tracebase.logs import grouper
    from tracebase.logs.collector import collect_files
    stats = collect_files()
    groups = {g["service"] + ":" + g["title"][:20]: g for g in grouper.find_errors(limit=50)}
    return stats, groups


def _gid(groups, prefix):
    return next(g["id"] for k, g in groups.items() if k.startswith(prefix))


# ------------------------------------------------------------------ parsing
def test_parses_many_formats_and_attaches_stack_traces():
    text = "\n".join([
        '[2026-11-08 14:04:04] production.ERROR: Undefined array key "coupon" {"userId":1}',
        "2026-11-08 03:00:01 [ERROR] checkout  SQLSTATE[HY000] [2002] Connection refused",
        '{"time": "2026-11-08T06:00:00", "level": "error", "service": "payments", "msg": "Stripe webhook signature mismatch"}',
        "2026-11-08 06:00:03,552 - invoice_bot - ERROR - failed to email invoice",
        "Traceback (most recent call last):",
        '  File "/app/invoice_bot/render.py", line 21, in email_invoice',
        "ConnectionRefusedError: [Errno 111] Connection refused",
        '2026/11/08 03:00:05 [error] 31#0: *1 connect() failed (111: Connection refused) while connecting to upstream',
        '10.0.0.1 - - [08/Nov/2026:03:00:06 +0000] "GET /api/cart HTTP/1.1" 502 157 "-" "curl"',
    ])
    ev = parse_text(text, source="mixed.log")
    assert [e.level for e in ev] == ["ERROR"] * 6
    assert ev[2].service == "payments"
    assert ev[3].message == "failed to email invoice"
    assert ev[3].stack[-1].startswith("ConnectionRefusedError")  # the traceback is attached to the event
    frames = extract_frames(ev[3].text)
    assert frames[0].path == "/app/invoice_bot/render.py" and frames[0].line == 21
    assert ev[5].message == "GET /api/cart -> 502"


def test_normalise_groups_variable_parts():
    a = normalise("Payment gateway timeout after 30s (order 88088) req_8f2k1 user 1234")
    b = normalise("Payment gateway timeout after 30s (order 91234) req_99zzz user 9876")
    assert a == b
    assert normalise("evt_1Q9x12345... signature mismatch") == normalise("evt_1Q9x99999... signature mismatch")


def test_time_expressions():
    ref = parse_ts("2026-11-08 15:00:00")
    assert parse_since("last night", ref) == parse_ts("2026-11-07 18:00:00")
    assert parse_since("2h", ref) == parse_ts("2026-11-08 13:00:00")
    assert parse_since("yesterday 6pm", ref) == parse_ts("2026-11-07 18:00:00")


# ------------------------------------------------------------------ grouping, spikes, alerts
def test_thousands_of_errors_become_a_few_groups(logs):
    stats, groups = logs
    assert stats["events"] > 5000
    refused = groups[next(k for k in groups if k.startswith("checkout:SQLSTATE"))]
    assert refused["count"] > 3000
    assert len(groups) == 5  # checkout x2, payments, shop-api, invoice-bot

    from tracebase.alerts import list_alerts
    titles = [a["title"] for a in list_alerts()]
    assert any(t.startswith("checkout: error spike") for t in titles)
    assert any("New error type in shop-api" in t for t in titles)


def test_resolved_error_regresses_and_alerts(logs):
    from tracebase.alerts import list_alerts
    from tracebase.logs import grouper
    from tracebase.logs.collector import ingest_payload
    _, groups = logs
    gid = _gid(groups, "invoice-bot:")
    grouper.set_status(gid, "resolved")
    g = grouper.get_group(gid)  # the same error (same message and stack) a day later
    ingest_payload({"events": [{"ts": "2026-11-09T06:00:03", "level": "error", "service": "invoice-bot",
                                "message": g["sample"], "stack": g["stack"]}]})
    assert grouper.get_group(gid)["status"] == "regressed"
    assert any(a["title"].startswith(f"E-{gid} is back") for a in list_alerts())


# ------------------------------------------------------------------ explanations
def test_explains_with_failing_code_fix_and_the_commit_that_changed_it(logs):
    from tracebase.logs.explain import explain_group
    _, groups = logs
    r = explain_group(_gid(groups, "shop-api:Undefined"))
    top = r["code"][0]
    assert (top["repo"], top["path"]) == ("shop-api", "app/Services/CartService.php")
    assert any("$cart['coupon']" in l["text"] for l in top["snippet"] if l["n"] == top["line"])
    assert "?? null" in r["fix"][0]  # concrete one-line fix
    assert r["commit"]["message"].startswith("Refactor applyDiscount")  # git blame of the failing line
    assert 0 <= r["commit"]["minutes_before_first_error"] < 60
    assert r["commit"]["sha"] in r["cause"]


def test_explains_outage_from_other_services(logs):
    from tracebase.logs.explain import explain_group
    _, groups = logs
    r = explain_group(_gid(groups, "checkout:SQLSTATE"))
    assert "db" in r["cause"] and "nightly-backup" in r["cause"]  # the backup that stopped the database
    assert any(e["service"] == "db" for e in r["events"])
    assert r["fix"][0].startswith("Use a live dump")


# ------------------------------------------------------------------ API + team mode
@pytest.fixture()
def api(logs, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    monkeypatch.setattr(settings, "enable_scheduler", False)
    from tracebase.db import tx
    with tx() as c:
        c.execute("DELETE FROM users")
    from tracebase.main import app
    return TestClient(app)


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_member_only_sees_logs_of_their_services(api, logs):
    _, groups = logs
    tok = api.post("/api/team/users", json={"name": "ravi", "repos": ["shop-api"]}, headers=_h(ADMIN)).json()["token"]
    h = _h(tok)
    mine = api.get("/api/logs/groups", headers=h).json()
    assert mine and {g["service"] for g in mine} == {"shop-api"}
    assert {s["service"] for s in api.get("/api/logs/services", headers=h).json()} == {"shop-api"}
    assert all(a["service"] == "shop-api" for a in api.get("/api/alerts", headers=h).json())
    other = _gid(groups, "checkout:SQLSTATE")
    assert api.get(f"/api/logs/groups/E-{other}", headers=h).status_code == 404
    assert api.post(f"/api/logs/groups/E-{other}/explain", headers=h).status_code == 404
    assert all(e["service"] == "shop-api" for e in api.get("/api/logs/events", headers=h).json())
    # their own error explains fine, with their code
    r = api.post(f"/api/logs/groups/E-{_gid(groups, 'shop-api:')}/explain", headers=h).json()
    assert r["code"][0]["repo"] == "shop-api"
    # they can only send logs for their services
    assert api.post("/api/logs/ingest", params={"service": "shop-api"}, content=b"2026-11-09 10:00:00 [ERROR] boom",
                    headers={**h, "content-type": "text/plain"}).status_code == 200
    assert api.post("/api/logs/ingest", params={"service": "payments"}, content=b"2026-11-09 10:00:00 [ERROR] boom",
                    headers={**h, "content-type": "text/plain"}).status_code == 403
    assert api.post("/api/logs/collect", headers=h).status_code == 403


def test_member_explanation_hides_other_services(api, logs):
    _, groups = logs
    tok = api.post("/api/team/users", json={"name": "meera", "repos": ["checkout"]}, headers=_h(ADMIN)).json()["token"]
    r = api.post(f"/api/logs/groups/E-{_gid(groups, 'checkout:SQLSTATE')}/explain", headers=_h(tok)).json()
    assert r["events"] == []  # db / nightly-backup are not theirs
    assert "nightly-backup" not in r["cause"]
