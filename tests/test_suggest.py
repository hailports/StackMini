"""Suggestion hold: dedupe, one-use token, edit, and no create without a click."""

from hailports.suggest import (
    allow,
    already_tracked,
    apply_form,
    apply_status,
    classify,
    confirm_html,
    decide,
    draft_from_source,
    edit_html,
    email_html,
    fuzzy_duplicate,
    grounded,
    idempotency_key,
    mark,
    pick,
    render_suggestion,
)


NOW = 1_700_000_000.0
ASK = "Riley: usernames are not working for the new shop and they cannot log in."


def _story_item(dry=False, title="Usernames are not working", desc=ASK):
    story = {
        "type": "Bug",
        "title": title,
        "description": desc,
        "acceptance": ["usernames are not working"],
        "area": "Desk",
        "iteration": "Current",
        "parent": "",
        "source_url": "https://example.test/thread/1",
        "requester": "Riley",
    }
    return {
        "status": "pending",
        "expires": NOW + 3600,
        "dry_run": dry,
        "source_key": "src-1",
        "idem_key": "idem-1",
        "action": {"payload": {"story": story, "dry_run": dry}},
    }


def test_classify_skips_weak_internal_and_known_ids():
    assert classify("Shop access", ASK) == "access"
    assert classify("Hello", "can you both log out and back in") == ""
    assert classify("Legal file", "please fix this", "/legal/file") == ""
    internal = {
        "source": "mail",
        "ref": "prep-1",
        "title": "[LOCAL :8080 PREP] card",
        "body": "please fix the gate on the north side",
        "url": "",
        "who": "Riley",
    }
    assert pick([internal], set(), {}, [], NOW) is None
    assert already_tracked("see 44021 in the note", {"44021"})
    assert not already_tracked("see 44021 in the note", {"99999"})
    row = {
        "source": "chat",
        "ref": "t-1",
        "title": "Shop access",
        "body": ASK,
        "url": "https://example.test/thread/1",
        "who": "thread_long",
    }
    own = dict(row)
    own["body"] = "Casey: can you fix the gate on the north side please"
    own["title"] = "North gate"
    picked = pick([row, own], set(), {}, [], NOW, operator_names=("casey",))
    assert picked["kind"] == "access"
    assert picked["story"]["requester"] == "Riley"
    assert pick([own], set(), {}, [], NOW, operator_names=("casey",)) is None
    assert pick([row], {"44021"}, {}, [], NOW) is None or "44021" not in ASK
    tracked = dict(row)
    tracked["body"] = ASK + "\nTracker 44021"
    assert pick([tracked], {"44021"}, set(), [], NOW) is None


def test_dedupe_idem_fuzzy_and_daily_cap():
    title = "Shop usernames are not working"
    key = idempotency_key("Riley", title)
    assert key == idempotency_key("riley", title)
    store = {}
    src = "source-a"
    ok, why = allow(store, src, key, NOW)
    assert ok and why == "ok"
    store = mark(store, src, key, NOW, "email")
    assert allow(store, src, "other", NOW)[1] == "source"
    assert allow(store, "other-source", key, NOW)[1] == "idem"
    store = mark(store, "gone", "gone-key", NOW, "dismiss")
    assert allow(store, "gone", "fresh", NOW)[0] is False
    only_never = {"never": ["gone"]}
    assert allow(only_never, "gone", "fresh", NOW)[1] == "never"
    capped = {"days": {__import__("time").strftime("%Y%m%d", __import__("time").localtime(NOW)): 10}}
    assert allow(capped, "fresh", "fresh-key", NOW)[1] == "cap"
    dup = fuzzy_duplicate(
        title,
        "usernames are not working for the new shop",
        [{"title": "Shop usernames failing", "description": "usernames are not working", "ref": "card-9", "ts": NOW - 100}],
        NOW,
    )
    assert dup["duplicate"] is True
    old = fuzzy_duplicate(
        title,
        "usernames are not working for the new shop",
        [{"title": "Shop usernames failing", "description": "usernames are not working", "ref": "card-9", "ts": NOW - 20 * 86400}],
        NOW,
    )
    assert old["duplicate"] is False
    assert grounded({"title": "Fourteen widgets deployed", "description": "deployed fourteen widgets"}, ASK)[0] is False


