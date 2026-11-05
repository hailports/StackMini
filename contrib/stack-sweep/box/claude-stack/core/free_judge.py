"""Free-API judge router for SocialsBot score/judge/rank lanes (operator 2026-10-20 12:45: scoring OFF the Mini, onto free
APIs only; no personal Codex/Claude/GLM/Cursor fallback, no Cloudflare 8B/70B, no local :8080).

Rules (SocialsBot): fail closed (no score -> (None, None), caller holds the draft); one judge alone is capped at 8;
a 9+ needs two judges from different providers and the result is the lower score; WEAK judges may screen but never
count toward a 9. 429 -> honor cooldown (Retry-After or 65 s) per model; models spread round-robin so no single
free quota carries the load. Ledger: data/hustle/logs/free_judge.jsonl (judge, status, ms; never prompt text or keys).
Rollback: data/runtime/free_judge.json {"enabled": false} -> caption_brain falls back to its previous judge path.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/operator/claude-stack")
CFG = ROOT / "data" / "runtime" / "free_judge.json"
COLD = ROOT / "data" / "runtime" / "nim_cold.json"  # shared with caption_brain (same keys for gemini-2.5-flash/groq)
LEDGER = ROOT / "data" / "hustle" / "logs" / "free_judge.jsonl"
RR = ROOT / "data" / "runtime" / "free_judge_rr.txt"

# Dead 10/20 12:55 (probed): gemini-2.5-flash-lite 404 retired, cerebras gpt-oss-120b/qwen-3.8-27b 402 (not free),
# nim glm-5.3-flash 30 s timeouts. Cloudflare 8B/70B excluded (10/17 agreement 21%->2%).
# (name, family, kind, model). Order = default preference; selection rotates over the healthy ones.
JUDGES = (
    ("gemini-2.5-flash", "google", "gemini", "gemini-2.5-flash"),
    ("gemini-3.5-flash", "google", "gemini", "gemini-3.5-flash"),
    ("gemini-3.8-flash", "google", "gemini", "gemini-3.8-flash"),
    ("gemini-flash-lite-latest", "google", "gemini", "gemini-flash-lite-latest"),
    ("gemini-3.1-flash-lite", "google", "gemini", "gemini-3.1-flash-lite"),
    ("groq:qwen3.8-27b", "groq", "openai", "qwen/qwen3.8-27b"),
    ("groq:gpt-oss-120b", "groq", "openai", "openai/gpt-oss-120b"),
    ("nim:nemotron", "nim", "openai", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    # 10/20 16:46 capacity pass (operator approved 16:30): separate per-model free quotas on the same keys.
    ("gemini-3-flash-preview", "google", "gemini", "gemini-3-flash-preview"),
    ("gemini-3.5-flash-lite", "google", "gemini", "gemini-3.5-flash-lite"),
    ("gemma-4-26b", "google", "gemma", "gemma-4-26b-a4b-it"),
    ("gemma-4-31b", "google", "gemma", "gemma-4-31b-it"),
    ("groq:gpt-oss-20b", "groq", "openai", "openai/gpt-oss-20b"),
    ("gemini-3.6-flash", "google", "gemini", "gemini-3.6-flash"),
    ("gemini-3.1-flash-lite-preview", "google", "gemini", "gemini-3.1-flash-lite-preview"),
    # 2026-11-02 operator [pt279u]: OpenRouter FREE tier (probed 12:50 CT). X lanes (hailports/darkgary) + LinkedIn only,
    # never Ines; nemotron never on DarkGary (same rule as nim:nemotron). family = model maker, so a 9 still needs
    # two different makers (or:gemma + google gemma cannot pair).
    ("or:nemotron-3-ultra", "nvidia", "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("or:nemotron-3.5-super", "nvidia", "openrouter", "nvidia/nemotron-3.5-super-vl:free"),
    ("or:gemma-4-31b", "google", "openrouter", "google/gemma-4-31b-it:free"),
)
# 2026-11-02 operator [pt279u] 12:50 CT: OpenRouter PAID judges go FIRST on X lanes (hailports/darkgary; never LinkedIn,
# never Ines) until the credit is burned (~$3/day CT, live balance, $0.50 floor), then free-only forever
# (core/openrouter_budget EXHAUSTED flag). Still used as overflow too. $/MTok in/out.
PAID = (  # 2026-11-02 12:57 CT operator [pt281u] "do it all": strong paid judges (probed 13:0x CT; $/MTok in/out, tier)
    # strong tier: ORIGINALS lanes (posts/threads/articles/viral); two different makers -> a 9 is two independent 9s
    ("orp:claude-sonnet-5.5", "anthropic", "openrouter_paid", "anthropic/claude-sonnet-5.5", 2.00, 10.00, "strong"),
    ("orp:deepseek-v4-pro", "deepseek", "openrouter_paid", "deepseek/deepseek-v4-pro-0813", 0.11, 4.00, "strong"),
    # core tier: replies / classify / human-sound checks, and fallback for originals
    ("orp:gpt-5.6-luna", "openai", "openrouter_paid", "openai/gpt-5.6-luna", 0.20, 1.20, "core"),
    ("orp:claude-haiku-5.5", "anthropic", "openrouter_paid", "anthropic/claude-haiku-5.5", 0.10, 0.50, "core"),
    ("orp:gemini-3.8-flash", "google", "openrouter_paid", "google/gemini-3.8-flash", 0.75, 3.75, "core"),
)
_NON_ORIGINAL = ("reply", "classify", "human", "short_react", "batch_screen", "pick", "pairwise", "tournament", "screen")


def _is_original(lane: str) -> bool:
    low = (lane or "").lower()
    return not any(t in low for t in _NON_ORIGINAL)


_PAID_BY = {j[0]: j for j in PAID}
_OR_URL = "https://openrouter.ai/api/v1/chat/completions"


def or_scope(lane: str) -> str:
    """'x' (hailports/darkgary judge lanes), 'li' (LinkedIn: free only) or '' (Ines / unresolved: no OpenRouter)."""
    low = (lane or "").lower()
    if "linkedin" in low:
        return "li"
    if any(t in low for t in ("ines", "x_ima", "comedy_judge", "fit_ima")):
        return ""
    if any(t in low for t in ("hailports", ":hp", "fit_hai", "darkgary", ":dg", "fit_dar")):
        return "x"
    return "x" if rb_active() in ("x_hailports", "x_darkgary") else ""


def _paid_first(lane: str, exclude=None, cold=None, now=None) -> list:
    """Paid OpenRouter judges ahead of the free pool on X lanes while credit lasts (budget-checked per call too)."""
    try:
        if or_scope(lane) != "x" or not os.environ.get("OPENROUTER_API_KEY") or not _cfg().get("openrouter_paid", True):
            return []
        from core import openrouter_budget as _OB
        if not _OB.allow()[0]:
            return []
        cold = cold if cold is not None else _cold()
        now = now or time.time()
        off = set(_cfg().get("dead") or [])
        ok = [j for j in PAID if j[0] not in off and not (exclude and j[0] in exclude) and not _is_cold(j[0], cold, now)]
        k = int(now) // 7
        strong = [j for j in ok if j[6] == "strong"]
        core = [j for j in ok if j[6] == "core"]
        if core:
            core = core[k % len(core):] + core[:k % len(core)]
        return (strong + core) if _is_original(lane) else core
    except Exception:
        return []


def _is_dg(lane: str) -> bool:
    low = (lane or "").lower()
    return any(t in low for t in ("darkgary", ":dg", "fit_dar")) or rb_active() == "x_darkgary"


def _or_ok(j: tuple, lane: str) -> bool:
    if j[2] != "openrouter":
        return True
    sc = or_scope(lane)
    if not sc:
        return False
    return not ("nemotron" in j[0] and _is_dg(lane))
_BASE = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY"),
    "nim": ("https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_NIM_API_KEY"),
}
_SC = re.compile(r'("score"\s*:\s*)(\d+)')
TIMEOUT_S = 30
GEMMA_TIMEOUT_S = 42  # 18:52: was 120; must fit inside caller budgets with patient waits


def _cfg() -> dict:
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _keys() -> None:
    try:
        from core.social_llm import _load_free_social_env
        _load_free_social_env()
    except Exception:
        pass
    if not os.environ.get("NVIDIA_NIM_API_KEY"):
        try:
            for line in (ROOT / ".env").read_text().splitlines():
                if line.startswith("NVIDIA_NIM_API_KEY="):
                    os.environ["NVIDIA_NIM_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass


def _cold() -> dict:
    try:
        return json.loads(COLD.read_text())
    except Exception:
        return {}


def _go_cold(name: str, secs: float, shared: bool = False) -> None:
    if not shared:
        name = "judge:" + name
    try:
        c = _cold()
        c[name] = max(float(c.get(name, 0) or 0), time.time() + secs)
        tmp = COLD.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(c))
        os.replace(tmp, COLD)
    except Exception:
        pass


def _ledger(row: dict) -> None:
    try:
        row["ts"] = datetime.now().isoformat(timespec="seconds")
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _post(url: str, body: dict, headers: dict, timeout: float | None = None) -> dict:
    from core.no_mini_guard import refuse_mini  # 2026-11-04 [t325u]: never judge on the Mini
    refuse_mini(url, "free_judge")
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                  "User-Agent": "curl/8", **headers})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
        return json.load(r)


_OR_LANE: dict = {"lane": ""}  # spend-ledger tag only (set by _judge right before the call)


def _call_one(j: tuple, prompt: str, system: str | None, max_tokens: int) -> str:
    name, fam, kind, model = j[:4]
    mt = max(256, min(int(max_tokens or 300), 1024))
    if kind == "gemma":  # 10/24: thinkingLevel minimal cuts gemma-4-26b/31b latency from 40s -> 1.5-10s (14k RPD free)
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("nokey")
        body = {"contents": [{"role": "user", "parts": [{"text": ((system + "\n\n") if system else "") + prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800,
                                     "thinkingConfig": {"thinkingLevel": "minimal"}}}
        d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                  {"x-goog-api-key": key}, timeout=GEMMA_TIMEOUT_S)
        parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if kind == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("nokey")
        gc = {"temperature": 0.2, "maxOutputTokens": mt + 2048}
        if "2.5" in model:
            gc["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            gc["thinkingConfig"] = {"thinkingLevel": "low"}
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                  {"x-goog-api-key": key})
        parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if kind in ("openrouter", "openrouter_paid"):
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("nokey")
        from core import openrouter_budget as _OB
        tier = "paid" if kind == "openrouter_paid" else "free"
        if tier == "paid":
            ok, why = _OB.allow()
            if not ok:
                raise RuntimeError("cap:" + why)
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = {"model": model, "messages": msgs, "temperature": 0.2, "usage": {"include": True},
                "max_tokens": mt + (700 if "nemotron" in model else 0), "reasoning": {"effort": "low", "exclude": True}}
        try:
            d = _post(_OR_URL, body, {"Authorization": "Bearer " + key, "X-Title": "socials-judge"})
        except urllib.error.HTTPError as e:
            _OB.record(model, tier, _OR_LANE.get("lane", ""), {}, status=str(e.code), est=0.0)
            raise
        u = d.get("usage") or {}
        est = None
        if tier == "paid" and u.get("cost") is None:
            pj = _PAID_BY.get(name)
            est = ((u.get("prompt_tokens") or 0) * pj[4] + (u.get("completion_tokens") or 0) * pj[5]) / 1e6 if pj else 0.0
        _OB.record(model, tier, _OR_LANE.get("lane", ""), u, est=est)
        out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return re.sub(r"(?is)<think>.*?</think>", "", out).strip()
    url, kname = _BASE[fam]
    key = os.environ.get(kname, "")
    if not key:
        raise RuntimeError("nokey")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "max_tokens": mt + (600 if "gpt-oss" in model else 0), "temperature": 0.2}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    if fam == "nim":
        body["chat_template_kwargs"] = {"thinking": False, "enable_thinking": False}
    elif "qwen" in model:
        body["reasoning_effort"] = "none"
    d = _post(url, body, {"Authorization": "Bearer " + key})
    out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return re.sub(r"(?is)<think>.*?</think>", "", out).strip()


async def _in_daemon(fn, *args):
    """19:44: run a blocking HTTP call in a DAEMON thread. asyncio.to_thread uses the loop's default executor, and
    asyncio.run() (brain_complete_sync) waits for that executor at shutdown, so one slow gemma read (socket timeout
    resets per recv) blocked whole supply runs for 350 s after wait_for had already given up."""
    import threading
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _deliver(cb):
        # 2026-10-22: the caller's wait_for can time out and asyncio.run() closes the loop while this daemon thread
        # is still in its HTTP read; delivering then raised "Event loop is closed" (50+ tracebacks/hr in engage logs).
        # A closed loop means nobody is waiting any more: drop the late result quietly.
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(cb)
        except RuntimeError:
            pass

    def run():
        try:
            res = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            _deliver(lambda e=exc: fut.done() or fut.set_exception(e))
            return
        _deliver(lambda: fut.done() or fut.set_result(res))

    threading.Thread(target=run, daemon=True).start()
    return await fut


def _strip(text: str) -> str:
    t = re.sub(r"(?is)<think>.*?</think>", "", text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    return t


def _json_clean(out: str, prompt: str) -> str:
    """18:45: judges that think inline (gemma) or wrap JSON in prose made callers' greedy {.*} regex fail ->
    'judge-unparseable' -> scored 0. When the prompt asks for JSON, return just the LAST parseable JSON object
    (thinking stripped). No parseable object -> "" so the router tries another judge instead of a silent 0."""
    if "json" not in (prompt or "").lower():
        return out
    dec = json.JSONDecoder()
    best = None
    i = out.find("{")
    while i != -1:
        try:
            obj, _end = dec.raw_decode(out, i)
            if isinstance(obj, dict) and obj:
                best = obj
        except Exception:
            pass
        i = out.find("{", i + 1)
    return json.dumps(best, ensure_ascii=False) if best is not None else ""


def score_of(text: str):
    m = _SC.search(text or "")
    return int(m.group(2)) if m else None


def set_score(text: str, v: int) -> str:
    return _SC.sub(lambda m: m.group(1) + str(v), text, count=1)


# 10/20 12:52 calibration on 20 codex-scored samples (10 known 9s): gemini-3.5-flash scored the 9s 9/10/10/10/9/7;
# nemotron, groq gpt-oss-120b and gemini-flash-lite-latest scored them 3-8 (and gpt-oss gave a 7 a 0). Those three are WEAK:
# they are WEAK and are not called (a weak-only score would have to be held anyway; as a 2nd judge they can't make a 9).
_SHARED_429 = ("gemini-2.5-flash",)  # same free quota as caption_brain's writer key; honor its 429 cooldown too
WEAK_DEFAULT = ("nim:nemotron", "groq:gpt-oss-120b", "gemini-flash-lite-latest",
                # 10/20 16:46 additions stay weak until calibrated strong (config "weak" list overrides)
                "gemini-3.5-flash-lite", "groq:gpt-oss-20b", "gemma-4-26b", "gemma-4-31b", "gemini-3-flash-preview")


def _weak() -> set:
    w = _cfg().get("weak")
    return set(w) if isinstance(w, list) else set(WEAK_DEFAULT)


# 10/20 16:46: LITE judges answer non-score lanes only (reply_guard classify: one word, no quality floor), so the
# strong judges' small free quotas go to originals. Lite answers can never count toward a 9 (they're also in weak).
LITE_DEFAULT = ("gemini-3.5-flash-lite", "gemini-flash-lite-latest", "groq:gpt-oss-20b", "nim:nemotron")
LITE_LANES_DEFAULT = ("classify",)


def _lite() -> list:
    w = _cfg().get("lite")
    return list(w) if isinstance(w, list) else list(LITE_DEFAULT)


def is_lite_lane(lane: str) -> bool:
    toks = _cfg().get("lite_lanes")
    toks = toks if isinstance(toks, list) else LITE_LANES_DEFAULT
    return any(t and t in (lane or "") for t in toks)


def _is_cold(name: str, cold: dict, now: float) -> bool:
    return max(cold.get("judge:" + name, 0), cold.get(name, 0) if name in _SHARED_429 else 0) > now


def _healthy_lite() -> list:
    off = set(_cfg().get("dead") or [])
    cold = _cold()
    now = time.time()
    by = {j[0]: j for j in JUDGES}
    out = [by[n] for n in _lite() if n in by and n not in off and not _is_cold(n, cold, now)]
    t1 = [j for j in out if j[1] == "google"]
    t2 = [j for j in out if j[1] != "google"]
    if len(t1) > 1:
        k = int(now) % len(t1)
        t1 = t1[k:] + t1[:k]
    return t1 + t2


def _healthy(lane: str, exclude: set | None = None, prefer_not_fam: str = "") -> list:
    cfg = _cfg()
    off = set(cfg.get("dead") or [])
    cold = _cold()
    now = time.time()
    weak = _weak()
    out = []
    for j in JUDGES:
        if j[0] in off or j[0] in weak or (exclude and j[0] in exclude):
            continue  # 12:58: weak-only scores are HELD (SocialsBot rule), so weak judges are not called at all
        if _is_cold(j[0], cold, now):
            continue
        if "nemotron" in j[0] and "x_ima" in (lane or ""):  # 10/19: miscalibrated on Ines comedy/hook
            continue
        if not _or_ok(j, lane):  # 2026-11-02 [pt279u]: OpenRouter free only on hailports/darkgary/LinkedIn lanes
            continue
        out.append(j)
    if not out:
        return out
    try:
        n = int(RR.read_text() or 0)
    except Exception:
        n = 0
    try:
        RR.write_text(str(n + 1))
    except Exception:
        pass

    def rot(xs):
        if not xs:
            return xs
        k = n % len(xs)
        return xs[k:] + xs[:k]
    strong = [j for j in out if j[0] not in weak]
    lo = [j for j in out if j[0] in weak]
    paid = _paid_first(lane, exclude, cold, now)
    if prefer_not_fam:
        strong = [j for j in strong if j[1] != prefer_not_fam] + [j for j in strong if j[1] == prefer_not_fam]
        paid = [j for j in paid if j[1] != prefer_not_fam]
        return paid + strong + rot(lo)
    return paid + rot(strong) + rot(lo)


CACHE_DIR = ROOT / "data" / "runtime" / "fj_cache"
_RETRY_RX = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)
_DELAY_RX = re.compile(r'"retryDelay"\s*:\s*"([\d.]+)s"')


def _ckey(j: tuple, prompt: str, system: str | None, max_tokens: int) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([j[0], system or "", prompt, int(max_tokens or 0)]).encode()).hexdigest()


def _cache_get(k: str) -> str:
    """10/20 16:46: the same draft re-judged (retries, re-queues, batch + single) reuses each judge's answer."""
    try:
        f = CACHE_DIR / k[:2] / k
        if time.time() - f.stat().st_mtime < float(_cfg().get("cache_ttl_s", 21600)):
            return f.read_text()
    except Exception:
        pass
    return ""


