"""Auto setup finds :8000 servers, never saves a dead llm default, and keyless hosted APIs never route."""

import json
from pathlib import Path

import pytest

from hailports.clip import board
from hailports.clip.hops import endpoints_from

OMLX = json.dumps({"data": [{"id": "Qwen3.8-27B-4bit"}]})


def test_scan_finds_server_on_8000():
    rows = board.scan_models(read_local=lambda url: OMLX if url == "http://127.0.0.1:8000/v1/models" else "", hosts=[])
    assert rows == [{"where": "This Mac", "host": "", "url": "http://127.0.0.1:8000/v1", "model": "Qwen3.8-27B-4bit"}]


def test_live_llm_uses_found_local_model():
    scan = {"suggest": {"ai": {"kind": "local", "name": "Qwen3.8-27B-4bit", "url": "http://127.0.0.1:8000/v1", "host": ""}}}
    got = board.live_llm({"base_url": "http://127.0.0.1:18080/v1", "model": "local"}, scan, "local", up=lambda u: False)
    assert got == {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.8-27B-4bit"}


def test_live_llm_drops_dead_loopback_default():
    got = board.live_llm({"base_url": "http://127.0.0.1:18080/v1", "model": "local"}, {}, "claude", up=lambda u: False)
    assert got is None


def test_live_llm_keeps_answering_or_remote_address():
    llm = {"base_url": "http://127.0.0.1:18080/v1", "model": "local"}
    assert board.live_llm(llm, {}, "", up=lambda u: True) == llm
    remote = {"base_url": "http://gpu-box.example:8000/v1", "model": "x"}
    assert board.live_llm(remote, {}, "", up=lambda u: False) == remote


def test_auto_setup_says_when_no_model_found(tmp_path: Path):
    scan = {"suggest": {"ai": {"kind": "none"}, "notes": {"kind": "folder"}}, "apps": [], "models": []}
    _, summary = board.auto_setup({}, scan, tmp_path, tmp_path / "notes")
    assert summary["ai"] == ""
    assert any("local model address" in need for need in summary["needs"])


def test_apply_board_drops_keyless_hosted_api():
    answers = {"link": "both", "locals": [{"url": "http://127.0.0.1:8000/v1", "model": "q"}],
               "apis": [{"url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "key_file": ""}]}
    out = board.apply_board({}, answers)
    assert all(not str(spec.get("base_url", "")).startswith("https://") for spec in out["models"].values())
    assert out["order"] and "api" not in out["order"]


def test_apply_board_api_only_without_key_asks_for_key():
    answers = {"link": "api", "apis": [{"url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "key_file": ""}]}
    with pytest.raises(board.BoardError, match="API key"):
        board.apply_board({}, answers)


def test_keyless_hosted_hop_is_skipped_at_draft_time():
    cfg = {"paiste": {"order": ["api", "local"], "models": {
        "api": {"via": "http", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "key_file": ""},
        "local": {"via": "http", "base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.8-27B-4bit"},
    }}}
    names = [hop["name"] for hop in endpoints_from(cfg)]
    assert names == ["local"]
