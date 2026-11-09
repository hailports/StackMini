# Security

HailPorts is a local tool. It does not phone home.

- The API key is read from `HAILPORTS_API_KEY` or the OS keychain. Setup refuses to write that key into the config file.
- Plug and bridge tokens are read from the environment. A `CHANGE_ME` value in the example config does not enable anything.
- Web chat, the model, and the embed server bind to 127.0.0.1 unless you set `HAILPORTS_ALLOW_LAN=1`.
- Binding to 127.0.0.1 does not protect a port that a tunnel publishes. The chat proxy and llama-server have no API key. If a tunnel (cloudflared, ngrok, Funnel) maps a public hostname to 127.0.0.1:8080 or :18080, anyone can use the model. The 2026-11-09 audit of the reference deployment found exactly that. Put an access policy in front of the hostname, or do not publish it. See `docs/REFERENCE_STATUS.md`.
- Page fetch allows public http and https only. Loopback, private, link-local, and shared-carrier addresses are refused, including after a redirect. The name is checked when the request starts. A DNS change after that check is not pinned to the first address, because pinning would break HTTPS certificates.
- Login alerts POST only to a loopback URL. A copy is always written to a local outbox.
- Service restart goes through launchd, systemd, or Task Scheduler, and only when the recorded process id still matches.
- Archives downloaded for the tiny end-to-end check are extracted only when every member stays inside the destination directory.

There is no telemetry.

To report a vulnerability, open a private security advisory on this GitHub repository. Do not include secrets or workplace data in the report.

On macOS, storing a key with the `security` tool passes the key as a process argument for that short call. Prefer `HAILPORTS_API_KEY` if other people can list processes on the machine.
