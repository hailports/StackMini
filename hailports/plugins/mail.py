from hailports.plugins.base import Plugin


class MailPlugin(Plugin):
    name = "mail"


# Types a draft may attach. A presentation is included. This map does not send mail.
DRAFT_ATTACH_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".html": "text/html",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
DRAFT_ATTACH_MAX_FILES = 3
DRAFT_ATTACH_MAX_FILE = 5_000_000
DRAFT_ATTACH_MAX_TOTAL = 10_000_000


def draft_attach_plan(files):
    """Check names and sizes for an existing unsent draft.

    Each item is a dict with name and size. The result is the file name, the
    size, and the content type. This function does not read the files, create
    a message, or send mail.
    """
    if not isinstance(files, list) or not files:
        raise ValueError("draft attach needs at least one file")
    if len(files) > DRAFT_ATTACH_MAX_FILES:
        raise ValueError("draft attach takes at most 3 files")
    staged = []
    running = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("draft attach needs a file name")
        raw = str(entry.get("name") or "").strip()
        base = raw.replace("\\", "/").split("/")[-1]
        if not base or base in {".", ".."}:
            raise ValueError("draft attach needs a file name")
        dot = base.rfind(".")
        ext = base[dot:].lower() if dot > 0 else ""
        if ext not in DRAFT_ATTACH_TYPES:
            raise ValueError("draft attach refuses extension " + (ext or "(none)"))
        try:
            size = int(entry.get("size"))
        except (TypeError, ValueError):
            raise ValueError("draft attach needs a file size")
        if size <= 0:
            raise ValueError("draft attach refuses an empty file")
        if size > DRAFT_ATTACH_MAX_FILE:
            raise ValueError("draft attach refuses a file over the size cap")
        running += size
        if running > DRAFT_ATTACH_MAX_TOTAL:
            raise ValueError("draft attach refuses a total over the size cap")
        staged.append({"name": base, "size": size, "type": DRAFT_ATTACH_TYPES[ext]})
    return staged


def read_message(was_read, fetch_body, restore):
    """Read one message without clearing an unread badge.

    was_read is the state before this read. fetch_body may mark the message
    read. When it was unread, restore runs once. A message that was already
    read is left read.
    """
    body = fetch_body()
    if was_read is False:
        restore()
        return {"body": body, "restored": True}
    return {"body": body, "restored": False}
