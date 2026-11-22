"""Git history helpers (work on working clones and on the bare mirrors kept by Backup Guardian)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import settings


def _run(args: list[str], cwd: Path, timeout: int = 30) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def find_git_dir(repo: str) -> Path | None:
    """Working clone in the repos dir (Phase 3 adds Backup Guardian's bare mirrors)."""
    from ..privacy import is_excluded_repo
    if is_excluded_repo(repo):
        return None
    p = settings.repos_dir / repo
    if (p / ".git").exists():
        return p
    return None


def history(repo: str, path: str | None = None, since: str | None = None, limit: int = 20,
            line: int | None = None) -> dict:
    d = find_git_dir(repo)
    if d is None:
        return {"error": f"no git history available for {repo} (not a git clone and no mirror found)"}
    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s"
    if line and path:
        # who last touched this line (git blame) + the commit
        try:
            out = _run(["blame", "-L", f"{line},{line}", "--porcelain", "HEAD", "--", path], d)
        except RuntimeError as e:
            return {"error": str(e)}
        sha = out.split()[0]
        show = _run(["show", "--stat", "--format=" + fmt, "--date=iso", sha], d)
        head, _, stat = show.partition("\n")
        h, short, author, date, subject = head.split("\x1f")
        return {"repo": repo, "path": path, "line": line, "commits": [
            {"sha": short, "full_sha": h, "author": author, "date": date, "message": subject,
             "diff_summary": stat.strip().splitlines()[-1] if stat.strip() else ""}],
            "diff": _run(["show", "--format=", "-U2", sha, "--", path], d)[:4000]}
    args = ["log", f"-n{limit}", "--date=iso", f"--format={fmt}", "--shortstat"]
    if since:
        args.append(f"--since={since}")
    args.append("--all")
    if path:
        args += ["--", path]
    try:
        out = _run(args, d)
    except RuntimeError as e:
        return {"error": str(e)}
    commits = []
    for block in out.split("\n"):
        if "\x1f" in block:
            h, short, author, date, subject = block.split("\x1f")
            commits.append({"sha": short, "full_sha": h, "author": author, "date": date, "message": subject, "diff_summary": ""})
        elif block.strip() and commits:
            commits[-1]["diff_summary"] = block.strip()
    return {"repo": repo, "path": path, "commits": commits}
