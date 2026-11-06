"""Service files for launchd, systemd user units, and Windows Task Scheduler."""

from __future__ import annotations

import html
from pathlib import Path

from hailports.osapi import ServiceSpec
from hailports_setup.weight_preflight import wrap_model_command


def render(spec: ServiceSpec, os_name: str) -> str:
    if os_name == "darwin":
        return _plist(spec)
    if os_name == "windows":
        return _task(spec)
    return _systemd(spec)


def _plist(spec: ServiceSpec) -> str:
    args = "\n".join(f"        <string>{html.escape(part)}</string>" for part in spec.command)
    work = html.escape(spec.workdir)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{html.escape(spec.label)}</string>
    <key>WorkingDirectory</key>
    <string>{work}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def _plain(text: str) -> str:
    return " ".join(str(text).split())


def _systemd(spec: ServiceSpec) -> str:
    cmd = " ".join(_sh_quote(p) for p in spec.command)
    work = _sh_quote(spec.workdir)
    label = _plain(spec.label)
    return f"""[Unit]
Description={label} ({spec.kind})

[Service]
Type=simple
WorkingDirectory={work}
ExecStart={cmd}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _win_arg(text: str) -> str:
    if not text or any(ch in text for ch in ' \t"'):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _task(spec: ServiceSpec) -> str:
    cmd = spec.command[0]
    args = " ".join(_win_arg(part) for part in spec.command[1:])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{html.escape(cmd)}</Command>
      <Arguments>{html.escape(args)}</Arguments>
      <WorkingDirectory>{html.escape(spec.workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _sh_quote(text: str) -> str:
    if not text or any(c in text for c in " \t\"'"):
        return "'" + text.replace("'", "'\\''") + "'"
    return text


def appliance_specs(label: str, mode: str, work: str, python: str, model_cmd: list[str] | None = None, embed_port: int = 8097, notes: bool = True, model_port: int = 18080) -> list[ServiceSpec]:
    """Service units for the model, embeddings, web chat, sentinel, idle work, and the memory watchdog."""
    specs: list[ServiceSpec] = []
    cfg_file = str(Path(work) / "config.yaml")
    if mode != "api":
        default_model = [
            "llama-server", "-m", "models/model.gguf",
            "--host", "127.0.0.1", "--port", str(model_port),
        ]
        specs.append(ServiceSpec(
            label + ".model",
            wrap_model_command(python, model_cmd or default_model),
            work,
            "model",
        ))
        if notes:
            from hailports_setup.models import embed_server_args
            specs.append(ServiceSpec(
                label + ".embed",
                ["llama-server", *embed_server_args("models/bge-small-en-v1.5-q8_0.gguf", embed_port)],
                work,
                "embed",
            ))
    specs.extend([
        ServiceSpec(label + ".webchat", [python, "-m", "hailports", "--config", cfg_file, "serve"], work, "webchat"),
        ServiceSpec(label + ".sentinel", [python, "-m", "hailports.sentinel", "--config", cfg_file], work, "sentinel"),
        ServiceSpec(label + ".idle", [python, "-m", "hailports.idle", "--config", cfg_file], work, "idle"),
        ServiceSpec(label + ".memory", [python, "-m", "hailports.memory", "--config", cfg_file], work, "memory"),
    ])
    return specs


def install_hint(os_name: str, label: str, unit: Path) -> list[str]:
    # Placeholders, not a real account path. The installer fills these in at apply time.
    if os_name == "darwin":
        dest = "$HOME/Library/LaunchAgents/" + unit.name
        return ["cp", str(unit), dest, "&&", "launchctl", "bootstrap", "gui/$UID", dest]
    if os_name == "windows":
        return ["schtasks", "/Create", "/TN", label, "/XML", str(unit), "/F"]
    dest = "$HOME/.config/systemd/user/" + unit.name
    return ["cp", str(unit), dest, "&&", "systemctl", "--user", "daemon-reload", "&&", "systemctl", "--user", "enable", "--now", label]
