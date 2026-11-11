"""Reviewable loopback adapters. Steps come from the user's own model, then the cleaner."""

from __future__ import annotations

import json
from pathlib import Path

from hailports.integrations.items import Item
from hailports.integrations.store import IntegrationError
from hailports.local_bridges.fabric import _host_ok
from hailports_setup.connect_assist import (
    KIND_SURFACE,
    SURFACE_KIND,
    build_adapter_prompt,
    builtin_bridge_steps,
    normalize_adapter,
)


def save_adapters(directory: Path, adapters: dict) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for surface, doc in adapters.items():
        if surface not in SURFACE_KIND or not isinstance(doc, dict):
            continue
        path = directory / f"{surface}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def load_adapter(directory: Path, surface: str) -> dict | None:
    path = Path(directory) / f"{surface}.json"
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    kind = SURFACE_KIND.get(surface) or "mail"
    port = 9222
    url = str(loaded.get("base_url") or "")
    if ":" in url:
        tail = url.rsplit(":", 1)[-1].split("/")[0]
        if tail.isdigit():
            port = int(tail)
    return normalize_adapter(loaded, surface, port)


def draft_adapter(cloud, *, os_name: str, surface: str, port: int, secret: str = "") -> dict:
    """One subscription call. A bad reply falls back to a loopback adapter with no steps from the model."""
    kind = SURFACE_KIND.get(surface) or "mail"
    fallback = normalize_adapter({}, surface, port)
    fallback["steps"] = builtin_bridge_steps([kind])
    fallback["source"] = "defaults"
    prompt = build_adapter_prompt(os_name, surface, port)
    if cloud is None or (secret and len(secret) >= 6 and secret in prompt):
        return fallback
    try:
        reply = cloud.chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0)
        text = str(getattr(reply, "text", "") or "")
    except Exception:
        return fallback
    if secret and len(secret) >= 6 and secret in text:
        return fallback
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return fallback
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return fallback
    raw = doc.get("adapter") if isinstance(doc.get("adapter"), dict) else doc
    cleaned = normalize_adapter(raw, surface, port)
    cleaned["source"] = "subscription"
    if secret and len(secret) >= 6 and secret in json.dumps(cleaned):
        return fallback
    return cleaned


def adapters_from_plan(plan: dict, enabled: list[str], ports: dict) -> dict:
    raw = plan.get("adapters") if isinstance(plan.get("adapters"), dict) else {}
    out = {}
    for kind in enabled:
        surface = KIND_SURFACE.get(kind)
        if not surface:
            continue
        out[surface] = normalize_adapter(raw.get(surface), surface, int(ports.get(kind) or 9222))
    return out


def pull_bridge(base_url: str, export_path: str, surface: str, provider: str, fetch) -> list[Item]:
    if not _host_ok(base_url):
        raise IntegrationError("bridge url is not loopback")
    path = export_path if str(export_path or "").startswith("/") else "/export"
    url = str(base_url).rstrip("/") + path
    if not _host_ok(url):
        raise IntegrationError("bridge url is not loopback")
    if fetch is None:
        raise IntegrationError("bridge export was not configured")
    raw = fetch(url)
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise IntegrationError("bridge export was not a document list") from None
    rows = loaded if isinstance(loaded, list) else (loaded.get("items") if isinstance(loaded, dict) else None)
    if not isinstance(rows, list):
        raise IntegrationError("bridge export was not a document list")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        external = str(row.get("id") or row.get("external_id") or "")
        title = str(row.get("title") or "")
        body = str(row.get("body") or "")
        if not external or not (title or body):
            continue
        try:
            when = float(row.get("when") or 0)
        except (TypeError, ValueError):
            when = 0
        items.append(Item(surface, provider or "bridge", external, title, body, when))
    return items
