"""Keep route.yaml stable across setup re-runs, relaunches and updates.

One identical engine is one entry. The engine the user picked (`preferred`) stays first.
A write never takes away the user's `order` or `preferred`, never swaps a saved engine address
for a default, and the launch-time repair only fills keys that are missing (backup first).
The default local model address is the Mini's :18080; the old :8080 is never written as a default.
"""

from __future__ import annotations

import time
from pathlib import Path

_IDENTITY = ("via", "ssh_host", "remote_url", "base_url", "model", "bin")
DEFAULT_LOCAL_URL = "http://127.0.0.1:18080/v1"
LEGACY_LOCAL_URLS = ("http://127.0.0.1:8080/v1", "http://localhost:8080/v1")


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


def keep_choice(old: dict | None, new: dict | None, pick: str = "", edits: bool = False) -> dict:
    """Merge a setup result into the saved settings without overriding the user's choice.

    old    the paiste block on disk; new  what a setup step produced.
    pick   an engine the user just picked by name (Use cursor): it becomes `preferred` and first.
    edits  the user edited engines in the setup form: their rows replace the saved rows
           (removed rows go), but a legacy :8080 default never replaces a saved address.
    Without edits nothing saved is dropped or changed; new keys and engines are only added.
    Always: the saved `order` keeps its order, the saved `preferred` stays first, and a new
    engine identical to a saved one is not added again.
    """
    old = dict(old or {})
    new = dict(new or {})
    old_models = old.get("models") if isinstance(old.get("models"), dict) else {}
    if not old_models:
        return tidy_paiste(new)
    new_models = new.get("models") if isinstance(new.get("models"), dict) else {}
    out = dict(new)
    for key, value in old.items():
        if key in ("models", "order", "preferred"):
            continue
        if not edits or key not in out:
            out[key] = value
    models: dict = {}
    for name, spec in old_models.items():
        if not isinstance(spec, dict):
            continue
        if name in new_models and isinstance(new_models[name], dict):
            fresh = new_models[name]
            merged = dict(spec)
            for key, value in fresh.items():
                if key not in merged:
                    merged[key] = value
                elif edits and value != merged[key]:
                    if str(value).strip() in LEGACY_LOCAL_URLS and str(merged[key]).strip():
                        continue
                    merged[key] = value
            models[name] = merged
        elif not edits:
            models[name] = dict(spec)
    seen = {spec_identity(spec) for spec in models.values()}
    for name, spec in new_models.items():
        if name in models or not isinstance(spec, dict):
            continue
        ident = spec_identity(spec)
        if ident in seen:
            continue
        seen.add(ident)
        models[name] = dict(spec)
    order: list = []
    for name in list(old.get("order") or []) + list(new.get("order") or []):
        name = str(name)
        if name in models and name not in order:
            order.append(name)
    if not order:
        order = list(models)
    preferred = str(pick or old.get("preferred") or new.get("preferred") or "").strip().lower()
    slot = _preferred_slot(preferred, order, models)
    if slot:
        order = [slot] + [name for name in order if name != slot]
    out["models"] = models
    out["order"] = order
    if preferred:
        out["preferred"] = preferred
    else:
        out.pop("preferred", None)
    return out


def fill_missing(paiste: dict) -> dict:
    """Launch repair: add keys that are missing, change nothing that is there."""
    out = dict(paiste or {})
    models = out.get("models")
    if isinstance(models, dict) and models and not out.get("order"):
        out["order"] = [str(name) for name in models]
    return out


def repair_route(path: Path, now: float | None = None) -> bool:
    """Fill missing keys in an existing route.yaml. Never reorders, drops or rewrites what is there.
    Backup first; no write when nothing is missing. Never raises."""
    from hailports.config import dumps, loads
    try:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        route = loads(text)
        if not isinstance(route, dict) or not isinstance(route.get("paiste"), dict):
            return False
        fixed = dict(route)
        fixed["paiste"] = fill_missing(route["paiste"])
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
