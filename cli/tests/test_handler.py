"""TracebaseHandler: Python logging -> Tracebase, batched in a background thread."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tracebase_cli.handler import TracebaseHandler


@pytest.fixture()
def server():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append({"key": self.headers.get("X-Tracebase-Key"), "path": self.path,
                        "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got
    srv.shutdown()


def test_sends_errors_with_traceback_and_context(server):
    url, got = server
    h = TracebaseHandler(url=url, key="tbk_test", interval=60, release="2.0.1")
    log = logging.getLogger("billing.test")
    log.addHandler(h)
    log.info("not sent: below WARNING")
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("charge failed", extra={"order_id": 7})
    h.close()  # flushes
    log.removeHandler(h)
    assert len(got) == 1 and got[0]["key"] == "tbk_test" and got[0]["path"] == "/api/logs/ingest"
    [e] = got[0]["body"]["events"]
    assert e["level"] == "error" and e["message"] == "charge failed"
    assert e["stack"][0] == "context: order_id=7, logger=billing.test, release=2.0.1"
    assert any("ZeroDivisionError" in line for line in e["stack"]) and any('File "' in line for line in e["stack"])


def test_rejects_a_non_project_key():
    with pytest.raises(ValueError):
        TracebaseHandler(url="http://x", key="tb_admin_token")
