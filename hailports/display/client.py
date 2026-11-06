"""Match a viewer name to a desktop size.

Tailscale is optional. Pass a whois result when you already have one.
This module does not look for a tailscale binary and does not open a socket
to ask the network who a peer is. A VNC peer, reverse DNS, mDNS, a Screens
name, a Luna name, or the hostname are all just strings the caller supplies.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from hailports.display.catalog import IPAD_NATIVE, IPHONE_MODE, MAC_DEFAULT, Mode

IPHONE_RX = re.compile(r"(?:^|[^a-z0-9])(?:iphone|ipod|phone)(?:[^a-z0-9]|$)", re.I)
IPAD_RX = re.compile(r"(?:^|[^a-z0-9])ipad(?:[^a-z0-9]|$)", re.I)
MAC_RX = re.compile(r"\b(?:macbook|imac|mac)\b", re.I)


def looks_unresolved(name: str | None) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return True
    return False


def whois_name(whois: Any) -> str:
    """Read a hostname from a whois dict or string. Does not fetch anything."""
    if whois is None:
        return ""
    if isinstance(whois, str):
        return whois.split(".")[0].strip()
    if isinstance(whois, dict):
        for key in ("HostName", "DNSName", "hostname", "name"):
            value = whois.get(key)
            if value:
                return str(value).split(".")[0].strip()
    return ""


def classify(name: str | None) -> Mode | None:
    text = (name or "").strip()
    if not text or looks_unresolved(text):
        return None
    if IPAD_RX.search(text):
        return IPAD_NATIVE
    if IPHONE_RX.search(text):
        return IPHONE_MODE
    if MAC_RX.search(text):
        return MAC_DEFAULT
    return None


def match_client(
    name: str | None = None,
    *,
    whois: Any = None,
    rdns: str | None = None,
    mdns: str | None = None,
    screens: str | None = None,
    luna: str | None = None,
) -> Mode | None:
    """Return the client mode, or None when the desktop should stay as it is.

    `whois` is read only when the caller passed it and the name is still an
    address or empty. A resolved hostname never consults whois.
    """
    label = (name or "").strip()
    if looks_unresolved(label):
        if whois is not None:
            label = whois_name(whois)
        if looks_unresolved(label):
            for hint in (screens, luna, rdns, mdns):
                if hint and not looks_unresolved(hint):
                    label = hint.strip()
                    break
    return classify(label)


def lookup_whois(peer: str, runner: Callable[[str], Any] | None = None) -> Any:
    """Call `runner` only when the caller supplied one. Otherwise return None."""
    if runner is None:
        return None
    return runner(peer)
