from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


CONTROLLER = Path(__file__).resolve().parents[1] / "scripts" / "bridge_ownership.sh"
SOURCE = CONTROLLER.read_text(encoding="utf-8")

HOME = "/var/bridge-test"
LABEL = "com.example.bridge.zoom"
BROWSER = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROFILE = "/var/bridge-test/bridge-profile"
PORT = "18812"
URL = "https://app.zoom.us/wc/team-chat"
KEEPALIVE = "/var/bridge-test/stack/bin/zoom-bridge-keepalive.sh"


def _block(begin: str, end: str) -> str:
    return SOURCE.split(begin, 1)[1].split(end, 1)[0]


LAUNCHD_BLOCK = _block(
    "# BRIDGE_LAUNCHD_OWNERSHIP_BEGIN",
    "# BRIDGE_LAUNCHD_OWNERSHIP_END",
)
RECEIPT_BLOCK = _block(
    "# BRIDGE_OWNED_RECEIPT_BEGIN",
    "# BRIDGE_OWNED_RECEIPT_END",
)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )


def _launchd_fixture(**overrides: str) -> str:
    values = {
        "state": "running",
        "program": BROWSER,
        "pid": "932",
        "path": f"{HOME}/Library/LaunchAgents/{LABEL}.plist",
        "browser_arg": BROWSER,
        "headless_arg": "--headless=new",
        "profile_arg": f"--user-data-dir={PROFILE}",
        "port_arg": f"--remote-debugging-port={PORT}",
        "url_arg": URL,
    }
    values.update(overrides)
    return "\n".join(
        [
            f"path = {values['path']}",
            f"state = {values['state']}",
            f"program = {values['program']}",
            "arguments = {",
            f"    {values['browser_arg']}",
            f"    {values['headless_arg']}",
            f"    {values['profile_arg']}",
            f"    {values['port_arg']}",
            f"    {values['url_arg']}",
            "}",
            f"pid = {values['pid']}",
        ]
    )


def _launchd_proof_script(fixture: str, *, persistent: str = "1") -> str:
    return f"""
set -u
HOME={shlex.quote(HOME)}
PERSISTENT={shlex.quote(persistent)}
BROWSER_LABEL={shlex.quote(LABEL)}
BROWSER={shlex.quote(BROWSER)}
PROFILE={shlex.quote(PROFILE)}
PORT={shlex.quote(PORT)}
URL={shlex.quote(URL)}
{LAUNCHD_BLOCK}
exact_bridge_pid() {{ printf '%s\\n' 932; }}
launchd_job_print() {{ cat <<'LAUNCHD'
{fixture}
LAUNCHD
}}
launchd_owned_bridge_pid
"""


def _keepalive_fixture(**overrides: str) -> str:
    values = {
        "state": "running",
        "program": "/bin/bash",
        "pid": "932",
        "path": f"{HOME}/Library/LaunchAgents/{LABEL}.plist",
        "arg1": "/bin/bash",
        "arg2": KEEPALIVE,
    }
    values.update(overrides)
    return "\n".join(
        [
            f"path = {values['path']}",
            f"state = {values['state']}",
            f"program = {values['program']}",
            "arguments = {",
            f"    {values['arg1']}",
            f"    {values['arg2']}",
            "}",
            f"pid = {values['pid']}",
        ]
    )


_LIVE_EDGE = (
    f"{BROWSER} --headless=new --user-data-dir={PROFILE} "
    f"--remote-debugging-port={PORT} {URL}"
)


def _keepalive_proof_script(fixture: str, live_command: str) -> str:
    return f"""
set -u
HOME={shlex.quote(HOME)}
PERSISTENT=1
BROWSER_LABEL={shlex.quote(LABEL)}
BROWSER={shlex.quote(BROWSER)}
PROFILE={shlex.quote(PROFILE)}
PORT={shlex.quote(PORT)}
URL={shlex.quote(URL)}
{LAUNCHD_BLOCK}
exact_bridge_pid() {{ printf '%s\\n' 932; }}
launchd_job_print() {{ cat <<'LAUNCHD'
{fixture}
LAUNCHD
}}
live_bridge_command() {{ printf '%s\\n' {shlex.quote(live_command)}; }}
launchd_owned_bridge_pid
"""


def test_keepalive_exec_bridge_is_adopted_when_live_argv_matches():
    result = _run(_keepalive_proof_script(_keepalive_fixture(), _LIVE_EDGE))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "932"


