"""FastAPI service — REST/JSON API for the React UI, plus the static UI itself.
(Phases 1-3: Code Search & Q&A, copy code across projects, team mode, Log Detective)"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, alerts, google_auth, privacy, projects, scheduler, team
from .reuse.bundle import bundle as make_bundle
from .reuse.retrieve import find_examples
from .reuse.stack import PRIMARY, detect as detect_stack
from .code import git, indexer, qa, search
from .code.secrets import suggestion
from .config import settings
from .db import encrypted, row, rows
from .embeddings import embedder
from .llm import LLMUnavailable, llm
from .logs import collector, grouper
from .logs.explain import code_context, explain_group
from .flags import flagsmith as flagsmith_api, rollback as flag_rollback, store as flag_store, suspects as flag_suspects

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    # Privacy upgrade: data stored before redaction existed (or under older rules) is redacted before serving.
    privacy.redact_stored()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Tracebase", version=__version__, lifespan=lifespan, dependencies=[Depends(team.authenticate)])
if settings.cors_origins:  # the UI is served by this app (same origin): cross-site access only if you list sites
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["Authorization", "Content-Type"])

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",  # "Continue with Google" needs the origin (not the path)
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


INGEST_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
               "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Tracebase-Key",
               "Access-Control-Max-Age": "86400"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # connected projects send logs from their own sites (the browser SDK): ingest accepts any origin. Safe: it is
    # authenticated by a header (never cookies), and a project key can only write its own logs.
    ingest = request.url.path == team.INGEST
    if ingest and request.method == "OPTIONS":
        return Response(status_code=204, headers=INGEST_CORS)
    resp = await call_next(request)
    if ingest:
        resp.headers.update(INGEST_CORS)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")  # API answers carry code and logs: never cache them
    return resp


@app.get("/api/health")
def health() -> dict:
    basic = {"status": "ok", "version": __version__, "phase": 3, "team": team.enabled()}
    if team.enabled() and team.current() is team.ANONYMOUS:
        return basic  # public servers: no paths or settings for visitors without a token
    return {
        **basic,
        "logs_dir": str(settings.logs_dir),
        "privacy": {"redaction": settings.redact_logs, "encrypted": encrypted(),
                    "log_retention_days": settings.log_retention_days},
        "alert_webhook": bool(settings.alert_webhook),
        "llm": {"provider": llm.provider, "model": settings.llm_model, "available": llm.available()},
        "embeddings": embedder.name,
        "repos_dir": str(settings.repos_dir),
        "scheduler": scheduler.jobs(),
    }


@app.get("/api/stats")
def stats() -> dict:
    f, p = team.sql_filter("repo")
    fr, pr = team.sql_filter("name")
    c = row(f"SELECT (SELECT COUNT(*) FROM repos WHERE 1=1{fr}) repos, (SELECT COUNT(*) FROM files WHERE 1=1{f}) files,"
            f" (SELECT COUNT(*) FROM chunks WHERE 1=1{f}) chunks, (SELECT COUNT(*) FROM secrets WHERE 1=1{f}) secrets,"
            f" (SELECT COUNT(*) FROM secrets WHERE severity='HIGH'{f}) secrets_high", tuple(pr + p * 4))
    langs = rows(f"SELECT language, COUNT(*) n FROM files WHERE 1=1{f} GROUP BY language ORDER BY n DESC LIMIT 12", tuple(p))
    s, sp = team.service_filter("service")
    lg = row(f"SELECT (SELECT COUNT(*) FROM error_groups WHERE status IN ('new','regressed'){s}) open_errors,"
             f" (SELECT COUNT(*) FROM log_events WHERE 1=1{s}) log_events,"
             f" (SELECT COUNT(*) FROM alerts WHERE read=0{s}) unread_alerts", tuple(sp * 3))
    return {**(c or {}), **(lg or {}), "languages": langs, "indexer": indexer.status}


@app.get("/api/code/repos")
def code_repos() -> list[dict]:
    return search.list_repos()


@app.get("/api/code/search")
def code_search(q: str, repo: str | None = None, language: str | None = None, limit: int = Query(10, le=50)) -> dict:
    return search.search(q, repo=repo, language=language, limit=limit)


@app.get("/api/code/grep")
def code_grep(pattern: str, repo: str | None = None, regex: bool = False, limit: int = Query(100, le=500)) -> dict:
    return search.exact_search(pattern, repo=repo, regex=regex, limit=limit)


@app.get("/api/code/file")
def code_file(repo: str, path: str, start: int | None = None, end: int | None = None, context: int = 0) -> dict:
    res = search.read_code(repo, path, start, end, context)
    if "error" in res:
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/code/tree")
def code_tree(repo: str) -> list[dict]:
    team.require_repo(repo)
    return rows("SELECT path, language, size FROM files WHERE repo=? ORDER BY path", (repo,))


@app.post("/api/code/reindex")
def code_reindex(force: bool = False) -> dict:
    team.require_admin()
    if indexer.status["running"]:
        return {"status": "already running"}
    threading.Thread(target=indexer.index_all, kwargs={"force": force}, daemon=True).start()
    return {"status": "started"}


@app.get("/api/code/index-status")
def code_index_status() -> dict:
    return indexer.status


@app.get("/api/code/secrets")
def code_secrets(repo: str | None = None) -> dict:
    scope, sp = team.sql_filter("repo")
    q, p = f"SELECT repo, path, line, severity, rule, preview FROM secrets WHERE 1=1{scope}", list(sp)
    if repo:
        q, p = q + " AND repo=?", p + [repo]
    found = rows(q + " ORDER BY severity='HIGH' DESC, repo, path", tuple(p))
    return {"findings": found, "suggestion": suggestion(found)}


@app.get("/api/code/history")
def code_history(repo: str, path: str | None = None, line: int | None = None, since: str | None = None) -> dict:
    team.require_repo(repo)
    return git.history(repo, path=path, line=line, since=since)


class AskReq(BaseModel):
    question: str
    repo: str | None = None


@app.post("/api/code/ask")
def code_ask(body: AskReq) -> dict:
    team.require_repo(body.repo)
    team.log("ask", body.repo, question=body.question[:300])
    return qa.ask(body.question, repo=body.repo)


class ExplainReq(BaseModel):
    repo: str
    path: str
    line: int | None = None


@app.post("/api/code/explain")
def code_explain(body: ExplainReq) -> dict:
    """Plain-English summary of the function/class containing `line` (or the start of the file)."""
    team.require_repo(body.repo)
    ch = None
    if body.line:
        ch = row("SELECT symbol, start_line, end_line FROM chunks WHERE repo=? AND path=? AND start_line<=? AND end_line>=?"
                 " ORDER BY (end_line-start_line) LIMIT 1", (body.repo, body.path, body.line, body.line))
    ctx = search.read_code(body.repo, body.path, ch["start_line"] if ch else None, ch["end_line"] if ch else None)
    if "error" in ctx:
        raise HTTPException(404, ctx["error"])
    target = {"symbol": ch["symbol"] if ch else None, "start": ctx["start"], "end": ctx["end"]}
    if not llm.available():
        return {**target, "explanation": None, "note": "No LLM available — start Ollama or set TRACEBASE_LLM_* to enable explanations."}
    code = "\n".join(f"{l['n']:>4} {l['text']}" for l in ctx["lines"][:250])
    try:
        text = llm.complete(
            f"Explain this code from {body.repo}/{body.path} for a developer in plain English: what it does, inputs/outputs, "
            f"side effects, and any bugs or security risks. Max 150 words, use short bullet points.\n\n```\n{code}\n```")
    except LLMUnavailable as e:
        return {**target, "explanation": None, "note": str(e)}
    return {**target, "explanation": text}


# ---------------------------------------------------------------- reuse: copy your old code into a new project
# Used by the `tracebase` CLI and the MCP server. Deterministic and local: no LLM, no network.
class ProjectCtx(BaseModel):
    name: str | None = None
    tree: list[str] = []
    files: dict[str, str] = {}


def _stack(project: ProjectCtx | None, languages: list[str] | None = None, frameworks: list[str] | None = None) -> dict:
    if project:
        files = project.files
        return detect_stack(project.tree or list(files), lambda p: files.get(p))
    fw = frameworks or []
    return {"languages": languages or [], "frameworks": fw, "primary": next((f for f in fw if f in PRIMARY), None)}


class ExamplesReq(BaseModel):
    task: str
    project: ProjectCtx | None = None
    languages: list[str] | None = None
    frameworks: list[str] | None = None
    limit: int = 8


@app.post("/api/reuse/examples")
def reuse_examples(body: ExamplesReq) -> dict:
    """Your best past implementations of a task, preferring the new project's language/framework."""
    stack = _stack(body.project, body.languages, body.frameworks)
    team.log("find", None, task=body.task[:300], project=body.project.name if body.project else None)
    res = find_examples(body.task, stack, limit=min(body.limit, 15),
                        exclude_repo=body.project.name if body.project else None)
    return {**res, "stack": stack}


