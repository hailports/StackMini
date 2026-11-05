"""Ingest an ask, check history, run a safe runbook or package a change, draft from the result.

handle() still does not write operator. That is the draft-runner path.
chat_act() is the chat path: nondestructive operator work runs through the
operatorBot env, destructive work waits for GO, and a new user gets one ADO story.
Drafts are never sent.
"""
from __future__ import annotations

import fcntl
import random
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_DATA_LINE = re.compile(
    r"(?i)(?:"
    r"\b(?:lawyer|attorney|lawsuit|accountant|payroll)\b"
    r"|\bbank\s+(?:statement|account|routing)\b"
    r"|\bpassword\s*(?:is|:|=)\s*\S"
    r"|\b(?:passcode|one[- ]time(?:\s+code)?|verification code|seed phrase)\b"
    r"|\b(?:2fa|otp)\b"
    r"|\b\d{6}\b.{0,24}\b(?:code|password|passcode)\b"
    r")"
)
_ASK_SPLIT = re.compile(r"(?<=[?.!])\s+|\n+")
_NAME = re.compile(r"\b([A-Z][a-zA-Z'`-]{2,})(?:\s+([A-Z][a-zA-Z'`-]{2,}))?\b")
_SF_ID = re.compile(r"\b(005[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?)\b")
_CASE = re.compile(r"(?i)\bcase\s*(?:#|number|no\.?)?\s*:?\s*(\d{5,10})\b")
_TICKET = re.compile(r"\bSF-(\d{3,6})\b", re.I)
_NOT_NAME = {
    "hi", "hey", "hello", "the", "this", "that", "please", "thanks", "thank",
    "operator", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "operator", "sf", "operator", "user", "account", "access", "new", "can", "could",
    "would", "where", "what", "when", "how",
}
_FORBIDDEN_DASH = re.compile(r"—|–|--")
_UNSAFE_FILE = re.compile(
    r"(?i)deploy|rollback|delete|destroy|mutate|provision|password|permission|"
    r"create.?user|user.?creat|release"
)
_SAFE_FILE = re.compile(
    r"(?i)read[- ]only|lookup|diagnos|investigat|select only|no write|status|"
    r"report|diff"
)
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "runtime", "secrets", "legal",
    "finance", "teacher", "snapshots", "llama", "lora", ".venv", "venv",
}
_DEPS: dict = {}

ROUTES = (
    {
        "ask": "ado_item",
        "safety": "safe",
        "handler": "ado",
        "rx": re.compile(
            r"(?i)\b(?:please|can you|could you)\b.{0,60}"
            r"\b(?:create|open|add)\b.{0,40}"
            r"\b(?:user story|ado|work item|ticket|bug)\b"
        ),
    },
    {
        "ask": "customer_id",
        "safety": "needs_ok",
        "handler": "prepare",
        "rx": re.compile(
            r"(?i)(?:generate|create).{0,40}customer\s*id"
            r"|\bnew direct customer\b"
            r"|#\s*customer\s*id"
        ),
    },
    {
        "ask": "user_create",
        "safety": "needs_ok",
        "handler": "user_setup",
        "rx": re.compile(
            r"(?i)(?:"
            r"\b(?:create|set ?up|setup|provision|add)\b.{0,48}\b(?:user|account)s?\b"
            r"|\b(?:user|account)s?\b.{0,48}\b(?:created|create|set ?up|setup|provision)\b"
            r"|\bsf account\b|\boperator account\b|\bnew (?:user|hire)\b"
            r")"
        ),
    },
    {
        "ask": "perm_change",
        "safety": "needs_ok",
        "handler": "prepare",
        "rx": re.compile(
            r"(?i)\b(?:grant|give|add|assign|remove)\b.{0,40}\b(?:access|permission|permset|profile|role)\b"
        ),
    },
    {
        "ask": "deploy",
        "safety": "needs_ok",
        "handler": "prepare",
        "rx": re.compile(r"(?i)\b(?:deploy|deployment|rollback|release to prod)\b"),
    },
    {
        "ask": "user_status",
        "safety": "safe",
        "handler": "playbook",
        "rx": re.compile(r"(?i)\b(?:username|login name|which login)\b"),
    },
    {
        "ask": "login_help",
        "safety": "safe",
        "handler": "playbook",
        "rx": re.compile(
            r"(?i)\b(?:can'?t|cannot|unable to|won'?t)\b.{0,30}\b(?:log|sign) ?in\b"
            r"|\blocked out\b|\bfrozen\b|\bpassword reset\b|\bforgot (?:my |the )?password\b"
            r"|\blook into\b.{0,50}\blogin\b"
            r"|\bdoesn'?t see\b.{0,40}\b(?:tab|warranty|portal)\b"
        ),
    },
    {
        "ask": "access_diagnose",
        "safety": "safe",
        "handler": "playbook",
        "rx": re.compile(
            r"(?i)\b(?:can'?t|cannot|unable to|don'?t|doesn'?t)\b.{0,40}\b(?:see|edit|open|access|find)\b"
        ),
    },
    {
        "ask": "report_lookup",
        "safety": "safe",
        "handler": "playbook",
        "rx": re.compile(r"(?i)\b(?:report|dashboard|list view)\b"),
    },
    {
        "ask": "judgment",
        "safety": "safe",
        "handler": "judgment",
        "rx": re.compile(
            r"(?i)\b(?:likelihood|odds|how likely|do you think|what do you think|your take|should we)\b"
        ),
    },
    {
        "ask": "record_lookup",
        "safety": "safe",
        "handler": "records",
        "rx": re.compile(r"(?i)\b(?:case\s*#?\s*\d{5,}|SF-\d{3,}|opportunit|how many)\b"),
    },
    {
        "ask": "data_edit",
        "safety": "needs_ok",
        "handler": "prepare",
        "rx": re.compile(
            r"(?i)\b(?:please|can you|could you|need you to|go ahead and)\b.{0,40}"
            r"\b(?:update|delete|change|deactivate)\b"
        ),
    },
)

BUILTIN = (
    {"ask": "user_status", "safety": "safe", "title": "Which username / login",
     "path": "llm-appliance/cli/support_playbooks.py#username_lookup"},
    {"ask": "login_help", "safety": "safe", "title": "Dealer portal / SSO login",
     "path": "llm-appliance/cli/support_playbooks.py#portal_login"},
    {"ask": "access_diagnose", "safety": "safe", "title": "Profile / role / object access",
     "path": "llm-appliance/cli/support_playbooks.py#profile_role_access"},
    {"ask": "report_lookup", "safety": "safe", "title": "Report / dashboard access",
     "path": "llm-appliance/cli/support_playbooks.py#report_dashboard"},
    {"ask": "record_lookup", "safety": "safe", "title": "operator record lookup",
     "path": "llm-appliance/cli/sf_record_lookup.py"},
    {"ask": "user_create", "safety": "needs_ok", "title": "New operator user",
     "path": "claude-stack/data/playbooks/user_provisioning"},
    {"ask": "perm_change", "safety": "needs_ok", "title": "Permission set change",
     "path": "llm-appliance/cli/support_playbooks.py#license_permset"},
    {"ask": "data_edit", "safety": "needs_ok", "title": "operator data change",
     "path": "llm-appliance/cli/work_graph_exec.py#_package_operatorbot_tip"},
    {"ask": "deploy", "safety": "needs_ok", "title": "Deploy / release",
     "path": "claude-stack/docs/ops/operator/sf_release_deployment_runbook.md"},
)

_ASK_FILE = (
    ("user_create", re.compile(r"(?i)new user|user provision|create user|onboard")),
    ("perm_change", re.compile(r"(?i)permission set|permset|grant access")),
    ("deploy", re.compile(r"(?i)deploy|rollback|release")),
    ("login_help", re.compile(r"(?i)\blogin\b|sign-in|portal")),
    ("access_diagnose", re.compile(r"(?i)profile|role|object access|field visibility")),
    ("report_lookup", re.compile(r"(?i)report|dashboard")),
    ("record_lookup", re.compile(r"(?i)read-only|lookup|select ")),
)


def set_deps(**kwargs) -> None:
    _DEPS.update(kwargs)


def clear_deps() -> None:
    _DEPS.clear()