def test_keepalive_exec_bridge_rejects_foreign_live_argv():
    cases = [
        _LIVE_EDGE.replace("--headless=new", "--headful"),
        _LIVE_EDGE.replace(PORT, "18813"),
        _LIVE_EDGE.replace(URL, "https://example.com"),
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--headless=new --user-data-dir={PROFILE} --remote-debugging-port={PORT} {URL}"
        ),
        "",
    ]
    for live_command in cases:
        result = _run(_keepalive_proof_script(_keepalive_fixture(), live_command))
        assert result.returncode != 0
        assert result.stdout == ""


def test_keepalive_exec_bridge_rejects_other_bash_script():
    fixture = _keepalive_fixture(arg2="/tmp/not-the-keepalive.sh")
    result = _run(_keepalive_proof_script(fixture, _LIVE_EDGE))

    assert result.returncode != 0
    assert result.stdout == ""


def test_keepalive_exec_bridge_rejects_pid_mismatch():
    fixture = _keepalive_fixture(pid="933")
    result = _run(_keepalive_proof_script(fixture, _LIVE_EDGE))

    assert result.returncode != 0
    assert result.stdout == ""


def test_exact_launchd_owned_bridge_is_accepted_after_reboot():
    result = _run(_launchd_proof_script(_launchd_fixture()))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "932"


def test_languishing_launchd_owned_bridge_is_accepted():
    result = _run(_launchd_proof_script(_launchd_fixture(state="languishing")))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "932"


def test_partial_or_foreign_launchd_proof_is_rejected():
    cases = [
        ({}, "0"),
        ({"state": "waiting"}, "1"),
        ({"program": "/tmp/Microsoft Edge"}, "1"),
        ({"pid": "933"}, "1"),
        ({"path": "/tmp/foreign.plist"}, "1"),
        ({"browser_arg": "/tmp/Microsoft Edge"}, "1"),
        ({"headless_arg": "--headless"}, "1"),
        ({"profile_arg": "--user-data-dir=/tmp/foreign"}, "1"),
        ({"port_arg": "--remote-debugging-port=18813"}, "1"),
        ({"url_arg": "https://example.com"}, "1"),
    ]
    for overrides, persistent in cases:
        result = _run(_launchd_proof_script(_launchd_fixture(**overrides), persistent=persistent))
        assert result.returncode != 0
        assert result.stdout == ""


def _receipt_script(tmp_path: Path, *, proof_succeeds: bool) -> str:
    owned = tmp_path / "owned"
    log_path = tmp_path / "log"
    owned.write_text("86572\n", encoding="utf-8")
    proof = "printf '%s\\n' 932" if proof_succeeds else "return 1"
    return f"""
set -u
OWNED={shlex.quote(str(owned))}
LOG={shlex.quote(str(log_path))}
{RECEIPT_BLOCK}
exact_bridge_pid() {{ printf '%s\\n' 932; }}
launchd_owned_bridge_pid() {{ {proof}; }}
log() {{ printf '%s\\n' "$*" >> "$LOG"; }}
owned_bridge_pid
status=$?
printf 'status=%s\\n' "$status"
printf 'receipt=%s\\n' "$(cat "$OWNED")"
[ -f "$LOG" ] && cat "$LOG"
exit "$status"
"""


def test_stale_receipt_is_atomically_rebound_only_after_full_launchd_proof(tmp_path: Path):
    result = _run(_receipt_script(tmp_path, proof_succeeds=True))

    assert result.returncode == 0, result.stderr
    assert "932\nstatus=0\nreceipt=932\n" in result.stdout
    assert "ADOPTED exact launchd-owned bridge pid 932 after reboot" in result.stdout
    assert not list(tmp_path.glob("owned.adopt.*"))


def test_failed_launchd_proof_preserves_stale_receipt(tmp_path: Path):
    result = _run(_receipt_script(tmp_path, proof_succeeds=False))

    assert result.returncode != 0
    assert (tmp_path / "owned").read_text(encoding="utf-8") == "86572\n"
    assert not (tmp_path / "log").exists()
    assert not list(tmp_path.glob("owned.adopt.*"))


def test_receipt_rebind_path_cannot_mutate_the_browser_process():
    forbidden = (
        "kill ",
        "pkill",
        "do_stop",
        "do_start",
        "launchctl bootout",
        "launchctl kickstart",
        "launchctl kill",
    )

    assert all(token not in RECEIPT_BLOCK for token in forbidden)
