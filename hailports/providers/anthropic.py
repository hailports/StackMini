"""Anthropic Messages API. This provider has no embeddings endpoint."""

from __future__ import annotations

import base64

from hailports.httpjson import ProviderError, request_json
from hailports.providers.base import Reply, message_text, usage_or_estimate


class AnthropicProvider:
    def __init__(self, label: str, base_url: str, model: str, api_key: str | None = None, timeout: float = 60.0) -> None:
        self.label = label or "anthropic"
        self.provider = self.label
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model or "claude-haiku"
        self.api_key = api_key or ""
        self.timeout = timeout
        self.supports_embed = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _messages_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/messages"
        return self.base_url + "/v1/messages"

    def health(self) -> tuple[bool, str]:
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            request_json(root + "/v1/models", None, self._headers(), min(self.timeout, 5), self.api_key, "GET")
            return True, "models endpoint answered"
        except Exception as exc:
            what = getattr(exc, "what", None) or str(exc)
            return False, what

    def _split(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        converted: list[dict] = []
        for message in messages:
            role = message.get("role") or "user"
            content = message.get("content")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else message_text([message]))
                continue
            if role not in ("user", "assistant"):
                role = "user"
            if isinstance(content, list):
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        blocks.append({"type": "text", "text": str(block.get("text") or "")})
                    elif block.get("type") == "image_url":
                        url = ((block.get("image_url") or {}).get("url")) or ""
                        if url.startswith("data:") and "," in url:
                            meta, data = url.split(",", 1)
                            media = "image/png"
                            if ":" in meta and ";" in meta:
                                media = meta.split(":", 1)[1].split(";", 1)[0]
                            blocks.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media, "data": data},
                            })
                converted.append({"role": role, "content": blocks or [{"type": "text", "text": ""}]})
            else:
                converted.append({"role": role, "content": str(content or "")})
        if not converted:
            converted = [{"role": "user", "content": ""}]
        return "\n".join(system_parts), converted

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2) -> Reply:
        system, converted = self._split(messages)
        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        doc = request_json(self._messages_url(), payload, self._headers(), self.timeout, self.api_key)
        parts = []
        for block in doc.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        if not parts and "content" not in doc:
            raise ProviderError("The chat response had no message.", "Check the model name.")
        text = "\n".join(parts)
        usage = doc.get("usage") or {}
        prompt_tokens, completion_tokens = usage_or_estimate(
            message_text(messages),
            text,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )
        return Reply(text, self.label, self.model, prompt_tokens, completion_tokens, doc)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None

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
