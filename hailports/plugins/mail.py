from hailports.plugins.base import Plugin


class MailPlugin(Plugin):
    name = "mail"


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
