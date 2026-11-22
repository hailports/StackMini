"""Tests for job_harness. Each builds a fixture job in tmp and drives the real wrapper
as a subprocess, with a fake agent (JOB_HARNESS_AGENT_CMD) instead of a paid CLI."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "contrib" / "job_harness" / "job_harness.py"

HANDOFF_OK = textwrap.dedent("""\
    TASK            fixture
    STATUS          complete
    OUTPUT          RESULT.md
    SOURCES         tests
    NEXT OWNER      nobody
    DECISIONS       none
    UNCERTAINTIES   none
    DO-NOT-ASSUME   nothing
    """)


def contract(done="bash done_when.sh", budget="max_wall_s=60 max_attempts=3 max_same_error=2", drop=None, extra=""):
    fields = {
        "goal": "fixture goal",
        "output": "RESULT.md",
        "scope": "this folder",
        "never": "fixture never",
        "done_when": done,
        "budget": budget,
    }
    if drop:
        fields[drop] = ""
    return "# CONTRACT fixture\n" + "".join(f"{k:<12}{v}\n" for k, v in fields.items()) + extra


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "jobs"
    (root / "_templates").mkdir(parents=True)
    host = {"never": ["HOSTNEVER-1 do not touch the fixture wall"], "protected": [str(tmp_path / "wall.txt")], "agents": []}
    (root / "_templates" / "host_defaults.json").write_text(json.dumps(host))
    (tmp_path / "wall.txt").write_text("wall v1")
    e = dict(os.environ, JOB_HARNESS_JOBS_ROOT=str(root), JOB_HARNESS_HOST_CONFIG="")
    e.pop("JOB_HARNESS_DEPTH", None)
    return root, e, tmp_path


def make_job(root, name="j", contract_text=None, done_sh="exit 0", agent="true", handoff=None, result=None):
    d = root / name
    d.mkdir()
    if contract_text is not False:
        (d / "CONTRACT.md").write_text(contract_text or contract())
    (d / "PROGRESS.md").write_text("# PROGRESS fixture\n\ndone\n  nothing\n")
    (d / "prompt.md").write_text("do the fixture task\n")
    (d / "done_when.sh").write_text("#!/bin/sh\n" + done_sh + "\n")
    (d / "agent.sh").write_text("#!/bin/sh\n" + agent + "\n")
    if handoff is not None:
        (d / "HANDOFF.md").write_text(handoff)
    if result is not None:
        (d / "RESULT.md").write_text(result)
    return d


def run(e, d, *extra, agent_cmd=None):
    env = dict(e, JOB_HARNESS_AGENT_CMD=agent_cmd or f"/bin/sh {d / 'agent.sh'} {{prompt_file}}")
    return subprocess.run([sys.executable, str(HARNESS), "run", str(d), *extra], capture_output=True, text=True,
                          env=env, timeout=120)


def audit(d):
    return [json.loads(l) for l in (d / "AUDIT.log").read_text().splitlines()]


def assert_escalated(d, root, word):
    for m in (os.environ.get("JOB_HARNESS_HUMAN_MARKER", "NEEDS_HUMAN"), "ESCALATED"):
        assert (d / m).exists(), m
        assert word in (d / m).read_text()
    assert word in (root / "_escalated" / "INDEX.md").read_text()
    assert "STOP" in (d / "PROGRESS.md").read_text()


# ------------------------------------------------------------------ refusals
def test_missing_contract_refuses(env):
    root, e, _ = env
    d = make_job(root, contract_text=False)
    r = run(e, d)
    assert r.returncode == 2
    assert "no CONTRACT.md" in r.stderr


@pytest.mark.parametrize("field", ["goal", "output", "scope", "never", "done_when", "budget"])
def test_missing_required_field_refuses(env, field):
    root, e, _ = env
    d = make_job(root, contract_text=contract(drop=field))
    r = run(e, d)
    assert r.returncode == 2
    assert field in r.stderr
    assert not (d / "attempt-1.agent0.log").exists()


@pytest.mark.parametrize("done", ["true", "exit 0", "bash missing_script.sh", "nonexistent-binary-xyz --go"])
def test_uncheckable_done_when_refuses(env, done):
    root, e, _ = env
    d = make_job(root, contract_text=contract(done=done))
    r = run(e, d)
    assert r.returncode == 2, r.stderr


def test_budget_without_wall_refuses(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_attempts=2"))
    r = run(e, d)
    assert r.returncode == 2 and "budget" in r.stderr


def test_host_nevers_injected_and_audited(env):
    root, e, _ = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 0, r.stderr
    start = [a for a in audit(d) if a["event"] == "start"][0]
    assert start["injected_nevers"] == ["HOSTNEVER-1 do not touch the fixture wall"]
    prompt = (d / ".prompt.attempt-1.md").read_text()
    assert "HOSTNEVER-1" in prompt and "fixture never" in prompt


# ------------------------------------------------------------------ exits
def test_same_error_twice_escalates(env):
    root, e, _ = env
    d = make_job(root, done_sh="echo 'FAIL test_x at line 12'; exit 1", handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 3
    assert "same error" in r.stderr
    assert_escalated(d, root, "same error signature")
    nd = [a for a in audit(d) if a["event"] == "not_done"]
    assert len(nd) == 2 and nd[0]["sig"] == nd[1]["sig"]


def test_different_errors_do_not_trip_same_error(env):
    root, e, tmp = env
    # done_when output changes each attempt (different words), so the signature differs; budget stops it at 3.
    d = make_job(root, done_sh='c=$(cat n 2>/dev/null || echo a); echo "${c}x" > n; echo "error kind $c"; exit 1',
                 handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 3
    assert "max_attempts" in (d / "ESCALATED").read_text()
    assert len([a for a in audit(d) if a["event"] == "not_done"]) == 3


def test_budget_attempts_spent_escalates(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1 max_same_error=5"),
                 done_sh="exit 1", handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 3
    assert_escalated(d, root, "max_attempts")


def test_budget_wall_spent_escalates(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=2 max_attempts=5"), agent="sleep 10",
                 handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 3
    assert_escalated(d, root, "max_wall_s")


def test_done_when_red_is_not_done_even_with_success_result(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1"),
                 done_sh="echo tests failed; exit 1", handoff=HANDOFF_OK,
                 result="ALL DONE, everything passed, success!")
    r = run(e, d)
    assert r.returncode != 0
    assert not (d / "DONE.json").exists()
    assert "rc=1" in (d / "DONE_WHEN.log").read_text()


def test_missing_handoff_is_not_done_even_if_done_when_green(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1"), done_sh="exit 0", result="ok")
    r = run(e, d)
    assert r.returncode != 0
    assert not (d / "DONE.json").exists()
    assert "rc=0" in (d / "DONE_WHEN.log").read_text()


@pytest.mark.parametrize("bad", [
    HANDOFF_OK.replace("DO-NOT-ASSUME   nothing\n", ""),           # missing heading
    HANDOFF_OK.replace("complete", "done"),                        # bad STATUS value
    HANDOFF_OK.replace("complete", "partial"),                     # valid but not complete
    HANDOFF_OK.replace("OUTPUT          RESULT.md", "OUTPUT          nope/missing.md"),  # output missing
    HANDOFF_OK.replace("SOURCES         tests", "SOURCES"),        # empty section
])
def test_invalid_handoff_is_not_done(env, bad):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1"), done_sh="exit 0",
                 handoff=bad, result="ok")
    r = run(e, d)
    assert r.returncode != 0
    assert not (d / "DONE.json").exists()


def test_handoff_blocked_escalates(env):
    root, e, _ = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK.replace("complete", "blocked"), result="ok")
    r = run(e, d)
    assert r.returncode == 3
    assert_escalated(d, root, "blocked")


def test_valid_handoff_and_green_done_when_succeeds(env):
    root, e, _ = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK, result="ok")
    r = run(e, d)
    assert r.returncode == 0, r.stderr
    assert (d / "DONE.json").exists()
    assert not (d / "ESCALATED").exists()
    ev = [a["event"] for a in audit(d)]
    assert ev[0] == "start" and "agent_exit" in ev and "done_when" in ev and ev[-1] == "green"
    assert len([x for x in ev if x == "agent_exit"]) == 1  # green -> no loop


def test_agent_writes_handoff_then_green(env):
    root, e, _ = env
    agent = f"cat > HANDOFF.md <<'X'\n{HANDOFF_OK}X\necho ok > RESULT.md"
    d = make_job(root, done_sh="test -s RESULT.md", agent=agent)
    r = run(e, d)
    assert r.returncode == 0, r.stderr + (d / "AUDIT.log").read_text()


def test_resume_prompt_carries_progress_and_last_failure(env):
    root, e, _ = env
    d = make_job(root, done_sh="echo red-marker-77; exit 1", handoff=HANDOFF_OK, result="ok")
    run(e, d)
    p2 = (d / ".prompt.attempt-2.md").read_text()
    assert "# PROGRESS fixture" in p2 and "red-marker" in p2 and "## CONTRACT.md" in p2


# ------------------------------------------------------------------ walls / judge / misc
def test_wall_change_escalates(env):
    root, e, tmp = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK, result="ok", agent=f"echo tampered > {tmp / 'wall.txt'}")
    r = run(e, d)
    assert r.returncode == 3
    assert_escalated(d, root, "wall")
    assert str(tmp / "wall.txt") in (d / "ESCALATED").read_text()


def test_judge_stub_fail_blocks_green(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1"), done_sh="exit 0",
                 handoff=HANDOFF_OK, result="ok")
    r = run(e, d, "--judge", "echo VERDICT: FAIL scope; exit 1")
    assert r.returncode != 0
    assert not (d / "DONE.json").exists()
    assert "ok=False" in (d / "JUDGE.log").read_text()


def test_judge_stub_pass_allows_green(env):
    root, e, _ = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK, result="ok")
    r = run(e, d, "--judge", "echo VERDICT: PASS")
    assert r.returncode == 0, r.stderr
    assert json.loads((d / "DONE.json").read_text())["judge"] is True


def test_recursion_refused(env):
    root, e, _ = env
    d = make_job(root, done_sh="exit 0", handoff=HANDOFF_OK, result="ok")
    r = run(dict(e, JOB_HARNESS_DEPTH="1"), d)
    assert r.returncode == 2 and "recursion" in r.stderr


def test_new_scaffolds_and_check_refuses_placeholders(env):
    root, e, _ = env
    r = subprocess.run([sys.executable, str(HARNESS), "new", "fresh"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stderr
    d = root / "fresh"
    for f in ("CONTRACT.md", "PROGRESS.md", "prompt.md", "done_when.sh"):
        assert (d / f).exists(), f
    assert not (d / "HANDOFF.md").exists()  # template only; a real one must be written by the job
    # scaffold with placeholders is not runnable until edited? done_when is real, so check passes;
    # the default done_when.sh is red until RESULT.md exists.
    rc = subprocess.run(["sh", str(d / "done_when.sh")], capture_output=True).returncode
    assert rc != 0


def test_custom_human_marker(env):
    root, e, _ = env
    d = make_job(root, contract_text=contract(budget="max_wall_s=60 max_attempts=1"), done_sh="exit 1",
                 handoff=HANDOFF_OK, result="ok")
    r = run(dict(e, JOB_HARNESS_HUMAN_MARKER="NEEDS_OWNER"), d)
    assert r.returncode == 3
    assert (d / "NEEDS_OWNER").exists() and (d / "ESCALATED").exists()
