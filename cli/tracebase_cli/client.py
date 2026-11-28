"""HTTP client for the Tracebase server (the box that indexed your repos and runs Ollama)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

DEFAULT_URL = "http://localhost:8080"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tracebase" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    """URL + token from `tracebase login`, readable only by this user (agents read it, so no token in their config)."""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(CONFIG)


class TracebaseError(RuntimeError):
    pass


class Client:
    def __init__(self, url: str | None = None, timeout: float = 900, token: str | None = None) -> None:
        cfg = load_config()
        self.url = (url or os.environ.get("TRACEBASE_URL") or cfg.get("url") or DEFAULT_URL).rstrip("/")
        self.token = token or os.environ.get("TRACEBASE_TOKEN") or (cfg.get("token") if cfg.get("url", self.url).rstrip("/") == self.url else None)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.http = httpx.Client(base_url=self.url, timeout=httpx.Timeout(timeout, connect=5), headers=headers)

    def _req(self, method: str, path: str, **kw) -> dict | list:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise TracebaseError(f"Cannot reach Tracebase at {self.url}. Is it running? "
                                 f"(set TRACEBASE_URL if it runs elsewhere)") from e
        except httpx.TimeoutException as e:
            raise TracebaseError("Tracebase took too long to answer (the local LLM may be overloaded).") from e
        if r.status_code == 401:
            raise TracebaseError(f"{self.url} is a team server and needs your token: run `tracebase login <token>`"
                                 + (" (the saved token was rejected)" if self.token else ""))
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise TracebaseError(f"{r.status_code}: {detail}")
        return r.json()

    def health(self) -> dict:
        return self._req("GET", "/api/health")

    def repos(self) -> list[dict]:
        return self._req("GET", "/api/code/repos")

    def search(self, q: str, repo: str | None = None, language: str | None = None, limit: int = 8) -> dict:
        params = {k: v for k, v in {"q": q, "repo": repo, "language": language, "limit": limit}.items() if v}
        return self._req("GET", "/api/code/search", params=params)

    def read(self, repo: str, path: str, start: int | None = None, end: int | None = None) -> dict:
        params = {k: v for k, v in {"repo": repo, "path": path, "start": start, "end": end}.items() if v}
        return self._req("GET", "/api/code/file", params=params)

    def ask(self, question: str, repo: str | None = None) -> dict:
        return self._req("POST", "/api/code/ask", json={"question": question, "repo": repo})

    def examples(self, task: str, project: dict | None = None, languages: list[str] | None = None,
                 frameworks: list[str] | None = None, limit: int = 8) -> dict:
        return self._req("POST", "/api/reuse/examples", json={
            "task": task, "project": project, "languages": languages, "frameworks": frameworks, "limit": limit})

    def bundle(self, repo: str, paths: list[str], project: dict | None = None, symbol: str | None = None,
               renames: list[str] | None = None) -> dict:
        return self._req("POST", "/api/reuse/bundle", json={"repo": repo, "paths": paths, "project": project,
                                                            "symbol": symbol, "renames": renames or []})

    # ---- team mode
    def me(self) -> dict:
        return self._req("GET", "/api/team/me")

    def members(self) -> list[dict]:
        return self._req("GET", "/api/team/users")

    def add_member(self, name: str, repos: list[str], role: str = "member", email: str | None = None) -> dict:
        return self._req("POST", "/api/team/users", json={"name": name, "repos": repos, "role": role, "email": email})

    def set_member_email(self, name: str, email: str | None) -> dict:
        return self._req("PUT", f"/api/team/users/{name}/email", json={"email": email})

    # connected projects
    def projects(self) -> list[dict]:
        return self._req("GET", "/api/projects")

    def add_project(self, name: str, platform: str) -> dict:
        return self._req("POST", "/api/projects", json={"name": name, "platform": platform})

    def rotate_project(self, name: str) -> dict:
        return self._req("POST", f"/api/projects/{name}/rotate")

    def remove_project(self, name: str, delete_data: bool = False) -> dict:
        return self._req("DELETE", f"/api/projects/{name}", params={"delete_data": str(delete_data).lower()})

    def test_project(self, name: str) -> dict:
        return self._req("POST", f"/api/projects/{name}/test")

    def set_member_repos(self, name: str, repos: list[str]) -> dict:
        return self._req("PUT", f"/api/team/users/{name}/repos", json=repos)

    def rotate_member(self, name: str) -> dict:
        return self._req("POST", f"/api/team/users/{name}/rotate")

    def remove_member(self, name: str) -> dict:
        return self._req("DELETE", f"/api/team/users/{name}")

    def audit(self, user: str | None = None, action: str | None = None, limit: int = 50) -> list[dict]:
        return self._req("GET", "/api/team/audit", params={k: v for k, v in
                                                           {"user": user, "action": action, "limit": limit}.items() if v})

    # ------------------------------------------------------------ Log Detective
    def errors(self, service: str | None = None, since: str | None = None, status: str | None = None,
               q: str | None = None, limit: int = 30) -> list[dict]:
        return self._req("GET", "/api/logs/groups", params={k: v for k, v in {
            "service": service, "since": since, "status": status, "q": q, "limit": limit}.items() if v})

    def error(self, gid: str) -> dict:
        return self._req("GET", f"/api/logs/groups/{gid}")

    def explain_error(self, gid: str, refresh: bool = False) -> dict:
        return self._req("POST", f"/api/logs/groups/{gid}/explain", params={"refresh": "true"} if refresh else None)

    def error_flags(self, gid: str) -> dict:
        return self._req("GET", f"/api/logs/groups/{gid}/flags")

    def rollback_flag(self, gid: str, change_id: int) -> dict:
        return self._req("POST", f"/api/logs/groups/{gid}/rollback", json={"change_id": change_id, "confirm": True})

    def flag_changes(self, feature: str | None = None, environment: str | None = None, since: str | None = None,
                     limit: int = 30) -> list[dict]:
        return self._req("GET", "/api/flags/changes", params={k: v for k, v in {
            "feature": feature, "environment": environment, "since": since, "limit": limit}.items() if v})

    def flag_where(self, feature: str) -> list[dict]:
        return self._req("GET", "/api/flags/where", params={"feature": feature})

    def set_error_status(self, gid: str, status: str) -> dict:
        return self._req("POST", f"/api/logs/groups/{gid}/status", json={"status": status})

    def ingest(self, text: str, service: str | None = None) -> dict:
        return self._req("POST", "/api/logs/ingest", params={"service": service} if service else None,
                         content=text.encode(), headers={"content-type": "text/plain"})

    def alerts(self, unread: bool = False) -> list[dict]:
        return self._req("GET", "/api/alerts", params={"unread": "true"} if unread else None)

    def read_alerts(self) -> dict:
        return self._req("POST", "/api/alerts/read", json={})

    # ------------------------------------------------------------ data & privacy (admin)
    def data_summary(self) -> dict:
        return self._req("GET", "/api/data/summary")

    def data_prune(self) -> dict:
        return self._req("POST", "/api/data/prune")

    def forget_repo(self, name: str) -> dict:
        return self._req("DELETE", f"/api/data/repos/{name}")

    def restore_repo(self, name: str) -> dict:
        return self._req("POST", f"/api/data/repos/{name}/restore")

    def forget_service(self, name: str, exclude: bool = False) -> dict:
        return self._req("DELETE", f"/api/data/services/{name}", params={"exclude": "true"} if exclude else None)

    def restore_service(self, name: str) -> dict:
        return self._req("POST", f"/api/data/services/{name}/restore")

    def purge(self, match: str, service: str | None = None, dry_run: bool = False) -> dict:
        return self._req("POST", "/api/data/purge", json={"match": match, "service": service, "dry_run": dry_run})

    def wipe(self, keep_team: bool = True) -> dict:
        return self._req("POST", "/api/data/wipe", json={"confirm": "delete everything", "keep_team": keep_team})

    def export(self, out, service: str | None = None, match: str | None = None) -> int:
        """Stream the NDJSON export into the file object `out`; returns the number of records."""
        params = {k: v for k, v in {"service": service, "match": match}.items() if v}
        n = 0
        try:
            with self.http.stream("GET", "/api/data/export", params=params) as r:
                if r.status_code >= 400:
                    r.read()
                    self._raise(r)
                for line in r.iter_lines():
                    if line:
                        out.write(line + "\n")
                        n += 1
        except httpx.ConnectError as e:
            raise TracebaseError(f"Cannot reach Tracebase at {self.url}. Is it running?") from e
        return max(n - 1, 0)  # minus the header line

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code == 401:
            raise TracebaseError(f"{self.url} is a team server and needs your token: run `tracebase login <token>`")
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        raise TracebaseError(f"{r.status_code}: {detail}")

    def event(self, action: str, repo: str | None, **detail) -> None:
        """Best effort: tell a team server what was written (audit log). Never fails the copy."""
        try:
            self._req("POST", "/api/team/events", json={"action": action, "repo": repo, "detail": detail})
        except TracebaseError:
            pass
