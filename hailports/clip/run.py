"""Command line for Paiste. The menu bar and the idle loop both land here."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from hailports.llm import ModelOffline
from hailports.clip.scene import (
    addressee_first,
    as_writer,
    bounced_choice,
    conditional_claim,
    copy_text,
    draft_rules,
    drop_ungrounded,
    ensure_commits,
    held_back,
    intro_narration,
    missing_commits,
    overreach,
    unasked_remedy,
    plain_reply,
    prepare,
    relevant_notes,
    reply_ok,
    rewrite_ask,
    run_inbox,
    scene_blob,
    scene_from,
    screen_has_choice,
    shape_voice,
    source_dirs,
    ground_reply,
    trim_overreach,
)
from hailports.clip.hops import complete, endpoints_from


def _complete(cfg: dict):
    hops = endpoints_from(cfg)

    def run(messages: list[dict]) -> str:
        text, name = complete(hops, messages)
        try:
            path = Path.home() / ".config" / "paiste" / "last-hop"
            path.write_text(name + "\n", encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass
        return text

    return run


def _instructions(cfg: dict | None) -> str:
    """The writing rules stay off a copied app. A local signer reads a local file. Everyone else asks the license server."""
    from hailports.clip.gate import credentials, device_id, secret_path
    local = Path.home() / ".config" / "paiste" / "instructions"
    if secret_path().is_file() and local.is_file():
        return local.read_text(encoding="utf-8", errors="replace").strip()
    paiste = (cfg or {}).get("paiste") or {}
    base = str(paiste.get("license_url") or "https://www.paisty.com/api/license").strip()
    url = base.rsplit("/", 1)[0] + "/prompt"
    # The license first; during a trial, the entitlement (it is not a license).
    for cred in credentials(cfg):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({**cred, "device": device_id()}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "paisty/1"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, TimeoutError):
            return ""
        if doc.get("ok") is True:
            return str(doc.get("system") or "").strip()
    return ""


def _context_note(cfg: dict | None) -> str:
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("context_file") or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()[:6000]


_LOOKUPS: dict = {}


def _lookup_proc(path: Path, query: str, timeout: int = 25) -> tuple[int, str]:
    """Run the notes helper once per query for a few minutes: routing and drafting share the answer."""
    import time as _time
    key = (str(path), query)
    hit = _LOOKUPS.get(key)
    if hit and _time.time() - hit[0] < 300:
        return hit[1], hit[2]
    try:
        proc = subprocess.run([str(path)], input=query, text=True, capture_output=True, timeout=timeout)
        code, out = proc.returncode, proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        code, out = 1, ""
    if len(_LOOKUPS) > 32:
        _LOOKUPS.clear()
    _LOOKUPS[key] = (_time.time(), code, out)
    return code, out


def _route_lookup(cfg: dict | None, doc: dict) -> str:
    """The notes helper's answer for this thread, for the figure-heavy check."""
    from hailports.clip.localfirst import thread_text
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("lookup_cmd") or "").strip()
    path = Path(raw).expanduser() if raw else Path()
    text = thread_text(doc)
    if not text.strip() or not raw or not path.is_file():
        return ""
    code, out = _lookup_proc(path, lookup_query(text))
    return out[:6000] if code == 0 else ""


def _brain_notes(cfg: dict | None, text: str) -> str:
    """Only the configured notes helper. Code and docs are searched by compose itself."""
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("lookup_cmd") or "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if not text.strip() or not raw or not path.is_file():
        return ""
    code, out = _lookup_proc(path, lookup_query(text))
    return out.strip()[:4000] if code == 0 else ""


def full_draft(doc: dict, route: dict, rules: str) -> tuple[str, str]:
    """The full-context path. Returns (reply, hop). Empty reply means fall back."""
    from hailports.clip import compose
    hop = {"name": ""}
    hops = endpoints_from(route)

    def run_model(messages: list[dict]) -> str:
        text, name = complete(hops, messages)
        hop["name"] = name
        return text

    text, _ctx, _attempts = compose.draft(doc, route, rules, run_model, lookup=lambda q: _brain_notes(route, q))
    return (text if reply_ok(text) else ""), hop["name"]


