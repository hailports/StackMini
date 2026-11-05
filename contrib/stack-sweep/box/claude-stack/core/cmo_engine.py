#!/usr/bin/env python3
"""@ducksays CMO Engine + 6 Viral Hacks for x_hailports, x_ima, and x_darkgary.

Implements:
1. Rolling Median Scorer & Winner Attribution (`score_posts_against_median`)
   - Scores every non-boosted post against the lane's rolling 30-post median (`views_x_median`)
   - Writes `/home/operator/socials/cmo_ledger_<lane>.json` and `/home/operator/socials/cmo_weekly_report.md`
2. "Don't Step On A Hot Post" Gate (`is_post_still_hot`)
   - Holds new posts on a lane when the latest post (<6h old) is above 1.8x median views,
     or >120 views in <2h, or climbing at >=35 views/hr.
3. Hack #1 — 275-Char 'Show More' Cliffhanger Fold + Hack #2 — Ego/Rage-Bait Bucket Ladders
   + Hack #5 — 'i had @bot go through...' / 'grokbot pilled' / 'saved me $X/mo' Hooks (`cmo_prompt_spec`)
4. Monthly Content Calendar of Notable Dates (`get_calendar_hook`)
5. Hack #6 — Dark-Mode Org Chart / Fleet Architecture / Tier Cheat Sheet Renderer (`render_cmo_card`)
   - Renders crisp dark-UI system org charts and tier cheat sheets (NEVER fake store receipts!)
     so flagship 'Show more' posts get X's 2.0x photo boost + 10x bookmark conversion.
"""
from __future__ import annotations

import json
import random
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
ROOT = Path("/home/operator/claude-stack")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOCIALS = Path("/home/operator/socials")
CT = ZoneInfo("America/Chicago")

METRICS_PATH = ROOT / "data" / "hustle" / "x_post_metrics.jsonl"
PUBLISHED_PATH = ROOT / "data" / "hustle" / "x_published.jsonl"
LOOP_POSTED_PATH = SOCIALS / "x_loop_posted.jsonl"
BOOSTED_PATH = SOCIALS / "boosted_posts.json"
CALENDAR_PATH = SOCIALS / "cmo_calendar.json"
OUTLIERS_PATH = SOCIALS / "cmo_peer_outliers.json"
REPORT_PATH = SOCIALS / "cmo_weekly_report.md"
CARD_DIR = ROOT / "data" / "hustle" / "rich_cards"

PEER_CREATORS = {
    "x_hailports": {
        "peers": ["ducksays", "levelsio", "swyx", "simonw", "karpathy", "rauchg", "garrytan", "mckaywrigley", "antonosika", "nutlope"],
        "must_follow_promise": "running a 10-bot engineering + ops + cmo fleet on @bot & a 24GB mac mini ($0 cloud bill). i post the real numbers, org charts & what broke.",
    },
    "x_ima": {
        "peers": ["ducksays", "ADHD_Alien", "WaitButWhy", "sweatystartup", "shaanvp", "stephsmithio", "packyM"],
        "must_follow_promise": "cali sober + bipolar/adhd + big-corp swe survival lab. how intentional thc, trauma recovery & @bot keep my brain whole.",
    },
    "x_darkgary": {
        "peers": ["ducksays", "TexAgs", "JoshPateCFB", "BrooksAustinBA", "SECNetwork", "BarstoolTexasAM", "BillyLiucci", "ColeCubelic"],
        "must_follow_promise": "texas a&m, sec football & houston sports numbers, delusion ladders & weekly pain audits. somebody has to keep the receipts.",
    },
}

