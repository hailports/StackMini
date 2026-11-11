"""Guided setup. Asks for the mode first, then only the questions that mode needs.

Safe to run again. Progress is saved without API keys. --yes answers with defaults.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path

from hailports import config as config_mod
from hailports.doctor import failed, format_report, run_doctor
from hailports.llm import health as local_health
from hailports.paths import config_dir
from hailports.promptui import PlainIO, YesIO
from hailports.accounts import (
    assist_accounts,
    edge_install_script,
    edge_record,
    find_edge,
    open_plan,
    parse_accounts,
    parse_screen,
    place_accounts,
)
from hailports.providers.factory import DEFAULTS, build_cloud, build_router
from hailports.secrets_store import EnvAndKeychain
from hailports_setup.connect_assist import (
    apply_connection,
    assist_bridges,
    describe,
    detect_native,
    fallback_plan,
    parse_kinds,
    parse_ports,
    permission_lines,
)
from hailports_setup.detect import probe
from hailports_setup.installer import default_config, parse_modules
from hailports_setup.models import pick_binary, pick_model
from hailports_setup.services import appliance_specs, install_hint, render

MODES = [
    ("local", "Everything stays on this computer."),
    ("hybrid", "This computer goes first. A cloud model is used only when the job is hard."),
    ("api", "No local model. Every feature uses a hosted endpoint."),
]
PROVIDERS = [
    ("openai", "OpenAI, or any server that speaks the same chat API."),
    ("anthropic", "Anthropic messages API."),
    ("google", "Google Gemini API."),
    ("xai", "xAI, OpenAI-compatible."),
    ("openrouter", "OpenRouter, OpenAI-compatible."),
    ("groq", "Groq, OpenAI-compatible."),
    ("together", "Together, OpenAI-compatible."),
    ("compatible", "Some other OpenAI-compatible base URL."),
]
MODELS = [
    ("auto", "Pick from the memory on this machine."),
    ("tiny", "A plumbing-sized model."),
    ("small", "A small text model."),
    ("balanced", "A mid-size text model."),
    ("vision", "A vision model, when memory allows."),
]


class StepFailed(Exception):
    def __init__(self, what: str, how: str) -> None:
        super().__init__(what)
        self.what = what
        self.how = how


def add_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Accept defaults. Do not ask questions.")
    parser.add_argument("--resume", action="store_true", help="Continue a setup that was interrupted.")
    parser.add_argument("--target-os", choices=["linux", "darwin", "windows"])
    parser.add_argument("--arch", choices=["x64", "arm64"])
    parser.add_argument("--accel", choices=["metal", "cuda", "rocm", "vulkan", "cpu"])
    parser.add_argument("--ram-gb", type=float)
    parser.add_argument("--vram-gb", type=float)
    parser.add_argument("--modules", default="cli,webchat,notes")
    parser.add_argument("--label")
    parser.add_argument("--model", default="auto", choices=["auto", "tiny", "small", "balanced", "vision"])
    parser.add_argument("--llama-tag", default="b12078")
    parser.add_argument("--e2e-tiny", action="store_true")
    parser.add_argument("--state-dir")
    parser.add_argument("--mode", choices=["local", "hybrid", "api"])
    parser.add_argument("--provider", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--cloud-model", default="")
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--model-port", type=int, default=18080)
    parser.add_argument("--embed-port", type=int, default=8097)
    parser.add_argument("--skip-network", action="store_true", help="Do not call the cloud during setup.")
    parser.add_argument("--allow-missing-model", action="store_true", help="Finish even if the local model is not running.")
    parser.add_argument("--download", action="store_true", help="Download the llama.cpp build and the model.")
    parser.add_argument("--apply-install", action="store_true", help="Allow setup to run a package manager after you agree.")
    parser.add_argument("--binary", choices=["prebuilt", "source"], default="prebuilt")
    parser.add_argument("--connect", choices=["native", "bridges", "accounts", "skip"], default="", help="Native apps, loopback bridges, Edge account sessions, or none.")
    parser.add_argument("--bridge-kinds", default="", help="Comma-separated bridges: mail, meetings, tracker, cdp.")
    parser.add_argument("--bridge-ports", default="", help="Ports for mail, meetings, tracker, and cdp.")
    parser.add_argument("--accounts", default="", help="name=https://host/login pairs. Each one gets a headful Edge window.")
    parser.add_argument("--screen", default="1440x900", help="Desktop size used to park Edge in the lower right.")
    parser.add_argument("--open-accounts", action="store_true", help="Open Edge so you can sign in. Off unless you pass this.")


def package_command(os_name: str, distro: str, package: str) -> list[str]:
    if os_name == "darwin":
        return ["brew", "install", package]
    if os_name == "windows":
        return ["winget", "install", "--id", package]
    if distro == "fedora":
        return ["sudo", "dnf", "install", "-y", package]
    if distro == "arch":
        return ["sudo", "pacman", "-S", "--noconfirm", package]
    return ["sudo", "apt-get", "install", "-y", package]


def download_with_progress(url: str, dest: Path, io) -> None:
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1/") or url.startswith("http://127.0.0.1:")):
        raise RuntimeError("download URL must be https, or a loopback test address")
    dest.parent.mkdir(parents=True, exist_ok=True)

    def hook(count, block, total):
        if total and total > 0:
            io.progress(dest.name, min(1.0, (count * block) / total))
        else:
            io.progress(dest.name, 0)

    urllib.request.urlretrieve(url, dest, reporthook=hook)
    io.progress(dest.name, 1)


def _presets(args, automatic: bool) -> dict:
    if not automatic:
        presets = {}
        if args.mode:
            presets["mode"] = args.mode
        if args.provider:
            presets["provider"] = args.provider
        if args.base_url:
            presets["base_url"] = args.base_url
        if args.cloud_model:
            presets["cloud_model"] = args.cloud_model
        if args.model and args.model != "auto":
            presets["local_model"] = args.model
        if args.connect:
            presets["connect"] = args.connect
        return presets
    return {
        "mode": args.mode or "local",
        "provider": args.provider or "openai",
        "base_url": args.base_url or "",
        "cloud_model": args.cloud_model or "",
        "key": "env",
        "hardware_ok": "yes",
        "local_model": args.model or "auto",
        "binary": args.binary or "prebuilt",
        "web_port": str(args.web_port),
        "model_port": str(args.model_port),
        "embed_port": str(args.embed_port),
        "modules": args.modules,
        "connect": args.connect or "skip",
        "install_cmake": "no",
        "retry": "no",
        "resume": "no",
        "write_ok": "yes",
    }


class Wizard:
    def __init__(self, args, io, secrets=None) -> None:
        self.args = args
        self.io = io
        self.secrets = secrets or EnvAndKeychain()
        self.answers: dict = {}
        self.saved_keys: set[str] = set()
        self.resume = False
        self.status = 0
        self.state = Path(args.state_dir) if args.state_dir else config_dir()
        self.hw = None

    def run(self) -> int:
        self.state.mkdir(parents=True, exist_ok=True)
        progress = self.state / "setup-progress.yaml"
        if progress.exists() and not self.args.resume and not (self.args.yes or self.args.non_interactive):
            if self.io.confirm("resume", "Continue the last setup?", "Answers already given are kept. Keys are not stored in that file.", True):
                self.args.resume = True
        if self.args.resume and progress.exists():
            self.answers.update(config_mod.load_file(progress))
            self.saved_keys = set(self.answers)
            self.resume = True
            self.io.say("Resuming setup.")
        if "mode" not in self.answers:
            picked = self._step_mode()
            if picked == "back":
                self.io.say("Setup stopped.")
                return 2
        order = self._order()
        index = int(self.answers.get("_i") or 0) if self.resume else 0
        index = max(0, min(index, len(order)))
        while index < len(order):
            order = self._order()
            if index >= len(order):
                break
            name = order[index]
            try:
                result = getattr(self, "_step_" + name)()
            except StepFailed as exc:
                self.io.fail(exc.what, exc.how)
                self._log("failed " + name + ": " + exc.what)
                if self.args.yes or self.args.non_interactive:
                    return 2
                if not self.io.confirm("retry", "Retry this step?", "You can change the answer and try again.", True):
                    return 2
                continue
            if result == "back":
                index = max(0, index - 1)
                self.answers["_i"] = index
                self._save()
                continue
            index += 1
            self.answers["_i"] = index
            self._save()
        return self.status

    def _order(self) -> list[str]:
        mode = self.answers.get("mode") or "local"
        steps: list[str] = []
        if mode in ("api", "hybrid"):
            steps += ["provider", "base_url", "cloud_model", "key"]
        if mode in ("local", "hybrid"):
            steps += ["hardware", "local_model", "binary"]
        steps += ["ports", "modules", "connect", "prereqs", "validate", "write", "summary"]
        return steps

    def _remember(self, key: str, value: str) -> str:
        if value in ("back", "skip"):
            return value
        self.answers[key] = value
        self._save()
        return value

    def _already(self, key: str):
        if self.resume and key in self.saved_keys and key in self.answers:
            self.saved_keys.discard(key)
            return self.answers[key]
        return None

    def _step_mode(self) -> str:
        cached = self._already("mode")
        if cached:
            self.io.say("Mode: " + str(cached))
            return str(cached)
        self.io.say("HailPorts can run in three ways. You can change this later with hailports mode.")
        picked = self.io.choose(
            "mode",
            "How should this machine answer?",
            "Local uses a model on this computer. Hybrid asks a cloud model only when the work is hard or unsure. API skips the local model, so a small computer is enough.",
            MODES,
            self.args.mode or "local",
        )
        if picked == "back":
            return "back"
        self._remember("mode", picked)
        self._log("mode " + picked)
        return picked

    def _step_provider(self) -> str:
        cached = self._already("provider")
        if cached:
            return "ok"
        picked = self.io.choose(
            "provider",
            "Which hosted service should the cloud calls use?",
            "OpenAI-compatible covers OpenAI, xAI, OpenRouter, Groq, Together, and a server you run. Anthropic and Google have their own adapters. The key stays in the environment or the OS keychain.",
            PROVIDERS,
            self.args.provider or "openai",
        )
        if picked in ("back", "skip"):
            return picked
        self._remember("provider", picked)
        return "ok"

    def _step_base_url(self) -> str:
        cached = self._already("base_url")
        if cached is not None:
            return "ok"
        provider = self.answers.get("provider") or "openai"
        default = self.args.base_url or DEFAULTS.get(provider, DEFAULTS["compatible"])[0]
        picked = self.io.ask(
            "base_url",
            "Cloud base URL",
            "This is the root the adapter calls. Leave the default unless you were given a different address.",
            default,
        )
        if picked in ("back", "skip"):
            return picked
        if not (picked.startswith("http://") or picked.startswith("https://")):
            raise StepFailed("That address does not start with http:// or https://.", "Paste the full base URL, then retry.")
        self._remember("base_url", picked)
        return "ok"

    def _step_cloud_model(self) -> str:
        cached = self._already("cloud_model")
        if cached is not None:
            return "ok"
        provider = self.answers.get("provider") or "openai"
        default = self.args.cloud_model or DEFAULTS.get(provider, DEFAULTS["compatible"])[1]
        picked = self.io.ask(
            "cloud_model",
            "Cloud model name",
            "Use the model id the provider expects.",
            default,
        )
        if picked in ("back", "skip"):
            return picked
        if not picked.strip():
            raise StepFailed("The model name is empty.", "Type the model id from the provider, then retry.")
        self._remember("cloud_model", picked.strip())
        return "ok"

    def _step_key(self) -> str:
        cached = self._already("key_source")
        if cached:
            return "ok"
        picked = self.io.choose(
            "key",
            "Where should the API key be read from?",
            "The key is not written to the config file. Export HAILPORTS_API_KEY, or store it in the OS keychain under the hailports service.",
            [("env", "Read HAILPORTS_API_KEY from the environment."), ("keychain", "Read or save the OS keychain entry.")],
            "env",
        )
        if picked in ("back", "skip"):
            return picked
        cfg = self._cfg()
        if picked == "env":
            if self.secrets.get(cfg):
                self.io.say("Found the key in the environment.")
                self._remember("key_source", "env")
                self._log("key source env")
                return "ok"
            if self.args.skip_network:
                self.io.say("No key yet. Network checks are skipped.")
                self._remember("key_source", "env")
                return "ok"
            raise StepFailed(
                "HAILPORTS_API_KEY is empty.",
                "Export HAILPORTS_API_KEY in this terminal and retry. Do not paste the key into a config file.",
            )
        if self.secrets.get(cfg):
            self.io.say("Found the key in the OS keychain.")
            self._remember("key_source", "keychain")
            self._log("key source keychain")
            return "ok"
        if self.args.yes or self.args.non_interactive:
            raise StepFailed(
                "The OS keychain has no hailports cloud key.",
                "Store the key in the keychain, or go back and choose the environment variable.",
            )
        value = self.io.secret("Paste the key to store in the OS keychain. It is not written to disk.")
        if value.strip().lower() == "back":
            return "back"
        if not value.strip():
            raise StepFailed("No key was entered.", "Paste the key, or choose the environment variable instead.")
        try:
            self.secrets.set(cfg, value.strip())
        except Exception as exc:
            raise StepFailed("The keychain did not store the key. " + str(exc), "Use the environment variable, or install the OS keychain tool and retry.") from exc
        self._remember("key_source", "keychain")
        self._log("key stored in keychain")
        return "ok"

    def _step_hardware(self) -> str:
        self.hw = probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        gib = self.hw.ram_bytes / (1024 ** 3) if self.hw.ram_bytes else 0
        self.io.say(
            f"This machine looks like {self.hw.os_name} {self.hw.arch}, accelerator {self.hw.accel}, about {gib:.1f} GB RAM."
        )
        if self.hw.simulated:
            self.io.say("Those values were supplied for a dry plan. They are not a live probe.")
        cached = self._already("hardware_ok")
        if cached:
            return "ok"
        ok = self.io.confirm("hardware_ok", "Use this hardware picture?", "Setup uses it only to choose a model size.", True)
        if not ok:
            return "back"
        self._remember("hardware_ok", "yes")
        return "ok"

    def _step_local_model(self) -> str:
        cached = self._already("local_model")
        if cached:
            return "ok"
        picked = self.io.choose(
            "local_model",
            "Which local model size should setup plan for?",
            "Auto follows the memory above. Tiny is only for a plumbing test. Vision needs a lot of memory.",
            MODELS,
            self.args.model or "auto",
        )
        if picked in ("back", "skip"):
            return picked
        self._remember("local_model", picked)
        return "ok"

    def _step_binary(self) -> str:
        cached = self._already("binary")
        if cached:
            choice = str(cached)
        else:
            choice = self.io.choose(
                "binary",
                "How should llama.cpp be installed?",
                "The prebuilt file is enough for most machines. Build from source only when you need a different accelerator flag.",
                [("prebuilt", "Download a published build."), ("source", "Compile with cmake.")],
                self.args.binary or "prebuilt",
            )
            if choice in ("back", "skip"):
                return choice
            self._remember("binary", choice)
        if choice == "source" and shutil.which("cmake") is None:
            hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
            cmd = package_command(hw.os_name, hw.distro, "cmake")
            self.io.say("cmake was not found. Command: " + " ".join(cmd))
            agree = self.io.confirm("install_cmake", "Install cmake now?", "You can also go back and choose the prebuilt file.", False)
            if not agree:
                raise StepFailed("cmake is missing.", "Install cmake, or go back and choose the prebuilt binary.")
            if not self.args.apply_install:
                raise StepFailed(
                    "Setup will not run a package manager unless you pass --apply-install.",
                    "Run the command above yourself, or re-run setup with --apply-install.",
                )
            self.io.say("Running: " + " ".join(cmd))
            import subprocess
            subprocess.run(cmd, check=False)
            if shutil.which("cmake") is None:
                raise StepFailed("cmake is still missing.", "Install it with the vendor package, then retry.")
        if choice == "prebuilt" and self.args.download:
            hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
            binary = pick_binary(hw, self.args.llama_tag)
            dest = self.state / "downloads" / binary.filename
            self.io.say("Downloading " + binary.filename)
            try:
                download_with_progress(binary.url, dest, self.io)
            except Exception as exc:
                raise StepFailed("The download failed. " + str(exc), "Check the network and retry, or choose a source build.") from exc
            self.answers["binary_path"] = str(dest)
        elif choice == "prebuilt":
            self.io.say("The prebuilt file is not downloaded yet. Re-run with --download when you want it.")
        return "ok"

    def _step_ports(self) -> str:
        mode = self.answers.get("mode")
        web = self._port("web_port", "Web chat port", self.args.web_port)
        if web in ("back", "skip"):
            return web
        if mode in ("local", "hybrid"):
            model_port = self._port("model_port", "Local model port", self.args.model_port)
            if model_port in ("back", "skip"):
                return model_port
            embed = self._port("embed_port", "Embeddings port", self.args.embed_port)
            if embed in ("back", "skip"):
                return embed
        return "ok"

    def _port(self, key: str, title: str, default: int):
        cached = self._already(key)
        if cached is not None:
            port = int(cached)
        else:
            raw = self.io.ask(key, title, "Use a free port on this computer. Type back to return to the previous step.", str(default))
            if raw in ("back", "skip"):
                return raw
            try:
                port = int(raw)
            except ValueError as exc:
                raise StepFailed("That is not a port number.", "Use a number from 1 to 65535.") from exc
            if port < 1 or port > 65535:
                raise StepFailed("That port is outside 1 to 65535.", "Pick a free user port, such as 8080.")
            self._remember(key, str(port))
        from hailports.doctor import port_open
        if port_open("127.0.0.1", port):
            return "ok"
        if self.args.yes or self.args.non_interactive:
            nxt = port + 1
            while nxt < 65535 and not port_open("127.0.0.1", nxt):
                nxt += 1
            self.io.say(f"Port {port} is in use. Using {nxt}.")
            self.answers[key] = str(nxt)
            self._log(f"port {key} moved from {port} to {nxt}")
            return "ok"
        raise StepFailed(f"Port {port} is already in use.", "Pick another port, or stop the program using that port, then retry.")

    def _step_modules(self) -> str:
        cached = self._already("modules")
        if cached is not None:
            return "ok"
        picked = self.io.ask(
            "modules",
            "Which features should start enabled?",
            "Comma-separated. Choices: cli, webchat, imessage, bridges, notes, draft_runner, plugins. iMessage is ignored unless this is a Mac.",
            self.args.modules,
        )
        if picked in ("back", "skip"):
            return picked
        self._remember("modules", picked)
        return "ok"

    def _step_connect(self) -> str:
        cached = self._already("connect")
        if cached is not None:
            return "ok"
        automatic = bool(self.args.yes or self.args.non_interactive)
        default = self.args.connect or ("skip" if automatic else "native")
        picked = self.io.choose(
            "connect",
            "How should email, notes, and chats connect?",
            "Native records installed apps. Bridges stay on this computer. Accounts opens Microsoft Edge, headful, in the lower right, and your selected model writes how that page is read and drafted. Nothing is sent.",
            [
                ("native", "Mail, notes, and chats through the apps installed here."),
                ("bridges", "Loopback bridges. Your subscription drafts the config."),
                ("accounts", "Any account in its own Edge window, parked in the lower right."),
                ("skip", "Leave email, notes, and chats unconnected."),
            ],
            default,
        )
        if picked == "back":
            return "back"
        extra = str(self.args.accounts or "").strip()
        if picked == "skip" and not extra:
            self._remember("connect", "skip")
            self._log("connect skip")
            return "ok"
        if picked == "native":
            result = self._connect_native()
        elif picked == "bridges":
            result = self._connect_bridges()
        elif picked in ("accounts", "skip"):
            result = self._connect_accounts()
        else:
            self._remember("connect", "skip")
            result = "ok"
        if result == "ok" and extra and picked not in ("accounts", "skip"):
            return self._connect_accounts(attach=True)
        return result

    def _connect_native(self) -> str:
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        self.hw = hw
        found = detect_native(hw.os_name)
        for surface in ("email", "notes", "chats"):
            item = found.get(surface) or {}
            if item.get("enabled"):
                self.io.say(surface + ": " + str(item.get("app")) + " at " + str(item.get("path")))
            else:
                self.io.say(surface + ": " + str(item.get("reason") or "not found"))
        for line in permission_lines(hw.os_name, found):
            self.io.say(line)
        self._remember("connect", "native")
        self._remember("native_json", json.dumps(found))
        self._log("connect native")
        return "ok"

    def _connect_bridges(self) -> str:
        kinds_raw = self.io.ask(
            "bridge_kinds",
            "Which local bridges should turn on?",
            "Comma-separated: mail, meetings, tracker, cdp. back returns to the previous step.",
            self.args.bridge_kinds or "mail,meetings",
        )
        if kinds_raw == "back":
            return "back"
        if kinds_raw == "skip":
            self._remember("connect", "skip")
            self._log("connect skip")
            return "ok"
        try:
            enabled = parse_kinds(kinds_raw)
        except ValueError as exc:
            raise StepFailed(str(exc), "Use mail, meetings, tracker, or cdp.") from exc
        if not enabled:
            raise StepFailed("No bridge name was recognized.", "Use mail, meetings, tracker, or cdp.")
        ports_raw = self.io.ask(
            "bridge_ports",
            "Ports for mail, meetings, tracker, and cdp",
            "Four numbers, or one number used for all four. Each URL stays on 127.0.0.1.",
            self.args.bridge_ports or "9222,9223,9224,9222",
        )
        if ports_raw == "back":
            return "back"
        if ports_raw == "skip":
            self._remember("connect", "skip")
            return "ok"
        try:
            ports = parse_ports(ports_raw)
        except ValueError as exc:
            raise StepFailed(str(exc), "Use ports from 1 to 65535, for example 9222,9223,9224,9222.") from exc
        ready = self._bridge_subscription()
        if ready in ("back", "skip"):
            if ready == "skip":
                self._remember("connect", "skip")
            return ready
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        self.hw = hw
        if self.args.skip_network:
            plan = fallback_plan(hw.os_name, enabled, ports, "skipped")
            self.io.say("Subscription assist skipped. Wrote the ports from this step.")
        else:
            key = self.secrets.get(self._cfg()) or ""
            if not key:
                raise StepFailed(
                    "Bridge setup uses your own model subscription, and no key is set.",
                    "Export HAILPORTS_API_KEY from that subscription, or go back and choose native.",
                )
            cloud = build_cloud(self._cfg(), key)
            provider = str((self._cfg().get("cloud") or {}).get("provider") or "the configured provider")
            self.io.say("Asking " + provider + " to draft the bridge config. Mail, notes, and chats are not sent.")
            plan = assist_bridges(cloud, os_name=hw.os_name, enabled=enabled, ports=ports, secret=key)
            if plan.get("source") == "subscription":
                self.io.say("Bridge config came from your subscription.")
            else:
                self.io.say("The subscription reply was not used. Wrote the ports from this step.")
        for line in plan.get("steps") or []:
            self.io.say(line)
        self._remember("connect", "bridges")
        self._remember("bridge_kinds", ",".join(enabled))
        self._remember("bridge_plan_json", json.dumps({"bridges": plan["bridges"], "source": plan.get("source") or "defaults"}))
        self._log("connect bridges " + str(plan.get("source") or "defaults"))
        return "ok"

    def _connect_accounts(self, attach: bool = False) -> str:
        automatic = bool(self.args.yes or self.args.non_interactive)
        raw = str(self.args.accounts or "")
        if not automatic:
            raw = self.io.ask(
                "accounts",
                "Which accounts should sign in?",
                "Comma-separated name=https://host/login. Each account gets its own headful Edge window in the lower right. Eight at most. Edge is installed from Microsoft, not from this repo.",
                raw,
            )
            if raw == "back":
                return "back"
            if raw == "skip":
                if not attach:
                    self._remember("connect", "skip")
                return "ok"
        try:
            rows = parse_accounts(raw)
            screen = parse_screen(self.args.screen or "1440x900")
        except ValueError as exc:
            raise StepFailed(str(exc), "Use name=https://host/login and a screen like 1440x900.") from exc
        sessions = place_accounts(rows, screen)
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        self.hw = hw
        binary = find_edge(hw.os_name)
        record = edge_record(hw.os_name, getattr(hw, "distro", "") or "", binary)
        if not binary:
            self.io.say("Microsoft Edge is not installed. This repo does not include it.")
            self.io.say("Install command: " + record["install"])
            script = edge_install_script(hw.os_name, getattr(hw, "distro", "") or "")
            if script:
                script_path = self.state / "edge-install.sh"
                script_path.write_text(script, encoding="utf-8")
                script_path.chmod(0o755)
                self.io.say("Linux install steps: " + str(script_path))
            if not automatic:
                agree = self.io.confirm(
                    "install_edge",
                    "Install Microsoft Edge now?",
                    "Setup runs that command only when you also pass --apply-install.",
                    False,
                )
                if agree:
                    if not self.args.apply_install:
                        raise StepFailed(
                            "Setup will not run a package manager unless you pass --apply-install.",
                            "Run the command above yourself, or re-run setup with --apply-install.",
                        )
                    self._install_edge(hw, script)
                    binary = find_edge(hw.os_name)
                    record = edge_record(hw.os_name, getattr(hw, "distro", "") or "", binary)
        if sessions and not automatic and not self.args.skip_network:
            mode = str(self.answers.get("mode") or self.args.mode or "local")
            if mode == "local":
                self.io.say("The local model writes read and write lines when you run hailports accounts adapt.")
            else:
                key = self.secrets.get(self._cfg()) or ""
                if not key:
                    raise StepFailed(
                        "Account setup uses your selected model, and no key is set.",
                        "Export HAILPORTS_API_KEY, or use local mode and run hailports accounts adapt.",
                    )
                cloud = build_cloud(self._cfg(), key)
                sessions = assist_accounts(cloud, sessions, secret=key)
                self.io.say("The selected model wrote how each account is read and drafted. Nothing is sent.")
        elif sessions:
            self.io.say("Edge sessions are planned in the lower right. Run hailports accounts adapt, then hailports accounts open --apply. Nothing is sent.")
        if not attach:
            self._remember("connect", "accounts")
        self._remember("accounts_json", json.dumps({"sessions": sessions, "edge": record}))
        self._log("connect accounts " + str(len(sessions)))
        if self.args.open_accounts and binary and sessions:
            self.io.say("Opening Edge in the lower right. Sign in yourself. Setup does not type a password.")
            self._open_edge(sessions, binary)
        elif self.args.open_accounts and sessions and not binary:
            raise StepFailed("Edge is not installed, so the window was not opened.", "Install Edge, then run hailports accounts open --apply.")
        return "ok"

    def _install_edge(self, hw, script: str) -> None:
        import subprocess
        if script:
            script_path = self.state / "edge-install.sh"
            script_path.write_text(script, encoding="utf-8")
            script_path.chmod(0o755)
            subprocess.run(["/bin/sh", str(script_path)], check=False)
            return
        from hailports.accounts import edge_install_argv
        subprocess.run(edge_install_argv(hw.os_name, getattr(hw, "distro", "") or ""), check=False)

    def _open_edge(self, sessions: dict, binary: str) -> None:
        import subprocess
        for argv in open_plan(sessions, binary):
            if any(part == "--headless" or part.startswith("--headless=") for part in argv):
                raise StepFailed("Edge sessions are headful.", "Do not pass a headless flag.")
            subprocess.Popen(argv)

    def _bridge_subscription(self) -> str:
        """Make sure a provider the user named is in the answers. Local mode has not asked yet."""
        if "provider" not in self.answers:
            result = self._step_provider()
            if result in ("back", "skip"):
                return result
        if "base_url" not in self.answers:
            result = self._step_base_url()
            if result in ("back", "skip"):
                return result
        if "cloud_model" not in self.answers:
            result = self._step_cloud_model()
            if result in ("back", "skip"):
                return result
        if "key_source" not in self.answers:
            result = self._step_key()
            if result in ("back", "skip"):
                return result
        return "ok"

    def _step_prereqs(self) -> str:
        mode = self.answers.get("mode") or "local"
        problems = []
        if sys.version_info < (3, 10):
            problems.append("Python 3.10 or newer is required.")
        if mode == "api":
            self.io.say("API mode does not need a C++ compiler, a llama.cpp build, or a model download.")
        else:
            self.io.say("Local and hybrid modes can use a prebuilt llama.cpp. A compiler is only required for a source build.")
            if self.answers.get("binary") == "source" and shutil.which("cmake") is None:
                problems.append("cmake is not on PATH.")
        if problems:
            raise StepFailed(" ".join(problems), "Install the missing piece, or switch to api mode, then retry.")
        self.io.say("Prerequisites look fine for " + mode + " mode.")
        self._log("prereqs ok " + mode)
        return "ok"

    def _step_validate(self) -> str:
        mode = self.answers.get("mode") or "local"
        cfg = self._cfg()
        if mode in ("api", "hybrid") and not self.args.skip_network:
            router = build_router(cfg, self.secrets)
            if router.cloud is None:
                raise StepFailed("No cloud provider was built.", "Go back and set the provider, the base URL, and the model.")
            ok, detail = router.cloud.health()
            if not ok:
                raise StepFailed("The cloud endpoint did not answer. " + detail, "Check the base URL, the model name, and the API key, then retry.")
            self.io.say("Cloud endpoint answered.")
            self._log("cloud ok")
        elif mode in ("api", "hybrid"):
            self.io.say("Cloud check skipped.")
        if mode in ("local", "hybrid"):
            base = (cfg.get("llm") or {}).get("base_url") or ""
            up = bool(base) and local_health(str(base), timeout=2)
            if up:
                self.io.say("Local model answered.")
                self._log("local model ok")
            elif self.args.allow_missing_model or self.args.dry_run:
                self.io.say("Local model is not running yet. The config can still be written. Start it later with hailports services start.")
                self._log("local model missing, allowed")
            else:
                raise StepFailed(
                    "The local model did not answer at " + str(base) + ".",
                    "Download the model with --download, start llama-server, or re-run with --allow-missing-model. API mode skips this.",
                )
        return "ok"

    def _step_write(self) -> str:
        cfg = self._cfg()
        text = config_mod.dumps(cfg)
        secret = self.secrets.get(cfg) or ""
        if secret and len(secret) >= 6 and secret in text:
            raise StepFailed("Refusing to write the config because it contains the API key.", "Remove the key from the answers. Keys belong in the environment or the keychain.")
        cached = self._already("write_ok")
        if cached is None:
            ok = self.io.confirm("write_ok", "Write the config and service files now?", "Running setup again replaces these files. Notes are left in place.", True)
            if not ok:
                return "back"
            self._remember("write_ok", "yes")
        path = self.state / "config.yaml"
        config_mod.save_file(path, cfg)
        self._write_units(cfg)
        self.io.say("Wrote " + str(path))
        self._log("wrote config")
        return "ok"

    def _step_summary(self) -> str:
        cfg = self._cfg()
        checks = run_doctor(cfg, self.secrets, live=not self.args.skip_network)
        self.io.say("Health summary")
        self.io.say(format_report(checks))
        self.io.say(describe(cfg))
        self.io.say("Next commands:")
        self.io.say("  hailports doctor")
        self.io.say("  hailports chat")
        self.io.say("  hailports serve")
        self.io.say("  hailports mode")
        self.io.say("  hailports paiste setup")
        self._log("summary done")
        mode = self.answers.get("mode")
        hard = []
        for check in checks:
            if check.ok:
                continue
            if check.name == "local-model" and (self.args.allow_missing_model or mode == "api"):
                continue
            if check.name == "cloud" and self.args.skip_network:
                continue
            if check.name == "api-key" and self.args.skip_network:
                continue
            hard.append(check)
        if hard and failed(hard):
            self.io.say("Setup wrote the files. Some checks still need attention.")
            self.status = 1
            return "ok"
        self.io.say("Setup finished.")
        return "ok"

    def _cfg(self) -> dict:
        mode = self.answers.get("mode") or "local"
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        self.hw = hw
        preference = str(self.answers.get("local_model") or self.args.model or "auto")
        model = pick_model(hw, preference if mode != "api" else "tiny")
        modules = parse_modules(str(self.answers.get("modules") or self.args.modules), hw.os_name)
        label = self.args.label or f"com.hailports.{hw.os_name}"
        cfg = default_config(hw, model, modules, label, mode=mode)
        provider = str(self.answers.get("provider") or self.args.provider or "openai")
        base_default, model_default = DEFAULTS.get(provider, DEFAULTS["compatible"])
        cfg["cloud"] = {
            "provider": provider,
            "base_url": str(self.answers.get("base_url") or self.args.base_url or base_default),
            "model": str(self.answers.get("cloud_model") or self.args.cloud_model or model_default),
            "key_env": "HAILPORTS_API_KEY",
            "keychain_service": "hailports",
            "keychain_account": "cloud",
        }
        web_port = int(self.answers.get("web_port") or self.args.web_port)
        cfg["webchat"] = {"bind": "127.0.0.1", "port": web_port}
        if mode == "api":
            cfg["llm"] = {
                "base_url": "",
                "upstream_url": "",
                "model": "",
                "model_url": "",
                "mmproj_url": "",
                "quant": "",
            }
            cfg["notes"]["embed_url"] = ""
        else:
            model_port = int(self.answers.get("model_port") or self.args.model_port)
            embed_port = int(self.answers.get("embed_port") or self.args.embed_port)
            cfg["llm"]["base_url"] = f"http://127.0.0.1:{model_port}/v1"
            cfg["llm"]["upstream_url"] = f"http://127.0.0.1:{model_port}"
            cfg["notes"]["embed_url"] = f"http://127.0.0.1:{embed_port}"
        cfg["routing"] = {
            "confidence_floor": 0.55,
            "complexity_ceiling": 1,
            "max_local_output_chars": 900,
            "compact_char_budget": 1600,
        }
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        self.hw = hw
        apply_connection(cfg, self.answers, hw.os_name)
        raw = str(self.answers.get("paiste_json") or "")
        if raw:
            try:
                paiste = json.loads(raw)
            except json.JSONDecodeError:
                paiste = None
            if isinstance(paiste, dict):
                cfg["paiste"] = paiste
        return cfg

    def _write_units(self, cfg: dict) -> None:
        hw = self.hw or probe(self.args.target_os, self.args.arch, self.args.accel, self.args.ram_gb, self.args.vram_gb)
        label = str(cfg.get("label") or "com.hailports.local")
        work = str(self.state)
        mode = str(cfg.get("mode") or "local")
        embed_url = str(((cfg.get("notes") or {}).get("embed_url") or ""))
        embed_port = 8097
        if ":" in embed_url.rsplit("/", 1)[-1]:
            try:
                embed_port = int(embed_url.rsplit(":", 1)[-1])
            except ValueError:
                embed_port = 8097
        notes = bool(((cfg.get("modules") or {}).get("notes")))
        from hailports_setup.models import pick_model, server_args
        preference = str(self.answers.get("local_model") or self.args.model or "auto")
        choice = pick_model(hw, preference if mode != "api" else "tiny")
        model_port = 18080
        upstream = str(((cfg.get("llm") or {}).get("upstream_url") or ""))
        tail = upstream.rsplit(":", 1)[-1]
        if tail.isdigit():
            model_port = int(tail)
        model_cmd = ["llama-server", *server_args(
            "models/model.gguf",
            choice,
            port=model_port,
            mmproj_path="models/mmproj.gguf" if choice.vision else "",
        )]
        specs = appliance_specs(
            label, mode, work, sys.executable,
            model_cmd=model_cmd, embed_port=embed_port, notes=notes, model_port=model_port,
        )
        unit_root = self.state / "services"
        suffix = {"darwin": ".plist", "windows": ".xml"}.get(hw.os_name, ".service")
        for spec in specs:
            path = unit_root / f"{spec.kind}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(spec, hw.os_name), encoding="utf-8")
            self.io.say("service file " + spec.kind)
            hint = " ".join(install_hint(hw.os_name, spec.label, path))
            self._log("install hint " + hint)

    def _save(self) -> None:
        safe = {}
        for key, value in self.answers.items():
            if "key" == key or str(key).endswith("_secret"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
        config_mod.save_file(self.state / "setup-progress.yaml", safe)

    def _log(self, text: str) -> None:
        secret = ""
        try:
            secret = self.secrets.get(self._cfg()) or ""
        except Exception:
            secret = ""
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
        path = self.state / "setup.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")


def _dry_run(args) -> int:
    from hailports_setup.installer import _print_plan, build_plan
    if not getattr(args, "mode", None):
        args.mode = "local"
    plan = build_plan(args)
    _print_plan(plan)
    print("dry-run: no files written, no downloads, no services installed")
    return 0


def launch(args, secrets=None) -> int:
    if args.e2e_tiny:
        args.model = "tiny"
        args.non_interactive = True
        from hailports_setup.e2e import run_tiny
        return run_tiny(args)
    if args.dry_run:
        return _dry_run(args)
    automatic = bool(args.yes or args.non_interactive)
    io = YesIO(_presets(args, True)) if automatic else PlainIO(presets=_presets(args, False))
    try:
        return Wizard(args, io, secrets=secrets).run()
    except KeyboardInterrupt:
        print("Setup stopped.")
        return 130


def main(argv: list[str] | None = None, secrets=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hailports setup",
        description="Guided setup for local, hybrid, or API mode. Safe to run again.",
    )
    add_setup_args(parser)
    args = parser.parse_args(argv)
    return launch(args, secrets=secrets)
