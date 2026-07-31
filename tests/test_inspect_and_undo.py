"""The two safety tools: preview before importing, undo after.

The properties that matter here are negative ones — preview must not write,
and undo must not leave the ledger in a state where totals are wrong — so most
of these assertions are about what did *not* change.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from spendtracker import inspect as inspect_mod
from spendtracker.config import Config
from spendtracker.ingest import csvimport, loader
from spendtracker.ingest.receipts import ReceiptData, store_receipt

from .conftest import write_csv

HEADER = ["Date", "Description", "Amount", "Balance"]
ROWS = [
    ["02/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", "-1200.00", "38800.00"],
    ["03/03/2026", "ATM CASH WITHDRAWAL SANDTON", "-2000.00", "36800.00"],
    ["05/03/2026", "SALARY ACB CREDIT", "40000.00", "76800.00"],
    ["07/03/2026", "CARD PURCHASE WOOLWORTHS HYDE PARK", "-640.50", "76159.50"],
]


@pytest.fixture
def statement(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "march.csv", HEADER, ROWS)


def _attach_slip(conn: sqlite3.Connection, cfg: Config, receipt_image) -> None:
    """Store a card slip that matches the Checkers row in ROWS."""
    store_receipt(
        conn,
        receipt_image("checkers.png"),
        cfg=cfg,
        account_id=1,
        data=ReceiptData(
            merchant_raw="CHECKERS FOURWAYS",
            merchant_norm="Checkers",
            receipt_date=date(2026, 3, 2),
            total_cents=120_000,
            tender_type="card",
            extractor="test",
        ),
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_reads_the_file_correctly(statement: Path) -> None:
    preview = inspect_mod.preview_file(statement)

    assert preview.row_count == 4
    assert preview.outflow_cents == 384_050   # 1200.00 + 2000.00 + 640.50
    assert preview.inflow_cents == 4_000_000
    assert preview.result.period_start == date(2026, 3, 2)
    assert preview.result.period_end == date(2026, 3, 7)


def test_preview_writes_nothing(
    statement: Path, conn: sqlite3.Connection
) -> None:
    """The whole point: you can look without committing to anything."""
    before = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
    inspect_mod.preview_file(statement)
    after = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]

    assert before == after == 0
    assert conn.execute("SELECT COUNT(*) n FROM statements").fetchone()["n"] == 0


def test_preview_surfaces_the_parser_warning_about_unsigned_amounts(
    tmp_path: Path,
) -> None:
    """A single amount column with no negatives and no balance is the classic
    ambiguous export. The parser assumes every row is an outflow and warns;
    the preview must put that warning in front of the user before import."""
    path = write_csv(
        tmp_path / "all_positive.csv",
        ["Date", "Description", "Amount"],
        [
            ["02/03/2026", "CARD PURCHASE CHECKERS", "1200.00"],
            ["03/03/2026", "CARD PURCHASE WOOLWORTHS", "640.50"],
            ["04/03/2026", "CARD PURCHASE ENGEN", "310.00"],
        ],
    )
    preview = inspect_mod.preview_file(path)

    assert preview.outflow_cents == 215_050
    assert preview.inflow_cents == 0
    assert any("treated as money out" in w for w in preview.result.warnings)
    assert "treated as money out" in inspect_mod.format_preview(preview)


def test_preview_flags_dates_it_could_not_disambiguate(tmp_path: Path) -> None:
    """Over a long span, no day above 12 means day-first was never confirmed."""
    path = write_csv(
        tmp_path / "ambiguous.csv",
        ["Date", "Description", "Amount", "Balance"],
        [
            ["01/03/2026", "CARD PURCHASE ONE", "-100.00", "900.00"],
            ["05/03/2026", "CARD PURCHASE TWO", "-100.00", "800.00"],
            ["02/04/2026", "CARD PURCHASE THREE", "-100.00", "700.00"],
            ["09/04/2026", "CARD PURCHASE FOUR", "-100.00", "600.00"],
        ],
    )
    preview = inspect_mod.preview_file(path)
    assert any("day above" in c for c in preview.concerns)


def test_preview_is_quiet_on_a_clean_file(statement: Path) -> None:
    preview = inspect_mod.preview_file(statement)
    assert preview.concerns == []
    assert "Safe to import" in inspect_mod.format_preview(preview)


def test_preview_raises_on_an_unparseable_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(csvimport.CsvFormatError):
        inspect_mod.preview_file(path)


def test_preview_dict_is_json_safe(statement: Path) -> None:
    import json

    payload = inspect_mod.preview_file(statement).to_dict()
    json.dumps(payload)  # must not raise
    assert payload["rows"] == 4
    assert payload["outflow_cents"] == 384_050


def test_preview_agrees_with_what_the_import_actually_does(
    statement: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    """Preview is worthless if it predicts something other than the truth."""
    preview = inspect_mod.preview_file(statement)
    report = loader.import_statement(conn, statement, cfg=cfg)

    assert report.rows_imported == preview.row_count
    assert report.outflow_cents == preview.outflow_cents
    assert report.inflow_cents == preview.inflow_cents


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_removes_exactly_what_the_import_added(
    statement: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    report = loader.import_statement(conn, statement, cfg=cfg)
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 4

    plan = inspect_mod.undo_statement(conn, report.statement_id)

    assert plan.transaction_count == 4
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM statements").fetchone()["n"] == 0


def test_undo_leaves_other_imports_untouched(
    tmp_path: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    march = write_csv(tmp_path / "march.csv", HEADER, ROWS)
    april = write_csv(
        tmp_path / "april.csv",
        HEADER,
        [
            ["02/04/2026", "CARD PURCHASE PICK N PAY", "-980.00", "75179.50"],
            ["04/04/2026", "DEBIT ORDER DISCOVERY", "-2450.00", "72729.50"],
        ],
    )
    first = loader.import_statement(conn, march, cfg=cfg)
    loader.import_statement(conn, april, cfg=cfg)
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 6

    inspect_mod.undo_statement(conn, first.statement_id)

    remaining = conn.execute(
        "SELECT description_raw FROM transactions ORDER BY txn_date"
    ).fetchall()
    assert len(remaining) == 2
    assert all("PICK N PAY" in r[0] or "DISCOVERY" in r[0] for r in remaining)


def test_undo_allows_a_clean_reimport(
    statement: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    """Undo must genuinely clear the fingerprints, not just hide the rows."""
    first = loader.import_statement(conn, statement, cfg=cfg)
    inspect_mod.undo_statement(conn, first.statement_id)

    second = loader.import_statement(conn, statement, cfg=cfg)

    assert second.rows_imported == 4
    assert second.already_imported is False
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 4


def test_plan_undo_changes_nothing(
    statement: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    report = loader.import_statement(conn, statement, cfg=cfg)
    plan = inspect_mod.plan_undo(conn, report.statement_id)

    assert plan.transaction_count == 4
    assert plan.outflow_cents == 384_050
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 4


def test_plan_undo_rejects_an_unknown_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        inspect_mod.plan_undo(conn, 999)


def test_undo_refuses_when_slips_are_linked(
    statement: Path, conn: sqlite3.Connection, cfg: Config, receipt_image
) -> None:
    """A linked slip is manual work. Losing it silently is the thing to avoid."""
    report = loader.import_statement(conn, statement, cfg=cfg)
    _attach_slip(conn, cfg, receipt_image)
    plan = inspect_mod.plan_undo(conn, report.statement_id)
    assert plan.linked_receipts == 1
    assert plan.blocked is True

    with pytest.raises(ValueError, match="force"):
        inspect_mod.undo_statement(conn, report.statement_id)

    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 4


def test_forced_undo_returns_slips_to_the_queue_rather_than_deleting_them(
    statement: Path, conn: sqlite3.Connection, cfg: Config, receipt_image
) -> None:
    report = loader.import_statement(conn, statement, cfg=cfg)
    _attach_slip(conn, cfg, receipt_image)

    inspect_mod.undo_statement(conn, report.statement_id, force=True)

    receipt = conn.execute("SELECT * FROM receipts").fetchone()
    assert receipt is not None, "the slip itself must survive"
    assert receipt["transaction_id"] is None
    assert receipt["link_status"] == "unmatched"
    assert receipt["counts_as_outflow"] == 0, "an unmatched slip still adds nothing"


def test_list_statements_reports_each_import(
    tmp_path: Path, conn: sqlite3.Connection, cfg: Config
) -> None:
    march = write_csv(tmp_path / "march.csv", HEADER, ROWS)
    loader.import_statement(conn, march, cfg=cfg)

    rows = inspect_mod.list_statements(conn)

    assert len(rows) == 1
    assert rows[0]["filename"] == "march.csv"
    assert rows[0]["rows_imported"] == 4
    assert rows[0]["outflow"] == 384_050
