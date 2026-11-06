"""Ask in-process, or build the curl line another machine would run.

SOCKS is added only when a proxy is passed in, or when 127.0.0.1 is already
listening on the probe port. Nothing here requires a tailscale binary.
"""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.request

from hailports.brain.ask import ask


def socks_if_present(port: int = 1055, host: str = "127.0.0.1", timeout: float = 0.2) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return f"{host}:{port}"
    except OSError:
        return None


def curl_command(
    base_url: str,
    question: str,
    *,
    scope: str = "all",
    k: int = 8,
    graph: bool = False,
    token: str = "",
    socks: str | None = None,
) -> list[str]:
    body = json.dumps({"q": question, "scope": scope, "k": int(k), "graph": bool(graph)})
    command = ["curl", "-sS", "-m", "30", "-H", "Content-Type: application/json"]
    if socks:
        command.extend(["--socks5-hostname", socks])
    if token:
        command.extend(["-H", f"Authorization: Bearer {token}"])
    command.extend(["-d", body, base_url.rstrip("/") + "/v1/ask"])
    return command


def ask_http(base_url: str, question: str, *, scope: str = "all", k: int = 8, graph: bool = False, token: str = "", socks: str | None = None, timeout: float = 30) -> dict:
    command = curl_command(base_url, question, scope=scope, k=k, graph=graph, token=token, socks=socks)
    # curl's -m is the deadline. Keep the subprocess timeout a step longer.
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 2, check=False)
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "curl failed")
    return json.loads(completed.stdout or "{}")


def ask_local(con, question: str, **kwargs) -> dict:
    return ask(con, question, **kwargs)


def ask_url(url: str, question: str, *, scope: str = "all", k: int = 8, graph: bool = False, token: str = "", timeout: float = 30) -> dict:
    """POST /v1/ask with the standard library. No SOCKS. Use ask_http when a proxy is required."""
    payload = json.dumps({"q": question, "scope": scope, "k": k, "graph": graph}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url.rstrip("/") + "/v1/ask", data=payload, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
