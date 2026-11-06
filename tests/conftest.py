import pytest

from app import create_app
from app.core import store
from app.documents import extract
from fakes import CONFIRMATION


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.web.auth._failures", {})
    connection = store.connect()
    yield connection
    connection.close()


@pytest.fixture
def client(db):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def ai(monkeypatch):
    """Stand-in for the file reading: counts calls, answers from ``ai.answer``."""
    class Reader:
        calls = 0
        answer = ("booking_confirmation", CONFIRMATION)

        def __call__(self, raw, purpose, settings):
            Reader.calls += 1
            return self.answer
    reader = Reader()
    monkeypatch.setattr(extract, "read_file", reader)
    return reader
