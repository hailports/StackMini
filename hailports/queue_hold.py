"""Hold and evening floor rules for a quiet queue.

Age by itself does not mark an item hot when views and likes are both zero.
During evening hours 17 through 22, if nothing original has gone out for at
least 3 hours and a draft is waiting, content floors drop. The voice floor
never drops.
"""
from __future__ import annotations

HOT_MAX_AGE_H = 6.0
HOT_AGE_H = 2.1
HOT_MULT = 1.8
HOT_VEL_AGE_H = 3.5
HOT_VEL = 40.0
HOT_LIKES = 5

EVENING_START_H = 17
EVENING_END_H = 22
STARVE_IDLE_H = 3.0

FLOOR_INTEREST = 9
FLOOR_VIRAL = 8
FLOOR_CLARITY = 7
FLOOR_VOICE = 8


def still_hot(age_h: float, views: int, likes: int, mult: float, vel: float) -> bool:
    """True when a recent item has a real signal. Zero views and zero likes are not a signal."""
    if float(age_h) > HOT_MAX_AGE_H:
        return False
    views_n = int(views or 0)
    likes_n = int(likes or 0)
    return (
        (float(age_h) <= HOT_AGE_H and (views_n > 0 or likes_n > 0))
        or float(mult) >= HOT_MULT
        or (float(age_h) <= HOT_VEL_AGE_H and float(vel) >= HOT_VEL)
        or (float(age_h) <= HOT_VEL_AGE_H and likes_n >= HOT_LIKES)
    )


def evening_starving(hour: int, hours_since_original: float, has_draft: bool) -> bool:
    """True in hours 17-22 when the queue has a draft and the last original is at least 3 hours old."""
    if int(hour) < EVENING_START_H or int(hour) > EVENING_END_H:
        return False
    if not has_draft:
        return False
    return float(hours_since_original) >= STARVE_IDLE_H


def content_floors(starving: bool) -> dict:
    """Interest, viral, and clarity drop while starving. Voice stays at 8."""
    if starving:
        return {"interest": 7, "viral": 7, "clarity": 6, "voice": FLOOR_VOICE}
    return {
        "interest": FLOOR_INTEREST,
        "viral": FLOOR_VIRAL,
        "clarity": FLOOR_CLARITY,
        "voice": FLOOR_VOICE,
    }
