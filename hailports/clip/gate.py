"""License gate. A draft needs a signed key. The signing secret stays off the app."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request
from pathlib import Path

def license_path() -> Path:
    return Path.home() / ".config" / "paiste" / "license"


def secret_path() -> Path:
    return Path.home() / ".config" / "paiste" / "license-secret"


def read_token(cfg: dict | None = None) -> str:
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("license") or "").strip()
    if raw:
        return raw
    path = license_path()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def sign(payload: dict, secret: bytes) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body.hex() + "." + sig


def _open_token(token: str) -> dict | None:
    try:
        body_hex, _sig = token.split(".", 1)
        return json.loads(bytes.fromhex(body_hex).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def verify(token: str, secret: bytes) -> bool:
    try:
        body_hex, sig = token.split(".", 1)
        body = bytes.fromhex(body_hex)
    except ValueError:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return False
    exp = int(payload.get("exp") or 0)
    return exp == 0 or exp > int(time.time())


def _cache_ok(token: str) -> bool:
    path = Path.home() / ".config" / "paiste" / "license-cache"
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if doc.get("token") != hashlib.sha256(token.encode("utf-8")).hexdigest():
        return False
    return int(time.time()) - int(doc.get("at") or 0) < 7 * 24 * 3600


def _cache_write(token: str) -> None:
    path = Path.home() / ".config" / "paiste" / "license-cache"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "token": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "at": int(time.time()),
    }), encoding="utf-8")


def allowed(cfg: dict | None = None, urlopen=None) -> bool:
    token = read_token(cfg)
    if not token:
        return False
    secret_file = secret_path()
    if secret_file.is_file():
        return verify(token, secret_file.read_bytes().strip())
    if _cache_ok(token):
        return True
    paiste = (cfg or {}).get("paiste") or {}
    url = str(paiste.get("license_url") or "https://www.paisty.com/api/license").strip()
    opener = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener(req, timeout=6) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        return False
    if doc.get("ok") is True:
        _cache_write(token)
        return True
    return False
