# Data & privacy

Tracebase indexes your source code and reads your production logs. This page lists exactly what it stores, where
it stores it, how long it keeps it, and how to export or delete it. It is written for whoever runs the server.

## Who is responsible for the data

- **You run Tracebase on your own machine, NAS or cloud account.** The data never leaves that machine, and no
  one else (including the Tracebase authors) can see it. You are the controller of whatever personal data your
  logs contain.
- **You run Tracebase for someone else** (a client's code on your server, a hosted service). You are then
  processing their data on their behalf: under GDPR or India's DPDP Act 2023 that means a written agreement
  (a DPA), telling them where the server is and who can reach it, and honouring export and deletion requests.
  The tools below cover the technical side. The paperwork is yours. Better: run it in **their** account.

## What is stored

Everything is in one SQLite database, `$TRACEBASE_DATA_DIR/tracebase.db` (in Docker, the `tracebase-data`
volume, mounted at `/data`).

| Kind | Tables | Contents | Kept |
|---|---|---|---|
| Code index | `repos`, `files`, `chunks`, `chunks_fts` | repo names and paths; file paths, languages, hashes; **your code**, split into functions/classes, with embeddings and a full-text index | while the repo is in the repos folder |
| Secrets findings | `secrets` | file, line, rule, a **masked** preview (`sk_live_ab****`) | while the line exists |
| Log lines | `log_events` | time, level, service, message + stack trace, **after redaction** | `LOG_RETENTION_DAYS` (30) |
| Error groups | `error_groups` | a redacted sample, stack, frames, the explanation | `ERROR_RETENTION_DAYS` (90) after last seen |
| Alerts | `alerts` | redacted title and body | `ALERT_RETENTION_DAYS` (90) |
| Team | `users`, `audit` | member name, role, repo patterns, a **SHA-256 hash** of the token (never the token); who did what | members: until removed. Audit: `AUDIT_RETENTION_DAYS` (365) |
| Internal | `log_offsets`, `kv` | how far each log file was read, settings | |

Not stored: your `.env` values (the secrets scan stores a masked preview of a line, not the value), the admin
token (it lives only in the environment), member tokens in clear, or anything about visitors.

On the user's machine, the CLI stores the server URL and token in `~/.config/tracebase/config.json` (mode 0600).
The web UI keeps the token in the browser's local storage.

## Redaction: personal data never reaches the database

Logs carry other people's data. Before a log line, stack trace, error sample or alert is stored, Tracebase replaces:

| Found | Becomes |
|---|---|
| email addresses | `[email]` |
| IPv4 / IPv6 addresses (except loopback) | `[ip]` |
| card numbers (Luhn-checked) | `[card]` |
| international phone numbers (`+…`) | `[phone]` |
| JWTs, `Authorization:` / `Bearer` / `Basic` credentials, cookies | `[jwt]`, `[redacted]` |
| API keys (Stripe, AWS, GitHub, GitLab, Slack, Google, OpenAI/Anthropic, SendGrid, Tracebase) | `[secret]` |
| passwords in URLs (`mysql://user:pass@…`) | `mysql://user:[redacted]@…` |
| values of `password`, `token`, `secret`, `api_key`, `session_id`, `otp`, `cvv`, `iban`… keys (`k=v`, `"k": "v"`, `'k' => 'v'`) | `[redacted]` |
| private key blocks | `[private-key]` |
| your own patterns (`TRACEBASE_REDACT_EXTRA='["CUST-\\d{6}"]'`) | `[redacted]` |

What explanations need is kept: messages, file paths, line numbers, function names, timestamps, order and request
ids. Errors that only differed by an email or an IP now fall into one group instead of one group per user.

- `TRACEBASE_REDACT=0` turns redaction off (logs you already scrub). `TRACEBASE_REDACT_IPS=0` keeps IPs.
- Redaction is pattern-based. It cannot know that `"name": "Asha Verma"` is a person. Use
  `TRACEBASE_REDACT_EXTRA` for identifiers specific to your app, and don't log what you don't need.
- **Upgrading:** data stored by a version without redaction (or with older rules) is redacted once, at startup.
  The database file is then rewritten (`VACUUM`), so the old text is not left in free pages.

## Encryption at rest

Set `TRACEBASE_DB_KEY` (or `TRACEBASE_DB_KEY_FILE`, for Docker secrets) and the database is encrypted with
SQLCipher (AES-256). A copied disk, volume, snapshot or backup can't be read without the key.

- An existing unencrypted database is encrypted in place on the first start with a key.
- **Keep the key safe** (a password manager). Without it the database cannot be opened. The code index can be
  rebuilt from the repos, but logs sent over HTTP cannot.
- A wrong key stops Tracebase at startup with `wrong TRACEBASE_DB_KEY`. It never overwrites the database.
- `deploy/setup.sh` generates a key for new servers and adds one to existing servers when you upgrade.
- Snapshots taken **before** encryption still hold the plain database: delete them once you have new ones.
- The Docker image includes SQLCipher. Outside Docker: `pip install sqlcipher3-binary` (Linux x86-64) or
  `sqlcipher3` with `libsqlcipher-dev`.
- Also encrypt the disk if you can (an encrypted EBS volume, LUKS on a NAS): it covers the repos and log folders too.

## Deleting and exporting (admin)

Every action below is admin-only and recorded in the audit log (team mode). Purges don't record the erased text.
Deleted rows are overwritten (`secure_delete`), and the write-ahead log is truncated afterwards.

| CLI | API | |
|---|---|---|
| `tracebase data` | `GET /api/data/summary` | what is stored, size, encryption, redaction, retention |
| `tracebase data export --service shop-api` | `GET /api/data/export?service=&match=` | everything about a service, or containing a text (NDJSON) |
| `tracebase data purge "CUST-10442"` | `POST /api/data/purge` `{match, service?, dry_run?}` | erase every log line, error and alert containing it |
| `tracebase data forget-service payments [--stop]` | `DELETE /api/data/services/{name}?exclude=` | delete a service's logs, errors, alerts (and drop new ones) |
| `tracebase data forget-repo client-a/site` | `DELETE /api/data/repos/{name}` | delete a repo's index and stop indexing it (the folder is untouched) |
| `tracebase data restore-repo …` / `restore-service …` | `POST …/restore` | undo the exclusion |
| `tracebase data prune` | `POST /api/data/prune` | apply the retention periods now |
| `tracebase data wipe` | `POST /api/data/wipe` `{"confirm": "delete everything"}` | delete everything (members kept unless `--no-keep-team`) |

The same is in the web UI under **Data & privacy**.

**Erasure request** ("delete everything about customer 10442"): `tracebase data export --match 10442` to show
them what was held, then `tracebase data purge 10442`. Their raw log files in `LOGS_DIR` and backups are
outside Tracebase. Delete them there too.

## What leaves the server

Nothing, unless you configure it:

- `TRACEBASE_ALERT_WEBHOOK`: alert titles and bodies (redacted) go to the URL you set. Off by default.
- `TRACEBASE_LLM_PROVIDER` / `LLM_BASE_URL`: code snippets and redacted error text go to that LLM server. Keep
  it local (Ollama on the same machine or network). Pointing it at a hosted API sends your code there.

## Hardening checklist for a public server

- [x] Team mode on (`ADMIN_TOKEN`): every API call needs a token. Members see only their repos and services.
- [x] HTTPS (Caddy), HSTS, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`. API responses are `no-store`.
- [x] `/api/health` tells visitors without a token only the version and that team mode is on.
- [x] Runs as an unprivileged user (uid 10001) on a read-only filesystem with all capabilities dropped. `/data`
      is mode 0700, the database files 0600.
- [x] No HTTP access log (URLs carry search terms). Log uploads are capped (`TRACEBASE_MAX_INGEST_MB`, 20).
- [x] No cross-site API access unless you list origins in `TRACEBASE_CORS_ORIGINS`.
- [x] Redaction on, retention on, database encrypted (`DB_KEY`).
- [ ] Encrypted disk, automatic snapshots, and the `DB_KEY` + `ADMIN_TOKEN` stored outside the server.
- [ ] Give out member tokens, not the admin token. Rotate any token that leaks (`tracebase team rotate`).
