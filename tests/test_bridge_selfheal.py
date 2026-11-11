"""Ladder for a local chat bridge and a local mail bridge. No live process."""
from __future__ import annotations

from dataclasses import replace

from hailports.local_bridges.selfheal import Snap, command_matches, detect, heal, kickstart_argv

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