CALENDAR_DATA = {
    "2026-10": [
        {
            "dates": ["2026-10-25", "2026-10-26"],
            "label": "Week 8 CFB Sunday Audit / Last Week of October Sprint Reset",
            "hooks": {
                "x_hailports": "sunday night fleet audit before the last week of october: what my 10 @bot agents shipped this week vs what i had to fix by hand",
                "x_ima": "sunday night audit before the last week of october: i had @bot go through my 47 open tabs, 12 months of impulse orders, and monday calendar",
                "x_darkgary": "week 8 college football sunday audit: the updated sec fanbase delusion ladder entering november",
            },
        },
        {
            "dates": ["2026-10-27", "2026-10-28", "2026-10-29"],
            "label": "End-of-Month AI & Cloud Bill Audit / CFB Playoff Bracket Projections",
            "hooks": {
                "x_hailports": "end-of-month ai spend audit: how much my local 24gb mac mini + @bot fleet cost in october ($0 api burn) vs cloud token bills",
                "x_ima": "end-of-month adhd tax audit: every subscription, impulse tab, and late task my @bot chief of chaos caught in october",
                "x_darkgary": "late-october cfb playoff projection reality check: comparing sec programs by what they paid vs what they actually won",
            },
        },
        {
            "dates": ["2026-10-30", "2026-10-31", "2026-11-01"],
            "label": "Halloween / November 1 Monthly Benchmarks & Reset",
            "hooks": {
                "x_hailports": "october -> november builder scorecard: 30 days of running specialized @bot agents + local models in production",
                "x_ima": "november 1 reset: my @bot assistant's brutal monthly report on my caffeine intake, screen time, and finished tasks",
                "x_darkgary": "november college football enters the chat: the exact calendar date every sec fanbase stops believing",
            },
        },
    ]
}

OUTLIER_TEMPLATES = {
    "ducksays_bot_fleet": {
        "views_ref": "20,918 - 76,922 views / 703 bookmarks (@ducksays)",
        "structure": (
            "1. Hook line (rough lowercase, concrete number + @bot or grok bot title)\n"
            "2. Blank line, then 2-3 short 1-sentence lines building to a 265-char CLIFFHANGER right before X's 'Show more' fold ('here is the exact org chart and what each bot actually runs:')\n"
            "3. Blank line, then 5-6 '- ' bullet lines with hyper-specific numbers/tasks\n"
            "4. Blank line, then a 1-2 line honest closer + direct reply question.\n"
            "TOTAL LENGTH: 420 to 680 characters (forces 'Show more' click + bookmark)."
        ),
    },
    "ducksays_bucket_ladder": {
        "views_ref": "35,008 views / 357 bookmarks (@ducksays)",
        "structure": (
            "1. Hook question + 1-line cliffhanger before the 265-char 'Show more' fold ('which stage of X are you in right now?\\n\\nhere is the honest ladder:')\n"
            "2. Blank line, then 4-5 ascending tiers ('level 1: ...\\nlevel 2: ...\\nlevel 3: ...\\nlevel 4: ...')\n"
            "3. Blank line, then 1-2 lines on why the middle tier is a trap\n"
            "4. Blank line, then 'i\\'m at <tier>. drop your number.'\n"
            "TOTAL LENGTH: 360 to 620 characters."
        ),
    },
    "ducksays_audit_shocker": {
        "views_ref": "65,863 views / 345 bookmarks (@ducksays)",
        "structure": (
            "1. Hook: 'i had @bot go through 12 months of my <X>. <N> <items>, <$ or hours>.\\n\\nit noticed 3 things that made me sick:' (placed right at char ~250 so 'Show more' cuts right after 'made me sick:')\n"
            "2. Blank line, then numbered findings ('1. ...\\n\\n2. ...\\n\\n3. ...') with concrete numbers\n"
            "3. Blank line, then the 1 rule/script that fixed it + 'have you audited yours yet?'\n"
            "TOTAL LENGTH: 400 to 680 characters."
        ),
    },
}


def _boosted_ids() -> set[str]:
    out = {"2113437323132108899", "2113303186932994224"}
    try:
        d = json.loads(BOOSTED_PATH.read_text())
        for x in (d.get("boosted_ids") or []):
            out.add(str(x))
    except Exception:
        pass
    return out


