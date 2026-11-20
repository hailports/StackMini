"""The menu bar pulse, the click rules and replace-versus-append live in contrib/clip/Pulse.swift.
Compile them with their test file and run it (macOS; skipped where Swift is not installed)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")
def test_pulse_state_machine_click_rules_and_replace(tmp_path):
    exe = tmp_path / "pulse-tests"
    build = subprocess.run(["swiftc", "-O", "-o", str(exe), str(ROOT / "contrib/clip/Pulse.swift"),
                            str(ROOT / "contrib/clip/tests/pulse/main.swift")], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-2000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "passed" in run.stdout


def test_menu_bar_uses_the_pulse_not_a_spinner():
    src = (ROOT / "contrib/clip/PaisteMenu.swift").read_text(encoding="utf-8")
    assert "func wheel(" not in src and "spinAngle" not in src
    assert "PulseSpec.frame(" in src and "accessibilityDisplayShouldReduceMotion" in src
    for script in ("build.sh", "package.sh"):
        assert "Pulse.swift" in (ROOT / "contrib/clip" / script).read_text(encoding="utf-8")


def test_second_click_routes_through_the_redraft_popup():
    src = (ROOT / "contrib/clip/PaisteMenu.swift").read_text(encoding="utf-8")
    assert "ClickRouter.decide(" in src and "showRedraftPopup" in src
    for title in ('"Redraft"', '"Copy again"', '"Cancel"'):
        assert title in src
    # A redraft goes through the replace path, never a plain paste on top.
    assert "deliverRedraft(text" in src and '"--prior"' in src
