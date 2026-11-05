#!/usr/bin/env python3
"""mem_heal (2026-10-19, operator: "the stack is SUPPOSED to handle this itself").
Small reusable memory-pressure self-heal. Call heal() (or `mem_heal.py tick`) whenever
mem_pressure_2 / swap_high / free<floor trips. Lightest-first, all existing sanctioned tools:
  1. ram_warden.py live run   (its own allowlist + protect guard: OS purge, idle-model unload,
                               browser renice, defunct reap, tab caps; never a revenue service)
  2. zoom_reload_lever        (memheal2: pressure high AND Zoom renderer > 1400 MB AND no draft placement in
                               flight AND < 6 reloads/24h AND none in last 20 min -> zoom_tab_watch.py force
                               (in-place Page.reload; watcher's own 6/day cap + send/heavy locks still apply),
                               then verify staged Zoom drafts survived and re-queue any that dropped)
  3. If still unhealthy and only the model is left: ONE self-email to operator per pressure episode.
HARD: never signals llama-server :8080, never reboots, never touches swap_manager's llama restart
path or SWAP_ALERT (swap_manager.sh owns those). Single-flight lock + cooldown so the 3-min tick,
the morning brief and swap_manager/ram_warden callers never stack reclaims on each other.
If still unhealthy after reclaim and the model is the only big consumer, result says
needs_operator=True (only an :8080 restart / reboot would help; both operator-only)."""
import fcntl, json, os, re, signal, subprocess, sys, time
from pathlib import Path
HOME = Path.home()
D = HOME / "llm-appliance/runtime/memheal"; D.mkdir(parents=True, exist_ok=True)
LOG, STATE, LOCK = D / "heal.jsonl", D / "state.json", D / "heal.lock"
FLOOR_MB = 350          # free+speculative floor
SWAP_SOFT_MB = 2800     # comfortably under the 3 GB cap (swap_manager restarts llama at 2560 idle / 3584 busy)
COOLDOWN_S = 480        # min gap between real reclaims
VENV = str(HOME / "claude-stack/.venv/bin/python3")
RAM_WARDEN = HOME / "claude-stack/tools/ram_warden.py"
ZOOM_WATCH = HOME / "llm-appliance/runtime/zoom-tab-watch/zoom_tab_watch.py"
# memheal2 2026-10-19 (operator: machine applies these itself): pressure-gated forced Zoom-tab reload
ZW_STATE = HOME / "llm-appliance/runtime/zoom-tab-watch/state.json"
ZLOCK = HOME / ".zoom_web_send.lock"           # held by every Zoom client-driving call (draft placement)
ZOOM_CAP_MB, ZOOM_DAILY_CAP, ZOOM_BACKOFF_S = 1400, 12, 1800   # watcher's cap + 6/24h; our own 20-min backoff
NODE = next((c for c in ("/opt/homebrew/opt/node@22/bin/node", "/opt/homebrew/bin/node") if Path(c).is_file()), "node")
OWA_WRITE = HOME / "claude-stack/tools/owa_write.js"
SELF = "redacted-email"          # HARD: only recipient
EPISODE_CLEAR_S = 1800                          # healthy this long = pressure episode over

def zoom_renderer_mb():
    try:
        bp = _sh(["lsof", "-nP", "-iTCP:18812", "-sTCP:LISTEN", "-t"]).split()[0]
        best = 0.0
        for k in _sh(["pgrep", "-P", bp]).split():
            c = _sh(["ps", "-o", "command=", "-p", k])
            if "--type=renderer" not in c or "--extension-process" in c: continue
            out = _sh(["top", "-l", "1", "-pid", k, "-stats", "mem"]).split()[-1]
            mb = float(out[:-1]) * {"K": 1/1024, "M": 1, "G": 1024}[out[-1]] if out[-1] in "KMG" else float(out) / 1048576
            best = max(best, mb)
        return best
    except Exception:
        return 0.0

def zoom_draft_sids():
    """Read-only Zoom drafts list (same query the draft runner uses). None = unknown."""
    try:
        env = dict(os.environ, ZOOM_WEB_CDP="http://127.0.0.1:18812")
        r = subprocess.run([NODE, str(HOME / "claude-stack/tools/zoom_write.js"), "drafts"], capture_output=True,
                           text=True, timeout=45, env=env, cwd=str(HOME / "claude-stack"))
        d = json.loads(r.stdout or "{}")
        return {str(x.get("sessionId") or "") for x in d.get("data") or []} if d.get("ok") else None
    except Exception:
        return None

