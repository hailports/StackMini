"""hailports integrations: connect, sync, prove, disconnect."""

from __future__ import annotations

import os
import time
from pathlib import Path

from hailports.config import dumps, save_file
from hailports.integrations.items import SURFACES
from hailports.integrations.oauth import (
    exchange_code,
    google_auth_url,
    google_device_finish,
    google_device_start,
    graph_device_finish,
    graph_device_start,
    listen_for_code,
    take_code,
)
from hailports.integrations.store import IntegrationError, TokenBox, load_mask, safe_text, save_mask
from hailports.integrations.sync import TOKEN_NAME, disconnect_surface, integrations_dir, sync_configured
from hailports.integrations.index import latest_text, open_brain, prove
from hailports.integrations.items import needle_for

PROVIDERS = {
    "email": {"gmail", "graph", "imap", "apple-mail"},
    "calendar": {"gcal", "graph", "caldav", "apple-calendar"},
    "notes": {"files", "graph", "carddav", "apple-notes"},
    "chats": {"apple-messages", "bridge"},
}


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "integrations",
        help="Connect email, calendar, notes, and chats, then search them.",
    )
    parser.add_argument("action", choices=["status", "connect", "sync", "disconnect", "prove"])
    parser.add_argument("surface", nargs="?")
    parser.add_argument("--via", default="", choices=["", "approved", "bridges", "native", "skip"])
    parser.add_argument("--provider", default="")
    parser.add_argument("--client-id", default="")
    parser.add_argument("--client-secret", default="")
    parser.add_argument("--account", default="")
    parser.add_argument("--app-password", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--service", default="", choices=["", "icloud", "fastmail"])
    parser.add_argument("--notes-dir", default="")
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--device", action="store_true")
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--code", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--mask", default="", help="Extra literals to redact. Stored in a private file, not the config.")


def dispatch(args, cfg: dict, cfg_path: Path, state: Path) -> int:
    action = args.action
    try:
        if action == "status":
            return _status(cfg, state)
        if action == "prove":
            return _prove(cfg, state, args.surface)
        if action == "sync":
            return _sync(cfg, state, args.surface, args.days)
        if action == "disconnect":
            if not args.surface:
                print("Name the surface to disconnect.")
                return 2
            result = disconnect_surface(cfg, state, args.surface, purge=bool(args.purge))
            _save(cfg, cfg_path, state)
            print(f"disconnected {result['surface']} purged {result['purged']}")
            return 0
        if action == "connect":
            return _connect(args, cfg, cfg_path, state)
    except IntegrationError as exc:
        print(safe_text(exc))
        return 2
    print("Unknown integrations action.")
    return 2


def _connect(args, cfg: dict, cfg_path: Path, state: Path) -> int:
    surface = str(args.surface or "")
    if surface not in SURFACES:
        print("Name a surface: email, calendar, notes, or chats.")
        return 2
    provider = str(args.provider or "").strip().lower()
    via = str(args.via or "approved")
    if via == "bridges":
        provider = provider or "bridge"
    if via == "approved" and provider not in PROVIDERS.get(surface, set()):
        print("That provider is not an approved path for " + surface + ".")
        return 2
    if args.mask:
        current = load_mask(integrations_dir(state))
        save_mask(integrations_dir(state), current + [part.strip() for part in args.mask.split(",")])
    block = cfg.setdefault("integrations", {})
    if args.days:
        block["window_days"] = int(args.days)
    block.setdefault("window_days", 30)
    block.setdefault("mask", [])
    for name in SURFACES:
        block.setdefault(name, {"enabled": False, "via": "skip", "provider": "", "app": "", "path": ""})
    item = dict(block.get(surface) or {})
    item["via"] = via
    item["provider"] = provider
    item["app"] = ""
    if provider == "files":
        folder = str(args.notes_dir or item.get("path") or "")
        if not folder:
            print("Pass --notes-dir for the notes folder.")
            return 2
        item["path"] = folder
    else:
        item["path"] = str(item.get("path") or "")
    if provider in TOKEN_NAME or provider == "bridge":
        _store_login(args, state, provider)
    item["enabled"] = True
    block[surface] = item
    from hailports_setup.connect_assist import kind_for
    block["kind"] = kind_for(block)
    if _save(cfg, cfg_path, state) != 0:
        return 2
    proofs = sync_configured(cfg, state, only=surface)
    return _print_proofs(proofs)


