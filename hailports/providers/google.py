"""Google Gemini generateContent and embedContent APIs."""

from __future__ import annotations

import base64

from hailports.httpjson import ProviderError, request_json
from hailports.providers.base import Reply, message_text, usage_or_estimate


class GoogleProvider:
    def __init__(self, label: str, base_url: str, model: str, api_key: str | None = None, embed_model: str = "", timeout: float = 60.0) -> None:
        self.label = label or "google"
        self.provider = self.label
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = model or "gemini-2.0-flash"
        self.embed_model = embed_model or "text-embedding-004"
        self.api_key = api_key or ""
        self.timeout = timeout
        self.supports_embed = True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    def health(self) -> tuple[bool, str]:
        try:
            request_json(self.base_url + "/models", None, self._headers(), min(self.timeout, 5), self.api_key, "GET")
            return True, "models endpoint answered"
        except Exception as exc:
            what = getattr(exc, "what", None) or str(exc)
            return False, what

    def _contents(self, messages: list[dict]) -> tuple[dict | None, list[dict]]:
        system = None
        contents: list[dict] = []
        for message in messages:
            role = message.get("role") or "user"
            content = message.get("content")
            if role == "system":
                text = content if isinstance(content, str) else message_text([message])
                system = {"parts": [{"text": text}]}
                continue
            mapped = "model" if role == "assistant" else "user"
            parts = _parts(content)
            contents.append({"role": mapped, "parts": parts})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]
        return system, contents

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2) -> Reply:
        system, contents = self._contents(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = system
        url = self.base_url + "/models/" + self.model + ":generateContent"
        doc = request_json(url, payload, self._headers(), self.timeout, self.api_key)
        texts: list[str] = []
        try:
            for part in doc["candidates"][0]["content"]["parts"]:
                if "text" in part:
                    texts.append(str(part.get("text") or ""))
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("The chat response had no message.", "Check the model name.") from exc
        text = "\n".join(texts)
        usage = doc.get("usageMetadata") or {}
        prompt_tokens, completion_tokens = usage_or_estimate(
            message_text(messages),
            text,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
        )
        return Reply(text, self.label, self.model, prompt_tokens, completion_tokens, doc)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        vectors: list[list[float]] = []
        url = self.base_url + "/models/" + self.embed_model + ":embedContent"
        try:
            for text in texts:
                doc = request_json(
                    url,
                    {"content": {"parts": [{"text": text}]}},
                    self._headers(),
                    self.timeout,
                    self.api_key,
                )
                values = ((doc.get("embedding") or {}).get("values")) or []
                if not values:
                    return None
                vectors.append([float(v) for v in values])
        except Exception:
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


def _parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        return [{"text": str(content or "")}]
    parts: list[dict] = []
    for block in content:
        if isinstance(block, str):
            parts.append({"text": block})
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" or "text" in block and "image_url" not in block:
            parts.append({"text": str(block.get("text") or "")})
        elif block.get("type") == "image_url":
            url = ((block.get("image_url") or {}).get("url")) or ""
            if url.startswith("data:") and "," in url:
                meta, data = url.split(",", 1)
                media = "image/png"
                if ":" in meta and ";" in meta:
                    media = meta.split(":", 1)[1].split(";", 1)[0]
                parts.append({"inline_data": {"mime_type": media, "data": data}})
    return parts or [{"text": ""}]
