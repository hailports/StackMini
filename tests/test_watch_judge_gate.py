import importlib.util
from pathlib import Path


def _runner(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("watch_runner", Path(__file__).resolve().parents[1] / "contrib/watch/runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_green_unchanged_result_skips_the_judge(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    green = {"need": False, "summary": "local model ready"}
    assert r.judge_needed("local-model-ready", dict(green)) is True
    assert r.judge_needed("local-model-ready", dict(green)) is False
    assert r.judge_needed("local-model-ready", {"need": False, "summary": "local model recovered"}) is True


def test_not_ok_always_judged_and_other_watches_untouched(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    down = {"need": True, "summary": "local model door down", "force": True}
    assert r.judge_needed("local-model-ready", dict(down)) is True
    assert r.judge_needed("local-model-ready", dict(down)) is True
    green = {"need": False, "summary": "fine"}
    assert r.judge_needed("other", dict(green)) is True and r.judge_needed("other", dict(green)) is True


def test_main_makes_no_model_call_when_green_and_unchanged(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(r, "local_judge", lambda s: calls.append(s) or {"need": False, "why": "ok"})
    monkeypatch.setattr(r, "run_check", lambda script, args: {"ok": True, "need": False, "summary": "local model ready"})
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\n")
    assert r.main(["local-model-ready", str(script)]) == 0
    assert r.main(["local-model-ready", str(script)]) == 0
    assert len(calls) == 1
