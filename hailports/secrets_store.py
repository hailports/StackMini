"""API keys live in the environment or the OS keychain. Never in a config file."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


DEFAULT_ENV = "HAILPORTS_API_KEY"
SERVICE = "hailports"
ACCOUNT = "cloud"


def _cloud(cfg: dict) -> dict:
    block = cfg.get("cloud") or {}
    return block if isinstance(block, dict) else {}


def env_name(cfg: dict) -> str:
    name = str(_cloud(cfg).get("key_env") or DEFAULT_ENV).strip()
    return name or DEFAULT_ENV


def service_name(cfg: dict) -> str:
    return str(_cloud(cfg).get("keychain_service") or SERVICE).strip() or SERVICE


def account_name(cfg: dict) -> str:
    return str(_cloud(cfg).get("keychain_account") or ACCOUNT).strip() or ACCOUNT


class MemorySecrets:
    """In-memory stand-in for tests. Same get/set shape as the OS store."""

    def __init__(self, env: dict | None = None, store: dict | None = None) -> None:
        self.env = env if env is not None else {}
        self.store = store if store is not None else {}

    def get(self, cfg: dict) -> str | None:
        value = self.env.get(env_name(cfg))
        if value:
            return str(value)
        saved = self.store.get((service_name(cfg), account_name(cfg)))
        return str(saved) if saved else None

    def set(self, cfg: dict, value: str) -> str:
        if not value or not str(value).strip():
            raise ValueError("empty key")
        self.store[(service_name(cfg), account_name(cfg))] = str(value).strip()
        return "keychain"


class EnvAndKeychain:
    def get(self, cfg: dict) -> str | None:
        value = os.environ.get(env_name(cfg))
        if value:
            return value.strip() or None
        return keychain_get(service_name(cfg), account_name(cfg))

    def set(self, cfg: dict, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("empty key")
        keychain_set(service_name(cfg), account_name(cfg), value.strip())
        return "keychain"


def keychain_get(service: str, account: str) -> str | None:
    if os.environ.get("HAILPORTS_NO_KEYCHAIN") == "1":
        return None
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return out.strip() or None
        if sys.platform == "win32":
            return _win_get(service, account)
        if shutil.which("secret-tool"):
            out = subprocess.check_output(
                ["secret-tool", "lookup", "service", service, "account", account],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return out.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def keychain_set(service: str, account: str, value: str) -> None:
    if os.environ.get("HAILPORTS_NO_KEYCHAIN") == "1":
        raise OSError("keychain disabled")
    if sys.platform == "darwin":
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return
    if sys.platform == "win32":
        _win_set(service, account, value)
        return
    if not shutil.which("secret-tool"):
        raise OSError("secret-tool is not installed")
    subprocess.run(
        ["secret-tool", "store", "--label", service, "service", service, "account", account],
        input=value,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _win_target(service: str, account: str) -> str:
    return service + "/" + account


def _win_get(service: str, account: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    cred_ptr = ctypes.POINTER(CREDENTIAL)()
    adv = ctypes.windll.advapi32
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredFree.argtypes = [ctypes.c_void_p]
    ok = adv.CredReadW(_win_target(service, account), 1, 0, ctypes.byref(cred_ptr))
    if not ok or not cred_ptr:
        return None
    try:
        blob = ctypes.string_at(cred_ptr.contents.CredentialBlob, cred_ptr.contents.CredentialBlobSize)
        return blob.decode("utf-16-le") or None
    finally:
        adv.CredFree(cred_ptr)


def _win_set(service: str, account: str, value: str) -> None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    encoded = value.encode("utf-16-le")
    buf = ctypes.create_string_buffer(encoded)
    cred = CREDENTIAL()
    cred.Type = 1
    cred.TargetName = _win_target(service, account)
    cred.CredentialBlobSize = len(encoded)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    cred.Persist = 2
    cred.UserName = account
    adv = ctypes.windll.advapi32
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError("credential manager refused the key")
