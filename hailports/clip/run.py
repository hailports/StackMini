"""Command line for Paiste. The menu bar and the idle loop both land here."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from hailports.llm import ModelOffline
from hailports.clip.scene import (
    as_writer,
    copy_text,
    overreach,
    prepare,
    reply_ok,
    run_inbox,
    scene_from,
    source_dirs,
    plain_reply,
    trim_overreach,
)
from hailports.clip.hops import complete, endpoints_from


def _complete(cfg: dict):
    hops = endpoints_from(cfg)

    def run(messages: list[dict]) -> str:
        text, name = complete(hops, messages)
        try:
            path = Path.home() / ".config" / "paiste" / "last-hop"
            path.write_text(name + "\n", encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass
        return text

    return run


def _instructions(cfg: dict | None) -> str:
    """The writing rules stay off a copied app. A local signer reads a local file. Everyone else asks the license server."""
    from hailports.clip.gate import read_token, secret_path
    local = Path.home() / ".config" / "paiste" / "instructions"
    if secret_path().is_file() and local.is_file():
        return local.read_text(encoding="utf-8", errors="replace").strip()
    token = read_token(cfg)
    if not token:
        return ""
    paiste = (cfg or {}).get("paiste") or {}
    base = str(paiste.get("license_url") or "https://www.paisty.com/api/license").strip()
    url = base.rsplit("/", 1)[0] + "/prompt"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        return ""
    if doc.get("ok") is True:
        return str(doc.get("system") or "").strip()
    return ""


def _context_note(cfg: dict | None) -> str:
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("context_file") or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()[:6000]


def lookup_query(text: str, limit: int = 2200) -> str:
    """Keep the ask. On a long window the questions sit past the first 500 characters."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return (flat[-1500:] + "\n" + flat[:400])[:limit]