def _classify_format(text: str, ref: str = "", comm: str = "") -> str:
    t = text or ""
    if "/status/" in t or "_quote" in (ref or ""):
        return "native_quote_post"
    if "@bot" in t.lower() or "grok bot" in t.lower() or "grokbot" in t.lower():
        return "ducksays_bot_fleet"
    if len(t) >= 320 and "\n" in t:
        return "ducksays_longform"
    if re.search(r"\n(-|\d+\.|tier \d|level \d)\s+", t, re.I) and len(t) >= 240:
        return "ducksays_bucket_ladder"
    if comm:
        return "community_post"
    if "_rich_" in (ref or ""):
        return "rich_media"
    return "short_original"


def score_posts_against_median(lane: str) -> dict:
    boosted = _boosted_ids()
    pub_by_id: dict[str, dict] = {}
    if PUBLISHED_PATH.is_file():
        for line in PUBLISHED_PATH.read_text().splitlines():
            try:
                r = json.loads(line)
                sid = str((r.get("live_check") or {}).get("id") or "")
                if sid:
                    pub_by_id[sid] = r
            except Exception:
                pass
    text_by_ref: dict[str, str] = {}
    if LOOP_POSTED_PATH.is_file():
        for line in LOOP_POSTED_PATH.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("ref") and r.get("text"):
                    text_by_ref[str(r["ref"])] = str(r["text"])
            except Exception:
                pass

    by_id: dict[str, dict] = {}
    if METRICS_PATH.is_file():
        for line in METRICS_PATH.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("lane") != lane or r.get("boosted"):
                continue
            url = str(r.get("url") or "")
            m = re.search(r"/status/(\d+)", url)
            sid = m.group(1) if m else str(r.get("id") or "")
            if not sid or sid in boosted:
                continue
            prev = by_id.get(sid)
            if not prev or (r.get("views") or 0) >= (prev.get("views") or 0):
                by_id[sid] = {**r, "id": sid}

    rows = sorted(by_id.values(), key=lambda x: str(x.get("ts") or ""))
    recent_30 = rows[-30:] if rows else []
    views_list = [int(r.get("views") or 0) for r in recent_30 if (r.get("views") or 0) > 0] or [25]
    likes_list = [int(r.get("likes") or 0) for r in recent_30] or [1]
    med_views = float(statistics.median(views_list))
    med_likes = float(statistics.median(likes_list))

    scored = []
    format_stats: dict[str, list[float]] = {}
    for r in rows[-40:]:
        sid = r["id"]
        pub = pub_by_id.get(sid) or {}
        ref = str(r.get("ref") or pub.get("ref") or "")
        comm = str(pub.get("community_name") or "")
        txt = str(r.get("text") or text_by_ref.get(ref) or "")
        fmt = _classify_format(txt, ref, comm)
        v = int(r.get("views") or 0)
        mult = round(v / max(1.0, med_views), 2)
        status = "viral_winner" if mult >= 2.0 else ("winner" if mult >= 1.5 else ("flop" if mult < 0.6 else "normal"))
        format_stats.setdefault(fmt, []).append(mult)
        scored.append({
            "id": sid,
            "url": r.get("url"),
            "ts": r.get("ts"),
            "ref": ref,
            "format": fmt,
            "community": comm or None,
            "views": v,
            "likes": int(r.get("likes") or 0),
            "replies": int(r.get("replies") or 0),
            "reposts": int(r.get("reposts") or 0),
            "views_x_median": mult,
            "verdict": status,
            "snippet": txt[:140],
        })

    fmt_summary = {
        k: {"count": len(vs), "avg_x_median": round(sum(vs) / len(vs), 2), "max_x_median": round(max(vs), 2)}
        for k, vs in format_stats.items() if vs
    }
    ledger = {
        "lane": lane,
        "updated_at_ct": datetime.now(CT).isoformat(timespec="seconds"),
        "rolling_30_median_views": round(med_views, 1),
        "rolling_30_median_likes": round(med_likes, 1),
        "format_performance": fmt_summary,
        "winners_1_5x_plus": [x for x in scored if x["views_x_median"] >= 1.5][-10:],
        "recent_scored": scored[-15:],
    }
    (SOCIALS / f"cmo_ledger_{lane}.json").write_text(json.dumps(ledger, indent=2) + "\n")
    return ledger