def enabled() -> bool:
    if os.environ.get("ASK_DO", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    return not (root_dir() / "OFF").exists()


def root_dir() -> Path:
    raw = os.environ.get("ASK_DO_DIR") or ""
    if raw:
        return Path(raw)
    return Path.home() / "llm-appliance/runtime/ask-do"


def data_line(text: str) -> str:
    """Skip lawyer, accountant, bank, password, and 2FA mail. Empty means ok."""
    if _DATA_LINE.search(text or ""):
        return "data_line"
    return ""


def _first(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    if "," in raw:
        rest = raw.split(",", 1)[1].strip()
        tok = rest.split()[0] if rest.split() else ""
    else:
        tok = raw.split()[0]
    tok = re.sub(r"[^A-Za-z'`-]", "", tok)
    if len(tok) < 2 or tok.lower() in _NOT_NAME:
        return ""
    return tok[0].upper() + tok[1:]


def _people(text: str) -> list[str]:
    out = []
    for m in _NAME.finditer(text or ""):
        first, last = m.group(1), m.group(2) or ""
        if first.lower() in _NOT_NAME:
            continue
        full = (first + (" " + last if last and last.lower() not in _NOT_NAME else "")).strip()
        if full not in out:
            out.append(full)
    return out[:6]


def _target(text: str) -> str:
    blob = text or ""
    m = re.search(r"(?i)\bfor\s+([A-Z][a-zA-Z'`-]{2,})\b", blob)
    if m and m.group(1).lower() not in _NOT_NAME and m.group(1).lower() != "operator":
        word = m.group(1)
        return word[0].upper() + word[1:]
    m = re.search(r"(?:^|\n)\s*([A-Z][a-zA-Z'`-]{2,})\s+[A-Z][a-zA-Z'`-]{2,}\s+-", blob)
    if m and m.group(1).lower() not in _NOT_NAME:
        word = m.group(1)
        return word[0].upper() + word[1:]
    names = _people(blob)
    for n in names:
        if n.split()[0].lower() not in ("operator",):
            return n.split()[0]
    return names[0].split()[0] if names else ""


def ingest(channel: str, who: str, subject: str, body: str) -> dict:
    blob = f"{subject or ''}\n{body or ''}".strip()
    asks = []
    for part in _ASK_SPLIT.split(blob):
        bit = re.sub(r"\s+", " ", part).strip()
        if len(bit) < 8:
            continue
        if re.search(r"(?i)\b(please|can you|could you|where|what|how|need|create|set up|access|username)\b", bit):
            asks.append(bit[:300])
    if not asks and len(blob) >= 8:
        asks.append(re.sub(r"\s+", " ", blob)[:300])
    entities = {
        "people": _people(blob),
        "sf_user_ids": _SF_ID.findall(blob),
        "cases": _CASE.findall(blob),
        "tickets": _TICKET.findall(blob),
    }
    return {
        "channel": channel,
        "requester": who or "",
        "first": _first(who),
        "subject": (subject or "")[:180],
        "asks": asks[:6],
        "topic": re.sub(r"\s+", " ", (subject or asks[0] if asks else ""))[:120],
        "target": _target(f"{subject or ''}\n{body or ''}"),
        "entities": entities,
        "text": blob[:2000],
    }


def _mask(text: str) -> str:
    t = _DATA_LINE.sub("[skipped]", text or "")
    t = re.sub(r"(?i)password\s*(?:is|:|=)\s*\S+", "password [skipped]", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


def _default_history(person: str, topic: str) -> list[dict]:
    """Sent mail and prior draft edits that mention this person. No bodies kept."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]{3,}", f"{person} {topic}") if t.lower() not in _NOT_NAME]
    tokens = tokens[:6]
    if not tokens:
        return []
    notes = []
    home = Path.home()
    sent = home / "llm-appliance/runtime/full-sweep/owa-sent-cache.json"
    try:
        if sent.is_file() and sent.stat().st_size < 2_000_000:
            data = json.loads(sent.read_text(errors="replace"))
            items = data.get("items") if isinstance(data, dict) else data
            for it in (items or [])[:80]:
                if not isinstance(it, dict):
                    continue
                blob = " ".join(str(it.get(k) or "") for k in ("subject", "preview", "to", "topic"))
                low = blob.lower()
                if any(tok in low for tok in tokens[:2]):
                    notes.append({"where": "sent", "note": _mask(it.get("subject") or blob)})
                if len(notes) >= 4:
                    break
    except Exception:
        pass
    edits = home / "claude-stack/data/learning/draft_edits.jsonl"
    try:
        if edits.is_file():
            lines = edits.read_text(errors="replace").splitlines()[-400:]
            for line in reversed(lines):
                low = line.lower()
                if not any(tok in low for tok in tokens[:2]):
                    continue
                if _DATA_LINE.search(line):
                    continue
                notes.append({"where": "draft_edits", "note": "similar ask handled before"})
                break
    except Exception:
        pass
    return notes[:5]


def _default_brain(question: str) -> dict:
    if data_line(question):
        return {"ok": True, "blocked": "data_line", "hits": []}
    try:
        import sys
        tools = str(Path.home() / "claude-stack/tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import work_brain_query as wb
        got = wb.ask(question, k=3, scope="all", graph=False)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "hits": []}
    hits = []
    for h in (got or {}).get("hits") or []:
        if not isinstance(h, dict):
            continue
        title = _mask(str(h.get("title") or ""))
        if title:
            hits.append({"title": title, "source": str(h.get("source") or "")[:40]})
    return {"ok": True, "blocked": (got or {}).get("blocked") or "", "hits": hits[:3]}


def _default_sf_query(soql: str) -> list:
    import sf_record_lookup as sf
    return sf.query(soql, limit=200)


def _default_playbook(text: str, people: list) -> list:
    import support_playbooks as sp
    return list(sp.facts_for(text, people or []) or [])


def _default_package(item_id: str, payload: dict) -> dict:
    if os.environ.get("ASK_DO_DRY") == "1":
        return {"ok": True, "dry": True, "status": "packaged_not_executed", "executed": False}
    import work_graph_exec as wge
    out = wge._package_operatorbot_tip(item_id, {"payload": payload})
    if not isinstance(out, dict):
        out = {"ok": False}
    out["executed"] = False
    out["status"] = "packaged_not_executed"
    return out


def _call(name: str, default):
    fn = _DEPS.get(name)
    return fn if fn is not None else default


def gather(ing: dict, *, remote: bool = False) -> dict:
    person = ing.get("requester") or ""
    topic = ing.get("topic") or ""
    history = _call("history", _default_history)(person, topic) or []
    brain = {"hits": []}
    if remote:
        q = f"{person} {topic}".strip()
        if q and not data_line(q):
            brain = _call("brain", _default_brain)(q) or {"hits": []}
    return {"history": list(history)[:5], "brain": brain}


def _read_head(path: Path, n: int = 1800) -> str:
    try:
        with path.open("r", errors="replace") as fh:
            return fh.read(n)
    except Exception:
        return ""


def classify_text(path: str, head: str) -> str:
    """safe or needs_ok. Unknown files stay needs_ok."""
    blob = f"{path}\n{head[:800]}"
    if re.search(r"(?i)deploy|rollback|delete|mutate|provision|password", path):
        return "needs_ok"
    if _UNSAFE_FILE.search(blob) and not re.search(r"(?i)read[- ]only", head[:600]):
        return "needs_ok"
    if _SAFE_FILE.search(head) or _SAFE_FILE.search(path):
        return "safe"
    return "needs_ok"


def _asks_for(path: str, head: str) -> list[str]:
    blob = f"{path}\n{head[:900]}"
    got = [ask for ask, rx in _ASK_FILE if rx.search(blob)]
    return got or ["record_lookup"]


def build_index(roots: list[str] | None = None) -> dict:
    entries = [dict(e) for e in BUILTIN]
    seen = {e["path"] for e in entries}
    if roots is None:
        home = Path.home()
        roots = [
            str(home / "claude-stack/docs"),
            str(home / "claude-stack/specs"),
            str(home / "claude-stack/data/playbooks"),
            str(home / "claude-stack/data/handoffs"),
            str(home / "llm-appliance/docs"),
        ]
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        if base.is_file():
            files = [base]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
                if any(seg.lower() in ("legal", "finance") for seg in Path(dirpath).parts):
                    continue
                for fn in filenames:
                    if not fn.lower().endswith((".md", ".txt", ".py")):
                        continue
                    if ".bak" in fn:
                        continue
                    files.append(Path(dirpath) / fn)
                    if len(files) > 400:
                        break
                if len(files) > 400:
                    break
        for path in files:
            try:
                if path.stat().st_size > 400_000:
                    continue
            except Exception:
                continue
            rel = str(path)
            home = str(Path.home())
            if rel.startswith(home):
                rel = rel[len(home):].lstrip("/")
            if rel in seen:
                continue
            head = _read_head(path)
            if not head.strip():
                continue
            if not re.search(r"(?i)runbook|playbook|read-only|steps:", head[:400] + " " + path.name):
                if "runbook" not in path.name.lower() and "playbook" not in path.name.lower():
                    continue
            safety = classify_text(rel, head)
            for ask in _asks_for(rel, head):
                # A file cannot mark a known destructive ask as safe.
                known = next((b for b in BUILTIN if b["ask"] == ask), None)
                if known and known["safety"] == "needs_ok":
                    safety = "needs_ok"
                entries.append({
                    "ask": ask,
                    "safety": safety if not (known and known["safety"] == "needs_ok") else "needs_ok",
                    "title": path.stem.replace("_", " ")[:80],
                    "path": rel,
                })
                seen.add(rel + "#" + ask)
    safe_n = sum(1 for e in entries if e["safety"] == "safe")
    ok_n = sum(1 for e in entries if e["safety"] == "needs_ok")
    idx = {"built": time.time(), "safe": safe_n, "needs_ok": ok_n, "entries": entries}
    return idx


def save_index(idx: dict | None = None) -> dict:
    idx = idx or build_index()
    path = root_dir() / "runbook-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(idx, indent=2) + "\n")
    return idx


def load_index() -> dict:
    path = root_dir() / "runbook-index.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("entries"):
                return data
        except Exception:
            pass
    return {"built": 0, "safe": sum(1 for e in BUILTIN if e["safety"] == "safe"),
            "needs_ok": sum(1 for e in BUILTIN if e["safety"] == "needs_ok"),
            "entries": [dict(e) for e in BUILTIN]}


def match(text: str, index: dict | None = None) -> dict | None:
    idx = index or load_index()
    entries = idx.get("entries") or []
    for route in ROUTES:
        if not route["rx"].search(text or ""):
            continue
        found = [e for e in entries if e.get("ask") == route["ask"]]
        if not found:
            found = [e for e in BUILTIN if e["ask"] == route["ask"]]
        # Known route safety wins. A scanned file cannot make user setup auto-run.
        return {
            "ask": route["ask"],
            "safety": route["safety"],
            "handler": route["handler"],
            "runbooks": found[:6],
        }
    return None


def _item_id(channel: str, key: str, ask: str) -> str:
    raw = f"{channel}|{key}|{re.sub(r'\\s+', ' ', (ask or '').lower())[:400]}"
    return "askdo-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")


def _lookup_user(name: str) -> list:
    if not name:
        return []
    soql = (
        "SELECT Id, Name, IsActive, Profile.Name FROM User "
        "WHERE Name = '%s' OR Name LIKE '%s %%'" % (_esc(name), _esc(name))
    )
    rows = _call("sf_query", _default_sf_query)(soql) or []
    return [r for r in rows if isinstance(r, dict)]


def _one_sentence(facts: list) -> str:
    for line in facts or []:
        t = str(line or "").strip()
        if t.startswith(("PLAYBOOK:", "RULE:", "STEPS:", "FIX-PENDING:", "RECEIPT:")):
            if t.startswith("FIX-PENDING:"):
                return "that change is not done yet"
            if t.startswith("RECEIPT:"):
                return "that's already done"
            continue
        t = re.sub(r"^SF:\s*", "", t)
        t = re.sub(r"\bUSER\b", "", t)
        t = re.sub(r"\b00[0-9A-Za-z]{12,18}\b", "", t)
        t = t.replace("--", ",").replace("\u2014", ",").replace("\u2013", ",")
        t = re.sub(r"\s+", " ", t).strip(" .")
        if not t:
            continue
        if t.startswith("I ") or t.startswith("I'"):
            pass
        elif t[0].isupper() and (len(t) < 2 or t[1].islower()):
            t = t[0].lower() + t[1:]
        return t
    return ""


def _package(item_id: str, ing: dict, route: dict) -> dict:
    payload = {
        "kind": "user_setup" if route["ask"] == "user_create" else route["ask"],
        "prepare_only": True,
        "execute": False,
        "needs_operator_ok": True,
        "who": ing.get("requester") or "",
        "target": ing.get("target") or "",
        "subject": (ing.get("subject") or "")[:180],
        "ask": (ing.get("text") or "")[:600],
        "org": "operator",
        "work_item_id": item_id,
    }
    out = _call("package", _default_package)(item_id, payload)
    if not isinstance(out, dict):
        out = {"ok": False}
    out["executed"] = False
    out["status"] = out.get("status") or "packaged_not_executed"
    return out


def _login_sentence(fix: dict) -> str:
    """One factual line. No username, email, password, or tool words."""
    if not isinstance(fix, dict) or not fix.get("ok"):
        return ""
    via = str(fix.get("via") or "")
    if via in ("operator_login_fix_need_users", "operator_login_fix_need_ident", "operator_login_fix_fallback"):
        return ""
    facts = str(fix.get("facts_block") or "")
    if "cleared stuck sign-in" in facts:
        return "I checked the operator login and cleared the stuck sign in. I did not change access"
    if "nothing to clear" in facts:
        return "I checked the operator login. Nothing is stuck, so I did not change anything"
    return ""


def _act_login(ing: dict, route: dict, ctx: dict) -> dict:
    sentence = ""
    changed = False
    if os.environ.get("ASK_DO_DRY") != "1":
        try:
            import operator_mail_work as _smw
            fix = _smw.login_fix_draft(ing.get("text") or "", write=True)
        except Exception:
            fix = {}
        sentence = _login_sentence(fix if isinstance(fix, dict) else {})
        changed = "cleared the stuck sign in" in sentence
    if not sentence:
        sentence = "I could not find that operator login from the note. I did not change anything"
    return {
        "handled": True,
        "ask": "login_help",
        "safety": "safe",
        "executed": changed,
        "execute": False,
        "sf_write": changed,
        "facts": [sentence],
        "history_n": len(ctx.get("history") or []),
        "runbooks": route.get("runbooks") or [],
    }


def _act_ado(ing: dict, route: dict, ctx: dict) -> dict:
    """One work item through the create guard. A miss stays prepare-only."""
    title = (ing.get("subject") or "work item").replace(" - ", ", ").replace("--", ", ")[:120]
    raw = (ing.get("text") or "")[:800]
    fact = "I have that ready for your go. I did not create a work item"
    wid = 0
    linked = False
    if os.environ.get("ASK_DO_DRY") != "1":
        ado = None
        try:
            ado = LiveAdo()
            ado._ask_requester = ing.get("requester") or ""
            res = ado.create(title, "<p>" + html.escape(raw, quote=False) + "</p>", "")
            wid = int((res or {}).get("id") or 0)
            linked = bool((res or {}).get("linked"))
        except Exception:
            wid = 0
        finally:
            if ado is not None:
                try:
                    ado.close()
                except Exception:
                    pass
    if wid and linked:
        fact = f"that work item is already open, {wid}. I did not make a second one"
    elif wid:
        fact = f"I created work item {wid}"
    return {
        "handled": True,
        "ask": "ado_item",
        "safety": "safe",
        "executed": bool(wid and not linked),
        "execute": False,
        "sf_write": False,
        "facts": [fact],
        "history_n": len(ctx.get("history") or []),
        "runbooks": route.get("runbooks") or [],
        "work_item_id": wid or "",
    }


def act(ing: dict, ctx: dict, route: dict) -> dict:
    """Run a safe runbook or package a change. operator writes stay with operatorBot."""
    if route["safety"] == "safe":
        if route.get("handler") == "ado":
            return _act_ado(ing, route, ctx)
        if route.get("ask") == "login_help":
            return _act_login(ing, route, ctx)
        if route.get("ask") == "judgment":
            return {
                "handled": True,
                "ask": "judgment",
                "safety": "safe",
                "executed": False,
                "execute": False,
                "sf_write": False,
                "facts": ["I don't have a call on that in the notes"],
                "history_n": len(ctx.get("history") or []),
                "runbooks": route.get("runbooks") or [],
            }
        facts: list = []
        if route["handler"] == "records":
            try:
                import sf_record_lookup as sf
                facts = list(sf.facts(ing.get("text") or "") or [])
            except Exception:
                facts = []
        else:
            try:
                facts = list(_call("playbook_facts", _default_playbook)(
                    ing.get("text") or "", ing.get("entities", {}).get("people") or []
                ) or [])
            except Exception:
                facts = []
        sentence = _one_sentence(facts)
        if not sentence and route.get("ask") == "record_lookup":
            sentence = "I don't have a cause for that in the notes yet"
        if not sentence:
            return {"handled": False, "ask": route["ask"], "safety": "safe"}
        return {
            "handled": True,
            "ask": route["ask"],
            "safety": "safe",
            "executed": True,
            "execute": False,
            "sf_write": False,
            "facts": [sentence],
            "history_n": len(ctx.get("history") or []),
            "runbooks": route.get("runbooks") or [],
        }
    found = []
    if route["ask"] == "user_create" and ing.get("target"):
        try:
            found = _lookup_user(ing["target"])
        except Exception:
            found = []
    if len(found) == 1 and found[0].get("IsActive") and found[0].get("Name"):
        return {
            "handled": True,
            "ask": route["ask"],
            "safety": "needs_ok",
            "done": True,
            "execute": False,
            "sf_write": False,
            "route": "already_in_operator",
            "needs_operator_ok": False,
            "target": ing.get("target") or "",
            "user_name": found[0].get("Name") or "",
            "runbooks": route.get("runbooks") or [],
            "history_n": len(ctx.get("history") or []),
        }
    iid = _item_id(ing.get("channel") or "", ing.get("subject") or "", ing.get("text") or "")
    pkg = _package(iid, ing, route)
    return {
        "handled": True,
        "ask": route["ask"],
        "safety": "needs_ok",
        "done": False,
        "execute": False,
        "sf_write": False,
        "route": "operatorbot_prepare_only",
        "needs_operator_ok": True,
        "status": "packaged_not_executed",
        "target": ing.get("target") or "",
        "package": {"status": pkg.get("status"), "executed": False, "dry": bool(pkg.get("dry"))},
        "runbooks": route.get("runbooks") or [],
        "history_n": len(ctx.get("history") or []),
        "id": iid,
    }


def draft_text(ing: dict, action: dict) -> str:
    who = ing.get("first") or _first(ing.get("requester") or "") or "there"
    if action.get("done"):
        target = action.get("target") or ing.get("target") or "they"
        return (
            f"hey {who},\n\n"
            f"{target}'s operator account is set up now. "
            f"{target} can log in with their operator SSO. let me know if anything is missing."
        )
    if action.get("route") == "operatorbot_prepare_only":
        if action.get("ask") == "customer_id":
            body = "I have the customer id ready for your go. I have not changed operator."
        else:
            target = action.get("target") or ""
            if target:
                body = f"I have {target} ready for your go. I have not changed operator."
            else:
                body = "I have that ready for your go. I have not changed operator."
        return f"hey {who},\n\n{body}"
    fact = (action.get("facts") or [""])[0]
    if not fact:
        return ""
    if _FORBIDDEN_DASH.search(fact):
        fact = _FORBIDDEN_DASH.sub(",", fact)
    return f"hey {who},\n\n{fact}."


def _write_item(rec: dict) -> None:
    iid = rec.get("id") or ""
    if not iid:
        return
    path = root_dir() / "items" / f"{iid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, default=str) + "\n")
    tmp.replace(path)


def handle(channel: str, who: str, subject: str, body: str, *, first: str = "",
           message_id: str = "", index: dict | None = None) -> dict | None:
    blob = f"{subject or ''}\n{body or ''}"
    if data_line(blob) or data_line(who or ""):
        return {
            "ok": False,
            "error": "hold:data_line",
            "text": "",
            "never_send": True,
            "sent": False,
            "skipped_noise": "data_line",
        }
    if not enabled() and os.environ.get("ASK_DO_DRY") != "1":
        return None
    ing = ingest(channel, who or first, subject, body)
    if first and not ing["first"]:
        ing["first"] = _first(first)
    route = match(ing["text"], index)
    ctx = gather(ing, remote=bool(route))
    if not route:
        return None
    action = act(ing, ctx, route)
    if not action.get("handled"):
        return None
    text = draft_text(ing, action)
    if not text or _FORBIDDEN_DASH.search(text):
        return None
    iid = action.get("id") or _item_id(channel, message_id or subject, ing["text"])
    rec = {
        "id": iid,
        "channel": channel,
        "message_id": message_id,
        "who": who,
        "subject": subject,
        "ask": route["ask"],
        "safety": route["safety"],
        "status": "done" if action.get("done") else action.get("status") or "done_safe",
        "execute": False,
        "needs_operator_ok": bool(action.get("needs_operator_ok")),
        "route": action.get("route") or "safe_runbook",
        "draft": text,
        "facts": action.get("facts") or [],
        "history": ctx.get("history") or [],
        "brain": (ctx.get("brain") or {}).get("hits") or [],
        "runbooks": [r.get("path") for r in (route.get("runbooks") or [])],
        "sf_write": False,
        "created": time.time(),
    }
    if action.get("executed") is True and action.get("route") != "operatorbot_prepare_only":
        rec["executed"] = True
    if action.get("work_item_id"):
        rec["work_item_id"] = action["work_item_id"]
    if os.environ.get("ASK_DO_DRY") != "1":
        try:
            _write_item(rec)
        except Exception:
            pass
    return {
        "ok": True,
        "text": text,
        "via": "ask_do",
        "never_send": True,
        "sent": False,
        "work_item_id": iid,
        "work": rec,
        "facts": rec["facts"],
        "questions": [],
    }


def intercept(channel: str, *, who: str, subject: str, body: str, first: str = "",
              message_id: str = "", log=None) -> dict | None:
    """Pull the shared context pack, then draft. A pack with no route still stays for the caller."""
    dc = None
    if os.environ.get("ASK_DO_DRY") != "1" and os.environ.get("DRAFT_CONTEXT_OFF") != "1":
        try:
            import draft_context as dc
            existing = dc.current()
            key_ok = existing and str(message_id or "") and str(message_id) in str(existing.get("key") or "")
            if not (key_ok and (existing.get("text") or existing.get("links"))):
                pack = dc.build_pack(
                    channel=channel,
                    subject=subject or "",
                    body=body or "",
                    ticket_id=message_id if str(message_id or "").isdigit() else "",
                    surface=channel,
                )
                dc.log_links(channel, message_id or subject or "", pack.get("links") or [])
                dc.stash(pack)
                if log:
                    kinds = ",".join(pack.get("kinds") or []) or "-"
                    log(f"draft_context n={len(pack.get('links') or [])} kinds={kinds}")
        except Exception as exc:
            dc = None
            if log:
                try:
                    log("draft_context_exc %s" % type(exc).__name__)
                except Exception:
                    pass
    _fc = None
    try:
        import fact_claim as _fc
        _fc.clear_lap()
    except Exception:
        _fc = None
    try:
        got = handle(channel, who, subject, body, first=first, message_id=message_id)
    except Exception as exc:
        if log:
            try:
                log(f"ask_do_exc {type(exc).__name__}")
            except Exception:
                pass
        return None
    if got and got.get("text") and dc is not None and not got.get("self_checked"):
        try:
            got = dc.guard(got, ask=body or subject or "", surface=channel, log=log)
        except Exception as exc:
            if log:
                try:
                    log("draft_guard_exc %s" % type(exc).__name__)
                except Exception:
                    pass
    if _fc is not None and got and isinstance(got.get("work"), dict):
        proof = dict(got["work"])
        if got.get("work_item_id"):
            proof["work_item_id"] = got["work_item_id"]
        _fc.set_lap(work=got["work"], proof=proof)
    if log and got and got.get("text"):
        try:
            log(f"ask_do {got.get('work', {}).get('ask')} id={got.get('work_item_id')}")
        except Exception:
            pass
    return got


def _ledger() -> dict:
    path = root_dir() / "user-ado.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_ledger(data: dict) -> None:
    path = root_dir() / "user-ado.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _load_user_live(sf_id: str) -> dict | None:
    q = _call("sf_query", _default_sf_query)
    rows = q(
        "SELECT Id, Name, Email, IsActive, Title, Country, TimeZoneSidKey, Profile.Name, "
        "UserRole.Name, Manager.Name, Regions__c, UserPermissionsSFContentUser, CreatedDate "
        "FROM User WHERE Id = '%s'" % _esc(sf_id)
    ) or []
    if not rows:
        return None
    r = rows[0]
    def _nm(v):
        return v.get("Name") if isinstance(v, dict) else v
    perms = q(
        "SELECT PermissionSet.Name FROM PermissionSetAssignment WHERE AssigneeId = '%s'" % _esc(sf_id)
    ) or []
    names = []
    for p in perms:
        ps = p.get("PermissionSet") or {}
        name = ps.get("Name") if isinstance(ps, dict) else ""
        if name and not name.startswith("X00"):
            names.append(name)
    groups = q(
        "SELECT Group.Name, Group.Type FROM GroupMember WHERE UserOrGroupId = '%s'" % _esc(sf_id)
    ) or []
    queues = []
    for g in groups:
        gd = g.get("Group") or {}
        if isinstance(gd, dict) and gd.get("Type") == "Queue" and gd.get("Name"):
            queues.append(gd["Name"])
    created = str(r.get("CreatedDate") or "")
    return {
        "Id": r.get("Id") or sf_id,
        "Name": r.get("Name") or "",
        "Email": r.get("Email") or "",
        "IsActive": r.get("IsActive"),
        "Title": r.get("Title") or "",
        "Country": r.get("Country") or "",
        "TimeZoneSidKey": r.get("TimeZoneSidKey") or "",
        "Profile": _nm(r.get("Profile")) or "",
        "Role": _nm(r.get("UserRole")) or "",
        "Manager": _nm(r.get("Manager")) or "",
        "Region": str(r.get("Regions__c") or "").replace(" - ", " "),
        "Content": bool(r.get("UserPermissionsSFContentUser")),
        "PermissionSets": names,
        "Queues": queues,
        "CreatedDate": created,
        "_loaded": True,
    }


def _ticket_html(rec: dict, *, requester: str, subject: str) -> str:
    """Plain sentences in HTML. No dashes, no tool names."""
    def p(text: str) -> str:
        return "<p>" + html.escape(text, quote=False) + "</p>"
    when = "Tue Nov 3 2026" if str(rec.get("CreatedDate") or "").startswith("2026-11-03") else "today"
    region = (rec.get("Region") or "").replace(" - ", " ").replace("--", " ")
    perms = ", ".join(rec.get("PermissionSets") or []) or "none listed"
    queues = ", ".join(rec.get("Queues") or []) or "none"
    subj = (subject or "").replace(" - ", ", ").replace("--", ", ").replace("\u2014", ",").replace("\u2013", ",")
    lines = [
        f"Requester {requester or 'the person who asked'}. {when}.",
        (
            f"operator user {rec.get('Name') or ''}, id {rec.get('Id') or ''}. "
            f"Profile {rec.get('Profile') or 'unknown'}. Role {rec.get('Role') or 'unknown'}. "
            f"Title {rec.get('Title') or 'unknown'}. {rec.get('Country') or ''}. "
            f"Time zone {rec.get('TimeZoneSidKey') or 'unknown'}. "
            f"Manager {rec.get('Manager') or 'unknown'}. Region {region or 'unknown'}."
        ),
        (
            f"Permission sets {perms}. Queue {queues}. "
            f"Content feature license is {'on' if rec.get('Content') else 'off'}."
        ),
    ]
    email = str(rec.get("Email") or "").strip()
    if email:
        lines.append(f"Email {email}.")
    if subj:
        lines.append(f"Requesting email subject: {subj}.")
    body = "".join(p(re.sub(r"\s+", " ", ln).strip()) for ln in lines)
    return "<div>" + body + "</div>"


def _same_sf_user(a: str, b: str) -> bool:
    """15-char and 18-char operator ids for the same user."""
    a, b = str(a or ""), str(b or "")
    if len(a) < 15 or len(b) < 15:
        return bool(a) and a == b
    return a[:15] == b[:15]


def _ledger_row(ledger: dict, sf_id: str) -> dict:
    direct = ledger.get(sf_id)
    if isinstance(direct, dict) and (direct.get("work_item_id") or direct.get("pending")):
        return direct
    for key, row in ledger.items():
        if isinstance(row, dict) and _same_sf_user(key, sf_id) and (row.get("work_item_id") or row.get("pending")):
            return row
    return {}


def _ledger_put(ledger: dict, sf_id: str, row: dict) -> None:
    ledger[sf_id] = row
    if len(sf_id) >= 15:
        ledger[sf_id[:15]] = dict(row)


def _ledger_drop_open(ledger: dict, sf_id: str) -> None:
    for key in list(ledger):
        row = ledger.get(key)
        if isinstance(row, dict) and _same_sf_user(key, sf_id) and not row.get("work_item_id"):
            ledger.pop(key, None)


# These three already exist for operator. The runner must never edit them.
_NEVER_TOUCH = {379941, 379947, 379948}
_AREA = "operator\\operator"
_USER_STORY_TYPE = "User Story"
_THREAD_LOCK = threading.Lock()
# Resolved read-only on 2026-11-03 from work items already assigned to each person.
_MENTION_BOOK = (
    ("operator", "Vizcaya", "operator Vizcaya", "redacted-uuid"),
    ("operator", "operator", "operator Inuganti", "redacted-uuid"),
    ("operator", "Killana", "operator Killana", "redacted-uuid"),
    ("operator", "Reddy", "operator Reddy", "redacted-uuid"),
)
_COMMENT_TAILS = (
    "is set up in operator and ready for testing.",
    "is in operator now and ready for testing.",
    "is set up and ready for testing.",
)


def _never_touch(wid: int) -> None:
    if int(wid) in _NEVER_TOUCH:
        raise RuntimeError("refusing to touch %s" % int(wid))


def _sf_key(sf_id: str) -> str:
    sf_id = str(sf_id or "")
    return sf_id[:15] if len(sf_id) >= 15 else sf_id


@contextmanager
def _ledger_lock():
    """One writer at a time, across threads and across processes."""
    path = root_dir() / "user-ado.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    with _THREAD_LOCK:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def _anchor(guid: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(guid or "")):
        return "@" + label
    return '<a href="#" data-vss-mention="version:2.0,%s">@%s</a>' % (guid, label)


def _default_mentions() -> dict:
    return {key: _anchor(guid, label) for key, _query, label, guid in _MENTION_BOOK}


def testing_comment(name: str, sf_id: str, mentions: dict | None = None) -> str:
    """One short note. Testers first, operator and operator at the end for awareness."""
    m = dict(_default_mentions())
    if isinstance(mentions, dict):
        for key, _query, label, guid in _MENTION_BOOK:
            got = mentions.get(key)
            if isinstance(got, str) and got.strip():
                m[key] = got.strip()
    tail = _COMMENT_TAILS[sum(ord(c) for c in str(sf_id or name)) % len(_COMMENT_TAILS)]
    safe_name = html.escape(str(name or ""), quote=False)
    text = f"{m['operator']} {m['operator']} {safe_name} {tail} fyi {m['operator']} {m['operator']}"
    visible = re.sub(r"<[^>]+>", "", text)
    if _FORBIDDEN_DASH.search(visible):
        raise RuntimeError("bad_comment")
    if len(visible) > 400:
        text = f"{m['operator']} {m['operator']} {name} is ready for testing. fyi {m['operator']} {m['operator']}"
    return text


def search_tokens(rec: dict) -> list[str]:
    """Name, email, and both operator id lengths. Nothing broader than that."""
    tokens: list[str] = []
    name = str((rec or {}).get("Name") or "").strip()
    if len(name) >= 3 and name.lower() != "operator user":
        tokens.append(name)
    email = str((rec or {}).get("Email") or (rec or {}).get("email") or "").strip()
    if email and "@" in email and " " not in email and len(email) <= 80:
        tokens.append(email)
    sf_id = str((rec or {}).get("Id") or "")
    if len(sf_id) >= 15:
        tokens.append(sf_id[:15])
    if len(sf_id) >= 18:
        tokens.append(sf_id[:18])
    out: list[str] = []
    for token in tokens:
        if token in out:
            continue
        if re.search(r"[\[\]\"\\]", token):
            continue
        out.append(token)
    return out


def _load_guard():
    tools = str(Path.home() / "claude-stack/tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import ado_create_guard as guard
    return guard


def build_existing_wiql(tokens: list[str], project: str = "operator",
                        requester: str = "", title: str = "") -> str:
    """Any state, last 14 days. The person, and also the requester plus title keywords."""
    parts = []
    for token in tokens:
        safe = str(token).replace("'", "''")
        parts.append("[System.Title] CONTAINS '%s'" % safe)
        parts.append("[System.Description] CONTAINS '%s'" % safe)
    person = "(" + " OR ".join(parts) + ")" if parts else ""
    ask = ""
    if requester or title:
        ask = _load_guard().ask_clause(requester, title)
    if person and ask:
        cond = "(" + person + " OR " + ask + ")"
    elif person:
        cond = person
    elif ask:
        cond = ask
    else:
        raise RuntimeError("no_search_key")
    return (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] = '%s' "
        "AND [System.CreatedDate] >= @Today - 14 "
        "AND %s "
        "ORDER BY [System.CreatedDate] ASC"
    ) % (str(project).replace("'", "''"), cond)


def _resolve_plan(current: str) -> str | None:
    """The only state write allowed is one move to Resolved. Never Active, Closed, or Removed."""
    cur = str(current or "")
    if cur in ("Closed", "Removed"):
        raise RuntimeError("refusing " + cur)
    if cur == "Resolved":
        return None
    return "Resolved"


def _user_story_ops(title: str, description_html: str, assignee: str, iteration: str,
                    wi_type: str = "User Story") -> list[dict]:
    if wi_type != _USER_STORY_TYPE:
        raise RuntimeError("refusing " + str(wi_type))
    it = str(iteration or "")
    if not it.startswith(_AREA + "\\"):
        raise RuntimeError("bad_iteration")
    return [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.Description", "value": description_html},
        {"op": "add", "path": "/fields/System.AreaPath", "value": _AREA},
        {"op": "add", "path": "/fields/System.IterationPath", "value": it},
        {"op": "add", "path": "/fields/System.AssignedTo", "value": assignee},
    ]


def _adobot_paths() -> list[Path]:
    raw = os.environ.get("ASK_DO_ADOBOT_LEDGERS", "")
    if raw.strip() == "-":
        return []
    if raw.strip():
        return [Path(p) for p in raw.split(":") if p]
    if os.environ.get("ASK_DO_DRY") == "1":
        return []
    home = Path.home()
    paths = [
        home / "claude-stack/data/ado_autolog.json",
        home / "llm-appliance/runtime/work-loop/story-requests/ledger.json",
    ]
    return [p for p in paths if p.is_file()]


def _adobot_wid(obj: dict) -> int:
    for key in ("work_item_id", "item_id", "wid", "ado_id"):
        val = obj.get(key)
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return 0


def _walk_adobot(obj, ids: list[str], name: str, email: str) -> int:
    if isinstance(obj, dict):
        wid = _adobot_wid(obj)
        if wid:
            blob = json.dumps(obj, default=str).lower()
            userish = (
                "operator user" in blob
                or "user setup" in blob
                or "new operator user" in blob
            )
            if any(i in blob for i in ids) or (userish and (
                (name and name.lower() in blob) or (email and email in blob)
            )):
                return wid
        for val in obj.values():
            hit = _walk_adobot(val, ids, name, email)
            if hit:
                return hit
    elif isinstance(obj, list):
        for val in obj:
            hit = _walk_adobot(val, ids, name, email)
            if hit:
                return hit
    return 0


def _adobot_existing(full: dict) -> int:
    """ADOBot already has this user. Leave the ticket on that path."""
    sf_id = str((full or {}).get("Id") or "")
    ids = []
    if len(sf_id) >= 15:
        ids.append(sf_id[:15].lower())
    if len(sf_id) >= 18:
        ids.append(sf_id[:18].lower())
    name = str((full or {}).get("Name") or "").strip()
    email = str((full or {}).get("Email") or "").strip().lower()
    for path in _adobot_paths():
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        hit = _walk_adobot(data, ids, name, email)
        if hit:
            return hit
    return 0


def _find_existing(own, full: dict, title: str) -> list[int]:
    if hasattr(own, "find_existing"):
        raw = own.find_existing(full) or []
    elif hasattr(own, "search"):
        raw = own.search(str(full.get("Id") or ""), title) or []
    else:
        raw = []
    out = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


class LiveAdo:
    """ADO writes for a user-setup User Story only. Uses ado_client on :18820."""

    def __init__(self) -> None:
        self._bridge = None
        self._ado = None

    def _open(self):
        if self._bridge is not None:
            return self._bridge
        import sys
        tools = str(Path.home() / "claude-stack/tools")
        stack = str(Path.home() / "claude-stack")
        for p in (tools, stack):
            if p not in sys.path:
                sys.path.insert(0, p)
        os.environ["ADO_CDP"] = "http://127.0.0.1:18820"
        import ado_client as ado
        self._ado = ado
        self._bridge = ado.AdoBridge().connect()
        return self._bridge

    def close(self) -> None:
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:
                pass
        self._bridge = None

    def _dry_blocked(self) -> bool:
        return os.environ.get("ASK_DO_DRY") == "1" and self._ado is None

    def search(self, sf_id: str, title: str) -> list[int]:
        return self.find_existing({
            "Id": sf_id,
            "Name": title.replace("operator user setup: ", "", 1),
            "_title": title,
            "_requester": str(getattr(self, "_ask_requester", "") or ""),
        })

    def find_existing(self, rec: dict) -> list[int]:
        tokens = search_tokens(rec or {})
        requester = str((rec or {}).get("_requester") or "")
        title = str((rec or {}).get("_title") or "")
        if not tokens and not (requester or title):
            return []
        b = self._open()
        ado = self._ado
        query = build_existing_wiql(
            tokens,
            project=getattr(ado, "PROJECT", "operator"),
            requester=requester,
            title=title,
        )
        data = b.api(
            "POST",
            f"{ado.PROJECT}/_apis/wit/wiql?api-version=7.0&$top=20",
            body={"query": query},
        ) or {}
        return [int(w["id"]) for w in (data.get("workItems") or [])]

    def current_iteration(self) -> str:
        ado = self._ado
        b = self._open()
        data = b.api(
            "GET",
            f"{ado.PROJECT}/operator/_apis/work/teamsettings/iterations"
            "?$timeframe=current&api-version=7.0",
        ) or {}
        chosen = ""
        for row in data.get("value") or []:
            attrs = row.get("attributes") or {}
            path = str(row.get("path") or "")
            if str(attrs.get("timeFrame") or "").lower() == "current" and path:
                chosen = path
                break
        if not chosen:
            today = time.strftime("%Y-%m-%d")
            for row in data.get("value") or []:
                attrs = row.get("attributes") or {}
                start = str(attrs.get("startDate") or "")[:10]
                finish = str(attrs.get("finishDate") or "")[:10]
                path = str(row.get("path") or "")
                if start and finish and start <= today <= finish:
                    chosen = path
                    break
        if not chosen.startswith(_AREA + "\\"):
            raise RuntimeError("bad_iteration")
        return chosen

    def _me(self) -> str:
        """operator's ADO identity, taken from a work item already assigned to him."""
        ado = self._ado
        b = self._open()
        data = b.api(
            "POST",
            f"{ado.PROJECT}/_apis/wit/wiql?api-version=7.0&$top=1",
            body={"query": (
                "SELECT [System.Id] FROM WorkItems WHERE "
                f"[System.TeamProject] = '{ado.PROJECT}' AND [System.AssignedTo] = @Me "
                "ORDER BY [System.ChangedDate] DESC"
            )},
        )
        ids = [int(w["id"]) for w in (data.get("workItems") or [])]
        if not ids:
            return ""
        fields = (ado.work_items(b, [ids[0]], ["System.AssignedTo"])[0] or {}).get("fields") or {}
        who = fields.get("System.AssignedTo") or {}
        if isinstance(who, dict):
            return str(who.get("uniqueName") or who.get("displayName") or "")
        return ""

    def create(self, title: str, description_html: str, assignee: str) -> dict:
        if self._dry_blocked():
            raise RuntimeError("dry_run_no_ado_write")
        ado = self._ado
        b = self._open()
        ado.assert_no_ai_trace(title)
        from tools import ado_sf_comment_gates as gates
        gates.assert_work_item_field_safe(description_html)
        requester = str(getattr(self, "_ask_requester", "") or "")
        guard = _load_guard()

        def _query(wiql):
            data = b.api(
                "POST",
                f"{ado.PROJECT}/_apis/wit/wiql?api-version=7.0&$top=20",
                body={"query": wiql},
            ) or {}
            found = []
            for row in (data.get("workItems") or []):
                try:
                    found.append(int(row.get("id")))
                except (TypeError, ValueError, AttributeError):
                    continue
            return found

        decision = guard.acquire(requester, title, _query, source="ask_do")
        if decision.get("action") == "skip" and decision.get("work_item_id"):
            return {"id": int(decision["work_item_id"]), "linked": True, "raw_state": ""}
        if decision.get("in_progress"):
            raise RuntimeError("create_in_progress")
        if decision.get("action") != "create":
            raise RuntimeError(decision.get("error") or "no_search_key")
        key = decision.get("key") or ""
        who = ""
        try:
            who = self._me()
        except Exception:
            who = ""
        who = who or assignee
        try:
            iteration = self.current_iteration()
            ops = _user_story_ops(title, description_html, who, iteration, _USER_STORY_TYPE)
        except Exception:
            guard.release(key)
            raise
        try:
            data = b.api(
                "POST",
                f"{ado.PROJECT}/_apis/wit/workitems/$User%20Story?api-version=7.0",
                body=ops,
                content_type="application/json-patch+json",
            )
        except Exception:
            guard.release(key)
            raise
        wid = int((data or {}).get("id") or 0)
        if wid:
            guard.commit(key, wid, requester=requester, title=title, source="ask_do")
        else:
            guard.release(key)
        return {
            "id": wid,
            "raw_state": ((data or {}).get("fields") or {}).get("System.State"),
        }

    def _state_of(self, wid: int) -> str:
        ado = self._ado
        rows = ado.work_items(self._bridge, [int(wid)], ["System.State"]) or []
        fields = (rows[0] or {}).get("fields") or {} if rows else {}
        return str(fields.get("System.State") or "")

    def resolve(self, wid: int) -> str:
        wid = int(wid)
        _never_touch(wid)
        if self._dry_blocked():
            raise RuntimeError("dry_run_no_ado_write")
        ado = self._ado
        b = self._open()
        current = self._state_of(wid)
        nxt = _resolve_plan(current)
        if nxt is None:
            return current
        ado.update_fields(b, wid, {"System.State": nxt})
        got = self._state_of(wid)
        if got in ("Closed", "Removed"):
            raise RuntimeError("refusing " + got)
        return got or current

    def comment(self, wid: int, text: str, title: str) -> dict:
        wid = int(wid)
        _never_touch(wid)
        if self._dry_blocked():
            raise RuntimeError("dry_run_no_ado_write")
        ado = self._ado
        b = self._open()
        visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip().lower()
        try:
            existing = ado.comments(b, wid, top=20) or []
        except Exception:
            existing = []
        for row in existing:
            have = re.sub(r"\s+", " ", ado.html_to_text(row.get("text") or "")).strip().lower()
            if visible and visible in have:
                return {"skipped": True, "duplicate": True}
        return ado.add_comment(b, wid, text, origin="", title=title)

    def mentions(self) -> dict:
        # Pinned board ids. Testers first, awareness names stay at the end.
        return _default_mentions()


def _comment_on(own, wid: int, name: str, sf_id: str, title: str) -> str:
    mentions = None
    if hasattr(own, "mentions"):
        try:
            got = own.mentions()
            if isinstance(got, dict):
                mentions = got
        except Exception:
            mentions = None
    note = testing_comment(name, sf_id, mentions)
    own.comment(wid, note, title)
    return note


def on_user_created(record: dict, *, ado=None, requester: str = "", subject: str = "") -> dict:
    """One User Story per operator user. Search first. Never a second item. Never Closed."""
    sf_id = str((record or {}).get("Id") or "")
    if not re.fullmatch(r"005[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?", sf_id):
        return {"ok": False, "error": "bad_id", "created": False}
    with _ledger_lock():
        return _on_user_created_locked(record, sf_id, ado=ado, requester=requester, subject=subject)


def _on_user_created_locked(record: dict, sf_id: str, *, ado=None, requester: str = "",
                            subject: str = "") -> dict:
    full = dict(record or {})
    if not full.get("_loaded") and os.environ.get("ASK_DO_DRY") != "1" and _DEPS.get("load_user") is None:
        try:
            loaded = _load_user_live(sf_id)
            if loaded:
                full.update(loaded)
        except Exception:
            pass
    elif _DEPS.get("load_user") is not None:
        loaded = _DEPS["load_user"](sf_id)
        if isinstance(loaded, dict):
            full.update(loaded)
    prof = str(full.get("Profile") or "")
    # 2026-11-03: dealer/partner portal users get a ticket too (operator). Guest/customer stay skipped.
    if any(x in prof.lower() for x in ("customer", "guest")):
        return {"ok": True, "created": False, "skipped": "external_user"}
    name = str(full.get("Name") or "").strip()
    if len(name) < 3:
        return {"ok": False, "error": "no_name", "created": False}
    title = f"operator user setup: {name}"
    if _FORBIDDEN_DASH.search(title) or "bot" in title.lower():
        return {"ok": False, "error": "bad_title", "created": False}
    if not search_tokens(full):
        return {"ok": False, "error": "no_search_key", "created": False}

    ledger = _ledger()
    prev = _ledger_row(ledger, sf_id)
    if prev.get("work_item_id"):
        return {
            "ok": True,
            "created": False,
            "already": True,
            "work_item_id": int(prev.get("work_item_id")),
            "state": prev.get("state") or "",
        }
    hit = _adobot_existing(full)
    if hit:
        _ledger_put(ledger, sf_id, {
            "work_item_id": int(hit),
            "state": "",
            "name": name,
            "title": title,
            "created": False,
            "deferred": True,
            "via": "adobot",
            "at": time.time(),
        })
        _save_ledger(ledger)
        return {
            "ok": True,
            "created": False,
            "deferred": True,
            "work_item_id": int(hit),
            "via": "adobot",
        }
    if prev.get("pending") and time.time() - float(prev.get("pending") or 0) < 600:
        return {"ok": False, "created": False, "error": "in_progress"}

    _ledger_put(ledger, sf_id, {"pending": time.time(), "name": name, "key": _sf_key(sf_id)})
    _save_ledger(ledger)
    own = ado
    close_live = False
    try:
        if own is None:
            if os.environ.get("ASK_DO_DRY") == "1":
                _ledger_drop_open(ledger, sf_id)
                _save_ledger(ledger)
                return {"ok": True, "created": False, "dry": True}
            own = LiveAdo()
            close_live = True
        try:
            own._ask_requester = requester
        except Exception:
            pass
        full["_requester"] = requester
        full["_title"] = title
        guard = _load_guard()
        known = guard.lookup(requester, title)
        if known:
            _ledger_put(ledger, sf_id, {
                "work_item_id": int(known),
                "state": "",
                "name": name,
                "title": title,
                "created": False,
                "found": True,
                "via": "ask_guard",
                "at": time.time(),
            })
            _save_ledger(ledger)
            return {
                "ok": True,
                "created": False,
                "found": True,
                "work_item_id": int(known),
                "via": "ask_guard",
            }
        existing = _find_existing(own, full, title)
        if existing:
            guard.remember(requester, title, min(existing), source="ask_do")
            wid = min(existing)
            _ledger_put(ledger, sf_id, {
                "work_item_id": wid,
                "state": "",
                "name": name,
                "title": title,
                "created": False,
                "found": True,
                "also_seen": existing,
                "at": time.time(),
            })
            _save_ledger(ledger)
            return {
                "ok": True,
                "created": False,
                "found": True,
                "work_item_id": wid,
                "also_seen": existing,
            }
        description = _ticket_html(full, requester=requester, subject=subject)
        made = own.create(title, description, "redacted-email")
        if made.get("linked"):
            wid = int(made.get("id") or 0)
            _ledger_put(ledger, sf_id, {
                "work_item_id": wid,
                "state": "",
                "name": name,
                "title": title,
                "created": False,
                "found": True,
                "via": "ask_guard",
                "at": time.time(),
            })
            _save_ledger(ledger)
            return {
                "ok": True,
                "created": False,
                "found": True,
                "work_item_id": wid,
                "via": "ask_guard",
            }
        wid = int(made.get("id") or 0)
        if not wid:
            raise RuntimeError("no_work_item_id")
        _never_touch(wid)
        _ledger_put(ledger, sf_id, {
            "work_item_id": wid,
            "state": "",
            "name": name,
            "title": title,
            "created": True,
            "resolve_attempted": True,
            "comment_attempted": False,
            "commented": False,
            "at": time.time(),
        })
        _save_ledger(ledger)
        state = own.resolve(wid)
        if state in ("Closed", "Removed"):
            raise RuntimeError("refusing " + state)
        comment_error = ""
        commented = False
        _ledger_put(ledger, sf_id, {
            "work_item_id": wid,
            "state": state,
            "name": name,
            "title": title,
            "created": True,
            "resolve_attempted": True,
            "comment_attempted": True,
            "commented": False,
            "at": time.time(),
        })
        _save_ledger(ledger)
        try:
            _comment_on(own, wid, name, sf_id, title)
            commented = True
        except Exception as exc:
            comment_error = type(exc).__name__ + ":" + str(exc)[:160]
        _ledger_put(ledger, sf_id, {
            "work_item_id": wid,
            "state": state,
            "name": name,
            "title": title,
            "created": True,
            "resolve_attempted": True,
            "comment_attempted": True,
            "commented": commented,
            "at": time.time(),
        })
        _save_ledger(ledger)
        return {
            "ok": True,
            "created": True,
            "work_item_id": wid,
            "state": state,
            "title": title,
            "comment_error": comment_error,
        }
    except Exception as exc:
        ledger = _ledger()
        row = _ledger_row(ledger, sf_id)
        if not row.get("work_item_id"):
            _ledger_drop_open(ledger, sf_id)
            _save_ledger(ledger)
        return {"ok": False, "created": False, "error": type(exc).__name__ + ":" + str(exc)[:180]}
    finally:
        if close_live and own is not None:
            own.close()

_AUDIT_USER = re.compile(
    r"Created new (?:[Pp]artner )?[Uu]ser\s+(.+?)\s+\(UserID:\s*\[([0-9A-Za-z]+)\]\)"
)


def watch_new_users() -> dict:
    """Read SetupAuditTrail for created users and open the one ADO ticket each."""
    if os.environ.get("ASK_DO_DRY") == "1":
        return {"ok": True, "dry": True, "results": []}
    try:
        rows = _call("sf_query", _default_sf_query)(
            "SELECT Action, Display, CreatedDate FROM SetupAuditTrail "
            "WHERE CreatedDate = LAST_N_DAYS:2 AND Action IN ('createduser', 'createdpartneruser') "
            "ORDER BY CreatedDate DESC"
        ) or []
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "results": []}
    results = []
    for row in rows[:8]:
        disp = str(row.get("Display") or "")
        m = _AUDIT_USER.search(disp)
        if not m:
            continue
        name, sid = m.group(1).strip(), m.group(2).strip()
        if data_line(name):
            continue
        if any(r.get("created") for r in results):
            time.sleep(random.randint(30, 90))
        results.append(on_user_created({"Id": sid, "Name": name}))
    return {"ok": True, "n": len(results), "results": [
        {"id": r.get("work_item_id"), "state": r.get("state"), "created": r.get("created"), "already": r.get("already")}
        for r in results
    ]}


def mark_work_done(work_id: str, draft: str, *, ado_id=None, ado_state: str = "") -> dict:
    """Mark an own-role item done. Does not execute anything."""
    path = Path.home() / "llm-appliance/runtime/work-loop/own-role" / f"{work_id}.json"
    if os.environ.get("ASK_DO_OWN_DIR"):
        path = Path(os.environ["ASK_DO_OWN_DIR"]) / f"{work_id}.json"
    if not path.is_file():
        return {"ok": False, "error": "missing"}
    rec = json.loads(path.read_text())
    rec["status"] = "done"
    rec["execute"] = False
    rec["needs_operator_ok"] = False
    rec["draft"] = draft
    rec["done_at"] = time.time()
    rec["sf_write"] = False
    if ado_id:
        rec["ado_work_item_id"] = ado_id
        rec["ado_state"] = ado_state
    if isinstance(rec.get("operatorbot"), dict):
        rec["operatorbot"]["executed"] = False
        rec["operatorbot"]["status"] = "already_in_operator"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    tmp.replace(path)
    return {"ok": True, "id": work_id, "status": "done"}


operator_DONE = (
    "hey operator,\n\n"
    "operator's operator account is set up now, he can log in with his operator SSO. "
    "let me know if he's missing anything."
)


# --- chat action path (2026-11-03 lact). handle() stays prepare-only for the draft runner.
# chat_act runs nondestructive operator work through the operatorBot env and leaves
# destructive work prepared until operator replies GO.

import shlex
import subprocess

_CHAT_STATE = "chat-state.json"
_ORG_PARTIAL = "vrm-partial"
_ORG_FULL = "fullsand"
_ORG_PROD = "operator"
_ALLOWED_ORGS = {_ORG_PARTIAL, _ORG_FULL, _ORG_PROD}

_GO_RE = re.compile(r"(?i)^\s*go\s*[.!]?\s*$")
_DESTRUCT_RE = re.compile(
    r"(?i)\b(?:deactivat\w*|delete|destroy|disable\s+(?:the\s+)?user|overwrite|"
    r"bulk\s+update|deploy|rollback|wipe|remove\s+(?:the\s+)?(?:permission|user|from))\b"
)
_USER_CREATE_RE = re.compile(
    r"(?i)\b(?:create|set ?up|setup|provision|add)\b.{0,80}\buser\b"
)
_PERM_RE = re.compile(
    r"(?i)\b(?:assign|add|grant|give)\b.{0,50}\bpermission\s*set\b"
)
_QUEUE_RE = re.compile(
    r"(?i)\b(?:add|put)\b.{0,60}\b(?:queue|public\s+group|group)\b"
)
_RECORD_RE = re.compile(
    r"(?i)\bcreate\s+(?:a\s+|an\s+)?(contact|account|case|lead|task)\b"
)
_ADO_RE = re.compile(
    r"(?i)\b(?:please|can you|could you)?\b.{0,20}\b(?:create|open|add)\b.{0,40}"
    r"\b(?:user story|work item|ado|ticket|bug)\b"
)
_MAIL_DRAFT_RE = re.compile(r"(?i)\b(?:draft|save)\b.{0,40}\b(?:email|e-mail|mail|outlook)\b")
_ZOOM_DRAFT_RE = re.compile(r"(?i)\b(?:draft|stage)\b.{0,40}\bzoom\b|\bzoom\b.{0,40}\b(?:draft|stage)\b")
_BRAIN_RE = re.compile(r"(?i)\b(?:search|look\s*up|lookup|find)\b.{0,40}\bwork brain\b|\bwork brain\b.{0,40}\b(?:search|look\s*up|find)\b")
_SEND_RE = re.compile(r"(?i)\b(?:send|post|chatter)\b")


def resolve_org(text: str) -> dict:
    """Pick the org from the words. Prod only when it is named."""
    blob = text or ""
    low = blob.lower()
    if re.search(r"(?i)\btavant", blob):
        return {"org": "", "explicit_prod": False, "refused": "tavantfull is off limits"}
    if re.search(r"(?i)\bpartial\b", blob):
        return {"org": _ORG_PARTIAL, "explicit_prod": False, "refused": ""}
    if re.search(r"(?i)\bfullsand\b|\bfull\s*sandbox\b|\bfull\s+sand\b", blob):
        return {"org": _ORG_FULL, "explicit_prod": False, "refused": ""}
    named_prod = bool(re.search(r"(?i)\b(?:prod|production|operator)\b", blob)) and "sandbox" not in low
    if named_prod:
        return {"org": _ORG_PROD, "explicit_prod": True, "refused": ""}
    return {"org": _ORG_PARTIAL, "explicit_prod": False, "refused": ""}


def _state_path() -> Path:
    return root_dir() / _CHAT_STATE


def _load_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {"last_user": {}, "pending": None}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"last_user": {}, "pending": None}
    if not isinstance(data, dict):
        return {"last_user": {}, "pending": None}
    data.setdefault("last_user", {})
    data.setdefault("pending", None)
    return data


def _save_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)


