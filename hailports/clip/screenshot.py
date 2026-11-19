"""Screenshot in, draft out. The same full-context path as a live window.

A screenshot (or a pasted image) of a thread is read two ways: the local vision model
transcribes it into the header/body shape the thread reader understands, and on a Mac the
built-in Vision OCR reads it too, as a check and a fallback. Then compose.draft runs with
the image attached, so a vision model can also look at inline tables and pictures.
"""

from __future__ import annotations

import base64
from pathlib import Path

from hailports.clip import attach

TRANSCRIBE = (
    "This is a screenshot of an email or chat thread. Transcribe it exactly, newest message first. "
    "For each message write a header block in this form, then the body:\n"
    "From: <name> <email if shown>\nSent: <date/time>\nTo: <names>\nCc: <names>\nSubject: <subject>\n\n<body text>\n\n"
    "Keep every name, number, date and file name exactly as shown. List attachment chips as 'Attachment: <file name>'. "
    "If a message has an image, chart or table inside it, transcribe its text under 'Image text:'. "
    "If a reply is being written (a compose box), put its text first under 'Draft so far:'. "
    "Output only the transcription."
)


def _data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def transcribe(paths: list[Path], complete) -> str:
    content = [{"type": "text", "text": TRANSCRIBE}] + [
        {"type": "image_url", "image_url": {"url": _data_url(p)}} for p in paths]
    try:
        text = str(complete([{"role": "user", "content": content}]) or "").strip()
    except Exception:
        return ""
    if text.lower().startswith(("i can't", "i cannot", "sorry")):
        return ""
    return text


def scene_for(paths: list[Path], complete, app: str = "screenshot", vision: bool = True) -> dict:
    """A scene dict the draft path understands."""
    paths = [Path(p).expanduser() for p in paths if Path(p).expanduser().is_file()]
    ocr = "\n\n".join(t for t in (attach.ocr_image(p) for p in paths) if t)
    text = transcribe(paths, complete) if vision else ""
    draft_so_far = ""
    if text.lower().startswith("draft so far:"):
        head, _, rest = text.partition("\n\n")
        draft_so_far, text = head[len("draft so far:"):].strip(), rest
    return {
        "app": app,
        "title": "",
        "field_text": (draft_so_far + "\n\n" + text).strip() if text else ocr,
        "window_ocr": ocr,
        "selection": "",
        "blocks": [],
        "origin": "top",
        "images": [str(p) for p in paths],
    }