def _staged_zoom():
    try:
        sys.path.insert(0, str(Path(__file__).parent)); import ops_draft_runner as R
        return {k: v for k, v in R._load()["items"].items() if isinstance(v, dict) and k.startswith("zoom:") and v.get("state") == "staged"}
    except Exception:
        return {}

def _placement_in_flight():
    """True if any Zoom client call holds the send lock, or a Zoom draft item is mid-stage (pending/retry touched <3 min)."""
    try:
        fh = open(ZLOCK, "a+"); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    except BlockingIOError:
        return "zoom_send_lock_held"
    except Exception:
        pass
    try:
        sys.path.insert(0, str(Path(__file__).parent)); import ops_draft_runner as R
        for k, v in R._load()["items"].items():
            if isinstance(v, dict) and k.startswith("zoom:") and v.get("state") in ("pending", "retry", "receipt_redraft", "running") \
               and time.time() - float(v.get("updated") or 0) < 180:
                return f"draft_mid_stage:{k[:30]}"
    except Exception:
        return "queue_unreadable"
    return ""

def zoom_reload_lever(dry_run):
    """Pressure-gated forced reload of the Zoom tab via zoom_tab_watch.py force (its own 6/24h cap, send-lock
    and heavy-lane checks still apply). Then verify staged drafts survived; any that dropped -> re-stage."""
    mb = zoom_renderer_mb()
    if mb <= ZOOM_CAP_MB: return {"step": "zoom_reload", "skipped": f"renderer {mb:.0f}MB <= cap {ZOOM_CAP_MB}"}
    try: rl = [x for x in json.loads(ZW_STATE.read_text()).get("reloads", []) if time.time() - x["ts"] < 86400]
    except Exception: rl = []
    if len(rl) >= ZOOM_DAILY_CAP:
        nxt = time.strftime("%H:%M", time.localtime(min(x["ts"] for x in rl) + 86400))
        return {"step": "zoom_reload", "skipped": f"daily cap {len(rl)}/{ZOOM_DAILY_CAP} (next slot {nxt} CT)", "mb": round(mb)}
    if rl and time.time() - max(x["ts"] for x in rl) < ZOOM_BACKOFF_S:
        return {"step": "zoom_reload", "skipped": "backoff: reloaded <20 min ago", "mb": round(mb)}
    busy = _placement_in_flight()
    if busy: return {"step": "zoom_reload", "skipped": busy, "mb": round(mb)}
    staged = _staged_zoom()
    pre = zoom_draft_sids()
    r = subprocess.run([VENV, str(ZOOM_WATCH), "force"] + (["--dry-run"] if dry_run else []), capture_output=True, text=True, timeout=200)
    out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-2:]
    res = {"step": "zoom_reload", "rc": r.returncode, "mb_before": round(mb), "tail": out,
           "staged_present_before": sorted(v.get("name") or k for k, v in staged.items()
                                           if pre is not None and k.split(":", 1)[1] in pre)}
    if dry_run or not any("RELOADED" in l for l in out): return res
    time.sleep(20)
    post = zoom_draft_sids(); res["mb_after"] = round(zoom_renderer_mb())
    if pre is None or post is None:
        res["verify"] = "drafts list unreadable; draft runner's own lost-draft check will re-stage"; return res
    dropped = [k for k in staged if k.split(":", 1)[1] in pre and k.split(":", 1)[1] not in post]
    res["dropped"] = dropped
    if dropped:
        try:
            import ops_draft_runner as R
            with R._queue_locked():
                data = R._load()
                for k in dropped:
                    it = data["items"].get(k) or {}
                    if it.get("state") == "staged":   # same transition the runner uses for a lost composer draft
                        it.update(state="retry", next_at=0, error="composer_draft_lost", lost_at=time.time(),
                                  lost_reason="mem_heal_zoom_reload")
                R._save(data)
            R._log(f"mem_heal_zoom_reload_restage {dropped}")
            res["restage"] = "queued for re-stage"
        except Exception as e:
            res["restage"] = f"error {e}"
    return res

def notify_operator_once(st, res, dry_run):
    """DISABLED 2026-10-19 (operator): never email about memory. Self-heal stays silent;
    restarting :8080 / rebooting remain manual and operator-only, surfaced in the log only."""
    if st.get("episode_notified") or dry_run:
        return False
    st["episode_notified"] = time.time()
    _log({"ts": time.time(), "reason": "notify_suppressed",
          "note": "mem self-email disabled by operator 2026-10-19; log-only",
          "free_mb": (res.get("after") or {}).get("free_mb"),
          "swap_used_mb": (res.get("after") or {}).get("swap_used_mb")})
    return False