def lookup_query(text: str, limit: int = 6000) -> str:
    """The whole thread, ask first. The open ask sits at the end of a window, so it leads;
    then the top of the thread (subject, who), then the rest of the thread up to the limit."""
    flat = " ".join((text or "").split())
    if len(flat) <= 1600:
        return flat
    head = flat[:1800]
    ask = flat[-600:]
    middle = flat[1800:-600]
    room = max(0, limit - len(ask) - len(head) - 2)
    return ((ask + "\n" + head + "\n" + middle[-room:]) if room else (ask + "\n" + head))[:limit]


def _lookup_notes(cfg: dict | None, text: str) -> str:
    from hailports.clip.board import code_notes
    brain = ""
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("lookup_cmd") or "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if text.strip() and path.is_file():
        code, out = _lookup_proc(path, lookup_query(text))
        if code == 0:
            brain = out.strip()[:2000]
    code = code_notes(text) if text.strip() else ""
    if code:
        code = "Code on this Mac. Use a file, label, or message written here. Do not invent one.\n" + code
    return (code + "\n" + brain).strip()[:4000]


def _with_points(messages: list[dict], points: list[str]) -> list[dict]:
    if not points:
        return messages
    lines = [
        "Answer the open asks in a few short sentences. Do not give every line its own answer.",
        "Do not skip an ask. Use I, not we. No labels or headers.",
        "Do not turn their update into a new rule they did not say.",
        "Do not start with a number. Do not end with lmk.",
    ]
    lines.extend(points)
    block = "\n".join(lines)
    out = [dict(item) for item in messages]
    for index in range(len(out) - 1, -1, -1):
        if out[index].get("role") == "user":
            out[index]["content"] = block + "\n\n" + str(out[index].get("content") or "")
            break
    return out


def _pin_rules(messages: list[dict], rules: str) -> list[dict]:
    """Hops keep the front of the system prompt and the front of the user turn."""
    out = [dict(item) for item in messages]
    brief = " ".join(rules.split())
    if out and out[0].get("role") == "system":
        body = str(out[0].get("content") or "")
        if not body.startswith("These rules win"):
            out[0]["content"] = brief + "\n\n" + body
    for item in out:
        if item.get("role") == "user":
            cur = str(item.get("content") or "")
            if not cur.startswith("These rules win"):
                item["content"] = brief + "\n\n" + cur
            break
    return out


def redraft_doc(doc: dict) -> dict:
    """A redraft: paisty's own placed draft is not their message. Take it out, note it, and work out
    which sentences the user kept. `redraft_locks` from the popup: {"pins": [...]} or {"fresh": true}."""
    prior_text = str(doc.pop("prior_text", "") or "").strip()
    req = doc.pop("redraft_locks", None)
    if not prior_text:
        return doc
    from hailports.clip import owndraft
    req = req if isinstance(req, dict) else {}
    pins = req.get("pins") if isinstance(req.get("pins"), list) else None
    pins = [str(p) for p in pins] if pins is not None else None
    return owndraft.strip_scene(doc, prior_text, pins=pins, fresh=bool(req.get("fresh")))


def _keep_locks(text: str, doc: dict, retry) -> str:
    """Every kept sentence must be in the redraft verbatim: one retry, then a deterministic splice."""
    prior = doc.get("prior_draft") if isinstance(doc.get("prior_draft"), dict) else {}
    plan = prior.get("locks")
    if not isinstance(plan, dict) or not plan.get("locks") or not (text or "").strip():
        return text
    from hailports.clip import locks
    gone = locks.missing(text, plan["locks"])
    if not gone:
        return text
    try:
        ask = ("Rewrite the reply. Keep these sentences word for word, in the same place: "
               + " | ".join('"' + g + '"' for g in gone) + ". Only the reply text.")
        again = plain_reply(retry(text, ask) or "")
        if reply_ok(again) and len(locks.missing(again, plan["locks"])) < len(gone):
            text = again
    except ModelOffline:
        pass
    return locks.enforce(text, plan)