def _store_login(args, state: Path, provider: str) -> None:
    name = TOKEN_NAME.get(provider)
    if name is None:
        return
    box = TokenBox(integrations_dir(state))
    existing = box.get(name) or {}
    if name in ("google", "graph"):
        client_id = str(args.client_id or os.environ.get("HAILPORTS_GOOGLE_CLIENT_ID" if name == "google" else "HAILPORTS_GRAPH_CLIENT_ID") or existing.get("client_id") or "")
        client_secret = str(args.client_secret or os.environ.get("HAILPORTS_CLIENT_SECRET") or existing.get("client_secret") or "")
        if existing.get("access_token") and not args.code and not args.device and not args.loopback:
            return
        if not client_id:
            raise IntegrationError("set your own client id in the environment or pass --client-id")
        token = _oauth(args, name, client_id, client_secret)
        token["client_id"] = client_id
        if client_secret:
            token["client_secret"] = client_secret
        box.put(name, token)
        return
    account = str(args.account or os.environ.get("HAILPORTS_ACCOUNT") or existing.get("account") or "")
    password = str(args.app_password or os.environ.get("HAILPORTS_APP_PASSWORD") or existing.get("password") or "")
    host = str(args.host or args.url or existing.get("host") or existing.get("url") or "")
    service = str(args.service or existing.get("service") or "")
    if not password or not account or not (host or service):
        raise IntegrationError("set the account, the app password, and the host or service")
    payload = {"account": account, "password": password, "service": service}
    if provider == "imap":
        payload["host"] = host
    else:
        payload["url"] = host or args.url
    box.put(name, payload)


def _oauth(args, name: str, client_id: str, client_secret: str) -> dict:
    if args.code:
        port = int(args.port or 8765)
        redirect = f"http://127.0.0.1:{port}/callback"
        token = exchange_code(None, client_id, args.code, redirect, client_secret)
        return _stamp(token)
    if name == "google" and args.loopback and not args.device:
        return _stamp(_loopback(client_id, client_secret, int(args.port or 0)))
    if name == "google":
        prompt = google_device_start(None, client_id)
        _show_device(prompt)
        return _stamp(google_device_finish(None, client_id, prompt, client_secret))
    prompt = graph_device_start(None, client_id)
    _show_device(prompt)
    return _stamp(graph_device_finish(None, client_id, prompt))


def _loopback(client_id: str, client_secret: str, port: int) -> dict:
    import secrets
    state = secrets.token_hex(8)
    server, queue = listen_for_code(state, port=port)
    try:
        bound = int(server.server_address[1])
        redirect = f"http://127.0.0.1:{bound}/callback"
        print("Open this address in your browser:")
        print(google_auth_url(client_id, redirect, state))
        code = take_code(queue, 180)
    finally:
        server.shutdown()
        server.server_close()
    return exchange_code(None, client_id, code, redirect, client_secret)


def _show_device(prompt) -> None:
    print("Enter this code: " + prompt.user_code)
    print(prompt.verification_url)


def _stamp(token: dict) -> dict:
    stamped = dict(token)
    try:
        expiry = float(stamped.get("expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if 0 < expiry < 10_000_000:
        stamped["expiry"] = time.time() + expiry
    return stamped


def _sync(cfg: dict, state: Path, surface: str | None, days: int) -> int:
    if days:
        cfg.setdefault("integrations", {})["window_days"] = int(days)
    if surface and surface not in SURFACES:
        print("Unknown surface.")
        return 2
    proofs = sync_configured(cfg, state, only=surface or None)
    if not proofs:
        print("Nothing is connected. Run hailports integrations connect.")
        return 0
    return _print_proofs(proofs)


def _prove(cfg: dict, state: Path, surface: str | None) -> int:
    db = Path(state) / "brain.sqlite"
    if not db.exists():
        print("proof fail nothing ingested")
        return 1
    names = [surface] if surface else list(SURFACES)
    failed = False
    con = open_brain(db)
    try:
        for name in names:
            if name not in SURFACES:
                print("Unknown surface.")
                return 2
            title, body = latest_text(con, name)
            proof = prove(con, name, needle_for(title, body))
            print(_line(proof))
            failed = failed or not proof["ok"]
    finally:
        con.close()
    return 1 if failed else 0


def _status(cfg: dict, state: Path) -> int:
    block = cfg.get("integrations") or {}
    box = TokenBox(integrations_dir(state))
    print("kind " + str(block.get("kind") or "off"))
    print("window_days " + str(block.get("window_days") or 30))
    for surface in SURFACES:
        item = block.get(surface) if isinstance(block.get(surface), dict) else {}
        provider = str((item or {}).get("provider") or "")
        token_name = TOKEN_NAME.get(provider)
        present = "yes" if token_name and box.get(token_name) else "no"
        enabled = "on" if (item or {}).get("enabled") else "off"
        via = str((item or {}).get("via") or "skip")
        print(f"{surface} {enabled} via {via} provider {provider or '-'} token {present}")
    return 0


def _print_proofs(proofs: list[dict]) -> int:
    failed = False
    for proof in proofs:
        print(_line(proof))
        failed = failed or not proof.get("ok")
    return 1 if failed else 0


def _line(proof: dict) -> str:
    mark = "pass" if proof.get("ok") else "fail"
    return "proof " + str(proof.get("surface") or "") + " " + mark


def _save(cfg: dict, cfg_path: Path, state: Path) -> int:
    text = dumps(cfg)
    box = TokenBox(integrations_dir(state))
    for name in ("google", "graph", "imap", "caldav", "carddav"):
        payload = box.get(name) or {}
        for key in ("access_token", "refresh_token", "password", "client_secret", "app_password"):
            value = str(payload.get(key) or "")
            if len(value) >= 6 and value in text:
                print("Refusing to write the config because it contains a secret.")
                return 2
    save_file(cfg_path, cfg)
    return 0
