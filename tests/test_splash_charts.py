"""The engine-switch charts render from the measured data file and the data stays internally consistent."""
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "docs/data/splash_2026-11-19.json").read_text())


def _mod():
    spec = importlib.util.spec_from_file_location("splash_charts", ROOT / "scripts/splash_charts.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_charts_render(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "OUT", tmp_path)
    from PIL import Image
    for p in (m.speed_chart(DATA), m.context_chart(DATA)):
        assert Image.open(p).size == (1600, 900)


def test_data_consistency():
    lt = DATA["live_traffic_same_day"]
    assert lt["after"]["median_decode"] > lt["before"]["median_decode"]
    assert DATA["after"]["context"] == 65536 and DATA["before"]["context"] == 16384
    assert all(r["prompt_tokens"] < DATA["after"]["context"] for r in DATA["context_sweep_after"])
    assert DATA["over_cap_failure_prompt_tokens"] > DATA["after"]["context"]
    assert len(DATA["vision_ttft_s"]["before"]) == len(DATA["vision_ttft_s"]["after"]) == 3