def _draft_scene_once(doc: dict, base_url: str, model: str, copy: bool, cfg: dict | None = None,
                      out: dict | None = None) -> int:
    """One draft on the route as given. `out` (when given) gets text and hop, and nothing is remembered."""
    remember = out is None
    doc = redraft_doc(doc)
    prepared = prepare(scene_from(doc), folders=source_dirs(cfg))
    if not prepared.get("ok"):
        print(prepared.get("reason") or "empty")
        return 2
    route = cfg or {"llm": {"base_url": base_url, "model": model or "local"}}
    from hailports.clip.gate import allowed
    if not allowed(route):
        print("license")
        return 2
    rules = _instructions(route)
    if not rules:
        print("license")
        return 2
    if not rules.startswith("You are viewing"):
        rules = (
            "You are viewing a communication on the screen. "
            "Use the on-screen text and every connected note or lookup. "
            "Then draft only the reply.\n\n"
            + rules
        )
    if "whole conversation" not in rules:
        rules = (
            "Read the whole conversation on screen, not only the last line. "
            "Use what was already said. Draft the reply to what is still open.\n\n"
            + rules
        )
    from hailports.clip import compose
    if compose.wants_full(doc, route):
        try:
            text, hop_name = full_draft(doc, route, rules)
        except ModelOffline as exc:
            print("model offline: " + str(exc))
            return 2
        except Exception:  # the older path still runs if the full path breaks
            text, hop_name = "", ""
        if text:
            if out is not None:
                out.update(text=text, hop=hop_name, path="full")
                return 0
            try:
                mark = Path.home() / ".config" / "paiste" / "last-hop"
                mark.write_text(hop_name + "\n", encoding="utf-8")
                mark.chmod(0o600)
            except OSError:
                pass
            try:
                from hailports.clip.drafts import append as remember_draft
                remember_draft(text, hop=hop_name, app=str(doc.get("app") or ""), scene=doc)
            except OSError:
                pass
            if copy:
                copy_text(text)
            print(text)
            return 0
    if "Search all connected mail" not in rules:
        rules = (
            "Search all connected mail, all connected calendar events, and all connected notes. "
            "Do not stop at a sample. Use what matches this conversation. "
            "Do not invent a fact that is not on screen and not in those.\n\n"
            + rules
        )
    messages = prepared.get("messages") or []
    rules = rules.replace(
        "Do not promise work that is not already done.",
        "When they ask you to do something, say you will. Do not claim it is already finished.",
    )
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = rules
    me = [part.strip() for part in str((route.get("paiste") or {}).get("me") or "").split(",") if part.strip()]
    sources = "\n".join(prepared.get("sources") or [])
    screen = scene_blob(doc)
    facts = ""
    try:
        from hailports.clip.lookups import facts_for_draft
        facts = facts_for_draft(route, screen)
    except (OSError, TimeoutError, ValueError):
        facts = ""
    who = addressee_first(screen + "\n" + sources, me)
    from hailports.clip.scene import latest_chat_message
    _latest_who, latest = latest_chat_message(screen, me)
    _note_scene(doc, who)
    choice = screen_has_choice(sources)
    voice = _context_note(route)[:1400]
    parts = []
    if me:
        parts.append(
            "You are " + me[0] + ". Never greet " + me[0] + ". "
            "Never start with hey " + me[0].split()[0] + ". "
            "If the window is their own item and nobody else asked a question, write a note with no greeting."
        )
    if voice:
        parts.append(voice)
    if latest:
        # A channel shows many old asks. Answer the newest message from someone else only.
        parts.append(
            "Reply only to the newest message from " + _latest_who + ":\n" + latest[:1200] + "\n"
            "Older messages on screen are background. Do not answer an older ask from someone else "
            "unless the newest message continues that same conversation. "
            "A name after @ is someone they tagged, not the person you are replying to."
        )
    found = relevant_notes(_lookup_notes(route, sources), sources, who)
    if found:
        parts.append(
            "Notes that match this message. Ignore a note that does not match. "
            "Style samples are tone only. "
            "If a note names a file, a label, or a message, use those words. "
            "Do not replace them with a vague restatement. "
            "Code lines outrank an older note when they disagree. "
            "Do not mention a person, a company, or a task from a note unless this message asks about it.\n" + found[:2200]
        )
    if doc.get("prior_draft"):
        from hailports.clip import owndraft
        parts.append(owndraft.prior_note(doc["prior_draft"]))
    note = "\n\n".join(parts).strip()
    if note:
        from hailports.clip.scene import runner_context
        prepared["messages"] = runner_context(prepared.get("messages") or [], note)
    from hailports.clip.board import misses_points, thin_reply, thread_points
    points = thread_points(latest) if latest else thread_points(sources)
    from hailports.clip.lookups import apply_facts
    prepared["messages"] = apply_facts(_with_points(prepared.get("messages") or [], points), facts)
    prepared["messages"] = _pin_rules(
        prepared.get("messages") or [],
        draft_rules(who, choice),
    )
    hop = ""
    try:
        hops = endpoints_from(route)
        text, hop = complete(hops, prepared["messages"])
        text = shape_voice(drop_ungrounded(plain_reply(text), screen, who), screen, who)
        weak = misses_points(text, points) or (bool(re.search(r"\.(?:cls|js|html)\b", found or "")) and thin_reply(text, found))
        if weak and len(hops) > 1:
            try:
                nxt, nxt_hop = complete(hops[1:], prepared["messages"])
                nxt = shape_voice(drop_ungrounded(plain_reply(nxt), screen, who), screen, who)
                if nxt and not thin_reply(nxt, found) and not misses_points(nxt, points):
                    text, hop = nxt, nxt_hop
            except ModelOffline:
                pass
        try:
            if remember:
                mark = Path.home() / ".config" / "paiste" / "last-hop"
                mark.write_text(hop + "\n", encoding="utf-8")
                mark.chmod(0o600)
        except OSError:
            pass
        gaps = missing_commits(text, sources)
        blocked = (
            held_back(text, sources)
            or conditional_claim(text, sources)
            or intro_narration(text, sources)
            or unasked_remedy(text, sources)
        )
        invented = overreach(text, sources) or bounced_choice(text, sources) or bool(gaps) or blocked
        first = text
        if invented or weak:
            ask = rewrite_ask(who, choice, points)
            if gaps:
                ask += " Say you will get these, and do not give a status for them: " + ", ".join(gaps) + "."
            if held_back(text, sources):
                ask += " Do not accept the role. Say you will wait."
            if conditional_claim(text, sources):
                ask += " Do not claim you have a spare. Say you will bring it if you have it."
            if intro_narration(text, sources):
                ask += " Write the intro itself. Do not say you will send it later."
            if unasked_remedy(text, sources):
                ask += " Do not promise a remedy they did not name. Say you will look."

            retry = list(prepared["messages"]) + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": ask},
            ]
            try:
                text = shape_voice(drop_ungrounded(plain_reply(_complete(route)(retry).strip()), screen, who), screen, who)
            except ModelOffline:
                # A weak first draft beats nothing. An invented one does not.
                text = "" if invented else first
        kept = ground_reply(drop_ungrounded(ensure_commits(as_writer(trim_overreach(text, sources), me), sources), screen, who), sources, me)
        # Cleanup that removes the whole reply must not put the raw text back.
        if reply_ok(kept):
            text = kept
        elif weak and not invented:
            # Retry returned worse junk; keep the usable first draft.
            fallback = ground_reply(drop_ungrounded(ensure_commits(as_writer(trim_overreach(first, sources), me), sources), screen, who), sources, me)
            text = fallback if reply_ok(fallback) else ""
        else:
            text = ""
    except ModelOffline as exc:
        print("model offline: " + str(exc))
        return 2
    try:
        from hailports.clip.lookups import concrete_reply
        text = concrete_reply(text, screen, facts)
    except (OSError, TimeoutError, ValueError):
        pass

    def _retry(prev: str, ask: str) -> str:
        return complete(endpoints_from(route), list(prepared["messages"]) + [
            {"role": "assistant", "content": prev}, {"role": "user", "content": ask}])[0]

    text = _keep_locks(text, doc, _retry)
    if not reply_ok(text):
        print("empty")
        return 2
    if out is not None:
        out.update(text=text, hop=str(hop or ""), path="quick")
        return 0
    try:
        from hailports.clip.drafts import append as remember_draft
        remember_draft(text, hop=str(hop or ""), app=str(doc.get("app") or ""), scene=doc)
    except OSError:
        pass
    if copy and text:
        copy_text(text)
    print(text)
    return 0 if text else 2




