"""Thin Ollama client: generate, chat and embeddings over HTTP."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(
        self,
        host: str,
        model: str,
        embed_model: str,
        timeout: int = 900,
        think: bool = False,
        num_ctx: int = 8192,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout
        # Reasoning models (gemma4, qwen3, deepseek-r1) spend the whole token budget
        # on hidden thinking and return an empty string unless thinking is disabled.
        self.think = think
        # Ollama defaults to 4096, which silently truncates a long brief bundle
        # before the model can write a full section.
        self.num_ctx = num_ctx
        # rolling stats so a run can report real throughput per model
        self.calls = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.gen_seconds = 0.0

    def _record(self, data: dict[str, Any]) -> None:
        self.calls += 1
        self.prompt_tokens += int(data.get("prompt_eval_count") or 0)
        self.output_tokens += int(data.get("eval_count") or 0)
        self.gen_seconds += float(data.get("eval_duration") or 0) / 1e9

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "gen_seconds": round(self.gen_seconds, 1),
            "tokens_per_second": round(self.output_tokens / self.gen_seconds, 1)
            if self.gen_seconds
            else 0.0,
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.host}{path}"
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OllamaError(f"cannot reach Ollama at {url}: {exc}") from exc
        if resp.status_code != 200:
            raise OllamaError(f"Ollama {path} -> {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self._record(data)
        return data

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.4,
        num_predict: int = 2048,
        json_mode: bool = False,
        model: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "think": self.think,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        data = self._post("/api/generate", payload)
        text = (data.get("response") or "").strip()
        if not text:
            reason = data.get("done_reason", "unknown")
            raise OllamaError(
                f"empty response from model {payload['model']} (done_reason={reason}); "
                "if 'length', the model spent its budget on hidden reasoning - set think=false"
            )
        return text

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.4,
        num_predict: int = 2048,
        json_mode: bool = False,
        model: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "think": self.think,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        if json_mode:
            payload["format"] = "json"
        data = self._post("/api/chat", payload)
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            reason = data.get("done_reason", "unknown")
            raise OllamaError(f"empty chat response from model {payload['model']} (done_reason={reason})")
        return text

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Falls back to zero vectors if unavailable."""
        if not texts:
            return []
        try:
            data = self._post("/api/embed", {"model": self.embed_model, "input": texts})
            return data.get("embeddings") or []
        except OllamaError as exc:
            log.warning("embeddings unavailable (%s); skipping similarity checks", exc)
            return []

    def json_generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> Any:
        """Generate and parse JSON, tolerating markdown fences."""
        raw = self.generate(prompt, system=system, json_mode=True, **kwargs)
        return _parse_json(raw)


def _parse_json(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