def _lookup_notes(cfg: dict | None, text: str) -> str:
    from hailports.clip.board import code_notes
    brain = ""
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("lookup_cmd") or "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if text.strip() and path.is_file():
        try:
            proc = subprocess.run(
                [str(path)],
                input=lookup_query(text),
                text=True,
                capture_output=True,
                timeout=25,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            brain = proc.stdout.strip()[:2000]
    code = code_notes(text) if text.strip() else ""
    if code:
        code = "Code on this Mac. Use a file, label, or message written here. Do not invent one.\n" + code
    return (code + "\n" + brain).strip()[:4000]


def _with_points(messages: list[dict], points: list[str]) -> list[dict]:
    if not points:
        return messages
    lines = ["Address every point below. One short line each. Do not skip one."]
    lines.extend("%d. %s" % (index, point) for index, point in enumerate(points, 1))
    block = "\n".join(lines)
    out = [dict(item) for item in messages]
    for index in range(len(out) - 1, -1, -1):
        if out[index].get("role") == "user":
            out[index]["content"] = str(out[index].get("content") or "") + "\n\n" + block
            break
    return out


def draft_scene(doc: dict, base_url: str, model: str, copy: bool, cfg: dict | None = None) -> int:
    prepared = prepare(scene_from(doc), folders=source_dirs(cfg))
    if not prepared.get("ok"):
        print(prepared.get("reason") or "empty")
        return 2
    route = cfg or {"llm": {"base_url": base_url, "model": model or "local"}}
    from hailports.clip.gate import allowed
    if not allowed(route):
        print("license")
        return 2
    rules = _instructions(route)
    if not rules:
        print("license")
        return 2
    if not rules.startswith("You are viewing"):
        rules = (
            "You are viewing a communication on the screen. "
            "Use the on-screen text and every connected note or lookup. "
            "Then draft only the reply.\n\n"
            + rules
        )
    if "whole conversation" not in rules:
        rules = (
            "Read the whole conversation on screen, not only the last line. "
            "Use what was already said. Draft the reply to what is still open.\n\n"
            + rules
        )
    if "Search all connected mail" not in rules:
        rules = (
            "Search all connected mail, all connected calendar events, and all connected notes. "
            "Do not stop at a sample. Use what matches this conversation. "
            "Do not invent a fact that is not on screen and not in those.\n\n"
            + rules
        )
    messages = prepared.get("messages") or []
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = rules
    me = [part.strip() for part in str((route.get("paiste") or {}).get("me") or "").split(",") if part.strip()]
    voice = _context_note(route)[:1400]
    parts = []
    if me:
        parts.append(
            "You are " + me[0] + ". Never greet " + me[0] + ". "
            "Never start with hey " + me[0].split()[0] + ". "
            "If the window is their own item and nobody else asked a question, write a note with no greeting."
        )
    if voice:
        parts.append(voice)
    found = _lookup_notes(route, "\n".join(prepared.get("sources") or []))
    if found:
        parts.append(
            "Notes that match this message. Ignore a note that does not match. "
            "If a note names a file, a label, or a message, use those words. "
            "Do not replace them with a vague restatement. "
            "Code lines outrank an older note when they disagree. "
            "Do not invent a name that is not written in the notes.\n" + found[:2200]
        )
    note = "\n\n".join(parts).strip()
    if note:
        from hailports.clip.scene import runner_context
        prepared["messages"] = runner_context(prepared.get("messages") or [], note)
    sources = "\n".join(prepared.get("sources") or [])
    from hailports.clip.board import misses_points, open_points, thin_reply
    points = open_points(sources)
    prepared["messages"] = _with_points(prepared.get("messages") or [], points)
    try:
        hops = endpoints_from(route)
        text, hop = complete(hops, prepared["messages"])
        text = plain_reply(text)
        researched = bool(re.search(r"\.(?:cls|js|html)\b", found or ""))
        weak = misses_points(text, points) or (found and thin_reply(text, found)) or (len(points) >= 2 and not researched)
        if weak and len(hops) > 1:
            try:
                nxt, nxt_hop = complete(hops[1:], prepared["messages"])
                nxt = plain_reply(nxt)
                if nxt and not thin_reply(nxt, found) and not misses_points(nxt, points):
                    text, hop = nxt, nxt_hop
            except ModelOffline:
                pass
        try:
            mark = Path.home() / ".config" / "paiste" / "last-hop"
            mark.write_text(hop + "\n", encoding="utf-8")
            mark.chmod(0o600)
        except OSError:
            pass
        if overreach(text, sources):
            retry = list(prepared["messages"]) + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "Rewrite that reply in the same voice. "
                    "Do not promise any action. Do not invent steps, times, or facts. "
                    "If the steps are not in the text, say they are not listed."
                )},
            ]
            text = plain_reply(_complete(route)(retry).strip())
        text = as_writer(trim_overreach(text, sources), me)
    except ModelOffline as exc:
        print("model offline: " + str(exc))
        return 2
    if overreach(text, sources) or not reply_ok(text):
        print("empty")
        return 2
    if copy and text:
        copy_text(text)
    print(text)
    return 0 if text else 2


def _route_dest(args):
    import os
    raw = str(getattr(args, "route_out", "") or "").strip()
    if not raw or raw == ".":
        raw = os.environ.get("PAISTE_ROUTE") or str(Path.home() / ".config" / "paiste" / "route.yaml")
    return Path(raw)


