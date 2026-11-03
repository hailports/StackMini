"""Useful work for a model that would otherwise sit idle. Nothing here deletes a file."""

from __future__ import annotations

import time

JOBS = ("freshness", "reembed", "summarize-thread", "news-digest", "compress-context")


def next_job(interactive: int, last: dict, now: float, interval: float) -> str | None:
    """Oldest due job. A positive interactive count means a person is using the model."""
    if interactive > 0:
        return None
    due = []
    for index, name in enumerate(JOBS):
        stamp = float(last.get(name) or 0)
        if now - stamp >= interval:
            due.append((stamp, index, name))
    if not due:
        return None
    due.sort()
    return due[0][2]


def run_job(name: str, *, router, brain, thread_con, news_fetch=None, now: float | None = None, max_age_s: float = 3600) -> dict:
    """Run one nondestructive job. User files are not removed or overwritten."""
    now = time.time() if now is None else now
    result = {"job": name, "changed_user_files": False}
    if name == "freshness":
        from hailports.notes import stale_ids
        result["stale"] = len(stale_ids(brain, now, max_age_s)) if brain is not None else 0
        return result
    if name == "reembed":
        from hailports.notes import reembed_stale
        if brain is None or router is None:
            result["count"] = 0
            return result
        result["count"] = reembed_stale(
            brain,
            lambda texts: router.embed(texts).vectors,
            now,
            max_age_s,
        )
        return result
    if name == "summarize-thread":
        from hailports.threads import maybe_summarize
        result["summary"] = bool(maybe_summarize(thread_con, router)) if thread_con is not None and router is not None else False
        return result
    if name == "news-digest":
        if brain is None or router is None or news_fetch is None:
            result["skipped"] = True
            return result
        items = news_fetch() or []
        if not items:
            result["skipped"] = True
            return result
        blob = "\n".join(item.get("title") or "" for item in items)
        reply = router.complete(
            [
                {"role": "system", "content": "Summarize these headlines in three lines. Do not add stories that are not listed."},
                {"role": "user", "content": blob},
            ],
            task="summarize",
        )
        from hailports.notes import add
        add(brain, "news digest", reply.text or blob)
        result["lines"] = len(items)
        return result
    if name == "compress-context":
        if brain is None or router is None:
            result["skipped"] = True
            return result
        row = brain.execute("select id, title, body from docs order by length(body) desc limit 1").fetchone()
        if not row or len(row[2] or "") < 400:
            result["skipped"] = True
            return result
        title = "digest " + str(row[1])
        existing = brain.execute("select id from docs where title = ?", (title,)).fetchone()
        if existing:
            result["skipped"] = True
            return result
        compact, saved = router.compress(row[2])
        from hailports.notes import add
        add(brain, title, compact)
        result["saved"] = saved
        return result
    result["skipped"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    from hailports.config import load_file
    from hailports.paths import data_dir
    from hailports.providers.factory import build_router
    from hailports.secrets_store import EnvAndKeychain
    from hailports.threads import connect as thread_connect
    from hailports.webchat import PriorityGate
    from hailports.webtools import news
    from hailports.notes import connect as brain_connect

    parser = argparse.ArgumentParser(prog="hailports.idle")
    parser.add_argument("--config", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--interactive", type=int, default=0)
    args = parser.parse_args(argv)
    cfg = load_file(Path(args.config)) if args.config else {}
    interval = int(((cfg.get("idle") or {}).get("interval_s") or args.interval))
    state_path = data_dir() / "idle-state.json"
    gate = PriorityGate()
    while True:
        last = {}
        if state_path.exists():
            try:
                last = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                last = {}
        interactive = args.interactive or gate.interactive
        name = next_job(interactive, last, time.time(), interval)
        if name:
            try:
                router = build_router(cfg, EnvAndKeychain()) if cfg else None
            except Exception:
                router = None
            brain = brain_connect(data_dir() / "brain.sqlite")
            threads = thread_connect(data_dir() / "thread.sqlite")
            news_url = str(((cfg.get("web") or {}).get("news_url") or ""))

            def fetch_news(url=news_url):
                return news(url) if url else []

            try:
                result = run_job(name, router=router, brain=brain, thread_con=threads, news_fetch=fetch_news if news_url else None)
            except Exception as exc:
                result = {"job": name, "error": str(exc)[:200], "changed_user_files": False}
            last[name] = time.time()
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(last), encoding="utf-8")
            print(json.dumps(result))
        else:
            print(json.dumps({"job": None}))
        if args.once:
            return 0
        time.sleep(max(5, interval))


if __name__ == "__main__":
    raise SystemExit(main())
