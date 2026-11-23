#!/bin/bash
# kickstart Splash, prove health + pong + image, print downtime
J=${SPLASH_KICK_LOGDIR:-$HOME/claude-stack/logs}; U=$(id -u)
T0=$(date +%s)
launchctl kickstart -k gui/$U/com.llm-appliance.mlx-qwen
for i in $(seq 1 90); do
  r=$(curl -s -m20 http://127.0.0.1:18080/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"Qwen3.6-35B-A3B","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}')
  echo "$r" | grep -qi pong && break; sleep 2; done
D=$(( $(date +%s)-T0 ))
IMG=$(/opt/homebrew/bin/python3 - <<'PY'
import base64,zlib,struct,json,urllib.request
def png(w=64,h=64,rgb=(220,20,20)):
    raw=b"".join(b"\x00"+bytes(rgb)*w for _ in range(h))
    c=lambda t,d: struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+c(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))+c(b"IDAT",zlib.compress(raw))+c(b"IEND",b"")
img="data:image/png;base64,"+base64.b64encode(png()).decode()
b={"model":"Qwen3.6-35B-A3B","max_tokens":8,"temperature":0,"chat_template_kwargs":{"enable_thinking":False},
   "messages":[{"role":"user","content":[{"type":"text","text":"What single color fills this image? Answer with one word."},{"type":"image_url","image_url":{"url":img}}]}]}
r=urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18080/v1/chat/completions",data=json.dumps(b).encode(),headers={"Content-Type":"application/json"}),timeout=120)
print(json.load(r)["choices"][0]["message"]["content"].strip())
PY
)
P=$(lsof -nP -tiTCP:18085 -sTCP:LISTEN|head -1)
echo "$(TZ=America/Chicago date '+%F %T %Z') kick_verify down=${D}s pong=$(echo "$r" | grep -qi pong && echo ok || echo FAIL) image=$IMG health=$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/health) engine=$(ps -o command= -p ${P:-1} | grep -oE '(Cellar/splash|opt/splash-)[0-9./]+' | head -1) $(grep -E 'Routed experts' $HOME/claude-stack/logs/splash-server.log | tail -1 | grep -oE '[0-9]+ slots for [0-9]+ experts \([0-9]+%\)')" | tee -a $J/splash-kicks.log
