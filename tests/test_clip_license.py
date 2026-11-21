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
    assert "PaistyLicense.claimUntilActive" in board, "after the website checkout the app turns itself on"
    assert "PaistyLicense.valid(" in board
    # 2026-11-21: no trial keys. The trial starts on the website; nothing in the app asks for one.
    for gone in ("Send my trial key", "api/trial", "sendTrialKey", "trial-email"):
        assert gone not in board, gone
    assert "PaistyAccess.showsKeyEntry" in board and "PaistyAccess.showsKeyEntry" in menu, "no key entry during a trial"
    assert "PaistyEntitlement.fromURL(url)" in menu and "PaistyEntitlement.clear()" in menu
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


PE = "pe1." + "C" * 60 + "." + "d" * 43


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _trial_home(monkeypatch, tmp_path, ends=None, license_text=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(gate, "device_id", lambda run=None: "d" * 32)
    conf = tmp_path / ".config" / "paiste"
    conf.mkdir(parents=True, exist_ok=True)
    if license_text is not None:
        (conf / "license").write_text(license_text)
    (conf / "entitlement").write_text(json.dumps({"token": PE, "ends": int(time.time()) + 86400 if ends is None else ends}))
    return conf


def test_trial_entitlement_drafts_without_a_license(monkeypatch, tmp_path):
    conf = _trial_home(monkeypatch, tmp_path)
    sent = []

    def opener(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp(b'{"ok": true, "trial": "on"}')

    assert gate.read_token({}) == "", "a trial writes no license file"
    assert gate.allowed({}, urlopen=opener)
    assert sent == [{"entitlement": PE, "device": "d" * 32}], "the entitlement goes in its own field, never as a token"
    assert not (conf / "license").exists()
    # Cached for 15 minutes: the next draft asks nobody.
    assert gate.allowed({}, urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert gate.credentials({}) == [{"entitlement": PE}]


def test_trial_entitlement_stops_when_the_server_says_so(monkeypatch, tmp_path):
    conf = _trial_home(monkeypatch, tmp_path)
    (conf / "entitlement-cache").write_text(json.dumps({"token": hashlib.sha256(PE.encode()).hexdigest(), "at": int(time.time()) - 3600}))
    assert not gate.allowed({}, urlopen=lambda req, timeout=0: _Resp(b'{"ok": false, "trial": "past_due"}'))
    assert not (conf / "entitlement-cache").exists(), "a no clears the cached yes"


def test_trial_entitlement_offline_grace(monkeypatch, tmp_path):
    conf = _trial_home(monkeypatch, tmp_path)
    digest = hashlib.sha256(PE.encode()).hexdigest()

    def down(req, timeout=0):
        raise OSError("offline")

    (conf / "entitlement-cache").write_text(json.dumps({"token": digest, "at": int(time.time()) - 3600}))
    assert gate.allowed({}, urlopen=down), "checked an hour ago: keeps drafting offline"
    (conf / "entitlement-cache").write_text(json.dumps({"token": digest, "at": int(time.time()) - 2 * 86400}))
    assert not gate.allowed({}, urlopen=down), "two days without a check: stops"
    _trial_home(monkeypatch, tmp_path, ends=int(time.time()) - 3 * 86400)
    (conf / "entitlement-cache").write_text(json.dumps({"token": digest, "at": int(time.time()) - 600 - 15 * 60}))
    assert not gate.allowed({}, urlopen=down), "never past a day after the trial end"


def test_license_first_and_a_working_key_is_never_traded_for_a_trial(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "secret_path", lambda: tmp_path / "missing-secret")
    _trial_home(monkeypatch, tmp_path, license_text="p2." + "A" * 60 + "." + "b" * 43)
    sent = []

    def opener(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp(b'{"ok": true}')

    assert gate.allowed({}, urlopen=opener)
    assert len(sent) == 1 and "token" in sent[0] and "entitlement" not in sent[0]
    assert gate.credentials({})[0] == {"token": "p2." + "A" * 60 + "." + "b" * 43}


def test_expired_old_key_falls_back_to_the_trial(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "secret_path", lambda: tmp_path / "missing-secret")
    _trial_home(monkeypatch, tmp_path, license_text="p2." + "A" * 60 + "." + "b" * 43)
    seen = []

    def opener(req, timeout=0):
        doc = json.loads(req.data)
        seen.append(sorted(doc))
        return _Resp(b'{"ok": true}' if "entitlement" in doc else b'{"ok": false}')

    assert gate.allowed({}, urlopen=opener)
    assert seen == [["device", "token"], ["device", "entitlement"]]


def test_only_a_real_entitlement_file_counts(monkeypatch, tmp_path):
    conf = _trial_home(monkeypatch, tmp_path)
    (conf / "entitlement").write_text(json.dumps({"token": "p2." + "A" * 60 + "." + "b" * 43}))
    assert gate.read_entitlement() == {}, "a license key in the trial file is ignored"
    (conf / "entitlement").write_text("not json")
    assert gate.read_entitlement() == {}
    assert not gate.allowed({}, urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))


def test_setup_text_and_instructions_use_the_trial(monkeypatch, tmp_path):
    from hailports.clip import board, run
    _trial_home(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, "secret_path", lambda: tmp_path / "missing-secret")
    seen = []

    def post(url, payload):
        seen.append(json.loads(payload))
        return json.dumps({"ok": True, "note": "# Notes\n\nA click searches this folder. paisty does not keep any data.\n"})

    note = board.server_setup_text(True, False, "claude", "", "https://www.paisty.com/api/setup", post=post, entitlement=PE)
    assert note and seen[0]["entitlement"] == PE and "token" not in seen[0]
    assert board.server_setup_text(True, False, "claude", "", "https://www.paisty.com/api/setup", post=post) == ""

    class Open:
        def __call__(self, req, timeout=0):
            seen.append(json.loads(req.data))
            return _Resp(b'{"ok": true, "system": "rules"}')

    monkeypatch.setattr(run.urllib.request, "urlopen", Open())
    assert run._instructions({}) == "rules"
    assert seen[-1] == {"entitlement": PE, "device": "d" * 32}
