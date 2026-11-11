"""Check the mode that is configured. API mode does not require a local model."""

from __future__ import annotations

import socket
from dataclasses import dataclass

from hailports.config import is_placeholder
from hailports.providers.factory import build_router
from hailports.secrets_store import EnvAndKeychain
from hailports.integrations.health import integration_checks
from hailports_setup.connect_assist import connection_status

_SECRET_KEYS = {"token", "api_key", "apikey", "secret", "password", "key"}
_SKIP_KEYS = {"key_env", "keychain_service", "keychain_account", "allowed_handle"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def secret_hits(cfg: dict) -> list[str]:
    hits: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                low = str(key).lower()
                here = path + "." + str(key)
                if low in _SKIP_KEYS:
                    continue
                if low in _SECRET_KEYS and isinstance(value, str):
                    if value.strip() and not is_placeholder(value):
                        hits.append(here)
                    continue
                walk(value, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(cfg, "")
    return hits


def file_contains_secret(text: str, secret: str | None) -> bool:
    if not secret or len(secret) < 6:
        return False
    return secret in text


def port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_doctor(cfg: dict, secrets=None, router=None, live: bool = True, root=None) -> list[Check]:
    secrets = secrets or EnvAndKeychain()
    mode = str(cfg.get("mode") or "local").strip().lower()
    checks = [Check("mode", mode in ("local", "hybrid", "api"), mode or "missing")]
    hits = secret_hits(cfg)
    checks.append(Check("secrets", not hits, "config has no api keys" if not hits else "key material in config"))
    if router is None:
        router = build_router(cfg, secrets)
    if mode in ("local", "hybrid"):
        if not live:
            checks.append(Check("local-model", True, "skipped"))
        elif router.local is None:
            checks.append(Check("local-model", False, "no local provider"))
        else:
            ok, detail = router.local.health()
            checks.append(Check("local-model", ok, detail))
    else:
        checks.append(Check("local-model", True, "skipped in api mode"))
    if mode in ("api", "hybrid"):
        key = secrets.get(cfg)
        if not live:
            checks.append(Check("api-key", True, "skipped"))
            checks.append(Check("cloud", True, "skipped"))
        else:
            checks.append(Check("api-key", bool(key), "found" if key else "set HAILPORTS_API_KEY or store it in the OS keychain"))
            if router.cloud is None or not key:
                checks.append(Check("cloud", False, "cloud provider was not checked"))
            else:
                ok, detail = router.cloud.health()
                checks.append(Check("cloud", ok, detail))
    else:
        checks.append(Check("cloud", True, "skipped in local mode"))
    if not live:
        checks.append(Check("embeddings", True, "skipped"))
    else:
        try:
            embed = router.embed(["ping"])
            detail = embed.provider + (" fallback" if embed.fallback else "")
            checks.append(Check("embeddings", True, detail))
        except Exception as exc:
            checks.append(Check("embeddings", False, str(exc)))
    web = cfg.get("webchat") or {}
    host = str(web.get("bind") or "127.0.0.1")
    port = int(web.get("port") or 8080)
    free = port_open(host, port)
    checks.append(Check("web-port", True, f"{host}:{port} " + ("free" if free else "already in use")))
    ok, detail = connection_status(cfg)
    checks.append(Check("connections", ok, detail))
    for name, healthy, detail in integration_checks(cfg, root):
        checks.append(Check(name, healthy, detail))
    return checks


def format_report(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = "ok" if check.ok else "needs attention"
        lines.append(f"{mark} {check.name} {check.detail}")
    return "\n".join(lines)


def failed(checks: list[Check]) -> bool:
    return any(not check.ok for check in checks)
