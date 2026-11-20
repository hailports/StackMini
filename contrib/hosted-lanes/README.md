# Hosted lanes

A hosted lane sends the prompt to a model that runs somewhere else. None of them is a default. A caller uses one by name, and the local model stays the default path.

## hermes-qwen4

Qwen4 Max through [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) and Nous Portal. Nous made `qwen/qwen4-max:free` free for five days starting 18 November 2026. Nous has not published a rate limit, a token cap, or an end time. After the window closes, the paid route is listed at $1.43 in and $4.28 out per million tokens.

Setup, once per machine:

```sh
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes auth add nous --type oauth   # device code: open the printed URL and enter the code
```

Use:

```sh
hermes-qwen4 "Explain this stack trace"     # one chat completion through `hermes proxy`
git diff | hermes-qwen4 -                    # prompt from stdin
hermes-qwen4 --agent "Write a test for x.py" # one-shot `hermes chat -q`, tools on
hermes-qwen4 --status                        # signed in? proxy up?
```

Raw mode starts `hermes proxy` on `127.0.0.1:8645` the first time it is needed and leaves it running. It does not install a login item.

Exit codes: `0` answer printed, `2` usage, `3` sign-in needed, `4` refused by the guard, `5` upstream error or the free window ended. A router should move to the next lane on `3` or `5`.

### Guard

`HERMES_QWEN_GUARD=secrets` is the default. It refuses credential shapes and private keys before anything leaves the machine. `gate` also runs an outside egress check named by `HERMES_QWEN_GATE` (`module:function`, called as `function(prompt=text)` and returning `(ok, reason)`), loaded from `HERMES_QWEN_GATE_PATH`. If that check cannot load, the prompt is refused. `off` sends everything.

### Data

Read the Nous Portal [Terms](https://portal.nousresearch.com/terms) (section 12.3) and [Privacy Policy](https://portal.nousresearch.com/privacy). By default, Nous may store prompts and outputs, train on them, and share them with model providers and partners. Turning on Privacy Mode in the Portal account stops Nous from storing and training on inference payloads. It does not change what the upstream model host does under its own terms.

Receipts go to `~/.hermes-qwen4/receipts.jsonl`. They hold a hash, the size, the outcome, and token counts, never the prompt text.