def _hist_blob(history) -> str:
    bits = []
    for item in history or []:
        if isinstance(item, str):
            bits.append(item)
        elif isinstance(item, dict):
            bits.append(str(item.get("content") or item.get("text") or ""))
    return "\n".join(bits)[-4000:]


def _soql_lit(value: str) -> str:
    return (value or "").replace("\\", "").replace("'", "")


def _redact_err(text: str) -> str:
    raw = str(text or "")
    raw = re.sub(r"(?i)(bearer|token|secret|password|session|authorization)\s*[:=]?\s*\S+", "[redacted]", raw)
    raw = re.sub(r"[A-Za-z0-9+/=]{40,}", "[redacted]", raw)
    return re.sub(r"\s+", " ", raw).strip()[:240]


def _sf_json(args: list[str], org: str, *, allow_prod: bool = False) -> dict:
    """operator CLI under the operatorBot env. The env is never printed."""
    if org not in _ALLOWED_ORGS or "tavant" in org:
        return {"status": 1, "message": "org refused"}
    if org == _ORG_PROD and not allow_prod:
        return {"status": 1, "message": "prod was not named"}
    if any(str(a) == _ORG_PROD for a in args) and not allow_prod:
        return {"status": 1, "message": "prod was not named"}
    if any("tavant" in str(a).lower() for a in args):
        return {"status": 1, "message": "org refused"}
    script = (
        'source "$HOME/claude-stack/bin/operatorbot-env.sh" >/dev/null 2>&1\n'
        "export SF_USE_GENERIC_UNIX_KEYCHAIN=true\n"
        "export SFDX_USE_GENERIC_UNIX_KEYCHAIN=true\n"
        "export SF_DISABLE_TELEMETRY=true\n"
        "exec sf " + " ".join(shlex.quote(str(a)) for a in args) + "\n"
    )
    try:
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"status": 1, "message": "operator timed out"}
    raw = proc.stdout or ""
    start = raw.find("{")
    if start < 0:
        return {"status": 1, "message": _redact_err(proc.stderr or "no json")}
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError:
        return {"status": 1, "message": "bad json from operator"}
    if not isinstance(data, dict):
        return {"status": 1, "message": "bad json from operator"}
    if data.get("message"):
        data["message"] = _redact_err(data.get("message"))
    return data


