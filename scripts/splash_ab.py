#!/usr/bin/env python3
"""Fair Splash A/B bench. Local only. Goes through the chat-priority proxy (:8080) as a priority chat,
so background filler is held off during each measured completion.
Usage: splash_ab.py OUT.json TAG [long_tokens=60000] [long_runs=2]"""
import json, sys, time, urllib.request, zlib, struct, base64, random
U = "http://127.0.0.1:18080"; M = "Qwen3.6-35B-A3B"; H = {"Content-Type": "application/json"}
out_path, tag = sys.argv[1], sys.argv[2]
LT = int(sys.argv[3]) if len(sys.argv) > 3 else 60000
LR = int(sys.argv[4]) if len(sys.argv) > 4 else 2
out = {"tag": tag, "t": time.strftime("%F %T")}
def png(w=64, h=64, rgb=(220, 20, 20)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
def chat_once(messages, max_tokens, stream=True, timeout=1200):
    body = {"model": M, "messages": messages, "max_tokens": max_tokens, "temperature": 0, "stream": stream,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(U + "/v1/chat/completions", data=json.dumps(body).encode(), headers=H)
    t0 = time.time(); first = None; text = ""; usage = {}; n = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not stream:
                d = json.load(r); return {"text": (d["choices"][0]["message"]["content"] or "")[:60], "wall": round(time.time() - t0, 2)}
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]": continue
                d = json.loads(line[5:])
                if d.get("usage"): usage = d["usage"]
                for c in d.get("choices", []):
                    p = (c.get("delta") or {}).get("content") or ""
                    if p:
                        n += 1; text += p
                        if first is None: first = time.time()
    except Exception as e:
        return {"err": str(e)[:200]}
    t1 = time.time(); ct = usage.get("completion_tokens") or n
    dec = (ct - 1) / (t1 - first) if first and t1 > first and ct > 1 else None
    return {"ttft": round(first - t0, 2) if first else None, "decode_tps": round(dec, 2) if dec else None,
            "completion_tokens": ct, "prompt_tokens": usage.get("prompt_tokens"), "wall": round(t1 - t0, 2)}

def chat(messages, max_tokens, stream=True, timeout=1200):
    # Splash answers 503 while admission is memory-held; retry for up to 4 min and record retries
    t0 = time.time(); tries = 0
    while True:
        r = chat_once(messages, max_tokens, stream, timeout); tries += 1
        if "503" not in str(r.get("err", "")) or time.time() - t0 > 240:
            r["tries"] = tries; return r
        time.sleep(5)

def longp(seed):
    rnd = random.Random(seed); words = "harbor ledger tide storm keeper lantern ship gull reef fog bell anchor rope chart compass".split()
    sents = []
    while len(sents) * 13 < LT * 0.98:
        sents.append(" ".join(rnd.choice(words) for _ in range(10)).capitalize() + ".")
    return f"Ledger {seed}.\n\n" + " ".join(sents) + "\n\nNow write a 300-word story about the keeper."
short = "Write a clear 400-word explanation of how a mixture-of-experts language model routes tokens."
# warm-up: grow the expert store / caches before measuring (not recorded as results)
out["warmup"] = [chat([{"role": "user", "content": short + " (warm-up)"}], 300),
                 chat([{"role": "user", "content": longp(f"warm-{tag}")[:40000]}], 64)]
out["pong"] = chat([{"role": "user", "content": "Reply with the single word: pong"}], 8, stream=False)
img = "data:image/png;base64," + base64.b64encode(png()).decode()
out["image"] = chat([{"role": "user", "content": [{"type": "text", "text": "What single color fills this image? Answer with one word."},
                                                 {"type": "image_url", "image_url": {"url": img}}]}], 8, stream=False)
out["short"] = [chat([{"role": "user", "content": short}], 400) for _ in range(3)]
code_src = """class Inventory:
    # Tracks items, quantities and unit prices for a small shop.
    def __init__(self):
        self.items = {}
    def add_item(self, name, qty, price):
        if qty <= 0 or price < 0:
            raise ValueError("bad qty or price")
        cur = self.items.get(name, (0, price))
        self.items[name] = (cur[0] + qty, price)
    def remove_item(self, name, qty):
        if name not in self.items:
            raise KeyError(name)
        have, price = self.items[name]
        if qty > have:
            raise ValueError("not enough stock")
        if qty == have:
            del self.items[name]
        else:
            self.items[name] = (have - qty, price)
    def get_quantity(self, name):
        return self.items.get(name, (0, 0))[0]
    def total_value(self):
        return sum(q * p for q, p in self.items.values())
    def low_stock(self, threshold=5):
        return sorted(n for n, (q, _) in self.items.items() if q < threshold)
"""
code_q = ("Here is a Python class:\n\n" + code_src * 4 +
          "\nWrite a complete pytest test module for the Inventory class: at least 25 small test functions covering every method, "
          "edge cases and exceptions. Output only the Python code, no explanation.")
out["code"] = [chat([{"role": "user", "content": code_q}], 1000) for _ in range(2)]
out["long"] = [chat([{"role": "user", "content": longp(f"{tag}-{i}")}], 300) for i in range(LR)]
med = lambda v: sorted(v)[len(v) // 2] if v else None
out["short_median"] = med([x["decode_tps"] for x in out["short"] if x.get("decode_tps")])
out["long_decode_mean"] = round(sum(x["decode_tps"] for x in out["long"] if x.get("decode_tps")) / max(1, len([x for x in out["long"] if x.get("decode_tps")])), 2)
out["long_ttft_mean"] = round(sum(x["ttft"] for x in out["long"] if x.get("ttft")) / max(1, len([x for x in out["long"] if x.get("ttft")])), 1)
out["code_decode_mean"] = round(sum(x["decode_tps"] for x in out["code"] if x.get("decode_tps")) / max(1, len([x for x in out["code"] if x.get("decode_tps")])), 2)
pp = [x["prompt_tokens"] / x["ttft"] for x in out["long"] if x.get("ttft") and x.get("prompt_tokens")]
out["long_pp_tps_mean"] = round(sum(pp) / len(pp), 1) if pp else None
json.dump(out, open(out_path, "w"), indent=1)
print(json.dumps({k: out[k] for k in ("tag", "short_median", "code_decode_mean", "long_decode_mean", "long_ttft_mean", "long_pp_tps_mean")}), json.dumps({"pong": out["pong"], "image": out["image"], "short": out["short"], "long": out["long"]})[:900])
