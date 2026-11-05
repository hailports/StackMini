"""Queue hold: age is not a signal at zero, and evening floors drop only while starving."""
from __future__ import annotations

from hailports.queue_hold import content_floors, evening_starving, still_hot


def test_zero_views_and_likes_are_not_hot():
    assert still_hot(1.0, 0, 0, 0.0, 0.0) is False
    assert still_hot(2.1, 0, 0, 0.0, 0.0) is False


def test_a_real_signal_is_hot():
    assert still_hot(1.0, 1, 0, 0.01, 1.0) is True
    assert still_hot(1.0, 0, 1, 0.0, 0.0) is True
    assert still_hot(3.0, 10, 0, 1.8, 3.0) is True
    assert still_hot(3.0, 10, 0, 0.2, 40.0) is True
    assert still_hot(3.5, 0, 5, 0.0, 0.0) is True


def test_weak_or_old_items_are_not_hot():
    assert still_hot(3.0, 0, 4, 0.0, 0.0) is False
    assert still_hot(3.6, 1, 0, 0.2, 39.0) is False
    assert still_hot(6.1, 100, 20, 5.0, 80.0) is False


def test_evening_starve_window():
    assert evening_starving(16, 5.0, True) is False
    assert evening_starving(17, 3.0, True) is True
    assert evening_starving(22, 3.0, True) is True
    assert evening_starving(23, 5.0, True) is False
    assert evening_starving(19, 2.9, True) is False
    assert evening_starving(19, 5.0, False) is False
    assert evening_starving(20, float("inf"), True) is True


def test_floors_drop_except_voice():
    normal = content_floors(False)
    soft = content_floors(True)
    assert normal == {"interest": 9, "viral": 8, "clarity": 7, "voice": 8}
    assert soft == {"interest": 7, "viral": 7, "clarity": 6, "voice": 8}
    assert soft["voice"] == normal["voice"] == 8
