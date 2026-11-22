# Deploying Tracebase on AWS (or any server)

One small Ubuntu server runs two containers: **Tracebase** and **Caddy**. Caddy gets a free HTTPS certificate
from Let's Encrypt and is the only thing reachable from the internet. Team mode is always on, so every visitor
needs a token.

```
internet ──443──▶ Caddy (HTTPS) ──▶ Tracebase :8080 (internal only)
                                      ├─ /srv/tracebase/repos   code to index (read-only)
                                      ├─ /srv/tracebase/logs    log files to watch (read-only)
                                      └─ volume tracebase-data  SQLite database
```

| File | |
|---|---|
| `docker-compose.prod.yml` | Tracebase + Caddy, healthcheck, log rotation |
| `Caddyfile` | HTTPS, http→https redirect, security headers |
| `.env.example` | settings; `setup.sh` turns it into `deploy/.env` with a fresh admin token |
| `setup.sh` | one-time server setup: Docker, swap, folders, firewall, start, repo-sync cron |
| `pull-repos.sh` | `git pull` of every repo in the repos folder (cron, every 15 min) |

## 1. Get a name for the server

You need a hostname that points at the server's IP for HTTPS. Options, free first:

| Option | Cost | Example | Notes |
|---|---|---|---|
| **sslip.io** | free, no sign-up | `13-233-10-20.sslip.io` | the IP is in the name. `setup.sh --domain auto` does it for you. Fine for a first test |
| **DuckDNS** | free | `tracebase-demo.duckdns.org` | sign in with GitHub/Google at duckdns.org, pick a name, paste the server IP. Best free option for a demo |
| **is-a.dev** | free | `tracebase.is-a.dev` | for developers' projects: you open a pull request on GitHub and wait for review (days) |
| **GitHub Student Pack** | free for a year (students) | `tracebase.me`, `.tech` | only if you have a student account |
| **A real domain** | about $10–15 a year | `tracebase.dev`, `tracebase.app`, `gettracebase.com` | Cloudflare Registrar (sells at cost), Porkbun or Namecheap. Best for a paid product: prospects trust it more |

