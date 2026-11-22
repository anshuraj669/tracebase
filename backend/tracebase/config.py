"""Runtime configuration, read from environment variables (TRACEBASE_*)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"TRACEBASE_{name}", default)


@dataclass
class Settings:
    repos_dir: Path = field(default_factory=lambda: Path(_env("REPOS_DIR", "/volume1/repos")))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # Log Detective: log files dropped (or synced, or written) here are tailed and grouped
    logs_dir: Path = field(default_factory=lambda: Path(_env("LOGS_DIR", "/volume1/logs")))
    log_retention_days: int = field(default_factory=lambda: int(_env("LOG_RETENTION_DAYS", "30")))
    log_interval: int = field(default_factory=lambda: int(_env("LOG_INTERVAL", "15")))
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
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # e.g. /volume1 not writable on a dev machine: log collection just finds nothing
            pass


settings = Settings()
