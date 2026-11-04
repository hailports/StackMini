"""Useful work for a model that would otherwise sit idle. Nothing here deletes a file."""

from __future__ import annotations

import re
import threading
import time

from hailports.contextpack import skip_unchanged, stable_fp

JOBS = ("freshness", "reembed", "summarize-thread", "news-digest", "compress-context", "deep-fill")

# One note, then stop. The ceiling is the size of one finished note.
# A stop string is not used: a source line can begin with the end word
# and still have the rest of the note to write.
DEEP_CAP = 2500
END_MARK = "ENDNOTE"
_PACK = 2300
_PRIMARY = 1400
_LINK_CAP = 4
_CLIP = 340
_WORK_SOURCES = {"tracker", "record", "pull", "doc", "learn"}
_META_KEYS = {"source", "ref", "url", "ties", "state"}
_CARD = re.compile(r"\bcard-(\d{3,8})\b", re.I)
_REC = re.compile(r"\brec-(\d{3,8})\b", re.I)
_PARENT = re.compile(r"(?m)^Parent:\s*(?:card-)?(\d{3,8})\s*$", re.I)
_PULL_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.I)
_BLOCKED_PATH = re.compile(r"(?i)(?:^|[\\/])(?:legal|finance)(?:[\\/]|$)")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{5,18}")
_STOPWORDS = {
    "ticket", "tickets", "update", "updated", "please", "thanks", "change",
    "changes", "status", "priority", "description", "request", "document",
    "summary", "review", "notes", "about", "their", "there", "which",
    "would", "could", "should", "story", "closed", "active", "outcome",
    "draft", "actual", "decision", "record", "records", "parent", "comment",
    "comments", "pull", "request",
}
DEEP_SYSTEM = (
    "Write one note for the single item in the source. "
    "Use only that text and the linked clips. "
    "Cover the facts, the result, the next steps, and a short check. "
    "The last line of the note must be a line by itself that says ENDNOTE. "
    "A source line that only begins with that word is not the end."
)


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


def run_job(name: str, *, router, brain, thread_con, news_fetch=None, now: float | None = None, max_age_s: float = 3600, allowed_repos: list[str] | None = None) -> dict:
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
    if name == "deep-fill":
        if brain is None or router is None:
            result["skipped"] = True
            return result
        items, pages = items_from_brain(brain)
        if not items:
            titles = set()
            try:
                titles = {str(row[0] or "") for row in brain.execute("select title from docs").fetchall()}
            except Exception:
                titles = set()
            items = follow_items(pages, titles)
        if not items:
            result["skipped"] = True
            return result
        chosen = take_one(items)
        nxt = items[1] if len(items) > 1 else None
        filled = deep_fill_one(router, chosen[0], pages, nxt, allowed_repos=allowed_repos)
        result.update(filled)
        result["changed_user_files"] = False
        if filled.get("ended") and filled.get("text"):
            from hailports.contextpack import stable_fp as _fp
            from hailports.notes import add
            fp = str((chosen[0] if chosen else {}).get("fp") or "") or _fp(str((chosen[0] if chosen else {}).get("text") or ""))
            add(brain, "deep " + str(filled.get("label") or "note"), filled["text"].rstrip() + "\nfp: " + fp)
            result["stored"] = True
        else:
            result["stored"] = False
        return result
    result["skipped"] = True
    return result


def allowed_repos_from(cfg: dict | None) -> list[str] | None:
    raw = ((cfg or {}).get("idle") or {}).get("allowed_repos")
    if not isinstance(raw, list):
        return None
    repos = [str(item).strip() for item in raw if str(item).strip()]
    return repos or None


def take_one(items: list) -> list:
    """One item per call. Anything still queued waits for the next call."""
    if not items:
        return []
    return [items[0]]


def note_ended(text: str) -> bool:
    """True only when ENDNOTE is a whole line.

    A result line may begin with that word and still have more to say.
    Matching the bare word would save a stub.
    """
    return re.search(r"(?m)^" + END_MARK + r"\n", text or "") is not None


