"""Tokenisation helpers shared by code search, embeddings and log grouping."""
from __future__ import annotations

import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "that", "this",
    "with", "as", "at", "by", "be", "do", "did", "does", "i", "we", "you", "my", "me", "where",
    "what", "which", "how", "show", "find", "code", "before", "every", "all", "any", "there",
    "from", "into", "are", "was", "were", "has", "have", "had", "can", "could", "would", "should",
    "implement", "implemented", "function", "functions", "file", "files", "project", "projects",
    "return", "self", "this", "var", "let", "const", "new", "public", "private", "protected",
    "static", "def", "true", "false", "null", "none", "if", "else", "then", "end",
}

# Light synonym expansion so natural-language queries meet code vocabulary.
SYNONYMS: dict[str, list[str]] = {
    "email": ["mail", "smtp", "mailer", "send"],
    "mail": ["email", "smtp", "mailer"],
    "resize": ["thumbnail", "scale", "width", "height", "image", "crop"],
    "image": ["img", "picture", "photo", "upload", "thumbnail"],
    "upload": ["file", "multipart", "store", "storage"],
    "login": ["auth", "authenticate", "signin", "session", "password"],
    "auth": ["login", "token", "jwt", "authenticate", "session"],
    "token": ["jwt", "auth", "refresh", "bearer"],
    "jwt": ["token", "refresh", "auth"],
    "refresh": ["rotate", "token", "renew"],
    "password": ["passwd", "pwd", "hash", "bcrypt", "reset"],
    "reset": ["forgot", "password", "recover"],
    "payment": ["stripe", "charge", "checkout", "invoice", "pay"],
    "stripe": ["payment", "webhook", "charge"],
    "webhook": ["hook", "callback", "signature", "event"],
    "database": ["db", "sql", "query", "mysql", "postgres", "sqlite"],
    "db": ["database", "sql", "query"],
    "delete": ["remove", "destroy", "drop"],
    "user": ["account", "member", "customer"],
    "cache": ["redis", "memcached", "ttl"],
    "discount": ["coupon", "promo", "voucher"],
    "coupon": ["discount", "promo", "voucher"],
    "order": ["checkout", "cart", "purchase"],
    "cart": ["basket", "order", "checkout"],
    "log": ["logger", "logging"],
    "retry": ["backoff", "attempt"],
    "config": ["settings", "env", "configuration"],
}


def split_identifier(word: str) -> list[str]:
    parts: list[str] = []
    for piece in word.split("_"):
        if piece:
            parts.extend(p for p in _CAMEL.split(piece) if p)
    return [p.lower() for p in parts]


def tokenize(text: str, keep_stop: bool = False) -> list[str]:
    out: list[str] = []
    for m in _WORD.finditer(text):
        w = m.group(0)
        subs = split_identifier(w)
        if len(subs) > 1:
            out.append(w.lower())
        out.extend(subs)
    if keep_stop:
        return out
    return [t for t in out if t not in STOPWORDS and len(t) > 1]


def stem(word: str) -> str:
    for suf in ("ings", "ing", "ers", "er", "ies", "es", "ed", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)] + ("y" if suf == "ies" else "")
    return word


def expand_query(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for t in tokens:
        for key in (t, stem(t)):
            for syn in SYNONYMS.get(key, []):
                if syn not in out:
                    out.append(syn)
    return out


def identifier_text(text: str) -> str:
    """Space-separated split identifiers, indexed in FTS so 'reset email' matches sendResetEmail."""
    seen: list[str] = []
    for t in tokenize(text, keep_stop=True):
        seen.append(t)
    return " ".join(seen)
