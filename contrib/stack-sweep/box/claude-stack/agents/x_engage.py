#!/usr/bin/env python3
"""x_engage.py — small, randomized, fail-closed engagement ticks for X lanes.

    python3 agents/x_engage.py --lane x_ima [--dry-run] [--tick] [--loop] [--check-identity]

One tick = ONE small randomized action batch (1-3 actions, random 20-120s gaps),
then exit. --loop re-ticks with random 4-15 min sleeps, only inside the window
(07:00-22:30 CT). Nothing is posted unless ALL hold:

  * kill switch /home/operator/socials/ENGAGE_OFF absent
  * lane not on a cooldown file (/home/operator/socials/engage_cooldown_<lane>;
    challenge cooldowns stay until a human removes the file)
  * CDP reachable, x.com logged in, identity == lanes.<lane>.expect
  * per-action daily cap (from engage_config.json caps) not yet used today
  * every text output passes content_quality + tone_gate + the lane safety gate
    (fiend_safety for x_ima, identity split for x_hailports, darkgary topic+judge
    gates for x_darkgary) and quote_coherent(require_overlap) for replies

--dry-run navigates and finds candidates, generates + gates reply text, and logs
what it WOULD do with dry_run=true. It performs ZERO state-changing clicks.
--check-identity is the read-only identity probe used by ensure-socials-engage.sh.

Ledger: /home/operator/socials/engage_ledger.jsonl (ts, lane, action, target, url, text, dry_run).
Stop conditions (rate-limit text, "something went wrong", challenge/locked/verify,
any warning toast) write a tip JSON + set a 6h cooldown and exit nonzero.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STACK = Path("/home/operator/claude-stack")
sys.path.insert(0, str(STACK))
os.environ.setdefault("SOCIALS_FREE_POOL_ONLY", "1")

CT = ZoneInfo("America/Chicago")
CONFIG = Path("/home/operator/socials/engage_config.json")
LEDGER = Path("/home/operator/socials/engage_ledger.jsonl")
KILL = Path("/home/operator/socials/ENGAGE_OFF")
TIPS_DIR = Path("/home/operator/socials/tips")
COOLDOWN_DIR = Path("/home/operator/socials")
FOLLOW_TTL_DAYS = 7

from agents.linkedin_publisher import Abort, Tab, utc  # noqa: E402
from agents import x_publisher as XP  # noqa: E402
from core import engage_common as EC  # noqa: E402

EXIT_OK, EXIT_GATED, EXIT_CDP, EXIT_LOGIN, EXIT_IDENT, EXIT_UI, EXIT_STOP = 0, 2, 3, 4, 5, 6, 9


def log(msg: str) -> None:
    print(f"[x_engage {utc()}] {msg}", file=sys.stderr, flush=True)


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


# ── ledger + caps (shared ledger in core.engage_common) ────────────────────────

def today_rows(lane: str) -> list[dict]:
    # ts is UTC ("...Z"); compare on the CT calendar day, not the UTC date prefix
    # (the prefix counted last night's 7pm-midnight CT actions against today's caps).
    today = datetime.now(CT).date()
    out = []
    for r in EC.ledger_rows():
        if r.get("lane") != lane:
            continue
        ts = _ts_of(r.get("ts"))
        if ts and ts.date() == today:
            out.append(r)
    return out


def cap_used(lane: str, action: str, cfg: dict) -> int:
    # Only REAL, confirmed actions count toward a cap. A failed attempt (done:false,
    # button_missing, skipped) must not consume the cap -- that was silently starving
    # the lane: 25 phantom follow failures ate the whole follow budget with 0 follows.
    return sum(1 for r in today_rows(lane)
               if r.get("action") == action and r.get("done") is True and not r.get("dry_run"))


def under_cap(lane: str, action: str, cfg: dict) -> bool:
    caps = dict(cfg.get("caps") or {})
    _KEY = {"like": "likes", "repost": "reposts", "reply": "replies",
            "follow": "follows", "unfollow": "unfollows", "dm": "dms"}
    key = _KEY.get(action, action + "s")
    # Per-lane override, e.g. hailports pushes likes/replies/follows to the safe upper end.
    lane_caps = (cfg.get("lane_caps") or {}).get(lane) or {}
    cap = int(lane_caps.get(key, caps.get(key, caps.get(action, 0))))
    return cap > 0 and cap_used(lane, action, cfg) < cap


# ── candidate collection (search page) ──────────────────────────────────────────

COLLECT_JS = r"""
(() => {
  const me = '';
  const out = [];
  document.querySelectorAll('article[data-testid="tweet"]').forEach(a => {
    const link = a.querySelector('a[href*="/status/"]');
    const txt = a.querySelector('[data-testid="tweetText"]');
    const uname = a.querySelector('[data-testid="User-Name"]');
    if (!link || !txt) return;
    const href = link.href;
    const m = href.match(/\/([^/]+)\/status\/(\d+)/);
    if (!m) return;
    const verified = !!(uname && uname.querySelector('[data-testid="icon-verified"], svg[aria-label*="Verified"]'));
    out.push({
      user: m[1], id: m[2], url: href.split('/photo')[0],
      text: (txt.innerText || '').slice(0, 300),
      name: (uname ? uname.innerText : '').slice(0, 120),
      verified: verified,
    });
  });
  return out;
})()
"""


def collect_candidates(tab: Tab, query: str) -> list[dict]:
    from urllib.parse import quote
    q_ver = query if "filter:" in query else f"{query} filter:verified"
    tab.goto(f"https://x.com/search?q={quote(q_ver)}&f=live", settle=8)
    rows = list(tab.js(COLLECT_JS) or [])
    try:  # load a second screen of results so one search feeds a whole batch
        tab.js("window.scrollBy(0, 2500)"); time.sleep(2.5)
        rows += list(tab.js(COLLECT_JS) or [])
        tab.js("window.scrollTo(0, 0)"); time.sleep(1)
    except Exception:
        pass
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


# ── text generation + gates ─────────────────────────────────────────────────────

def lane_voice(lane: str) -> tuple[str, str]:
    """(system/brief, instructions) per lane persona."""
    from core.content_generator import PERSONA_BRIEF
    if lane == "x_ima":
        return (PERSONA_BRIEF["ima"].split("STYLE REFERENCES")[0].strip(),  # replies: short persona, no style guide
                "Reply to this post in ONE lowercase joke under 220 chars, same savage "
                "satire voice. Riff on what the post is actually about, in her own voice; do NOT drag "
                "office life (standups, okrs, managers, jira, slack, meetings) into a post that isn't about work. "
                "Naturally reuse 1-2 exact words/nouns from the post so your joke connects directly to their wording. "
                "No advice, no dosing, no sourcing, no mentions, no links, no hashtags.")
    if lane == "x_hailports":
        return ("You are an engineer writing build-in-public notes about tuning a "
                "local model and an automation stack. Concrete, calm. Never name a "
                "person, employer, client, CRM, or job title. No hashtags, no links, "
                "no selling. Never use: ensure, streamline, leverage, seamless, "
                "stakeholders, moving forward, submit, portal.",
                "Reply in ONE short note under 220 chars that engages the post's specific "
                "point with a NON-OBVIOUS insight or a counterpoint (never plain agreement or praise). "
                "Write in casual lowercase like a sharp engineer typing fast between meetings (short uneven sentences, "
                "a fragment or dry aside; never formal essay register). Naturally reuse 1-2 exact words/nouns from the post. "
                "Never mention your own build, tools or setup; never use a number that is not in the post. Vary your openings; "
                "do not start with 'Same' or 'Agreed'. No mentions.")
    if lane == "x_darkgary":
        return (PERSONA_BRIEF["darkgary"],
                "Reply to this sports post in ONE dry, deadpan lowercase funny line under 200 chars. "
                "Turn their exact words against a real football/sports truth or wound (NO cartoon metaphors like "
                "'restraining order', 'piñata', 'witness protection', or 'next-of-kin paperwork'). "
                "ANCHORS THAT SCORE 9 (copy the dry deadpan cadence, never the words):\n"
                "- 'they spent all week calling it a statement win. the statement was they still cannot tackle'\n"
                "- 'tough all day. just not in the fourth quarter. otherwise airtight'\n"
                "- 'we have not won anything since before your grandfather was drafted and i will still be louder than you in october'\n"
                "If about Aggies or Houston teams (Rockets/Astros/Texans), warm self-roast our own team's ability to make "
                "anything stressful (never rude to our fans). If about a rival/other team, bait them by turning their own "
                "phrase in THEIR POST into the punchline on the last word. Reuse 1-2 exact words from THEIR POST. Only use "
                "numbers/names from THEIR POST. No mentions, no hashtags.")
    raise Abort(EXIT_GATED, "unknown_lane", lane=lane)


_SKEPTIC_FACT_BAN = re.compile(r"\b(llm|judge|scor\w*|caption|writer|models?|gpt|claude|codex|glm|ai|browser|chrome|cdp|scraper|devtools|"
                               r"bots?|automat\w*|post(s|ing)?)\b", re.I)


# 2026-10-22 11:05 CT operator: curated REAL build facts for hailports skeptic answers. Every line is a paraphrase of
# the source file named next to it (numbers copied, never invented). Same ban filter applies on top.
SKEPTIC_CURATED = [
    ("on a 24GB Mac mini, 70B/103B-class weights aren't safe to run as the default",
     "core/local_model_registry.py"),
    ("on 2026-09-19 the 4.7GB 7b got deleted and a 1.5B became the resident always-on one",
     "core/local_model_registry.py"),
    ("the deleted 7b stays deleted: a gate refuses to pull it back down",
     "core/local_model_registry.py"),
    ("the local coding fallback is a 7B coder, picked because it's the fastest and lightest on RAM",
     "core/local_model_registry.py"),
    ("a background proxy got switched off for good on 2026-09-23 because it was thrashing RAM",
     "bin/personal-codex-kick.py"),
    ("memory thrash froze the server twice on 10/18 (15:03-15:30 and 20:08-20:42), so the supervisor now frees "
     "memory when available RAM runs low", "bin/socials-supervisor.sh"),
    ("after a hang on 10/18, every scheduled command runs under a hard timeout", "bin/socials-supervisor.sh"),
    ("there's no cron or systemd on that server, so the supervisor schedules itself in 20s wall-clock slices",
     "bin/socials-supervisor.sh"),
    ("the supervisor and a guard watch each other and restart the other if its heartbeat goes quiet for 10 min",
     "bin/socials-supervisor.sh"),
    ("pgrep under timeout was matching the timeout wrapper itself, so dead jobs looked alive until 10/19",
     "bin/socials-supervisor.sh"),
    ("one slow API read held a whole run for 350 s because the socket timeout resets on every recv; a daemon "
     "thread fixed it", "core/free_judge.py"),
    ("a long job only checked in between runs, so anything past 25 min got killed as stale; now it checks in "
     "every minute, capped at 45 min", "agents/x_engage.py"),
    ("since 10/18 the overnight supervisor has caught and restarted a stale job 238 times",
     "data/runtime/socials-supervisor.log"),
]


def skeptic_curated() -> list[str]:
    return [f for f, _src in SKEPTIC_CURATED if not _SKEPTIC_FACT_BAN.search(f)]


def skeptic_facts() -> list[str]:
    """2026-10-22: real build facts safe for a public skeptic answer (no talk of who writes the posts).
    Curated Mac mini / server-ops facts first; the counted build-log facts (e.g. the number check) rank last."""
    out = skeptic_curated()
    try:
        from core.buildlog_facts import collect as _facts
        out += [f for f in (_facts(limit=30) or []) if f and not _SKEPTIC_FACT_BAN.search(f) and f not in out]
    except Exception:
        pass
    return out


def pick_skeptic_fact(rnd=None) -> str:
    """90%: a curated fact. The build-log number-check fact is a rare fallback, never the default."""
    import random as _r
    rnd = rnd or _r
    cur, allf = skeptic_curated(), skeptic_facts()
    if cur and (rnd.random() < 0.9 or len(allf) == len(cur)):
        return rnd.choice(cur)
    return rnd.choice(allf) if allf else ""


def generate_reply(lane: str, post_text: str, context: str = "", skeptic: bool = False, feedback: str = "") -> str:
    from core.caption_brain import brain_complete_sync
    system, instr = lane_voice(lane)
    if skeptic and lane == "x_hailports":  # 2026-10-22 operator: calm answer to a doubter on OUR post, one real detail
        fact = pick_skeptic_fact()
        if not fact:
            generate_reply.last_facts = []
            return ""
        generate_reply.last_facts = [fact]
        prompt = (
            (f"FULL THREAD (root first):\n{context[:1800]}\n\n" if context else "")
            + f"THEIR REPLY (a doubt about our post):\n\"{post_text[:280]}\"\n\n"
            "Answer them in ONE short, calm, non-defensive note under 200 chars. Grant the fair part of the doubt in a "
            "few words (reuse one of their own words naturally), then give exactly ONE concrete detail, paraphrased from this REAL fact and nothing else:\n- "
            + fact + "\n\nNo other claims, no numbers unless they are in that fact, no selling, no hype, no 'trust me'. "
            "Do not say who or what writes these posts, and do not use the words 'draft', 'automation' or 'bot'. Never mention 'my stack', 'my setup', 'my pipeline' or any "
            "judge/score. No mentions, links or hashtags. Do not open with 'agreed', 'same', 'exactly' or 'great question'. "
            "Write casually, like a person typing. Output ONLY the reply text."
        )
        raw, _provider = brain_complete_sync(prompt, system=system, max_tokens=120, lane=lane)
        return " ".join((raw or "").split()).strip().strip('"')[:220]
    prompt = (
        f"{instr}\n\n" + (f"FULL THREAD (root first):\n{context[:1800]}\n\nAnswer what that person actually said, "
                          "in context, like a human in the conversation.\n\n" if context else "")
        + f"THE POST:\n\"{post_text[:280]}\"\n\n"
        "Never talk about your own tools, automation, judging, scoring, pipeline, stack, setup or model. "
        "Write casually, like a person typing. Output ONLY the reply text, nothing else."
    )
    if lane == "x_hailports":
        try:
            from core.buildlog_facts import collect as _facts
            facts = _facts(limit=4)
        except Exception:
            facts = []
        generate_reply.last_facts = facts
        import random as _rnd
        if False and facts:  # 2026-10-17: replies never reference the build (machinery ban + token trim)  # most replies stand on the idea alone, no build-log reference
            prompt += (
                "\n\nOPTIONAL REAL FACT from the build log (use only if it truly fits; never invent a number):\n- "
                + _rnd.choice(facts)
            )
        prompt += ("\n\nRULES: never open with agreement ('agreed', 'same', 'this', 'exactly', '100%', 'so true'). "
                   "Do NOT mention 'my stack', 'in my stack' or 'my setup'. Lead with the counterpoint or the "
                   "non-obvious angle itself.")
    if feedback and feedback != "no_draft":
        prompt += (f"\n\nPREVIOUS DRAFT WAS REJECTED ({feedback}). Fix that exact issue: reuse 1-2 exact words from "
                   "THE POST, write in casual lowercase like a real person typing fast, and make the point/joke specific to THE POST.")
    raw, _provider = brain_complete_sync(
        prompt, system=system, max_tokens=120, lane=lane,
    )
    text = " ".join((raw or "").split()).strip().strip('"')
    for lead in ("reply:", "reply text:", "@"):
        if text.lower().startswith(lead) and lead != "@":
            text = text[len(lead):].strip()
    return text[:220]


def _lane_reply_cfg(lane: str | None) -> dict:
    if not lane:
        return {}
    try:
        return ((load_config().get("lane_reply") or {}).get(lane) or {})
    except Exception:
        return {}


def _reply_floor(lane: str | None = None) -> int:
    """Global viral.reply_floor unless this lane sets lane_reply.<lane>.reply_floor."""
    try:
        lane_cfg = _lane_reply_cfg(lane)
        if lane_cfg.get("reply_floor") is not None:
            return int(lane_cfg["reply_floor"])
        return int((load_config().get("viral") or {}).get("reply_floor", 8))
    except Exception:
        return 8


def _human_score_min(lane: str | None) -> int:
    """Hard-coded 8 unless lane_reply.<lane>.human_score_min is set."""
    try:
        lane_cfg = _lane_reply_cfg(lane)
        if lane_cfg.get("human_score_min") is not None:
            return int(lane_cfg["human_score_min"])
    except Exception:
        return 8
    return 8


def _viral_reply_floor(lane: str | None) -> int | None:
    """None keeps viral_reason's default reply floor. A set lane value overrides it."""
    try:
        lane_cfg = _lane_reply_cfg(lane)
        if lane_cfg.get("viral_reply_floor") is not None:
            return int(lane_cfg["viral_reply_floor"])
    except Exception:
        return None
    return None


