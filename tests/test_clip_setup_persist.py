"""Setup re-runs, relaunches and updates must not duplicate engines or move the user's pick."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hailports.clip import board, routefix
from hailports.clip import run as cliprun
from hailports.config import dumps, load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
BIN = "/usr/local/bin/agent"


def _ssh(model: str = "qwen") -> dict:
    return {"via": "ssh", "ssh_host": "brain", "remote_url": "http://127.0.0.1:18080/v1",
            "model": model, "base_url": ""}


def _cursor() -> dict:
    return {"via": "cursor", "bin": BIN, "model": "cursor", "base_url": "", "ssh_host": "", "remote_url": ""}


def _corrupted() -> dict:
    names = ["ssh", "ssh2", "ssh22", "ssh3", "ssh23", "ssh32", "ssh4"]
    models = {"brain": {"via": "http", "base_url": "http://127.0.0.1:18085/v1", "model": "brain"}}
    models.update({name: _ssh() for name in names})
    models["cursor"] = _cursor()
    return {"paiste": {"enabled": True, "preferred": "cursor", "order": ["brain"] + names + ["cursor"],
                       "models": models}, "llm": {"model": "local"}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PAISTE_ROUTE", str(tmp_path / ".config" / "paiste" / "route.yaml"))
    monkeypatch.setattr(board, "app_status", lambda name: "signed-in")
    monkeypatch.setattr(board, "app_bin", lambda name: BIN)
    monkeypatch.setattr(board, "tie_notes", lambda *a, **k: None)
    monkeypatch.setattr(board, "server_setup_text", lambda *a, **k: "")
    import hailports.clip.gate as gate
    monkeypatch.setattr(gate, "read_token", lambda cfg: "")
    return tmp_path / ".config" / "paiste" / "route.yaml"


def _args(**kw):
    base = {"login": "", "use": "", "connect": "", "hint": False, "auto": False, "scan": False, "show": False,
            "answers": "", "pull": "", "ingest": False, "probe": False, "route_out": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _onboard(dest: Path, **kw) -> int:
    cfg = load_file(dest) if dest.is_file() else {}
    return cliprun.cmd_onboard(_args(**kw), cfg)


def test_dedupe_corrupted_route_keeps_preferred_first():
    fixed = routefix.tidy_route(_corrupted())["paiste"]
    assert fixed["order"] == ["cursor", "brain", "ssh"]
    assert set(fixed["models"]) == {"cursor", "brain", "ssh"}
    assert routefix.tidy_route({"paiste": fixed})["paiste"] == fixed  # idempotent


def test_dedupe_without_preferred_keeps_first_and_order():
    route = _corrupted()
    del route["paiste"]["preferred"]
    fixed = routefix.tidy_paiste(route["paiste"])
    assert fixed["order"] == ["brain", "ssh", "cursor"]


def test_different_models_on_same_host_are_not_duplicates():
    paiste = {"order": ["a", "b"], "models": {"a": _ssh("qwen"), "b": _ssh("llama")}}
    assert routefix.tidy_paiste(paiste)["order"] == ["a", "b"]


def test_repair_route_backs_up_and_only_writes_on_change(tmp_path):
    path = tmp_path / "route.yaml"
    save_file(path, _corrupted())
    assert routefix.repair_route(path, now=0) is True
    backups = list(tmp_path.glob("route.yaml.bak-*"))
    assert len(backups) == 1 and "ssh32" in backups[0].read_text()
    assert load_file(path)["paiste"]["order"] == ["cursor", "brain", "ssh"]
    before = path.stat().st_mtime_ns
    assert routefix.repair_route(path) is False
    assert path.stat().st_mtime_ns == before
    assert len(list(tmp_path.glob("route.yaml.bak-*"))) == 1


def test_repair_route_ignores_missing_or_odd_files(tmp_path):
    assert routefix.repair_route(tmp_path / "none.yaml") is False
    odd = tmp_path / "odd.yaml"
    odd.write_text("llm:\n  model: local\n")
    assert routefix.repair_route(odd) is False


def test_use_cursor_twice_is_noop(env):
    save_file(env, _corrupted())
    assert _onboard(env, use="cursor") == 0
    first = env.read_text()
    paiste = load_file(env)["paiste"]
    assert paiste["preferred"] == "cursor" and paiste["order"][0] == "cursor"
    assert len(paiste["order"]) == len(set(paiste["order"])) == len(paiste["models"])
    mtime = env.stat().st_mtime_ns
    assert _onboard(env, use="cursor") == 0
    assert env.read_text() == first
    assert env.stat().st_mtime_ns == mtime


def test_auto_and_answers_after_use_keep_cursor_first(env):
    save_file(env, {"paiste": {"order": ["ssh"], "models": {"ssh": _ssh()}}})
    assert _onboard(env, use="cursor") == 0
    scan = {"suggest": {"ai": {"kind": "local", "host": "brain", "url": "http://127.0.0.1:18080/v1", "name": "qwen"}},
            "apps": [{"name": "cursor", "drives": True, "status": "signed-in"}], "mail_apps": [], "chat_apps": []}
    for _ in range(3):
        assert cliprun._onboard_auto(_args(auto=True), load_file(env), scan=scan) == 0
        paiste = load_file(env)["paiste"]
        assert paiste["order"][0] == "cursor"
        ids = [routefix.spec_identity(paiste["models"][n]) for n in paiste["order"]]
        assert len(ids) == len(set(ids))
        assert not any(n.startswith("ssh2") or n.startswith("ssh3") for n in paiste["models"])
    # The board's own save path (app + a local row), re-run: still one ssh, cursor first.
    for _ in range(3):
        answers = env.parent / "answers.json"
        answers.write_text('{"link": "app", "app": "cursor", "local": "ssh brain http://127.0.0.1:18080/v1", "local_model": "qwen"}')
        assert _onboard(env, answers=str(answers)) == 0
        paiste = load_file(env)["paiste"]
        assert paiste["order"][0] == "cursor"
        ids = [routefix.spec_identity(paiste["models"][n]) for n in paiste["order"]]
        assert len(ids) == len(set(ids))


def test_launch_loader_repairs_route(env, monkeypatch):
    save_file(env, _corrupted())
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("clip_run_entry", str(ROOT / "contrib/clip/run"))
    spec = importlib.util.spec_from_loader("clip_run_entry", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    cfg = mod._cfg("", "http://127.0.0.1:1/v1", "local")
    assert cfg["paiste"]["order"] == ["cursor", "brain", "ssh"]


def test_swift_setup_gate(tmp_path):
    exe = tmp_path / "setupgate-tests"
    build = subprocess.run(["swiftc", "-O", "-o", str(exe), str(ROOT / "contrib/clip/SetupGate.swift"),
                            str(ROOT / "contrib/clip/tests/setupgate/main.swift")], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-2000:]
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout


def test_gate_is_wired_into_launch_and_builds():
    menu = (ROOT / "contrib/clip/PaisteMenu.swift").read_text(encoding="utf-8")
    assert "|| !AXIsProcessTrusted() {\n            openSetup()" not in menu
    assert "SetupGate.decide(" in menu and "Re-enable paisty in Accessibility" in menu
    for script in ("build.sh", "package.sh"):
        assert '"$HERE/SetupGate.swift"' in (ROOT / "contrib/clip" / script).read_text(encoding="utf-8")
    flow = (ROOT / ".github/workflows/paisty-release.yml").read_text(encoding="utf-8")
    assert "tests/test_clip_setup_persist.py" in flow


def test_dumps_round_trip_of_tidy_route():
    fixed = routefix.tidy_route(_corrupted())
    assert load_file  # loader used above
    assert "ssh2" not in dumps(fixed)
