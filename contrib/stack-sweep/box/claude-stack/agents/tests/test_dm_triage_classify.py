#!/usr/bin/env python3
"""Synthetic classifier checks for hailports X DM triage."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.dm_triage_classify import classify, classify_label


def test_onlyfans_spam():
    r = classify({
        "handle": "luna_free",
        "display_name": "Luna 💋",
        "preview": "hey handsome check my bio for OnlyFans free trial",
        "bucket": "hidden",
        "followers": 3,
    })
    assert r["label"] == "spam_sex_bot", r


def test_hey_handsome_hidden():
    assert classify_label({
        "handle": "hotgirl92831",
        "preview": "Hey handsome",
        "bucket": "hidden",
        "followers": 5,
    }) == "spam_sex_bot"


def test_crypto_romance():
    assert classify_label({
        "handle": "profitqueen",
        "preview": "I can help you double your bitcoin gift me on telegram t.me/scam",
        "bucket": "you_may_know",
    }) == "spam_sex_bot"


def test_stackmini_partner():
    r = classify({
        "handle": "localbuildr",
        "preview": "love StackMini — want to talk partnership on a local LLM guide",
        "bucket": "priority",
        "followers": 1200,
    })
    assert r["label"] == "real_opportunity", r


def test_purchase_question():
    assert classify_label({
        "handle": "dev_jane",
        "preview": "I purchased /buy checkout but stripe failed, can you help?",
        "bucket": "you_may_know",
    }) == "real_opportunity"


def test_press_ask():
    assert classify_label({
        "handle": "techpod",
        "preview": "Would love to interview you about Mac Mini local inference for our podcast",
        "bucket": "priority",
    }) == "real_opportunity"


def test_plain_hey_unsure():
    # Empty opener without hidden/new-account stacking → unsure (fail closed)
    assert classify_label({
        "handle": "sam_codes",
        "preview": "hey",
        "bucket": "you_may_know",
        "followers": 400,
    }) == "unsure"


def test_hidden_alone_not_spam():
    # X misfiles; no sexual signal → unsure, not spam
    assert classify_label({
        "handle": "realperson",
        "preview": "saw your mac mini writeup",
        "bucket": "hidden",
        "followers": 80,
    }) in ("unsure", "real_opportunity")


def test_mixed_fail_closed():
    # Both OF bait and "partner" token → unsure
    assert classify_label({
        "handle": "weirdmix",
        "preview": "partner with me on OnlyFans collab nudes",
        "bucket": "priority",
    }) in ("spam_sex_bot", "unsure")  # OF should dominate → spam; allow either strong side


def test_nudes_word():
    assert classify_label({
        "handle": "x",
        "preview": "want nudes? link in bio",
        "bucket": "hidden",
    }) == "spam_sex_bot"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
