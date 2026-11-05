"""Pick a published GGUF that fits the machine, and the matching llama.cpp binary."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hailports_setup.detect import Hardware

# Reference vision weights. Byte size matches the published file.
VISION_URL = (
    "https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B-gguf/resolve/main/"
    "MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf"
)
VISION_MMPROJ = (
    "https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B-gguf/resolve/main/"
    "mmproj-model-f16-4x.gguf"
)
TINY_URL = "https://huggingface.co/ggml-org/models-moved/resolve/main/tinyllamas/stories260K.gguf"
SMALL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
    "qwen2.5-1.5b-instruct-q4_k_m.gguf"
)
BALANCED_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/"
    "qwen2.5-7b-instruct-q4_k_m.gguf"
)

LLAMA_TAG = "b12078"
LLAMA_RELEASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"

# Commit that built the reference Metal server. Installer prebuilts are newer.
REFERENCE_COMMIT = "f174553e1621c6a22a64e7bfcbb58d3a5c49e6fb"


@dataclass
class ModelChoice:
    name: str
    url: str
    mmproj_url: str
    quant: str
    reason: str
    ctx: int
    vision: bool


@dataclass
class BinaryChoice:
    tag: str
    url: str
    filename: str
    backend: str
    source_flags: list[str]


def _gib(n: int) -> float:
    return n / (1024 ** 3)


def pick_model(hw: Hardware, preference: str = "auto") -> ModelChoice:
    if preference == "tiny":
        return ModelChoice("stories260K", TINY_URL, "", "f16", "explicit tiny model", 512, False)
    ram = _gib(hw.ram_bytes)
    vram = _gib(hw.vram_bytes)
    pool = max(ram, vram)
    if preference == "vision" or (preference == "auto" and pool >= 20 and hw.accel in ("metal", "cuda")):
        return ModelChoice(
            "MiniCPM-V-4.7-35B-A3B",
            VISION_URL,
            VISION_MMPROJ,
            "mix_q3gud_im",
            "unified or GPU memory can hold the 3-bit mix plus a vision projector",
            16384,
            True,
        )
    if preference == "small" or pool < 8:
        return ModelChoice("Qwen2.5-1.5B-Instruct", SMALL_URL, "", "Q4_K_M", "under 8 GB", 4096, False)
    if preference == "balanced" or pool < 20:
        return ModelChoice("Qwen2.5-7B-Instruct", BALANCED_URL, "", "Q4_K_M", "8 to 20 GB, text model", 8192, False)
    return ModelChoice(
        "MiniCPM-V-4.7-35B-A3B",
        VISION_URL,
        VISION_MMPROJ,
        "mix_q3gud_im",
        "enough memory for the reference vision quant",
        16384,
        True,
    )


def _asset(hw: Hardware) -> tuple[str, str]:
    os_name, arch, accel = hw.os_name, hw.arch, hw.accel
    if os_name == "darwin":
        name = f"llama-{LLAMA_TAG}-bin-macos-{'arm64' if arch == 'arm64' else 'x64'}.tar.gz"
        return name, "metal"
    if os_name == "windows":
        if accel == "cuda" and arch == "x64":
            return f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip", "cuda"
        if accel == "vulkan":
            suffix = "arm64" if arch == "arm64" else "x64"
            return f"llama-{LLAMA_TAG}-bin-win-vulkan-{suffix}.zip", "vulkan"
        if accel == "rocm" and arch == "x64":
            return f"llama-{LLAMA_TAG}-bin-win-rocm-10.0-x64.zip", "rocm"
        suffix = "arm64" if arch == "arm64" else "x64"
        return f"llama-{LLAMA_TAG}-bin-win-cpu-{suffix}.zip", "cpu"
    # linux family shares the ubuntu glibc builds
    if accel == "cuda" and arch == "x64":
        return f"llama-{LLAMA_TAG}-bin-ubuntu-cuda-12.8-x64.tar.gz", "cuda"
    if accel == "rocm" and arch == "x64":
        return f"llama-{LLAMA_TAG}-bin-ubuntu-rocm-10.0-x64.tar.gz", "rocm"
    if accel == "vulkan":
        suffix = "arm64" if arch == "arm64" else "x64"
        return f"llama-{LLAMA_TAG}-bin-ubuntu-vulkan-{suffix}.tar.gz", "vulkan"
    if arch == "arm64":
        return f"llama-{LLAMA_TAG}-bin-ubuntu-arm64.tar.gz", "cpu"
    return f"llama-{LLAMA_TAG}-bin-ubuntu-x64.tar.gz", "cpu"


def pick_binary(hw: Hardware, tag: str = LLAMA_TAG) -> BinaryChoice:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", tag or ""):
        raise ValueError("llama release tag must be a plain release name")
    filename, backend = _asset(hw)
    # Rebuild the name if the caller overrode the tag.
    filename = filename.replace(LLAMA_TAG, tag)
    url = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{filename}"
    return BinaryChoice(tag=tag, url=url, filename=filename, backend=backend, source_flags=source_flags(hw))


def source_flags(hw: Hardware) -> list[str]:
    flags = ["-DGGML_NATIVE=ON", "-DLLAMA_BUILD_SERVER=ON", "-DCMAKE_BUILD_TYPE=Release"]
    if hw.os_name == "darwin":
        flags += ["-DGGML_METAL=ON", "-DGGML_METAL_EMBED_LIBRARY=ON", "-DGGML_BLAS=ON", "-DGGML_BLAS_VENDOR=Apple",
                  "-DGGML_CUDA=OFF", "-DGGML_VULKAN=OFF"]
    elif hw.accel == "cuda":
        flags += ["-DGGML_CUDA=ON", "-DGGML_METAL=OFF"]
    elif hw.accel == "rocm":
        flags += ["-DGGML_HIP=ON", "-DGGML_CUDA=OFF", "-DGGML_METAL=OFF"]
    elif hw.accel == "vulkan":
        flags += ["-DGGML_VULKAN=ON", "-DGGML_CUDA=OFF", "-DGGML_METAL=OFF"]
    else:
        flags += ["-DGGML_CUDA=OFF", "-DGGML_VULKAN=OFF", "-DGGML_METAL=OFF"]
    return flags


def embed_server_args(model_path: str, port: int = 8097) -> list[str]:
    """Flags for a local bge-small embeddings server.

    The weight file is bge-small-en-v1.5-q8_0.gguf. It is not shipped here.
    """
    return [
        "-m", model_path,
        "--embeddings",
        "--pooling", "mean",
        "-ngl", "99",
        "-c", "4096",
        "-np", "8",
        "-b", "4096",
        "-ub", "512",
        "-t", "8",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]


def server_args(model_path: str, choice: ModelChoice, port: int = 18080, mmproj_path: str = "") -> list[str]:
    args = [
        "-m", model_path,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-c", str(choice.ctx),
        "-np", "1",
        "--jinja",
    ]
    if choice.vision:
        args += ["-ngl", "99", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--metrics", "--n-cpu-moe", "0",
                 "--cache-ram", "512", "--prio", "2"]
        if mmproj_path:
            args += ["--mmproj", mmproj_path]
    return args
