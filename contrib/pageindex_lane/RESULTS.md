# Trial results (public-safe summary)

Setup: one Mac mini, one local 35B-A3B MoE model behind an OpenAI-compatible
server, every call serialized through `count_proxy.py`. 3 long internal documents:
a source-control/release SOP, a portal architecture guide, and a finance-process
design guide. Each was 9-12 text-PDF pages. 10 section/policy questions, each scored
by hand against the source section.

| | PageIndex (local, flash, merge) | chunk search (k=8) + 1 local answer |
|---|---|---|
| correct / partial / wrong | 10 / 0 / 0 | 4 / 2 / 4 |
| median latency | 14.9 s | 8.5 s |
| model calls per question | 3 (median), 3.2 mean | 1 |
| index cost | ~45 s, 9-12 calls per doc | (existing index) |

How the baseline went wrong: a chunk was truncated before the rule it needed (1),
an answer was pulled from a neighbouring document (2), it said "not in sources"
when the passage was there (1), and it missed part of a two-part answer (2).

Caveat: PageIndex was handed the right document, while the baseline searched the
whole corpus. Part of the gap is document routing. Before routing anything, add a
doc-picker step and re-test without that hint.

Recommendation: use it as an opt-in side lane for named long structured documents.
Don't replace general search with it.