class BundleReq(BaseModel):
    repo: str
    paths: list[str]
    symbol: str | None = None  # e.g. "AuthService::login": brings the endpoints that call exactly this
    project: ProjectCtx | None = None
    renames: list[str] = []    # ["Doctor=Staff", "clinic=booking"]


@app.post("/api/reuse/bundle")
def reuse_bundle(body: BundleReq) -> dict:
    """The picked file(s) plus everything they need from the same repo, ready to copy."""
    team.require_repo(body.repo)
    team.log("bundle", body.repo, paths=body.paths, project=body.project.name if body.project else None)
    renames = [tuple(x.split("=", 1)) for x in body.renames if "=" in x]
    res = make_bundle(body.repo, body.paths, body.project.model_dump() if body.project else None, symbol=body.symbol,
                      renames=renames)
    if "error" in res:
        raise HTTPException(404, res["error"])
    return res


# ---------------------------------------------------------------- Log Detective: errors grouped, explained, linked to code
def _gid(v: str) -> int:
    try:
        return int(v.upper().removeprefix("E-"))
    except ValueError:
        raise HTTPException(400, "bad error id (expected E-12)")


def _group_or_404(gid: str) -> dict:
    g = grouper.get_group(_gid(gid))
    if not g or not team.can_service(g["service"]):
        raise HTTPException(404, f"error {gid} not found")
    return g


