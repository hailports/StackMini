"""Suggest one card from a real ask, and hold it until a person clicks.

Opening a link does not create a card. A test token cannot create one.
The same source and the same ask are not suggested twice. Search failures
and existing matches fail closed.
"""

from __future__ import annotations

import hashlib
import html
import re
import time

from hailports.ticket_guard import key_for

DAILY_CAP = 10
WINDOW_S = 14 * 86400
TTL_S = 86400
DEFAULT_AREA = "Desk"
DEFAULT_ITERATION = "Current"

HARD = re.compile(
    r"(?i)(legal|finance|lawyer|attorney|accountant|\bcpa\b|bank|"
    r"password|passcode|seed phrase|verification code|\b2fa\b|\bmfa\b|\totp\b)"
)
HARD_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
TELLS = re.compile(r"(?i)\b(as an assistant|chatbot)\b|—")
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
    r"(?i)^(re:\s*)?(tagged on tracker|user story \d|bug \d|meeting calendar|meeting summaries|"
    r"your meeting\b|accepted:|declined:|canceled:|cancelled:)"
)
INTERNAL = re.compile(
    r"(?i)(\[local\b|:8080|\bprep\]|idlefill|packet depth|self-note|suggested:)"
)
_WORDS = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")
_STOP = {
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "have",
    "please", "can", "could", "would", "need", "just", "about", "they", "them",
    "are", "was", "were", "has", "not", "but", "its", "our", "out", "into",
}
_ID = re.compile(r"\b(\d{5,8})\b")


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
    return key_for(requester or "", title or "")


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
    if re.search(r"(?i)\b(record store|crm)\b", blob) and re.search(r"(?i)\b(change|update|add|fix)\b", blob):
        return "record_change"
    if DEFECT.search(blob):
        return "bug"
    return "request"


def already_tracked(text: str, known_ids: set) -> bool:
    known = {str(item) for item in (known_ids or set())}
    return any(num in known for num in _ID.findall(text or ""))


def fuzzy_duplicate(title: str, description: str, rows: list, now: float | None = None) -> dict:
    """rows: title, description, ref, ts. Any state, last 14 days."""
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


def _day(now: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(now))


def allow(store: dict, source: str, idem: str, now: float, cap: int = DAILY_CAP) -> tuple:
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
    sent = int((store.get("days") or {}).get(_day(now)) or 0)
    if sent >= cap:
        return False, "cap"
    return True, "ok"


def mark(store: dict, source: str, idem: str, now: float, how: str) -> dict:
    store = dict(store or {})
    sources = dict(store.get("sources") or {})
    sources[source] = {"at": now, "how": how, "idem": idem}
    store["sources"] = sources
    if idem:
        idem_map = dict(store.get("idem") or {})
        idem_map[idem] = {"at": now, "how": how, "source": source}
        store["idem"] = idem_map
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
    return store


def scrub(text: str) -> str:
    text = (text or "").replace("—", ", ").replace("–", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    text = TELLS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    return text


def grounded(story: dict, source: str) -> tuple:
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


def _operator_speaker(name: str, names: tuple) -> bool:
    blob = (name or "").casefold()
    return any(tok and tok.casefold() in blob for tok in names)


def draft_from_source(title: str, body: str, who: str, url: str, parent: str = "", operator_names: tuple = ()) -> dict:
    """Copy a card from the source text. No new facts."""
    kind = classify(title, body)
    blob = "{0}\n{1}".format(title, body)
    wtype = "Bug" if kind in ("bug", "access") and DEFECT.search(blob) else "User Story"
    if kind == "access":
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
    if _operator_speaker(who, operator_names):
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
    if match and match.group(1) in blob:
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


def pick(rows: list, known_ids: set, store: dict, catalog: list, now: float, operator_names: tuple = ()) -> dict | None:
    """One ask that is not already a card. No model call."""
    best = None
    best_score = -1
    for row in rows or []:
        title = str(row.get("title") or "")
        body = str(row.get("body") or "")
        url = str(row.get("url") or "")
        if hard_blocked(title + "\n" + body, url):
            continue
        if INTERNAL.search(title) or INTERNAL.search(body[:500]) or INTERNAL.search(url):
            continue
        if already_tracked(title + "\n" + body[:400], known_ids):
            continue
        kind = classify(title, body, url)
        if not kind:
            continue
        ask = ""
        for raw in body.splitlines():
            if STRONG.search(raw) and not hard_blocked(raw):
                ask = raw
                break
        if _operator_speaker(_speaker(ask), operator_names):
            continue
        who = str(row.get("who") or "")
        story = draft_from_source(title, body, who, url, operator_names=operator_names)
        if not story.get("grounded"):
            continue
        src = source_key(str(row.get("source") or ""), str(row.get("ref") or ""))
        idem = idempotency_key(story.get("requester") or who, story.get("title") or "")
        ok, _why = allow(store, src, idem, now)
        if not ok:
            continue
        found = fuzzy_duplicate(story.get("title") or "", story.get("description") or "", catalog, now)
        if found.get("duplicate"):
            continue
        score = 0
        if row.get("source") == "mail":
            score += 5000
        elif row.get("source") == "chat":
            score += 2000
        if kind == "access":
            score += 1000
        if score > best_score:
            best_score = score
            best = {
                "source_key": src,
                "idem_key": idem,
                "kind": kind,
                "story": story,
                "source": row.get("source") or "",
                "ref": row.get("ref") or "",
            }
    return best


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


def render_suggestion(story: dict, approve_url: str, edit_url: str, dismiss_url: str) -> str:
    """The note body. This does not send mail and does not create a card."""
    return email_html(story, approve_url, edit_url, dismiss_url)


def _story_of(item: dict) -> dict:
    return dict(((item.get("action") or {}).get("payload") or {}).get("story") or {})


def _dry(item: dict) -> bool:
    payload = (item.get("action") or {}).get("payload") or {}
    return bool(item.get("dry_run") or payload.get("dry_run"))


def _expired(item: dict, now: float) -> bool:
    exp = item.get("expires")
    return bool(exp) and now > float(exp) and item.get("status") == "pending"


def decide(item: dict, form: dict | None, intent: str, search_fn, create_fn, now: float | None = None) -> dict:
    """Click handler. A preview does not search or create. A dry token never creates."""
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
        err = created.get("error") if isinstance(created, dict) else "create_failed"
        return {"ok": False, "error": err or "create_failed", "mutated": False, "created": False}
    return {
        "ok": True, "mutated": True, "created": True, "status": "approved",
        "work_item_id": str(created.get("work_item_id")), "story": story,
    }


def apply_status(item: dict, result: dict) -> dict:
    """Single-use token. A finished click cannot create again."""
    item = dict(item)
    if result.get("status") == "approved":
        item["status"] = "approved"
    if result.get("status") == "denied":
        item["status"] = "denied"
        item["deny_confirmed"] = True
    return item


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
        link = "/cards/" + re.sub(r"[^A-Za-z0-9_-]", "", str(result.get("work_item_id")))
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