def cmd_onboard(args, cfg) -> int:
    """Write the model choice. The window text is not saved and nothing is sent."""
    from hailports.config import save_file
    from hailports.clip.board import BoardError, app_status, apply_board, attach_keys, discover, probe_model, setup_hint, show_board, start_login, use_app
    if getattr(args, "login", ""):
        print(start_login(str(args.login)))
        return 0
    if getattr(args, "use", ""):
        name = str(args.use).strip().lower()
        state = app_status(name)
        if state != "signed-in":
            print(state)
            return 2
        dest = _route_dest(args)
        try:
            updated = use_app(cfg.get("paiste") or {}, name)
        except BoardError as exc:
            print(exc)
            return 2
        dest.parent.mkdir(parents=True, exist_ok=True)
        save_file(dest, {"paiste": updated})
        dest.chmod(0o600)
        print("using")
        return 0
    if getattr(args, "connect", ""):
        print(app_status(str(args.connect)))
        return 0
    if getattr(args, "hint", False):
        print(setup_hint() or "")
        return 0
    if getattr(args, "scan", False):
        from hailports.clip.board import scan_setup
        print(json.dumps(scan_setup()))
        return 0
    if getattr(args, "show", False):
        print(json.dumps(show_board(cfg.get("paiste") or {}, discover())))
        return 0
    path = Path(getattr(args, "answers", "") or "")
    answers = None
    if path.is_file():
        try:
            answers = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            answers = None
        try:
            path.unlink()
        except OSError:
            pass
    if getattr(args, "ingest", False):
        from hailports.clip.board import ingest_files
        from hailports.config import save_file
        paths = []
        if isinstance(answers, dict) and isinstance(answers.get("paths"), list):
            paths = [str(item) for item in answers["paths"]]
        root = Path.home() / ".config" / "paiste" / "sources"
        info = ingest_files(paths, root)
        if info["added"]:
            paiste = dict(cfg.get("paiste") or {})
            paiste["sources_dir"] = str(root)
            route = dict(cfg)
            route["paiste"] = paiste
            dest = _route_dest(args)
            dest.parent.mkdir(parents=True, exist_ok=True)
            save_file(dest, route)
            try:
                dest.chmod(0o600)
            except OSError:
                pass
        print("added %d%s" % (info["added"], " sent" if info.get("sent") else ""))
        return 0
    if getattr(args, "probe", False):
        doc = answers if isinstance(answers, dict) else {}
        text = probe_model(str(doc.get("line") or ""), str(doc.get("key") or ""))
        print(text)
        return 0 if text.startswith("Connected") else 2
    if not isinstance(answers, dict):
        print("answers")
        return 2
    dest = _route_dest(args)
    try:
        answers = attach_keys(dest.parent, answers)
        updated = apply_board(cfg.get("paiste") or {}, answers)
    except BoardError as exc:
        print(str(exc))
        return 2
    route = dict(cfg)
    route["paiste"] = updated
    save_file(dest, route)
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    print(str(dest))
    print("sent false")
    return 0


def cmd_paiste(args, cfg, state: Path) -> int:
    paiste = cfg.get("paiste") or {}
    action = args.action or "draft"
    if action == "onboard":
        return cmd_onboard(args, cfg)
    if action == "setup":
        from hailports.config import save_file
        from hailports.clip.board import discover, guide
        from hailports.promptui import PlainIO, YesIO
        io = YesIO() if getattr(args, "yes", False) else PlainIO()
        import os
        os.environ.setdefault("PAISTY_SETUP_URL", "https://paisty.com/api/setup")
        updated = guide(io, dict(paiste), discover())
        route = {"paiste": updated, "llm": cfg.get("llm") or {}}
        dest = Path(getattr(args, "route_out", "") or "") 
        if not str(dest):
            dest = Path.home() / ".config" / "paiste" / "route.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        save_file(dest, route)
        print(str(dest))
        print("sent false")
        return 0
    if action == "menu":
        app = Path(__file__).resolve().parents[1] / "contrib" / "paiste" / "Paiste.app" / "Contents" / "MacOS" / "Paiste"
        print("Build the menu with contrib/paiste/build.sh, then open Paiste.app.")
        print(str(app))
        return 0
    if action == "auto":
        inbox = Path(args.inbox or state / "paiste-inbox")
        outbox = Path(args.outbox or state / "paiste-outbox")
        folders = source_dirs(cfg)
        try:
            rows = run_inbox(inbox, outbox, _complete(cfg), folders=folders, copy=bool(args.copy or paiste.get("copy")))
        except ModelOffline as exc:
            print("model offline: " + str(exc))
            return 2
        print(json.dumps({"sent": False, "results": rows}))
        return 0
    raw = ""
    if args.scene:
        raw = Path(args.scene).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    if not raw.strip():
        print("Pass a scene file with --scene.")
        return 2
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        print("scene was not JSON")
        return 2
    llm = cfg.get("llm") or {}
    return draft_scene(doc, str(llm.get("base_url") or ""), str(llm.get("model") or "local"), bool(args.copy), cfg)
