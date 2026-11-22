"""Code Q&A: retrieve the best chunks, let the LLM answer with repo/file:line citations.
Every citation in the answer is checked against the retrieved chunks; if the model invents one,
the grounded fallback answer is returned instead."""
from __future__ import annotations

import logging
import re

from ..db import row
from ..llm import LLMUnavailable, llm
from .search import search

log = logging.getLogger(__name__)

SYSTEM = """You are Tracebase, a private assistant that answers questions about the developer's own code.
You get numbered code excerpts retrieved from their repositories. Answer ONLY from these excerpts.
Cite every implementation as plain text repo/path:start-end exactly as given in the excerpt header (no markdown links). Never invent files.
List EVERY project (repo) in the excerpts that implements what was asked — one numbered item per project,
with its citation(s) and one line on how it works. Then add a one-line recommendation of the best approach.
If the excerpts do not answer the question, say so. Be concise: a short intro + numbered list."""

CITE = re.compile(r"([\w.\-]+(?:/[\w.\-]+)*\.[A-Za-z]{1,6})\s*:\s*(\d+)")


def _fallback(hits: list[dict]) -> str:
    if not hits:
        return "I couldn't find matching code in the indexed repositories."
    lines = [f"{i}. **{h['repo']}** — `{h['path']}` : {h['lines'][0]}-{h['lines'][1]} → `{h['symbol']}`"
             for i, h in enumerate(hits, 1)]
    return f"I found {len(hits)} likely matches:\n\n" + "\n".join(lines)


def ask(question: str, repo: str | None = None, k: int = 6) -> dict:
    res = search(question, repo=repo, limit=k)
    hits = res["results"]
    out = {"question": question, "sources": hits, "mode": "search"}
    if not hits or not llm.available():
        return {**out, "answer": _fallback(hits)}
    blocks = []
    for i, h in enumerate(hits, 1):
        ch = row("SELECT content FROM chunks WHERE repo=? AND path=? AND start_line=? LIMIT 1", (h["repo"], h["path"], h["lines"][0]))
        code = (ch["content"] if ch else h["preview"])[:1800]
        blocks.append(f"[{i}] {h['repo']}/{h['path']}:{h['lines'][0]}-{h['lines'][1]} ({h['symbol']}, {h['language']})\n```\n{code}\n```")
    try:
        text = llm.chat([{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"Question: {question}\n\nExcerpts:\n\n" + "\n\n".join(blocks)}],
                        temperature=0.1)["content"].strip()
    except LLMUnavailable as e:
        log.warning("LLM unavailable: %s", e)
        return {**out, "answer": _fallback(hits)}
    known_paths = {h["path"] for h in hits}
    for m in CITE.finditer(text):
        path = m.group(1)
        if not any(p.endswith(path) or path.endswith(p) for p in known_paths):
            log.info("LLM cited unknown file %s; returning grounded answer", path)
            return {**out, "answer": _fallback(hits)}
    return {**out, "answer": text or _fallback(hits), "mode": "llm"}
