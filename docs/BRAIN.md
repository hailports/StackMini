# Notes brain

`hailports notes` and `hailports brain` use the same note file as `hailports/notes.py`. Ingest, search, and status behave as before. `ask` is the cited lookup.

## Ask

In process:

```python
from hailports.brain.ask import ask
ask(connection, "where is the runbook", k=8, scope="all", graph=False)
```

HTTP, bound to loopback by default:

```
GET /health
GET /v1/ask?q=...&scope=all&k=8&graph=0
POST /v1/ask    {"q","scope","k","graph"}
```

Scopes ship as `notes`, `web`, `email`, `calendar`, `chats`, and `all`. A config map can replace them. `all` includes notes, files, web pages, mail, calendar events, chats, and contacts. `graph=1` adds up to two neighbors from a 2-hop walk of `brain_edge` (`via: graph`). The other hits are `via: hybrid`.

Connected mail, calendar, notes, and chats are indexed by `hailports integrations sync`. See `docs/INTEGRATIONS.md`.

Hybrid search runs a BM25 query and, when you pass a query vector, a cosine ranking. Reciprocal rank fusion (k=60) merges the lists so a row that ranks well in either list can surface, and a row in both lists ranks higher.

From another machine, `hailports.brain.client.curl_command` builds a curl line. Add `--socks5-hostname` only when a SOCKS proxy was passed in, or when `socks_if_present()` finds a listener on 127.0.0.1 port 1055. Overlay membership can be the access check when the server is bound only to an overlay address and `allow_overlay` is set. A bearer token in `HAILPORTS_BRAIN_TOKEN` is required when that variable is set. The tailscale command is not required.

A public address is refused.

## Data line

These questions are refused before any search: passwords, seed phrases, private keys, API keys, one-time codes, legal counsel, finance folders, bank, wire, payroll, and salary. A password-reset runbook is not the same as asking for a password. "Finance approval" is not a finance folder.

A hit is dropped when its path has a segment such as `legal` or `finance`, or when the chunk itself contains a password assignment, a seed phrase, a private key, or a live one-time code. Dropped hits increment `filtered` and are not returned.

## Cycle

`run_cycle` runs ingest, embed, probe, publish, drop, lookup, and parity. A step that raises is recorded and the next step still runs. `publish_manifest` will not write a file that contains a restricted path or a secret. `drop_doc` removes one row from the local note file only. `nightly_embed` recomputes vectors for notes older than the configured age. Freshness is `green`, `degraded`, or `undecidable`. A missing or old verdict is undecidable, which is not a pass.
