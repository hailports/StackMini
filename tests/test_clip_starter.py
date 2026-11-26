"""paisty's starter AI hop: opt-in, license-carrying, replaced by your own AI, clear cap and trial-end."""

import io
import json
import urllib.error

import pytest

from hailports.clip import board, gate, hops
from hailports.llm import ModelOffline


def _starter_cfg():
    return {"paiste": board.use_starter({})}


def test_use_starter_is_the_only_engine():
    out = board.use_starter({"mail": "front", "order": ["codex"], "models": {"codex": {"via": "codex", "bin": "/x"}}})
    assert out["order"] == ["starter"]
    assert out["models"]["starter"]["via"] == "starter"
    assert out["mail"] == "front"
    eps = hops.endpoints_from({"paiste": out})
    assert [e["via"] for e in eps] == ["starter"]


def test_starter_is_opt_in_only():
    # A route without the starter never sends to paisty.com.
    eps = hops.endpoints_from({"paiste": {"order": ["llm"], "models": {"llm": {"base_url": "http://127.0.0.1:9/v1"}}}})
    assert all(e["via"] != "starter" for e in eps)
    assert all(e["via"] != "starter" for e in hops.endpoints_from({}))


def test_own_ai_replaces_starter():
    models = {"starter": {"via": "starter"}, "codex": {"via": "codex", "bin": "/usr/bin/true"}}
    eps = hops.endpoints_from({"paiste": {"order": ["starter", "codex"], "models": models}})
    assert [e["via"] for e in eps] == ["codex"]


def test_starter_url_must_be_paisty_or_loopback():
    assert hops.starter_url_ok("https://www.paisty.com/api/draft")
    assert hops.starter_url_ok("http://127.0.0.1:8788/api/draft")
    assert not hops.starter_url_ok("http://www.paisty.com/api/draft")
    assert not hops.starter_url_ok("https://evil.example/api/draft")
    eps = hops.endpoints_from({"paiste": {"order": ["s"], "models": {"s": {"via": "starter", "base_url": "https://evil.example/x"}}}})
    assert all(e["via"] != "starter" for e in eps)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup(monkeypatch, creds=({"token": "tok"},)):
    monkeypatch.setattr(gate, "credentials", lambda cfg=None: list(creds))
    monkeypatch.setattr(gate, "device_id", lambda: "a" * 32)


def test_starter_sends_license_device_and_messages(monkeypatch):
    _setup(monkeypatch)
    seen = {}

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp(json.dumps({"ok": True, "text": "sure, Friday works."}).encode())

    msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "Friday?"}]
    out = hops._starter({"base_url": hops.STARTER_URL}, msgs, urlopen=opener)
    assert out == "sure, Friday works."
    assert seen["url"] == hops.STARTER_URL
    assert seen["body"] == {"token": "tok", "device": "a" * 32, "messages": msgs}


def _http_error(code, doc):
    return urllib.error.HTTPError(hops.STARTER_URL, code, "x", {}, io.BytesIO(json.dumps(doc).encode()))


def test_starter_cap_and_trial_end(monkeypatch):
    _setup(monkeypatch)

    def capped(req, timeout=0):
        raise _http_error(429, {"ok": False, "reason": "cap"})

    def ended(req, timeout=0):
        raise _http_error(403, {"ok": False, "reason": "license"})

    with pytest.raises(hops.StarterCapped):
        hops._starter({}, [{"role": "user", "content": "x"}], urlopen=capped)
    with pytest.raises(hops.StarterEnded):
        hops._starter({}, [{"role": "user", "content": "x"}], urlopen=ended)
    _setup(monkeypatch, creds=())
    with pytest.raises(hops.StarterEnded):
        hops._starter({}, [{"role": "user", "content": "x"}], urlopen=capped)


def test_complete_keeps_the_starter_reason(monkeypatch):
    # A local fallback failing after a capped starter must not hide why: the menu explains the cap.
    def fake(endpoint, messages):
        raise hops.StarterCapped("starter cap")

    monkeypatch.setattr(hops, "_starter", fake)
    eps = [{"name": "starter", "via": "starter"}, {"name": "llm", "via": "http", "base_url": "http://127.0.0.1:9/v1", "model": "m"}]

    def dead(endpoint, messages):
        raise ModelOffline("down")

    with pytest.raises(ModelOffline) as info:
        hops.complete(eps, [{"role": "user", "content": "hi"}], http=dead)
    assert str(info.value) == "starter cap"


def test_secret_never_reaches_starter(monkeypatch):
    called = []
    monkeypatch.setattr(hops, "_starter", lambda e, m: called.append(1) or "x")
    with pytest.raises(ModelOffline):
        hops.complete([{"name": "starter", "via": "starter"}],
                      [{"role": "user", "content": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"}])
    assert called == []


def test_show_and_apply_board_keep_starter():
    cur = board.use_starter({})
    assert board.show_board(cur, {})["app"] == "starter"
    out = board.apply_board(cur, {"link": "app", "app": "starter", "mail": "front"})
    assert out["order"] == ["starter"]
    assert out["mail"] == "front"


def test_starter_fit_keeps_newest_text():
    msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "old " * 20000 + "NEWEST"}]
    out = hops.starter_fit(msgs)
    assert sum(len(m["content"]) for m in out) <= hops.STARTER_MAX_CHARS
    assert out[0]["content"] == "rules"
    assert out[1]["content"].endswith("NEWEST")
    assert hops.starter_fit(msgs[:1]) == msgs[:1]
