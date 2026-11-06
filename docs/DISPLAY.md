# Display

`hailports display` sizes a desktop for the client that is viewing it, then keeps windows on that canvas.

## Override

`setup` writes a scale-resolutions plist. Each logical `WxH` becomes three records:

- 1x points, flag 1, tail `0x200000`
- 2x framebuffer, flag 9, tail `0x200000`
- a bare 2x pair

Copy the file into `/Library/Displays/Contents/Resources/Overrides/` with administrator rights. Log out, or unplug and reconnect the display, so the modes show up. Do not kill the window server.

A dummy HDMI plug is often vendor `0x843` product `0x104`. A built-in panel override in the field used vendor `0x610` product `0xa049` with 1344x840, 1344x1008, and 1024x768. Those are examples. Pass the ids `displayplacer` or `ioreg` prints for the panel in front of you.

## Catalog

Built-in logical sizes cover 16:10, 16:9, and 4:3, including 1440x900, 1680x1050, 1344x840, 1344x1008, and 1024x768. iPad rows are native pixels divided by two. The 13-inch class panel is 2752x2064, so the desktop is 1376x1032. `add` remembers a custom `WxH` on this machine.

## Client match

| Name | Desktop |
| --- | --- |
| iPad | 1376x1032 |
| iPhone | 768x1024 (1024x768 rotated) |
| Mac | 1680x1050, 16:10 |
| anything else | no change |

Tailscale is optional. `watch` does not run `tailscale whois` unless you pass `--whois-cmd`. You can pass `--whois` with JSON you already have. A hostname, VNC peer, reverse DNS, mDNS, Screens, or Luna name is enough on its own.

`apply` records the size and fits any window frames you pass so they sit on the new canvas. It does not change the live desktop unless you pass `--live`, and even then it refuses until a screen id is available. `restore` swaps back to the previous recorded size. That swap is also a record, not a live mode change.

## Phone keyboard

`hailports display reflow` reads a session file:

```
updated=UNIX
mobile_connected=1
mobile_profile=iphone
```

The same shape is what a Remote4by3 controller writes at `~/Library/Application Support/Remote4by3/mobile-session-state.env`. A hailports install reads `~/Library/Application Support/hailports/mobile-session-state.env`. `REMOTE_SESSION_STATE_FILE` overrides both. The freshest file inside 45 seconds wins. Older, future-dated, or broken files do nothing.

The focused display must be 768x1024. The controller then sets yabai's global `bottom_padding` to 202 (usable bottom 963 minus the window bottom 761). The keyboard row starts near y=852, which leaves 91 points. Floating windows it moved are clamped to that bottom and restored later. A positive non-phone state (`mobile_connected=0`, or an iPad profile) puts the old global padding back. Per-space padding is not used, because yabai keeps a per-space flag it cannot clear.

`--dry-run` prints the math and does not call yabai to set anything. `--install-agent` writes a launch agent labeled `com.hailports.iphone-keyboard-reflow` with a 4 second interval. It does not load the agent.

yabai has to be installed and permitted. Until it is, the reflow stays quiet.
