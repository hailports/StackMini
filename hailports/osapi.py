"""OS process and service helpers. Core code calls this, not launchctl or bash."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServiceSpec:
    label: str
    command: list[str]
    workdir: str
    kind: str  # model, webchat, notes, sentinel


def current_os() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def process_rss_mb(pid: int) -> float | None:
    """Resident memory for one process id, in megabytes. None when it cannot be read."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        status = Path(f"/proc/{pid}/status")
        try:
            for line in status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
        except (OSError, ValueError, IndexError):
            return None
        return None
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True, timeout=5).strip()
            return int(out) / 1024.0
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return None


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)) + "\n", encoding="utf-8")


class ServiceManager:
    """Build service-manager commands. Restart goes through the manager, never pkill."""

    def __init__(self, os_name: str, apply: bool = False) -> None:
        self.os_name = os_name
        self.apply = apply
        self.calls: list[list[str]] = []

    def unit_path(self, label: str, state_dir: Path) -> Path:
        safe = label.replace(" ", "-")
        if self.os_name == "darwin":
            return state_dir / "launchd" / f"{safe}.plist"
        if self.os_name == "windows":
            return state_dir / "windows" / f"{safe}.xml"
        return state_dir / "systemd" / f"{safe}.service"

    def restart_command(self, label: str) -> list[str]:
        if self.os_name == "darwin":
            uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
            return ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
        if self.os_name == "windows":
            return ["schtasks", "/Run", "/TN", label]
        return ["systemctl", "--user", "restart", label]

    def start_command(self, label: str) -> list[str]:
        if self.os_name == "darwin":
            uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
            return ["launchctl", "kickstart", f"gui/{uid}/{label}"]
        if self.os_name == "windows":
            return ["schtasks", "/Run", "/TN", label]
        return ["systemctl", "--user", "start", label]

    def stop_command(self, label: str) -> list[str]:
        if self.os_name == "darwin":
            uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
            return ["launchctl", "bootout", f"gui/{uid}/{label}"]
        if self.os_name == "windows":
            return ["schtasks", "/End", "/TN", label]
        return ["systemctl", "--user", "stop", label]

    def run(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if not self.apply:
            return "planned"
        if shutil.which(argv[0]) is None and argv[0] not in ("schtasks",):
            return "manager-missing"
        try:
            subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return "manager-error"
        return "ran"


def restart_exact(mgr: ServiceManager, label: str, recorded_pid: int | None, current_pid: int | None, healthy: bool) -> str:
    """Heal only when the recorded PID is the service PID, or the service is already gone.

    A different live PID is left alone. The restart itself is a service-manager command.
    """
    if healthy:
        return "ok"
    if recorded_pid and current_pid and recorded_pid != current_pid:
        return "refused-pid-mismatch"
    if recorded_pid and current_pid and recorded_pid == current_pid:
        mgr.run(mgr.restart_command(label))
        return "restarted"
    if current_pid and not recorded_pid:
        return "refused-no-record"
    mgr.run(mgr.start_command(label))
    return "started"


def memory_bytes() -> int | None:
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb * 1024
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=5).strip()
            return int(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    if sys.platform == "win32":
        try:
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = M()
            stat.dwLength = ctypes.sizeof(M)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
        except Exception:
            return None
    return None


def cpu_count() -> int:
    return os.cpu_count() or 1
