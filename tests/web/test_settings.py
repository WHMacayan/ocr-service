"""The Settings page and its secrets."""


from app.core import store
from fakes import signed_in


def test_settings_page_saves_and_keeps_an_empty_secret(client, db):
    token = signed_in(client)
    form = {"csrf": token, "openai_api_key": "sk-1", "max_retries": "3", "openai_model": "gpt-4o-mini"}
    client.post("/settings", data=form)
    client.post("/settings", data=dict(form, openai_api_key=""))

    assert store.get_setting(db, "openai_api_key") == "sk-1"
    assert store.get_setting(db, "max_retries", int) == 3


def test_saved_secret_shows_its_last_characters_never_the_value(client, db):
    token = signed_in(client)
    client.post("/settings", data={"csrf": token, "openai_api_key": "sk-secret-value-wxyz"})

    page = client.get("/settings").get_data(as_text=True)
    assert "Saved, ends in <code>wxyz</code>" in page
    assert "sk-secret-value" not in page
