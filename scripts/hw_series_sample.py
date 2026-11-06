#!/usr/bin/env python3
"""Interval hardware samples for the local model host.

Telemetry only. No inference, no server flag changes, no restarts.
One sample is a few local reads: sysctl, vm_stat, memory_pressure, pmset,
ps, and a short loopback GET of llama-server /metrics.

`ts` is America/Chicago with a numeric offset. The hour of that timestamp
(0-23) is the same hour index `hourly_decode` uses. Missing sensors stay
null. This file does not say why a decode rate moved.

The Mini log is ~/claude-stack/data/runtime/hw_series.jsonl. A daily
collector can call summarize_day() on those rows. This script does not
edit that collector.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
INTERVAL_S = 180
# 180s * 720 = 36h. Keep 108h, then drop the oldest lines.
MAX_LINES = 2160
LABEL = os.environ.get("HW_SERIES_LABEL", "com.llm-appliance.hw-series")
PROC = os.environ.get("HW_SERIES_PROC", "llama-server")
METRICS_URL = os.environ.get("HW_SERIES_METRICS_URL", "http://127.0.0.1:18080/metrics")

_USER_DIR = "/" + "Users" + "/"
_HOME_DIR = "/" + "home" + "/"
_PRIVATE = re.compile(
    _USER_DIR
    + "|"
    + _HOME_DIR
    + r"|@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b|AB#\d{3,}|ado-\d{3,}",
    re.I,
)
_SWAP = re.compile(r"total = ([\d.]+)([KMGT])\s+used = ([\d.]+)([KMGT])")
_SWAP_SCALE = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
_PRESSURE_WORD = {1: "normal", 2: "warn", 4: "urgent", 8: "critical"}
_METRIC = re.compile(r"^(llamacpp:[A-Za-z0-9_:]+)(?:\{[^}]*\})?\s+(\S+)")
_TEMP = re.compile(
    r"\b(CPU|GPU|ANE)\b[^0-9\n]{0,40}?([\d.]+)\s*C\b",
    re.I,
)


def log_path() -> Path:
    override = os.environ.get("HW_SERIES_LOG")
    if override:
        return Path(override)
    return Path.home() / "claude-stack/data/runtime/hw_series.jsonl"


def meta_path(path: Path | None = None) -> Path:
    return (path or log_path()).with_name("hw_series_meta.json")


def _run(argv: list[str], timeout: float) -> str:
    env = os.environ.copy()
    # ps lstart month names stay English even when the user locale is not.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout or ""


def _private(text: str) -> bool:
    return bool(_PRIVATE.search(text))


def parse_sysctl(text: str) -> dict:
    out: dict = {}
    m = _SWAP.search(text)
    if m:
        out["swap_total_mb"] = round(float(m.group(1)) * _SWAP_SCALE[m.group(2)], 2)
        out["swap_used_mb"] = round(float(m.group(3)) * _SWAP_SCALE[m.group(4)], 2)
    for key, name in (
        ("wired_limit_mb", "iogpu.wired_limit_mb"),
        ("vm_memory_pressure", "vm.memory_pressure"),
        ("memory_pressure_level", "kern.memorystatus_vm_pressure_level"),
        ("page_bytes", "hw.pagesize"),
    ):
        mm = re.search(rf"^{re.escape(name)}:\s*(\d+)", text, re.M)
        if mm:
            out[key] = int(mm.group(1))
    return out


def _pages(text: str, label: str) -> float | None:
    m = re.search(rf"{re.escape(label)}:\s+([\d.]+)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_vm_stat(text: str, page_bytes: int | None = None) -> dict:
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else (page_bytes or 0)
    out: dict = {}
    if page:
        for label, key in (
            ("Pages free", "free_mb"),
            ("Pages wired down", "wired_mb"),
            ("Pages occupied by compressor", "compressor_mb"),
        ):
            n = _pages(text, label)
            if n is not None:
                out[key] = round(n * page / 1048576, 1)
    th = _pages(text, "Pages throttled")
    if th is not None:
        out["pages_throttled"] = int(th)
    return out


def parse_memory_pressure(text: str) -> dict:
    out: dict = {}
    m = re.search(r"System-wide memory free percentage:\s+(\d+)%", text)
    if m:
        out["mem_free_pct"] = int(m.group(1))
    # The command can say the pressure state in words. Keep that word, not a guess.
    low = text.lower()
    for word in ("critical", "warning", "urgent", "normal"):
        if re.search(rf"\b{word}\b", low):
            out["memory_pressure_word"] = word
            break
    return out


def parse_pmset(text: str) -> dict:
    if not text.strip():
        return {}
    low = text.lower()
    out: dict = {}
    for phrase, key in (
        ("thermal warning level", "pmset_thermal_warning"),
        ("performance warning level", "pmset_perf_warning"),
        ("cpu power status", "pmset_cpu_power"),
    ):
        if f"no {phrase} has been recorded" in low:
            out[key] = "none_recorded"
        elif phrase.split()[0] in low:
            out[key] = "recorded"
    levels = {}
    for ln in text.splitlines():
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k, v = k.strip(), v.strip()
        if k and len(k) <= 40 and re.fullmatch(r"[A-Za-z0-9_ ]+", k) and len(v) <= 40:
            levels[k] = v
    if levels:
        out["pmset_levels"] = levels
    return out


def parse_metrics(text: str) -> dict:
    """Cache-size gauges only. Cumulative *_total counters are not a size."""
    out: dict = {}
    for ln in text.splitlines():
        if not ln or ln.startswith("#"):
            continue
        m = _METRIC.match(ln)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        if name.lower().endswith("_total"):
            continue
        try:
            num: int | float = float(raw) if "." in raw else int(float(raw))
        except ValueError:
            continue
        lname = name.lower()
        if not any(part in lname for part in ("kv_cache", "cache_usage", "prompt_cache")) and not lname.endswith("cache_tokens"):
            continue
        if "ratio" in lname:
            out["kv_cache_ratio"] = num
        else:
            out["kv_cache_tokens"] = num
    return out


def parse_powermetrics(text: str) -> dict:
    """Pull die temperatures if a one-shot powermetrics print already has them."""
    found: dict[str, float] = {}
    for m in _TEMP.finditer(text or ""):
        kind = m.group(1).lower()
        key = {"cpu": "cpu_c", "gpu": "gpu_c", "ane": "ane_c"}[kind]
        if key not in found:
            found[key] = float(m.group(2))
    return found


def _metrics_ok(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    return u.scheme == "http" and u.hostname == "127.0.0.1" and u.path == "/metrics"


def fetch_metrics(url: str = METRICS_URL, timeout: float = 2.0) -> str:
    if not _metrics_ok(url):
        return ""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(262144).decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, TimeoutError):
        return ""


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_therm(path: Path) -> dict:
    """Try one non-interactive powermetrics sample, then remember the result.

    A password prompt or a slow sample is recorded UNAVAILABLE and not retried.
    """
    blank = {"cpu_c": None, "gpu_c": None, "ane_c": None}
    meta = _read_meta(path)
    if meta.get("powermetrics") == "UNAVAILABLE":
        return blank
    if meta.get("powermetrics") != "ok":
        try:
            r = subprocess.run(
                ["sudo", "-n", "powermetrics", "--samplers", "smc", "-n", "1", "-i", "100"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            _write_meta(path, {"powermetrics": "UNAVAILABLE", "reason": "timeout"})
            return blank
        err = (r.stderr or "").lower()
        if r.returncode != 0 or "password" in err:
            why = "sudo_required" if "password" in err or r.returncode != 0 else "failed"
            _write_meta(path, {"powermetrics": "UNAVAILABLE", "reason": why})
            return blank
        parsed = parse_powermetrics(r.stdout or "")
        if not parsed:
            _write_meta(path, {"powermetrics": "UNAVAILABLE", "reason": "no_temp_fields"})
            return blank
        _write_meta(path, {"powermetrics": "ok"})
        return {**blank, **parsed}
    text = _run(["sudo", "-n", "powermetrics", "--samplers", "smc", "-n", "1", "-i", "100"], 2)
    return {**blank, **parse_powermetrics(text)}


def find_server(pattern: str) -> tuple[int | None, str | None, float | None]:
    text = _run(["pgrep", "-f", pattern], 2)
    me = os.getpid()
    pids = []
    for tok in text.split():
        if tok.isdigit() and int(tok) != me:
            pids.append(int(tok))
    if not pids:
        return None, None, None
    best, best_rss, best_lstart = None, -1.0, None
    for pid in pids:
        cmd = _run(["ps", "-o", "command=", "-p", str(pid)], 2)
        base = ""
        if cmd.strip():
            base = cmd.strip().split(" ", 1)[0].rsplit("/", 1)[-1]
        # A shell whose command line repeats the pattern is not the server.
        # The command text itself is never stored.
        if base in {"bash", "sh", "zsh", "ssh", "sshd"} or "hw_series_sample" in cmd:
            continue
        ps = _run(["ps", "-o", "rss=,lstart=", "-p", str(pid)], 2).strip()
        if not ps:
            continue
        rss_s, _, lstart = ps.partition(" ")
        try:
            rss_kb = float(rss_s)
        except ValueError:
            continue
        if rss_kb > best_rss:
            best, best_rss, best_lstart = pid, rss_kb, " ".join(lstart.split())
    if best is None:
        return None, None, None
    started = _lstart_ct(best_lstart or "")
    rss_mb = round(best_rss / 1024, 1)
    return best, started, rss_mb


def _lstart_ct(raw: str) -> str | None:
    raw = " ".join(raw.split())
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    host = datetime.now().astimezone().tzinfo
    return naive.replace(tzinfo=host).astimezone(CT).isoformat(timespec="seconds")


def _num(row: dict, key: str):
    v = row.get(key)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _median(vals: list[float]):
    if not vals:
        return None
    return round(statistics.median(vals), 1)


def hour_of(ts: str) -> tuple[str, int] | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    local = dt.astimezone(CT)
    return local.date().isoformat(), local.hour


def summarize_day(rows: list[dict], day: str | None = None) -> dict:
    """Hourly medians. `day` is YYYY-MM-DD in America/Chicago.

    Hour `h` is the Chicago hour of `ts`, the same index as hourly_decode.
    A null sensor stays null. The counter prompt_tokens_cached_total is not
    reported as a cache size.
    """
    buckets: dict[int, list[dict]] = {}
    lstarts = []
    unavailable = set()
    for row in rows:
        got = hour_of(str(row.get("ts") or ""))
        if not got:
            continue
        row_day, hour = got
        if day and row_day != day:
            continue
        buckets.setdefault(hour, []).append(row)
        if row.get("lstart") and row["lstart"] not in lstarts:
            lstarts.append(row["lstart"])
        for name in row.get("unavailable") or []:
            unavailable.add(str(name))
    swap, pressure, temp, kv = [], [], [], []
    for hour in sorted(buckets):
        group = buckets[hour]
        n = len(group)
        used = [_num(r, "swap_used_mb") for r in group]
        used = [v for v in used if v is not None]
        total = [_num(r, "swap_total_mb") for r in group]
        total = [v for v in total if v is not None]
        if used:
            swap.append({
                "h": hour,
                "used_mb": _median(used),
                "total_mb": _median(total) if total else None,
                "n": n,
            })
        free = [v for v in (_num(r, "mem_free_pct") for r in group) if v is not None]
        wired = [v for v in (_num(r, "wired_mb") for r in group) if v is not None]
        freemb = [v for v in (_num(r, "free_mb") for r in group) if v is not None]
        throttled = [v for v in (_num(r, "pages_throttled") for r in group) if v is not None]
        vmp = [v for v in (_num(r, "vm_memory_pressure") for r in group) if v is not None]
        words = [r.get("memory_pressure") for r in group if r.get("memory_pressure")]
        if free or wired or vmp or words:
            pressure.append({
                "h": hour,
                "memory_pressure": words[-1] if words else None,
                "mem_free_pct": _median(free) if free else None,
                "vm_memory_pressure": _median(vmp) if vmp else None,
                "pages_throttled_max": max(throttled) if throttled else None,
                "wired_mb": _median(wired) if wired else None,
                "free_mb": _median(freemb) if freemb else None,
                "n": n,
            })
        temps = {}
        any_temp = False
        for key in ("cpu_c", "gpu_c", "ane_c"):
            vals = [v for v in (_num(r, key) for r in group) if v is not None]
            temps[key] = _median(vals) if vals else None
            any_temp = any_temp or bool(vals)
        if any_temp:
            temp.append({"h": hour, **temps, "n": n})
        kv_vals = []
        for r in group:
            for key in ("kv_cache_tokens", "prompt_cache_tokens", "kv_cache_ratio"):
                v = _num(r, key)
                if v is not None:
                    kv_vals.append(v)
                    break
        if kv_vals:
            kv.append({"h": hour, "tokens": _median(kv_vals), "n": n})
    if not temp:
        unavailable.update(("cpu_c", "gpu_c", "ane_c"))
    if not kv:
        unavailable.add("kv_cache_tokens")
    return {
        "day": day,
        "n": sum(len(v) for v in buckets.values()),
        "hourly_temp": temp,
        "hourly_swap": swap,
        "hourly_pressure": pressure,
        "hourly_kv": kv,
        "server_lstarts": lstarts,
        "unavailable": sorted(unavailable),
    }


def build_row(parts: dict, now: datetime | None = None, sample_ms: int | None = None) -> dict:
    now = now or datetime.now(CT)
    therm = parts.get("therm") or {}
    sysctl = parts.get("sysctl") or {}
    vm = parts.get("vm") or {}
    mem = parts.get("mem") or {}
    pm = parts.get("pm") or {}
    metrics = parts.get("metrics") or {}
    unavailable = []
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "pid": parts.get("pid"),
        "lstart": parts.get("lstart"),
        "cpu_c": therm.get("cpu_c"),
        "gpu_c": therm.get("gpu_c"),
        "ane_c": therm.get("ane_c"),
        "swap_used_mb": sysctl.get("swap_used_mb"),
        "swap_total_mb": sysctl.get("swap_total_mb"),
        "wired_mb": vm.get("wired_mb"),
        "wired_limit_mb": sysctl.get("wired_limit_mb"),
        "free_mb": vm.get("free_mb"),
        "compressor_mb": vm.get("compressor_mb"),
        "pages_throttled": vm.get("pages_throttled"),
        "mem_free_pct": mem.get("mem_free_pct"),
        "memory_pressure_level": sysctl.get("memory_pressure_level"),
        "memory_pressure": _PRESSURE_WORD.get(sysctl.get("memory_pressure_level"))
        or mem.get("memory_pressure_word"),
        "vm_memory_pressure": sysctl.get("vm_memory_pressure"),
        "pmset_thermal_warning": pm.get("pmset_thermal_warning"),
        "pmset_perf_warning": pm.get("pmset_perf_warning"),
        "rss_mb": parts.get("rss_mb"),
        "sample_ms": sample_ms,
    }
    for key in ("kv_cache_tokens", "kv_cache_ratio"):
        if key in metrics:
            row[key] = metrics[key]
    if "kv_cache_tokens" not in row and "kv_cache_ratio" not in row:
        unavailable.append("kv_cache_tokens")
    for key in ("cpu_c", "gpu_c", "ane_c", "memory_pressure"):
        if row[key] is None:
            unavailable.append(key)
    for key in ("pid", "swap_used_mb", "wired_mb", "mem_free_pct", "pmset_thermal_warning"):
        if row.get(key) is None:
            unavailable.append(key)
    row["unavailable"] = unavailable
    return row


def sample(now: datetime | None = None) -> dict:
    t0 = time.monotonic()
    sysctl = parse_sysctl(_run(
        ["sysctl", "vm.swapusage", "iogpu.wired_limit_mb", "vm.memory_pressure",
         "kern.memorystatus_vm_pressure_level", "hw.pagesize"],
        2,
    ))
    vm = parse_vm_stat(_run(["vm_stat"], 2), sysctl.get("page_bytes"))
    mem = parse_memory_pressure(_run(["memory_pressure", "-Q"], 2))
    pm = parse_pmset(_run(["pmset", "-g", "therm"], 2))
    pid, lstart, rss_mb = find_server(PROC)
    metrics = parse_metrics(fetch_metrics())
    therm = read_therm(meta_path())
    ms = int((time.monotonic() - t0) * 1000)
    return build_row({
        "sysctl": sysctl, "vm": vm, "mem": mem, "pm": pm, "metrics": metrics,
        "therm": therm, "pid": pid, "lstart": lstart, "rss_mb": rss_mb,
    }, now=now, sample_ms=ms)


def append_row(path: Path, row: dict, max_lines: int = MAX_LINES) -> None:
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    if _private(line):
        raise RuntimeError("refusing a row that is not public-safe")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        size = path.stat().st_size
    except OSError:
        return
    # Production rows are hundreds of bytes, so a small file is still under the
    # 108h cap. Tests pass a small max_lines and always take the count path.
    if size < 250_000 and max_lines >= MAX_LINES and size <= 1_500_000:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines and size <= 1_500_000:
        return
    kept = lines[-max_lines:]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def sample_once(path: Path | None = None) -> dict:
    path = path or log_path()
    lock = _lock(path.with_name("hw_series.lock"))
    if lock is None:
        return {"skipped": "locked"}
    try:
        row = sample()
        append_row(path, row)
        return row
    finally:
        lock.close()


def _plist(script: Path, interval: int) -> dict:
    py = "/opt/homebrew/bin/python3"
    if not Path(py).is_file():
        py = sys.executable
    return {
        "Label": LABEL,
        "ProgramArguments": [py, str(script), "--once"],
        "EnvironmentVariables": {
            "HW_SERIES_PROC": PROC,
            "HW_SERIES_LABEL": LABEL,
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StartInterval": int(interval),
        "RunAtLoad": True,
        "Nice": 15,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
    }


def install(interval: int = INTERVAL_S) -> int:
    if sys.platform != "darwin":
        print("install is for macOS launchd")
        return 2
    home = Path.home()
    la = home / "Library/LaunchAgents"
    la.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    dest = la / f"{LABEL}.plist"
    if dest.exists():
        bak = dest.with_name(dest.name + ".bak-20261106-hwseries")
        if not bak.exists():
            dest.replace(bak)  # keep the previous plist once
        else:
            dest.unlink()
    with dest.open("wb") as fh:
        plistlib.dump(_plist(script, interval), fh, fmt=plistlib.FMT_XML)
    uid = os.getuid()
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", domain, str(dest)], capture_output=True, text=True, timeout=15)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(dest)], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        r = subprocess.run(["launchctl", "load", "-w", str(dest)], capture_output=True, text=True, timeout=15)
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        print("launchd install failed")
        return 1
    print(f"installed {LABEL} interval {interval}")
    return 0


def load_rows(path: Path) -> list[dict]:
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or _private(ln):
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ts"):
            rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    if "--install" in argv:
        return install(INTERVAL_S)
    if "--summarize" in argv:
        path = log_path()
        day = None
        if "--day" in argv:
            day = argv[argv.index("--day") + 1]
        print(json.dumps(summarize_day(load_rows(path), day), indent=1))
        return 0
    row = sample_once()
    # One short line. The JSONL is the record. Do not print paths.
    if row.get("skipped"):
        print("skipped locked")
        return 0
    print(f"ok ms={row.get('sample_ms')} pid={row.get('pid')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
