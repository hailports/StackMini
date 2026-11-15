# Schedule context for the local chat

When the user asks a schedule question ("what do I have going on this week", "am I traveling",
"meetings tomorrow"), the local chat answers from the real calendar first, then work items.

## Pieces

- `hailports/schedule_context.py`
  - `is_schedule_ask(text)` detects schedule intent. Topic questions ("what's going on with
    ticket 12345") are left to the normal work path.
  - `calendar_block(days)` reads a local snapshot JSON and renders a compact, dated block:
    travel / multi-day / out-of-office first, an on-site summary (venues listed on events inside a
    multi-day block), physical locations, then meetings by day. Meeting links are dropped.
  - `SYSTEM` is the answer prompt: lead with travel and where, then key meetings with start
    times, then work items. Only events in the block may be named.
  - `sanitize_location()` is for the sync job that writes the snapshot.
- A periodic sync job (every 5 minutes is fine) writes the snapshot. It is read-only toward the
  calendar. Shape: `{"sync_epoch": <unix>, "events": [{"subject","start","end","is_all_day",
  "cancelled","my_response","show_as","location"}]}` with UTC ISO times.

## Agent hook

Early in the chat turn, before the work-history path:

```python
import schedule_context as sc
if sc.is_schedule_ask(user_text):
    try:
        cal = sc.calendar_block()
    except Exception as exc:          # fail-open: one line, then the normal path
        print(f"(calendar unavailable: {type(exc).__name__}; answering from work notes only)")
    else:
        facts = cal + "\n\nWORK ITEMS (secondary):\n" + work_items_or("none pulled")
        return chat([{"role": "system", "content": sc.SYSTEM},
                     {"role": "user", "content": user_text + "\n\n" + facts}])
```

## Settings

`SCHEDULE_SNAPSHOT` (path), `SCHEDULE_TZ` (IANA zone, default machine local), `SCHEDULE_DAYS`
(default 10), `SCHEDULE_STALE_S` (default 6h), `SCHEDULE_CONTEXT=0` turns it off.

The reply label in the chat is `local` by default (`LLM_REPLY_LABEL` overrides), so a local
answer is never shown under a premium model's name.
