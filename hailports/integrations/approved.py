"""Approved connectors: Google, Microsoft Graph, IMAP, CalDAV, CardDAV, files, and Apple apps.

Tokens are passed in by the caller. This module does not embed a client id or a key.
"""

from __future__ import annotations

import imaplib
import re
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import urlencode

from hailports.integrations.http import call_http, call_json
from hailports.integrations.items import Item
from hailports.integrations.oauth import bearer
from hailports.integrations.store import IntegrationError

SERVICES = {
    "icloud": {
        "imap": "imap.mail.me.com",
        "caldav": "https://caldav.icloud.com/",
        "carddav": "https://contacts.icloud.com/",
    },
    "fastmail": {
        "imap": "imap.fastmail.com",
        "caldav": "https://caldav.fastmail.com/",
        "carddav": "https://carddav.fastmail.com/",
    },
}

_TEXT = {".md", ".txt", ".text", ".rst", ".markdown"}
GMAIL = "https://gmail.googleapis.com/gmail/v1" + "/users" + "/me"
GCAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GRAPH = "https://graph.microsoft.com/v1.0"


def service_host(service: str, kind: str, override: str = "") -> str:
    if override:
        return override
    block = SERVICES.get(str(service or "").lower()) or {}
    return str(block.get(kind) or "")


