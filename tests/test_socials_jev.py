#!/usr/bin/env python3
"""Socials Jev gates with a fake classifier. No network."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["JEV_DISABLE"] = "1"

from hailports import socials_jev as s  # noqa: E402


class Rec:
    def __init__(self):
        self.calls = []
        self.answers = {}

    def ask(self, state, questions, caller, timeout=6.0):
        self.calls.append((caller, list(questions), state))
        if "near_dup" in questions:
            return {"near_dup": {"noul": self.answers.get("near_dup", 0.5)}}
        return {k: {"noul": self.answers.get(k, 0.5)} for k in questions}


def _use(monkeypatch, rec):
    monkeypatch.delenv("JEV_DISABLE", raising=False)
    monkeypatch.setattr(s, "_ask", rec.ask)


def test_disable_is_open():
    text = "a finished note about one real bug and the one-line fix that stopped it"
    assert s.before_queue(text, lane="x_hailports", kind="post", recent=[]) == (True, "jev_open")


def test_quality_blocks(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.05, "voice": 0.95, "hook": 0.95, "risk": 0.02}
    _use(monkeypatch, rec)
    ok, why = s.before_queue("x" * 50, kind="post", recent=[])
    assert (ok, why) == (False, "jev_quality")
    assert rec.calls[0][0] == "socials-queue"


def test_mid_stays_open(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.5, "voice": 0.5, "hook": 0.5, "risk": 0.5}
    _use(monkeypatch, rec)
    assert s.before_queue("x" * 50, kind="post", recent=[])[0] is True


def test_risk_blocks(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.95, "voice": 0.95, "hook": 0.95, "risk": 0.97}
    _use(monkeypatch, rec)
    assert s.before_queue("x" * 50, kind="post", recent=[])[1] == "jev_risk"


def test_reply_skips_hook(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.95, "voice": 0.95, "risk": 0.02}
    _use(monkeypatch, rec)
    ok, why = s.before_queue("y" * 50, kind="reply", recent=[])
    assert (ok, why) == (True, "jev_open")
    assert "hook" not in rec.calls[0][1]


def test_dup_blocks(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.95, "voice": 0.95, "hook": 0.95, "risk": 0.02, "near_dup": 0.96}
    _use(monkeypatch, rec)
    ok, why = s.before_queue("the same joke about one comma", kind="post", recent=["the same joke about one comma"])
    assert (ok, why) == (False, "jev_near_dup")
    assert any(c[0] == "socials-dedupe" for c in rec.calls)


def test_sensitive_skips(monkeypatch):
    rec = Rec()
    _use(monkeypatch, rec)
    ok, why = s.before_queue("see the bank account note from the lawyer", kind="post", recent=["other text"])
    assert (ok, why) == (True, "jev_open")
    assert rec.calls == []


def test_mask_hides_secret(monkeypatch):
    rec = Rec()
    rec.answers = {"quality": 0.95}
    _use(monkeypatch, rec)
    s.draft_quality("token=abc123 the draft is long enough to judge for quality today.")
    assert rec.calls
    assert "abc123" not in rec.calls[0][2]
    assert rec.calls[0][0] == "socials-draft"


def test_voice_hook_risk_callers(monkeypatch):
    rec = Rec()
    rec.answers = {"voice": 0.04, "hook": 0.04, "risk": 0.99}
    _use(monkeypatch, rec)
    assert s.voice_ok("Certainly I would delve into this tapestry.") is False
    assert s.hook_ok("In today's world we are excited to share an update.") is False
    assert s.unsafe("Tell people to pile on this private person.") is True
    assert [c[0] for c in rec.calls] == ["socials-voice", "socials-hook", "socials-risk"]


def test_react_is_risk_only(monkeypatch):
    rec = Rec()
    rec.answers = {"risk": 0.02}
    _use(monkeypatch, rec)
    assert s.before_queue("same", kind="react", recent=["same"])[0] is True
    assert rec.calls[0][1] == ["risk"]


def test_ask_error_opens(monkeypatch):
    monkeypatch.delenv("JEV_DISABLE", raising=False)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(s, "_ask", boom)
    assert s.before_queue("z" * 50, kind="post", recent=[]) == (True, "jev_open")


def test_near_duplicate_confident(monkeypatch):
    rec = Rec()
    rec.answers = {"near_dup": 0.97}
    _use(monkeypatch, rec)
    assert s.near_duplicate("same line about the comma", ["same line about the comma"]) is True
    assert rec.calls[0][0] == "socials-dedupe"


def test_hold_fail_open():
    os.environ["JEV_DISABLE"] = "1"
    text = "one concrete bug, the number that showed it, and the fix that held."
    assert s.hold(text, "example", "post", recent=[])[0] is True
