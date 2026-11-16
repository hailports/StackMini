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
    "hey Casey -- thanks, I'll check the dealer's permission set assignments on asset 440199 "
    "and get back to you."
)
WOVEN = (
    "hey Casey -- asset 440199 sits on the North Shop account, "
    "and their Dealer Clerk login has no permission set group. "
    "that access change needs a helpdesk ticket -- "
    "can you open one for the Dealer Clerk login?"
)
JIRA = (
    "Morgan Lee 2:11 PM\n"
    "PORTAL-4401 is stuck on the dealer login. can you check?"
)
SERIAL = (
    "Ren Cole 4:44 PM\n"
    "serial SN44021 will not power on after the swap. can you check?"
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


def _raw_ids(text):
    import re
    found = []
    for match in re.finditer(r"\b[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?\b", text or ""):
        token = match.group(0)
        if valid_sf_id(token) or (len(token) in (15, 18) and token[:3] in {"001", "005", "02i", "0PS", "0PG"}):
            found.append(token)
    return found


def _assert_paste(text, item):
    assert item in text
    assert "/lightning/" not in text.lower()
    assert "i have not" not in text.lower()
    assert _raw_ids(text) == []


def test_prompt_names_the_item_and_bans_a_generic_look():
    rules = draft_rules("Casey")
    assert "name that item and the concrete next step" in rules
    assert "never a raw CRM id" in rules
    assert "/lightning/" in rules
    assert "have not changed anything" in rules
    assert "I'll take a look" in rules
    assert "Do not invent a finding" in rules
    assert "helpdesk ticket" in rules
    assert "Never offer, promise, or claim to assign" in rules
    ask = rewrite_ask("Casey")
    assert "name that item and the concrete next step" in ask
    assert "never a raw CRM id" in ask
    assert "/lightning/" in ask
    assert "I'll take a look" in ask
    assert "helpdesk ticket" in ask
    assert "Never offer, promise, or claim to assign" in ask


def test_generic_look_becomes_the_exact_item_and_the_next_step():
    assert concrete_reply(BEFORE, SCREEN, "") == AFTER
    _assert_paste(AFTER, "asset 440199")
    assert "take a look" not in AFTER.lower()
    assert "lookup" not in AFTER.lower()
    erred = (
        "Quinn 1:05 PM\n"
        "serial SN44021 failed. error: PUMP_OFFLINE on asset 440199. can you check?"
    )
    fixed = concrete_reply("hey Quinn -- I'll take a look.", erred, "")
    assert fixed == (
        "hey Quinn -- I'll check the PUMP_OFFLINE error on serial SN44021, asset 440199."
    )
    _assert_paste(fixed, "serial SN44021")
    assert "PUMP_OFFLINE" in fixed
    assert "asset 440199" in fixed
    assert "take a look" not in fixed.lower()
    assert "lookup" not in fixed.lower()
    kept = (
        "hey Casey -- thanks, I'll check the dealer's permission set assignments on asset 440199 "
        "and get back to you."
    )
    assert concrete_reply(kept, SCREEN, "") == kept
    leaked = (
        "hey Casey -- I'll open /lightning/r/Asset/02i000000000001AAA/view, asset 440199. "
        "I'll check the dealer's permission set assignments. I have not changed anything."
    )
    hidden = concrete_reply(leaked, SCREEN, "")
    _assert_paste(hidden, "asset 440199")
    assert hidden == AFTER
    order = "hey Pete -- I'll look at order 1844."
    assert concrete_reply(order, "order 1844 showed up with plates. I want this fixed.", "") == order
    claimed = (
        "hey Casey -- I'll open /lightning/r/Asset/02i000000000001AAA/view, asset 440199. "
        "I already assigned the permission set."
    )
    safe = concrete_reply(claimed, SCREEN, "")
    assert "already assigned" not in safe.lower()
    _assert_paste(safe, "asset 440199")
    assert concrete_reply("", SCREEN, "") == ""
    assert _DONE.search("I have not changed anything.") is None
    assert _DONE.search("I already assigned the set.")
    ticket = concrete_reply("hey Morgan -- I'll take a look.", JIRA, "")
    assert ticket == (
        "hey Morgan -- thanks, I'll check the dealer login on PORTAL-4401 and get back to you."
    )
    _assert_paste(ticket, "PORTAL-4401")
    serial = concrete_reply("hey Ren -- I'll take a look.", SERIAL, "")
    assert serial == (
        "hey Ren -- thanks, I'll check serial SN44021 and get back to you."
    )
    _assert_paste(serial, "serial SN44021")
    already = "hey Quinn -- serial SN44021 failed with PUMP_OFFLINE on asset 440199. I'll check it and get back to you."
    assert concrete_reply(already, erred, "") == already


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
    assert draft == WOVEN
    _assert_paste(draft, "asset 440199")
    assert "North Shop" in draft
    assert "Dealer Clerk" in draft
    assert "helpdesk ticket" in draft
    assert "which group" not in draft.lower()
    assert "take a look" not in draft.lower()
    assert "I already" not in draft
    assert FACTS_LABEL not in draft
    assert concrete_reply(WOVEN, SCREEN, facts) == WOVEN
    assert concrete_reply(AFTER, SCREEN, facts) == AFTER
    messages = apply_facts([{"role": "user", "content": "hello"}], facts)
    assert messages[0]["content"].startswith("Weave these facts into one or two natural sentences")
    assert "helpdesk ticket" in messages[0]["content"]
    assert FACTS_LABEL in messages[0]["content"]
    assert apply_facts([{"role": "user", "content": "hello"}], "")[0]["content"] == "hello"


def test_access_offers_become_a_helpdesk_ticket():
    offered = (
        "hey Casey -- asset 440199 sits on the North Shop account, "
        "and their Dealer Clerk login has no permission set group. "
        "which group should I add?"
    )
    assert concrete_reply(offered, SCREEN, "") == WOVEN
    assign = (
        "hey Casey -- I'll assign the permission set group for the Dealer Clerk login "
        "on asset 440199."
    )
    fixed = concrete_reply(assign, SCREEN, "")
    assert fixed == (
        "hey Casey -- that access change needs a helpdesk ticket -- "
        "can you open one for the Dealer Clerk login?"
    )
    _assert_paste(fixed, "Dealer Clerk")
    assert "I'll assign" not in fixed
    assert "which group" not in fixed.lower()
    ahead = (
        "hey Casey -- asset 440199 sits on the North Shop account, "
        "and their Dealer Clerk login has no permission set group. "
        "want me to go ahead?"
    )
    assert concrete_reply(ahead, SCREEN, "") == WOVEN


def test_unrelated_drafts_do_not_mention_a_helpdesk_ticket():
    serial = concrete_reply("hey Ren -- I'll take a look.", SERIAL, "")
    assert serial == (
        "hey Ren -- thanks, I'll check serial SN44021 and get back to you."
    )
    assert "ticket" not in serial.lower()
    assert "helpdesk" not in serial.lower()
    order = concrete_reply(
        "hey Pete -- I'll look at order 1844.",
        "order 1844 showed up with plates. I want this fixed.",
        "",
    )
    assert order == "hey Pete -- I'll look at order 1844."
    assert "ticket" not in order.lower()
    assert "helpdesk" not in order.lower()
    assert "ticket" not in AFTER.lower()
    assert "helpdesk" not in AFTER.lower()
    assert concrete_reply(AFTER, SCREEN, "") == AFTER


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
