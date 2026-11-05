"""2026-11-04 [t349u] @hailports link-out: deep dives, side quests, analytics/findings threads
end with a final reply carrying one clean (UTM-free) hailports.com or GitHub URL.
Single originals stay link-free (handled elsewhere; this only touches threads)."""
from __future__ import annotations
import re

LINK_KINDS = re.compile(r"deep|side|quest|analytic|finding|launch|howto|how_to", re.I)
_URL = re.compile(r"https?://\S+")
TARGETS = [  # (pattern in thread text, url, final-reply text)
    (re.compile(r"\barrange\b|ipad|sidecar|display arrang", re.I),
     "https://github.com/hailports/arrange", "the app is open source: https://github.com/hailports/arrange"),
    (re.compile(r"stack ?mini|mac mini|\bmini\b|local model|tok/s", re.I),
     "https://hailports.com/stackmini", "full setup and repo here: https://hailports.com/stackmini"),
]
DEFAULT = ("https://hailports.com", "more builds like this: https://hailports.com")


def clean(url: str) -> str:
    url = re.sub(r"[?&](utm_[^=&]+|ref|fbclid|gclid)=[^&#\s]*", "", url)
    return url.rstrip("?&")


def wants_link(kind: str | None) -> bool:
    return bool(kind) and bool(LINK_KINDS.search(str(kind)))


def ensure_final_link(parts: list[str], kind: str | None) -> list[str]:
    parts = [str(p).strip() for p in parts if str(p).strip()]
    if not parts or not wants_link(kind):
        return parts
    if len(parts) > 1 and _URL.search(parts[-1]):
        parts[-1] = _URL.sub(lambda m: clean(m.group(0)), parts[-1])
        return parts
    blob = " ".join(parts)
    for pat, _u, line in TARGETS:
        if pat.search(blob):
            return parts + [line]
    return parts + [DEFAULT[1]]
