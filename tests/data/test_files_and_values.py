"""Files and cell values: Excel and CSV in and out, and what a cell can say."""

import io
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

from app.shared.data import Column, auto_map, mapped_rows
from app.shared.data import convert
from app.shared.data.tables import CSV, XLSX, read_table, write_table


def test_a_table_goes_out_as_excel_and_comes_back_whole():
    data = write_table(["Name", "Count", "Active"], [["Quartz", 3, "Yes"], ["Apex", None, "No"]], XLSX, sheet="Vendors")
    headers, rows = read_table(data, "Vendors.xlsx")
    assert headers == ["Name", "Count", "Active"] and rows == [["Quartz", 3, "Yes"], ["Apex", None, "No"]]


def test_a_text_that_would_run_as_a_formula_is_written_as_text_in_both_formats():
    """An email's subject is whatever its sender wrote; opened in Excel, =HYPERLINK(...) would run."""
    evil = '=HYPERLINK("http://evil.test","click")'
    xlsx = load_workbook(io.BytesIO(write_table(["Subject"], [[evil], ["+1 call me"], ["-5"], ["@SUM(A1)"]], XLSX)))
    cells = [c.value for c in xlsx.active["A"][1:]]
    assert cells == ["'" + evil, "'+1 call me", "'-5", "'@SUM(A1)"] and all(c.data_type == "s" for c in xlsx.active["A"][1:])
    assert read_table(write_table(["Subject"], [[evil]], CSV), "x.csv")[1] == [["'" + evil]]
    # and it comes back as it was, for an import
    assert convert.to_text("'" + evil) == evil and convert.to_text("it's fine") == "it's fine"


def test_csv_is_read_with_its_bom_and_its_delimiter_and_empty_rows_are_dropped():
    raw = "﻿Name;Count\r\nQuartz;3\r\n;\r\nApex;4\r\n".encode("utf-8")
    assert read_table(raw, "x.csv") == (["Name", "Count"], [["Quartz", "3"], ["Apex", "4"]])
    assert read_table("Name,Count\nCafé,1\n".encode("cp1252"), "x.csv")[1] == [["Café", "1"]]


def test_a_file_that_is_not_a_table_is_refused_in_words():
    with pytest.raises(ValueError, match="Excel workbook"):
        read_table(b"x", "x.pdf")
    with pytest.raises(ValueError, match="no rows"):
        read_table(b"\n\n", "x.csv")
    with pytest.raises(ValueError, match="could not be opened"):
        read_table(b"not a workbook", "x.xlsx")


def test_cells_are_read_as_the_types_a_record_holds():
    assert convert.to_text(94659872.0) == "94659872"                      # a booking number Excel stored as a number
    assert convert.to_bool("yes") is True and convert.to_bool("No") is False and convert.to_bool(1) is True
    assert convert.to_int("1,200") == 1200 and convert.to_float("4,900.50") == 4900.5
    assert convert.to_date(datetime(2026, 11, 4, 8)) == "2026-11-04" and convert.to_date("2026-11-04 00:00:00") == "2026-11-04"
    assert convert.to_datetime(date(2026, 11, 4)) == "2026-11-04 00:00:00"
    assert convert.to_choice("ocean", (("ocean", "Ocean"), ("inland", "Inland"))) == "ocean"
    assert convert.to_choice("INLAND", (("ocean", "Ocean"), ("inland", "Inland"))) == "inland"
    for bad, call in (("maybe", convert.to_bool), ("1.5", convert.to_int), ("abc", convert.to_float)):
        with pytest.raises(ValueError):
            call(bad)


def test_a_date_typed_as_text_must_be_year_first_because_day_first_is_a_guess():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        convert.to_date("04/11/2026")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        convert.to_date("11-04-2026")


def test_a_choice_that_is_not_offered_names_what_is():
    with pytest.raises(ValueError, match="Ocean, Inland"):
        convert.to_choice("air", (("ocean", "Ocean"), ("inland", "Inland")))


def test_columns_are_matched_by_label_or_name_however_cased_and_a_field_is_taken_once():
    columns = [Column("booking_number", "Booking Number"), Column("etd", "ETD"), Column("state", "State", importable=False)]
    assert auto_map(["booking number", "ETD", "State", "Extra"], columns) == ["booking_number", "etd", "", ""]
    assert auto_map(["Booking_Number", "booking number"], columns) == ["booking_number", ""]
    assert mapped_rows([["BK1", "2026-11-04"], ["BK2", None]], ["booking_number", ""]) == [
        (2, {"booking_number": "BK1"}), (3, {"booking_number": "BK2"})]
