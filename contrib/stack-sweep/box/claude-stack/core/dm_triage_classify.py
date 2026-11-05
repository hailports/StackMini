#!/usr/bin/env python3
"""Pure hailports X DM request classifier (no network, no CDP).

Labels: spam_sex_bot | real_opportunity | unsure
Fail closed toward unsure. Never decides accept/block — callers own actions.
"""
from __future__ import annotations

import re
from typing import Any

LABELS = ("spam_sex_bot", "real_opportunity", "unsure")

# Strong spam: sexual solicitation, paid NSFW funnels, crypto-romance openers.
_SPAM = [
    r"\bonlyfans\b",
    r"\bfansly\b",
    r"\bfanvue\b",
    r"\bmanyvids\b",
    r"\blinktr\.ee\b",
    r"\ballmylinks\b",
    r"\bsext?ing\b",
    r"\bnudes?\b",
    r"\bnsfw\b",
    r"\bporn\b",
    r"\bcock\b",
    r"\bdick\b",
    r"\bpussy\b",
    r"\boral\b",
    r"\bblow\s*job\b",
    r"\bcam\s*girl\b",
    r"\bescort\b",
    r"\bsugar\s*(?:baby|daddy|mom)\b",
    r"\bhot\s+wives?\b",
    r"\blonely\s+(?:girl|woman|lady)\b",
    r"\bcheck\s+(?:my\s+)?(?:bio|profile|link)\b",
    r"\bsee\s+(?:my\s+)?(?:bio|link\s+in\s+bio)\b",
    r"\blink\s+in\s+(?:my\s+)?bio\b",
    r"\bsubscribe\s+(?:to\s+)?(?:my|me)\b",
    r"\bi(?:'| a)m\s+(?:horny|wet|naughty)\b",
    r"\bwant\s+to\s+(?:fuck|sex|hook\s*up)\b",
    r"\bmiss\s+you\b.{0,40}\b(?:baby|handsome|honey)\b",
    r"\bhey\s+(?:handsome|sexy|cutie|babe|baby|honey|dear)\b",
    r"\bhi\s+(?:handsome|sexy|cutie|babe)\b",
    r"\b(?:btc|bitcoin|eth|crypto|usdt|binance).{0,40}(?:profit|invest|double|gift)\b",
    r"\b(?:profit|invest|double|gift).{0,40}(?:btc|bitcoin|eth|crypto|usdt)\b",
    r"\btelegram\b.{0,30}(?:@|t\.me/)",
    r"\bt\.me/\w+",
    r"\bwhats?app\b.{0,40}\+?\d{8,}",
    r"\bcustom\s+(?:vid|video|pic)\b",
    r"\bpay\s*(?:per|for)\s*(?:view|pic|vid)\b",
    r"\bfree\s+trial\b.{0,40}(?:of|on|at)\b",
    r"\b18\+\b",
    r"\badult\s+content\b",
]

# Partnership / builder / product signals for @hailports.
_OPP = [
    r"\bpartner(?:ship|ing)?\b",
    r"\bcollab(?:oration|orate)?\b",
    r"\bsponsor(?:ship|ed)?\b",
    r"\baffiliate\b",
    r"\bintegrat(?:e|ion)\b",
    r"\bapi\b",
    r"\bstackmini\b",
    r"\bhailports\b",
    r"\bmac\s*mini\b",
    r"\blocal\s+(?:llm|model|ai|inference)\b",
    r"\bollama\b",
    r"\bvllm\b",
    r"\bmlx\b",
    r"\bgpu\b",
    r"\btok/?s\b",
    r"\btokens?\s*/\s*s(?:ec)?\b",
    r"\bself[- ]?host",
    r"\bopen[- ]?source\b",
    r"\bmit\s+licen[cs]e\b",
    r"\bbought\b.{0,40}(?:stackmini|/buy)",
    r"\bpurchas(?:e|ed)\b",
    r"\bcheckout\b",
    r"\bstripe\b",
    r"\binvoice\b",
    r"\bpress\b",
    r"\bpodcast\b",
    r"\binterview\b",
    r"\bnewsletter\b",
    r"\bguest\s+post\b",
    r"\bwould\s+love\s+to\s+(?:chat|connect|collab|partner)\b",
    r"\bbuild(?:ing|er)?\b.{0,40}(?:local|mini|llm|agent)",
    r"\bagents?\b.{0,40}(?:local|mini|stack)",
    r"\bdev\s*tools?\b",
    r"\byour\s+(?:product|tool|repo|setup|guide)\b",
]