def _note_scene(doc: dict | None, who: str) -> None:
    """One line in paste-debug.log naming the chat the screen read captured, so a draft for the
    wrong chat is visible. Local file only; never fails a draft."""
    try:
        import re as _re
        from datetime import datetime, timezone
        doc = doc if isinstance(doc, dict) else {}
        text = str(doc.get("field_text") or "")
        speakers = _re.findall(r"(?m)^([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*)?)\s+\d{1,2}:\d{2}\s*[AP]M\b", text)
        seen = []
        for name in speakers:
            if name not in seen:
                seen.append(name)
        line = "scene app=%s origin=%s to=%s speakers=%s chars=%d" % (
            str(doc.get("app") or "?"), str(doc.get("origin") or "?"), who or "?",
            ",".join(seen[-4:]) or "?", len(text))
        path = Path.home() / ".config" / "paiste" / "paste-debug.log"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(stamp + " " + line + "\n")
    except Exception:
        pass


def draft_scene(doc: dict, base_url: str, model: str, copy: bool, cfg: dict | None = None) -> int:
    """Local engines first when the saved order puts them first: a draft that fails the gate gets
    one shorter local rewrite, then the whole draft runs on the premium engines. Nothing is sent."""
    import contextlib
    import copy as _copy
    import io
    import time as _time
    from hailports.clip import localfirst
    from hailports.clip import hops as _hops
    _hops.reset_slow()
    plan = localfirst.split(cfg) if cfg else None
    if not plan:
        return _draft_scene_once(doc, base_url, model, copy, cfg)
    local_cfg, premium_cfg = plan
    kind, cap = localfirst.kind_cap(doc)
    incoming = localfirst.incoming_words(doc)
    heavy, reasons = localfirst.classify(doc, _route_lookup(cfg, doc))
    if heavy:
        # Attachments, figures or tables: premium first (cursor, claude, codex), local only if they all fail.
        routed = localfirst.premium_first(cfg) or cfg
        t1 = _time.time()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _draft_scene_once(doc, base_url, model, copy, routed)
        printed = buf.getvalue()
        sys.stdout.write(printed)
        try:
            hop = (Path.home() / ".config" / "paiste" / "last-hop").read_text(encoding="utf-8").strip()
        except OSError:
            hop = routed["paiste"]["order"][0]
        models = (routed.get("paiste") or {}).get("models") or {}
        tier = "premium" if localfirst._via(models.get(hop) or {}) in localfirst.PREMIUM else "local"
        localfirst.log({"engine": hop, "tier": tier, "kind": kind, "route": "figures", "reason": reasons[:4],
                        "words": localfirst.shape.words(printed) if code == 0 else 0, "gate": "premium_first",
                        "ms": int((_time.time() - t1) * 1000), "code": code, "app": str(doc.get("app") or "")})
        return code
    reasons = ["no attachments, tables or 3+ figures"]
    me = [part.strip() for part in str((cfg.get("paiste") or {}).get("me") or "").split(",") if part.strip()]
    first = ((me or [""])[0].split() or [""])[0]
    t0 = _time.time()
    got: dict = {}
    why: list[str] = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = _draft_scene_once(_copy.deepcopy(doc), base_url, model, False, local_cfg, out=got)
    except Exception as exc:  # the local engine is down, busy or slow: premium takes it
        code, why = 2, ["local error " + type(exc).__name__]
    text = str(got.get("text") or "")
    if code == 0 and text:
        why = localfirst.gate(text, kind, cap, me, incoming)
        if why:
            try:
                again, _name = complete(endpoints_from(local_cfg),
                                        localfirst.shorter_messages(text, why, kind, cap, first))
                from hailports.clip import shape as _shape
                again = _shape.finish(plain_reply(again), kind, me)
                if reply_ok(again) and not localfirst.gate(again, kind, cap, me, incoming):
                    text, why = again, []
                else:
                    why = ["after shorter retry: " + "; ".join(localfirst.gate(again, kind, cap, me, incoming) or ["empty"])]
            except Exception as exc:
                why = why + ["shorter retry " + type(exc).__name__]
    elif not why:
        why = ["local returned no draft"]
    ms = int((_time.time() - t0) * 1000)
    from hailports.clip import shape as _shape
    if not why:
        hop = str(got.get("hop") or local_cfg["paiste"]["order"][0])
        localfirst.log({"engine": hop, "tier": "local", "kind": kind, "words": _shape.words(text),
                        "gate": "pass", "ms": ms, "route": "plain", "reason": reasons,
                        "app": str(doc.get("app") or "")})
        try:
            mark = Path.home() / ".config" / "paiste" / "last-hop"
            mark.write_text(hop + "\n", encoding="utf-8")
            mark.chmod(0o600)
        except OSError:
            pass
        try:
            from hailports.clip.drafts import append as remember_draft
            remember_draft(text, hop=hop, app=str(doc.get("app") or ""), scene=doc)
        except OSError:
            pass
        if copy:
            copy_text(text)
        print(text)
        return 0
    localfirst.log({"engine": str(got.get("hop") or local_cfg["paiste"]["order"][0]), "tier": "local",
                    "kind": kind, "words": _shape.words(text), "gate": "fail", "why": why[:4], "ms": ms, "route": "plain",
                    "app": str(doc.get("app") or "")})
    t1 = _time.time()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _draft_scene_once(doc, base_url, model, copy, premium_cfg)
    printed = buf.getvalue()
    sys.stdout.write(printed)
    try:
        hop = (Path.home() / ".config" / "paiste" / "last-hop").read_text(encoding="utf-8").strip()
    except OSError:
        hop = premium_cfg["paiste"]["order"][0]
    localfirst.log({"engine": hop, "tier": "premium", "kind": kind, "words": _shape.words(printed) if code == 0 else 0,
                    "gate": "fallback", "route": "plain", "ms": int((_time.time() - t1) * 1000), "code": code,
                    "app": str(doc.get("app") or "")})
    return code


