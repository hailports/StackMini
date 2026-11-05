# Parity

Each row is a feature from `INVENTORY.md` or from the parity request. Status is what the tests on this machine showed.

| Feature | Module | Test | Status |
| --- | --- | --- | --- |
| Command-line chat | `hailports/cli.py`, `hailports/router.py` | `tests/test_modes.py::test_three_modes_with_fakes` | pass |
| Whole-thread memory, summary, reset | `hailports/threads.py` | `tests/test_parity.py::test_thread_summary_and_reset`, `test_cli_thread_reset_and_idle` | pass |
| Web chat, interactive before batch | `hailports/webchat.py` | `tests/test_appliance.py::test_priority_gate`, `test_webchat_forwards`, `tests/test_modes.py::test_webchat_router_stats` | pass |
| Web chat thread and reset | `hailports/webchat.py` | `tests/test_parity.py::test_cli_thread_reset_and_idle` (page script) | pass |
| iMessage poll | `hailports/imessage.py` | `tests/test_appliance.py::test_imessage_guard` | pass on Linux as a macOS-only guard. Sending still needs a Mac. |
| iMessage reply text | `hailports/imessage.py` `compose` | `tests/test_parity.py::test_model_replies_are_not_canned_templates` | pass. The reply is model text. The Messages app is not polled on this box. |
| Web fetch | `hailports/webtools.py` | `tests/test_appliance.py::test_sanitize_and_plugins` | pass |
| Web search and news | `hailports/webtools.py` | `tests/test_parity.py::test_web_search_and_news` | pass against a local server. The search URL and the news URL come from config. |
| Ask / do, read then draft | `hailports/askdo.py` | `tests/test_parity.py::test_ask_do_reads_then_drafts`, `tests/test_appliance.py::test_ask_runbook_guard_story` | pass. `gather` does not write. `respond` is the model reply. |
| Grounded draft with citation hold | `hailports/drafter.py`, `hailports/features.py` | `tests/test_parity.py::test_citation_hold_and_model_draft`, `tests/test_appliance.py::test_draft_and_runner`, `tests/test_draft_skip.py` | pass. A line with no `[n]` is held and not returned as the draft. A time answer in the newest text is staged only from that text. |
| Draft queue | `hailports/draft_runner.py`, `hailports/chat_badge.py` | `tests/test_appliance.py::test_draft_and_runner`, `tests/test_draft_skip.py`, `tests/test_chat_badge.py` | pass. With a router, the queue uses the citation hold. A skip line has the reason and the subject or sender. The same message id is handled once per round. A chat badge is restored once, only after this runner opened an unread chat whose newest line is from the other person. A draft save leaves the badge alone. A chat that cannot be read while closed is skipped with a reason. |
| Story card | `hailports/story.py` | `tests/test_parity.py::test_model_replies_are_not_canned_templates`, `tests/test_appliance.py::test_ask_runbook_guard_story` | pass. `hailports story` asks the model. `story_from_request` remains the offline card shape. |
| Sprint packet and focus blocks | `hailports/sprint.py`, `hailports/calendar_blocker.py` | `tests/test_parity.py::test_sprint_packet_and_near_duplicate_guard`, `tests/test_appliance.py::test_ask_runbook_guard_story` | pass. The packet is the plan, the blocks, and an `.ics` body. |
| Duplicate card guard | `hailports/ticket_guard.py`, `hailports/plugins/tracker.py` | `tests/test_parity.py::test_sprint_packet_and_near_duplicate_guard` | pass. Exact titles and near-duplicate titles are refused. Nothing is posted to a remote tracker. |
| Runbook index | `hailports/runbooks.py` | `tests/test_appliance.py::test_ask_runbook_guard_story` | pass |
| Correction memory into the prompt and a runbook | `hailports/learning.py` | `tests/test_parity.py::test_learning_loop_updates_prompt_and_runbook` | pass |
| Notes ingest | `hailports/notes.py` | `tests/test_parity.py::test_ingest_connectors_and_embed_server` | pass for files, HTML, and a URL fetch. |
| Embeddings (bge-small server) | `hailports_setup/models.py`, `hailports/notes.py` | `tests/test_parity.py::test_ingest_connectors_and_embed_server` | pass for the server flags and the `/v1/embeddings` client. The GGUF is not in the repo. |
| Hybrid search | `hailports/notes.py` | `tests/test_parity.py::test_hybrid_search_prefers_the_closer_vector` | pass |
| Freshness and embed auto-heal | `hailports/notes.py`, `hailports/sentinel.py` | `tests/test_parity.py::test_freshness_and_embed_autoheal` | pass. A dead embed URL is restarted only through the service manager when the recorded process id matches. |
| Focus list | `hailports/worknow.py` | `tests/test_appliance.py::test_brain_learn_now`, `tests/test_parity.py::test_model_replies_are_not_canned_templates` | pass. `hailports now` asks the model when one is up, and prints the list when it is not. |
| Service-file hash | `hailports/integrity.py` | `tests/test_appliance.py::test_integrity` | pass |
| Memory budget and exact-PID restart | `hailports/memory.py`, `hailports/osapi.py` | `tests/test_appliance.py::test_exact_pid_restart`, `tests/test_parity.py::test_memory_watch_restarts_only_matching_pid` | pass |
| Idle useful work | `hailports/idle.py` | `tests/test_parity.py::test_idle_skips_when_busy_and_does_not_delete`, `tests/test_idle_deep.py` | pass. Interactive use skips the job. Jobs do not delete files. Deep fill writes one note and gathers the next item's clips during the call. |
| Open-card lap | `hailports/openloop.py` | `tests/test_openloop.py` | pass. Assigned, mentioned, and active cards stay in the lap. Unchanged cards are skipped. A full lap with no change falls through. The next step cites a related note and waits for a go. The first packet is not mailed. A later material change mails once per revision. |
| Suggested card with a click | `hailports/suggest.py`, `hailports/ticket_guard.py` | `tests/test_suggest.py` | pass. One ask is drafted from the source text. The same source, the same key, a near title, and a known card are not suggested again. Opening the note does not create a card. A test token cannot create one. Approve and Post is single use. Edit posts the edited fields. Cancel and Dismiss do not create. A search error fails closed. |
| Precompute / compress before a paid call | `hailports/precompute.py`, `hailports/router.py` | `tests/test_parity.py::test_precompute_packet_drops_the_long_tail`, `tests/test_modes.py::test_hybrid_compresses_before_cloud` | pass |
| Vision, including HEIC | `hailports/images.py`, `hailports/features.py` | `tests/test_parity.py::test_vision_png_and_heic_dependency`, `tests/test_modes.py::test_vision_hybrid_sends_description` | pass for PNG and for the HEIC error when pillow-heif is missing. A real HEIC file was not decoded on this box. |
| Stats line on every model reply | `hailports/stats.py` | `tests/test_modes.py::test_three_modes_with_fakes`, `tests/test_parity.py::test_model_replies_are_not_canned_templates` | pass |
| Login-broken alert on a private channel | `hailports/alerts.py` | `tests/test_parity.py::test_login_alert_stays_on_a_private_channel` | pass. Loopback POST plus a local outbox. A public URL is refused. |
| Three modes and the wizard | `hailports/wizard.py`, `hailports/router.py` | `tests/test_modes.py`, `tests/test_wizard_pexpect.py` | pass, including the interactive pexpect wizard |
| Optional plugs | `hailports/plugins/` | `tests/test_appliance.py::test_sanitize_and_plugins` | off until the token is in the environment. A placeholder in the config file does not turn a plug on. |
| Unsent draft attachment types, including a presentation | `hailports/plugins/mail.py` `draft_attach_plan` | `tests/test_draft_attach.py` | pass. A `.pptx` file is staged with the presentation content type. An unknown extension, an empty file, and a file over the size cap are refused. Nothing is sent. |
| Queue hold | `hailports/queue_hold.py` | `tests/test_queue_hold.py` | pass. Zero views and zero likes are not hot. Hours 17-22 with a waiting draft and 3 hours idle lower interest, viral, and clarity. The voice floor stays 8. |

`handle()` in `hailports/askdo.py` is still the deterministic classifier the older unit test calls. `hailports ask` does not print that text. It reads notes, then prints a model reply with the stats line.
