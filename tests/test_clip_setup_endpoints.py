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


def test_unloadable_and_embedding_models_are_not_picked_first():
    body = json.dumps({"data": [{"id": "Qwen3.8-27B-MTP-4bit"}, {"id": "nomic-embed-text"}, {"id": "Qwen3.8-27B-4bit"}]})
    assert board.model_ids(body)[0] == "Qwen3.8-27B-4bit"
    rows = board.scan_models(read_local=lambda url: body if url == "http://127.0.0.1:8000/v1/models" else "", hosts=[])
    assert rows[0]["model"] == "Qwen3.8-27B-4bit"


def test_server_with_only_embedding_models_is_not_offered():
    body = json.dumps({"data": [{"id": "text-embedding-3-small"}, {"id": "bge-m3"}]})
    assert board.scan_models(read_local=lambda url: body if ":8000/" in url else "", hosts=[]) == []


def test_keyless_custom_https_gateway_is_kept():
    answers = {"link": "api", "apis": [{"url": "https://llm.example.org/v1", "model": "qwen", "key_file": ""}]}
    out = board.apply_board({}, answers)
    assert out["order"] == ["api"]
    cfg = {"paiste": {"order": ["api"], "models": {"api": out["models"]["api"]}}}
    assert [hop["name"] for hop in endpoints_from(cfg)] == ["api"]


def test_every_listed_paid_provider_counts_as_needing_a_key():
    from hailports.clip.hops import needs_key
    assert all(needs_key(row[3]) for row in board.API_PROVIDERS)


def test_first_save_never_writes_a_dead_default_address(tmp_path, monkeypatch):
    from hailports.clip import run as cliprun
    dest = tmp_path / "route.yaml"
    route = {"llm": {"base_url": "http://127.0.0.1:18080/v1", "model": "local"},
             "paiste": {"order": ["claude"], "models": {"claude": {"via": "claude", "bin": "/usr/bin/true"}}}}
    monkeypatch.setattr(board, "_model_up", lambda url: False)
    cliprun._save_route(dest, dict(route))
    assert "llm" not in dest.read_text()
    dest.unlink()
    monkeypatch.setattr(board, "_model_up", lambda url: True)
    cliprun._save_route(dest, dict(route))
    assert "127.0.0.1:18080" in dest.read_text()


def test_saved_llm_address_is_never_removed_by_a_later_save(tmp_path, monkeypatch):
    from hailports.clip import run as cliprun
    from hailports.config import save_file
    dest = tmp_path / "route.yaml"
    save_file(dest, {"llm": {"base_url": "http://127.0.0.1:8000/v1", "model": "q"},
                     "paiste": {"order": ["local"], "models": {"local": {"via": "http", "base_url": "http://127.0.0.1:8000/v1", "model": "q"}}}})
    monkeypatch.setattr(board, "_model_up", lambda url: False)
    cliprun._save_route(dest, {"llm": {"base_url": "http://127.0.0.1:8000/v1", "model": "q"},
                               "paiste": {"order": ["local"], "models": {"local": {"via": "http", "base_url": "http://127.0.0.1:8000/v1", "model": "q"}}, "copy": True}})
    assert "127.0.0.1:8000" in dest.read_text().split("paiste:")[0]


def _auto(dest: Path, cfg: dict, scan: dict) -> dict:
    import argparse
    from hailports.clip import run as cliprun
    from hailports.config import load_file
    cliprun._onboard_auto(argparse.Namespace(route_out=str(dest)), cfg, scan)
    return load_file(dest)


def _scan(url: str = "", model: str = "") -> dict:
    ai = {"kind": "local", "name": model, "url": url, "host": ""} if url else {"kind": "none"}
    return {"suggest": {"ai": ai, "notes": {"kind": "folder"}}, "apps": [], "models": [{"url": url}] if url else [],
            "mail_apps": [], "chat_apps": []}


def test_auto_setup_writes_the_found_model_on_a_fresh_mac(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(board, "_model_up", lambda url: ":8000/" in url)
    doc = _auto(tmp_path / "route.yaml", {"llm": {"base_url": "http://127.0.0.1:18080/v1", "model": "local"}},
                _scan("http://127.0.0.1:8000/v1", "Qwen3.8-27B-4bit"))
    assert doc["llm"] == {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.8-27B-4bit"}


def test_auto_setup_on_a_fresh_mac_with_nothing_running_writes_no_address(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(board, "_model_up", lambda url: False)
    doc = _auto(tmp_path / "route.yaml", {"llm": {"base_url": "http://127.0.0.1:18080/v1", "model": "local"}}, _scan())
    assert "llm" not in doc


def test_auto_setup_never_removes_or_replaces_a_saved_address(tmp_path, monkeypatch):
    from hailports.config import save_file
    monkeypatch.setenv("HOME", str(tmp_path))
    dest = tmp_path / "route.yaml"
    mine = {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.8-27B-4bit"}
    save_file(dest, {"llm": dict(mine)})
    # Server off: the saved address stays.
    monkeypatch.setattr(board, "_model_up", lambda url: False)
    assert _auto(dest, {"llm": dict(mine)}, _scan())["llm"] == mine
    # Server on, and the scan also found Ollama first: the saved, answering address stays.
    monkeypatch.setattr(board, "_model_up", lambda url: True)
    assert _auto(dest, {"llm": dict(mine)}, _scan("http://127.0.0.1:11434/v1", "llama3.2"))["llm"] == mine


def test_auto_summary_names_the_local_model(tmp_path):
    scan = {"suggest": {"ai": {"kind": "local", "name": "Qwen3.8-27B-4bit", "url": "http://127.0.0.1:8000/v1", "host": ""},
                        "notes": {"kind": "folder"}}, "apps": [], "models": [{"url": "http://127.0.0.1:8000/v1"}]}
    _, summary = board.auto_setup({}, scan, tmp_path, tmp_path / "notes")
    assert summary["ai"] == "local" and summary["model"] == "Qwen3.8-27B-4bit" and summary["host"] == ""
