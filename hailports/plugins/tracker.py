from hailports.plugins.base import Plugin
from hailports.ticket_guard import acquire, remember


class TrackerPlugin(Plugin):
    name = "tracker"

    def create(self, con, requester: str, title: str) -> dict:
        """Record a card locally. A near-duplicate title is refused before anything is created."""
        result = acquire(con, requester, title)
        if not result["ok"]:
            result["remote"] = False
            return result
        card_id = "local-1" if self.configured() else "local-held"
        remember(con, requester, title, card_id)
        result["card_id"] = card_id
        result["remote"] = False
        return result
