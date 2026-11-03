# Session keepers

Reference scripts that keep a long-lived web session readable: Zoom web Team Chat and Outlook on the web, each open in a Chrome that is driven over the DevTools protocol (CDP). They do not restart the browser that holds the session.

They are not part of the `hailports` command and are not installed by `hailports setup`. Several of them import helpers from the operator's own stack (`stack.bridge_endpoints`, `tools.zoom_cdp_session`, `tools.zoom_web_send`, `tools.authlink`). Read them as working examples and wire in your own helpers.

## macOS

- `macos/zoom_session_watchdog.py` and `macos/outlook_session_watchdog.py` run from launchd every 180 seconds. Each tick reads the page over CDP and sorts it into ok, rows0 (signed in but no conversation rows), signed_out, sso_error, page_missing, or browser_down. The read is read-only.
- After three bad ticks in a row they try one fix inside the same tab: expand the collapsed chat section, reload the same tab, or restore the cookie jar on an endpoint that is not protected. If the next tick is still bad, they send one login-link alert for that break and one "back in" note when it recovers.
- `macos/zoom_session_vault.py` and `macos/owa_session_vault.py` save and restore the site cookies. Files are mode 0600 and the newest three are kept. Restore refuses a protected port.
- `macos/launchd/` holds example launchd agents. The label prefix is `com.example`. Change the paths to match your install.

## Linux

- `linux/ensure_chrome_cdp.sh` keeps the CDP Chrome alive. A live process is relaunched only after three probes in a row fail. A missing process is relaunched at once. Chrome runs with `--disable-http2`. When every host resolves to one egress address, HTTP/2 connection reuse sent cross-origin API calls to the wrong host and the chat list never loaded.
- `linux/zoom_post_launch.py` runs after a relaunch. It restores the Zoom and Outlook sign-in from the vault, keeps one app tab, closes duplicates, and expands the chat list. It is safe to run again and prints nothing when the session is healthy.
- `linux/ts_socks.py` is a small SOCKS helper for reaching the macOS host over a userspace tailnet proxy on 127.0.0.1 port 1055.

## Placeholders

Hostnames, users, and paths are placeholders: `mini.tailnet-host.example.invalid`, `box.tailnet-host.example.invalid`, the `operator` account, `/opt/hailports/keeper`, and `idp.corp.example.com`. Replace them with your own values in your copy. Do not commit real ones.

## Self-test

Each Python script has a `self-test` command that checks the page classifier without touching a browser:

```sh
python3 contrib/session_keepers/macos/zoom_session_watchdog.py self-test
python3 contrib/session_keepers/macos/outlook_session_watchdog.py self-test
python3 contrib/session_keepers/linux/zoom_post_launch.py self-test
```
