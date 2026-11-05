"""Best-effort Outlook reply when a dig hold would have dropped the thread.

Drafts only. Never sends. Local notes and the local model only.
"""
from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import quote

_HOLD_BITS = (
    "need_dig_citation",
    "needs_operator_input",
    "empty_compose",
    "review_reject_hold",
    "self_check_failed",
)
_CLOSED = (
    "secret",
    "hard_data",
    "password",
    "finance",
    "no_reply_needed",
    "noise_skip",
    "automated",
    "system_sender",
    "meeting_invite",
)


def is_dig_hold(err: str, noise: str = "", body: str = "") -> bool:
    blob = f"{err or ''} {noise or ''}".lower()
    # Every need_dig_citation variant, including sf_facts_missing_after_dig.
    if "need_dig_citation" in blob or "needs_operator_input" in blob:
        return True
    closed = tuple(bit for bit in _CLOSED if bit != "no_reply_needed")
    if any(bit in blob for bit in closed):
        return False
    if any(bit in blob for bit in _HOLD_BITS):
        return True
    if "no_reply_needed" in blob:
        # A no-reply decision is terminal. A month word in a promo is not a dig hold.
        return False
    return False


def _token() -> str:
    path = Path.home() / "claude-stack/data/runtime/owa_token_cache.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return ""
    return str(data.get("token") or "")


