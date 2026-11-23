#!/usr/bin/env python3
"""Splash upgrade proof: health, completion, image, short + long decode. Local only. Usage: splash_bench.py OUT.json [long_tokens=60000]"""
import json, sys, time, urllib.request, zlib, struct, base64
U = "http://127.0.0.1:18080"; M = "Qwen3.6-35B-A3B"
out = {"t": time.strftime("%F %T")}
def get(path):
    try:
        r = urllib.request.urlopen(U + path, timeout=10); return r.status
    except Exception as e: return str(e)
def png(w=64, h=64, rgb=(220, 20, 20)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
def chat(messages, max_tokens, stream=True, timeout=900):
    body = {"model": M, "messages": messages, "max_tokens": max_tokens, "temperature": 0, "stream": stream,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(U + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time(); first = None; text = ""; usage = {}; n_chunks = 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if not stream:
            d = json.load(r); return {"text": d["choices"][0]["message"]["content"], "usage": d.get("usage"), "wall": time.time() - t0}
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]": continue
            d = json.loads(line[5:])
            if d.get("usage"): usage = d["usage"]
            for c in d.get("choices", []):
                piece = (c.get("delta") or {}).get("content") or ""
                if piece:
                    n_chunks += 1
                    if first is None: first = time.time()
                    text += piece
    t1 = time.time(); ct = usage.get("completion_tokens") or n_chunks
    dec = (ct - 1) / (t1 - first) if first and t1 > first and ct > 1 else None
    return {"ttft": round(first - t0, 2) if first else None, "decode_tps": round(dec, 2) if dec else None,
            "completion_tokens": ct, "prompt_tokens": usage.get("prompt_tokens"), "wall": round(t1 - t0, 2), "text": text[:120]}
out["health"] = get("/health"); out["models"] = get("/v1/models")
out["pong"] = chat([{"role": "user", "content": "Reply with the single word: pong"}], 8, stream=False)
img = "data:image/png;base64," + base64.b64encode(png()).decode()
try:
    out["image"] = chat([{"role": "user", "content": [{"type": "text", "text": "What single color fills this image? Answer with one word."},
                                                     {"type": "image_url", "image_url": {"url": img}}]}], 8, stream=False)
except Exception as e: out["image"] = {"err": str(e)}
short = "Write a clear 400-word explanation of how a mixture-of-experts language model routes tokens."
out["short"] = [chat([{"role": "user", "content": short}], 400) for _ in range(3)]
LT = int(sys.argv[2]) if len(sys.argv) > 2 else 60000
para = ("In the quiet harbor town, the lighthouse keeper logged every ship, every storm and every unusual tide in a thick ledger. ")
filler = para * int(LT / 24)
out["long"] = chat([{"role": "user", "content": "Here is a long ledger excerpt.\n\n" + filler + "\n\nNow write a 300-word story about the keeper."}], 300, timeout=1500)
for k in ("short",):
    v = [x["decode_tps"] for x in out[k] if x.get("decode_tps")]; out[k + "_median"] = sorted(v)[len(v) // 2] if v else None
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(json.dumps({k: out[k] for k in ("health", "models", "short_median")}), json.dumps({k: (out[k] if k != "long" else {kk: vv for kk, vv in out[k].items() if kk != "text"}) for k in ("pong", "image", "long")})[:900])