@app.post("/api/logs/ingest")
async def logs_ingest(request: Request, service: str | None = None) -> dict:
    project = getattr(request.state, "project", None)  # sent with a project key: stored under that project
    """Send log lines from anywhere: raw text, {"lines": [...]}, {"events": [...]} or a JSON list of events.
    Personal data and secrets are redacted before anything is stored."""
    limit = settings.max_ingest_mb * 1024 * 1024
    too_big = HTTPException(413, f"log upload larger than {settings.max_ingest_mb} MB: send it in parts")
    if int(request.headers.get("content-length") or 0) > limit:
        raise too_big
    buf = bytearray()
    async for part in request.stream():
        buf += part
        if len(buf) > limit:
            raise too_big
    body = bytes(buf)
    if "json" in request.headers.get("content-type", ""):
        try:
            payload: Any = json.loads(body or b"null")
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        if isinstance(payload, list):
            payload = {"events": payload}
        if not isinstance(payload, dict):
            raise HTTPException(400, 'expected {"lines": [...]}, {"events": [...]} or a list of events')
    else:
        payload = body.decode("utf-8", "replace")
    if project:
        n = len(payload.get("events") or payload.get("lines") or []) if isinstance(payload, dict) else payload.count("\n") + 1
        if not projects.allow(project, max(n, 1)):
            raise HTTPException(429, f"{project} sent more than {projects.EVENTS_PER_MINUTE} events this minute: "
                                     "the rest of this minute's events are dropped")
    try:  # parsing + redaction is CPU work: off the event loop (the request's user context goes with it)
        res = await run_in_threadpool(collector.ingest_payload, payload, project or service, project)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if project:
        await run_in_threadpool(projects.record, project, res["events"])
    else:
        team.log("logs_ingest", None, service=service, events=res["events"])
    return {**res, "new_groups": [f"E-{g}" for g in res["new_groups"]]}


@app.post("/api/logs/collect")
def logs_collect() -> dict:
    team.require_admin()
    return {"files": collector.collect_files(), "docker": collector.collect_docker()}


