"""The correctness core: never double count, never silently drop."""

from __future__ import annotations

from datetime import date

from spendtracker.dedupe import (
    assign_occurrences,
    fingerprint_description,
    row_fingerprint,
    score_duplicate,
)
from spendtracker.ingest import loader
from spendtracker.ingest.csvimport import ParsedRow

from .conftest import write_csv


# --- fingerprints ----------------------------------------------------------


def test_same_row_produces_the_same_fingerprint():
    a = row_fingerprint(date(2026, 3, 1), -35000, "CARD PURCHASE CHECKERS", balance_cents=965000)
    b = row_fingerprint(date(2026, 3, 1), -35000, "CARD PURCHASE CHECKERS", balance_cents=965000)
    assert a == b


def test_narration_noise_does_not_change_identity():
    """Two exports of the same purchase can word it differently."""
    a = row_fingerprint(date(2026, 3, 1), -35000, "CARD PURCHASE 4067****1234 CHECKERS FOURWAYS 01/03")
    b = row_fingerprint(date(2026, 3, 1), -35000, "POS PURCHASE CHECKERS FOURWAYS ZA")
    assert a == b


def test_different_balance_means_different_row():
    a = row_fingerprint(date(2026, 3, 1), -4250, "VIDA E CAFFE", balance_cents=100000)
    b = row_fingerprint(date(2026, 3, 1), -4250, "VIDA E CAFFE", balance_cents=95750)
    assert a != b


def test_occurrence_index_separates_identical_same_day_purchases():
    """Two coffees, same price, same day, no balance column: both must survive."""
    a = row_fingerprint(date(2026, 3, 3), -4250, "VIDA E CAFFE", occurrence=0)
    b = row_fingerprint(date(2026, 3, 3), -4250, "VIDA E CAFFE", occurrence=1)
    assert a != b


def test_assign_occurrences_counts_within_date_amount_and_merchant():
    rows = [
        ParsedRow(date(2026, 3, 3), "VIDA E CAFFE", -4250),
        ParsedRow(date(2026, 3, 3), "VIDA E CAFFE", -4250),
        ParsedRow(date(2026, 3, 3), "VIDA E CAFFE", -5000),   # different amount
        ParsedRow(date(2026, 3, 4), "VIDA E CAFFE", -4250),   # different day
    ]
    assert assign_occurrences(rows) == [0, 1, 0, 0]


def test_fingerprint_description_normalises_to_the_merchant():
    assert fingerprint_description("CARD PURCHASE 4067****1234 CHECKERS FOURWAYS ZA") == \
        fingerprint_description("checkers fourways")


# --- scoring ---------------------------------------------------------------


def _score(**kw):
    base = dict(
        new_date=date(2026, 3, 1), new_amount=-35000, new_desc="CHECKERS FOURWAYS",
        new_balance=None, existing_date=date(2026, 3, 1), existing_amount=-35000,
        existing_desc="CHECKERS FOURWAYS", existing_balance=None, same_statement=False,
    )
    base.update(kw)
    return score_duplicate(**base)


def test_different_amount_is_never_a_duplicate():
    score, reason = _score(existing_amount=-36000)
    assert score == 0.0 and "amount" in reason


def test_different_merchant_is_never_a_duplicate():
    score, reason = _score(existing_desc="WOOLWORTHS SANDTON")
    assert score == 0.0 and "merchant" in reason


def test_rows_from_the_same_statement_are_never_duplicates():
    """The bank listed them separately, so they are separate."""
    score, reason = _score(same_statement=True)
    assert score == 0.0 and "same statement" in reason


def test_differing_balances_prove_distinctness():
    score, reason = _score(new_balance=100000, existing_balance=95750)
    assert score == 0.0 and "balance" in reason


def test_matching_balances_give_near_certainty():
    score, _ = _score(new_balance=100000, existing_balance=100000)
    assert score >= 0.95


def test_same_day_amount_and_merchant_is_treated_as_duplicate():
    score, _ = _score()
    assert score >= 0.90


def test_a_day_apart_is_flagged_but_not_assumed():
    score, _ = _score(existing_date=date(2026, 2, 28))
    assert 0.55 <= score < 0.90


def test_far_apart_is_not_a_duplicate():
    score, _ = _score(existing_date=date(2026, 2, 1))
    assert score == 0.0


# --- end-to-end import behaviour -------------------------------------------


def _active_outflow(conn) -> int:
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(-amount_cents),0) c FROM transactions "
            "WHERE status='active' AND amount_cents<0"
        ).fetchone()["c"]
    )


def test_reimporting_the_identical_file_is_a_no_op(conn, cfg, signed_statement):
    path = signed_statement()
    first = loader.import_statement(conn, path, cfg=cfg)
    before = _active_outflow(conn)

    second = loader.import_statement(conn, path, cfg=cfg)
    assert second.already_imported is True
    assert _active_outflow(conn) == before
    assert first.rows_imported == 7