def _epoch(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _b64url(data: str) -> str:
    import base64
    raw = str(data or "")
    pad = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _within(when: float, start: float | None) -> bool:
    if start is None or when <= 0:
        return True
    return when >= start


def pull_files(root: str, *, cursor: str = "") -> tuple[list[Item], str]:
    path = Path(root)
    if not path.is_dir():
        raise IntegrationError("the notes folder was not found")
    floor = float(cursor) if cursor else 0
    newest = floor
    items = []
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.suffix.lower() not in _TEXT:
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        newest = max(newest, mtime)
        rel = item.relative_to(path).as_posix()
        if ".." in rel.split("/"):
            continue
        body = item.read_text(encoding="utf-8", errors="replace")
        items.append(Item("notes", "files", rel, item.stem, body, mtime, "note"))
    return items, str(newest or "")


def pull_gmail(http, token: str, *, since: float, cursor: str = "", limit: int = 50) -> tuple[list[Item], str]:
    if not token:
        raise IntegrationError("gmail has no token")
    head = bearer(token)
    new_cursor = cursor
    ids: list[str] = []
    history_ok = False
    if cursor:
        try:
            doc = call_json(
                http, "GET",
                f"{GMAIL}/history?startHistoryId={cursor}&historyTypes=messageAdded&maxResults={int(limit)}",
                head,
            )
            history_ok = True
            for row in doc.get("history") or []:
                for added in row.get("messagesAdded") or []:
                    mid = str((added.get("message") or {}).get("id") or "")
                    if mid:
                        ids.append(mid)
            new_cursor = str(doc.get("historyId") or cursor)
        except IntegrationError:
            history_ok = False
    if not history_ok:
        days = max(1, int((time.time() - since) / 86400) or 1)
        listed = call_json(http, "GET", f"{GMAIL}/messages?q=newer_than:{days}d&maxResults={int(limit)}", head)
        ids = [str(row.get("id") or "") for row in listed.get("messages") or [] if row.get("id")]
        profile = call_json(http, "GET", f"{GMAIL}/profile", head)
        new_cursor = str(profile.get("historyId") or new_cursor)
    items = []
    for mid in ids:
        if not mid:
            continue
        msg = call_json(http, "GET", f"{GMAIL}/messages/{mid}?format=full", head)
        when = 0
        try:
            when = float(msg.get("internalDate") or 0) / 1000.0
        except (TypeError, ValueError):
            when = 0
        if not _within(when, since):
            continue
        title = ""
        for header in ((msg.get("payload") or {}).get("headers") or []):
            if str(header.get("name") or "").lower() == "subject":
                title = str(header.get("value") or "")
                break
        body = _gmail_body(msg.get("payload") or {}) or str(msg.get("snippet") or "")
        items.append(Item("email", "gmail", mid, title or "mail", body, when, "email"))
    return items, new_cursor


def _gmail_body(payload: dict) -> str:
    data = str((payload.get("body") or {}).get("data") or "")
    if data:
        return _b64url(data)
    parts = []
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            text = _gmail_body(part)
            if text:
                parts.append(text)
    return "\n".join(parts)


def pull_gcal(http, token: str, *, since: float, limit: int = 50) -> tuple[list[Item], str]:
    if not token:
        raise IntegrationError("google calendar has no token")
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode({
        "timeMin": start,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(int(limit)),
    })
    doc = call_json(http, "GET", GCAL + "?" + query, bearer(token))
    items = []
    for row in doc.get("items") or []:
        begin = ((row.get("start") or {}).get("dateTime") or (row.get("start") or {}).get("date") or "")
        when = _epoch(str(begin))
        title = str(row.get("summary") or "event")
        body = str(row.get("description") or "")
        external = str(row.get("id") or title)
        items.append(Item("calendar", "gcal", external, title, body or title, when, "calendar"))
    return items, str(doc.get("nextSyncToken") or "")


def pull_graph_mail(http, token: str, *, since: float, limit: int = 50) -> tuple[list[Item], str]:
    if not token:
        raise IntegrationError("graph mail has no token")
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode({
        "$filter": f"receivedDateTime ge {start}",
        "$top": str(int(limit)),
        "$select": "id,subject,bodyPreview,receivedDateTime",
    })
    doc = call_json(http, "GET", GRAPH + "/me/messages?" + query, bearer(token))
    items = []
    for row in doc.get("value") or []:
        when = _epoch(str(row.get("receivedDateTime") or ""))
        external = str(row.get("id") or "")
        if not external:
            continue
        title = str(row.get("subject") or "mail")
        body = str(row.get("bodyPreview") or "")
        items.append(Item("email", "graph", external, title, body or title, when, "email"))
    return items, str(doc.get("@odata.deltaLink") or start)


def pull_graph_calendar(http, token: str, *, since: float, limit: int = 50) -> tuple[list[Item], str]:
    if not token:
        raise IntegrationError("graph calendar has no token")
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.fromtimestamp(since + (366 * 86400), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode({"startDateTime": start, "endDateTime": end, "$top": str(int(limit))})
    doc = call_json(http, "GET", GRAPH + "/me/calendarView?" + query, bearer(token))
    items = []
    for row in doc.get("value") or []:
        begin = ((row.get("start") or {}).get("dateTime") or "")
        when = _epoch(str(begin))
        external = str(row.get("id") or "")
        title = str(row.get("subject") or "event")
        body = str((row.get("body") or {}).get("content") or "")
        if external:
            items.append(Item("calendar", "graph", external, title, body or title, when, "calendar"))
    return items, start


def pull_graph_notes(http, token: str, *, limit: int = 50) -> tuple[list[Item], str]:
    if not token:
        raise IntegrationError("graph notes have no token")
    doc = call_json(http, "GET", GRAPH + f"/me/onenote/pages?$top={int(limit)}", bearer(token))
    items = []
    for row in doc.get("value") or []:
        external = str(row.get("id") or "")
        title = str(row.get("title") or "note")
        body = str(row.get("contentPreview") or "")
        content_url = str(row.get("contentUrl") or "")
        if content_url:
            host = content_url.split("/")[2] if content_url.startswith("https://") else ""
            if host == "graph.microsoft.com":
                raw = call_http(http, "GET", content_url, bearer(token))
                if raw:
                    body = _html_text(str(raw)) or body
        when = _epoch(str(row.get("lastModifiedDateTime") or ""))
        if external:
            items.append(Item("notes", "graph", external, title, body or title, when, "note"))
    return items, ""


def _html_text(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split())


def _decode_mime(value: str) -> str:
    parts = []
    for text, encoding in decode_header(value or ""):
        if isinstance(text, bytes):
            parts.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts)


def pull_imap(host: str, account: str, password: str, *, since: float, factory=None, limit: int = 50) -> tuple[list[Item], str]:
    if not host or not account or not password:
        raise IntegrationError("imap needs a host, an account, and an app password")
    if any(ch in host for ch in " /@"):
        raise IntegrationError("imap host is not a hostname")
    client = factory(host) if factory else imaplib.IMAP4_SSL(host, timeout=30)
    try:
        try:
            typ, _ = client.login(account, password)
        except Exception:
            raise IntegrationError("imap login failed") from None
        if str(typ).upper() != "OK":
            raise IntegrationError("imap login failed")
        client.select("INBOX", readonly=True)
        since_txt = time.strftime("%d-%b-%Y", time.gmtime(since))
        typ, data = client.search(None, "SINCE", since_txt)
        if str(typ).upper() != "OK":
            raise IntegrationError("imap search failed")
        nums = (data[0] or b"").split()[-int(limit):]
        items = []
        for num in nums:
            typ, fetched = client.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)] BODY.PEEK[TEXT]<0.2000>)")
            if str(typ).upper() != "OK":
                continue
            blob = _imap_blob(fetched)
            title = _decode_mime(_header(blob, "subject") or "mail")
            when = _epoch(_header(blob, "date"))
            body = blob.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in blob else blob
            external = num.decode("ascii", errors="replace") if isinstance(num, bytes) else str(num)
            items.append(Item("email", "imap", external, title, _decode_mime(body)[:4000], when, "email"))
        return items, since_txt
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _imap_blob(fetched) -> str:
    chunks = []
    for part in fetched or []:
        if isinstance(part, tuple):
            for piece in part:
                if isinstance(piece, bytes):
                    chunks.append(piece.decode("utf-8", errors="replace"))
        elif isinstance(part, bytes):
            chunks.append(part.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _header(blob: str, name: str) -> str:
    match = re.search(rf"(?im)^{name}:\s*(.+)$", blob)
    return match.group(1).strip() if match else ""


def pull_caldav(http, url: str, account: str, password: str, *, since: float) -> tuple[list[Item], str]:
    if not url or not account or not password:
        raise IntegrationError("caldav needs the collection url, an account, and an app password")
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = datetime.fromtimestamp(since + (366 * 86400), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        "<c:filter><c:comp-filter name=\"VCALENDAR\"><c:comp-filter name=\"VEVENT\">"
        f'<c:time-range start="{start}" end="{end}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )
    raw = _dav(http, url, account, password, report, "application/xml")
    items = []
    for event in _vevents(_unfold(raw)):
        title = event.get("SUMMARY") or "event"
        external = event.get("UID") or title
        body = event.get("DESCRIPTION") or title
        when = _epoch(event.get("DTSTART") or "")
        items.append(Item("calendar", "caldav", external, title, body, when, "calendar"))
    return items, start


def pull_carddav(http, url: str, account: str, password: str) -> tuple[list[Item], str]:
    if not url or not account or not password:
        raise IntegrationError("carddav needs the collection url, an account, and an app password")
    report = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><card:address-data/></d:prop>"
        "</card:addressbook-query>"
    )
    raw = _dav(http, url, account, password, report, "application/xml")
    items = []
    for card in _vcards(_unfold(raw)):
        title = card.get("FN") or "contact"
        external = card.get("UID") or title
        body = card.get("NOTE") or title
        items.append(Item("notes", "carddav", external, title, body, 0, "contact"))
    return items, ""


def _dav(http, url: str, account: str, password: str, report: str, content_type: str) -> str:
    import base64
    token = base64.b64encode(f"{account}:{password}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": "Basic " + token,
        "Content-Type": content_type,
        "Depth": "1",
    }
    return str(call_http(http, "REPORT", url, headers, report.encode("utf-8")))


def _unfold(text: str) -> str:
    return str(text or "").replace("\r\n ", "").replace("\n ", "").replace("\r\n\t", "")


def _blocks(text: str, begin: str, end: str) -> list[str]:
    found = []
    for match in re.finditer(rf"(?is)BEGIN:{begin}(.*?)END:{end}", text or ""):
        found.append(match.group(1))
    return found


def _fields(block: str) -> dict[str, str]:
    out = {}
    for line in (block or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        name = key.split(";")[0].strip().upper()
        if name and name not in out:
            out[name] = value.strip()
    return out


def _vevents(text: str) -> list[dict[str, str]]:
    return [_fields(block) for block in _blocks(text, "VEVENT", "VEVENT")]


def _vcards(text: str) -> list[dict[str, str]]:
    return [_fields(block) for block in _blocks(text, "VCARD", "VCARD")]


NOTES_SCRIPT = """
tell application "Notes"
  set chunks to {}
  set i to 0
  repeat with n in notes
    set i to i + 1
    if i > 200 then exit repeat
    set end of chunks to (id of n as text) & linefeed & (name of n as text) & linefeed & (body of n as text)
  end repeat
  set AppleScript's text item delimiters to "---ITEM---"
  return chunks as text
end tell
"""

CAL_SCRIPT = """
tell application "Calendar"
  set chunks to {}
  set i to 0
  set startDate to (current date) - (30 * days)
  repeat with c in calendars
    repeat with e in (every event of c whose start date > startDate)
      set i to i + 1
      if i > 200 then exit repeat
      set end of chunks to (uid of e as text) & linefeed & (summary of e as text) & linefeed & ((start date of e) as text) & linefeed & (description of e as text)
    end repeat
    if i > 200 then exit repeat
  end repeat
  set AppleScript's text item delimiters to "---ITEM---"
  return chunks as text
end tell
"""

MAIL_SCRIPT = """
tell application "Mail"
  set chunks to {}
  set i to 0
  repeat with m in messages of inbox
    set i to i + 1
    if i > 50 then exit repeat
    set end of chunks to (id of m as text) & linefeed & (subject of m as text) & linefeed & ((date received of m) as text) & linefeed & (content of m as text)
  end repeat
  set AppleScript's text item delimiters to "---ITEM---"
  return chunks as text
end tell
"""

CHAT_SCRIPT = """
tell application "Messages"
  set chunks to {}
  set i to 0
  repeat with c in chats
    set i to i + 1
    if i > 40 then exit repeat
    set msgText to ""
    try
      set msgText to text of last message of c
    end try
    set end of chunks to (id of c as text) & linefeed & (name of c as text) & linefeed & msgText
  end repeat
  set AppleScript's text item delimiters to "---ITEM---"
  return chunks as text
end tell
"""

APPLE = {
    "apple-notes": ("notes", "note", NOTES_SCRIPT),
    "apple-calendar": ("calendar", "calendar", CAL_SCRIPT),
    "apple-mail": ("email", "email", MAIL_SCRIPT),
    "apple-messages": ("chats", "chat", CHAT_SCRIPT),
}


def run_osascript(script: str, runner=None) -> str:
    if runner is not None:
        return str(runner(script) or "")
    if sys.platform != "darwin":
        raise IntegrationError("Apple apps are available on macOS")
    import subprocess
    try:
        out = subprocess.check_output(["osascript", "-e", script], text=True, timeout=40, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise IntegrationError("the Apple app did not answer. Allow Automation for this terminal.") from None
    return out


def pull_apple(provider: str, *, runner=None) -> tuple[list[Item], str]:
    spec = APPLE.get(provider)
    if not spec:
        raise IntegrationError("this app is recorded. Use an approved API to read it.")
    surface, kind, script = spec
    raw = run_osascript(script, runner=runner)
    items = []
    for block in raw.split("---ITEM---"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        external, title = lines[0], lines[1]
        body = "\n".join(lines[2:]) or title
        items.append(Item(surface, provider, external, _strip_html(title), _strip_html(body), 0, kind))
    return items, ""


def _strip_html(value: str) -> str:
    return _html_text(value) if "<" in value and ">" in value else value


