#!/usr/bin/env python3
"""x_dm_triage.py — hailports (and lane) X message-request triage.

    python3 agents/x_dm_triage.py --lane x_hailports            # dry-run (default)
    python3 agents/x_dm_triage.py --lane x_hailports --apply    # tips + ledger; NEVER accept/block/send
    python3 agents/x_dm_triage.py --lane x_hailports --classify-only   # no CDP; stdin/fixture JSONL

v1 policy (operator widget t350s1735):
  * spam_sex_bot  → leave in Hidden / do not accept; log only; NEVER block
  * real_opportunity → tip operator + ledger line; never auto-accept / never auto-reply
  * unsure → tip as needs_review + ledger; never auto-accept
  * PIN page or unlock failure → soft fail (exit 0); successful harvest clears DM_PIN_NEEDED_<lane>
  * Never send messages. Never block. Never Accept in v1.

Rules: agents/x_dm_triage_RULES.md
Classifier: core/dm_triage_classify.py (unit-tested)
Ledger: /home/operator/socials/dm_inbox_<lane>.jsonl
Tips: /home/operator/socials/tips/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STACK = Path("/home/operator/claude-stack")
sys.path.insert(0, str(STACK))

SOC = Path("/home/operator/socials")
TIPS = SOC / "tips"
KILL = SOC / "ENGAGE_OFF"
TRIAGE_OFF = SOC / "DM_TRIAGE_OFF"

from agents.linkedin_publisher import Abort, Tab, utc  # noqa: E402
from agents import x_publisher as XP  # noqa: E402
from core.dm_triage_classify import classify  # noqa: E402

EXIT_OK, EXIT_GATED, EXIT_CDP, EXIT_PIN, EXIT_UI = 0, 2, 3, 0, 6  # PIN soft-fails as 0

# Message-request list scrapers. Classic /messages/requests + XChat /i/chat.
# Best-effort selectors; empty list is fine (caller logs).
_LIST_JS = r"""
(() => {
  const bucketGuess = () => {
    const u = (location.href || '').toLowerCase();
    const t = (document.body && document.body.innerText || '').slice(0, 4000).toLowerCase();
    if (u.includes('hidden') || /\bhidden requests?\b/.test(t) && document.querySelector('[aria-selected="true"]')) {
      // prefer explicit selected tab text
    }
    const sel = document.querySelector('[role="tab"][aria-selected="true"], [data-testid*="ScrollSnap"] [aria-selected="true"]');
    const selT = (sel && (sel.innerText || sel.textContent) || '').toLowerCase();
    if (/hidden|filtered|low.?quality|spam/.test(selT) || /[?&]f=hidden|\/hidden/.test(u)) return 'hidden';
    if (/priority|primary|you may know|verified/.test(selT)) return 'priority';
    if (/messages\/requests/.test(u)) return 'you_may_know';
    return 'unknown';
  };
  const bucket = bucketGuess();
  const out = [];
  const seen = new Set();
  const push = (row) => {
    const h = (row.handle || '').replace(/^@/, '').toLowerCase();
    if (!h || seen.has(h)) return;
    seen.add(h);
    out.push(row);
  };
  // Conversation rows (classic DM inbox + request inbox)
  document.querySelectorAll('[data-testid="conversation"], a[href*="/messages/"], a[href*="/i/chat/"]').forEach(el => {
    const a = el.closest('a') || el.querySelector('a[href]') || el;
    const href = (a && a.href) || '';
    if (!/\/messages\/|\/i\/chat\//.test(href)) return;
    if (/\/messages\/compose|\/messages\/settings/.test(href)) return;
    const text = (el.innerText || '').trim();
    if (!text) return;
    const lines = text.split(/\n+/).map(s => s.trim()).filter(Boolean);
    let handle = '';
    const hm = href.match(/\/(?:messages|i\/chat)\/([^/?#]+)/);
    // Prefer @handle from visible text
    for (const ln of lines) {
      const m = ln.match(/^@([A-Za-z0-9_]{1,30})$/);
      if (m) { handle = m[1]; break; }
    }
    if (!handle) {
      for (const ln of lines) {
        const m = ln.match(/@([A-Za-z0-9_]{1,30})/);
        if (m) { handle = m[1]; break; }
      }
    }
    if (!handle && hm) {
      // conversation ids are often numeric; skip pure ids as handles
      if (!/^\d+$/.test(hm[1])) handle = hm[1];
    }
    if (!handle) return;
    const display = lines[0] && !lines[0].startsWith('@') ? lines[0].slice(0, 80) : '';
    const preview = lines.slice(1).join(' ').slice(0, 280) || lines.join(' ').slice(0, 280);
    push({
      handle: handle.replace(/^@/, ''),
      display_name: display,
      preview: preview,
      text: preview,
      href: href.split('?')[0],
      bucket: bucket,
      source: 'dom_list',
    });
  });
  // Fallback: UserCell-style request cards
  document.querySelectorAll('[data-testid="UserCell"]').forEach(el => {
    const a = el.querySelector('a[href^="/"]');
    if (!a) return;
    const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9_]{1,30})(?:\?|$)/);
    if (!m) return;
    const text = (el.innerText || '').trim().slice(0, 400);
    push({
      handle: m[1],
      display_name: (text.split(/\n/)[0] || '').slice(0, 80),
      preview: text,
      text: text,
      href: 'https://x.com/' + m[1],
      bucket: bucket,
      source: 'user_cell',
    });
  });
  return { bucket, count: out.length, rows: out.slice(0, 80), href: location.href };
})()
"""

_PIN_JS = r"""(() => {
  const u = (location.href || '').toLowerCase();
  if (u.includes('/i/chat/pin') || u.includes('/i/chat/setup')) return true;
  const t = (document.body && document.body.innerText || '').slice(0, 3000).toLowerCase();
  return /enter your (chat )?pin|create a (chat )?pin|unlock (your )?chat|chat pin/.test(t);
})()"""

_CLICK_HIDDEN_JS = r"""(() => {
  const nodes = Array.from(document.querySelectorAll('a, button, [role="tab"], span'));
  for (const n of nodes) {
    const t = (n.innerText || n.textContent || '').trim().toLowerCase();
    if (!t || t.length > 40) continue;
    if (/^hidden(\s+requests?)?$/.test(t) || t === 'filtered' || t === 'spam') {
      n.click();
      return t;
    }
  }
  return null;
})()"""


def log(msg: str) -> None:
    print(f"[x_dm_triage {utc()}] {msg}", file=sys.stderr, flush=True)


def pin_flag(lane: str) -> Path:
    return SOC / f"DM_PIN_NEEDED_{lane}"


def ledger_path(lane: str) -> Path:
    return SOC / f"dm_inbox_{lane}.jsonl"


def seen_handles(lane: str) -> set[str]:
    p = ledger_path(lane)
    out: set[str] = set()
    if not p.is_file():
        return out
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        h = str(r.get("handle") or "").lower()
        if h:
            out.add(h)
    return out


def ledger_append(lane: str, row: dict) -> None:
    p = ledger_path(lane)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def tip_dm(lane: str, kind: str, row: dict, verdict: dict) -> Path | None:
    """Write a tip for operator. No employer/real-name fields; handle + preview only."""
    TIPS.mkdir(parents=True, exist_ok=True)
    handle = str(row.get("handle") or "unknown")
    # Dedupe: one tip per handle+kind per calendar day (CT-ish via local box clock)
    day = datetime.now().strftime("%Y%m%d")
    if any(TIPS.glob(f"{day}-*_{lane}_dm_{kind}_{handle}.md")):
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = TIPS / f"{ts}_{lane}_dm_{kind}_{handle}.md"
    preview = (row.get("preview") or row.get("text") or "")[:400]
    reasons = ", ".join(verdict.get("reasons") or []) or "(none)"
    body = (
        f"# Tip for operator: {lane} DM `{kind}` — no auto-accept/reply/block\n\n"
        f"- From: @{handle}\n"
        f"- Bucket: {row.get('bucket', 'unknown')}\n"
        f"- Href: {row.get('href', '')}\n"
        f"- Preview: {preview}\n"
        f"- Classifier: {verdict.get('label')} ({reasons})\n"
        f"- Spam hits: {verdict.get('spam_hits')}\n"
        f"- Opportunity hits: {verdict.get('opp_hits')}\n\n"
        f"Leave Hidden spam alone. For real/unsure: open Messages in hailports Chrome "
        f"(CDP :18811), accept only if you want, draft any reply yourself.\n"
    )
    p.write_text(body)
    return p


def soft_pin(lane: str, why: str) -> dict:
    flag = pin_flag(lane)
    if not flag.exists():
        flag.write_text(utc())
    log(f"PIN soft-fail lane={lane} why={why} flag={flag}")
    return {
        "ok": True,
        "skipped": "xchat_pin_required",
        "lane": lane,
        "why": why,
        "flag": str(flag),
        "hint": "Enter Chat PIN in hailports Chrome (DISPLAY=:42 / CDP :18811), then re-run; a successful harvest clears the flag.",
    }


def collect_requests(tab: Tab) -> tuple[list[dict], dict]:
    """Navigate request inboxes; return (rows, meta). Detects PIN page."""
    meta: dict = {"pages": []}
    rows: list[dict] = []

    def harvest(label: str) -> None:
        if tab.js(_PIN_JS):
            raise RuntimeError("pin_page")
        data = tab.js(_LIST_JS) or {}
        meta["pages"].append({"label": label, "href": data.get("href"), "count": data.get("count")})
        for r in data.get("rows") or []:
            r = dict(r)
            if label == "hidden":
                r["bucket"] = "hidden"
            rows.append(r)

    # Classic requests
    tab.goto("https://x.com/messages/requests", settle=14)
    time.sleep(2)
    if tab.js(_PIN_JS):
        raise RuntimeError("pin_page")
    harvest("requests")

    # Try Hidden tab (do not Accept anything)
    clicked = tab.js(_CLICK_HIDDEN_JS)
    if clicked:
        time.sleep(3)
        harvest("hidden")
    else:
        # Some builds use a query or path
        tab.goto("https://x.com/messages/requests?f=hidden", settle=10)
        time.sleep(1)
        if not tab.js(_PIN_JS):
            harvest("hidden_url")

    # XChat fallback surface
    tab.goto("https://x.com/i/chat", settle=12)
    time.sleep(2)
    if tab.js(_PIN_JS):
        raise RuntimeError("pin_page")
    harvest("xchat")

    # Dedupe by handle
    by: dict[str, dict] = {}
    for r in rows:
        h = str(r.get("handle") or "").lower()
        if not h:
            continue
        prev = by.get(h)
        if not prev or (r.get("bucket") == "hidden" and prev.get("bucket") != "hidden"):
            by[h] = r
        elif prev and not prev.get("preview") and r.get("preview"):
            by[h] = r
    return list(by.values()), meta


def process_rows(lane: str, rows: list[dict], *, apply: bool, already: set[str] | None = None) -> dict:
    already = already if already is not None else seen_handles(lane)
    summary = {
        "lane": lane,
        "dry_run": not apply,
        "total": len(rows),
        "spam_sex_bot": 0,
        "real_opportunity": 0,
        "unsure": 0,
        "skipped_seen": 0,
        "actions": [],
    }
    for row in rows:
        handle = str(row.get("handle") or "").lstrip("@")
        if not handle:
            continue
        key = handle.lower()
        verdict = classify(row)
        label = verdict["label"]
        action = {
            "handle": handle,
            "bucket": row.get("bucket"),
            "label": label,
            "reasons": verdict.get("reasons"),
            "preview": (row.get("preview") or "")[:160],
        }
        if key in already and label != "real_opportunity":
            # Still re-tip fresh real_opportunity; otherwise skip duplicates
            action["would"] = "skip_seen"
            summary["skipped_seen"] += 1
            summary["actions"].append(action)
            continue

        summary[label] = summary.get(label, 0) + 1

        if label == "spam_sex_bot":
            action["would"] = "leave_hidden_log_only"
            action["did"] = None
            if apply:
                ledger_append(lane, {
                    "ts": utc(),
                    "lane": lane,
                    "handle": handle,
                    "bucket": row.get("bucket"),
                    "label": label,
                    "action": "leave_hidden",
                    "blocked": False,
                    "accepted": False,
                    "replied": False,
                    "preview": (row.get("preview") or "")[:280],
                    "reasons": verdict.get("reasons"),
                    "spam_hits": verdict.get("spam_hits"),
                    "dry_run": False,
                })
                action["did"] = "ledger"
                already.add(key)
        elif label == "real_opportunity":
            action["would"] = "tip_and_ledger_no_accept"
            if apply:
                tip_p = tip_dm(lane, "opportunity", row, verdict)
                ledger_append(lane, {
                    "ts": utc(),
                    "lane": lane,
                    "handle": handle,
                    "bucket": row.get("bucket"),
                    "label": label,
                    "action": "tip_opportunity",
                    "blocked": False,
                    "accepted": False,
                    "replied": False,
                    "preview": (row.get("preview") or "")[:280],
                    "reasons": verdict.get("reasons"),
                    "opp_hits": verdict.get("opp_hits"),
                    "tip": str(tip_p) if tip_p else None,
                    "dry_run": False,
                })
                action["did"] = "tip+ledger"
                action["tip"] = str(tip_p) if tip_p else None
                already.add(key)
        else:
            action["would"] = "tip_needs_review_no_accept"
            if apply:
                tip_p = tip_dm(lane, "needs_review", row, verdict)
                ledger_append(lane, {
                    "ts": utc(),
                    "lane": lane,
                    "handle": handle,
                    "bucket": row.get("bucket"),
                    "label": "unsure",
                    "action": "tip_needs_review",
                    "blocked": False,
                    "accepted": False,
                    "replied": False,
                    "preview": (row.get("preview") or "")[:280],
                    "reasons": verdict.get("reasons"),
                    "tip": str(tip_p) if tip_p else None,
                    "dry_run": False,
                })
                action["did"] = "tip+ledger"
                action["tip"] = str(tip_p) if tip_p else None
                already.add(key)

        summary["actions"].append(action)
        log(f"{label} @{handle} bucket={row.get('bucket')} would={action['would']}")

    return summary


def run_browser(lane: str, *, apply: bool) -> dict:
    if KILL.exists():
        return {"ok": True, "skipped": "ENGAGE_OFF", "lane": lane}
    if TRIAGE_OFF.exists():
        return {"ok": True, "skipped": "DM_TRIAGE_OFF", "lane": lane}
    # If DM_PIN_NEEDED_* exists we still probe CDP once: after operator enters the
    # Chat PIN in hailports Chrome, a successful requests harvest clears the flag.
    # A PIN page keeps/refreshes the flag and soft-fails (no accept/block/send).

    cfg = XP.lane_cfg(lane)
    expect = (cfg.get("expect") or "").lstrip("@")
    try:
        with Tab(cfg["cdp_url"]) as tab:
            # Identity probe via home (same family as engage)
            tab.goto("https://x.com/home", settle=12)
            if tab.js(_PIN_JS):
                return soft_pin(lane, "pin_on_home")
            ident = tab.js(r"""(() => {
              const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
              if (!a) return null;
              const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9_]+)/);
              return m ? m[1] : null;
            })()""")
            if expect and ident and ident.lower() != expect.lower():
                return {"ok": False, "error": "identity_mismatch", "ident": ident, "expect": expect}

            try:
                rows, meta = collect_requests(tab)
            except RuntimeError as exc:
                if "pin" in str(exc).lower():
                    return soft_pin(lane, "pin_during_collect")
                raise

            # Successfully listed without PIN → clear stale flag if any
            flag = pin_flag(lane)
            if flag.exists():
                flag.unlink()
                log(f"cleared stale PIN flag {flag}")

            summary = process_rows(lane, rows, apply=apply)
            summary["ok"] = True
            summary["meta"] = meta
            summary["identity"] = ident
            summary["cdp"] = cfg["cdp_url"]
            summary["policy"] = {
                "accept": False,
                "block": False,
                "reply": False,
                "spam": "leave_hidden",
            }
            return summary
    except Abort as exc:
        payload = {"ok": False, "error": "abort", "detail": str(exc)}
        if getattr(exc, "code", None) == EXIT_CDP or "cdp" in str(exc).lower():
            payload["exit_hint"] = EXIT_CDP
        return payload


def run_classify_only(lane: str, path: Path | None, apply: bool) -> dict:
    rows: list[dict] = []
    if path:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    else:
        raw = sys.stdin.read()
        if raw.strip().startswith("["):
            rows = json.loads(raw)
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    summary = process_rows(lane, rows, apply=apply)
    summary["ok"] = True
    summary["mode"] = "classify_only"
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="X DM request triage (hailports). Never accept/block/send in v1.")
    ap.add_argument("--lane", default="x_hailports", choices=["x_hailports", "x_darkgary", "x_ima"])
    ap.add_argument("--apply", action="store_true", help="Write tips+ledger. Still never accept/block/send.")
    ap.add_argument("--dry-run", action="store_true", help="Default. Print actions only.")
    ap.add_argument("--classify-only", action="store_true", help="No CDP; read JSONL rows from --fixture or stdin.")
    ap.add_argument("--fixture", type=Path, help="JSONL fixture for --classify-only")
    ap.add_argument("--json", action="store_true", help="Print summary JSON on stdout")
    args = ap.parse_args()
    apply = bool(args.apply) and not args.dry_run

    if args.classify_only:
        summary = run_classify_only(args.lane, args.fixture, apply=apply)
    else:
        summary = run_browser(args.lane, apply=apply)

    if args.json or True:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") or summary.get("skipped") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
