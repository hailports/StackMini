#!/usr/bin/env python3
"""Monitor ADO work item comments & operator Chatter (FeedItem / FeedComment) for @mentions of operator.

100% local compute on the Mini (:8080 MiniCPM-V-4.7-35B-A3B). Zero paid API calls.
When someone tags operator in an ADO or operator comment:
  1. Gathers ticket context + comment thread + local work_brain context.
  2. Drafts a short, grounded reply in operator's voice (lowercase except proper names, dates, and I) on :8080.
  3. Queues a signed approval token on :8791 (kind="ado_comment" with mention_reply=True, or kind="sfc_comment").
  4. Emails operator a previewed draft with one-click "Post comment" (/approve/<token>?s=<sig>)
     and "Edit & post" (/deny/<token>?s=<sig>) buttons.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("SF_USE_GENERIC_UNIX_KEYCHAIN", "true")
os.environ.setdefault("SFDX_USE_GENERIC_UNIX_KEYCHAIN", "true")

HOME = Path.home()
CLI = HOME / "llm-appliance" / "cli"
STACK = HOME / "claude-stack"
TOOLS = STACK / "tools"
CORE = STACK / "core"
for _p in (str(CLI), str(STACK), str(TOOLS), str(CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUNTIME = HOME / "llm-appliance" / "runtime" / "work-loop"
SEEN_PATH = RUNTIME / "mention_seen.json"
LOG_PATH = HOME / "llm-appliance" / "logs" / "mention_comment_watch.log"
LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "MiniCPM-V-4.7-35B-A3B")

_TAG_RE = re.compile(r"<[^>]+>")
_operator_TAG_RE = re.compile(
    r"(@\s*de\s*operator|@\s*operator\b|operator\.operator|ad720957|redacted-uuid|\boperator\b)",
    re.I,
)
_PURE_QA_PASS_RE = re.compile(
    r"^(?:@[\w,\s.]+\s*)*(?:testing\s+completed(?:\s+successfully)?|verified\s+in\s+(?:uat|sandbox|partial|prod)|verified\s+the\s+|working\s+as\s+expected)",
    re.I,
)
_operator_AUTHOR_RE = re.compile(r"(ad720957|operator|operator)", re.I)
_AI_STRIP_RE = re.compile(
    r"\b(openai|chatgpt|claude|anthropic|copilot|cursor|minicpm|qwen|llm|work_brain|claude-stack|openclaw)\b",
    re.I,
)


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def _strip_html(val: object) -> str:
    txt = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>|</li\s*>", "\n", str(val or ""), flags=re.I)
    txt = html.unescape(_TAG_RE.sub(" ", txt)).replace("\u200b", "").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in txt.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l for l in lines if l)).strip()


def _load_seen() -> dict:
    try:
        if SEEN_PATH.exists():
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"seeded_at": 0, "items": {}}


def _save_seen(state: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(SEEN_PATH)


def _is_operator(author: object) -> bool:
    if isinstance(author, dict):
        blob = " ".join(str(author.get(k) or "") for k in ("displayName", "uniqueName", "mailAddress", "name", "Name"))
    else:
        blob = str(author or "")
    return bool(_operator_AUTHOR_RE.search(blob))


def _mentions_operator(raw_html_or_text: str) -> bool:
    s = str(raw_html_or_text or "")
    plain = _strip_html(s)
    if not (_operator_TAG_RE.search(s) or re.search(r"@operator\b|\boperator\b.*?\?", plain, re.I)):
        return False
    # Skip pure QA pass sign-offs with no question (e.g. "Testing completed successfully... working as expected.")
    if "?" not in plain and _PURE_QA_PASS_RE.search(plain) and not re.search(r"\b(please|can you|could you|need|issue|fail|error|block|revert|miss)\b", plain, re.I):
        return False
    return True


def _work_brain_snippets(query: str, limit: int = 5) -> list[str]:
    try:
        import sqlite3
        db = STACK / "data" / "work_context.db"
        if not db.exists():
            return []
        words = [w for w in re.findall(r"[A-Za-z0-9_]{3,}", query) if w.lower() not in {"the", "and", "for", "with", "this", "that", "from", "have", "can", "you", "operator"}][:6]
        if not words:
            return []
        fts_q = " OR ".join(f'"{w}"' for w in words)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT source, title, substr(body, 1, 360) FROM docs WHERE rowid IN "
            "(SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? LIMIT 25) ORDER BY ts DESC LIMIT ?",
            (fts_q, limit),
        ).fetchall()
        con.close()
        return [f"[{r[0]}] {r[1]}: {re.sub(r'\\s+', ' ', str(r[2] or '')).strip()}" for r in rows]
    except Exception:
        return []


def _enforce_operator_lowercase(text: str) -> str:
    """Ensure operator's lowercase style while preserving proper nouns/acronyms/ticket IDs and 'I'."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:\w+)?\n?|\n?```$", "", t).strip()
    t = t.replace("—", " - ").replace("–", " - ")
    t = _AI_STRIP_RE.sub("", t)
    lines = []
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        words = line.split(" ")
        if words:
            w0 = words[0]
            if w0 != "I" and not w0.startswith("I'") and not re.match(r"^[A-Z]{2,}", w0):
                words[0] = w0[:1].lower() + w0[1:]
        line_out = " ".join(words)
        line_out = re.sub(r"\bi(?=('(?:m|ll|ve|d)|\b))", "I", line_out)
        lines.append(line_out)
    return "\n\n".join(lines).strip()


