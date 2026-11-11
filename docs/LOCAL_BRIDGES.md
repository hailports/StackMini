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

## Setup

`hailports setup` asks for native apps or these bridges. Native records Mail, Notes, and Messages on macOS, and Outlook and OneNote on Windows, when the app is installed. It does not open them. Linux has no native app in that step.

The bridge choice calls the provider and key already configured. The prompt is the operating system, the bridge names, and the ports. Mail, notes, and chats are not sent. A URL that is not loopback or an overlay address is dropped. `--connect skip` leaves both paths off. `--yes` skips this unless `--connect` is set.

## Account sessions

`--connect accounts` is not the loopback bridge table. You pass accounts as `name=https://host/login`. Each account gets one Edge user-data directory and one loopback debug port, starting at 9340. The window is headful. On a 1440x900 screen the first one sits at 1020,580 in a 420x320 frame, which is the lower-right corner. The next windows step 20px up and left and stay on screen.

Microsoft Edge is not in this repository. Setup looks for the installed binary. When it is missing, the install line is `brew install --cask microsoft-edge` on macOS, `winget install --id Microsoft.Edge` on Windows, or `state/edge-install.sh` for Debian and Fedora. That script is the Microsoft apt or dnf repo plus `microsoft-edge-stable`. Setup runs the package manager only when you confirm and pass `--apply-install`. `--yes` and CI do not install Edge and do not open a window. `--open-accounts` opens the windows. You sign in yourself. Setup does not type a password.

The model selected in setup writes `read_list` and `write_fill`. `send` is forced false. `hailports accounts tick` takes page text from `--page`. It returns ingest, draft, or none. A send op is dropped. Script and a secret page are dropped before the model is called. Model-written JavaScript is not executed. A draft row uses channel `account`, never a mail or meetings channel, and `sent` stays false.

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