def test_two_identical_same_day_purchases_both_survive(conn, cfg, signed_statement):
    loader.import_statement(conn, signed_statement(), cfg=cfg)
    count = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE merchant_norm LIKE 'Vida%' AND status='active'"
    ).fetchone()["c"]
    assert count == 2, "the two identical coffees must not be collapsed into one"


def test_overlapping_statements_deduplicate_without_losing_new_rows(conn, cfg, tmp_path):
    """The normal way people export: a fresh range that overlaps the last one."""
    shared = [
        ("10/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", -350.00),
        ("11/03/2026", "DEBIT ORDER NETFLIX.COM", -199.00),
        ("12/03/2026", "CARD PURCHASE ENGEN FOURWAYS", -800.00),
    ]
    later = [("13/03/2026", "CARD PURCHASE WOOLWORTHS", -420.00)]

    def build(name, rows, opening):
        balance, out = opening, []
        for day, desc, amount in rows:
            balance += amount
            out.append([day, desc, f"{amount:.2f}", f"{balance:.2f}"])
        return write_csv(tmp_path / name, ["Date", "Description", "Amount", "Balance"], out)

    first = build("a.csv", shared, 10_000.0)
    second = build("b.csv", shared + later, 10_000.0)

    r1 = loader.import_statement(conn, first, cfg=cfg)
    r2 = loader.import_statement(conn, second, cfg=cfg)

    assert r1.rows_imported == 3
    assert r2.rows_imported == 1, "only the genuinely new row should be added"
    assert r2.rows_duplicate_exact == 3
    assert _active_outflow(conn) == 176_900


def test_repeat_purchase_outside_prior_coverage_is_not_flagged(conn, cfg, tmp_path):
    """Same shop, same amount, a month later. That is a repeat purchase, not a
    duplicate - flagging it was the main false-positive risk."""

    def build(name, day, opening):
        return write_csv(
            tmp_path / name,
            ["Date", "Description", "Amount", "Balance"],
            [[day, "CARD PURCHASE CHECKERS FOURWAYS", "-350.00", f"{opening - 350:.2f}"]],
        )

    loader.import_statement(conn, build("m1.csv", "10/03/2026", 10_000.0), cfg=cfg)
    report = loader.import_statement(conn, build("m2.csv", "10/04/2026", 9_000.0), cfg=cfg)

    assert report.rows_imported == 1
    assert report.rows_flagged_duplicate == 0
    assert report.rows_flagged_review == 0
    pending = conn.execute(
        "SELECT COUNT(*) c FROM duplicate_candidates WHERE resolution='pending'"
    ).fetchone()["c"]
    assert pending == 0


def test_suspected_duplicate_is_held_out_and_reinstatable(conn, cfg, tmp_path):
    """Without a balance to prove it, an overlap match is held out of totals but
    stays visible and reversible."""

    def build(name, rows):
        return write_csv(
            tmp_path / name, ["Date", "Description", "Amount"],
            [[d, s, f"{a:.2f}"] for d, s, a in rows],
        )

    # First statement establishes coverage over 10-12 March.
    loader.import_statement(
        conn,
        build("p1.csv", [
            ("10/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", -350.00),
            ("12/03/2026", "CARD PURCHASE ENGEN", -800.00),
        ]),
        cfg=cfg,
    )
    # Second statement re-reports 11 March only, one day off the first row, so the
    # occurrence fingerprint differs and the data cannot prove either way.
    report = loader.import_statement(
        conn, build("p2.csv", [("11/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", -350.00)]), cfg=cfg
    )

    candidates = conn.execute(
        "SELECT id, resolution FROM duplicate_candidates WHERE resolution='pending'"
    ).fetchall()
    assert candidates, "an unprovable overlap match must be surfaced, not guessed"

    from spendtracker.dedupe import resolve_candidate

    # Nothing was deleted - it can be reinstated.
    resolve_candidate(conn, int(candidates[0]["id"]), "distinct")
    statuses = {
        r["status"]
        for r in conn.execute("SELECT status FROM transactions WHERE merchant_norm='Checkers'")
    }
    assert statuses == {"active"}


def test_inflows_are_categorised_as_income_not_spend(conn, cfg, signed_statement):
    loader.import_statement(conn, signed_statement(), cfg=cfg)
    row = conn.execute(
        "SELECT category FROM transactions WHERE amount_cents > 0"
    ).fetchone()
    assert row["category"] == "Income"


def test_cash_withdrawal_is_flagged(conn, cfg, signed_statement):
    loader.import_statement(conn, signed_statement(), cfg=cfg)
    row = conn.execute(
        "SELECT is_cash_withdrawal, category FROM transactions WHERE description_raw LIKE '%ATM%'"
    ).fetchone()
    assert row["is_cash_withdrawal"] == 1
    assert row["category"] == "Cash Withdrawals"
