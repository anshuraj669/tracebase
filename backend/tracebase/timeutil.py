"""Parse human time expressions ("yesterday 18:00", "last night", "2h", ISO dates)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%S"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime(FMT)


def parse_since(expr: str | None, ref: datetime | None = None) -> datetime | None:
    if not expr:
        return None
    ref = ref or now()
    s = expr.strip().lower()
    m = re.fullmatch(r"(?:last\s+)?(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)(?:\s+ago)?", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return ref - delta
    if s in ("last hour", "past hour"):
        return ref - timedelta(hours=1)
    if s in ("last day", "past day", "last 24h"):
        return ref - timedelta(days=1)
    if s in ("last week", "past week", "this week"):
        return ref - timedelta(days=7)
    if s in ("today", "this morning"):
        return ref.replace(hour=0, minute=0, second=0)
    if s in ("last night", "tonight", "overnight"):
        return (ref - timedelta(days=1)).replace(hour=18, minute=0, second=0)
    m = re.fullmatch(r"(today|yesterday)(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", s)
    if m:
        base = ref if m.group(1) == "today" else ref - timedelta(days=1)
        h = int(m.group(2) or 0)
        if m.group(4) == "pm" and h < 12:
            h += 12
        if m.group(4) == "am" and h == 12:
            h = 0
        return base.replace(hour=h, minute=int(m.group(3) or 0), second=0)
    return parse_ts(expr)


_TS_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S", "%d/%b/%Y:%H:%M:%S %z", "%Y-%m-%d", "%b %d %H:%M:%S",
]


def parse_ts(value: str | float | int | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000
        return datetime.fromtimestamp(v)
    s = str(value).strip().replace("Z", "+0000")
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)
    for f in _TS_FORMATS:
        try:
            dt = datetime.strptime(s, f)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        if dt.year == 1900:
            dt = dt.replace(year=now().year)
        return dt.replace(microsecond=0)
    return None
