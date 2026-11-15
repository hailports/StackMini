"""Ladder for a local chat bridge and a local mail bridge. No live process."""
from __future__ import annotations

from dataclasses import replace

from hailports.local_bridges.selfheal import (
    Snap, command_matches, detect, heal, kickstart_argv,
    launchd_is_stuck, rebootstrap_argv, rungs_for,
)

ALLOWED = frozenset({"chat", "mail"})
CHAT_CMD = (
    "Browser --headless=new --user-data-dir=/tmp/profile-chat "
    "--remote-debugging-port=18812 https://example.test/chat"
)


class Board:
    def __init__(self, snap: Snap):
        self.snap = snap
        self.calls: list[str] = []
        self.ensure_line = "UP"
        self.kick_up = True

    def adopt(self, bridge: str) -> None:
        self.calls.append("adopt")
        self.snap.receipt_pid = self.snap.service_pid

    def kickstart(self, bridge: str) -> None:
        self.calls.append("kickstart")
        if self.kick_up:
            self.snap.port_up = True
            self.snap.cdp_up = True

    def unstick(self, bridge: str) -> None:
        self.calls.append("unstick")
        if getattr(self, "unstick_up", True):
            self.snap.port_up = True
            self.snap.cdp_up = True
            self.snap.service_state = "running"
            self.snap.service_pid = self.snap.service_pid or 9
            self.snap.receipt_pid = self.snap.service_pid

    def restart_pid(self, bridge: str) -> None:
        self.calls.append("restart_pid")
        self.snap.port_up = True
        self.snap.cdp_up = True
        self.snap.receipt_pid = self.snap.service_pid

    def ensure(self, bridge: str) -> str:
        self.calls.append("ensure")
        return self.ensure_line

    def refresh(self, bridge: str) -> None:
        self.calls.append("refresh")

    def snapshot(self, bridge: str) -> Snap:
        return replace(self.snap)

    def as_actions(self) -> dict:
        return {
            "adopt": self.adopt,
            "unstick": self.unstick,
            "kickstart": self.kickstart,
            "restart_pid": self.restart_pid,
            "ensure": self.ensure,
            "refresh": self.refresh,
            "snapshot": self.snapshot,
        }


def test_adopt_stops_when_the_receipt_matches():
    board = Board(Snap(port_up=True, cdp_up=True, receipt_pid=1, service_pid=9, service_state="running", cache_age_s=5, unread=0))
    row = heal("chat", board.snapshot("chat"), board.as_actions(), {}, now=1000, allowed=ALLOWED)
    assert row["rung"] == "adopt"
    assert board.calls == ["adopt", "ensure"]


def test_kickstart_runs_after_a_down_port():
    board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
    row = heal("chat", board.snapshot("chat"), board.as_actions(), {}, now=2000, allowed=ALLOWED)
    assert row["rung"] == "kickstart"
    assert board.calls == ["adopt", "kickstart", "ensure"]


def test_restart_pid_runs_only_when_kickstart_stays_down():
    board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
    board.kick_up = False
    row = heal("chat", board.snapshot("chat"), board.as_actions(), {}, now=3000, allowed=ALLOWED)
    assert row["rung"] == "restart_pid"
    assert "restart_pid" in board.calls


def test_stale_cache_triggers_kickstart_without_a_second_kill():
    board = Board(Snap(
        port_up=True, cdp_up=True, receipt_pid=9, service_pid=9,
        service_state="running", cache_age_s=601, unread=2,
    ))
    row = heal("mail", board.snapshot("mail"), board.as_actions(), {}, now=4000, allowed=ALLOWED)
    assert row["reason"] == "stale_cache"
    assert "kickstart" in board.calls
    assert "restart_pid" not in board.calls


def test_fresh_or_quiet_cache_is_not_a_trigger():
    assert detect(Snap(port_up=True, cdp_up=True, receipt_pid=9, service_pid=9, service_state="running", cache_age_s=30, unread=4)) is None
    assert detect(Snap(port_up=True, cdp_up=True, receipt_pid=9, service_pid=9, service_state="running", cache_age_s=9999, unread=0)) is None


def test_three_then_exhausted_note():
    state: dict = {}
    times = (10000, 10070, 10200, 10500)
    rows = []
    for now in times:
        board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
        rows.append(heal("chat", board.snapshot("chat"), board.as_actions(), state, now=now, allowed=ALLOWED))
    assert [r["action"] for r in rows] == ["heal", "heal", "heal", "HEAL_EXHAUSTED"]
    assert "no email" in state["note"]


def test_unknown_label_is_refused():
    try:
        kickstart_argv("com.example.other", 1, allowed=frozenset({"com.example.chat"}))
        raise AssertionError("label was accepted")
    except ValueError:
        pass
    assert kickstart_argv("com.example.chat", 7, allowed=frozenset({"com.example.chat"})) == [
        "launchctl", "kickstart", "-k", "gui/7/com.example.chat"
    ]


