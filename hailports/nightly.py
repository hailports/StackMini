"""Nightly regression gate. The rubric is local. A mail replay waits for a quiet machine."""

from __future__ import annotations

MAIL_BASELINE = "0/20"
SWAP_LINE_MB = 2800


def mail_gate(swap_mb: float, busy: bool, preempt: bool, line: float = SWAP_LINE_MB) -> str:
    """Empty means the one mail item may run. Otherwise say why it waits."""
    if float(swap_mb) > float(line):
        return "swap"
    if preempt:
        return "preempt"
    if busy:
        return "busy"
    return ""


def alarm_line(day: str, traps: float, trap_floor: float, mail_ran: int, mail_pass: int, mail_skipped: str, baseline: str = MAIL_BASELINE) -> str:
    """One status line. No prompt text and no draft text."""
    mail_bit = "mail {0}/{1} baseline {2}".format(mail_pass, mail_ran, baseline) if mail_ran else "mail skipped:{0} baseline {1}".format(mail_skipped or "idle", baseline)
    reasons = []
    if traps < trap_floor:
        reasons.append("traps {0:.1f}% < {1:.1f}".format(traps * 100, trap_floor * 100))
    if reasons:
        return "REALEVAL REGRESSION {0}: {1}; {2}".format(day, "; ".join(reasons), mail_bit)
    return "REALEVAL {0}: {1}".format(day, mail_bit)
