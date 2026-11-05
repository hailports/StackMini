"""Guided setup. Safe to run again. Dry-run prints the plan and writes nothing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hailports import config as config_mod
from hailports.paths import config_dir, data_dir
from hailports_setup.detect import probe
from hailports_setup.models import pick_binary, pick_model, server_args
from hailports_setup.services import appliance_specs, install_hint, render


MODULES = ("cli", "webchat", "imessage", "bridges", "notes", "draft_runner", "plugins")


def default_config(hw, model, modules: dict[str, bool], label: str, mode: str = "local") -> dict:
    return {
        "label": label,
        "mode": mode if mode in ("local", "hybrid", "api") else "local",
        "llm": {
            "base_url": "http://127.0.0.1:18080/v1",
            "upstream_url": "http://127.0.0.1:18080",
            "model": model.name,
            "model_url": model.url,
            "mmproj_url": model.mmproj_url or "",
            "quant": model.quant,
        },
        "webchat": {"bind": "127.0.0.1", "port": 8080},
        "notes": {"bind": "127.0.0.1", "port": 8078, "embed_url": "http://127.0.0.1:8097", "max_age_s": 86400},
        "modules": modules,
        "memory": {"budget_mb": 0},
        "runbooks": {"dir": "runbooks"},
        "web": {"search_url": "", "news_url": ""},
        "alerts": {"probe_url": "", "private_url": ""},
        "idle": {"interval_s": 120},
        "thread": {"summary_chars": 2400},
        "plugins": {
            "crm": {"base_url": "https://crm.example.invalid", "token": "CHANGE_ME"},
            "tracker": {"base_url": "https://tracker.example.invalid", "token": "CHANGE_ME", "project": "PROJECT"},
            "mail": {"base_url": "https://mail.example.invalid", "token": "CHANGE_ME"},
            "meetings": {"base_url": "https://meet.example.invalid", "token": "CHANGE_ME"},
            "files": {"base_url": "https://files.example.invalid", "token": "CHANGE_ME"},
        },
        "bridges": {
            "telegram": {"token": "CHANGE_ME", "chat_id": "CHANGE_ME"},
            "signal": {"base_url": "http://127.0.0.1:8088", "account": "CHANGE_ME"},
            "matrix": {"homeserver": "https://matrix.example.invalid", "token": "CHANGE_ME", "room": "CHANGE_ME"},
        },
        "imessage": {"allowed_handle": "CHANGE_ME"},
        "accel": hw.accel,
        "os": hw.os_name,
        "cloud": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "key_env": "HAILPORTS_API_KEY",
            "keychain_service": "hailports",
            "keychain_account": "cloud",
        },
        "routing": {
            "confidence_floor": 0.55,
            "complexity_ceiling": 1,
            "max_local_output_chars": 900,
            "compact_char_budget": 1600,
        },
    }


def parse_modules(text: str, os_name: str) -> dict[str, bool]:
    wanted = {p.strip() for p in text.split(",") if p.strip()}
    out = {name: name in wanted for name in MODULES}
    if out.get("imessage") and os_name != "darwin":
        out["imessage"] = False
    return out


def build_plan(args: argparse.Namespace) -> dict:
    hw = probe(args.target_os, args.arch, args.accel, args.ram_gb, args.vram_gb)
    model = pick_model(hw, args.model)
    binary = pick_binary(hw, args.llama_tag)
    modules = parse_modules(args.modules, hw.os_name)
    label = args.label or f"com.hailports.{hw.os_name}"
    mode = getattr(args, "mode", None) or "local"
    cfg = default_config(hw, model, modules, label, mode=mode)
    work = str(data_dir())
    model_cmd = ["llama-server", *server_args("models/model.gguf", model, mmproj_path="models/mmproj.gguf" if model.vision else "")]
    specs = appliance_specs(
        label,
        mode,
        work,
        sys.executable,
        model_cmd=model_cmd,
        embed_port=8097,
        notes=bool(modules.get("notes")),
    )
    units = {spec.label: render(spec, hw.os_name) for spec in specs}
    return {"hw": hw, "model": model, "binary": binary, "config": cfg, "units": units, "specs": specs}


def _print_plan(plan: dict) -> None:
    hw = plan["hw"]
    model = plan["model"]
    binary = plan["binary"]
    gib = hw.ram_bytes / (1024 ** 3) if hw.ram_bytes else 0
    mode = plan["config"].get("mode") or "local"
    print(f"mode: {mode}")
    print(f"os: {hw.os_name} arch: {hw.arch} accel: {hw.accel} ram_gib: {gib:.1f} simulated: {hw.simulated}")
    if hw.distro:
        print(f"distro: {hw.distro}")
    if mode == "api":
        print("model: skipped")
        print("reason: api mode does not download or build a local model")
        print("llama_binary: skipped")
    else:
        print(f"model: {model.name} quant: {model.quant} vision: {model.vision}")
        print(f"reason: {model.reason}")
        print(f"model_url: {model.url}")
        if model.mmproj_url:
            print(f"mmproj_url: {model.mmproj_url}")
        print(f"llama_binary: {binary.url}")
        print("source_build: cmake -B build " + " ".join(binary.source_flags))
    print("modules: " + ",".join(k for k, v in plan["config"]["modules"].items() if v))
    for spec in plan["specs"]:
        print(f"service: {spec.label} kind: {spec.kind}")
        hint = install_hint(hw.os_name, spec.label, Path(spec.label))
        print("install: " + " ".join(hint))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup.py", description="Set up the local model appliance")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--target-os", choices=["linux", "darwin", "windows"])
    parser.add_argument("--arch", choices=["x64", "arm64"])
    parser.add_argument("--accel", choices=["metal", "cuda", "rocm", "vulkan", "cpu"])
    parser.add_argument("--ram-gb", type=float)
    parser.add_argument("--vram-gb", type=float)
    parser.add_argument("--modules", default="cli,webchat,notes")
    parser.add_argument("--label")
    parser.add_argument("--model", default="auto", choices=["auto", "tiny", "small", "balanced", "vision"])
    parser.add_argument("--mode", default="local", choices=["local", "hybrid", "api"])
    parser.add_argument("--llama-tag", default="b12078")
    parser.add_argument("--e2e-tiny", action="store_true")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)

    if args.e2e_tiny:
        args.model = "tiny"
        args.non_interactive = True
        from hailports_setup.e2e import run_tiny
        return run_tiny(args)

    interactive = not args.non_interactive and not args.dry_run and sys.stdin.isatty()
    if interactive:
        print("Press enter to keep the default shown in brackets.")
        mode = input(f"Mode local/hybrid/api [{args.mode}]: ").strip()
        if mode in ("local", "hybrid", "api"):
            args.mode = mode
        mods = input(f"Modules [{args.modules}]: ").strip()
        if mods:
            args.modules = mods
        label = input("Service label [com.hailports.local]: ").strip()
        if label:
            args.label = label
        choice = input("Model auto/tiny/small/balanced/vision [auto]: ").strip()
        if choice in ("auto", "tiny", "small", "balanced", "vision"):
            args.model = choice

    plan = build_plan(args)
    _print_plan(plan)
    if args.dry_run:
        print("dry-run: no files written, no downloads, no services installed")
        return 0

    if not args.yes and not args.non_interactive:
        print("Pass --yes or --non-interactive to write config and service files.")
        return 2

    cfg_path = Path(args.state_dir) if args.state_dir else config_dir()
    cfg_path.mkdir(parents=True, exist_ok=True)
    config_mod.save_file(cfg_path / "config.yaml", plan["config"])
    unit_root = cfg_path / "services"
    for spec in plan["specs"]:
        text = plan["units"][spec.label]
        path = unit_root / spec.kind
        suffix = {"darwin": ".plist", "windows": ".xml"}.get(plan["hw"].os_name, ".service")
        path = unit_root / f"{spec.kind}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print(f"wrote {cfg_path / 'config.yaml'}")
    print("Rerun is safe. Existing files are replaced with this plan.")
    return 0
