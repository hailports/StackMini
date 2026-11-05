"""Hardware and accelerator detection, with a simulated mode for dry runs."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hailports.osapi import cpu_count, current_os, memory_bytes


@dataclass
class Hardware:
    os_name: str
    arch: str
    ram_bytes: int
    vram_bytes: int
    accel: str
    cores: int
    simulated: bool
    distro: str


def _arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def _distro() -> str:
    path = "/etc/os-release"
    if not os.path.exists(path):
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace").lower()
    for key in ("fedora", "arch", "debian", "ubuntu", "rhel", "centos"):
        if key in text:
            return key
    return ""


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def detect_accel(os_name: str) -> tuple[str, int]:
    if os_name == "darwin":
        return "metal", 0
    if _have("nvidia-smi"):
        vram = _nvidia_vram()
        return "cuda", vram
    if _have("rocminfo") or os.path.exists("/dev/kfd"):
        return "rocm", 0
    if _have("vulkaninfo"):
        return "vulkan", 0
    return "cpu", 0


def _nvidia_vram() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            timeout=5,
        )
        first = out.strip().splitlines()[0]
        return int(float(first.split()[0])) * 1024 * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def probe(target_os: str | None = None, arch: str | None = None, accel: str | None = None,
          ram_gb: float | None = None, vram_gb: float | None = None) -> Hardware:
    actual = current_os()
    os_name = target_os or actual
    simulated = bool(target_os and target_os != actual)
    if ram_gb is not None:
        ram = int(ram_gb * (1024 ** 3))
    elif not simulated:
        ram = memory_bytes() or 0
    else:
        ram = 16 * (1024 ** 3)
    if vram_gb is not None:
        vram = int(vram_gb * (1024 ** 3))
    else:
        vram = 0
    if accel:
        chosen = accel
    elif simulated:
        chosen = {"darwin": "metal", "windows": "cpu", "linux": "cpu"}[os_name]
    else:
        chosen, seen = detect_accel(os_name)
        if vram == 0:
            vram = seen
    return Hardware(
        os_name=os_name,
        arch=arch or (_arch() if not simulated else ("arm64" if os_name == "darwin" else "x64")),
        ram_bytes=ram,
        vram_bytes=vram,
        accel=chosen,
        cores=cpu_count(),
        simulated=simulated,
        distro=_distro() if os_name == "linux" and not simulated else "",
    )