def _sf_query(org: str, soql: str, *, allow_prod: bool = False) -> list:
    data = _sf_json(["data", "query", "-o", org, "-q", soql, "--json"], org, allow_prod=allow_prod)
    if data.get("status") not in (0, "0", None) and not (data.get("result") or {}).get("records"):
        if data.get("status") not in (0, "0"):
            return []
    return list(((data.get("result") or {}).get("records")) or [])


def _sf_id(data: dict) -> str:
    result = data.get("result") or {}
    if isinstance(result, dict):
        return str(result.get("id") or result.get("Id") or "")
    return ""


def _facts(lines: list[str], *, must: list[str] | None = None, executed: bool = False, org: str = "") -> dict:
    body = "\n".join(lines).strip()
    header = (
        "WORK NOTES\n"
        "ACTION FACTS. A tool already ran. Report what it did, with every id below. "
        "Do not say you lack access, do not say you cannot create users, and do not mention an admin portal. "
        "Ask operator to reply GO only when the facts say to reply GO. "
        "Do not say a change is done when the facts say nothing was changed.\n"
    )
    return {
        "handled": True,
        "facts": header + body,
        "must": [m for m in (must or []) if m],
        "executed": executed,
        "org": org,
    }


def _remember_user(row: dict, org: str) -> None:
    state = _load_state()
    state["last_user"] = {
        "org": org,
        "id": row.get("Id") or "",
        "name": row.get("Name") or "",
        "username": row.get("Username") or "",
        "email": row.get("Email") or "",
        "phone": row.get("Phone") or "",
        "active": row.get("IsActive"),
    }
    _save_state(state)


