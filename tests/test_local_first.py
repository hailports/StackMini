#!/usr/bin/env python3
"""Policy tests for the local-first router. No network and no paid CLI."""
import os
import sys

import pytest

NOT_ON_WINDOWS = pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec and /tmp sentinel paths")
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LOCAL_FIRST_RECORD"] = "0"
os.environ.pop("LOCAL_FIRST_ROUTER", None)
os.environ.pop("LOCAL_FIRST_DONE", None)
for _env_name in ("GROK_REAL_BIN", "CLAUDE_REAL_BIN", "CODEX_REAL_BIN", "CURSOR_AGENT_REAL_BIN"):
    os.environ.pop(_env_name, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hailports import local_first as r  # noqa: E402


def test_policy_matrix():
    assert r.classify_stakes("Summarize this log in one sentence: started then stopped")[0] == "low"
    assert r.classify_stakes("deploy this change to production")[0] == "high"
    assert r.classify_stakes("email the customer a status update")[0] == "high"
    assert r.classify_stakes("draft a reply comment for the thread")[0] == "high"
    assert r.classify_stakes("help me with this")[0] == "unsure"
    foot = "Summarize this log: the router started\n\n## STANDING RULE\ncommit and push"
    assert r.classify_stakes(foot)[0] == "low"
    assert r.classify_stakes("first draft of a checklist for me")[0] == "low"
    assert r.classify_stakes("note this ledger-mutate please", ["ledger-mutate"])[0] == "high"


def test_low_completes_and_high_does_not():
    low = r.policy_decision(
        "Summarize this log in one sentence",
        '{"decision":"complete_local","confidence":0.91,"why":"short log","answer":"The router started and did not open a paid session."}',
    )
    assert low["decision"] == "complete_local"
    assert low["paid_invoked"] is False
    assert low["answer"].startswith("The router")
    assert low["brief"].startswith(r.BRIEF_HEADER)
    high = r.policy_decision(
        "implement the function and deploy it to production",
        '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"I already changed production and emailed the team."}',
    )
    assert high["decision"] == "need_premium"
    assert high["answer"] == ""
    weak = r.policy_decision("Summarize this log for me now", "I don't know which log.")
    assert weak["decision"] == "need_premium"


def test_grok_plan_does_not_exec_on_local():
    low = r.policy_decision(
        "Summarize this log in one sentence please",
        '{"decision":"complete_local","confidence":0.8,"why":"ok","answer":"One sentence is enough for this short log line."}',
    )
    action, args = r.plan_grok(["--background-wait-timeout", "31536000", "-p", "Summarize this log in one sentence please"], low)
    assert action == "local" and args == []
    high = r.policy_decision("refactor the parser", "A short brief of the parser edges.")
    action, args = r.plan_grok(["--background-wait-timeout", "31536000", "-p", "refactor the parser"], high)
    assert action == "escalate"
    assert args[0] == "--background-wait-timeout"
    assert args[1] == "31536000"
    joined = " ".join(args)
    assert "[LOCAL-FIRST BRIEF v1]" in joined


def test_route_with_fake_fetch_never_marks_paid():
    def fetch(_prompt):
        return {"ok": True, "text": '{"decision":"complete_local","confidence":0.88,"why":"log","answer":"The three lines say the local model finished alone."}', "backend": "fake", "usage": {"prompt_tokens": 10}}

    os.environ["LOCAL_FIRST_DRY"] = "1"
    result = r.route_task(
        "Summarize this log: a b c d e f g h",
        caller="bot",
        fetch=fetch,
        config={"transport": "inject", "extra_high_substrings": []},
        execute_paid=lambda _p: (_ for _ in ()).throw(AssertionError("paid launched")),
    )
    assert result["decision"] == "complete_local"
    assert result["paid_invoked"] is False
    assert result["backend"] == "fake"


def _fake_low(_prompt):
    return {
        "ok": True,
        "text": '{"decision":"complete_local","confidence":0.9,"why":"log","answer":"The router started and wrote a one-line summary."}',
        "backend": "fake",
        "usage": {"prompt_tokens": 4, "completion_tokens": 8},
    }


def _fake_high(_prompt):
    return {
        "ok": True,
        "text": '{"decision":"complete_local","confidence":0.99,"why":"done","answer":"Shipped the production deploy already and mailed the team today."}',
        "backend": "fake",
    }


def _cfg(paid: str) -> dict:
    return {
        "transport": "inject",
        "extra_high_substrings": [],
        "paid_bins": {"grok": paid, "claude": paid, "codex": paid, "cursor-agent": paid},
    }


def test_prompt_extraction_matches_real_flags():
    # claude 2.1.283: -p/--print is boolean; the prompt is positional.
    got = r.extract_prompt("claude", ["-p", "--model", "opus", "--output-format", "text", "Summarize this log"])
    assert got["kind"] == "inline" and got["text"] == "Summarize this log"
    assert r.extract_prompt("claude", ["explain this project"], stdout_is_tty=True)["kind"] == "none"
    assert r.extract_prompt("claude", ["doctor"])["kind"] == "none"
    assert r.extract_prompt("claude", ["-p"], stdout_is_tty=True)["kind"] == "stdin"
    # codex non-interactive is `exec` (alias `e`); a bare prompt stays in the TUI.
    got = r.extract_prompt("codex", ["exec", "--sandbox", "read-only", "-m", "paid", "Summarize this log"])
    assert got["text"] == "Summarize this log"
    assert r.extract_prompt("codex", ["e", "Summarize this log"])["text"] == "Summarize this log"
    assert r.extract_prompt("codex", ["Summarize this log"])["kind"] == "none"
    assert r.extract_prompt("codex", ["exec", "-"])["kind"] == "stdin"
    assert r.extract_prompt("codex", ["review"])["kind"] == "none"
    # cursor-agent 2026.10.19: --print is headless; prompt is the positional.
    got = r.extract_prompt(
        "cursor-agent",
        ["--print", "--force", "--trust", "--output-format", "text", "--model", "paid", "--mode", "ask", "--workspace", "/tmp/ws", "Summarize this log"],
    )
    assert got["text"] == "Summarize this log"
    assert r.extract_prompt("cursor-agent", ["Summarize this log"], stdout_is_tty=True)["kind"] == "none"
    assert r.extract_prompt("cursor-agent", ["login"])["kind"] == "none"
    assert r.extract_prompt("cursor-agent", ["agent", "--print", "Summarize this log"])["text"] == "Summarize this log"
    assert r.extract_prompt("grok", ["--prompt-file", "note.md"])["kind"] == "file"


@NOT_ON_WINDOWS
def test_complete_local_does_not_exec_paid_bins(tmp_path=None):
    paid = Path("/tmp/local-first-paid-should-not-run")
    paid.write_text("#!/bin/sh\necho EXEC\nexit 99\n")
    paid.chmod(0o755)
    calls = []

    def fake_exec(path, argv):
        calls.append((path, list(argv)))
        raise AssertionError("paid binary exec")

    old = r.os.execv
    r.os.execv = fake_exec
    cfg = _cfg(str(paid))
    cases = [
        ("grok", ["--background-wait-timeout", "31536000", "-p", "Summarize this log in one sentence"]),
        ("claude", ["-p", "Summarize this log in one sentence"]),
        ("codex", ["exec", "Summarize this log in one sentence"]),
        ("cursor-agent", ["--print", "Summarize this log in one sentence"]),
    ]
    try:
        for caller, argv in cases:
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                rc = r.run_intercept(caller, argv, fetch=_fake_low, config=cfg, stdout_is_tty=True)
            assert rc == 0, caller
            assert "one-line summary" in out.getvalue(), caller
    finally:
        r.os.execv = old
        paid.unlink(missing_ok=True)
    assert calls == []


@NOT_ON_WINDOWS
def test_high_stakes_escalates_without_complete_local():
    paid = Path("/tmp/local-first-paid-sentinel")
    paid.write_text("#!/bin/sh\necho EXEC\n")
    paid.chmod(0o755)
    calls = []

    def fake_exec(path, argv):
        calls.append((path, list(argv)))
        raise SystemExit(0)

    old = r.os.execv
    r.os.execv = fake_exec
    cfg = _cfg(str(paid))
    try:
        for caller, argv in (
            ("claude", ["-p", "implement the function and deploy it to production"]),
            ("codex", ["exec", "implement the function and deploy it to production"]),
            ("cursor-agent", ["--print", "refactor the parser"]),
            ("grok", ["-p", "merge the pull request"]),
        ):
            calls.clear()
            os.environ.pop("LOCAL_FIRST_DONE", None)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    r.run_intercept(caller, argv, fetch=_fake_high, config=cfg, stdout_is_tty=True)
            except SystemExit:
                pass
            finally:
                os.environ.pop("LOCAL_FIRST_DONE", None)
            assert calls, caller
            blob = " ".join(calls[0][1])
            assert calls[0][0] == str(paid), caller
            assert "[LOCAL-FIRST BRIEF v1]" in blob, caller
    finally:
        r.os.execv = old
        paid.unlink(missing_ok=True)


@NOT_ON_WINDOWS
def test_interactive_and_router_off_passthrough():
    paid = Path("/tmp/local-first-paid-pass")
    paid.write_text("#!/bin/sh\nexit 0\n")
    paid.chmod(0o755)
    calls = []

    def fake_exec(path, argv):
        calls.append((path, list(argv)))
        raise SystemExit(0)

    def boom(_prompt):
        raise AssertionError("local route on passthrough")

    old = r.os.execv
    r.os.execv = fake_exec
    cfg = _cfg(str(paid))
    try:
        for argv in ([], ["explain this project"]):
            calls.clear()
            try:
                r.run_intercept("claude", argv, fetch=boom, config=cfg, stdout_is_tty=True)
            except SystemExit:
                pass
            assert calls and calls[0][1] == [str(paid), *argv]
        calls.clear()
        os.environ["LOCAL_FIRST_ROUTER"] = "0"
        try:
            try:
                r.run_intercept("codex", ["exec", "Summarize this log"], fetch=boom, config=cfg, stdout_is_tty=True)
            except SystemExit:
                pass
        finally:
            os.environ.pop("LOCAL_FIRST_ROUTER", None)
        assert calls and calls[0][1][1:] == ["exec", "Summarize this log"]
        calls.clear()
        os.environ["LOCAL_FIRST_DONE"] = "1"
        try:
            try:
                r.run_intercept("cursor-agent", ["--print", "Summarize this log"], fetch=boom, config=cfg, stdout_is_tty=True)
            except SystemExit:
                pass
        finally:
            os.environ.pop("LOCAL_FIRST_DONE", None)
        assert calls and "--print" in calls[0][1]
    finally:
        r.os.execv = old
        paid.unlink(missing_ok=True)


@NOT_ON_WINDOWS
def test_fail_open_execs_real_bin():
    paid = Path("/tmp/local-first-paid-failopen")
    paid.write_text("#!/bin/sh\nexit 0\n")
    paid.chmod(0o755)
    calls = []

    def fake_exec(path, argv):
        calls.append((path, list(argv)))
        raise SystemExit(0)

    def broken(_task, **_k):
        raise RuntimeError("router blew up")

    old_exec = r.os.execv
    old_route = r.route_task
    r.os.execv = fake_exec
    r.route_task = broken
    try:
        try:
            r.run_intercept("claude", ["-p", "Summarize this log"], config=_cfg(str(paid)), stdout_is_tty=True)
        except SystemExit:
            pass
    finally:
        r.os.execv = old_exec
        r.route_task = old_route
        paid.unlink(missing_ok=True)
    assert calls and calls[0][0] == str(paid)
    assert calls[0][1][1:] == ["-p", "Summarize this log"]


def test_resolve_skips_wrapper(tmp=None):
    root = Path("/tmp/local-first-path-test")
    wrap_dir = root / "wrap"
    real_dir = root / "real"
    wrap_dir.mkdir(parents=True, exist_ok=True)
    real_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrap_dir / "claude"
    real = real_dir / "claude"
    wrapper.write_text("#!/bin/sh\n# local-first-wrapper-v1 claude\nexit 0\n")
    real.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    real.chmod(0o755)
    old = os.environ.get("PATH")
    os.environ["PATH"] = f"{wrap_dir}{os.pathsep}{real_dir}"
    os.environ.pop("CLAUDE_REAL_BIN", None)
    try:
        found = r.resolve_real_bin("claude", {"paid_bins": {}})
    finally:
        if old is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old
    assert found == str(real)


def test_openai_door_local_and_high():
    cfg = {"transport": "inject", "extra_high_substrings": []}
    status, headers, body, _stream = r.openai_completion(
        {"messages": [{"role": "user", "content": "Summarize this log in one sentence"}]},
        fetch=_fake_low,
        config=cfg,
    )
    assert status == 200 and headers["X-Local-First-Decision"] == "complete_local"
    assert "one-line summary" in body
    status, headers, body, _stream = r.openai_completion(
        {"messages": [{"role": "user", "content": "implement the function and deploy it to production"}]},
        fetch=_fake_high,
        config=cfg,
    )
    assert headers["X-Local-First-Decision"] == "need_premium"
    assert "LOCAL-FIRST BRIEF" in body
    assert "cannot start the paid editor model" in body


if __name__ == "__main__":
    test_policy_matrix()
    test_low_completes_and_high_does_not()
    test_grok_plan_does_not_exec_on_local()
    test_route_with_fake_fetch_never_marks_paid()
    test_prompt_extraction_matches_real_flags()
    test_complete_local_does_not_exec_paid_bins()
    test_high_stakes_escalates_without_complete_local()
    test_interactive_and_router_off_passthrough()
    test_fail_open_execs_real_bin()
    test_resolve_skips_wrapper()
    test_openai_door_local_and_high()
    print("ok")
