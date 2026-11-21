"""License gate. A draft needs a signed key, or a running trial's entitlement. The signing secret stays off the app."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

_DEVICE: list[str] = []


def device_hash(uuid: str) -> str:
    """Same as the app: salted SHA-256 of the hardware UUID, 32 hex. The UUID never leaves the Mac."""
    return hashlib.sha256(("paisty-device:" + uuid.upper()).encode("utf-8")).hexdigest()[:32]


def device_id(run=subprocess.run) -> str:
    if _DEVICE:
        return _DEVICE[0]
    value = ""
    try:
        out = run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=5).stdout
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', out or "")
        value = device_hash(match.group(1)) if match else ""
    except (OSError, subprocess.SubprocessError):
        value = ""
    _DEVICE.append(value)
    return value


def license_path() -> Path:
    return Path.home() / ".config" / "paiste" / "license"


def secret_path() -> Path:
    return Path.home() / ".config" / "paiste" / "license-secret"


def entitlement_path() -> Path:
    return Path.home() / ".config" / "paiste" / "entitlement"


def read_entitlement() -> dict:
    """The trial entitlement the app saved after checkout. Not a license: never in the license file."""
    try:
        doc = json.loads(entitlement_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    token = str(doc.get("token") or "") if isinstance(doc, dict) else ""
    if not token.startswith("pe1.") or token.count(".") != 2 or len(token) > 4000:
        return {}
    try:
        ends = int(doc.get("ends") or 0)
    except (TypeError, ValueError):
        ends = 0
    return {"token": token, "ends": ends}


def credentials(cfg: dict | None = None) -> list[dict]:
    """What a server call can carry, in order: the license, then a running trial's entitlement."""
    out = []
    token = read_token(cfg)
    if token:
        out.append({"token": token})
    ent = read_entitlement().get("token")
    if ent:
        out.append({"entitlement": ent})
    return out


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


def verify(token: str, secret: bytes, device: str | None = None) -> bool:
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
    dev = str(payload.get("dev") or "")
    if dev and dev != (device_id() if device is None else device):
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


# A trial's yes is cached for 15 minutes, so a cancelled or failed trial stops soon after Stripe says so.
# Offline, a trial that checked out within the last day keeps drafting until a day after its end.
ENT_CACHE = 15 * 60
ENT_OFFLINE = 24 * 3600


def _ent_cache_path() -> Path:
    return Path.home() / ".config" / "paiste" / "entitlement-cache"


def _ent_cache_read() -> dict:
    try:
        doc = json.loads(_ent_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _ent_cache_write(digest: str, now: int) -> None:
    path = _ent_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": digest, "at": now}), encoding="utf-8")


def _entitlement_ok(ent: dict, cfg: dict | None, urlopen=None) -> bool:
    token = ent["token"]
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = int(time.time())
    cache = _ent_cache_read()
    same = cache.get("token") == digest
    age = now - int(cache.get("at") or 0)
    if same and age < ENT_CACHE:
        return True
    paiste = (cfg or {}).get("paiste") or {}
    url = str(paiste.get("license_url") or "https://www.paisty.com/api/license").strip()
    opener = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"entitlement": token, "device": device_id()}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "paisty/1"},
            method="POST",
        )
        with opener(req, timeout=6) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        ends = int(ent.get("ends") or 0)
        return same and age < ENT_OFFLINE and (not ends or now < ends + 24 * 3600)
    if doc.get("ok") is True:
        _ent_cache_write(digest, now)
        return True
    try:
        _ent_cache_path().unlink()
    except OSError:
        pass
    return False


def allowed(cfg: dict | None = None, urlopen=None) -> bool:
    """A working license first. Without one, a running trial's entitlement."""
    token = read_token(cfg)
    if token and _license_ok(token, cfg, urlopen):
        return True
    ent = read_entitlement()
    return bool(ent) and _entitlement_ok(ent, cfg, urlopen)


def _license_ok(token: str, cfg: dict | None, urlopen=None) -> bool:
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
            data=json.dumps({"token": token, "device": device_id()}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "paisty/1"},
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
