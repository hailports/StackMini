"""The Model menu: pick among the chat models a server lists, without disturbing a hand-edited route.yaml."""

import json

from hailports.config import loads
from hailports.clip import board
from hailports.clip.pick_model import listing, set_value, use

OMLX = json.dumps({"object": "list", "data": [
    {"id": "Qwen3.8-27B-MTP-4bit"}, {"id": "Qwen3.8-27B-4bit"}, {"id": "Qwen3.6-35B-A3B-4bit"},
    {"id": "bge-m3-embed"},
]})

HAND = """# my setup, edited by hand
llm:
  base_url: http://127.0.0.1:8000/v1
  # dense was slow to load
  model: Qwen3.8-27B-4bit
paiste:
  order:
    - api
  models:
    api:
      base_url: https://api.openai.com/v1
      model: gpt-4.1-mini
"""


def _server(url):
    return OMLX if url == "http://127.0.0.1:8000/v1/models" else ""


def test_the_menu_lists_chat_models_and_marks_the_current_one():
    doc = listing(loads(HAND), read=_server)
    assert doc == {"ok": True, "models": ["Qwen3.8-27B-4bit", "Qwen3.6-35B-A3B-4bit"],
                   "current": "Qwen3.8-27B-4bit", "where": "this Mac", "switchable": True}


def test_switching_changes_one_line_and_keeps_comments(tmp_path):
    route = tmp_path / "route.yaml"
    route.write_text(HAND)
    assert use(loads(HAND), "Qwen3.6-35B-A3B-4bit", route, read=_server) == {"ok": True, "model": "Qwen3.6-35B-A3B-4bit"}
    after = route.read_text()
    assert after == HAND.replace("  model: Qwen3.8-27B-4bit", "  model: Qwen3.6-35B-A3B-4bit")
    assert "# dense was slow to load" in after


def test_switching_refuses_a_name_the_server_does_not_list(tmp_path):
    route = tmp_path / "route.yaml"
    route.write_text(HAND)
    for name in ("Qwen3.8-27B-MTP-4bit", "bge-m3-embed", "made-up"):
        assert use(loads(HAND), name, route, read=_server)["ok"] is False
    assert route.read_text() == HAND


def test_an_engine_in_the_order_is_switched_where_it_lives(tmp_path):
    text = ("paiste:\n  order:\n    - local\n  models:\n    local:\n      base_url: http://127.0.0.1:8000/v1\n"
            "      model: Qwen3.8-27B-4bit\n      timeout_s: 120\n")
    route = tmp_path / "route.yaml"
    route.write_text(text)
    assert use(loads(text), "Qwen3.6-35B-A3B-4bit", route, read=_server)["ok"] is True
    assert loads(route.read_text())["paiste"]["models"]["local"] == {
        "base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.6-35B-A3B-4bit", "timeout_s": 120}


def test_set_value_adds_a_missing_model_line_and_quotes_when_needed():
    text = "llm:\n    base_url: http://127.0.0.1:11434/v1\n"
    out = set_value(text, ["llm", "model"], "llama3.2:latest")
    assert out == 'llm:\n    model: "llama3.2:latest"\n    base_url: http://127.0.0.1:11434/v1\n'
    assert loads(out)["llm"]["model"] == "llama3.2:latest"


def test_set_value_refuses_what_it_cannot_prove():
    assert set_value("paiste:\n  order: []\n", ["llm", "model"], "x") is None  # no llm block to put it in
    assert set_value("llm: {base_url: x}\n", ["llm", "model"], "x") is None  # not a block mapping


def test_without_a_saved_model_the_list_shows_but_does_not_switch(tmp_path):
    cfg = {"llm": {"base_url": "http://127.0.0.1:8000/v1", "model": "local"}}
    assert listing(cfg, read=_server)["switchable"] is True
    assert use(cfg, "Qwen3.6-35B-A3B-4bit", tmp_path / "missing.yaml", read=_server)["ok"] is False


def test_a_dead_server_says_so():
    doc = listing(loads(HAND), read=lambda url: "")
    assert doc["ok"] is False and doc["why"] == "The model server on this Mac did not answer."


def test_setup_scan_offers_every_chat_model_on_a_server():
    rows = board.scan_models(read_local=_server, hosts=[])
    assert [(r["url"], r["model"]) for r in rows] == [
        ("http://127.0.0.1:8000/v1", "Qwen3.8-27B-4bit"), ("http://127.0.0.1:8000/v1", "Qwen3.6-35B-A3B-4bit")]


def test_setup_scan_puts_a_loaded_model_first():
    tags = json.dumps({"object": "list", "data": [{"id": "qwen3:32b"}, {"id": "llama3.2:latest"}]})
    ps = json.dumps({"models": [{"name": "llama3.2:latest", "model": "llama3.2:latest"}]})
    lms = json.dumps({"data": [{"id": "gemma-3-27b", "state": "not-loaded"}, {"id": "qwen3-8b", "state": "loaded"}]})

    def read(url):
        return {"http://127.0.0.1:11434/v1/models": tags, "http://127.0.0.1:11434/api/ps": ps,
                "http://127.0.0.1:1234/v1/models": json.dumps({"data": [{"id": "gemma-3-27b"}, {"id": "qwen3-8b"}]}),
                "http://127.0.0.1:1234/api/v0/models": lms}.get(url, "")

    rows = board.scan_models(read_local=read, hosts=[])
    assert [r["model"] for r in rows] == ["llama3.2:latest", "qwen3:32b", "qwen3-8b", "gemma-3-27b"]
