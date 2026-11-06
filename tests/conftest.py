import pytest

from app.core import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.web.auth._failures", {})
    connection = store.connect()
    yield connection
    connection.close()