def test_preview_and_dry_token_do_not_create_and_token_is_single_use():
    calls = {"n": 0, "sent": 0}

    def search_fn(_story):
        return {"ok": True, "ids": []}

    def create_fn(_story):
        calls["n"] += 1
        return {"ok": True, "work_item_id": "55"}

    def sender(*_args):
        calls["sent"] += 1

    item = _story_item(dry=False)
    preview = decide(item, None, "get", search_fn, create_fn, NOW)
    assert preview["created"] is False and preview["preview"] is True and calls["n"] == 0
    page = render_suggestion(item["action"]["payload"]["story"], "/approve", "/edit", "/dismiss")
    assert "Approve &amp; Post" in page and "Edit" in page and "Dismiss" in page
    assert calls["sent"] == 0 and calls["n"] == 0
    email_html(item["action"]["payload"]["story"], "/approve", "/edit", "/dismiss")
    assert calls["sent"] == 0

    dry = _story_item(dry=True)
    first = decide(dry, None, "approve", search_fn, create_fn, NOW)
    assert first["dry_run"] is True and first["created"] is False and calls["n"] == 0
    assert "Test only" in confirm_html(first)
    dry = apply_status(dry, first)
    second = decide(dry, None, "approve", search_fn, create_fn, NOW)
    assert second["error"] == "status_approved" and calls["n"] == 0

    live = _story_item(dry=False)
    posted = decide(live, None, "approve", search_fn, create_fn, NOW)
    assert posted["created"] is True and calls["n"] == 1
    live = apply_status(live, posted)
    again = decide(live, None, "post", search_fn, create_fn, NOW)
    assert again["created"] is False and again["error"] == "status_approved" and calls["n"] == 1


def test_edit_cancel_dismiss_and_fail_closed():
    calls = {"n": 0}

    def search_ok(_story):
        return {"ok": True, "ids": []}

    def search_dup(_story):
        return {"ok": True, "ids": ["9"]}

    def search_down(_story):
        raise RuntimeError("down")

    def create_fn(story):
        calls["n"] += 1
        return {"ok": True, "work_item_id": "77"}

    item = _story_item()
    form = edit_html(item["action"]["payload"]["story"], "/edit", "sig")
    assert "textarea" in form and "Save &amp; Post" in form and "Usernames are not working" in form
    assert "Cancel" in form
    edited = decide(
        item,
        {"title": "Usernames are not working today", "description": ASK, "type": "Bug"},
        "post",
        search_ok,
        create_fn,
        NOW,
    )
    assert edited["created"] is True and calls["n"] == 1
    assert edited["story"]["title"] == "Usernames are not working today"
    cancelled = decide(item, {"title": "ignored"}, "cancel", search_ok, create_fn, NOW)
    assert cancelled["cancelled"] is True and cancelled["created"] is False and calls["n"] == 1
    dismissed = decide(_story_item(), None, "dismiss", search_ok, create_fn, NOW)
    assert dismissed["dismissed"] is True and calls["n"] == 1
    dup = decide(_story_item(), None, "approve", search_dup, create_fn, NOW)
    assert dup["error"] == "duplicate" and dup["created"] is False and calls["n"] == 1
    assert "Not created" in confirm_html(dup)
    closed = decide(_story_item(), None, "approve", search_down, create_fn, NOW)
    assert closed["error"] == "fail_closed" and calls["n"] == 1
    shaped = apply_form({"title": "old", "type": "note"}, {"type": "bug", "acceptance": "one; two"})
    assert shaped["type"] == "Bug" and shaped["acceptance"] == ["one", "two"]
    draft = draft_from_source("Shop access", ASK, "thread_abc", "https://example.test/thread/1")
    assert draft["grounded"] is True
    assert draft["type"] == "Bug"
    assert "Riley" in draft["description"]
