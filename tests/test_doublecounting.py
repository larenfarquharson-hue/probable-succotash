"""The tests that matter most: nothing may ever be counted twice.

Three separate risks are covered:
  1. the same statement file imported again
  2. two exports whose date ranges overlap
  3. a till slip describing money already on the statement
"""

import os
import tempfile
import unittest
from pathlib import Path

from spendtrack import analysis, categorise, db, ingest, matching, slips, taxonomy

SIGNED = """\
Transaction Date,Description,Amount,Balance
01/06/2026,SALARY ACME PTY LTD,20000.00,20000.00
02/06/2026,CARD PURCHASE 4123****8891 CHECKERS HYPER SANDTON,-500.00,19500.00
03/06/2026,CARD PURCHASE 4123****8891 VIDA E CAFFE ROSEBANK,-52.00,19448.00
03/06/2026,CARD PURCHASE 4123****8891 VIDA E CAFFE ROSEBANK,-52.00,19396.00
04/06/2026,ATM CASH WITHDRAWAL SANDTON CITY,-1000.00,18396.00
05/06/2026,MONTHLY ACCOUNT FEE,-135.00,18261.00
"""

# Overlaps 03-05 June, adds 06 June. A different layout, same underlying data.
OVERLAP = """\
Date;Details;Debit;Credit
03-06-2026;CARD PURCHASE 4123****8891 VIDA E CAFFE ROSEBANK;52.00;
03-06-2026;CARD PURCHASE 4123****8891 VIDA E CAFFE ROSEBANK;52.00;
04-06-2026;ATM CASH WITHDRAWAL SANDTON CITY;1000.00;
05-06-2026;MONTHLY ACCOUNT FEE;135.00;
06-06-2026;CARD PURCHASE 4123****8891 WOOLWORTHS FOOD HYDE PARK;300.00;
"""


class DoubleCountingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SPENDTRACK_HOME"] = self.tmp.name
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.cat = categorise.Categoriser()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SPENDTRACK_HOME", None)
        self.tmp.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def outflows(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(-amount), 0) t FROM transactions WHERE amount < 0"
        ).fetchone()
        return round(float(row["t"]), 2)


class TestStatementReimport(DoubleCountingCase):
    def test_same_file_twice_changes_nothing(self):
        path = self.write("june.csv", SIGNED)
        first = ingest.import_statement(self.conn, path, categoriser=self.cat)
        total_after_first = self.outflows()

        second = ingest.import_statement(self.conn, path, categoriser=self.cat)
        self.assertEqual(first.inserted, 6)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, 6)
        self.assertEqual(self.outflows(), total_after_first)

    def test_genuine_same_day_repeats_both_survive(self):
        """Two identical coffees on one day are two transactions, not one."""
        self.write("june.csv", SIGNED)
        ingest.import_statement(self.conn, Path(self.tmp.name) / "june.csv",
                               categoriser=self.cat)
        count = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions WHERE amount = -52.0").fetchone()["n"]
        self.assertEqual(count, 2)

    def test_overlapping_periods_do_not_double_count(self):
        ingest.import_statement(self.conn, self.write("june.csv", SIGNED),
                               categoriser=self.cat)
        before = self.outflows()
        summary = ingest.import_statement(
            self.conn, self.write("overlap.csv", OVERLAP), categoriser=self.cat)

        # Four of the five rows already existed; only 6 June is new.
        self.assertEqual(summary.duplicates, 4)
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(self.outflows(), round(before + 300.0, 2))

    def test_dry_run_writes_nothing(self):
        path = self.write("june.csv", SIGNED)
        summary = ingest.import_statement(self.conn, path, dry_run=True,
                                         categoriser=self.cat)
        self.assertEqual(summary.inserted, 6)
        self.assertEqual(self.outflows(), 0.0)

    def test_different_account_is_not_a_duplicate(self):
        path = self.write("june.csv", SIGNED)
        ingest.import_statement(self.conn, path, account="cheque", categoriser=self.cat)
        summary = ingest.import_statement(self.conn, path, account="savings",
                                         categoriser=self.cat)
        self.assertEqual(summary.inserted, 6)