def is_post_still_hot(lane: str) -> dict:
    """@ducksays rule: 'tells me when a post is still hot so i don't step on it'."""
    ledger = score_posts_against_median(lane)
    med = max(20.0, float(ledger.get("rolling_30_median_views") or 25.0))
    now_utc = datetime.now(timezone.utc)
    latest_pub = None
    if PUBLISHED_PATH.is_file():
        for line in reversed(PUBLISHED_PATH.read_text().splitlines()[-80:]):
            try:
                r = json.loads(line)
                if r.get("lane") == lane and r.get("verified") is True and (r.get("live_check") or {}).get("id"):
                    latest_pub = r
                    break
            except Exception:
                pass
    if not latest_pub:
        return {"hot": False, "reason": "no_recent_post"}
    sid = str((latest_pub.get("live_check") or {}).get("id") or "")
    try:
        ts = datetime.fromisoformat(str(latest_pub.get("ts") or "").replace("Z", "+00:00"))
        _rf = SOCIALS / "counter_reset_epoch.json"
        if _rf.is_file() and ts.timestamp() < float(json.loads(_rf.read_text()).get("reset_ts", 0)):
            return {"hot": False, "reason": "before_counter_reset_epoch"}
        age_h = (now_utc - ts).total_seconds() / 3600.0
    except Exception:
        age_h = 99.0
    if age_h > 6.0:
        return {"hot": False, "age_h": round(age_h, 2), "reason": "older_than_6h"}

    scored_map = {x["id"]: x for x in (ledger.get("recent_scored") or [])}
    m = scored_map.get(sid) or {}
    views = int(m.get("views") or 0)
    likes = int(m.get("likes") or 0)
    mult = round(views / med, 2) if views else 0.0
    vel = round(views / max(0.25, age_h), 1)
    # 2026-10-26 X Algo Update: every new post gets its fresh-original lift (to ~16th in For You) ONLY in its first 2h
    # and first 200-500 views, and a 2nd post in that window gets a 62% diversity cut.
    # Age alone does not mark a post hot. Under 2.1h still needs views > 0 or likes > 0 (2026-11-05).
    if (age_h <= 2.1 and (views > 0 or likes > 0)) or mult >= 1.8 or (age_h <= 3.5 and vel >= 40.0) or (age_h <= 3.5 and likes >= 5):
        return {
            "hot": True,
            "id": sid,
            "age_h": round(age_h, 2),
            "views": views,
            "median_views": med,
            "views_x_median": mult,
            "velocity_vph": vel,
            "reason": f"post {sid} is HOT ({views} views = {mult}x median in {age_h:.1f}h, {vel} views/h) — holding so we don't step on it",
        }
    return {"hot": False, "id": sid, "age_h": round(age_h, 2), "views": views, "views_x_median": mult}


def get_calendar_hook(lane: str, now: datetime | None = None) -> str:
    now = now or datetime.now(CT)
    CALENDAR_PATH.write_text(json.dumps(CALENDAR_DATA, indent=2) + "\n")
    d_str = now.strftime("%Y-%m-%d")
    ym = now.strftime("%Y-%m")
    for entry in CALENDAR_DATA.get(ym, CALENDAR_DATA.get("2026-10", [])):
        if d_str in entry.get("dates", []):
            return str((entry.get("hooks") or {}).get(lane) or "")
    return str(CALENDAR_DATA["2026-10"][0]["hooks"].get(lane, ""))


