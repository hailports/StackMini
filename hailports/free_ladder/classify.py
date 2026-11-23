"""Cheap deterministic request classifier (no model call)."""
from __future__ import annotations

import re
from typing import Any

_CLASSIFY = re.compile(r"\b(?:classif\w*|triage|categorize|label|rank|score|rate|yes\s*/\s*no)\b", re.I)
_EXTRACT = re.compile(r"\b(?:extract\w*|pull(?:\s+out)?|parse|list the|outline|bullet)\b", re.I)
_SUM = re.compile(r"\b(?:summar\w*|digest|rollup|rundown|tl;dr|tldr|meeting summary|thread summary)\b", re.I)
_CODE = re.compile(
    r"\b(?:code|python|typescript|javascript|apex|sql|refactor|implement|debug|patch|"
    r"function|class|module|compile|stacktrace|traceback)\b",
    re.I,
)
_REASON = re.compile(
    r"\b(?:reason|prove|analyze|architecture|design|trade-?off|plan|strategy|"
    r"root cause|deep|complex|multi-?step)\b",
    re.I,
)
_VISION = re.compile(r"\b(?:image|screenshot|photo|vision|diagram|png|jpe?g|webp)\b", re.I)
_LONG = re.compile(r"\b(?:long context|entire (?:doc|pdf|thread)|full transcript|research)\b", re.I)


def _content_blob(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    if p.get("type") == "image_url" or "image" in str(p.get("type", "")):
                        parts.append("[image]")
                    elif isinstance(p.get("text"), str):
                        parts.append(p["text"])
    if isinstance(payload.get("system"), str):
        parts.append(payload["system"])
    return "\n".join(parts)


def estimate_tokens(payload: dict[str, Any]) -> int:
    blob = _content_blob(payload)
    # rough 4 chars/token
    return max(1, len(blob) // 4)


def needs_vision(payload: dict[str, Any]) -> bool:
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and (
                    p.get("type") in {"image_url", "image"} or "image_url" in p
                ):
                    return True
    return bool(_VISION.search(_content_blob(payload)))


def needs_tools(payload: dict[str, Any]) -> bool:
    return bool(payload.get("tools") or payload.get("functions") or payload.get("tool_choice"))


def classify(payload: dict[str, Any]) -> dict[str, Any]:
    blob = _content_blob(payload)
    toks = estimate_tokens(payload)
    vision = needs_vision(payload)
    tools = needs_tools(payload)
    task = "chat"
    if vision:
        task = "vision"
    elif toks >= 24_000 or _LONG.search(blob):
        task = "long_context"
    elif _SUM.search(blob) and toks >= 2_000:
        task = "summarize"
    elif _SUM.search(blob):
        task = "summarize"
    elif _CODE.search(blob):
        task = "code"
    elif _REASON.search(blob):
        task = "reasoning"
    elif _CLASSIFY.search(blob) and toks < 4_000:
        task = "classify"
    elif _EXTRACT.search(blob) and toks < 6_000:
        task = "extract"
    latency = "low" if task in {"classify", "extract"} else ("high" if task in {"reasoning", "code", "long_context"} else "med")
    return {
        "task": task,
        "tokens": toks,
        "vision": vision,
        "tools": tools,
        "latency": latency,
    }
