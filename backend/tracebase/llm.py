"""LLM adapter. Everything that talks to a language model goes through `LLM.chat`, so the
any OpenAI-compatible endpoint (llama.cpp, vLLM, LM Studio, hosted APIs) can be swapped in by config."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(self) -> None:
        self._last_check = 0.0
        self._available: bool | None = None

    @property
    def provider(self) -> str:
        return settings.llm_provider

    def available(self) -> bool:
        if self.provider == "none":
            return False
        if self._available is not None and time.time() - self._last_check < 30:
            return self._available
        self._last_check = time.time()
        try:
            base = settings.llm_base_url.rstrip("/")
            url = f"{base}/api/tags" if self.provider == "ollama" else f"{base}/v1/models"
            r = httpx.get(url, timeout=3, headers=self._headers())
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        return self._available

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None,
             temperature: float = 0.1, model: str | None = None, json_mode: bool = False,
             num_ctx: int = 8192) -> dict[str, Any]:
        """Returns {"content": str, "tool_calls": [{"name": str, "arguments": dict}]}."""
        if self.provider == "none":
            raise LLMUnavailable("LLM disabled")
        base = settings.llm_base_url.rstrip("/")
        model = model or settings.llm_model
        try:
            if self.provider == "ollama":
                body: dict[str, Any] = {
                    "model": model, "messages": messages, "stream": False,
                    "options": {"temperature": temperature, "num_ctx": num_ctx},
                }
                if tools:
                    body["tools"] = tools
                if json_mode:
                    body["format"] = "json"
                r = httpx.post(f"{base}/api/chat", json=body, timeout=settings.llm_timeout)
                r.raise_for_status()
                msg = r.json().get("message", {})
                calls = []
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        args = _safe_json(args)
                    calls.append({"name": fn.get("name"), "arguments": args})
                return {"content": msg.get("content") or "", "tool_calls": calls}
            # OpenAI-compatible
            body = {"model": model, "messages": _to_openai(messages), "temperature": temperature}
            if tools:
                body["tools"] = tools
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = httpx.post(f"{base}/v1/chat/completions", json=body, headers=self._headers(),
                           timeout=settings.llm_timeout)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            calls = [
                {"name": c["function"]["name"], "arguments": _safe_json(c["function"].get("arguments") or "{}"),
                 "id": c.get("id")}
                for c in msg.get("tool_calls") or []
            ]
            return {"content": msg.get("content") or "", "tool_calls": calls}
        except httpx.HTTPError as e:
            self._available = False
            raise LLMUnavailable(str(e)) from e

    def complete(self, prompt: str, system: str = "You are Tracebase, a concise senior DevOps engineer.") -> str:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}])["content"]


def _safe_json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _to_openai(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_call_id": m.get("tool_call_id", "call")})
        elif m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"id": c.get("id", f"call{i}"), "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": json.dumps(c["function"]["arguments"])}}
                for i, c in enumerate(m["tool_calls"])]})
        else:
            out.append(m)
    return out


llm = LLM()
