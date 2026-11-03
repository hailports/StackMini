"""Sort a message into a question, a task, or a refusal."""

from __future__ import annotations

from hailports.drafter import draft
from hailports.runbooks import search as search_runbooks
from hailports.webtools import DataLine, refuse_sensitive


def classify(text: str) -> str:
    try:
        refuse_sensitive(text)
    except DataLine:
        return "refuse"
    stripped = text.strip()
    low = stripped.lower()
    if not stripped:
        return "refuse"
    if low.startswith(("how ", "what ", "when ", "where ", "who ", "why ", "which ")) or stripped.endswith("?"):
        return "ask"
    return "do"


def gather(text: str, runbook_root, brain=None, allow_root=None) -> dict:
    """Read runbooks and notes. This does not write, delete, or run a command."""
    kind = classify(text)
    actions: list[str] = []
    notes: list[str] = []
    titles: list[str] = []
    if kind == "refuse":
        return {"kind": "refuse", "text": "", "notes": [], "actions": ["held"], "changed": False, "runbooks": []}
    hits = search_runbooks(runbook_root, text) if runbook_root else []
    if hits:
        actions.append("read-runbook")
        for hit in hits:
            titles.append(hit["title"])
            notes.append(hit["body"])
    else:
        actions.append("no-runbook")
    if brain is not None:
        from hailports.notes import search as brain_search
        rows = brain_search(brain, text)
        if rows:
            actions.append("search-notes")
            notes.extend(row["body"] for row in rows)
    if allow_root is not None and kind == "do":
        root = allow_root.resolve()
        if root.exists():
            actions.append("list-files")
            for path in sorted(root.rglob("*.md"))[:5]:
                target = path.resolve()
                if root not in target.parents and target != root:
                    continue
                notes.append(target.read_text(encoding="utf-8", errors="replace")[:4000])
    return {
        "kind": kind,
        "notes": notes,
        "actions": actions,
        "changed": False,
        "runbooks": titles,
    }


def respond(router, text: str, bundle: dict):
    """Ask the model to draft from the notes that were read. The model is the reply."""
    if bundle.get("kind") == "refuse":
        return router.complete(
            [
                {"role": "system", "content": "The message was held locally and was not sent. Say that. Do not guess any secret."},
                {"role": "user", "content": "A message was held."},
            ],
            task="read",
        )
    notes = "\n\n".join(bundle.get("notes") or []) or "(no local notes)"
    task = "draft" if bundle.get("kind") == "do" else "read"
    return router.complete(
        [
            {"role": "system", "content": "Use only the notes. Say what was read. Do not claim any file was changed."},
            {"role": "user", "content": "Actions: " + ", ".join(bundle.get("actions") or []) + "\nNotes:\n" + notes + "\n\nRequest: " + text},
        ],
        task=task,
    )


def handle(text: str, runbook_root, sources: list[str] | None = None) -> dict:
    kind = classify(text)
    if kind == "refuse":
        return {"kind": "refuse", "text": "That looks private. It was not sent anywhere."}
    if kind == "ask":
        hits = search_runbooks(runbook_root, text) if runbook_root else []
        notes = sources or [h["body"] for h in hits]
        if notes:
            try:
                answer = draft(text, notes)
            except Exception:
                answer = notes[0][:500]
        else:
            answer = "No local notes match that question."
        return {"kind": "ask", "text": answer, "runbooks": [h["title"] for h in hits]}
    hits = search_runbooks(runbook_root, text) if runbook_root else []
    if not hits:
        return {
            "kind": "do",
            "text": "No matching runbook. Nothing was changed. Add a runbook or run it yourself.",
            "changed": False,
        }
    return {
        "kind": "do",
        "text": "Matched a runbook. Review it before you run it.\n" + hits[0]["body"][:800],
        "runbook": hits[0]["title"],
        "changed": False,
    }
