"""`tracebase connect <agent>`: register Tracebase's MCP server with the AI agent the developer uses in their
terminal or editor. Config files are merged (other servers are kept) and backed up first."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "tracebase"


def command() -> list[str]:
    """How the agent should start the server: the installed `tracebase` (absolute path), else this Python."""
    exe = shutil.which("tracebase")
    return [exe, "mcp"] if exe else [sys.executable, "-m", "tracebase_cli.main", "mcp"]


def _env() -> dict:
    url = os.environ.get("TRACEBASE_URL")
    return {"TRACEBASE_URL": url} if url else {}


def _backup(p: Path) -> None:
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}"))


def _merge_json(path: Path, key_path: list[str], entry: dict) -> None:
    data = json.loads(path.read_text() or "{}") if path.exists() else {}
    node = data
    for k in key_path:
        node = node.setdefault(k, {})
    node[NAME] = entry
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _std_entry() -> dict:
    cmd = command()
    e = {"command": cmd[0], "args": cmd[1:]}
    if _env():
        e["env"] = _env()
    return e


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return f"ran: {' '.join(cmd)}"


# ------------------------------------------------------------------ agents
def claude(home: Path) -> str:
    """Claude Code: `claude mcp add --scope user` (available in every project)."""
    cmd = command()
    if shutil.which("claude"):
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", NAME], capture_output=True)
        env = [a for k, v in _env().items() for a in ("-e", f"{k}={v}")]
        return _run(["claude", "mcp", "add", "--scope", "user", *env, NAME, "--", *cmd])
    _merge_json(home / ".claude.json", ["mcpServers"], {"type": "stdio", **_std_entry()})
    return f"wrote {home / '.claude.json'}"


def codex(home: Path) -> str:
    """OpenAI Codex CLI: [mcp_servers.tracebase] in ~/.codex/config.toml."""
    path = home / ".codex" / "config.toml"
    cmd = command()
    block = f'[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd[0])}\nargs = {json.dumps(cmd[1:])}\n'
    if _env():
        block += "env = { " + ", ".join(f'{k} = {json.dumps(v)}' for k, v in _env().items()) + " }\n"
    text = path.read_text() if path.exists() else ""
    # replace an existing [mcp_servers.tracebase] table (up to the next table header)
    text = re.sub(rf"(?ms)^\[mcp_servers\.{NAME}\]\n.*?(?=^\[|\Z)", "", text).rstrip()
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text + "\n\n" if text else "") + block)
    return f"wrote {path}"


def gemini(home: Path) -> str:
    """Google Gemini CLI: mcpServers in ~/.gemini/settings.json."""
    path = home / ".gemini" / "settings.json"
    _merge_json(path, ["mcpServers"], _std_entry())
    return f"wrote {path}"


def opencode(home: Path) -> str:
    """opencode (works with Ollama and cloud models): mcp in ~/.config/opencode/opencode.json."""
    path = home / ".config" / "opencode" / "opencode.json"
    entry = {"type": "local", "command": command(), "enabled": True}
    if _env():
        entry["environment"] = _env()
    _merge_json(path, ["mcp"], entry)
    return f"wrote {path}"


def cursor(home: Path) -> str:
    """Cursor (editor + cursor-agent CLI): mcpServers in ~/.cursor/mcp.json."""
    path = home / ".cursor" / "mcp.json"
    _merge_json(path, ["mcpServers"], _std_entry())
    return f"wrote {path}"


def ollama(home: Path) -> str:
    """Ollama models in the terminal through ollmcp (pip install mcp-client-for-ollama)."""
    path = home / ".config" / "tracebase" / "mcp-servers.json"
    _merge_json(path, ["mcpServers"], _std_entry())
    return (f"wrote {path}\n  start it in your project with:  ollmcp --servers-json {path} --model qwen2.5-coder:7b\n"
            "  (any Ollama model with tool calling works: qwen2.5-coder, llama3.1, mistral-nemo…)")


AGENTS = {
    "claude": (claude, "Claude Code", "claude"),
    "codex": (codex, "OpenAI Codex CLI", "codex"),
    "gemini": (gemini, "Gemini CLI", "gemini"),
    "opencode": (opencode, "opencode (Ollama or cloud models)", "opencode"),
    "cursor": (cursor, "Cursor / cursor-agent", "cursor-agent"),
    "ollama": (ollama, "Ollama via ollmcp", "ollmcp"),
}


def detected() -> list[str]:
    return [k for k, (_, _, exe) in AGENTS.items() if shutil.which(exe)]


def snippet() -> str:
    """Generic config for any other MCP client."""
    return json.dumps({"mcpServers": {NAME: _std_entry()}}, indent=2)
