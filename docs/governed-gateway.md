# Governed tool gateway

One choke point for bot and CLI tool calls on the local-first loopback gateway (`:8790`).

## Endpoints

- `GET /health` — includes `gateway_mode` (`shadow` or `enforce`)
- `POST /v1/tools/check` — evaluate a tool call (also `/v1/tools/invoke`, check-only)

Request body:

```json
{
  "caller": "claude",
  "tool": "outlook.send",
  "target": "outlook",
  "action": "send",
  "text": "optional body to scan",
  "path": "optional path to scan",
  "surface": "optional employer surface id"
}
```

Response fields: `allowed`, `decision` (`allow` | `shadow_deny` | `deny`), `reasons`, `mode`.

## Policy

`data/runtime/gateway_policy.json` (see `contrib/local-first/gateway_policy.example.json`) — per-caller allow/deny lists for tools and target systems.

## Hard rules (everyone)

1. Hard data line: Legal/Finance paths, lawyer/accountant/bank mail, passwords, seed phrases, 2FA codes.
2. Outbound mail, chat, and Chatter are draft-only; send/post is denied.
3. Only `crm-bot` may make CRM mutations; prod mutations are denied by default.
4. AI-fingerprint text on employer / work surfaces is blocked.

## Shadow vs enforce

- Default: **shadow** — would-be denies are logged as `shadow_deny` but `allowed` stays true.
- Enforce: create `data/flags/gateway_enforce` (any file). Remove it to return to shadow.

## Audit

- Every decision: `data/runtime/gateway-audit.jsonl`
- Daily counts appended under `## gateway` in the offload rollup markdown.

## CLI

```sh
python3 -m hailports.governed_gateway check --caller claude --tool outlook.send --action send --json
python3 -m hailports.governed_gateway --selftest
```

## Restart

Only the gateway process / launchd label for local-first-gateway. Do not restart model servers.
