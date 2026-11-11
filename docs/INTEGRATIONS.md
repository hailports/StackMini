# Guided integrations

`hailports setup` can connect four surfaces: email, calendar, notes, and chats. Each surface is one of:

| Path | What it does |
| --- | --- |
| approved | An official API, or a folder of notes on disk. Uses the keys you supply. |
| native | Records the app installed on this computer. On macOS, sync reads Mail, Calendar, Notes, and Messages through AppleScript. |
| bridges | A loopback browser bridge. Your own model drafts a reviewable adapter. |
| skip | Leave that surface off. |

`--yes` leaves every surface off unless you pass `--connect`. `--connect` is one path for all four, or a per-surface list such as `notes=approved,email=skip`.

## Approved providers

You bring the client id and the secret. The repo does not ship one, and it does not proxy anyone else's key.

| Surface | Providers |
| --- | --- |
| email | `gmail`, `graph`, `imap`, `apple-mail` |
| calendar | `gcal`, `graph`, `caldav`, `apple-calendar` |
| notes | `files`, `graph` (OneNote), `carddav`, `apple-notes` |
| chats | `apple-messages` on macOS, or a loopback bridge |

Google is an OAuth device flow, or a loopback flow on `127.0.0.1`. Microsoft Graph is a device-code flow for mail, calendar, and OneNote. IMAP, CalDAV, and CardDAV take an app password for a host you name. `icloud` and `fastmail` are built-in host presets. `files` reads Markdown from a folder you pass with `--notes-dir`.

Tokens go to the OS keychain when it is available. Otherwise they go to a mode `0600` file under the config directory `integrations/tokens.json`. They are not written to `config.yaml` and they are not logged. A mask list (config `integrations.mask`, plus a private `integrations/mask.json`) is stripped out of text before it is indexed. Password assignments, one-time codes, and private-key blocks are stripped too. A path with a legal or finance segment is not indexed.

## What sync reads

Mail, calendar, and chats use `integrations.window_days` (default 30). Notes are read in full. The brain stores one row per external id. A later sync skips unchanged text and keeps a cursor (`historyId` for Gmail, a timestamp or delta link for the others).

```sh
hailports integrations connect notes --via approved --provider files --notes-dir ~/notes
hailports integrations sync
hailports integrations prove
hailports integrations disconnect email --purge
```

`connect` and `sync` print `proof <surface> pass` or `proof <surface> fail` after a brain search for a word from something just ingested. `hailports doctor` reports `integration-email`, `integration-calendar`, `integration-notes`, and `integration-chats`.

Setup can run that ingest itself with `--pull`. Without `--pull`, `--yes` only records the choice.

## Bridges

For an app with no approved API, the bridge path asks the model already configured in setup. The prompt is the system, the surface, and the port. The reply is kept only when every URL is loopback and the existing step cleaner accepts the lines. The adapter is written to `adapters/<surface>.json` so you can read it before anything runs. Sync reads `GET` on that loopback URL plus `/export` when a local helper is actually serving items. There is no send path.

## Disconnect

`hailports integrations disconnect <surface>` deletes that provider's token when no other surface still uses it, and deletes the adapter file. `--purge` also deletes the documents that surface added. Other notes stay.
