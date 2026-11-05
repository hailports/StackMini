"""Download a tiny GGUF and a prebuilt llama-server, then complete one chat turn."""

from __future__ import annotations

import os
import socket
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

from hailports.llm import ModelOffline, chat
from hailports.providers.openai_compat import OpenAICompatProvider
from hailports.router import Policy, Router
from hailports_setup.detect import probe
from hailports_setup.models import TINY_URL, pick_binary, pick_model


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"reuse {dest}")
        return
    print(f"download {url}")
    urllib.request.urlretrieve(url, dest)


def _safe_name(root: Path, name: str) -> None:
    if not name or name.startswith(("/", "\\")) or Path(name).is_absolute():
        raise RuntimeError("archive path is not safe")
    parts = Path(name).parts
    if ".." in parts:
        raise RuntimeError("archive path is not safe")
    target = (root / name).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise RuntimeError("archive path is not safe")


def _safe_link(name: str, link: str) -> None:
    if not link or link.startswith(("/", "\\")) or Path(link).is_absolute():
        raise RuntimeError("archive link is not safe")
    joined = os.path.normpath(str(Path(name).parent / link))
    if joined.startswith("..") or os.path.isabs(joined):
        raise RuntimeError("archive link is not safe")


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _safe_name(root, info.filename)
            zf.extractall(dest)
        return
    with tarfile.open(archive) as tf:
        members = []
        for member in tf.getmembers():
            _safe_name(root, member.name)
            if member.issym() or member.islnk():
                _safe_link(member.name, member.linkname or "")
            members.append(member)
        tf.extractall(dest, members=members)


def _find_server(root: Path) -> Path | None:
    names = ("llama-server", "llama-server.exe")
    for path in root.rglob("*"):
        if path.name in names and path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
            return path
    return None


def run_tiny(args) -> int:
    hw = probe("linux" if not args.target_os else args.target_os, args.arch, "cpu", args.ram_gb, args.vram_gb)
    if hw.os_name != "linux":
        print("tiny end-to-end proof runs on the Linux machine in front of you")
        return 2
    state = Path(args.state_dir) if args.state_dir else Path("/tmp/hailports-e2e")
    state.mkdir(parents=True, exist_ok=True)
    binary = pick_binary(hw, args.llama_tag)
    archive = state / binary.filename
    _download(binary.url, archive)
    _extract(archive, state / "llama")
    server = _find_server(state / "llama")
    if server is None:
        print("llama-server was not in the archive")
        return 3
    model_path = state / "stories260K.gguf"
    _download(TINY_URL, model_path)
    choice = pick_model(hw, "tiny")
    port = _free_port()
    cmd = [str(server), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(port),
           "-c", "512", "-np", "1", "-t", "2"]
    print("start " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 90
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                print("server exited early")
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.4)
        if not up:
            print("server did not become healthy")
            return 4
        try:
            result = chat(f"http://127.0.0.1:{port}/v1", choice.name, "Say hello in one short sentence.", timeout=60)
        except ModelOffline as exc:
            print("chat failed: " + str(exc))
            return 5
        print("CHAT " + result.text.strip())
        local = OpenAICompatProvider("local", f"http://127.0.0.1:{port}/v1", choice.name, api_key=None)
        routed = Router("local", local, None, Policy())
        reply = routed.complete([{"role": "user", "content": "Say hello in one short sentence."}], max_tokens=32)
        print(reply.render())
        if "provider=local" not in reply.stats.line() or reply.stats.mode != "local":
            print("stats line missing")
            return 6
        print("e2e-ok")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
