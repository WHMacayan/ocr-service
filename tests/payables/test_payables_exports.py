"""The export folder: picked inside the one folder shared with the service, typed short or pasted whole."""

import pytest

from app.payables import exports

ROOT = r"C:\Users\me\OneDrive - ATV INC"


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYABLES_EXPORT_HOST", ROOT)
    monkeypatch.setenv("PAYABLES_EXPORT_MOUNT", str(tmp_path))
    return tmp_path


def test_a_folder_is_typed_after_the_shared_folder_or_pasted_whole(shared):
    assert exports.relative(r"OCR AP\Exports") == (r"OCR AP\Exports", None)
    assert exports.relative("OCR AP/Exports/ ") == (r"OCR AP\Exports", None)
    assert exports.relative(r"c:\users\ME\OneDrive - ATV INC\OCR AP\Exports") == (r"OCR AP\Exports", None)  # any case
    assert exports.folder(r"OCR AP\Exports") == shared / "OCR AP" / "Exports"
    assert exports.full_path(r"OCR AP\Exports") == ROOT + r"\OCR AP\Exports"
    assert exports.root_name() == "OneDrive - ATV INC" and exports.default_folder() == r"OCR AP\Exports"


def test_folders_outside_the_shared_one_are_refused(shared):
    for typed in (r"C:\Users\me\Documents\Exports", r"D:\OneDrive - ATV INC\x", r"C:\Users\me", r"..\Documents",
                  r"C:\Users\me\OneDrive - ATV INC\..\Documents"):
        rel, error = exports.relative(typed)
        assert rel is None and error


def test_nothing_can_be_chosen_until_a_folder_is_shared(monkeypatch):
    monkeypatch.delenv("PAYABLES_EXPORT_HOST", raising=False)
    assert exports.relative("OCR AP") == (None, exports.NOT_SHARED) and "administrator" in exports.NOT_SHARED


def test_a_server_folder_matches_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYABLES_EXPORT_HOST", "/srv/ocr-exports")
    monkeypatch.setenv("PAYABLES_EXPORT_MOUNT", str(tmp_path))
    assert exports.relative("/srv/ocr-exports/run 1") == ("run 1", None)
    assert exports.relative("/srv/OCR-exports/run 1")[0] is None                   # Linux folder names keep their case
    assert exports.full_path("run 1") == "/srv/ocr-exports/run 1"


def test_check_makes_a_missing_folder_and_leaves_nothing_behind(shared):
    assert exports.check(r"OCR AP\Exports") == (r"OCR AP\Exports", None)
    assert (shared / "OCR AP" / "Exports").is_dir() and not list((shared / "OCR AP" / "Exports").iterdir())


def test_check_says_when_the_folder_cannot_be_used(shared):
    (shared / "taken").write_text("a file, not a folder")
    rel, error = exports.check("taken")
    assert rel is None and "can't be used" in error


def test_each_export_gets_the_next_run_folder(shared):
    assert exports.next_run(r"OCR AP\Exports") == shared / "OCR AP" / "Exports" / "Run 1"   # not made yet: Run 1
    for name in ("Run 9", "run 11", "Run 10", "Run 7 old", "Notes"):
        (shared / "OCR AP" / "Exports" / name).mkdir(parents=True)
    assert exports.next_run(r"OCR AP\Exports") == shared / "OCR AP" / "Exports" / "Run 12"


def test_existing_folders_are_suggested_two_levels_down(shared):
    for folder in ("OCR AP/Run 11/deep", "Finance", ".hidden"):
        (shared / folder).mkdir(parents=True)
    assert exports.suggestions() == ["Finance", "OCR AP", r"OCR AP\Run 11"]
