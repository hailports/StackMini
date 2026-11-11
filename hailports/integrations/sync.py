"""Pull a window, index it, and prove the brain can find one new item per surface."""

from __future__ import annotations

import time
from pathlib import Path

from hailports.integrations.adapter import load_adapter, pull_bridge
from hailports.integrations.approved import (
    pull_apple,
    pull_caldav,
    pull_carddav,
    pull_files,
    pull_gcal,
    pull_gmail,
    pull_graph_calendar,
    pull_graph_mail,
    pull_graph_notes,
    pull_imap,
    service_host,
)
from hailports.integrations.index import get_cursor, index_items, latest_text, open_brain, prove, purge_surface, set_cursor
from hailports.integrations.items import SURFACES, needle_for, window_start
from hailports.integrations.oauth import GOOGLE_TOKEN, GRAPH_TOKEN, refresh_token
from hailports.integrations.store import IntegrationError, TokenBox, load_mask, read_status, safe_text, write_status

TOKEN_NAME = {
    "gmail": "google",
    "gcal": "google",
    "graph": "graph",
    "imap": "imap",
    "caldav": "caldav",
    "carddav": "carddav",
}


def integrations_dir(state: Path) -> Path:
    return Path(state) / "integrations"


def _terms(cfg: dict, box: TokenBox) -> list[str]:
    block = cfg.get("integrations") or {}
    terms = [str(item) for item in (block.get("mask") or []) if str(item or "").strip()]
    terms.extend(load_mask(box.root))
    for name in ("google", "graph", "imap", "caldav", "carddav"):
        payload = box.get(name) or {}
        for key in ("access_token", "refresh_token", "password", "client_secret", "app_password"):
            value = str(payload.get(key) or "")
            if len(value) >= 6:
                terms.append(value)
    return terms


