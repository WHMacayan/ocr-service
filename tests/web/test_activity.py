"""The activity log: its pager, and what is recorded."""


from fakes import signed_in


def test_activity_pages_like_odoo_and_takes_a_typed_range(client, db):
    signed_in(client)
    with db:
        db.executemany("INSERT INTO activity (kind, actor, summary) VALUES ('processing', 'worker', ?)",
                       [(f"Entry {n:03}",) for n in range(200)])
    total = db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    first = client.get("/activity").get_data(as_text=True)
    assert 'value="1-80"' in first and f"/ {total}" in first
    assert "Entry 199" in first and "Entry 119" not in first
    assert 'href="/activity?offset=80"' in first                        # next page
    second = client.get("/activity?offset=80").get_data(as_text=True)
    assert 'value="81-160"' in second and "Entry 119" in second and "Entry 199" not in second
    typed = client.get("/activity?range=1-150").get_data(as_text=True)  # typed into the pager
    assert 'value="1-150"' in typed and "Entry 060" in typed
    filtered = client.get("/activity?q=Entry+00").get_data(as_text=True)
    assert 'value="1-10"' in filtered and "/ 10" in filtered and 'name="q" value="Entry 00"' in filtered


def test_activity_records_settings_security_and_hides_secrets(client, db):
    token = signed_in(client)
    client.post("/settings", data={"csrf": token, "openai_model": "gpt-4o", "openai_api_key": "super-secret-key"})
    client.post("/connections/new", data={"csrf": token, "name": "Staging", "login": "admin",
                                          "api_key": "odoo-secret-key"})
    client.post("/subject-formats/new", data={"csrf": token, "name": "Maersk",
                                              "subject_pattern": r"BKG (?P<booking_number>\d+)"})
    rows = [dict(r) for r in db.execute("SELECT kind, summary, detail, actor FROM activity ORDER BY id")]

    assert rows[0]["kind"] == "security" and rows[0]["summary"] == "Admin password set on first start"
    settings = next(r for r in rows if r["summary"].startswith("Changed settings"))
    assert "Model: gpt-4o-mini → gpt-4o" in settings["detail"] and "OpenAI API key: changed" in settings["detail"]
    created = next(r for r in rows if r["summary"] == "Created Odoo connection Staging")
    assert "Login: (empty) → admin" in created["detail"] and "API key: changed" in created["detail"]
    assert "super-secret-key" not in str(rows) and "odoo-secret-key" not in str(rows)
    assert "super-secret-key" not in str(rows)
    assert any(r["summary"] == "Created subject format Maersk" for r in rows)
    assert rows[-1]["actor"].startswith("admin from")

    page = client.get("/activity?kind=settings").get_data(as_text=True)
    assert "Created subject format Maersk" in page and "Admin password set" not in page
