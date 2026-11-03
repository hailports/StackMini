"""One stats line for every reply, in every mode."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Stats:
    provider: str
    mode: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    escalated: bool
    saved_tokens: int

    def line(self) -> str:
        escalated = "yes" if self.escalated else "no"
        saved = max(0, int(self.saved_tokens))
        return (
            f"hailports-stats provider={self.provider} mode={self.mode} model={self.model} "
            f"in={int(self.prompt_tokens)} out={int(self.completion_tokens)} "
            f"escalated={escalated} saved={saved}"
        )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