def _absolute_expiry(token: dict, now: float) -> dict:
    stamped = dict(token)
    try:
        expiry = float(stamped.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if 0 < expiry < 10_000_000:
        stamped["expiry"] = now + expiry
    return stamped


def _access(box: TokenBox, name: str, http, token_url: str, now: float) -> str:
    payload = box.get(name) or {}
    token = str(payload.get("access_token") or "")
    try:
        expiry = float(payload.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    refresh = str(payload.get("refresh_token") or "")
    if token and (expiry <= 0 or expiry > now + 30):
        return token
    if not refresh:
        if token:
            return token
        raise IntegrationError(name + " has no token")
    fresh = refresh_token(
        http, token_url, str(payload.get("client_id") or ""), refresh, str(payload.get("client_secret") or ""),
    )
    fresh = _absolute_expiry(fresh, now)
    if payload.get("client_id"):
        fresh["client_id"] = payload.get("client_id")
    box.put(name, fresh)
    return str(fresh.get("access_token") or "")


def _basic(box: TokenBox, name: str) -> tuple[str, str, str, str]:
    payload = box.get(name) or {}
    return (
        str(payload.get("host") or payload.get("url") or ""),
        str(payload.get("account") or ""),
        str(payload.get("password") or payload.get("app_password") or ""),
        str(payload.get("service") or ""),
    )


def pull_surface(surface: str, item: dict, cfg: dict, box: TokenBox, state: Path, *, http=None, imap_factory=None, osascript=None, fetch=None, now: float) -> tuple[list, str, str]:
    provider = str(item.get("provider") or "")
    via = str(item.get("via") or "")
    days = int((cfg.get("integrations") or {}).get("window_days") or 30)
    start = window_start(now, days, surface)
    since = start if start is not None else now - (30 * 86400)
    con = open_brain(Path(state) / "brain.sqlite")
    try:
        cursor = get_cursor(con, surface, provider or via or "local")
    finally:
        con.close()
    if provider == "files":
        items, new_cursor = pull_files(str(item.get("path") or ""), cursor=cursor)
        return items, new_cursor, provider
    if provider == "gmail":
        token = _access(box, "google", http, GOOGLE_TOKEN, now)
        items, new_cursor = pull_gmail(http, token, since=since, cursor=cursor)
        return items, new_cursor, provider
    if provider == "gcal":
        token = _access(box, "google", http, GOOGLE_TOKEN, now)
        items, new_cursor = pull_gcal(http, token, since=since)
        return items, new_cursor or cursor, provider
    if provider == "graph" and surface == "email":
        token = _access(box, "graph", http, GRAPH_TOKEN, now)
        items, new_cursor = pull_graph_mail(http, token, since=since)
        return items, new_cursor, provider
    if provider == "graph" and surface == "calendar":
        token = _access(box, "graph", http, GRAPH_TOKEN, now)
        items, new_cursor = pull_graph_calendar(http, token, since=since)
        return items, new_cursor, provider
    if provider == "graph" and surface == "notes":
        token = _access(box, "graph", http, GRAPH_TOKEN, now)
        items, new_cursor = pull_graph_notes(http, token)
        return items, new_cursor, provider
    if provider == "imap":
        host, account, password, service = _basic(box, "imap")
        host = service_host(service, "imap", host)
        items, new_cursor = pull_imap(host, account, password, since=since, factory=imap_factory)
        return items, new_cursor, provider
    if provider == "caldav":
        url, account, password, service = _basic(box, "caldav")
        url = url or service_host(service, "caldav")
        items, new_cursor = pull_caldav(http, url, account, password, since=since)
        return items, new_cursor, provider
    if provider == "carddav":
        url, account, password, service = _basic(box, "carddav")
        url = url or service_host(service, "carddav")
        items, new_cursor = pull_carddav(http, url, account, password)
        return items, new_cursor, provider
    if provider in ("apple-notes", "apple-calendar", "apple-mail", "apple-messages"):
        items, new_cursor = pull_apple(provider, runner=osascript)
        return items, new_cursor, provider
    if via == "bridges" or provider == "bridge":
        adapter = load_adapter(Path(state) / "adapters", surface) or {}
        items = pull_bridge(
            str(adapter.get("base_url") or item.get("base_url") or ""),
            str(adapter.get("export_path") or "/export"),
            surface,
            "bridge",
            fetch,
        )
        return items, "", "bridge"
    if via == "native":
        raise IntegrationError("this app is recorded. Use an approved API or the Apple apps on macOS to read it.")
    raise IntegrationError("provider is not set")


def sync_configured(cfg: dict, state: Path, *, http=None, imap_factory=None, osascript=None, fetch=None, now: float | None = None, only: str | None = None) -> list[dict]:
    stamp = time.time() if now is None else float(now)
    state = Path(state)
    box = TokenBox(integrations_dir(state))
    terms = _terms(cfg, box)
    block = cfg.get("integrations") or {}
    status = read_status(integrations_dir(state))
    proofs = []
    for surface in SURFACES:
        if only and surface != only:
            continue
        item = block.get(surface) or {}
        if not item.get("enabled"):
            continue
        provider = str(item.get("provider") or item.get("via") or "")
        try:
            items, cursor, provider = pull_surface(
                surface, item, cfg, box, state,
                http=http, imap_factory=imap_factory, osascript=osascript, fetch=fetch, now=stamp,
            )
            con = open_brain(state / "brain.sqlite")
            try:
                stats = index_items(con, items, terms, now=stamp)
                if cursor:
                    set_cursor(con, surface, provider, cursor, now=stamp)
                word = (stats.get("needles") or [""])[0]
                if not word:
                    title, body = latest_text(con, surface)
                    word = needle_for(title, body)
                proof = prove(con, surface, word)
            finally:
                con.close()
            proof["provider"] = provider
            proof["added"] = stats["added"]
            proof["updated"] = stats["updated"]
            proof["skipped"] = stats["skipped"]
            proof["dropped"] = stats["dropped"]
        except Exception as exc:
            proof = {
                "surface": surface,
                "ok": False,
                "query": "",
                "provider": provider,
                "reason": safe_text(exc),
                "added": 0,
            }
        status[surface] = {
            "via": str(item.get("via") or ""),
            "provider": provider,
            "proof": "pass" if proof.get("ok") else "fail",
            "query": str(proof.get("query") or ""),
            "synced_at": stamp,
        }
        proofs.append(proof)
    write_status(integrations_dir(state), status)
    return proofs


def disconnect_surface(cfg: dict, state: Path, surface: str, *, purge: bool = False) -> dict:
    if surface not in SURFACES:
        raise IntegrationError("unknown surface")
    state = Path(state)
    block = cfg.setdefault("integrations", {})
    item = block.get(surface) or {}
    provider = str(item.get("provider") or "")
    box = TokenBox(integrations_dir(state))
    token_name = TOKEN_NAME.get(provider)
    removed_token = False
    if token_name and not _token_still_used(block, surface, token_name):
        box.delete(token_name)
        removed_token = True
    removed = 0
    if purge and (state / "brain.sqlite").exists():
        con = open_brain(state / "brain.sqlite")
        try:
            removed = purge_surface(con, surface)
        finally:
            con.close()
    if isinstance(item, dict):
        item["enabled"] = False
        block[surface] = item
    adapter = Path(state) / "adapters" / f"{surface}.json"
    if adapter.exists():
        adapter.unlink()
    status = read_status(integrations_dir(state))
    status.pop(surface, None)
    write_status(integrations_dir(state), status)
    from hailports_setup.connect_assist import kind_for
    block["kind"] = kind_for(block)
    if removed_token:
        token_state = "removed"
    elif token_name:
        token_state = "kept"
    else:
        token_state = "none"
    return {"surface": surface, "purged": removed, "token": token_state}


def _token_still_used(block: dict, surface: str, token_name: str) -> bool:
    for other in SURFACES:
        if other == surface:
            continue
        item = block.get(other) or {}
        if item.get("enabled") and TOKEN_NAME.get(str(item.get("provider") or "")) == token_name:
            return True
    return False
