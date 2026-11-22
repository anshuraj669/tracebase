"""MCP tools for Log Detective: the agent can list production errors and get the full explanation."""
from tracebase_cli import mcp_server

GROUP = {"id": 5, "code": "E-5", "service": "shop-api", "title": 'Undefined array key "coupon"', "status": "new",
         "count": 64, "window_count": 64, "spike": True, "first_seen": "2026-11-08T14:04:04", "last_seen": "2026-11-08T14:53:02",
         "samples": [{"message": 'production.ERROR: Undefined array key "coupon"'}]}
EXPLAIN = {"cause": "Code reads a key that is missing. Last changed in commit f971906.", "impact": "64 occurrences.",
           "fix": ["In shop-api/app/Services/CartService.php:57 change `$code = $cart['coupon'];` to `$code = $cart['coupon'] ?? null;`"],
           "events": [{"ts": "2026-11-08T13:42:10", "service": "deploy", "message": "deployed shop-api"}],
           "code": [{"repo": "shop-api", "path": "app/Services/CartService.php", "line": 57, "func": None,
                     "snippet": [{"n": 56, "text": "$total = $cart['total'];"}, {"n": 57, "text": "$code  = $cart['coupon'];"}]}],
           "commit": {"sha": "f971906", "message": "Refactor applyDiscount", "author": "Dev", "date": "2026-11-08 13:42",
                      "minutes_before_first_error": 22}}


class FakeApi:
    def __init__(self):
        self.calls = []

    def errors(self, **kw):
        self.calls.append(("errors", kw))
        return [GROUP]

    def error(self, gid):
        self.calls.append(("error", gid))
        return GROUP

    def explain_error(self, gid, refresh=False):
        self.calls.append(("explain", gid))
        return EXPLAIN


def _tool(api, name):
    server = mcp_server.build_server(api=api)
    tools = getattr(server, "_tool_manager", None) or getattr(server, "tool_manager", None)
    return tools.get_tool(name).fn


def test_find_errors_lists_groups():
    api = FakeApi()
    out = _tool(api, "find_errors")(service="shop-api", since="last night")
    assert "E-5" in out and "SPIKING" in out and "count=64" in out
    assert api.calls[0] == ("errors", {"service": "shop-api", "since": "last night", "status": None, "q": None, "limit": 15})


def test_explain_error_gives_cause_code_commit_and_fix():
    api = FakeApi()
    out = _tool(api, "explain_error")(error_id="5")
    assert ("explain", "E-5") in api.calls  # "5" is normalised to "E-5"
    assert ">>   57 $code  = $cart['coupon'];" in out
    assert "commit f971906" in out and "22 min before the first error" in out
    assert "?? null" in out and "deploy: deployed shop-api" in out
