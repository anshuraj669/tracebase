"""Connected projects (write-only ingest keys) and "Continue with Google" sign-in."""
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tracebase.config import settings

ADMIN = "admin-secret-for-tests"
CLIENT_ID = "123-test.apps.googleusercontent.com"


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def api(indexed, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    monkeypatch.setattr(settings, "enable_scheduler", False)
    from tracebase.db import tx
    with tx() as c:
        for t in ("sessions", "users", "projects", "log_events", "error_groups", "alerts"):
            c.execute(f"DELETE FROM {t}")
    from tracebase import projects
    projects._window.clear()
    from tracebase.main import app
    return TestClient(app)


def _project(api, name="meetsnap-web", platform="browser"):
    r = api.post("/api/projects", json={"name": name, "platform": platform}, headers=_h(ADMIN))
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ projects
def test_project_key_sends_only_its_own_logs_and_reads_nothing(api):
    p = _project(api)
    key = p["key"]
    assert key.startswith("tbk_") and p["key_hint"] == key[:10]
    events = {"events": [{"level": "error", "service": "payments", "message": "TypeError: x is undefined",
                          "stack": ["at saveLead (https://app.example.com/src/main.ts?t=1:262:9)"]}]}
    r = api.post("/api/logs/ingest", json=events, headers={"X-Tracebase-Key": key})
    assert r.status_code == 200 and r.json()["new_groups"]
    assert r.headers["access-control-allow-origin"] == "*"
    # stored under the project, whatever service the event claimed
    groups = api.get("/api/logs/groups", headers=_h(ADMIN)).json()
    assert {g["service"] for g in groups} == {"meetsnap-web"}
    assert groups[0]["frames"][0]["path"] == "src/main.ts"
    # the same key as a Bearer token works too, but only for ingest
    assert api.post("/api/logs/ingest", content=b"2026-11-28 10:00:00 [ERROR] boom",
                    headers={**_h(key), "content-type": "text/plain"}).status_code == 200
    for path in ("/api/logs/groups", "/api/projects", "/api/team/me", "/api/stats"):
        assert api.get(path, headers=_h(key)).status_code == 403
    assert api.post("/api/logs/ingest", json=events, headers={"X-Tracebase-Key": "tbk_wrong"}).status_code == 401
    listed = api.get("/api/projects", headers=_h(ADMIN)).json()
    assert listed[0]["events"] == 2 and listed[0]["open_errors"] == 2 and "key" not in listed[0]


def test_browser_preflight_is_allowed_for_ingest_only(api):
    r = api.options("/api/logs/ingest", headers={"Origin": "https://shop.example.com",
                                                 "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type,x-tracebase-key"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
    assert "X-Tracebase-Key" in r.headers["access-control-allow-headers"]
    r = api.get("/api/logs/groups", headers={**_h(ADMIN), "Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_rotate_delete_and_rate_limit(api, monkeypatch):
    p = _project(api, "billing-api", "python")
    new = api.post("/api/projects/billing-api/rotate", headers=_h(ADMIN)).json()["key"]
    one = {"events": [{"level": "error", "message": "boom"}]}
    assert api.post("/api/logs/ingest", json=one, headers={"X-Tracebase-Key": p["key"]}).status_code == 401
    assert api.post("/api/logs/ingest", json=one, headers={"X-Tracebase-Key": new}).status_code == 200
    from tracebase import projects
    monkeypatch.setattr(projects, "EVENTS_PER_MINUTE", 3)
    many = {"events": [{"level": "info", "message": f"line {i}"} for i in range(3)]}
    assert api.post("/api/logs/ingest", json=many, headers={"X-Tracebase-Key": new}).status_code == 429
    assert api.post("/api/projects", json={"name": "Bad Name!", "platform": "node"}, headers=_h(ADMIN)).status_code == 400
    assert api.post("/api/projects", json={"name": "billing-api"}, headers=_h(ADMIN)).status_code == 409
    r = api.delete("/api/projects/billing-api", params={"delete_data": "true"}, headers=_h(ADMIN))
    assert r.status_code == 200
    assert api.post("/api/logs/ingest", json=one, headers={"X-Tracebase-Key": new}).status_code == 401
    assert not [g for g in api.get("/api/logs/groups", headers=_h(ADMIN)).json() if g["service"] == "billing-api"]


def test_members_cannot_manage_projects_but_see_their_own(api):
    _project(api, "meetsnap-web")
    _project(api, "billing-api")
    tok = api.post("/api/team/users", json={"name": "ravi", "repos": ["meetsnap-web"]}, headers=_h(ADMIN)).json()["token"]
    assert [p["name"] for p in api.get("/api/projects", headers=_h(tok)).json()] == ["meetsnap-web"]
    assert api.post("/api/projects", json={"name": "x-app"}, headers=_h(tok)).status_code == 403
    assert api.post("/api/projects/meetsnap-web/rotate", headers=_h(tok)).status_code == 403
    assert api.get("/api/projects/billing-api", headers=_h(tok)).status_code == 404


def test_sdk_is_served_to_other_sites(api):
    r = api.get("/sdk/tracebase.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["access-control-allow-origin"] == "*" and "captureException" in r.text


# ------------------------------------------------------------------ Continue with Google
@pytest.fixture()
def google(api, monkeypatch):
    """A fake Google: tokens signed with a test key that the verifier is told to trust."""
    keypair = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from tracebase import google_auth
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(google_auth, "_keys",
                        lambda: SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=keypair.public_key())))

    def token(email, verified=True, aud=CLIENT_ID, iss="https://accounts.google.com", ttl=600):
        t = int(time.time())
        return jwt.encode({"iss": iss, "aud": aud, "sub": "1", "email": email, "email_verified": verified,
                           "iat": t, "exp": t + ttl, "name": "Test"}, keypair, algorithm="RS256", headers={"kid": "k"})
    return token


def test_google_sign_in_for_invited_members_only(api, google, monkeypatch):
    assert api.get("/api/auth/config").json() == {"team": True, "google_client_id": CLIENT_ID}
    # not invited: refused, with what to do
    r = api.post("/api/auth/google", json={"credential": google("stranger@example.com")})
    assert r.status_code == 403 and "invite" in r.json()["detail"]
    # invited by email: a session token that works like their member token
    api.post("/api/team/users", json={"name": "priya", "repos": ["meetsnap-web"], "email": "Priya@Example.com"},
             headers=_h(ADMIN))
    r = api.post("/api/auth/google", json={"credential": google("priya@example.com")}).json()
    assert r["name"] == "priya" and r["role"] == "member" and r["token"].startswith("tbs_")
    me = api.get("/api/team/me", headers=_h(r["token"])).json()
    assert me["name"] == "priya" and me["repos"] == ["meetsnap-web"]
    # sign out ends the session
    api.delete("/api/auth/session", headers=_h(r["token"]))
    assert api.get("/api/team/me", headers=_h(r["token"])).status_code == 401
    # an admin email becomes an admin on first sign-in
    monkeypatch.setattr(settings, "admin_emails", "anshu@example.com")
    r = api.post("/api/auth/google", json={"credential": google("anshu@example.com")}).json()
    assert r["role"] == "admin" and r["name"] == "anshu"
    assert api.get("/api/team/users", headers=_h(r["token"])).status_code == 200


def test_google_tokens_are_checked(api, google):
    api.post("/api/team/users", json={"name": "priya", "email": "priya@example.com"}, headers=_h(ADMIN))
    for bad in (google("priya@example.com", verified=False), google("priya@example.com", aud="someone-else"),
                google("priya@example.com", iss="https://evil.example.com"), google("priya@example.com", ttl=-120),
                "not-a-jwt"):
        assert api.post("/api/auth/google", json={"credential": bad}).status_code == 401
    # a removed member's sessions stop working
    tok = api.post("/api/auth/google", json={"credential": google("priya@example.com")}).json()["token"]
    api.delete("/api/team/users/priya", headers=_h(ADMIN))
    assert api.get("/api/team/me", headers=_h(tok)).status_code == 401


def test_google_is_off_without_a_client_id(api):
    assert api.get("/api/auth/config").json()["google_client_id"] is None
    assert api.post("/api/auth/google", json={"credential": "x"}).status_code == 404