def reply_ok(lane: str, reply: str, post_text: str) -> tuple[bool, str]:
    """Every gate, fail-closed: quality, tone, coherence, lane safety."""
    from core.content_quality import gate, tone_gate, quote_coherent
    if not reply or len(reply) > 220:
        return False, "length"
    if "@" in reply or "http" in reply.lower() or "#" in reply:
        return False, "mention/link/hashtag"
    from core.reply_guard import machinery_hit
    _mh_txt = re.sub(r"\bscor(?:e|ed|es|ing)\b", "", reply, flags=re.I) if lane == "x_darkgary" else reply
    if (mh := machinery_hit(_mh_txt)):
        return False, f"machinery:{mh}"
    v = gate(reply, channel="x", persona=("ima" if lane == "x_ima" else
                                          "darkgary" if lane == "x_darkgary" else "generic"))
    if not v.passed:
        return False, "gate:" + "; ".join(v.reasons[:2])
    ok, why = tone_gate(reply, is_reply=True)
    if not ok:
        return False, "tone:" + "; ".join(why[:2])
    ok, why = quote_coherent(reply, post_text, require_overlap=True)
    if not ok:
        return False, f"coherent:{why}"
    if lane == "x_ima":
        from core.ima_topic_guard import forced_office  # 19:47 operator: never force office words onto a non-work post
        if (fo := forced_office(reply, post_text)):
            return False, f"forced_office:{fo}"
        from core import fiend_safety
        ok, reasons = fiend_safety.check(reply)
        if not ok:
            return False, "fiend:" + "; ".join(r.split(":")[0] for r in reasons[:2])
        from core.fiend_safety import passes_comedy_floor
        jok, jwhy = passes_comedy_floor(reply, floor=_reply_floor(lane), context=post_text[:400])
        if not jok:
            return False, jwhy
        return True, "ok"
    if lane == "x_hailports":
        # same identity split as the supply script's LEAK regex
        hit = re.search(r"\b(operator|operator|operator|operator|operator|operator|operator|"
                        r"agtech|crm|clients?|employer)\b", reply, re.I)
        if hit:
            return False, f"leak:{hit.group(0)}"
        if re.match(r"\s*(agreed|same\b|same lesson|this\b|exactly|100%|so true|totally|\+1|yes[,.!])", reply, re.I) \
                or re.search(r"\b(in |on )?my (stack|setup|pipeline)\b|same lesson", reply, re.I):
            return False, "hp_banned_phrase"
        from core.hailports_judge import judge_reply
        facts = getattr(generate_reply, "last_facts", None) or []
        score, why = judge_reply(reply, post_text, facts)
        if score < _reply_floor(lane):
            return False, f"hailports_judge:{score}:{why}"
        return True, "ok"
    if lane == "x_darkgary":
        from core.darkgary_topic_guard import fact_blocked, topic_blocked
        if topic_blocked(reply, post_text):
            return False, "topic_guard"
        if fact_blocked(reply, post_text or ""):
            return False, "fact_blocked"
        try:
            from agents.darkgary_banter import judge
            score, why = judge(reply, post_text)
            if score < _reply_floor(lane):  # replies: global floor unless lane_reply overrides
                return False, f"darkgary_judge:{score}:{why}"
        except Exception:
            return False, "judge-unreachable"  # fail closed
    return True, "ok"


