"""Keys turn themselves on and stay bound to one Mac; updates run on a schedule and survive launchd.
Swift parts compile and run on macOS (skipped where swiftc is missing)."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hailports.clip import gate

ROOT = Path(__file__).resolve().parents[1]
CLIP = ROOT / "contrib/clip"
SECRET = b"test-secret-not-real"


def _key(claims: dict) -> str:
    return gate.sign(claims, SECRET)


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")
def test_swift_license_and_schedule(tmp_path):
    exe = tmp_path / "license-tests"
    build = subprocess.run(["swiftc", "-O", "-framework", "IOKit", "-o", str(exe), str(CLIP / "License.swift"),
                            str(CLIP / "tests/license/main.swift")], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-2000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "0 failed" in run.stdout


def test_device_hash_matches_the_app_rule():
    uuid = "test-hardware-one"
    want = hashlib.sha256(("paisty-device:" + uuid.upper()).encode()).hexdigest()[:32]
    assert gate.device_hash(uuid.lower()) == want
    assert len(want) == 32 and "hardware" not in want


def test_device_id_reads_ioreg_once():
    gate._DEVICE.clear()
    calls = []

    class Out:
        stdout = '  "IOPlatformUUID" = "1A2B-3C4D"\n'

    def fake(*a, **k):
        calls.append(a)
        return Out()

    try:
        assert gate.device_id(run=fake) == gate.device_hash("1A2B-3C4D")
        assert gate.device_id(run=fake) == gate.device_id()
        assert len(calls) == 1
    finally:
        gate._DEVICE.clear()


def test_lifetime_device_key_is_forever_and_bound():
    here, there = "a" * 32, "b" * 32
    life = _key({"exp": 0, "plan": "lifetime", "sub": "own_k1", "dev": here, "kid": "k1"})
    assert gate.verify(life, SECRET, device=here)
    assert not gate.verify(life, SECRET, device=there)
    assert not gate.verify(life, SECRET, device="")
    assert not gate.verify(life, b"other-secret", device=here)
    # Unbound keys behave as before.
    assert gate.verify(_key({"exp": 0, "plan": "lifetime", "sub": "cus_1"}), SECRET, device="")
    assert not gate.verify(_key({"exp": int(time.time()) - 5, "plan": "trial", "sub": "t"}), SECRET, device="")


def test_server_check_sends_the_device(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "license_path", lambda: tmp_path / "license")
    monkeypatch.setattr(gate, "secret_path", lambda: tmp_path / "missing-secret")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(gate, "device_id", lambda run=None: "d" * 32)
    (tmp_path / "license").write_text("p2." + "A" * 60 + "." + "b" * 43)
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout=0):
        seen.update(json.loads(req.data))
        return Resp()

    assert gate.allowed({}, urlopen=opener)
    assert seen["device"] == "d" * 32 and seen["token"].startswith("p2.")


def test_app_wires_activation_claim_and_url_scheme():
    menu = (CLIP / "PaisteMenu.swift").read_text(encoding="utf-8")
    assert "kAEGetURL" in menu and "PaistyLicense.activationKey(from:" in menu
    assert "PaistyLicense.claimUntilActive" in menu
    assert '"--check-update"' in menu
    board = (CLIP / "Onboard.swift").read_text(encoding="utf-8")
    assert "PaistyLicense.claimUntilActive" in board, "Send my trial key turns the app on by itself"
    assert "PaistyLicense.valid(" in board
    for script in ("build.sh", "package.sh"):
        text = (CLIP / script).read_text(encoding="utf-8")
        assert "License.swift" in text and "-framework IOKit" in text
        assert "<string>paisty</string></array>" in text, "paisty:// is registered"


def test_updater_schedule_headers_and_launchd_safe_swap():
    src = (CLIP / "Updater.swift").read_text(encoding="utf-8")
    assert "PaistyLicense.feedHeaders()" in src
    assert "UpdateSchedule.every" in src and "didWakeNotification" in src
    # The old helper script waited on the pid; launchd kills a job's children, so the swap is in-process now.
    assert "kill -0" not in src and "paisty-swap.sh" not in src
    assert "UpdateSchedule.underLaunchd(" in src and "fm.moveItem(at: app, to: current)" in src
    assert "func dryRun()" in src


def test_release_runs_these_tests():
    wf = (ROOT / ".github/workflows/paisty-release.yml").read_text(encoding="utf-8")
    assert "tests/test_clip_license.py" in wf
    # Builds step by ten so a manual rebuild can publish the same commit one step up.
    assert "BUILD=$((COUNT * 10 + REBUILD))" in wf and "rebuild:" in wf
    assert "form download serves" in wf, "CI reads back the dmg the form serves"
