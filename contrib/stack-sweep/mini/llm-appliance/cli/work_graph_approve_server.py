#!/usr/bin/env python3
"""One-click approve / revise server for work_graph riskier actions.

  http://127.0.0.1:8791/approve/<token>?s=<sig>
  http://127.0.0.1:8791/deny/<token>?s=<sig>
  http://127.0.0.1:8791/health

GET never posts or drops an item (mail prefetch must not apply).
POST /approve runs the existing execute_approval path (ADO comment post).
GET /deny opens an editor. POST /deny saves the revision as the pending
comment, or saves it and then posts that revision.

Bind: WORK_GRAPH_APPROVE_HOST (default 0.0.0.0) · PORT 8791
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLI = Path.home() / "llm-appliance" / "cli"
if str(CLI) not in sys.path:
    sys.path.insert(0, str(CLI))

import work_graph_exec as wge  # noqa: E402

_NAA_BIN = Path.home() / "claude-stack" / "bin"   # 2026-10-19 emailfix: outcome_html renderer
if str(_NAA_BIN) not in sys.path:
    sys.path.insert(0, str(_NAA_BIN))

HOST = os.environ.get("WORK_GRAPH_APPROVE_HOST", "127.0.0.1")  # fail closed; plist sets Tailscale IP
PORT = int(os.environ.get("WORK_GRAPH_APPROVE_PORT", "8791"))
_MAX_BODY = 65536


def _page(title: str, body: str, ok: bool = True) -> bytes:
    color = "#0a7a32" if ok else "#b00020"
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#1a1a1a">
  <h1 style="color:{color};font-size:22px">{html.escape(title)}</h1>
  <div style="line-height:1.5">{body}</div>
</body></html>"""
    return doc.encode()


def hmac_ok(item: dict, token: str, sig: str) -> bool:
    import hmac
    got = str(item.get("sig") or "")
    if not sig or not got:
        return False
    try:
        return hmac.compare_digest(got, sig) and hmac.compare_digest(wge._sign(token), sig)
    except Exception:
        return False


def _load_signed(token: str, sig: str) -> tuple[dict | None, str]:
    path = wge.APPROVALS / f"{token}.json"
    if not path.exists():
        return None, "unknown_token"
    try:
        item = json.loads(path.read_text())
    except Exception:
        return None, "bad_item"
    if not hmac_ok(item, token, sig):
        return None, "bad_sig"
    return item, ""


def _comment_text(item: dict) -> str:
    action = item.get("action") or {}
    payload = action.get("payload") or {}
    return str(payload.get("text") or "")


