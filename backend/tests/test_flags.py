# SPDX-License-Identifier: Apache-2.0
"""Feature flags (Flagsmith): webhooks on the timeline, flags as suspects in "why", the approved kill switch."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tracebase.config import settings

ADMIN = "admin-secret-for-tests"
SECRET = "whsec-for-tests"
T0 = datetime(2026, 11, 20, 10, 0, 0)  # the flag is turned on; errors start 3 minutes later


def flag_webhook(enabled_before, enabled_after, ts, feature="new_checkout", segment=None, by="asha@shop.test"):
    def state(enabled):
        return {"feature": {"id": 7, "name": feature, "project": {"id": 1, "name": "Shop"}, "type": "STANDARD"},
                "environment": {"id": 3, "name": "Production"}, "identity": None, "identity_identifier": None,
                "feature_segment": {"segment": segment} if segment else None, "enabled": enabled,
                "feature_state_value": None, "multivariate_feature_state_values": []}
    data = {"changed_by": by, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000000"), "new_state": state(enabled_after)}
    if enabled_before is not None:
        data["previous_state"] = state(enabled_before)
    return {"event_type": "FLAG_UPDATED", "data": data}


def signed(payload, secret=SECRET):
    body = json.dumps(payload, sort_keys=True)
    return body.encode(), {"content-type": "application/json",
                           "X-Flagsmith-Signature": hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()}


def checkout_errors(start, n=12, every=20):
    """Laravel errors from the demo shop's CheckoutV2, the code behind the new_checkout flag."""
    path = settings.repos_dir / "shop-api/app/Services/CheckoutV2.php"
    lines = path.read_text().splitlines()
    fail = next(i for i, l in enumerate(lines, 1) if "$rate  = $rates[$cart['currency']];" in l)
    out = []
    for i in range(n):
        t = start + timedelta(seconds=every * i)
        out += [f"[{t:%Y-%m-%d %H:%M:%S}] production.ERROR: Undefined array key \"currency\" {{\"exception\":\"[object] "
                f"(ErrorException(code: 0): Undefined array key \\\"currency\\\" at /var/www/html/app/Services/CheckoutV2.php:{fail})",
                "[stacktrace]",
                f"#0 /var/www/html/app/Services/CheckoutV2.php({fail}): Illuminate\\Foundation\\Bootstrap\\HandleExceptions->handleError()",
                "#1 /var/www/html/app/Http/Controllers/CheckoutController.php(30): App\\Services\\CheckoutV2->total()",
                "#2 {main}", "\"}"]
    return "\n".join(out) + "\n"


