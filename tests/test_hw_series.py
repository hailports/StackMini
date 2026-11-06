"""Parser and hour-alignment checks for the hardware sampler. No live host."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

p = Path(__file__).resolve().parents[1] / "scripts" / "hw_series_sample.py"
spec = importlib.util.spec_from_file_location("hw_series_sample", p)
hs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hs)

SYSCTL = """vm.swapusage: total = 3072.00M  used = 1905.75M  free = 1166.25M  (encrypted)
iogpu.wired_limit_mb: 23040
vm.memory_pressure: 0
kern.memorystatus_level: 10
hw.pagesize: 16384
"""

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4852.
Pages throttled:                                   0.
Pages wired down:                            1213627.
Pages occupied by compressor:                 178479.
"""

MEM = """The system has 25769803776 (1572864 pages with a page size of 16384).
System-wide memory free percentage: 9%
"""

PM = """Note: No thermal warning level has been recorded
Note: No performance warning level has been recorded
Note: No CPU power status has been recorded
"""

METRICS = """# HELP llamacpp:prompt_tokens_cached_total cached
llamacpp:prompt_tokens_cached_total 307720
llamacpp:n_tokens_max 16384
llamacpp:requests_processing 1
llamacpp:requests_deferred 0
llamacpp:prompt_tokens_total 796811
"""


def test_sysctl_swap_and_pressure():
    got = hs.parse_sysctl(SYSCTL)
    assert got["swap_used_mb"] == 1905.75
    assert got["swap_total_mb"] == 3072.0
    assert got["wired_limit_mb"] == 23040
    assert got["vm_memory_pressure"] == 0
    assert got["memorystatus_level"] == 10
    assert got["page_bytes"] == 16384


def test_vm_stat_wired_mb():
    got = hs.parse_vm_stat(VM)
    assert got["wired_mb"] == round(1213627 * 16384 / 1048576, 1)
    assert got["pages_throttled"] == 0
    assert got["free_mb"] == round(4852 * 16384 / 1048576, 1)


def test_memory_pressure_pct():
    assert hs.parse_memory_pressure(MEM)["mem_free_pct"] == 9


def test_pmset_none_recorded():
    got = hs.parse_pmset(PM)
    assert got["pmset_thermal_warning"] == "none_recorded"
    assert got["pmset_perf_warning"] == "none_recorded"


def test_metrics_have_no_kv_size():
    got = hs.parse_metrics(METRICS)
    assert "kv_cache_tokens" not in got
    assert got["prompt_tokens_cached_total"] == 307720
    assert got["n_tokens_max"] == 16384


def test_metrics_keep_a_real_kv_gauge():
    text = METRICS + "llamacpp:kv_cache_tokens 4321\n"
    assert hs.parse_metrics(text)["kv_cache_tokens"] == 4321


def test_row_leaves_temps_null_and_omits_fake_kv():
    now = datetime.fromisoformat("2026-11-06T02:15:00-06:00")
    row = hs.build_row({
        "sysctl": hs.parse_sysctl(SYSCTL),
        "vm": hs.parse_vm_stat(VM),
        "mem": hs.parse_memory_pressure(MEM),
        "pm": hs.parse_pmset(PM),
        "metrics": hs.parse_metrics(METRICS),
        "therm": {"cpu_c": None, "gpu_c": None, "ane_c": None},
        "pid": 30734,
        "lstart": "2026-11-06T05:43:07-06:00",
        "rss_mb": 16306.9,
    }, now=now, sample_ms=120)
    assert row["ts"] == "2026-11-06T02:15:00-06:00"
    assert row["cpu_c"] is None and row["gpu_c"] is None and row["ane_c"] is None
    assert "kv_cache_tokens" not in row
    assert "kv_cache_tokens" in row["unavailable"]
    assert row["swap_used_mb"] == 1905.75
    assert row["vm_memory_pressure"] == 0
    assert row["pid"] == 30734
    assert hs._private(json.dumps(row)) is False


def test_hour_index_matches_chicago():
    assert hs.hour_of("2026-11-06T02:15:00-06:00") == ("2026-11-06", 2)
    # 07:15 UTC is 01:15 CST in November.
    assert hs.hour_of("2026-11-06T07:15:00+00:00") == ("2026-11-06", 1)


def test_summarize_does_not_invent_temp_or_kv_size():
    rows = []
    for minute in (0, 20, 40):
        rows.append({
            "ts": f"2026-11-06T02:{minute:02d}:00-06:00",
            "swap_used_mb": 1800 + minute,
            "swap_total_mb": 3072,
            "mem_free_pct": 9,
            "vm_memory_pressure": 0,
            "pages_throttled": 0,
            "wired_mb": 18900,
            "cpu_c": None,
            "gpu_c": None,
            "ane_c": None,
            "prompt_tokens_cached_total": 1000 + minute,
            "lstart": "2026-11-06T05:43:07-06:00",
            "unavailable": ["cpu_c", "gpu_c", "ane_c", "kv_cache_tokens"],
        })
    # A UTC stamp in the same Chicago hour must land in hour 2, not hour 8.
    rows.append({
        "ts": "2026-11-06T08:10:00+00:00",
        "swap_used_mb": 2000,
        "swap_total_mb": 3072,
        "mem_free_pct": 8,
        "vm_memory_pressure": 0,
        "pages_throttled": 0,
        "wired_mb": 19000,
        "cpu_c": None,
        "unavailable": ["kv_cache_tokens"],
    })
    out = hs.summarize_day(rows, "2026-11-06")
    assert out["hourly_temp"] == []
    assert out["hourly_kv"] == []
    assert "kv_cache_tokens" in out["unavailable"]
    # 1800, 1820, 1840, 2000 -> median 1830
    assert out["hourly_swap"] == [{"h": 2, "used_mb": 1830.0, "total_mb": 3072.0, "n": 4}]
    assert out["hourly_pressure"][0]["h"] == 2
    assert out["hourly_pressure"][0]["mem_free_pct"] == 9
    assert out["server_lstarts"] == ["2026-11-06T05:43:07-06:00"]
    assert out["n"] == 4


def test_append_rotates_and_refuses_private(tmp_path):
    path = tmp_path / "hw_series.jsonl"
    for i in range(8):
        hs.append_row(path, {"ts": f"2026-11-06T02:00:{i:02d}-06:00", "n": i}, max_lines=5)
    lines = path.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["n"] == 3
    assert json.loads(lines[-1])["n"] == 7
    bad = {"ts": "2026-11-06T02:00:00-06:00", "note": "/" + "Users" + "/someone"}
    with pytest.raises(RuntimeError):
        hs.append_row(path, bad, max_lines=5)
