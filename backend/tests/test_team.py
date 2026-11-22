"""Team mode: tokens, per-member repo access on every endpoint, admin-only management, audit log."""
import pytest
from fastapi.testclient import TestClient

from tracebase.config import settings

ADMIN = "admin-secret-for-tests"


@pytest.fixture()
def api(indexed, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", ADMIN)
    monkeypatch.setattr(settings, "enable_scheduler", False)
    from tracebase.db import tx
    with tx() as c:
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM audit")
    from tracebase.main import app
    return TestClient(app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _member(api, name="priya", repos=("freelance/*",), role="member") -> str:
    r = api.post("/api/team/users", json={"name": name, "repos": list(repos), "role": role}, headers=_h(ADMIN))
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_token_required_except_health(api):
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/health").json()["team"] is True
    assert api.get("/api/code/repos").status_code == 401
    assert api.get("/api/code/repos", headers=_h("wrong")).status_code == 401
    assert api.get("/api/code/repos", headers=_h(ADMIN)).status_code == 200


def test_member_only_sees_their_repos_everywhere(api):
    t = _member(api)
    h = _h(t)
    repos = [r["name"] for r in api.get("/api/code/repos", headers=h).json()]
    assert repos and all(r.startswith("freelance/") for r in repos)
    res = api.get("/api/code/search", params={"q": "jwt refresh token"}, headers=h).json()["results"]
    assert res and all(r["repo"].startswith("freelance/") for r in res)
    grep = api.get("/api/code/grep", params={"pattern": "function"}, headers=h).json()["results"]
    assert grep and all(r["repo"].startswith("freelance/") for r in grep)
    ex = api.post("/api/reuse/examples", json={"task": "jwt refresh token"}, headers=h).json()["examples"]
    assert all(e["repo"].startswith("freelance/") for e in ex)
    secrets = api.get("/api/code/secrets", headers=h).json()["findings"]
    assert all(s["repo"].startswith("freelance/") for s in secrets)
    stats_member = api.get("/api/stats", headers=h).json()
    stats_admin = api.get("/api/stats", headers=_h(ADMIN)).json()
    assert 0 < stats_member["repos"] < stats_admin["repos"]
    # a repo outside their patterns does not exist for them
    assert api.get("/api/code/file", params={"repo": "side/expense-tracker", "path": "src/auth/tokens.ts"},
                   headers=h).status_code == 404
    assert api.post("/api/reuse/bundle", json={"repo": "side/expense-tracker", "paths": ["src/auth/tokens.ts"]},
                    headers=h).status_code == 404
    assert api.get("/api/code/tree", params={"repo": "shop-api"}, headers=h).status_code == 404
    assert api.post("/api/code/ask", json={"question": "x", "repo": "shop-api"}, headers=h).status_code == 404
    # …but their own repos work
    assert api.post("/api/reuse/bundle", json={"repo": "freelance/clinic-app",
                                               "paths": ["app/Http/Middleware/EnsureUserHasRole.php"]},
                    headers=h).status_code == 200


def test_only_admins_manage_members(api):
    t = _member(api)
    assert api.get("/api/team/users", headers=_h(t)).status_code == 403
    assert api.post("/api/team/users", json={"name": "x"}, headers=_h(t)).status_code == 403
    assert api.post("/api/code/reindex", headers=_h(t)).status_code == 403
    me = api.get("/api/team/me", headers=_h(t)).json()
    assert me["name"] == "priya" and me["repos"] == ["freelance/*"]
    users = api.get("/api/team/users", headers=_h(ADMIN)).json()
    assert [u["name"] for u in users] == ["priya"] and "token" not in users[0] and "token_hash" not in users[0]
    assert api.post("/api/team/users", json={"name": "priya"}, headers=_h(ADMIN)).status_code == 409


def test_rotate_update_and_remove(api):
    old = _member(api)
    new = api.post("/api/team/users/priya/rotate", headers=_h(ADMIN)).json()["token"]
    assert api.get("/api/code/repos", headers=_h(old)).status_code == 401
    assert api.get("/api/code/repos", headers=_h(new)).status_code == 200
    api.put("/api/team/users/priya/repos", json=["shop-api"], headers=_h(ADMIN))
    assert [r["name"] for r in api.get("/api/code/repos", headers=_h(new)).json()] == ["shop-api"]
    assert api.delete("/api/team/users/priya", headers=_h(ADMIN)).status_code == 200
    assert api.get("/api/code/repos", headers=_h(new)).status_code == 401


def test_audit_log_records_who_did_what(api):
    t = _member(api)
    api.post("/api/reuse/examples", json={"task": "login"}, headers=_h(t))
    api.post("/api/reuse/bundle", json={"repo": "freelance/clinic-app", "paths": ["app/Models/User.php"]}, headers=_h(t))
    api.post("/api/team/events", json={"action": "copied", "repo": "freelance/clinic-app",
                                       "detail": {"files": ["app/Models/User.php"], "project": "booking-api"}}, headers=_h(t))
    log = api.get("/api/team/audit", params={"user": "priya"}, headers=_h(ADMIN)).json()
    assert [e["action"] for e in log] == ["copied", "bundle", "find"]
    assert log[0]["detail"]["project"] == "booking-api" and log[1]["repo"] == "freelance/clinic-app"
    assert api.post("/api/team/events", json={"action": "copied", "repo": "shop-api"}, headers=_h(t)).status_code == 404


def test_solo_mode_needs_no_token(indexed, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    from tracebase.main import app
    c = TestClient(app)
    assert c.get("/api/code/repos").status_code == 200
    assert c.get("/api/team/me").json()["name"] == "local"