def _get(path: str) -> dict:
    tok = _token()
    if not tok:
        return {}
    req = urllib.request.Request(
        "https://outlook.office.com/api/v2.0" + path,
        headers={"Authorization": "Bearer " + tok, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode() or "{}")


def _keep_unread(message_id: str, was_unread: bool) -> None:
    """If this read flipped an unread note to read, put the badge back once."""
    if was_unread is not True or not message_id:
        return
    mid = quote(str(message_id), safe="")
    try:
        cur = _get("/me/messages/" + mid + "?$select=Id,IsRead")
    except Exception:
        return
    if cur.get("IsRead") is not True:
        return
    tok = _token()
    if not tok:
        return
    data = json.dumps({"IsRead": False}).encode()
    req = urllib.request.Request(
        "https://outlook.office.com/api/v2.0/me/messages/" + mid,
        data=data,
        method="PATCH",
        headers={
            "Authorization": "Bearer " + tok,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
    except Exception:
        return


def _plain(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def full_thread(conversation_id: str, message_id: str, body: str) -> str:
    """Every non-draft message in the Outlook conversation, oldest first."""
    parts: list[str] = []
    cid = (conversation_id or "").replace("'", "").strip()
    if cid and _token():
        try:
            filt = quote(f"ConversationId eq '{cid}'")
            listed = _get(
                "/me/messages?$filter="
                + filt
                + "&$top=12&$select=Id,ReceivedDateTime,IsDraft,Subject"
            )
            rows = [r for r in (listed.get("value") or []) if not r.get("IsDraft")]
            rows.sort(key=lambda r: str(r.get("ReceivedDateTime") or ""))
            for row in rows[-8:]:
                mid = str(row.get("Id") or "")
                if not mid:
                    continue
                was_unread = False
                try:
                    meta = _get("/me/messages/" + quote(mid, safe="") + "?$select=Id,IsRead")
                    was_unread = meta.get("IsRead") is False
                except Exception:
                    was_unread = False
                full = _get(
                    "/me/messages/"
                    + quote(mid)
                    + "?$select=From,ReceivedDateTime,Subject,Body"
                )
                _keep_unread(mid, was_unread)
                who = str(
                    (((full.get("From") or {}).get("EmailAddress") or {}).get("Name"))
                    or ""
                )
                when = str(full.get("ReceivedDateTime") or "")[:16]
                text = _plain(((full.get("Body") or {}).get("Content")) or "")[:1400]
                if text:
                    parts.append(f"{who} ({when}): {text}")
        except Exception:
            parts = []
    blob = "\n\n".join(parts).strip()
    base = (body or "").strip()
    if base and base[:180] not in blob:
        blob = (base + "\n\n" + blob).strip() if blob else base
    return blob[:8000]


def local_notes(text: str) -> str:
    try:
        import dig_toolkit as dk

        lines = dk._work_recall_facts((text or "")[:1800], budget=2200) or []
    except Exception:
        return ""
    out = []
    for line in lines:
        s = str(line or "").strip()
        if s:
            out.append(s)
    return "\n".join(out)[:2200]


def _scrub(text: str) -> str:
    raw = text or ""
    raw = re.sub(r"(?is)<think>.*?</think>", " ", raw)
    raw = raw.replace("\u2014", ", ").replace("\u2013", ", ")
    raw = raw.strip().strip('"').strip()
    raw = re.sub(r"(?is)^```[a-z]*\n?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    if raw.upper().startswith("HOLD"):
        return ""
    raw = re.sub(r"(?i)\[\s*(?:operator|TODO|INSERT|NAME|PLACEHOLDER|GAP|TBD|FIXME)[^\]]*\]", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw


def _latest(body: str) -> str:
    try:
        from ops_stub_detectors import outlook_latest_inbound

        return outlook_latest_inbound(body or "") or (body or "")
    except Exception:
        return body or ""


def _focus_terms(latest: str) -> list[str]:
    try:
        import draft_burst_gates as dbg

        terms = [t for t in dbg._terms(latest or "") if len(t) >= 5]
    except Exception:
        terms = re.findall(r"[a-z][a-z0-9']{4,}", (latest or "").lower())
    stop = {
        "there", "about", "would", "could", "should", "please", "thanks",
        "thank", "hello", "regards", "scott", "operator", "message", "email",
    }
    terms = [t for t in terms if t not in stop]
    terms.sort(key=len, reverse=True)
    out = []
    for term in terms:
        if term not in out:
            out.append(term)
        if len(out) >= 3:
            break
    return out


def deterministic(first: str, subject: str, body: str) -> str:
    # 2026-11-01: the old "went through the whole thread / pin that one point"
    # line was staged as a real reply. A missing fact stays a hold.
    return ""


def _voice_ok(text: str, subject: str) -> tuple[bool, str]:
    if not text or "\u2014" in text or "\u2013" in text:
        return False, "dash"
    low = text.lower()
    for word in (
        "as an ai", "language model", "chatgpt", "claude", "grok", "llm",
        "automation", "mac mini", "script", "chatbot",
        "went through the whole thread", "pin that one point", "subject side is",
        "piece i cannot settle", "what should i do", "let me know if anything",
        "i can look into", "i'll look into", "i will look into", "i will check",
        "which part should i confirm", "i read the note", "i read the mail",
    ):
        if word in low:
            return False, word
    try:
        import tells_gate

        got = tells_gate.check(text, channel="outlook")
        if not got.get("ok"):
            return False, "tells:" + ",".join(got.get("reasons") or [])[:80]
    except Exception as exc:
        return False, "tells_exc:" + type(exc).__name__
    try:
        import operator_outbound_gate as gate

        got = gate.check(text, subject=subject or "")
        if not got.get("ok"):
            return False, "operator:" + ",".join(got.get("reasons") or [])[:80]
    except Exception as exc:
        return False, "operator_exc:" + type(exc).__name__
    return True, ""


def _stub_ok(text: str, body: str) -> tuple[bool, str]:
    try:
        import ops_stub_detectors as osd

        det = osd.detect_body(text, "outlook", source=body or "")
    except Exception as exc:
        return False, "stub_exc:" + type(exc).__name__
    if det.get("stubbish"):
        return False, "stub:" + ",".join(det.get("reasons") or [])[:80]
    return True, ""


def _burst_ok(text: str, body: str) -> tuple[bool, str]:
    try:
        import draft_burst_gates as dbg
        from ops_stub_detectors import outlook_latest_inbound

        latest = outlook_latest_inbound(body or "") or (body or "")
        verdict = dbg.evaluate(
            text,
            [{"from_self": False, "text": latest, "t": 1}],
            "",
        )
    except Exception as exc:
        return False, "burst_exc:" + type(exc).__name__
    if not verdict.get("ok"):
        return False, "burst:" + ",".join(verdict.get("reasons") or [])[:80]
    return True, ""


def _prompt(first: str, subject: str, thread: str, notes: str) -> str:
    name = (first or "").strip() or "there"
    return (
        "Write operator's short Outlook reply. Output only the reply, or the single word HOLD.\n"
        f"First line must be: hey {name},\n"
        "Then a blank line, then 1 to 3 short lowercase sentences. Names, dates, and I stay capitalized.\n"
        "Use only the thread and the notes. Do not invent numbers, dates, or status.\n"
        "Answer what was asked. Do not restate the subject. Do not ask a question unless the mail asks something the notes do not answer.\n"
        "If you cannot answer from the thread and the notes, output HOLD.\n"
        "No --, no em dash, and no en dash, including the greeting. No lists. No sign-off.\n"
        "Do not mention tools, models, or files.\n\n"
        f"SUBJECT:\n{subject or ''}\n\n"
        f"THREAD:\n{(thread or '')[:6500]}\n\n"
        f"NOTES:\n{(notes or '')[:1800]}\n\n"
        "REPLY:\n"
    )



_BAN = ("tool", "tools", "model", "models", "script", "scripts", "bot", "bots", "automation")


def plain_question(first: str, subject: str, body: str) -> str:
    """Filler questions were getting staged. A missing fact stays a hold."""
    return ""


def compose(first: str, subject: str, body: str, thread: str, notes: str, chat, log=None) -> dict | None:
    """Always a plain reply, or None when even the fallback fails the voice gates."""
    log = log or (lambda *_a, **_k: None)
    try:
        import draft_context as dc
        pack = dc.current()
        if not pack or not (pack.get("text") or pack.get("links")):
            pack = dc.build_pack(channel="outlook", subject=subject or "", body=thread or body or "", surface="outlook")
            dc.log_links("outlook", subject or "", pack.get("links") or [])
            dc.stash(pack)
        extra = dc.prompt_block(pack)
        if extra:
            notes = (notes or "") + "\n" + extra
    except Exception as exc:
        log("draft_context_exc %s" % type(exc).__name__)
    text = ""
    if callable(chat):
        try:
            text = _scrub(chat(_prompt(first, subject, thread or body, notes), 220, "") or "")
        except Exception as exc:
            log(f"owa_best_effort_chat_exc {type(exc).__name__}:{str(exc)[:60]}")
            text = ""
    why = ""
    if text:
        try:
            ok_v, why = _voice_ok(text, subject)
            ok_s, why_s = _stub_ok(text, body)
            ok_b, why_b = _burst_ok(text, body)
            if not (ok_v and ok_s and ok_b):
                log(f"owa_best_effort_model_drop {why or why_s or why_b}")
                text = ""
        except BaseException as exc:
            log(f"owa_best_effort_post_exc {type(exc).__name__}:{str(exc)[:80]}")
            text = ""
    if not text:
        text = deterministic(first, subject, thread or body)
        if not text:
            log("owa_best_effort_hold no_grounded_line")
            return None
        ok_v, why = _voice_ok(text, subject)
        ok_s, why_s = _stub_ok(text, body)
        ok_b, why_b = _burst_ok(text, body)
        if not (ok_v and ok_s and ok_b):
            log(f"owa_best_effort_fallback_drop {why or why_s or why_b}")
            return None
    out = {
        "ok": True,
        "text": text,
        "via": "best_effort",
        "liberal": True,
        "never_send": True,
        "sent": False,
        "facts": [],
        "skipped_noise": "",
        "error": "",
        "hold_note": "",
        "liberal_reason": "",
    }
    try:
        import draft_context as dc
        out = dc.guard(out, ask=body or subject or "", surface="outlook", log=log)
    except Exception as exc:
        log("draft_guard_exc %s" % type(exc).__name__)
        return None
    if not out or not str(out.get("text") or "").strip():
        log("draft_hold %s" % str((out or {}).get("error") or "self_check")[:80])
        return None
    return out