def study_peer_creators(lane: str) -> dict:
    payload = {
        "updated_at_ct": datetime.now(CT).isoformat(timespec="seconds"),
        "peers": PEER_CREATORS,
        "winning_templates": OUTLIER_TEMPLATES,
    }
    OUTLIERS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return {
        "lane": lane,
        "peers": PEER_CREATORS.get(lane, {}).get("peers", []),
        "must_follow_promise": PEER_CREATORS.get(lane, {}).get("must_follow_promise", ""),
        "templates": OUTLIER_TEMPLATES,
    }


def render_cmo_card(lane: str, title: str, subtitle: str, rows: list[tuple[str, str, str]], footer: str = "") -> Path | None:
    """2026-10-26 De-Fingerprinted Visual Engine:
    - `x_darkgary`: ALWAYS returns `None` (real sports fans never draw PIL infographic cards!).
    - `x_ima`: Renders a plain borderless iPhone Apple Notes screenshot (`ima_ios_note`).
    - `x_hailports`: Renders technical telemetry/architecture/terminal cards."""
    if lane == "x_darkgary":
        return None
    try:
        from core.intrigue_arcs import pick_visual_subformat, render_lane_visual
        sub = pick_visual_subformat(lane)
        rows4 = [(r[0], r[1], r[2], r[0]) for r in rows]
        return render_lane_visual(lane, sub or "", {"title": title, "subtitle": subtitle, "rows": rows4, "footer": footer})
    except Exception:
        return None
    """Hack #6: Dark-Mode Org Chart / Fleet Architecture / Tier Cheat Sheet Card.
    Looks like a clean developer/creator dark-mode system dashboard (never a fake receipt!)."""
    from PIL import Image, ImageDraw
    from core.rich_cards import font, _save

    W, H = 1200, 180 + 96 * len(rows) + 90
    im = Image.new("RGB", (W, H), (13, 17, 23))
    d = ImageDraw.Draw(im)

    # Subtle border & header bar
    d.rounded_rectangle((24, 24, W - 24, H - 24), radius=22, fill=(22, 27, 34), outline=(48, 54, 61), width=2)
    accent = {"x_hailports": (88, 166, 255), "x_ima": (210, 168, 255), "x_darkgary": (248, 81, 73)}.get(lane, (88, 166, 255))

    # Top status dots + title
    for i, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse((56 + i * 24, 52, 70 + i * 24, 66), fill=col)
    d.text((140, 44), title[:48], font=font("mono", 28), fill=(240, 246, 252))
    d.text((56, 94), subtitle[:75], font=font("mono", 21), fill=accent)
    d.line((56, 136, W - 56, 136), fill=(48, 54, 61), width=2)

    y = 158
    for badge, role, detail in rows[:7]:
        d.rounded_rectangle((56, y, W - 56, y + 80), radius=14, fill=(13, 17, 23), outline=(48, 54, 61), width=1)
        # Badge pill
        bw = max(140, min(250, 24 + len(badge) * 14))
        d.rounded_rectangle((74, y + 18, 74 + bw, y + 62), radius=10, fill=(33, 38, 45), outline=accent, width=1)
        d.text((90, y + 27), badge[:16], font=font("mono", 20), fill=accent)
        # Role + detail
        d.text((94 + bw, y + 16), role[:48], font=font("sans", 24), fill=(240, 246, 252))
        d.text((94 + bw, y + 46), detail[:74], font=font("mono", 19), fill=(139, 148, 158))
        y += 96

    if footer:
        d.text((56, H - 64), footer[:85], font=font("mono", 20), fill=(139, 148, 158))
    return _save(im, f"cmo_{lane}")


