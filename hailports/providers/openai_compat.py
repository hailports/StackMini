"""OpenAI-compatible chat, embeddings, and vision.

Covers OpenAI, xAI, OpenRouter, Groq, Together, and a local llama.cpp server.
"""

from __future__ import annotations

import base64

from hailports.httpjson import request_json
from hailports.providers.base import Reply, message_text, usage_or_estimate


class OpenAICompatProvider:
    def __init__(self, label: str, base_url: str, model: str, api_key: str | None = None, embed_model: str = "", embed_base_url: str = "", timeout: float = 30.0) -> None:
        self.label = label
        self.provider = label
        self.base_url = base_url.rstrip("/")
        self.model = model or "local"
        self.api_key = api_key or ""
        self.embed_model = embed_model or self.model
        self.embed_base_url = (embed_base_url or "").rstrip("/")
        self.timeout = timeout
        self.supports_embed = True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _root(self) -> str:
        root = self.base_url
        if root.endswith("/v1"):
            return root[: -len("/v1")]
        return root

    def _v1(self) -> str:
        return _with_v1(self.base_url)

    def health(self) -> tuple[bool, str]:
        try:
            request_json(self._v1() + "/models", None, self._headers(), min(self.timeout, 5), self.api_key, "GET")
            return True, "models endpoint answered"
        except Exception:
            pass
        try:
            request_json(self._root() + "/health", None, {}, min(self.timeout, 3), self.api_key, "GET")
            return True, "health endpoint answered"
        except Exception as exc:
            what = getattr(exc, "what", None) or str(exc)
            return False, what

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2) -> Reply:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        doc = request_json(
            self._v1() + "/chat/completions",
            payload,
            self._headers(),
            self.timeout,
            self.api_key,
        )
        try:
            text = str(doc["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            from hailports.httpjson import ProviderError
            raise ProviderError("The chat response had no message.", "Check the model name.") from exc
        usage = doc.get("usage") or {}
        prompt_tokens, completion_tokens = usage_or_estimate(
            message_text(messages),
            text,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return Reply(text, self.label, self.model, prompt_tokens, completion_tokens, doc)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        payload = {"model": self.embed_model, "input": texts}
        base = _with_v1(self.embed_base_url or self.base_url)
        try:
            doc = request_json(base + "/embeddings", payload, self._headers(), min(self.timeout, 8), self.api_key)
            rows = doc.get("data") or []
            vectors = [list(row["embedding"]) for row in rows]
        except Exception:
            return None
        if len(vectors) != len(texts):
            return None
        return vectors

    def vision(self, prompt: str, png: bytes, max_tokens: int = 512) -> Reply:
        encoded = base64.b64encode(png).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ],
        }]
        return self.chat(messages, max_tokens=max_tokens)


def _with_v1(base: str) -> str:
    root = base.rstrip("/")
    if root.endswith("/v1"):
        return root
    return root + "/v1"
