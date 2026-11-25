import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "contrib/session_keepers/macos/zoom_session_watchdog.py"
SPEC = importlib.util.spec_from_file_location("zoom_watchdog", MODULE)
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


def page(target="A", route="/wc/home"):
    return {
        "id": target,
        "type": "page",
        "url": f"https://{w.ZOOM_HOST}{route}",
        "webSocketDebuggerUrl": f"{w.MINI_ZOOM.replace('http:', 'ws:')}/devtools/page/{target}",
    }


@pytest.fixture
def browser(monkeypatch):
    pages = [page("CHAT", "/wc/team-chat"), page("HOME")]
    replies = {}
    calls = []

    class Tab:
        def __init__(self, target, timeout=20):
            self.target = target["id"]
            self.crashed = False

        def call(self, method):
            calls.append((self.target, method))
            result = replies.get((self.target, method), {"result": {}})
            if isinstance(result, Exception):
                raise result
            return result

        def ev(self, js):
            result = self.call("probe")
            return json.dumps(result)

        def close(self):
            calls.append((self.target, "close"))

    monkeypatch.setattr(w, "_Tab", Tab)
    monkeypatch.setattr(w, "_pages", lambda ep: pages)
    monkeypatch.setattr(w, "_owns_listener", lambda ep: ep == w.MINI_ZOOM)
    return pages, replies, calls


def test_inspector_crash_observed_even_when_reply_times_out(monkeypatch):
    class Socket:
        def send(self, message):
            pass

        def settimeout(self, timeout):
            assert 0 < timeout <= 3

        def recv(self):
            if getattr(self, "sent_event", False):
                raise TimeoutError()
            self.sent_event = True
            return '{"method":"Inspector.targetCrashed"}'

    monkeypatch.setattr(w, "_ws", lambda target, timeout: Socket())
    assert w._renderer_fault(w._Tab(page(), timeout=3))


def test_repair_selects_crashed_home_alongside_live_chat_and_rechecks(browser, monkeypatch):
    pages, replies, calls = browser
    home = pages[1]
    replies[("HOME", "Page.getFrameTree")] = {
        "result": {"frameTree": {"frame": {"unreachableUrl": home["url"]}}}
    }
    replies[("CHAT", "probe")] = {"href": w.TEAM_CHAT, "rows": 4, "upcoming": 0, "history": 0}
    state = {}
    monkeypatch.setattr(w, "endpoint", lambda: w.MINI_ZOOM)
    monkeypatch.setattr(w, "_read_state", lambda: {})
    monkeypatch.setattr(w, "_write_state", state.update)
    monkeypatch.setattr(w, "_log", lambda msg: None)

    def recover(seconds):
        del replies[("HOME", "Page.getFrameTree")]

    monkeypatch.setattr(w.time, "sleep", recover)
    assert w.tick() == 0
    assert [event for event in calls if event[1] == "Page.reload"] == [("HOME", "Page.reload")]
    assert state["last_state"] == "ok"
    assert state["last_rows"] == 4
    assert state["last_target"] == "CHAT"


def test_reload_is_rate_limited_and_requires_positive_fault(browser):
    _, replies, calls = browser
    assert w.heal(w.MINI_ZOOM, "page_crashed", {}, dry=False, target="HOME") == "reload_fault_cleared"
    replies[("HOME", "Page.getFrameTree")] = {
        "result": {"frameTree": {"frame": {"unreachableUrl": page()["url"]}}}
    }
    state = {}
    assert w.heal(w.MINI_ZOOM, "page_crashed", state, dry=True, target="HOME") == "would_reload_crashed_tab"
    assert w.heal(w.MINI_ZOOM, "page_crashed", state, dry=False, target="HOME") == "reloaded_tab"
    assert w.heal(w.MINI_ZOOM, "page_crashed", state, dry=False, target="HOME") == "reload_cooldown"
    assert calls.count(("HOME", "Page.reload")) == 1


@pytest.mark.parametrize("field,value", [
    ("id", "REPLACED"), ("url", "https://other.example/wc/home"),
    ("url", "https://login.example/"), ("url", w.TEAM_CHAT + "?meeting=1"),
    ("url", w.TEAM_CHAT + "#channel"), ("url", "https://["),
    ("webSocketDebuggerUrl", "ws://other.example/devtools/page/HOME"),
])
def test_changed_target_or_route_cannot_be_reloaded(browser, field, value):
    pages, _, calls = browser
    pages[1][field] = value
    assert w.heal(w.MINI_ZOOM, "page_crashed", {}, dry=False, target="HOME") == "reload_target_changed"
    assert not calls


