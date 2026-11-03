"""Local-first routing.

Local does the cheap work and the heavy-context work: reading, summarizing,
retrieval, drafting, and compression. Hybrid mode calls the cloud only when
the complexity, confidence, or answer-length policy says so, and it sends a
short precomputed packet instead of the raw notes. API mode never calls a
local model. Local mode never calls the cloud.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from hailports.httpjson import ProviderError
from hailports.precompute import build_packet
from hailports.providers.base import Reply, message_text
from hailports.stats import estimate_tokens

CHEAP_TASKS = ("read", "summarize", "retrieve", "draft", "compress")
_CONFIDENCE = re.compile(r"confidence\s*[:=]\s*(0?\.\d+|1(?:\.0+)?)", re.I)
_UNSURE = ("i don't know", "i do not know", "not sure", "unsure", "cannot tell", "low confidence")


@dataclass
class Policy:
    confidence_floor: float = 0.55
    complexity_ceiling: int = 1
    max_local_output_chars: int = 900
    compact_char_budget: int = 1600


@dataclass
class EmbedOutcome:
    vectors: list[list[float]]
    provider: str
    fallback: bool
    prompt_tokens: int

    def line(self, mode: str) -> str:
        from hailports.stats import Stats
        return Stats(self.provider, mode, self.provider, self.prompt_tokens, 0, False, 0).line()


def infer_task(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("summarize", "summary", "tl;dr", "tldr")):
        return "summarize"
    if any(k in low for k in ("retrieve", "search my notes", "look up in notes")):
        return "retrieve"
    if low.startswith("draft") or "draft a" in low or "draft the" in low:
        return "draft"
    if "compress" in low or "compact this" in low:
        return "compress"
    if "read this" in low or "read the" in low:
        return "read"
    return "chat"


def complexity_score(text: str) -> int:
    low = text.lower()
    score = 0
    if any(word in low for word in ("implement", "refactor", "debug", "write a function", "write code")):
        score += 2
    if any(word in low for word in ("step by step", "compare", "tradeoff", "analyze", "prove")):
        score += 2
    if text.count("?") > 1:
        score += 1
    if len(text) > 4000 and score == 0:
        score += 0
    return score


def parse_confidence(text: str) -> float:
    match = _CONFIDENCE.search(text or "")
    if match:
        try:
            return max(0.0, min(1.0, float(match.group(1))))
        except ValueError:
            pass
    low = (text or "").lower()
    if any(phrase in low for phrase in _UNSURE):
        return 0.2
    return 0.85


def decide(mode: str, score: int, confidence: float, output_chars: int, policy: Policy) -> str:
    """Return 'local' or 'cloud'."""
    if mode == "local":
        return "local"
    if mode == "api":
        return "cloud"
    if score > policy.complexity_ceiling:
        return "cloud"
    if confidence < policy.confidence_floor:
        return "cloud"
    if output_chars > policy.max_local_output_chars:
        return "cloud"
    return "local"


def lexical_vectors(texts: list[str], dims: int = 32) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        buckets = [0.0] * dims
        for token in re.findall(r"[A-Za-z0-9']+", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            buckets[digest[0] % dims] += 1.0
        total = sum(buckets) or 1.0
        vectors.append([value / total for value in buckets])
    return vectors


class Router:
    def __init__(self, mode: str, local, cloud, policy: Policy | None = None) -> None:
        self.mode = mode if mode in ("local", "hybrid", "api") else "local"
        self.local = local
        self.cloud = cloud
        self.policy = policy or Policy()

    def _need_local(self):
        if self.local is None:
            raise ProviderError(
                "No local model is configured.",
                "Run hailports setup and choose local or hybrid, or switch the mode.",
            )
        return self.local

    def _need_cloud(self):
        if self.cloud is None:
            raise ProviderError(
                "No cloud provider is configured.",
                "Run hailports setup and set HAILPORTS_API_KEY, or switch to local mode.",
            )
        return self.cloud

    def _stamp(self, reply: Reply, escalated: bool, saved: int) -> Reply:
        return reply.with_stats(self.mode, escalated, max(0, saved))

    def compress(self, text: str) -> tuple[str, int]:
        budget = self.policy.compact_char_budget
        if len(text) <= budget:
            return text, 0
        try:
            reply = self._need_local().chat(
                [
                    {"role": "system", "content": "Compress the notes. Keep facts and numbers. Be short."},
                    {"role": "user", "content": text[:20000]},
                ],
                max_tokens=400,
            )
            compact = (reply.text or "").strip()[:budget]
        except ProviderError:
            compact = ""
        if not compact:
            compact = text[:budget]
        saved = estimate_tokens(text) - estimate_tokens(compact)
        return compact, max(0, saved)

    def complete(self, messages: list[dict], task: str | None = None, max_tokens: int = 512) -> Reply:
        full = message_text(messages)
        user = _last_user(messages) or full
        kind = task or infer_task(user)
        score = complexity_score(user if kind == "chat" else user)
        if self.mode == "api":
            reply = self._need_cloud().chat(messages, max_tokens=max_tokens)
            return self._stamp(reply, False, 0)
        if self.mode == "local":
            reply = self._need_local().chat(messages, max_tokens=max_tokens)
            return self._stamp(reply, False, 0)

        saved = 0
        working = messages
        # Heavy context is reduced on this machine before any cloud call.
        # Cheap tasks still let the local model read the original text.
        cloud_first = decide(self.mode, score, 1.0, 0, self.policy) == "cloud"
        if cloud_first and len(full) > self.policy.compact_char_budget:
            working, saved = build_packet(self, full)
        elif (not cloud_first) and kind in CHEAP_TASKS:
            local_reply = self._need_local().chat(messages, max_tokens=max_tokens)
            confidence = parse_confidence(local_reply.text)
            choice = decide(self.mode, score, confidence, len(local_reply.text or ""), self.policy)
            if choice == "local":
                return self._stamp(local_reply, False, 0)
            digest = (local_reply.text or "").strip()
            saved = max(0, estimate_tokens(full) - estimate_tokens(digest))
            packet = [{"role": "user", "content": "Precomputed notes:\n" + digest + "\n\nRequest:\n" + _short_request(user)}]
            cloud = self._need_cloud().chat(packet, max_tokens=max_tokens)
            return self._stamp(cloud, True, saved)

        if cloud_first:
            if saved == 0:
                brief = self._need_local().chat(
                    [{"role": "user", "content": "List only the facts needed to answer, in under 80 words.\n" + user}],
                    max_tokens=120,
                )
                digest = (brief.text or "").strip()
                saved = max(0, estimate_tokens(full) - estimate_tokens(digest))
                working = [{"role": "user", "content": "Precomputed notes:\n" + digest + "\n\nRequest:\n" + user}]
            cloud = self._need_cloud().chat(working, max_tokens=max_tokens)
            return self._stamp(cloud, True, saved)

        local_reply = self._need_local().chat(working, max_tokens=max_tokens)
        confidence = parse_confidence(local_reply.text)
        choice = decide(self.mode, score, confidence, len(local_reply.text or ""), self.policy)
        if choice == "local":
            return self._stamp(local_reply, False, 0)
        digest = (local_reply.text or "").strip()
        saved = max(saved, estimate_tokens(full) - estimate_tokens(digest))
        packet = [{"role": "user", "content": "Precomputed notes:\n" + digest + "\n\nRequest:\n" + _short_request(user)}]
        cloud = self._need_cloud().chat(packet, max_tokens=max_tokens)
        return self._stamp(cloud, True, saved)

    def vision(self, prompt: str, png: bytes, max_tokens: int = 512) -> Reply:
        if self.mode == "api":
            reply = self._need_cloud().vision(prompt, png, max_tokens=max_tokens)
            return self._stamp(reply, False, 0)
        if self.mode == "local":
            reply = self._need_local().vision(prompt, png, max_tokens=max_tokens)
            return self._stamp(reply, False, 0)
        local_reply = self._need_local().vision(prompt, png, max_tokens=max_tokens)
        score = complexity_score(prompt)
        confidence = parse_confidence(local_reply.text)
        choice = decide(self.mode, score, confidence, len(local_reply.text or ""), self.policy)
        if choice == "local":
            return self._stamp(local_reply, False, 0)
        digest = (local_reply.text or "").strip()
        saved = max(0, (len(png) // 4) - estimate_tokens(digest))
        packet = [{"role": "user", "content": "What the local model saw:\n" + digest + "\n\nRequest:\n" + prompt}]
        cloud = self._need_cloud().chat(packet, max_tokens=max_tokens)
        return self._stamp(cloud, True, saved)

    def embed(self, texts: list[str]) -> EmbedOutcome:
        tokens = estimate_tokens("\n".join(texts))
        if self.mode in ("local", "hybrid") and self.local is not None:
            vectors = _try_embed(self.local, texts)
            if vectors:
                return EmbedOutcome(vectors, "local", False, tokens)
        if self.mode in ("api", "hybrid") and self.cloud is not None:
            vectors = _try_embed(self.cloud, texts)
            if vectors:
                return EmbedOutcome(vectors, getattr(self.cloud, "label", "cloud"), False, tokens)
        return EmbedOutcome(lexical_vectors(texts), "lexical", True, tokens)


def _try_embed(provider, texts: list[str]):
    embed = getattr(provider, "embed", None)
    if embed is None:
        return None
    try:
        return embed(texts)
    except Exception:
        return None


def _short_request(user: str) -> str:
    if len(user) <= 500:
        return user
    return "Answer using only the precomputed notes."


def _last_user(messages: list[dict]) -> str:
    for message in reversed(messages):
        if (message.get("role") or "user") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            return message_text([message])
    return ""
