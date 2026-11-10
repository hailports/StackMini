"""Interactive setup, driven with pexpect. Skipped on Windows and in CI."""

from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("pexpect")
import pexpect

from tests.test_modes import KEY, MockAPI

ROOT = Path(__file__).resolve().parents[1]


def _free() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def test_interactive_wizard(tmp_path):
    if sys.platform.startswith("win"):
        pytest.skip("pexpect wizard is run on macOS and Linux")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    httpd.hits = []
    httpd.bodies = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    web = _free()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["HAILPORTS_PROMPT"] = "plain"
    env["HAILPORTS_NO_KEYCHAIN"] = "1"
    env["HAILPORTS_API_KEY"] = KEY
    child = pexpect.spawn(
        sys.executable,
        ["-m", "hailports", "setup", "--state-dir", str(tmp_path)],
        cwd=str(ROOT),
        env=env,
        timeout=40,
        encoding="utf-8",
    )
    child.expect("step mode")
    child.sendline("api")
    child.expect("step provider")
    child.sendline("openai")
    child.expect("step base_url")
    child.sendline(f"http://127.0.0.1:{port}/v1")
    child.expect("step cloud_model")
    child.sendline("mock")
    child.expect("step key")
    child.sendline("env")
    child.expect("step web_port")
    child.sendline(str(web))
    child.expect("step modules")
    child.sendline("cli,webchat")
    child.expect("step connect")
    child.sendline("skip")
    child.expect("step write_ok")
    child.sendline("y")
    child.expect("Setup finished.")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "mode: api" in text
    assert KEY not in text
    assert "mock" in text
    httpd.shutdown()
