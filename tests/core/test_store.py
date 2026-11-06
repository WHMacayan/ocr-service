"""The store: settings seeded once, secrets encrypted, changes kept."""


from app.core import store


def test_defaults_seeded_and_secrets_encrypted(db, tmp_path):
    assert store.get_setting(db, "max_retries", int) == 5
    assert store.get_setting(db, "poll_seconds", int) == 60
    assert store.get_setting(db, "openai_api_key") is None

    store.set_setting(db, "openai_api_key", "sk-test")
    raw = db.execute("SELECT value FROM setting WHERE key = 'openai_api_key'").fetchone()[0]
    assert raw != "sk-test"
    assert store.get_setting(db, "openai_api_key") == "sk-test"
    assert (tmp_path / "secret.key").exists()


def test_a_changed_setting_survives_a_restart(db, tmp_path):
    store.set_setting(db, "max_retries", 9)
    again = store.connect()
    assert store.get_setting(again, "max_retries", int) == 9
    again.close()
