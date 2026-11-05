"""Optional plugs for a CRM, a tracker, mail, meetings, and a file library."""

from hailports.plugins.base import Plugin
from hailports.plugins.crm import CrmPlugin
from hailports.plugins.files import FilesPlugin
from hailports.plugins.mail import MailPlugin
from hailports.plugins.meetings import MeetingsPlugin
from hailports.plugins.tracker import TrackerPlugin

__all__ = ["Plugin", "CrmPlugin", "TrackerPlugin", "MailPlugin", "MeetingsPlugin", "FilesPlugin", "load_all"]


def load_all(cfg: dict) -> list[Plugin]:
    block = cfg.get("plugins") or {}
    return [
        CrmPlugin(block.get("crm") or {}),
        TrackerPlugin(block.get("tracker") or {}),
        MailPlugin(block.get("mail") or {}),
        MeetingsPlugin(block.get("meetings") or {}),
        FilesPlugin(block.get("files") or {}),
    ]