def _profile_id(org: str, allow_prod: bool) -> tuple[str, str]:
    for name in ("Standard User", "Standard Platform User"):
        rows = _sf_query(
            org,
            "SELECT Id, Name FROM Profile WHERE Name = '%s' LIMIT 1" % _soql_lit(name),
            allow_prod=allow_prod,
        )
        if rows and rows[0].get("Id"):
            return str(rows[0]["Id"]), str(rows[0].get("Name") or name)
    return "", ""


def _user_rows(org: str, where: str, allow_prod: bool) -> list:
    soql = (
        "SELECT Id, Name, Username, Email, Phone, IsActive, Profile.Name "
        "FROM User WHERE %s LIMIT 5" % where
    )
    return _sf_query(org, soql, allow_prod=allow_prod)


def _parse_person(text: str) -> tuple[str, str]:
    match = re.search(
        r"(?i)\b(?:called|named)\s+([A-Za-z][A-Za-z'`-]{0,40}(?:\s+[A-Za-z][A-Za-z'`-]{0,40})?)",
        text or "",
    )
    if not match:
        return "", ""
    words = []
    for word in match.group(1).split():
        if word.lower() in {"and", "with", "email", "phone", "in", "give", "the"}:
            break
        words.append(word)
    if not words:
        return "", ""
    if len(words) == 1:
        return words[0], words[0]
    return words[0], words[-1]


