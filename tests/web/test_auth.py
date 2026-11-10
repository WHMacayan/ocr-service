"""Signing in: the first start, login and its lockout, the form token, the login cookie."""


from app.core import store
from fakes import csrf, signed_in


def test_first_start_asks_for_a_password_then_requires_login(client):
    assert client.get("/messages").headers["Location"].endswith("/setup")
    signed_in(client)
    assert client.get("/messages").status_code == 200

    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    assert client.get("/messages").headers["Location"] == "/login?next=/messages"
    token = csrf(client, "/login")
    assert "Wrong password" in client.post("/login", data={"csrf": token, "password": "nope"},
                                           follow_redirects=True).get_data(as_text=True)
    client.post("/login", data={"csrf": token, "password": "long-password"})
    assert client.get("/messages").status_code == 200


def test_post_without_csrf_token_is_refused(client, db):
    signed_in(client)
    page = client.post("/settings", data={"max_retries": "3"}, follow_redirects=True).get_data(as_text=True)
    assert "nothing was saved" in page and "Settings" in page
    assert store.get_setting(db, "max_retries", int) == 5


def test_an_expired_action_form_goes_back_to_the_page_it_came_from(client):
    signed_in(client)
    resp = client.post("/logout", data={"csrf": "old"}, headers={"Referer": "http://localhost/messages"})
    assert resp.headers["Location"] == "/messages"
    resp = client.post("/logout", data={"csrf": "old"}, headers={"Referer": "https://elsewhere.example/x"})
    assert resp.headers["Location"] == "/"


def test_the_login_has_a_cookie_of_its_own_install_and_lasts_30_days_from_the_last_visit(client):
    from app import session_cookie_name
    signed_in(client)
    name = client.application.config["SESSION_COOKIE_NAME"]
    assert name == session_cookie_name(client.application.secret_key) and name.startswith("ocr_session_")
    assert name != session_cookie_name("another copy's key")         # another copy on another port can't clash
    assert client.get_cookie(name) is not None and client.get_cookie("session") is None
    assert client.application.permanent_session_lifetime.days == 30
    first = client.get_cookie(name).expires
    client.get("/messages")
    assert client.get_cookie(name).expires >= first                    # each visit renews it


def test_signing_in_goes_back_to_the_page_that_asked(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    asked = client.get("/activity?kind=odoo")
    assert asked.headers["Location"] == "/login?next=/activity?kind%3Dodoo"
    page = client.get(asked.headers["Location"]).get_data(as_text=True)
    assert 'name="next" value="/activity?kind=odoo"' in page and 'data-reveal="password"' in page
    token = csrf(client, "/login")
    resp = client.post("/login", data={"csrf": token, "password": "long-password", "next": "/activity?kind=odoo"})
    assert resp.headers["Location"] == "/activity?kind=odoo"
    assert client.get("/login?next=/bookings").headers["Location"] == "/bookings"   # already signed in: straight there
    assert client.get("/messages", headers={"X-Live": "1"}).status_code == 200


def test_the_log_says_why_a_sign_in_was_needed(client, caplog):
    signed_in(client)
    name = client.application.config["SESSION_COOKIE_NAME"]
    good = client.get_cookie(name).value
    client.set_cookie(name, good[:-4] + "XXXX")                         # made by another copy, or damaged
    client.get("/messages")
    assert "the login cookie wasn't accepted" in caplog.text
    client.delete_cookie(name)
    client.get("/activity", headers={"X-Live": "1"})
    assert "Sign-in needed for GET /activity (background refresh): the browser sent no login cookie" in caplog.text


def test_signing_in_never_sends_you_to_another_site(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    for elsewhere in ("https://evil.example/x", "//evil.example/x", "/\evil.example", "javascript:alert(1)"):
        token = csrf(client, "/login")
        resp = client.post("/login", data={"csrf": token, "password": "long-password", "next": elsewhere})
        assert resp.headers["Location"] == "/messages"
        client.post("/logout", data={"csrf": csrf(client, "/messages")})
    assert client.get("/messages", headers={"X-Live": "1"}).headers["Location"] == "/login"   # a background refresh


def test_background_requests_keep_the_login_form_working(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    token = csrf(client, "/login")
    client.get("/favicon.ico")  # browsers ask for this after the page loads
    client.post("/login", data={"csrf": token, "password": "long-password"})
    assert client.get("/messages").status_code == 200


def test_login_locks_out_after_five_wrong_passwords(client):
    signed_in(client)
    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    token = csrf(client, "/login")
    for _ in range(5):
        assert client.post("/login", data={"csrf": token, "password": "nope"}).status_code == 200
    locked = client.post("/login", data={"csrf": token, "password": "long-password"})
    assert locked.status_code == 429
    assert client.get("/messages").headers["Location"] == "/login?next=/messages"


def test_changing_the_password_signs_out_other_browsers(client, db):
    from app import create_app
    other = create_app().test_client()
    signed_in(client)
    token = csrf(other, "/login")
    other.post("/login", data={"csrf": token, "password": "long-password"})
    assert other.get("/messages").status_code == 200

    token = csrf(client, "/password")
    client.post("/password", data={"csrf": token, "current": "long-password",
                                   "password": "another-password", "confirm": "another-password"})

    assert client.get("/messages").status_code == 200
    assert other.get("/messages").headers["Location"] == "/login?next=/messages"
