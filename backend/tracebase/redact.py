"""Redaction: strip personal data and credentials from log text BEFORE it is stored.

Production logs carry other people's data (your customers' emails, IPs, card numbers, session tokens).
Tracebase only needs the shape of an error to group and explain it, so every log line, stack trace, error
sample and alert is passed through `redact()` on the way in. What is removed never reaches the database.

On by default. Settings:
  TRACEBASE_REDACT=0           turn it off (e.g. logs that are already scrubbed)
  TRACEBASE_REDACT_IPS=0       keep IP addresses (on by default: an IP is personal data under GDPR)
  TRACEBASE_REDACT_EXTRA='["CUST-\\\\d{6}", "(?i)patient id: \\\\S+"]'   your own patterns (JSON list of regexes)
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from functools import lru_cache

from .config import settings

log = logging.getLogger(__name__)

# Bumped when the rules change in a way that should be re-applied to data stored by an older version.
VERSION = 1

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?\s*)(?:(bearer|basic|token|digest)\s+)?[^\s\"',;]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE = re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*[\"']?)[^\"'\n]+")
_KNOWN_KEYS = re.compile(
    r"\b(?:(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{10,}"          # Stripe
    r"|whsec_[0-9A-Za-z]{10,}"                                    # Stripe webhook secret
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"                                 # AWS access key id
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}"
    r"|glpat-[A-Za-z0-9_\-]{20}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"                              # Slack
    r"|AIza[0-9A-Za-z\-_]{35}"                                    # Google
    r"|sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{32,}"                     # OpenAI / Anthropic
    r"|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"              # SendGrid
    r"|tb_[A-Za-z0-9_\-]{30,})"                                   # Tracebase member tokens
)
_URL_CREDS = re.compile(r"\b([a-z][a-z0-9+.\-]*://[^:\s/@]+:)[^@\s/]+@", re.I)
# key=value / "key": "value" / key: value for credential-like keys
_SECRET_KV = re.compile(
    r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|passphrase|secret|token|api[_\-]?key|apikey|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|session[_\-]?id|sessid|laravel_session|csrf|otp|ssn|cvv|cvc|"
    r"card[_\-]?number|iban)[\w\-]*\b[\"']?\s*(?:=>|[:=])\s*[\"']?)"
    r"(?![\[<])([^\s\"'&,;}{)\]]+)"
)
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_CARD = re.compile(r"(?<![\d-])(?:[3-6]\d{3})(?:[ -]?\d{4}){2}(?:[ -]?\d{1,4}){1,2}(?![\d-])")
_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}[ \-.]?(?:\(\d{1,4}\)[ \-.]?)?\d{2,4}(?:[ \-.]?\d{2,4}){1,4}(?!\d)")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]|\.\d)")
_IPV6 = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:])")

KEEP_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "::1", "::"}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def _ipv4(m: re.Match) -> str:
    s = m.group(0)
    if s in KEEP_IPS:
        return s
    try:
        ipaddress.IPv4Address(s)
    except ValueError:
        return s
    return "[ip]"


def _ipv6(m: re.Match) -> str:
    s = m.group(0)
    # "12:30:45" (a time) and "Db::add" (a PHP static call) look like IPv6: require a digit and a real address shape
    if s in KEEP_IPS or not any(c.isdigit() for c in s) or ("::" not in s and s.count(":") != 7):
        return s
    try:
        ipaddress.IPv6Address(s)
    except ValueError:
        return s
    return "[ip]"


@lru_cache(maxsize=4)
def _extra(spec: str) -> tuple[re.Pattern, ...]:
    if not spec.strip():
        return ()
    try:
        items = json.loads(spec)
        if isinstance(items, str):
            items = [items]
    except ValueError:
        items = [spec]
    out = []
    for p in items:
        try:
            out.append(re.compile(p))
        except re.error as e:
            log.error("TRACEBASE_REDACT_EXTRA: invalid pattern %r: %s", p, e)
    return tuple(out)


def enabled() -> bool:
    return settings.redact_logs


def redact(text: str | None) -> str:
    """Return `text` with credentials and personal data replaced by placeholders ([email], [ip], [card]...)."""
    if not text or not settings.redact_logs:
        return text or ""
    t = _PRIVATE_KEY.sub("[private-key]", text)
    t = _JWT.sub("[jwt]", t)
    t = _AUTH_HEADER.sub(lambda m: m.group(1) + (m.group(2) + " " if m.group(2) else "") + "[redacted]", t)
    t = _BEARER.sub(lambda m: m.group(1) + " [redacted]", t)
    t = _COOKIE.sub(lambda m: m.group(1) + "[redacted]", t)
    t = _KNOWN_KEYS.sub("[secret]", t)
    t = _URL_CREDS.sub(lambda m: m.group(1) + "[redacted]@", t)
    t = _SECRET_KV.sub(lambda m: m.group(1) + "[redacted]", t)
    t = _EMAIL.sub("[email]", t)
    t = _CARD.sub(_card, t)
    t = _PHONE.sub("[phone]", t)
    if settings.redact_ips:
        t = _IPV4.sub(_ipv4, t)
        if ":" in t:
            t = _IPV6.sub(_ipv6, t)
    for rx in _extra(settings.redact_extra):
        t = rx.sub("[redacted]", t)
    return t


def redact_lines(lines: list[str]) -> list[str]:
    if not settings.redact_logs:
        return lines
    # Multi-line secrets (private keys) span lines: redact the block as a whole, then split again.
    joined = "\n".join(lines)
    out = redact(joined)
    return out.split("\n") if out != joined else lines