def test_unowned_browser_is_excluded_and_probe_errors_do_not_trigger_heals(browser, monkeypatch):
    _, replies, calls = browser
    monkeypatch.setattr(w, "_owns_listener", lambda ep: False)
    replies[("CHAT", "probe")] = TimeoutError()
    assert w.probe(w.MINI_ZOOM)[0] == "probe_error"
    assert w.step({"consecutive_bad": 9}, "probe_error", now=1) == []
    assert w.heal(w.MINI_ZOOM, "page_crashed", {}, dry=False, target="HOME") == "reload_unowned"
    assert ("HOME", "Inspector.enable") not in calls
    assert ("HOME", "Page.reload") not in calls


@pytest.mark.parametrize("listener,owned", [
    ("n127.0.0.1:18812", True), ("n*:18812", False),
    ("n127.0.0.1:18812\nn*:18812", False), ("", False),
])
def test_ownership_requires_exact_job_pid_and_listener(monkeypatch, listener, owned):
    monkeypatch.setenv("ZOOM_WD_BROWSER_LABEL", "example.zoom-browser")

    def inspect(argv, **kwargs):
        if argv[0].endswith("/launchctl"):
            assert argv[1:] == ["print", f"gui/{w.os.getuid()}/example.zoom-browser"]
            return "state = running\n  pid = 42\n"
        assert argv == ["/usr/sbin/lsof", "-nP", "-a", "-p", "42", "-iTCP:18812", "-sTCP:LISTEN", "-Fn"]
        return f"p42\n{listener}\n"

    monkeypatch.setattr(w.subprocess, "check_output", inspect)
    assert w._owns_listener(w.MINI_ZOOM) == owned
    assert not w._owns_listener("http://other.example:18812")
    monkeypatch.delenv("ZOOM_WD_BROWSER_LABEL")
    assert not w._owns_listener(w.MINI_ZOOM)


@pytest.mark.parametrize("url", [
    "https://other.example/?continue=https://zoom.us/signin",
    "https://zoom.us.other.example/wc/team-chat",
    "https://zoom.us:@other.example/wc/team-chat",
    "https://@zoom.us/signin",
    "https://[",
    f"http://{w.ZOOM_HOST}/wc/home",
])
def test_unrelated_and_malformed_pages_are_excluded(browser, url):
    pages, _, calls = browser
    pages[:] = [dict(page("OTHER"), url=url)]
    assert w.classify({"href": url, "err": True}) == "page_missing"
    assert w.probe(w.MINI_ZOOM)[0] == "page_missing"
    for state in ("rows0", "sso_error"):
        assert w.heal(w.MINI_ZOOM, state, {}, dry=False, target="OTHER") == "none_no_page"
    assert calls == []


def test_generic_browser_error_page_is_never_selected(browser):
    pages, _, calls = browser
    pages[:] = [dict(page("ERROR"), url="chrome-error://chromewebdata/")]
    assert w.probe(w.MINI_ZOOM)[0] == "page_missing"
    assert w.heal(w.MINI_ZOOM, "sso_error", {}, dry=False, target="ERROR") == "none_no_page"
    assert calls == []


def test_soft_heal_requires_same_tab_and_live_zoom_frame(browser, monkeypatch):
    from contextlib import nullcontext

    _, replies, calls = browser
    assert w.heal(w.MINI_ZOOM, "rows0", {}, dry=False, target="GONE") == "none_no_page"
    monkeypatch.setattr(w, "_reader_lock", lambda: nullcontext(True))
    replies[("CHAT", "Page.getFrameTree")] = {"result": {"frameTree": {"frame": {
        "url": "chrome-error://chromewebdata/", "unreachableUrl": "https://other.example/"
    }}}}
    assert w.heal(w.MINI_ZOOM, "sso_error", {}, dry=False, target="CHAT") == "none_target_changed"
    assert calls == [("CHAT", "Page.getFrameTree"), ("CHAT", "close")]


def test_zoom_signin_host_is_recognized_but_configured_reader_is_preferred(browser):
    pages, _, _ = browser
    signin = dict(page("LOGIN"), url="https://zoom.us/signin")
    pages.insert(0, signin)
    assert w.classify({"href": signin["url"]}) == "signed_out"
    assert w._zoom_page(w.MINI_ZOOM)[1]["id"] == "CHAT"
    assert w._zoom_page(w.MINI_ZOOM, [signin])[1] is signin
