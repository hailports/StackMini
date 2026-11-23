from hailports.clip import localfirst, run

ME = "Morgan Hale, Morgan"


def _cfg(order):
    models = {
        "local": {"via": "http", "base_url": "http://127.0.0.1:18080/v1", "model": "m"},
        "brain": {"via": "ssh", "ssh_host": "brain", "remote_url": "http://127.0.0.1:18080/v1", "model": "m"},
        "cursor": {"via": "cursor", "bin": "/usr/local/bin/agent", "model": "cursor"},
        "claude": {"via": "claude", "bin": "/usr/local/bin/claude", "model": "claude"},
    }
    return {"paiste": {"me": ME, "order": list(order), "preferred": order[0], "models": models}}


GOOD = ("hey Dana,\n\nyes, the five placeholder accounts only give those order totals a home. "
        "they are not onboarded dealers and they do not replace the dealer accounts.\n\n"
        "fix is:\n1. map the ship-to numbers\n2. backfill the totals\n3. check the portal\n\n"
        "if you're good with that, we can get it done under a ticket.\n\nbest,\nMorgan")


def test_split_local_first():
    local, premium = localfirst.split(_cfg(["local", "cursor", "claude"]))
    assert local["paiste"]["order"] == ["local"] and local["paiste"]["preferred"] == "local"
    assert premium["paiste"]["order"] == ["cursor", "claude"]


def test_split_premium_first_is_untouched():
    assert localfirst.split(_cfg(["cursor", "local"])) is None
    assert localfirst.split(_cfg(["local", "brain"])) is None


def test_gate_passes_shaped_email():
    assert localfirst.gate(GOOD, "email", 180, ["Morgan Hale", "Morgan"]) == []


def test_gate_flags_long_dump_and_greeting():
    dump = "Hi Dana -- " + " ".join(f"C{i} has {i * 1000} orders." for i in range(70)) + "\n\nbest,\nMorgan"
    why = localfirst.gate(dump, "email", 180, ["Morgan Hale", "Morgan"])
    assert any("too long" in w for w in why)
    assert any("number dump" in w for w in why)
    assert any("hey" in w for w in why)


def test_gate_needs_signoff_on_email_only():
    body = GOOD.rsplit("\n\nbest,", 1)[0]
    assert any("sign-off" in w for w in localfirst.gate(body, "email", 180, ["Morgan Hale", "Morgan"]))
    assert localfirst.gate("hey Dana, yes, done by friday. ok?", "chat", 70, ["Morgan"]) == []


def _doc():
    return {"app": "Microsoft Outlook", "title": "accounts", "field_text": "From: Dana\nSent: today\nquestion"}


def test_local_pass_never_calls_premium(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAISTE_ENGINE_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def once(doc, base_url, model, copy, cfg=None, out=None):
        calls.append(cfg["paiste"]["order"])
        out.update(text=GOOD, hop="local")
        return 0

    monkeypatch.setattr(run, "_draft_scene_once", once)
    assert run.draft_scene(_doc(), "", "", False, _cfg(["local", "cursor"])) == 0
    assert calls == [["local"]]
    assert "hey Dana" in capsys.readouterr().out
    assert '"tier": "local"' in (tmp_path / "e.jsonl").read_text()


def test_local_fail_retries_shorter_then_premium(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAISTE_ENGINE_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))
    calls, short = [], []
    bad = "Hi Dana -- " + "word " * 300

    def once(doc, base_url, model, copy, cfg=None, out=None):
        calls.append(cfg["paiste"]["order"])
        if out is not None:
            out.update(text=bad, hop="local")
            return 0
        print("hey Dana,\n\npremium reply\n\nbest,\nMorgan")
        return 0

    def fake_complete(hops, messages):
        short.append([h["name"] for h in hops])
        return bad, "local"

    monkeypatch.setattr(run, "_draft_scene_once", once)
    monkeypatch.setattr(run, "complete", fake_complete)
    assert run.draft_scene(_doc(), "", "", False, _cfg(["local", "cursor", "claude"])) == 0
    assert calls == [["local"], ["cursor", "claude"]]
    assert short == [["local"]]
    log = (tmp_path / "e.jsonl").read_text()
    assert '"gate": "fail"' in log and '"tier": "premium"' in log
    assert "premium reply" in capsys.readouterr().out


def test_local_down_goes_premium(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAISTE_ENGINE_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))

    def once(doc, base_url, model, copy, cfg=None, out=None):
        if out is not None:
            print("model offline: local:URLError")
            return 2
        print("hey Dana,\n\npremium\n\nbest,\nMorgan")
        return 0

    monkeypatch.setattr(run, "_draft_scene_once", once)
    assert run.draft_scene(_doc(), "", "", False, _cfg(["local", "cursor"])) == 0
    out = capsys.readouterr().out
    assert "model offline" not in out and "premium" in out


def test_timeout_passes_to_ssh():
    cfg = _cfg(["brain", "cursor"])
    cfg["paiste"]["models"]["brain"]["timeout_s"] = 90
    from hailports.clip.hops import endpoints_from, ssh_argv
    hop = endpoints_from(cfg)[0]
    assert hop["timeout_s"] == 90
    assert "90" in ssh_argv(hop["ssh_host"], hop["base_url"], hop["timeout_s"])


