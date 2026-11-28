"""Log parsing: JSON logs, Laravel, Nginx/Apache, Python, Node, Java, Docker and generic formats.
Multi-line stack traces are attached to the event that precedes them."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..timeutil import parse_ts

LEVELS = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "information": "INFO", "notice": "INFO",
    "warn": "WARNING", "warning": "WARNING", "error": "ERROR", "err": "ERROR", "crit": "CRITICAL",
    "critical": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL",
    "fatal": "CRITICAL", "severe": "ERROR", "panic": "CRITICAL",
}


def norm_level(v: str | int | None) -> str:
    if v is None:
        return "INFO"
    if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
        n = int(v)  # bunyan/pino numeric levels
        return "CRITICAL" if n >= 60 else "ERROR" if n >= 50 else "WARNING" if n >= 40 else "INFO" if n >= 30 else "DEBUG"
    return LEVELS.get(str(v).strip().lower(), "INFO")


@dataclass
class Frame:
    path: str
    line: int
    func: str | None = None

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "func": self.func}


@dataclass
class Event:
    ts: datetime | None
    level: str
    service: str
    message: str
    stack: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def text(self) -> str:
        return self.message + ("\n" + "\n".join(self.stack) if self.stack else "")


TS = r"(\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
LVL = r"(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRIT|CRITICAL|ALERT|EMERG|EMERGENCY|FATAL|SEVERE|PANIC)"

PATTERNS: list[tuple[str, re.Pattern]] = [
    # Laravel: [2026-11-08 03:00:01] production.ERROR: message {"exception":...}
    ("laravel", re.compile(rf"^\[{TS}\]\s+(\w+)\.{LVL}:\s?(.*)$", re.I)),
    # 2026-11-08 03:00:01 [ERROR] checkout  message
    ("bracket", re.compile(rf"^{TS}\s+\[{LVL}\]\s+(?:([\w.\-]+)\s{{2,}}|([\w.\-]+):\s+)?(.*)$", re.I)),
    # [2026-11-08 03:00:01] [ERROR] message  /  [2026-..] ERROR message
    ("bracket2", re.compile(rf"^\[{TS}\]\s+\[?{LVL}\]?:?\s+(.*)$", re.I)),
    # Python logging: 2026-11-08 03:00:01,123 - name - ERROR - msg  |  2026-.. ERROR name: msg
    ("python", re.compile(rf"^{TS}\s+-\s+([\w.\-]+)\s+-\s+{LVL}\s+-\s+(.*)$", re.I)),
    # Java/Logback/Spring: 2026-11-08 03:00:01.123 ERROR 1234 --- [main] c.e.Svc : msg  /  ... ERROR [main] c.e.Svc - msg
    ("java", re.compile(rf"^{TS}\s+{LVL}\s+(?:\d+\s+---\s+)?\[([^\]]+)\]\s+([\w.$]+)\s*[:\-]\s+(.*)$", re.I)),
    # MySQL server log: 2026-11-08T03:00:00.512Z 0 [System] [MY-013172] [Server] message
    ("mysql", re.compile(rf"^{TS}\s+\d+\s+\[(System|Note|Warning|ERROR|Error)\]\s+(?:\[[^\]]+\]\s+)*(.*)$")),
    # Nginx error log: 2026/11/08 03:00:01 [error] 123#0: *1 message
    ("nginx_error", re.compile(rf"^(\d{{4}}/\d{{2}}/\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})\s+\[(\w+)\]\s+\d+#\d+:\s+(?:\*\d+\s+)?(.*)$")),
    # Combined access log
    ("access", re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "([A-Z]+) (\S+)[^"]*" (\d{3}) (\d+|-)(?: "[^"]*" "[^"]*")?')),
    # Generic: TS LEVEL message
    ("generic", re.compile(rf"^{TS}\s+{LVL}:?\s+(.*)$", re.I)),
    # Python bare: ERROR:name:msg
    ("pybare", re.compile(rf"^{LVL}:([\w.\-]+):(.*)$")),
    # Syslog: Nov  8 03:00:01 host prog[123]: message
    ("syslog", re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}) (\S+) ([\w\-./]+)(?:\[\d+\])?: (.*)$")),
]

CONTINUATION = re.compile(
    r"^(\s+at\s|\s*#\d+\s|\s+File \"|Traceback|\s+\.\.\.|Caused by:|\s{2,}|\t|\[stacktrace\]|"
    r"[\w.$]+(Exception|Error)(:|$)|\}|\"\}|Stack trace:|  thrown in)"
)

FRAME_PATTERNS = [
    re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[\w<>.]+))?'),  # Python
    re.compile(r"#\d+\s+(?P<path>[^\s(]+\.\w+)\((?P<line>\d+)\):\s*(?P<func>[\w\\:>\-$]+)?"),  # PHP trace
    re.compile(r"at\s+(?:(?P<func>[\w$.<>\[\] ]+?)\s+\()?(?P<path>(?:/|\.{1,2}/|[A-Za-z]:\\|webpack:|file:)?[\w./\\@\-]+\.(?:js|ts|mjs|cjs|tsx|jsx|php|py|rb|go)):(?P<line>\d+)(?::\d+)?\)?"),  # Node / "at file:line"
    re.compile(r"at\s+(?P<func>[\w$.<>]+)\((?P<path>[\w$\-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)"),  # Java
    re.compile(r"in (?P<path>/[\w./\-]+\.\w+)(?: on line |:)(?P<line>\d+)"),  # PHP "in /x.php on line 5" / "in /x.php:57"
    re.compile(r"(?P<path>[\w./\-]+\.(?:go|rs|rb|cs|c|cpp)):(?P<line>\d+)"),  # Go/Rust/Ruby
]


def extract_frames(text: str, limit: int = 15) -> list[Frame]:
    """The stack frames in your own code (libraries skipped), innermost first: the line that failed comes first."""
    frames: list[Frame] = []
    seen = set()
    python = False
    for line in text.splitlines():
        for i, pat in enumerate(FRAME_PATTERNS):
            m = pat.search(line)
            if not m:
                continue
            python = python or i == 0
            path = m.group("path").replace("file://", "").replace("webpack:///", "")
            if any(x in path for x in ("/vendor/", "node_modules/", "/site-packages/", "/usr/lib/", "/usr/local/lib/",
                                       "internal/", "<frozen")):
                break
            key = (path, int(m.group("line")))
            if key not in seen:
                seen.add(key)
                func = m.groupdict().get("func")
                frames.append(Frame(path, int(m.group("line")), func.strip() if func else None))
            break
    if python:  # Python prints the innermost call last ("most recent call last"); PHP, Java and Node print it first
        frames.reverse()
    return frames[:limit]


def service_from_source(source: str) -> str:
    name = source.replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".jsonl", ".json", ".log", ".txt", ".out", ".err"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    name = re.sub(r"[-_.]?\d{4}-?\d{2}-?\d{2}$", "", name)  # laravel-2026-11-08
    name = re.sub(r"[-_.](error|access|app|out|err|stderr|stdout)$", "", name)
    if name in ("laravel", "app", "application", "error", "access", "server", "output", "") and "/" in source:
        parent = source.replace("\\", "/").rsplit("/", 2)
        if len(parent) >= 2 and parent[-2] not in ("logs", "log", ""):
            return parent[-2]
    return name or "app"


# fields that never explain anything on their own (ids, process info): not repeated as context
_JSON_SKIP = {"logger", "request_id", "trace_id", "span_id", "pid", "hostname", "host", "v"}


def _parse_json(line: str, default_service: str) -> Event | None:
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(o, dict):
        return None
    def pick(keys):  # the first of these keys present: (key, value)
        return next(((k, o[k]) for k in keys if k in o), (None, None))
    k_ts, ts = pick(("ts", "time", "timestamp", "@timestamp", "datetime", "date", "t"))
    k_lv, level = pick(("level", "severity", "levelname", "log.level", "lvl", "level_name"))
    k_msg, msg = pick(("message", "msg", "error", "event", "text"))
    msg = msg or ""
    k_svc, svc = pick(("service", "app", "service.name", "logger", "name", "channel", "component"))
    svc = svc or default_service
    if isinstance(msg, dict):
        msg = json.dumps(msg)
    k_st, stack_val = pick(("stack", "stack_trace", "stacktrace", "exc_info", "trace", "exception"))
    used = {k_ts, k_lv, k_msg, k_svc, k_st, "err"}
    stack: list[str] = []
    if isinstance(stack_val, str):
        stack = stack_val.splitlines()
    elif isinstance(stack_val, list):
        stack = [str(s) for s in stack_val]
    elif isinstance(stack_val, dict):
        stack = json.dumps(stack_val).split("\\n")
    if isinstance(o.get("err"), dict):  # pino
        msg = msg or o["err"].get("message", "")
        stack = str(o["err"].get("stack", "")).splitlines()
    # the other structured fields (error="ReadTimeout", circuit=..., attempt=3) explain the line: keep them as
    # context next to the stack, not in the message, so lines that differ only by ids still group together
    context = [f"{k}={v}" for k, v in o.items() if k not in used and k not in _JSON_SKIP
               and isinstance(v, (str, int, float, bool)) and v != "" and len(str(v)) < 300]
    if context:
        stack = ["context: " + ", ".join(context)[:1000]] + stack
    return Event(parse_ts(ts), norm_level(level), str(svc), str(msg), stack)


def parse_line(line: str, default_service: str) -> Event | None:
    s = line.rstrip("\n")
    if s.startswith("{") and s.endswith("}"):
        e = _parse_json(s, default_service)
        if e:
            return e
    for name, pat in PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        g = m.groups()
        if name == "laravel":
            msg = g[3]
            stack: list[str] = []
            jm = re.search(r'\{"exception":"\[object\] \((.*)$', msg)
            if jm:
                stack = [jm.group(1)]
            cm = re.search(r'\s\{".*$', msg)
            if cm:  # strip Laravel's JSON context from the message, keep it in the stack
                if not jm:
                    stack = [msg[cm.start():].strip()]
                msg = msg[: cm.start()].strip() or msg
            return Event(parse_ts(g[0]), norm_level(g[2]), default_service, msg, stack)
        if name == "bracket":
            svc = g[2] or g[3] or default_service
            return Event(parse_ts(g[0]), norm_level(g[1]), svc, g[4].strip())
        if name == "bracket2":
            return Event(parse_ts(g[0]), norm_level(g[1]), default_service, g[2].strip())
        if name == "python":
            return Event(parse_ts(g[0]), norm_level(g[2]), default_service, g[3].strip())
        if name == "java":
            cls = g[3].rsplit(".", 1)[-1]
            return Event(parse_ts(g[0]), norm_level(g[1]), default_service, f"{cls}: {g[4].strip()}")
        if name == "mysql":
            return Event(parse_ts(g[0]), norm_level({"system": "info", "note": "info"}.get(g[1].lower(), g[1])), default_service, g[2].strip())
        if name == "nginx_error":
            return Event(parse_ts(g[0]), norm_level(g[1]), default_service, g[2].strip())
        if name == "access":
            status = int(g[4])
            lvl = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
            return Event(parse_ts(g[1]), lvl, default_service, f'{g[2]} {g[3]} -> {status}')
        if name == "generic":
            return Event(parse_ts(g[0]), norm_level(g[1]), default_service, g[2].strip())
        if name == "pybare":
            return Event(None, norm_level(g[0]), g[1] or default_service, g[2].strip())
        if name == "syslog":
            lvl = "ERROR" if re.search(r"\b(error|failed|fatal|panic)\b", g[3], re.I) else "INFO"
            return Event(parse_ts(g[0]), lvl, g[2], g[3].strip())
    return None


def parse_text(text: str, source: str = "", default_service: str | None = None) -> list[Event]:
    svc = default_service or service_from_source(source)
    events: list[Event] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        # docker --timestamps prefix: 2026-11-08T03:00:01.123456789Z <line>
        dm = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d{9}Z (.*)$", raw)
        docker_ts = None
        if dm:
            docker_ts, raw = parse_ts(dm.group(1) + "Z"), dm.group(2)
        ev = None
        if not (events and CONTINUATION.match(raw)):
            ev = parse_line(raw, svc)
        if ev is None:
            if events and (CONTINUATION.match(raw) or not re.match(r"^\d{4}[-/]\d{2}", raw)):
                # continuation of previous event (stack trace etc.)
                prev = events[-1]
                if len(prev.stack) < 200:
                    prev.stack.append(raw.rstrip())
                # Python/Node: the final "XError: msg" line is the real message
                if re.match(r"^[\w.$]*(Error|Exception)\b.*:", raw) and (prev.message.startswith("Traceback") or not prev.message):
                    prev.message = raw.strip()
                continue
            lvl = "ERROR" if re.search(r"\b(error|exception|fatal|failed|traceback)\b", raw, re.I) else "INFO"
            ev = Event(docker_ts, lvl, svc, raw.strip())
        elif docker_ts and ev.ts is None:
            ev.ts = docker_ts
        ev.source = source
        if ev.message.startswith("Traceback"):
            ev.stack.append(ev.message)
        events.append(ev)
    # fill missing timestamps from neighbours
    last = None
    for e in events:
        if e.ts is None:
            e.ts = last
        last = e.ts or last
    return events
