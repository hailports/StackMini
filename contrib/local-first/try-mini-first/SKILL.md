# try-mini-first

Route cheap CLI work through the local model before a paid CLI.

## When

Use for summarize, classify, triage, extract, short rewrite, first draft, commit
message, and grep-explain. Never for building, multi-file edits, tool-heavy agent
loops, or social content.

## How

1. Keep `claude`, `codex`, and `cursor-agent` on PATH via `install-local-first-wrappers`.
2. On a laptop that is not the model host, set `LOCAL_FIRST_CONFIG` to a JSON file
   with `transport: ssh` (see `work-mac-router.example.json`) or point
   `LOCAL_FIRST_URL` at the Mini loopback tunnel.
3. Interactive TUIs pass through. Print/exec prompts are classified first.

## Fail-open

If `:8080` is down, the door escalates to the real paid binary with a short brief.
`LOCAL_FIRST_ROUTER=0` or `LOCAL_FIRST_DONE=1` skips the door entirely (required for
social lanes).
