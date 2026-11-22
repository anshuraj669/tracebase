"""Pending copy plans: what Tracebase *would* write into the project, saved until the user approves it.

An AI agent (through MCP) or `tracebase copy --later` creates a plan; nothing is written until the user approves
it (in the agent's approval prompt or with `tracebase apply` in a terminal). A plan is applied exactly as it was
previewed, and only if the files it touches have not changed since."""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from . import reuse

DIR = Path(".tracebase") / "pending"


def _dir(root: Path) -> Path:
    return root / DIR


def save(root: Path, bundle: dict, changes: list[reuse.Change], request: str = "") -> dict:
    todo = [c for c in changes if c.new_text is not None]
    written = "\n".join(c.new_text or "" for c in todo)
    plan = {
        "id": secrets.token_hex(3),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request": request,
        "repo": bundle["repo"],
        "picked": [f["path"] for f in bundle["files"] if f.get("role") == "picked"],
        "adapted": bundle.get("adapted") or [],
        "changes": [asdict(c) for c in changes],
        "packages": reuse.packages_needed(bundle, written),
        "env": reuse.env_missing(root, bundle, written),
    }
    reuse.private_dir(root)  # backups and pending plans never get committed
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{plan['id']}.json").write_text(json.dumps(plan, indent=1))
    return plan


def load(root: Path, plan_id: str) -> dict:
    if not plan_id.isalnum():
        raise reuse.CopyError(f"bad plan id: {plan_id}")
    p = _dir(root) / f"{plan_id}.json"
    if not p.is_file():
        raise reuse.CopyError(f"no pending plan {plan_id} in {root} (run `tracebase pending` to list them)")
    return json.loads(p.read_text())


def pending(root: Path) -> list[dict]:
    d = _dir(root)
    plans = [json.loads(p.read_text()) for p in d.glob("*.json")] if d.is_dir() else []
    return sorted(plans, key=lambda p: p["created"])


def discard(root: Path, plan_id: str) -> None:
    (_dir(root) / f"{plan_id}.json").unlink(missing_ok=True)


def changes(plan: dict) -> list[reuse.Change]:
    return [reuse.Change(**c) for c in plan["changes"]]


def stale(root: Path, plan: dict) -> list[str]:
    """Files that changed since the plan was previewed (applying would overwrite those edits)."""
    out = []
    for c in changes(plan):
        if c.new_text is None:
            continue
        target = reuse.safe_target(root, c.target)
        now = target.read_text(errors="ignore") if target.exists() else None
        if now != c.old_text:
            out.append(c.target)
    return out


def summary(plan: dict) -> str:
    """Short, human-readable description of a plan (used in approval prompts)."""
    lines = [f"Copy from {plan['repo']} ({', '.join(plan['picked'])})"]
    for c in changes(plan):
        if c.new_text is None and c.kind != "manual":
            continue
        n = len((c.new_text or "").splitlines()) - len((c.old_text or "").splitlines())
        size = f"{len(c.new_text.splitlines())} lines" if c.kind == "create" else f"+{max(n, 0)} lines"
        what = {"create": "new file", "merge": "add " + ", ".join(c.added), "wire": "add wiring line",
                "manual": "you add by hand", "deps": "add " + ", ".join(c.added),
                "env": "document " + ", ".join(c.added)}.get(c.kind, c.kind)
        sized = c.kind not in ("manual", "deps", "env")
        lines.append(f"  {c.kind:6} {c.target}  ({what}{', ' + size if sized else ''})")
    if plan.get("adapted"):
        lines.append("  adapted to this project: " + "; ".join(plan["adapted"]))
    loose = [p["name"] for p in plan["packages"] if not p.get("added_to")]
    if loose:
        lines.append("  packages to install: " + ", ".join(loose))
    if plan["env"]:
        lines.append("  env vars to set: " + ", ".join(plan["env"]))
    return "\n".join(lines)


def apply(root: Path, plan: dict, only: set[str] | None = None) -> list[tuple[str, str]]:
    """Write the plan (or only the `only` targets). Returns [(note, target)]. Removes the plan afterwards."""
    bad = stale(root, plan)
    if bad:
        raise reuse.CopyError("these files changed after the preview, preview again: " + ", ".join(bad))
    backup = root / ".tracebase" / "backups" / time.strftime("%Y%m%d-%H%M%S")
    done = [(reuse.apply(root, c, backup), c.target) for c in changes(plan)
            if c.new_text is not None and (only is None or c.target in only)]
    discard(root, plan["id"])
    return done


def install_steps(packages: list[dict]) -> list[str]:
    """`npm install` once for packages added to package.json, `pip install x` for the rest…"""
    added = sorted({p["added_to"] for p in packages if p.get("added_to")})
    out = [f"run `{reuse.INSTALL_AFTER[m]}` ({', '.join(p['name'] for p in packages if p.get('added_to') == m)} "
           f"added to {m})" for m in added]
    return out + [f"install {p['name']}: {p['install']}" for p in packages if not p.get("added_to")]


def todo_text(plan: dict) -> list[str]:
    """What the user still has to do by hand after applying."""
    out = [f"{c.target}: add `{s['line'].strip()}`" + (f" (inside: {s['inside']})" if s.get("inside") else "")
           for c in changes(plan) for s in c.manual]
    out += install_steps(plan["packages"])
    if plan["env"]:
        out.append(f"add to .env: {', '.join(plan['env'])}")
    return out
