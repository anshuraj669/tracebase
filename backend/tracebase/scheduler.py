"""Scheduler: re-indexes the repos folder (incremental), tails log files (and Docker logs if enabled), and deletes
records older than their retention period (at startup and nightly)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .code.indexer import index_all
from .config import settings
from .logs.collector import collect_docker, collect_files, docker_available
from . import privacy

log = logging.getLogger(__name__)
scheduler: BackgroundScheduler | None = None


def _safe_index() -> None:
    try:
        index_all()
    except Exception:
        log.exception("scheduled indexing failed")


def _safe(fn, *a):
    def inner():
        try:
            fn(*a)
        except Exception:
            log.exception("scheduled job %s failed", fn.__name__)
    return inner


def start() -> None:
    global scheduler
    if scheduler is not None or not settings.enable_scheduler:
        return
    scheduler = BackgroundScheduler(job_defaults={"max_instances": 1, "coalesce": True})
    scheduler.add_job(_safe_index, "interval", seconds=max(settings.watch_interval, 15), id="code:index",
                      next_run_time=datetime.now() + timedelta(seconds=2))
    scheduler.add_job(_safe(collect_files), "interval", seconds=max(settings.log_interval, 5), id="logs:files",
                      next_run_time=datetime.now() + timedelta(seconds=4))
    if docker_available():
        scheduler.add_job(_safe(collect_docker), "interval", seconds=max(settings.log_interval * 2, 15), id="logs:docker")
    scheduler.add_job(_safe(privacy.prune), "cron", hour=4, minute=30, id="privacy:retention",
                      next_run_time=datetime.now() + timedelta(seconds=30))
    scheduler.start()


def stop() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None


def jobs() -> list[dict]:
    if scheduler is None:
        return []
    return [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None} for j in scheduler.get_jobs()]