def note_body(text: str) -> str:
    match = re.search(r"(?m)^" + END_MARK + r"\n", text or "")
    if not match:
        return (text or "").strip()
    return (text or "")[:match.start()].strip()


def page_from_doc(doc_id: int, title: str, body: str) -> dict:
    meta: dict[str, str] = {}
    lines = (body or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            if meta:
                index += 1
            break
        if ":" not in line:
            break
        key, val = line.split(":", 1)
        key = key.strip().lower()
        if key not in _META_KEYS or " " in key.strip():
            break
        meta[key] = val.strip()
        index += 1
    text = "\n".join(lines[index:]).strip()
    ties = [part for part in re.split(r"[\s,]+", meta.get("ties") or "") if part]
    return {
        "doc_id": doc_id,
        "ref": meta.get("ref") or f"doc-{doc_id}",
        "source": (meta.get("source") or "page").lower(),
        "title": title or "",
        "body": text if meta else (body or ""),
        "url": meta.get("url") or "",
        "ties": ties,
        "state": meta.get("state") or "",
    }


def _note_fps(rows) -> dict[str, str]:
    found = {}
    for _doc_id, title, body in rows:
        name = str(title or "")
        if not name.startswith("deep "):
            continue
        match = re.search(r"(?m)^fp: ([0-9a-f]{8,16})\s*$", body or "")
        if match:
            found[name[5:]] = match.group(1)
    return found


def _due(label: str, text: str, titles: set[str], fps: dict[str, str]) -> bool:
    """False when a stored note exists and its fingerprint still matches."""
    if f"deep {label}" not in titles:
        return True
    current = stable_fp(text)
    stored = fps.get(label)
    if stored and stored != current:
        return True
    return False


def items_from_brain(con) -> tuple[list[dict], list[dict]]:
    rows = con.execute("select id, title, body from docs").fetchall()
    titles = {str(title or "") for _doc_id, title, _body in rows}
    fps = _note_fps(rows)
    pages = [page_from_doc(int(doc_id), title or "", body or "") for doc_id, title, body in rows]
    items = []
    for page in pages:
        if page["source"] not in _WORK_SOURCES:
            continue
        if str(page["title"]).startswith("deep "):
            continue
        label = str(page["ref"])
        if not _due(label, page["body"], titles, fps):
            continue
        items.append({
            "label": label,
            "ref": page["ref"],
            "source": page["source"],
            "text": page["body"],
            "fp": stable_fp(page["body"]),
        })
    return items, pages


def follow_items(pages: list[dict], titles: set[str]) -> list[dict]:
    """Later sources once the primary note for a page is already stored.

    Open items get a local reply. A parent that has no note of its own is next.
    A stored note can be checked once against the current page.
    """
    by_ref = {str(page.get("ref") or ""): page for page in pages}
    items = []
    seen = set()

    def add(label: str, source: str, ref: str, text: str) -> None:
        if not text or label in seen or f"deep {label}" in titles:
            return
        if _blocked(text):
            return
        seen.add(label)
        items.append({
            "label": label,
            "ref": ref,
            "source": source,
            "text": text,
            "fp": stable_fp(text),
        })

    for page in pages:
        if str(page.get("title") or "").startswith("deep "):
            continue
        if page.get("source") not in _WORK_SOURCES:
            continue
        ref = str(page.get("ref") or "")
        body = str(page.get("body") or "")
        if f"deep {ref}" in titles and _is_open(page):
            add("reply-" + ref, "reply", ref, "Local pre-draft only. Not sent.\n" + body)
        if f"deep {ref}" in titles:
            add("recheck-" + ref, "recheck", ref, "Recheck the stored note against this page.\n" + body)
        parent = _parent_id(body)
        if parent:
            parent_ref = "card-" + parent
            parent_page = by_ref.get(parent_ref)
            if parent_page and f"deep {parent_ref}" not in titles:
                add(parent_ref, "parent", parent_ref, str(parent_page.get("body") or ""))
    return items


def _clip_text(text: str, limit: int) -> str:
    cleaned = []
    for line in (text or "").replace("\r", "").splitlines():
        if line.strip() == END_MARK:
            continue
        cleaned.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()
    return out[:limit]


def _blocked(text: str) -> bool:
    return _BLOCKED_PATH.search(text or "") is not None


def _mentions(page: dict, token: str) -> bool:
    if token in (page.get("ties") or []):
        return True
    blob = "\n".join([str(page.get("title") or ""), str(page.get("body") or ""), str(page.get("ref") or "")])
    return re.search(r"(?i)(?<![A-Za-z0-9-])" + re.escape(token) + r"(?![A-Za-z0-9-])", blob) is not None


def _same(page: dict, item: dict) -> bool:
    return str(page.get("ref") or "") == str(item.get("ref") or "")


def _card_ids(item: dict) -> list[str]:
    found = []
    label = str(item.get("label") or "")
    match = re.fullmatch(r"card-(\d{3,8})", label, re.I)
    if match:
        found.append(match.group(1))
    for match in _CARD.finditer(str(item.get("text") or "")):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


def _rec_ids(text: str) -> list[str]:
    found = []
    for match in _REC.finditer(text or ""):
        if match.group(1) not in found:
            found.append(match.group(1))
        if len(found) >= 4:
            break
    return found


def _parent_id(text: str) -> str:
    match = _PARENT.search(text or "")
    return match.group(1) if match else ""


def _repo_ok(url: str, allowed: list[str] | None) -> bool:
    if not allowed:
        return True
    blob = (url or "").lower()
    return any(repo.lower() in blob for repo in allowed if repo)


def _pull_name(url: str) -> str:
    match = _PULL_URL.search(url or "")
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}#{match.group(3)}"


