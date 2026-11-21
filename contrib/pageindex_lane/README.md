# PageIndex side lane (local model only)

A trial harness for answering questions about long, structured documents (manuals,
specs, SOPs) with [PageIndex](https://pypi.org/project/pageindex/) tree-index
reasoning, and comparing that against an existing chunk-search retriever. Both
sides use the same local OpenAI-compatible model.

This is a side lane. It doesn't replace general search: mail, chat, tickets and CRM
records stay on chunk/hybrid search.

## Layout

```
<lane>/venv/            isolated venv: python3.12 -m venv venv && venv/bin/pip install -U pageindex
<lane>/bin/             these scripts
<private>/docs/         text PDFs you build (never commit)
<private>/indexes/      PageIndex local storage (never commit)
<private>/logs/         proxy_calls.jsonl, trial.log
```

## Pieces

| file | what it does |
|---|---|
| `count_proxy.py` | Loopback reverse proxy in front of the model server. It allows one upstream request at a time, counts calls (`GET /__stats`), refuses work when a hold flag exists or during an optional quiet window, and only accepts a loopback upstream. |
| `gate.sh` | macOS memory gate (free %, swap used, kernel pressure level, hold flag). The runner calls it before every index and every question. |
| `lane.py` | `ping` / `index <pdf>` / `ask <doc_id> <q>` against PageIndex in local mode. It refuses a non-loopback base, removes any `PAGEINDEX_*` key, blocks other HTTP(S) egress with a dead proxy, sets `summary_concurrency=1`, and turns off agent tracing. |
| `to_pdf.py` | docx (pandoc) or plain text → text PDF (weasyprint) with heading bookmarks, so Flash mode can use the embedded outline. No cloud OCR. |
| `run_trial.py` | Sequential A/B over a question file. Each row records the PageIndex answer, latency and model calls, plus the same for the retriever + one local answer call. You can re-run it: finished rows are skipped. |

## Pointing PageIndex at a local server

```python
from pageindex import PageIndexClient
be = {"api_base": "http://127.0.0.1:18990/v1", "api_key": "local"}
client = PageIndexClient(
    index={"model": "openai/<served-model-id>", "storage_path": "./indexes",
           "summary_concurrency": 1, "optimize": "merge", "backend": be},
    chat_model="openai/<served-model-id>",
    chat_backend={"api_key": "local", "base_url": "http://127.0.0.1:18990/v1"},
)
```

Gotchas we hit with pageindex 0.7.4:

- `index={...}` and the flat `index_backend=` can't be combined. Put the backend inside the dict as `"backend"`.
- The LiteLLM model string is `openai/` plus the id the server reports at `/v1/models`. If the served id already has a slash, keep it: `openai/local/<model>`.
- Chat uses the `openai-agents` package (`import agents`). A top-level `agents/` directory anywhere on `sys.path` shadows it. One example is a repo root added by a sitecustomize hook. `lane.py` strips those path entries before importing.
- The default `summary_concurrency` is 128. On a single local model, set it to 1.
- Set `LITELLM_LOCAL_MODEL_COST_MAP=True` so LiteLLM doesn't fetch its price map over the network.

## Run

```
UPSTREAM=http://127.0.0.1:<model-port> LOG=logs/proxy_calls.jsonl venv/bin/python bin/count_proxy.py &
venv/bin/python bin/run_trial.py --docs docs.json --questions questions.json \
  --out results.jsonl --retriever-cmd "<python> <your_retriever>.py"
```

The retriever command gets the question as argv and prints a JSON list of
`{"title","text"}` hits. It must not call the model itself. `run_trial.py` makes the
single answer call, so the call counts compare like for like.

Score answers by hand against the source section. Don't send document text to a
hosted model for scoring.

## Results

See `RESULTS.md`. It's a public-safe summary only, with no document content.
