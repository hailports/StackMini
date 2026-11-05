#!/usr/bin/env python3
"""Suggest one tracker item from a real ask, and hold it for a click.

Nothing is created until Approve & Post or Save & Post. A test token cannot
create. The same source, the same ask, and a similar title already mailed are not
suggested twice. Mail goes only through the caller's sender, which on the
mini is the self-note path.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

DAILY_CAP = 10
WINDOW_S = 14 * 86400
TTL_S = 86400

HARD = re.compile(
    r"(?i)(legal|finance|lawyer|attorney|accountant|\bcpa\b|bank|"
    r"password|passcode|seed phrase|verification code|\b2fa\b|\bmfa\b|\botp\b)"
)
HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
TELLS = re.compile(
    r"(?i)\b(ai|llm|qwen|grok|cursor|claude|gpt|chatgpt|copilot|bot|automat\w*|"
    r"as an assistant)\b|—"
)
STRONG = re.compile(
    r"(?i)("
    r"please (?:add|change|fix|create|update|grant|look)|"
    r"can you (?:please )?(?:add|change|fix|create|update|grant|look|check)|"
    r"could you (?:please )?(?:add|change|fix|create|update|grant|look|check)|"
    r"need access|needs access|no access|"
    r"not working|aren'?t working|are not working|doesn'?t work|cannot log|can'?t log|"
    r"none of (?:their|the|our).{0,48}working|"
    r"usernames? (?:are|aren'?t|are not) working|"
    r"is broken|are broken|is failing|are failing|"
    r"we need to (?:change|fix|add|update)"
    r")"
)
DEFECT = re.compile(r"(?i)\b(bug|broken|error|fail(?:ing|s|ed)?|not working|doesn't work|cannot)\b")
SKIP_TITLE = re.compile(
    r"(?i)^(re:\s*)?(tagged on ado|user story \d|bug \d|zoom calendar|zoom meeting summaries|"
    r"your meeting\b|accepted:|declined:|canceled:|cancelled:)"
)
INTERNAL = re.compile(
    r"(?i)(\[local\b|:8080|\bprep\]|idlefill|packet depth|work[-_ ]brain|self-note|suggested:)"
)
_WORDS = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")
_STOP = {
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "have",
    "please", "can", "could", "would", "need", "just", "about", "they", "them",
    "are", "was", "were", "has", "not", "but", "its", "our", "out", "into",
}
DEFAULT_AREA = "operator\\operator"
DEFAULT_ITERATION = "operator"


def hard_blocked(text: str, url: str = "") -> bool:
    blob = "{0}\n{1}".format(url or "", text or "")
    if HARD_PATH.search(blob):
        return True
    return bool(HARD.search(blob))


def keywords(text: str) -> list:
    out = []
    for word in _WORDS.findall((text or "").lower()):
        if word in _STOP or word.isdigit():
            continue
        if word not in out:
            out.append(word)
    out.sort(key=lambda w: (-len(w), w))
    return out[:8]


def idempotency_key(requester: str, title: str) -> str:
    try:
        import ado_create_guard as guard
        return guard.idempotency_key(requester or "", title or "")
    except Exception:
        raw = (requester or "").strip().lower() + "\n" + "\n".join(keywords(title))
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def source_key(source: str, ref: str) -> str:
    return hashlib.sha1("{0}|{1}".format(source or "", ref or "").encode("utf-8", "replace")).hexdigest()[:20]


def classify(title: str, body: str, url: str = "") -> str:
    if hard_blocked("{0}\n{1}".format(title, body), url):
        return ""
    if SKIP_TITLE.search(title or ""):
        return ""
    blob = "{0}\n{1}".format(title or "", body or "")
    if not STRONG.search(blob):
        return ""
    if re.search(r"(?i)\b(access|permission|username|log ?in)\b", blob):
        return "access"
    if re.search(r"(?i)\b(operator|sf-|crm)\b", blob) and re.search(r"(?i)\b(change|update|add|fix)\b", blob):
        return "sf_change"
    if DEFECT.search(blob):
        return "bug"
    return "request"


def fuzzy_duplicate(title: str, description: str, rows: list, now: float | None = None) -> dict:
    """rows: dicts title, description, ref, ts. Last 14 days, any state."""
    now = time.time() if now is None else now
    keys = keywords(title)[:6]
    if len(keys) < 2:
        return {"duplicate": True, "reason": "thin"}
    wanted = set(keys)
    for row in rows or []:
        ts = float(row.get("ts") or 0)
        if ts and now - ts > WINDOW_S:
            continue
        other = set(keywords("{0} {1}".format(row.get("title") or "", (row.get("description") or "")[:400])))
        if not other:
            continue
        inter = wanted & other
        if len(inter) >= 3 or (len(wanted) and len(inter) / len(wanted) >= 0.6 and len(inter) >= 2):
            return {"duplicate": True, "reason": "fuzzy", "ref": row.get("ref"), "overlap": sorted(inter)}
    return {"duplicate": False, "reason": "clear"}


def title_key(title: str) -> str:
    """Stable id from title keywords. Requester text is not part of it."""
    raw = "\n".join(keywords(title))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _within_window(entry, now: float) -> bool:
    if isinstance(entry, dict) and entry.get("at"):
        try:
            return now - float(entry["at"]) <= WINDOW_S
        except (TypeError, ValueError):
            return True
    return True


_NONE_WORD = re.compile(r"(?i)^(?:none|n/?a|null|empty|tbd)$")


def _real_acceptance(acceptance) -> list:
    if isinstance(acceptance, str):
        acceptance = [part.strip() for part in acceptance.split(";")]
    kept = []
    for item in acceptance or []:
        text = scrub(str(item))
        if not text:
            continue
        parts = [part for part in re.split(r"[\s,;]+", text) if part]
        if parts and all(_NONE_WORD.fullmatch(part) for part in parts):
            continue
        kept.append(text)
    return kept


def thin_reason(title: str, acceptance=None) -> str:
    """Blank when the title has 3+ keywords and acceptance is not empty or none."""
    if len(keywords(title or "")) < 3:
        return "thin_title"
    if acceptance is None:
        return ""
    if not _real_acceptance(acceptance):
        return "thin_acceptance"
    return ""


def prior_title_hit(title: str, store: dict, now: float) -> dict:
    """Fuzzy match against titles already mailed or dismissed. Same overlap rules."""
    rows = []
    for row in (store or {}).get("mailed_titles") or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "title": row.get("title") or "",
            "description": " ".join(row.get("title_keys") or []),
            "ref": row.get("source") or row.get("idem") or "",
            "ts": row.get("at") or 0,
        })
    found = fuzzy_duplicate(title or "", "", rows, now)
    if found.get("reason") == "thin":
        return {"duplicate": False, "reason": "thin"}
    return found


def _note_title(store: dict, source: str, idem: str, now: float, how: str, title: str) -> dict:
    fp = title_key(title)
    if not fp:
        return store
    titles = dict(store.get("title_keys") or {})
    titles[fp] = {"at": now, "how": how, "idem": idem, "source": source}
    kept_keys = {}
    for key, rec in titles.items():
        if key == fp or _within_window(rec, now):
            kept_keys[key] = rec
    store["title_keys"] = kept_keys
    if how == "dismiss":
        never = set(store.get("never") or [])
        never.add(fp)
        store["never"] = sorted(never)
    if how in ("email", "dismiss", "sent"):
        rows = [row for row in (store.get("mailed_titles") or []) if isinstance(row, dict)]
        rows.append({
            "at": now,
            "title_keys": keywords(title),
            "title": (title or "")[:120],
            "idem": idem,
            "source": source,
        })
        cutoff = float(now) - WINDOW_S
        fresh = []
        for row in rows:
            try:
                at = float(row.get("at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at and at < cutoff:
                continue
            fresh.append(row)
        store["mailed_titles"] = fresh[-200:]
    return store


def _day(now: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(now))


def allow(store: dict, source: str, idem: str, now: float, cap: int = DAILY_CAP, title: str = "") -> tuple:
    store = store or {}
    if source and source in (store.get("sources") or {}):
        return False, "source"
    if idem and idem in (store.get("idem") or {}):
        return False, "idem"
    never = set(store.get("never") or [])
    if source and source in never:
        return False, "never"
    if idem and idem in never:
        return False, "never"
    fp = title_key(title) if title else ""
    if fp:
        if fp in never:
            return False, "never"
        rec = (store.get("title_keys") or {}).get(fp)
        if rec and _within_window(rec, now):
            return False, "title"
        if len(keywords(title)) < 3:
            return False, "thin_title"
        hit = prior_title_hit(title, store, now)
        if hit.get("duplicate"):
            return False, "title_fuzzy"
    sent = int((store.get("days") or {}).get(_day(now)) or 0)
    if sent >= cap:
        return False, "cap"
    return True, "ok"


def mark(store: dict, source: str, idem: str, now: float, how: str, title: str = "") -> dict:
    store = dict(store or {})
    store.setdefault("sources", {})[source] = {"at": now, "how": how, "idem": idem}
    if idem:
        store.setdefault("idem", {})[idem] = {"at": now, "how": how, "source": source}
    if how == "dismiss":
        never = set(store.get("never") or [])
        if source:
            never.add(source)
        if idem:
            never.add(idem)
        store["never"] = sorted(never)
    if how in ("sent", "email"):
        days = dict(store.get("days") or {})
        key = _day(now)
        days[key] = int(days.get(key) or 0) + 1
        store["days"] = days
    if title:
        store = _note_title(store, source, idem, now, how, title)
    return store


def scrub(text: str) -> str:
    text = (text or "").replace("—", ", ").replace("–", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    text = TELLS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    return text


def grounded(story: dict, source: str) -> tuple:
    """Every number and each content word in the title must be in the source."""
    blob = (source or "").lower()
    title = scrub(story.get("title") or "")
    desc = scrub(story.get("description") or "")
    if len(title) < 8 or TELLS.search(title) or TELLS.search(desc):
        return False, "tell_or_short"
    for number in re.findall(r"\d{2,}", title + " " + desc):
        if number not in (source or ""):
            return False, "number:" + number
    words = [w for w in keywords(title) if len(w) >= 5]
    if words and not any(w in blob for w in words):
        return False, "title_not_in_source"
    missing = [w for w in words if w not in blob]
    if len(missing) >= 2:
        return False, "invented"
    return True, "ok"


def _speaker(line: str) -> str:
    match = re.match(r"^([^:]{2,80}):\s+\S", (line or "").strip())
    return match.group(1).strip() if match else ""


def _operator_speaker(name: str) -> bool:
    return bool(re.search(r"(?i)operator|operator", name or ""))


def draft_from_source(title: str, body: str, who: str, url: str, parent: str = "") -> dict:
    """Copy a ticket from the source text. No new facts."""
    kind = "bug" if classify(title, body) == "bug" or (classify(title, body) == "access" and DEFECT.search(body or "")) else "request"
    wtype = "Bug" if kind in ("bug", "access") and DEFECT.search("{0}\n{1}".format(title, body)) else "User Story"
    if classify(title, body) == "access":
        wtype = "Bug"
    line = ""
    for raw in (body or "").splitlines():
        if STRONG.search(raw) and not hard_blocked(raw):
            line = scrub(raw)
            break
    if not line:
        line = scrub(body or title)
    speaker = _speaker(line)
    if not who or "@" in who or str(who).startswith("thread_") or len(who) > 80:
        who = speaker
    if _operator_speaker(who):
        who = ""
    subject = scrub(re.sub(r"(?i)^(re|fw|fwd)\s*:\s*", "", title or ""))
    ticket_title = subject[:70] if len(subject) >= 8 else scrub(line)[:70]
    description = line[:400]
    if who and who.lower() not in description.lower():
        description = "{0} asked: {1}".format(scrub(who), description)[:400]
    checks = []
    if line:
        checks.append(line[:160])
    checks.append("What works today still works.")
    parent_id = ""
    match = re.search(r"\b(\d{5,8})\b", parent or "")
    if match and match.group(1) in "{0}\n{1}".format(title, body):
        parent_id = match.group(1)
    story = {
        "type": wtype,
        "title": ticket_title[:90],
        "description": description,
        "acceptance": checks[:4],
        "area": DEFAULT_AREA,
        "iteration": DEFAULT_ITERATION,
        "parent": parent_id,
        "source_url": url if str(url).startswith("http") else "",
        "requester": scrub(who)[:80],
    }
    ok, reason = grounded(story, "{0}\n{1}\n{2}".format(title, body, who))
    story["grounded"] = ok
    story["ground_reason"] = reason
    return story


def parse_model(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        if key in ("title", "type", "description", "acceptance", "parent", "area", "iteration"):
            out[key] = val.strip()
    if out.get("type", "").lower().startswith("bug"):
        out["type"] = "Bug"
    elif out.get("type"):
        out["type"] = "User Story"
    if isinstance(out.get("acceptance"), str):
        out["acceptance"] = [p.strip() for p in out["acceptance"].split(";") if p.strip()][:4]
    return out


def merge_model(seed: dict, model: dict, source: str) -> dict:
    story = dict(seed)
    for key in ("title", "description", "area", "iteration", "parent"):
        val = scrub(str(model.get(key) or ""))
        if not val:
            continue
        if key == "parent":
            if not re.fullmatch(r"\d{5,8}", val) or val not in (source or ""):
                continue
        trial = dict(story)
        trial[key] = val[:400]
        ok, _reason = grounded(trial, source)
        if ok:
            story[key] = trial[key]
    if model.get("type") in ("Bug", "User Story"):
        story["type"] = model["type"]
    acc = model.get("acceptance") or []
    if isinstance(acc, list) and acc:
        kept = []
        for line in acc:
            trial = dict(story)
            trial["description"] = story.get("description") or ""
            # Acceptance may be a check written from the source words.
            if grounded({"title": story.get("title") or "placeholder title", "description": scrub(line)}, source)[0]:
                kept.append(scrub(line)[:180])
        if kept:
            story["acceptance"] = kept[:4]
    story["grounded"], story["ground_reason"] = grounded(story, source)
    return story


def email_html(story: dict, approve_url: str, edit_url: str, dismiss_url: str) -> str:
    checks = "".join("<li>{0}</li>".format(html.escape(c)) for c in (story.get("acceptance") or []))
    return """<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:680px;margin:20px auto;color:#1f2328;line-height:1.45">
  <div style="font-size:12px;font-weight:700;letter-spacing:.04em;color:#57606a">Suggested work item</div>
  <h2 style="margin:6px 0 8px;font-size:18px">{title}</h2>
  <p style="color:#57606a;font-size:13px">{typ} · Area {area} · Iteration {iteration}</p>
  <div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px">
    <div style="white-space:pre-wrap">{desc}</div>
    <ul>{checks}</ul>
    <p style="font-size:13px">Parent: {parent}<br>Source: {src}</p>
  </div>
  <p style="margin:18px 0">
    <a href="{approve}" style="display:inline-block;padding:10px 16px;background:#1a7f37;color:#fff;text-decoration:none;border-radius:6px;font-weight:600">Approve &amp; Post</a>
    <a href="{edit}" style="display:inline-block;padding:10px 16px;background:#0969da;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;margin-left:8px">Edit</a>
    <a href="{dismiss}" style="display:inline-block;padding:10px 16px;background:#f3f4f6;color:#24292f;text-decoration:none;border-radius:6px;margin-left:8px">Dismiss</a>
  </p>
  <p style="font-size:12px;color:#6e7781">Opening a link does not create anything. Approve &amp; Post asks once more, then creates this item. Nothing is sent to anyone else.</p>
