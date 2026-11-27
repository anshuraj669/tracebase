#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Live demo: a feature flag in self-hosted Flagsmith breaks checkout, Tracebase finds the flag and turns it off.

  docker compose -f docker-compose.yml -f docker-compose.flagsmith.yml up -d --build
  python3 demo/flags_demo.py setup        # Flagsmith: user, project, new_checkout flag, webhook to Tracebase, API key
  (restart Tracebase with the two values it prints, see below)
  python3 demo/flags_demo.py traffic      # the demo shop: checkouts every 2 s, reading the flag from Flagsmith
  python3 demo/flags_demo.py on           # or flip new_checkout in the Flagsmith dashboard: checkouts start failing
  tracebase errors && tracebase why E-<n> --rollback

Standard library only. Settings are saved to demo/.flags-demo.json (not committed).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / ".flags-demo.json"
CHECKOUT = HERE / "volume1/repos/shop-api/app/Services/CheckoutV2.php"
CONTROLLER = HERE / "volume1/repos/shop-api/app/Http/Controllers/CheckoutController.php"
FLAG = "new_checkout"


def call(method: str, url: str, body=None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} -> {e.code}: {e.read().decode(errors='replace')[:400]}")
    except (urllib.error.URLError, ConnectionError) as e:
        raise SystemExit(f"cannot reach {url}: {getattr(e, 'reason', e)}")


def results(data):
    return data.get("results", []) if isinstance(data, dict) else data


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))
    os.chmod(STATE, 0o600)


# ------------------------------------------------------------------ setup
def setup(a: argparse.Namespace) -> int:
    fs = a.flagsmith.rstrip("/") + "/api/v1"
    st = load()
    email, password = a.email, st.get("password") or secrets.token_urlsafe(14)
    st.update({"flagsmith": a.flagsmith.rstrip("/"), "email": email, "password": password})
    save(st)  # before signing up: a later failure must not lose the password
    try:
        token = call("POST", f"{fs}/auth/users/", {"email": email, "password": password, "first_name": "Demo",
                                                    "last_name": "Admin", "sign_up_type": "NO_INVITE"})["key"]
    except SystemExit as e:
        if "already exists" not in str(e) and "unique" not in str(e).lower():
            raise
        token = call("POST", f"{fs}/auth/login/", {"email": email, "password": password})["key"]
    h = {"Authorization": f"Token {token}"}

    orgs = results(call("GET", f"{fs}/organisations/", headers=h))
    org = next((o for o in orgs if o["name"] == "Acme Shop"), None) or call("POST", f"{fs}/organisations/", {"name": "Acme Shop"}, h)
    projects = results(call("GET", f"{fs}/projects/?organisation={org['id']}", headers=h))
    project = next((p for p in projects if p["name"] == "shop-api"), None) or \
        call("POST", f"{fs}/projects/", {"name": "shop-api", "organisation": org["id"]}, h)
    envs = results(call("GET", f"{fs}/environments/?project={project['id']}", headers=h))
    env = next((e for e in envs if e["name"] == "Production"), None) or \
        call("POST", f"{fs}/environments/", {"name": "Production", "project": project["id"]}, h)
    feats = results(call("GET", f"{fs}/projects/{project['id']}/features/?search={FLAG}", headers=h))
    feat = next((f for f in feats if f["name"] == FLAG), None) or call(
        "POST", f"{fs}/projects/{project['id']}/features/",
        {"name": FLAG, "default_enabled": False,
         "description": "New checkout: charge in the shopper's currency (app/Services/CheckoutV2.php)"}, h)

    secret = st.get("webhook_secret") or secrets.token_hex(24)
    hooks = results(call("GET", f"{fs}/environments/{env['api_key']}/webhooks/", headers=h))
    hook = next((w for w in hooks if w["url"] == a.webhook_url), None)
    if hook:
        call("PATCH", f"{fs}/environments/{env['api_key']}/webhooks/{hook['id']}/", {"enabled": True, "secret": secret}, h)
    else:
        call("POST", f"{fs}/environments/{env['api_key']}/webhooks/", {"url": a.webhook_url, "enabled": True, "secret": secret}, h)

    api_key = st.get("api_key")
    if not api_key:
        api_key = call("POST", f"{fs}/organisations/{org['id']}/master-api-keys/",
                       {"name": "Tracebase (flag rollbacks)", "is_admin": True}, h)["key"]
    st.update({"org_id": org["id"], "project_id": project["id"], "environment_id": env["id"], "env_key": env["api_key"],
               "feature_id": feat["id"], "webhook_secret": secret, "api_key": api_key})
    save(st)
    print(f"Flagsmith ready: {a.flagsmith}  (log in as {email} / {password})")
    print(f"  project shop-api, environment Production, flag {FLAG} (off), webhook -> {a.webhook_url}")
    print("\nGive Tracebase these two values, then restart it:\n")
    print(f"  FLAGSMITH_API_KEY={api_key}")
    print(f"  FLAGSMITH_WEBHOOK_SECRET={secret}")
    print("\n  e.g. put them in .env next to docker-compose.yml and run:\n"
          "  docker compose -f docker-compose.yml -f docker-compose.flagsmith.yml up -d tracebase")
    return 0


