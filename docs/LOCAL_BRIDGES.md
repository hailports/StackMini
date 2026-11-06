# Local bridges

`hailports/bridges.py` is still the Telegram, Signal, and Matrix stubs. They stay off until an environment token is set.

`hailports/local_bridges/` is a different layer: loopback HTTP and browser CDP sessions.

| Name | Role |
| --- | --- |
| mail | Save a mail draft |
| meetings | Save a meetings draft |
| tracker | Save a tracker draft |
| cdp | A browser session reached over the DevTools protocol |

`enabled: false`, a missing URL, or a URL that is not loopback or an overlay address means the bridge is off. A public URL does not turn one on.

## What a tick does

- Probe `/health` or the CDP `/json/version` port you configured.
- Recycle an idle renderer only after several observations with no connected CDP client and with the owning job not running.
- A hot browser is watched, then reloaded, then the renderer is recycled, then the main process is bounced, and only when a bounce is allowed. A login, a held lock, or text sitting in a composer skips that.
- Garbage collection is separate. It never quits, reloads, or clicks.
- Restart goes through the service manager, and only when the recorded process id is still the live one. A different pid is left alone.

## Drafts

`DraftBook.write` stores a draft and marks `sent` false. `send` raises `DraftOnly`. There is no send path.

`contrib/session_keepers/` remains the longer example of cookie vaults and post-launch restore. Those scripts are not installed by setup. Point them at your own host names.

The same SOCKS-if-present helper used by the notes client can reach a bridge that is bound on another machine's overlay address. The tailscale command is not required.