def write_weekly_report() -> str:
    ledgers = {ln: score_posts_against_median(ln) for ln in ("x_hailports", "x_ima", "x_darkgary")}
    lines = [
        f"# @ducksays CMO Weekly Report — {datetime.now(CT).strftime('%Y-%m-%d %I:%M %p %Z')}",
        "",
        "## 1. Rolling 30-Post Medians & Top Winners",
    ]
    for ln, ldg in ledgers.items():
        lines.append(f"### `{ln}` (Median Views: `{ldg['rolling_30_median_views']}`, Median Likes: `{ldg['rolling_30_median_likes']}`)")
        lines.append(f"- **Format Performance:** `{json.dumps(ldg['format_performance'])}`")
        for w in (ldg.get("winners_1_5x_plus") or [])[-4:]:
            lines.append(f"  - **Winner ({w['views_x_median']}x median — {w['views']} views, {w['likes']} likes):** `{w['url']}` [{w['format']}] — *{w['snippet']}*")
        lines.append("")
    lines.append("## 2. Active CMO Directives & 6 Viral Hacks")
    lines.append("- **Hack #1 (275-Char 'Show More' Cliffhanger Fold):** 380–680 char rough-lowercase posts with a cliffhanger right at char ~260 so scrollers click 'Show more' (`ContClickDwellTimeWeight = 0.4`).")
    lines.append("- **Hack #2 (Ego/Rage-Bait Bucket Ladders):** 4-tier ladders ending with `'drop your number'` / `'which tier is your team in?'` (`Reply Weight = 20.0`).")
    lines.append("- **Hack #3 (Author Self-Reply + 2-Min Reply-Back):** `x_presence.py` + `x_engage.py` reply to commenters first and add a self-followup on rising posts.")
    lines.append("- **Hack #4 (<15m First-Wave Verified Sniping):** `x_replyguy.py` prioritizes Verified posts `<=15m` old with early velocity.")
    lines.append("- **Hack #5 ('i had @bot go through...' / 'grokbot pilled' Hooks):** High-bookmark `@bot` fleet & audit stories.")
    lines.append("- **Hack #6 (Dark-Mode Org Chart / Tier Cards):** Attached via `render_cmo_card()` for 2.0x photo boost + bookmark multiplier.")
    report_md = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(report_md)
    return report_md


