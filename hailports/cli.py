"""One command for setup, chat, and the rest of the appliance.

    hailports setup
    hailports doctor
    hailports chat
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hailports import config as config_mod
from hailports import __version__
from hailports.alerts import alert_text, deliver, probe_login
from hailports.askdo import gather, respond
from hailports.bridges import load_all as load_bridges
from hailports.calendar_blocker import carve, to_ics
from hailports.doctor import failed, file_contains_secret, format_report, run_doctor
from hailports.draft_runner import connect as draft_connect
from hailports.draft_runner import enqueue, pending, run_next
from hailports.features import draft_reply, see
from hailports.httpjson import ProviderError
from hailports.idle import allowed_repos_from, next_job, run_job
from hailports.images import to_png_bytes
from hailports.imessage import MacOnly, compose, poll_hint
from hailports.learning import connect as learn_connect
from hailports.learning import prompt_with_lessons, teach, write_runbook
from hailports.llm import health
from hailports.osapi import ServiceManager, current_os, pid_alive, read_pid, restart_exact
from hailports.paths import config_dir, data_dir
from hailports.plugins import load_all as load_plugins
from hailports.providers.factory import build_router
from hailports.secrets_store import EnvAndKeychain
from hailports.sprint import packet as sprint_packet
from hailports.story import story_reply
from hailports.threads import append as thread_append
from hailports.threads import connect as thread_connect
from hailports.threads import context_messages, maybe_summarize, reset as thread_reset
from hailports.ticket_guard import acquire, connect as guard_connect, remember
from hailports.webtools import DataLine, gather_web, refuse_sensitive
from hailports.wizard import add_setup_args, launch as launch_setup
from hailports.notes import add as brain_add
from hailports.notes import brain_status, connect as brain_connect, hybrid_search, ingest_path, note_meta, search as brain_search, store_vector
from hailports.worknow import brief as focus_brief
from hailports.worknow import load_cards
from hailports_setup.services import appliance_specs, render as render_unit

PLUG_NAMES = ("crm", "tracker", "mail", "meetings", "files")
SETUP_WORDS = ("setup", "build", "cmake", "llama", "port", "api key", "install", "wizard", "doctor")


def repo_example() -> Path:
    return Path(__file__).resolve().parents[1] / "config.example.yaml"


def resolve_config(path: str | None) -> Path:
    if path:
        return Path(path)
    candidate = config_dir() / "config.yaml"
    if candidate.exists():
        return candidate
    return repo_example()


def load_config(path: str | None) -> tuple[dict, Path]:
    file_path = resolve_config(path)
    if file_path.exists():
        return config_mod.load_file(file_path), file_path
    return {}, file_path


def _refuse_example_write(path: Path) -> int | None:
    try:
        if path.resolve() == repo_example().resolve():
            print("That file is the example shipped with HailPorts. Run hailports setup to write your own config.")
            return 2
    except OSError:
        return None
    return None


def _router(cfg: dict):
    return build_router(cfg, EnvAndKeychain())


def _print_reply(reply) -> None:
    print(reply.render())


def _fail_provider(exc: ProviderError) -> int:
    print("That step failed.")
    print("What went wrong: " + exc.what)
    print("How to fix: " + exc.how)
    return 2


def _setup_log(cfg_path: Path) -> str:
    candidates = [
        cfg_path.parent / "setup.log",
        data_dir() / "setup.log",
        config_dir() / "setup.log",
    ]
    for path in candidates:
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
    return ""


def _state_dir(cfg_path: Path) -> Path:
    try:
        if cfg_path.resolve() == repo_example().resolve():
            return data_dir()
    except OSError:
        return data_dir()
    if cfg_path.name == "config.yaml":
        return cfg_path.parent
    return data_dir()


def _runbook_read_root(cfg: dict) -> Path:
    root = Path((cfg.get("runbooks") or {}).get("dir") or (data_dir() / "runbooks"))
    if not root.is_absolute():
        candidate = Path(__file__).resolve().parents[1] / root
        if candidate.exists():
            return candidate
        return data_dir() / root
    return root


def _plain_setup_help(text: str, log: str) -> str:
    del text
    lines = [
        "That step failed.",
        "What went wrong: no model or API is ready to answer.",
        "How to fix: run hailports setup, then retry.",
    ]
    if log.strip():
        lines.append("Last setup log:")
        lines.extend(log.strip().splitlines()[-12:])
    return "\n".join(lines)


def _model_ready(cfg: dict) -> bool:
    mode = str(cfg.get("mode") or "local")
    if mode == "local":
        base = ((cfg.get("llm") or {}).get("base_url")) or ""
        return bool(base) and health(str(base), timeout=2)
    if mode in ("api", "hybrid"):
        return bool(EnvAndKeychain().get(cfg))
    return False


def cmd_doctor(cfg: dict, cfg_path: Path) -> int:
    secret = EnvAndKeychain().get(cfg) if str(cfg.get("mode") or "local") in ("api", "hybrid") else ""
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    checks = run_doctor(cfg)
    print(format_report(checks))
    if file_contains_secret(text, secret):
        print("needs attention config-file the API key is written in the config file")
        return 1
    return 1 if failed(checks) else 0


def cmd_mode(cfg: dict, cfg_path: Path, name: str | None) -> int:
    current = str(cfg.get("mode") or "local")
    if not name:
        print(current)
        return 0
    blocked = _refuse_example_write(cfg_path)
    if blocked:
        return blocked
    cfg["mode"] = name
    cfg.setdefault("cloud", {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "HAILPORTS_API_KEY",
        "keychain_service": "hailports",
        "keychain_account": "cloud",
    })
    cfg.setdefault("routing", {
        "confidence_floor": 0.55,
        "complexity_ceiling": 1,
        "max_local_output_chars": 900,
        "compact_char_budget": 1600,
    })
    config_mod.save_file(cfg_path, cfg)
    print(f"mode {name}")
    if name == "api":
        print("API mode will not build or download a local model.")
    elif name == "local":
        print("Local mode keeps every call on this computer.")
    else:
        print("Hybrid mode stays local until the complexity, confidence, or length policy says to escalate.")
    if name in ("api", "hybrid") and not EnvAndKeychain().get(cfg):
        print("The API key is not set. Export HAILPORTS_API_KEY or store it in the OS keychain.")
    return 0


def _lessons_system(cfg_path: Path) -> str:
    root = _state_dir(cfg_path) / "lessons.sqlite"
    if not root.exists():
        return ""
    return prompt_with_lessons(learn_connect(root), _state_dir(cfg_path) / "runbooks")


def cmd_chat(cfg: dict, cfg_path: Path, prompt: str, image: str | None) -> int:
    if prompt.strip().lower() == "reset":
        thread_reset(thread_connect(_state_dir(cfg_path) / "thread.sqlite"))
        print("reset")
        return 0
    system = _lessons_system(cfg_path)
    try:
        router = _router(cfg)
        store = thread_connect(_state_dir(cfg_path) / "thread.sqlite")
        summary, history = context_messages(store)
        web = "" if image else gather_web(
            prompt,
            str(((cfg.get("web") or {}).get("search_url") or "")),
            str(((cfg.get("web") or {}).get("news_url") or "")),
        )
        if image:
            png = to_png_bytes(Path(image))
            reply = see(router, prompt or "What is in this image?", png)
        else:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            if summary:
                messages.append({"role": "system", "content": "Earlier summary:\n" + summary})
            messages.extend(history)
            messages.append({"role": "user", "content": prompt + web})
            reply = router.complete(messages, task="summarize" if web else None)
        thread_append(store, "user", prompt)
        thread_append(store, "assistant", reply.text)
        budget = int(((cfg.get("thread") or {}).get("summary_chars") or 2400))
        try:
            maybe_summarize(store, router, budget_chars=budget)
        except ProviderError:
            pass
    except ProviderError as exc:
        return _fail_provider(exc)
    except DataLine as exc:
        print("That step failed.")
        print("What went wrong: " + str(exc))
        print("How to fix: drop the private text and try a public page.")
        return 2
    _print_reply(reply)
    return 0


def cmd_chat_repl(cfg: dict, cfg_path: Path) -> int:
    print("Chat. Type quit on its own line to leave. Type reset to drop the thread.")
    while True:
        try:
            line = input("you> ")
        except EOFError:
            print()
            return 0
        if line.strip().lower() in ("quit", "exit"):
            return 0
        if not line.strip():
            continue
        code = cmd_chat(cfg, cfg_path, line, None)
        if code not in (0, 2):
            return code
    return 0


def cmd_ask(cfg: dict, cfg_path: Path, text: str) -> int:
    low = text.lower()
    setupish = any(word in low for word in SETUP_WORDS)
    log = _setup_log(cfg_path)
    try:
        refuse_sensitive(text)
    except DataLine:
        if not _model_ready(cfg):
            print("That step failed.")
            print("What went wrong: the message looks private.")
            print("How to fix: remove secrets and ask again. It was not sent.")
            return 2
        try:
            reply = respond(_router(cfg), text, {"kind": "refuse", "notes": [], "actions": ["held"]})
        except ProviderError as exc:
            return _fail_provider(exc)
        _print_reply(reply)
        return 0
    if not _model_ready(cfg):
        print(_plain_setup_help(text, log))
        return 0
    try:
        router = _router(cfg)
        if setupish:
            reply = router.complete(
                [
                    {"role": "system", "content": "Answer the setup question in plain words. Use the log. Do not invent commands that are not in the log."},
                    {"role": "user", "content": "Setup log:\n" + log[-4000:] + "\n\nQuestion: " + text},
                ],
                task="read",
            )
        else:
            brain = brain_connect(_state_dir(cfg_path) / "brain.sqlite")
            bundle = gather(text, _runbook_read_root(cfg), brain, allow_root=_runbook_read_root(cfg))
            reply = respond(router, text, bundle)
    except ProviderError as exc:
        print(_plain_setup_help(text, log))
        print("What went wrong: " + exc.what)
        print("How to fix: " + exc.how)
        return 2
    _print_reply(reply)
    return 0


def _embed_port(cfg: dict) -> int:
    url = str(((cfg.get("notes") or {}).get("embed_url") or ""))
    if ":" in url.rsplit("/", 1)[-1]:
        try:
            return int(url.rsplit(":", 1)[-1])
        except ValueError:
            return 8097
    return 8097


def _specs(cfg: dict, work: Path):
    label = str(cfg.get("label") or "com.hailports.local")
    mode = str(cfg.get("mode") or "local")
    modules = cfg.get("modules") or {}
    notes = bool(modules.get("notes", True))
    return appliance_specs(
        label,
        mode,
        str(work),
        sys.executable,
        model_cmd=["llama-server", "-m", "models/model.gguf"],
        embed_port=_embed_port(cfg),
        notes=notes,
    )


def cmd_services(cfg: dict, cfg_path: Path, action: str, apply: bool) -> int:
    work = cfg_path.parent
    os_name = str(cfg.get("os") or current_os())
    if os_name not in ("darwin", "linux", "windows"):
        os_name = current_os()
    mgr = ServiceManager(os_name, apply=apply)
    specs = _specs(cfg, work)
    if action == "install":
        blocked = _refuse_example_write(cfg_path)
        if blocked:
            return blocked
        for spec in specs:
            path = mgr.unit_path(spec.label, work)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_unit(spec, os_name), encoding="utf-8")
            print(f"wrote {path}")
        print("Restart goes through the service manager only, and only when the recorded process id still matches.")
        return 0
    if action == "status":
        for spec in specs:
            pid_path = work / f"{spec.kind}.pid"
            recorded = read_pid(pid_path)
            alive = pid_alive(recorded) if recorded else False
            print(f"{spec.label} pid={recorded if recorded is not None else 'none'} alive={'yes' if alive else 'no'}")
        return 0
    if action == "logs":
        for spec in specs:
            path = work / "logs" / f"{spec.kind}.log"
            print(f"log {spec.kind} {path}")
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[-40:]:
                    print(line)
            else:
                print("(no log yet)")
        return 0
    command_for = {
        "start": mgr.start_command,
        "stop": mgr.stop_command,
    }
    if action == "restart":
        for spec in specs:
            pid_path = work / f"{spec.kind}.pid"
            recorded = read_pid(pid_path)
            current = recorded if recorded and pid_alive(recorded) else None
            outcome = restart_exact(mgr, spec.label, recorded, current, healthy=False)
            print(f"{spec.label} {outcome}")
        return 0
    builder = command_for.get(action)
    if builder is None:
        print("Use install, start, stop, restart, status, or logs.")
        return 2
    for spec in specs:
        argv = builder(spec.label)
        result = mgr.run(argv)
        print(result + " " + " ".join(argv))
    if not apply:
        print("Nothing was sent to the service manager. Pass --apply to run those commands.")
    return 0


def cmd_plugins(cfg: dict, cfg_path: Path, action: str, name: str | None, base_url: str | None, token: str | None) -> int:
    if token:
        print("Tokens stay in the environment, not on the command line or in the config file.")
        return 2
    if action in ("list", "", None):
        for plug in load_plugins(cfg):
            enabled = str(((cfg.get("plugins") or {}).get(plug.name) or {}).get("enabled"))
            print(plug.describe() + f" enabled={enabled}")
        for bridge in load_bridges(cfg):
            state = "configured" if bridge.configured() else "off"
            print(f"bridge {bridge.name}: {state}")
        return 0
    if name not in PLUG_NAMES:
        print("Unknown plug. Choices: " + ", ".join(PLUG_NAMES))
        return 2
    blocked = _refuse_example_write(cfg_path)
    if blocked:
        return blocked
    block = cfg.setdefault("plugins", {}).setdefault(name, {})
    if action == "enable":
        block["enabled"] = True
        block.setdefault("token", "CHANGE_ME")
        config_mod.save_file(cfg_path, cfg)
        print(f"{name}: marked enabled. It stays off until the token is in the environment, not the config file.")
        return 0
    if action == "configure":
        if not base_url:
            print("Pass --base-url. Do not pass the token.")
            return 2
        block["base_url"] = base_url
        block.setdefault("token", "CHANGE_ME")
        config_mod.save_file(cfg_path, cfg)
        print(f"{name}: saved the base URL. Set the token in the environment.")
        return 0
    print("Use list, enable, or configure.")
    return 2


def _cmd_idle(cfg: dict, cfg_path: Path) -> int:
    state_path = _state_dir(cfg_path) / "idle-state.json"
    last = {}
    if state_path.exists():
        try:
            last = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            last = {}
    raw_interval = (cfg.get("idle") or {}).get("interval_s")
    interval = 120.0 if raw_interval is None else float(raw_interval)
    name = next_job(0, last, time_now(), interval)
    if not name:
        print(json.dumps({"job": None}))
        return 0
    brain = brain_connect(_state_dir(cfg_path) / "brain.sqlite")
    threads = thread_connect(_state_dir(cfg_path) / "thread.sqlite")
    news_url = str(((cfg.get("web") or {}).get("news_url") or ""))
    try:
        router = _router(cfg)
    except ProviderError:
        router = None
    fetch = None
    if news_url:
        from hailports.webtools import news as fetch_news
        fetch = lambda: fetch_news(news_url)
    try:
        result = run_job(
            name,
            router=router,
            brain=brain,
            thread_con=threads,
            news_fetch=fetch,
            allowed_repos=allowed_repos_from(cfg),
        )
    except ProviderError as exc:
        return _fail_provider(exc)
    last[name] = time_now()
    state_path.write_text(json.dumps(last), encoding="utf-8")
    print(json.dumps(result))
    return 0


def _cmd_alerts(cfg: dict, cfg_path: Path, probe: str | None, channel: str | None) -> int:
    probe_url = probe or str(((cfg.get("alerts") or {}).get("probe_url") or ""))
    channel_url = channel if channel is not None else str(((cfg.get("alerts") or {}).get("private_url") or ""))
    if not probe_url:
        print("That step failed.")
        print("What went wrong: no probe URL is set.")
        print("How to fix: pass --probe or set alerts.probe_url.")
        return 2
    state = probe_login(probe_url)
    print(state)
    if state != "login-broken":
        return 0
    outbox = _state_dir(cfg_path) / "alerts.log"
    try:
        reply = alert_text(_router(cfg), "probe")
        text = reply.text or "login-broken service=probe"
        _print_reply(reply)
    except ProviderError as exc:
        text = "login-broken service=probe"
        deliver(text, channel_url, outbox)
        return _fail_provider(exc)
    info = deliver(text, channel_url, outbox)
    print(json.dumps(info))
    return 0


def time_now() -> float:
    import time
    return time.time()


def cmd_brain(cfg: dict, action: str, title: str, body: str, query: str, db: Path | None = None) -> int:
    con = brain_connect(db or (data_dir() / "brain.sqlite"))
    if action == "ingest":
        doc_id = brain_add(con, title, body)
        note_meta(con, doc_id, title, "paste", body)
        try:
            router = _router(cfg)
            outcome = router.embed([body])
            if outcome.vectors:
                store_vector(con, doc_id, outcome.provider, outcome.vectors[0])
            print(json.dumps({"id": doc_id, "embeddings": outcome.provider, "fallback": outcome.fallback}))
            print(outcome.line(router.mode))
        except ProviderError as exc:
            print(json.dumps({"id": doc_id, "embeddings": "none"}))
            return _fail_provider(exc)
        return 0
    if action == "search":
        vector = None
        try:
            outcome = _router(cfg).embed([query])
            if outcome.vectors:
                vector = outcome.vectors[0]
        except (ProviderError, OSError):
            vector = None
        print(json.dumps(hybrid_search(con, query, vector)))
        return 0
    if action == "status":
        print(json.dumps(brain_status(con)))
        return 0
    print("Use ingest, search, or status.")
    return 2


def cmd_uninstall(cfg: dict, cfg_path: Path, delete_data: bool, apply: bool) -> int:
    blocked = _refuse_example_write(cfg_path)
    if blocked:
        return blocked
    os_name = str(cfg.get("os") or current_os())
    mgr = ServiceManager(os_name if os_name in ("darwin", "linux", "windows") else current_os(), apply=apply)
    for spec in _specs(cfg, cfg_path.parent):
        argv = mgr.stop_command(spec.label)
        result = mgr.run(argv)
        print(result + " " + " ".join(argv))
        unit = mgr.unit_path(spec.label, cfg_path.parent)
        if unit.exists():
            unit.unlink()
            print(f"removed {unit}")
    services = cfg_path.parent / "services"
    if services.exists():
        for path in services.glob("*"):
            if path.is_file():
                path.unlink()
                print(f"removed {path}")
    if cfg_path.exists() and cfg_path.name == "config.yaml":
        cfg_path.unlink()
        print(f"removed {cfg_path}")
    if delete_data:
        root = data_dir()
        if root.exists():
            for path in root.glob("*.sqlite"):
                path.unlink()
                print(f"removed {path}")
    else:
        print("Notes were kept. Pass --delete-data to remove the local database files.")
    if not apply:
        print("Stop commands were not sent. Pass --apply to call the service manager.")
    return 0


def cmd_update() -> int:
    print(f"hailports {__version__}")
    print("Update with: python -m pip install --user --upgrade .")
    print("Or, if you used pipx: pipx upgrade hailports-appliance")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hailports",
        description="Local, hybrid, or API model appliance. Start with hailports setup.",
    )
    parser.add_argument("--config", default=None, help="Config file. Defaults to the one setup wrote.")
    parser.add_argument("--version", action="store_true", help="Print the version and exit.")
    sub = parser.add_subparsers(dest="cmd", required=False)

    setup_p = sub.add_parser("setup", help="Guided setup. Asks for the mode first.")
    add_setup_args(setup_p)

    sub.add_parser("doctor", help="Test the mode you configured.")
    mode_p = sub.add_parser("mode", help="Show or switch local, hybrid, or api.")
    mode_p.add_argument("name", nargs="?", choices=["local", "hybrid", "api"])

    chat_p = sub.add_parser("chat", help="Talk to the model. One line, or a session when you omit the text.")
    chat_p.add_argument("prompt", nargs="*", help="What to ask. Omit it to start a session. Use reset to drop the thread.")
    chat_p.add_argument("--image", default=None, help="Image file for a vision turn. HEIC is converted when pillow-heif is installed.")
    chat_p.add_argument("--reset", action="store_true", help="Drop the saved thread.")

    serve_p = sub.add_parser("serve", help="Web chat on the configured port.")
    serve_p.add_argument("--port", type=int, default=None)

    brain_p = sub.add_parser("brain", help="Notes: ingest, search, or status.")
    brain_p.add_argument("action", choices=["ingest", "ingest-path", "search", "status"])
    brain_p.add_argument("text", nargs="*")

    draft_p = sub.add_parser("drafter", help="Draft from notes you supply.")
    draft_p.add_argument("ask")
    draft_p.add_argument("--source", action="append", required=True)

    svc_p = sub.add_parser("services", help="Install, start, stop, status, or logs. Exact process id, service manager only.")
    svc_p.add_argument("action", choices=["install", "start", "stop", "restart", "status", "logs"])
    svc_p.add_argument("--apply", action="store_true", help="Actually call the service manager.")

    plug_p = sub.add_parser("plugins", help="List, enable, or configure optional plugs.")
    plug_p.add_argument("action", nargs="?", default="list", choices=["list", "enable", "configure"])
    plug_p.add_argument("name", nargs="?")
    plug_p.add_argument("--base-url", default=None)
    plug_p.add_argument("--token", default=None, help=argparse.SUPPRESS)

    sub.add_parser("update", help="Show the version and the update command.")
    un_p = sub.add_parser("uninstall", help="Remove service files this tool wrote.")
    un_p.add_argument("--delete-data", action="store_true")
    un_p.add_argument("--apply", action="store_true")

    ask_p = sub.add_parser("ask", help="Read notes, then ask the model. Setup questions use the setup log.")
    ask_p.add_argument("text")

    idle_p = sub.add_parser("idle", help="Run one useful job when the model is idle.")
    idle_p.add_argument("--once", action="store_true", default=True)

    alert_p = sub.add_parser("alerts", help="Probe a login URL and tell the owner on a private channel.")
    alert_p.add_argument("--probe", default=None)
    alert_p.add_argument("--channel", default=None)

    sub.add_parser("health", help="Short model reachability check.")
    story_p = sub.add_parser("story", help="Turn a sentence into a story card.")
    story_p.add_argument("text")
    add_p = sub.add_parser("brain-add", help="Add a note without embeddings.")
    add_p.add_argument("title")
    add_p.add_argument("body")
    find_p = sub.add_parser("brain-find", help="Search notes.")
    find_p.add_argument("query")
    sub.add_parser("now", help="Show the focus list.")
    learn_p = sub.add_parser("learn", help="Remember a correction.")
    learn_p.add_argument("prompt")
    learn_p.add_argument("correction")
    sprint_p = sub.add_parser("sprint", help="Pack a sprint from the backlog.")
    sprint_p.add_argument("hours", type=float)
    old_draft = sub.add_parser("draft", help="Queue a grounded draft.")
    old_draft.add_argument("ask")
    old_draft.add_argument("source")
    guard_p = sub.add_parser("guard", help="Refuse a duplicate card.")
    guard_p.add_argument("requester")
    guard_p.add_argument("title")
    block_p = sub.add_parser("block", help="Write calendar blocks as an ics file.")
    block_p.add_argument("day")
    block_p.add_argument("title")
    msg_p = sub.add_parser("imessage", help="macOS poll hint, or a model reply for a message.")
    msg_p.add_argument("text", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version or args.cmd is None:
        if args.version or args.cmd is None:
            if args.cmd is None and not args.version:
                parser.print_help()
                return 2
            print(__version__)
            return 0
    if args.cmd == "setup":
        return launch_setup(args)
    if args.cmd == "update":
        return cmd_update()

    cfg, cfg_path = load_config(args.config)

    if args.cmd == "doctor":
        return cmd_doctor(cfg, cfg_path)
    if args.cmd == "mode":
        return cmd_mode(cfg, cfg_path, args.name)
    if args.cmd == "chat":
        if args.reset:
            thread_reset(thread_connect(_state_dir(cfg_path) / "thread.sqlite"))
            print("reset")
            return 0
        prompt = " ".join(args.prompt).strip()
        if args.image or prompt:
            return cmd_chat(cfg, cfg_path, prompt, args.image)
        if sys.stdin.isatty():
            return cmd_chat_repl(cfg, cfg_path)
        print("Pass the text to send, or run hailports chat in a terminal.")
        return 2
    if args.cmd == "serve":
        from hailports.webchat import serve_router
        if args.port:
            cfg.setdefault("webchat", {})["port"] = args.port
        router = _router(cfg)
        web = cfg.get("webchat") or {}
        bind = str(web.get("bind") or "127.0.0.1")
        port = int(web.get("port") or 8080)
        httpd = serve_router(bind, port, router)
        print(f"webchat {bind}:{port} mode={router.mode}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            return 0
        return 0
    if args.cmd == "brain":
        text = args.text or []
        if args.action == "ingest":
            if len(text) < 2:
                print("Usage: hailports brain ingest TITLE BODY")
                return 2
            return cmd_brain(cfg, "ingest", text[0], " ".join(text[1:]), "", _state_dir(cfg_path) / "brain.sqlite")
        if args.action == "ingest-path":
            if not text:
                print("Usage: hailports brain ingest-path PATH")
                return 2
            con = brain_connect(_state_dir(cfg_path) / "brain.sqlite")
            ids = ingest_path(con, Path(text[0]))
            print(json.dumps({"ids": ids}))
            return 0
        if args.action == "search":
            if not text:
                print("Usage: hailports brain search QUERY")
                return 2
            return cmd_brain(cfg, "search", "", "", " ".join(text), _state_dir(cfg_path) / "brain.sqlite")
        return cmd_brain(cfg, "status", "", "", "", _state_dir(cfg_path) / "brain.sqlite")
    if args.cmd == "drafter":
        sources = []
        for item in args.source:
            path = Path(item)
            sources.append(path.read_text(encoding="utf-8") if path.exists() else item)
        try:
            reply = draft_reply(_router(cfg), args.ask, sources)
            if reply.raw.get("held"):
                reply = _router(cfg).complete(
                    [
                        {"role": "system", "content": "Tell the user the draft was held because a line had no source citation. Do not invent the draft."},
                        {"role": "user", "content": "The draft was held."},
                    ],
                    task="draft",
                )
        except ProviderError as exc:
            return _fail_provider(exc)
        except Exception as exc:
            print("That step failed.")
            print("What went wrong: " + str(exc))
            print("How to fix: pass at least one note with --source.")
            return 2
        _print_reply(reply)
        return 0
    if args.cmd == "services":
        return cmd_services(cfg, cfg_path, args.action, args.apply)
    if args.cmd == "plugins":
        return cmd_plugins(cfg, cfg_path, args.action, args.name, args.base_url, args.token)
    if args.cmd == "uninstall":
        return cmd_uninstall(cfg, cfg_path, args.delete_data, args.apply)
    if args.cmd == "ask":
        return cmd_ask(cfg, cfg_path, args.text)
    if args.cmd == "health":
        base = ((cfg.get("llm") or {}).get("base_url")) or "http://127.0.0.1:18080/v1"
        mode = str(cfg.get("mode") or "local")
        if mode == "api":
            print(json.dumps({"model": "api", "mode": mode}))
            return 0
        up = health(base) if base else False
        print(json.dumps({"model": "up" if up else "down", "base_url": base, "mode": mode}))
        return 0
    if args.cmd == "story":
        project = ((cfg.get("plugins") or {}).get("tracker") or {}).get("project") or "PROJECT"
        try:
            reply = story_reply(_router(cfg), args.text, str(project))
        except ProviderError as exc:
            return _fail_provider(exc)
        _print_reply(reply)
        return 0
    if args.cmd == "brain-add":
        con = brain_connect(data_dir() / "brain.sqlite")
        print(brain_add(con, args.title, args.body))
        return 0
    if args.cmd == "brain-find":
        con = brain_connect(data_dir() / "brain.sqlite")
        print(json.dumps(brain_search(con, args.query)))
        return 0
    if args.cmd == "now":
        from hailports.worknow import pack
        cards = load_cards(_state_dir(cfg_path) / "focus.json")
        if not _model_ready(cfg):
            print(pack(cards))
            return 0
        try:
            reply = focus_brief(_router(cfg), cards)
        except ProviderError as exc:
            return _fail_provider(exc)
        _print_reply(reply)
        return 0
    if args.cmd == "learn":
        state = _state_dir(cfg_path)
        con = learn_connect(state / "lessons.sqlite")
        teach(con, args.prompt, args.correction)
        write_runbook(con, state / "runbooks")
        print("saved")
        return 0
    if args.cmd == "sprint":
        items = cfg.get("backlog") or [{"title": "Write the first runbook", "hours": 2}]
        print(json.dumps(sprint_packet(items, args.hours, "20260101")))
        return 0
    if args.cmd == "draft":
        con = draft_connect(_state_dir(cfg_path) / "drafts.sqlite")
        job = enqueue(con, args.ask, [args.source])
        try:
            done = run_next(con, router=_router(cfg))
        except ProviderError as exc:
            return _fail_provider(exc)
        print(json.dumps({"id": job, "pending": pending(con), "job": done}))
        return 0
    if args.cmd == "guard":
        con = guard_connect(data_dir() / "guard.sqlite")
        result = acquire(con, args.requester, args.title)
        if result["ok"]:
            remember(con, args.requester, args.title, "local-1")
            result["card_id"] = "local-1"
        print(json.dumps(result))
        return 0
    if args.cmd == "block":
        print(to_ics(carve([args.title], args.day)))
        return 0
    if args.cmd == "idle":
        return _cmd_idle(cfg, cfg_path)
    if args.cmd == "alerts":
        return _cmd_alerts(cfg, cfg_path, args.probe, args.channel)
    if args.cmd == "imessage":
        if args.text:
            try:
                reply = compose(_router(cfg), args.text)
            except ProviderError as exc:
                return _fail_provider(exc)
            _print_reply(reply)
            return 0
        try:
            print(poll_hint())
        except MacOnly as exc:
            print(str(exc))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