`.dev` and `.app` are never free (they're sold by Google Registry). They only work over HTTPS, which this setup
already does. Avoid "free .tk / .ml" sites: Freenom, which ran them, has shut down.

## 2. Create the server (AWS Lightsail: the simplest AWS option)

1. Lightsail → **Create instance** → Linux/Unix → **OS Only → Ubuntu 24.04 LTS**.
   Pick the region closest to your users (e.g. Mumbai). Plan: **2 GB** minimum, **4 GB** recommended.
2. **Networking** tab → **Attach static IP** (otherwise the IP changes when the server restarts).
3. **Networking** tab → IPv4 firewall → **Add rule: HTTPS (443)**. SSH (22) and HTTP (80) are open by default.
4. Point your name at the static IP (the DuckDNS page, or an `A` record at your registrar).

EC2 works the same: Ubuntu 24.04, `t3.small`/`t3.medium`, an Elastic IP, and a security group allowing 22, 80, 443.

## 3. Copy Tracebase to the server

The project is private, so copy it straight from your laptop (no public GitHub needed):

```bash
# on your laptop, from the project folder (the key comes from Lightsail → Account → SSH keys)
rsync -az --delete -e "ssh -i ~/LightsailDefaultKey.pem" \
  --exclude .git --exclude node_modules --exclude .venv --exclude __pycache__ \
  --exclude frontend/dist --exclude demo/volume1 --exclude deploy/.env \
  ./ ubuntu@<STATIC-IP>:~/tracebase/
```

(Or push to a **private** GitHub repo and `git clone` it on the server with a deploy key.)

## 4. Run the setup

```bash
ssh -i ~/LightsailDefaultKey.pem ubuntu@<STATIC-IP>
cd ~/tracebase
sudo ./deploy/setup.sh --domain tracebase-demo.duckdns.org --demo   # demo repos + logs
# or: sudo ./deploy/setup.sh --domain auto --demo                   # no domain yet: <ip>.sslip.io
# or, for real use, without --demo (then add your repos, step 6)
```

It installs Docker, adds swap, creates `deploy/.env` with a random admin token, opens the firewall, builds and
starts everything. The first build takes a few minutes. Then open `https://<your domain>`: the browser asks for a
token.

```bash
sudo grep ADMIN_TOKEN ~/tracebase/deploy/.env     # your admin token: keep it to yourself
sudo grep DB_KEY ~/tracebase/deploy/.env          # the database encryption key: copy it to a password manager now
```

The database is encrypted with `DB_KEY` (SQLCipher). Without the key it can't be opened, so keep a copy off the
server. Upgrading a server set up before encryption existed: `setup.sh` adds a key and the existing database is
encrypted on the next start.

## 5. Give people access

Hand out **member tokens**, never the admin token. From your laptop:

```bash
export TRACEBASE_URL=https://tracebase-demo.duckdns.org TRACEBASE_TOKEN=<admin token>
tracebase team add priya --repos "freelance/*"          # prints her token, once
tracebase team add demo-visitor --repos shop-api         # a limited token for a prospect
tracebase team list
tracebase team rotate priya       # new token, the old one stops working
tracebase team remove demo-visitor
tracebase team audit              # who searched, copied, explained what
```

Without the CLI:

```bash
curl -s https://tracebase-demo.duckdns.org/api/team/users -H "Authorization: Bearer <admin token>" \
  -H "Content-Type: application/json" -d '{"name": "priya", "repos": ["freelance/*"]}'
```

They paste their token in the web page, or run `tracebase login <token> --url https://…` on their machine.
They can also connect their AI agent with `tracebase connect claude`.

## 6. Your real code and logs

```bash
# repos: clone into /srv/tracebase/repos (use a read-only deploy key per repo); pull-repos.sh keeps them updated
sudo git clone git@github.com:acme/shop-api.git /srv/tracebase/repos/shop-api
# logs: from your app servers, send them with a member token that covers that service
tail -n 500 /var/log/shop/laravel.log | curl -s -X POST "https://tracebase-demo.duckdns.org/api/logs/ingest?service=shop-api" \
  -H "Authorization: Bearer <token>" -H "Content-Type: text/plain" --data-binary @-
```

Or rsync/scp log files into `/srv/tracebase/logs`: they're tailed automatically.
If you switch from `--demo` to your own data, set `REPOS_DIR` / `LOGS_DIR` in `deploy/.env` back to
`/srv/tracebase/…` and re-run `sudo ./deploy/setup.sh`.

## Day to day

```bash
cd ~/tracebase/deploy
docker compose -f docker-compose.prod.yml ps               # status (Tracebase shows "healthy")
docker compose -f docker-compose.prod.yml logs -f          # logs
sudo ./setup.sh                                            # after copying new code: rebuild + restart (keeps .env)
```

- **Backups:** Lightsail → Snapshots → enable automatic snapshots (the database is in the Docker volume on that
  disk). The repos can always be cloned again. Snapshots of an encrypted database need `DB_KEY` to restore.
  Delete snapshots from before encryption was turned on: they hold the plain database.
- **Data & privacy:** `tracebase data` shows what is stored. `tracebase data purge <text>` erases a customer's
  data. Retention, redaction and encryption are set in `deploy/.env`. See [../PRIVACY.md](../PRIVACY.md).
- **Costs:** a 2–4 GB Lightsail server is about $12–24 a month, plus a domain if you buy one. AWS's free tier for
  new accounts changes over time, so check the current Lightsail / EC2 offers before you pick a plan.
- **AI explanations:** `LLM_PROVIDER=none` (the default here) uses the built-in rules, which work well on a
  small server. Ollama on AWS needs a big or GPU server (hundreds of dollars a month): not worth it for a demo.

## Security notes

- The admin token can do everything: it stays with you. Rotate a leaked member token with `tracebase team rotate`.
- Members only see the repos and log services their patterns allow, enforced on the server.
- A member can send logs for their own services, so give a public demo token access to a demo service only.
- Hosting a client's code on **your** server makes you responsible for it. For a client, run this in **their**
  AWS account.
- Personal data in logs (emails, IPs, card numbers, tokens) is redacted before it is stored. Add your own
  identifiers with `REDACT_EXTRA` in `deploy/.env`.
- The container runs as an unprivileged user on a read-only filesystem, with no HTTP access log.
  Lightsail encrypts its disks; on EC2 turn on EBS encryption; elsewhere use LUKS: that also covers `/srv/tracebase`.