def _cache_put(k: str, out: str) -> None:
    try:
        d = CACHE_DIR / k[:2]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (k + ".tmp%d" % os.getpid())
        tmp.write_text(out)
        os.replace(tmp, d / k)
    except Exception:
        pass


def _quota_wait(exc) -> float:
    """Seconds the provider says to wait. Gemini free flash models have a 20/day cap per model; its 429 says
    'retry in 9h25m' -- benching those for 65 s just burned calls and held drafts all afternoon."""
    try:
        body = exc.read().decode("utf-8", "replace")[:4000]
    except Exception:
        return 0.0
    m = _DELAY_RX.search(body)
    if m:
        return float(m.group(1))
    m = _RETRY_RX.search(body)
    if m and any(m.groups()):
        h, mi, se = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    return 0.0


async def _judge(j: tuple, prompt: str, system: str | None, max_tokens: int, lane: str) -> str:
    t0 = time.time()
    ck = _ckey(j, prompt, system, max_tokens)
    hit = _cache_get(ck)
    if hit and score_of(hit) is None:  # only non-score answers (classify/pick/screen) are reused
        _ledger({"lane": (lane or "")[:60], "judge": j[0], "status": "cache", "ms": 0, "score": score_of(hit)})
        return hit
    status = "ok"
    out = ""
    _OR_LANE["lane"] = lane or ""
    try:
        to = (GEMMA_TIMEOUT_S if j[2] == "gemma" else TIMEOUT_S) + 5
        try:
            out = _strip(await asyncio.wait_for(_in_daemon(_call_one, j, prompt, system, max_tokens), to))
        except urllib.error.HTTPError as e5:
            if j[2] != "gemma" or e5.code not in (500, 502, 503):
                raise
            await asyncio.sleep(4)  # 18:37: gemma 5xx are transient; one retry before benching
            out = _strip(await asyncio.wait_for(_in_daemon(_call_one, j, prompt, system, max_tokens), to))
        if out:
            raw_out, out = out, _json_clean(out, (system or "") + prompt)
            if not out:
                status = "unparsed"  # not a bench-worthy failure: next judge is tried right away
        elif not out:
            status = "empty"
            _go_cold(j[0], 120)
    except urllib.error.HTTPError as exc:
        status = str(exc.code)
        ra = 0.0
        try:
            ra = float(exc.headers.get("Retry-After") or 0)
        except Exception:
            ra = 0.0
        if exc.code == 429:
            ra = max(ra, min(_quota_wait(exc), 86400.0))
            if ra > 600:
                status = "429-daily"
        floor = 12.0 if ((j[1] == "groq" or j[2] == "gemma") and ra <= 60) else 65.0  # groq/gemma short RPM cap
        secs = max(ra, floor) if exc.code == 429 else (900 if exc.code in (401, 402, 403, 404) else (30 if exc.code in (500, 502, 503) else 120))  # 17:08: gemma/flash-lite 5xx are transient
        _go_cold(j[0], secs)
        if exc.code == 429 and j[0] in _SHARED_429:
            _go_cold(j[0], secs, shared=True)
    except Exception as exc:
        status = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in str(exc) else \
            ("nokey" if str(exc) == "nokey" else (str(exc)[:40] if str(exc).startswith("cap:") else "err:" + type(exc).__name__))
        if status != "nokey" and not status.startswith("cap:"):  # 2026-10-24: missing env in one process must never bench judges globally in nim_cold.json
            _go_cold(j[0], 90 if status == "timeout" else 120)  # 17:08: was 300; flash-lite timeouts are transient
    _ledger({"lane": (lane or "")[:60], "judge": j[0], "status": status, "ms": int((time.time() - t0) * 1000),
             "score": score_of(out) if out else None})
    # Numeric scores are NEVER cached: Ines/hailports 2-of-3 votes re-ask the same prompt and need independent
    # samples; a cached 9 would turn one vote into three and quietly relax the rule.
    if status == "ok" and score_of(out) is None:
        _cache_put(ck, out)
    return out if status == "ok" else ""


