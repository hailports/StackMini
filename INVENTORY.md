# Inventory

This repo is a fresh write of a local model appliance. Private trees, databases, logs, mail, and records from any one workplace were not copied.

## Features

| Feature | This repo |
| --- | --- |
| Command-line chat | hailports/cli.py (`hailports chat`), hailports/router.py |
| Web chat on port 8080, model on 18080, interactive before batch | hailports/webchat.py |
| iMessage replies | hailports/imessage.py (macOS only) |
| Web fetch, search, and news | hailports/webtools.py |
| Ask versus do, runbook match | hailports/askdo.py, hailports/runbooks.py |
| Draft only from notes you supply, citation hold, newest-text scheduling confirm | hailports/drafter.py, hailports/features.py |
| Draft queue, one id per round, skip reason in the log, chat badge restored once | hailports/draft_runner.py, hailports/chat_badge.py |
| Story card from a short request | hailports/story.py |
| Sprint pack and focus blocks | hailports/sprint.py, hailports/calendar_blocker.py |
| Do not open a duplicate card | hailports/ticket_guard.py, hailports/plugins/tracker.py |
| Runbook index | hailports/runbooks.py, hailports/learning.py |
| Notes ingest and hybrid search | hailports/notes.py |
| Focus list | hailports/worknow.py |
| Health check, freshness, and restart | hailports/sentinel.py |
| Service file hash | hailports/integrity.py |
| Memory budget and exact-PID restart | hailports/memory.py, hailports/osapi.py |
| Correction memory | hailports/learning.py |
| Whole-thread chat memory | hailports/threads.py |
| Idle useful work, one deep note at a time with linked clips | hailports/idle.py |
| Open-card lap, one changed card, next step held for a go | hailports/openloop.py |
| Suggested card held until Approve and Post, Edit, or Dismiss | hailports/suggest.py, hailports/ticket_guard.py |
| Precomputed packet for a paid call | hailports/precompute.py, hailports/router.py |
| Login-broken alert | hailports/alerts.py |
| Vision input | hailports/images.py, hailports_setup, docs/MODEL.md |
| Embedding server | hailports_setup/models.py `embed_server_args`, hailports/notes.py |
| Zoom and Outlook web session watchdogs, cookie vault, post-launch restore (reference, not installed) | contrib/session_keepers/ |

## Services

The installer writes a fresh launchd, systemd, or Task Scheduler file from `hailports_setup/services.py`. No machine-specific service file is shipped. Each unit binds the model and the embed server to 127.0.0.1 and passes the config path you chose.

## Left out on purpose

Workplace tools stay off this repo. Optional plugs for a CRM, a tracker, mail, meetings, and a file library ship as stubs. Chat bridges for Telegram, Signal, and Matrix ship as stubs. They stay off until you set a token in the environment. `CHANGE_ME` in the example config is not a token.

Also left out:

- Note databases, snapshots, corpora, and embedding indexes
- Logs, ledgers, runtime JSON, caches, and backups
- Model weight files (download them yourself)
- Secret files, tokens, and browser profiles
- Anything from a legal or finance folder
- Synced drive copies

## Default ports

| Port | Role |
| --- | --- |
| 8080 | Web chat |
| 18080 | llama.cpp chat and vision |
| 8097 | Embedding server |
| 8078 | Notes service bind |