def _parse_email(text: str) -> str:
    match = re.search(r"(?i)\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", text or "")
    return match.group(1) if match else ""


def _parse_phone(text: str) -> str:
    match = re.search(r"(?i)\bphone(?:\s+number)?(?:\s+of|\s+is|:)?\s*(\+?[0-9][0-9\-\s().]{6,18})", text or "")
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(1))


def _flat_user(row: dict) -> dict:
    profile = row.get("Profile")
    profile_name = profile.get("Name") if isinstance(profile, dict) else ""
    return {
        "Id": row.get("Id") or "",
        "Name": row.get("Name") or "",
        "Username": row.get("Username") or "",
        "Email": row.get("Email") or "",
        "Phone": row.get("Phone") or "",
        "IsActive": row.get("IsActive"),
        "Profile": profile_name or "",
    }


def _create_user(text: str, picked: dict) -> dict:
    org = picked["org"]
    allow = bool(picked.get("explicit_prod"))
    if org == _ORG_PROD and not allow:
        return _facts(["Prod was named in a way that is not explicit. Nothing was changed."], org=org)
    first, last = _parse_person(text)
    email = _parse_email(text)
    phone = _parse_phone(text)
    missing = []
    if not first or not last:
        missing.append("a first and last name")
    if not email:
        missing.append("an email")
    if missing:
        return _facts(
            ["Nothing was changed. The request is missing %s." % " and ".join(missing), "Org would be %s." % org],
            org=org,
        )
    existing = _user_rows(
        org,
        "Email = '%s' AND FirstName = '%s' AND LastName = '%s'" % (_soql_lit(email), _soql_lit(first), _soql_lit(last)),
        allow,
    )
    created = False
    username = ""
    if existing:
        user = _flat_user(existing[0])
        username = user["Username"]
    else:
        profile_id, profile_name = _profile_id(org, allow)
        if not profile_id:
            return _facts(["Nothing was changed. No standard profile was found in %s." % org], org=org)
        stamp = time.strftime("%m%d%H%M%S")
        username = "test.test.%s@%s" % (stamp, email.split("@", 1)[-1])
        alias = ("tt" + stamp)[:8]
        nick = ("test" + stamp)[:40]
        values = " ".join([
            "FirstName=%s" % first,
            "LastName=%s" % last,
            "Email=%s" % email,
            "Username=%s" % username,
            "Alias=%s" % alias,
            "CommunityNickname=%s" % nick,
            "TimeZoneSidKey=America/Chicago",
            "LocaleSidKey=en_US",
            "EmailEncodingKey=UTF-8",
            "LanguageLocaleKey=en_US",
            "ProfileId=%s" % profile_id,
            "IsActive=true",
            "Regions__c='1 - North America'",
        ])
        if phone:
            values += " Phone=%s" % phone
        made = _sf_json(
            ["data", "create", "record", "--sobject", "User", "--values", values, "--target-org", org, "--json"],
            org,
            allow_prod=allow,
        )
        user_id = _sf_id(made)
        if not user_id:
            return _facts(
                ["Nothing was changed in %s. operator did not create the user. %s" % (org, made.get("message") or "no id")],
                org=org,
            )
        created = True
        rows = _user_rows(org, "Id = '%s'" % _soql_lit(user_id), allow)
        user = _flat_user(rows[0]) if rows else {
            "Id": user_id, "Name": "%s %s" % (first, last), "Username": username,
            "Email": email, "Phone": phone, "IsActive": True, "Profile": profile_name,
        }
    _remember_user(user, org)
    ado = {}
    try:
        ado = on_user_created(
            {
                "Id": user.get("Id") or "",
                "Name": user.get("Name") or ("%s %s" % (first, last)),
                "Email": user.get("Email") or email,
                "Profile": user.get("Profile") or "Standard User",
                "Username": user.get("Username") or username,
                "_loaded": True,
            },
            requester="operator",
            subject="operator user setup",
        ) or {}
    except Exception as exc:
        ado = {"ok": False, "error": type(exc).__name__}
    ado_id = str(ado.get("work_item_id") or "")
    ado_line = "ADO work item: none."
    if ado_id and ado.get("created"):
        ado_line = "ADO work item %s was created." % ado_id
    elif ado_id:
        ado_line = "ADO work item %s was already there. A second one was not created." % ado_id
    elif ado.get("error"):
        ado_line = "ADO ticket was not created (%s)." % _redact_err(ado.get("error"))
    lines = [
        "Org: %s. Prod was not used." % org if org != _ORG_PROD else "Org: operator, named in the request.",
        "Created the operator user." if created else "The operator user was already there. A second one was not created.",
        "Id: %s" % (user.get("Id") or ""),
        "Name: %s" % (user.get("Name") or ""),
        "Username: %s" % (user.get("Username") or username),
        "Email: %s" % (user.get("Email") or email),
        "Phone: %s" % (user.get("Phone") or phone or ""),
        "Profile: %s" % (user.get("Profile") or "Standard User"),
        "Active: %s" % user.get("IsActive"),
        ado_line,
    ]
    must = [str(user.get("Id") or "")]
    if ado_id:
        must.append(ado_id)
    return _facts(lines, must=must, executed=created, org=org)


