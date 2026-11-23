# Free ladder gateway

OpenAI-compatible `/v1/chat/completions` + `/v1/models` and Anthropic `/v1/messages` on port **8790**.

Climb order: local → Cerebras → Groq → Gemini Flash/Pro → OpenRouter `:free` only → Plugsky → GLM / personal Claude / personal Codex.

Keys: `~/.config/free-llm/keys.env` (chmod 600). Never commit keys.

```
export OPENAI_BASE_URL=http://127.0.0.1:8790/v1
# optional second bind: set FREE_LADDER_TS_IP to a private interface IP
```

Model `auto` = ladder routing. Pin with `cerebras/<model>`, `groq/<model>`, etc.
OpenRouter paid models are refused even if pinned.
