"""Shared reply shape for every provider."""

from __future__ import annotations

from dataclasses import dataclass, field

from hailports.stats import Stats, estimate_tokens


@dataclass
class Reply:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    raw: dict = field(default_factory=dict)

    def with_stats(self, mode: str, escalated: bool, saved_tokens: int) -> "Reply":
        self.stats = Stats(
            provider=self.provider,
            mode=mode,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            escalated=escalated,
            saved_tokens=saved_tokens,
        )
        return self

    def render(self) -> str:
        stats = getattr(self, "stats", None)
        if stats is None:
            stats = Stats(self.provider, "", self.model, self.prompt_tokens, self.completion_tokens, False, 0)
        body = (self.text or "").rstrip()
        return body + "\n" + stats.line()


def usage_or_estimate(prompt_text: str, completion: str, prompt_tokens: int | None, completion_tokens: int | None) -> tuple[int, int]:
    pin = prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens >= 0 else estimate_tokens(prompt_text)
    pout = completion_tokens if isinstance(completion_tokens, int) and completion_tokens >= 0 else estimate_tokens(completion)
    return pin, pout


def message_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("text", None) and "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
    return "\n".join(parts)
