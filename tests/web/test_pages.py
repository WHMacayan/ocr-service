"""What every page does: times in the viewer's zone, security headers, field tooltips."""


from fakes import signed_in


def test_times_are_marked_for_the_viewers_time_zone(client):
    from app.web.ui import when
    assert when("2026-11-06 01:54:48") == ('<time datetime="2026-11-06T01:54:48Z" title="2026-11-06 01:54:48 UTC">'
                                          '2026-11-06 01:54:48 UTC</time>')
    assert when("2026-11-06T09:54:48+08:00").startswith('<time datetime="2026-11-06T01:54:48Z"')
    assert when(None) == "" and when("not a time") == "not a time"
    signed_in(client)
    page = client.get("/activity").get_data(as_text=True)
    assert '<time datetime="' in page and "(UTC)" not in page


def test_pages_send_security_headers_and_run_no_inline_script(client):
    signed_in(client)
    resp = client.get("/settings")
    policy = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "<script>" not in resp.get_data(as_text=True)


def test_every_field_explains_itself_in_a_tooltip(client):
    from app.web.forms import FORMS
    from app.web.settings import SETTING_FIELDS
    assert all(f.tip for form in FORMS.values() for f in form.fields)
    assert all(f.tip for f in SETTING_FIELDS)
    signed_in(client)
    page = client.get("/mailboxes/new").get_data(as_text=True)
    assert 'role="tooltip" id="tip-group_address"' in page


def test_short_lists_group_every_row_with_empty_values_last():
    from app.web.ui import group_rows, in_order
    rows = [{"v": "b"}, {"v": None}, {"v": "a"}, {"v": "b"}, {"v": ""}]
    groups = group_rows(rows, lambda r: r["v"], str.upper)
    assert [(g["label"], g["key"], g["count"]) for g in groups] == [("A", "a", 1), ("B", "b", 2), ("None", "", 2)]
    assert group_rows(rows)[0]["rows"] == rows and group_rows([]) == []
    assert in_order({"2026-11-01": 1, None: 1, "2026-11-03": 1}, reverse=True) == ["2026-11-03", "2026-11-01", None]
