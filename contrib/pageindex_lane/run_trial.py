"""Sequential A/B: PageIndex tree reasoning vs an existing retriever + same local model.

  run_trial.py --docs docs.json --questions questions.json --out results.jsonl \
      --retriever-cmd "<cmd that prints JSON hits for a question on argv>"

docs.json: {"<doc_key>": "<path to text PDF>"}; questions.json: [{"id","doc","q","key"}...]
Every model call goes through the counting proxy (LANE_BASE). Before each index
and each question the gate script runs; a nonzero gate stops the run (resumable:
finished rows in --out are skipped).
"""
import argparse, json, os, shlex, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
BASE = os.environ.get("LANE_BASE", "http://127.0.0.1:18990/v1")
MODEL_RAW = os.environ.get("BASELINE_MODEL", "local/Qwen3.6-35B-A3B")

def gate():
    r = subprocess.run([str(HERE / "gate.sh")], capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()

def calls():
    return json.load(urllib.request.urlopen(BASE.rsplit("/v1", 1)[0] + "/__stats", timeout=5))["calls"]

def lane(*args):
    r = subprocess.run([PY, "-I", str(HERE / "lane.py"), *args], capture_output=True, text=True, timeout=1800)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (r.stderr or r.stdout)[-800:]}

def baseline(q, retriever_cmd):
    t0, c0 = time.time(), calls()
    r = subprocess.run(shlex.split(retriever_cmd) + [q], capture_output=True, text=True, timeout=300)
    hits = json.loads(r.stdout)
    t_ret = time.time() - t0
    ctx = "\n\n".join(f"[{i+1}] {h.get('title','')}\n{h.get('text','')}" for i, h in enumerate(hits))
    body = {"model": MODEL_RAW, "max_tokens": 600, "temperature": 0,
            "messages": [{"role": "system", "content": "Answer only from the numbered sources. Cite [n]. If the sources do not answer, say so."},
                         {"role": "user", "content": f"Sources:\n{ctx}\n\nQuestion: {q}"}]}
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "authorization": "Bearer local"})
    ans = json.load(urllib.request.urlopen(req, timeout=900))["choices"][0]["message"]["content"]
    return {"answer": ans, "sec": round(time.time() - t0, 1), "retrieval_sec": round(t_ret, 1),
            "model_calls": calls() - c0, "hit_titles": [h.get("title", "")[:80] for h in hits]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True); ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--retriever-cmd", required=True)
    ap.add_argument("--doc-ids", default=None, help="json map doc_key->doc_id (written/updated)")
    a = ap.parse_args()
    docs = json.load(open(a.docs)); qs = json.load(open(a.questions))
    idp = Path(a.doc_ids or Path(a.out).with_name("doc_ids.json"))
    ids = json.load(open(idp)) if idp.exists() else {}
    for k, path in docs.items():
        if k in ids:
            continue
        ok, g = gate(); print("gate", g, flush=True)
        if not ok:
            print("STOPPED before index", k); return 2
        r = lane("index", path); print("index", k, json.dumps(r)[:300], flush=True)
        if "doc_id" not in r:
            return 3
        ids[k] = r["doc_id"]; ids[k + "__meta"] = {"sec": r["sec"], "model_calls": r["model_calls"]}
        idp.write_text(json.dumps(ids, indent=1))
    done = set()
    if Path(a.out).exists():
        done = {json.loads(l)["id"] for l in open(a.out) if l.strip()}
    for q in qs:
        if q["id"] in done:
            continue
        ok, g = gate(); print("gate", g, flush=True)
        if not ok:
            print("STOPPED before", q["id"]); return 2
        pi = lane("ask", ids[q["doc"]], q["q"])
        ok, g = gate()
        if not ok:
            row = {"id": q["id"], "pageindex": pi, "baseline": {"skipped": g}}
        else:
            try:
                bl = baseline(q["q"], a.retriever_cmd)
            except Exception as e:
                bl = {"error": str(e)[:300]}
            row = {"id": q["id"], "pageindex": pi, "baseline": bl, "gate_after": g}
        with open(a.out, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(q["id"], "pi", pi.get("sec"), pi.get("model_calls"), "| bl", row["baseline"].get("sec"), row["baseline"].get("model_calls"), flush=True)
        if not ok:
            return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