def save_comment_revision(token: str, sig: str, text: str) -> dict:
    """Write revised ADO comment text back onto the pending item. Does not post."""
    item, err = _load_signed(token, sig)
    if err:
        return {"ok": False, "error": err}
    if item.get("status") == "approved":
        return {"ok": False, "error": "status_approved", "item": item}
    action = dict(item.get("action") or {})
    if action.get("kind") not in ("ado_comment", "sfc_comment"):
        return {"ok": False, "error": "not_comment_action", "item": item}
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return {"ok": False, "error": "empty_comment", "item": item}
    if len(text) > 1500:
        return {"ok": False, "error": "bad_text", "item": item}
    payload = dict(action.get("payload") or {})
    payload["text"] = text
    action["payload"] = payload
    item["action"] = action
    # A revise puts the item back in the queue. It does not deny or post.
    item["status"] = "pending"
    item.pop("deny_confirmed", None)
    item["revised_at"] = time.time()
    item["summary"] = wge._action_summary(action)
    path = wge.APPROVALS / f"{token}.json"
    path.write_text(json.dumps(item, indent=2, default=str) + "\n")
    if action.get("kind") == "ado_comment":
        gate = wge.evaluate_comment_gate(
            text,
            ticket_id=str(item.get("ticket_id") or payload.get("id") or ""),
            ran=item.get("tool_evidence"),
        )
        if payload.get("mention_reply"):
            reasons = [r for r in gate.get("reasons", []) if r != "status_only_no_answer_no_change"]
            gate = {"ok": not reasons, "reasons": reasons, "ticket_id": str(item.get("ticket_id") or payload.get("id") or "")}
    else:
        gate = {"ok": True, "reasons": []}
    return {"ok": True, "item": item, "gate": gate}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[wg-approve] {self.address_string()} {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        # GET never mutates. Mail clients and loopback prefetch were
        # hitting /deny and consuming the token before operator clicked Approve.
        self._route(commit=False)

    def do_POST(self) -> None:  # noqa: N802
        self._route(commit=True)

    def _read_form(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > _MAX_BODY:
            n = _MAX_BODY
        raw = self.rfile.read(n)
        return parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)

    def _route(self, *, commit: bool) -> None:
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query or "")
        if path == "/health":
            self._json({"ok": True, "service": "work-graph-approve", "editor": True})
            return
        if path == "/followups" and not commit:  # 2026-11-02 read-only follow-up status page
            self._followups_page()
            return
        parts = [p for p in path.split("/") if p]
        if path == "/stories":  # 2026-10-21 storyreq: local list of stories waiting for operator (read-only page)
            import story_request as _sr
            rows = "".join(
                f"<div style='border:1px solid #ddd;border-radius:6px;padding:12px;margin:12px 0'>"
                f"<pre style='white-space:pre-wrap;font-size:13px;margin:0 0 8px'>{html.escape(_sr.story_text(x['story']))}</pre>"
                f"<a href='{html.escape(x['approve_url'])}'>Create this story</a> &nbsp;|&nbsp; "
                f"<a href='{html.escape(x['deny_url'])}'>Drop it</a></div>" for x in _sr.pending_items())
            self._html(_page("Stories waiting for your OK", rows or "<p>None right now.</p>"
                             "<p style='color:#666;font-size:13px'>Opening a link does nothing until you press its button.</p>"), 200)
            return
        if len(parts) == 2 and parts[0] == "dismiss":  # TICKETSUG-20261104
            token = parts[1]
            sig = (qs.get("s") or [""])[0]
            if commit:
                form = self._read_form()
                if not sig:
                    sig = (form.get("s") or [""])[0]
            self._dismiss(token, sig, commit=commit)
            return
        if len(parts) == 2 and parts[0] in ("approve", "deny"):
            token = parts[1]
            sig = (qs.get("s") or [""])[0]
            ua = (self.headers.get("User-Agent") or "")[:180]
            sys.stderr.write(
                f"[wg-approve] {'POST' if commit else 'GET'} {parts[0]} "
                f"token={token[:8]} ua={ua!r}\n"
            )
            _story = self._is_story(token)  # 2026-10-21 storyreq
            if not commit:
                if parts[0] == "approve":
                    self._confirm(parts[0], token, sig)
                elif self._is_suggest(token):
                    self._suggest_editor(token, sig)
                elif _story:
                    self._story_drop_page(token, sig)
                else:
                    self._editor(token, sig)
                return
            form = self._read_form()
            if not sig:
                sig = (form.get("s") or [""])[0]
            if parts[0] == "deny" and self._is_suggest(token):  # TICKETSUG-20261104
                self._suggest_post(token, sig, form)
                return
            if parts[0] == "deny" and _story:
                import story_request as _sr
                item, err = _load_signed(token, sig)
                res = {"ok": False, "error": err} if err else _sr.deny(token, sig)
                self._html(_page("Dropped" if res.get("ok") else "Not dropped",
                                 "<p>Nothing was created. The reply will be drafted the normal way.</p>" if res.get("ok")
                                 else f"<p>{html.escape(str(res.get('error')))}</p>", ok=bool(res.get("ok"))), 200)
                return
            if parts[0] == "approve":
                result = wge.execute_approval(token, sig)
                self._result_page("approve", result)
                return
            self._revise(token, sig, form)
            return
        if len(parts) == 3 and parts[0] == "naa" and parts[1] in ("approve", "deny"):
            self._naa(parts[1], parts[2], qs, commit=commit)
            return
        if len(parts) == 3 and parts[0] in ("n", "brief") and not commit:
            self._next_steps(parts[1], parts[2], context=(parts[0] == "brief"))
            return
        if len(parts) == 3 and parts[0] == "d":
            self._decision(parts[1], parts[2], commit=commit)
            return
        if len(parts) == 3 and parts[0] == "apply":  # 2026-10-23 ADO bridge: staged agent update
            dry = (qs.get("dry") or [""])[0] == "1"
            if commit and not dry:
                self._read_form()
            self._apply_staged(parts[1], parts[2], commit=commit, dry=dry)
            return
        if len(parts) == 2 and parts[0] == "operatorbot":
            self._operatorbot(parts[1], (qs.get("s") or [""])[0] or (self._read_form().get("s") or [""])[0] if commit else (qs.get("s") or [""])[0], commit=commit)
            return
        self.send_response(404)
        data = _page("Not found", "<p>unknown path</p>", ok=False)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _next_steps(self, ticket: str, token: str, *, context: bool) -> None:
        """2026-10-23: Next steps link. GET only, read-only. /n -> 302 to nextsteps:// (meta refresh fallback); /brief -> text."""
        if context:
            txt = wge.next_steps_brief(ticket, token)
            if txt is None:
                self._html(_page("Not found", "<p>This link is not valid.</p>", ok=False), 404); return
            data = txt.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        mode = os.environ.get("NEXT_STEPS_MODE", "agent").strip().lower()
        if mode == "agent":
            url, why = wge.next_steps_launch(ticket, token)
            sys.stderr.write(f"[wg-approve] next_steps ticket={ticket} result={why}\n")
            if url:
                esc = html.escape(url, quote=True)
                data = (f"<!doctype html><meta http-equiv='refresh' content='0;url={esc}'>"
                        f"<p><a href='{esc}'>Open next steps</a></p>").encode("utf-8")
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            mode = "page"  # launch failed: show the brief instead
            _why_failed = why
        if mode != "scheme":
            # No launcher is installed yet, so show the brief itself with a copy button.
            txt = wge.next_steps_brief(ticket, token)
            if txt is None:
                self._html(_page("Not found", "<p>This link is not valid.</p>", ok=False), 404); return
            _wf = locals().get("_why_failed") or ""
            if _wf == "webhook_pending":
                _banner = ("<meta http-equiv='refresh' content='4'>"
                           "<p style='background:#e6f4ea;padding:10px;border-radius:4px'><b>Starting cloud task in mini-gateway...</b> "
                           "This page refreshes automatically and will redirect into Cursor as soon as the session URL is ready (about 10-15 seconds).</p>")
            elif _wf.endswith(":credit"):
                _banner = ("<p style='background:#fff4ce;padding:10px;border-radius:4px'><b>Waiting on Cursor credit.</b> "
                           "The handoff is ready, but Cursor would not start it because the account's prepaid balance is used up. "
                           "Once credit is back, tap Next steps in the email again and it will start. "
                           "Until then you can copy the brief below.</p>")
            elif _wf.startswith("launch_failed"):
                _banner = ("<p style='background:#fff4ce;padding:10px;border-radius:4px'><b>Cursor did not start this one.</b> "
                           "Try the Next steps link again in a few minutes. Until then you can copy the brief below.</p>")
            else:
                _banner = ""
            body = _banner + ("<p><button onclick=\"navigator.clipboard.writeText(document.getElementById('b').innerText)\" "
                    "style='padding:10px 14px;font-size:15px'>Copy brief</button></p>"
                    f"<pre id='b' style='white-space:pre-wrap;font-size:13px;background:#f6f8fa;padding:12px'>{html.escape(txt)}</pre>")
            self._html(_page("Next steps", body), 200); return
        target = wge.next_steps_target(ticket, token)
        if not target:
            self._html(_page("Not found", "<p>This link is not valid.</p>", ok=False), 404); return
        esc = html.escape(target, quote=True)
        data = ("<!doctype html><html><head><meta charset='utf-8'>"
                f"<meta http-equiv='refresh' content='0;url={esc}'><title>Next steps</title></head>"
                f"<body><p><a href='{esc}'>Open next steps</a></p></body></html>").encode("utf-8")
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _apply_staged(self, ticket: str, token: str, *, commit: bool, dry: bool) -> None:
        """GET shows the staged ADO update with one button (mail prefetch never writes).
        POST (operator's tap) writes comment, then optional state, exactly once. ?dry=1 never writes.
        APPLY_ONE_TAP=1 makes the GET itself apply."""
        ab = wge._ado_bridge()
        root = ab.STAGED
        p = root / f"{ticket}.json"
        try:
            st = json.loads(p.read_text())
        except Exception:
            st = None
        if not st or str(st.get("token")) != token:
            self._html(_page("Not found", "<p>This link is not valid.</p>", ok=False), 404); return
        one_tap = os.environ.get("APPLY_ONE_TAP") == "1"
        if dry:
            res = ab.apply(ticket, token, dry_run=True, root=root)
            sys.stderr.write(f"[wg-approve] apply_dry ticket={ticket}\n")
            self._json(res); return
        if not commit and not one_tap:
            if st.get("status") == "applied":
                self._html(_page("Already applied", "<p>This update was already posted to ADO.</p>"), 200); return
            body = (f"<p><b>ADO {html.escape(ticket)}</b></p><pre style='white-space:pre-wrap'>{html.escape(ab.comment_text(st))}</pre>"
                    + (f"<p>Then set state to <b>{html.escape(st['state'])}</b>.</p>" if st.get("state") else "")
                    + "<form method='post'><button style='padding:10px 14px;font-size:15px'>Apply to ADO</button></form>")
            self._html(_page("Apply to ADO", body), 200); return
        res = ab.apply(ticket, token, dry_run=False, caller="approve_post" if commit else "approve_get_one_tap")
        sys.stderr.write(f"[wg-approve] apply ticket={ticket} ok={res.get('ok')} already={bool(res.get('already'))} err={str(res.get('error') or '')[:60]}\n")
        if res.get("already"):
            self._html(_page("Already applied", "<p>This update was already posted to ADO. Nothing was posted again.</p>"), 200)
        elif res.get("ok"):
            self._html(_page("Posted", "<p>The comment is on the work item" + (" and the state was updated" if st.get("state") else "") + ".</p>"), 200)
        else:
            ado_url = f"https://operator/operator/operator/_workitems/edit/{html.escape(ticket)}"
            manual = ("<p>You can post it yourself: copy the text and open the work item.</p>"
                      f"<pre id='c' style='white-space:pre-wrap'>{html.escape(ab.comment_text(st))}</pre>"
                      "<p><button onclick=\"navigator.clipboard.writeText(document.getElementById('c').innerText)\">Copy text</button> "
                      f"&nbsp; <a href='{ado_url}'>Open ADO {html.escape(ticket)}</a></p>")
            self._html(_page("Not posted", f"<p>{html.escape(str(res.get('error')))}</p>"
                             + "".join(f"<p>{html.escape(str(r))}</p>" for r in (res.get("reasons") or [])[:5]) + manual, ok=False), 200)

    def _decision(self, ticket: str, token: str, *, commit: bool) -> None:
        if not commit:
            page, code = wge.decision_page(ticket, token)
        else:
            form = self._read_form()
            page, code = wge.decision_submit(
                ticket,
                token,
                (form.get("option") or [""])[0],
                (form.get("other") or [""])[0],
            )
        if isinstance(page, str):
            page = page.encode("utf-8")
        self._html(page, code)

    def _naa(self, verb: str, ticket: str, qs: dict, *, commit: bool) -> None:
        """2026-10-17 needs_operator email buttons. GET = confirm page only (mail prefetch-safe). POST = act.
        Approve runs needs_operator_actions.py approve <t> --nonce N --execute (operatorBot tip / gated ADO comment lane).
        Deny records the denial and sends operator to the ADO work item with a suggested comment. Signed + nonce-bound."""
        import hmac as _h, subprocess as _sp
        nonce = (qs.get("n") or [""])[0]; sig = (qs.get("s") or [""])[0]
        if commit and (not nonce or not sig):
            f = self._read_form(); nonce = nonce or (f.get("n") or [""])[0]; sig = sig or (f.get("s") or [""])[0]
        if not (ticket.isdigit() and nonce and sig and _h.compare_digest(sig, wge._sign(f"naa:{verb}:{ticket}:{nonce}"))):
            self._html(_page("Link not valid", "<p>unknown or unsigned link</p>", ok=False), 403); return
        tool = str(Path.home() / "claude-stack" / "bin" / "needs_operator_actions.py")
        cards = json.loads(_sp.run(["python3", tool, "cards"], capture_output=True, text=True, timeout=60).stdout or "[]")
        c = next((x for x in cards if x.get("ticket") == ticket), None)
        if not c or c.get("nonce") != nonce:
            self._html(_page("Out of date", "<p>This email is older than the current analysis for this ticket. Use the newest email in the thread.</p>", ok=False), 409); return
        url = html.escape(c["deny"]["open_url"])
        if verb == "deny":
            sug = html.escape(c["deny"]["suggested_comment"])
            if commit:
                _sp.run(["python3", tool, "deny", ticket, "--record"], capture_output=True, text=True, timeout=60)
                self.send_response(303); self.send_header("Location", c["deny"]["open_url"]); self.end_headers(); return
            body = (f"<p><b>{html.escape(c.get('title') or ticket)}</b></p><p>Suggested comment (copy, then edit on the ticket):</p>"
                    f"<textarea style='width:100%;height:90px;font-size:14px'>{sug}</textarea>"
                    f"<form method='POST' action='{html.escape(self.path)}'><button type='submit' style='padding:10px 16px;background:#b00020;color:#fff;border:0;border-radius:6px;font-weight:600'>Record deny and open ticket</button></form>"
                    f"<p style='font-size:13px'>or just <a href='{url}'>open the ticket</a> without recording.</p>")
            self._html(_page("Deny recommendation", body, ok=False), 200); return
        a = c["approve"]
        links = c.get("links") or [{"label": f"ADO {ticket}", "url": c["deny"]["open_url"]}]
        link_html = "".join(f"<li><a href='{html.escape(l['url'])}' target='_blank'>{html.escape(l['label'])}</a> "
                            f"<span style='color:#666;font-size:12px'>{html.escape(l['url'])}</span></li>" for l in links)
        if commit:
            # 2026-10-19 emailfix: show the REAL lane outcome (done / held / handed_off / not_wired / failed), never a blanket "Approved".
            r = _sp.run(["python3", tool, "approve", ticket, "--nonce", nonce, "--execute"], capture_output=True, text=True, timeout=240)
            sys.stderr.write(f"[wg-approve] naa approve {ticket} rc={r.returncode}\n")
            try: res = json.loads(r.stdout)
            except Exception: res = {}
            o = res.get("outcome")
            if o:
                import needs_operator_actions as _naa
                emailed = _sp.Popen(["python3", tool, "outcome-email", ticket], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True) is not None
                lane_int = f"<p style='font-size:12px;color:#666'>Lane: {html.escape(str(o.get('lane') or ''))}</p>"
                raw = html.escape(json.dumps(o.get("raw") or {}, indent=2, default=str)[:3000])
                body = (_naa.outcome_html(o) + lane_int +
                        f"<details style='margin-top:8px'><summary style='font-size:12px;color:#666'>Raw lane result</summary><pre style='white-space:pre-wrap;font-size:12px;background:#f6f8fa;padding:10px'>{raw}</pre></details>" +
                        ("<p style='font-size:13px;color:#555'>A copy of this result is being emailed to you in the same ticket thread.</p>" if emailed else ""))
                title = {"verified_closed": "Verified closed", "staged": "Draft staged (not sent)", "staged_unverified": "Draft staged, not yet confirmed", "done": "Done", "done_unverified": "Posted, not yet confirmed", "handed_off": "Handed off, not done yet",
                         "held": "Not done: held", "not_wired": "Not run: not wired yet", "unknown": "Unconfirmed"}.get(o.get("status"), "Failed")
                self._html(_page(title, body, ok=bool(o.get("ok"))), 200); return
            why = res.get("refused") or res.get("error") or (r.stderr.strip().splitlines() or ["no result from the action lane"])[-1]
            extra = res.get("why") or ""
            body = (f"<p><b>{html.escape(c.get('title') or ticket)}</b></p><p>Nothing ran. Reason: <b>{html.escape(str(why))}</b></p>" +
                    (f"<p>{html.escape(str(extra))}</p>" if extra else "") + f"<ul>{link_html}</ul>" +
                    f"<details><summary style='font-size:12px;color:#666'>Raw</summary><pre style='white-space:pre-wrap;font-size:12px'>{html.escape((r.stdout or r.stderr)[-2500:])}</pre></details>")
            self._html(_page("Not run", body, ok=False), 200); return
        txt = a.get("comment") or a.get("draft") or a.get("close_comment") or ""
        pc = c.get("precheck") or {}
        will = bool(pc.get("will_run"))
        pc_html = (f"<div style='padding:10px 12px;border-radius:6px;margin:10px 0;background:{'#dafbe1' if will else '#ffebe9'};color:{'#1a7f37' if will else '#cf222e'};font-size:14px'>"
                   f"<b>{'Pre-check: will run' if will else 'Pre-check: will NOT run'}</b> &mdash; {html.escape(pc.get('summary') or '')}</div>")
        prev = ""
        try:
            stp = Path.home() / "llm-appliance/runtime/work-loop/action-cards" / f"{ticket}.json"
            po = (json.loads(stp.read_text()) if stp.exists() else {}).get("outcome") or {}
            if po.get("nonce") == nonce:
                prev = (f"<div style='padding:8px 12px;border-radius:6px;background:#fff8e5;font-size:13px;margin:8px 0'>Already approved for this card at "
                        f"{html.escape(str(po.get('at')))}: <b>{html.escape(str(po.get('status')))}</b> &mdash; {html.escape(str(po.get('summary')))}</div>")
        except Exception:
            pass
        label = "Approve and run" if will else "Approve anyway (pre-check says nothing will run)"
        body = (f"<p><b>{html.escape(c.get('title') or ticket)}</b></p><p style='margin:4px 0'>Ticket:</p><ul style='margin:0 0 8px 18px;padding:0'>{link_html}</ul>"
                f"<p style='font-size:13px;color:#555'>Card <code>{html.escape(nonce)}</code> &middot; Action: {html.escape(str(c.get('lane_public') or ''))}</p>"
                f"{prev}{pc_html}<p>Lane: {html.escape(a['lane'])}</p><ol>" +
                "".join(f"<li>{html.escape(x)}</li>" for x in a["steps"]) + "</ol>" +
                (f"<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>{html.escape(txt)}</pre>" if txt else "") +
                f"<form method='POST' action='{html.escape(self.path)}'><button type='submit' style='padding:10px 16px;background:{'#0a7a32' if will else '#6e7781'};color:#fff;border:0;border-radius:6px;font-weight:600'>{html.escape(label)}</button></form>"
                "<p style='color:#666;font-size:13px'>Nothing happens until you press the button. Opening the mail link does not approve. "
                "After it runs, this page shows exactly what happened (and a copy is emailed to you in the ticket thread).</p>")
        self._html(_page("Confirm approve", body, ok=True), 200)

    def _operatorbot(self, job_id: str, sig: str, *, commit: bool) -> None:
        """2026-10-12: 'Run via operatorBot' for first-line support fixes. GET = confirm page only.
        POST = enqueue the job file to operatorBot on the agent bus. This server never mutates operator."""
        import hmac as _h
        import support_playbooks as sp
        good = bool(sig) and _h.compare_digest(sig, wge._sign("operatorbot:" + job_id))
        tip_p = sp.TIPS / f"{job_id}.json"
        if not good or not tip_p.exists():
            self._html(_page("Link not valid", "<p>unknown or unsigned job</p>", ok=False), 403)
            return
        tip = json.loads(tip_p.read_text())
        pu = tip.get("proposed_update") or {}
        st = str(tip.get("status") or "")
        if commit:
            res = sp.enqueue_approved(job_id)
            sys.stderr.write(f"[wg-approve] operatorbot {job_id} ok={res.get('ok')}\n")
            self._html(_page("Sent to operatorBot" if res.get("ok") else "Not sent",
                             f"<p>{html.escape(json.dumps(res))}</p><p>operatorBot executes, verifies with a SELECT and writes the receipt; "
                             f"the drafter then re-drafts the reply as done.</p>", ok=bool(res.get("ok"))), 200)
            return
        body = (f"<p><b>{html.escape(str(pu.get('kind')))}</b> for {html.escape(str(pu.get('user') or pu.get('contact') or pu.get('account')))}</p>"
                f"<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>{html.escape(json.dumps(pu, indent=2))}</pre>"
                f"<p>why: {html.escape(str(pu.get('why')))}<br>verify: <code>{html.escape(str(tip.get('verify_soql')))}</code><br>status: {html.escape(st)}</p>")
        if st == "proposed_awaiting_operator_approval":
            body += (f"<form method='POST' action='{html.escape(self.path)}'><button type='submit' style='padding:10px 16px;background:#0a7a32;color:#fff;"
                     f"border:0;border-radius:6px;font-weight:600'>Run via operatorBot</button></form>"
                     f"<p style='color:#666;font-size:13px'>Nothing happens until you press the button. operatorBot is the only thing that changes operator.</p>")
        self._html(_page("Confirm operatorBot fix", body, ok=True), 200)

    def _confirm(self, verb: str, token: str, sig: str) -> None:
        item_path = wge.APPROVALS / f"{token}.json"
        summary = "unknown request"
        status = ""
        item = None
        if item_path.exists() and sig:
            try:
                item = json.loads(item_path.read_text())
                if hmac_ok(item, token, sig):
                    summary = wge._action_summary(item.get("action") or {})
                    status = str(item.get("status") or "")
                else:
                    item = None
            except Exception:
                summary = "could not read request"
                item = None
        if status == "approved":
            self._html(_page("Already applied", f"<p>{html.escape(summary)}</p>", ok=True), 200)
            return
        if status == "denied" and item and item.get("deny_confirmed") and verb == "approve":
            self._html(_page("Already denied", f"<p>{html.escape(summary)}</p>", ok=False), 200)
            return
        is_noop = bool(item and (item.get("action") or {}).get("kind") == "test_noop")
        kind_now = (item.get("action") or {}).get("kind") if item else ""
        action = "Run test approve (no changes)" if is_noop else ("Post this operator comment" if kind_now == "sfc_comment" else "Post this ADO comment")
        if kind_now == "ado_suggest":  # TICKETSUG-20261104
            action = "Approve & Post"
        preview = ""
        if kind_now == "ado_suggest" and item:  # TICKETSUG-20261104
            try:
                ts = self._suggest_mod()
                story = ((item.get("action") or {}).get("payload") or {}).get("story") or {}
                preview = (
                    "<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>"
                    + html.escape((story.get("type") or "") + ": " + (story.get("title") or ""))
                    + "</pre><p style='white-space:pre-wrap'>"
                    + html.escape(story.get("description") or "")
                    + "</p>"
                )
            except Exception:
                preview = ""
        if item and (item.get("action") or {}).get("kind") == "ado_create":  # 2026-10-21 storyreq
            import story_request as _sr
            action = "Create this story"
            preview = ("<p style='margin:12px 0 4px;font-size:13px;color:#555'>Story that will be created</p>"
                       "<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>"
                       f"{html.escape(_sr.story_text(((item.get('action') or {}).get('payload') or {}).get('story') or {}))}</pre>"
                       "<p style='color:#666;font-size:13px'>Once it exists, the reply with the story number goes to your drafts. Nothing is sent.</p>")
        if item and (item.get("action") or {}).get("kind") in ("ado_comment", "sfc_comment"):
            preview = (
                "<p style='margin:12px 0 4px;font-size:13px;color:#555'>Comment that will be posted</p>"
                f"<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>"
                f"{html.escape(_comment_text(item))}</pre>"
            )
        body = (
            f"<p>{html.escape(summary)}</p>"
            f"{preview}"
            f"<form method='POST' action='{html.escape(self.path)}'>"
            f"<button type='submit' style='padding:10px 16px;background:#0a7a32;color:#fff;"
            f"border:0;border-radius:6px;font-weight:600'>{html.escape(action)}</button>"
            f"</form>"
            f"<p style='color:#666;font-size:13px'>Nothing happens until you press the button. "
            f"Opening the mail link does not approve or deny.</p>"
        )
        self._html(_page("Confirm approve", body, ok=True), 200)


    def _is_suggest(self, token: str) -> bool:
        try:
            it = json.loads((wge.APPROVALS / f"{token}.json").read_text())
            return (it.get("action") or {}).get("kind") == "ado_suggest"
        except Exception:
            return False

    def _suggest_mod(self):
        tools = Path.home() / "claude-stack" / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import ticket_suggest as ts
        return ts

    def _suggest_editor(self, token: str, sig: str, *, notice: str = "") -> None:
        item, err = _load_signed(token, sig)
        if err:
            self._html(_page("Cannot open editor", f"<p>{html.escape(err)}</p>", ok=False), 400)
            return
        if item.get("status") == "approved":
            self._html(_page("Already applied", "<p>This suggestion was already used.</p>"), 200)
            return
        ts = self._suggest_mod()
        story = ((item.get("action") or {}).get("payload") or {}).get("story") or {}
        note = f"<p>{html.escape(notice)}</p>" if notice else ""
        body = note + ts.edit_html(story, self.path, sig)
        self._html(body.encode(), 200)

    def _suggest_post(self, token: str, sig: str, form: dict) -> None:
        item, err = _load_signed(token, sig)
        if err:
            self._html(_page("Cannot edit", f"<p>{html.escape(err)}</p>", ok=False), 400)
            return
        intent = ((form.get("intent") or ["post"])[0] or "post").strip().lower()
        ts = self._suggest_mod()
        if intent == "cancel":
            wge.deny_approval(token, sig)
            self._html(ts.confirm_html({"ok": True, "cancelled": True, "created": False}).encode(), 200)
            return
        flat = {key: (form.get(key) or [""])[0] for key in (
            "title", "type", "description", "acceptance", "area", "iteration", "parent", "source_url")}
        story = ts.apply_form(((item.get("action") or {}).get("payload") or {}).get("story") or {}, flat)
        action = dict(item.get("action") or {})
        payload = dict(action.get("payload") or {})
        payload["story"] = story
        action["payload"] = payload
        item["action"] = action
        item["_intent"] = "post"
        item["_form"] = flat
        path = wge.APPROVALS / f"{token}.json"
        path.write_text(json.dumps(item, indent=2, default=str) + "\n")
        result = wge.execute_approval(token, sig)
        self._result_page("approve", result)

    def _dismiss(self, token: str, sig: str, *, commit: bool) -> None:
        item, err = _load_signed(token, sig)
        if err:
            self._html(_page("Link not valid", f"<p>{html.escape(err)}</p>", ok=False), 400)
            return
        ts = self._suggest_mod()
        if not commit:
            body = (
                "<p>This suggestion will not be shown again.</p>"
                f"<form method='POST' action='{html.escape(self.path)}'>"
                f"<input type='hidden' name='s' value='{html.escape(sig)}'>"
                "<button type='submit' style='padding:10px 16px'>Dismiss</button></form>"
                "<p style='color:#666;font-size:13px'>Opening this page does not dismiss it.</p>"
            )
            self._html(_page("Dismiss suggestion", body, ok=False), 200)
            return
        try:
            ts.remember_dismiss(Path.home(), item)
        except Exception:
            pass
        wge.deny_approval(token, sig)
        self._html(ts.confirm_html({"ok": True, "dismissed": True, "created": False}).encode(), 200)

    def _is_story(self, token: str) -> bool:
        try:
            it = json.loads((wge.APPROVALS / f"{token}.json").read_text())
            return (it.get("action") or {}).get("kind") == "ado_create"
        except Exception:
            return False

    def _story_drop_page(self, token: str, sig: str) -> None:
        item, err = _load_signed(token, sig)
        if err:
            self._html(_page("Link not valid", f"<p>{html.escape(err)}</p>", ok=False), 400)
            return
        import story_request as _sr
        st = ((item.get("action") or {}).get("payload") or {}).get("story") or {}
        body = (f"<pre style='white-space:pre-wrap;background:#f6f8fa;padding:12px;font-size:13px'>{html.escape(_sr.story_text(st))}</pre>"
                f"<form method='POST' action='{html.escape(self.path)}'><input type='hidden' name='s' value='{html.escape(sig)}'>"
                "<button type='submit' style='padding:10px 16px;background:#b00020;color:#fff;border:0;border-radius:6px;font-weight:600'>"
                "Drop this story</button></form><p style='color:#666;font-size:13px'>Opening this link does not drop anything.</p>")
        self._html(_page("Drop story?", body, ok=False), 200)

    def _editor(self, token: str, sig: str, *, notice: str = "") -> None:
        item, err = _load_signed(token, sig)
        if err:
            self._html(
                _page("Cannot open editor", f"<p>{html.escape(err)}</p>", ok=False),
                400,
            )
            return
        if item.get("status") == "approved":
            summary = html.escape(wge._action_summary(item.get("action") or {}))
            self._html(_page("Already applied", f"<p>{summary}</p>", ok=True), 200)
            return
        text = _comment_text(item)
        kind = (item.get("action") or {}).get("kind") or ""
        summary = html.escape(wge._action_summary(item.get("action") or {}))
        note = f"<p style='padding:8px 12px;background:#fff8e6;border-radius:6px'>{html.escape(notice)}</p>" if notice else ""
        payload_ed = ((item.get("action") or {}).get("payload") or {})
        gate = wge.evaluate_comment_gate(
            text,
            ticket_id=str(item.get("ticket_id") or ""),
            ran=item.get("tool_evidence"),
        ) if kind == "ado_comment" and text.strip() else {"ok": True, "reasons": []}
        if kind == "ado_comment" and payload_ed.get("mention_reply"):
            reasons_ed = [r for r in gate.get("reasons", []) if r != "status_only_no_answer_no_change"]
            gate = {"ok": not reasons_ed, "reasons": reasons_ed, "ticket_id": str(item.get("ticket_id") or "")}
        gate_html = ""
        if kind == "ado_comment" and not gate.get("ok"):
            reasons = ", ".join(gate.get("reasons") or [])
            gate_html = (
                "<p style='color:#8a5a00;font-size:13px'>Comment gate would hold this text: "
                f"{html.escape(reasons)}. You can still save it as the pending comment. "
                "Post will not run until the gate passes.</p>"
            )
        action = html.escape(self.path)
        buttons = (
            "<button type='submit' name='intent' value='save' "
            "style='padding:10px 16px;margin-right:8px;background:#333;color:#fff;border:0;border-radius:6px;font-weight:600'>"
            "Save as pending</button>"
        )
        if kind != "ado_comment" or gate.get("ok"):
            buttons += (
                "<button type='submit' name='intent' value='post' "
                "style='padding:10px 16px;background:#0a7a32;color:#fff;border:0;border-radius:6px;font-weight:600'>"
                "Approve and post this revision</button>"
            )
        else:
            buttons += (
                "<button type='submit' name='intent' value='post' "
                "style='padding:10px 16px;background:#0a7a32;color:#fff;border:0;border-radius:6px;font-weight:600'>"
                "Check again and post</button>"
            )
        body = (
            f"{note}"
            f"<p>{summary}</p>"
            f"{gate_html}"
            f"<form method='POST' action='{action}'>"
            f"<label for='text' style='display:block;font-size:13px;color:#555;margin-bottom:6px'>"
            f"Comment text</label>"
            f"<textarea id='text' name='text' rows='12' "
            f"style='width:100%;box-sizing:border-box;font:14px/1.45 ui-monospace,Menlo,monospace;padding:10px'>"
            f"{html.escape(text)}</textarea>"
            f"<input type='hidden' name='s' value='{html.escape(sig)}'>"
            f"<p style='margin-top:12px'>{buttons}</p>"
            f"</form>"
            f"<p style='color:#666;font-size:13px'>Opening this link does not drop or post the comment. "
            f"Save keeps the revision as the pending text. Post uses the same approve path as the Approve link.</p>"
        )
        self._html(_page("Revise comment", body, ok=True), 200)

    def _revise(self, token: str, sig: str, form: dict) -> None:
        intent = ((form.get("intent") or ["save"])[0] or "save").strip().lower()
        if intent not in ("save", "post"):
            intent = "save"
        text = (form.get("text") or [""])[0]
        item, err = _load_signed(token, sig)
        if err:
            self._html(_page("Cannot revise", f"<p>{html.escape(err)}</p>", ok=False), 400)
            return
        kind = (item.get("action") or {}).get("kind")
        if intent == "post" and kind not in ("ado_comment", "sfc_comment"):
            result = wge.execute_approval(token, sig)
            self._result_page("approve", result)
            return
        saved = save_comment_revision(token, sig, text)
        if not saved.get("ok"):
            self._editor(token, sig, notice=f"Not saved: {saved.get('error') or 'failed'}")
            return
        if intent != "post":
            self._editor(token, sig, notice="Saved. This is now the pending comment. Nothing was posted.")
            return
        gate = saved.get("gate") or {}
        if not gate.get("ok"):
            reasons = ", ".join(gate.get("reasons") or []) or "comment_gate"
            self._editor(
                token,
                sig,
                notice=f"Not posted ({reasons}). The revision is saved as the pending comment.",
            )
            return
        result = wge.execute_approval(token, sig)
        self._result_page("approve", result)

    def _result_page(self, verb: str, result: dict) -> None:
        ok = bool(result.get("ok"))
        item = result.get("item") or {}
        detail = result.get("result") or result
        summary = html.escape(str(item.get("summary") or result.get("error") or ""))
        pre = html.escape(json.dumps(detail, indent=2, default=str)[:4000])
        title = "Approved" if verb == "approve" and ok else (
            "Denied" if verb == "deny" and ok else "Failed"
        )
        fup = (detail.get("followup") if isinstance(detail, dict) else None) or {}
        fup_banner = (
            f"<p style='color:#0a7a32;font-weight:600'>Follow-up {'already queued' if fup.get('deduped') else 'queued'} "
            f"({html.escape(str(fup.get('action_kind') or 'background_job'))}). "
            f"{'It waits for PR approval and merge before any prep task, and prod still needs your GO. ' if fup.get('action_kind') == 'deploy' else ''}"
            f"<a href='/followups'>status</a></p>"
            if fup.get("triggered") else ""
        )
        if isinstance(detail, dict) and detail.get("idempotent_skip"):
            fup_banner += "<p style='color:#8a5300'>Already posted. Nothing was posted again.</p>"
        if ((item.get("action") or {}).get("kind") == "ado_suggest"):  # TICKETSUG-20261104
            try:
                ts = self._suggest_mod()
                page = ts.confirm_html(detail if isinstance(detail, dict) else {"ok": ok})
                self._html(page.encode(), 200 if ok else 400)
                return
            except Exception:
                pass
        body = f"<p>{summary}</p>{fup_banner}<pre style='background:#f6f8fa;padding:12px;overflow:auto;font-size:12px'>{pre}</pre>"
        self._html(_page(title, body, ok=ok), 200 if ok else 400)

    def _followups_page(self) -> None:
        fdir = Path.home() / "llm-appliance" / "runtime" / "work-loop" / "mention-followups"
        rows = []
        for f in sorted(fdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            try:
                m = json.loads(f.read_text())
            except Exception:
                continue
            if "ticket_id" not in m:
                continue
            drafts = "".join(
                f"<div style='margin:6px 0;padding:8px;background:#fff8e1;border-radius:4px'><b>draft reply to "
                f"{html.escape(str(d.get('reviewer')))} on PR {html.escape(str(d.get('pr')))}</b> (not posted) "
                f"<a href='{html.escape(str(d.get('comment_url') or ''))}'>comment</a>"
                f"<pre style='white-space:pre-wrap'>{html.escape(str(d.get('draft') or '(write your reply)'))}</pre></div>"
                for d in m.get("drafts") or [])
            agent = f"<p><a href='{html.escape(str(m['agent_url']))}'>prep task</a></p>" if m.get("agent_url") else ""
            rows.append(
                f"<div style='border:1px solid #ddd;border-radius:6px;padding:12px;margin:12px 0'>"
                f"<b>ADO {html.escape(str(m.get('ticket_id')))}</b> {html.escape(str(m.get('title') or ''))}<br>"
                f"status <b>{html.escape(str(m.get('status')))}</b>, waiting on <b>{html.escape(str(m.get('waiting_on') or ''))}</b>, "
                f"checks {html.escape(str(m.get('checks') or 0))}<p style='font-size:13px'>{html.escape(str(m.get('summary') or ''))}</p>"
                f"{drafts}{agent}</div>")
        self._html(_page("Mention follow-ups", "".join(rows) or "<p>None.</p>"
                         "<p style='color:#666;font-size:13px'>Read-only. Prod deploys still need your GO through operatorBot.</p>"), 200)

    def _html(self, data: bytes, code: int) -> None:
        try:
            _g = b"<script>document.addEventListener('submit',function(e){var f=e.target;if(f.dataset.sent){e.preventDefault();return}f.dataset.sent='1';setTimeout(function(){f.querySelectorAll('button').forEach(function(b){b.disabled=true})},0)},true)</script>"
            if b"<form" in data and b"</body>" in data and _g not in data:
                data = data.replace(b"</body>", _g + b"</body>", 1)
        except Exception:
            pass
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(json.dumps({"ok": True, "listen": f"{HOST}:{PORT}"}))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
