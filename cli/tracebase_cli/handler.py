"""Send a Python app's logs and errors to Tracebase.

    import logging
    from tracebase_cli.handler import TracebaseHandler, capture_uncaught

    handler = TracebaseHandler(url="https://your-tracebase.example.com", key=os.environ["TRACEBASE_KEY"])
    logging.getLogger().addHandler(handler)       # WARNING and above by default
    capture_uncaught(handler)                     # crashes too

Events are batched and sent from a background thread, so logging never blocks a request. The key (tbk_...) is
the project's write-only ingest key from Tracebase's "Connect a project" page. Standard library only.
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import sys
import threading
import traceback
import urllib.request
from datetime import datetime, timezone

LEVELS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error",
          logging.CRITICAL: "critical"}
# attributes every LogRecord has: anything else came from `extra={...}` and is sent as context
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


class TracebaseHandler(logging.Handler):
    def __init__(self, url: str, key: str, level: int = logging.WARNING, release: str = "",
                 batch: int = 50, interval: float = 2.0, timeout: float = 10.0):
        super().__init__(level)
        if not key.startswith("tbk_"):
            raise ValueError("key must be a Tracebase project key (tbk_...)")
        self.endpoint = url.rstrip("/") + "/api/logs/ingest"
        self.key, self.batch, self.interval, self.timeout = key, batch, interval, timeout
        self.release_tag = release  # not `release`: logging.Handler.release() is its lock
        self._q: queue.Queue = queue.Queue(maxsize=5000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tracebase-handler", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stack: list[str] = []
            ctx = {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}
            ctx.update({"logger": record.name} if record.name != "root" else {})
            if self.release_tag:
                ctx["release"] = self.release_tag
            if ctx:
                stack.append("context: " + ", ".join(f"{k}={str(v)[:120]}" for k, v in ctx.items()))
            if record.exc_info:
                stack += "".join(traceback.format_exception(*record.exc_info)).rstrip().splitlines()
            event = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                     "level": LEVELS.get(record.levelno, "info"), "message": record.getMessage()[:2000], "stack": stack}
            self._q.put_nowait(event)
        except queue.Full:
            pass  # Tracebase unreachable for a long time: drop rather than grow without limit
        except Exception:
            self.handleError(record)

    def _send(self, events: list[dict]) -> None:
        req = urllib.request.Request(self.endpoint, data=json.dumps({"events": events}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Tracebase-Key": self.key})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as e:  # never raise into the app; say it once on stderr
            print(f"[tracebase] could not send {len(events)} log events: {e}", file=sys.stderr)

    def _drain(self) -> list[dict]:
        out = []
        while len(out) < self.batch:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            while events := self._drain():
                self._send(events)

    def flush(self) -> None:
        while events := self._drain():
            self._send(events)

    def close(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self.flush()
        super().close()


def capture_uncaught(handler: TracebaseHandler) -> None:
    """Also send crashes (uncaught exceptions in the main thread and in threads), then keep Python's usual output."""
    log = logging.getLogger("tracebase.uncaught")
    log.addHandler(handler)
    log.propagate = False
    previous, previous_thread = sys.excepthook, threading.excepthook

    def hook(tp, value, tb):
        if not issubclass(tp, KeyboardInterrupt):
            log.critical("Uncaught %s: %s", tp.__name__, value, exc_info=(tp, value, tb))
            handler.flush()
        previous(tp, value, tb)

    def thread_hook(args):
        log.critical("Uncaught %s in thread %s: %s", args.exc_type.__name__, getattr(args.thread, "name", "?"),
                     args.exc_value, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    sys.excepthook, threading.excepthook = hook, thread_hook