# Weak spam / low-signal openers (alone → unsure or spam if stacked).
_WEAK_SPAM = [
    r"^\s*h+e+y+\s*[!.?]*\s*$",
    r"^\s*h+i+\s*[!.?]*\s*$",
    r"^\s*hello\s*[!.?]*\s*$",
    r"^\s*wyd\s*[!.?]*\s*$",
    r"^\s*hru\s*[!.?]*\s*$",
    r"^\s*what'?s?\s+up\s*[!.?]*\s*$",
    r"^\s*you\s+look\s+(?:lonely|bored)\b",
    r"\bignore\s+(?:the|my)\s+(?:last|previous)\b",
]

_SPAM_RE = [re.compile(p, re.I) for p in _SPAM]
_OPP_RE = [re.compile(p, re.I) for p in _OPP]
_WEAK_RE = [re.compile(p, re.I) for p in _WEAK_SPAM]

# Display-name / handle tells (common OF / romance-bot patterns).
_HANDLE_SPAM = re.compile(
    r"(?:onlyfans|fansly|free\s*nudes?|hot\s*wife|sugar|xxx|nsfw|sexy|camgirl|"
    r"nude|boobs?|feet\s*pic)",
    re.I,
)
_GENERIC_HANDLE = re.compile(
    r"^(?:user|girl|babe|sexy|hot|lovely|pretty)\d{2,}$",
    re.I,
)


def _blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("text") or ""),
        str(row.get("preview") or ""),
        str(row.get("display_name") or ""),
        str(row.get("bio") or ""),
        str(row.get("handle") or row.get("user") or ""),
    ]
    return "\n".join(parts)


def _hits(res: list[re.Pattern[str]], text: str) -> list[str]:
    out = []
    for r in res:
        m = r.search(text)
        if m:
            out.append(m.group(0)[:80])
    return out


def classify(row: dict[str, Any]) -> dict[str, Any]:
    """Return {label, reasons, spam_hits, opp_hits, score_spam, score_opp}.

    Bucket hint: row['bucket'] in {priority, you_may_know, hidden, low_quality, unknown}.
    Hidden alone is NOT enough for spam_sex_bot (X misfiles real people too).
    """
    text = _blob(row)
    handle = str(row.get("handle") or row.get("user") or "")
    bucket = str(row.get("bucket") or "unknown").lower()
    followers = row.get("followers")
    try:
        followers_n = int(followers) if followers is not None else None
    except (TypeError, ValueError):
        followers_n = None

    spam_hits = _hits(_SPAM_RE, text)
    opp_hits = _hits(_OPP_RE, text)
    weak_hits = _hits(_WEAK_RE, text)
    reasons: list[str] = []

    if _HANDLE_SPAM.search(handle) or _HANDLE_SPAM.search(str(row.get("display_name") or "")):
        spam_hits.append("handle_or_name_nsfw")
        reasons.append("handle_or_display_name_nsfw_token")

    score_spam = len(spam_hits) * 3 + len(weak_hits)
    score_opp = len(opp_hits) * 3

    if bucket in ("hidden", "low_quality") and spam_hits:
        score_spam += 2
        reasons.append("hidden_bucket_plus_spam_signal")
    if bucket in ("hidden", "low_quality") and not opp_hits and weak_hits:
        score_spam += 1
        reasons.append("hidden_plus_empty_opener")

    if followers_n is not None and followers_n < 15 and spam_hits:
        score_spam += 1
        reasons.append("very_new_account_plus_spam_signal")
    if followers_n is not None and followers_n < 15 and weak_hits and not opp_hits:
        score_spam += 1
        reasons.append("very_new_account_plus_weak_opener")

    if _GENERIC_HANDLE.match(handle) and (spam_hits or weak_hits) and not opp_hits:
        score_spam += 1
        reasons.append("generic_bot_handle")

    # Decision: fail closed to unsure unless one side clearly wins.
    label = "unsure"
    if score_spam >= 3 and score_spam > score_opp + 1:
        label = "spam_sex_bot"
        reasons.append(f"spam_score={score_spam}>opp={score_opp}")
    elif score_opp >= 3 and score_opp > score_spam + 1:
        label = "real_opportunity"
        reasons.append(f"opp_score={score_opp}>spam={score_spam}")
    else:
        if spam_hits and opp_hits:
            reasons.append("mixed_signals_fail_closed")
        elif not spam_hits and not opp_hits:
            reasons.append("no_strong_signal")
        else:
            reasons.append("scores_too_close_fail_closed")

    return {
        "label": label,
        "reasons": reasons,
        "spam_hits": spam_hits[:12],
        "opp_hits": opp_hits[:12],
        "weak_hits": weak_hits[:8],
        "score_spam": score_spam,
        "score_opp": score_opp,
    }


def classify_label(row: dict[str, Any]) -> str:
    return classify(row)["label"]
