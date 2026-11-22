"""`tracebase data`: the right API calls, confirmation before deleting, private export files."""
import json
import os

import httpx

from tracebase_cli import main
from tracebase_cli.client import Client


def _client(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params), req.content.decode() or None))
        if req.url.path == "/api/data/export":
            lines = [{"type": "export"}, {"type": "log_event", "message": "a"}, {"type": "error_group"}]
            return httpx.Response(200, text="".join(json.dumps(l) + "\n" for l in lines))
        if req.url.path == "/api/data/purge":
            body = json.loads(req.content)
            key = "would_delete" if body["dry_run"] else "deleted"
            return httpx.Response(200, json={key: {"log_events": 3, "error_groups": 1, "alerts": 0}})
        return httpx.Response(200, json={"deleted": {"chunks": 4}, "repo": "x"})

    api = Client("http://tb.test")
    api.http = httpx.Client(base_url="http://tb.test", transport=httpx.MockTransport(handler))
    return api


def _run(argv, api, monkeypatch):
    monkeypatch.setattr(main, "Client", lambda url=None, **kw: api)
    return main.main(argv)


def test_purge_previews_then_deletes(monkeypatch, capsys):
    calls = []
    api = _client(calls)
    assert _run(["data", "purge", "CUST-10442", "--yes"], api, monkeypatch) == 0
    bodies = [json.loads(c[3]) for c in calls if c[1] == "/api/data/purge"]
    assert [b["dry_run"] for b in bodies] == [True, False]
    assert "Deleted 3 log events, 1 error groups" in capsys.readouterr().out


def test_deletes_need_confirmation(monkeypatch, capsys):
    calls = []
    api = _client(calls)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _run(["data", "forget-repo", "client-a/old"], api, monkeypatch) == 1
    assert _run(["data", "wipe"], api, monkeypatch) == 1
    assert not any(c[0] == "DELETE" or c[1] == "/api/data/wipe" for c in calls)
    assert _run(["data", "forget-service", "payments", "--stop", "-y"], api, monkeypatch) == 0
    assert ("DELETE", "/api/data/services/payments", {"exclude": "true"}, None) in calls


def test_export_writes_an_owner_only_file(monkeypatch, tmp_path):
    calls = []
    out = tmp_path / "x.ndjson"
    assert _run(["data", "export", "--service", "shop-api", "-o", str(out)], _client(calls), monkeypatch) == 0
    assert calls[0][2] == {"service": "shop-api"}
    assert len(out.read_text().splitlines()) == 3
    assert os.stat(out).st_mode & 0o077 == 0