# ── actions (every click guarded by dry_run at the caller) ──────────────────────

def find_article_js(status_id: str) -> str:
    return f"""(() => {{
      const arts = [...document.querySelectorAll('article[data-testid="tweet"]')];
      const a = arts.find(x => [...x.querySelectorAll('a[href*="/status/{status_id}"]')].length);
      return a ? true : false;
    }})()"""


def click_in_article(tab: Tab, status_id: str, testid: str) -> bool:
    expr = f"""(() => {{
      const arts = [...document.querySelectorAll('article[data-testid="tweet"]')];
      const a = arts.find(x => [...x.querySelectorAll('a[href*="/status/{status_id}"]')].length);
      if (!a) return false;
      const b = a.querySelector('[data-testid="{testid}"]');
      if (!b || b.getAttribute('aria-disabled') === 'true') return false;
      b.scrollIntoView({{block: 'center'}});
      b.click();
      return true;
    }})()"""
    return bool(tab.wait_js(expr, timeout=8))


def _has_js(status_id: str, testid: str) -> str:
    return f"""(() => {{
      const a = [...document.querySelectorAll('article[data-testid="tweet"]')]
        .find(x => [...x.querySelectorAll('a[href*="/status/{status_id}"]')].length);
      return !!(a && a.querySelector('[data-testid="{testid}"]'));
    }})()"""


def click_in_article_has(tab: Tab, status_id: str, testid: str) -> bool:
    return bool(tab.js(_has_js(status_id, testid)))


def _own_skip(action: str, cand: dict) -> dict | None:
    from core.own_accounts import is_own
    user = cand.get("user") or cand.get("target") or ""
    if is_own(user):
        return {"action": action, "target": user, "url": cand.get("url"), "skipped": "own_account"}
    return None


def act_like(tab: Tab, cand: dict, dry: bool, lane: str) -> dict:
    if (skip := _own_skip("like", cand)):
        return skip
    if not tab.js(find_article_js(cand["id"])):
        tab.goto(cand["url"], settle=12)
    if dry:
        return {"action": "like", "target": cand["user"], "url": cand["url"],
                "would": "like", "testid": "like"}
    if not click_in_article(tab, cand["id"], "like"):
        if click_in_article_has(tab, cand["id"], "unlike"):
            return {"action": "like", "target": cand["user"], "url": cand["url"], "skipped": "already_liked"}
        return {"action": "like", "target": cand["user"], "url": cand["url"], "done": False, "why": "button_missing"}
    if not tab.wait_js(_has_js(cand["id"], "unlike"), timeout=5):
        return {"action": "like", "target": cand["user"], "url": cand["url"], "done": False, "why": "like_not_registered"}
    return {"action": "like", "target": cand["user"], "url": cand["url"], "done": True}


_ONBRAND_RE = {
    "x_ima": re.compile(r"(office|meeting|standup|slack|email|coffee|caffeine|burnout|"
                        r"monday|friday|deadline|jira|zoom|wfh|boss|shift|corporate|vape|"
                        r"cold brew|nicotine|adderall|vyvanse|adhd|ritalin|concerta|hyperfocus|executive dysfunction|okr|all.hands)", re.I),
    "x_hailports": re.compile(r"(automation|llm|agent|build|pipeline|script|workflow|"
                              r"open source|api|model|deploy)", re.I),
    "x_darkgary": re.compile(r"(aggie|aggies|gig em|astros|texans|rockets|sec\b|"
                             r"college football|kyle field|houston)", re.I),
}


def act_repost(tab: Tab, cand: dict, dry: bool, lane: str) -> dict:
    if (skip := _own_skip("repost", cand)):
        return skip
    # only clearly on-brand, safe posts; keyword must hit the post text itself
    if not _ONBRAND_RE[lane].search(cand.get("text") or ""):
        return {"action": "repost", "target": cand["user"], "url": cand["url"], "skipped": "not_on_brand"}
    if dry:
        return {"action": "repost", "target": cand["user"], "url": cand["url"], "would": "repost"}
    if not tab.js(find_article_js(cand["id"])):
        tab.goto(cand["url"], settle=12)
    if not click_in_article(tab, cand["id"], "retweet"):
        return {"action": "repost", "target": cand["user"], "url": cand["url"], "done": False, "why": "button_missing"}
    # confirm inside the menu
    confirmed = tab.wait_js("""(() => {
      const item = [...document.querySelectorAll('[role="menuitem"], [data-testid="confirmationSheetConfirm"]')]
        .find(b => /repost/i.test(b.innerText || '') || b.getAttribute('data-testid') === 'confirmationSheetConfirm');
      if (!item) return false;
      item.click();
      return true;
    })()""", timeout=8)
    return {"action": "repost", "target": cand["user"], "url": cand["url"], "done": bool(confirmed)}


def act_follow(tab: Tab, cand: dict, dry: bool, lane: str, cfg: dict) -> dict:
    if (skip := _own_skip("follow", cand)):
        return skip
    handle = cand["user"]
    if cand.get("verified") is False:  # 10/25 operator: 100% Verified-only follows (500 Verified followers goal)
        return {"action": "follow", "target": handle, "url": f"https://x.com/{handle}", "skipped": "not_verified"}
    url = f"https://x.com/{handle}"
    tab.goto(url, settle=9)
    pinfo = tab.js(r"""(() => {
      const d = document.querySelector('[data-testid="UserDescription"]')?.innerText || '';
      const n = document.querySelector('[data-testid="UserName"]')?.innerText || '';
      const ver = !!document.querySelector('[data-testid="UserName"] [data-testid="icon-verified"], [data-testid="UserName"] svg[aria-label*="Verified"]');
      const a = document.querySelector('a[href$="/verified_followers"], a[href$="/followers"]');
      const m = a && (a.innerText || '').replace(/,/g, '').match(/([\d.]+)\s*([KM]?)/i);
      const fn = m ? Math.round(parseFloat(m[1]) * ({k: 1e3, m: 1e6}[(m[2] || '').toLowerCase()] || 1)) : null;
      const fa = document.querySelector('a[href$="/following"]');
      const fm = fa && (fa.innerText || '').replace(/,/g, '').match(/([\d.]+)\s*([KM]?)/i);
      const fgn = fm ? Math.round(parseFloat(fm[1]) * ({k: 1e3, m: 1e6}[(fm[2] || '').toLowerCase()] || 1)) : null;
      const already = [...document.querySelectorAll('button[aria-label^="Unfollow @"], [role="button"][aria-label^="Unfollow @"], [data-testid$="-unfollow"], [data-testid="userUnfollow"]')]
        .some(x => !/^Subscribe/i.test((x.innerText || '').trim()) && !/^Subscribe/i.test(x.getAttribute('aria-label') || ''));
      return {bio: (d + ' ' + n) || document.body.innerText.slice(0, 1500), ver, followers_n: fn, following_n: fgn, already};
    })()""") or {}
    if pinfo.get("already"):
        return {"action": "follow", "target": handle, "url": url, "skipped": "already_following"}
    if not pinfo.get("ver"):  # 10/25: never waste follow slots on unverified accounts
        return {"action": "follow", "target": handle, "url": url, "skipped": "not_verified"}
    if (pinfo.get("followers_n") or 0) > 50_000:  # mega-accounts rarely follow back; save slots for <50k Verified peers
        return {"action": "follow", "target": handle, "url": url, "skipped": "too_large_for_followback", "followers_n": pinfo.get("followers_n")}
    # 2026-11-02 operator [pt282u] max-effort: likely follow-backs only: followers/following ratio < 2
    _fn, _fg = pinfo.get("followers_n"), pinfo.get("following_n")
    _rmax = float(((cfg.get("follow_targeting") or {}).get("max_ratio")) or 2.0)
    if _fn is not None and _fg is not None and _fn > _rmax * max(1, _fg):
        return {"action": "follow", "target": handle, "url": url, "skipped": "ratio_high", "followers_n": _fn, "following_n": _fg}
    bio = str(pinfo.get("bio") or "")
    kws = (cfg.get("lanes", {}).get(lane, {}).get("bio_keywords")) or []
    surfaced = (cand.get("text") or "")
    onbrand_re = _ONBRAND_RE.get(lane)
    bio_hit = (any(k.lower() in bio.lower() for k in kws)
               or (onbrand_re and onbrand_re.search(bio))
               or (onbrand_re and onbrand_re.search(surfaced))
               or bool(cand.get("_verified_commenter"))
               or bool(cand.get("_community")))
    if kws and bio.strip() and not bio_hit:
        return {"action": "follow", "target": handle, "url": url, "skipped": "bio_mismatch"}
    if dry:
        return {"action": "follow", "target": handle, "url": url, "would": "follow", "verified": True, "bio_hit": bool(bio_hit)}
    # 2026-10-26 anti-ban: human profile reading dwell + scroll before clicking Follow
    try:
        tab.js(f"window.scrollBy(0, {random.randint(180, 480)})")
        time.sleep(random.uniform(2.2, 4.8))
        tab.js("window.scrollTo(0, 0)")
        time.sleep(random.uniform(1.2, 2.8))
    except Exception:
        pass
    clicked = tab.js(r"""(() => {
      const b = [...document.querySelectorAll('[data-testid$="-follow"], button[aria-label^="Follow @"], [role="button"]')]
        .find(x => !x.closest('[data-testid="UserCell"]') && (/^Follow(\s+@|$)/i.test((x.getAttribute('aria-label')||'').trim()) || /^Follow$/i.test((x.innerText||'').trim())));
      if (!b) return false;
      b.click();
      return true;
    })()""")
    if not clicked:
        return {"action": "follow", "target": handle, "url": url, "done": False, "why": "button_missing"}
    confirmed = tab.wait_js(r"""(() => {
      return [...document.querySelectorAll('button[aria-label^="Unfollow @"], [role="button"][aria-label^="Unfollow @"], [data-testid$="-unfollow"], [data-testid="userUnfollow"]')]
        .some(x => !/^Subscribe/i.test((x.innerText || '').trim()) && !/^Subscribe/i.test(x.getAttribute('aria-label') || ''));
    })()""", timeout=6)
    return {"action": "follow", "target": handle, "url": url, "done": bool(confirmed), "verified": True,
            "followers_n": pinfo.get("followers_n"), "following_n": pinfo.get("following_n"), "bio_hit": bool(bio_hit)}


