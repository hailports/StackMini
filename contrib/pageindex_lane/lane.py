"""PageIndex side lane, local-only. Usage:
  lane.py ping
  lane.py index <pdf>            -> prints {"doc_id":...}
  lane.py ask <doc_id> <question>
Env: LANE_BASE (OpenAI-compatible base, loopback only), LANE_MODEL, LANE_STORE.
"""
import json, os, sys, time, urllib.request
from urllib.parse import urlparse

# drop any path entry whose top-level "agents" package would shadow openai-agents
sys.path[:] = [p for p in sys.path if "site-packages" in p
               or not os.path.isfile(os.path.join(p or ".", "agents", "__init__.py"))]

BASE = os.environ.get("LANE_BASE", "http://127.0.0.1:18990/v1")
MODEL = os.environ.get("LANE_MODEL", "openai/local/Qwen3.6-35B-A3B")
STORE = os.path.expanduser(os.environ.get("LANE_STORE", "./.pageindex"))
assert urlparse(BASE).hostname in ("127.0.0.1", "localhost"), "LANE_BASE must be loopback"
os.environ.pop("PAGEINDEX_API_KEY", None)
os.environ.pop("PAGEINDEX_SEARCH_API_KEY", None)
os.environ.update({
    "OPENAI_API_KEY": "local", "OPENAI_BASE_URL": BASE, "OPENAI_API_BASE": BASE,
    "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_TELEMETRY": "False",
    "OPENAI_AGENTS_DISABLE_TRACING": "1",
    # fail closed on any non-loopback HTTP(S) egress
    "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
})

def stats():
    root = BASE.rsplit("/v1", 1)[0]
    try:
        return json.load(urllib.request.urlopen(root + "/__stats", timeout=5))["calls"]
    except Exception:
        return None

def client():
    from pageindex import PageIndexClient
    be = {"api_base": BASE, "api_key": "local"}
    return PageIndexClient(
        index={"model": MODEL, "storage_path": STORE, "summary_concurrency": 1,
               "optimize": "merge", "backend": be},
        chat_model=MODEL,
        chat_backend={"api_key": "local", "base_url": BASE},
    )

def main():
    cmd = sys.argv[1]
    c0, t0 = stats(), time.time()
    out = {}
    if cmd == "ping":
        import litellm
        r = litellm.completion(model=MODEL, api_base=BASE, api_key="local",
                               messages=[{"role": "user", "content": "Reply with one word: pong"}], max_tokens=8)
        out["reply"] = r.choices[0].message.content
    elif cmd == "index":
        r = client().submit_document(sys.argv[2], wait=True)
        out = {"doc_id": r["doc_id"], "name": r.get("name"), "usage": r.get("usage")}
    elif cmd == "ask":
        cl = client()
        ans = cl.chat(sys.argv[3], doc_id=sys.argv[2], citations=True, max_turns=12)
        out["answer"] = ans if isinstance(ans, str) else str(ans)
    out.update(sec=round(time.time() - t0, 1), model_calls=(stats() - c0) if c0 is not None else None)
    print(json.dumps(out, default=str))

if __name__ == "__main__":
    main()