</body></html>""".format(
        title=html.escape(story.get("title") or ""),
        typ=html.escape(story.get("type") or ""),
        area=html.escape(story.get("area") or ""),
        iteration=html.escape(story.get("iteration") or ""),
        desc=html.escape(story.get("description") or ""),
        checks=checks,
        parent=html.escape(story.get("parent") or "none"),
        src=html.escape(story.get("source_url") or ""),
        approve=html.escape(approve_url or ""),
        edit=html.escape(edit_url or ""),
        dismiss=html.escape(dismiss_url or ""),
    )


def edit_html(story: dict, post_url: str, sig: str) -> str:
    checks = "; ".join(story.get("acceptance") or [])
    fields = (
        ("title", "Title", story.get("title") or ""),
        ("type", "Type", story.get("type") or "User Story"),
        ("description", "Description", story.get("description") or ""),
        ("acceptance", "Acceptance", checks),
        ("area", "Area", story.get("area") or ""),
        ("iteration", "Iteration", story.get("iteration") or ""),
        ("parent", "Parent", story.get("parent") or ""),
        ("source_url", "Source", story.get("source_url") or ""),
    )
    blocks = []
    for name, label, val in fields:
        blocks.append(
            "<label style='display:block;margin:10px 0 4px;font-size:13px'>{label}</label>"
            "<textarea name='{name}' rows='2' style='width:100%;box-sizing:border-box'>{val}</textarea>".format(
                label=html.escape(label), name=html.escape(name), val=html.escape(val)
            )
        )
    return (
        "<!doctype html><html><body style='font-family:Segoe UI,sans-serif;max-width:680px;margin:24px auto'>"
        "<h1>Edit suggested work item</h1><form method='POST' action='{url}'>"
        "<input type='hidden' name='s' value='{sig}'>{fields}"
        "<p style='margin-top:14px'>"
        "<button type='submit' name='intent' value='post' style='padding:10px 16px;background:#1a7f37;color:#fff;border:0;border-radius:6px;font-weight:600'>Save &amp; Post</button> "
        "<button type='submit' name='intent' value='cancel' style='padding:10px 16px;background:#f3f4f6;border:0;border-radius:6px'>Cancel</button>"
        "</p></form>"
        "<p style='color:#666;font-size:13px'>Opening this page does not create or discard the suggestion.</p>"
        "</body></html>"
    ).format(url=html.escape(post_url), sig=html.escape(sig), fields="".join(blocks))


def apply_form(story: dict, form: dict) -> dict:
    updated = dict(story or {})
    for key in ("title", "type", "description", "area", "iteration", "parent", "source_url"):
        if key in form and str(form.get(key) or "").strip():
            updated[key] = scrub(str(form.get(key)))[:500]
    if "acceptance" in form:
        raw = str(form.get("acceptance") or "")
        updated["acceptance"] = [scrub(p)[:180] for p in raw.split(";") if scrub(p)][:4]
    if str(updated.get("type") or "").lower().startswith("bug"):
        updated["type"] = "Bug"
    elif updated.get("type"):
        updated["type"] = "User Story"
    return updated


def _story_of(item: dict) -> dict:
    return dict(((item.get("action") or {}).get("payload") or {}).get("story") or {})


def _dry(item: dict) -> bool:
    payload = (item.get("action") or {}).get("payload") or {}
    return bool(item.get("dry_run") or payload.get("dry_run"))


def _expired(item: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    exp = item.get("expires")
    return bool(exp) and now > float(exp) and item.get("status") == "pending"


def decide(item: dict, form: dict | None, intent: str, search_fn, create_fn, now: float | None = None) -> dict:
    """Click handler. GET is not a valid intent. create_fn runs only for a real post.

    search_fn() -> {"ok": bool, "ids": [int]} . A search error or a hit fails closed.
    create_fn(story) -> {"ok": bool, "work_item_id": str}. Not called for dry_run.
    """
    now = time.time() if now is None else now
    intent = (intent or "").lower()
    if intent in ("", "get", "preview"):
        return {"ok": True, "mutated": False, "created": False, "preview": True}
    status = item.get("status") or "pending"
    if status == "approved":
        return {"ok": False, "error": "status_approved", "mutated": False, "created": False}
    if status == "denied" and item.get("deny_confirmed"):
        return {"ok": False, "error": "status_denied", "mutated": False, "created": False}
    if _expired(item, now):
        return {"ok": False, "error": "expired", "mutated": False, "created": False}
    if intent == "cancel":
        return {"ok": True, "mutated": False, "created": False, "cancelled": True, "status": "denied"}
    if intent == "dismiss":
        return {
            "ok": True, "mutated": False, "created": False, "dismissed": True,
            "status": "denied", "source_key": item.get("source_key") or "",
            "idem_key": item.get("idem_key") or "",
        }
    if intent not in ("approve", "post"):
        return {"ok": False, "error": "bad_intent", "mutated": False, "created": False}
    story = _story_of(item)
    if form:
        story = apply_form(story, form)
    if _dry(item):
        return {
            "ok": True, "mutated": False, "created": False, "dry_run": True,
            "status": "approved", "story": story,
        }
    if TELLS.search("{0} {1}".format(story.get("title") or "", story.get("description") or "")):
        return {"ok": False, "error": "tell", "mutated": False, "created": False}
    try:
        found = search_fn(story) if search_fn else {"ok": False}
    except Exception:
        found = {"ok": False}
    if not found or not found.get("ok"):
        return {"ok": False, "error": "fail_closed", "mutated": False, "created": False}
    ids = [i for i in (found.get("ids") or []) if i]
    if ids:
        return {
            "ok": False, "error": "duplicate", "mutated": False, "created": False,
            "work_item_id": str(ids[0]),
        }
    try:
        created = create_fn(story) if create_fn else {"ok": False}
    except Exception:
        return {"ok": False, "error": "create_failed", "mutated": False, "created": False}
    if not created or not created.get("ok") or not created.get("work_item_id"):
        return {"ok": False, "error": created.get("error") if isinstance(created, dict) else "create_failed",
                "mutated": False, "created": False}
    return {
        "ok": True, "mutated": True, "created": True, "status": "approved",
        "work_item_id": str(created.get("work_item_id")), "story": story,
    }


def confirm_html(result: dict) -> str:
    if result.get("dry_run"):
        body = "<p>Test only. No work item was created.</p>"
    elif result.get("cancelled"):
        body = "<p>Discarded. Nothing was created.</p>"
    elif result.get("dismissed"):
        body = "<p>Dismissed. This will not be suggested again.</p>"
    elif result.get("error") == "duplicate":
        wid = html.escape(str(result.get("work_item_id") or ""))
        body = "<p>Not created. A matching item already exists: {0}</p>".format(wid)
    elif result.get("created") and result.get("work_item_id"):
        wid = html.escape(str(result.get("work_item_id")))
        link = "https://operator/operator/operator/_workitems/edit/{0}".format(wid)
        body = "<p>Created <a href='{0}'>{1}</a>.</p>".format(html.escape(link), wid)
    elif result.get("preview"):
        body = "<p>Nothing was created.</p>"
    else:
        body = "<p>Not created. {0}</p>".format(html.escape(str(result.get("error") or "stopped")))
    ok = bool(result.get("ok"))
    color = "#0a7a32" if ok else "#8a5300"
    return (
        "<!doctype html><html><body style='font-family:Segoe UI,sans-serif;max-width:680px;margin:32px auto'>"
        "<h1 style='color:{0}'>{1}</h1>{2}</body></html>"
    ).format(color, "Done" if ok else "Not created", body)


# --- mini wiring -----------------------------------------------------------------

def _home(home: Path | None = None) -> Path:
    return home if home is not None else Path.home()


def _store_path(home: Path) -> Path:
    return home / "claude-stack/data/idle_filler/ticket_suggest_state.json"


def load_store(home: Path) -> dict:
    try:
        data = json.loads(_store_path(home).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_store(home: Path, store: dict) -> None:
    path = _store_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, default=str) + "\n")
    tmp.replace(path)


def _log(home: Path, msg: str) -> None:
    path = home / "claude-stack/data/idle_filler/ticket_suggest.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write("{0} {1}\n".format(time.strftime("%Y-%m-%dT%H:%M:%S"), msg))


def _catalog(con, now: float) -> list:
    rows = []
    try:
        found = con.execute(
            "select ref, ts, title, substr(body,1,400) from docs "
            "where source='ado' and ts>? limit 400",
            (now - WINDOW_S,),
        ).fetchall()
    except Exception:
        return rows
    for ref, ts, title, body in found:
        rows.append({"ref": ref, "ts": ts, "title": title or "", "description": body or ""})
    return rows


def scan(home: Path, now: float | None = None) -> dict | None:
    """Pick one ask that is not already a ticket. No model call."""
    home = _home(home)
    now = time.time() if now is None else now
    store = load_store(home)
    if store.get("pending"):
        return None
    try:
        import sqlite3
        path = home / "claude-stack/data/work_context.db"
        con = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=10)
        con.execute("PRAGMA query_only=ON")
    except Exception as exc:
        _log(home, "scan db {0}".format(type(exc).__name__))
        return None
    try:
        catalog = _catalog(con, now)
        known_ids = set()
        for (ref,) in con.execute("select ref from docs where source='ado' and ref like 'wi_%'"):
            wid = str(ref)[3:]
            if wid.isdigit():
                known_ids.add(wid)
        rows = con.execute(
            "select source, ref, ts, title, substr(body,1,1200), url, substr(participants,1,120) "
            "from docs where source in ('email','zoom','sp_corpus','playbooks') and ts>? "
            "order by ts desc limit 180",
            (now - WINDOW_S,),
        ).fetchall()
    finally:
        con.close()
    best = None
    for source, ref, ts, title, body, url, who in rows:
        if hard_blocked("{0}\n{1}".format(title, body), url or ""):
            continue
        if INTERNAL.search(title or "") or INTERNAL.search((body or "")[:500]) or INTERNAL.search(url or ""):
            continue
        kind = classify(title or "", body or "", url or "")
        if not kind:
            continue
        named = set(re.findall(r"\b(\d{5,8})\b", "{0}\n{1}".format(title or "", (body or "")[:400])))
        if named & known_ids:
            continue
        ask = ""
        for raw in (body or "").splitlines():
            if STRONG.search(raw) and not hard_blocked(raw):
                ask = raw
                break
        if _operator_speaker(_speaker(ask)):
            continue
        sk = source_key(source, str(ref).split("@", 1)[0])
        story = draft_from_source(title or "", body or "", who or "", url or "")
        if not story.get("grounded"):
            continue
        idem = idempotency_key(story.get("requester") or who or "", story.get("title") or "")
        titled = story.get("title") or ""
        ok, reason = allow(store, sk, idem, now, title=titled)
        if not ok:
            if reason in ("title", "title_fuzzy"):
                store = mark(store, sk, idem, now, "dup", title=titled)
            elif reason == "thin_title":
                store = mark(store, sk, idem, now, "thin", title=titled)
            continue
        if thin_reason(titled, story.get("acceptance")):
            store = mark(store, sk, idem, now, "thin", title=titled)
            continue
        dup = fuzzy_duplicate(story["title"], story["description"], catalog, now)
        if dup.get("duplicate"):
            store = mark(store, sk, idem, now, "dup")
            continue
        score = float(ts or 0)
        if source == "email":
            score += 5000
        elif source == "zoom":
            score += 2000
        if kind == "access":
            score += 1000
        if best is None or score > best[0]:
            best = (score, {
                "source": source,
                "ref": str(ref)[:180],
                "source_key": sk,
                "idem_key": idem,
                "kind": kind,
                "title": title or "",
                "body": body or "",
                "who": who or "",
                "url": url or "",
                "story": story,
            })
    save_store(home, store)
    if not best:
        _log(home, "scan none")
        return None
    store = load_store(home)
    store["pending"] = best[1]
    save_store(home, store)
    _log(home, "scan pending kind={0} key={1}".format(best[1]["kind"], best[1]["source_key"][:8]))
    return best[1]


def _served_gap(home: Path) -> bool:
    try:
        import open_ado_loop as loop
        raw = loop.state_path(home).read_text()
        served = int((json.loads(raw) or {}).get("served") or 0)
    except Exception:
        served = 0
    last = int((load_store(home).get("suggest_at_served") or 0))
    return (served - last) >= 3


def refresh(home: Path) -> None:
    """Look for one new ask. Call this while a model call is already running."""
    home = _home(home)
    store = load_store(home)
    if store.get("pending"):
        return
    if time.time() - float(store.get("scanned") or 0) < 90:
        return
    store["scanned"] = time.time()
    save_store(home, store)
    scan(home)


def due(home: Path) -> bool:
    home = _home(home)
    store = load_store(home)
    return bool(store.get("pending")) and _served_gap(home)


def claim(home: Path, force: bool = False) -> dict | None:
    home = _home(home)
    store = load_store(home)
    pending = store.get("pending") or {}
    if not pending.get("story"):
        return None
    if not force and not _served_gap(home):
        return None
    story = pending.get("story") or {}
    if not story.get("title"):
        return None
    source = "{0}\n{1}\n{2}".format(pending.get("title") or "", pending.get("body") or "", pending.get("who") or "")
    prompt = (
        "Write one work item from the source. Use only words and numbers that appear in the source.\n"
        "TITLE: a short title from the source\n"
        "TYPE: Bug or User Story\n"
        "DESCRIPTION: one sentence copied from the source\n"
        "ACCEPTANCE: checks separated by semicolons\n"
        "PARENT: a work item number from the source, or blank\n\n"
        "SOURCE\n{0}"
    ).format(source[:1800])
    return {
        "prompt": prompt,
        "cap": 500,
        "grammar": (
            'root ::= "TITLE: " [A-Za-z0-9] [^\\n]{8,80} "\\n" '
            '"TYPE: " ("Bug" | "User Story") "\\n" '
            '"DESCRIPTION: " [^\\n]{12,220} "\\n" '
            '"ACCEPTANCE: " [^\\n]{8,180} "\\n" '
            '"PARENT: " [0-9]{0,8} "\\n"'
        ),
        "pending": pending,
    }


def finish(home: Path, job: dict, text: str, sender=None) -> dict:
    """Turn one model reply into one email. Does not create a work item."""
    home = _home(home)
    pending = job.get("pending") or {}
    source = "{0}\n{1}\n{2}".format(pending.get("title") or "", pending.get("body") or "", pending.get("who") or "")
    seed = pending.get("story") or draft_from_source(
        pending.get("title") or "", pending.get("body") or "", pending.get("who") or "", pending.get("url") or ""
    )
    model = parse_model(text or "")
    story = merge_model(seed, model, source) if model else seed
    if not story.get("grounded"):
        story = seed
    if not story.get("grounded"):
        store = load_store(home)
        store["pending"] = None
        store = mark(store, pending.get("source_key") or "", pending.get("idem_key") or "", time.time(), "hold")
        save_store(home, store)
        _log(home, "hold ungrounded")
        return {"ok": False, "status": "hold"}
    queued = queue_email(home, pending, story, sender=sender)
    return queued


def queue_email(home: Path, pending: dict, story: dict, sender=None) -> dict:
    """Write the approval file and email the operator. Create is not called."""
    home = _home(home)
    now = time.time()
    store = load_store(home)
    titled = story.get("title") or ""
    sk = pending.get("source_key") or ""
    idem = pending.get("idem_key") or ""
    ok, reason = allow(store, sk, idem, now, title=titled)
    if not ok:
        if reason in ("title", "title_fuzzy"):
            store = mark(store, sk, idem, now, "dup", title=titled)
        elif reason == "thin_title":
            store = mark(store, sk, idem, now, "thin", title=titled)
        store["pending"] = None
        save_store(home, store)
        _log(home, "skip {0}".format(reason))
        return {"ok": False, "status": reason}
    gap = thin_reason(titled, story.get("acceptance"))
    if gap:
        store = mark(store, sk, idem, now, "thin", title=titled)
        store["pending"] = None
        save_store(home, store)
        _log(home, "skip {0}".format(gap))
        return {"ok": False, "status": gap}
    import sys
    cli = str(home / "llm-appliance/cli")
    tools = str(home / "claude-stack/tools")
    for p in (cli, tools):
        if p not in sys.path:
            sys.path.insert(0, p)
    import work_graph_exec as wge
    action = {
        "kind": "ado_suggest",
        "payload": {"story": story, "dry_run": False, "source_key": pending.get("source_key") or ""},
    }
    queued = wge.queue_approval(pending.get("source_key") or "suggest", action)
    if not queued.get("approve_url"):
        _log(home, "queue blocked")
        return {"ok": False, "status": "blocked"}
    token = queued["token"]
    path = wge.APPROVALS / "{0}.json".format(token)
    item = json.loads(path.read_text())
    item["source_key"] = pending.get("source_key") or ""
    item["idem_key"] = pending.get("idem_key") or ""
    path.write_text(json.dumps(item, indent=2, default=str) + "\n")
    base = wge.approve_base()
    dismiss_url = "{0}/dismiss/{1}?s={2}".format(base, token, queued.get("sig"))
    html_doc = email_html(story, queued.get("approve_url") or "", queued.get("deny_url") or "", dismiss_url)
    if TELLS.search(re.sub(r"(?is)<[^>]+>", " ", html_doc)):
        _log(home, "email tell blocked")
        return {"ok": False, "status": "tell"}
    folder = home / "claude-stack/data/idle_filler/suggest_mail"
    folder.mkdir(parents=True, exist_ok=True)
    html_path = folder / "{0}.html".format(token)
    html_path.write_text(html_doc)
    subject = "Suggested: {0}".format((story.get("title") or "work item")[:70])
    if sender is None:
        import threading
        import ops_work_digest as owd

        def _mail() -> None:
            try:
                os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")
                mailed = owd.send_self_alert(subject=subject, html_path=html_path)
                ok = bool((mailed or {}).get("ok"))
                _log(home, "email ok={0} token={1} rc={2}".format(ok, token[:8], "0" if ok else (mailed or {}).get("rc")))
            except Exception as exc:
                _log(home, "email err {0}".format(type(exc).__name__))

        threading.Thread(target=_mail, name="suggest-mail", daemon=False).start()
        emailed = True
    else:
        mailed = sender(subject=subject, html_path=html_path)
        emailed = bool((mailed or {}).get("ok"))
        _log(home, "email ok={0} token={1}".format(emailed, token[:8]))
    store = mark(
        store, pending.get("source_key") or "", pending.get("idem_key") or "", now,
        "email" if emailed else "email_fail", title=titled if emailed else "",
    )
    store["pending"] = None
    store["suggest_at_served"] = int(store.get("suggest_at_served") or 0)
    try:
        import open_ado_loop as loop
        loop_state = json.loads(loop.state_path(home).read_text())
        store["suggest_at_served"] = int(loop_state.get("served") or 0)
    except Exception:
        pass
    store["last"] = {"token": token, "emailed": emailed, "title": story.get("title"), "at": now}
    save_store(home, store)
    _log(home, "email queued token={0}".format(token[:8]))
    return {"ok": emailed, "status": "emailed" if emailed else "email_fail", "token": token, "title": story.get("title")}


def remember_dismiss(home: Path, item: dict) -> None:
    home = _home(home)
    store = load_store(home)
    story = _story_of(item)
    store = mark(
        store, str(item.get("source_key") or ""), str(item.get("idem_key") or ""), time.time(), "dismiss",
        title=story.get("title") or "",
    )
    save_store(home, store)


def mint_test(home: Path, story: dict | None = None) -> dict:
    """A token that exercises the pages and cannot create a work item."""
    home = _home(home)
    import sys
    cli = str(home / "llm-appliance/cli")
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import work_graph_exec as wge
    story = story or {
        "type": "Bug", "title": "Test suggestion does not post",
        "description": "This is a dry run token.",
        "acceptance": ["No work item is created."],
        "area": DEFAULT_AREA, "iteration": DEFAULT_ITERATION, "parent": "", "source_url": "",
        "requester": "test",
    }
    action = {"kind": "ado_suggest", "payload": {"story": story, "dry_run": True}}
    queued = wge.queue_approval("suggest-test", action)
    path = wge.APPROVALS / "{0}.json".format(queued["token"])
    item = json.loads(path.read_text())
    item["dry_run"] = True
    item["source_key"] = "test"
    item["idem_key"] = "test"
    path.write_text(json.dumps(item, indent=2, default=str) + "\n")
    base = wge.approve_base()
    dismiss = "{0}/dismiss/{1}?s={2}".format(base, queued["token"], queued.get("sig"))
    return {
        "token": queued["token"],
        "sig": queued["sig"],
        "approve_url": queued["approve_url"],
        "edit_url": queued["deny_url"],
        "dismiss_url": dismiss,
    }


def exec_approved(item: dict) -> dict:
    """Called by the approve server on POST. Dry-run never opens a client."""
    form = item.get("_form") or None
    intent = item.get("_intent") or "approve"

    def search_fn(story: dict) -> dict:
        import sys
        tools = str(Path.home() / "claude-stack/tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import ado_create_guard as guard
        import ado_client as ado
        os.environ["ADO_CDP"] = os.environ.get("WORK_GRAPH_ADO_CDP") or "http://127.0.0.1:9227"
        if ":18820" in os.environ["ADO_CDP"]:
            return {"ok": False, "error": "refuse_edge"}
        bridge = ado.AdoBridge().connect()
        try:
            def _query(wiql):
                data = bridge.api(
                    "POST",
                    "operator/_apis/wit/wiql?api-version=7.0&$top=20",
                    body={"query": wiql},
                ) or {}
                ids = []
                for row in (data.get("workItems") or []):
                    try:
                        ids.append(int(row.get("id")))
                    except (TypeError, ValueError, AttributeError):
                        continue
                return ids
            decision = guard.acquire(
                story.get("requester") or "", story.get("title") or "", _query,
                source="ticket_suggest", write=True,
            )
        finally:
            try:
                bridge.close()
            except Exception:
                pass
        if decision.get("action") == "skip" and decision.get("work_item_id"):
            return {"ok": True, "ids": [int(decision["work_item_id"])]}
        if decision.get("action") == "create":
            item["_guard_key"] = decision.get("key") or ""
            return {"ok": True, "ids": []}
        return {"ok": False, "error": decision.get("error") or "no_search"}

    def create_fn(story: dict) -> dict:
        import sys
        tools = str(Path.home() / "claude-stack/tools")
        cli = str(Path.home() / "llm-appliance/cli")
        for p in (tools, cli):
            if p not in sys.path:
                sys.path.insert(0, p)
        import ado_client as ado
        import ado_create_guard as guard
        from tools import ado_sf_comment_gates as gates
        ado.assert_no_ai_trace(story.get("title") or "")
        desc = "<div>{0}</div>".format(html.escape(story.get("description") or ""))
        if story.get("source_url"):
            desc += "<p>Source: {0}</p>".format(html.escape(story["source_url"]))
        ac = "<ul>" + "".join("<li>{0}</li>".format(html.escape(c)) for c in (story.get("acceptance") or [])) + "</ul>"
        gates.assert_work_item_field_safe(desc)
        gates.assert_work_item_field_safe(ac)
        wtype = "Bug" if story.get("type") == "Bug" else "User Story"
        field = "/fields/Microsoft.VSTS.TCM.ReproSteps" if wtype == "Bug" else "/fields/System.Description"
        ops = [
            {"op": "add", "path": "/fields/System.Title", "value": story.get("title")},
            {"op": "add", "path": "/fields/System.AreaPath", "value": story.get("area") or DEFAULT_AREA},
            {"op": "add", "path": "/fields/System.IterationPath", "value": story.get("iteration") or DEFAULT_ITERATION},
            {"op": "add", "path": "/fields/System.AssignedTo", "value": "redacted-email"},
            {"op": "add", "path": field, "value": desc},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": ac},
        ]
        parent = str(story.get("parent") or "")
        if parent.isdigit():
            ops.append({
                "op": "add", "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": "https://operator/operator/operator/_apis/wit/workItems/{0}".format(parent),
                },
            })
        bridge = ado.AdoBridge().connect()
        key = item.get("_guard_key") or ""
        try:
            try:
                created = bridge.api(
                    "POST",
                    "operator/_apis/wit/workitems/${0}?api-version=7.0".format(wtype.replace(" ", "%20")),
                    body=ops, content_type="application/json-patch+json",
                )
            except Exception:
                if parent.isdigit():
                    created = bridge.api(
                        "POST",
                        "operator/_apis/wit/workitems/${0}?api-version=7.0".format(wtype.replace(" ", "%20")),
                        body=ops[:-1], content_type="application/json-patch+json",
                    )
                else:
                    guard.release(key)
                    raise
            wid = str((created or {}).get("id") or "")
            if not wid:
                guard.release(key)
                return {"ok": False, "error": "no_id"}
            guard.commit(key, int(wid), requester=story.get("requester") or "", title=story.get("title") or "", source="ticket_suggest")
            return {"ok": True, "work_item_id": wid}
        finally:
            try:
                bridge.close()
            except Exception:
                pass

    return decide(item, form, intent, search_fn, create_fn)


def self_check() -> None:
    assert INTERNAL.search("[LOCAL :8080 PREP] ADO 375729")
    assert hard_blocked("reset the password", "")
    assert hard_blocked("notes", "Finance/q3.pdf")
    assert classify("Tagged on ADO #1: Pump", "please fix") == ""
    assert classify("Zoom chat", "Hey gents- can you both log out and back in? The warranty app should now be visible") == ""
    assert classify("Dealer login", "their usernames are not working and they need to order parts") == "access"
    rows = [{"ref": "wi_1", "ts": time.time(), "title": "Dealer usernames not working", "description": "order parts"}]
    dup = fuzzy_duplicate("Dealer usernames not working for parts", "order parts", rows)
    assert dup["duplicate"] is True
    clear = fuzzy_duplicate("Warranty table column on the product page", "show the warranty attribute", rows)
    assert clear["duplicate"] is False
    store = {}
    ok, _why = allow(store, "s1", "i1", 10)
    assert ok
    store = mark(store, "s1", "i1", 10, "email")
    assert allow(store, "s1", "i2", 10)[0] is False
    assert allow(store, "s2", "i1", 10)[0] is False
    for n in range(9):
        store = mark(store, "x{0}".format(n), "y{0}".format(n), 10, "email")
    assert allow(store, "new", "newidem", 10)[0] is False
    story = draft_from_source(
        "Dealer login",
        "their usernames are not working and they need to order parts",
        "Rayl, Melissa",
        "https://example.test/thread",
    )
    assert story["grounded"] is True
    assert grounded({"title": "Fourteen lambdas deployed", "description": "deployed fourteen lambdas"}, "usernames are not working")[0] is False
    item = {
        "status": "pending", "expires": time.time() + 100,
        "dry_run": True,
        "action": {"kind": "ado_suggest", "payload": {"story": story, "dry_run": True}},
    }
    calls = {"n": 0}

    def boom(_story):
        calls["n"] += 1
        return {"ok": True, "ids": []}

    preview = decide(item, None, "get", boom, boom)
    assert preview["preview"] and calls["n"] == 0 and preview["created"] is False
    posted = decide(item, None, "approve", boom, boom)
    assert posted["dry_run"] and posted["created"] is False and calls["n"] == 0
    edited = decide(item, {"title": "usernames are not working today"}, "post", boom, boom)
    assert edited["dry_run"] and edited["created"] is False and calls["n"] == 0
    assert edited["story"]["title"] == "usernames are not working today"
    item["status"] = "approved"
    again = decide(item, None, "approve", boom, boom)
    assert again["error"] == "status_approved" and calls["n"] == 0
    live = {
        "status": "pending", "expires": time.time() + 100,
        "action": {"payload": {"story": story, "dry_run": False}},
    }
    dup_hit = decide(live, None, "approve", lambda s: {"ok": True, "ids": [55]}, boom)
    assert dup_hit["error"] == "duplicate" and dup_hit["created"] is False and calls["n"] == 0
    closed = decide(live, None, "approve", lambda s: {"ok": False}, boom)
    assert closed["error"] == "fail_closed" and calls["n"] == 0
    gone = decide(dict(live, source_key="src-a", idem_key="idem-a"), None, "dismiss", boom, boom)
    assert gone["dismissed"] and calls["n"] == 0
    banned = mark({}, "src-a", "idem-a", 1, "dismiss")
    assert allow(banned, "src-a", "other", 2)[0] is False
    edited = apply_form(story, {"title": "Dealer usernames are not working", "acceptance": "They can order parts; Existing orders still work"})
    assert "order parts" in " ".join(edited["acceptance"]).lower() or "parts" in edited["acceptance"][0].lower()
    page = edit_html(story, "/deny/tok", "sig")
    assert "Save" in page and "Dealer" in page and "textarea" in page
    mail = email_html(story, "http://t/a", "http://t/e", "http://t/d")
    assert "Approve" in mail and "Edit" in mail and "Dismiss" in mail
    cancelled = decide(live, None, "cancel", boom, boom)
    assert cancelled["cancelled"] and calls["n"] == 0
    mailed = "Dealer login request for parts desk"
    other_ask = "Warranty table column on the product page"
    store = mark({}, "src-mailed", "idem-mailed", 100, "email", title=mailed)
    assert allow(store, "src-new", "idem-new", 110, title=mailed)[1] == "title"
    close = allow(store, "src-new2", "idem-new2", 110, title="Dealer login request for parts counter")
    assert close[0] is False and close[1] == "title_fuzzy"
    assert allow(store, "src-open", "idem-open", 110, title=other_ask)[0] is True
    assert allow({}, "src-thin", "idem-thin", 110, title="impacted by outage?")[1] == "thin_title"
    assert thin_reason("impacted by outage?", ["none", "none"]) == "thin_title"
    assert thin_reason(mailed, ["none none"]) == "thin_acceptance"
    assert thin_reason(mailed, ["They can order parts"]) == ""
    aged = mark({}, "src-old", "idem-old", 100 - (20 * 86400), "email", title=mailed)
    assert allow(aged, "src-later", "idem-later", 100, title=mailed)[0] is True
    _self_check_rescan()


def _self_check_rescan() -> None:
    """A second scan with a new ref and the same or a similar title does not queue."""
    import sqlite3
    import tempfile
    home = Path(tempfile.mkdtemp())
    db = home / "claude-stack/data/work_context.db"
    db.parent.mkdir(parents=True)
    now = 1_700_000_000.0
    body = "please add access so the parts desk can log in and order parts"
    title = "Dealer login request for parts desk"
    con = sqlite3.connect(db)
    con.execute(
        "create table docs (source text, ref text, ts real, title text, body text, url text, participants text)"
    )
    con.execute(
        "insert into docs values (?,?,?,?,?,?,?)",
        ("email", "msg-1", now, title, body, "https://example.test/1", "Riley North"),
    )
    con.commit()
    con.close()
    first = scan(home, now)
    assert first and first["story"]["title"] == title
    store = load_store(home)
    store["pending"] = None
    store = mark(store, first["source_key"], first["idem_key"], now, "email", title=first["story"]["title"])
    save_store(home, store)
    con = sqlite3.connect(db)
    rows = [
        ("email", "msg-2", now + 30, title, body, "https://example.test/2", "Quinn West"),
        ("email", "msg-3", now + 40, "Dealer login request for parts counter", body, "https://example.test/3", "Quinn West"),
        ("email", "msg-thin", now + 50, "impacted by outage?", "the report is broken and not working", "", "Riley North"),
        (
            "email", "msg-ok", now + 10,
            "Warranty table column on the product page",
            "please fix the warranty table column. it is broken and not working on the product page",
            "https://example.test/4", "Riley North",
        ),
    ]
    con.executemany("insert into docs values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    second = scan(home, now + 60)
    assert second is not None
    assert "warranty" in (second.get("story") or {}).get("title", "").lower()
    assert "dealer" not in (second.get("story") or {}).get("title", "").lower()
    assert "outage" not in (second.get("story") or {}).get("title", "").lower()
    saved = load_store(home)
    hows = {(row or {}).get("how") for row in (saved.get("sources") or {}).values() if isinstance(row, dict)}
    assert "email" in hows and "dup" in hows and "thin" in hows


if __name__ == "__main__":
    self_check()
    print("suggest check ok")