def _episode(st, unhealthy):
    now = time.time()
    if unhealthy:
        st.setdefault("episode_start", now); st.pop("healthy_since", None)
    else:
        st.setdefault("healthy_since", now)
        if now - st["healthy_since"] >= EPISODE_CLEAR_S:
            for k in ("episode_start", "episode_notified"): st.pop(k, None)

def _restarts_work_job(cmd) -> bool:
    """True when cmd kill/kickstarts a claude-stack or work-loop job."""
    text = " ".join(str(x) for x in (cmd or [])).lower()
    if "kickstart" not in text and not re.search(r"(^|\s)kill(\s|$)", text):
        return False
    return any(tok in text for tok in (
        "claude-stack", "work_loop", "work-loop", "work_context", "com.llm-appliance.work",
    ))


def _defer_writer(action: str) -> bool:
    """Skip-only. Never starts a restart; declines one while the writer is fresh."""
    try:
        guard_dir = HOME / "llm-appliance/runtime/swap-guard"
        if str(guard_dir) not in sys.path:
            sys.path.insert(0, str(guard_dir))
        from writer_lock_guard import writer_busy
        busy, info = writer_busy()
    except Exception:
        return False
    if not busy:
        return False
    pid = (info or {}).get("pid")
    _log({"ts": time.time(), "msg": f"DEFER {action}: work-context writer pid={pid} active"})
    return True


def _sh(cmd, timeout=10):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception: return ""

