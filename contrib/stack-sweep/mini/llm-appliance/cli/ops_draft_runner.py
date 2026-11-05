#!/usr/bin/env python3
"""Draft runner — the ONLY place Zoom/Outlook reply drafts get composed + staged.

Feeds open + scrape + remark, then enqueue. The runner runs after EVERY surface is
scraped and the work_brain push landed, so each draft sees a real full sweep:

  candidate → lane gate (Andy = CommsBot tip only) → full_sweep_dig.sweep
            → closed_in_sent? drop → compose w/ sweep block → detect_body
            → stage (Zoom tee w/ prior_body | Outlook owa_write reply)
            → remark unread → proof

HARD: never send, never Enter, Mini-local only, operator parked.
Zoom stages are held until the self-thread never-Enter prove passed for the CURRENT
iframe JS (sha-keyed, 12h). STOP file halts everything; SEND_SUSPECT writes STOP.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re  # 2026-10-20: used at _liberal_* check (line ~928); was NameError for non-liberal drafts
import sys
import time
from contextlib import contextmanager
from pathlib import Path

CLI = Path(__file__).resolve().parent
HOME = Path.home()
STACK = HOME / "claude-stack"
for p in (str(CLI), str(STACK / "tools"), str(STACK)):
    if p not in sys.path:
        sys.path.insert(0, p)

import ops_lanes as lanes  # noqa: E402
import ops_stub_detectors as osd  # noqa: E402

RUNTIME = Path(os.environ.get("DRAFT_RUNNER_RUNTIME",
                              str(HOME / "llm-appliance/runtime/local-cycle-browser")))
QUEUE = RUNTIME / "draft-queue.json"
QUEUE_LOCK = RUNTIME / "draft-queue.lock"
RUN_LOCK = RUNTIME / "DRAFT_RUNNER_LOCK"
STOP = RUNTIME / "DRAFT_RUNNER_STOP"
PROVE_CACHE = RUNTIME / "zoom-prove.json"
IFRAME_JS = CLI / "zoom_safari_draft_iframe.js"
ZOOM_URL = "https://operator/wc/team-chat?integrate=true"

PROVE_TTL_S = 12 * 3600
RUN_LOCK_STALE_S = 30 * 60
MAX_ATTEMPTS = 6
BACKOFF_BASE_S = 120
BACKOFF_MAX_S = 3600
MLX_BACKOFF_S = 20  # HARD: mlx flakes must not park a source for an hour
DONE_KEEP = 300

# terminal outcomes: never retried for the same content hash
TERMINAL = {
    "staged",
    "commsbot_only",
    "closed_in_sent",
    "dead",
    "send_suspect",
    "skipped_noise",
    "parked_not_unread",
    "skipped_no_passive_read",
    "quarantined",  # v2 (2026-10-10): operator/gate quarantine; reopens only via a new content hash
    "held",  # zoom grounded:exc compose failure; not retried
}


def _pending_from_them(it: dict) -> bool:
    """2026-10-16 (Pruthi): our own bridge open clears the Zoom badge, so 'not live unread'
    is not proof operator read it. Newest message from them + seen unread within 36h = keep it."""
    try:
        msgs = it.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        who = str(last.get("from") or last.get("sender") or "").strip().lower()
        if who in ("you", "operator", "operator operator"):
            return False
        seen = float(it.get("sidebar_seen_at") or 0)
        return bool(it.get("live_unread")) and seen > 0 and time.time() - seen < 36 * 3600
    except Exception:
        return False


def park_stale_zoom_queue() -> int:
    """Park every Zoom queue row that is not a live unread. Returns count parked."""
    live = _live_zoom_sids()
    n = 0
    with _queue_locked():
        data = _load()
        now = time.time()
        for qk, it in list((data.get("items") or {}).items()):
            if not isinstance(it, dict) or it.get("channel") != "zoom":
                continue
            if it.get("state") in TERMINAL:
                continue
            sid = str(it.get("sid") or it.get("key") or "").strip()
            if sid and sid in live:
                continue
            # Never park lastword rows while ZOOM_NO_OPEN; process() is passive-only.
            if it.get("lastword") and _zoom_no_open():
                continue
            # HARD 2026-10-08 zoomdm: our own tip-read/stage attempt cleared the
            # badge and remint failed (client_lock_busy) -> never park that.
            if it.get("force_reopen") or _pending_from_them(it):
                if _zoom_no_open():
                    it["state"] = "parked_not_unread"
                    it["error"] = "not_live_unread"
                    it["updated"] = now
                    n += 1
                    _log(f"zoom_no_open_block zoom_force_reopen_not_live {(it.get('name') or sid)[:40]}")
                continue
            it["state"] = "parked_not_unread"
            it["error"] = "not_live_unread"
            it["updated"] = now
            n += 1
        if n:
            _save(data)
    if n:
        _log(f"park_stale_zoom n={n}")
    return n


def _live_zoom_sids() -> set[str]:
    """Only sids Zoom currently paints as unread. Stale queue ≠ a target (Trisha 2026-09-29).

    HARD 2026-10-07: bridge SoT is zoom-bridge-sidebar.json. The safari-unread
    mirror can lag or be empty while the sidebar still shows unreads — that
    parked Jennifer (and every live Zoom) as parked_not_unread and the runner
    processed 0 forever.
    """
    out: set[str] = set()
    # 1) bridge sidebar (live)
    try:
        import ops_bridge_lane as bridges

        blob = json.loads((RUNTIME / "zoom-bridge-sidebar.json").read_text())
        payload = blob.get("payload") or {}
        if isinstance(payload, dict) and payload.get("ok"):
            for row in bridges.sidebar_unread_rows(payload):
                sid = str(
                    row.get("sessionId") or row.get("sid") or row.get("id") or ""
                ).strip()
                if sid:
                    out.add(sid)
    except Exception:
        pass
    # 2) mirror written by write_zoom_unread_from_bridge
    try:
        blob = json.loads((RUNTIME / "zoom-safari-unread.json").read_text())
    except Exception:
        blob = {}
    for row in list(blob.get("unreads") or []):
        if not isinstance(row, dict):
            continue
        sid = str(row.get("sid") or "").strip()
        if sid and (row.get("unread") is True or int(row.get("unread_count") or 0) > 0):
            out.add(sid)
    return out


def _zoom_no_open() -> bool:
    try:
        import ops_safari_feeds as feeds

        return bool(feeds.zoom_no_open())
    except Exception:
        return (HOME / "claude-stack/data/flags/ZOOM_NO_OPEN").is_file()


def _log(msg: str) -> None:
    try:
        import ops_safari_feeds as feeds

        feeds._log(f"draft_runner {msg}")
    except Exception:
        pass


@contextmanager
def _queue_locked():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_LOCK, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load() -> dict:
    try:
        data = json.loads(QUEUE.read_text())
        if isinstance(data, dict):
            data.setdefault("items", {})
            return data
    except Exception:
        pass
    return {"items": {}}


def _save(data: dict) -> None:
    items = data.get("items") or {}
    done = [(k, v) for k, v in items.items() if v.get("state") in TERMINAL]
    if len(done) > DONE_KEEP:
        done.sort(key=lambda kv: float(kv[1].get("updated") or 0))
        for k, _ in done[: len(done) - DONE_KEEP]:
            items.pop(k, None)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(QUEUE)


def _qkey(channel: str, key: str) -> str:
    return f"{channel}:{key}"


def _newest_peer_id(it: dict) -> str:
    try:
        if (it.get("channel") or "") != "zoom":
            return str(it.get("message_id") or "")
        import draft_v2 as _v2n

        ms = _v2n.zoom_chrono_order(it.get("messages") or [])
        i = _v2n.zoom_thread_times(ms)[2]
        if i is None:
            return ""
        m = ms[i]
        import hashlib, re as _re
        try:
            import grounded_drafter as _gdn
            t = _gdn._speaker_body(m)[2]
        except Exception:
            t = str(m.get("text") or m.get("body") or "")
        t = _re.sub(r"(?m)^(Unread|\(Edited\))\s*$", "", t)
        t = _re.sub(r"[^a-z0-9]+", " ", t.lower())[:160]
        t = _re.sub(r"\s+", " ", t).strip().lower()[:400]
        return "h:" + hashlib.sha1(t.encode()).hexdigest()[:16]  # scrape ids churn; text is stable
    except Exception:
        return ""



def _zoom_local_record(sid: str) -> dict:
    """2026-10-19 zoomfix: local staged-draft record for sid ({} if none)."""
    sid = (sid or "").strip()
    if not sid or "/" in sid or ".." in sid:
        return {}
    try:
        rec = json.loads((HOME / "claude-stack/data/zoom_staged_drafts" / f"{sid}.json").read_text())
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


_ZD_CACHE = {"t": 0.0, "sids": None}


def _zoom_draft_sids() -> set | None:
    """Session ids that currently hold a Zoom composer draft (read-only drafts list, 60 s cache). None = unknown."""
    if time.time() - _ZD_CACHE["t"] < 60 and _ZD_CACHE["sids"] is not None:
        return _ZD_CACHE["sids"]
    try:
        import subprocess
        env = dict(os.environ)
        env.setdefault("ZOOM_WEB_CDP", "http://127.0.0.1:18812")
        out = subprocess.run([next((c for c in ("/opt/homebrew/opt/node@22/bin/node", "/opt/homebrew/bin/node", "/usr/local/bin/node") if Path(c).is_file()), "node"), str(Path.home() / "claude-stack/tools/zoom_write.js"), "drafts"], capture_output=True,
                             text=True, timeout=40, env=env, cwd=str(Path.home() / "claude-stack"))
        d = json.loads(out.stdout or "{}")
        if not d.get("ok"):
            return None
        sids = {str(r.get("sessionId") or "") for r in d.get("data") or []}
        _ZD_CACHE.update(t=time.time(), sids=sids)
        return sids
    except Exception:
        return None


def enqueue(cand: dict) -> dict:
    """Idempotent. Same (channel,key,content_hash) already staged/terminal → not queued."""
    channel = cand.get("channel") or ""
    key = str(cand.get("key") or "")
    ch = cand.get("content_hash") or ""
    if channel not in ("zoom", "outlook") or not key or not ch:
        return {"queued": False, "reason": "bad_candidate"}
    if channel == "outlook":
        import ops_safari_feeds as _feeds_fz

        _fz_mid = str(cand.get("message_id") or key)
        if _feeds_fz.owa_is_frozen(str(cand.get("message_id") or "")) or _feeds_fz.owa_is_frozen(key):
            _feeds_fz._owa_frozen_log(_fz_mid)
            return {"queued": False, "reason": "owa_frozen", "frozen": True}
    with _queue_locked():
        data = _load()
        qk = _qkey(channel, key)
        if qk not in data["items"]:  # 2026-10-14 repick-storm: Outlook ids arrive in mixed case -> one item per thread
            _ql = qk.lower()
            for _k in list(data["items"].keys()):
                if _k.lower() == _ql:
                    qk = _k
                    break
        prev = data["items"].get(qk) or {}
        # 2026-10-12 (operator "remember discarded drafts"): once a draft was staged or discarded for the NEWEST
        # peer message, never redraft/restage it until a NEW peer message arrives (scrape hash churn ignored).
        try:  # 2026-10-12 support_playbooks: operatorBot receipt landed for this thread -> redraft once, as done
            import support_playbooks as _spr
            if _spr.receipt_redraft_due([cand.get("name"), key, cand.get("sid"), cand.get("from_name"), cand.get("subject")]):
                for _k in ("handled_msg_id", "discarded_msg_id"):
                    prev.pop(_k, None)
                prev["content_hash"] = ""
                prev["state"] = "receipt_redraft"
                _log(f"support_receipt_redraft {qk}")
        except Exception:
            pass
        _nid = _newest_peer_id(cand)
        _handled = {str(prev.get("handled_msg_id") or ""), str(prev.get("discarded_msg_id") or "")} - {""}
        if _nid and _nid in _handled and str(prev.get("state") or "") in ("staged", "discarded_by_operator", "skipped_noise", "closed_in_sent", "commsbot_only", "operator_retired"):
            if channel == "zoom" and prev.get("state") == "staged":
                # 2026-10-19 zoomfix: the drafts list is a read-only SDK query (no open), so this
                # check runs while ZOOM_NO_OPEN is on. Never-confirmed / composer-only drafts that
                # vanished are LOST -> re-stage. A confirmed Zoom server draft that vanished was
                # removed or sent by operator -> keep the 2026-10-12 "remember discarded drafts" rule.
                _zsid = str(cand.get("sid") or key)
                _sids = _zoom_draft_sids()
                if _sids is not None and _zsid not in _sids:
                    _rec = _zoom_local_record(_zsid)
                    if _rec.get("staged_via") == "zoom_server_draft" and _rec.get("server_draft_id"):
                        # Server drafts persist, so gone = operator sent it or deleted it.
                        try:
                            import ops_safari_feeds as _fz
                            _sent = bool(_fz._zoom_tip_from_self(cand.get("messages") or []))
                        except Exception:
                            _sent = False
                        _gone = float(prev.get("draft_gone_at") or 0)
                        if _sent:
                            prev.update(state="closed_in_sent", error="sent_by_operator", closed_at=time.time())
                            _save(data)
                            _log(f"zoom_staged_draft_sent {qk} msg={_nid}")
                        elif not _gone:
                            prev["draft_gone_at"] = time.time()  # wait for the passive tip to catch up
                            _save(data)
                            _log(f"zoom_staged_draft_gone_wait {qk}")
                        elif time.time() - _gone >= 1200:
                            prev.update(state="discarded_by_operator", discarded_msg_id=_nid, discarded_at=time.time(),
                                        error="composer_cleared_by_operator")
                            _save(data)
                            _log(f"discard_detected {qk} msg={_nid}")
                            try:
                                from core.draft_feedback_learning import record_outcome as _rec_disc
                                _rec_disc(surface="zoom", source_key=_zsid, outcome="deleted",
                                          draft_text=str(_rec.get("draft_text") or prev.get("last_draft") or ""),
                                          thread_label=str(cand.get("name") or key))
                            except Exception:
                                pass
                    else:
                        prev.update(state="retry", next_at=0, error="composer_draft_lost",
                                    lost_at=time.time(), messages=cand.get("messages") or prev.get("messages"))
                        _save(data)
                        _log(f"zoom_draft_lost_restage {qk} msg={_nid}")
                        return {"queued": True, "key": qk, "reason": "composer_draft_lost"}
            return {"queued": False, "reason": f"handled_msg:{prev.get('state')}"}
        # 2026-10-21 unstarve: a held / no-ask / self-check-failed thread with NO new peer message
        # is not re-drafted on scrape-hash churn or a reset. Same newest peer text -> back off
        # (1h, 4h, 24h by prior storm tries) until a genuinely new inbound message arrives.
        try:
            _prev_err_u = str(prev.get("error") or "")
            _prev_state_u = str(prev.get("state") or "")
            _hold_like = _hold_class(_prev_err_u) or any(t in _prev_err_u for t in ("no_ask", "group_no_ask", "no_reply_needed"))
            if prev and _hold_like and _prev_state_u in ("held", "retry", "dead", "pending") and not _mlx_fail(_prev_err_u):
                _pid_prev = _newest_peer_id(prev)
                if _nid and _pid_prev and _nid == _pid_prev:
                    # 2026-10-21 checkin: attempts and error_hits record the SAME failure -> max, not sum (1 fail = 1h, not 4h)
                    _tries = max(int(prev.get("attempts") or 0), len([1 for v in (prev.get("error_hits") or {}).values() for _ in v]))
                    _wait = 600 if _tries <= 1 else (1200 if _tries <= 3 else 1800)
                    _last = float(prev.get("updated") or prev.get("enqueued") or 0)
                    if time.time() - _last < _wait:
                        _log(f"unstarve_skip_same_peer {qk[:48]} err={_prev_err_u[:40]} wait_s={int(_wait - (time.time() - _last))}")
                        return {"queued": False, "reason": "hold_same_peer_msg"}
        except Exception:
            pass
        if prev.get("content_hash") == ch:
            # Same inbound parked for operator stays parked until the hash changes.
            if prev.get("held_for_operator"):
                _held_age = time.time() - float(prev.get("updated") or 0)
                if _held_age < 900 and not prev.get("ask_do_due"):
                    return {"queued": False, "reason": "held_for_operator"}
            # Allow re-queue when prior attempt starved on MLX / work_brain.
            prev_err = str(prev.get("error") or "")
            prev_state = str(prev.get("state") or "")
            # HARD 2026-09-29 (operator): live unread returned after a silent
            # close/skip/park — reopen so tip-read cannot clear the badge forever.
            reopen_states = {
                "closed_in_sent",
                "skipped_noise",
                "parked_not_unread",
                "dead",
                "commsbot_only",
            }
            if prev_state in ("dead", "retry") and (
                _mlx_fail(prev_err)
                or "work_brain" in prev_err.lower()
                or "need_dig_citation:" in prev_err
                or "sf_facts_missing" in prev_err
            ):
                pass  # fall through and replace — allow redraft after dig/auth fix
            elif prev_state in ("skipped_noise", "closed_in_sent", "commsbot_only", "operator_retired"):
                # Same content hash. Reopening these every lap resets next_at to 0
                # and is the key_storm (picks ~30s apart, backoff never applied).
                # A new peer message carries a new id; same id or unknown id stays parked.
                if _nid and _nid not in _handled:
                    _log(f"enqueue_reopen {qk} from={prev_state} new_peer=1")
                    pass
                else:
                    return {"queued": False, "reason": f"terminal_same_hash:{prev_state}"}
            elif prev_state in ("parked_not_unread", "dead"):
                _log(f"enqueue_reopen {qk} from={prev_state}")
                pass  # badge returned; one reopen
            else:
                if channel == "zoom" and cand.get("live_unread") and not prev.get("live_unread"):
                    prev["live_unread"] = True
                    data["items"][qk] = prev
                    _save(data)
                    _log(f"enqueue_keep_live_unread {qk[:48]}")
                return {"queued": False, "reason": f"same_hash:{prev.get('state')}"}
        item = dict(cand)
        if channel == "zoom" and prev.get("live_unread") and not item.get("live_unread"):
            item["live_unread"] = True
            _log(f"enqueue_keep_live_unread {qk[:48]}")
        item.update({
            "state": "pending",
            "attempts": 0,
            "next_at": 0,
            "enqueued": time.time(),
            "updated": time.time(),
            # our last staged body on this thread → supersede only replaces OUR draft
            "prior_body": prev.get("staged_body") or prev.get("prior_body") or "",
        })
        if prev.get("content_hash") == ch and prev.get("error_hits"):
            item["error_hits"] = prev.get("error_hits")
        data["items"][qk] = item
        _save(data)
    return {"queued": True, "key": qk}


def _update(qk: str, **fields) -> None:
    with _queue_locked():
        data = _load()
        it = data["items"].get(qk)
        if it is None:
            return
        it.update(fields)
        it["updated"] = time.time()
        _save(data)


def _mlx_fail(err: str) -> bool:
    e = (err or "").lower()
    return any(
        x in e
        for x in (
            "mlx",
            "timeout",
            "unreachable",
            "409",
            "507",
            "busy",
            "prefill",
            "insufficient storage",
            "conflict",
        )
    )


def _err_key(error: str) -> str:
    return " ".join(str(error or "").split()).lower()[:160]


def _hold_class(error: str) -> bool:
    """Hold-class errors can park for operator. MLX flakes must keep the short backoff."""
    if _mlx_fail(error):
        return False
    e = (error or "").lower()
    return any(k in e for k in (
        "need_dig_citation", "needs_operator", "non_answer", "grounded_hold",
        "core_hold", "sf_facts_missing", "self_check_failed", "quality_refuse",
    ))


def _retry(qk: str, it: dict, error: str) -> str:
    attempts = int(it.get("attempts") or 0) + 1
    # Zoom / lastword: model-too-short after a failed fact-verify is not an endless retry.
    if (
        (it.get("lastword") or str(it.get("channel") or "") == "zoom")
        and str(error or "").startswith("grounded:exc")
        and attempts >= 3
    ):
        name = str(it.get("name") or "")
        _update(qk, state="held", attempts=attempts, error="compose_failed")
        try:
            import liberal_policy as _lp

            _lp.append_draft_hold(name, "", "compose_failed")
        except Exception:
            pass
        _log(f"zoom_hold_compose_failed {name}")
        return "held"
    _err_hits = None
    if _hold_class(error):
        # 2026-10-21 retry-storm: count this hold; hard-cap → held. Else force ≥4h next_at.
        try:
            import retry_storm as _rs_r

            _mid = str(it.get("message_id") or it.get("key") or it.get("sid") or qk)
            _ch = str(it.get("content_hash") or "")
            _surf = "zoom" if str(it.get("channel") or "") == "zoom" else "owa"
            _snap = _rs_r.record_attempt(
                _mid, _ch, error, facts_hash=str(it.get("facts_hash") or ""), surface=_surf, allowed=True,
            )
            _try_n = int((_snap or {}).get("try_n") or 0)
            if _try_n >= int(getattr(_rs_r, "MAX_TRIES_PER_HASH", 3)):
                # Do not park the ask forever. Hand it to ask_do on a short retry.
                _update(
                    qk,
                    state="retry",
                    attempts=attempts,
                    error=error,
                    held_for_operator=False,
                    ask_do_due=True,
                    next_at=time.time() + 60,
                    storm_skip="ask_do_due",
                )
                _log(
                    f"storm_ask_do_due {(str(it.get('name') or qk))[:40]} try_n={_try_n}"
                )
                return "retry"
            # Still under cap: fall through to normal retry but delay ≥ BACKOFF_S.
            it = dict(it)
            it["_storm_min_delay"] = float(getattr(_rs_r, "BACKOFF_S", 4 * 3600))
        except Exception:
            pass
        now = time.time()
        key = _err_key(error)
        prev_hits = []
        for t in ((it.get("error_hits") or {}).get(key) or []):
            try:
                if now - float(t) < 7200:
                    prev_hits.append(float(t))
            except (TypeError, ValueError):
                pass
        prev_hits.append(now)
        _err_hits = dict(it.get("error_hits") or {})
        _err_hits[key] = prev_hits[-12:]
        if len(prev_hits) > 3 or it.get("held_for_operator"):
            if len(prev_hits) > 3 and not it.get("held_for_operator"):
                try:
                    import liberal_policy as _lp

                    _lp.append_draft_hold(
                        str(it.get("name") or ""),
                        str(it.get("subject") or it.get("channel") or ""),
                        error,
                    )
                except Exception:
                    pass
                _log(f"ask_do_due_park {(str(it.get('name') or qk))[:40]} n={len(prev_hits)}")
            _update(
                qk,
                state="retry",
                attempts=attempts,
                error=error,
                error_hits=_err_hits,
                held_for_operator=False,
                ask_do_due=True,
                next_at=time.time() + 60,
            )
            return "retry"
    if attempts >= MAX_ATTEMPTS:
        _update(qk, state="dead", attempts=attempts, error=error)
        return "dead"
    # HARD anti-starve: MLX flakes retry in seconds, not hour-long backoff.
    if _mlx_fail(error):
        delay = MLX_BACKOFF_S
    else:
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (attempts - 1)))
    delay = min(delay, 900)
    # Hold-class storm floor stays, but nothing waits more than 15 minutes.
    try:
        _storm_floor = float(it.get("_storm_min_delay") or 0)
        if _storm_floor > 0:
            delay = max(delay, min(_storm_floor, 900.0))
    except Exception:
        pass
    _fields = {"state": "retry", "attempts": attempts, "error": error, "next_at": time.time() + delay}
    if _err_hits is not None:
        _fields["error_hits"] = _err_hits
    try:  # v2 (2026-10-10): flag-file gated 2-strikes backoff; {} when flag absent
        import draft_v2 as _v2

        _fields.update(_v2.backoff_fields(qk, it, error, delay, mlx_fail=_mlx_fail(error)) or {})
    except Exception:
        pass
    # A backoff helper must not park an ask for hours. 15 minutes is the ceiling.
    try:
        _na = float(_fields.get("next_at") or 0)
        if _na > time.time() + 900:
            _fields["next_at"] = time.time() + 900
    except Exception:
        pass
    _update(qk, **_fields)
    return "retry"


def _resurrect_starved() -> int:
    """Pull mlx-dead / long-backoff items back to pending so supply never starves."""
    n = 0
    now = time.time()
    with _queue_locked():
        data = _load()
        for qk, it in list((data.get("items") or {}).items()):
            err = str(it.get("error") or "")
            st = it.get("state")
            if st == "dead" and _mlx_fail(err):
                it["state"] = "pending"
                it["attempts"] = min(int(it.get("attempts") or 0), 2)
                it["next_at"] = 0
                it["updated"] = now
                it["resurrected"] = now
                n += 1
            elif st == "retry" and _mlx_fail(err) and float(it.get("next_at") or 0) > now + MLX_BACKOFF_S:
                it["next_at"] = now
                it["updated"] = now
                n += 1
            elif st == "dead" and "work_brain" in err.lower():
                it["state"] = "pending"
                it["attempts"] = min(int(it.get("attempts") or 0), 1)
                it["next_at"] = 0
                it["updated"] = now
                n += 1
            elif st == "dead" and any(
                tok in err.lower()
                for tok in (
                    "quality_refuse", "topic_mismatch", "need_dig",
                    "ask_recipient_for_schema", "draft_mismatch",
                )
            ):
                # Refuse must not leave runner stuck — reopen for rewrite same item
                it["state"] = "pending"
                it["attempts"] = min(int(it.get("attempts") or 0), 2)
                it["next_at"] = 0
                it["updated"] = now
                it["prior_body"] = ""
                try:  # v2 (2026-10-10): flag-file gated resurrect backoff (storm engine)
                    import draft_v2 as _v2

                    it.update(_v2.resurrect_delay(qk, it) or {})
                except Exception:
                    pass
                n += 1
        if n:
            _save(data)
    if n:
        _log(f"resurrect_starved n={n}")
    return n


def _self_mend_draft(channel: str, it: dict, block: str, feeds, prev_err: str) -> dict:
    """Lean MLX retry (optional) → deterministic tee. Never leave an actionable source red.

    Frozen Outlook threads are not rewritten.
    """
    if channel == "outlook" and (
        feeds.owa_is_frozen(str((it or {}).get("message_id") or ""))
        or feeds.owa_is_frozen(str((it or {}).get("key") or ""))
    ):
        feeds._owa_frozen_log(str((it or {}).get("message_id") or (it or {}).get("key") or ""))
        return {
            "ok": False,
            "error": "owa_frozen",
            "skipped": "owa_frozen",
            "frozen": True,
            "never_send": True,
        }
    # HARD: if the failure WAS already mlx/hydrate/RAM, skip another lean mlx burn —
    # go straight to deterministic stage so the lane cannot starve under pressure.
    name = it.get("name") or "there"
    err_l = (prev_err or "").lower()
    skip_lean = _mlx_fail(prev_err) or any(
        x in err_l for x in ("composer_not_hydrated", "hydrate", "prefill", "507", "memory")
    )
    _log(f"self_mend begin ch={channel} skip_lean={skip_lean} err={prev_err[:80]}")
    draft: dict = {"ok": False, "error": prev_err}
    if not skip_lean:
        try:
            os.environ["OWA_COMPOSE_LEAN_FREE_MB"] = "99999"
            if hasattr(feeds, "_mlx_shed_for_compose"):
                feeds._mlx_shed_for_compose()
            if channel == "zoom":
                draft = feeds._zoom_draft_marker(
                    it["sid"],
                    name,
                    it.get("messages") or [],
                    sweep_block=block[:800] if block else "",
                    prior_body=it.get("prior_body") or "",
                )
            else:
                draft = feeds._owa_draft_marker(
                    it["message_id"],
                    name,
                    it.get("subject") or "",
                    it.get("body") or "",
                    sweep_block=block[:800] if block else "",
                )
            if draft.get("ok"):
                draft["compose_via"] = (draft.get("compose_via") or "") + "+lean_mend"
                return draft
        except Exception as e:
            _log(f"self_mend_lean_exc:{e}")
            draft = {"ok": False, "error": f"mend_lean:{e}"[:160]}
        finally:
            os.environ.pop("OWA_COMPOSE_LEAN_FREE_MB", None)

    # Outlook: deterministic fallback + tee
    if channel == "outlook":
        try:
            first = feeds._owa_first_name(name) or "there"
            subj = it.get("subject") or ""
            body = (it.get("body") or "")[:1200]
            fb = feeds._owa_deterministic_fallback(first, subj, body, it.get("body") or "")
            if fb and fb.get("skipped_noise"):
                return {
                    "ok": False,
                    "error": f"noise:{fb.get('skipped_noise')}",
                    "skipped_noise": fb.get("skipped_noise"),
                    "never_send": True,
                }
            text = (fb or {}).get("text") or ""
            if text.strip() and not feeds._body_preview_stubbish(text, "outlook"):
                tee = feeds._owa_reply_tee(
                    message_id=it["message_id"],
                    text=text.strip(),
                    supersede=True,
                    subject=subj,
                    sender=name or "",
                    inbound=(it.get("body") or ""),
                )
                if tee.get("ok"):
                    return {
                        "ok": True,
                        "draft_id": tee.get("draft_id") or tee.get("id"),
                        "compose_via": (fb or {}).get("via") or "runner_deterministic_mend",
                        "compose_chars": len(text),
                        "body_preview": text[:180],
                        "full_text": text,
                        "never_send": True,
                    }
        except Exception as e:
            _log(f"self_mend_det_exc:{e}")

    # Zoom: solve-hint first, then one-ask only if no path
    if channel == "zoom":
        try:
            msgs = it.get("messages") or []
            first = feeds._zoom_greet_first(name, msgs)
            if first and not feeds._zoom_is_group_or_channel(name):
                blob = feeds._zoom_thread_blob(msgs)
                latest = ""
                for m in reversed(msgs):
                    if not isinstance(m, dict):
                        continue
                    who = str(m.get("from") or "").strip().lower()
                    if m.get("from_self") or who in {"you", "operator, operator", "operator"}:
                        continue
                    latest = str(m.get("text") or m.get("body") or "").strip()
                    if latest:
                        break
                hint = feeds._deterministic_solve_hint(first, latest, blob)
                text = (hint or {}).get("text") if hint and hint.get("ok") else ""
                if not text or feeds._body_preview_stubbish(text, "zoom"):
                    return {"ok": False, "error": "no_deterministic_solve", "never_send": True}
                if not feeds._body_preview_stubbish(text, "zoom"):
                    if feeds.zoom_no_open():
                        feeds._log("zoom_no_open_block zoom_tee_mend")
                        _who_m = ""
                        try:
                            _who_m = feeds._zoom_greet_first(name, msgs) or ""
                        except Exception:
                            _who_m = ""
                        staged = feeds.zoom_stage_local(
                            it["sid"],
                            name or it["sid"],
                            text,
                            feeds._zoom_source_message_id(msgs),
                            sender_name=_who_m,
                            inbound=latest or "",
                        )
                        if staged.get("staged"):
                            return {
                                "ok": True,
                                "staged": True,
                                "via": "local_store",
                                "compose_via": "runner_zoom_local_store",
                                "staged_to_composer": bool(staged.get("staged_to_composer")),
                                "compose_chars": len(text),
                                "body_preview": text[:180],
                                "full_text": text,
                                "never_send": True,
                                "teed_up": True,
                            }
                        return staged
                    from zoom_safari_draft import tee as zoom_tee

                    tee = zoom_tee(
                        session_id=it["sid"],
                        display_name=name or it["sid"],
                        text=text,
                        navigate=True,
                        supersede=True,
                        prior_body=it.get("prior_body") or "",
                    )
                    teed = bool(tee.get("teed_up") or tee.get("composer_hydrated")) and bool(
                        tee.get("ok")
                    )
                    if teed and not feeds._body_preview_stubbish(
                        (tee.get("preview") or text)[:180], "zoom"
                    ):
                        return {
                            "ok": True,
                            "compose_via": "runner_zoom_deterministic_mend",
                            "compose_chars": len(text),
                            "body_preview": text[:180],
                            "full_text": text,
                            "never_send": True,
                            "teed_up": True,
                        }
        except Exception as e:
            _log(f"self_mend_zoom_exc:{e}")

    return draft if isinstance(draft, dict) else {"ok": False, "error": prev_err}


def pending(now: float | None = None) -> list[tuple[str, dict]]:
    now = time.time() if now is None else now
    with _queue_locked():
        data = _load()
    out = [(k, v) for k, v in data["items"].items()
           if v.get("state") in ("pending", "retry") and float(v.get("next_at") or 0) <= now]
    out.sort(key=lambda kv: float(kv[1].get("enqueued") or 0))
    return out


def _claim_run_lock() -> bool:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(RUN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid_s, ts_s = RUN_LOCK.read_text().split()[:2]
            alive = True
            try:
                os.kill(int(pid_s), 0)
            except (ProcessLookupError, ValueError):
                alive = False
            if not alive:
                RUN_LOCK.unlink(missing_ok=True)
                return _claim_run_lock()
        except Exception:
            pass
        return False


def _release_run_lock() -> None:
    try:
        RUN_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def iframe_sha() -> str:
    return hashlib.sha256(IFRAME_JS.read_bytes()).hexdigest()[:16]


def prove_gate(*, run_prove: bool = True) -> dict:
    """Cached per iframe sha for 12h. Runs the self-thread prove when stale + allowed."""
    sha = iframe_sha()
    try:
        c = json.loads(PROVE_CACHE.read_text())
        if c.get("ok") and c.get("sha") == sha and time.time() - float(c.get("at") or 0) < PROVE_TTL_S:
            return {"ok": True, "cached": True, "sha": sha}
    except Exception:
        pass
    self_sid = os.environ.get("ZOOM_SELF_SID", "").strip()
    if not self_sid:
        return {"ok": False, "error": "ZOOM_SELF_SID_unset", "sha": sha}
    if not run_prove:
        return {"ok": False, "error": "prove_stale", "sha": sha}
    if _zoom_no_open():
        _log("zoom_no_open_block prove")
        return {"ok": True, "cached": False, "sha": sha, "skipped": "ZOOM_NO_OPEN", "never_send": True}
    import zoom_safari_draft as zds

    out = zds.prove(self_sid=self_sid, display_name="self", navigate=True)
    ok = bool(out.get("ok")) and not out.get("send_suspect")
    # Soft accept: cleared + no send_suspect. Newlines optional — slate often
    # flattens breaks; open_zoom_unreads inline tee is the live SoT for drafts.
    if (
        not ok
        and not out.get("send_suspect")
        and out.get("cleared")
        and out.get("error") not in ("composer_missing", "self_composer_not_empty", "draft_lock_held")
    ):
        ok = True
        _log("prove_soft_ok cleared_no_suspect (newlines optional)")
    rec = {"ok": ok, "sha": sha, "at": time.time(), "result": {k: out.get(k) for k in (
        "ok", "error", "send_suspect", "verified", "cleared", "newlines_preserved", "hydrated")}}
    PROVE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROVE_CACHE.write_text(json.dumps(rec, indent=2, default=str) + "\n")
    if out.get("send_suspect"):
        _halt("prove_send_suspect")
    return {"ok": ok, "cached": False, "sha": sha, "error": None if ok else out.get("error"),
            "soft": bool(ok and not out.get("ok"))}


def _halt(reason: str) -> None:
    STOP.parent.mkdir(parents=True, exist_ok=True)
    STOP.write_text(json.dumps({"at": time.time(), "reason": reason}) + "\n")
    _log(f"HALT {reason}")


def _sweep(it: dict) -> dict:
    import full_sweep_dig as fsd

    if it["channel"] == "zoom":
        sw = fsd.sweep(person=it.get("name") or "", sid=it.get("sid") or "", channel="zoom",
                       live_messages=it.get("messages") or [])
    else:
        sw = fsd.sweep(person=it.get("name") or "", email=it.get("email") or "", channel="outlook",
                       subject=it.get("subject") or "", source_body=it.get("body") or "",
                       received_at=it.get("received_at") or "")
    try:
        import dig_toolkit as _dk

        if it.get("channel") == "zoom":
            try:
                import ops_safari_feeds as _feeds

                blob = _feeds._zoom_thread_blob(it.get("messages") or [])
            except Exception:
                blob = "\n".join(str((m or {}).get("text") or (m or {}).get("body") or "") for m in (it.get("messages") or []) if isinstance(m, dict))
        else:
            blob = (it.get("subject") or "") + "\n" + (it.get("body") or "")
        lane = _dk.location_family_facts(blob) if _dk.location_family_question(blob) else None
        counts = dict(sw.get("counts") or {})
        counts["operator"] = int((lane or {}).get("count") or 0)
        sw["counts"] = counts
        if isinstance(sw.get("facts"), list):
            for f in (lane or {}).get("facts") or []:
                if f not in sw["facts"]:
                    sw["facts"].append(f)
        _log(f"full-sweep dig counts={counts}")
    except Exception as _e:
        _log(f"operator_lane_exc {str(_e)[:80]}")
    return sw



def _learn_process_outcome(channel: str, it: dict, result: dict) -> None:
    """One row per real outcome. Called from every process() return that is not noise."""
    try:
        from core.draft_feedback_learning import record_runner_disposition
    except Exception as exc:
        _log(f"learn_disposition_fail:{exc}")
        return
    state = str(result.get("state") or "")
    err = str(result.get("error") or "")
    key = str(it.get("message_id") or it.get("sid") or it.get("key") or result.get("key") or "")[:96]
    if not key:
        return
    try:
        _kw = dict(
            surface="zoom" if channel == "zoom" else "outlook",
            source_key=key,
            thread_label=str(it.get("subject") or it.get("name") or "")[:240],
            disposition=f"{state}:{err}"[:120],
            draft_text=str(result.get("draft_text") or "")[:500],
            lesson=f"{state}:{err}"[:240],
        )
        try:  # v2 (2026-10-10): arm + retry outcome on the row; {} when flag absent
            import draft_v2 as _v2

            _ex = _v2.disposition_extra(str(result.get("key") or key))
        except Exception:
            _ex = {}
        if _ex:
            try:
                record_runner_disposition(**_kw, extra=_ex)
            except TypeError:
                record_runner_disposition(**_kw)
        else:
            record_runner_disposition(**_kw)
    except Exception as exc:
        _log(f"learn_disposition_fail:{exc}")


def _liberal_draft_through(draft: dict) -> bool:
    """A liberal body that already teed must not be retried as a dig/hold failure.

    Tee conflict / stubbish errors stay retryable (ok is false for those).
    """
    if not isinstance(draft, dict) or not draft.get("ok"):
        return False
    err = str(draft.get("error") or "")
    if err in {
        "existing_composer_conflict",
        "existing_draft_conflict",
        "stubbish_composer",
        "composer_missing",
        "composer_not_hydrated",
        "fill_failed",
        "stubbish_after_tee",
    }:
        return False
    text = str(draft.get("full_text") or draft.get("body_preview") or draft.get("text") or "")
    if not (draft.get("liberal") or re.search(r"(?i)\[operator:\s*decide\b", text)):
        return False
    try:
        import liberal_policy as _lp
        if not _lp.enabled():
            return False
        # Already teed drafts stay put (do not supersede/delete them).
        if draft.get("draft_id") or draft.get("kept_existing"):
            return True
        if text and _lp.reject_reason(text, "", ""):
            return False
        return True
    except Exception:
        return False


def process(qk: str, it: dict) -> dict:
    """One candidate end-to-end. Returns the proof-bearing result."""
    import ops_safari_feeds as feeds

    _ch_fz = str((it or {}).get("channel") or "")
    if _ch_fz == "outlook" and (
        feeds.owa_is_frozen(str((it or {}).get("message_id") or ""))
        or feeds.owa_is_frozen(str((it or {}).get("key") or ""))
    ):
        feeds._owa_frozen_log(str((it or {}).get("message_id") or (it or {}).get("key") or ""))
        return {"key": qk, "state": "owa_frozen", "skipped": "owa_frozen", "frozen": True}

    import full_sweep_dig as fsd

    try:  # v2 (2026-10-10): tell draft_v2 which queue item is being composed (arm key)
        import draft_v2 as _v2

        _v2.set_current(qk, str(it.get("content_hash") or ""))
    except Exception:
        pass

    channel = it["channel"]
    name = it.get("name") or ""
    if str(it.get("state") or "") == "discarded_by_operator" or (
            it.get("discarded_msg_id") and it.get("discarded_msg_id") == _newest_peer_id(it)):
        return {"key": qk, "state": "discarded_by_operator"}
    sid = str(it.get("sid") or "")
    if channel == "zoom" and it.get("messages"):
        # 2026-10-12 (operator): reply target = NEWEST non-operator message after operator's last reply, not list tail
        try:
            import draft_v2 as _v2o

            _b = float(it.get("enqueued") or 0) or None
            it["messages"] = _v2o.zoom_chrono_order(it["messages"], None, _b if _b and _b > 1e9 else None)
        except Exception:
            pass

    def _finish(result: dict) -> dict:
        """HARD: Zoom skip without a staged draft remints unread — EXCEPT when
        operator already owns a meaningful tip (tip_from_self / closed_in_sent).
        Reminting those caused Mercedes/Marc sticky unread + permanent skip.
        """
        staged = result.get("state") == "staged" or bool(result.get("staged"))
        # already not in live unread list — do not re-badge
        if result.get("state") != "parked_not_unread":
            _learn_process_outcome(channel, it, result)
        if result.get("state") == "parked_not_unread":
            return result
        # 2026-10-14 repick-storm: a terminal skip/close is HANDLED for this newest peer message; enqueue
        # will not re-pick it until a new peer message arrives (badge behaviour below is unchanged).
        if result.get("state") in ("closed_in_sent", "commsbot_only", "operator_retired", "skipped_noise"):
            try:
                _nid_h = _newest_peer_id(it)
                if _nid_h:
                    _update(qk, handled_msg_id=_nid_h)
            except Exception:
                pass
        handled = {
            "closed_in_sent",
            "operator_retired",
        }  # 2026-10-26: skipped_noise (group_no_ask/group_banter) and commsbot_only are NOT operator_read and must re-badge!
        err = str(result.get("error") or "")
        # HARD 2026-10-13 (Marie King): only a STAGED draft may leave a Zoom chat
        # read. tip_from_self/closed_in_sent/held/skipped Zoom items ALWAYS remint
        # unread (the scrape opened them). Non-Zoom handled items keep old behavior.
        if channel != "zoom" and (
            result.get("state") in handled
            or err in {"tip_from_self", "operator_retired"}
        ):
            result["remark_after_skip"] = {"ok": True, "skipped": "handled_no_remint"}
            return result
        # HARD 2026-10-15 (operator, Marie King): never remint a Zoom chat operator read/replied/closed.
        if channel == "zoom" and (result.get("state") in handled or err in {"tip_from_self", "operator_retired"}):
            try:
                feeds.zoom_note_operator_read(sid, "handled_no_remint_zoom")
            except Exception as exc:
                _log(f"zoom_note_operator_read_fail {(sid or '')[:40]}:{exc}")
            result["remark_after_skip"] = {"ok": True, "skipped": "handled_no_remint_zoom"}
            return result
        if channel == "zoom" and sid and not staged:  # re-enabled 2026-10-14 13:35 under prior-unread gate (operator)
            # v2 remark-once guard REMOVED 2026-10-13 (Marie King): every unstaged
            # Zoom skip/hold remints unread; never leave it read.
            # 2026-10-26: even with ZOOM_NO_OPEN, always restore unread badge via SDK markChatAsUnread
            try:
                remark = feeds.zoom_remark_unread(sid, was_unread=bool(it.get("live_unread")), last_from_self=(bool(feeds._zoom_tip_from_self(it.get("messages"))) if it.get("messages") else None), source_message_id=(_newest_peer_id(it) or None))
                result["remark_after_skip"] = {
                    "ok": remark.get("ok"),
                    "via": remark.get("via"),
                    "error": (remark.get("error") or "")[:120],
                }
                _log(
                    f"zoom_remark_after_skip {(name or sid)[:40]} "
                    f"state={result.get('state')} ok={remark.get('ok')} via={remark.get('via')}"
                )
            except Exception as e:
                result["remark_after_skip"] = {"ok": False, "error": str(e)[:120]}
                _log(f"zoom_remark_after_skip_exc {(name or sid)[:40]}:{e}")
            # HARD 2026-10-08 zoomdm (Vijay): stage hit client_lock_busy, remint
            # failed, next lap parked not_live_unread and the DM was lost read.
            # Our attempt cleared the badge -> keep the item runner-owned.
            if False and not (result.get("remark_after_skip") or {}).get("ok") and result.get(
                "state"
            ) not in TERMINAL:
                try:
                    _update(qk, force_reopen=True)
                    _log(f"zoom_force_reopen_after_remark_fail {(name or sid)[:40]}")
                except Exception:
                    pass
        return result

    if channel == "zoom" and sid and feeds._operator_retired_sid(sid):
        _update(qk, state="closed_in_sent", error="operator_retired")
        return _finish({"key": qk, "state": "closed_in_sent", "error": "operator_retired"})
    if channel == "zoom" and sid:
        # 2026-10-19 zoomfix: a draft already written for this exact inbound message is re-placed
        # (no recompose, no chat open). `staged` only once the reply box is confirmed.
        _lrec = _zoom_local_record(sid)
        try:
            _cur_src = feeds._zoom_source_message_id(it.get("messages") or [])
        except Exception:
            _cur_src = ""
        if _lrec.get("body") and _cur_src and str(_lrec.get("source_message_id") or "") == _cur_src:
            if feeds.zoom_no_open() and str(_lrec.get("body") or "").strip() and _lrec.get("staged_to_composer"):
                _update(qk, state="staged", error=None, staged_body=str(_lrec.get("body")),
                        handled_msg_id=_newest_peer_id(it), composer_attempts=0, staged_via=_lrec.get("staged_via") or "local_store")
                _log(f"zoom_no_open_local_staged {(name or sid)[:40]} replaced")
                return _finish({"key": qk, "state": "staged", "staged": True, "via": _lrec.get("staged_via") or "local_store",
                                "handled": "zoom_no_open_local"})
            _placed = None
            if not _lrec.get("staged_to_composer") or it.get("error") == "composer_draft_lost":
                _placed = feeds._composer_stage_ready_chat(sid, str(_lrec.get("body")), _cur_src)
                _ok = isinstance(_placed, dict) and _placed.get("ok") is True and _placed.get("reason") == "staged_to_composer"
            else:
                _ok = True
            if _ok:
                _update(qk, state="staged", error=None, staged_body=str(_lrec.get("body")),
                        handled_msg_id=_newest_peer_id(it), composer_attempts=0,
                        staged_via=(_placed or {}).get("via") or _lrec.get("staged_via") or "",
                        server_draft_id=(_placed or {}).get("draft_id") or _lrec.get("server_draft_id") or "")
                _log(f"zoom_replace_local_draft ok {(name or sid)[:40]} via={(_placed or {}).get('via')}")
                return _finish({"key": qk, "state": "staged", "staged": True, "via": (_placed or {}).get("via") or "local_store",
                                "handled": "replaced_existing_draft"})
            _ca = int(it.get("composer_attempts") or 0) + 1
            _why = str((_placed or {}).get("reason") or (_placed or {}).get("error") or "composer_unconfirmed")[:80]
            if _ca >= 12:
                _update(qk, state="held", error=f"composer_unconfirmed:{_why}", composer_attempts=_ca)
                return _finish({"key": qk, "state": "held", "error": f"composer_unconfirmed:{_why}"})
            _update(qk, state="retry", next_at=time.time() + 600, error=f"composer_unconfirmed:{_why}", composer_attempts=_ca)
            _log(f"zoom_replace_local_draft retry {(name or sid)[:40]} why={_why} n={_ca}")
            return {"key": qk, "state": "retry", "error": f"composer_unconfirmed:{_why}"}
    if channel == "zoom":
        live = _live_zoom_sids()
        if not sid or sid not in live:
            # ZOOM_NO_OPEN: passive digest only. Unanswered peer → local draft. No nav, no remark.
            _passive_go = False
            if feeds.zoom_no_open():
                _passive_msgs = feeds.zoom_passive_messages(sid, name)
                if feeds.zoom_passive_should_draft(_passive_msgs):
                    _src = feeds._zoom_source_message_id(_passive_msgs)
                    if feeds.zoom_local_same_source(sid, _src) and _zoom_local_record(sid).get("staged_to_composer"):
                        _update(qk, state="staged", error=None)
                        return _finish({
                            "key": qk,
                            "state": "staged",
                            "staged": True,
                            "via": "local_store",
                            "handled": "same_source",
                        })
                    it["messages"] = _passive_msgs
                    _passive_go = True
                    _log(f"zoom_passive_read_draft {(name or sid)[:40]}")
            if not _passive_go:
                # Explicit quality reopen may stage even if the badge flap cleared mid-cycle.
                if feeds.zoom_no_open() or not (it.get("force_reopen") or os.environ.get("ZOOM_FORCE_REOPEN") == "1" or _pending_from_them(it)):
                    if feeds.zoom_no_open() and (it.get("force_reopen") or os.environ.get("ZOOM_FORCE_REOPEN") == "1" or _pending_from_them(it)):
                        _log(f"zoom_no_open_block zoom_force_reopen_not_live {(name or sid)[:40]}")
                    else:
                        _log(f"zoom_park_not_unread {(name or sid)[:40]}")
                    _update(qk, state="parked_not_unread", error="not_live_unread")
                    return _finish({"key": qk, "state": "parked_not_unread", "error": "not_live_unread"})
                _log(f"zoom_force_reopen_not_live {(name or sid)[:40]}")
    # HARD operator 2026-09-28: discard once → try harder. Twice same hash → park
    # until a new message. Never bump the counter here (detect owns the count).
    # HARD 2026-10-21: discarded_by_user + unchanged content_hash → never re-stage.
    lr_key = sid if channel == "zoom" else str(it.get("message_id") or it.get("key") or "")
    if lr_key:
        surf = "zoom" if channel == "zoom" else "owa"
        prev = feeds.last_run_get(surf, lr_key) or {}
        n_disc = feeds._discard_count(prev)
        _ch_now = str(it.get("content_hash") or "")
        # heal legacy rows that set discarded_by_user without a counter
        if prev.get("discarded_by_user") and int(prev.get("discard_retry_n") or 0) == 0:
            feeds.last_run_put(
                surf,
                lr_key,
                {
                    "reopen_after_discard": True,
                    "discard_retry_n": max(n_disc, 2),
                    "discard_parked": True,
                    "discarded_by_user": True,
                    "action": "discarded_park_healed",
                },
            )
            n_disc = 2
        _disc_block = False
        try:
            import retry_storm as _rs_run

            _disc_block = _rs_run.discarded_blocks_restage(prev, _ch_now)
        except Exception:
            _disc_block = bool(
                (prev.get("discarded_by_user") or prev.get("discard_parked") or n_disc >= 1)
                and _ch_now
                and prev.get("content_hash") == _ch_now
            )
        if _disc_block or n_disc >= 2 or prev.get("discard_parked"):
            _update(qk, state="closed_in_sent", error="discarded_same_hash" if _disc_block else "discarded_twice")
            _log(f"runner_skip_discarded {channel} {lr_key[:48]} n={n_disc} storm={_disc_block}")
            return _finish({"key": qk, "state": "closed_in_sent", "error": "discarded_same_hash" if _disc_block else "discarded_twice"})
        # try_harder reopen only when content_hash changed (new inbound); never for same hash.
        if prev.get("discarded_by_user") and n_disc == 1 and not prev.get("try_harder_done"):
            _prior_ch = str(prev.get("content_hash") or "")
            if _ch_now and _prior_ch and _ch_now != _prior_ch:
                feeds.last_run_put(
                    surf,
                    lr_key,
                    {
                        "reopen_after_discard": True,
                        "discarded_by_user": False,
                        "try_harder": True,
                        "try_harder_done": True,  # only one reopen ever for this hash
                        "discard_retry_n": 1,
                        "discard_parked": False,
                        "drafted": False,
                        "draft_ok": False,
                        "atomic_ok": False,
                        "action": "runner_reopen_after_discard",
                    },
                )
                it["try_harder"] = True
                it["discard_retry_n"] = 1
                _log(f"runner_reopen_after_discard {channel} {lr_key[:48]} n=1")
            else:
                _update(qk, state="closed_in_sent", error="discarded_same_hash")
                _log(f"runner_skip_discarded_same_hash {channel} {lr_key[:48]}")
                return _finish({"key": qk, "state": "closed_in_sent", "error": "discarded_same_hash"})
    if channel == "zoom" and feeds.zoom_no_open() and not it.get("messages"):
        got = feeds.zoom_passive_messages(sid, name)
        if got:
            it["messages"] = got
            _log(f"zoom_passive_read {(name or sid)[:40]} n={len(got)}")
        else:
            _log(f"zoom_skip_no_passive_read {(name or sid)[:40]}")
            _update(qk, state="skipped_no_passive_read", error="skipped_no_passive_read")
            return _finish({"key": qk, "state": "skipped_no_passive_read", "error": "skipped_no_passive_read", "staged": False})
    if channel == "zoom":
        msgs = it.get("messages") or []
        if feeds._zoom_should_skip_self_tip(msgs, name):
            _update(qk, state="closed_in_sent", error="tip_from_self")
            _log(f"runner_skip_tip_from_self {(name or sid)[:40]}")
            result = {"key": qk, "state": "closed_in_sent", "error": "tip_from_self"}
            _learn_process_outcome(channel, it, result)
            return _finish(result)
        if feeds._zoom_tip_from_self(msgs) and feeds._zoom_needs_meaningful_draft(msgs, name):
            _log(f"runner_restage_after_stub_self {(name or sid)[:40]}")
        # HARD 2026-10-05: Name↔SID — room/DM display name must match queue sid key
        q_sid = str(it.get("sid") or it.get("key") or "").strip()
        if sid and q_sid and sid != q_sid and not q_sid.endswith(sid) and sid not in q_sid:
            st = _retry(qk, it, f"name_sid_mismatch:{sid[:24]}")
            return _finish({"key": qk, "state": st, "error": "name_sid_mismatch"})
        # Topic pre-check against prior_body (wrong SOP body must not re-zap)
        prior = str(it.get("prior_body") or it.get("staged_body") or "")
        if prior and hasattr(feeds, "_zoom_draft_matches_latest_inbound"):
            mm = feeds._zoom_draft_matches_latest_inbound(prior, msgs, name)
            if mm:
                it["prior_body"] = ""  # drop poisoned prior so compose starts clean
                _update(qk, prior_body="")
                _log(f"runner_clear_offtopic_prior {(name or sid)[:40]} why={mm}")
        gskip = feeds._zoom_group_should_skip_draft(name, sid, msgs)
        if gskip:
            _update(qk, state="skipped_noise", error=gskip, skipped_noise=gskip)
            return _finish({"key": qk, "state": "skipped_noise", "error": gskip})
    # v2 (2026-10-10) STALENESS GATE (default ON; draft_gates.json / draft_gates.OFF): never compose
    # a reply to an inbound older than N days (default 3) or already answered since by operator.
    try:
        import draft_v2 as _v2s

        _rows = None
        if channel != "zoom":
            try:
                _rows = (json.loads((HOME / ".openclaw/workspace/operator-local/digests/OUTLOOK_INBOX.json").read_text()) or {}).get("rows")
            except Exception:
                _rows = None
        _sk = _v2s.stale_check(channel, it, inbox_rows=_rows)
        if _sk.get("skip") and _sk.get("reason") == "answered_since" and channel == "zoom":
            # keep the existing stub-self-tip restage rule: operator's tip may be a stub that still needs a real reply
            try:
                if feeds._zoom_tip_from_self(it.get("messages") or []) and feeds._zoom_needs_meaningful_draft(
                    it.get("messages") or [], name
                ):
                    _sk = {"skip": False, "reason": "stub_self_tip_restage", "age_days": _sk.get("age_days")}
            except Exception:
                pass
        if _sk.get("reason") and it.get("v2_stale_logged") != it.get("content_hash"):
            _v2s.stale_event(qk, it, _sk)
            _update(qk, v2_stale_logged=it.get("content_hash") or "x")
        if _sk.get("skip"):
            _why = str(_sk.get("reason"))
            _log(f"v2_stale_skip {channel} {(name or qk)[:40]} why={_why} age_d={_sk.get('age_days')}")
            _update(qk, state="skipped_noise", error=_why, skipped_noise=_why)
            return _finish({"key": qk, "state": "skipped_noise", "error": _why})
    except Exception as _se:
        _log(f"v2_stale_exc:{str(_se)[:80]}")
    lane = lanes.lane_for(name=name, sid=sid, email=it.get("email") or "")
    if channel == "zoom" and lane["lane"] == lanes.LANE_COMMSBOT:
        lanes.handoff_to_commsbot(name=name, sid=sid, channel="zoom",
                                  reason=lane["reason"], content_hash=it.get("content_hash") or "")
        _update(qk, state="commsbot_only", lane=lane)
        return _finish({"key": qk, "state": "commsbot_only"})

    # JEV 2026-10-07 triage: confident fyi_noise skips sweep + Qwen compose. Fail-open:
    # Jev None/err/unsure -> compose exactly as before. try_harder items are never triaged.
    if not it.get("try_harder") and os.environ.get("JEV_DISABLE", "") != "1":
        try:
            import sys as _jsys
            from pathlib import Path as _JP
            _jcore = str(_JP.home() / "claude-stack" / "core")
            if _jcore not in _jsys.path:
                _jsys.path.append(_jcore)
            import jev_hooks as _jh
            if channel == "zoom":
                _jtxt = f"Zoom chat with {name}:\n" + str(feeds._zoom_thread_blob(it.get("messages") or []))[-3000:]
            else:
                _jtxt = f"Email from {name}\nSubject: {it.get('subject') or ''}\n\n" + str(it.get("body") or "")[:3000]
            _jk = _jh.triage(_jtxt, caller=f"ops_draft_runner:{channel}")
            if _jk == "fyi_noise":
                _log(f"jev_triage_skip_noise {channel} {(name or qk)[:40]}")
                _update(qk, state="skipped_noise", error="jev_fyi_noise", skipped_noise="jev_fyi_noise")
                return _finish({"key": qk, "state": "skipped_noise", "error": "jev_fyi_noise"})
        except Exception as e:
            _log(f"jev_triage_exc:{str(e)[:80]}")
    # Zoom and Outlook share process(). Flag file off -> no-op. Never sends or stages.
    try:
        import work_ask_enqueue as _wae

        _wae.handle_item(it)
    except Exception as _we:
        _log(f"work_ask_enqueue_exc:{str(_we)[:80]}")
    sw = _sweep(it)
    if not sw.get("gate_ok"):
        ge = str(sw.get("gate_error") or "")
        # Soft-open: work_brain blip must not starve the lane.
        if "work_brain" in ge.lower() or "unavailable" in ge.lower() or "timeout" in ge.lower():
            _log(f"sweep_soft_open {ge[:80]}")
            sw = {**sw, "gate_ok": True, "soft_gate": True, "gate_error": ge}
        else:
            st = _retry(qk, it, f"sweep_gate:{sw.get('gate_error')}")
            return _finish({"key": qk, "state": st, "error": sw.get("gate_error"), "queried": sw.get("queried")})
    if sw.get("closed_in_sent"):
        proof = fsd.write_proof(sw, {"outcome": "closed_in_sent"})
        _update(qk, state="closed_in_sent", proof=str(proof))
        return _finish({"key": qk, "state": "closed_in_sent", "proof": str(proof)})

    block = fsd.render_prompt_block(sw) if not sw.get("soft_gate") else ""
    # Under Metal pressure, skip the doomed fat MLX pass — go straight to mend.
    low_ram = False
    try:
        low_ram = float(feeds._mlx_free_mb()) < 450.0
    except Exception:
        low_ram = False
    if it.get("try_harder") and channel == "zoom":
        try:
            feeds._mlx_shed_for_compose()
        except Exception:
            pass
        dig = feeds._zoom_digest_msgs_for_person(sid, name)
        dig_peer = [
            m for m in dig if isinstance(m, dict) and feeds._zoom_msg_from_dm_peer(m, name)
        ]
        if dig_peer:
            it["messages"] = dig_peer
            _log(f"runner_try_harder_digest_peer {name[:40]} n={len(dig_peer)}")
    # Qwen resident (~16GB) makes free pages look tiny — that is NOT "MLX down".
    # Skipping compose here is what pasted the operator QR leftover on every Outlook draft.
    qwen_up = False
    try:
        qwen_up = bool(feeds._mlx_already_resident())
    except Exception:
        qwen_up = False
    if low_ram and not qwen_up and not it.get("try_harder"):
        _log(f"low_ram_skip_primary_mlx free_mb≈{getattr(feeds, '_mlx_free_mb', lambda: '?')()}")
        draft = _self_mend_draft(channel, it, block, feeds, "low_ram_preempt")
        source = (
            feeds._zoom_thread_blob(it.get("messages") or [])
            if channel == "zoom"
            else (it.get("body") or "")
        )
    elif channel == "zoom":
        draft = feeds._zoom_draft_marker(it["sid"], name, it.get("messages") or [],
                                         sweep_block=block, prior_body=it.get("prior_body") or "")
        source = feeds._zoom_thread_blob(it.get("messages") or [])
    else:
        try:  # conversation id must be visible to askgate.stage_block -> promote_if_assign_draft
            import sf_onsend_exec as _oe_pre
            _oe_pre.note_outlook_item(it)
        except Exception:
            pass
        draft = feeds._owa_draft_marker(it["message_id"], name, it.get("subject") or "",
                                        it.get("body") or "", sweep_block=block)
        source = it.get("body") or ""

    if draft.get("send_suspect") or draft.get("error") == "SEND_SUSPECT":
        _halt(f"send_suspect:{qk}")
        _update(qk, state="send_suspect", error="SEND_SUSPECT")
        return _finish({"key": qk, "state": "send_suspect"})

    err_now = str(draft.get("error") or "")
    if draft.get("self_check") == "fail" or (
        err_now.startswith("hold:") and not str(draft.get("full_text") or draft.get("text") or "").strip()
    ):
        draft = dict(draft)
        draft["ok"] = False
        draft["full_text"] = ""
        draft["text"] = ""
        draft["liberal"] = False
        err_now = str(draft.get("error") or "hold:self_check")
    # HARD 2026-10-05 NEVER DIE: topic/quality refuse must retry same item (advance lap),
    # never park as skipped_noise — that left Core Group stuck on wrong SOP body.
    # A liberal stage (ok + body) is not one of those refusals.
    if not _liberal_draft_through(draft) and (
        draft.get("rewrite_same_item")
        or err_now.startswith("quality_refuse")
        or err_now.startswith("draft_mismatch")
        or "topic_mismatch" in err_now
        or "need_dig_citation" in err_now
        or "ask_recipient_for_schema" in err_now
    ):
        st = _retry(qk, it, err_now[:160] or "quality_refuse")
        return _finish({"key": qk, "state": st, "error": err_now or "quality_refuse", "rewrite_same_item": True})
    if not _liberal_draft_through(draft) and (
        draft.get("skipped_noise")
        or err_now.startswith("noise:")
        or draft.get("error") in ("group_no_ask", "group_banter")
    ):
        _update(qk, state="skipped_noise", error=draft.get("skipped_noise") or draft.get("error"),
                handled_msg_id=_newest_peer_id(it))
        return _finish({"key": qk, "state": "skipped_noise", "error": draft.get("error")})

    if channel == "zoom" and str(draft.get("error") or "") == "skipped_no_passive_read":
        _update(qk, state="skipped_no_passive_read", error="skipped_no_passive_read")
        return _finish({"key": qk, "state": "skipped_no_passive_read", "error": "skipped_no_passive_read", "staged": False})

    if not draft.get("ok"):
        err = str(draft.get("error") or "stage_failed")
        # Anti-starve: mend MLX / compose flakes before parking as retry.
        if _mlx_fail(err) or "compose" in err.lower() or "stubbish" in err.lower():
            draft = _self_mend_draft(channel, it, block, feeds, err)
            if draft.get("skipped_noise"):
                _update(qk, state="skipped_noise", error=draft.get("skipped_noise"))
                return _finish({"key": qk, "state": "skipped_noise"})
        if not draft.get("ok"):
            st = _retry(qk, it, str(draft.get("error") or err)[:160])
            return _finish({"key": qk, "state": st, "error": draft.get("error") or err})
    # belt + braces: the marker already gated compose; refuse to COUNT a stub that slipped by
    det = osd.detect_body(draft.get("full_text") or "", channel, source=source)

    # draft THEN remark (the stage re-opened / re-read the source)
    if channel == "zoom":
        remark = feeds.zoom_remark_unread(it["sid"], was_unread=bool(it.get("live_unread")), last_from_self=(bool(feeds._zoom_tip_from_self(it.get("messages"))) if it.get("messages") else None), source_message_id=(_newest_peer_id(it) or None))
        remarked = bool(remark.get("ok")) and not remark.get("skipped")
    else:
        remark = feeds._owa_mark_unread(message_id=it["message_id"], was_unread=bool(it.get("live_unread") or it.get("source_unread")))
        remarked = bool(remark.get("ok")) and not remark.get("skipped")

    proof = fsd.write_proof(sw, {
        "outcome": "staged",
        "stage_channel": channel,
        "body_preview": (draft.get("body_preview") or "")[:180],
        "compose_chars": draft.get("compose_chars"),
        "draft_id": draft.get("draft_id"),
        "remarked_unread": remarked,
        "remark": remark,
        "detect": det,
        "sent": False,
    })
    if channel == "zoom" and str(draft.get("via") or "") == "local_store" and not draft.get("staged_to_composer"):
        # 2026-10-27: Even under ZOOM_NO_OPEN, require staged_to_composer (via non-opening zoom_server_draft).
        if False and feeds.zoom_no_open() and str(draft.get("full_text") or "").strip():
            _log(f"zoom_no_open_local_staged {(name or it.get('sid') or '')[:40]}")
        else:
            # 2026-10-19 zoomfix: saved on disk only. Not `staged` until the reply box is confirmed;
            # the pre-hook re-places this same body next lap without recomposing.
            _update(qk, state="retry", next_at=time.time() + 600, staged_body=draft.get("full_text") or "",
                    proof=str(proof), error="composer_unconfirmed:local_only",
                    composer_attempts=int(it.get("composer_attempts") or 0) + 1)
            _log(f"zoom_stage_unconfirmed_retry {(name or it.get('sid') or '')[:40]}")
            return _finish({"key": qk, "state": "retry", "error": "composer_unconfirmed:local_only", "staged": False})
    _update(qk, state="staged", staged_body=draft.get("full_text") or "", handled_msg_id=_newest_peer_id(it),
            remarked=remarked, proof=str(proof), error=None if remarked else "remark_failed")
    if channel == "outlook":
        try:
            import sf_onsend_exec as _oe_st
            _oe_st.record_staged_assign(str(draft.get("full_text") or draft.get("text") or ""), it)
        except Exception as _oe_e:
            _log("sf_onsend_stage_exc:%s" % type(_oe_e).__name__)
    feeds.last_run_put("zoom" if channel == "zoom" else "owa", it["key"], {
        "action": "runner_stage_remark",
        "draft_ok": True,
        "remarked": remarked,
        "atomic_ok": remarked,
        "draft_proof": (draft.get("body_preview") or "")[:120],
    })
    result = {"key": qk, "state": "staged", "remarked": remarked, "proof": str(proof),
              "draft_text": (draft.get("body_preview") or "")[:180], "staged": True}
    if channel == "zoom" and str(draft.get("via") or "") == "local_store":
        result["via"] = "local_store"
        result["staged"] = True
    # HARD 2026-10-05: restore continuous learning pending row so sent_edited can record
    try:
        from core.draft_feedback_learning import record_staged_draft
        full = str(draft.get("full_text") or draft.get("text") or result.get("draft_text") or "")
        record_staged_draft(
            surface="zoom" if channel == "zoom" else "outlook",
            source_key=str(it.get("message_id") or it.get("sid") or it.get("key") or qk)[:96],
            source_text=str(source or "")[:4000],
            draft_text=full,
            thread_label=str(it.get("subject") or it.get("name") or "")[:240],
            actor=str(it.get("name") or "")[:240],
            native_draft_marker=str(draft.get("draft_id") or "")[:96],
        )
    except Exception as exc:
        _log(f"learn_staged_draft_fail:{exc}")
    _learn_process_outcome(channel, it, result)
    return result



def _spawn_outcome_observer() -> None:
    """Detached read-only observer of operator's sent/edited/deleted verdicts (draft_outcomes.json flag)."""
    import subprocess

    flag = RUNTIME / "draft_outcomes.json"
    try:
        if not json.loads(flag.read_text()).get("enabled"):
            return
    except Exception:
        return
    stamp = RUNTIME / "draft_outcomes.last"
    now = time.time()
    try:
        if now - stamp.stat().st_mtime < 600:
            return
    except Exception:
        pass
    stamp.write_text(str(now))
    logf = open(RUNTIME / "draft_outcomes.log", "a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / "draft_outcome_observer.py")],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    _log("v2_outcome_observer_spawned")


def _repay_unread_remarks() -> dict:
    """Zoom only: put back badges still owed from a real unread dot. Never sends. Outlook stays killed."""
    import ops_safari_feeds as feeds
    fixed = failed = skipped = 0
    self_sid = "redacted-email"
    try:
        print("zoom_owed_repay start", flush=True)
        feeds.zoom_seed_owed_from_today()
        rows = feeds.zoom_owed_rows()
    except Exception as e:
        print("zoom_owed_repay fail %s" % str(e)[:120], flush=True)
        _log("remark_repay_owed_fail:%s" % e)
        return {"fixed": 0, "failed": 1, "skipped": "owed_fail"}
    already = None
    try:
        with _queue_locked():
            data = _load()
        items = [it for it in (data.get("items") or {}).values() if isinstance(it, dict)]
    except Exception:
        items = []
    by_sid = {str(it.get("sid")): it for it in items if it.get("sid")}
    for row in rows:
        sid = str((row or {}).get("sid") or "")
        if not sid or sid == self_sid:
            skipped += 1
            continue
        if already is not None and sid in already:
            skipped += 1
            print("zoom_owed_repay skip already_unread sid=%s" % sid[:48], flush=True)
            continue
        it = by_sid.get(sid) or {}
        last_from_self = None
        if it.get("messages"):
            try:
                last_from_self = bool(feeds._zoom_tip_from_self(it.get("messages")))
            except Exception:
                last_from_self = None
        if last_from_self is True:
            try:
                feeds.zoom_clear_owed(sid, "latest_from_operator")
            except Exception:
                pass
            skipped += 1
            print("zoom_owed_repay skip latest_from_operator sid=%s" % sid[:48], flush=True)
            continue
        # The owed list is not a fresh open. Restoring here re-marked chats operator had already read.
        live = set()
        try:
            live = _live_zoom_sids()
        except Exception:
            live = set()
        if live and sid not in live and sid not in getattr(feeds, "_ZOOM_OPENED_THIS_PASS", set()):
            mid = str((row or {}).get("source_message_id") or _newest_peer_id(it) or "")
            try:
                feeds._zoom_send()._remint_ledger_note_operator_cleared(sid, mid or None, "dot_cleared_without_open")
            except Exception as e:
                _log("zoom_operator_clear_fail %s" % str(e)[:80])
            try:
                feeds.zoom_clear_owed(sid, "dot_cleared")
                feeds.zoom_consume_preopen(sid)
            except Exception:
                pass
            skipped += 1
            _log("zoom_remark_skip operator_read sid=%s" % sid[:48])
            print("zoom_owed_repay skip operator_read sid=%s" % sid[:48], flush=True)
            continue
        skipped += 1
        _log("zoom_remark_skip not_this_open sid=%s" % sid[:48])
        print("zoom_owed_repay skip not_this_open sid=%s" % sid[:48], flush=True)
    print("zoom_owed_repay fixed=%s failed=%s skipped=%s" % (fixed, failed, skipped), flush=True)
    _log("remark_repay owed fixed=%s failed=%s skipped=%s" % (fixed, failed, skipped))
    return {"fixed": fixed, "failed": failed, "skipped": skipped}


def _repay_unread_remarks_DISABLED_DO_NOT_CALL() -> dict:
    """Put the badge back on anything this runner opened and left read. Never sends."""
    import ops_safari_feeds as feeds
    fixed = 0
    failed = 0
    try:
        with _queue_locked():
            data = _load()
        items = list((data.get("items") or {}).items())
    except Exception as e:
        _log("remark_repay_queue_fail:%s" % e)
        items = []
    for qk, it in items:
        if not isinstance(it, dict):
            continue
        if it.get("channel") != "zoom" or it.get("state") != "staged":
            continue
        if it.get("error") != "remark_failed":
            continue
        sid = str(it.get("sid") or "")
        if not sid:
            continue
        try:
            if feeds.zoom_no_open():
                feeds._log("zoom_no_open_skip_remark repay")
                remark = {"ok": False, "skipped": "ZOOM_NO_OPEN"}
            else:
                remark = feeds.zoom_remark_unread(sid)
        except Exception as e:
            remark = {"ok": False, "error": str(e)[:120]}
        if remark.get("ok"):
            _update(qk, error=None, remarked=True)
            fixed += 1
            _log("remark_repay_ok zoom %s" % (it.get("name") or sid)[:40])
        else:
            failed += 1
            err = str(remark.get("error") or "")
            _log(
                "remark_repay_fail zoom %s %s"
                % ((it.get("name") or sid)[:40], err[:80])
            )
            if any(s in err.lower() for s in ("timeout", "connect_over_cdp", "handshake", "target crashed")):
                _log("remark_repay_cdp_down")
                return {"fixed": fixed, "failed": failed, "stopped": "cdp"}
    try:
        last = json.loads(feeds.LAST_RUN_PATH.read_text()) if feeds.LAST_RUN_PATH.exists() else {}
        owa = (last.get("surfaces") or {}).get("owa") or {}
    except Exception as e:
        _log("remark_repay_owa_load:%s" % e)
        owa = {}
    now = time.time()
    for mid, entry in list(owa.items()):
        if not isinstance(entry, dict) or entry.get("remarked") is not False:
            continue
        if entry.get("discarded_by_user") or entry.get("discard_parked"):
            continue
        if now - float(entry.get("at") or 0) > 3 * 86400:
            continue
        try:  # v2 remark fix (2026-10-10): repay only for a REAL staged draft, max 2 tries, never for no-draft rows
            import draft_v2 as _v2p

            if _v2p.remark_fix_on():
                if not (entry.get("draft_ok") and entry.get("drafted")) or entry.get("action") in (
                    "noise_handled", "handled_no_draft", "drain_defer",
                ):
                    continue
                _n = int(entry.get("remark_repay_n") or 0)
                if _n >= 2:
                    continue
                feeds.last_run_put("owa", str(mid), {"remark_repay_n": _n + 1})
        except Exception:
            pass
        try:
            mark = feeds._owa_mark_unread(message_id=str(mid))
        except Exception as e:
            mark = {"ok": False, "error": str(e)[:120]}
        if mark.get("ok"):
            feeds.last_run_put("owa", str(mid), {"remarked": True, "atomic_ok": True, "error": None})
            fixed += 1
            _log("remark_repay_ok owa %s" % str(entry.get("subject") or mid)[:40])
        else:
            failed += 1
            _log("remark_repay_fail owa %s" % str(entry.get("subject") or mid)[:40])
    if fixed or failed:
        _log("remark_repay fixed=%s failed=%s" % (fixed, failed))
    return {"fixed": fixed, "failed": failed}


def run(*, max_n: int = 8, run_prove: bool = True) -> dict:
    if STOP.exists():
        return {"ok": False, "halted": True, "reason": STOP.read_text()[:200], "never_send": True}
    if not _claim_run_lock():
        return {"ok": True, "skipped": "locked", "never_send": True}
    results: list[dict] = []
    resurrected = 0
    try:
        resurrected = _resurrect_starved()
        try:  # v2 (2026-10-10): flag-gated READ-ONLY verdict observer, detached, <=1 run / 10 min
            _spawn_outcome_observer()
        except Exception:
            pass
        repaid = _repay_unread_remarks()
        parked_zoom = park_stale_zoom_queue()
        todo = pending()[:max_n]
        # Safari SoT: Zoom is drafted inline in feed_zoom. Bridge SoT: queue + runner.
        outlook_todo = [(qk, it) for qk, it in todo if it.get("channel") == "outlook"]
        via = (os.environ.get("ZOOM_DRAFT_VIA") or "").strip().lower()
        force = (os.environ.get("ZOOM_RUNNER_FORCE") or "").strip() in ("1", "true", "yes")
        zoom_todo = []
        if via == "bridge" or force:
            zoom_todo = [(qk, it) for qk, it in todo if it.get("channel") == "zoom"]
        gate = {
            "ok": True,
            "skipped": None if zoom_todo else "zoom_inline_sot",
            "parked_zoom": parked_zoom,
            "zoom_via": via or "safari",
            "zoom_runner": bool(zoom_todo),
        }
        work = outlook_todo + zoom_todo
        for qk, it in work:
            if STOP.exists():
                break
            try:
                _r = process(qk, it)
            except (Exception, SystemExit) as e:
                _r = {"key": qk, "state": _retry(qk, it, f"exc:{e}"[:160]), "error": str(e)[:160]}
            results.append(_r)
            if it.get("channel") == "zoom" and _r.get("state") != "staged":
                _why = str(_r.get("error") or _r.get("state") or "unstaged")[:120]
                print("zoom_skip reason=%s state=%s key=%s" % (_why, _r.get("state"), str(qk)[:80]), flush=True)
    finally:
        _release_run_lock()
    staged = [r for r in results if r.get("state") == "staged"]
    out = {
        "ok": not STOP.exists() and (
            len(staged) == 0
            or all(r.get("remarked") for r in staged)
        ),
        "halted": STOP.exists(),
        "prove_gate": gate if "gate" in locals() else None,
        "processed": len(results),
        "staged": len(staged),
        "remark_failed": sum(1 for r in staged if not r.get("remarked")),
        "held": sum(1 for r in results if r.get("state") == "held"),
        "retry": sum(1 for r in results if r.get("state") == "retry"),
        "resurrected": resurrected,
        "results": results,
        "never_send": True,
        "anti_starve": True,
    }
    _log(f"run processed={out['processed']} staged={out['staged']} held={out['held']} "
         f"retry={out['retry']} halted={out['halted']}")
    return out


def fixture_compose(
    *,
    channel: str,
    name: str,
    subject: str,
    body: str,
    email: str = "",
) -> dict:
    """Compose a draft from synthetic inbound — no unread mail, never stages/tees/sends.

    Use to exercise Qwen + gates when both Outlook and Zoom are empty.
    HARD 2026-10-07: sets COMPOSE_FIXTURE_LEAN so proof runs skip deep_solve
    digs (resident Qwen otherwise disables RAM-lean and burns 10–20m).
    """
    import ops_safari_feeds as feeds

    # Keep JSON on stdout clean (compose logs go to the feeds log file only).
    os.environ["LLM_QUIET_OPS"] = "1"
    os.environ["COMPOSE_FIXTURE_LEAN"] = "1"

    channel = (channel or "").strip().lower()
    name = (name or "Fixture").strip()
    subject = (subject or "FIXTURE: draft runner selftest").strip()
    body = (body or "").strip()
    if channel not in ("outlook", "zoom"):
        return {"ok": False, "error": "channel must be outlook|zoom", "never_send": True}
    if len(body) < 40:
        return {"ok": False, "error": "body too short (need >=40 chars)", "never_send": True}
    composed: dict = {}
    try:
        if channel == "outlook":
            composed = feeds._compose_owa_reply_local(name, subject, body)
        else:
            msgs = [
                {
                    "from": name,
                    "text": body,
                    "body": body,
                    "ts": time.time(),
                    "sender": name,
                }
            ]
            compose = getattr(feeds, "_compose_zoom_reply_local", None)
            if callable(compose):
                composed = compose(name, msgs, sweep_block="")
            else:
                composed = feeds._zoom_draft_marker(
                    "fixture-no-unread-sid",
                    name,
                    msgs,
                    sweep_block="",
                    prior_body="",
                )
                if composed.get("draft_id") or composed.get("teed"):
                    return {
                        "ok": False,
                        "error": "fixture refused live zoom tee",
                        "never_send": True,
                        "composed": composed,
                    }
    finally:
        os.environ.pop("COMPOSE_FIXTURE_LEAN", None)
    text = (composed.get("text") or composed.get("full_text") or "").strip()
    return {
        "ok": bool(composed.get("ok") and text),
        "fixture": True,
        "channel": channel,
        "name": name,
        "subject": subject,
        "via": composed.get("via"),
        "error": composed.get("error"),
        "text": text,
        "compose_chars": len(text),
        "never_send": True,
        "staged": False,
        "teed": False,
        "sent": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--no-prove", action="store_true", help="never run the self-thread prove")
    ap.add_argument("--status", action="store_true")
    ap.add_argument(
        "--fixture",
        choices=("outlook", "zoom"),
        help="compose from synthetic inbound — no unread required; never stages",
    )
    ap.add_argument("--body-file", help="with --fixture: path to inbound body text")
    ap.add_argument("--body", default="", help="with --fixture: inline inbound body")
    ap.add_argument("--subject", default="FIXTURE: draft runner selftest")
    ap.add_argument("--name", default="Fixture")
    ap.add_argument("--email", default="")
    args = ap.parse_args()
    if args.status:
        with _queue_locked():
            data = _load()
        counts: dict[str, int] = {}
        for v in data["items"].values():
            counts[v.get("state") or "?"] = counts.get(v.get("state") or "?", 0) + 1
        print(json.dumps({"counts": counts, "stop": STOP.exists(), "prove_sha": iframe_sha()}, indent=2))
        return 0
    if args.fixture:
        body = (args.body or "").strip()
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8", errors="replace").strip()
        out = fixture_compose(
            channel=args.fixture,
            name=args.name,
            subject=args.subject,
            body=body,
            email=args.email,
        )
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1
    out = run(max_n=args.max, run_prove=not args.no_prove)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
