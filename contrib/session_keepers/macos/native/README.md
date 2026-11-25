# Native browser session maintenance on macOS

Run one tick against a dedicated, single-tab Edge or Chrome process launched directly by a user LaunchAgent. Use Python 3 with the `websocket-client` package. The keeper verifies that the launch job's process owns the IPv4 loopback listener before opening the tab.

```sh
python3 keep_session.py zoom --browser-job com.example.zoom-browser --port 19321
python3 keep_session.py outlook --browser-job com.example.mail-browser --port 19322
```

For Zoom single sign-on, add `--zoom-site example.zoom.us` with your own Zoom vanity hostname. To schedule checks, run the same command from a LaunchAgent every five minutes with absolute interpreter and script paths. Start the browser on the Zoom Team Chat or Outlook Mail home page. One keeper should own each dedicated browser.

The keeper prints a single status and exits successfully only for `ready`. A responsive app needs visible navigation and search controls to qualify. Empty chats and calendars are valid. A crashed or unreachable app root gets one same-tab reload. A blank tab must be observed twice before it can return to the app. A visible signed-out landing can start the provider's ordinary sign-in flow. An identity prompt prints `sign_in_pending` and waits for the person signing in. Recovery attempts are spaced at least ten minutes apart, except for returning from a Zoom portal opened by this keeper.

On a signed-out Outlook landing, the keeper follows a visible Microsoft sign-in link to the authorization endpoint. The browser carries the redirect parameters through the sign-in flow. If the page shows a sign-in control without a suitable link, the keeper reports `sign_in_pending`.

Multiple tabs, active work items and unrecognized pages are preserved. The keeper stores status and retry timing under `~/Library/Application Support/StackMini/native-session/`. Test the decision paths offline from this directory with `python3 -m unittest discover -s .`. Node enables the offline Outlook sign-in link test.
