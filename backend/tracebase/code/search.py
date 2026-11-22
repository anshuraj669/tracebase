"""Hybrid code search: FTS5 keyword (BM25) + vector similarity, plus exact regex/symbol search."""
from __future__ import annotations

import re
import time

from ..db import connect, rows
from ..embeddings import embedder, vector_index
from ..textutil import expand_query, stem, tokenize
from .chunker import detect_language
from ..team import sql_filter
from .sources import get_source

KEYWORD_WEIGHT = 0.55
SEMANTIC_WEIGHT = 0.45


def _fts_query(tokens: list[str]) -> str:
    terms = []
    for t in tokens:
        t = re.sub(r"[^\w]", "", t)
        if not t:
            continue
        terms.append(f'"{t}"')
        s = stem(t)
        if s != t and len(s) >= 3:
            terms.append(f'"{s}"*')
    return " OR ".join(dict.fromkeys(terms))


def _filters(repo: str | None, language: str | None) -> tuple[str, list]:
    where, params = [], []
    if repo:
        where.append("(c.repo = ? OR c.repo LIKE ?)")
        params += [repo, f"%/{repo}"]
    if language:
        where.append("c.language = ?")
        params.append(language.lower())
    scope, sp = sql_filter("c.repo")  # team mode: only this member's repos
    if scope:
        where.append(scope.removeprefix(" AND "))
        params += sp
    return (" AND " + " AND ".join(where)) if where else "", params


def search(q: str, repo: str | None = None, language: str | None = None, limit: int = 10) -> dict:
    t0 = time.time()
    conn = connect()
    base_tokens = tokenize(q)
    tokens = expand_query(base_tokens)
    where, params = _filters(repo, language)

    kw: dict[int, float] = {}
    fq = _fts_query(tokens)
    if fq:
        sql = (f"SELECT c.id, bm25(chunks_fts, 4.0, 2.0, 1.0, 1.5) AS s FROM chunks_fts "
               f"JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ? {where} ORDER BY s LIMIT 200")
        try:
            res = conn.execute(sql, [fq, *params]).fetchall()
        except Exception:
            res = []
        if res:
            best = -min(r["s"] for r in res) or 1.0
            kw = {r["id"]: (-r["s"]) / best for r in res}

    allowed = None
    if where:
        allowed = {r["id"] for r in conn.execute(f"SELECT c.id FROM chunks c WHERE 1=1 {where}", params)}
    qvec = embedder.embed(q, query=True)
    sem = dict(vector_index.search(qvec, k=200, allowed=allowed))

    # Boost: chunks matching original (non-expanded) query terms in symbol/path.
    ids = set(kw) | set(sem)
    if not ids:
        return {"results": [], "took_ms": int((time.time() - t0) * 1000), "query": q}
    meta = {r["id"]: r for r in rows(
        f"SELECT id, repo, path, language, symbol, kind, start_line, end_line, content FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
        tuple(ids))}
    scored = []
    idf = _idf(conn, base_tokens)
    total_idf = sum(idf.values()) or 1.0
    for cid, m in meta.items():
        s = KEYWORD_WEIGHT * kw.get(cid, 0.0) + SEMANTIC_WEIGHT * max(0.0, sem.get(cid, 0.0))
        # Boost chunks whose *symbol name* contains query words, weighted by rarity (IDF):
        # "refresh" in refresh() matters more than the ubiquitous "token".
        sym_toks = {stem(t) for t in tokenize(m["symbol"] or "")}
        if sym_toks:
            s += 0.3 * sum(w for t, w in idf.items() if t in sym_toks) / total_idf
        if m["kind"] == "module":
            s *= 0.8
        scored.append((s, m))
    scored.sort(key=lambda x: -x[0])

    out, per_file = [], {}
    for s, m in scored:
        key = (m["repo"], m["path"])
        if per_file.get(key, 0) >= 2:
            continue
        per_file[key] = per_file.get(key, 0) + 1
        out.append({
            "repo": m["repo"], "path": m["path"], "lines": [m["start_line"], m["end_line"]],
            "symbol": m["symbol"], "kind": m["kind"], "language": m["language"],
            "score": round(min(s, 1.0), 2), "preview": _preview(m["content"], base_tokens),
        })
        if len(out) >= limit:
            break
    return {"results": out, "took_ms": int((time.time() - t0) * 1000), "query": q}


