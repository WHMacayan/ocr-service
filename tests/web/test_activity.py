"""The activity log: its pager, and what is recorded."""


from fakes import signed_in


def test_activity_pages_like_odoo_and_takes_a_typed_range(client, db):
    signed_in(client)
    with db:
        db.executemany("INSERT INTO activity (kind, actor, summary) VALUES ('processing', 'worker', ?)",
                       [(f"Entry {n:03}",) for n in range(200)])
    total = db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    first = client.get("/activity?view=log").get_data(as_text=True)
    assert 'value="1-80"' in first and f"/ {total}" in first
    assert "Entry 199" in first and "Entry 119" not in first
    assert 'href="/activity?view=log&amp;offset=80"' in first                        # next page
    second = client.get("/activity?view=log&offset=80").get_data(as_text=True)
    assert 'value="81-160"' in second and "Entry 119" in second and "Entry 199" not in second
    typed = client.get("/activity?view=log&range=1-150").get_data(as_text=True)  # typed into the pager
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


def test_activity_groups_every_entry_and_opens_a_group_as_odoo_does(client, db):
    signed_in(client)
    with db:
        db.executemany("INSERT INTO activity (kind, actor, summary) VALUES (?, 'worker', ?)",
                       [("odoo" if n % 2 else "processing", f"Entry {n:03}") for n in range(200)])
    grouped = client.get("/activity?groupby=kind").get_data(as_text=True)
    assert 'aria-current="true">Type</a>' in grouped                       # chosen in the Group By menu
    assert grouped.index('data-group="odoo"') < grouped.index('data-group="processing"') < grouped.index('data-group="security"')
    assert '<span class="clip">Odoo writes</span><span class="n">100</span>' in grouped   # every entry, not a page
    assert "Entry 1" not in grouped and 'class="o-pager"' not in grouped    # folded until opened
    assert 'href="/activity?groupby=kind&amp;group=processing" data-group-toggle aria-expanded="false"' in grouped
    opened = client.get("/activity?groupby=kind&group=processing").get_data(as_text=True)
    assert 'data-group="processing" data-loaded' in opened and "Entry 198" in opened and "Entry 040" in opened
    assert "Entry 038" not in opened and "Entry 199" not in opened
    assert "<b>1-80</b> / 100" in opened                                    # the group's own pager
    assert 'href="/activity?groupby=kind&amp;group=processing&amp;offset=80" data-group-page aria-label="Next' in opened
    assert 'href="/activity?groupby=kind&amp;group=processing&amp;offset=80" data-group-page aria-label="Previous' in opened
    second = client.get("/activity?groupby=kind&group=processing&offset=80").get_data(as_text=True)
    assert "<b>81-100</b> / 100" in second and "Entry 038" in second and "Entry 000" in second and "Entry 040" not in second
    assert 'href="/activity?groupby=kind&amp;group=processing" data-group-page aria-label="Next' in second  # back to the first
    assert "data-group-page" not in client.get("/activity?groupby=kind&group=security").get_data(as_text=True)
    assert 'href="/activity?kind=odoo&amp;groupby=kind"' in grouped         # the type tabs keep the grouping
    assert 'data-group=' not in client.get("/activity").get_data(as_text=True)


def test_activity_overview_folds_routine_work_and_lifts_problems(client, db):
    signed_in(client)
    with db:
        db.execute("INSERT INTO mailbox (name, status, last_error) VALUES ('customs@x.test', 'error', 'Sign-in expired')")
        db.executemany("INSERT INTO activity (kind, level, actor, summary) VALUES (?, ?, ?, ?)", [
            ("processing", "info", "worker", "Processed: first"), ("odoo", "info", "worker", "Wrote booking A"),
            ("odoo", "info", "worker", "Wrote booking B"), ("settings", "info", "admin from 1.2.3.4", "Changed settings: Model"),
            ("processing", "error", "worker", "Could not read mailbox customs@x.test"),
            ("processing", "info", "worker", "Processed: second")])
    page = client.get("/activity").get_data(as_text=True)
    assert "Service: 1 email processed, 2 Odoo writes" in page          # the first run, folded
    assert "Service: 1 email processed, 0 Odoo writes" in page          # after the error and the admin's change
    assert "Changed settings: Model" in page and "Sign-in expired" in page
    attention = page[page.index('id="attention"'):page.index("History")]
    assert "Could not read mailbox customs@x.test" in attention and "Changed settings" not in attention
