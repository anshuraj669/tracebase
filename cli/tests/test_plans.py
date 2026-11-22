import asyncio
import json
from pathlib import Path

import pytest

from tracebase_cli import connect, mcp_server, plans, reuse

BUNDLE = {
    "repo": "old/app",
    "files": [{"path": "src/auth.py", "target": "src/auth.py", "role": "picked", "reason": "picked",
               "content": "def login():\n    return 'ok'\n"}],
    "packages": [], "env": [],
}


def _plan(root: Path) -> dict:
    (root / "src").mkdir(parents=True, exist_ok=True)
    return plans.save(root, BUNDLE, reuse.plan_changes(root, BUNDLE), "login")


def test_plan_is_saved_not_written_then_applied(tmp_path):
    plan = _plan(tmp_path)
    assert not (tmp_path / "src/auth.py").exists()
    assert [p["id"] for p in plans.pending(tmp_path)] == [plan["id"]]
    assert (tmp_path / ".tracebase/.gitignore").read_text() == "*\n"
    assert "create src/auth.py" in plans.summary(plan)
    assert plans.apply(tmp_path, plans.load(tmp_path, plan["id"])) == [("created", "src/auth.py")]
    assert (tmp_path / "src/auth.py").read_text() == "def login():\n    return 'ok'\n"
    assert plans.pending(tmp_path) == []


def test_stale_plan_is_refused(tmp_path):
    plan = _plan(tmp_path)
    (tmp_path / "src/auth.py").write_text("# written by hand meanwhile\n")
    with pytest.raises(reuse.CopyError, match="changed after the preview"):
        plans.apply(tmp_path, plan)
    assert (tmp_path / "src/auth.py").read_text() == "# written by hand meanwhile\n"


def test_bad_plan_id(tmp_path):
    with pytest.raises(reuse.CopyError):
        plans.load(tmp_path, "../../etc")


class FakeCtx:
    """Stands in for the MCP request context of an agent that supports (or not) asking the user."""
    def __init__(self, answer):
        self.answer, self.asked = answer, []
        caps = type("Caps", (), {"elicitation": object() if answer else None})()
        self.request_context = type("RC", (), {"session": type("S", (), {"client_capabilities": caps})()})()

    async def elicit(self, message, schema):
        self.asked.append(message)
        return type("R", (), {"action": self.answer, "data": schema(write=True)})()


def _apply_tool(root):
    server = mcp_server.build_server(api=None, workspace=root)
    tools = getattr(server, "_tool_manager", None) or getattr(server, "tool_manager", None)
    return tools.get_tool("apply_copy").fn


@pytest.mark.parametrize("answer,written", [("accept", True), ("decline", False)])
def test_apply_copy_asks_the_user(tmp_path, answer, written):
    plan = _plan(tmp_path)
    ctx = FakeCtx(answer)
    out = asyncio.run(_apply_tool(tmp_path)(plan_id=plan["id"], ctx=ctx))
    assert ctx.asked and "src/auth.py" in ctx.asked[0]
    assert (tmp_path / "src/auth.py").exists() is written
    assert ("declined" in out) is not written


def test_apply_copy_without_elicitation_needs_explicit_confirmation(tmp_path):
    plan = _plan(tmp_path)
    fn = _apply_tool(tmp_path)
    out = asyncio.run(fn(plan_id=plan["id"], ctx=FakeCtx(None)))
    assert "not approved" in out and not (tmp_path / "src/auth.py").exists()
    asyncio.run(fn(plan_id=plan["id"], user_confirmed=True, ctx=FakeCtx(None)))
    assert (tmp_path / "src/auth.py").exists()


def test_connect_merges_configs(tmp_path, monkeypatch):
    monkeypatch.setattr(connect, "command", lambda: ["/usr/bin/tracebase", "mcp"])
    gem = tmp_path / ".gemini/settings.json"
    gem.parent.mkdir()
    gem.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    connect.gemini(tmp_path)
    data = json.loads(gem.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    assert data["mcpServers"]["tracebase"] == {"command": "/usr/bin/tracebase", "args": ["mcp"]}
    assert list(tmp_path.glob(".gemini/settings.json.bak-*"))

    toml = tmp_path / ".codex/config.toml"
    toml.parent.mkdir()
    toml.write_text('model = "o3"\n\n[mcp_servers.tracebase]\ncommand = "old"\n\n[mcp_servers.other]\ncommand = "y"\n')
    connect.codex(tmp_path)
    text = toml.read_text()
    assert text.count("[mcp_servers.tracebase]") == 1 and '"/usr/bin/tracebase"' in text and '"old"' not in text
    assert "[mcp_servers.other]" in text and 'model = "o3"' in text

    connect.opencode(tmp_path)
    oc = json.loads((tmp_path / ".config/opencode/opencode.json").read_text())
    assert oc["mcp"]["tracebase"]["command"] == ["/usr/bin/tracebase", "mcp"]
    assert "ollmcp --servers-json" in connect.ollama(tmp_path)


def test_packages_go_into_the_manifest_and_env_into_env_example(tmp_path):
    (tmp_path / "package.json").write_text('{\n    "name": "notes",\n    "dependencies": {"express": "^4"}\n}\n')
    (tmp_path / ".env.example").write_text("PORT=3000\n")
    bundle = {"repo": "side/old", "files": [{"path": "src/t.ts", "target": "src/t.ts", "role": "picked", "reason": "picked",
                                             "content": "import jwt from 'jsonwebtoken'\nconst s = process.env.JWT_SECRET\n"}],
              "packages": [{"manager": "package.json", "name": "jsonwebtoken", "version": "^9.0.0", "module": "jsonwebtoken",
                            "install": "npm install jsonwebtoken"}],
              "env": ["JWT_SECRET"]}
    changes = {c.target: c for c in reuse.plan_changes(tmp_path, bundle)}
    deps, env = changes["package.json"], changes[".env.example"]
    assert deps.kind == "deps" and '"jsonwebtoken": "^9.0.0"' in deps.new_text and '"express": "^4"' in deps.new_text
    assert '\n    "name"' in deps.new_text                    # keeps the file's indentation
    assert env.new_text.startswith("PORT=3000\n\n") and "JWT_SECRET=\n" in env.new_text
    plan = plans.save(tmp_path, bundle, list(changes.values()))
    assert plans.install_steps(plan["packages"]) == ["run `npm install` (jsonwebtoken added to package.json)"]


def test_no_manifest_means_install_hint_only(tmp_path):
    bundle = {"repo": "old", "files": [{"path": "a.py", "target": "a.py", "role": "picked", "reason": "p",
                                        "content": "import jwt\n"}],
              "packages": [{"manager": "python", "name": "PyJWT", "version": "PyJWT==2.8.0", "module": "jwt",
                            "install": "pip install PyJWT"}], "env": []}
    changes = reuse.plan_changes(tmp_path, bundle)
    assert [c.kind for c in changes] == ["create"]
    assert plans.install_steps(bundle["packages"]) == ["install PyJWT: pip install PyJWT"]