def _state_line(page: dict) -> str:
    if str(page.get("state") or "").strip():
        return "state: " + str(page["state"]).strip()
    match = re.search(r"(?im)^state:\s*\S[^\n]{0,40}", page.get("body") or "")
    return match.group(0).strip() if match else ""


def _is_open(page: dict) -> bool:
    blob = _state_line(page) + "\n" + str(page.get("body") or "")
    return re.search(r"(?i)\bstate:\s*open\b", blob) is not None


def _brain_words(item: dict) -> list[str]:
    words = []
    for word in _WORD.findall(str(item.get("text") or "")[:700]):
        low = word.lower()
        if low in _STOPWORDS or low in words:
            continue
        words.append(low)
        if len(words) >= 3:
            break
    return words


def linked_clips(item: dict, pages: list[dict], allowed_repos: list[str] | None = None) -> list[str]:
    """Short clips from the pages that actually belong to this one item.

    At most four clips. Each clip is a few hundred characters. Read only.
    """
    pages = pages or []
    text = str(item.get("text") or "")
    source = str(item.get("source") or "")
    label = str(item.get("label") or "")
    pieces: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, key: str, body: str) -> None:
        if len(pieces) >= _LINK_CAP or not key or key in seen:
            return
        clip = _clip_text(body, _CLIP)
        if len(clip) < 40 or _blocked(clip):
            return
        seen.add(key)
        pieces.append({"kind": kind, "text": f"LINK {kind}\n{clip}"})

    def comments(card: str) -> None:
        if not card:
            return
        token = f"card-{card}"
        for page in pages:
            if str(page.get("source")) != "comments" or _same(page, item):
                continue
            if not _mentions(page, token):
                continue
            add("comments", "comments:" + token, f"{page.get('title') or ''}\n{page.get('body') or ''}")
            return

    def pulls(card: str, limit: int) -> None:
        if not card or limit <= 0:
            return
        token = f"card-{card}"
        opened = []
        other = []
        for page in pages:
            if str(page.get("source")) != "pull" or _same(page, item):
                continue
            if not _mentions(page, token):
                continue
            if not _repo_ok(str(page.get("url") or ""), allowed_repos):
                continue
            bucket = opened if _is_open(page) else other
            bucket.append(page)
        kept = 0
        for page in opened + other:
            if kept >= limit or len(pieces) >= _LINK_CAP:
                break
            name = _pull_name(str(page.get("url") or "")) or str(page.get("ref") or "")
            add(
                "pull",
                "pull:" + str(page.get("ref") or name),
                "\n".join([name, _state_line(page), str(page.get("title") or ""), str(page.get("body") or "")[:220]]),
            )
            kept += 1

    def records(num: str, limit: int = 1) -> None:
        if not num or limit <= 0:
            return
        token = f"rec-{num}"
        exact = []
        other = []
        for page in pages:
            if str(page.get("source")) != "record" or _same(page, item):
                continue
            if str(page.get("ref") or "").lower() == token:
                exact.append(page)
            elif _mentions(page, token):
                other.append(page)
        # The record item itself is already the primary text.
        ordered = other if str(item.get("ref") or "").lower() == token else exact + other
        kept = 0
        for page in ordered:
            if _same(page, item):
                continue
            add("record", "record:" + str(page.get("ref")), f"{page.get('title') or ''}\n{page.get('body') or ''}")
            kept += 1
            if kept >= limit or len(pieces) >= _LINK_CAP:
                break

    def trackers_for_record(num: str) -> None:
        if not num:
            return
        token = f"rec-{num}"
        kept = 0
        for page in pages:
            if str(page.get("source")) != "tracker" or _same(page, item):
                continue
            if not _mentions(page, token):
                continue
            add("tracker", "tracker:" + str(page.get("ref")), "\n".join([
                str(page.get("title") or ""), _state_line(page), str(page.get("body") or "")[:180],
            ]))
            kept += 1
            if kept >= 2:
                break

    def parent(card: str) -> None:
        parent_id = _parent_id(text)
        if not parent_id or parent_id == card:
            return
        token = f"card-{parent_id}"
        for page in pages:
            if _same(page, item):
                continue
            if str(page.get("ref") or "").lower() != token and not (
                str(page.get("source")) == "parent" and _mentions(page, token)
            ):
                continue
            add("parent", "parent:" + token, f"{page.get('title') or ''}\n{_state_line(page)}")
            return

    def tracker_brief(card: str) -> None:
        if not card:
            return
        token = f"card-{card}"
        for page in pages:
            if str(page.get("source")) != "tracker" or _same(page, item):
                continue
            if str(page.get("ref") or "").lower() != token and not _mentions(page, token):
                continue
            add("tracker", "tracker:" + token, "\n".join([
                str(page.get("title") or ""), _state_line(page), str(page.get("body") or "")[:180],
            ]))
            return

    def brain() -> None:
        words = _brain_words(item)
        if len(words) < 2:
            return
        kept = 0
        for page in pages:
            if kept >= 2 or len(pieces) >= _LINK_CAP or _same(page, item):
                continue
            if str(page.get("source")) not in {"page", "notes", "tracker", "record", "pull", "doc", "learn"}:
                continue
            blob = "\n".join([
                str(page.get("url") or ""), str(page.get("title") or ""),
                str(page.get("ref") or ""), str(page.get("body") or ""),
            ])
            if _blocked(blob):
                continue
            low = blob.lower()
            if words[0] in low and words[1] in low:
                add("page", "page:" + str(page.get("ref"))[:48], "\n".join([
                    str(page.get("source") or "page"), str(page.get("title") or ""), str(page.get("body") or "")[:180],
                ]))
                kept += 1

    card = ""
    if label.lower().startswith("card-") and label[5:].isdigit():
        card = label[5:]
    rec_label = ""
    if label.lower().startswith("rec-") and label[4:].isdigit():
        rec_label = label[4:]
    if source == "tracker" or card:
        comments(card)
        recs = _rec_ids(text)
        pulls(card, 2 if not recs else 1)
        for num in recs[:2]:
            records(num)
        parent(card)
        if len(pieces) < 2:
            pulls(card, 1)
    elif source == "record" or rec_label:
        num = rec_label or (_rec_ids(text)[:1] or [""])[0]
        trackers_for_record(num)
        records(num, 2)
        for rel in _card_ids(item)[:1]:
            if rel == card:
                continue
            pulls(rel, 1)
            comments(rel)
        if len(pieces) < 1:
            brain()
    elif source == "pull" or label.lower().startswith("pull-"):
        for rel in _card_ids(item)[:2]:
            tracker_brief(rel)
            comments(rel)
        for num in _rec_ids(text)[:2]:
            records(num)
            trackers_for_record(num)
    else:
        for num in _rec_ids(text)[:2]:
            trackers_for_record(num)
            records(num)
        for rel in _card_ids(item)[:2]:
            tracker_brief(rel)
            pulls(rel, 1)
        if len(pieces) < 2:
            brain()
    return [piece["text"] for piece in pieces]


