import importlib.util
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "contrib/session_keepers/macos/outlook_session_watchdog.py"
SPEC = importlib.util.spec_from_file_location("outlook_session_watchdog", P)
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


def test_unreachable_optional_reader_is_not_a_break():
    assert w.classify(None, pages_ok=False, optional=True) == "not_served"


def test_unreachable_primary_is_still_browser_down():
    assert w.classify(None, pages_ok=False) == "browser_down"


def test_self_test_passes():
    assert w.self_test() == 0
