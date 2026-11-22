"""rules-gate unit tests. Stub model only: no network, no live engine."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
try:
    import rules_gate as rg  # live layout: tools/rules_gate/rules_gate.py
except ImportError:  # public layout: hailports/rules_gate.py
    from hailports import rules_gate as rg

FIXTURE = HERE / "fixtures" / ("generic_replay.json" if (HERE / "fixtures" / "generic_replay.json").exists() else "rules_gate_replay.json")

CASES = [
    {"id": "tz", "question": "what time zone am i in", "checks": [
        {"id": "k1", "type": "regex", "value": "america/chicago", "rule_hint": "America/Chicago",
         "only_if": "the user asks about time"}]},
    {"id": "greet", "question": "hello", "checks": [
        {"id": "k1", "type": "max_len", "value": 20, "rule_hint": "short greeting", "only_if": "the message is a greeting"}]},
    {"id": "name", "question": "what's your name", "checks": [
        {"id": "k1", "type": "contains", "value": "claude", "rule_hint": "named Claude",
         "only_if": "the user asks for your name"}]},
]

OLD = "# rules\n- greet with a short greeting\n- you are named Claude\n"


class Stub:
    """Answer depends on which rule lines are in the prompt, like a model that obeys rules."""

    def __init__(self):
        self.calls = 0

    def __call__(self, rules_text, rules_hash, case):
        self.calls += 1
        r = rules_text
        verbose = "be verbose" in r
        if case["id"] == "tz":
            return "you are on America/Chicago" if "America/Chicago" in r else "not sure"
        if case["id"] == "greet":
            return "hey there" if "short greeting" in r and not verbose else "hello! " * 10
        if case["id"] == "name":
            return "i'm claude" if "named Claude" in r else "i'm your assistant"
        return case.get("recorded_answer", "")


class Down:
    def __call__(self, *a):
        raise rg.ModelDown("engine paused")


@pytest.fixture
def gate(tmp_path):
    target = tmp_path / "RULES.md"
    target.write_text(OLD)
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"cases": CASES}))
    cfg = rg.load_config(None, replay=str(replay), state=str(tmp_path / "state"),
                         gated=[str(target)], prompt_files=[str(target)], model_url="http://127.0.0.1:9/v1",
                         probe_timeout_s=0.3, nice=0)
    return cfg, target


def rejected(cfg):
    p = Path(cfg["state"]) / "rejected.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------ accept / reject
def test_accept_when_helps_more_than_hurts(gate):
    cfg, target = gate
    new = OLD + "- the user's time zone is America/Chicago\n"
    res = rg.apply_edit(str(target), new, cfg=cfg, model=Stub())
    assert res["status"] == "accept", res
    assert res["counts"]["helped"] == 1 and res["counts"]["broke"] == 0
    assert target.read_text() == new
    assert rg.has_receipt(cfg, new)
    assert not list(target.parent.glob("RULES.md.bak-rulesgate-*"))  # no backup inside tmp


def test_reject_when_hurts_more(gate):
    cfg, target = gate
    res = rg.apply_edit(str(target), OLD + "- be verbose\n", cfg=cfg, model=Stub())
    assert res["status"] == "reject" and res["counts"]["broke"] == 1
    assert target.read_text() == OLD
    rows = rejected(cfg)
    assert len(rows) == 1 and rows[0]["counts"]["broke"] == 1 and "be verbose" in rows[0]["excerpt"]["added"][0]
    assert rows[0]["ts"][-6] in "+-"  # timestamped with an offset


def test_reject_on_tie(gate):
    cfg, target = gate
    res = rg.apply_edit(str(target), OLD + "- be verbose\n- zone is America/Chicago\n", cfg=cfg, model=Stub())
    assert res["status"] == "reject"
    assert res["counts"]["helped"] == res["counts"]["broke"] == 1
    assert target.read_text() == OLD


def test_reject_zero_zero_non_noop(gate):
    cfg, target = gate
    res = rg.apply_edit(str(target), OLD + "- prefer tea over coffee\n", cfg=cfg, model=Stub())
    assert res["status"] == "reject" and "no evidence" in res["reason"]
    assert target.read_text() == OLD


def test_noop_is_success_and_writes_nothing(gate):
    cfg, target = gate
    m = Stub()
    res = rg.apply_edit(str(target), OLD, cfg=cfg, model=m)
    assert res["status"] == "noop" and m.calls == 0
    assert not rejected(cfg)


def test_rejected_edit_is_refused_on_sight(gate):
    cfg, target = gate
    rg.apply_edit(str(target), OLD + "- be verbose\n", cfg=cfg, model=Stub())
    m = Stub()
    res = rg.apply_edit(str(target), OLD + "- be verbose\n", cfg=cfg, model=m)
    assert res["status"] == "duplicate" and m.calls == 0
    assert target.read_text() == OLD
    audit = (Path(cfg["state"]) / "AUDIT.jsonl").read_text()
    assert "rejected_duplicate" in audit


# ------------------------------------------------------------------ locks + only-if
def test_working_rules_get_locked(gate):
    cfg, target = gate
    rg.apply_edit(str(target), OLD + "- zone is America/Chicago\n", cfg=cfg, model=Stub())
    texts = [r["text"] for r in rg.load_locks(cfg).values()]
    assert "- greet with a short greeting" in texts and "- you are named Claude" in texts


def test_regressing_deletion_rewritten_with_only_if(gate):
    cfg, target = gate
    new = "# rules\n- greet with a short greeting\n- zone is America/Chicago\n"  # drops the name rule
    res = rg.apply_edit(str(target), new, cfg=cfg, model=Stub())
    assert res["status"] == "accept", res
    assert res.get("only_if_rewrite")
    text = target.read_text()
    assert "you are named Claude -- only if the user asks for your name" in text
    assert "America/Chicago" in text


def test_locked_deletion_without_condition_rejects(gate, tmp_path):
    cfg, target = gate
    cases = json.loads(json.dumps(CASES))
    for c in cases:
        for ch in c["checks"]:
            ch.pop("only_if", None)
    new = "# rules\n- greet with a short greeting\n- zone is America/Chicago\n"
    res = rg.apply_edit(str(target), new, cfg=cfg, model=Stub(), cases=cases)
    assert res["status"] == "reject" and "only-if" in res["reason"]
    assert target.read_text() == OLD


def test_already_locked_rule_deletion_is_rewritten_before_replay(gate):
    cfg, target = gate
    rg.baseline(cfg, offline=False, model=Stub())  # locks working rules
    assert rg.load_locks(cfg)
    new = "# rules\n- greet with a short greeting\n- zone is America/Chicago\n"
    res = rg.evaluate(cfg, str(target), OLD, new, model=Stub())
    assert res["status"] == "accept" and res["only_if_rewrite"]
    assert "only if" in res["proposal"]


# ------------------------------------------------------------------ model down / skip
def test_model_down_skips_and_queues(gate):
    cfg, target = gate
    res = rg.apply_edit(str(target), OLD + "- zone is America/Chicago\n", cfg=cfg, model=Down())
    assert res["status"] == "skip"
    assert target.read_text() == OLD
    assert len(list((Path(cfg["state"]) / "pending").glob("*.json"))) == 1


def test_unreachable_engine_skips_fast(gate):
    cfg, target = gate
    t = time.time()
    res = rg.apply_edit(str(target), OLD + "- zone is America/Chicago\n", cfg=cfg)  # real client, port 9
    assert res["status"] == "skip" and time.time() - t < 5
    assert target.read_text() == OLD


def test_cli_exit_codes(gate, tmp_path, monkeypatch):
    cfg, target = gate
    cj = tmp_path / "cfg.json"
    cj.write_text(json.dumps({k: cfg[k] for k in ("replay", "state", "gated", "prompt_files", "model_url")}
                             | {"probe_timeout_s": 0.3, "nice": 0}))
    newf = tmp_path / "new.md"
    newf.write_text(OLD + "- zone is America/Chicago\n")
    assert rg.main(["--config", str(cj), "apply", "--target", str(target), "--new", str(newf)]) == rg.RC_SKIP
    assert target.read_text() == OLD
    assert rg.main(["--config", str(cj), "--replay", str(tmp_path / "missing.json"), "apply",
                    "--target", str(target), "--new", str(newf)]) == rg.RC_REFUSE
    assert rg.main(["--help"]) == 0
    assert rg.main(["apply"]) == rg.RC_REFUSE
    assert rg.main(["--config", str(cj), "status"]) == 0
    monkeypatch.setattr(rg, "SplashModel", lambda c: Stub())
    assert rg.main(["--config", str(cj), "check", "--old", str(target), "--new", str(newf), "--target", str(target)]) == rg.RC_OK
    bad = tmp_path / "bad.md"
    bad.write_text(OLD + "- be verbose\n")
    assert rg.main(["--config", str(cj), "check", "--old", str(target), "--new", str(bad), "--target", str(target)]) == rg.RC_REJECT


def test_cache_makes_rerun_cheap(gate):
    cfg, target = gate
    m = Stub()
    rg.evaluate(cfg, str(target), OLD, OLD + "- zone is America/Chicago\n", model=m)
    n = m.calls
    rg.evaluate(cfg, str(target), OLD, OLD + "- zone is America/Chicago\n", model=m)
    assert m.calls == n


# ------------------------------------------------------------------ fail-open chat
def test_chat_allowed_fail_open(monkeypatch):
    monkeypatch.setenv("RULES_GATE_CONFIG", "/nonexistent/broken.json")
    assert rg.chat_allowed() is True


def test_safe_call_swallows_errors_and_timeouts():
    def boom():
        raise RuntimeError("x")
    assert rg.safe_call(boom, default="fallback") == "fallback"
    t = time.time()
    assert rg.safe_call(time.sleep, 5, timeout=0.2, default="late") == "late"
    assert time.time() - t < 2


def test_chat_modules_never_import_gate():
    pkg = Path(rg.__file__).resolve().parent
    for name in ("chat_window.py", "self_consistency.py", "local_first.py"):
        p = pkg / name
        if p.exists():
            assert "rules_gate" not in p.read_text(errors="replace"), name


# ------------------------------------------------------------------ writer hook
def test_learning_runbook_gated_when_configured(gate, tmp_path):
    from hailports import learning
    cfg, _ = gate
    con = learning.connect(tmp_path / "l.sqlite")
    learning.teach(con, "time zone", "say America/Chicago")
    book = tmp_path / "book"
    p = learning.write_runbook(con, book)  # ungated default: written
    before = p.read_text()
    learning.teach(con, "greeting", "be verbose")
    cfg = dict(cfg, gated=[str(p)], prompt_files=[str(p)])
    out = learning.write_runbook(con, book, gate=cfg, gate_model=Down())
    assert out == p and p.read_text() == before  # model down: unchanged, queued
    assert list((Path(cfg["state"]) / "pending").glob("*.json"))


# ------------------------------------------------------------------ fixtures + checks
def test_generic_fixture_loads_and_passes_with_recorded_answers(tmp_path):
    cases = rg.load_replay(FIXTURE)
    assert len(cases) >= 30
    cfg = rg.load_config(None, replay=str(FIXTURE), state=str(tmp_path / "st"))
    out = rg.baseline(cfg, offline=True)
    assert out["method"] == "offline-from-recorded-answers"
    assert out["checks_pass"] == out["checks_total"] >= 50
    stub = rg.baseline(cfg, offline=False, model=Stub())
    assert stub["checks_pass"] == stub["checks_total"]


def test_check_types():
    rc = rg.run_check
    assert rc({"type": "contains", "value": "Claude"}, "i'm claude")
    assert not rc({"type": "contains", "value": "Claude", "case": True}, "i'm claude")
    assert rc({"type": "not_contains", "value": "x"}, "abc")
    assert rc({"type": "regex", "value": r"^pong$"}, "PONG")
    assert rc({"type": "not_regex", "value": r"secret"}, "fine")
    assert rc({"type": "max_len", "value": 3}, "abc") and not rc({"type": "max_len", "value": 2}, "abc")
    assert rc({"type": "min_len", "value": 3}, "abc")
    assert not rc({"type": "not_empty"}, "  ")
    assert rc({"type": "json_path", "value": "a.0.b=ok"}, 'here: {"a": [{"b": "ok"}]}')
    assert not rc({"type": "json_path", "value": "a.1"}, '{"a": [1]}')


def test_bad_replay_refused(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"cases": [{"id": "x", "question": "q", "checks": [{"id": "k", "type": "vibes"}]}]}))
    with pytest.raises(ValueError):
        rg.load_replay(p)
    with pytest.raises(FileNotFoundError):
        rg.load_replay(tmp_path / "nope.json")