@app.get("/api/logs/groups")
def logs_groups(service: str | None = None, since: str | None = None, level: str | None = None,
                status: str | None = None, q: str | None = None, limit: int = Query(100, le=500)) -> list[dict]:
    return grouper.find_errors(service, since, level, status, q, limit)


@app.get("/api/logs/groups/{gid}")
def logs_group(gid: str) -> dict:
    return _group_or_404(gid)


@app.post("/api/logs/groups/{gid}/explain")
def logs_explain(gid: str, refresh: bool = False) -> dict:
    g = _group_or_404(gid)
    team.log("explain_error", None, error=g["code"], service=g["service"])
    return explain_group(g["id"], refresh=refresh)


@app.post("/api/logs/groups/{gid}/status")
def logs_status(gid: str, status: str = Body(..., embed=True)) -> dict:
    if status not in ("new", "known", "resolved", "ignored"):
        raise HTTPException(400, "status must be new, known, resolved or ignored")
    g = _group_or_404(gid)
    grouper.set_status(g["id"], status)
    team.log("error_status", None, error=g["code"], status=status)
    return {"ok": True, "group": g["code"], "status": status}


@app.get("/api/logs/services")
def logs_services() -> list[dict]:
    return grouper.services()


@app.get("/api/logs/timeline")
def logs_timeline(service: str | None = None, hours: int = Query(48, le=24 * 31)) -> list[dict]:
    if service:
        team.require_service(service)
    return grouper.timeline(service, hours)


@app.get("/api/logs/stats")
def logs_stats(service: str | None = None, window: int = 10) -> dict:
    if service:
        team.require_service(service)
    elif team.allowed_services() is not None:
        raise HTTPException(400, "pass ?service=")
    return grouper.rate_stats(service, window_min=window)


@app.get("/api/logs/events")
def logs_events(group: str | None = None, service: str | None = None, level: str | None = None,
                q: str | None = None, limit: int = Query(200, le=1000)) -> list[dict]:
    scope, sp = team.service_filter("service")
    where, p = ["1=1" + scope], list(sp)
    if group:
        where.append("group_id=?")
        p.append(_gid(group))
    if service:
        where.append("service=?")
        p.append(service)
    if level:
        where.append("level=?")
        p.append(level.upper())
    if q:
        where.append("message LIKE ?")
        p.append(f"%{q}%")
    return rows(f"SELECT * FROM log_events WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?", (*p, limit))


# ---------------------------------------------------------------- feature flags (Flagsmith): a cause, and a kill switch
@app.post("/api/flags/webhook")
async def flags_webhook(request: Request) -> dict:
    """Flagsmith environment webhook. Signed with the webhook secret (X-Flagsmith-Signature) when one is set; in team
    mode a secret (or an admin token) is required, since this path is open to Flagsmith without a Tracebase token."""
    body = await request.body()
    if len(body) > 1024 * 1024:
        raise HTTPException(413, "webhook body too large")
    secret = settings.flagsmith_webhook_secret
    if secret:
        if not flag_store.valid_signature(body, request.headers.get("x-flagsmith-signature"), secret):
            raise HTTPException(401, "bad X-Flagsmith-Signature (check TRACEBASE_FLAGSMITH_WEBHOOK_SECRET)")
    elif team.enabled() and not team.current().is_admin:
        raise HTTPException(401, "set TRACEBASE_FLAGSMITH_WEBHOOK_SECRET and the same secret on the Flagsmith webhook")
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a Flagsmith webhook payload")
    change = flag_store.parse_flagsmith(payload)
    if not change:
        return {"ok": True, "ignored": payload.get("event_type")}
    stored = await run_in_threadpool(flag_store.record, change)
    return {"ok": True, "recorded": bool(stored), "change": stored}


@app.get("/api/flags/status")
def flags_status() -> dict:
    last = flag_store.history(limit=1)
    return {"flagsmith_url": settings.flagsmith_url or None, "can_change_flags": flagsmith_api.configured(),
            "webhook_signed": bool(settings.flagsmith_webhook_secret), "last_change": last[0] if last else None}


