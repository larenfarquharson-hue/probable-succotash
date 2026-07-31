"""The offline HTML report.

The property that matters most is negative and easy to break silently: the file
must make no network requests. A stray CDN link would work fine on the machine
that generated it and fail on the aeroplane, or worse, quietly announce to a
third party that someone is reading their bank statement.

The rest is edge cases. Chart code divides by totals and takes maxima, so empty
periods, single categories and single-day ranges are where it breaks.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from spendtracker import analytics, report_html
from spendtracker.config import Config
from spendtracker.ingest import loader
from spendtracker.periods import parse_period

from .conftest import write_csv

HEADER = ["Date", "Description", "Amount", "Balance"]
ROWS = [
    ["02/03/2026", "BOND REPAYMENT ABSA HOMELOAN", "-12850.00", "37150.00"],
    ["03/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", "-1200.00", "35950.00"],
    ["05/03/2026", "SALARY ACB CREDIT", "40000.00", "75950.00"],
    ["07/03/2026", "CARD PURCHASE HOLLYWOODBETS ONLINE", "-800.00", "75150.00"],
    ["11/03/2026", "DEBIT ORDER NETFLIX.COM", "-199.00", "74951.00"],
    ["18/03/2026", "CARD PURCHASE WOOLWORTHS HYDE PARK", "-640.50", "74310.50"],
    ["25/03/2026", "ATM CASH WITHDRAWAL SANDTON", "-2000.00", "72310.50"],
]


def summarise(conn: sqlite3.Connection, cfg: Config, period: str = "2026-03"):
    return analytics.period_summary(conn, parse_period(period), cfg=cfg)


@pytest.fixture
def report(conn: sqlite3.Connection, cfg: Config, tmp_path: Path) -> str:
    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    return report_html.render_report(conn, summarise(conn, cfg), cfg=cfg)


# ---------------------------------------------------------------------------
# Self-contained: the whole point of the format
# ---------------------------------------------------------------------------


def test_nothing_is_loaded_from_the_network(report: str) -> None:
    urls = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', report)
    external = [u for u in urls if u.startswith(("http://", "https://", "//"))]
    assert external == [], f"report would phone home: {external}"


def test_there_are_no_scripts(report: str) -> None:
    """A static report needs none, and a file holding bank data should have none."""
    assert "<script" not in report.lower()
    assert "javascript:" not in report.lower()


def test_there_are_no_remote_stylesheets_or_fonts(report: str) -> None:
    assert "@import" not in report
    assert re.search(r"url\(", report) is None, "no external asset references"


def test_it_is_a_complete_document(report: str) -> None:
    assert report.startswith("<!DOCTYPE html>")
    assert report.rstrip().endswith("</html>")
    assert "<title>" in report


def test_it_declares_a_mobile_viewport(report: str) -> None:
    """It is written to be read on a phone."""
    assert 'name="viewport"' in report
    assert "width=device-width" in report


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_the_headline_figures_are_present(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    summary = summarise(conn, cfg)
    document = report_html.render_report(conn, summary, cfg=cfg)

    from spendtracker.money import fmt

    assert fmt(summary.spend_cents, cfg.currency_symbol) in document
    assert fmt(summary.total_inflow_cents, cfg.currency_symbol) in document


def test_merchants_and_categories_appear(report: str) -> None:
    assert "Checkers" in report
    assert "Groceries" in report or "Housing" in report


def test_the_reconciliation_verdict_is_stated(report: str) -> None:
    """The claim the whole app rests on should not be buried."""
    assert "Does this add up?" in report
    assert "balances" in report


def test_a_failed_reconciliation_is_stated_loudly(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    summary = summarise(conn, cfg)
    # Force a residual: the report must not present the numbers as trustworthy.
    summary.reconciliation.residual_cents = 12_345

    document = report_html.render_report(conn, summary, cfg=cfg)

    assert "does not balance" in document
    assert "unexplained" in document
    assert "provisional" in document


def test_the_footer_says_it_is_a_snapshot(report: str) -> None:
    """Someone opening a stale file should be able to tell that it is stale."""
    assert "snapshot" in report.lower()
    assert "generated" in report.lower()


def test_the_generation_time_is_shown(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    stamp = datetime(2026, 3, 31, 14, 5)
    document = report_html.render_report(
        conn, summarise(conn, cfg), cfg=cfg, generated_at=stamp
    )
    assert "31 Mar 2026" in document


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<script>alert(1)</script>", "&lt;script&gt;"),
        ("Joe & Sons", "&amp;"),
        ('say "hi"', "&quot;"),
    ],
)
def test_values_are_escaped(raw: str, expected: str) -> None:
    """Merchant names come from bank narration — text this program did not choose."""
    assert expected in report_html._e(raw)
    assert raw not in report_html._e(raw)


# ---------------------------------------------------------------------------
# Edge cases — where chart maths divides by zero
# ---------------------------------------------------------------------------


def test_an_empty_period_still_renders(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    document = report_html.render_report(conn, summarise(conn, cfg), cfg=cfg)

    assert document.startswith("<!DOCTYPE html>")
    assert document.rstrip().endswith("</html>")


def test_a_single_category_draws_a_full_ring(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """A 100% slice cannot be an arc — start and end coincide and it vanishes."""
    loader.import_statement(
        conn,
        write_csv(
            tmp_path / "one.csv",
            HEADER,
            [["05/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", "-500.00", "9500.00"]],
        ),
        cfg=cfg,
    )
    document = report_html.render_report(conn, summarise(conn, cfg), cfg=cfg)

    assert "<circle" in document, "a sole category must render as a full ring"


def test_a_single_day_skips_the_daily_chart(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    loader.import_statement(
        conn,
        write_csv(
            tmp_path / "one.csv",
            HEADER,
            [["05/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", "-500.00", "9500.00"]],
        ),
        cfg=cfg,
    )
    document = report_html.render_report(conn, summarise(conn, cfg), cfg=cfg)

    assert "Day by day" not in document, "one point is not a chart"


def test_inflow_only_periods_do_not_break(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """No spending at all means every share calculation divides by zero."""
    loader.import_statement(
        conn,
        write_csv(
            tmp_path / "in.csv",
            HEADER,
            [["05/03/2026", "SALARY ACB CREDIT", "40000.00", "40000.00"]],
        ),
        cfg=cfg,
    )
    document = report_html.render_report(conn, summarise(conn, cfg), cfg=cfg)

    assert document.rstrip().endswith("</html>")


def test_optional_sections_are_simply_omitted(report: str) -> None:
    """Called without advice or recurring, those sections do not appear at all."""
    assert "Where you could cut" not in report
    assert "Committed every month" not in report


def test_advice_shows_its_assumptions(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path
) -> None:
    """A saving estimate without its assumption is a prediction, which it is not."""
    from spendtracker import advice as advice_mod

    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    period = parse_period("2026-03")
    summary = analytics.period_summary(conn, period, cfg=cfg)
    advice = advice_mod.build_advice(conn, period, cfg=cfg, summary=summary)
    if not advice.findings:
        pytest.skip("no findings generated for this fixture")

    document = report_html.render_report(conn, summary, cfg=cfg, advice=advice)

    assert "Where you could cut" in document
    assert "Assumes:" in document
    assert "estimates" in document


# ---------------------------------------------------------------------------
# The CLI wiring
# ---------------------------------------------------------------------------


def test_the_cli_writes_the_file(
    conn: sqlite3.Connection, cfg: Config, tmp_path: Path, capsys
) -> None:
    from spendtracker.cli import main

    loader.import_statement(
        conn, write_csv(tmp_path / "march.csv", HEADER, ROWS), cfg=cfg
    )
    conn.commit()
    target = tmp_path / "report.html"

    code = main(
        [
            "--data-dir",
            str(cfg.data_dir),
            "report",
            "--period",
            "2026-03",
            "--html",
            str(target),
        ]
    )

    assert code == 0
    assert target.exists()
    document = target.read_text(encoding="utf-8")
    assert document.startswith("<!DOCTYPE html>")
    assert "Checkers" in document