@pytest.fixture()
def flags(indexed, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "flagsmith_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "flagsmith_url", "http://flagsmith.test")
    monkeypatch.setattr(settings, "flagsmith_api_key", "key")
    monkeypatch.setattr(settings, "enable_scheduler", False)
    from tracebase.db import tx
    with tx() as c:
        for t in ("log_events", "error_groups", "alerts", "flag_changes", "flag_rollbacks", "users"):
            c.execute(f"DELETE FROM {t}")
    from tracebase.main import app
    return TestClient(app)


def _ingest(api, text):
    r = api.post("/api/logs/ingest", params={"service": "shop-api"}, content=text.encode())
    assert r.status_code == 200, r.text
    return r.json()


def _checkout_group(api):
    return next(g for g in api.get("/api/logs/groups").json() if "currency" in g["title"])


# ------------------------------------------------------------------ webhook
def test_webhook_needs_a_valid_signature(flags):
    body, h = signed(flag_webhook(False, True, T0))
    assert flags.post("/api/flags/webhook", content=body, headers={**h, "X-Flagsmith-Signature": "0" * 64}).status_code == 401
    assert flags.post("/api/flags/webhook", content=body, headers={"content-type": "application/json"}).status_code == 401
    r = flags.post("/api/flags/webhook", content=body, headers=h).json()
    assert r["recorded"] and r["change"]["summary"] == "turned on" and r["change"]["changed_by"] == "asha@shop.test"
    # Flagsmith retries webhooks: the same change is stored once
    assert flags.post("/api/flags/webhook", content=body, headers=h).json()["recorded"] is False
    assert [c["feature"] for c in flags.get("/api/flags/changes").json()] == ["new_checkout"]


def test_webhook_describes_values_rollouts_and_ignores_other_events(flags):
    from tracebase.flags.store import parse_flagsmith
    p = flag_webhook(True, True, T0)
    p["data"]["previous_state"]["multivariate_feature_state_values"] = [
        {"multivariate_feature_option": {"value": "v2"}, "percentage_allocation": 10}]
    p["data"]["new_state"]["multivariate_feature_state_values"] = [
        {"multivariate_feature_option": {"value": "v2"}, "percentage_allocation": 50}]
    assert parse_flagsmith(p)["summary"] == "rollout v2 10% -> v2 50%"
    assert parse_flagsmith({"event_type": "AUDIT_LOG_CREATED", "data": {}}) is None
    body, h = signed({"event_type": "AUDIT_LOG_CREATED", "data": {"log": "x"}})
    assert flags.post("/api/flags/webhook", content=body, headers=h).json()["ignored"] == "AUDIT_LOG_CREATED"


def test_team_mode_webhook_is_open_to_flagsmith_only_with_a_signature(flags, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    body, h = signed(flag_webhook(False, True, T0))
    assert flags.post("/api/flags/webhook", content=body, headers=h).status_code == 200   # no Tracebase token needed
    assert flags.get("/api/flags/changes").status_code == 401                             # but everything else does
    monkeypatch.setattr(settings, "flagsmith_webhook_secret", "")
    assert flags.post("/api/flags/webhook", content=body, headers=h).status_code == 401   # no secret: refused


# ------------------------------------------------------------------ flags as suspects in "why"
def test_why_blames_the_flag_that_guards_the_failing_code(flags):
    body, h = signed(flag_webhook(False, True, T0))
    flags.post("/api/flags/webhook", content=body, headers=h)
    # an unrelated flag changed too, earlier: it must not outrank the one guarding the code
    body2, h2 = signed(flag_webhook(False, True, T0 - timedelta(minutes=50), feature="dark_mode"))
    flags.post("/api/flags/webhook", content=body2, headers=h2)
    _ingest(flags, checkout_errors(T0 + timedelta(minutes=3)))
    g = _checkout_group(flags)
    r = flags.post(f"/api/logs/groups/{g['code']}/explain").json()
    top = r["flags"][0]
    assert top["change"]["feature"] == "new_checkout" and top["link"] == "guards"
    assert top["minutes_before_first_error"] == 3
    assert top["flag_check"]["path"] == "app/Services/CheckoutV2.php"
    assert "isFeatureEnabled('new_checkout')" in top["flag_check"]["text"]
    assert r["cause"].startswith("Feature flag 'new_checkout' was turned on in Production by asha@shop.test 3 minutes before")
    assert r["fix"][0].startswith("Turn off the flag 'new_checkout' in Production")
    # the code shipped dark weeks earlier: the commit is not blamed, the flag is
    assert "likely culprit" not in r["cause"]
    assert flags.get("/api/flags/where", params={"feature": "new_checkout"}).json()[0]["path"] == "app/Services/CheckoutV2.php"


def test_flag_change_after_a_cached_explanation_is_picked_up(flags):
    _ingest(flags, checkout_errors(T0 + timedelta(minutes=3)))
    g = _checkout_group(flags)
    assert flags.post(f"/api/logs/groups/{g['code']}/explain").json()["flags"] == []
    body, h = signed(flag_webhook(False, True, T0))  # the webhook arrives late (Flagsmith retried it)
    flags.post("/api/flags/webhook", content=body, headers=h)
    assert flags.post(f"/api/logs/groups/{g['code']}/explain").json()["flags"][0]["change"]["feature"] == "new_checkout"


def test_flag_turned_on_long_before_and_unrelated_to_the_code_is_not_a_suspect(flags):
    body, h = signed(flag_webhook(False, True, T0 - timedelta(hours=2), feature="dark_mode"))
    flags.post("/api/flags/webhook", content=body, headers=h)
    _ingest(flags, checkout_errors(T0 + timedelta(minutes=3)))
    g = _checkout_group(flags)
    assert flags.get(f"/api/logs/groups/{g['code']}/flags").json()["suspects"] == []


# ------------------------------------------------------------------ kill switch (approved) + verification
class FakeFlagsmith:
    def __init__(self):
        self.enabled, self.calls = True, []

    def find_state(self, environment_id, feature_id, segment_id=None):
        self.calls.append(("find", environment_id, feature_id, segment_id))
        return {"id": 99, "enabled": self.enabled, "identity": None, "feature_segment": None,
                "environment": environment_id, "feature": feature_id}

    def set_enabled(self, state, enabled):
        self.calls.append(("set", state["id"], enabled))
        self.enabled = enabled
        return {"id": state["id"], "enabled": enabled}


@pytest.fixture()
def fake_flagsmith(monkeypatch):
    fake = FakeFlagsmith()
    from tracebase.flags import rollback
    monkeypatch.setattr(rollback, "Flagsmith", lambda: fake)
    return fake


def _suspect(flags, start):
    body, h = signed(flag_webhook(False, True, start - timedelta(minutes=3)))
    flags.post("/api/flags/webhook", content=body, headers=h)
    _ingest(flags, checkout_errors(start))
    g = _checkout_group(flags)
    s = flags.get(f"/api/logs/groups/{g['code']}/flags").json()["suspects"][0]
    return g, s


def _age_rollbacks(minutes):
    from tracebase.db import tx
    from tracebase.timeutil import iso, now
    with tx() as c:
        c.execute("UPDATE flag_rollbacks SET created_at=?", (iso(now() - timedelta(minutes=minutes)),))


def test_rollback_needs_approval_then_turns_the_flag_off_and_resolves_the_error(flags, fake_flagsmith):
    from tracebase.flags.rollback import verify
    start = datetime.now().replace(microsecond=0) - timedelta(minutes=30)
    g, s = _suspect(flags, start)
    assert s["can_roll_back"]
    url = f"/api/logs/groups/{g['code']}/rollback"
    assert flags.post(url, json={"change_id": s["change"]["id"]}).status_code == 400  # not approved: nothing happens
    assert fake_flagsmith.calls == []
    r = flags.post(url, json={"change_id": s["change"]["id"], "confirm": True}).json()
    assert r["rollback"]["status"] == "watching" and fake_flagsmith.calls[-1] == ("set", 99, False)
    assert flags.post(url, json={"change_id": s["change"]["id"], "confirm": True}).json()["already"]  # no double rollback
    tl = flags.get("/api/flags/changes").json()
    assert tl[0]["source"] == "tracebase" and tl[0]["summary"].startswith("turned off")
    # Flagsmith's own webhook for that change is not shown twice
    now = datetime.now()
    body, h = signed(flag_webhook(True, False, now, by="tracebase-api-key"))
    assert flags.post("/api/flags/webhook", content=body, headers=h).json()["recorded"] is False
    assert verify() == []  # still inside the watch window
    _age_rollbacks(settings.flag_verify_minutes + 1)
    done = verify()
    assert done[0]["status"] == "worked"
    assert flags.get(f"/api/logs/groups/{g['code']}").json()["status"] == "resolved"
    assert any("stopped after turning off 'new_checkout'" in a["title"] for a in flags.get("/api/alerts").json())
    # rollbacks are Tracebase's fix, never a new suspect
    assert all(x["change"]["source"] != "tracebase" for x in flags.get(f"/api/logs/groups/{g['code']}/flags").json()["suspects"])


def test_rollback_that_did_not_help_says_so(flags, fake_flagsmith):
    from tracebase.flags.rollback import verify
    start = datetime.now().replace(microsecond=0) - timedelta(minutes=30)
    g, s = _suspect(flags, start)
    flags.post(f"/api/logs/groups/{g['code']}/rollback", json={"change_id": s["change"]["id"], "confirm": True})
    _age_rollbacks(settings.flag_verify_minutes + 1)
    _ingest(flags, checkout_errors(datetime.now().replace(microsecond=0) - timedelta(minutes=2), n=3))
    assert verify()[0]["status"] == "did_not_help"
    assert flags.get(f"/api/logs/groups/{g['code']}").json()["status"] != "resolved"


def test_only_admins_can_turn_flags_off(flags, fake_flagsmith, monkeypatch):
    start = datetime.now().replace(microsecond=0) - timedelta(minutes=30)
    g, s = _suspect(flags, start)
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    tok = flags.post("/api/team/users", json={"name": "ravi", "repos": ["shop-api"]},
                     headers={"Authorization": f"Bearer {ADMIN}"}).json()["token"]
    r = flags.post(f"/api/logs/groups/{g['code']}/rollback", json={"change_id": s["change"]["id"], "confirm": True},
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and fake_flagsmith.calls == []


def test_error_that_comes_back_after_a_rollback_blames_the_new_flag_change(flags, fake_flagsmith):
    """Second incident: the flag is turned on again after a rollback that worked. The new change is the suspect
    (timed from when the errors came back, not from the group's first error) and can be turned off again."""
    from tracebase.flags.rollback import verify
    start = datetime.now().replace(microsecond=0) - timedelta(minutes=40)
    g, s = _suspect(flags, start)
    flags.post(f"/api/logs/groups/{g['code']}/rollback", json={"change_id": s["change"]["id"], "confirm": True})
    _age_rollbacks(35)  # after the first burst ended (40 to 36 minutes ago)
    assert verify()[0]["status"] == "worked"
    flags.post(f"/api/logs/groups/{g['code']}/explain")  # cached while resolved
    # turned on again 26 minutes after the first errors stopped (less than the 30-minute quiet gap)
    again = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
    body, h = signed(flag_webhook(False, True, again, by="ravi@shop.test"))
    flags.post("/api/flags/webhook", content=body, headers=h)
    _ingest(flags, checkout_errors(again + timedelta(minutes=1), n=5))
    assert flags.get(f"/api/logs/groups/{g['code']}").json()["status"] == "regressed"
    info = flags.get(f"/api/logs/groups/{g['code']}/flags").json()
    top = info["suspects"][0]
    assert top["change"]["changed_by"] == "ravi@shop.test" and top["minutes_before_first_error"] == 1
    assert top["can_roll_back"] and not [r for r in info["rollbacks"] if r["change_id"] == top["change"]["id"]]
    r = flags.post(f"/api/logs/groups/{g['code']}/explain").json()  # not the explanation cached before it came back
    assert "by ravi@shop.test 1 minute before the errors started" in r["cause"]
    assert not flags.post(f"/api/logs/groups/{g['code']}/rollback",
                          json={"change_id": top["change"]["id"], "confirm": True}).json().get("already")
    # the second rollback works too: the panel keeps showing this incident, not the first one
    from tracebase.db import tx
    from tracebase.timeutil import iso, now
    with tx() as c:
        c.execute("UPDATE flag_rollbacks SET created_at=? WHERE status='watching'", (iso(now() - timedelta(minutes=6)),))
    assert verify()[0]["status"] == "worked"
    info = flags.get(f"/api/logs/groups/{g['code']}/flags").json()
    assert info["suspects"][0]["change"]["changed_by"] == "ravi@shop.test"