def _find_target_user(text: str, history, picked: dict) -> dict:
    org = picked["org"]
    allow = bool(picked.get("explicit_prod"))
    state = _load_state()
    last = state.get("last_user") or {}
    blob = (text or "") + "\n" + _hist_blob(history)
    if not re.search(r"(?i)\bpartial\b|\bfullsand\b|\bfull\s*sandbox\b|\bprod\b", text or ""):
        if last.get("org") in _ALLOWED_ORGS:
            org = last["org"]
            allow = org == _ORG_PROD and bool(picked.get("explicit_prod"))
            picked = dict(picked)
            picked["org"] = org
    id_match = _SF_ID.search(blob)
    email = _parse_email(text or "")
    rows = []
    if id_match:
        rows = _user_rows(org, "Id = '%s'" % _soql_lit(id_match.group(1)), allow)
    elif email:
        rows = _user_rows(org, "Email = '%s'" % _soql_lit(email), allow)
    elif re.search(r"(?i)\b(that|this|the)\s+user\b|\bthem\b", text or "") and last.get("id") and last.get("org") == org:
        rows = _user_rows(org, "Id = '%s'" % _soql_lit(last["id"]), allow)
    else:
        first, last_name = _parse_person(text or "")
        if first and last_name:
            rows = _user_rows(
                org,
                "FirstName = '%s' AND LastName = '%s'" % (_soql_lit(first), _soql_lit(last_name)),
                allow,
            )
    if not rows and last.get("id"):
        rows = _user_rows(last.get("org") or org, "Id = '%s'" % _soql_lit(last["id"]), allow=False)
        org = last.get("org") or org
    if not rows:
        return {"org": org, "user": {}, "allow_prod": allow}
    return {"org": org, "user": _flat_user(rows[0]), "allow_prod": allow and org == _ORG_PROD}


def _prepare_destructive(text: str, history, picked: dict) -> dict:
    if picked.get("refused"):
        return _facts(["Nothing was changed. %s" % picked["refused"]], org="")
    found = _find_target_user(text, history, picked)
    org = found["org"]
    user = found["user"]
    if org == _ORG_PROD and not picked.get("explicit_prod"):
        return _facts(["Prod was not named. Nothing was changed."], org=org)
    kind = "change"
    low = text.lower()
    if "deactiv" in low or "disable" in low:
        kind = "deactivate"
    elif "delete" in low or "destroy" in low:
        kind = "delete"
    elif "deploy" in low or "rollback" in low:
        kind = "deploy"
    elif "bulk" in low:
        kind = "bulk update"
    elif "overwrite" in low or "remove" in low:
        kind = "overwrite"
    if kind == "deploy":
        state = _load_state()
        state["pending"] = {"op": "deploy", "org": org, "at": time.time(), "explicit_prod": bool(picked.get("explicit_prod"))}
        _save_state(state)
        return _facts([
            "Org: %s." % org,
            "A deploy was prepared only. Nothing was deployed.",
            "Ask operator to reply GO.",
        ], must=["GO"], executed=False, org=org)
    if not user.get("Id"):
        return _facts([
            "Org: %s." % org,
            "The %s was not done. No matching user was found, so nothing was changed." % kind,
            "Ask operator to reply GO only after the user is named.",
        ], must=["GO"], executed=False, org=org)
    state = _load_state()
    state["pending"] = {
        "op": kind,
        "org": org,
        "user_id": user["Id"],
        "username": user.get("Username") or "",
        "name": user.get("Name") or "",
        "explicit_prod": bool(picked.get("explicit_prod")) and org == _ORG_PROD,
        "at": time.time(),
    }
    _save_state(state)
    _remember_user(user, org)
    return _facts([
        "Org: %s." % org,
        "Prepared a %s for operator user id %s, username %s, name %s." % (
            kind, user["Id"], user.get("Username") or "", user.get("Name") or ""),
        "The user is still active: %s. Nothing was changed." % user.get("IsActive"),
        "Ask operator to reply GO to %s that user." % kind,
    ], must=[user["Id"], "GO"], executed=False, org=org)


def _run_go() -> dict:
    state = _load_state()
    pending = state.get("pending") or {}
    if not pending:
        return _facts(["Nothing is waiting. There is no prepared change to run."], executed=False)
    org = str(pending.get("org") or "")
    allow = bool(pending.get("explicit_prod")) and org == _ORG_PROD
    if org == _ORG_PROD and not allow:
        state["pending"] = None
        _save_state(state)
        return _facts(["The prepared change pointed at prod without an explicit ask. It was dropped. Nothing was changed."])
    op = str(pending.get("op") or "")
    if op == "deploy":
        return _facts([
            "Org: %s." % org,
            "GO was received. A deploy is still not run from chat. Nothing was deployed.",
        ], must=["GO"], executed=False, org=org)
    user_id = str(pending.get("user_id") or "")
    if not user_id or op not in {"deactivate", "delete", "overwrite", "bulk update"}:
        return _facts(["The prepared change is not one chat can run. Nothing was changed."], org=org)
    if op == "deactivate":
        made = _sf_json(
            ["data", "update", "record", "--sobject", "User", "--record-id", user_id,
             "--values", "IsActive=false", "--target-org", org, "--json"],
            org,
            allow_prod=allow,
        )
        if not _sf_id(made) and made.get("status") not in (0, "0"):
            return _facts([
                "GO was received. The deactivation did not go through. %s" % (made.get("message") or "no result"),
                "User id %s was not changed." % user_id,
            ], must=[user_id], executed=False, org=org)
        state["pending"] = None
        _save_state(state)
        return _facts([
            "Org: %s." % org,
            "Deactivated operator user id %s, username %s." % (user_id, pending.get("username") or ""),
        ], must=[user_id], executed=True, org=org)
    return _facts([
        "GO was received for a %s on user id %s. That kind of change is still not run from chat. Nothing was changed." % (op, user_id),
    ], must=[user_id, "GO"], executed=False, org=org)