# ------------------------------------------------------------------ flip the flag (what you'd do in the dashboard)
def toggle(a: argparse.Namespace, on: bool) -> int:
    st = load()
    if not st:
        raise SystemExit("run `flags_demo.py setup` first")
    fs = st["flagsmith"] + "/api/v1"
    # signed in as the demo user, like a click in the dashboard (the change shows who made it)
    token = call("POST", f"{fs}/auth/login/", {"email": st["email"], "password": st["password"]})["key"]
    h = {"Authorization": f"Token {token}"}
    states = results(call("GET", f"{fs}/features/featurestates/?environment={st['environment_id']}&feature={st['feature_id']}", headers=h))
    state = next(s for s in states if s.get("identity") is None and s.get("feature_segment") is None)
    call("PATCH", f"{fs}/features/featurestates/{state['id']}/",
         {"enabled": on, "environment": state["environment"], "feature": state["feature"]}, h)
    print(f"{FLAG} turned {'on' if on else 'off'} in Production")
    return 0


# ------------------------------------------------------------------ the demo shop's traffic
def _line(path: Path, needle: str) -> int:
    return next(i for i, l in enumerate(path.read_text().splitlines(), 1) if needle in l)


def traffic(a: argparse.Namespace) -> int:
    st = load()
    if not st:
        raise SystemExit("run `flags_demo.py setup` first")
    fail = _line(CHECKOUT, "$rate  = $rates[$cart['currency']];")
    ctrl = _line(CONTROLLER, "$total = $this->carts->total($cart);")
    tb = a.tracebase.rstrip("/")
    th = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    order, end = 91000, time.time() + a.minutes * 60 if a.minutes else None
    print(f"shop-api traffic -> {tb} (reads {FLAG} from Flagsmith every {a.every}s; Ctrl-C to stop)")
    while end is None or time.time() < end:
        try:
            on = _tick(st, tb, th, fail, ctrl, a, order)
        except SystemExit as e:  # Tracebase or Flagsmith restarting: keep the shop running, try again
            print(f"  (waiting: {e})", flush=True)
            time.sleep(a.every)
            continue
        order += a.batch
        time.sleep(a.every)
    return 0


def _tick(st: dict, tb: str, th: dict, fail: int, ctrl: int, a: argparse.Namespace, order: int) -> bool:
    """One round of checkouts: read the flag from Flagsmith like the app's SDK would, send the logs to Tracebase."""
    flags = call("GET", st["flagsmith"] + "/api/v1/flags/", headers={"X-Environment-Key": st["env_key"]})
    on = any(f["feature"]["name"] == FLAG and f["enabled"] for f in flags)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = []
    for _ in range(a.batch):
        order += 1
        if on:  # the new checkout reads $cart['currency'], which carts created before the rollout don't have
            events.append({"ts": ts, "level": "error", "service": "shop-api",
                           "message": f'Undefined array key "currency" (order {order})',
                           "stack": [f"#0 /var/www/html/app/Services/CheckoutV2.php({fail}): "
                                     "Illuminate\\Foundation\\Bootstrap\\HandleExceptions->handleError()",
                                     f"#1 /var/www/html/app/Http/Controllers/CheckoutController.php({ctrl}): "
                                     "App\\Services\\CheckoutV2->total()", "#2 {main}"]})
        else:
            events.append({"ts": ts, "level": "info", "service": "shop-api", "message": f"Order {order} created"})
    res = call("POST", f"{tb}/api/logs/ingest?service=shop-api", {"events": events}, th)
    mark = "\033[31mERROR\033[0m" if on else "ok"
    print(f"  {ts[11:19]}  {FLAG}={'on ' if on else 'off'}  {a.batch} checkouts  {mark}"
          + (f"  new error group {', '.join(res['new_groups'])}" if res.get("new_groups") else ""), flush=True)
    return on


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create the Flagsmith project, flag, webhook and API key")
    s.add_argument("--flagsmith", default="http://localhost:8000")
    s.add_argument("--email", default="demo@tracebase.local")
    s.add_argument("--webhook-url", default="http://tracebase.internal:8080/api/flags/webhook",
                   help="where Flagsmith sends flag changes (http://host.docker.internal:8080/... for `make dev`)")
    t = sub.add_parser("traffic", help="send the demo shop's checkouts to Tracebase")
    t.add_argument("--tracebase", default=os.environ.get("TRACEBASE_URL", "http://localhost:8080"))
    t.add_argument("--token", default=os.environ.get("TRACEBASE_TOKEN", ""))
    t.add_argument("--every", type=float, default=2.0)
    t.add_argument("--batch", type=int, default=3)
    t.add_argument("--minutes", type=float, default=0, help="stop after this long (0 = until Ctrl-C)")
    sub.add_parser("on", help=f"turn {FLAG} on (as you would in the dashboard)")
    sub.add_parser("off", help=f"turn {FLAG} off")
    a = p.parse_args(argv)
    if a.cmd == "setup":
        return setup(a)
    if a.cmd == "traffic":
        try:
            return traffic(a)
        except KeyboardInterrupt:
            return 0
    return toggle(a, a.cmd == "on")


if __name__ == "__main__":
    sys.exit(main())
