"""Named items in a draft, and read-only lookups when the user turns them on."""

import os
import stat
import time
from pathlib import Path

import pytest

from hailports.clip.lookups import (
    FACTS_LABEL,
    LookupProvider,
    CrmProvider,
    _DONE,
    apply_facts,
    collect_facts,
    concrete_reply,
    facts_for_draft,
    find_anchors,
    guard_select,
    lookups_enabled,
    sf_checksum,
    valid_sf_id,
)
from hailports.clip.scene import draft_rules, rewrite_ask


SCREEN = (
    "Casey North 9:14 AM\n"
    "Hi @Ada / @Bea the asset is created already in crm "
    "/lightning/r/Asset/02i000000000001AAA/view "
    "it might be a permission issue. @Quinn can you please check for the permissions of the dealer. "
    "asset 440199"
)
BEFORE = "hey Casey -- I'll take a look at the dealer's permissions on 440199."
AFTER = (
    "hey Casey -- I'll open /lightning/r/Asset/02i000000000001AAA/view, asset 440199. "
    "I'll check the dealer's permission set assignments. I have not changed anything."
)
FAKE = """#!/usr/bin/env python3
import json, os, sys, time
log = os.environ.get("SF_LOG", "")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\\t".join(sys.argv[1:]) + "\\n")
if os.environ.get("SF_SLEEP"):
    time.sleep(float(os.environ["SF_SLEEP"]))
if os.environ.get("SF_FAIL"):
    sys.exit(1)
args = sys.argv[1:]
if args[:2] == ["org", "display"]:
    json.dump({"status": 0, "result": {"id": "00XEXAMPLEORG0001", "alias": "north"}}, sys.stdout)
    sys.exit(0)
if args[:3] == ["data", "get", "record"]:
    json.dump({
        "status": 0,
        "result": {
            "Id": "02i000000000001AAA",
            "Name": "North pump",
            "OwnerId": "005000000000001AAA",
            "AccountId": "001000000000001AAA",
            "Username": "clerk-login",
        },
    }, sys.stdout)
    sys.exit(0)
if args[:2] == ["data", "query"]:
    query = args[args.index("--query") + 1] if "--query" in args else ""
    if not query.lower().lstrip().startswith("select"):
        sys.exit(2)
    low = query.lower()
    if "from account" in low:
        recs = [{"Id": "001000000000001AAA", "Name": "North Shop"}]
    elif "from user" in low:
        recs = [{"Id": "005000000000002AAA", "Name": "Dealer Clerk", "IsActive": True}]
    elif "from permissionsetassignment" in low:
        recs = []
    else:
        recs = []
    json.dump({"status": 0, "result": {"records": recs, "totalSize": len(recs), "done": True}}, sys.stdout)
    sys.exit(0)
sys.exit(2)
"""