class TestSlipDoubleCounting(DoubleCountingCase):
    def setUp(self):
        super().setUp()
        ingest.import_statement(self.conn, self.write("june.csv", SIGNED),
                               categoriser=self.cat)
        self.baseline = self.outflows()

    def add_slip(self, **kwargs) -> slips.SlipSaveResult:
        from datetime import date
        slip = slips.Slip(
            merchant=kwargs.get("merchant", "Checkers Hyper Sandton"),
            slip_date=date.fromisoformat(kwargs.get("date", "2026-06-02")),
            slip_time=kwargs.get("time", "17:42"),
            total=kwargs.get("total", 500.00),
            payment_method=kwargs.get("method", "card"),
            card_last4=kwargs.get("last4"),
        )
        return slips.save_slip(self.conn, slip, categoriser=self.cat)

    def test_card_slip_adds_no_money(self):
        self.add_slip()
        report = matching.match_slips(self.conn)
        self.assertEqual(len(report.matched), 1)
        self.assertEqual(self.outflows(), self.baseline)

    def test_matched_slip_improves_the_category(self):
        """A slip names the shop, so it can beat a guess from the bank line."""
        self.conn.execute(
            "UPDATE transactions SET category = ?, category_source = 'fallback'"
            " WHERE amount = -500.0", (taxonomy.UNCATEGORISED,))
        self.conn.commit()
        self.add_slip()
        matching.match_slips(self.conn)
        row = self.conn.execute(
            "SELECT category, category_source FROM transactions WHERE amount = -500.0"
        ).fetchone()
        self.assertEqual(row["category"], "Groceries")
        self.assertEqual(row["category_source"], "slip")

    def test_manual_category_is_not_overridden_by_a_slip(self):
        self.conn.execute(
            "UPDATE transactions SET category = 'Gifts & Donations',"
            " category_source = 'manual' WHERE amount = -500.0")
        self.conn.commit()
        self.add_slip()
        matching.match_slips(self.conn)
        row = self.conn.execute(
            "SELECT category FROM transactions WHERE amount = -500.0").fetchone()
        self.assertEqual(row["category"], "Gifts & Donations")

    def test_same_slip_twice_is_refused(self):
        first = self.add_slip()
        second = self.add_slip()
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.slip_id, first.slip_id)

    def test_two_slips_cannot_claim_one_transaction(self):
        """Only one slip may be the evidence for a given statement line."""
        self.add_slip(merchant="Checkers Hyper Sandton", time="17:42")
        self.add_slip(merchant="Checkers Hyper Sandton", time="19:10")
        report = matching.match_slips(self.conn)
        self.assertEqual(len(report.matched), 1)
        self.assertEqual(len(report.unmatched) + len(report.over_cash), 1)
        self.assertEqual(self.outflows(), self.baseline)

    def test_cash_slip_reallocates_rather_than_adding(self):
        self.add_slip(merchant="Sandton Butchery", date="2026-06-05", total=400.0,
                      method="cash")
        report = matching.match_slips(self.conn)
        self.assertEqual(len(report.cash_allocated), 1)
        self.assertEqual(self.outflows(), self.baseline)

        position = matching.cash_position(self.conn, "2026-06-01", "2026-06-30")
        self.assertEqual(position["withdrawn"], 1000.0)
        self.assertEqual(position["explained"], 400.0)
        self.assertEqual(position["unexplained"], 600.0)

    def test_cash_slips_cannot_exceed_the_withdrawal(self):
        self.add_slip(merchant="Butchery A", date="2026-06-05", total=700.0,
                      method="cash")
        self.add_slip(merchant="Butchery B", date="2026-06-06", total=700.0,
                      method="cash")
        report = matching.match_slips(self.conn)
        self.assertEqual(len(report.cash_allocated), 1)
        self.assertEqual(len(report.over_cash), 1)
        self.assertEqual(self.outflows(), self.baseline)

    def test_unmatched_slip_is_reported_not_counted(self):
        self.add_slip(merchant="Somewhere Else", date="2026-06-20", total=1250.0,
                      method="card")
        report = matching.match_slips(self.conn)
        self.assertEqual(len(report.unmatched), 1)
        self.assertEqual(self.outflows(), self.baseline)

    def test_cash_reallocation_moves_value_sideways_only(self):
        self.add_slip(merchant="Sandton Butchery", date="2026-06-05", total=400.0,
                      method="cash")
        matching.match_slips(self.conn)
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))

        self.assertTrue(report.reconciliation.balances)
        categories = {b.name: b for b in report.categories}
        # The withdrawal was R1,000; R400 of it is now explained.
        self.assertEqual(categories[taxonomy.CASH].total, 600.0)
        self.assertIn("Groceries", categories)
        self.assertEqual(categories["Groceries"].from_cash, 400.0)
        self.assertEqual(
            round(sum(b.total for b in report.categories), 2),
            report.reconciliation.total_out)