_NO_WAIT = ("classify", "batch_screen")
_SHORT_WAIT = ("reply", "human")  # 18:45: replies wait briefly (60 s) instead of failing closed -> "judge-unparseable"


def _patient(lane: str) -> bool:
    """10/20 18:37 patient pairing: original-post lanes wait for a benched strong judge instead of failing closed
    instantly (supply runs ahead of posting). Replies/classify/screens stay instant."""
    low = (lane or "").lower()
    return bool(_cfg().get("patient", True)) and not any(t in low for t in _NO_WAIT)


async def _wait_healthy(lane: str, exclude: set | None, prefer_not_fam: str, budget: float) -> list:
    import random
    end = time.time() + budget
    while True:
        hs = _healthy(lane, exclude=exclude, prefer_not_fam=prefer_not_fam)
        if hs or time.time() >= end:
            return hs
        await asyncio.sleep(min(8.0 + random.random() * 4, max(0.5, end - time.time())))


def _ov_single(lane: str, s1, judge: str, prompt: str) -> bool:
    """TEMPORARY 10/21 override: one strong judge's >= 8 counts on override lanes' originals (never on failure)."""
    try:
        from core import judge_override as _jo
        if s1 is None or s1 < _jo.min_single() or not _jo.active(lane):
            return False
        _jo.record_pass(lane, judge, [s1], prompt)
        _ledger({"lane": (lane or "")[:60], "judge": judge, "status": "override_pass", "ms": 0, "score": s1})
        return True
    except Exception:
        return False


