"""hermes-qwen4 sign-in detection reads only the Nous adapter line. No network."""
import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "contrib" / "hosted-lanes" / "hermes-qwen4"


def load():
    loader = importlib.machinery.SourceFileLoader("hermes_qwen4", str(SCRIPT))
    spec = importlib.util.spec_from_loader("hermes_qwen4", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def status(nous, xai):
    return (
        "Hermes proxy upstream adapters\n\n"
        f"  [nous    ] Nous Portal — {nous}\n"
        f"  [xai     ] xAI Grok OAuth — {xai}\n\n"
        "Start the proxy with: hermes proxy start [--provider <name>]\n"
    )


@pytest.fixture
def hq(monkeypatch, tmp_path):
    mod = load()
    fake = tmp_path / "hermes"
    fake.write_text("")
    monkeypatch.setenv("HERMES_BIN", str(fake))

    def use(text):
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=text, stderr=""))
    return mod, use


@pytest.mark.parametrize("nous,xai,want", [
    ("ready (bearer expires 2026-11-21T06:00:00Z)", "not logged in", True),
    ("not logged in", "ready (bearer expires 2026-11-21T06:00:00Z)", False),
    ("credentials need attention", "ready", False),
    ("not logged in", "not logged in", False),
    ("ready", "ready", True),
])
def test_signed_in_reads_only_nous_line(hq, nous, xai, want):
    mod, use = hq
    use(status(nous, xai))
    assert mod.signed_in() is want


def test_signed_in_false_when_nous_line_missing(hq):
    mod, use = hq
    use("Hermes proxy upstream adapters\n\n  [xai     ] xAI Grok OAuth — ready\n")
    assert mod.signed_in() is False
