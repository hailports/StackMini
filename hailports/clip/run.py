"""Command line for Paiste. The menu bar and the idle loop both land here."""

from __future__ import annotations

import json
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
    strip_signoff,
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


def _lookup_notes(cfg: dict | None, text: str) -> str:
    paiste = (cfg or {}).get("paiste") or {}
    raw = str(paiste.get("lookup_cmd") or "").strip()
    if not raw or not text.strip():
        return ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return ""
    try:
        proc = subprocess.run(
            [str(path)],
            input=text[:4000],
            text=True,
            capture_output=True,
            timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()[:4000]


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
    note = _context_note(route)
    if me:
        note = (
            "You are " + me[0] + ". Never greet " + me[0] + ". "
            "Never start with hey " + me[0].split()[0] + ". "
            "If the window is their own item and nobody else asked a question, write a note with no greeting.\n\n"
            + note
        ).strip()
    found = _lookup_notes(route, "\n".join(prepared.get("sources") or []))
    if found:
        note = (note + "\n\nWork brain, use only the parts that match this message:\n" + found).strip()
    if note:
        from hailports.clip.scene import runner_context
        prepared["messages"] = runner_context(prepared.get("messages") or [], note)
    sources = "\n".join(prepared.get("sources") or [])
    try:
        text = strip_signoff(_complete(route)(prepared["messages"]).strip())
        if overreach(text, sources):
            retry = list(prepared["messages"]) + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "Rewrite that reply in the same voice. "
                    "Do not promise any action. Do not invent steps, times, or facts. "
                    "If the steps are not in the text, say they are not listed."
                )},
            ]
            text = strip_signoff(_complete(route)(retry).strip())
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
