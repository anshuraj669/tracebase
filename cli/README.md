# tracebase-cli

<!-- mcp-name: io.github.anshuraj669/tracebase -->

Command line and **MCP server** for [Tracebase](https://github.com/anshuraj669/tracebase), the self-hosted search
and Q&A over all your past projects and production logs.

It lets you, and the AI agent you already use (Claude Code, Codex, Gemini CLI, opencode, Cursor, an Ollama model),
**find code you already wrote in your old projects and copy it into the current one**, and **explain production
errors** with the failing code and the commit that changed it. Nothing is written until you approve it.

The CLI talks to a running Tracebase server (your own, or the public demo).

## Install

```bash
pipx install tracebase-cli        # or: uv tool install tracebase-cli
```

## Try it against the public demo

```bash
tracebase login tb_escsEwbpgDuxtXQ9xIxKT30IyXzO3_Mp --url https://demo.tracebase.duckdns.org
tracebase find "send password reset email"
tracebase errors
tracebase why E-1
```

## Connect your AI agent (MCP)

```bash
tracebase connect claude          # or: codex, gemini, opencode, cursor, ollama
```

Then ask your agent things like *"copy the role based access control from my old projects"* or *"why did
checkout fail last night?"*. The MCP server exposes these tools:

| Tool | What it does |
|---|---|
| `find_code` | your past implementations of something (repo, path, symbol) |
| `preview_copy` | what would be copied into this project, with its models, migrations and wiring, as a diff; nothing is written |
| `apply_copy` | writes an approved copy; asks you directly when the agent supports MCP elicitation |
| `search_code`, `read_code`, `list_repos`, `get_code_bundle` | search and read your indexed repos |
| `find_errors`, `explain_error` | grouped production errors; cause, failing code, the commit, a suggested fix |

You can also run it by hand: `tracebase mcp` (stdio).

## Commands

```
tracebase status                 check the connection to the server
tracebase find "login logic"     your past implementations, with code
tracebase copy "login logic"     find it and copy it into this project (shows the plan first)
tracebase search "jwt refresh"   search across all your repos
tracebase errors / why E-5       production errors, grouped and explained
tracebase ingest app.log         send log files (or stdin: -)
tracebase team add priya --repos "freelance/*"     team server admin
```

`tracebase --help` lists everything.

## Run your own server

See [the main project](https://github.com/anshuraj669/tracebase): Docker, a NAS, or one small cloud server with
automatic HTTPS.

## License

[FSL-1.1-ALv2](LICENSE.md): free to use, self-host and modify, except to build a competing product. Each
release becomes Apache 2.0 two years after it's published.
