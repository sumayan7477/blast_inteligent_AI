"""
Local-only LLM client for Ollama.

Guarantees
----------
* The endpoint must resolve to this machine (``localhost`` / ``127.0.0.1`` /
  ``::1``); any other host raises at construction. There is NO cloud fallback:
  if Ollama or the model is unavailable, callers get ``LLMUnavailable`` and
  must answer without an LLM.
* HTTP proxies from the environment are ignored and redirects are refused, so
  a request can never be routed off the machine.
* Prompt/response contents are never logged.

Uses only the standard library (urllib) - no extra dependency.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import config

log = logging.getLogger("llm_service")


class LLMUnavailable(Exception):
    """The local LLM cannot be used. ``code`` is one of the values below; messages contain no prompt data."""

    CODES = ("connection_error", "timeout", "model_missing", "http_error", "bad_response")

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not allowed for the local LLM", headers, fp)


class OllamaClient:
    """Minimal client for a local Ollama server (``/api/tags`` and ``/api/chat``)."""

    STATUS_CACHE_SECONDS = 15.0

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None) -> None:
        url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("Ollama URL must use http or https")
        self.base_url = config.assert_local_url(url)
        self.model = model or config.OLLAMA_MODEL
        self.timeout = timeout or config.OLLAMA_TIMEOUT_SECONDS
        # Empty ProxyHandler: ignore HTTP(S)_PROXY so traffic cannot leave the machine.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    # -- transport ---------------------------------------------------------------
    def _request(self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("error", "")
            except (ValueError, AttributeError):
                pass
            if exc.code == 404 and "model" in str(detail).lower():
                raise LLMUnavailable("model_missing", f"model '{self.model}' is not installed in Ollama") from None
            raise LLMUnavailable("http_error", f"Ollama returned HTTP {exc.code}") from None
        except (socket.timeout, TimeoutError):
            raise LLMUnavailable("timeout", "Ollama did not respond in time") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise LLMUnavailable("timeout", "Ollama did not respond in time") from None
            raise LLMUnavailable("connection_error", f"cannot reach Ollama at {self.base_url}") from None
        except (ConnectionError, OSError):
            raise LLMUnavailable("connection_error", f"cannot reach Ollama at {self.base_url}") from None
        try:
            return json.loads(body)
        except ValueError:
            raise LLMUnavailable("bad_response", "Ollama returned invalid JSON") from None

    # -- status ------------------------------------------------------------------
    def model_matches(self, name: str) -> bool:
        want = self.model if ":" in self.model else f"{self.model}:latest"
        have = name if ":" in name else f"{name}:latest"
        return have == want

    def list_models(self) -> list[str]:
        data = self._request("/api/tags", timeout=min(self.timeout, 5.0))
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LLMUnavailable("bad_response", "unexpected /api/tags response")
        return [m.get("name") or m.get("model") for m in models if isinstance(m, dict) and (m.get("name") or m.get("model"))]

    def status(self, *, use_cache: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if use_cache and self._status_cache and now - self._status_cache[0] < self.STATUS_CACHE_SECONDS:
            return self._status_cache[1]
        out: dict[str, Any] = {"provider": "ollama", "base_url": self.base_url, "local_only": True,
                               "cloud_fallback": False, "model": self.model, "reachable": False,
                               "model_available": False, "available_models": [], "error": None}
        try:
            models = self.list_models()
            out.update(reachable=True, available_models=sorted(models)[:50],
                       model_available=any(self.model_matches(m) for m in models))
            if not out["model_available"]:
                out["error"] = "model_missing"
        except LLMUnavailable as exc:
            out["error"] = exc.code
        self._status_cache = (now, out)
        return out

    # -- generation ----------------------------------------------------------------
    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False, temperature: float = 0.0,
             max_tokens: int = 600) -> str:
        """Single non-streaming chat completion. Raises ``LLMUnavailable``; never falls back elsewhere."""
        st = self.status(use_cache=True)
        if not st["reachable"]:
            raise LLMUnavailable(st["error"] or "connection_error", f"Ollama is not reachable at {self.base_url}")
        if not st["model_available"]:
            raise LLMUnavailable("model_missing", f"model '{self.model}' is not installed (run: ollama pull {self.model})")
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": False, "keep_alive": "10m",
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 4096},
        }
        if json_mode:
            payload["format"] = "json"
        data = self._request("/api/chat", payload)
        content = (data.get("message") or {}).get("content") if isinstance(data, dict) else None
        if not isinstance(content, str):
            raise LLMUnavailable("bad_response", "Ollama response had no message content")
        return strip_reasoning(content)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_reasoning(content: str) -> str:
    """Remove reasoning-model scratchpads (e.g. deepseek-r1 ``<think>...</think>``).

    Only the final answer is kept and then goes through the normal grounding checks.
    An unterminated ``<think>`` (output cut off mid-reasoning) yields an empty answer,
    which callers reject - the scratchpad is never shown or used as an answer.
    """
    text = _THINK_BLOCK.sub("", content)
    if re.search(r"<think>", text, re.I):
        text = text.split("<think>", 1)[0] if not text.lower().startswith("<think>") else ""
    return text.strip()