def _install_sf(tmp_path, monkeypatch, sleep="", fail=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "sf"
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "sf.log"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SF_LOG", str(log))
    monkeypatch.delenv("SF_SLEEP", raising=False)
    monkeypatch.delenv("SF_FAIL", raising=False)
    if sleep:
        monkeypatch.setenv("SF_SLEEP", sleep)
    if fail:
        monkeypatch.setenv("SF_FAIL", "1")
    return log


def test_prompt_names_the_item_and_bans_a_generic_look():
    rules = draft_rules("Casey")
    assert "repeat that exact item and the concrete next step" in rules
    assert "I'll take a look" in rules
    assert "Do not invent a finding" in rules
    ask = rewrite_ask("Casey")
    assert "repeat that exact item and the concrete next step" in ask
    assert "I'll take a look" in ask


def test_generic_look_becomes_the_exact_item_and_the_next_step():
    assert concrete_reply(BEFORE, SCREEN, "") == AFTER
    assert "take a look" not in AFTER.lower()
    assert "lookup" not in AFTER.lower()
    erred = (
        "Quinn 1:05 PM\n"
        "serial SN44021 failed. error: PUMP_OFFLINE on asset 440199. can you check?"
    )
    fixed = concrete_reply("hey Quinn -- I'll take a look.", erred, "")
    assert "serial SN44021" in fixed
    assert "PUMP_OFFLINE" in fixed
    assert "asset 440199" in fixed
    assert "take a look" not in fixed.lower()
    assert "lookup" not in fixed.lower()
    kept = (
        "hey Casey -- I'll open /lightning/r/Asset/02i000000000001AAA/view, asset 440199. "
        "I'll check the dealer's permission set assignments."
    )
    assert concrete_reply(kept, SCREEN, "") == kept
    order = "hey Pete -- I'll look at order 1844."
    assert concrete_reply(order, "order 1844 showed up with plates. I want this fixed.", "") == order
    claimed = (
        "hey Casey -- I'll open /lightning/r/Asset/02i000000000001AAA/view, asset 440199. "
        "I already assigned the permission set."
    )
    safe = concrete_reply(claimed, SCREEN, "")
    assert "already assigned" not in safe.lower()
    assert "have not changed" in safe
    assert concrete_reply("", SCREEN, "") == ""
    assert _DONE.search("I have not changed anything.") is None
    assert _DONE.search("I already assigned the set.")


def test_lookups_are_off_until_the_config_says_on(tmp_path):
    home = tmp_path / "home"
    assert lookups_enabled({}, home) is False
    assert lookups_enabled({"lookups": "off"}, home) is False
    assert lookups_enabled({"lookups": "on"}, home) is True
    assert lookups_enabled({"paiste": {"lookups": "on"}}, home) is True
    side = home / ".config" / "paiste" / "lookups"
    side.parent.mkdir(parents=True)
    side.write_text("on\n", encoding="utf-8")
    assert lookups_enabled({}, home) is True
    assert lookups_enabled({"lookups": "off"}, home) is False


def test_crm_id_checksum_and_anchors():
    assert sf_checksum("02i000000000001") == "02i000000000001AAA"
    assert valid_sf_id("02i000000000001AAA")
    assert not valid_sf_id("02i000000000001ZZZ")
    assert not valid_sf_id("documentation12345")
    kinds = [(item.kind, item.exact) for item in find_anchors(SCREEN)]
    assert ("url", "/lightning/r/Asset/02i000000000001AAA/view") in kinds
    assert ("ticket", "asset 440199") in kinds
    assert not any(item[0] == "sfid" for item in kinds)


def test_only_a_select_can_run():
    assert guard_select("  SELECT Id FROM Asset WHERE Id = '02i000000000001AAA' ").startswith("SELECT")
    for query in (
        "DELETE FROM Account",
        "update Account set Name = 'x'",
        "SELECT Id FROM Asset; delete from User",
        "INSERT INTO Account (Name) VALUES ('x')",
    ):
        with pytest.raises(ValueError):
            guard_select(query)

    def boom(argv, timeout):
        raise AssertionError(argv)

    provider = CrmProvider(run=boom, which=lambda: "sf")
    with pytest.raises(ValueError):
        provider._call(
            ["sf", "data", "query", "--query", "DELETE FROM User", "--json"],
            time.monotonic() + 5,
        )
    with pytest.raises(ValueError):
        provider._call(["sf", "data", "delete", "record", "--json"], time.monotonic() + 5)


def test_fake_sf_is_read_only_and_caps_the_facts(tmp_path, monkeypatch):
    log = _install_sf(tmp_path, monkeypatch)
    assert facts_for_draft({}, SCREEN, tmp_path / "home") == ""
    assert not log.exists()
    facts = facts_for_draft({"lookups": "on"}, SCREEN, tmp_path / "home")
    assert facts.startswith(FACTS_LABEL + "\n")
    assert "02i000000000001AAA" in facts
    assert "North pump" in facts
    assert "North Shop" in facts
    assert "Dealer Clerk" in facts
    assert "no permission set assignment" in facts
    assert "clerk-login" not in facts
    assert "@" not in facts
    called = log.read_text(encoding="utf-8")
    assert "data\tget\trecord" in called
    assert "data\tquery" in called
    assert "DELETE" not in called.upper()
    assert "UPDATE" not in called.upper()
    assert "INSERT" not in called.upper()
    for line in called.splitlines():
        if "\tquery\t" in line:
            query = line.split("--query\t", 1)[1].split("\t", 1)[0]
            assert query.lower().startswith("select")
    draft = concrete_reply(BEFORE, SCREEN, facts)
    assert "take a look" not in draft.lower()
    assert "North pump" in draft
    assert "no permission set assignment" in draft
    assert "have not assigned" in draft
    assert "I already" not in draft
    assert FACTS_LABEL not in draft
    messages = apply_facts([{"role": "user", "content": "hello"}], facts)
    assert messages[0]["content"].startswith("State these facts plainly")
    assert FACTS_LABEL in messages[0]["content"]
    assert apply_facts([{"role": "user", "content": "hello"}], "")[0]["content"] == "hello"


def test_missing_sf_timeout_or_error_adds_nothing(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert collect_facts(SCREEN) == ""
    _install_sf(tmp_path, monkeypatch, fail=True)
    assert collect_facts(SCREEN, timeout_s=2) == ""
    _install_sf(tmp_path, monkeypatch, sleep="5")
    started = time.monotonic()
    assert collect_facts(SCREEN, timeout_s=0.4) == ""
    assert time.monotonic() - started < 3


def test_a_later_provider_can_plug_in_without_crm():
    class Later(LookupProvider):
        name = "jira"

        def ready(self):
            return False

    assert collect_facts("PORTAL-4401 needs a check", found=[Later()]) == ""

    class Big(LookupProvider):
        name = "ado"

        def ready(self):
            return True

        def facts(self, text, deadline):
            return "n" * 5000

    block = collect_facts("PORTAL-4401", found=[Big()])
    assert block.startswith(FACTS_LABEL)
    assert len(block) < 2100


def test_onboarding_copy_matches_the_p_icon_and_the_toggle():
    root = Path(__file__).resolve().parents[1] / "contrib" / "clip"
    onboard = (root / "Onboard.swift").read_text(encoding="utf-8")
    menu = (root / "PaisteMenu.swift").read_text(encoding="utf-8")
    assert "the p icon" in onboard
    assert "paperclip" not in onboard
    assert "No mail or notes leave this Mac" not in onboard
    assert "paisty itself doesn't send or keep your mail; your AI model writes the draft." in onboard
    assert "Look things up before drafting is in the menu" in onboard
    assert "app found." in onboard
    assert '"Mail: "' not in onboard
    assert '"Chat: "' not in onboard
    assert 'title: "Look things up before drafting"' in menu
    assert "left-click the p icon to draft" in menu
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    assert "lookups: off" in example.read_text(encoding="utf-8")


def test_injection_never_reaches_sf(tmp_path, monkeypatch):
    log = _install_sf(tmp_path, monkeypatch)
    hostile = SCREEN + " '; DELETE FROM User --"
    facts = collect_facts(hostile)
    assert facts.startswith(FACTS_LABEL)
    assert "DELETE" not in log.read_text(encoding="utf-8").upper()
    assert "';" not in log.read_text(encoding="utf-8")