def draft_screenshot(paths: list[str], base_url: str, model: str, copy: bool, cfg: dict | None = None) -> int:
    """A screenshot or pasted image of a thread in, a draft out."""
    from hailports.clip import screenshot
    route = cfg or {"llm": {"base_url": base_url, "model": model or "local"}}
    hops = endpoints_from(route)
    try:
        doc = screenshot.scene_for([Path(p) for p in paths], lambda m: complete(hops, m)[0])
    except ModelOffline as exc:
        print("model offline: " + str(exc))
        return 2
    if not (doc.get("field_text") or "").strip():
        print("empty")
        return 2
    return draft_scene(doc, base_url, model, copy, cfg)


def _route_dest(args):
    import os
    raw = str(getattr(args, "route_out", "") or "").strip()
    if not raw or raw == ".":
        raw = os.environ.get("PAISTE_ROUTE") or str(Path.home() / ".config" / "paiste" / "route.yaml")
    return Path(raw)


def _save_route(dest: Path, route: dict, pick: str = "", edits: bool = False) -> bool:
    """The one route writer for setup. The saved order and `preferred` always survive (see
    routefix.keep_choice); `pick` is an engine the user just chose, `edits` a setup-form save.
    No duplicate engines, no write when unchanged."""
    from hailports.config import dumps, load_file
    from hailports.clip.routefix import keep_choice, tidy_route
    clean = dict(route or {})
    old: dict = {}
    if dest.is_file():
        try:
            loaded = load_file(dest)
            old = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, TypeError):
            old = {}
    if isinstance(old.get("paiste"), dict) and old["paiste"].get("models"):
        for key, value in old.items():
            if key not in clean:
                clean[key] = value
        clean["paiste"] = keep_choice(old["paiste"], clean.get("paiste") or {}, pick=pick, edits=edits)
    else:
        clean = tidy_route(clean)
    if old == clean:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(dumps(clean), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(dest)
    return True


def _onboard_auto(args, cfg, scan: dict | None = None) -> int:
    """One click. Apply what the scan found. Keys go to 0600 files and are never printed."""
    from hailports.config import save_file
    from hailports.clip.board import BoardError, auto_setup, scan_setup, server_setup_text, tie_notes
    from hailports.clip.gate import read_entitlement, read_token
    dest = _route_dest(args)
    found = scan if scan is not None else scan_setup()
    license_url = str((cfg.get("paiste") or {}).get("license_url") or "https://www.paisty.com/api/license")
    setup_url = license_url.rsplit("/", 1)[0] + "/setup"

    def setup_text(mail_on: bool, app: str) -> str:
        return server_setup_text(mail_on, False, app, read_token(cfg), setup_url,
                                 entitlement=read_entitlement().get("token", ""))

    try:
        updated, summary = auto_setup(
            cfg.get("paiste") or {},
            found,
            dest.parent,
            Path.home() / "Documents" / "paisty-notes",
            setup_text=setup_text,
        )
        route = dict(cfg)
        route["paiste"] = updated
        dest.parent.mkdir(parents=True, exist_ok=True)
        _save_route(dest, route)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
        app = summary["ai"] if summary["ai"] in {"claude", "codex", "cursor"} else ""
        tie_notes(summary["notes"], app, "local" if summary["ai"] == "local" else "")
    except (BoardError, OSError) as exc:
        reason = "could not write settings" if isinstance(exc, OSError) else (str(exc).splitlines() or ["setup stopped"])[0][:120]
        print(json.dumps({"ai": "", "notes": "", "mail": "", "chat": "", "needs": [reason]}))
        return 2
    print(json.dumps(summary))
    return 0


def cmd_onboard(args, cfg) -> int:
    """Write the model choice. Nothing is sent. Window text for a draft is kept on this Mac for 7 days so Redo can rewrite it."""
    from hailports.config import save_file
    from hailports.clip.board import BoardError, app_status, apply_board, attach_keys, discover, probe_model, setup_hint, show_board, start_login, use_app
    if getattr(args, "login", ""):
        print(start_login(str(args.login)))
        return 0
    if getattr(args, "use", ""):
        name = str(args.use).strip().lower()
        state = app_status(name)
        if state != "signed-in":
            print(state)
            return 2
        dest = _route_dest(args)
        try:
            updated = use_app(cfg.get("paiste") or {}, name)
        except BoardError as exc:
            print(exc)
            return 2
        dest.parent.mkdir(parents=True, exist_ok=True)
        updated["preferred"] = name
        route = dict(cfg)
        route["paiste"] = updated
        _save_route(dest, route, pick=name, edits=True)
        print("using")
        return 0
    if getattr(args, "connect", ""):
        print(app_status(str(args.connect)))
        return 0
    if getattr(args, "hint", False):
        print(setup_hint() or "")
        return 0
    if getattr(args, "auto", False):
        return _onboard_auto(args, cfg)
    if getattr(args, "scan", False):
        from hailports.clip.board import scan_setup
        print(json.dumps(scan_setup()))
        return 0
    if getattr(args, "show", False):
        print(json.dumps(show_board(cfg.get("paiste") or {}, discover())))
        return 0
    path = Path(getattr(args, "answers", "") or "")
    answers = None
    if path.is_file():
        try:
            answers = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            answers = None
        try:
            path.unlink()
        except OSError:
            pass
    if getattr(args, "pull", ""):
        from hailports.clip.board import apple_notes_text, monday_fetch, pull_source
        from hailports.config import save_file
        kind = str(args.pull).strip().lower()
        home = Path.home()
        token = ""
        text = ""
        doc = None
        folder = None
        if kind == "notes":
            from hailports.clip.board import make_notes
            chosen = str((answers or {}).get("path") or "").strip()
            dest_notes = Path(chosen) if chosen and Path(chosen).is_dir() else home / "Documents" / "paisty-notes"
            mail_on = bool((answers or {}).get("mail"))
            calendar_on = bool((answers or {}).get("calendar"))
            app = str((answers or {}).get("app") or "").strip().lower()
            from hailports.clip.board import server_setup_text
            from hailports.clip.gate import read_entitlement, read_token
            license_url = str((cfg.get("paiste") or {}).get("license_url") or "https://www.paisty.com/api/license")
            setup_url = license_url.rsplit("/", 1)[0] + "/setup"
            written = server_setup_text(mail_on, calendar_on, app, read_token(cfg), setup_url,
                                        entitlement=read_entitlement().get("token", ""))
            made = make_notes(dest_notes, mail=mail_on, calendar=calendar_on, body=written)
            paiste = dict(cfg.get("paiste") or {})
            paiste["notes"] = "folder"
            paiste["notes_dir"] = made["path"]
            route = dict(cfg)
            route["paiste"] = paiste
            dest = _route_dest(args)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _save_route(dest, route)
            try:
                dest.chmod(0o600)
            except OSError:
                pass
            print("added 1")
            print("path " + made["path"])
            return 0
        if kind == "apple-notes":
            text = apple_notes_text()
        elif kind == "monday":
            token = str((answers or {}).get("token") or "").strip()
            key = home / ".config" / "paiste" / "keys" / "monday.key"
            if not token and key.is_file():
                token = key.read_text(encoding="utf-8", errors="replace").strip()
            if token:
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_text(token, encoding="utf-8")
                key.chmod(0o600)
                doc = monday_fetch(token)
        elif kind == "zoom":
            folder = home / "Documents" / "Zoom"
        elif kind == "littlebird":
            folder = home / "Library" / "Application Support" / "Littlebird"
        elif kind == "onenote":
            for candidate in (
                home / "Library/Group Containers/UBF8T346G9.Office/OneNote",
                home / "Library/Containers/com.microsoft.onenote.mac/Data",
            ):
                if candidate.is_dir():
                    folder = candidate
                    break
        dest_root = home / ".config" / "paiste" / "sources" / kind
        info = pull_source(kind, dest_root, token=token, text=text, doc=doc, folder=folder)
        if info["added"]:
            paiste = dict(cfg.get("paiste") or {})
            paiste["sources_dir"] = str(home / ".config" / "paiste" / "sources")
            route = dict(cfg)
            route["paiste"] = paiste
            dest = _route_dest(args)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _save_route(dest, route)
            try:
                dest.chmod(0o600)
            except OSError:
                pass
        tail = info.get("need") or ""
        print("added %d%s" % (info["added"], (" " + tail) if tail else ""))
        return 0
    if getattr(args, "ingest", False):
        from hailports.clip.board import ingest_files
        from hailports.config import save_file
        paths = []
        if isinstance(answers, dict) and isinstance(answers.get("paths"), list):
            paths = [str(item) for item in answers["paths"]]
        root = Path.home() / ".config" / "paiste" / "sources"
        info = ingest_files(paths, root)
        if info["added"]:
            paiste = dict(cfg.get("paiste") or {})
            paiste["sources_dir"] = str(root)
            route = dict(cfg)
            route["paiste"] = paiste
            dest = _route_dest(args)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _save_route(dest, route)
            try:
                dest.chmod(0o600)
            except OSError:
                pass
        print("added %d%s" % (info["added"], " sent" if info.get("sent") else ""))
        return 0
    if getattr(args, "probe", False):
        doc = answers if isinstance(answers, dict) else {}
        text = probe_model(str(doc.get("line") or ""), str(doc.get("key") or ""))
        print(text)
        return 0 if text.startswith("Connected") else 2
    if not isinstance(answers, dict):
        print("answers")
        return 2
    dest = _route_dest(args)
    try:
        answers = attach_keys(dest.parent, answers)
        updated = apply_board(cfg.get("paiste") or {}, answers)
    except BoardError as exc:
        print(str(exc))
        return 2
    route = dict(cfg)
    route["paiste"] = updated
    _save_route(dest, route, edits=True)
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    from hailports.clip.board import tie_notes
    order = list(updated.get("order") or [])
    app = next((name for name in order if name in {"claude", "codex", "cursor"}), "")
    local = next((name for name in order if name not in {"claude", "codex", "cursor"}), "")
    tie_notes(str(updated.get("notes_dir") or ""), app, local)
    print(str(dest))
    print("sent false")
    return 0


def cmd_paiste(args, cfg, state: Path) -> int:
    paiste = cfg.get("paiste") or {}
    action = args.action or "draft"
    if action == "onboard":
        return cmd_onboard(args, cfg)
    if action in {"drafts", "redo"}:
        from hailports.clip.drafts import cmd as drafts_cmd
        return drafts_cmd(action, args, cfg)
    if action == "setup":
        from hailports.config import save_file
        from hailports.clip.board import discover, guide
        from hailports.promptui import PlainIO, YesIO
        io = YesIO() if getattr(args, "yes", False) else PlainIO()
        import os
        os.environ.setdefault("PAISTY_SETUP_URL", "https://paisty.com/api/setup")
        updated = guide(io, dict(paiste), discover())
        route = {"paiste": updated, "llm": cfg.get("llm") or {}}
        dest = Path(getattr(args, "route_out", "") or "") 
        if not str(dest):
            dest = Path.home() / ".config" / "paiste" / "route.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _save_route(dest, route, edits=True)
        print(str(dest))
        print("sent false")
        return 0
    if action == "menu":
        app = Path(__file__).resolve().parents[1] / "contrib" / "paiste" / "Paiste.app" / "Contents" / "MacOS" / "Paiste"
        print("Build the menu with contrib/paiste/build.sh, then open Paiste.app.")
        print(str(app))
        return 0
    if action == "auto":
        inbox = Path(args.inbox or state / "paiste-inbox")
        outbox = Path(args.outbox or state / "paiste-outbox")
        folders = source_dirs(cfg)
        try:
            rows = run_inbox(inbox, outbox, _complete(cfg), folders=folders, copy=bool(args.copy or paiste.get("copy")))
        except ModelOffline as exc:
            print("model offline: " + str(exc))
            return 2
        print(json.dumps({"sent": False, "results": rows}))
        return 0
    raw = ""
    if args.scene:
        raw = Path(args.scene).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    if not raw.strip():
        print("Pass a scene file with --scene.")
        return 2
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        print("scene was not JSON")
        return 2
    llm = cfg.get("llm") or {}
    return draft_scene(doc, str(llm.get("base_url") or ""), str(llm.get("model") or "local"), bool(args.copy), cfg)
