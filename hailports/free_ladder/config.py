"""Free-ladder config, keys, and static rung defaults.

Keys load from ~/.config/free-llm/keys.env at runtime. Never log or print them.
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "1.0.0"
LADDER_PORT = int(os.environ.get("FREE_LADDER_PORT", "8790"))
STACK = Path(os.environ.get("HAILPORTS_HOME", str(Path(__file__).resolve().parents[2])))
KEYS_ENV = Path(os.environ.get("FREE_LLM_KEYS", str(Path.home() / ".config/free-llm/keys.env")))
RUNTIME = STACK / "data" / "runtime" / "free_ladder"
LOG_DIR = STACK / "data" / "logs" / "free_ladder"
QUOTA_DB = RUNTIME / "quota.sqlite"
CALL_LOG = LOG_DIR / "calls.jsonl"
DAILY_SUMMARY = LOG_DIR / "daily_summary.jsonl"
OR_FREE_CACHE = RUNTIME / "openrouter_free.json"

LOCAL_BASE = os.environ.get("FREE_LADDER_LOCAL_URL", "http://127.0.0.1:8080/v1").rstrip("/")
TAILSCALE_IP = os.environ.get("FREE_LADDER_TS_IP", "").strip()
BIND_HOSTS = [
    h.strip()
    for h in os.environ.get("FREE_LADDER_BIND", "127.0.0.1" + (f",{TAILSCALE_IP}" if TAILSCALE_IP else "")).split(",")
    if h.strip()
]

# Provider OpenAI-compatible bases
PROVIDERS = {
    "local": {"base": LOCAL_BASE, "key_env": None, "rung": 0},
    "cerebras": {"base": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY", "rung": 1},
    "groq": {"base": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "rung": 2},
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "rung": 3},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "rung": 4},
    "plugsky": {"base": "https://api.plugsky.com/v1", "key_env": "PLUGSKY_API_KEY", "rung": 5},
    "glm": {"base": "https://api.z.ai/api/coding/paas/v4", "key_env": "ZAI_API_KEY", "rung": 6},
    "paid": {"base": None, "key_env": None, "rung": 6},
}

# Default models per provider (live-probed 2026-11-23; OpenRouter refreshed at runtime)
DEFAULT_MODELS = {
    "local": os.environ.get("FREE_LADDER_LOCAL_MODEL", "auto"),
    "cerebras": os.environ.get("CEREBRAS_MODEL", "qwen-3.8-27b"),
    "cerebras_strong": os.environ.get("CEREBRAS_STRONG_MODEL", "gpt-oss-120b"),
    "groq_fast": os.environ.get("GROQ_FAST_MODEL", "openai/gpt-oss-20b"),
    "groq_strong": os.environ.get("GROQ_STRONG_MODEL", "openai/gpt-oss-120b"),
    "gemini_flash": os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    "gemini_pro": os.environ.get("GEMINI_PRO_MODEL", "gemini-2.5-pro"),
    "plugsky": os.environ.get("PLUGSKY_MODEL", "plugsky-ultra"),
    "glm": os.environ.get("GLM_MODEL", "glm-5.3"),
    "glm_fast": os.environ.get("GLM_FAST_MODEL", "glm-4.7"),
}

# Soft daily quotas (climb when exhausted). RPM soft-limits too.
QUOTAS = {
    "local": {"req_day": 1_000_000, "tok_day": 50_000_000, "rpm": 600},
    "cerebras": {"req_day": 14_400, "tok_day": 1_000_000, "rpm": 30},
    "groq": {"req_day": 14_400, "tok_day": 500_000, "rpm": 30},
    "gemini": {"req_day": 1_500, "tok_day": 1_000_000, "rpm": 15},
    "gemini_pro": {"req_day": 50, "tok_day": 200_000, "rpm": 5},
    "openrouter": {"req_day": 200, "tok_day": 500_000, "rpm": 10},
    "plugsky": {"req_day": 5_000, "tok_day": 5_000_000, "rpm": 60},
    "glm": {"req_day": 2_000, "tok_day": 2_000_000, "rpm": 30},
    "paid": {"req_day": 200, "tok_day": 500_000, "rpm": 10},
}

# Task class -> preferred free provider order (best capable, not just first)
# Rung numbers still enforce climb order within a class preference.
CLASS_PREFERENCE = {
    "classify": ["local", "groq", "cerebras", "gemini", "openrouter", "plugsky", "glm", "paid"],
    "extract": ["local", "groq", "cerebras", "gemini", "openrouter", "plugsky", "glm", "paid"],
    "chat": ["local", "cerebras", "groq", "gemini", "openrouter", "plugsky", "glm", "paid"],
    "summarize": ["gemini", "local", "cerebras", "groq", "openrouter", "plugsky", "glm", "paid"],
    "long_context": ["gemini", "openrouter", "local", "cerebras", "plugsky", "glm", "paid"],
    "code": ["cerebras", "openrouter", "local", "groq", "plugsky", "glm", "paid"],
    "reasoning": ["cerebras", "openrouter", "gemini", "groq", "plugsky", "glm", "paid"],
    "vision": ["gemini", "openrouter", "groq", "local", "plugsky", "glm", "paid"],
}

TIMEOUTS = {
    "local": float(os.environ.get("FREE_LADDER_LOCAL_TIMEOUT", "90")),
    "cerebras": 60.0,
    "groq": 60.0,
    "gemini": 90.0,
    "openrouter": 120.0,
    "plugsky": 120.0,
    "glm": 120.0,
    "paid": 180.0,
}

USER_AGENT = "free-ladder/1.0 (+hailports)"


def ensure_dirs() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_keys(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=VAL / export KEY=VAL from keys.env. Values never logged."""
    p = path or KEYS_ENV
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
            # also inject into process env for child helpers that read os.environ
            if k not in os.environ or not os.environ.get(k):
                os.environ[k] = v
    return out


def key_for(provider: str, keys: dict[str, str] | None = None) -> str:
    meta = PROVIDERS.get(provider) or {}
    env_name = meta.get("key_env")
    if not env_name:
        return ""
    bag = keys if keys is not None else load_keys()
    return (bag.get(env_name) or os.environ.get(env_name) or "").strip()
