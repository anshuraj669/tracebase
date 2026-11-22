"""tracebase: find code you already wrote and copy it into the project you're working on.

  tracebase copy "login logic"                   find it in your old projects and copy it here (+ what it needs)
  tracebase copy "role based access control"     e.g. middleware + User.hasRole + Role model + migration + wiring
  tracebase copy "appointments" --rename Appointment=Booking   adapt names while copying
  tracebase find "jwt refresh"                   show your past implementations, with code
  tracebase search "send password reset email"   search across all your repos
  tracebase connect claude                       let your AI agent use it (claude, codex, gemini, opencode, cursor, ollama)
  tracebase pending / apply <id>                 review and write copies your agent prepared
  tracebase login <token> --url http://nas:8080  use your team's shared Tracebase

  tracebase errors --since "last night"          production errors, grouped (Log Detective)
  tracebase why E-5                              why it happened: cause, the failing code, the commit, the fix
  docker logs app 2>&1 | tracebase ingest - --service app      send logs from anywhere
  tracebase data                                 what is stored; export / purge / forget (admin)

Everything stays on your machine: no AI writes the code and nothing is sent to the internet. Tracebase finds
your code, copies it, and merges it into existing files (missing methods, imports, wiring lines). It never
writes a file without showing you the diff first.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, connect, plans, project, reuse, ui
from .client import CONFIG, Client, TracebaseError, load_config, save_config


def _edit(content: str, suffix: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        subprocess.call([*editor.split(), tmp])
        return Path(tmp).read_text()
    finally:
        os.unlink(tmp)


def candidates(examples: list[dict], repo: str | None = None) -> list[dict]:
    """One entry per file (best-ranked symbol first)."""
    out, seen = [], set()
    for e in examples:
        if repo and e["repo"] != repo and not e["repo"].endswith("/" + repo):
            continue
        key = (e["repo"], e["path"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _print_candidate(n: int, e: dict, preview: int = 0) -> None:
    fw = f" · {e['framework']}" if e.get("framework") else ""
    meta = f"{e['symbol']} ({e['language']}{fw})"
    ui.info(f"{ui.bold(str(n))}. {ui.teal(e['repo'])} {e['path']}:{e['lines'][0]}-{e['lines'][1]}  {ui.dim(meta)}")
    for ln in e["code"].splitlines()[:preview]:
        ui.info(ui.dim("     │ ") + ln)


# ---------------------------------------------------------------- copy
def cmd_copy(args: argparse.Namespace, api: Client) -> int:
    root = Path(args.path).resolve()
    task = " ".join(args.task).strip()
    ctx = project.context(root, task)

    with ui.Spinner("Searching your projects"):
        found = api.examples(task, project=ctx, limit=12)
    cands = candidates(found["examples"], args.repo)[:8]
    about = f"({found.get('query', task)} · this project: {found.get('stack_text') or '?'})"
    ui.header(f"Found in your projects  {ui.dim(about)}")
    if not cands:
        ui.error("Nothing matching found in your indexed projects.")
        return 1
    for n, e in enumerate(cands, 1):
        _print_candidate(n, e, preview=6 if n == 1 else 0)

    if args.pick:
        pick = args.pick
    elif args.yes:
        pick = 1
    else:
        while True:
            try:
                ans = input(f"\nCopy which one? [1-{len(cands)}, q] (1) ").strip().lower() or "1"
            except EOFError:
                return 0
            if ans == "q":
                return 0
            if ans.isdigit() and 1 <= int(ans) <= len(cands):
                pick = int(ans)
                break
    if not 1 <= pick <= len(cands):
        ui.error(f"--pick must be between 1 and {len(cands)}")
        return 2
    chosen = cands[pick - 1]

    manifests = {k: v for k, v in ctx["files"].items() if "/" not in k}
    with ui.Spinner("Collecting what it needs"):
        bundle = api.bundle(chosen["repo"], [chosen["path"]], {"name": ctx["name"], "tree": ctx["tree"], "files": manifests},
                            symbol=chosen.get("symbol"), renames=args.rename)
    changes = reuse.plan_changes(root, bundle)
    ui.header(f"Copy from {ui.teal(bundle['repo'])} into {ui.bold(root.name)}")
    labels = {"create": ui.green("new   "), "merge": ui.yellow("merge "), "wire": ui.yellow("wire  "),
              "same": ui.dim("same  "), "manual": ui.cyan("manual"), "deps": ui.yellow("deps  "), "env": ui.yellow("env   ")}
    for c in changes:
        extra = f" (+ {', '.join(c.added)})" if c.kind in ("merge", "deps", "env") else ""
        ui.info(f"{labels.get(c.kind, c.kind)} {c.target}  {ui.dim(c.reason + extra)}")
    if bundle.get("adapted"):
        ui.info(ui.cyan("adapted to this project: ") + "; ".join(bundle["adapted"]))
    todo = [c for c in changes if c.new_text is not None]
    if not todo:
        ui.info(ui.dim("Nothing to write: everything is already in this project."))
    if args.dry_run or not todo:
        return 0
    if not args.yes and ui.ask(f"\nReview and copy {len(todo)} file(s)?", "yn", "y") != "y":
        return 0

    written, _ = _review(root, todo, args.yes)
    if written:
        api.event("copied", bundle["repo"], project=root.name, files=[c.target for c, _ in written],
                  picked=chosen["path"], adapted=bundle.get("adapted") or [])
    text = "\n".join(c.new_text or "" for c, _ in written)
    env = [k for c, _ in written if c.kind == "env" for k in c.added] or reuse.env_missing(root, bundle, text)
    _followups(changes, reuse.packages_needed(bundle, text), env)
    return 0


def _review(root: Path, todo: list[reuse.Change], yes: bool) -> tuple[list, list]:
    """Show each diff and write the ones the user approves. Returns (written [(change, note)], skipped)."""
    backup_dir = root / ".tracebase" / "backups" / time.strftime("%Y%m%d-%H%M%S")
    written, skipped, apply_all = [], [], yes
    for i, c in enumerate(todo):
        ui.header(f"{c.target}  {ui.dim('from ' + c.source)}")
        print(ui.diff(c.target, c.old_text, c.new_text))
        choice = "y" if apply_all else ui.ask(f"Write {c.target}?  (y)es (n)o (e)dit (a)ll (q)uit", "yneaq", "y")
        if choice == "e":
            c.new_text = _edit(c.new_text, Path(c.target).suffix)
            choice = "y"
        if choice == "a":
            apply_all, choice = True, "y"
        if choice == "q":
            skipped += [x.target for x in todo[i:]]
            break
        if choice == "y":
            written.append((c, reuse.apply(root, c, backup_dir)))
            ui.info(ui.green("✓ written"))
        else:
            skipped.append(c.target)

    ui.header("Done")
    for c, note in written:
        ui.info(f"{ui.green('✓')} {note:8} {c.target}")
    for t in skipped:
        ui.info(f"{ui.dim('–')} skipped  {ui.dim(t)}")
    if any(n == "updated" for _, n in written):
        ui.info(ui.dim(f"Originals backed up to {backup_dir.relative_to(root)}/"))
    return written, skipped


def _followups(changes: list[reuse.Change], pkgs: list[dict], env: list[str]) -> None:
    manual = [(c, sn) for c in changes for sn in c.manual]
    if manual or pkgs or env:
        ui.header("Still to do by hand")
    for c, sn in manual:
        where = f" (it was inside: {sn['inside']})" if sn.get("inside") else ""
        ui.info(f"• {c.target}: add {ui.bold(sn['line'].strip())}{ui.dim(where)}")
    for step in plans.install_steps(pkgs):
        ui.info(f"• {step}")
    if env:
        ui.info(f"• set in your .env: {', '.join(env)}")
    ui.info(ui.dim("Review with `git diff` and adjust names to this project before committing."))


# ---------------------------------------------------------------- plans made by AI agents (MCP)
def cmd_pending(args: argparse.Namespace, api: Client) -> int:
    root = Path(args.path).resolve()
    items = plans.pending(root)
    if not items:
        ui.info(ui.dim("No pending copies in this project."))
        return 0
    for p in items:
        ui.header(f"{ui.bold(p['id'])}  {ui.dim(p['created'])}  {p.get('request') or ''}")
        print(plans.summary(p))
    ui.info(ui.dim("\nReview and write one with `tracebase apply <id>`, or drop it with `tracebase discard <id>`."))
    return 0


def cmd_apply(args: argparse.Namespace, api: Client) -> int:
    root = Path(args.path).resolve()
    if not args.id:
        items = plans.pending(root)
        if len(items) != 1:
            ui.error("Which one? " + (", ".join(p["id"] for p in items) if items else "no pending copies here"))
            return 2
        args.id = items[0]["id"]
    plan = plans.load(root, args.id)
    bad = plans.stale(root, plan)
    if bad:
        ui.error("These files changed after the preview, ask your agent to preview again: " + ", ".join(bad))
        return 1
    ui.header(f"Pending copy {plan['id']}")
    print(plans.summary(plan))
    changes = plans.changes(plan)
    todo = [c for c in changes if c.new_text is not None]
    if not args.yes and ui.ask(f"\nReview and copy {len(todo)} file(s)?", "yn", "y") != "y":
        return 0
    written, _ = _review(root, todo, args.yes)
    if written:
        plans.discard(root, plan["id"])
        api.event("copied", plan["repo"], project=root.name, files=[c.target for c, _ in written],
                  picked=", ".join(plan["picked"]), adapted=plan.get("adapted") or [], via="tracebase apply")
    _followups(changes, plan["packages"], plan["env"])
    return 0


def cmd_discard(args: argparse.Namespace, api: Client) -> int:
    root = Path(args.path).resolve()
    plans.load(root, args.id)
    plans.discard(root, args.id)
    ui.info(f"Discarded {args.id}.")
    return 0


# ---------------------------------------------------------------- connect an AI agent
def cmd_connect(args: argparse.Namespace, api: Client) -> int:
    home = Path.home()
    if args.print:
        print(connect.snippet())
        return 0
    if not args.agent:
        found = connect.detected()
        ui.header("Connect Tracebase to your AI agent")
        for k, (_, label, exe) in connect.AGENTS.items():
            mark = ui.green("● installed") if k in found else ui.dim("○ not found")
            ui.info(f"  tracebase connect {k:9} {label:36} {mark}")
        ui.info(ui.dim("\nAny other MCP client: `tracebase connect --print` shows the config to paste."))
        return 0
    targets = connect.detected() if args.agent == "all" else [args.agent]
    if not targets:
        ui.error("No supported agent found on PATH.")
        return 1
    for k in targets:
        fn, label, _ = connect.AGENTS[k]
        try:
            ui.info(f"{ui.green('✓')} {label}: {fn(home)}")
        except Exception as e:
            ui.error(f"{label}: {e}")
            return 1
    ui.info(ui.dim("Restart the agent, open your project folder in it and ask e.g. "
                   "\"copy the login logic from my old projects\". It will show you the code and ask before writing."))
    return 0


# ---------------------------------------------------------------- read-only commands
def cmd_find(args: argparse.Namespace, api: Client) -> int:
    task = " ".join(args.task)
    ctx = None if args.anywhere else project.context(Path(args.path).resolve(), task, max_files=4)
    with ui.Spinner("Searching your projects"):
        res = api.examples(task, project=ctx, limit=args.limit)
    if ctx:
        ui.info(ui.dim(f"this project: {res.get('stack_text') or '?'}"))
    for e in res["examples"]:
        ui.header(f"[{e['id']}] {e['repo']}/{e['path']}:{e['lines'][0]}-{e['lines'][1]}  {ui.dim(e['symbol'] or '')}")
        body = e["code"].splitlines()
        print("\n".join(f"  {ui.dim(str(e['lines'][0] + n).rjust(4))} {ln}" for n, ln in enumerate(body[:args.lines])))
        if len(body) > args.lines:
            ui.info(ui.dim(f"… {len(body) - args.lines} more lines"))
    return 0 if res["examples"] else 1


def cmd_search(args: argparse.Namespace, api: Client) -> int:
    res = api.search(" ".join(args.query), repo=args.repo, language=args.language, limit=args.limit)
    for r in res["results"]:
        meta = f"{r['language']} · {r['score']:.2f}"
        print(f"{ui.teal(r['repo'])} {r['path']}:{r['lines'][0]}-{r['lines'][1]}  {ui.bold(r['symbol'] or '')} {ui.dim(meta)}")
    if not res["results"]:
        print(ui.dim("no matches"))
    return 0


def cmd_show(args: argparse.Namespace, api: Client) -> int:
    res = api.read(args.repo, args.file, args.start, args.end)
    for ln in res["lines"]:
        print(f"{ui.dim(str(ln['n']).rjust(5))}  {ln['text']}")
    return 0


def cmd_status(args: argparse.Namespace, api: Client) -> int:
    h = api.health()
    repos = api.repos()
    print(f"{ui.green('●')} Tracebase {h.get('version')} at {api.url}")
    if h.get("team"):
        me = api.me()
        print(f"  team server · signed in as {ui.bold(me['name'])} ({me['role']}) · repos: {', '.join(me['repos'])}")
    print(f"  {len(repos)} repos visible" + ("" if h.get("team") else f", indexed from {h.get('repos_dir')}"))
    return 0


# ---------------------------------------------------------------- Log Detective
STATUS_COLOR = {"new": ui.yellow, "regressed": ui.red, "known": ui.dim, "resolved": ui.green, "ignored": ui.dim}


def _gid(v: str) -> str:
    v = v.strip().upper()
    return v if v.startswith("E-") else f"E-{v}"


def cmd_errors(args: argparse.Namespace, api: Client) -> int:
    since = None if args.all else args.since
    groups = api.errors(service=args.service, since=since, status=args.status, q=args.grep, limit=args.limit)
    if not groups:
        print(ui.dim("no errors" + (f" since {since}" if since else "")))
        return 0
    head = f"Errors{' since ' + since if since else ''}" + (f" in {args.service}" if args.service else "")
    ui.header(head)
    for g in groups:
        n = g.get("window_count") if since else g["count"]
        flag = ui.red(" ▲ spike") if g.get("spike") else ""
        st = STATUS_COLOR.get(g["status"], str)(g["status"])
        print(f"  {ui.bold(g['code'].ljust(6))} {ui.teal(g['service'][:14].ljust(14))} {str(n).rjust(6)}x  "
              f"{st.ljust(18 if ui.COLOR else 9)} {g['title'][:70]}{flag}")
    print(ui.dim(f"\n  tracebase why {groups[0]['code']}    explain one (cause, failing code, fix)"))
    return 0


def cmd_why(args: argparse.Namespace, api: Client) -> int:
    gid = _gid(args.id)
    g = api.error(gid)
    with ui.Spinner(f"Investigating {gid}"):
        r = api.explain_error(gid, refresh=args.refresh)
    ui.header(f"{gid}  {g['service']} · {g['title'][:90]}")
    print(f"  {ui.dim(str(g['count']) + ' times · first ' + g['first_seen'].replace('T', ' ') + ' · last ' + g['last_seen'].replace('T', ' ') + ' · ' + g['status'])}")
    print(f"\n{ui.bold('Why')}\n  {r['cause']}")
    print(f"\n{ui.bold('Impact')}\n  {r['impact']}")
    if r.get("events"):
        print(f"\n{ui.bold('Around the same time in other services')}")
        for e in r["events"][:6]:
            print(f"  {ui.dim(e['ts'].replace('T', ' ')[5:19])} {ui.teal(e['service'])}  {e['message'][:100]}")
    for c in (r.get("code") or [])[:2]:
        print(f"\n{ui.bold('Code')}  {ui.teal(c['repo'])}/{c['path']}:{c['line']}" + (f"  {ui.dim(c['func'])}" if c.get("func") else ""))
        for ln in c["snippet"]:
            mark = ui.red("›") if ln["n"] == c["line"] else " "
            text = ui.red(ln["text"]) if ln["n"] == c["line"] else ln["text"]
            print(f"  {mark}{ui.dim(str(ln['n']).rjust(4))}  {text}")
    cm = r.get("commit")
    if cm:
        when = cm.get("minutes_before_first_error")
        lag = f", {when} min before the first error" if when is not None and 0 <= when < 1440 else ""
        print(f"\n{ui.bold('Last changed by')}  {ui.yellow(cm['sha'])} {cm['message']}  {ui.dim(cm['author'] + ', ' + cm['date'] + lag)}")
    if r.get("fix"):
        print(f"\n{ui.bold('Fix')}")
        for i, f in enumerate(r["fix"], 1):
            print(f"  {i}. {f}")
    src = "rules + your code and git history" if r.get("source") == "rules" else f"local LLM ({r.get('source')})"
    print(ui.dim(f"\n  explained by {src} · mark it: tracebase resolve {gid} [--as known|ignored]"))
    return 0


def cmd_resolve(args: argparse.Namespace, api: Client) -> int:
    gid = _gid(args.id)
    api.set_error_status(gid, args.status)
    print(f"{ui.green('✓')} {gid} marked {args.status}" +
          (ui.dim("  (it reopens as 'regressed' if it happens again)") if args.status == "resolved" else ""))
    return 0


def cmd_ingest(args: argparse.Namespace, api: Client) -> int:
    total = {"events": 0, "errors": 0, "new_groups": []}
    for f in args.files:
        text = sys.stdin.read() if f == "-" else Path(f).read_text(errors="replace")
        svc = args.service or (None if f == "-" else Path(f).name.split(".")[0])  # payments.log -> payments
        res = api.ingest(text, service=svc)
        for k in ("events", "errors"):
            total[k] += res.get(k, 0)
        total["new_groups"] += res.get("new_groups", [])
    print(f"{ui.green('✓')} {total['events']} log lines, {total['errors']} errors/warnings"
          + (f", new: {', '.join(total['new_groups'])}" if total["new_groups"] else ""))
    return 0


def cmd_alerts(args: argparse.Namespace, api: Client) -> int:
    items = api.alerts(unread=not args.all)
    if not items:
        print(ui.dim("no unread alerts" if not args.all else "no alerts"))
    for a in items:
        sev = ui.red("●") if a["severity"] == "critical" else ui.yellow("●")
        print(f"{sev} {ui.dim(a['created_at'][5:16])}  {a['title']}")
        for line in (a.get("body") or "").splitlines()[:2]:
            print(f"    {ui.dim(line[:110])}")
    if args.read and items:
        api.read_alerts()
        print(ui.dim("marked as read"))
    return 0


# ---------------------------------------------------------------- team mode
def cmd_login(args: argparse.Namespace, api: Client) -> int:
    url = (args.url or api.url).rstrip("/")
    me = Client(url, token=args.token).me()   # verify before saving
    save_config({"url": url, "token": args.token})
    ui.info(f"{ui.green('✓')} Signed in to {url} as {ui.bold(me['name'])} ({me['role']}). "
            f"Saved to {CONFIG} (only you can read it).")
    ui.info(ui.dim("Your AI agent's `tracebase mcp` uses this login too: no token goes into its config."))
    return 0


def cmd_logout(args: argparse.Namespace, api: Client) -> int:
    cfg = load_config()
    if cfg.pop("token", None):
        save_config(cfg)
    ui.info("Signed out.")
    return 0


def cmd_team(args: argparse.Namespace, api: Client) -> int:
    if args.action == "list":
        for m in api.members():
            seen = m.get("last_seen") or "never"
            print(f"{ui.bold(m['name']):24} {m['role']:7} repos: {', '.join(m['repos']):40} {ui.dim('last seen ' + seen)}")
        return 0
    if args.action == "audit":
        for e in reversed(api.audit(args.user, args.what, args.limit)):
            d = e["detail"]
            what = d.get("task") or d.get("question") or ", ".join(d.get("files") or d.get("paths") or []) or \
                ", ".join(f"{k}={v}" for k, v in d.items())
            print(f"{ui.dim(e['at'])}  {ui.bold(e['user']):14} {e['action']:14} {ui.teal(e['repo'] or '')}  {what[:90]}")
        return 0
    if not args.name:
        ui.error(f"tracebase team {args.action} needs a member name")
        return 2
    if args.action == "add":
        res = api.add_member(args.name, args.repos or ["*"], "admin" if args.admin else "member")
        ui.info(f"{ui.green('✓')} Added {ui.bold(args.name)} with access to: {', '.join(args.repos or ['*'])}")
        ui.info(f"Token (shown once, send it privately): {ui.bold(res['token'])}")
        ui.info(ui.dim(f"They run:  tracebase login {res['token']} --url {api.url}"))
    elif args.action == "repos":
        api.set_member_repos(args.name, args.repos or ["*"])
        ui.info(f"{ui.green('✓')} {args.name} now sees: {', '.join(args.repos or ['*'])}")
    elif args.action == "rotate":
        res = api.rotate_member(args.name)
        ui.info(f"{ui.green('✓')} New token for {args.name} (the old one stopped working): {ui.bold(res['token'])}")
    elif args.action == "remove":
        api.remove_member(args.name)
        ui.info(f"{ui.green('✓')} Removed {args.name}; their token no longer works.")
    return 0


# ---------------------------------------------------------------- data & privacy (admin)
def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        ui.error("refusing to delete without confirmation: pass --yes")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _deleted(res: dict) -> str:
    d = res.get("deleted") or res.get("would_delete") or {}
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in d.items() if v) or "nothing"


def cmd_data(args: argparse.Namespace, api: Client) -> int:
    a = args.action
    if a == "summary":
        s = api.data_summary()
        db, c = s["database"], s["counts"]
        print(f"{ui.bold('Database')}  {db['path']}  {_size(db['size_bytes'])}  "
              + (ui.green("encrypted") if db["encrypted"] else ui.yellow("not encrypted (set TRACEBASE_DB_KEY)")))
        print(f"{ui.bold('Code')}      {c['repos']} repos, {c['files']} files, {c['chunks']} code chunks, "
              f"{c['secrets']} secrets findings")
        span = f" ({s['logs']['oldest'] or '-'} → {s['logs']['newest'] or '-'})" if c["log_events"] else ""
        print(f"{ui.bold('Logs')}      {c['log_events']} log lines{span}, {c['error_groups']} error groups, "
              f"{c['alerts']} alerts")
        print(f"{ui.bold('Team')}      {c['users']} members, {c['audit']} audit entries")
        r = s["redaction"]
        print(f"{ui.bold('Redaction')} " + (ui.green("on") + (", IPs too" if r["ips"] else ", IPs kept")
                                            + (", custom patterns" if r["custom_patterns"] else "")
                                            if r["enabled"] else ui.yellow("off: personal data in logs is stored")))
        print(f"{ui.bold('Retention')} " + ", ".join(f"{k.replace('_', ' ')} {v}d" if v else f"{k.replace('_', ' ')} forever"
                                                     for k, v in s["retention_days"].items()))
        ex = s["excluded"]
        if ex["repos"] or ex["services"]:
            print(f"{ui.bold('Forgotten')} repos: {', '.join(ex['repos']) or '-'} · services: {', '.join(ex['services']) or '-'}")
        o = s["outbound"]
        print(f"{ui.bold('Outbound')}  alert webhook: {'on' if o['alert_webhook'] else 'off'} · "
              f"LLM: {o['llm']['provider']} at {o['llm']['base_url']}")
        return 0
    if a == "prune":
        print(f"{ui.green('✓')} Retention applied: deleted {_deleted(api.data_prune())}")
        return 0
    if a == "export":
        out_path = args.output or f"tracebase-export-{args.service or 'all'}.ndjson"
        if out_path == "-":
            n = api.export(sys.stdout, service=args.service, match=args.match)
        else:
            fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # personal data: owner-only
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                n = api.export(out, service=args.service, match=args.match)
        if out_path != "-":
            ui.info(f"{ui.green('✓')} {n} records written to {out_path} (only you can read it)")
        return 0
    if a == "wipe":
        if not _confirm("Delete EVERYTHING Tracebase stored (code index, logs, errors, alerts"
                        + ("" if args.keep_team else ", members, audit log") + ")?", args.yes):
            return 1
        api.wipe(keep_team=args.keep_team)
        ui.info(f"{ui.green('✓')} All stored data deleted. The code index is rebuilt from the repos folder on the next run.")
        return 0
    if not args.name:
        ui.error(f"tracebase data {a} needs a name (or text to match)")
        return 2
    if a == "forget-repo":
        if not _confirm(f"Delete the index of {args.name} and stop indexing it?", args.yes):
            return 1
        res = api.forget_repo(args.name)
        ui.info(f"{ui.green('✓')} Forgot {args.name}: deleted {_deleted(res)}. It won't be indexed again "
                f"(`tracebase data restore-repo {args.name}` to undo). The folder on disk is untouched.")
    elif a == "restore-repo":
        api.restore_repo(args.name)
        ui.info(f"{ui.green('✓')} {args.name} will be indexed again on the next run.")
    elif a == "forget-service":
        if not _confirm(f"Delete all logs, errors and alerts of {args.name}?", args.yes):
            return 1
        res = api.forget_service(args.name, exclude=args.stop)
        ui.info(f"{ui.green('✓')} Deleted {_deleted(res)} of {args.name}."
                + (" New logs for it are dropped." if args.stop else ""))
    elif a == "restore-service":
        api.restore_service(args.name)
        ui.info(f"{ui.green('✓')} Logs for {args.name} are accepted again.")
    elif a == "purge":
        preview = api.purge(args.name, service=args.service, dry_run=True)
        if not any((preview.get("would_delete") or {}).values()):
            ui.info("Nothing stored contains that text.")
            return 0
        if args.dry_run or not _confirm(f"Delete {_deleted(preview)} containing that text?", args.yes):
            ui.info(f"Would delete: {_deleted(preview)}")
            return 0
        ui.info(f"{ui.green('✓')} Deleted {_deleted(api.purge(args.name, service=args.service))}")
    return 0


def cmd_mcp(args: argparse.Namespace, api: Client) -> int:
    from .mcp_server import run
    run(api)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tracebase", description="Find code you already wrote and copy it here.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 2)[2])
    p.add_argument("--url", help="Tracebase server (default $TRACEBASE_URL or http://localhost:8080)")
    p.add_argument("--version", action="version", version=f"tracebase {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("copy", help="find code in your old projects and copy it into this one")
    c.add_argument("task", nargs="+", help='what to copy, e.g. "login logic"')
    c.add_argument("--path", default=".", help="project to copy into (default: current folder)")
    c.add_argument("--from", dest="repo", help="only take code from this repo")
    c.add_argument("--pick", type=int, help="pick result N without asking")
    c.add_argument("--dry-run", action="store_true", help="show what would be copied, write nothing")
    c.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                   help="rename a word in the copied code and file names, every spelling (e.g. Doctor=Staff); repeatable")
    c.add_argument("-y", "--yes", action="store_true", help="take the best match and write without asking")
    c.set_defaults(fn=cmd_copy)

    f = sub.add_parser("find", help="show your past implementations of something, with code")
    f.add_argument("task", nargs="+")
    f.add_argument("--path", default=".")
    f.add_argument("--anywhere", action="store_true", help="don't prefer this project's language/framework")
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--lines", type=int, default=40)
    f.set_defaults(fn=cmd_find)

    s = sub.add_parser("search", help="search across all your repos")
    s.add_argument("query", nargs="+")
    s.add_argument("--repo")
    s.add_argument("--language")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    sh = sub.add_parser("show", help="print a file from one of your repos")
    sh.add_argument("repo")
    sh.add_argument("file")
    sh.add_argument("--start", type=int)
    sh.add_argument("--end", type=int)
    sh.set_defaults(fn=cmd_show)

    pe = sub.add_parser("pending", help="copies your AI agent prepared and that wait for your approval")
    pe.add_argument("--path", default=".")
    pe.set_defaults(fn=cmd_pending)

    ap = sub.add_parser("apply", help="review a pending copy and write it into this project")
    ap.add_argument("id", nargs="?")
    ap.add_argument("--path", default=".")
    ap.add_argument("-y", "--yes", action="store_true", help="write without asking")
    ap.set_defaults(fn=cmd_apply)

    di = sub.add_parser("discard", help="drop a pending copy")
    di.add_argument("id")
    di.add_argument("--path", default=".")
    di.set_defaults(fn=cmd_discard)

    co = sub.add_parser("connect", help="connect Tracebase to your AI agent (Claude Code, Codex, Gemini, opencode, Ollama…)")
    co.add_argument("agent", nargs="?", choices=[*connect.AGENTS, "all"])
    co.add_argument("--print", action="store_true", help="print MCP config for any other client")
    co.set_defaults(fn=cmd_connect)

    lo = sub.add_parser("login", help="sign in to a team Tracebase server with your token")
    lo.add_argument("token")
    lo.add_argument("--url", default=argparse.SUPPRESS, help="the team server, e.g. http://nas.local:8080")
    lo.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="forget the saved token").set_defaults(fn=cmd_logout)

    tm = sub.add_parser("team", help="team server admin: members, repo access, audit log")
    tm.add_argument("action", choices=["list", "add", "repos", "rotate", "remove", "audit"])
    tm.add_argument("name", nargs="?")
    tm.add_argument("--repos", nargs="+", metavar="PATTERN", help='repos they can see, e.g. "freelance/*" shop-api')
    tm.add_argument("--admin", action="store_true", help="make them an admin")
    tm.add_argument("--user", help="audit: only this member")
    tm.add_argument("--what", help="audit: only this action (find, bundle, copied, ask…)")
    tm.add_argument("--limit", type=int, default=50)
    tm.set_defaults(fn=cmd_team)

    er = sub.add_parser("errors", help="production errors, grouped (Log Detective)")
    er.add_argument("--since", default="24h", help='"last night", "2h", "yesterday 18:00", ... (default 24h)')
    er.add_argument("--all", action="store_true", help="any time")
    er.add_argument("--service")
    er.add_argument("--status", choices=["new", "regressed", "known", "resolved", "ignored"])
    er.add_argument("--grep", help="only errors whose message contains this")
    er.add_argument("--limit", type=int, default=20)
    er.set_defaults(fn=cmd_errors)
    wy = sub.add_parser("why", help="explain an error: cause, failing code, the commit, fix")
    wy.add_argument("id", help="E-5 or 5")
    wy.add_argument("--refresh", action="store_true", help="investigate again instead of the saved explanation")
    wy.set_defaults(fn=cmd_why)
    rs = sub.add_parser("resolve", help="mark an error resolved (or known / ignored)")
    rs.add_argument("id")
    rs.add_argument("--as", dest="status", default="resolved", choices=["resolved", "known", "ignored", "new"])
    rs.set_defaults(fn=cmd_resolve)
    ig = sub.add_parser("ingest", help="send log files (or stdin: -) to Tracebase")
    ig.add_argument("files", nargs="+")
    ig.add_argument("--service", help="service name (default: from the log lines / file name)")
    ig.set_defaults(fn=cmd_ingest)
    al = sub.add_parser("alerts", help="new error types and error spikes")
    al.add_argument("--all", action="store_true", help="include read alerts")
    al.add_argument("--read", action="store_true", help="mark them as read")
    al.set_defaults(fn=cmd_alerts)

    da = sub.add_parser("data", help="what is stored, export it, delete it (admin)",
                        description="Data & privacy: see what Tracebase stores, export it, and delete it.",
                        epilog="""examples:
  tracebase data                                   what is stored, encryption, redaction, retention
  tracebase data export --service shop-api         everything stored about a service (NDJSON)
  tracebase data purge "CUST-10442"                erase every log line / error / alert containing it
  tracebase data forget-repo client-a/old-site     delete a repo's index and stop indexing it
  tracebase data forget-service payments --stop    delete a service's logs and drop new ones
  tracebase data prune                             apply the retention periods now""",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    da.add_argument("action", nargs="?", default="summary",
                    choices=["summary", "export", "purge", "forget-repo", "restore-repo", "forget-service",
                             "restore-service", "prune", "wipe"])
    da.add_argument("name", nargs="?", help="repo, service, or the text to purge")
    da.add_argument("--service", help="export / purge: only this service")
    da.add_argument("--match", help="export: only records containing this text")
    da.add_argument("-o", "--output", help="export: file to write (- for stdout)")
    da.add_argument("--stop", action="store_true", help="forget-service: also drop new logs for it")
    da.add_argument("--dry-run", action="store_true", help="purge: only count what would be deleted")
    da.add_argument("--keep-team", action=argparse.BooleanOptionalAction, default=True,
                    help="wipe: keep members and the audit log (default)")
    da.add_argument("-y", "--yes", action="store_true", help="delete without asking")
    da.set_defaults(fn=cmd_data)

    sub.add_parser("status", help="check the connection to the Tracebase server").set_defaults(fn=cmd_status)
    sub.add_parser("mcp", help="MCP server (stdio) for your AI assistant").set_defaults(fn=cmd_mcp)

    args = p.parse_args(argv)
    api = Client(args.url)
    try:
        return args.fn(args, api)
    except (TracebaseError, reuse.CopyError) as e:
        ui.error(str(e))
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
