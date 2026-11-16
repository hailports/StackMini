# Coop routing

The local model and a premium model can share a task. Three patterns are on by default. Each one fails open: a local error, a timeout, or a bad score leaves the older decision in place.

Set `COOP_MODE=0` to turn all three off. A config value `coop_mode: false` does the same when the environment variable is unset.

## How a call is decided

1. Cache. If the task is not high-stakes and `answer_cache_path` has an exact match that is less than seven days old, the stored answer is returned. `decision` is `complete_local`, `backend` is `answer_cache`, and the model is not called.
2. Unsure digs. High-stakes work never finishes locally. An unsure task that looks like a digest (status, summary, classify, extract, health, counts) can finish locally when a self-check passes. The self-check rejects empty text, customer-mail shape, code, secrets, and answers that drop the task's numbers. A Jev score of escalate rejects the answer. A Jev score of stop accepts it. When Jev is down or mid, only a number-heavy task (three or more numbers of two or more digits, and the answer keeps enough of them) is accepted. Anything else keeps today's skip.
3. Pre-compression. If the call is still escalating and the task is at least `coop_long_chars` (default 1500), the local model is asked for a short bullet list. The list replaces the body of the brief only when it is shorter than 75% of the task, keeps the numbers, and does not look like mail or code. `decision` stays `need_premium`. `backend` stays `skipped_stakes`. A bad compression keeps the original brief text.
4. Low-stakes work is unchanged. A finished local answer is stored in the cache when a path is configured.

People-facing text and code stay on the premium path. The local model may only supply a checked digest or a shorter brief.

## Receipts

Each receipt can include:

- `coop_pattern`: `cache`, `jev_gate`, `precompress`, `low_local`, or empty
- `premium_in_saved` and `premium_out_saved`: a four-characters-per-token estimate, or the server's completion-token count when that count exists
- `prompt_tokens` and `completion_tokens`: filled from `usage` or, when usage is empty, from server timings (`prompt_n`, `predicted_n`, `cache_n`)

## Patterns that were measured and not turned on

Premium-writes-a-plan then local expansion spent more premium output and missed when the plan did not carry the source text. Local draft plus a premium critique still spent premium tokens on drafts that were already good enough to accept. Sending a high-stakes extract back to the local model did not shorten the premium prompt on the sample.

## Replay harness

`contrib/local-first/replay/harness.py` scores the same gates on `sample_tasks.jsonl`. The sample is synthetic. It does not call a model unless you pass `--live-url`. Token counts in `--live-url` mode come from the server timings. Offline mode uses the four-character estimate.

## Hosted personal lane

`scripts/qwen4-max-run` is a separate capped hosted call to Qwen4-Max, used only after the local model, and only for personal tasks. Work and employer prompts are blocked. It is not part of the three coop patterns above, and it does not turn on paid inference for the local chat door.