PLAIN = ("From: Dana Reyes\nSent: Thursday, November 19, 2026 10:15 AM\nTo: Morgan Hale\nSubject: call\n\n"
         "hey morgan, can you join the call tomorrow to walk through the portal login flow?\n\n"
         "Dana Reyes\nMobile +1 217 555 0142\n1234 Main Street, Springfield IL 62701\n")
FIGS = ("From: Dana\nSent: today\nSubject: totals\n\n"
        "the C18764 account shows $12,400 and 108 hours, about 14% over plan. why?\n")


def test_classify_plain_thread_stays_local():
    heavy, why = localfirst.classify({"app": "Microsoft Outlook", "field_text": PLAIN})
    assert not heavy, why


def test_classify_figures():
    heavy, why = localfirst.classify({"app": "Microsoft Outlook", "field_text": FIGS})
    assert heavy and "figures" in why[0]


def test_classify_attachment_and_table():
    heavy, why = localfirst.classify({"field_text": "hey, see attached: Q3 totals.xlsx please check"})
    assert heavy and any("attachment" in w for w in why)
    heavy, why = localfirst.classify({"field_text": "hey can you check the attachment"}, "notes: see dealer map.pdf")
    assert heavy and any("lookup" in w for w in why)
    heavy, why = localfirst.classify({"field_text": "a | b | c\n1 | 2 | 3\n"})
    assert heavy and "table" in why


def test_premium_first_order():
    out = localfirst.premium_first(_cfg(["local", "claude", "cursor"]))
    assert out["paiste"]["order"] == ["cursor", "claude", "local"] and out["paiste"]["preferred"] == "cursor"


def test_figure_heavy_goes_premium_first(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAISTE_ENGINE_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def once(doc, base_url, model, copy, cfg=None, out=None):
        calls.append(cfg["paiste"]["order"])
        print("hey Dana,\n\npremium\n\nbest,\nMorgan")
        return 0

    monkeypatch.setattr(run, "_draft_scene_once", once)
    doc = {"app": "Microsoft Outlook", "title": "totals", "field_text": FIGS}
    assert run.draft_scene(doc, "", "", False, _cfg(["local", "cursor", "claude"])) == 0
    assert calls == [["cursor", "claude", "local"]]
    log = (tmp_path / "e.jsonl").read_text()
    assert '"route": "figures"' in log and "figures in thread" in log


def test_plain_thread_logs_route(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAISTE_ENGINE_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("HOME", str(tmp_path))

    def once(doc, base_url, model, copy, cfg=None, out=None):
        out.update(text=GOOD, hop="local")
        return 0

    monkeypatch.setattr(run, "_draft_scene_once", once)
    doc = {"app": "Microsoft Outlook", "title": "call", "field_text": PLAIN}
    assert run.draft_scene(doc, "", "", False, _cfg(["local", "cursor"])) == 0
    assert '"route": "plain"' in (tmp_path / "e.jsonl").read_text()


def test_unrelated_lookup_files_and_figures_do_not_count():
    lookup = "[case 2026-09-10] review deck l_Copy Review_091026.pptx | amount=$1450.00 | id=00523782 | hours=72"
    assert localfirst.classify({"field_text": PLAIN}, lookup) == (False, [])


def test_lookup_file_counts_when_thread_names_it_or_refers_to_attachment():
    lookup = "notes: totals are in dealer map.xlsx"
    heavy, why = localfirst.classify({"field_text": "hey, can you look at the dealer map.xlsx numbers?"}, lookup)
    assert heavy and any("dealer map.xlsx" in w for w in why)
    heavy, why = localfirst.classify({"field_text": "hey, did you see the file I attached this morning?"}, lookup)
    assert heavy and any("lookup" in w for w in why)


def test_thin_floor_only_for_long_incoming():
    short = "hey Dana,\n\nsure, tuesday at noon works. want me to book a table?\n\nbest,\nMorgan"
    me = ["Morgan Hale", "Morgan"]
    assert localfirst.gate(short, "email", 180, me, incoming=15) == []
    assert any("too thin" in w for w in localfirst.gate(short, "email", 180, me, incoming=120))


def test_figures_route_gives_cursor_60s_plain_keeps_45():
    out = localfirst.premium_first(_cfg(["local", "cursor"]))
    from hailports.clip.hops import endpoints_from, CURSOR_TIMEOUT_S
    assert endpoints_from(out)[0]["timeout_s"] == 60
    assert "timeout_s" not in endpoints_from(_cfg(["cursor", "local"]))[0] and CURSOR_TIMEOUT_S == 45


def test_slow_cursor_is_not_asked_twice(monkeypatch):
    from hailports.clip import hops
    clock = {"t": 0.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])
    asked = []

    def slow_cursor(endpoint, messages):
        asked.append("cursor")
        clock["t"] += 130
        return "hey Dana, first"

    monkeypatch.setattr(hops, "_cursor", slow_cursor)
    eps = [{"name": "cursor", "via": "cursor", "bin": "/x"}, {"name": "local", "via": "http", "base_url": "http://x/v1"}]
    hops.reset_slow()
    assert hops.complete(eps, [{"role": "user", "content": "q"}], http=lambda e, m: "hey Dana, local")[1] == "cursor"
    assert hops.complete(eps, [{"role": "user", "content": "q"}], http=lambda e, m: "hey Dana, local")[1] == "local"
    assert asked == ["cursor"]
    hops.reset_slow()
    assert hops.complete(eps, [{"role": "user", "content": "q"}], http=lambda e, m: "x")[1] == "cursor"
