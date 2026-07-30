"""Till slips are evidence, never a second outflow."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from spendtracker import analytics
from spendtracker.dedupe import match_receipt, rematch_all_receipts
from spendtracker.ingest import loader
from spendtracker.ingest.receipts import (
    ReceiptData,
    ReceiptItem,
    ignore_receipt,
    parse_receipt_text,
    store_receipt,
    update_receipt,
)
from spendtracker.periods import parse_period

from .conftest import write_csv

SLIP_TEXT = """\
CHECKERS HYPER FOURWAYS
Shop 12 Fourways Mall
VAT REG 4123456789
--------------------------------
2026/03/14  14:32   Till 04
--------------------------------
FRESH MILK 2L          1  32.99
BROWN BREAD            2  29.98
CHICKEN BRAAI PACK        89.99
--------------------------------
SUBTOTAL                 152.96
VAT 15%                   19.95
TOTAL                    152.96
--------------------------------
CASH TENDERED            200.00
CHANGE DUE                47.04
--------------------------------
THANK YOU
"""


class TestTextParser:
    def test_total_ignores_cash_tendered_and_change(self):
        data = parse_receipt_text(SLIP_TEXT)
        assert data.total_cents == 15296, "must not pick up 200.00 tendered or 47.04 change"

    def test_merchant_date_time_and_vat(self):
        data = parse_receipt_text(SLIP_TEXT)
        assert "Checkers" in data.merchant_norm
        assert data.receipt_date == date(2026, 3, 14)
        assert data.receipt_time == "14:32"
        assert data.vat_cents == 1995

    def test_cash_tender_is_inferred(self):
        assert parse_receipt_text(SLIP_TEXT).tender_type == "cash"

    def test_card_tender_is_inferred(self):
        text = "SPAR LONEHILL\n01/03/2026\nTOTAL 250.00\nVISA DEBIT CARD ****1234\nAPPROVED"
        data = parse_receipt_text(text)
        assert data.tender_type == "card"
        assert data.card_last4 == "1234"

    def test_no_total_line_falls_back_with_low_confidence(self):
        data = parse_receipt_text("SOME SHOP\n01/03/2026\n12.00\n45.00\n99.00\n")
        assert data.total_cents == 9900
        assert data.confidence is not None and data.confidence < 0.4
        assert "check" in (data.notes or "")

    def test_unreadable_text_yields_nothing_rather_than_a_guess(self):
        data = parse_receipt_text("")
        assert data.total_cents is None
        assert data.notes

    def test_line_item_mismatch_is_reported(self):
        data = ReceiptData(
            total_cents=50_000,
            items=[ReceiptItem("A", 1, 1000, 1000), ReceiptItem("B", 1, 2000, 2000)],
        )
        warning = data.consistency_warning()
        assert warning and "may have been missed" in warning

    def test_matching_line_items_produce_no_warning(self):
        data = ReceiptData(total_cents=3000, items=[ReceiptItem("A", 1, 3000, 3000)])
        assert data.consistency_warning() is None


# ---------------------------------------------------------------------------


def _ledger(conn, cfg, tmp_path):
    rows = [
        ("10/03/2026", "CARD PURCHASE 4067****1234 CHECKERS FOURWAYS", -1_200.00),
        ("12/03/2026", "ATM CASH WITHDRAWAL SANDTON", -2_000.00),
        ("15/03/2026", "CARD PURCHASE 4067****1234 WOOLWORTHS FOOD", -650.00),
        ("20/03/2026", "SALARY ACB CREDIT", 40_000.00),
    ]
    balance, out = 50_000.0, []
    for day, desc, amount in rows:
        balance += amount
        out.append([day, desc, f"{amount:.2f}", f"{balance:.2f}"])
    path = write_csv(tmp_path / "ledger.csv", ["Date", "Description", "Amount", "Balance"], out)
    loader.import_statement(conn, path, cfg=cfg)
    return parse_period("2026-03")


def _outflow(conn, cfg, period) -> int:
    return analytics.period_summary(conn, period, cfg=cfg).total_outflow_cents


class TestMatching:
    def test_card_slip_links_to_its_bank_row(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_raw="CHECKERS HYPER FOURWAYS", merchant_norm="Checkers",
                receipt_date=date(2026, 3, 10), total_cents=120_000,
                tender_type="card", category="Groceries", extractor="test",
            ),
        )
        row = conn.execute(
            "SELECT link_status, transaction_id, counts_as_outflow FROM receipts WHERE id=?",
            (result.receipt_id,),
        ).fetchone()
        assert row["link_status"] == "matched"
        assert row["transaction_id"] is not None
        assert row["counts_as_outflow"] == 0

    def test_matching_never_changes_the_total(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        before = _outflow(conn, cfg, period)
        store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
                total_cents=120_000, tender_type="card", extractor="test",
            ),
        )
        assert _outflow(conn, cfg, period) == before

    def test_card_purchase_posting_a_day_later_still_matches(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Woolworths", receipt_date=date(2026, 3, 14),
                total_cents=65_000, tender_type="card", extractor="test",
            ),
        )
        row = conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert row["link_status"] == "matched"

    def test_wrong_merchant_at_the_same_amount_does_not_match(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Totally Different Shop", receipt_date=date(2026, 3, 10),
                total_cents=120_000, tender_type="card", extractor="test",
            ),
        )
        row = conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert row["link_status"] == "unmatched"

    def test_unmatched_slip_is_excluded_from_totals(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        before = _outflow(conn, cfg, period)
        store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Mystery Shop", receipt_date=date(2026, 3, 11),
                total_cents=98_765, tender_type="card", extractor="test",
            ),
        )
        assert _outflow(conn, cfg, period) == before
        rec = analytics.period_summary(conn, period, cfg=cfg).reconciliation
        assert rec.receipts_unmatched == 1
        assert rec.receipts_unmatched_cents == 98_765
        assert any("could not be tied" in w for w in rec.warnings)

    def test_two_slips_cannot_claim_the_same_bank_row(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        common = dict(
            merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
            total_cents=120_000, tender_type="card", extractor="test",
        )
        first = store_receipt(conn, receipt_image("a.png"), cfg=cfg, account_id=1,
                             data=ReceiptData(**common))
        second = store_receipt(conn, receipt_image("b.png"), cfg=cfg, account_id=1,
                              data=ReceiptData(**common))
        statuses = [
            conn.execute("SELECT link_status FROM receipts WHERE id=?", (r.receipt_id,)).fetchone()["link_status"]
            for r in (first, second)
        ]
        assert statuses.count("matched") == 1, "one bank row cannot be two purchases"


class TestCash:
    def _cash_slip(self, conn, cfg, receipt_image, *, total=45_000, day=14, name="c.png"):
        return store_receipt(
            conn, receipt_image(name), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_raw="TOPS AT SPAR LONEHILL", merchant_norm="TOPS at SPAR",
                receipt_date=date(2026, 3, day), total_cents=total,
                tender_type="cash", category="Alcohol & Tobacco", extractor="test",
            ),
        )

    def test_cash_slip_is_allocated_against_a_withdrawal(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = self._cash_slip(conn, cfg, receipt_image)
        row = conn.execute(
            "SELECT link_status, transaction_id FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert row["link_status"] == "cash_allocated"
        assert row["transaction_id"] is None, "a cash slip has no card row to link to"
        allocated = conn.execute(
            "SELECT SUM(amount_cents) s FROM cash_allocations WHERE receipt_id=?",
            (result.receipt_id,),
        ).fetchone()["s"]
        assert allocated == 45_000

    def test_cash_slip_is_not_linked_to_a_same_amount_card_row(self, conn, cfg, tmp_path, receipt_image):
        """Regression: matching used to run before checking tender type, so a cash
        slip could be attached to an unrelated card purchase."""
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Woolworths", receipt_date=date(2026, 3, 15),
                total_cents=65_000, tender_type="cash", extractor="test",
            ),
        )
        row = conn.execute(
            "SELECT link_status, transaction_id FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert row["link_status"] == "cash_allocated"
        assert row["transaction_id"] is None

    def test_cash_allocation_does_not_change_the_total(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        before = _outflow(conn, cfg, period)
        self._cash_slip(conn, cfg, receipt_image)
        assert _outflow(conn, cfg, period) == before

    def test_cash_allocation_reclassifies_out_of_cash_withdrawals(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        before = {b.name: b.total_cents for b in
                  analytics.period_summary(conn, period, cfg=cfg).by_category}
        self._cash_slip(conn, cfg, receipt_image)
        after_summary = analytics.period_summary(conn, period, cfg=cfg)
        after = {b.name: b.total_cents for b in after_summary.by_category}

        assert after["Cash Withdrawals"] == before["Cash Withdrawals"] - 45_000
        assert after.get("Alcohol & Tobacco", 0) == before.get("Alcohol & Tobacco", 0) + 45_000
        assert after_summary.reconciliation.residual_cents == 0
        assert after_summary.reconciliation.cash_explained_cents == 45_000

    def test_cash_beyond_the_withdrawal_is_only_partly_explained(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        result = self._cash_slip(conn, cfg, receipt_image, total=300_000)
        row = conn.execute(
            "SELECT link_status, match_reason FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        allocated = conn.execute(
            "SELECT SUM(amount_cents) s FROM cash_allocations WHERE receipt_id=?",
            (result.receipt_id,),
        ).fetchone()["s"]
        assert allocated == 200_000, "cannot allocate more cash than was withdrawn"
        assert "could be traced" in row["match_reason"]
        assert _outflow(conn, cfg, period) == 385_000

    def test_two_cash_slips_share_one_withdrawal_without_exceeding_it(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        self._cash_slip(conn, cfg, receipt_image, total=150_000, name="c1.png")
        self._cash_slip(conn, cfg, receipt_image, total=150_000, name="c2.png")
        total_allocated = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM cash_allocations"
        ).fetchone()["s"]
        assert total_allocated <= 200_000
        assert analytics.period_summary(conn, period, cfg=cfg).reconciliation.residual_cents == 0

    def test_cash_slip_before_any_withdrawal_is_unmatched(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = self._cash_slip(conn, cfg, receipt_image, day=5)
        row = conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert row["link_status"] == "unmatched"


class TestStorage:
    def test_same_image_twice_is_a_no_op(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        path = receipt_image()
        first = store_receipt(conn, path, cfg=cfg, account_id=1,
                             data=ReceiptData(total_cents=1000, extractor="test"))
        second = store_receipt(conn, path, cfg=cfg, account_id=1)
        assert second.is_duplicate
        assert second.duplicate_of == first.receipt_id
        assert conn.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 1

    def test_same_slip_photographed_twice_is_flagged(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        common = dict(merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
                      total_cents=120_000, tender_type="card", extractor="test")
        store_receipt(conn, receipt_image("x.png"), cfg=cfg, account_id=1, data=ReceiptData(**common))
        second = store_receipt(conn, receipt_image("y.png"), cfg=cfg, account_id=1,
                              data=ReceiptData(**common))
        assert any("photographed one slip twice" in w for w in second.warnings)

    def test_line_items_are_stored(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
                total_cents=120_000, tender_type="card", extractor="test",
                items=[ReceiptItem("MILK", 1, 3299, 3299), ReceiptItem("BREAD", 2, 1499, 2998)],
            ),
        )
        count = conn.execute(
            "SELECT COUNT(*) c FROM receipt_items WHERE receipt_id=?", (result.receipt_id,)
        ).fetchone()["c"]
        assert count == 2

    def test_slip_with_no_total_warns_and_stays_unmatched(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(conn, receipt_image(), cfg=cfg, account_id=1,
                              data=ReceiptData(merchant_norm="Checkers", extractor="test"))
        assert any("no total" in w for w in result.warnings)

    def test_correcting_the_total_re_runs_matching(self, conn, cfg, tmp_path, receipt_image):
        _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
                total_cents=99_999, tender_type="card", extractor="test",
            ),
        )
        assert conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["link_status"] == "unmatched"

        update_receipt(conn, result.receipt_id, cfg=cfg, total_cents=120_000)
        assert conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["link_status"] == "matched"

    def test_dismissing_a_slip_releases_its_cash_allocation(self, conn, cfg, tmp_path, receipt_image):
        period = _ledger(conn, cfg, tmp_path)
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=1,
            data=ReceiptData(
                merchant_norm="TOPS at SPAR", receipt_date=date(2026, 3, 14),
                total_cents=45_000, tender_type="cash", category="Alcohol & Tobacco",
                extractor="test",
            ),
        )
        ignore_receipt(conn, result.receipt_id)
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM cash_allocations WHERE receipt_id=?", (result.receipt_id,)
        ).fetchone()["c"]
        assert remaining == 0
        summary = analytics.period_summary(conn, period, cfg=cfg)
        assert summary.reconciliation.cash_explained_cents == 0
        assert summary.reconciliation.residual_cents == 0

    def test_importing_a_statement_rematches_orphan_slips(self, conn, cfg, tmp_path, receipt_image):
        """A slip uploaded before its statement should attach once it arrives."""
        result = store_receipt(
            conn, receipt_image(), cfg=cfg, account_id=None,
            data=ReceiptData(
                merchant_norm="Checkers", receipt_date=date(2026, 3, 10),
                total_cents=120_000, tender_type="card", extractor="test",
            ),
        )
        assert conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["link_status"] == "unmatched"

        _ledger(conn, cfg, tmp_path)
        assert conn.execute(
            "SELECT link_status FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["link_status"] == "matched"

    def test_missing_file_raises(self, conn, cfg, tmp_path):
        with pytest.raises(FileNotFoundError):
            store_receipt(conn, tmp_path / "nope.png", cfg=cfg)