def snapshot():
    vm = _sh(["vm_stat"]); ps = int((re.search(r"page size of (\d+)", vm) or [0, 16384])[1])
    pg = lambda k: int((re.search(rf"Pages {k}:\s+(\d+)", vm) or [0, 0])[1])
    sw = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", _sh(["sysctl", "-n", "vm.swapusage"]))
    lv = _sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    top = []
    for ln in _sh(["ps", "-axo", "pid=,rss=,comm="]).splitlines():
        p = ln.split(None, 2)
        if len(p) == 3 and p[1].isdigit(): top.append({"pid": int(p[0]), "mb": int(p[1]) // 1024, "name": p[2].rsplit("/", 1)[-1][:40]})
    top = sorted(top, key=lambda t: -t["mb"])[:6]
    return {"ts": time.time(), "level": int(lv) if lv.isdigit() else -1,
            "free_mb": (pg("free") + pg("speculative")) * ps // 1048576,
            "swap_used_mb": float(sw[2]) if sw else -1.0, "swap_total_mb": float(sw[1]) if sw else -1.0, "top": top}

def problems(s):
    p = []
    if s["level"] != 1: p.append(f"mem_pressure_{s['level']}")
    if s["free_mb"] < FLOOR_MB: p.append(f"free_{s['free_mb']}MB<{FLOOR_MB}")
    if s["swap_used_mb"] < 0 or s["swap_used_mb"] > SWAP_SOFT_MB: p.append(f"swap_{s['swap_used_mb']:.0f}MB>{SWAP_SOFT_MB}")
    return p

def _log(rec):
    with LOG.open("a") as f: f.write(json.dumps(rec) + "\n")

# Match the real server. Health is on :8080; the process argv binds :18080.
# A miss used to return "just started" and skip every reclaim.
# Three plain patterns: macOS pgrep is basic regex and does not treat | as or.
_LLAMA_PATTERNS = (
    "llama-server.*--port 18080",
    "llama-server.*--port 8080",
    "llama-server.*MiniCPM",
)
_REBOOT = re.compile(r"(^|\s)(reboot|shutdown|halt)(\s|$)")


def _llama_pids() -> list:
    found = []
    for pat in _LLAMA_PATTERNS:
        for pid in _sh(["pgrep", "-f", pat]).split():
            if pid.isdigit() and pid not in found:
                found.append(pid)
    return found


def _etime_young(et: str) -> bool:
    """True only for a process younger than 3 minutes (mm:ss)."""
    et = (et or "").strip()
    if not re.fullmatch(r"\d\d:\d\d", et):
        return False
    return int(et.split(":")[0]) < 3


def _llama_young():
    """swap_manager may have just restarted the model server — don't pile a reclaim on a reload.

    Young only when every matched server is under 3 minutes. An old :18080 server is not young.
    No match still means unknown (a restart in progress), so the tick waits.
    """
    pids = _llama_pids()
    if not pids:
        return True
    return all(_etime_young(_sh(["ps", "-o", "etime=", "-p", pid]).strip()) for pid in pids)


# Regenerable jobs. Not the model, not the draft runner, not work brain, not the bridges.
_SAFE_KICK = (
    "com.operator.ministeps",
    "com.claude-stack.github-workbrain-sync",
    "com.claude-stack.idle-filler",
    "com.hailports.guides-server",
    "com.claude-stack.calendar-mirror",
    "com.claude-stack.naa-reply-ingest",
    "com.claude-stack.dealer-warranty-watch",
    "com.llm-appliance.reach",
    "com.llm-appliance.imsg-chat",
    "com.claude-stack.loadup-broker",
)
_SACRED_CMD = ("18812", "18822", "18820", "--port 18080", "--port 8080", "--port 8097", "llama-server")


def _launchd_pids() -> dict:
    found = {}
    for line in _sh(["launchctl", "list"]).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            found[parts[-1]] = int(parts[0])
    return found


def _footprint_mb(pid: int):
    try:
        raw = subprocess.run(
            ["footprint", "-p", str(pid), "--noCategories"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"phys_footprint:\s+([\d.]+)\s+([KMG])B", raw)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "G":
        return val * 1024
    if unit == "K":
        return val / 1024
    return val


def _etime_minutes(pid: int) -> float:
    et = _sh(["ps", "-o", "etime=", "-p", str(pid)]).strip()
    if not et:
        return 0
    days = 0
    if "-" in et:
        day, et = et.split("-", 1)
        days = int(day)
    parts = [int(x) for x in et.split(":") if x.isdigit()]
    if len(parts) == 3:
        mins = parts[0] * 60 + parts[1]
    elif len(parts) == 2:
        mins = parts[0]
    else:
        mins = 0
    return days * 1440 + mins


def _term_stale_telemetry(dry_run: bool) -> list:
    """operator CLI telemetry uploaders. Orphans, not a bridge and not the model."""
    actions = []
    marker = "plugin-telemetry/processes/upload.js"
    for line in _sh(["ps", "-axo", "pid=,ppid=,command="]).splitlines():
        bits = line.split(None, 2)
        if len(bits) < 3:
            continue
        pid, ppid, cmd = bits
        if ppid != "1" or marker not in cmd:
            continue
        if any(tok in cmd for tok in _SACRED_CMD):
            continue
        if _etime_minutes(int(pid)) < 30:
            continue
        if dry_run:
            actions.append({"step": "telemetry", "pid": int(pid), "dry": True})
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(1)
            if _sh(["ps", "-p", str(pid), "-o", "pid="]).strip():
                os.kill(int(pid), signal.SIGKILL)
                actions.append({"step": "telemetry", "pid": int(pid), "sig": "KILL"})
            else:
                actions.append({"step": "telemetry", "pid": int(pid), "sig": "TERM"})
        except ProcessLookupError:
            actions.append({"step": "telemetry", "pid": int(pid), "skipped": "gone"})
    return actions


def safe_reclaim(dry_run: bool = False) -> dict:
    """Kickstart allowlisted jobs until swap is under the line, or the list is done.

    One exact-PID SIGTERM is reserved for stale CLI telemetry uploaders.
    Never the model, the draft runner, work brain, or the Zoom/Outlook/ADO bridges.
    """
    actions = _term_stale_telemetry(dry_run)
    uid = os.getuid()
    labels = _launchd_pids()
    stopped = ""
    for label in _SAFE_KICK:
        swap = snapshot()["swap_used_mb"]
        if swap <= SWAP_SOFT_MB:
            stopped = "under_target"
            break
        pid = labels.get(label)
        if not pid or _etime_minutes(pid) < 30:
            continue
        cmd = _sh(["ps", "-p", str(pid), "-o", "command="])
        if any(tok in cmd for tok in _SACRED_CMD):
            actions.append({"step": "safe_reclaim", "label": label, "skipped": "sacred"})
            continue
        fp = _footprint_mb(pid)
        if fp is None or fp < 150:
            continue
        row = {"step": "safe_reclaim", "label": label, "pid": pid, "fp": round(fp)}
        if dry_run:
            row["dry"] = True
            actions.append(row)
            continue
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=40,
        )
        time.sleep(2)
        row["rc"] = proc.returncode
        row["swap_after"] = round(snapshot()["swap_used_mb"])
        actions.append(row)
    else:
        if not stopped:
            stopped = "allowlist_done"
    swap = snapshot()["swap_used_mb"]
    return {"step": "safe_reclaim", "stopped": stopped, "swap_mb": round(swap), "n": len(actions), "actions": actions}


def _refuses_model_or_reboot(cmd) -> bool:
    """The healer never restarts or kills the model server and never reboots."""
    text = " ".join(str(x) for x in (cmd or []))
    if _REBOOT.search(text):
        return True
    low = text.lower()
    if "ram_warden.py" in low:
        return False
    if "llama-server" in low or "--port 18080" in low or "--port 8080" in low:
        if re.search(r"(^|\s)(kill|killall|pkill)(\s|$)", low) or "kickstart" in low:
            return True
    return False

def heal(reason="", dry_run=False, force=False):
    before = snapshot(); probs = problems(before)
    res = {"reason": reason, "before": before, "problems": probs, "actions": [], "dry_run": dry_run}
    if not probs and not force:
        try:
            st0 = json.loads(STATE.read_text()) if STATE.exists() else {}
            _episode(st0, False); STATE.write_text(json.dumps(st0))
        except Exception: pass
        res.update(healthy=True, skipped="healthy"); return res
    lf = open(LOCK, "w")
    try: fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        res.update(healthy=False, skipped="another heal in flight"); return res
    try:
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
        if not force and time.time() - st.get("last_heal", 0) < COOLDOWN_S:
            res.update(healthy=False, skipped=f"cooldown {int(COOLDOWN_S - (time.time() - st['last_heal']))}s"); return res
        if _llama_young():
            res.update(healthy=False, skipped=":8080 just (re)started by its owner; waiting"); return res
        steps = [("ram_warden", ["/opt/homebrew/bin/python3", str(RAM_WARDEN)] + (["--dry-run"] if dry_run else []), 240)]
        # zoom tab: the standalone zoom-tab-watch job (5 min) keeps its own tick; here the pressure-gated
        # zoom_reload_lever (below) replaces a plain tick so every reload WE cause gets the staged-draft verify.
        for name, cmd, to in steps:
            if _refuses_model_or_reboot(cmd):
                res["actions"].append({"step": name, "skipped": "refuse_model_or_reboot"})
                continue
            if _restarts_work_job(cmd) and _defer_writer(name):
                res["actions"].append({"step": name, "skipped": "writer_lock"})
                continue
            t0 = time.time()
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=to)
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
                res["actions"].append({"step": name, "rc": r.returncode, "s": round(time.time() - t0, 1), "tail": tail})
            except Exception as e:
                res["actions"].append({"step": name, "error": str(e)[:200]})
        try: res["actions"].append(safe_reclaim(dry_run))
        except Exception as e: res["actions"].append({"step": "safe_reclaim", "error": str(e)[:200]})
        try: res["actions"].append(zoom_reload_lever(dry_run))
        except Exception as e: res["actions"].append({"step": "zoom_reload", "error": str(e)[:200]})
        _episode(st, True)
        if not dry_run:
            st["last_heal"] = time.time(); STATE.write_text(json.dumps(st))
        time.sleep(0 if dry_run else 20)
        after = snapshot(); left = problems(after)
        big = after["top"][0] if after["top"] else {}
        res.update(after=after, remaining=left, healthy=not left,
                   freed_mb=after["free_mb"] - before["free_mb"],
                   swap_delta_mb=round(after["swap_used_mb"] - before["swap_used_mb"], 1),
                   needs_operator=bool(left) and "llama" in big.get("name", "") and
                              sum(t["mb"] for t in after["top"][1:]) < 1500)
        # only after the safe levers ran and the model is the one left; long-lived (>=20 min) episodes only
        if res["needs_operator"] and time.time() - st.get("episode_start", time.time()) >= 1200:
            res["operator_notified"] = False; notify_operator_once(st, res, dry_run)  # log-only; never emails
            if not dry_run: STATE.write_text(json.dumps(st))
        return res
    finally:
        _log({k: v for k, v in res.items() if k not in ("before", "after")} |
             {"before": {k: res["before"][k] for k in ("level", "free_mb", "swap_used_mb")},
              "after": {k: res.get("after", {}).get(k) for k in ("level", "free_mb", "swap_used_mb")}})
        lf.close()

if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]; dry = "--dry-run" in a
    if a[0] == "status":
        s = snapshot(); print(json.dumps({**s, "problems": problems(s)}, indent=1))
    elif a[0] == "reclaim":
        print(json.dumps(safe_reclaim(dry), default=str))
    elif a[0] in ("tick", "heal"):
        r = heal(reason=a[0], dry_run=dry, force=(a[0] == "heal" and "--force" in a))
        print(json.dumps({k: v for k, v in r.items() if k not in ("before", "after")}, default=str))
