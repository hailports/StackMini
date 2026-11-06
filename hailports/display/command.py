"""`hailports display` commands. Nothing here changes a live resolution unless `--live` is passed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hailports.display.catalog import CATALOG, Mode, aspect_name, find_named, mode as make_mode
from hailports.display.client import lookup_whois, match_client
from hailports.display.keyboard_reflow import Controller, geometry, render_agent
from hailports.display.override import build_plist, install_hint, parse_mode, parse_plist
from hailports.display.windows import reflow


def add_parser(sub) -> None:
    disp = sub.add_parser("display", help="Display sizes, client match, and the phone keyboard inset.")
    inner = disp.add_subparsers(dest="display_cmd", required=True)

    setup = inner.add_parser("setup", help="Write a scale-resolutions override file.")
    setup.add_argument("--vendor", required=True, help="Display vendor id, for example 0x843.")
    setup.add_argument("--product", required=True, help="Display product id, for example 0x104.")
    setup.add_argument("--mode", action="append", required=True, help="Logical WxH. Repeat for more.")
    setup.add_argument("--name", default="HiDPI override")
    setup.add_argument("--out", default="", help="Where to write the plist. Prints a path when omitted.")

    inner.add_parser("status", help="Show the saved mode and the phone geometry.")
    inner.add_parser("list", help="List built-in sizes and any sizes you added.")

    apply_p = inner.add_parser("apply", help="Record a desktop size. Pass --live to call a display tool.")
    apply_p.add_argument("--mode", required=True)
    apply_p.add_argument("--live", action="store_true")
    apply_p.add_argument("--frames", default="", help="JSON list of windows to fit onto the new size.")

    add_p = inner.add_parser("add", help="Remember a custom WxH.")
    add_p.add_argument("mode")

    watch = inner.add_parser("watch", help="Match one client name. Does not call tailscale unless you pass a command.")
    watch.add_argument("--name", default="")
    watch.add_argument("--whois", default="", help="JSON object from a whois lookup you already ran.")
    watch.add_argument("--whois-cmd", default="", help="Optional command that prints whois JSON. Omit it to skip.")
    watch.add_argument("--rdns", default="")
    watch.add_argument("--mdns", default="")
    watch.add_argument("--screens", default="")
    watch.add_argument("--luna", default="")

    inner.add_parser("restore", help="Swap back to the previously recorded size.")

    reflow_p = inner.add_parser("reflow", help="One keyboard-reflow tick, or a dry run that only prints the math.")
    reflow_p.add_argument("--dry-run", action="store_true")
    reflow_p.add_argument("--status", action="store_true")
    reflow_p.add_argument("--install-agent", action="store_true")
    reflow_p.add_argument("--dest", default="", help="Directory for the agent file. Used with --install-agent.")
    reflow_p.add_argument("--label", default="com.hailports.iphone-keyboard-reflow")


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _custom(data: dict) -> list[Mode]:
    modes = []
    for item in data.get("custom") or []:
        try:
            modes.append(make_mode(int(item["width"]), int(item["height"]), "custom", item.get("name") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    return modes


def _mode_from_text(text: str, data: dict) -> Mode:
    found = find_named(text, _custom(data))
    if found:
        return found
    width, height = parse_mode(text)
    return make_mode(width, height, "custom")


def dispatch(args, state_dir: Path) -> int:
    state_path = state_dir / "display.json"
    data = _load(state_path)
    cmd = args.display_cmd
    if cmd == "list":
        rows = [item.as_dict() for item in list(CATALOG) + _custom(data)]
        print(json.dumps(rows))
        return 0
    if cmd == "status":
        print(json.dumps({"saved": data.get("applied"), "previous": data.get("previous"), "geometry": geometry()}))
        return 0
    if cmd == "add":
        width, height = parse_mode(args.mode)
        custom = list(data.get("custom") or [])
        custom.append({"width": width, "height": height, "aspect": aspect_name(width, height), "name": f"{width}x{height}"})
        data["custom"] = custom
        _save(state_path, data)
        print(json.dumps(custom[-1]))
        return 0
    if cmd == "setup":
        modes = [parse_mode(item) for item in args.mode]
        vendor = int(str(args.vendor), 0)
        product = int(str(args.product), 0)
        blob = build_plist(vendor, product, modes, args.name)
        out = Path(args.out) if args.out else state_dir / f"DisplayProductID-{product:x}.plist"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        parsed = parse_plist(blob)
        print(json.dumps({"out": str(out), "modes": parsed["modes"]}))
        print(install_hint(vendor, product, str(out)))
        return 0
    if cmd == "apply":
        chosen = _mode_from_text(args.mode, data)
        if data.get("applied"):
            data["previous"] = data["applied"]
        record = chosen.as_dict()
        moved = []
        if args.frames:
            frames = json.loads(Path(args.frames).read_text(encoding="utf-8"))
            moved = reflow(frames, {"x": 0, "y": 0, "w": chosen.width, "h": chosen.height})
        record["windows_moved"] = len(moved)
        data["applied"] = record
        _save(state_path, data)
        if not args.live:
            print(json.dumps({"recorded": record, "live": False, "moved": moved}))
            return 0
        tool = shutil.which("displayplacer")
        if not tool:
            print("No display tool is on PATH. The size was recorded and the desktop was not changed.")
            return 2
        print(json.dumps({
            "recorded": record,
            "live": False,
            "tool": tool,
            "reason": "a screen id is required before the desktop is changed",
        }))
        return 2
    if cmd == "watch":
        whois = None
        if args.whois_cmd:
            import subprocess
            completed = subprocess.run(args.whois_cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
            whois = json.loads(completed.stdout or "{}")
        elif args.whois:
            whois = json.loads(args.whois)
        else:
            whois = lookup_whois(args.name or "", None)
        matched = match_client(
            args.name or None,
            whois=whois,
            rdns=args.rdns or None,
            mdns=args.mdns or None,
            screens=args.screens or None,
            luna=args.luna or None,
        )
        if matched is None:
            print(json.dumps({"change": False}))
            return 0
        print(json.dumps({"change": True, "mode": matched.as_dict()}))
        return 0
    if cmd == "restore":
        previous = data.get("previous")
        if not previous:
            print(json.dumps({"restored": False}))
            return 0
        data["previous"] = data.get("applied")
        data["applied"] = previous
        _save(state_path, data)
        print(json.dumps({"restored": True, "mode": previous, "live": False}))
        return 0
    if cmd == "reflow":
        if args.install_agent:
            dest = Path(args.dest) if args.dest else state_dir
            dest.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve().parent / "keyboard_reflow.py"
            session_dir = dest / "session"
            text = render_agent(dest, script, session_dir, label=args.label)
            target = dest / f"{args.label}.plist"
            target.write_text(text, encoding="utf-8")
            print(json.dumps({"plist": str(target), "label": args.label, "loaded": False}))
            return 0
        controller = Controller()
        if args.dry_run:
            print(json.dumps(controller.once(dry_run=True), sort_keys=True))
            return 0
        if args.status:
            print(json.dumps(controller.status(), sort_keys=True))
            return 0
        print(json.dumps(controller.once(dry_run=False), sort_keys=True))
        return 0
    print("Unknown display command.")
    return 2