def act_unfollow(tab: Tab, handle: str, dry: bool, lane: str) -> dict:
    """Unfollow a NON-follower we followed >= 7 days ago. Never unfollow mutuals."""
    from core.own_accounts import is_own
    if is_own(handle):
        return {"action": "unfollow", "target": handle, "skipped": "own_account"}
    url = f"https://x.com/{handle}"
    tab.goto(url, settle=12)
    follows_you = tab.js("""(() => {
      const t = document.body.innerText || '';
      return /follows you/i.test(t.slice(0, 3000));
    })()""")
    if follows_you:
        return {"action": "unfollow", "target": handle, "url": url, "skipped": "mutual"}
    if dry:
        return {"action": "unfollow", "target": handle, "url": url, "would": "unfollow_nonfollower"}
    clicked = tab.js("""(() => {
      const b = document.querySelector('[data-testid$="-unfollow"], [data-testid="userUnfollow"]');
      if (!b) return false;
      b.click();
      return true;
    })()""")
    if not clicked:
        return {"action": "unfollow", "target": handle, "url": url, "done": False, "why": "button_missing"}
    confirmed = tab.wait_js("""(() => {
      const b = document.querySelector('[data-testid="confirmationSheetConfirm"]');
      if (!b) return false;
      b.click();
      return true;
    })()""", timeout=8)
    return {"action": "unfollow", "target": handle, "url": url, "done": bool(confirmed)}


_THREAD_JS = r"""
(() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const inQuote = el => !!el.closest('div[role="link"]');
  const links = [...a.querySelectorAll('a[href*="/status/"]')].filter(l => !inQuote(l))
    .map(l => (l.getAttribute('href') || '').split('?')[0])
    .filter(h => /^\/[^/]+\/status\/\d+$/.test(h));
  const tm = [...a.querySelectorAll('time')].find(t => !inQuote(t));
  const tl = tm ? tm.closest('a') : null;
  const h = ((tl && tl.getAttribute('href')) || links[0] || '').split('?')[0];
  const m = h.match(/^\/([^/]+)\/status\/(\d+)/);
  const allIds = links.map(x => (x.match(/status\/(\d+)/) || [])[1]).filter(Boolean);
  const texts = [...a.querySelectorAll('[data-testid="tweetText"]')];
  const mainEl = texts.find(t => !inQuote(t));
  const quoteEl = texts.find(t => inQuote(t));
  const cardEl = a.querySelector('[data-testid="card.wrapper"]');
  const alts = [...a.querySelectorAll('[data-testid="tweetPhoto"] img[alt], img[alt]')]
    .map(i => i.getAttribute('alt') || '')
    .filter(x => x && x !== 'Image' && !/^(Square|Circle) profile picture$/i.test(x) && x.length > 3)
    .slice(0, 2).join(' | ');
  let full = mainEl ? (mainEl.innerText || '').slice(0, 380) : '';
  if (quoteEl && quoteEl.innerText) full += (full ? ' [Quoting: ' : '[Quoting: ') + (quoteEl.innerText || '').slice(0, 240) + ']';
  else if (cardEl && cardEl.innerText) full += (full ? ' [Card: ' : '[Card: ') + (cardEl.innerText || '').replace(/\s+/g, ' ').slice(0, 180) + ']';
  if (alts) full += (full ? ' [Image: ' : '[Image: ') + alts.slice(0, 160) + ']';
  const uMatch = a.querySelector('[data-testid="User-Name"]');
  const uFallback = uMatch ? ((uMatch.innerText || '').match(/@([A-Za-z0-9_]{1,15})/) || [])[1] : '';
  const user = (m && m[1]) || uFallback || '';
  const id = (m && m[2]) || allIds[0] || '';
  return (user && (id || full)) ? {user, id, all_ids: allIds, text: full.slice(0, 600)} : null;
}).filter(Boolean))()
"""


def fetch_thread(tab: Tab, url: str, sid: str) -> dict:
    """Open the status page: ancestors (root first), the focal post, sibling replies below it."""
    tab.goto(url, settle=9)
    tab.wait_js("""!!document.querySelector('article[data-testid="tweet"]')""", timeout=8)
    rows = tab.js(_THREAD_JS) or []
    idx = next((i for i, r in enumerate(rows) if r.get("id") == sid or sid in (r.get("all_ids") or [])), -1)
    if idx < 0 and rows:
        m_u = re.search(r"x\.com/([^/]+)/status/", url or "")
        want_u = (m_u.group(1) if m_u else "").lower()
        idx = next((i for i, r in enumerate(rows) if (r.get("user") or "").lower() == want_u and r.get("text")), 0)
        rows[idx]["id"] = sid
    if idx < 0 or not (rows[idx].get("text") or "").strip():
        return {"ancestors": [], "focal": None, "siblings": [], "ok": False}
    return {"ancestors": rows[:idx], "focal": rows[idx], "siblings": rows[idx + 1:idx + 7], "ok": True}


def thread_text(th: dict) -> str:
    lines = [f"@{r['user']}: {r['text']}" for r in th.get("ancestors") or []]
    if th.get("focal"):
        lines.append(f">>> THE MESSAGE YOU ARE REPLYING TO, @{th['focal']['user']}: {th['focal']['text']}")
    if th.get("siblings"):
        lines.append("other replies under it:")
        lines += [f"  @{r['user']}: {r['text'][:160]}" for r in th["siblings"]]
    return "\n".join(lines)


_OWN_REPLIED: dict = {}


def own_replied_ids(tab: Tab, lane: str) -> set:
    """Status IDs visible on our own with_replies tab (cached 10 min). Any hit = refuse."""
    import time as _t
    c = _OWN_REPLIED.get(lane)
    if c and _t.time() - c[0] < 600:
        return c[1]
    from core.own_accounts import handle_lane
    me = next((h for h, l in handle_lane().items() if l == lane and h not in ("ima_furad", "darkgary", "darkrgary")), "")
    ids: set = set()
    if me:
        tab.goto(f"https://x.com/{me}/with_replies", settle=9)
        for _ in range(3):
            ids |= {r["id"] for r in (tab.js(_THREAD_JS) or [])}
            tab.js("window.scrollBy(0, 2500)"); _t.sleep(2.5)
    _OWN_REPLIED[lane] = (_t.time(), ids)
    return ids


