"""Fit-to-context trimmer for chat/completion bodies (used by splash_shim.py).

Budget = ctx - output headroom (max_tokens, or a default reserve) - a safety margin.
Steps run in order and stop as soon as the request fits:
  1 dedupe repeated system / boilerplate messages
  2 drop tool schemas no turn mentions (keeps tools named by tool_choice or used before);
    shorten long tool descriptions
  3 strip base64 blobs pasted as text, collapse whitespace (older messages only)
  4 head+tail truncate long tool outputs and logs (older messages only)
  5 drop oldest turns (assistant tool calls go with their tool results), leaving one note
  6 last resort: head+tail cut of the system prompt, then of the latest user text
The latest user message keeps its images and files whole. A request that still cannot fit is
refused (never forwarded over the cap). Stats are counts only, never content.
"""
from __future__ import annotations

import copy
import json
import re

B64 = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]{200,}|(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{400,}={0,2}")
MARK = "\n[... {n} chars trimmed to fit the context window ...]\n"


def _text_len(c) -> int:
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for p in c:
            if isinstance(p, dict):
                if p.get("type") in ("text", "input_text"):
                    n += len(p.get("text") or "")
                elif p.get("type") in ("image_url", "input_image"):
                    n += 4500  # ~1.5k tokens per image at the default pixel cap
                elif p.get("type") in ("file", "input_file"):
                    n += 12000
        return n
    return len(json.dumps(c)) if c else 0


def estimate(obj: dict) -> int:
    """Rough token estimate (chars / 3) used to skip exact counting for small requests."""
    n = sum(_text_len(m.get("content")) + len(json.dumps(m.get("tool_calls") or "")) for m in obj.get("messages") or [] if isinstance(m, dict))
    n += len(json.dumps(obj.get("tools") or ""))
    return n // 3 + 16 * len(obj.get("messages") or [])


def _ht(s: str, keep: int) -> str:
    if len(s) <= keep:
        return s
    h = int(keep * 0.6)
    t = keep - h
    return s[:h] + MARK.format(n=len(s) - keep) + s[-t:]


def _map_text(c, fn):
    if isinstance(c, str):
        return fn(c)
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text") and isinstance(p.get("text"), str):
                p = dict(p, text=fn(p["text"]))
            out.append(p)
        return out
    return c


def _last_user(msgs) -> int:
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
            return i
    return len(msgs) - 1


def fit(obj: dict, ctx: int, count, default_out: int = 2048, margin: int = 256):
    """Return (obj, stats). count(obj)->int tokens or None. stats['refused'] set if impossible."""
    st = {"before": None, "after": None, "budget": None, "dedupe": 0, "tools_dropped": 0, "tools_shortened": 0,
          "b64": 0, "ws": 0, "truncated": 0, "turns_dropped": 0, "cut_system": 0, "cut_latest": 0,
          "max_tokens_lowered": 0, "refused": False}
    out = obj.get("max_completion_tokens") or obj.get("max_tokens") or default_out
    out = max(16, min(int(out), ctx // 2))
    budget = ctx - out - margin
    st["budget"] = budget
    if estimate(obj) < budget * 0.55:
        return obj, None  # clearly fits: no exact count, no changes
    n = count(obj)
    if n is None:
        return obj, None
    st["before"] = n
    if n <= budget:
        return obj, None
    obj = copy.deepcopy(obj)
    msgs = obj.get("messages") or []

    def recount():
        return count(obj) or 10 ** 9

    def done():
        st["after"] = n
        return obj, st

    # 1 dedupe
    seen, keep = set(), []
    li = _last_user(msgs)
    for i, m in enumerate(msgs):
        key = json.dumps([m.get("role"), m.get("content")], sort_keys=True) if isinstance(m, dict) and not m.get("tool_calls") and m.get("role") != "tool" else None
        if key and i != li and key in seen:
            st["dedupe"] += 1
            continue
        if key:
            seen.add(key)
        keep.append(m)
    if st["dedupe"]:
        msgs[:] = keep
        n = recount()
        if n <= budget:
            return done()
    # 2 tools
    tools = obj.get("tools") or []
    if tools:
        convo = json.dumps(msgs)
        tc = obj.get("tool_choice")
        forced = tc.get("function", {}).get("name") if isinstance(tc, dict) else None
        def name(t): return (t.get("function") or {}).get("name") or t.get("name") or ""
        used = [t for t in tools if name(t) and (name(t) == forced or name(t) in convo)]
        if used and len(used) < len(tools):
            st["tools_dropped"] = len(tools) - len(used)
            obj["tools"] = tools = used
        for t in tools:
            f = t.get("function") or t
            if isinstance(f.get("description"), str) and len(f["description"]) > 300:
                f["description"] = f["description"][:300]
                st["tools_shortened"] += 1
        if st["tools_dropped"] or st["tools_shortened"]:
            n = recount()
            if n <= budget:
                return done()
    li = _last_user(msgs)
    # 3 base64 + whitespace (not the latest user message)
    def clean(s):
        s2, k = B64.subn(lambda m: f"[base64 removed {len(m.group(0))} chars]", s)
        st["b64"] += k
        s3 = re.sub(r"[ \t]+\n", "\n", s2)
        s3 = re.sub(r"\n{3,}", "\n\n", s3)
        s3 = re.sub(r"[ \t]{3,}", "  ", s3)
        if len(s3) < len(s2):
            st["ws"] += 1
        return s3
    for i, m in enumerate(msgs):
        if i != li and isinstance(m, dict):
            m["content"] = _map_text(m.get("content"), clean)
    n = recount()
    if n <= budget:
        return done()
    # 4 head+tail long tool outputs / logs, shrinking the per-message cap
    for cap in (8000, 3000, 1200):
        changed = False
        for i, m in enumerate(msgs):
            if i == li or not isinstance(m, dict) or m.get("role") == "system":
                continue
            if _text_len(m.get("content")) > cap:
                m["content"] = _map_text(m.get("content"), lambda s: _ht(s, cap))
                st["truncated"] += 1
                changed = True
        if changed:
            n = recount()
            if n <= budget:
                return done()
    # 5 drop oldest turns (keep system msgs and the latest user turn onward)
    dropped = 0
    while n > budget:
        idx = next((i for i, m in enumerate(msgs) if i < _last_user(msgs) and isinstance(m, dict) and m.get("role") != "system"), None)
        if idx is None:
            break
        m = msgs.pop(idx)
        dropped += 1
        if isinstance(m, dict) and m.get("tool_calls"):
            while idx < len(msgs) and isinstance(msgs[idx], dict) and msgs[idx].get("role") == "tool":
                msgs.pop(idx)
                dropped += 1
        while idx < len(msgs) and isinstance(msgs[idx], dict) and msgs[idx].get("role") == "tool" and idx < _last_user(msgs):
            msgs.pop(idx)  # orphaned tool results
            dropped += 1
        n = recount()
    if dropped:
        st["turns_dropped"] = dropped
        first_non_sys = next((i for i, m in enumerate(msgs) if m.get("role") != "system"), 0)
        msgs.insert(first_non_sys, {"role": "user", "content": f"[{dropped} earlier messages were omitted to fit the context window]"})
        if first_non_sys < len(msgs) - 1 and msgs[first_non_sys + 1].get("role") == "user":
            # keep roles alternating: fold the note into the next user turn
            note = msgs.pop(first_non_sys)["content"]
            nxt = msgs[first_non_sys]
            nxt["content"] = (note + "\n\n" + nxt["content"]) if isinstance(nxt.get("content"), str) else ([{"type": "text", "text": note}] + list(nxt.get("content") or []))
        n = recount()
        if n <= budget:
            return done()
    # 6 last resort: system prompt, then latest user text (images/files stay)
    for role_pick, key in (("system", "cut_system"), ("user", "cut_latest")):
        for _ in range(8):
            if n <= budget:
                return done()
            chars = sum(_text_len(x.get("content")) for x in msgs if isinstance(x, dict)) or 1
            ratio = max(2.0, chars / max(1, n))  # measured chars per token for this request
            over_chars = int((n - budget) * ratio * 1.15) + 400
            targets = [m for m in msgs if isinstance(m, dict) and m.get("role") == role_pick]
            if role_pick == "user":
                targets = [msgs[_last_user(msgs)]]
            m = max(targets, key=lambda x: _text_len(x.get("content")), default=None)
            if m is None:
                break
            c = m.get("content")
            cur = len(c) if isinstance(c, str) else sum(len(p.get("text") or "") for p in c or [] if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
            if cur < 2000:
                break
            m["content"] = _map_text(m.get("content"), lambda s: _ht(s, max(1000, len(s) - over_chars)))
            st[key] += 1
            n = recount()
    if n <= budget:
        return done()
    # prompt fits the window but not with the requested output: lower max_tokens
    room = ctx - n - margin
    if room >= 256:
        for k in ("max_completion_tokens", "max_tokens"):
            if k in obj:
                obj[k] = room
        if "max_tokens" not in obj and "max_completion_tokens" not in obj:
            obj["max_tokens"] = room
        st["max_tokens_lowered"] = 1
        return done()
    st["refused"] = True
    st["after"] = n
    return obj, st


def fit_prompt(obj: dict, ctx: int, count_text, default_out: int = 2048, margin: int = 128):
    """Raw /v1/completions prompt: head+tail cut until it fits."""
    p = obj.get("prompt")
    if not isinstance(p, str):
        return obj, None
    out = max(16, min(int(obj.get("max_tokens") or default_out), ctx // 2))
    budget = ctx - out - margin
    if len(p) // 3 < budget * 0.55:
        return obj, None
    n = count_text(p)
    if n is None or n <= budget:
        return obj, None
    st = {"before": n, "budget": budget, "cut_prompt": 0, "refused": False}
    for _ in range(8):
        ratio = max(2.0, len(p) / max(1, n))
        p = _ht(p, max(1000, len(p) - int((n - budget) * ratio * 1.15) - 400))
        st["cut_prompt"] += 1
        n = count_text(p) or 10 ** 9
        if n <= budget:
            break
    obj = dict(obj, prompt=p)
    st["after"] = n
    st["refused"] = n > budget
    return obj, st