def _assign_perm(text: str, picked: dict) -> dict:
    org = picked["org"]
    allow = bool(picked.get("explicit_prod"))
    name_match = re.search(r"(?i)permission\s*set\s+([A-Za-z0-9_ ]{2,80})", text or "")
    if not name_match:
        return _facts(["Nothing was changed. The permission set name was not in the request.", "Org: %s." % org], org=org)
    ps_name = name_match.group(1).strip(" .")
    found = _find_target_user(text, [], picked)
    user = found["user"]
    if not user.get("Id"):
        return _facts(["Nothing was changed. No user was found for the permission set.", "Org: %s." % org], org=org)
    rows = _sf_query(
        org,
        "SELECT Id, Name FROM PermissionSet WHERE Name = '%s' AND IsOwnedByProfile = false LIMIT 1" % _soql_lit(ps_name),
        allow_prod=allow,
    )
    if not rows:
        rows = _sf_query(
            org,
            "SELECT Id, Name FROM PermissionSet WHERE Label = '%s' AND IsOwnedByProfile = false LIMIT 1" % _soql_lit(ps_name),
            allow_prod=allow,
        )
    if not rows:
        return _facts(["Nothing was changed. Permission set %s was not found in %s." % (ps_name, org)], org=org)
    made = _sf_json(
        ["data", "create", "record", "--sobject", "PermissionSetAssignment",
         "--values", "AssigneeId=%s PermissionSetId=%s" % (user["Id"], rows[0]["Id"]),
         "--target-org", org, "--json"],
        org,
        allow_prod=allow,
    )
    new_id = _sf_id(made)
    if not new_id:
        return _facts([
            "Nothing was changed. Permission set %s was not assigned. %s" % (ps_name, made.get("message") or "no id"),
        ], org=org)
    return _facts([
        "Org: %s." % org,
        "Assigned permission set %s to user id %s." % (ps_name, user["Id"]),
        "Assignment id: %s" % new_id,
    ], must=[user["Id"], new_id], executed=True, org=org)


def _add_queue(text: str, picked: dict) -> dict:
    org = picked["org"]
    allow = bool(picked.get("explicit_prod"))
    kind = "Queue" if re.search(r"(?i)\bqueue\b", text or "") else "Regular"
    name_match = re.search(r"(?i)\b(?:queue|group)\s+([A-Za-z0-9_ ]{2,80})", text or "")
    if not name_match:
        return _facts(["Nothing was changed. The queue or group name was not in the request.", "Org: %s." % org], org=org)
    gname = name_match.group(1).strip(" .")
    found = _find_target_user(text, [], picked)
    user = found["user"]
    if not user.get("Id"):
        return _facts(["Nothing was changed. No user was found to add.", "Org: %s." % org], org=org)
    rows = _sf_query(
        org,
        "SELECT Id, Name, Type FROM Group WHERE Name = '%s' AND Type = '%s' LIMIT 1" % (_soql_lit(gname), kind),
        allow_prod=allow,
    )
    if not rows:
        return _facts(["Nothing was changed. %s %s was not found in %s." % (kind, gname, org)], org=org)
    made = _sf_json(
        ["data", "create", "record", "--sobject", "GroupMember",
         "--values", "GroupId=%s UserOrGroupId=%s" % (rows[0]["Id"], user["Id"]),
         "--target-org", org, "--json"],
        org,
        allow_prod=allow,
    )
    new_id = _sf_id(made)
    if not new_id:
        return _facts([
            "Nothing was changed. The user was not added. %s" % (made.get("message") or "no id"),
        ], org=org)
    return _facts([
        "Org: %s." % org,
        "Added user id %s to %s %s." % (user["Id"], kind.lower(), gname),
        "Membership id: %s" % new_id,
    ], must=[user["Id"], new_id], executed=True, org=org)


def _create_record(text: str, picked: dict) -> dict:
    org = picked["org"]
    allow = bool(picked.get("explicit_prod"))
    match = _RECORD_RE.search(text or "")
    if not match:
        return None
    obj = match.group(1).capitalize()
    if obj == "Case":
        fields = "Subject=Request from chat Origin=Phone"
    elif obj == "Task":
        fields = "Subject=Request from chat"
    else:
        name = _parse_person(text)
        label = " ".join(p for p in name if p) or "New record"
        if obj == "Contact":
            first, last = _parse_person(text)
            if not last:
                return _facts(["Nothing was changed. A contact needs a name.", "Org: %s." % org], org=org)
            fields = "FirstName=%s LastName=%s" % (first, last)
            email = _parse_email(text)
            if email:
                fields += " Email=%s" % email
        else:
            fields = "Name=%s" % label.replace(" ", "")
    made = _sf_json(
        ["data", "create", "record", "--sobject", obj, "--values", fields, "--target-org", org, "--json"],
        org,
        allow_prod=allow,
    )
    new_id = _sf_id(made)
    if not new_id:
        return _facts(["Nothing was changed. The %s was not created. %s" % (obj, made.get("message") or "no id")], org=org)
    return _facts(["Org: %s." % org, "Created %s id %s." % (obj, new_id)], must=[new_id], executed=True, org=org)


def _ado_from_chat(text: str) -> dict:
    title = re.sub(r"\s+", " ", text or "").strip()[:120]
    title = title.replace(" - ", ", ").replace("--", ", ").replace("\u2014", ",").replace("\u2013", ",")
    ing = ingest("chat", "operator", title, text or "")
    route = {"runbooks": [], "ask": "ado_item"}
    try:
        result = _act_ado(ing, route, {"history": []})
    except Exception as exc:
        return _facts(["The work item was not created (%s)." % type(exc).__name__])
    wid = str(result.get("work_item_id") or "")
    fact = (result.get("facts") or ["The work item was not created."])[0]
    return _facts([fact], must=[wid] if wid else [], executed=bool(result.get("executed")))


def _outlook_draft(text: str) -> dict:
    if _SEND_RE.search(text or ""):
        return _facts(["Nothing was sent. Chat does not send mail. A draft was not saved because the request said send."])
    email = _parse_email(text)
    sub = re.search(r"(?i)\bsubject\s*[:=]\s*(.+)$", text or "")
    body = re.search(r"(?i)\bbody\s*[:=]\s*(.+)$", text or "")
    if not email or not sub or not body:
        return _facts(["Nothing was sent. To save an Outlook draft, the request needs a recipient, subject:, and body:."])
    import tempfile
    path = ""
    try:
        tools = str(Path.home() / "claude-stack/tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from outlook_zap_draft import owa, DEFAULT_ACCOUNT
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        path = handle.name
        handle.write(body.group(1).strip() + "\n")
        handle.close()
        res = owa(
            "draft-new",
            "--to", email,
            "--subject", sub.group(1).strip()[:180],
            "--text-file", path,
            "--from", DEFAULT_ACCOUNT,
        )
    except Exception as exc:
        return _facts(["Nothing was sent. The Outlook draft was not saved (%s)." % type(exc).__name__])
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass
    if not isinstance(res, dict) or not res.get("ok"):
        return _facts(["Nothing was sent. The Outlook draft was not saved."])
    draft_id = str(res.get("draft_id") or "")
    return _facts([
        "Saved an Outlook draft. It was not sent.",
        "Draft id: %s" % (draft_id or "saved"),
        "To: %s" % email,
    ], must=[draft_id] if draft_id else [], executed=True)


def _zoom_draft(text: str) -> dict:
    if _SEND_RE.search(text or ""):
        return _facts(["Nothing was sent. Chat does not send Zoom messages."])
    sid_match = re.search(r"(?i)\bsid\s*[:=]\s*([A-Za-z0-9_.:-]{4,80})", text or "")
    body_match = re.search(r"(?i)\bbody\s*[:=]\s*(.+)$", text or "")
    if not sid_match or not body_match:
        return _facts(["Nothing was sent. A Zoom draft needs sid: and body:. Nothing was put in a chat."])
    try:
        tools = str(Path.home() / "claude-stack/tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from zoom_composer_stage import stage
        res = stage(sid_match.group(1), body_match.group(1).strip(), name="chat")
    except Exception as exc:
        return _facts(["Nothing was sent. The Zoom draft was not staged (%s)." % type(exc).__name__])
    if not isinstance(res, dict) or not res.get("ok"):
        return _facts(["Nothing was sent. The Zoom draft was not staged."])
    return _facts([
        "Staged a Zoom draft. It was not sent.",
        "Draft id: %s" % (res.get("draft_id") or "staged"),
    ], executed=True)


def _lookup(text: str, picked: dict) -> dict | None:
    try:
        import sf_record_lookup as sf
    except Exception:
        return None
    if not sf.has_ids(text or ""):
        return None
    org = picked["org"] if picked["org"] in {_ORG_PARTIAL, _ORG_FULL} or picked.get("explicit_prod") else _ORG_PARTIAL
    old = sf.ORG
    try:
        sf.ORG = org if org != _ORG_PROD or picked.get("explicit_prod") else old
        if not picked.get("explicit_prod") and org == _ORG_PARTIAL:
            sf.ORG = _ORG_PARTIAL
        rows = list(sf.facts(text or "") or [])
    except Exception as exc:
        return _facts(["The record lookup failed (%s). Nothing was changed." % type(exc).__name__], org=org)
    finally:
        sf.ORG = old
    if not rows:
        return _facts(["Record lookup in %s found nothing. Nothing was changed." % org], org=org)
    return _facts(["Record lookup in %s:" % org] + [str(r)[:300] for r in rows[:6]], org=org)


def _brain(text: str) -> dict:
    got = _default_brain(text or "")
    hits = got.get("hits") or []
    if got.get("blocked"):
        return _facts(["Work brain did not search that. Nothing was changed."])
    if not hits:
        return _facts(["Work brain search returned no hits."])
    lines = ["Work brain search:"]
    for hit in hits[:5]:
        lines.append("%s: %s" % (hit.get("source") or "note", hit.get("title") or ""))
    return _facts(lines)


def chat_act(text: str, history=None) -> dict | None:
    """Run one chat action. None means the turn is ordinary talk."""
    blob = (text or "").strip()
    if len(blob) < 2:
        return None
    if data_line(blob):
        return _facts(["This request is on the data line. It was not done. Do not quote the sensitive part."])
    if _GO_RE.match(blob):
        return _run_go()
    # Org comes from this message only. A follow-up with no org uses the last user.
    picked = resolve_org(blob)
    if not re.search(r"(?i)\bpartial\b|\bfullsand\b|\bfull\s*sandbox\b|\b(?:prod|production|operator)\b", blob):
        picked["explicit_prod"] = False
        if picked["org"] == _ORG_PROD:
            picked["org"] = _ORG_PARTIAL
    if _DESTRUCT_RE.search(blob):
        return _prepare_destructive(blob, history, picked)
    if _USER_CREATE_RE.search(blob) and not re.search(r"(?i)\buser story\b", blob):
        return _create_user(blob, picked)
    if _PERM_RE.search(blob):
        return _assign_perm(blob, picked)
    if _QUEUE_RE.search(blob):
        return _add_queue(blob, picked)
    if _RECORD_RE.search(blob):
        made = _create_record(blob, picked)
        if made:
            return made
    if _ADO_RE.search(blob) and not _USER_CREATE_RE.search(blob):
        return _ado_from_chat(blob)
    if _MAIL_DRAFT_RE.search(blob):
        return _outlook_draft(blob)
    if _ZOOM_DRAFT_RE.search(blob):
        return _zoom_draft(blob)
    looked = _lookup(blob, picked)
    if looked:
        return looked
    if _BRAIN_RE.search(blob):
        return _brain(blob)
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "counts"
    if cmd == "index":
        idx = save_index(build_index())
        print(json.dumps({"safe": idx["safe"], "needs_ok": idx["needs_ok"], "n": len(idx["entries"])}))
    elif cmd == "counts":
        idx = load_index()
        print(json.dumps({"safe": idx.get("safe"), "needs_ok": idx.get("needs_ok"), "n": len(idx.get("entries") or [])}))
    elif cmd == "watch":
        print(json.dumps(watch_new_users()))
    else:
        print("usage: ask_do.py [index|counts|watch]")
        raise SystemExit(2)