def cmo_prompt_spec(lane: str, kind: str = "") -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) with strictly de-correlated text structures per account:
    - `x_hailports`: lowercase technical engineering voice, numbered lists (`1.`, `2.`, `3.`) or specs.
    - `x_ima`: flowing lowercase personal-essay/diary paragraphs (2-4 short paragraphs, ZERO `- label: explanation` colon-bullet ladders!).
    - `x_darkgary`: Normal Sentence Case (capitalizing first words of sentences and proper nouns like Texas A&M, Kyle Field, SEC, Texans, CJ Stroud, Astros), 1-3 punchy conversational sentences (ZERO bullet lists, ZERO lowercase-only typing, ZERO academic colon headers!)."""
    cal_hook = get_calendar_hook(lane)
    study_peer_creators(lane)
    sys_map = {
        "x_hailports": (
            "You write for @hailports in a rough, lowercase, high-signal systems engineer voice. "
            "You are an anonymous engineer running a specialized AI agent fleet on @bot and a local 24GB M4 Mac mini home server ($0 API burn). "
            "HARD RULES: strictly lowercase, use numbered lists (1., 2., 3.) when listing architecture steps or metrics. "
            "Never name any employer, company, operator, operator, operator, CRM, real person name, IP, or hostname. "
            "No hashtags, no external links, no emojis, no em-dashes, no colon-label openers."
        ),
        "x_ima": (
            "You write for @InesFiends in a raw, literary, darkly funny, lowercase first-person diary/personal-essay voice. "
            "Who you are: a big-corp software engineer who survived a fucked-up childhood and sexual abuse trauma, lives with bipolar disorder and ADHD, "
            "and is an outspoken pro-THC / Cali sober advocate (going Cali sober and replacing alcohol/hard substance chaos with intentional THC changed your life "
            "for the better and let you take back 100% control of all substance use). You also use @bot workflows to survive 47 open tabs and corporate meetings. "
            "HARD RULES: write 2 to 4 flowing lowercase paragraphs. NEVER use bullet lists ('- item: description'), NEVER use numbered ladders ('level 1:'), "
            "NEVER use @hailports's '//' headers, NEVER write 'mg' or specific dosage numbers, no hashtags, no external links, no em-dashes."
        ),
        "x_darkgary": (
            "You write for @DarkrGaryGigsEm in natural, conversational Sentence Case like a real Texas A&M Aggie, SEC football, "
            "and Houston sports (Texans, Astros, Rockets) diehard talking at a tailgate or sports bar. "
            "HARD RULES: Use NORMAL SENTENCE CASE (capitalize the first word of every sentence and proper nouns like Texas A&M, Kyle Field, SEC, Alabama, Texas, LSU, Texans, CJ Stroud, Astros, Rockets, I). "
            "Write 1 to 3 punchy conversational sentences (140 to 260 chars total). NEVER write in all-lowercase, NEVER use bullet lists ('- '), "
            "NEVER use 'tier 1 / tier 2' ladders, NEVER use academic colon headers. No hashtags, no external links, no emojis, no em-dashes."
        ),
    }
    if lane == "x_hailports":
        p = (
            "Write ONE technical X post for @hailports (380 to 620 characters total, lowercase, using numbered steps 1. 2. 3. "
            "separated by blank lines \n\n so X truncates at ~270 chars with a 'Show more' button).\n\n"
            f"CALENDAR ANGLE: {cal_hook}\n\n"
            "REAL GROUNDED FACTS YOU MAY CITE (copy numbers exactly):\n"
            "1. 7 specialized agents + 1 local 35b-a3b moe model (3b active) on a standard $599 24gb m4 mac mini ($0 marginal api burn)\n"
            "2. 40.36m local tokens processed, cutting paid frontier token burn by 91% ($42.50/day down to $3.90/day)\n"
            "3. 2,048-token prefix pinned in kv ram at 88.4% hit rate; 3.5s incremental shard re-bakes\n"
            "4. every job runs in 20s wall-clock slices under a hard timeout after 2 ram freezes\n\n"
            "Open with a sharp 2-line hook before char 250, then numbered items 1. 2. 3. 4., and end with a concrete engineering question.\n"
            'Return JSON only: {"text": "..."}'
        )
    elif lane == "x_ima":
        p = (
            "Write ONE raw, deeply human lowercase personal-essay X post for @InesFiends (360 to 580 characters total, "
            "written as 3 or 4 short flowing paragraphs separated by blank lines \n\n).\n\n"
            "STRICT FORMAT BAN: NO bullet lists ('- '), NO 'level 1 / level 2' ladders, NO 'before vs after:' colon lists, NO '//' headers. "
            "Write like a real woman typing a late-night confession in her own voice.\n\n"
            "ROTATE ACROSS THESE 3 PERSONAL PILLARS (pick one and tell a specific, vivid story):\n"
            "- Pillar A (Pro-THC / Cali Sober Advocacy): how ditching alcohol and party chaos for going cali sober with intentional thc stopped my 3am bipolar shame spirals, gave me 7.5 hours of actual sleep, and let me take back 100% control of what goes into my body.\n"
            "- Pillar B (Surviving Childhood & Sexual Abuse Trauma + Bipolar in Big Tech): the surreal whiplash of sitting calmly on a corporate software engineering zoom call at 10am after surviving a chaotic childhood, sexual abuse trauma, and bipolar mania-vs-crash cycles.\n"
            "- Pillar C (ADHD + @bot Survival): how my adhd brain used to drown in 47 browser tabs and unread slack threads until i quietly wired @bot to triage my workday while i protect my peace.\n\n"
            'End with a genuine, conversational question in the final sentence. Return JSON only: {"text": "..."}'
        )
    else:  # x_darkgary — Normal Sentence Case, 1-3 punchy conversational sentences, NO bullets/ladders!
        p = (
            "Write ONE authentic, funny, high-conviction sports fan X post for @DarkrGaryGigsEm (140 to 250 characters, "
            "1 to 3 conversational sentences in NORMAL SENTENCE CASE).\n\n"
            "REAL GROUNDED FACTS YOU MAY REFERENCE:\n"
            "- Texas A&M lost 31-20 at Alabama (Oct 24) and sits in the bye week before South Carolina\n"
            "- Texas lost twice by 3 points (24-27) and their fans are still doing playoff bracket math\n"
            "- LSU beat Auburn 38-17\n"
            "- Houston Texans and CJ Stroud in the AFC South / Houston Rockets 3-0 start\n\n"
            "HARD FORMAT RULES:\n"
            "- MUST use normal Sentence Case (capitalize first word of every sentence, capitalize 'I', 'Texas A&M', 'Aggies', 'Kyle Field', 'SEC', 'Alabama', 'Texas', 'Texans', 'Houston').\n"
            "- Write 1 to 3 natural sentences ONLY. NO bullet points, NO 'tier 1 / tier 2' lists, NO colon labels.\n"
            "- End with a sharp punchline or a direct question SEC/Houston fans will argue about in the replies.\n"
            'Return JSON only: {"text": "..."}'
        )
    return sys_map[lane], p


if __name__ == "__main__":
    rep = write_weekly_report()
    c1 = render_cmo_card(
        "x_hailports",
        "hailports // @bot + 24gb mini fleet",
        "7 specialized @bot agents + 1 resident local model ($0/mo api spend)",
        [
            ("BOT 01 // CMO", "chief marketing officer (@bot)", "scores drafts vs 30-post median; holds queue when a post is hot"),
            ("BOT 02 // OPS", "20s wall-clock supervisor", "hard timeouts on every job; 0 ram freezes since oct 18"),
            ("BOT 03 // LOCAL", "1.5b resident model (24gb mini)", "replaced 4.7gb 7b swap-thrasher; 0 cloud token burn"),
            ("BOT 04 // EVAL", "3-model free judge panel", "fails closed under 9/10; verifies every number against logs"),
            ("BOT 05 // RECON", "peer & community harvester", "tracks verified outliers + active threads across 10 communities"),
        ],
        "human role: i review the exceptions and press the button.",
    )
    c2 = render_cmo_card(
        "x_ima",
        "ines // adhd survival fleet on @bot",
        "outsourcing executive dysfunction so my brain stops losing to 47 tabs",
        [
            ("BOT 01 // TABS", "4pm browser tab auditor (@bot)", "closes the 39 tabs i opened during a 5-minute slack question"),
            ("BOT 02 // SLEEP", "cali sober wind-down guard", "locks slack at 9pm so i get 7.5 hours of actual sleep instead of a 3am spiral"),
            ("BOT 03 // SYNC", "standup translator", "turns a 45-minute sync into the 1 sentence that actually applies to me"),
            ("BOT 04 // 2AM", "notes app triage", "separates real tasks from 2am side quests i will abandon by sunday"),
        ],
        "status: 1 cold brew in hand. i just press the button.",
    )
    c3 = render_cmo_card(
        "x_darkgary",
        "darkgary // late-october sec delusion ladder",
        "week 8 sunday audit // college station & houston pain index",
        [
            ("TIER 1 // FINE", "casual box-score watchers", "turns off the tv at 4pm and enjoys their saturday evening"),
            ("TIER 2 // COPE", "'quality loss' mathematicians", "explaining how losing 31-20 at alabama is actually a moral victory"),
            ("TIER 3 // SPIRAL", "napkin tiebreaker truther", "calculating 6-way sec tiebreakers after losing twice by 3 points"),
            ("TIER 4 // AGGIE", "maroon Stockholm syndrome", "watching every snap for 4 quarters knowing the 2nd half will hurt"),
        ],
        "current status: tier 4 until further notice. gig em.",
    )
    print("Rendered flagship cards:", c1, c2, c3)
