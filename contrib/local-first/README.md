# Local-first CLI door

Low-stakes prompts finish on the local model. High-stakes work and unsure replies escalate with a short brief. The paid binary runs only after that decision.

## Install

From this repo or a stack checkout:

```sh
sh contrib/local-first/install-local-first-wrappers
```

The script links `grok`, `claude`, `codex`, and `cursor-agent` in `~/.local/bin` when it can find the real binary. It does not wrap `code` or `cursor`, because those names open the editor. It wraps `agent` only when that file is the Cursor agent launcher.

Run it again any time. It is safe on macOS and Linux.

Override a real binary with `GROK_REAL_BIN`, `CLAUDE_REAL_BIN`, `CODEX_REAL_BIN`, or `CURSOR_AGENT_REAL_BIN`.

`LOCAL_FIRST_ROUTER=0` skips the door. A prompt that is already a local-first brief is passed through.

Interactive sessions with no print / exec prompt are passed through. `claude -p`, `codex exec`, and `cursor-agent --print` are intercepted. JSON output modes are passed through so a script still sees the CLI's own event stream.

## Editor chat

Start the loopback door:

```sh
python3 hailports/local_first.py --openai-serve --port 8791
```

It listens on 127.0.0.1 only. Low-stakes chat can finish there. It cannot start a paid editor model. On a high-stakes turn the response is the brief plus a note to switch the model picker to a paid model.

VS Code Chat can add `contrib/local-first/local-first-vscode.example.json` as a custom chat-completions model (Chat: Manage Language Models, Add Models, Custom Endpoint). That does not replace Copilot inline completions.

Cursor's OpenAI base-URL override is not a reliable hook for a loopback door: current reports say those requests leave the machine. Use the `cursor-agent` PATH wrapper when the IDE or a script actually launches that CLI.