@app.get("/api/flags/changes")
def flags_changes(feature: str | None = None, environment: str | None = None, since: str | None = None,
                  limit: int = Query(100, le=1000)) -> list[dict]:
    from .timeutil import iso, parse_since
    s = parse_since(since) if since else None
    return flag_store.history(feature, environment, iso(s) if s else None, limit=limit)


@app.get("/api/flags/where")
def flags_where(feature: str) -> list[dict]:
    """Where the code checks a flag (its name in quotes), in the repos the caller may see."""
    return flag_suspects.flag_refs(feature)


@app.get("/api/logs/groups/{gid}/flags")
def logs_group_flags(gid: str) -> dict:
    g = _group_or_404(gid)
    code = code_context(g["frames"], g["service"])
    return {"group": g["code"], "suspects": flag_suspects.for_group(g, code),
            "rollbacks": flag_store.rollbacks(g["id"]), "flagsmith_connected": flagsmith_api.configured(),
            # turning a flag off is for admins: members see the suspect, not a button that would be refused
            "can_change_flags": flagsmith_api.configured() and team.current().is_admin}


class RollbackReq(BaseModel):
    change_id: int
    confirm: bool = False      # the person approved turning the flag off


@app.post("/api/logs/groups/{gid}/rollback")
def logs_group_rollback(gid: str, body: RollbackReq) -> dict:
    """Turn off the flag a change turned on (after the person approved it), then watch whether the error stops."""
    team.require_admin()
    g = _group_or_404(gid)
    if not body.confirm:
        raise HTTPException(400, "a flag is only changed with confirm=true, after the person approved it")
    if not flagsmith_api.configured():
        raise HTTPException(409, "Flagsmith is not connected: set TRACEBASE_FLAGSMITH_URL and TRACEBASE_FLAGSMITH_API_KEY")
    try:
        return flag_rollback.apply(g["id"], body.change_id, approved_by=team.current().name)
    except flag_rollback.RollbackError as e:
        raise HTTPException(502, str(e))


@app.get("/api/alerts")
def alerts_list(unread: bool = False) -> list[dict]:
    return alerts.list_alerts(unread)


@app.post("/api/alerts/read")
def alerts_read(alert_id: int | None = Body(None, embed=True)) -> dict:
    alerts.mark_read(alert_id)
    return {"ok": True}


# ---------------------------------------------------------------- sign-in: Continue with Google
class GoogleReq(BaseModel):
    credential: str            # the ID token from Google's sign-in button


@app.get("/api/auth/config")
def auth_config() -> dict:
    return {"team": team.enabled(), "google_client_id": settings.google_client_id if team.google_enabled() else None}


@app.post("/api/auth/google")
def auth_google(body: GoogleReq) -> dict:
    if not team.google_enabled():
        raise HTTPException(404, "Google sign-in is not turned on for this Tracebase")
    try:
        claims = google_auth.verify(body.credential)
    except google_auth.GoogleAuthError as e:
        raise HTTPException(401, str(e))
    u = team.user_for_email(claims["email"])
    if not u:
        raise HTTPException(403, f"{claims['email']} isn't a member of this Tracebase yet. Ask an admin to invite "
                                 "this email (Team page, or `tracebase team add <name> --email ...`).")
    token = team.start_session(u["name"])
    tok = team.set_current(team.User(u["name"], u["role"]))
    try:
        team.log("signed_in", None, via="google", email=claims["email"])
    finally:
        team._current.reset(tok)
    return {"token": token, "name": u["name"], "role": u["role"], "email": claims["email"],
            "picture": claims.get("picture")}


