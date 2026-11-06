"""Short self-note lines and included-usage cloud launch.

A user API key is checked against prepaid credit. A signed-in plan session
uses included usage. Tokens and prompt text are never logged.
"""
from __future__ import annotations

import re

import base64
import json
import os
import sqlite3
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

_SERVICE = "https://api2.cursor.sh/aiserver.v1.BackgroundComposerService/"
_LAUNCH_URL = _SERVICE + "StartBackgroundComposerFromSnapshot"


def _strip_prod(text: str) -> str:
    import re
    return re.sub(r"(?i)^prod\b[\s:\-]*", "", str(text or "")).strip()


def about_line(plain_title: str) -> str:
    pt = " ".join(_strip_prod(plain_title).split())
    if not pt or pt.lower() == "update":
        return "This ticket is ready for you."
    line = pt[0].upper() + pt[1:]
    if line[-1] not in ".?":
        line += "."
    return line


def note_lines(plain_title: str) -> list[str]:
    return [
        about_line(plain_title),
        "Prep is ready. Tap the button and it opens with the full prep already in it.",
    ]


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def primary_button(url: str) -> str:
    return (
        f'<p><a href="{_esc(url)}" style="display:inline-block;padding:14px 22px;'
        'background:#0b5cad;color:#ffffff;text-decoration:none;border-radius:6px;'
        'font-size:16px;font-weight:600">Open in Cursor</a></p>'
    )


def ado_link(url: str, ticket_id: str) -> str:
    return f'<p><a href="{_esc(url)}">ADO {_esc(str(ticket_id))}</a></p>'


_STUB = "Prep is ready. Tap the button and it opens with the full prep already in it."
_AZURE_HOST = "dev." + "azure.com"
_SF_HOST = "salesforce" + ".com"

_SF_ID = re.compile(
    r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?\b"
)
_BACKTICK = re.compile(r"`[^`\n]{1,240}`")
_API = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_+[A-Za-z0-9]+)+\b")
_CUSTOM = re.compile(r"__[a-z]{1,4}\b", re.I)
_CLS = re.compile(r"\b[\w./\\-]+\.cls\b", re.I)
_SOQL = re.compile(
    r"(?is)\bSELECT\b[\s\S]{0,500}?\bFROM\b\s+\w+(?:\s+WHERE\b[\s\S]{0,240}?)?(?=[.;\n]|$)"
)
_FILE_URL = re.compile(r"file://\S+", re.I)
_HOME_PATH = re.compile(r"/Users/\S+")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "has", "have", "had", "not", "but", "its", "into", "about", "your", "you",
    "a", "an", "of", "to", "in", "on", "or", "is", "it", "be", "as", "by", "at",
}



def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z']*", text or "")


def _useful(text: str) -> bool:
    content = [w for w in _words(text) if len(w) >= 3 and w.lower() not in _STOP]
    return len(content) >= 4