def act_reply(tab: Tab, cand: dict, dry: bool, lane: str) -> dict:
    if os.path.exists("/home/operator/socials/REPLIES_OFF") or os.path.exists(f"/home/operator/socials/REPLIES_OFF_{lane}"):
        return {"action": "reply", "target": cand.get("user"), "url": cand.get("url"), "skipped": "replies_paused"}
    if (skip := _own_skip("reply", cand)):
        return skip
    try:  # never reply twice to one post (a click that posted may have been logged done=False before the fix)
        _sid = str(cand.get("id") or cand["url"].rstrip("/").split("/")[-1])
        _t_low = str(cand.get("user") or "").lstrip("@").lower()
        _recent_lines = open("/home/operator/socials/engage_ledger.jsonl").readlines()[-3000:]
        if cand.get("via") == "reply_guy" and _t_low:
            for _l in _recent_lines[-450:]:
                if '"action": "reply"' in _l and '"done": true' in _l and f'"{lane}"' in _l and f'"{_t_low}"' in _l.lower():
                    return {"action": "reply", "target": cand.get("user"), "url": cand["url"], "skipped": "author_recent"}
        for _l in _recent_lines:
            if _sid in _l and '"reply"' in _l and '"done": true' in _l or (_sid in _l and '"reply"' in _l and '"done": false' in _l and "composer_missing" not in _l and "reply_button_missing" not in _l):
                return {"action": "reply", "target": cand.get("user"), "url": cand["url"], "skipped": "already_replied"}
    except Exception:
        pass
    from core import x_dedupe, reply_guard
    _sid = str(cand.get("id") or cand["url"].rstrip("/").split("/")[-1])
    _b0 = {"action": "reply", "target": cand.get("user"), "url": cand["url"]}
    if x_dedupe.seen(lane, "reply:" + _sid):
        return {**_b0, "skipped": "already_attempted"}
    if not dry and _sid in own_replied_ids(tab, lane):
        x_dedupe.claim(lane, "reply:" + _sid)
        return {**_b0, "skipped": "own_with_replies_hit"}
    from core.own_accounts import is_own as _is_own
    from core import free_judge as _FJ
    fast_pre = cand.get("via") == "reply_guy" and len((cand.get("text") or "").strip()) >= 30
    if fast_pre:
        focal_txt = cand.get("text") or ""
        ctx = f">>> THE MESSAGE YOU ARE REPLYING TO, @{cand.get('user','')}: {focal_txt}"
    else:
        th = fetch_thread(tab, cand["url"], _sid)
        if not th["ok"]:
            return {**_b0, "skipped": "thread_unreadable"}
        if cand.get("via") != "reply_back" and any(_is_own(r["user"]) for r in th["ancestors"]):
            return {**_b0, "skipped": "own_thread_not_inbox"}
        ctx = thread_text(th)
        focal_txt = (th.get("focal") or {}).get("text") or cand.get("text") or ""
    if cand.get("via") == "reply_guy":
        # Standalone stranger post in our niche: only skip hostile or sensitive (words like 'automated'/'bot' in an AI post are on-niche, not directed at us)
        if reply_guard._HOSTILE.search(focal_txt):
            kind = "hostile"
        elif reply_guard._SENSITIVE.search(focal_txt):
            kind = "sensitive"
        else:
            kind = "neutral"
    else:
        kind = reply_guard.classify_incoming(focal_txt)
    skeptic = kind == "skeptical" and reply_guard.autoreply_ok(kind, lane, cand.get("via") or "")
    if not reply_guard.autoreply_ok(kind, lane, cand.get("via") or ""):
        if cand.get("via") == "reply_back":
            reply_guard.tip(lane, kind, {**cand, "id": _sid, "context": ctx})
        x_dedupe.claim(lane, "reply:" + _sid)
        return {**_b0, "skipped": "no_autoreply:" + kind}
    coh_src = focal_txt + "\n" + (cand.get("text") or "") + "\n" + ctx[-900:]
    reply, ok, why, tries = "", False, "no_draft", []
    with _FJ.reply_back_mode(lane):  # 10/24 operator: fast calibrated free judges for all outbound replies (floors 7/8 intact)
        for _ in range(3):  # first draft that clears every gate wins; retries get the judge's feedback
            reply = generate_reply(lane, focal_txt, ctx, skeptic=skeptic, feedback=why)
            ok, why = reply_ok(lane, reply, coh_src)
            if ok and skeptic:  # deterministic: every number must come from the one fact or their reply
                _src = " ".join(getattr(generate_reply, "last_facts", None) or []) + " " + focal_txt
                _bad = [n for n in re.findall(r"\d+(?:\.\d+)?", reply) if n not in re.findall(r"\d+(?:\.\d+)?", _src)]
                if _bad:
                    ok, why = False, f"skeptic_invented_number:{_bad[0]}"
            if ok:
                _hs, _hw = reply_guard.human_score(reply, ctx)
                if _hs < _human_score_min(lane):
                    ok, why = False, f"sounds_like_bot:{_hs}:{_hw}"
                else:  # 2026-10-20 operator: lighter viral-reason check on replies (value + reason to engage)
                    try:
                        from core.viral_reason import passes as _vr
                        # keyword floor: positional slot after ref is votes, not floor
                        _vok, _vs, _ = _vr(reply, lane, "reply", ctx[-900:], "", True, _sid, floor=_viral_reply_floor(lane))
                        if not _vok:
                            ok, why = False, f"viral_reply:{_vs}"
                    except Exception as _e:
                        ok, why = False, f"viral_reply_error:{type(_e).__name__}"
            tries.append(why[:60])
            if ok:
                break
    base = {"action": "reply", "target": cand["user"], "url": cand["url"], "text": reply, "tries": tries}
    if not ok:
        return {**base, "skipped": "gate", "why": why}
    if dry:
        return {**base, "would": "reply", "dry_run_gate": why}
    if fast_pre and not tab.js(find_article_js(_sid)):
        th = fetch_thread(tab, cand["url"], _sid)
        if th.get("ok") and any(_is_own(r["user"]) for r in th["ancestors"]):
            return {**base, "skipped": "own_thread_not_inbox"}
    if not x_dedupe.claim(lane, "reply:" + _sid):  # fsynced before ANY click; never twice, whatever happens
        return {**base, "skipped": "already_attempted"}
    if not tab.js(find_article_js(_sid)):
        tab.goto(cand["url"], settle=10)
    time.sleep(random.uniform(2.5, 6.0))  # 2026-10-26 anti-ban: human reading dwell on target tweet before opening reply
    if not click_in_article(tab, _sid, "reply"):
        return {**base, "done": False, "why": "reply_button_missing"}
    # the status page has an inline box already; the reply click opens a DIALOG. Wait for that one.
    box = tab.wait_js("""!!document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')""", timeout=12)
    if not box:
        # 2026-10-17 fallback: open the status page itself and use the inline tweetTextarea_0
        try:
            tab.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape")
            tab.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape")
        except Exception:
            pass
        tab.goto(cand["url"], settle=10)
        box = tab.wait_js("""!!document.querySelector('main [data-testid="tweetTextarea_0"]')""", timeout=12)
        if not box:
            return {**base, "done": False, "why": "composer_missing"}
        base["via_inline_fallback"] = True
    inline = not tab.js("""!!document.querySelector('[role="dialog"] [data-testid^="tweetTextarea"]')""")
    if not XP.send_verified(tab, reply):  # shared send path: exact read-back + trusted click
        try:
            XP.clear_editor(tab)
            tab.js("""(() => { const b = document.querySelector('[role="dialog"] [aria-label="Close"], [role="dialog"] [data-testid="app-bar-close"]'); if (b) b.click(); return true; })()""")
            time.sleep(1)
            tab.js("""(() => { const b = document.querySelector('[data-testid="confirmationSheetCancel"]'); if (b) b.click(); return !!b; })()""")
        except Exception:
            pass
        return {**base, "done": False, "why": "composer_text_mismatch"}
    time.sleep(3)
    clicked = True
    _probe = json.dumps(" ".join(reply.split()[:4]).lower())
    cleared = tab.js("""(() => {  // sent = our text is no longer sitting in any composer
      const p = %s;
      return ![...document.querySelectorAll('[data-testid="tweetTextarea_0"]')]
        .some(b => (b.innerText || '').replace(/\\s+/g, ' ').toLowerCase().includes(p));
    })()""" % _probe)
    try:  # leave the dialog closed whatever happened
        tab.js("""(() => { const b = document.querySelector('[role="dialog"] [aria-label="Close"], [role="dialog"] [data-testid="app-bar-close"]');
          if (b) b.click(); return true; })()""")
    except Exception:
        pass
    try:  # post-send live check: mismatch is deleted immediately and tipped
        _me = XP._norm_handle((XP.read_identity(tab) or {}).get("profile_href") or "") or (XP.lane_cfg(lane).get("expect") or "").lstrip("@")
        lc = XP.live_check(tab, _me, reply, lane, "reply")
    except Exception as exc:  # noqa: BLE001
        lc = {"live_ok": False, "error": str(exc)[:120]}
    _out = {**base, "done": bool(clicked and cleared and lc.get("live_ok")), "live_check": lc}
    _me = locals().get("_me") or ""
    if lc.get("live_ok") and lc.get("id") and _me:  # 2026-10-22: record OUR reply's URL, not just the target's
        _out["reply_url"] = f"https://x.com/{_me.lstrip('@')}/status/{lc['id']}"
    return _out


# ── DM: only warm (new follower who engaged, or an existing thread). Max 5/day. ──

