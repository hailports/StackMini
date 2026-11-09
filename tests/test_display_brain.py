"""Display override, client match, keyboard reflow, brain ask, and local bridges."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

NOT_ON_WINDOWS = pytest.mark.skipif(sys.platform == "win32", reason="macOS-only reflow needs fcntl")

from hailports.brain.ask import ask
from hailports.brain.client import curl_command, socks_if_present
from hailports.brain.cycle import drop_doc, parity, probe_health, publish_manifest, run_cycle
from hailports.brain.dataline import query_block
from hailports.brain.freshness import is_trustworthy, snapshot, write_verdict
from hailports.brain.httpask import bind_allowed, overlay_network, serve
from hailports.brain.retrieve import link, rrf
from hailports.cli import main as cli_main
from hailports.display.catalog import CATALOG, IPAD_NATIVE, IPHONE_MODE, MAC_DEFAULT
from hailports.display.client import lookup_whois, match_client
from hailports.display.keyboard_reflow import (
    PHONE_PADDING,
    PHONE_SAFE_BOTTOM,
    Controller,
    Yabai,
    geometry,
    parse_env,
    render_agent,
    safe_frame,
)
from hailports.display.override import FLAG_2X, FLAG_BASE, TAIL, build_plist, parse_plist
from hailports.display.windows import fit_frame
from hailports.local_bridges.fabric import DraftBook, DraftOnly, enabled_specs, gc_allowed, heal, recycle_allowed
from hailports.notes import add, connect, note_meta, store_vector


NOW = 1_700_000_000.0


def _cfg(tmp_path: Path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text("mode: local\n", encoding="utf-8")
    return str(path)


def test_override_round_trip_includes_the_field_modes():
    modes = [(1440, 900), (1680, 1050), (1376, 1032), (1344, 1008)]
    blob = build_plist(0x843, 0x104, modes, "dummy")
    parsed = parse_plist(blob)
    assert parsed["modes"] == modes
    assert parsed["vendor"] == 0x843
    assert parsed["product"] == 0x104
    assert FLAG_BASE == 1 and FLAG_2X == 9 and TAIL == 0x200000
    for width, height in modes:
        assert (width * 2, height * 2) in parsed["hidpi"]
        assert (width * 2, height * 2) in parsed["pairs"]


def test_catalog_has_the_aspect_families_and_ipad_natives():
    aspects = {item.aspect for item in CATALOG}
    assert {"16:10", "16:9", "4:3"} <= aspects
    assert any(item.kind == "ipad" and item.width == 1376 and item.height == 1032 for item in CATALOG)
    assert IPAD_NATIVE.kind == "ipad"
    assert IPHONE_MODE.width == 768 and IPHONE_MODE.height == 1024
    assert MAC_DEFAULT.aspect == "16:10"


def test_client_matcher_and_optional_whois():
    ipad = match_client("ipad-host")
    assert ipad is not None and (ipad.width, ipad.height) == (1376, 1032)
    phone = match_client("iphone-host")
    assert phone is not None and (phone.width, phone.height) == (768, 1024)
    mac = match_client("macbook")
    assert mac is not None and mac.aspect == "16:10"
    assert match_client("conference-room") is None
    assert match_client("127.0.0.1") is None

    class Boom(dict):
        def get(self, key, default=None):
            raise AssertionError("whois was read")

    assert match_client("ipad-host", whois=Boom()) is not None
    named = match_client("127.0.0.1", whois={"HostName": "iphone"})
    assert named is not None and named.kind == "iphone"
    calls = []
    assert lookup_whois("peer", None) is None
    assert calls == []
    lookup_whois("peer", lambda peer: calls.append(peer))
    assert calls == ["peer"]


def test_window_reflow_pulls_a_frame_onto_the_canvas():
    fitted = fit_frame({"x": 2000, "y": 10, "w": 800, "h": 600}, {"x": 0, "y": 0, "w": 1680, "h": 1050})
    assert fitted["x"] + fitted["w"] <= 1680
    assert fitted["w"] <= 800
    assert fit_frame({"x": 10, "y": 10, "w": 100, "h": 100}, {"w": 1680, "h": 1050}) == {"x": 10, "y": 10, "w": 100, "h": 100}


def test_reflow_math_and_unknown_state_is_a_noop(tmp_path: Path):
    assert geometry()["safe_bottom"] == 761
    assert geometry()["padding"] == 202
    assert PHONE_SAFE_BOTTOM == 761 and PHONE_PADDING == 202
    assert geometry()["usable_bottom"] - PHONE_SAFE_BOTTOM == PHONE_PADDING
    clamped = safe_frame({"x": 0, "y": 800, "w": 700, "h": 400})
    assert clamped["y"] + clamped["h"] <= 761

    assert parse_env(None, NOW) == "unknown"
    assert parse_env("mobile_connected=1\n", NOW) == "unknown"
    assert parse_env(f"updated={NOW + 100}\nmobile_connected=1\nmobile_profile=iphone\n", NOW) == "unknown"
    assert parse_env(f"updated={NOW - 46}\nmobile_connected=1\nmobile_profile=iphone\n", NOW) == "unknown"
    assert parse_env(f"updated={NOW}\nmobile_connected=1\nmobile_profile=iphone\n", NOW) == "iphone"
    assert parse_env(f"updated={NOW}\nmobile_connected=1\nmobile_profile=ipad\n", NOW) == "normal"
    assert parse_env(f"updated={NOW}\nmobile_connected=0\nmobile_profile=none\n", NOW) == "normal"
    assert parse_env(f"updated={NOW}\nmobile_connected=1\nmobile_profile=other\n", NOW) == "unknown"

    calls = []

    def runner(args):
        calls.append(args)
        return "0"

    text = f"updated={NOW - 46}\nmobile_connected=1\nmobile_profile=iphone\n"
    controller = Controller(
        Yabai(runner=runner),
        support_dir=tmp_path,
        files=[(tmp_path / "mobile-session-state.env", text)],
        now=lambda: NOW,
    )
    result = controller.once(dry_run=False)
    assert result["noop"] == "unknown-state"
    assert calls == []
    dry = controller.once(dry_run=True)
    assert dry["dry_run"] is True
    assert dry["geometry"]["padding"] == 202
    assert dry["would"] == "noop"


@NOT_ON_WINDOWS
def test_reflow_arms_only_for_a_portrait_phone(tmp_path: Path):
    class Box:
        def __init__(self):
            self.padding = 12
            self.sets = []

        def runner(self, args):
            if args[:2] == ["config", "bottom_padding"] and len(args) == 2:
                return str(self.padding)
            if args[:2] == ["config", "bottom_padding"] and len(args) == 3:
                self.padding = int(args[2])
                self.sets.append(self.padding)
                return str(self.padding)
            if args[:3] == ["query", "--displays", "--display"]:
                return json.dumps({"index": 1, "frame": {"x": 0, "y": 0, "w": 768, "h": 1024}})
            if args[:3] == ["query", "--spaces", "--space"]:
                return json.dumps({"index": 1, "display": 1})
            if args[:2] == ["query", "--windows"]:
                return "[]"
            return ""

    box = Box()
    phone = f"updated={NOW}\nmobile_connected=1\nmobile_profile=iphone\n"
    controller = Controller(
        Yabai(runner=box.runner),
        support_dir=tmp_path,
        files=[(tmp_path / "mobile-session-state.env", phone)],
        now=lambda: NOW,
    )
    armed = controller.once(dry_run=False)
    assert armed["ok"] is True
    assert armed["padding"] == 202
    assert box.padding == 202

    normal = f"updated={NOW}\nmobile_connected=0\nmobile_profile=none\n"
    controller.files = [(tmp_path / "mobile-session-state.env", normal)]
    restored = controller.once(dry_run=False)
    assert restored["ok"] is True
    assert box.padding == 12


def test_rrf_prefers_a_row_that_appears_in_both_lists():
    fused = rrf(
        [
            [{"id": 2, "title": "b", "body": "b"}, {"id": 1, "title": "a", "body": "a"}],
            [{"id": 1, "title": "a", "body": "longer a"}],
        ],
        limit=2,
    )
    assert fused[0]["id"] == 1
    assert fused[0]["via"] == "hybrid"


def test_brain_ask_blocks_the_data_line_and_drops_secret_chunks(tmp_path: Path):
    assert query_block("what is the password") == "credentials"
    assert query_block("password reset runbook") is None
    assert query_block("finance approval") is None
    con = connect(tmp_path / "brain.sqlite")
    blocked = ask(con, "what is the password")
    assert blocked["blocked"] == "data_line"
    assert blocked["hits"] == []

    keep = add(con, "Desk", "shared note the runbook is on the desk")
    secret = add(con, "Vault", "shared note password: example-secret-value")
    hidden = add(con, "Counsel", "shared note the retainer is here")
    note_meta(con, keep, "notes/desk.txt", "note", "shared note the runbook is on the desk")
    note_meta(con, secret, "notes/vault.txt", "note", "shared note password: example-secret-value")
    note_meta(con, hidden, "notes/legal/retainer.txt", "note", "shared note the retainer is here")
    found = ask(con, "shared note", k=8)
    titles = [hit["title"] for hit in found["hits"]]
    assert "Desk" in titles
    assert "Vault" not in titles
    assert "Counsel" not in titles
    assert found["filtered"] >= 2
    assert all(hit["via"] == "hybrid" for hit in found["hits"])


def test_graph_rows_use_via_graph(tmp_path: Path):
    con = connect(tmp_path / "brain.sqlite")
    first = add(con, "Anchor", "uniqueanchorword lives here")
    second = add(con, "Neighbor", "a related page with other words")
    note_meta(con, first, "notes/anchor.txt", "note", "uniqueanchorword lives here")
    note_meta(con, second, "notes/neighbor.txt", "note", "a related page with other words")
    link(con, first, second, "related")
    found = ask(con, "uniqueanchorword", k=8, graph=True)
    vias = {hit["title"]: hit["via"] for hit in found["hits"]}
    assert vias["Anchor"] == "hybrid"
    assert vias["Neighbor"] == "graph"


def test_vectors_participate_in_hybrid_ask(tmp_path: Path):
    con = connect(tmp_path / "brain.sqlite")
    alpha = add(con, "Alpha", "color word")
    beta = add(con, "Beta", "color word")
    store_vector(con, alpha, "test", [1.0, 0.0])
    store_vector(con, beta, "test", [0.0, 1.0])
    found = ask(con, "color", k=2, query_vector=[1.0, 0.0])
    assert found["hits"]
    assert found["hits"][0]["via"] == "hybrid"
    assert found["hits"][0]["title"] == "Alpha"


def test_freshness_cycle_and_http_ask(tmp_path: Path):
    missing = snapshot(path=tmp_path / "missing.json", now=NOW)
    assert missing["state"] == "undecidable"
    assert is_trustworthy(missing) is False
    path = tmp_path / "verdict.json"
    write_verdict(path, degraded=False, problems=[], checked_n=2, total_n=2, now=NOW)
    assert snapshot(path=path, now=NOW + 10)["state"] == "green"
    write_verdict(path, degraded=True, problems=["embed down"], checked_n=2, total_n=2, now=NOW)
    assert snapshot(path=path, now=NOW + 10)["state"] == "degraded"
    assert snapshot(path=path, now=NOW + 5000)["state"] == "undecidable"

    seen = []

    def ingest():
        seen.append("ingest")
        return 1

    def embed():
        raise RuntimeError("down")

    def publish():
        seen.append("publish")
        return 1

    report = run_cycle({"ingest": ingest, "embed": embed, "publish": publish})
    assert report["ingest"]["ok"] is True
    assert report["embed"]["ok"] is False
    assert report["publish"]["ok"] is True
    assert seen == ["ingest", "publish"]

    refused = publish_manifest([{"title": "x", "text": "password: example-secret-value", "source": "a.txt"}], tmp_path / "out.json")
    assert refused["ok"] is False and refused["wrote"] is False
    kept = publish_manifest([{"title": "x", "text": "a public note", "source": "a.txt"}], tmp_path / "out.json")
    assert kept["ok"] is True
    assert parity(2, 2)["ok"] is True
    assert parity(2, 1)["ok"] is False
    assert probe_health(lambda: 200)["ok"] is True

    con = connect(tmp_path / "brain.sqlite")
    doc_id = add(con, "Gone", "remove me")
    assert drop_doc(con, doc_id) is True
    assert con.execute("select count(*) from docs").fetchone()[0] == 0

    server = serve("127.0.0.1", 0, con, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/ask?q=hello", timeout=2)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/ask",
            data=json.dumps({"q": "hello", "scope": "all", "k": 2}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["hits"] == []
    finally:
        server.shutdown()
    with pytest.raises(OSError):
        serve("0.0.0.0", 0, con)
    host = str(next(overlay_network().hosts()))
    assert bind_allowed(host, allow_overlay=True)
    assert bind_allowed(host, allow_overlay=False) is False


def test_curl_adds_socks_only_when_given():
    plain = curl_command("http://127.0.0.1:9", "hello")
    assert "--socks5-hostname" not in plain
    proxied = curl_command("http://127.0.0.1:9", "hello", socks="127.0.0.1:1")
    assert proxied[proxied.index("--socks5-hostname") + 1] == "127.0.0.1:1"
    assert socks_if_present(port=1, timeout=0.05) is None


def test_bridges_stay_draft_only_and_off_until_configured():
    assert enabled_specs({}) == []
    assert enabled_specs({"local_bridges": {"mail": {"enabled": True}}}) == []
    assert enabled_specs({"local_bridges": {"mail": {"enabled": True, "base_url": "https://mail.example.invalid"}}}) == []
    specs = enabled_specs({"local_bridges": {"mail": {"enabled": True, "base_url": "http://127.0.0.1:9222", "label": "com.hailports.mail", "pid": 4}}})
    assert len(specs) == 1 and specs[0].configured
    book = DraftBook()
    row = book.write("mail", "draft body")
    assert row["sent"] is False and row["status"] == "draft"
    with pytest.raises(DraftOnly):
        book.send(row["id"])
    assert recycle_allowed(established_clients=1, job_running=False, idle_observations=9) is False
    assert recycle_allowed(established_clients=0, job_running=True, idle_observations=9) is False
    assert recycle_allowed(established_clients=0, job_running=False, idle_observations=2) is True
    assert gc_allowed(flag_on=True, lock_free=False, composer_empty=True, footprint_mb=2000) is False

    class Mgr:
        def run(self, argv):
            self.argv = argv
            return "ran"

        def restart_command(self, label):
            return ["restart", label]

        def start_command(self, label):
            return ["start", label]

    assert heal(Mgr(), "com.hailports.mail", 4, 9, False) == "refused-pid-mismatch"


def test_cli_display_notes_and_agent_template(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert cli_main(["--config", cfg, "display", "list"]) == 0
    assert cli_main(["--config", cfg, "notes", "status"]) == 0
    assert cli_main(["--config", cfg, "display", "reflow", "--dry-run"]) == 0
    assert cli_main(["--config", cfg, "display", "add", "1400x900"]) == 0
    plist = tmp_path / "panel.plist"
    assert cli_main(["--config", cfg, "display", "setup", "--vendor", "0x610", "--product", "0xa049", "--mode", "1344x840", "--mode", "1024x768", "--out", str(plist)]) == 0
    parsed = parse_plist(plist.read_bytes())
    assert (1344, 840) in parsed["modes"]
    assert cli_main(["--config", cfg, "display", "watch", "--name", "macbook"]) == 0
    assert cli_main(["--config", cfg, "display", "watch", "--name", "unknown-box"]) == 0
    assert cli_main(["--config", cfg, "display", "apply", "--mode", "1680x1050"]) == 0
    assert cli_main(["--config", cfg, "display", "restore"]) == 0
    assert cli_main(["--config", cfg, "brain", "ask", "empty question"]) == 0
    assert cli_main(["--config", cfg, "display", "reflow", "--install-agent", "--dest", str(tmp_path / "agent")]) == 0
    text = (tmp_path / "agent" / "com.hailports.iphone-keyboard-reflow.plist").read_text(encoding="utf-8")
    assert "com.hailports.iphone-keyboard-reflow" in text
    assert "<integer>4</integer>" in text
    banned = "com." + "al" + "ex."
    assert banned not in text
    sample = render_agent(tmp_path, tmp_path / "keyboard_reflow.py", tmp_path / "session")
    assert "StartInterval" in sample