@app.delete("/api/auth/session")
def auth_signout(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token.startswith(team.SESSION_PREFIX):
        team.end_session(token)
    return {"ok": True}


# ---------------------------------------------------------------- connected projects (write-only ingest keys)
class ProjectReq(BaseModel):
    name: str
    platform: str = "other"


@app.get("/api/projects")
def projects_list() -> list[dict]:
    return [p for p in projects.list_all() if team.can_service(p["name"])]


@app.get("/api/projects/{name}")
def project_get(name: str) -> dict:
    team.require_service(name)
    p = next((x for x in projects.list_all() if x["name"] == name), None)
    if not p:
        raise HTTPException(404, f"no project {name}")
    return p


@app.post("/api/projects")
def project_create(body: ProjectReq) -> dict:
    team.require_admin()
    try:
        p, key = projects.create(body.name, body.platform, team.current().name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileExistsError:
        raise HTTPException(409, f"a project called {body.name} already exists")
    team.log("project_created", None, name=p["name"], platform=p["platform"])
    return {**p, "key": key, "note": "shown once: put it in the app's configuration"}


@app.post("/api/projects/{name}/rotate")
def project_rotate(name: str) -> dict:
    team.require_admin()
    try:
        key = projects.rotate(name)
    except KeyError:
        raise HTTPException(404, f"no project {name}")
    team.log("project_key_rotated", None, name=name)
    return {**projects.get(name), "key": key}


@app.delete("/api/projects/{name}")
def project_delete(name: str, delete_data: bool = False) -> dict:
    team.require_admin()
    try:
        projects.delete(name)
    except KeyError:
        raise HTTPException(404, f"no project {name}")
    res = privacy.forget_service(name) if delete_data else {}
    team.log("project_deleted", None, name=name, delete_data=delete_data)
    return {"deleted": name, "data": res}


@app.post("/api/projects/{name}/test")
def project_test(name: str) -> dict:
    """Sends one sample error as the project would, to check the pipeline end to end."""
    team.require_admin()
    if not projects.get(name):
        raise HTTPException(404, f"no project {name}")
    res = collector.ingest_payload({"events": [{
        "level": "error", "message": "Tracebase test event: your project is connected",
        "stack": ["context: sent from the Connect a project page"]}]}, name, name)
    projects.record(name, res["events"])
    return {"ok": True, **res, "new_groups": [f"E-{g}" for g in res["new_groups"]]}


# ---------------------------------------------------------------- team mode: members, tokens, audit log
class MemberReq(BaseModel):
    name: str
    repos: list[str] = ["*"]   # glob patterns over repo names, e.g. ["freelance/*", "shop-api"]
    role: str = "member"
    email: str | None = None   # lets them "Continue with Google"


class EventReq(BaseModel):
    action: str                # e.g. "copied" (reported by the CLI after writing files)
    repo: str | None = None
    detail: dict = {}


@app.get("/api/team/me")
def team_me() -> dict:
    u = team.current()
    return {"name": u.name, "role": u.role, "repos": list(u.patterns), "team_mode": team.enabled(),
            "visible_repos": team.allowed_repos()}


@app.get("/api/team/users")
def team_users() -> list[dict]:
    team.require_admin()
    return team.list_users()


@app.post("/api/team/users")
def team_add(body: MemberReq) -> dict:
    team.require_admin()
    try:
        token = team.add_user(body.name, body.repos, body.role, body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(409, f"{body.name} already exists")
    team.log("member_added", None, name=body.name, repos=body.repos, role=body.role, email=bool(body.email))
    return {"name": body.name, "token": token, "note": "shown once: give it to the member"}


@app.put("/api/team/users/{name}/repos")
def team_repos(name: str, repos: list[str]) -> dict:
    team.require_admin()
    try:
        team.update_repos(name, repos)
    except KeyError:
        raise HTTPException(404, f"no member {name}")
    team.log("member_repos", None, name=name, repos=repos)
    return {"name": name, "repos": repos}


class EmailReq(BaseModel):
    email: str | None = None


@app.put("/api/team/users/{name}/email")
def team_email(name: str, body: EmailReq) -> dict:
    team.require_admin()
    try:
        team.set_email(name, body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, f"no member {name}")
    team.log("member_email", None, name=name, email=bool(body.email))
    return {"name": name, "email": body.email}


@app.post("/api/team/users/{name}/rotate")
def team_rotate(name: str) -> dict:
    team.require_admin()
    try:
        token = team.rotate(name)
    except KeyError:
        raise HTTPException(404, f"no member {name}")
    team.log("token_rotated", None, name=name)
    return {"name": name, "token": token}


@app.delete("/api/team/users/{name}")
def team_remove(name: str) -> dict:
    team.require_admin()
    try:
        team.remove_user(name)
    except KeyError:
        raise HTTPException(404, f"no member {name}")
    team.log("member_removed", None, name=name)
    return {"removed": name}


@app.get("/api/team/audit")
def team_audit(user: str | None = None, action: str | None = None, limit: int = Query(100, le=1000)) -> list[dict]:
    team.require_admin()
    return team.audit(user, action, limit)


@app.post("/api/team/events")
def team_event(body: EventReq) -> dict:
    """The CLI reports what it actually wrote (e.g. after `tracebase copy`), for the audit log."""
    team.require_repo(body.repo)
    team.log(body.action[:40], body.repo, **body.detail)
    return {"ok": True}


# ---------------------------------------------------------------- data & privacy: what is stored, export, delete
# Admin only, and every call is written to the audit log (team mode).
class PurgeReq(BaseModel):
    match: str
    service: str | None = None
    dry_run: bool = False


class WipeReq(BaseModel):
    confirm: str
    keep_team: bool = True


@app.get("/api/data/summary")
def data_summary() -> dict:
    team.require_admin()
    return privacy.summary()


@app.post("/api/data/prune")
def data_prune() -> dict:
    team.require_admin()
    res = privacy.prune()
    team.log("data_prune", None, **res)
    return {"deleted": res}


@app.delete("/api/data/repos/{name:path}")
def data_forget_repo(name: str) -> dict:
    team.require_admin()
    try:
        res = privacy.forget_repo(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    team.log("data_forget_repo", name, **res["deleted"])
    return res


@app.post("/api/data/repos/{name:path}/restore")
def data_restore_repo(name: str) -> dict:
    team.require_admin()
    try:
        res = privacy.restore_repo(name)
    except KeyError:
        raise HTTPException(404, f"{name} is not a forgotten repo")
    team.log("data_restore_repo", name)
    return res


@app.delete("/api/data/services/{service}")
def data_forget_service(service: str, exclude: bool = False) -> dict:
    team.require_admin()
    try:
        res = privacy.forget_service(service, exclude=exclude)
    except ValueError as e:
        raise HTTPException(400, str(e))
    team.log("data_forget_service", None, service=service, excluded=exclude, **res["deleted"])
    return res


@app.post("/api/data/services/{service}/restore")
def data_restore_service(service: str) -> dict:
    team.require_admin()
    try:
        res = privacy.restore_service(service)
    except KeyError:
        raise HTTPException(404, f"{service} is not an excluded service")
    team.log("data_restore_service", None, service=service)
    return res


@app.post("/api/data/purge")
def data_purge(body: PurgeReq) -> dict:
    team.require_admin()
    try:
        res = privacy.purge_matching(body.match, body.service, body.dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not body.dry_run:  # the audit entry must not keep the very value that was erased
        team.log("data_purge", None, service=body.service, match_length=len(body.match), **res["deleted"])
    return res


@app.get("/api/data/export")
def data_export(service: str | None = None, match: str | None = None) -> StreamingResponse:
    team.require_admin()
    if match is not None and len(match.strip()) < 4:
        raise HTTPException(400, "give at least 4 characters to match")
    team.log("data_export", None, service=service, match=bool(match))
    name = f"tracebase-export-{service or 'all'}.ndjson"
    return StreamingResponse(privacy.export(service, match), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/data/wipe")
def data_wipe(body: WipeReq) -> dict:
    team.require_admin()
    if body.confirm != "delete everything":
        raise HTTPException(400, 'send {"confirm": "delete everything"} to wipe all stored data')
    res = privacy.wipe(keep_team=body.keep_team)
    team.log("data_wipe", None, keep_team=body.keep_team)
    return res


SDK = Path(__file__).parent / "static" / "tracebase.js"


@app.get("/sdk/tracebase.js", include_in_schema=False)
def sdk_js():
    """The browser SDK that connected projects load (<script src=".../sdk/tracebase.js" data-key="tbk_...">)."""
    return FileResponse(SDK, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*",
                                 "Cross-Origin-Resource-Policy": "cross-origin"})


dist = settings.frontend_dist.resolve()
if (dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        f = (dist / full_path).resolve()
        if full_path and f.is_file() and str(f).startswith(str(dist)):
            return FileResponse(f)
        return FileResponse(dist / "index.html")
