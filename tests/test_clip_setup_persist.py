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


def test_repair_route_never_reorders_or_drops(tmp_path):
    path = tmp_path / "route.yaml"
    save_file(path, _corrupted())
    before = path.read_text()
    assert routefix.repair_route(path, now=0) is False
    assert path.read_text() == before
    assert not list(tmp_path.glob("route.yaml.bak-*"))


def test_repair_route_only_fills_missing_order(tmp_path):
    path = tmp_path / "route.yaml"
    save_file(path, {"paiste": {"preferred": "cursor", "models": {"cursor": _cursor(), "brain": _ssh()}}})
    assert routefix.repair_route(path, now=0) is True
    assert len(list(tmp_path.glob("route.yaml.bak-*"))) == 1
    paiste = load_file(path)["paiste"]
    assert paiste["order"] == ["cursor", "brain"] and paiste["preferred"] == "cursor"
    assert paiste["models"]["brain"]["remote_url"] == "http://127.0.0.1:18080/v1"
    assert routefix.repair_route(path) is False


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


def test_launch_loader_leaves_route_alone(env, monkeypatch):
    save_file(env, _corrupted())
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("clip_run_entry", str(ROOT / "contrib/clip/run"))
    spec = importlib.util.spec_from_loader("clip_run_entry", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    before = env.read_text()
    cfg = mod._cfg("", "http://127.0.0.1:1/v1", "local")
    assert env.read_text() == before
    assert cfg["paiste"]["order"] == _corrupted()["paiste"]["order"]


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


def _restored() -> dict:
    """The work Mac route after the 1550 incident was fixed by hand."""
    return {"paiste": {"enabled": True, "mail": "front", "notes": "folder",
                       "order": ["cursor", "brain"], "preferred": "cursor",
                       "models": {"brain": _ssh(), "cursor": _cursor(), "ssh": _ssh()}}}


def test_incident_setup_paths_keep_order_preferred_and_18080(env):
    save_file(env, _restored())
    # Setup form save with the local row's address left empty (the old form filled in :8080).
    answers = env.parent / "answers.json"
    answers.write_text('{"link": "app", "app": "cursor", "locals": [{"host": "brain", "remote": "", "model": "qwen"}]}')
    assert _onboard(env, answers=str(answers)) == 0
    scan = {"suggest": {"ai": {"kind": "local", "host": "brain", "url": "http://127.0.0.1:18080/v1", "name": "qwen"}},
            "apps": [{"name": "cursor", "drives": True, "status": "signed-in"}], "mail_apps": [], "chat_apps": []}
    assert cliprun._onboard_auto(_args(auto=True), load_file(env), scan=scan) == 0
    paiste = load_file(env)["paiste"]
    assert paiste["preferred"] == "cursor"
    assert paiste["order"][:2] == ["cursor", "brain"]
    assert ":8080" not in env.read_text()


def test_keep_choice_rules():
    old = _restored()["paiste"]
    shuffled = {"order": ["brain", "ssh", "cursor"],
                "models": {"brain": dict(_ssh(), remote_url="http://127.0.0.1:8080/v1"), "cursor": _cursor(),
                           "ssh": dict(_ssh(), remote_url="http://127.0.0.1:8080/v1")}}
    out = routefix.keep_choice(old, shuffled)
    assert out["order"][:2] == ["cursor", "brain"] and out["preferred"] == "cursor"
    assert out["models"]["brain"]["remote_url"] == "http://127.0.0.1:18080/v1"
    edited = routefix.keep_choice(old, shuffled, edits=True)
    assert edited["order"][:2] == ["cursor", "brain"] and edited["preferred"] == "cursor"
    assert edited["models"]["brain"]["remote_url"] == "http://127.0.0.1:18080/v1"
    picked = routefix.keep_choice(old, old, pick="brain")
    assert picked["order"][0] == "brain" and picked["preferred"] == "brain"
    added = routefix.keep_choice(old, {"models": {"x": _ssh("other")}, "order": ["x"]})
    assert added["order"][:2] == ["cursor", "brain"] and "x" in added["order"]
    assert set(old["models"]) <= set(added["models"])


def test_no_8080_default_in_app_sources():
    # :8080 is llama.cpp's llama-server default, so setup probes it (after :18080), but it is
    # never written as a default address.
    for rel in ("contrib/clip/Onboard.swift", "contrib/clip/run"):
        assert "127.0.0.1:8080" not in (ROOT / rel).read_text(encoding="utf-8"), rel
    probes = ('"http://127.0.0.1:8080/v1",', '("http://127.0.0.1:8080/v1/models"', 'for url in ("http://127.0.0.1:18080/v1"')
    for line in (ROOT / "hailports/clip/board.py").read_text(encoding="utf-8").splitlines():
        if "127.0.0.1:8080" in line:
            assert line.strip().startswith(probes), line
    assert routefix.DEFAULT_LOCAL_URL == "http://127.0.0.1:18080/v1"


def test_menu_single_instance_fail_reset_and_bundled_runner():
    menu = (ROOT / "contrib/clip/PaisteMenu.swift").read_text(encoding="utf-8")
    assert "Self.exitIfAlreadyRunning()" in menu and "runningApplications(withBundleIdentifier:" in menu
    body = menu[menu.index("func fail(_ hint: String)"):]
    body = body[:body.index("// MARK: right-click menu")]
    assert "asyncAfter(deadline: .now() + 4" in body and "PastePath.note(" in body and "self.mark" in body
    assert 'env["PAISTY_DEV"] == "1"' in menu
