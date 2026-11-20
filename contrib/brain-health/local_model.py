#!/usr/bin/env python3
"""Read-only local model probe for brain health (qwen4 judge). GET only; never restarts anything."""
import json, time, urllib.request

def get(url, timeout=6):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace"), int((time.perf_counter() - t0) * 1000)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"[:160], int((time.perf_counter() - t0) * 1000)

def probe():
    out = {}
    st, body, ms = get("http://127.0.0.1:18080/health")
    out["shim_18080_health"] = {"ok": st == 200, "status": st, "ms": ms}
    st, body, ms = get("http://127.0.0.1:18080/v1/models")
    ids = []
    try: ids = [m.get("id") for m in json.loads(body).get("data", [])][:4]
    except Exception: pass
    out["shim_18080_models"] = {"ok": st == 200 and bool(ids), "status": st, "ms": ms, "models": ids}
    return out

if __name__ == "__main__":
    print(json.dumps(probe(), indent=1))
