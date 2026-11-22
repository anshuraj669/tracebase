"""MCP server (stdio): lets the developer's AI agent (Claude Code, Codex, Gemini CLI, opencode, Cursor, an Ollama
model through ollmcp, …) find their own code in their old projects and copy it into the current project.

Flow: find_code -> preview_copy (diff, nothing written) -> the USER approves -> apply_copy writes it.
apply_copy asks the user directly through MCP elicitation when the agent supports it, so the agent cannot approve
on the user's behalf. Tracebase itself uses no AI and makes no internet requests.
Started by the agent with `tracebase mcp` (see `tracebase connect`)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

try:  # mcp >= 2
    from mcp.server.mcpserver import Context, MCPServer as FastMCP
    from mcp_types import ToolAnnotations
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.types import ToolAnnotations

from . import plans, project, reuse
from .client import Client, TracebaseError
from .ui import diff

INSTRUCTIONS = """Tracebase indexes ALL of the developer's past projects on their own machine.
When the developer asks to reuse something they built before ("copy the login logic from my old projects",
"implement role based access control like I did before"), DO NOT search the web and DO NOT write it from scratch:
1. call find_code to see their past implementations (repo, path, symbol),
2. call preview_copy for the one that fits: it shows exactly what would be copied into this project
   (the code plus what it depends on: models, migrations, wiring) as a diff. Nothing is written yet.
3. show the user that summary and ASK whether to implement it here,
4. only if the user says yes, call apply_copy with the plan_id. Tracebase may ask the user to confirm again.
Mention the source repo/path of anything you reuse, and list the "still to do" steps after applying.

Tracebase also collects the developer's production logs (Log Detective). When they ask what broke, why something
is failing, or about an error ("why did checkout fail last night?", "what's E-5?"):
1. call find_errors (optionally with service / since / query) to see the error groups,
2. call explain_error for the relevant one: the likely cause, the failing code from their repos, the commit that
   last changed that line, what happened in other services at the same time, and a suggested fix,
3. explain it to them. If they want it fixed, edit the code in the current project as usual (show the change first).
Never paste their logs into a web search."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


class Approval(BaseModel):
    write: bool = Field(default=True, description="Write these changes into the project")


def _fmt_examples(res: dict) -> str:
    if not res.get("examples"):
        return "No related code found in the developer's indexed projects."
    out = [f"Current project: {res.get('stack_text') or 'unknown'}  ·  searched for: {res.get('query')}", ""]
    for e in res["examples"]:
        fw = f", {e['framework']}" if e.get("framework") else ""
        out.append(f"[{e['id']}] repo={e['repo']} path={e['path']} symbol={e['symbol']} "
                   f"lines {e['lines'][0]}-{e['lines'][1]} ({e['language']}{fw})")
        out.append(f"```{e['language'] or ''}\n{e['code']}\n```")
    return "\n".join(out)


def can_elicit(ctx: Context | None) -> bool:
    """Does the connected agent support asking the user directly (MCP elicitation)?"""
    try:
        s = ctx.request_context.session
        caps = getattr(s, "client_capabilities", None) or getattr(getattr(s, "client_params", None), "capabilities", None)
        return bool(caps and getattr(caps, "elicitation", None) is not None)
    except Exception:
        return False


def _applied(workspace: Path, plan: dict, done: list[tuple[str, str]]) -> str:
    lines = [f"Copied from {plan['repo']} into {workspace.name}:"]
    lines += [f"- {note} {target}" for note, target in done]
    if any(n == "updated" for n, _ in done):
        lines.append("Originals of updated files are backed up in .tracebase/backups/.")
    todo = plans.todo_text(plan)
    if todo:
        lines.append("\nStill to do (tell the user, or do it if they ask):")
        lines += [f"- {t}" for t in todo]
    return "\n".join(lines)


