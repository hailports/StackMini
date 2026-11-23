import json, sys, os
sys.argv = [sys.argv[0]] + sys.argv[1:]
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "splash_ab.py")).read()
# reuse helpers, run only warm short + pong + image + short x3 + code x2 (no 60K runs)
head = src.split('# warm-up:')[0]
g = {"__name__": "quick"}
sys.argv = ["x", sys.argv[1], sys.argv[2], "1000", "0"]
exec(head, g)
chat = g["chat"]; out = g["out"]; short = g["short"]
exec(src[src.index('code_src = """'):src.index('out["code"] =')], g)
out["warmup"] = [chat([{"role": "user", "content": short + " (warm-up)"}], 300)]
out["pong"] = chat([{"role": "user", "content": "Reply with the single word: pong"}], 8, stream=False)
out["short"] = [chat([{"role": "user", "content": short}], 400) for _ in range(3)]
out["code"] = [chat([{"role": "user", "content": g["code_q"]}], 1000) for _ in range(2)]
med = lambda v: sorted(v)[len(v)//2] if v else None
out["short_median"] = med([x["decode_tps"] for x in out["short"] if x.get("decode_tps")])
cd = [x["decode_tps"] for x in out["code"] if x.get("decode_tps")]
out["code_decode_mean"] = round(sum(cd)/len(cd), 2) if cd else None
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(json.dumps({k: out[k] for k in ("tag","short_median","code_decode_mean")}), json.dumps(out["pong"]))