def pack_context(item: dict, clips: list[str]) -> str:
    seed = str(item.get("text") or "").strip()
    primary_limit = 900 if clips else _PRIMARY
    primary = "PRIMARY\n" + _clip_text(seed, primary_limit) if seed else ""
    chunks = []
    used = 0
    if primary and not _blocked(primary):
        chunks.append(primary)
        used = len(primary)
    for extra in clips:
        room = _PACK - used - 2
        if room < 80:
            break
        piece = extra.strip()[:room]
        if len(piece) < 40 or _blocked(piece):
            continue
        chunks.append(piece)
        used += len(piece) + 2
    return "\n\n".join(chunks)[:_PACK]


def deep_prompt(item: dict, context: str) -> str:
    return (
        "Kind: deep\n"
        "One item only. Fill every section, then stop on its own end line.\n\n"
        f"SOURCE\nITEM {item.get('label') or ''}\n{context}"
    )


def deep_fill_one(router, item: dict, pages: list[dict], next_item: dict | None = None, *, allowed_repos: list[str] | None = None) -> dict:
    """Write one note. Gather the next item's clips while this note is written."""
    clips = linked_clips(item, pages, allowed_repos=allowed_repos)
    holder: dict = {}

    def _ahead() -> None:
        try:
            holder["clips"] = linked_clips(next_item or {}, pages, allowed_repos=allowed_repos)
        except Exception:
            holder["clips"] = []

    worker = None
    if next_item is not None:
        worker = threading.Thread(target=_ahead, name="idle-prefetch", daemon=True)
        worker.start()
    prompt = deep_prompt(item, pack_context(item, clips))
    reply = router.complete(
        [
            {"role": "system", "content": DEEP_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        task="summarize",
        max_tokens=DEEP_CAP,
    )
    if worker is not None:
        worker.join(timeout=30)
    raw = getattr(reply, "text", "") or ""
    ended = note_ended(raw)
    kinds = []
    for clip in clips:
        kind = clip.split("\n", 1)[0].replace("LINK ", "", 1).strip()
        if kind and kind not in kinds:
            kinds.append(kind)
    prefetched = holder.get("clips") or []
    return {
        "job": "deep-fill",
        "label": str(item.get("label") or ""),
        "text": note_body(raw) if ended else raw.strip(),
        "ended": ended,
        "stop": "end" if ended else "cap",
        "links": len(clips),
        "kinds": kinds,
        "prefetched_links": len(prefetched),
        "changed_user_files": False,
    }


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
            repos = allowed_repos_from(cfg)

            def fetch_news(url=news_url):
                return news(url) if url else []

            try:
                result = run_job(
                    name,
                    router=router,
                    brain=brain,
                    thread_con=threads,
                    news_fetch=fetch_news if news_url else None,
                    allowed_repos=repos,
                )
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