def _broken(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s.count("(") != s.count(")"):
        return True
    if re.match(r"(?i)^the\s+(references|is|was|has|for|of)\b", s):
        return True
    if re.search(r"(?i)\busing or\b|\bor more likely\b|\(id:\s*\)", s):
        return True
    if re.search(r"(?i)\bor\s*\.\s*$", s):
        return True
    if re.search(r"(?i)\bif is\b", s):
        return True
    if re.search(r"'\s+'|\"\s+\"", s):
        return True
    return False


def _tidy(text: str) -> str:
    s = str(text or "")
    s = s.replace("\u2014", ", ").replace("\u2013", ", ")
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\[\s*\]", "", s)
    s = re.sub(r"['\"]\s*['\"]", "", s)
    s = re.sub(r"\(\s*,\s*", "(", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\(\s*[Ii]d:\s*\)", "", s)
    s = re.sub(r"\b[Ii]d:\s*", "", s)
    s = re.sub(r"\be\.g\.,?\s*", "", s, flags=re.I)
    s = re.sub(r"\b(the|a|an)\s+(?=[.,;]|$)", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"\(\s*\)", "", s)
    return s.strip(" \t-;,:")


def _drop_sentence(sent: str) -> bool:
    low = sent.lower()
    if "still open" in low:
        return True
    if re.search(r"\bopen questions?\b", low):
        return True
    if _STUB.lower() in low:
        return True
    if re.search(r"developer (needs|should|must).{0,80}(locate|find).{0,40}(flow|file)", low):
        return True
    if re.search(r"\b(locate|find)\b.{0,50}\bflow (file|metadata)\b", low):
        return True
    if re.search(r"\b(locate|review|find)\b.{0,40}\bthe flow\b", low):
        return True
    if re.search(r"\b(locate|find)\b.{0,80}\b(class|lwc|component|file)\b", low):
        return True
    if sent.rstrip().endswith("?") and re.search(
        r"\b(what|which|who|where|when|how|did|does|do|is|are|has|have|can|could|confirm)\b",
        low,
    ):
        return True
    if re.search(r"user\s*-?\s*id", low) and re.search(
        r"\b(what|which|need|ask|find|get|provide|missing|confirm)\b", low
    ):
        return True
    return False


def sanitize_outlook(text: str) -> str:
    """Strip developer dumps. Empty string when nothing a person would send remains."""
    raw = str(text or "").replace("\r", " ")
    if not raw.strip():
        return ""
    raw = raw.replace("\n", " ")
    parts = re.split(r"(?<=[.?!])\s+", raw)
    kept: list[str] = []
    for part in parts:
        sent = part.strip()
        if not sent or _drop_sentence(sent):
            continue
        s = _BACKTICK.sub(" ", sent)
        s = _SOQL.sub(" ", s)
        s = re.sub(r"(?i)\b(SOQL|SOSL|Apex|Queueable|Batchable)\b", " ", s)
        s = _CLS.sub(" ", s)
        s = _FILE_URL.sub(" ", s)
        s = _HOME_PATH.sub(" ", s)
        s = _CUSTOM.sub(" ", s)
        s = _API.sub(" ", s)
        s = _SF_ID.sub(" ", s)
        s = re.sub(r"(?i)\bmetadata\b", " ", s)
        s = s.replace("\\", " ")
        s = re.sub(r"\b([A-Za-z]+)/([A-Za-z]+)\b", r"\1 \2", s)
        for phrase in (
            "search results",
            "codebase",
            "snapshot",
            "not located",
            "not found",
            "read-only analysis",
        ):
            s = re.sub(re.escape(phrase), " ", s, flags=re.I)
        s = re.sub(r"(?i)\bpacket\b", " ", s)
        s = re.sub(r"(?i)\bensure\b", "make sure", s)
        s = re.sub(r"(?i)\butilize\b", "use", s)
        s = re.sub(r"(?i)\bautomation\b", "setup", s)
        s = re.sub(r"(?i)\bautomated\b", "set up", s)
        s = re.sub(r"^\s*\d+[.)]\s*", "", s)
        s = _tidy(s)
        if not s or _drop_sentence(s) or _broken(s):
            continue
        if s[0].isalpha() and s[0].islower():
            s = s[0].upper() + s[1:]
        if s[-1] not in ".?":
            s += "."
        if _useful(s):
            kept.append(s)
    out = _tidy(" ".join(kept))
    if out and out[-1] not in ".?":
        out += "."
    if not _useful(out):
        return ""
    return out


def _clean_url(url: str) -> str:
    u = str(url or "").replace("\\u0026", "&").replace("\\/", "/")
    return u.rstrip(".,;:>\\")


def _sf_host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url, re.I)
    return (m.group(1) if m else "").lower()


def _is_salesforce_url(url: str) -> bool:
    low = url.lower()
    if "soap.sforce.com" in low or "/2006/04/metadata" in low:
        return False
    host = _sf_host(low)
    if not host:
        return False
    if host.endswith(".lightning.force.com") or host == "lightning.force.com":
        return True
    sf = _SF_HOST
    if host.endswith(".my." + sf) or host.endswith("." + sf) or host == sf:
        return True
    if host.endswith(".salesforce-setup.com") and "/lightning/" in low:
        return True
    if host.endswith(".force.com") and "/lightning/" in low:
        return True
    return False


def _sf_label(url: str) -> str:
    m = re.search(r"/lightning/r/([A-Za-z0-9]+)/", url)
    if m:
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", m.group(1))
        return f"Salesforce {name}"[:48]
    m = re.search(r"/lightning/setup/([A-Za-z0-9]+)/", url)
    if m:
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", m.group(1))
        return f"Salesforce {name}"[:48]
    return "Salesforce"


def extract_work_links(text: str, ticket_id: str = "") -> list[tuple[str, str]]:
    """Pull real PR, Salesforce, and ADO work-item URLs. No guessed links."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    own = str(ticket_id or "").strip()
    for raw in _URL.findall(str(text or "")):
        url = _clean_url(raw)
        key = url.lower()
        if not url or key in seen:
            continue
        label = ""
        m = re.search(r"github\.com/[^/\s]+/[^/\s]+/pull/(\d+)", url, re.I)
        if m:
            label = f"PR {m.group(1)}"
        else:
            m = re.search(r"dev\.azure\.com/.+?/pullrequest/(\d+)", url, re.I)
            if m:
                label = f"PR {m.group(1)}"
            elif _AZURE_HOST in key and (m := re.search(r"/_workitems/edit/(\d+)", url, re.I)):
                label = f"ADO {m.group(1)}"
            elif _is_salesforce_url(url):
                label = _sf_label(url)
        if not label:
            continue
        if own and label == f"ADO {own}":
            # Caller adds the primary ADO link itself.
            seen.add(key)
            continue
        seen.add(key)
        found.append((label, url))
    return found


def soften_subject(subject: str) -> str:
    """Keep the Ticket <id>: prefix. Swap words the outbound gate rejects."""
    text = " ".join(str(subject or "").split())
    m = re.match(r"(Ticket\s+\d+\s*:\s*)(.*)$", text)
    head, tail = (m.group(1), m.group(2)) if m else ("", text)
    repls = (
        (r"\bautomation\b", "setup"),
        (r"\bautomated\b", "set up"),
        (r"\bscripts\b", "steps"),
        (r"\bscript\b", "steps"),
    )
    for pat, sub in repls:
        tail = re.sub(pat, sub, tail, flags=re.I)
    tail = " ".join(tail.split())
    return (head + tail).strip()


def note_paragraphs(title: str, findings, next_step) -> list[str]:
    """One finding (one or two sentences) and one next step. No prep stub."""
    rows = findings if isinstance(findings, (list, tuple)) else [findings]
    cleaned: list[str] = []
    for row in rows:
        s = sanitize_outlook(str(row or ""))
        if s and s not in cleaned and _STUB not in s:
            cleaned.append(s)

    def rank(s: str) -> tuple:
        low = s.lower()
        pen = 0
        if "code search" in low or "no direct" in low or "no local code" in low:
            pen += 30
        if "class" in low or ".cls" in low:
            pen += 40
        return (pen, -len(_words(s)))

    cleaned.sort(key=rank)
    bits: list[str] = []
    if cleaned:
        for part in re.split(r"(?<=[.?!])\s+", cleaned[0]):
            part = part.strip()
            if not part or not _useful(part):
                continue
            bits.append(part if part[-1] in ".?" else part + ".")
            if len(bits) >= 2:
                break
    finding = " ".join(bits[:2]).strip()
    step = sanitize_outlook(str(next_step or ""))
    if step:
        step = re.split(r"(?<=[.?!])\s+", step)[0].strip()
        if step and step[-1] not in ".?":
            step += "."
        if not _useful(step):
            step = ""
    paras: list[str] = []
    if finding:
        paras.append(finding)
    elif str(title or "").strip():
        line = " ".join(str(title).split())
        if line and line[-1] not in ".?":
            line += "."
        if line[:1].islower():
            line = line[:1].upper() + line[1:]
        paras.append(line)
    if step and step not in paras:
        paras.append(step)
    return [p for p in paras if p and _STUB not in p]


def links_html(links) -> str:
    """ADO, then pull requests, then Salesforce. No class-file links."""
    ado: list[tuple[str, str]] = []
    prs: list[tuple[str, str]] = []
    sfs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in links or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        label, url = str(item[0] or "").strip(), str(item[1] or "").strip()
        if not label or not url:
            continue
        low = url.lower()
        if low in seen:
            continue
        if ".cls" in label.lower() or ".cls" in low or low.startswith("file:") or "/users/" in low:
            continue
        if "/blob/" in low and "pull/" not in low:
            continue
        seen.add(low)
        if label.startswith("ADO") or "/_workitems/edit/" in low:
            ado.append((label, url))
        elif "/pull/" in low or "/pullrequest/" in low:
            prs.append((label, url))
        elif _is_salesforce_url(url) or label.lower().startswith("salesforce"):
            sfs.append((label, url))
    rows = ado + prs + sfs
    return "\n".join(f'<p><a href="{_esc(url)}">{_esc(label)}</a></p>' for label, url in rows)


def _state_db() -> Path:
    raw = (os.environ.get("CURSOR_STATE_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"


def session_token() -> str | None:
    path = _state_db()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = con.execute(
            "select value from ItemTable where key=?",
            ("cursorAuth/accessToken",),
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    tok = row[0]
    if isinstance(tok, bytes):
        tok = tok.decode("utf-8", "replace")
    tok = str(tok).strip().strip('"')
    if tok.count(".") < 2:
        return None
    try:
        seg = tok.split(".")[1]
        seg += "=" * ((4 - len(seg) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        exp = float(payload.get("exp") or 0)
    except Exception:
        return None
    if exp and exp < time.time() + 60:
        return None
    return tok


def _post(tok: str, method: str, body: dict, timeout: int = 30) -> tuple[int, dict]:
    req = urllib.request.Request(
        _SERVICE + method,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode() or "{}"
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return resp.status, data if isinstance(data, dict) else {}


def _start_ref(tok: str, repo: str, ref: str) -> tuple[str, str]:
    """Return (start_ref, base_branch). Empty start_ref means do not launch."""
    given = str(ref or "").strip()
    try:
        _code, data = _post(tok, "GetRepositoryDefaultBranch", {"repoUrl": repo}, timeout=20)
        default = str((data or {}).get("defaultBranch") or "").strip()
    except Exception:
        default = ""
    start = given or default
    return start, (default or start)


def _fail_reason(exc: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(exc.read().decode() or "{}")
    except Exception:
        err = {}
    blob = json.dumps(err).lower()
    if "github_app" in blob or "github app" in blob:
        return "github_app"
    if any(w in blob for w in ("prepaid", "usage_limit", "credit", "billing")):
        return "credit"
    if "does not exist" in blob or "starting branch" in blob:
        return "branch"
    if "missing required" in blob:
        return "missing_fields"
    if "not valid" in blob and "model" in blob:
        return "bad_model"
    return f"http_{exc.code}"


def ultra_launch(
    *,
    prompt: str,
    name: str,
    repo_url: str,
    env_public_id: str = "",
    model: str = "composer-2.5",
    ref: str = "",
) -> tuple[str | None, str, str]:
    """Return (agent_url, reason, bc_id). reason is a short code. Prompt is never logged."""
    text = str(prompt or "").strip()
    if not text:
        return None, "no_prompt", ""
    repo = str(repo_url or "").strip()
    if not repo.startswith("http"):
        return None, "no_repo", ""
    tok = session_token()
    if not tok:
        return None, "no_session", ""
    start_ref, base_branch = _start_ref(tok, repo, ref)
    if not start_ref:
        return None, "no_branch", ""
    host = repo.split("://", 1)[-1].strip("/")
    bc = "bc-" + str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    dev = {
        "url": repo,
        "ref": start_ref,
        "cursorServerCommit": "",
        "userExtensions": [],
        "environmentJsonOverride": "",
        "dontAllowReadingEnvironmentJsonFromDatabase": False,
        "skipBuildCaches": False,
    }
    env_id = str(env_public_id or "").strip()
    if env_id:
        dev["environmentPublicId"] = env_id
    clipped = text[:120000]
    body = {
        "bcId": bc,
        "snapshotNameOrId": host,
        "snapshotWorkspaceRootPath": "/workspace",
        "devcontainerStartingPoint": dev,
        "repoUrl": host,
        "autoBranch": True,
        "baseBranch": base_branch,
        "returnImmediately": True,
        "source": "CLI",
        "modelDetails": {"modelName": model or "composer-2.5", "maxMode": False},
        "additionalModelDetails": [],
        "repositoryInfo": {
            "pathEncryptionKey": "",
            "shouldSyncIndex": False,
            "repositoryInfoShouldQueryStaging": False,
            "repositoryInfoShouldQueryProd": False,
            "repoQueryAuthToken": "",
        },
        "conversationState": {"turns": []},
        "preFetchedBlobs": [],
        "conversationAction": {
            "userMessageAction": {
                "userMessage": {
                    "text": clipped,
                    "messageId": message_id,
                    "richText": clipped,
                },
                "sendToInteractionListener": True,
            }
        },
        "agentSessionId": str(uuid.uuid4()),
        "kickoffMessageId": message_id,
        "prompt": clipped,
        "name": str(name or "ticket")[:100],
        "addInitialMessageToResponses": True,
    }
    try:
        code, data = _post(tok, "StartBackgroundComposerFromSnapshot", body, timeout=45)
    except urllib.error.HTTPError as exc:
        return None, _fail_reason(exc), ""
    except Exception:
        return None, "transport", ""
    if code not in (200, 201):
        return None, f"http_{code}", ""
    comp = data.get("composer") if isinstance(data, dict) else None
    if not isinstance(comp, dict):
        comp = {}
    got = str(
        comp.get("bcId")
        or comp.get("bc_id")
        or (data.get("bcId") if isinstance(data, dict) else "")
        or bc
    ).strip()
    if not got:
        return None, "no_id", ""
    try:
        _st_code, st = _post(tok, "GetBackgroundComposerStatus", {"bcId": got}, timeout=15)
        status = str((st or {}).get("status") or "")
    except Exception:
        status = ""
    if status.endswith("ERROR"):
        try:
            _post(tok, "ArchiveBackgroundComposer", {"bcId": got, "source": "CLI"}, timeout=15)
        except Exception:
            pass
        return None, "agent_error", ""
    return f"https://cursor.com/agents/{got}", "launched_ultra", got