def _ov_pair(lane: str, lo, judges: str, scores: list, prompt: str) -> bool:
    try:
        from core import judge_override as _jo
        if lo < _jo.min_single() or not _jo.pair8_counts() or not _jo.active(lane):
            return False
        _jo.record_pass(lane, judges, scores, prompt)
        _ledger({"lane": (lane or "")[:60], "judge": judges, "status": "override_pass_pair", "ms": 0, "score": lo})
        return True
    except Exception:
        return False


# ── 2026-10-22 10:44 CT operator: REPLY-BACK lite judges (replies on our OWN posts only, all three X lanes) ──
RB_FLAG = ROOT / "data" / "runtime" / "reply_back_lite_judges.json"
RB_LANES = ("x_ima", "x_darkgary", "x_hailports")
_RB = {"lane": ""}  # set only while x_engage handles one via=reply_back reply (single-threaded engage process)


def rb_cfg() -> dict:
    try:
        d = json.loads(RB_FLAG.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def rb_active() -> str:
    lane = _RB.get("lane") or ""
    return lane if lane in RB_LANES and bool(rb_cfg().get("enabled")) else ""


class reply_back_mode:
    def __init__(self, lane: str):
        self.lane = lane

    def __enter__(self):
        _RB["lane"] = self.lane
        return self

    def __exit__(self, *exc):
        _RB["lane"] = ""
        return False


def _rb_candidates(engage_lane: str) -> tuple[list, list]:
    cfg = _cfg()
    off = set(cfg.get("dead") or [])
    cold = _cold()
    now = time.time()
    weak = _weak()
    ok = [j for j in JUDGES if j[0] not in off and not _is_cold(j[0], cold, now)
          and "gpt-oss" not in j[0]
          and not ("nemotron" in j[0] and engage_lane in ("x_ima", "x_darkgary", "unknown", "human"))
          and not (j[2] == "openrouter" and engage_lane not in ("x_hailports", "x_darkgary"))]
    strong = [j for j in ok if j[0] not in weak]
    if engage_lane in ("x_hailports", "x_darkgary"):  # 2026-11-02 [pt279u]: paid first while credit lasts
        strong = _paid_first(engage_lane + ":rb_reply", None, cold, now) + strong
    allowed = set(_lite())  # only operator's approved lite set (gemma-4-26b, 3.5/latest flash-lite, gpt-oss-20b, nemotron)
    lite_all = [j for j in ok if j[0] in weak and j[0] in allowed]
    try:
        k = int(RR.read_text() or 0)
        RR.write_text(str(k + 1))
    except Exception:
        k = int(now)
    if strong:
        fast_s = [j for j in strong if j[2] != "gemma"]
        slow_s = [j for j in strong if j[2] == "gemma"]
        if fast_s:
            fast_s = fast_s[k % len(fast_s):] + fast_s[:k % len(fast_s)]
        strong = fast_s + slow_s
    # 2026-10-24: try fast calibrated Gemini + gpt-oss-120b + gemma-4-26b first (rotated), before gpt-oss-20b / nemotron
    tier1 = [j for j in lite_all if j[1] == "google"]
    tier2 = [j for j in lite_all if j not in tier1]
    if tier1:
        tier1 = tier1[k % len(tier1):] + tier1[:k % len(tier1)]
    lite = tier1 + tier2
    return strong, lite


async def _rb_judge(prompt: str, system, max_tokens: int, lane: str, engage_lane: str, c: dict | None = None,
                    tag: str = "rb"):
    _keys()
    c = c if c is not None else rb_cfg()
    deadline = time.time() + float(c.get("total_budget_s", 120))
    sc_cap = float(c.get("strong_call_s", 25))
    lc_cap = float(c.get("lite_call_s", 20))
    for _pass in range(2):
        strong, lite = _rb_candidates(engage_lane)
        if not strong and not lite and time.time() + 10 < deadline:
            await asyncio.sleep(7.0)
            strong, lite = _rb_candidates(engage_lane)
        fast_s = [(j, sc_cap) for j in strong if j[2] != "gemma"][:int(c.get("strong_tries", 2))]
        slow_s = [(j, 18.0) for j in strong if j[2] == "gemma"][:max(0, int(c.get("strong_tries", 2)) - len(fast_s))]
        order = fast_s + [(j, lc_cap) for j in lite] + slow_s
        for j, cap in order:
            left = deadline - time.time()
            if left < 4:
                break
            try:
                t = await asyncio.wait_for(_judge(j, prompt, system, max_tokens, lane), min(cap, left))
            except (asyncio.TimeoutError, TimeoutError):
                _go_cold(j[0], 45)
                _ledger({"lane": (lane or "")[:60], "judge": j[0], "status": "rb_timeout", "ms": int(min(cap, left) * 1000),
                         "score": None})
                continue
            sc = score_of(t) if t else None
            if sc is None or not 0 <= sc <= 10:  # unreadable/failed judges are never votes
                continue
            sc2 = min(sc, int(c.get("cap_score", 8)))
            _ledger({"lane": (lane or "")[:60], "judge": j[0], "status": f"{tag}_ok:" + engage_lane, "ms": 0, "score": sc2})
            return set_score(t, sc2), (f"free-{tag}:" if j in strong else f"free-{tag}-lite:") + j[0]
        if _pass == 0 and time.time() + 12 < deadline:
            await asyncio.sleep(6.0)
    if engage_lane in ("x_hailports", "x_darkgary"):  # 2026-11-02 [pt279u]: capped paid overflow, still capped at cap_score
        _po = await _paid_overflow(prompt, system, max_tokens, engage_lane + ":" + (lane or ""), set())
        if _po and score_of(_po[1]) is not None and 0 <= score_of(_po[1]) <= 10:
            sc2 = min(score_of(_po[1]), int(c.get("cap_score", 8)))
            _ledger({"lane": (lane or "")[:60], "judge": _po[0][0], "status": f"{tag}_ok:" + engage_lane, "ms": 0, "score": sc2})
            return set_score(_po[1], sc2), f"paid-{tag}:" + _po[0][0]
    _ledger({"lane": (lane or "")[:60], "judge": "-", "status": f"{tag}_held_no_judge:" + engage_lane, "ms": 0, "score": None})
    return None, None


# ── 2026-10-22 18:18 CT operator: lite/backup judges for ORIGINALS (viral + bot-sound; overall score only when no strong
# judge answered). Scoped to the originals processes (x-loop, supply, rich supply); auto-off at expires_at.
OL_FLAG = ROOT / "data" / "runtime" / "originals_lite_judges.json"


def ol_cfg() -> dict:
    try:
        d = json.loads(OL_FLAG.read_text())
    except Exception:
        return {}
    if not isinstance(d, dict) or not d.get("enabled"):
        return {}
    try:
        from datetime import datetime as _dt
        if not d.get("permanent") and time.time() >= _dt.fromisoformat(str(d.get("expires_at"))).timestamp():
            return {}
    except Exception:
        return {}
    import sys as _sys
    me = Path(_sys.argv[0] or "").name
    return d if me in set(d.get("procs") or []) else {}


def ol_lane(lane: str) -> str:
    low = (lane or "").lower()
    if "ines" in low or "x_ima" in low:
        return "x_ima"
    if ":hp" in low or "hailports" in low:
        return "x_hailports"
    if ":dg" in low or "darkgary" in low:
        return "x_darkgary"
    return rb_active() or "unknown"


async def free_judge(prompt: str, system: str | None, max_tokens: int, lane: str):
    _keys()
    if personal_primary(lane, prompt, system):  # 2026-11-04 [pt343u]: personal Codex/Claude first, box only
        _pt, _pp = await _personal_core(prompt, system, max_tokens, lane)
        if _pt is not None:
            return _pt, _pp
    _low_lane = (lane or "").lower()
    _olc = ol_cfg() if ('"score"' in ((system or "") + (prompt or "")).lower()
                        and not is_lite_lane(lane) and "batch_screen" not in _low_lane) else {}
    if _olc and ("viral_judge" in _low_lane or "human_judge" in _low_lane or "hook_judge" in _low_lane):
        return await _rb_judge(prompt, system, max_tokens, lane, ("human" if "human_judge" in _low_lane else ol_lane(lane)), _olc, "ol")
    t, p = await _free_judge_core(prompt, system, max_tokens, lane)
    if t is None and ol_cfg() and not _olc:  # 2026-10-24: non-score judge calls (batch_screen, is_postable, pairwise) lite fallback when strong cold
        for j in _healthy_lite()[:3]:
            if "nemotron" in j[0] and "x_ima" in _low_lane:
                continue
            t2 = await _judge(j, prompt, system, max_tokens, lane)
            if t2 and ("[" in t2 or "{" in t2):
                return t2, "free-lite-aux:" + j[0]
    if t is None and _olc:  # strong judges gave no valid answer: lite fallback, capped 8
        t, p = await _rb_judge(prompt, system, max_tokens, lane, ol_lane(lane), {**_olc, "strong_tries": 0}, "ol")
        t, p = _ol_override8(t, p, lane)
    return t, p


def _ol_override8(t, p, lane):
    """2026-10-22 18:51 CT operator: on an override lane (Ines until 02:00, hailports until 02:30, per judge_override
    lane_expires) a backup-judge 8 counts like a strong 8 (9/8/8 passes). Needs judge_override.json lite_8_counts."""
    try:
        from core import judge_override as _jo
        sc = score_of(t) if t else None
        if sc is not None and sc >= 8 and _jo.active(lane) and (_jo._flag(_jo.FLAG) or {}).get("lite_8_counts"):
            _ledger({"lane": (lane or "")[:60], "judge": str(p), "status": "ol_override_pass", "ms": 0, "score": 9})
            return set_score(t, 9), str(p) + "+override8"
    except Exception:
        pass
    return t, p


def paid_ok(lane: str) -> bool:
    try:
        return or_scope(lane) == "x" and bool(_cfg().get("openrouter_paid", True)) and bool(os.environ.get("OPENROUTER_API_KEY"))
    except Exception:
        return False


async def _paid_overflow(prompt: str, system, max_tokens: int, lane: str, exclude: set, not_fam: str = ""):
    """Only after the free pool failed. Same _judge path (429 bench, rotation, ledger). Returns (judge, text) or None."""
    if not paid_ok(lane):
        return None
    from core import openrouter_budget as _OB
    ok, why = _OB.allow()
    if not ok:
        _ledger({"lane": (lane or "")[:60], "judge": "orp", "status": "paid_" + why, "ms": 0, "score": None})
        return None
    cold, now = _cold(), time.time()
    js = [j for j in PAID if j[0] not in exclude and j[1] != not_fam and not _is_cold(j[0], cold, now)]
    try:
        k = int(RR.read_text() or 0)
    except Exception:
        k = int(now)
    if js:
        js = js[k % len(js):] + js[:k % len(js)]
    for j in js:
        t = await _judge(j, prompt, system, max_tokens, lane)
        if t and (score_of(t) is not None or "{" in t or "[" in t):
            return j, t
    return None


async def _free_judge_core(prompt: str, system: str | None, max_tokens: int, lane: str):
    """(text, provider) or (None, None). Never raises. Never calls a paid/flat-rate CLI or the Mini."""
    try:
        _keys()
        _rbl = rb_active()
        if _rbl and not is_lite_lane(lane) and '"score"' in ((system or "") + (prompt or "")).lower():
            return await _rb_judge(prompt, system, max_tokens, lane, _rbl)
        if is_lite_lane(lane):  # classify etc.: one lite answer is the whole job; strong only if every lite is down
            for j in _healthy_lite()[:3]:
                t = await _judge(j, prompt, system, max_tokens, lane)
                if t:
                    return t, "free-lite:" + j[0]
            if not _cfg().get("lite_fallback_strong", True):
                _ledger({"lane": (lane or "")[:60], "judge": "-", "status": "held_no_lite", "ms": 0, "score": None})
                return None, None
        first = None
        pat = _patient(lane) and not bool(ol_cfg())  # 10/24: when lite fallback is active, do not sleep 90s before falling back
        cfg = _cfg()
        w1 = float(cfg.get("first_wait_s", 120)) if pat else 0.0
        w2 = float(cfg.get("pair_wait_s", 300)) if pat else 0.0
        if pat and any(t in (lane or "").lower() for t in _SHORT_WAIT):
            w1, w2 = min(w1, 60.0), min(w2, 60.0)
        # 18:52: sync callers time out at 120-540 s (hook_layer 240, tournament 120): keep the whole call inside a
        # wall budget so patient waits never turn into a caller TimeoutError.
        budget = float(cfg.get("tournament_budget_s", 90)) if ("tournament" in (lane or "") or "pairwise" in (lane or "")) \
            else float(cfg.get("total_budget_s", 190))
        deadline = time.time() + budget
        w1 = max(0.0, min(w1, budget - 100))
        t_end = time.time() + w1
        tried: set = set()
        while first is None:
            hs = [j for j in _healthy(lane) if j[0] not in tried][:4] if not pat else \
                [j for j in await _wait_healthy(lane, tried, "", max(0.0, t_end - time.time()))][:4]
            if not hs:
                break
            for j in hs:
                tried.add(j[0])
                t = await _judge(j, prompt, system, max_tokens, lane)
                if t:
                    first = (j, t)
                    break
            if not pat or time.time() >= t_end:
                break
            tried = set()  # judges that failed are benched now; next pass only picks recovered ones
        if not first:  # 2026-11-02 [pt279u]: capped OpenRouter paid overflow on X lanes, else hold (fail closed)
            _po = await _paid_overflow(prompt, system, max_tokens, lane, set())
            if _po:
                first = _po
        if not first:  # no strong judge answered: hold (never reject)
            _ledger({"lane": (lane or "")[:60], "judge": "-", "status": "held_no_judge", "ms": 0, "score": None})
            return None, None
        j1, t1 = first
        s1 = score_of(t1)
        if s1 is None or s1 < 8:  # non-numeric (classify/pairwise) or a screen-out: one judge is enough
            return t1, "free:" + j1[0]
        second = None
        t_end = time.time() + (w2 if s1 >= 9 else min(w2, 90.0))  # first score kept; only the missing judge re-asked
        t_end = min(t_end, deadline - 95)  # room for one more (gemma-length) call before the caller gives up
        while second is None:
            hs = await _wait_healthy(lane, {j1[0]}, j1[1], max(0.0, t_end - time.time())) if pat else \
                _healthy(lane, exclude={j1[0]}, prefer_not_fam=j1[1])
            if not hs:
                break
            for j in hs[:3]:
                t = await _judge(j, prompt, system, max_tokens, lane)
                if t and score_of(t) is not None:
                    second = (j, t)
                    break
            if not pat or time.time() >= t_end:
                break
        if second is None:  # 2026-11-02 [pt279u]: second opinion from a different maker via capped paid overflow
            _po = await _paid_overflow(prompt, system, max_tokens, lane, {j1[0]}, j1[1])
            if _po and score_of(_po[1]) is not None:
                second = _po
        if pat and second is None:
            _ledger({"lane": (lane or "")[:60], "judge": j1[0], "status": "pair_wait_timeout", "ms": 0, "score": s1})
        if not second:
            # 2026-10-21 08:42 operator TEMPORARY override (core/judge_override.py): Ines/hailports ORIGINALS may clear on
            # one strong judge's 8 while the pool is broken. Reported as 9 so the lane floors pass; logged as override.
            if _ov_single(lane, s1, j1[0], prompt):
                return set_score(t1, 9), "free-override8:" + j1[0]
            return set_score(t1, min(s1, 8)), "free-single-capped:" + j1[0]
        j2, t2 = second
        s2 = score_of(t2)
        lo = min(s1, s2)
        txt = t2 if s2 <= s1 else t1
        _ledger({"lane": (lane or "")[:60], "judge": f"{j1[0]}+{j2[0]}", "status": "pair_ok", "ms": 0, "score": lo})
        if lo < 9 and _ov_pair(lane, lo, f"{j1[0]}+{j2[0]}", [s1, s2], prompt):
            return set_score(txt, 9), f"free-override8-pair:{j1[0]}+{j2[0]}"
        return set_score(txt, lo), f"free-pair:{j1[0]}+{j2[0]}"
    except Exception as exc:
        _ledger({"lane": (lane or "")[:60], "judge": "-", "status": "router:" + type(exc).__name__, "ms": 0, "score": None})
        return None, None


# ── 2026-11-04 operator [pt343u]/[t344u]: PERSONAL CLI judges are PRIMARY on X + LinkedIn score lanes ─────────────
# Claude Code (claude-personal, maker anthropic) + Codex (codex-personal, maker openai), box only, never the Mini,
# no hard timeouts. Free/OpenRouter judges become fallback / second maker only. No Grok rung (operator t344u).
# Maker rule: a 9 needs two DIFFERENT makers; maker_of() maps routed models (groq gpt-oss -> openai,
# OpenRouter x-ai/* -> xai, etc.) so the same maker reached through another provider never pairs.
import subprocess as _sp
import tempfile as _tf
PERSONAL = (
    ("cli:codex-personal", "openai", "cli_codex", "codex"),
    ("cli:claude-personal", "anthropic", "cli_claude", "claude"),
)


def maker_of(j: tuple) -> str:
    m = (j[3] if len(j) > 3 else "").lower()
    for pre, mk in (("x-ai/", "xai"), ("grok", "xai"), ("openai/", "openai"), ("gpt-", "openai"),
                    ("anthropic/", "anthropic"), ("claude", "anthropic"), ("google/", "google"), ("gemini", "google"),
                    ("gemma", "google"), ("qwen", "alibaba"), ("deepseek", "deepseek"), ("nvidia/", "nvidia"),
                    ("nemotron", "nvidia"), ("meta-llama", "meta"), ("llama", "meta")):
        if pre in m:
            return mk
    return j[1]


def personal_primary(lane: str, prompt: str, system) -> bool:
    if not _cfg().get("personal_cli_primary", True):
        return False
    return or_scope(lane) in ("x", "li") and '"score"' in ((system or "") + (prompt or "")).lower()


def _call_cli(j: tuple, prompt: str, system) -> str:
    full = ((system + "\n\n") if system else "") + prompt + "\n\nAnswer with the JSON only. Do not run tools."
    with _tf.TemporaryDirectory(prefix="fj_cli_") as d:
        if j[2] == "cli_codex":
            outf = os.path.join(d, "out.txt")
            r = _sp.run(["/home/operator/.npm-global/bin/codex", "exec", "--skip-git-repo-check", "--ephemeral",
                         "-s", "read-only", "-o", outf, "-"], input=full, cwd=d, capture_output=True, text=True)
            txt = open(outf).read() if os.path.exists(outf) else ""
            if not txt.strip():
                raise RuntimeError("cli_err:" + (r.stderr or r.stdout)[-120:].replace("\n", " "))
            return txt
        from core.caption_brain import _cli_env
        r = _sp.run(["/home/operator/.npm-global/bin/claude", "-p", "--tools", ""], input=full, cwd=d,
                    capture_output=True, text=True, env=_cli_env())
        out = (r.stdout or "") + (r.stderr or "")
        if "Failed to authenticate" in out or "OAuth" in out or r.returncode != 0:
            raise RuntimeError("cli_auth" if "authenticate" in out or "OAuth" in out else "cli_err:" + out[-120:])
        return r.stdout


async def _cli_judge(j: tuple, prompt: str, system, lane: str) -> str:
    t0, status, out = time.time(), "ok", ""
    if _is_cold(j[0], _cold(), time.time()):
        return ""
    try:
        out = _strip(await _in_daemon(_call_cli, j, prompt, system))
        out = _json_clean(out, (system or "") + prompt) if out else ""
        if not out or score_of(out) is None:
            status = "unparsed"
            out = ""
    except Exception as exc:
        status = str(exc)[:60]
        _go_cold(j[0], 3600 if "auth" in status else 300)
    _ledger({"lane": (lane or "")[:60], "judge": j[0], "status": status, "ms": int((time.time() - t0) * 1000),
             "score": score_of(out) if out else None})
    return out if status == "ok" else ""


async def _personal_core(prompt: str, system, max_tokens: int, lane: str):
    """Personal CLIs first; missing second maker from free pool, then capped paid overflow. (text, provider) or
    (None, None) -> caller falls back to the normal free router."""
    votes = []
    for j in PERSONAL:
        if votes and maker_of(j) == maker_of(votes[0][0]):
            continue
        t = await _cli_judge(j, prompt, system, lane)
        if not t:
            continue
        votes.append((j, t))
        if score_of(t) < 8 or len(votes) == 2:
            break
    if not votes:
        return None, None
    j1, t1 = votes[0]
    s1 = score_of(t1)
    if s1 < 8:
        return t1, "personal:" + j1[0]
    if len(votes) < 2:
        m1 = maker_of(j1)
        for j in [x for x in _healthy(lane) if maker_of(x) != m1][:4]:
            t = await _judge(j, prompt, system, max_tokens, lane)
            if t and score_of(t) is not None:
                votes.append((j, t))
                break
    if len(votes) < 2:
        po = await _paid_overflow(prompt, system, max_tokens, lane, set(), maker_of(j1))
        if po and score_of(po[1]) is not None and maker_of(po[0]) != maker_of(j1):
            votes.append(po)
    if len(votes) < 2:
        return set_score(t1, min(s1, 8)), "personal-single-capped:" + j1[0]
    j2, t2 = votes[1]
    s2 = score_of(t2)
    lo = min(s1, s2)
    _ledger({"lane": (lane or "")[:60], "judge": f"{j1[0]}+{j2[0]}", "status": "pair_ok", "ms": 0, "score": lo})
    return set_score(t2 if s2 <= s1 else t1, lo), f"personal-pair:{j1[0]}+{j2[0]}"
