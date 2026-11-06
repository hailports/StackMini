"""Refuse legal and finance paths, and chunks that look like secrets.

This is a generic safety check. It does not know about any one workplace.
A question that asks for a password, a seed phrase, a private key, or a
one-time code is blocked before retrieval. A stored chunk is dropped when
its path has a restricted segment, or when the text itself holds a secret.
"""

from __future__ import annotations

import re

_QUERY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:seed|recovery)[\s-]+phrases?\b"), "seed_phrase"),
    (re.compile(r"(?i)\bprivate\s+keys?\b"), "private_key"),
    (re.compile(r"(?i)\b(?:passcodes?|passphrases?|credentials?)\b|\bpasswords?\b(?!\s+(?:reset|recovery|polic(?:y|ies)|runbook|flow|template|link|lifetime|lockout|expired?|expiry|change\s+flow))"), "credentials"),
    (re.compile(r"(?i)\blogin[\s-]+secrets?\b"), "credentials"),
    (re.compile(r"(?i)\bapi[\s_-]*keys?\b"), "api_key"),
    (re.compile(r"(?i)\b(?:api|access|auth|bearer|secret)[\s_-]*tokens?\b"), "api_token"),
    (re.compile(r"(?i)\b(?:2fa|mfa|otp)\b"), "otp"),
    (re.compile(r"(?i)\bverification[\s-]+codes?\b"), "otp"),
    (re.compile(r"(?i)\bone[\s-]*time[\s-]+(?:codes?|passwords?)\b"), "otp"),
    (re.compile(r"(?i)\btwo[\s-]*factor\b"), "otp"),
    (re.compile(r"(?i)\b(?:lawyers?|attorneys?)\b"), "legal_counsel"),
    (re.compile(r"(?i)\blegal\s+counsel\b"), "legal_counsel"),
    (re.compile(r"(?i)\b(?:legal|finance)\s+folders?\b"), "restricted_folder"),
    (re.compile(r"(?i)\baccountants?\b"), "accountant"),
    (re.compile(r"(?i)\bcpas?\b"), "accountant"),
    (re.compile(r"(?i)\btax\s+returns?\b"), "tax_return"),
    (re.compile(r"(?i)\bbank(?:ing)?\b|\bbank\s+statements?\b"), "bank"),
    (re.compile(r"(?i)\bwire\s+(?:transfers?|instructions?|money|funds|payments?)\b"), "wire"),
    (re.compile(r"(?i)\bpayroll\b"), "payroll"),
    (re.compile(r"(?i)\bsalar(?:y|ies)\b"), "salary"),
)

_SEGMENT = re.compile(
    r"(?i)\b(legal|law|litigation|contracts?|finance|financ\w*|accounting|"
    r"treasury|tax(?:es)?|payroll|bank(?:ing)?|compensation|salar\w*)\b"
)

_SEED_RE = re.compile(r"(?i)\b(?:seed|recovery)[\s-]+phrases?\b")
_OTP_RE = re.compile(
    r"(?i)(?:\b(?:2fa|mfa|otp)(?:\s+codes?)?\b|\b(?:verification|security)\s+codes?\b).{0,20}?\d{4,8}\b"
)
_PW_RE = re.compile(r"(?i)\b(?:password|passcode|pwd)\b\s*(?::|=|\bis\b)\s*(?!\[masked\])\S+")
_PRIVACY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:token|api[_ -]?key|secret|client[_ -]?secret|access[_ -]?key|password)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{8,})"
)
_CORR_RE = re.compile(
    r"(?i)attorney[- ]client privilege|privileged (and|&) confidential|"
    r"bank statement|routing (number|no)|wire instructions"
)


def query_block(question: str) -> str | None:
    text = question or ""
    for pattern, reason in _QUERY_RULES:
        if pattern.search(text):
            return reason
    return None


def path_block(value: str) -> str | None:
    text = value or ""
    if "/" not in text and "\\" not in text:
        return None
    for segment in text.replace("\\", "/").split("/"):
        segment = segment.strip()
        if segment and _SEGMENT.search(segment):
            return "restricted_path"
    return None


def hit_block(hit: dict) -> str | None:
    if not isinstance(hit, dict):
        return None
    for key in ("source", "url", "path", "ref"):
        reason = path_block(str(hit.get(key) or ""))
        if reason:
            return reason
    blob = "\n".join((str(hit.get("text") or hit.get("body") or ""), str(hit.get("title") or "")))
    if _SEED_RE.search(blob) or _OTP_RE.search(blob) or _PW_RE.search(blob) or _PRIVACY.search(blob):
        return "secret"
    if _CORR_RE.search(blob):
        return "privileged"
    return None
