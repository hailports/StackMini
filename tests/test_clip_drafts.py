"""Recent drafts history for the right-click menu. No network."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

from hailports.clip import drafts


def test_append_list_trim_and_redo(tmp_path, monkeypatch, capsys):
    path = tmp_path / "drafts.jsonl"
    monkeypatch.setenv("PAISTE_DRAFTS", str(path))
    for n in range(35):
        drafts.append(f"reply number {n}\nsecond line", hop="local", app="Mail")
    rows = drafts.load()
    assert len(rows) == drafts.KEEP
    assert rows[0]["text"].startswith("reply number 5")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    newest = drafts.recent()
    assert len(newest) == drafts.MENU
    assert newest[0]["text"].startswith("reply number 34")
    assert "\n" not in newest[0]["preview"] and newest[0]["app"] == "Mail" and newest[0]["hop"] == "local"
    assert len({r["id"] for r in rows}) == len(rows)

    assert drafts.cmd("drafts", SimpleNamespace()) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in listed] == [r["id"] for r in newest]

    pick = newest[3]
    assert drafts.cmd("redo", SimpleNamespace(id=pick["id"], copy=False)) == 2
    assert capsys.readouterr().out.strip() == "no scene"
    assert drafts.cmd("redo", SimpleNamespace(id="missing", copy=False)) == 2


def test_redo_drafts_again_from_the_stored_scene(tmp_path, monkeypatch, capsys):
    from hailports.clip import run as clip_run
    path = tmp_path / "drafts.jsonl"
    monkeypatch.setenv("PAISTE_DRAFTS", str(path))
    scene = {"app": "Slack", "text": "can you check the build?"}
    row = drafts.append("first reply", hop="claude", app="Slack", scene=scene)
    assert drafts.find(row["id"])["scene"] == scene
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    seen = {}

    def fake_draft(doc, base_url, model, copy, cfg=None):
        seen.update(doc=doc, base_url=base_url, model=model, copy=copy, cfg=cfg)
        drafts.append("second reply", hop="claude", app=doc.get("app", ""), scene=doc)
        print("second reply")
        return 0

    monkeypatch.setattr(clip_run, "draft_scene", fake_draft)
    cfg = {"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "m"}, "paiste": {"order": []}}
    assert drafts.cmd("redo", SimpleNamespace(id=row["id"], copy=True), cfg) == 0
    assert capsys.readouterr().out.strip() == "second reply"
    assert seen == {"doc": scene, "base_url": "http://127.0.0.1:9/v1", "model": "m", "copy": True, "cfg": cfg}
    assert [r["text"] for r in drafts.recent()] == ["second reply", "first reply"]

    assert drafts.cmd("drafts", SimpleNamespace()) == 0
    listed = json.loads(capsys.readouterr().out)
    assert all(set(item) == set(drafts.LISTED) for item in listed)
    assert "check the build" not in json.dumps(listed)


def test_scene_is_capped(tmp_path):
    path = tmp_path / "d.jsonl"
    big = {"app": "Mail", "text": "a" * 50000 + "THE ASK", "rows": ["b" * 9000] * 5}
    row = drafts.append("ok", path=path, scene=big)
    kept = row["scene"]
    assert len(json.dumps(kept)) <= drafts.SCENE_CAP
    assert kept["app"] == "Mail" and kept["text"].endswith("THE ASK")
    assert drafts.append("ok", path=path, scene="not a dict").get("scene") is None


def test_empty_text_and_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "d.jsonl"
    assert drafts.append("   ", path=path) is None
    path.write_text("not json\n{\"id\": \"a\", \"text\": \"hi\"}\n", encoding="utf-8")
    assert [r["id"] for r in drafts.load(path)] == ["a"]
    long = drafts.append("x" * 200, path=path)
    assert len(long["preview"]) <= 80
    assert drafts.find("a", path)["text"] == "hi"


def test_scene_with_secret_is_dropped(tmp_path):
    path = tmp_path / "drafts.jsonl"
    row = drafts.append("sure, will do", path=path,
                        scene={"app": "Mail", "blocks": [{"text": "your verification code is 482913"}]})
    assert "scene" not in row and "scene" not in drafts.load(path)[0]
    row = drafts.append("ok", path=path, scene={"app": "Mail", "blocks": [{"text": "api_key=sk-abc123456789xyz"}]})
    assert "scene" not in row
    row = drafts.append("ok", path=path, scene={"app": "Mail", "blocks": [{"text": "lunch at noon?"}]})
    assert row["scene"]["app"] == "Mail"


def test_old_scenes_expire_but_text_stays(tmp_path):
    from datetime import datetime, timedelta, timezone
    path = tmp_path / "drafts.jsonl"
    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    new_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    path.write_text(
        json.dumps({"id": "old", "at": old_at, "text": "old reply", "scene": {"app": "Mail"}}) + "\n"
        + json.dumps({"id": "new", "at": new_at, "text": "new reply", "scene": {"app": "Slack"}}) + "\n",
        encoding="utf-8")
    rows = drafts.load(path)
    assert rows[0]["text"] == "old reply" and "scene" not in rows[0]
    assert rows[1]["scene"] == {"app": "Slack"}
    drafts.append("latest", path=path, scene={"app": "Mail"})
    stored = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert "scene" not in stored[0] and stored[0]["text"] == "old reply"
    assert "old reply" not in json.dumps(stored[0].get("scene", ""))