_NOTIF_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('article[data-testid="notification"], article[data-testid="tweet"]').forEach(a => {
    const t = (a.innerText || '');
    const users = [...new Set([...a.querySelectorAll('a[href^="/"]')].map(l => l.getAttribute('href'))
      .filter(h => /^\/[A-Za-z0-9_]{1,15}$/.test(h)).map(h => h.slice(1)))];
    let kind = null;
    if (/followed you/i.test(t)) kind = 'followed';
    else if (/liked your|reposted your|Replying to/i.test(t)) kind = 'engaged';
    if (!kind) return;
    // a "Replying to @us" tweet: only the author (first link) engaged
    const who = /Replying to/i.test(t) ? users.slice(0, 1) : users;
    who.forEach(u => out.push({user: u, kind}));
  });
  return out;
})()
"""


def warm_dm_targets(tab: Tab, lane: str) -> list[str]:
    """Users who BOTH followed us and liked/replied (same notifications page snapshot)."""
    tab.goto("https://x.com/notifications", settle=10)
    rows = []
    for _ in range(4):
        rows += tab.js(_NOTIF_JS) or []
        tab.js("window.scrollBy(0, 2500)"); time.sleep(2)
    follows, engaged = set(), set()
    for r in rows:
        if r.get("kind") == "followed":
            follows.add(r["user"])
        elif r.get("kind") == "engaged":
            engaged.add(r["user"])
    from core.own_accounts import is_own
    dmed = {str(r.get("target") or "").lower() for r in EC.ledger_rows()
            if r.get("lane") == lane and r.get("action") == "dm" and r.get("done")}
    return sorted(h for h in (follows & engaged) if h and not is_own(h) and h.lower() not in dmed)


def act_dm(tab: Tab, handle: str, dry: bool, lane: str) -> dict:
    from core.own_accounts import is_own
    if is_own(handle):
        return {"action": "dm", "target": handle, "skipped": "own_account"}
    text = {
        "x_ima": "thanks for the follow. the cold brew is free, the jokes cost everything.",
        "x_hailports": "thanks for following along — more builds shipping weekly.",
        "x_darkgary": "appreciate the follow. gig em.",
    }.get(lane, "thanks for the follow.")
    url = f"https://x.com/{handle}"
    if dry:
        return {"action": "dm", "target": handle, "url": url, "text": text, "would": "dm_thank_you"}
    tab.goto(url, settle=12)
    clicked = tab.js("""(() => {
      const b = document.querySelector('[data-testid="sendDMFromProfile"], [aria-label*="Message"]');
      if (!b) return false;
      b.click();
      return true;
    })()""")
    if not clicked:
        return {"action": "dm", "target": handle, "url": url, "done": False, "why": "dm_button_missing"}
    time.sleep(4)
    if "/i/chat/pin" in str(tab.js("location.href") or ""):
        # XChat encrypted DMs need the account's chat PIN; a human must set/enter it once.
        flag = Path(f"/home/operator/socials/DM_PIN_NEEDED_{lane}")
        flag.write_text(utc())
        return {"action": "dm", "target": handle, "url": url, "skipped": "xchat_pin_required"}
    box = tab.wait_js("""!!document.querySelector('[data-testid="dmComposerTextInput"], [data-testid="dmComposerTextarea"], [data-testid*="xchat"] textarea, [data-testid*="xchat"] [contenteditable="true"]')""", timeout=12)
    if not box:
        return {"action": "dm", "target": handle, "url": url, "done": False, "why": "composer_missing"}
    XP.type_text(tab, text)
    time.sleep(0.8)
    # 10/18: read-back before send (same rule as posts/replies): composer must equal the draft exactly
    _BOX_JS = """(() => { const e = document.querySelector('[data-testid="dmComposerTextInput"], [data-testid="dmComposerTextarea"], [data-testid*="xchat"] textarea, [data-testid*="xchat"] [contenteditable="true"]');
      return e ? (e.value !== undefined && e.tagName === 'TEXTAREA' ? e.value : e.innerText) : null; })()"""
    typed = tab.js(_BOX_JS)
    if XP._live_norm(typed or "") != XP._live_norm(text):
        tab.js("""(() => { const e = document.activeElement; if (e) { document.execCommand('selectAll'); document.execCommand('delete'); } })()""")
        return {"action": "dm", "target": handle, "url": url, "done": False, "why": "readback_mismatch", "typed": (typed or "")[:200]}
    sent = tab.js("""(() => {
      const b = document.querySelector('[data-testid="dmComposerSendButton"]');
      if (!b || b.getAttribute('aria-disabled') === 'true') return false;
      b.click();
      return true;
    })()""")
    if not sent:
        return {"action": "dm", "target": handle, "url": url, "text": text, "done": False, "why": "send_disabled"}
    # post-send live check: the exact text must appear in the conversation
    live = False
    for _ in range(4):
        time.sleep(3)
        body = XP._live_norm(str(tab.js("document.querySelector('main') ? document.querySelector('main').innerText : ''") or ""))
        if XP._live_norm(text) in body:
            live = True
            break
    if not live:
        try:
            from core.reply_guard import tip
            tip(lane, "dm_not_seen", {"id": handle, "draft": text})
        except Exception:
            pass
    return {"action": "dm", "target": handle, "url": url, "text": text, "done": live, "live_ok": live}


# ── tick ────────────────────────────────────────────────────────────────────────

def _candidates_for(lane: str, cfg: dict, tab: Tab) -> list[dict]:
    queries = (cfg.get("lanes", {}).get(lane, {}).get("queries")) or []
    if not queries:
        return []
    _adhd = [q for q in queries if re.search(r"adhd|adderall|vyvanse|ritalin|concerta|hyperfocus|executive dysf|time blind|bipolar|cali sober", q, re.I)]
    _q = os.environ.get("ENGAGE_QUERY") or (random.choice(_adhd) if lane == "x_ima" and _adhd and random.random() < 0.5 else random.choice(queries))
    cands = collect_candidates(tab, _q)
    me = (tab.js("""(() => {
      const p = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
      return p ? (p.getAttribute('href') || '').replace(/^\\/+|\\/+$/g, '').split('/')[0] : '';
    })()""") or "").lower()
    from core.own_accounts import is_own
    acted = {str(r.get("url") or "").split("/status/")[-1] for r in today_rows(lane)
             if r.get("action") in ("like", "repost") and (r.get("done") or r.get("skipped") == "already_liked")}
    return [c for c in cands
            if c["id"] not in acted
            and c["user"].lower() != me
            and not c["user"].lower().startswith("home")
            and not is_own(c["user"])]


HOURLY_CAP = 55           # confirmed actions per lane per rolling hour (anti-ban safe ceiling)
FOLLOW_GAP_S = 32        # at least 32s+jitter between follows


def _recent_done_rows(lane: str, seconds: float, action: str | None = None) -> list[dict]:
    now = datetime.now(CT)
    out = []
    for r in today_rows(lane):
        if not r.get("done") or r.get("dry_run") or (action and r.get("action") != action):
            continue
        ts = _ts_of(r.get("ts"))
        if ts and (now - ts).total_seconds() < seconds:
            out.append(r)
    return out


def _recent_done(lane: str, seconds: float, action: str | None = None) -> list[datetime]:
    now = datetime.now(CT)
    out = []
    for r in today_rows(lane):
        if not r.get("done") or r.get("dry_run") or (action and r.get("action") != action):
            continue
        ts = _ts_of(r.get("ts"))
        if ts and (now - ts).total_seconds() < seconds:
            out.append(ts)
    return out


def _rate_ok(lane: str, action: str) -> bool:
    try:
        from core.req_pacer import wait as _pace; _pace(lane)  # 10/18 per-lane req/min cap + jitter
    except Exception:
        pass
    cap = int((load_config().get("tick") or {}).get("hourly_cap", HOURLY_CAP))
    if len(_recent_done(lane, 3600)) >= cap:
        return False
    if action == "reply" and len(_recent_done(lane, 3600, "reply")) >= 9:
        return False  # anti-ban: never exceed 9 replies/hr on one lane (avoids X 326/226 reply rate limit)
    if action == "follow":
        _fw = (load_config().get("follow_targeting") or {}).get("hours_ct") or [7, 23]  # 2026-11-02 [pt282u]: follows 7 AM-11 PM CT only
        if not (int(_fw[0]) <= datetime.now(CT).hour < int(_fw[1])):
            return False
        if len(_recent_done(lane, 3600, "follow")) >= 8 or _recent_done(lane, 180, "unfollow"):
            return False  # anti-ban: max 8 follows/hr & never follow within 3 min of an unfollow
        last = _recent_done(lane, 120, "follow")
        if last:
            wait = FOLLOW_GAP_S - (datetime.now(CT) - max(last)).total_seconds()
            if wait > 0:
                time.sleep(wait + random.uniform(6, 18))
    return True


def tick(lane: str, dry: bool) -> dict:
    cfg = load_config()
    if EC.KILL.exists():
        return {"skipped": "kill_switch"}
    cd = EC.on_cooldown(lane)
    if cd:
        return {"skipped": cd}
    if not EC.in_window(cfg) and not os.environ.get("ENGAGE_IGNORE_WINDOW"):
        return {"skipped": "outside_window"}
    if lane not in (cfg.get("lanes") or {}):
        return {"skipped": "unknown_lane"}
    lane_cfg = XP.lane_cfg(lane)
    with Tab(lane_cfg["cdp_url"]) as tab:
        for _m, _kw in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                        ("Page.setWebLifecycleState", {"state": "active"})):
            try:
                tab.cmd(_m, **_kw)
            except Exception:
                pass
        ident = XP.read_identity(tab)
        XP.check_identity(ident, lane, lane_cfg, require=True)
        if (d := EC.danger_scan(tab, lane)):
            return {"skipped": d}
        me = (ident.get("handle") or "").lower()

        results = []
        viral = cfg.get("viral") or {}
        # 2026-10-24 operator 'need WAY more replies': reply phases run FIRST in every tick (using fast free judges),
        # followed by the like/follow/repost batch.
        if not any(r.get("paused") for r in results) and not EC.on_cooldown(lane):
            if not dry and under_cap(lane, "reply", cfg):
                try:
                    results += reply_back(tab, lane, dry, me)
                except Exception as _e:
                    results.append({"action": "reply", "via": "reply_back", "error": str(_e)[:120]})
            if viral.get("reply_guy") and under_cap(lane, "reply", cfg):
                from agents.x_replyguy import pick_candidates
                _picks = pick_candidates(tab, lane, cfg)
                if len(_picks) < 8:  # top up from niche search cache so we never starve on candidates
                    _seen_ids = {str(p.get("id")) for p in _picks}
                    for _c in (_candidates_for(lane, cfg, tab)):
                        if str(_c.get("id")) not in _seen_ids and len((_c.get("text") or "").strip()) >= 25:
                            _picks.append(_c); _seen_ids.add(str(_c.get("id")))
                if not _picks:
                    EC.ledger_append({"action": "reply", "via": "reply_guy", "lane": lane, "skipped": "no_fresh_candidate",
                                      "dry_run": dry, "ts": utc()})
                _replied = {str(r.get("url") or "").split("/status/")[-1] for r in EC.ledger_rows()
                            if r.get("lane") == lane and r.get("action") == "reply" and r.get("done")}
                _recent_authors = {str(r.get("target") or "").lower() for r in _recent_done_rows(lane, 6 * 3600, "reply")}
                _wl = {str(h).lower() for h in (((cfg.get("lanes") or {}).get(lane) or {}).get("reply_targets") or [])}
                _recent_authors |= {str(r.get("target") or "").lower() for r in _recent_done_rows(lane, 86400, "reply")} & _wl  # 2026-11-02 [pt282u]: big accounts max 1 reply/author/day
                _seen_tick_authors: set[str] = set()
                _dedup_picks = []
                for p in _picks:
                    _au = str(p.get("user") or "").lower()
                    if str(p.get("id")) in _replied or _au in _recent_authors or _au in _seen_tick_authors:
                        continue  # 2026-10-26 X Algo Update: never double-reply / self-reply chain under external accounts!
                    _seen_tick_authors.add(_au)
                    _dedup_picks.append(p)
                _picks = _dedup_picks
                for cand in (_picks[:12] if _rate_ok(lane, "reply") else []):  # try up to 12 candidates per tick to land 4-5 replies
                    cand = {**cand, "via": "reply_guy"}
                    res = act_reply(tab, cand, dry, lane)
                    res["via"] = "reply_guy"
                    res.setdefault("lane", lane)
                    res["dry_run"] = dry
                    res.setdefault("ts", utc())
                    EC.ledger_append(res)  # log judge rejects too (skipped/why); caps count only done=True
                    results.append(res)
                    if res.get("done") or res.get("would"):
                        if res.get("done") and not dry:  # 10/25 Multi-Touch Verified Combo: Reply + Like + Follow!
                            try:
                                if under_cap(lane, "like", cfg):
                                    _lk = act_like(tab, cand, False, lane)
                                    _lk.update({"lane": lane, "dry_run": False, "ts": utc(), "via": "reply_combo"})
                                    EC.ledger_append(_lk); results.append(_lk)
                                if under_cap(lane, "follow", cfg) and cand.get("verified") is not False:
                                    _fl = act_follow(tab, cand, False, lane, cfg)
                                    _fl.update({"lane": lane, "dry_run": False, "ts": utc(), "via": "reply_combo"})
                                    EC.ledger_append(_fl); results.append(_fl)
                            except Exception:
                                pass
                        if not dry and (d := EC.danger_scan(tab, lane)):
                            results.append({"skipped": d, "paused": True})
                            break
                        _ok_n = sum(1 for x in results if x.get("via") == "reply_guy" and (x.get("done") or x.get("would")))
                        if _ok_n >= int(((cfg.get("lane_tick") or {}).get(lane) or {}).get("replies_per_tick", viral.get("replies_per_tick", 3))) or not _rate_ok(lane, "reply"):
                            break
                        time.sleep(random.uniform(45, 115))  # 2026-10-26 anti-ban: 45-115s human spacing between replies
            if viral.get("double_down", True) and under_cap(lane, "reply", cfg):
                from core.engagement_watch import mark_flag, pending_flags
                from core.own_accounts import is_own as _own
                for flag in pending_flags(lane)[:1]:
                    if _own(flag.get("user") or ""):
                        mark_flag(lane, flag.get("user") or "")
                        continue
                    if not flag.get("id") or not flag.get("url"):
                        continue
                    res = act_reply(tab, flag, dry, lane)
                    res["via"] = "double_down"
                    res.setdefault("lane", lane)
                    res["dry_run"] = dry
                    res.setdefault("ts", utc())
                    if res.get("done") or res.get("would"):
                        EC.ledger_append(res)
                        mark_flag(lane, flag.get("user") or "")
                    results.append(res)
                    break
        # candidate actions weighted by remaining caps. Reply-guy owns replies.
        pool = [a for a in ("like", "follow") if under_cap(lane, a, cfg)]  # 10/25: no random plain reposts cluttering profile feed
        if not viral.get("reply_guy"):
            if under_cap(lane, "reply", cfg):
                pool.append("reply")
        if (under_cap(lane, "dm", cfg) and random.random() < 0.25 and not dry
                and not Path(f"/home/operator/socials/DM_PIN_NEEDED_{lane}").exists()):
            try:
                _t = warm_dm_targets(tab, lane)
                if _t and _rate_ok(lane, "dm"):
                    _r = act_dm(tab, _t[0], dry, lane)
                    _r.update({"lane": lane, "dry_run": dry, "ts": utc()})
                    EC.ledger_append(_r); results.append(_r)
                elif not _t:
                    results.append({"action": "dm", "skipped": "no_warm_target"})
            except Exception as _e:
                results.append({"action": "dm", "error": str(_e)[:120]})
        if not pool and not results and not under_cap(lane, "reply", cfg):
            return {"skipped": "all_caps"}
        tk = dict(cfg.get("tick") or {}); tk.update((cfg.get("lane_tick") or {}).get(lane) or {})  # 10/18 per-lane cadence
        n = random.randint(int(tk.get("batch_min", 3)), int(tk.get("batch_max", 6)))
        # likes dominate (cheap, human-normal); follows/reposts mixed in by weight
        weights = {"like": 7, "follow": int((cfg.get("follow_targeting") or {}).get("weight", 2)), "repost": 1, "reply": 1, "dm": 1}  # 2026-11-02 [pt282u]: follow weight from config (was 2)
        batch = random.choices(pool, weights=[weights.get(a, 1) for a in pool], k=n) if pool else []
        # 2026-10-23: cheap actions (like/follow/repost) first; judge-heavy replies/DMs last, so one slow reply
        # judge can't starve the tick's likes/follows (overnight + mornings showed ~3 likes/h for this reason).
        batch.sort(key=lambda a: a in ("reply", "dm"))
        _cache: list[dict] = []
        _used: set = set()
        for action in batch:
            if (d := EC.danger_scan(tab, lane)):
                results.append({"skipped": d})
                break
            if not dry and not _rate_ok(lane, action):
                results.append({"skipped": "hourly_cap"})
                break
            if action == "dm":
                targets = warm_dm_targets(tab, lane)
                if not targets:
                    results.append({"action": "dm", "skipped": "no_warm_target"})
                    continue
                res = act_dm(tab, targets[0], dry, lane)
            else:
                # ONE search per tick, reused (was a fresh 15s search per action)
                fresh = [c for c in _cache if c["id"] not in _used]
                if len(fresh) < 2:
                    _cache = _candidates_for(lane, cfg, tab)
                    fresh = [c for c in _cache if c["id"] not in _used]
                if not fresh:
                    results.append({"action": action, "skipped": "no_candidates"})
                    continue
                cand = random.choice(fresh[:12])
                _used.add(cand["id"])
                if action in ("like", "repost") and not tab.js(find_article_js(cand["id"])):
                    # a follow left the search page; reload results once rather than per-post loads
                    _cache = _candidates_for(lane, cfg, tab)
                    fresh = [c for c in _cache if c["id"] not in _used]
                    if fresh:
                        cand = random.choice(fresh[:12]); _used.add(cand["id"])
                if action == "like":
                    res = act_like(tab, cand, dry, lane)
                elif action == "repost":
                    res = act_repost(tab, cand, dry, lane)
                elif action == "reply":
                    res = act_reply(tab, cand, dry, lane)
                elif action == "follow":
                    res = act_follow(tab, cand, dry, lane, cfg)
                else:
                    res = {"skipped": "unknown"}
            res.setdefault("lane", lane)
            res["dry_run"] = dry
            res.setdefault("ts", utc())
            EC.ledger_append(res)
            results.append(res)
            if not dry and (d := EC.danger_scan(tab, lane)):
                results.append({"skipped": d, "paused": True})
                break
            if res.get("done") or res.get("would"):
                time.sleep(random.uniform(float(tk.get("gap_min", 12)), float(tk.get("gap_max", 50))))
            else:
                time.sleep(random.uniform(3, 8))  # skips/failures don't need a human pause
        return {"lane": lane, "dry_run": dry, "actions": results}


_MENTION_JS = r"""
(() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const link = a.querySelector('a[href*="/status/"]'); const txt = a.querySelector('[data-testid="tweetText"]');
  if (!link || !txt) return null;
  const m = link.href.match(/\/([^/]+)\/status\/(\d+)/); if (!m) return null;
  const tm = a.querySelector('time');
  return {user: m[1], id: m[2], url: link.href.split('/photo')[0], text: (txt.innerText||'').slice(0, 300),
          time: tm ? tm.getAttribute('datetime') : ''};
}).filter(Boolean))()
"""


def reply_back(tab: Tab, lane: str, dry: bool, me: str, limit: int = 5) -> list[dict]:
    """Inbox: every reply/mention is tracked in a per-lane inbox file. Each gets ONE answer, written with
    the full thread (root, ancestors, siblings). Confused/skeptical/bot-question/hostile/sensitive
    messages are never auto-answered; they become tips for operator. All lanes, hailports included."""
    from core.own_accounts import is_own
    from core import x_dedupe, reply_guard
    inbox = Path(f"/home/operator/socials/inbox_{lane}.jsonl")
    known = {}
    if inbox.exists():
        for l in inbox.read_text().splitlines():
            try:
                r = json.loads(l); known[r["id"]] = r
            except Exception:
                pass
    try:
        from core.mutuals import refresh_mutuals
        refresh_mutuals(lane)
    except Exception:
        pass
    tab.goto("https://x.com/notifications/mentions", settle=9)
    rows = tab.js(_MENTION_JS) or []
    # 2026-10-26 X Algo Update: also scan our own freshest post (<2.5h old) directly so any comment inside the critical
    # first-2-hours / first-200-views window gets a fast Like + Reply-Back (score = 20)!
    try:
        for _pr in reversed(XP.ledger_rows()[-40:]):
            if _pr.get("lane") == lane and _pr.get("verified") and (_pr.get("live_check") or {}).get("id"):
                _psid = str(_pr["live_check"]["id"])
                _pts = _ts_of(_pr.get("ts") or "")
                if _pts and (datetime.now(CT) - _pts).total_seconds() <= 2.5 * 3600:
                    tab.goto(f"https://x.com/{me}/status/{_psid}", settle=7)
                    for _cr in (tab.js(_MENTION_JS) or [])[1:10]:
                        if (_cr.get("user") or "").lower() != me and not any(x.get("id") == _cr.get("id") for x in rows):
                            rows.append(_cr)
                break
    except Exception:
        pass
    # 10/18 growth: answer the freshest first (first-hour reply-backs lift the post; ownedge reply-back study)
    rows.sort(key=lambda r: _ts_of(r.get("time") or "") or datetime.min.replace(tzinfo=CT), reverse=True)
    out = []
    for r in rows:
        u = (r.get("user") or "").lower()
        if not u or u == me or is_own(u) or r["id"] in known:
            continue
        _t = _ts_of(r.get("time") or "")
        if not _t or (datetime.now(CT) - _t).total_seconds() > 24 * 3600:
            continue
        if len(out) >= limit or not _rate_ok(lane, "reply") or not under_cap(lane, "reply", load_config()):
            break
        th = fetch_thread(tab, r["url"], r["id"])
        entry = {"id": r["id"], "author": r["user"], "text": r.get("text", ""), "url": r["url"], "seen_at": utc(),
                 "parent_chain": [{"id": x["id"], "user": x["user"], "text": x["text"][:280]} for x in th["ancestors"]]}
        if x_dedupe.seen(lane, "reply:" + r["id"]):
            entry["status"] = "already_attempted"
        else:
            from core import free_judge as _FJ  # 2026-10-22 operator: lite judges may score reply-backs (flag file)
            with _FJ.reply_back_mode(lane):
                res = act_reply(tab, {**r, "via": "reply_back"}, dry, lane)
            res.update({"via": "reply_back", "lane": lane, "dry_run": dry, "ts": utc()})
            EC.ledger_append(res)
            out.append(res)
            entry["status"] = "replied" if res.get("done") else (res.get("skipped") or res.get("why") or "failed")
            if res.get("done"):
                try:
                    act_like(tab, r, dry, lane)
                except Exception:
                    pass
                time.sleep(random.uniform(15, 45))
        with inbox.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tab.goto("https://x.com/notifications/mentions", settle=7)
    return out


def unfollow_sweep(lane: str, dry: bool, limit: int = 5) -> dict:
    """Unfollow non-followers we followed >= 7 days ago (mutuals never touched)."""
    cfg = load_config()
    _hb = Path("/home/operator/claude-stack/data/runtime/hb") / f"unfollow_{lane}"
    if _hb.exists() and time.time() - _hb.stat().st_mtime < 3600:  # 10/23: agents/x_unfollow.py owns unfollows
        return {"skipped": "x_unfollow_agent_active"}
    if EC.KILL.exists() or EC.on_cooldown(lane):
        return {"skipped": "gated"}
    if cap_used(lane, "unfollow", cfg) >= int(cfg["caps"]["unfollows"]):
        return {"skipped": "unfollow_cap"}
    now = datetime.now(CT)
    follows_old = {}
    for r in EC.ledger_rows():
        if r.get("lane") != lane or r.get("action") != "follow" or not r.get("done"):
            continue
        ts = _ts_of(r.get("ts"))
        if ts and (now - ts).days >= FOLLOW_TTL_DAYS:
            follows_old[r["target"]] = True
    handled = {str(r.get("target") or "").lower() for r in EC.ledger_rows()
               if r.get("lane") == lane and r.get("action") == "unfollow"
               and (r.get("done") or r.get("skipped") in ("mutual", "own_account"))}
    targets = [h for h in sorted(follows_old) if h.lower() not in handled][:limit]
    if not targets:
        return {"skipped": "no_mature_follows"}
    lane_cfg = XP.lane_cfg(lane)
    done = 0
    with Tab(lane_cfg["cdp_url"]) as tab:
        ident = XP.read_identity(tab)
        XP.check_identity(ident, lane, lane_cfg, require=True)
        for h in targets:
            res = act_unfollow(tab, h, dry, lane)
            res.update({"lane": lane, "dry_run": dry, "ts": utc()})
            EC.ledger_append(res)
            if res.get("done") and not dry:
                done += 1
            time.sleep(random.uniform(20, 90))
    return {"lane": lane, "unfollowed": done, "dry_run": dry}


def _ts_of(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(CT)
    except Exception:
        return None


def cmd_check_identity(lane: str) -> int:
    lane_cfg = XP.lane_cfg(lane)
    with Tab(lane_cfg["cdp_url"]) as tab:
        ident = XP.read_identity(tab)
        XP.check_identity(ident, lane, lane_cfg, require=True)
    print(json.dumps({"ok": True, "lane": lane, "handle": ident.get("handle")}))
    return EXIT_OK


def _tick_heartbeat(lane: str, max_s: float = 2700):
    """2026-10-22 root cause of the 'crash': the hb file was only touched BETWEEN ticks, and a reply tick with slow free
    judges (wait_for up to 300-540 s each) ran >25 min, so the supervisor killed it mid-tick as stale and the whole
    tick's output was lost. Touch hb during a tick too, but only for max_s (45 min): a truly hung tick still goes stale."""
    import threading
    stop = threading.Event()
    hb = Path("/home/operator/claude-stack/data/runtime/hb/engage_" + lane)
    end = time.time() + max_s

    def run():
        while not stop.wait(60) and time.time() < end:
            try:
                hb.touch()
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()
    return stop


