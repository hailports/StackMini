"""python -m core.free_ladder [--serve] [--port 8790] [--selftest]"""
from __future__ import annotations

import json
import sys


def _selftest() -> int:
    from .classify import classify
    from .providers import is_openrouter_free, list_openrouter_free
    from .redact import redact_text
    from .router import build_plan, parse_model_pin

    assert parse_model_pin("auto") == (None, None)
    assert parse_model_pin("cerebras/qwen-3.8-27b")[0] == "cerebras"
    masked = redact_text("password: hunter2 and Bearer abc.def")
    assert "hunter2" not in masked and "abc.def" not in masked
    info = classify({"messages": [{"role": "user", "content": "classify this as spam or ham"}]})
    assert info["task"] == "classify"
    plan = build_plan({"model": "auto", "messages": [{"role": "user", "content": "summarize this long document " + ("x" * 1000)}]})
    assert "order" in plan and plan["order"]
    # paid openrouter must be refused by helper
    assert is_openrouter_free("openai/gpt-4o") is False
    free = list_openrouter_free()
    for mid in free:
        assert mid.endswith(":free") or mid == "openrouter/free", mid
        assert is_openrouter_free(mid) is True
    print(json.dumps({"ok": True, "openrouter_free": len(free), "task": info["task"], "plan0": plan["order"][:3]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    port = None
    hosts = None
    if "--port" in argv:
        i = argv.index("--port")
        port = int(argv[i + 1])
    if "--bind" in argv:
        i = argv.index("--bind")
        hosts = [h.strip() for h in argv[i + 1].split(",") if h.strip()]
    # default action is serve
    from .config import BIND_HOSTS, LADDER_PORT
    from .gateway import serve

    return serve(hosts=hosts or BIND_HOSTS, port=port or LADDER_PORT)


if __name__ == "__main__":
    raise SystemExit(main())
