"""Vector index: embeddings stored as float32 blobs in SQLite, cosine similarity in numpy.

Default provider is a zero-dependency feature-hashing embedder (stemmed identifier words,
synonym-expanded, plus bigrams). It runs anywhere and is deterministic. Swap to a real
embedding model with TRACEBASE_EMBED_PROVIDER=ollama.
"""
from __future__ import annotations

import hashlib
import logging

import httpx
import numpy as np

from .config import settings
from .textutil import expand_query, stem, tokenize

log = logging.getLogger(__name__)
DIM = 512


def _h(token: str) -> tuple[int, float]:
    d = hashlib.blake2b(token.encode(), digest_size=8).digest()
    idx = int.from_bytes(d[:4], "little") % DIM
    sign = 1.0 if d[4] & 1 else -1.0
    return idx, sign


def _hash_embed(text: str, expand: bool) -> np.ndarray:
    toks = [stem(t) for t in tokenize(text)]
    if expand:
        toks = [stem(t) for t in expand_query(toks)]
    v = np.zeros(DIM, dtype=np.float32)
    if not toks:
        return v
    counts: dict[str, int] = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    for t, c in counts.items():
        i, s = _h("w:" + t)
        v[i] += s * (1.0 + np.log(c))
    for a, b in zip(toks, toks[1:]):
        i, s = _h(f"b:{a}_{b}")
        v[i] += 0.5 * s
    n = np.linalg.norm(v)
    return v / n if n else v


class Embedder:
    def __init__(self) -> None:
        self.provider = settings.embed_provider
        self._fallback = False

    @property
    def name(self) -> str:
        return "hash" if self.provider == "hash" or self._fallback else f"{self.provider}:{settings.embed_model}"

    def embed(self, text: str, *, query: bool = False) -> np.ndarray:
        if self.provider == "ollama" and not self._fallback:
            try:
                r = httpx.post(
                    f"{settings.llm_base_url.rstrip('/')}/api/embeddings",
                    json={"model": settings.embed_model, "prompt": text[:8000]},
                    timeout=60,
                )
                r.raise_for_status()
                v = np.asarray(r.json()["embedding"], dtype=np.float32)
                n = np.linalg.norm(v)
                return v / n if n else v
            except Exception as e:  # pragma: no cover - depends on local server
                log.warning("embedding provider failed (%s); falling back to hash embeddings", e)
                self._fallback = True
        return _hash_embed(text, expand=query)


embedder = Embedder()


def to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class VectorIndex:
    """In-memory matrix of chunk embeddings, lazily (re)loaded from SQLite when dirty."""

    def __init__(self) -> None:
        self.ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self.matrix: np.ndarray = np.zeros((0, DIM), dtype=np.float32)
        self.dirty = True

    def mark_dirty(self) -> None:
        self.dirty = True

    def _load(self) -> None:
        from .db import connect

        cur = connect().execute("SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL")
        ids, vecs = [], []
        for r in cur:
            v = from_blob(r["embedding"])
            ids.append(r["id"])
            vecs.append(v)
        if vecs:
            dim = max(len(v) for v in vecs)
            vecs = [v if len(v) == dim else np.zeros(dim, dtype=np.float32) for v in vecs]
            self.matrix = np.vstack(vecs)
        else:
            self.matrix = np.zeros((0, DIM), dtype=np.float32)
        self.ids = np.asarray(ids, dtype=np.int64)
        self.dirty = False

    def search(self, qvec: np.ndarray, k: int = 50, allowed: set[int] | None = None) -> list[tuple[int, float]]:
        if self.dirty:
            self._load()
        if not len(self.ids) or self.matrix.shape[1] != qvec.shape[0]:
            return []
        scores = self.matrix @ qvec
        order = np.argsort(-scores)
        out: list[tuple[int, float]] = []
        for i in order:
            cid = int(self.ids[i])
            if allowed is not None and cid not in allowed:
                continue
            out.append((cid, float(scores[i])))
            if len(out) >= k:
                break
        return out


vector_index = VectorIndex()
