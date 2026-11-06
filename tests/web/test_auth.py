"""Signing in: the first start, login and its lockout, the form token, the login cookie."""


from app.core import store
from fakes import csrf, signed_in


def test_first_start_asks_for_a_password_then_requires_login(client):
    assert client.get("/messages").headers["Location"].endswith("/setup")
    signed_in(client)
    assert client.get("/messages").status_code == 200

    client.post("/logout", data={"csrf": csrf(client, "/messages")})
    assert client.get("/messages").headers["Location"].endswith("/login")
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


def test_the_login_has_its_own_cookie_and_lasts_a_week(client):
    signed_in(client)
    cookie = client.get_cookie("ocr_session")
    assert cookie is not None and client.get_cookie("session") is None  # another local app's "session" can't clash
    assert client.application.permanent_session_lifetime.days == 7


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
    assert client.get("/messages").headers["Location"].endswith("/login")


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
    assert other.get("/messages").headers["Location"].endswith("/login")
