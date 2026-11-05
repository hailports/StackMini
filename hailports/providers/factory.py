"""Build a local provider, a cloud provider, and the router from config.

The config names the provider and the env var. It does not hold the key.
"""

from __future__ import annotations

from hailports.providers.anthropic import AnthropicProvider
from hailports.providers.google import GoogleProvider
from hailports.providers.openai_compat import OpenAICompatProvider
from hailports.router import Policy, Router
from hailports.secrets_store import EnvAndKeychain

OPENAI_STYLE = ("openai", "xai", "openrouter", "groq", "together", "compatible")

DEFAULTS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "xai": ("https://api.x.ai/v1", "grok-2"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.1-8b-instant"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"),
    "compatible": ("http://127.0.0.1:18080/v1", "local"),
    "anthropic": ("https://api.anthropic.com", "claude-haiku"),
    "google": ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.0-flash"),
}


def policy_from(cfg: dict) -> Policy:
    block = cfg.get("routing") or {}
    return Policy(
        confidence_floor=float(block.get("confidence_floor") if block.get("confidence_floor") is not None else 0.55),
        complexity_ceiling=int(block.get("complexity_ceiling") if block.get("complexity_ceiling") is not None else 1),
        max_local_output_chars=int(block.get("max_local_output_chars") or 900),
        compact_char_budget=int(block.get("compact_char_budget") or 1600),
    )


def build_local(cfg: dict, api_key: str | None = None):
    llm = cfg.get("llm") or {}
    base = str(llm.get("base_url") or "http://127.0.0.1:18080/v1")
    model = str(llm.get("model") or "local")
    brain = cfg.get("notes") or {}
    embed_model = str(brain.get("embed_model") or model)
    embed_base = str(brain.get("embed_url") or "")
    return OpenAICompatProvider(
        "local", base, model, api_key=api_key, embed_model=embed_model, embed_base_url=embed_base
    )


def build_cloud(cfg: dict, api_key: str | None):
    cloud = cfg.get("cloud") or {}
    provider = str(cloud.get("provider") or "openai").strip().lower()
    default_base, default_model = DEFAULTS.get(provider, DEFAULTS["compatible"])
    base = str(cloud.get("base_url") or default_base)
    model = str(cloud.get("model") or default_model)
    embed_model = str(cloud.get("embed_model") or "")
    if provider == "anthropic":
        return AnthropicProvider(provider, base, model, api_key=api_key)
    if provider == "google":
        return GoogleProvider(provider, base, model, api_key=api_key, embed_model=embed_model or "text-embedding-004")
    label = provider if provider in OPENAI_STYLE or provider == "compatible" else provider
    return OpenAICompatProvider(label or "openai", base, model, api_key=api_key, embed_model=embed_model or model)


def build_router(cfg: dict, secrets=None) -> Router:
    secrets = secrets or EnvAndKeychain()
    mode = str(cfg.get("mode") or "local").strip().lower()
    if mode not in ("local", "hybrid", "api"):
        mode = "local"
    local = None
    cloud = None
    key = None
    if mode in ("api", "hybrid"):
        key = secrets.get(cfg)
    if mode in ("local", "hybrid"):
        local_key = None
        local = build_local(cfg, local_key)
    if mode in ("api", "hybrid"):
        cloud = build_cloud(cfg, key)
    return Router(mode, local, cloud, policy_from(cfg))