def test_command_match_rejects_other_ports():
    assert command_matches(CHAT_CMD, port=18812, profile="profile-chat", browser_mark="Browser")
    assert not command_matches(CHAT_CMD.replace("18812", "9227"), port=9227, profile="profile-chat", browser_mark="Browser")
    assert not command_matches(CHAT_CMD.replace("18812", "8080"), port=8080, profile="profile-chat", browser_mark="Browser")
    assert not command_matches(CHAT_CMD, port=18822, profile="profile-mail", browser_mark="Browser")


def test_unstick_runs_when_spawn_failed_after_sigterm():
    board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=None, service_pid=None, service_state="spawn failed"))
    row = heal("chat", board.snapshot("chat"), board.as_actions(), {}, now=30000, allowed=ALLOWED)
    assert row["action"] == "heal"
    assert row["rung"] == "unstick"
    assert row["result"] == "cleared"
    assert board.calls == ["adopt", "unstick", "ensure"]
    assert "kickstart" not in board.calls
    assert "restart_pid" not in board.calls


def test_rungs_for_exit_15_stuck_states():
    assert launchd_is_stuck("spawn scheduled", None, port_up=False)
    assert launchd_is_stuck("spawn failed", None, port_up=False)
    assert launchd_is_stuck("exited", None, port_up=False)
    assert not launchd_is_stuck("running", 42, port_up=False)
    stuck = Snap(port_up=False, cdp_up=False, service_pid=None, service_state="spawn scheduled")
    assert rungs_for(stuck) == ("adopt", "unstick", "kickstart")
    running = Snap(port_up=False, cdp_up=False, service_pid=9, service_state="running")
    assert rungs_for(running) == ("adopt", "kickstart", "restart_pid")


def test_unstick_then_kickstart_if_rebootstrap_leaves_port_down():
    board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=None, service_pid=None, service_state="spawn failed"))
    board.unstick_up = False
    board.kick_up = True
    row = heal("chat", board.snapshot("chat"), board.as_actions(), {}, now=31000, allowed=ALLOWED)
    assert row["rung"] == "kickstart"
    assert board.calls == ["adopt", "unstick", "kickstart", "ensure"]
    assert "restart_pid" not in board.calls


def test_rebootstrap_argv_uses_asuser_into_gui_domain():
    allowed = frozenset({"com.example.chat"})
    plist = "/tmp/LaunchAgents/com.example.chat.plist"
    cmds = rebootstrap_argv("com.example.chat", 501, plist, allowed=allowed)
    assert cmds == [
        ["launchctl", "asuser", "501", "launchctl", "bootout", "gui/501", plist],
        ["launchctl", "asuser", "501", "launchctl", "bootstrap", "gui/501", plist],
    ]
    try:
        rebootstrap_argv("com.example.other", 501, plist, allowed=allowed)
        raise AssertionError("label was accepted")
    except ValueError:
        pass
    try:
        rebootstrap_argv("com.example.chat", 501, "/tmp/other.plist", allowed=allowed)
        raise AssertionError("foreign plist was accepted")
    except ValueError:
        pass


def test_kickstart_argv_can_omit_kill_when_no_pid():
    allowed = frozenset({"com.example.chat"})
    assert kickstart_argv("com.example.chat", 7, allowed=allowed) == [
        "launchctl", "kickstart", "-k", "gui/7/com.example.chat",
    ]
    assert kickstart_argv("com.example.chat", 7, allowed=allowed, kill=False) == [
        "launchctl", "kickstart", "gui/7/com.example.chat",
    ]


def test_exhausted_clears_and_resets_budget_once_healthy():
    state: dict = {}
    times = (40000, 40070, 40200, 40500)
    for now in times[:-1]:
        board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
        assert heal("chat", board.snapshot("chat"), board.as_actions(), state, now=now, allowed=ALLOWED)["action"] == "heal"
    board = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
    assert heal("chat", board.snapshot("chat"), board.as_actions(), state, now=times[-1], allowed=ALLOWED)["action"] == "HEAL_EXHAUSTED"
    healthy = Board(Snap(port_up=True, cdp_up=True, receipt_pid=9, service_pid=9, service_state="running", cache_age_s=5, unread=0))
    row = heal("chat", healthy.snapshot("chat"), healthy.as_actions(), state, now=40600, allowed=ALLOWED)
    assert row["action"] == "none"
    assert healthy.calls == []
    again = Board(Snap(port_up=False, cdp_up=False, receipt_pid=9, service_pid=9, service_state="running"))
    assert heal("chat", again.snapshot("chat"), again.as_actions(), state, now=40700, allowed=ALLOWED)["action"] == "heal"
