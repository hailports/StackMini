"""Proves the Outlook note hides dumps and keeps a finding, a step, and real links."""
import handoff_mail as hm


def test_flow_id_and_question_stay_out():
    dump = (
        "Flow 301Ab00000xyzYzYAY is the approval path. "
        "Still open: what is the user id for the reviewer?"
    )
    plain = "The 2026 program rows exist, but the performance fields are still blank."
    step = "Check the 2026 program rows and fill the blank performance fields."
    paras = hm.note_paragraphs("program year enablement", [dump, plain], step)
    html = "\n".join(f"<p>{p}</p>" for p in paras)
    html += "\n" + hm.primary_button("http://127.0.0.1/n/1/tok")
    assert "301Ab00000xyzYzYAY" not in html
    assert "still open" not in html.lower()
    assert "user id" not in html.lower()
    assert "performance fields are still blank" in html
    assert "blank performance fields" in html
    assert "Prep is ready. Tap the button and it opens with the full prep already in it." not in html


def test_links_become_anchors():
    azure = "https://" + "dev." + "azure.com"
    ado1 = azure + "/ExampleOrg/Proj/_workitems/edit/100001"
    ado2 = azure + "/ExampleOrg/Proj/_workitems/edit/100002"
    text = "\n".join([
        "https://github.com/acme/widgets/pull/1303",
        "https://example.lightning.force.com/lightning/r/Case/500000000000001/view",
        ado1,
        ado2,
        "classes/Widget.cls",
    ])
    links = [("ADO 100001", ado1)]
    links.extend(hm.extract_work_links(text, "100001"))
    html = hm.links_html(links)
    assert 'href="https://github.com/acme/widgets/pull/1303"' in html
    assert ">PR 1303<" in html
    assert "lightning.force.com/lightning/r/Case/" in html
    assert ">Salesforce Case<" in html
    assert ">ADO 100001<" in html
    assert ">ADO 100002<" in html
    assert ".cls" not in html
    assert html.index("ADO 100001") < html.index("PR 1303")
    assert html.index("PR 1303") < html.index("Salesforce Case")


def test_no_stub_when_there_is_a_step():
    paras = hm.note_paragraphs(
        "Reset the portal password",
        [],
        "Add the identity check once security names who support must verify.",
    )
    assert paras
    assert all("Prep is ready" not in p for p in paras)
    assert "identity check" in paras[-1].lower()


def test_soql_and_api_names_drop():
    raw = (
        "SOQL queries confirm Widget__c rows exist. "
        "SELECT Id, Name FROM Account WHERE Name = 'x'. "
        "See `WidgetGuard.cls` and " + ("/" + "Users" + "/someone/proj/Foo.cls")
    )
    out = hm.sanitize_outlook(raw)
    assert "SELECT" not in out.upper()
    assert "__c" not in out
    assert ".cls" not in out
    assert "/Users/" not in out
    assert "SOQL" not in out.upper()


if __name__ == "__main__":
    test_flow_id_and_question_stay_out()
    test_links_become_anchors()
    test_no_stub_when_there_is_a_step()
    test_soql_and_api_names_drop()
    print("ok")