def _ticket_local_context(wid: str) -> str:
    if not wid or not str(wid).isdigit():
        return ""
    bits = []
    try:
        hp = RUNTIME / "hybrid-prep" / f"ticket-{wid}.json"
        if hp.is_file():
            d = json.loads(hp.read_text())
            td = str(d.get("team_decision") or "").strip()
            lp = str(d.get("local_prepped") or "").strip()
            if td:
                bits.append(f"Team hold/rule: {td[:240]}")
            if lp:
                bits.append(f"Local prep: {lp[:240]}")
    except Exception:
        pass
    try:
        sp = RUNTIME / "ado-staged" / f"{wid}.json"
        if sp.is_file():
            st = json.loads(sp.read_text())
            bits.append(f"Last staged status={st.get('status')}: {str(st.get('comment') or '')[:220]}")
    except Exception:
        pass
    return " | ".join(bits)


def draft_reply_local(item: dict) -> str:
    """Generate a reply draft using 100% local Mini :8080 MiniCPM-V-4.7-35B-A3B."""

    try:
        import draft_context as _dc
        _pack = _dc.build_pack(
            channel=str(item.get("surface") or "ado"),
            subject=str(item.get("title") or ""),
            body=str(item.get("comment_text") or ""),
            ticket_id=str(item.get("ticket_id") or ""),
            thread=item.get("recent_thread") or [],
            description=str(item.get("description") or ""),
            surface=str(item.get("surface") or "ado"),
        )
        _dc.log_links(str(item.get("surface") or "ado"), str(item.get("ticket_id") or item.get("key") or ""), _pack.get("links") or [])
        _dc.stash(_pack)
        if _pack.get("blocked"):
            _log("draft_hold data_line ticket=%s" % item.get("ticket_label"))
            return ""
    except Exception as _ce:
        _log("draft_context_exc %s" % type(_ce).__name__)
    try:
        import ask_do as _ad
        _got = _ad.intercept(
            "ado",
            who=str(item.get("author") or ""),
            subject=str(item.get("title") or ""),
            body=str(item.get("comment_text") or ""),
            first="",
            message_id=str(item.get("ticket_id") or ""),
        )
        if _got is not None and _got.get("text"):
            return _got["text"]
        if _got is not None and str(_got.get("error") or "").startswith("hold:data_line"):
            return ""
    except Exception:
        pass
    surface = item.get("surface", "ado")
    ticket_label = item.get("ticket_label", "")
    title = item.get("title", "")
    author = item.get("author", "")
    comment_text = item.get("comment_text", "")
    description = item.get("description", "")[:1200]
    recent_thread = item.get("recent_thread", [])
    wb_hits = _work_brain_snippets(f"{ticket_label} {title} {comment_text[:250]}")
    local_ctx = _ticket_local_context(str(item.get("ticket_id") or "")) if surface == "ado" else ""
    if local_ctx:
        wb_hits = [f"[verified ticket state] {local_ctx}"] + list(wb_hits or [])

    sys_prompt = (
        "You are operator operator drafting a short, direct reply to a comment where a teammate tagged him "
        f"on {'an Azure DevOps work item' if surface == 'ado' else 'a operator Chatter thread'}.\n"
        "Voice rules:\n"
        "- Use lowercase throughout, capitalizing ONLY proper names (e.g. operator, Pruthi, Marie, Pavan, Lokesh, operator, SAP, Tavant), "
        "dates/days, ticket IDs (redacted-ticket), and the word 'I'.\n"
        "- Plain, warm, direct words. 1 to 3 short sentences max. No bullet lists, no sign-off, no em dashes.\n"
        "- Never invent a count, file path, line number, or deployment status not in the evidence.\n"
        "- Never mention AI, models, bots, scripts, or automation.\n"
        "Reply with ONLY the comment body text and nothing else."
    )
    try:
        import draft_context as _dc
        _block = _dc.prompt_block(_dc.current() or {})
    except Exception:
        _block = ""
    user_prompt = (
        f"Ticket: {ticket_label} — {title}\n"
        f"State / Assigned: {item.get('state', '')} / {item.get('assigned_to', '')}\n"
        f"Description excerpt:\n{description}\n\n"
        f"Recent comment thread:\n" + "\n".join(f"- {c}" for c in recent_thread[-5:]) + "\n\n"
        f"Work context hits:\n" + ("\n".join(wb_hits) if wb_hits else "(none)") + "\n\n"
        f"Newest comment from {author} tagging operator:\n{comment_text}\n\n"
        "Write operator's reply comment now (lowercase style, plain words, 1-3 sentences, no sign-off):\n"
        + (_block or "")
    )
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 260,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(LLM_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        raw = re.sub(r"(?is)<think>.*?</think>", "", raw).strip()
        cleaned = _enforce_operator_lowercase(raw)
        if len(cleaned) >= 12:
            try:
                import draft_context as _dc
                _checked = _dc.apply_check(
                    str(item.get("comment_text") or ""),
                    str((_dc.current() or {}).get("text") or ""),
                    cleaned,
                    surface=str(item.get("surface") or "ado"),
                )
            except Exception as _se:
                _log("draft_hold self_check_exc %s" % type(_se).__name__)
                return ""
            if not _checked.get("ok"):
                _log("draft_hold %s ticket=%s" % (_checked.get("reason") or "self_check", ticket_label))
                return ""
            return str(_checked.get("text") or "")[:1200]
    except Exception as e:
        _log(f"local_llm_draft_error surface={surface} ticket={ticket_label} err={e}")
    _log(f"draft_hold no_grounded_reply surface={surface} ticket={ticket_label}")
    return ""


def scan_ado_mentions(days: int = 3) -> list[dict]:
    """Scan recently changed ADO work items for comments tagging operator."""
    found: list[dict] = []
    try:
        import ado_client as ado
        os.environ["ADO_CDP"] = "http://127.0.0.1:18820"
        os.environ["ADO_FORCE_MINI"] = "1"
        bridge = ado.AdoBridge().connect()
    except Exception as e:
        _log(f"ado_bridge_connect_skip: {e}")
        return found
    try:
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = 'operator' "
            f"AND [System.ChangedDate] >= @Today - {int(days)} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        res = bridge.api("POST", "operator/_apis/wit/wiql?api-version=7.0&$top=60", body={"query": wiql})
        ids = [int(w["id"]) for w in ((res or {}).get("workItems") or []) if w.get("id")][:50]
        if not ids:
            return found
        items = ado.work_items(bridge, ids)
        by_id = {int(it["id"]): it for it in items if it.get("id")}
        for wid in ids:
            if str(wid) == "370225":
                continue
            wi = by_id.get(wid) or {}
            fields = wi.get("fields") or {}
            try:
                cdata = ado.comments(bridge, wid, top=15)
            except Exception:
                continue
            clist = list(cdata if isinstance(cdata, list) else ((cdata or {}).get("comments") or []))
            if not clist:
                continue
            clist.sort(key=lambda c: str(c.get("createdDate") or c.get("modifiedDate") or ""))
            newest = clist[-1]
            author_obj = newest.get("createdBy") or newest.get("modifiedBy") or {}
            if _is_operator(author_obj):
                continue
            raw_text = str(newest.get("text") or "")
            if not _mentions_operator(raw_text):
                continue
            cid = str(newest.get("id") or newest.get("commentId") or "")
            created_at = str(newest.get("createdDate") or "")
            author_name = str(author_obj.get("displayName") or author_obj.get("uniqueName") or "Teammate")
            assigned_obj = fields.get("System.AssignedTo")
            assigned_name = assigned_obj.get("displayName") if isinstance(assigned_obj, dict) else str(assigned_obj or "Unassigned")
            thread_lines = []
            for c in clist[-6:]:
                who = (c.get("createdBy") or {}).get("displayName") or "User"
                thread_lines.append(f"{who}: {_strip_html(c.get('text'))[:350]}")
            found.append({
                "key": f"ado:{wid}:{cid}",
                "surface": "ado",
                "ticket_id": str(wid),
                "ticket_label": f"ADO #{wid}",
                "title": str(fields.get("System.Title") or ""),
                "state": str(fields.get("System.State") or ""),
                "assigned_to": assigned_name,
                "description": _strip_html(fields.get("System.Description")),
                "comment_id": cid,
                "comment_at": created_at,
                "author": author_name,
                "comment_text": _strip_html(raw_text)[:2000],
                "recent_thread": thread_lines,
                "url": f"https://operator/operator/operator/_workitems/edit/{wid}",
            })
    except Exception as e:
        _log(f"scan_ado_mentions_err: {e}")
    finally:
        try:
            bridge.close()
        except Exception:
            pass
    return found


def scan_sfc_mentions(days: int = 3) -> list[dict]:
    """Scan operator Chatter (FeedItem + FeedComment) for @mentions of operator using chatter_reply_relay."""
    found: list[dict] = []
    try:
        import chatter_reply_relay as crr
        threads = crr.find_threads_awaiting_operator(days=days, limit=15, feed_limit=400)
        for row in threads:
            parent_id = str(row.get("ticket_id") or "")
            comment_id = str(row.get("newest_comment_id") or "")
            if not parent_id or not comment_id:
                continue
            ctx = crr.ticket_context(parent_id)
            ticket_num = str(ctx.get("ticket") or parent_id)
            subj = str(ctx.get("subject") or "operator Ticket")
            feed = crr.record_feed(parent_id, limit=25)
            author_name = "Teammate"
            thread_lines = []
            for th in feed:
                for ev in th.get("events") or []:
                    nm = ev.get("name") or ("operator" if ev.get("from_self") else "Teammate")
                    thread_lines.append(f"{nm}: {str(ev.get('text') or '')[:350]}")
                    if ev.get("id") == comment_id and ev.get("name"):
                        author_name = str(ev["name"])
            inst = crr._org_instance_url() or "https://operator"
            found.append({
                "key": f"sfc:{parent_id}:{comment_id}",
                "surface": "sfc",
                "ticket_id": parent_id,
                "ticket_label": ticket_num,
                "title": subj,
                "state": str(ctx.get("status") or ""),
                "assigned_to": str(ctx.get("assigned_to") or ""),
                "description": str(ctx.get("description") or ""),
                "comment_id": comment_id,
                "feed_item_id": str(row.get("feed_item_id") or row.get("root_feed_item_id") or ""),
                "comment_at": str(row.get("created_at") or ""),
                "author": author_name,
                "comment_text": str(row.get("newest_body") or "")[:2000],
                "recent_thread": thread_lines[-6:],
                "url": f"{inst}/{parent_id}",
            })
    except Exception as e:
        _log(f"scan_sfc_mentions_err: {e}")
    return found


def build_email_html(item: dict, draft_text: str, queued: dict) -> str:
    surface_badge = "Azure DevOps" if item["surface"] == "ado" else "operator Chatter"
    approve_url = str(queued.get("approve_url") or "")
    edit_url = str(queued.get("deny_url") or "")
    rec_url = str(item.get("url") or "")
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:20px auto;color:#1f2328;line-height:1.5">
  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#57606a;margin-bottom:4px">
    {html.escape(surface_badge)} @mention &middot; {html.escape(item.get('ticket_label') or '')}
  </div>
  <h2 style="margin:0 0 8px;font-size:18px">
    <a href="{html.escape(rec_url)}" style="color:#0969da;text-decoration:none">{html.escape(item.get('ticket_label') or '')}: {html.escape(item.get('title') or '')}</a>
  </h2>
  <div style="font-size:13px;color:#57606a;margin-bottom:14px">
    Tagged by <b>{html.escape(item.get('author') or '')}</b> &middot; State: {html.escape(item.get('state') or '—')} &middot; Assigned: {html.escape(item.get('assigned_to') or '—')}
  </div>

  <div style="background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:12px 14px;margin-bottom:16px">
    <div style="font-size:12px;font-weight:600;color:#7d4e00;margin-bottom:4px">Comment from {html.escape(item.get('author') or '')}:</div>
    <div style="white-space:pre-wrap;font-size:14px;color:#24292f">{html.escape(item.get('comment_text') or '')}</div>
  </div>

  <div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;margin-bottom:16px">
    <div style="font-size:12px;font-weight:600;color:#1a7f37;margin-bottom:4px">Previewed reply draft:</div>
    <div style="white-space:pre-wrap;font-size:14.5px;color:#1f2328">{html.escape(draft_text)}</div>
  </div>

  <div style="margin:18px 0">
    <a href="{html.escape(approve_url)}" style="display:inline-block;padding:10px 18px;background:#1a7f37;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;margin-right:10px">Post comment</a>
    <a href="{html.escape(edit_url)}" style="display:inline-block;padding:10px 18px;background:#0969da;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;margin-right:10px">Edit &amp; post</a>
    <a href="{html.escape(rec_url)}" style="display:inline-block;padding:10px 14px;background:#f3f4f6;color:#24292f;text-decoration:none;border-radius:6px;font-size:13px">Open {html.escape(item.get('ticket_label') or 'record')}</a>
  </div>
  <p style="font-size:12px;color:#6e7781">Opening either link shows a confirmation/editor page first so email link scanners never auto-post.</p>
</body></html>"""


def queue_and_notify(item: dict, *, dry_run: bool = False, send_email: bool = True) -> dict:
    import work_graph_exec as wge

    draft_text = draft_reply_local(item)
    if not str(draft_text or "").strip():
        _log("draft_hold empty key=%s" % item.get("key"))
        return {"ok": False, "held": True, "key": item.get("key"), "reason": "no_draft"}
    if item["surface"] == "ado":
        action = {
            "kind": "ado_comment",
            "payload": {
                "id": item["ticket_id"],
                "work_item_id": item["ticket_id"],
                "text": draft_text,
                "mention_reply": True,
                "reply_to_comment_id": item["comment_id"],
                "reply_to_author": item["author"],
                "reply_to_text": item.get("comment_text") or "",
                "ticket_title": item.get("title") or "",
                "ticket_state": item.get("state") or "",
                "url": item.get("url") or "",
            },
        }
    else:
        action = {
            "kind": "sfc_comment",
            "payload": {
                "record_id": item["ticket_id"],
                "record_label": item["ticket_label"],
                "feed_item_id": item.get("feed_item_id") or "",
                "text": draft_text,
                "mention_reply": True,
                "reply_to_comment_id": item["comment_id"],
                "reply_to_author": item["author"],
                "reply_to_text": item.get("comment_text") or "",
                "ticket_title": item.get("title") or "",
                "url": item.get("url") or "",
            },
        }
    if dry_run:
        return {"ok": True, "dry_run": True, "key": item["key"], "draft": draft_text, "action": action}

    queued = wge.queue_approval(item["ticket_id"], action)
    if queued.get("blocked") or not queued.get("approve_url"):
        return {"ok": False, "key": item["key"], "blocked": True, "reasons": queued.get("reasons")}

    emailed = False
    if send_email:
        try:
            import ops_work_digest as owd
            subj = f"Tagged on {item['ticket_label']}: {item['title'][:65]}"
            html_doc = build_email_html(item, draft_text, queued)
            html_file = RUNTIME / f"mention-email-{queued['token']}.html"
            html_file.write_text(html_doc, encoding="utf-8")
            r_mail = owd.send_self_alert(subject=subj, html_path=html_file)
            emailed = bool((r_mail or {}).get("ok"))
        except Exception as e:
            _log(f"mention_email_err key={item['key']} err={e}")

    _log(f"queued_mention key={item['key']} token={queued.get('token')} emailed={emailed}")
    return {
        "ok": True,
        "key": item["key"],
        "ticket": item["ticket_label"],
        "author": item["author"],
        "draft": draft_text,
        "token": queued.get("token"),
        "approve_url": queued.get("approve_url"),
        "edit_url": queued.get("deny_url"),
        "emailed": emailed,
    }


def run_once(*, days: int = 3, dry_run: bool = False, seed_only: bool = False, max_new: int = 3) -> dict:
    state = _load_seen()
    seen_items: dict = state.setdefault("items", {})
    first_boot = not bool(state.get("seeded_at"))

    ado_hits = scan_ado_mentions(days=days)
    sfc_hits = scan_sfc_mentions(days=days)
    all_hits = ado_hits + sfc_hits

    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if seed_only or first_boot:
        seeded = 0
        for h in all_hits:
            if seed_only or not str(h.get("comment_at") or "").startswith(today_prefix):
                seen_items[h["key"]] = {
                    "at": time.time(),
                    "seeded": True,
                    "comment_at": h.get("comment_at"),
                    "author": h.get("author"),
                }
                seeded += 1
        state["seeded_at"] = time.time()
        if not dry_run:
            _save_seen(state)
        if seed_only:
            return {"ok": True, "seeded": seeded, "ado_scanned": len(ado_hits), "sfc_scanned": len(sfc_hits)}

    processed = []
    for h in all_hits:
        if h["key"] in seen_items:
            continue
        if len(processed) >= max_new:
            break
        res = queue_and_notify(h, dry_run=dry_run, send_email=not dry_run)
        processed.append(res)
        if not dry_run and res.get("ok"):
            seen_items[h["key"]] = {
                "at": time.time(),
                "token": res.get("token"),
                "approve_url": res.get("approve_url"),
                "edit_url": res.get("edit_url"),
                "emailed": res.get("emailed"),
                "author": h.get("author"),
                "comment_at": h.get("comment_at"),
            }
            _save_seen(state)

    return {
        "ok": True,
        "ado_mentions_found": len(ado_hits),
        "sfc_mentions_found": len(sfc_hits),
        "new_processed": len(processed),
        "results": processed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--test-latest", action="store_true", help="Draft + queue (no email) for the latest found mention")
    args = ap.parse_args()
    if args.test_latest:
        hits = scan_ado_mentions(days=14) + scan_sfc_mentions(days=30)
        if not hits:
            print(json.dumps({"ok": True, "note": "no recent mentions in window"}))
            return 0
        res = queue_and_notify(hits[0], dry_run=False, send_email=False)
        print(json.dumps(res, indent=2, default=str))
        return 0
    out = run_once(days=args.days, dry_run=args.dry_run, seed_only=args.seed)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