def _idf(conn, tokens: list[str]) -> dict[str, float]:
    import math

    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 1
    out: dict[str, float] = {}
    for t in {stem(t) for t in tokens}:
        word = re.sub(r"[^\w]", "", t)
        if not word:
            continue
        try:
            df = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'tokens:"{word}"*',)).fetchone()[0]
        except Exception:
            df = 0
        out[t] = math.log(1 + n / (1 + df))
    return out


def _preview(content: str, tokens: list[str], n: int = 8) -> str:
    lines = content.split("\n")
    best, best_i = -1, 0
    toks = [t.lower() for t in tokens]
    for i, l in enumerate(lines):
        low = l.lower()
        sc = sum(1 for t in toks if t in low)
        if sc > best:
            best, best_i = sc, i
    start = max(0, best_i - 2)
    return "\n".join(lines[start:start + n])


def exact_search(pattern: str, repo: str | None = None, regex: bool = False, limit: int = 100) -> dict:
    """Regex/literal search over indexed chunks (line-accurate)."""
    t0 = time.time()
    try:
        rx = re.compile(pattern if regex else re.escape(pattern))
    except re.error as e:
        return {"error": f"invalid regex: {e}", "results": []}
    where, params = _filters(repo, None)
    # Prefilter with FTS on literal words when possible.
    words = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern if not regex else re.sub(r"\\.", " ", pattern))]
    conn = connect()
    if words and not regex:
        fq = " AND ".join(f'"{w}"' for w in words)
        cur = conn.execute(f"SELECT c.* FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? {where}",
                           [fq, *params])
    else:
        cur = conn.execute(f"SELECT c.* FROM chunks c WHERE 1=1 {where}", params)
    out, seen = [], set()
    for r in cur:
        for off, line in enumerate(r["content"].split("\n")):
            if rx.search(line):
                ln = r["start_line"] + off
                key = (r["repo"], r["path"], ln)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"repo": r["repo"], "path": r["path"], "line": ln, "text": line.strip()[:240], "symbol": r["symbol"]})
        if len(out) >= limit:
            break
    out.sort(key=lambda x: (x["repo"], x["path"], x["line"]))
    return {"results": out[:limit], "took_ms": int((time.time() - t0) * 1000)}


def read_code(repo: str, path: str, start: int | None = None, end: int | None = None, context: int = 0) -> dict:
    src = get_source(repo)
    text = src.read(path) if src else None
    if text is None:
        # fall back to indexed content (e.g. repo matched by suffix)
        scope, sp = sql_filter("repo")
        r = rows(f"SELECT repo FROM files WHERE path=? AND (repo=? OR repo LIKE ?){scope} LIMIT 1", (path, repo, f"%{repo}", *sp))
        if r:
            src = get_source(r[0]["repo"])
            repo = r[0]["repo"]
            text = src.read(path) if src else None
    if text is None:
        return {"error": f"file not found: {repo}/{path}"}
    lines = text.split("\n")
    s = max(1, (start or 1) - context)
    e = min(len(lines), (end or start or len(lines)) + context)
    if not start and not end:
        e = min(len(lines), 400)
    return {
        "repo": repo, "path": path, "language": detect_language(path), "start": s, "end": e,
        "total_lines": len(lines), "lines": [{"n": i, "text": lines[i - 1]} for i in range(s, e + 1)],
    }


def resolve_path(path: str, repo_hint: str | None = None) -> list[dict]:
    """Map a stack-trace path (absolute, container path, or partial) to indexed files."""
    p = path.replace("\\", "/").lstrip("./")
    parts = p.split("/")
    candidates: list[dict] = []
    # try progressively shorter suffixes
    for i in range(len(parts)):
        suffix = "/".join(parts[i:])
        if not suffix:
            continue
        q = "SELECT repo, path FROM files WHERE (path = ? OR path LIKE ?)"
        params: list = [suffix, f"%/{suffix}"]
        if repo_hint:
            q += " ORDER BY (repo = ? OR repo LIKE ?) DESC"
            params += [repo_hint, f"%{repo_hint}%"]
        found = rows(q + " LIMIT 5", tuple(params))
        if found:
            candidates = found
            break
    return candidates


def list_repos() -> list[dict]:
    import json as _json

    scope, sp = sql_filter("name")
    out = rows(f"SELECT * FROM repos WHERE 1=1{scope} ORDER BY name", tuple(sp))
    for r in out:
        r["languages"] = _json.loads(r["languages"] or "{}")
    return out
