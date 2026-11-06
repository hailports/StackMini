"""Mail draft, meetings, tracker, and browser CDP session bridges.

Health is a probe. Idle recycle only runs when no CDP client is connected
and the owning job is not running, across several observations. A restart
goes through the service manager and only for the recorded process id.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

from hailports.brain.httpask import overlay_network
from hailports.osapi import restart_exact

KINDS = ("mail", "meetings", "tracker", "cdp")


class DraftOnly(RuntimeError):
    """There is no send path."""


@dataclass
class Spec:
    name: str
    enabled: bool
    base_url: str = ""
    label: str = ""
    recorded_pid: int | None = None

    @property
    def configured(self) -> bool:
        return self.enabled


def _host_ok(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if host in ("localhost",):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in overlay_network()


def specs_from_config(cfg: dict | None) -> list[Spec]:
    block = (cfg or {}).get("local_bridges") or {}
    found = []
    for name in KINDS:
        item = block.get(name) or {}
        enabled = bool(item.get("enabled"))
        base = str(item.get("base_url") or "").strip()
        if enabled and not _host_ok(base):
            enabled = False
        pid = item.get("pid")
        try:
            recorded = int(pid) if pid else None
        except (TypeError, ValueError):
            recorded = None
        found.append(Spec(name=name, enabled=enabled, base_url=base, label=str(item.get("label") or ""), recorded_pid=recorded))
    return found


def enabled_specs(cfg: dict | None) -> list[Spec]:
    return [spec for spec in specs_from_config(cfg) if spec.enabled]


class DraftBook:
    """Drafts stay drafts. send() always refuses."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def write(self, kind: str, body: str) -> dict:
        if kind not in ("mail", "meetings", "tracker"):
            raise ValueError("unknown draft kind")
        draft_id = str(len(self._rows) + 1)
        row = {"id": draft_id, "kind": kind, "body": body, "status": "draft", "sent": False}
        self._rows[draft_id] = row
        return dict(row)

    def send(self, draft_id: str) -> None:
        raise DraftOnly("no send path")


def probe(fetch) -> dict:
    try:
        status = int(fetch())
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": status == 200, "status": status}


def recycle_allowed(*, established_clients: int, job_running: bool, idle_observations: int, min_observations: int = 2) -> bool:
    """True only when the session is idle across several checks. Never mid-job."""
    if established_clients > 0 or job_running:
        return False
    return idle_observations >= min_observations


def cdp_decide(
    *,
    cpu: float,
    main_cpu: float,
    renderer_cpu: float,
    hot_streak: int,
    reloads: int,
    recycles: int,
    lock_held: bool,
    login: bool,
    composer: bool,
    bounce_ok: bool,
    thresh: float = 100.0,
    min_samples: int = 3,
    reloads_before: int = 2,
) -> str:
    """What to do with a hot CDP browser. A composer, a lock, or a login is left alone."""
    if cpu < thresh:
        return "clear"
    if login:
        return "skip_login"
    if lock_held:
        return "skip_lock"
    if composer:
        return "skip_composer"
    if hot_streak < min_samples:
        return "watch"
    if reloads < reloads_before:
        return "reload"
    if renderer_cpu >= thresh and main_cpu < thresh and recycles < 1:
        return "recycle"
    if bounce_ok:
        return "bounce"
    return "skip_no_bounce"


def gc_allowed(*, flag_on: bool, lock_free: bool, composer_empty: bool, footprint_mb: float, minimum_mb: float = 900) -> bool:
    """Garbage collection only. Never a reason to quit, reload, or click."""
    return bool(flag_on and lock_free and composer_empty and footprint_mb >= minimum_mb)


def heal(mgr, label: str, recorded_pid: int | None, current_pid: int | None, healthy: bool) -> str:
    """Restart only through the service manager, and only for the recorded pid."""
    return restart_exact(mgr, label, recorded_pid, current_pid, healthy)
