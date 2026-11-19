# Optional Jev classifier

Cheap yes/no, choice, or score judgments via TypeSafe System One (`jev-latest`). Fail-open: any error, timeout, or missing key returns `None` / prints `null` and exits 0. Kill switch: `JEV_DISABLE=1`.

This is for internal agent judgments only. Do not send legal or finance mail, lawyer or accountant or bank correspondence, or any text with passwords, seed phrases, or 2FA codes. Mask secrets. Do not post classifier output onto a customer surface.

## Key

First match wins:

1. `TYPESAFE_API_KEY`
2. `JEV_API_KEY`
3. `JEV_KEY_FILE`
4. `~/.config/jev/typesafe_api_key` (mode 0600)
5. `$CLAUDE_STACK_HOME/secrets/typesafe_api_key` (default `~/claude-stack`)

Never commit the key. Never log it.

## Call

```sh
python -m hailports.jev --state 'The word hello contains the letter e.' \
  --question 'has_e=noul:The text claims that the word hello contains the letter e'
```

Stdin JSON also works:

```sh
printf '%s\n' '{"state":"...","questions":{"has_e":{"type":"noul","instructions":"..."}}}' \
  | python -m hailports.jev
```

Python:

```python
from hailports.jev import classify, noul, choice, score
answers = classify(state, questions, caller="example")  # None on error
```

Logs go to `~/.local/state/jev/jev.jsonl` (question ids, ok/ms/err, caller, host). The state text and the key are not logged. Secrets in the state (passwords, tokens, one-time codes) are masked before the request. State is clipped to 4000 characters.

Treat only confident scores as decisive (`noul` >= 0.9 or <= 0.1). A mid score, a timeout, or `null` leaves the caller's existing behavior. Never block a task on this call.

`hailports.jev_gates` is the decision layer: local-answer quality, a pre-route check, a done-check, a risk check before a mutation, whether a pack answers a question, a draft gate, and noise triage. Draft staging fails closed only on a confident fail. Everything else fails open.