def build_server(api: Client, workspace: Path | None = None) -> FastMCP:
    mcp = FastMCP("tracebase", instructions=INSTRUCTIONS)
    workspace = (workspace or Path(os.environ.get("TRACEBASE_WORKSPACE") or os.getcwd())).resolve()

    def ctx(task: str = "", n: int = 2) -> dict | None:
        return project.context(workspace, task, max_files=n, budget=8000) if workspace.is_dir() else None

    def manifest_ctx() -> dict | None:
        c = ctx()
        return {"name": c["name"], "tree": c["tree"], "files": {k: v for k, v in c["files"].items() if "/" not in k}} if c else None

    @mcp.tool(annotations=READ_ONLY)
    def find_code(task: str, languages: list[str] | None = None, frameworks: list[str] | None = None,
                  limit: int = 6) -> str:
        """Find the developer's own past implementations of something (e.g. "login", "role based access
        control", "stripe webhook") across all their old projects. Prefers the current project's language and
        framework. Returns code with repo and path, to pass to preview_copy."""
        try:
            c = None if (languages or frameworks) else ctx(task)
            return _fmt_examples(api.examples(task, project=c, languages=languages, frameworks=frameworks, limit=limit))
        except TracebaseError as e:
            return f"Tracebase error: {e}"

    @mcp.tool(annotations=READ_ONLY)
    def preview_copy(repo: str, path: str, symbol: str | None = None, renames: list[str] | None = None,
                     request: str = "") -> str:
        """Prepare copying a file (or one function/class of it, via `symbol`) from an old project into the
        current project, plus what it needs from that project (models, methods it calls, migrations, wiring
        such as route/middleware registration). Writes NOTHING: returns a plan_id, a summary and the diff.
        Show the summary to the user and ask whether to implement it; call apply_copy only if they agree.
        The old project's name is replaced by this project's automatically; `renames` adapts domain words in
        every spelling and in file names, e.g. ["Doctor=Staff", "appointment=booking"]."""
        try:
            b = api.bundle(repo, [path], manifest_ctx(), symbol=symbol, renames=renames)
            changes = reuse.plan_changes(workspace, b)
        except (TracebaseError, reuse.CopyError) as e:
            return f"Tracebase error: {e}"
        todo = [c for c in changes if c.new_text is not None]
        if not todo and not any(c.manual for c in changes):
            return f"Nothing to copy: everything from {b['repo']}/{path} is already in {workspace.name}."
        plan = plans.save(workspace, b, changes, request)
        out = [f"plan_id: {plan['id']}  (nothing written yet)", "", plans.summary(plan)]
        for c in todo:
            out.append(f"\n{diff(c.target, c.old_text, c.new_text, max_lines=160)}")
        out.append(f"\nAsk the user whether to implement this in {workspace.name}. If yes: apply_copy(plan_id=\"{plan['id']}\"). "
                   f"They can also review it themselves with `tracebase apply {plan['id']}` in a terminal.")
        return "\n".join(out)

    @mcp.tool(annotations=WRITES)
    async def apply_copy(plan_id: str, user_confirmed: bool = False, ctx: Context | None = None) -> str:
        """Write a plan made by preview_copy into the project, after the user approved it. Asks the user to
        confirm through the agent's approval prompt when the agent supports it. Set user_confirmed=true ONLY
        if the user explicitly said yes to this plan in the conversation. Originals are backed up to
        .tracebase/backups/; a plan whose files changed since the preview is refused."""
        try:
            plan = plans.load(workspace, plan_id)
            bad = plans.stale(workspace, plan)
            if bad:
                return "Tracebase error: these files changed after the preview, call preview_copy again: " + ", ".join(bad)
            if can_elicit(ctx):
                res = await ctx.elicit(f"Tracebase: write this into {workspace.name}?\n\n{plans.summary(plan)}", Approval)
                if res.action != "accept" or not getattr(res.data, "write", False):
                    return (f"The user declined. Nothing was written. The plan is kept: they can still apply it "
                            f"with `tracebase apply {plan_id}`.")
            elif not user_confirmed:
                return ("Not written: the user has not approved this yet. Show them the preview summary and ask. "
                        "If they say yes, call apply_copy again with user_confirmed=true. They can also run "
                        f"`tracebase apply {plan_id}` in a terminal to review and write it themselves.")
            done = plans.apply(workspace, plan)
            if api is not None:
                api.event("copied", plan["repo"], project=workspace.name, files=[t for _, t in done],
                          picked=", ".join(plan["picked"]), adapted=plan.get("adapted") or [], via="AI agent (MCP)")
            return _applied(workspace, plan, done)
        except reuse.CopyError as e:
            return f"Tracebase error: {e}"

    @mcp.tool(annotations=READ_ONLY)
    def find_errors(service: str | None = None, since: str | None = "24h", query: str | None = None,
                    status: str | None = None, limit: int = 15) -> str:
        """Production errors from the developer's logs, grouped (thousands of identical errors = one group with an
        id like E-5). `since`: "last night", "2h", "yesterday 18:00", "7d" or null for any time. `query` filters the
        message text, `status`: new / regressed / known / resolved / ignored."""
        try:
            groups = api.errors(service=service, since=since, status=status, q=query, limit=limit)
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        if not groups:
            return "No errors found" + (f" since {since}" if since else "") + "."
        return "\n".join(
            f"{g['code']} [{g['status']}{', SPIKING' if g.get('spike') else ''}] service={g['service']} "
            f"count={g.get('window_count') if since else g['count']} first={g['first_seen']} last={g['last_seen']}: {g['title']}"
            for g in groups) + "\n\nCall explain_error(error_id) for the cause, the failing code and a fix."

    @mcp.tool(annotations=READ_ONLY)
    def explain_error(error_id: str) -> str:
        """Investigate one error group (e.g. "E-5"): likely cause, impact, the failing lines of code in the
        developer's repos, the git commit that last changed them, notable events in other services at the same time
        (restarts, deploys, backups) and suggested fixes. Computed on their machine from their own logs and code."""
        gid = error_id.strip().upper()
        gid = gid if gid.startswith("E-") else f"E-{gid}"
        try:
            g = api.error(gid)
            r = api.explain_error(gid)
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        out = [f"{gid} in {g['service']}: {g['title']}",
               f"{g['count']} times, first {g['first_seen']}, last {g['last_seen']}, status {g['status']}",
               f"\nLikely cause: {r['cause']}", f"Impact: {r['impact']}"]
        if r.get("events"):
            out.append("\nOther services around that time:")
            out += [f"- {e['ts']} {e['service']}: {e['message'][:160]}" for e in r["events"][:8]]
        for c in r.get("code") or []:
            out.append(f"\nCode at {c['repo']}/{c['path']}:{c['line']}" + (f" ({c['func']})" if c.get("func") else ""))
            out.append("```\n" + "\n".join(f"{'>>' if l['n'] == c['line'] else '  '} {l['n']:>4} {l['text']}"
                                            for l in c["snippet"]) + "\n```")
        if r.get("commit"):
            cm = r["commit"]
            out.append(f"\nThat line was last changed by commit {cm['sha']} \"{cm['message']}\" ({cm['author']}, {cm['date']}"
                       + (f", {cm['minutes_before_first_error']} min before the first error" if cm.get("minutes_before_first_error") is not None else "") + ")")
        if r.get("fix"):
            out.append("\nSuggested fix:")
            out += [f"{i}. {f}" for i, f in enumerate(r["fix"], 1)]
        if g.get("samples"):
            out.append("\nSample log line:\n" + g["samples"][0]["message"][:800])
        return "\n".join(out)

    @mcp.tool(annotations=READ_ONLY)
    def get_code_bundle(repo: str, path: str, symbol: str | None = None) -> str:
        """Show a file from an old project PLUS everything it needs from that project, without planning a copy
        (use this when you want to adapt the code yourself instead of copying it)."""
        try:
            b = api.bundle(repo, [path], manifest_ctx(), symbol=symbol)
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        out = [f"Bundle from {b['repo']} ({len(b['files'])} files):"]
        for f in b["files"]:
            state = "EXISTS in current project" if f["exists_in_project"] else "new"
            out.append(f"\n## {f['path']}  [{f['role']}, {state}]  — {f['reason']}")
            if f["role"] == "wiring" and f["snippets"]:
                out.append("Relevant lines:\n" + "\n".join(f"  {s['line'].strip()}" +
                                                           (f"   (inside: {s['inside']})" if s.get("inside") else "")
                                                           for s in f["snippets"]))
            else:
                out.append(f"```{f['language'] or ''}\n{f['content']}\n```")
        if b.get("packages"):
            out.append("\nPackages used: " + ", ".join(f"{p['name']} {p['version']}" for p in b["packages"]))
        if b.get("env"):
            out.append("Env vars read: " + ", ".join(b["env"]))
        return "\n".join(out)

    @mcp.tool(annotations=READ_ONLY)
    def search_code(query: str, repo: str | None = None, language: str | None = None, limit: int = 8) -> str:
        """Keyword + meaning search over all the developer's past repositories, e.g. "send password reset email"."""
        try:
            res = api.search(query, repo=repo, language=language, limit=limit)
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        if not res["results"]:
            return "No matches."
        return "\n\n".join(
            f"repo={r['repo']} path={r['path']} lines {r['lines'][0]}-{r['lines'][1]} {r['symbol']} ({r['language']})\n"
            f"```\n{r['preview']}\n```" for r in res["results"])

    @mcp.tool(annotations=READ_ONLY)
    def read_code(repo: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file (or a line range) from one of the developer's past repositories."""
        try:
            res = api.read(repo, path, start_line, end_line)
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        body = "\n".join(f"{ln['n']:>5}  {ln['text']}" for ln in res["lines"])
        return f"{res['repo']}/{res['path']} (lines {res['start']}-{res['end']} of {res['total_lines']})\n{body}"

    @mcp.tool(annotations=READ_ONLY)
    def list_repos() -> str:
        """List the developer's indexed repositories."""
        try:
            repos = api.repos()
        except TracebaseError as e:
            return f"Tracebase error: {e}"
        return "\n".join(f"{r['name']} ({r['kind']}, {r['files']} files): {', '.join(list(r.get('languages', {}))[:4])}"
                         for r in repos) or "No repositories indexed."

    @mcp.prompt()
    def copy(what: str) -> str:
        """Find something you built before in your old projects and copy it into this one (asks before writing)."""
        return (f"Find my own past implementation of \"{what}\" in my old projects with the tracebase tools "
                "(find_code, then preview_copy for the best match). Show me what would be copied and ask me before "
                "writing anything. Do not search the web.")

    @mcp.prompt()
    def why(what: str = "what broke last night") -> str:
        """Find out why something failed in production, from your own logs and code."""
        return (f"Use the tracebase tools (find_errors, then explain_error) to find out {what}. Show me the cause, the "
                "failing code and the commit that changed it, and suggest a fix. Do not search the web.")

    return mcp


def run(api: Client) -> None:
    build_server(api).run()  # stdio