def _lane_off(lane: str) -> bool:
    try:
        from core.lanes import is_disabled
        return is_disabled(lane)
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True, choices=["x_ima", "x_hailports", "x_darkgary"])
    ap.add_argument("--dry-run", action="store_true", help="find + gate, log would-dos, zero state clicks")
    ap.add_argument("--tick", action="store_true", help="one randomized action batch, then exit")
    ap.add_argument("--loop", action="store_true", help="ticks with random 4-15 min sleeps, window only")
    ap.add_argument("--unfollow-sweep", action="store_true", help="one unfollow batch of mature non-followers")
    ap.add_argument("--check-identity", action="store_true", help="read-only identity probe (used by ensure script)")
    args = ap.parse_args()
    if _lane_off(args.lane) and not args.check_identity:  # 2026-10-29: lane disabled in config/disabled_lanes.json
        print(json.dumps({"lane": args.lane, "skipped": "lane_disabled"}), flush=True); return 0
    if not (args.tick or args.loop or args.unfollow_sweep or args.check_identity):
        ap.print_help()
        return EXIT_GATED
    if args.check_identity:
        try:
            return cmd_check_identity(args.lane)
        except Abort as a:
            print(json.dumps({"ok": False, "reason": a.reason}))
            return a.code
    from core.x_dedupe import lane_lock
    if not lane_lock(args.lane, "engage"):
        print(json.dumps({"skipped": "another x_engage holds the lane lock", "lane": args.lane}))
        return EXIT_OK
    try:
        while True:
            try:
                Path("/home/operator/claude-stack/data/runtime/hb/engage_" + args.lane).touch()
            except Exception:
                pass
            _hb_stop = _tick_heartbeat(args.lane)
            try:
                if args.unfollow_sweep:
                    out = unfollow_sweep(args.lane, args.dry_run)
                else:
                    out = tick(args.lane, args.dry_run)
            except Abort as a:
                _hb_stop.set()
                out = {"abort": a.reason}
                log(f"ABORT {a.code} {a.reason}")
                if a.code == EXIT_LOGIN:
                    return a.code
                if a.code == EXIT_CDP:
                    # 10/20: a down Chrome used to kill the loop every round (supervisor re-armed it every 5 min).
                    # Wait for the watchdog to relaunch Chrome; give up only after ~15 min of consecutive cdp_down.
                    _cdp_fails = globals().get("_CDP_FAILS", 0) + 1
                    globals()["_CDP_FAILS"] = _cdp_fails
                    if _cdp_fails >= 15:
                        return a.code
                    print(json.dumps({"tick": out}, ensure_ascii=False), flush=True)
                    for _ in range(6):
                        try:
                            Path("/home/operator/claude-stack/data/runtime/hb/engage_" + args.lane).touch()
                        except Exception:
                            pass
                        time.sleep(10)
                    continue
            except Exception as exc:  # noqa: BLE001
                import traceback as _tb; log(_tb.format_exc()[-1500:])
                out = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                log(json.dumps(out))
            else:
                globals()["_CDP_FAILS"] = 0
            _hb_stop.set()
            print(json.dumps({"tick": out}, ensure_ascii=False), flush=True)
            if not args.loop:
                return EXIT_OK
            if args.loop and not args.unfollow_sweep and random.random() < 0.12:
                try:
                    log(json.dumps({"unfollow": unfollow_sweep(args.lane, args.dry_run, limit=6)}))
                except Exception as _e:
                    log(f"unfollow_sweep error {_e}")
            _c = load_config(); _tk = dict(_c.get("tick") or {}); _tk.update((_c.get("lane_tick") or {}).get(args.lane) or {})  # 10/19: was undefined `lane` -> NameError killed loop after 1 tick
            _end = time.time() + random.uniform(float(_tk.get("sleep_min", 150)), float(_tk.get("sleep_max", 420)))
            while time.time() < _end:
                try:
                    Path("/home/operator/claude-stack/data/runtime/hb/engage_" + args.lane).touch()
                except Exception:
                    pass
                time.sleep(max(0.0, min(60.0, _end - time.time())))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
