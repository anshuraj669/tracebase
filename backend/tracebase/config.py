"""Runtime configuration, read from environment variables (TRACEBASE_*)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"TRACEBASE_{name}", default)


def _read_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        raise RuntimeError(f"TRACEBASE_DB_KEY_FILE: cannot read {path}: {e}") from e


@dataclass
class Settings:
    repos_dir: Path = field(default_factory=lambda: Path(_env("REPOS_DIR", "/volume1/repos")))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # Log Detective: log files dropped (or synced, or written) here are tailed and grouped
    logs_dir: Path = field(default_factory=lambda: Path(_env("LOGS_DIR", "/volume1/logs")))
    log_interval: int = field(default_factory=lambda: int(_env("LOG_INTERVAL", "15")))

    # Privacy. Retention: days to keep each kind of record (0 = keep forever). Pruned nightly and at startup.
    log_retention_days: int = field(default_factory=lambda: int(_env("LOG_RETENTION_DAYS", "30")))
    error_retention_days: int = field(default_factory=lambda: int(_env("ERROR_RETENTION_DAYS", "90")))
    alert_retention_days: int = field(default_factory=lambda: int(_env("ALERT_RETENTION_DAYS", "90")))
    audit_retention_days: int = field(default_factory=lambda: int(_env("AUDIT_RETENTION_DAYS", "365")))
    # Redaction of personal data and credentials in logs, before they are stored (see redact.py)
    redact_logs: bool = field(default_factory=lambda: _env("REDACT", "1") != "0")
    redact_ips: bool = field(default_factory=lambda: _env("REDACT_IPS", "1") != "0")
    redact_extra: str = field(default_factory=lambda: _env("REDACT_EXTRA", ""))
    # Encryption at rest (SQLCipher). Set a passphrase, or a file holding it (Docker secrets). See db.py.
    db_key: str = field(default_factory=lambda: _env("DB_KEY", "") or _read_file(_env("DB_KEY_FILE", "")))
    # Largest accepted POST /api/logs/ingest body
    max_ingest_mb: int = field(default_factory=lambda: int(_env("MAX_INGEST_MB", "20")))
    # Browser origins allowed to call the API from another site (comma-separated). Empty = same origin only.
    cors_origins: list[str] = field(default_factory=lambda: [o.strip() for o in _env("CORS_ORIGINS", "").split(",") if o.strip()])
    # Optional: push alerts to YOUR webhook (self-hosted ntfy, Slack...). Off by default: nothing leaves the machine.
    alert_webhook: str = field(default_factory=lambda: _env("ALERT_WEBHOOK", ""))

    # LLM adapter: "ollama", "openai" (any OpenAI-compatible server, e.g. the NAS LLM), or "none"
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "ollama"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://localhost:11434"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "llama3.1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT", "120")))

    # Embeddings: "hash" (built-in, zero-dependency) or "ollama" (e.g. nomic-embed-text)
    embed_provider: str = field(default_factory=lambda: _env("EMBED_PROVIDER", "hash"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "nomic-embed-text"))

    watch_interval: int = field(default_factory=lambda: int(_env("WATCH_INTERVAL", "60")))
    max_file_kb: int = field(default_factory=lambda: int(_env("MAX_FILE_KB", "512")))
    enable_scheduler: bool = field(default_factory=lambda: _env("SCHEDULER", "1") == "1")
    frontend_dist: Path = field(default_factory=lambda: Path(_env("FRONTEND_DIST", "../frontend/dist")))

    # Team mode: set an admin token to require a token on every API call and enable members + audit log
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracebase.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:  # the database holds code and logs: only the Tracebase user may open the folder
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # e.g. /volume1 not writable on a dev machine: log collection just finds nothing
            pass


settings = Settings()