class TestReconciliationInvariant(DoubleCountingCase):
    def test_buckets_always_add_back_to_the_total(self):
        ingest.import_statement(self.conn, self.write("june.csv", SIGNED),
                               categoriser=self.cat)
        ingest.import_statement(self.conn, self.write("overlap.csv", OVERLAP),
                               categoriser=self.cat)
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        rec = report.reconciliation
        self.assertTrue(rec.balances, f"off by {rec.difference}")
        self.assertEqual(rec.accounted, rec.total_out)
        self.assertEqual(rec.total_out, self.outflows())

    def test_savings_and_debt_are_not_counted_as_consumption(self):
        self.write("x.csv",
                   "Date,Description,Amount\n"
                   "01/06/2026,IB PAYMENT TO EASYEQUITIES ZAR WALLET,-3000.00\n"
                   "02/06/2026,IB PAYMENT TO WESBANK VEHICLE FINANCE,-5480.00\n"
                   "03/06/2026,CARD PURCHASE CHECKERS HYPER,-500.00\n")
        ingest.import_statement(self.conn, Path(self.tmp.name) / "x.csv",
                               positive_is="inflow", categoriser=self.cat)
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        rec = report.reconciliation
        self.assertEqual(rec.savings, 3000.0)
        self.assertEqual(rec.debt, 5480.0)
        self.assertEqual(rec.consumption, 500.0)
        self.assertTrue(rec.balances)

    def test_suggested_savings_do_not_overlap(self):
        """No transaction may be claimed by more than one suggestion."""
        self.write("y.csv",
                   "Date,Description,Amount\n"
                   "01/06/2026,SALARY ACME,20000.00\n"
                   "02/06/2026,CARD PURCHASE BETWAY ZA,-500.00\n"
                   "03/06/2026,DEBIT ORDER NETFLIX.COM 111,-199.00\n"
                   "04/06/2026,DEBIT ORDER SHOWMAX 222,-99.00\n"
                   "05/06/2026,CARD PURCHASE UBER EATS,-350.00\n"
                   "06/06/2026,MONTHLY ACCOUNT FEE,-135.00\n")
        ingest.import_statement(self.conn, Path(self.tmp.name) / "y.csv",
                               categoriser=self.cat)
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        claimed = sum(i.period_amount for i in report.insights if i.counts_to_total)
        # Claims cannot exceed the consumption they are drawn from.
        self.assertLessEqual(round(claimed, 2), report.reconciliation.consumption + 0.01)
        self.assertLessEqual(report.monthly_reducible, report.reconciliation.consumption)


if __name__ == "__main__":
    unittest.main()
