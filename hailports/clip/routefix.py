"""Keep route.yaml stable across setup re-runs, relaunches and updates.

One identical engine is one entry. The engine the user picked (`preferred`) stays first.
Pure functions plus one cheap launch-time repair that writes a backup before it changes anything.
"""

from __future__ import annotations

import time
from pathlib import Path

_IDENTITY = ("via", "ssh_host", "remote_url", "base_url", "model", "bin")


def spec_identity(spec: dict) -> tuple:
    via = str(spec.get("via") or "http").strip().lower()
    return (via,) + tuple(str(spec.get(key) or "").strip() for key in _IDENTITY[1:])


def _preferred_slot(preferred: str, order: list, models: dict) -> str:
    if not preferred:
        return ""
    if preferred in models:
        return preferred
    for name in order:
        if str((models.get(name) or {}).get("via") or "").lower() == preferred:
            return name
    return ""


def tidy_paiste(paiste: dict | None) -> dict:
    """Drop duplicate engines (keep the first, or the preferred name) and put `preferred` first."""
    out = dict(paiste or {})
    raw_models = out.get("models")
    if not isinstance(raw_models, dict):
        return out
    raw_order = [str(name) for name in (out.get("order") or []) if isinstance(name, (str, int))]
    preferred = str(out.get("preferred") or "").strip().lower()
    # Ordered names: the order list first, then any model not listed.
    names = []
    for name in raw_order + [str(key) for key in raw_models]:
        if name not in names and isinstance(raw_models.get(name), dict):
            names.append(name)
    listed = set(raw_order)
    kept: dict[tuple, str] = {}
    slots: list[str] = []
    for name in names:
        ident = spec_identity(raw_models[name])
        if ident in kept:
            # A duplicate that carries the preferred name takes over the earlier slot's name.
            if name == preferred:
                at = slots.index(kept[ident])
                slots[at] = name
                kept[ident] = name
            continue
        kept[ident] = name
        slots.append(name)
    models = {name: raw_models[name] for name in slots}
    listed_ids = {spec_identity(raw_models[name]) for name in listed if name in raw_models}
    order = [name for name in slots if spec_identity(models[name]) in listed_ids]
    slot = _preferred_slot(preferred, order, models)
    if slot:
        order = [slot] + [name for name in order if name != slot]
    out["order"] = order
    out["models"] = models
    return out


def tidy_route(route: dict) -> dict:
    out = dict(route or {})
    if isinstance(out.get("paiste"), dict):
        out["paiste"] = tidy_paiste(out["paiste"])
    return out


def repair_route(path: Path, now: float | None = None) -> bool:
    """Dedupe an existing route.yaml in place. Backup first; no write when nothing changed. Never raises."""
    from hailports.config import dumps, loads
    try:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        route = loads(text)
        if not isinstance(route, dict) or not isinstance(route.get("paiste"), dict):
            return False
        fixed = tidy_route(route)
        if fixed == route:
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
        backup = path.with_name(path.name + ".bak-" + stamp)
        backup.write_text(text, encoding="utf-8")
        backup.chmod(0o600)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(dumps(fixed), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        return False
