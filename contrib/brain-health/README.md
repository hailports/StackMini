# Brain health, Qwen 4 Max judge

A launchd job that checks the brain and the local model three times a day (8 AM, noon and 8 PM) and writes `latest.md`.

1. `collect.py` (deployment-specific, not in this repo) prints one JSON snapshot: `services` (query, MCP and ask health with latency), `freshness` (newest row per source against a threshold), `jobs` (launchd ingest jobs: loaded, last exit, paused), `consumers` (gateway status and wiring flags) and `probes` (known-answer questions, ok or not).
2. `local_model.py` adds `local_model`: GET `/health` and `/v1/models` on the local model port. It only reads. It never restarts or kills anything.
3. `run_qwen4.py` scores the snapshot with fixed rules, then hands the snapshot and those findings to Qwen 4 Max through `contrib/hosted-lanes/hermes-qwen4`. The model has no tools and sees only the snapshot. It returns status, findings and suggested fixes as JSON.
4. The fixed rules win: if they find a hard failure, the report stays RED even when the model says GREEN.
5. Suggested kickstarts are listed, not run. `WBH_REMEDIATE=1` runs them for loaded, unpaused ingest jobs only. Model servers, protected labels (`protected_labels.txt`, one per line) and model ports are always refused.

If `hermes-qwen4` exits 3 (sign-in), 4 (guard) or 5 (upstream error or the free window ended), the report is still written from the fixed rules, with a line that says the Qwen layer was unavailable. There is no paid fallback.

Install: put these files and your `collect.py` in `~/claude-stack/tools/brain_health/`, copy `brain-health-qwen4.plist.example` to `~/Library/LaunchAgents/com.claude-stack.brain-health-qwen4.plist`, replace `__HOME__` with your home directory, then `launchctl bootstrap gui/$(id -u) <plist>`. Run once: `launchctl kickstart gui/$(id -u)/com.claude-stack.brain-health-qwen4`. Reports go to `~/claude-stack/data/reports/brain_health/` (`WBH_OUT`). Pause: `touch ~/claude-stack/data/runtime/BRAIN_HEALTH_OFF` (`WBH_OFF`). `WBH_LABEL` sets the label shown in the report.

The snapshot holds statuses, counts, latencies and job labels, not document text. It is still sent to Nous Portal; read `contrib/hosted-lanes/README.md` for the data terms.
