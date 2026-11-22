"""Secrets scan: flags leaked API keys, private keys, hard-coded passwords and committed .env files."""
from __future__ import annotations

import re
from dataclasses import dataclass

@dataclass
class Rule:
    name: str
    severity: str
    pattern: re.Pattern
    group: int = 0


RULES = [
    Rule("Stripe live secret key", "HIGH", re.compile(r"\b(sk|rk)_live_[0-9a-zA-Z]{10,}")),
    Rule("AWS access key id", "HIGH", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("AWS secret access key", "HIGH", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}")),
    Rule("GitHub token", "HIGH", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    Rule("GitLab token", "HIGH", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20}\b")),
    Rule("Slack token", "HIGH", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    Rule("Google API key", "HIGH", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    Rule("OpenAI/Anthropic API key", "HIGH", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{32,}")),
    Rule("Private key", "HIGH", re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    Rule("Hard-coded DB password", "HIGH", re.compile(
        r"""(?i)(db_?pass(word)?|database_password|mysql_password|pg_?password|'password'|"password")\s*(=>|=|:)\s*['"]([^'"\s$]{4,})['"]"""), 4),
    Rule("Password in connection URL", "HIGH", re.compile(r"(?i)\b(mysql|postgres(ql)?|mongodb(\+srv)?|redis|amqp)://[^:\s/]+:([^@\s]{3,})@"), 4),
    Rule("Plain-text password in config", "MEDIUM", re.compile(
        r"(?i)^\s*-?\s*([A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY)[A-Z0-9_]*)\s*[=:]\s*['\"]?([^\s'\"$#{}]{4,})"), 3),
]

PLACEHOLDERS = {"changeme", "change_me", "password", "secret", "example", "xxxx", "your_password", "null",
                "none", "true", "false", "test", "dummy", "placeholder", "redacted", "****", "env", "required"}


def mask(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "****"
    return value[:8] + "****"


def scan_text(path: str, text: str) -> list[dict]:
    findings: list[dict] = []
    is_env = path.rsplit("/", 1)[-1].startswith(".env") and not path.endswith((".example", ".sample", ".dist", ".template"))
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000:
            continue
        for rule in RULES:
            m = rule.pattern.search(line)
            if not m:
                continue
            secret = m.group(rule.group) or m.group(0)
            low = secret.lower().strip("'\"")
            if low in PLACEHOLDERS or "change" in low or low.startswith(("<", "your", "xxx")) or secret.startswith(("${", "env(", "getenv", "process.env", "os.environ")):
                continue
            sev = rule.severity
            if rule.name == "Plain-text password in config" and is_env:
                sev = "HIGH"
            preview = line.strip()
            if secret and secret in preview:
                preview = preview.replace(secret, mask(secret))
            findings.append({"line": i, "severity": sev, "rule": rule.name, "preview": preview[:160]})
            break  # one finding per line
    return findings


def suggestion(findings: list[dict]) -> str:
    if not findings:
        return "No secrets found."
    tips = ["move secrets to .env (git-ignored)"]
    if any("Stripe" in f["rule"] for f in findings):
        tips.append("rotate the Stripe key")
    if any("AWS" in f["rule"] for f in findings):
        tips.append("rotate the AWS credentials")
    if any("Private key" in f["rule"] for f in findings):
        tips.append("revoke and regenerate the private key")
    return "Suggested fix: " + " and ".join(tips) + "."
