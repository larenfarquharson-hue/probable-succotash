"""Statement parsing: the layouts, and the sign-convention safety net."""

from __future__ import annotations

from datetime import date

import pytest

from spendtracker.ingest import csvimport
from spendtracker.ingest.csvimport import CsvFormatError, detect_dayfirst, parse_statement

from .conftest import write_csv


def test_signed_amount_layout(signed_statement):
    result = parse_statement(signed_statement())
    assert len(result.rows) == 7
    assert result.column_map.txn_date == 0
    assert result.column_map.amount == 2
    assert result.column_map.balance == 3
    assert result.rows[0].txn_date == date(2026, 3, 1)
    assert result.rows[0].amount_cents == -35000
    # The preamble must be skipped, not parsed as data.
    assert any("preamble" in n for n in result.column_map.detection_notes)
    # And the signs must be confirmed against the balance column.
    assert any("confirmed" in n for n in result.column_map.detection_notes)
    assert result.warnings == []


def test_debit_credit_layout_with_semicolons_and_space_thousands(debit_credit_statement):
    result = parse_statement(debit_credit_statement())
    assert result.delimiter == ";"
    assert result.column_map.debit >= 0 and result.column_map.credit >= 0
    amounts = [r.amount_cents for r in result.rows]
    assert amounts == [-35000, -129900, 2500000]


def test_inverted_signs_are_repaired_against_the_balance(tmp_path):
    """A statement whose outflows are positive must be corrected, not trusted.

    This is the case that would otherwise invert an entire statement and report
    spending as income.
    """
    rows, balance = [], 10_000.0
    for day, desc, amount in [
        ("01/03/2026", "CARD PURCHASE CHECKERS", -350.00),
        ("02/03/2026", "DEBIT ORDER NETFLIX", -199.00),
        ("03/03/2026", "CARD PURCHASE ENGEN", -800.00),
        ("04/03/2026", "SALARY", 25_000.00),
    ]:
        balance += amount
        # Deliberately wrong convention: outflows written positive.
        rows.append([day, desc, f"{-amount:.2f}", f"{balance:.2f}"])
    path = write_csv(tmp_path / "inverted.csv", ["Date", "Description", "Amount", "Balance"], rows)

    result = parse_statement(path)
    assert [r.amount_cents for r in result.rows] == [-35000, -19900, -80000, 2500000]
    assert any("inverted" in n for n in result.column_map.detection_notes)


def test_month_first_dates_are_detected(tmp_path):
    rows = [
        ["03/01/2026", "PURCHASE A", "-100.00"],
        ["03/25/2026", "PURCHASE B", "-200.00"],   # 25 proves month-first
    ]
    path = write_csv(tmp_path / "mdy.csv", ["Posting Date", "Details", "Amount"], rows)
    result = parse_statement(path)
    assert result.column_map.dayfirst is False
    assert result.rows[0].txn_date == date(2026, 3, 1)
    assert result.rows[1].txn_date == date(2026, 3, 25)


def test_day_first_is_the_default_when_ambiguous(tmp_path):
    rows = [["03/01/2026", "PURCHASE A", "-100.00"], ["04/02/2026", "PURCHASE B", "-200.00"]]
    path = write_csv(tmp_path / "ambig.csv", ["Date", "Details", "Amount"], rows)
    result = parse_statement(path)
    assert result.column_map.dayfirst is True
    assert result.rows[0].txn_date == date(2026, 1, 3)


def test_detect_dayfirst_evidence():
    assert detect_dayfirst(["25/03/2026", "01/02/2026"]) is True
    assert detect_dayfirst(["03/25/2026", "02/01/2026"]) is False
    assert detect_dayfirst(["01/02/2026"]) is True         # ambiguous -> day first


def test_parenthesised_amounts_are_outflows(tmp_path):
    rows = [["01/03/2026", "PURCHASE", "(160.96)"], ["02/03/2026", "REFUND", "40.00"]]
    path = write_csv(tmp_path / "paren.csv", ["Date", "Details", "Transaction Amount"], rows)
    result = parse_statement(path)
    assert [r.amount_cents for r in result.rows] == [-16096, 4000]


def test_trailing_total_rows_are_skipped_not_parsed(signed_statement):
    path = signed_statement()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n,Closing Balance,,33283.00\n")
    result = parse_statement(path)
    assert len(result.rows) == 7
    assert len(result.skipped) == 1
    assert "date" in result.skipped[0][1]


def test_multiple_description_columns_are_joined(tmp_path):
    rows = [["01/03/2026", "CARD", "PURCHASE CHECKERS FOURWAYS", "-350.00"]]
    path = write_csv(
        tmp_path / "multi.csv", ["Date", "Reference", "Details", "Amount"], rows
    )
    result = parse_statement(path)
    assert result.column_map.description == [1, 2]
    assert result.rows[0].description == "CARD PURCHASE CHECKERS FOURWAYS"


def test_all_positive_single_column_warns_loudly(tmp_path):
    """No negatives and no balance means the convention is unknowable."""
    rows = [["01/03/2026", "PURCHASE A", "100.00"], ["02/03/2026", "PURCHASE B", "200.00"]]
    path = write_csv(tmp_path / "pos.csv", ["Date", "Details", "Amount"], rows)
    result = parse_statement(path)
    assert all(r.amount_cents < 0 for r in result.rows)
    assert result.warnings, "silently guessing here would misreport deposits as spend"
    assert "money out" in result.warnings[0]


def test_missing_amount_column_is_an_error(tmp_path):
    path = write_csv(tmp_path / "bad.csv", ["Date", "Notes"], [["01/03/2026", "hello"]])
    with pytest.raises(CsvFormatError, match="amount"):
        parse_statement(path)


def test_empty_file_is_an_error(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(CsvFormatError):
        parse_statement(path)


def test_headerless_file_is_inferred_with_a_warning(tmp_path):
    rows = [
        ["01/03/2026", "CHECKERS FOURWAYS", "-350.00", "9650.00"],
        ["02/03/2026", "NETFLIX", "-199.00", "9451.00"],
        ["03/03/2026", "ENGEN GARAGE", "-800.00", "8651.00"],
    ]
    path = tmp_path / "noheader.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        import csv as _csv

        _csv.writer(fh).writerows(rows)
    result = parse_statement(path)
    assert len(result.rows) == 3
    assert result.warnings, "an inferred layout must be flagged, not presented as certain"


def test_cp1252_encoding_is_handled(tmp_path):
    path = tmp_path / "cp1252.csv"
    path.write_bytes(
        "Date,Description,Amount\r\n01/03/2026,CAF\xc9 SOCIETY,-42.50\r\n".encode("cp1252")
    )
    result = parse_statement(path)
    assert len(result.rows) == 1
    assert "CAF" in result.rows[0].description


def test_opening_and_closing_balances_are_derived(signed_statement):
    result = parse_statement(signed_statement(opening=10_000.0))
    assert result.opening_balance_cents == 1_000_000
    assert result.closing_balance_cents == result.rows[-1].balance_cents


def test_sha256_differs_between_files(signed_statement, debit_credit_statement):
    a = parse_statement(signed_statement())
    b = parse_statement(debit_credit_statement())
    assert a.file_sha256 != b.file_sha256
