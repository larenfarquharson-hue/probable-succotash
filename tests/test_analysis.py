"""Period handling, recurring detection and the shape of the suggestions."""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from spendtrack import analysis, categorise, db, ingest, report_html, report_text


class TestParsePeriod(unittest.TestCase):
    def test_month(self):
        period = analysis.parse_period("2026-06")
        self.assertEqual((period.start, period.end), ("2026-06-01", "2026-06-30"))
        self.assertEqual(period.days, 30)

    def test_february_leap_year(self):
        self.assertEqual(analysis.parse_period("2024-02").end, "2024-02-29")

    def test_year(self):
        period = analysis.parse_period("2026")
        self.assertEqual((period.start, period.end), ("2026-01-01", "2026-12-31"))

    def test_explicit_range(self):
        period = analysis.parse_period("2026-06-15:2026-07-14")
        self.assertEqual((period.start, period.end), ("2026-06-15", "2026-07-14"))
        self.assertEqual(period.days, 30)

    def test_single_day(self):
        period = analysis.parse_period("2026-06-15")
        self.assertEqual(period.days, 1)

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            analysis.parse_period("sometime last winter")

    def test_all_needs_a_connection(self):
        with self.assertRaises(ValueError):
            analysis.parse_period("all")


class AnalysisCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SPENDTRACK_HOME"] = self.tmp.name
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.cat = categorise.Categoriser()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SPENDTRACK_HOME", None)
        self.tmp.cleanup()

    def load(self, content: str, **kwargs):
        path = Path(self.tmp.name) / f"s{len(list(Path(self.tmp.name).glob('*.csv')))}.csv"
        path.write_text(content, encoding="utf-8")
        return ingest.import_statement(self.conn, path, categoriser=self.cat, **kwargs)


THREE_MONTHS = """\
Date,Description,Amount
01/05/2026,SALARY ACME,20000.00
02/05/2026,DEBIT ORDER NETFLIX.COM 111,-199.00
03/05/2026,PREPAID ELECTRICITY CITY POWER,-900.00
04/05/2026,CARD PURCHASE VIDA E CAFFE,-52.00
05/05/2026,CARD PURCHASE VIDA E CAFFE,-52.00
06/05/2026,CARD PURCHASE VIDA E CAFFE,-52.00
07/05/2026,CARD PURCHASE VIDA E CAFFE,-52.00
08/05/2026,TRAFFIC FINE JMPD PAYMENT,-750.00
01/06/2026,SALARY ACME,20000.00
02/06/2026,DEBIT ORDER NETFLIX.COM 222,-199.00
03/06/2026,PREPAID ELECTRICITY CITY POWER,-1400.00
04/06/2026,CARD PURCHASE VIDA E CAFFE,-52.00
05/06/2026,CARD PURCHASE VIDA E CAFFE,-52.00
06/06/2026,CARD PURCHASE VIDA E CAFFE,-52.00
07/06/2026,CARD PURCHASE VIDA E CAFFE,-52.00
01/07/2026,SALARY ACME,20000.00
02/07/2026,DEBIT ORDER NETFLIX.COM 333,-199.00
03/07/2026,PREPAID ELECTRICITY CITY POWER,-1100.00
04/07/2026,CARD PURCHASE VIDA E CAFFE,-52.00
"""


class TestRecurring(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.load(THREE_MONTHS)
        self.report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        self.by_merchant = {e["merchant"]: e for e in self.report.recurring}

    def test_fixed_charge_is_detected_across_changing_references(self):
        """NETFLIX.COM 111 / 222 / 333 is one charge, not three."""
        entry = self.by_merchant["Netflix"]
        self.assertTrue(entry["fixed"])
        self.assertEqual(entry["typical_amount"], 199.0)
        self.assertEqual(entry["months_seen"], 3)
        self.assertEqual(entry["annualised"], 2388.0)

    def test_variable_monthly_charge_is_flagged_variable(self):
        entry = self.by_merchant["Prepaid Electricity"]
        self.assertFalse(entry["fixed"])
        self.assertEqual(entry["min_amount"], 900.0)
        self.assertEqual(entry["max_amount"], 1400.0)

    def test_frequent_small_visits_are_not_a_commitment(self):
        """Four coffees a month is a habit; calling it recurring inflates totals."""
        self.assertNotIn("Vida e Caffè", self.by_merchant)

    def test_habit_insight_picks_up_the_coffees_instead(self):
        titles = [i.title for i in self.report.insights]
        self.assertTrue(any("Habit spend" in t for t in titles), titles)

    def test_one_off_is_not_annualised_as_monthly(self):
        """The single May fine must not be projected as twelve fines a year."""
        may = analysis.build_report(
            self.conn, analysis.parse_period("2026-05", self.conn))
        fine_insight = next(i for i in may.insights if "fines" in i.title.lower())
        self.assertLess(fine_insight.annual_saving, fine_insight.monthly_saving * 12)
        # Fines appear in 1 of 3 observed months, so roughly a third of naive.
        self.assertLess(fine_insight.annual_saving, fine_insight.monthly_saving * 5)

    def test_months_observed_is_reported(self):
        self.assertEqual(self.report.months_observed, 3)


class TestReportShape(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.load(THREE_MONTHS)
        self.report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))

    def test_reconciles(self):
        self.assertTrue(self.report.reconciliation.balances)

    def test_categories_add_to_the_outflow_total(self):
        self.assertEqual(
            round(sum(b.total for b in self.report.categories), 2),
            self.report.reconciliation.total_out)

    def test_income_is_not_counted_as_spending(self):
        names = {b.name for b in self.report.categories}
        self.assertNotIn("Income", names)
        self.assertEqual(self.report.reconciliation.income, 20000.0)

    def test_savings_never_exceed_consumption(self):
        self.assertLessEqual(self.report.monthly_reducible,
                             self.report.reconciliation.consumption)

    def test_context_insights_claim_no_saving(self):
        for insight in self.report.insights:
            if not insight.counts_to_total:
                self.assertEqual(insight.annual_saving, 0.0)

    def test_confidence_values_are_known(self):
        for insight in self.report.insights:
            self.assertIn(insight.confidence, {"high", "medium", "low"})

    def test_daily_series_covers_only_days_with_spend(self):
        self.assertTrue(all(v > 0 for _d, v in self.report.daily))

    def test_empty_period_is_handled(self):
        report = analysis.build_report(
            self.conn, analysis.parse_period("2020-01", self.conn))
        self.assertEqual(report.reconciliation.total_out, 0.0)
        self.assertTrue(report.data_quality)
        self.assertIn("No transactions", report.data_quality[0])


class TestRenderers(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.load(THREE_MONTHS)
        self.report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))

    def test_text_report_renders(self):
        output = report_text.render(self.report)
        self.assertIn("WHERE EVERY RAND WENT", output)
        self.assertIn("BY TYPE OF SPEND", output)
        self.assertIn("WHAT COULD BE CUT", output)

    def test_html_report_is_self_contained(self):
        html = report_html.render(self.report)
        self.assertIn("<!DOCTYPE html>", html)
        for forbidden in ("http://", "https://", "<script", "@import", " src="):
            self.assertNotIn(forbidden, html, f"HTML must not reference {forbidden}")

    def test_html_escapes_merchant_names(self):
        self.load("Date,Description,Amount\n"
                  "10/06/2026,CARD PURCHASE <script>alert(1)</script> SHOP,-100.00\n")
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        html = report_html.render(report)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_report_renders_without_error(self):
        report = analysis.build_report(
            self.conn, analysis.parse_period("2020-01", self.conn))
        self.assertIn("SPENDING REPORT", report_text.render(report))
        self.assertIn("<!DOCTYPE html>", report_html.render(report))


class TestCompare(AnalysisCase):
    def test_side_by_side_totals(self):
        self.load(THREE_MONTHS)
        periods = [analysis.parse_period(p) for p in ("2026-05", "2026-06")]
        result = analysis.compare(self.conn, periods)
        self.assertEqual(result["periods"], ["May 2026", "June 2026"])
        electricity = next(r for r in result["rows"]
                           if r["category"] == "Electricity & Water")
        self.assertEqual(electricity["values"], [900.0, 1400.0])
        self.assertEqual(electricity["change"], 500.0)
        self.assertIn("Fines & Traffic", [r["category"] for r in result["rows"]])
        output = report_text.render_comparison(result)
        self.assertIn("PERIOD COMPARISON", output)


class TestOverrides(AnalysisCase):
    def test_manual_override_applies_and_persists_to_new_imports(self):
        self.load("Date,Description,Amount\n"
                  "01/06/2026,ZZQQ WIDGET EMPORIUM 88,-250.00\n")
        row = self.conn.execute(
            "SELECT id, description_key, category FROM transactions").fetchone()
        self.assertEqual(row["category"], "Uncategorised")

        ingest.set_override(self.conn, row["description_key"], category="Pets",
                            merchant="Widget Emporium")
        updated = self.conn.execute(
            "SELECT category, merchant, category_source FROM transactions"
            " WHERE id = ?", (row["id"],)).fetchone()
        self.assertEqual(updated["category"], "Pets")
        self.assertEqual(updated["category_source"], "manual")

        # A later month with the same description must inherit the decision.
        self.load("Date,Description,Amount\n"
                  "01/07/2026,ZZQQ WIDGET EMPORIUM 99,-300.00\n")
        later = self.conn.execute(
            "SELECT category FROM transactions WHERE txn_date = '2026-07-01'").fetchone()
        self.assertEqual(later["category"], "Pets")

    def test_recategorise_preserves_manual_decisions(self):
        self.load("Date,Description,Amount\n"
                  "01/06/2026,CARD PURCHASE CHECKERS HYPER,-500.00\n")
        row = self.conn.execute("SELECT id, description_key FROM transactions").fetchone()
        ingest.set_override(self.conn, row["description_key"], category="Gifts & Donations")
        ingest.recategorise(self.conn)
        after = self.conn.execute("SELECT category FROM transactions").fetchone()
        self.assertEqual(after["category"], "Gifts & Donations")

    def test_excluded_transactions_leave_consumption(self):
        self.load("Date,Description,Amount\n"
                  "01/06/2026,CARD PURCHASE CHECKERS HYPER,-500.00\n"
                  "02/06/2026,REIMBURSED WORK TRAVEL BOOKING,-1000.00\n")
        row = self.conn.execute(
            "SELECT description_key FROM transactions"
            " WHERE description LIKE '%REIMBURSED%'").fetchone()
        ingest.set_override(self.conn, row["description_key"], excluded=True)
        report = analysis.build_report(
            self.conn, analysis.parse_period("2026-06", self.conn))
        self.assertEqual(report.reconciliation.excluded, 1000.0)
        self.assertEqual(report.reconciliation.consumption, 500.0)
        self.assertTrue(report.reconciliation.balances)


class TestUndoImport(AnalysisCase):
    def test_undo_removes_exactly_what_it_added(self):
        first = self.load("Date,Description,Amount\n"
                          "01/06/2026,CARD PURCHASE CHECKERS,-500.00\n")
        second = self.load("Date,Description,Amount\n"
                           "02/06/2026,CARD PURCHASE WOOLWORTHS,-300.00\n")
        removed = ingest.undo_import(self.conn, second.import_id)
        self.assertEqual(removed, 1)
        remaining = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions").fetchone()["n"]
        self.assertEqual(remaining, 1)
        self.assertIsNotNone(first.import_id)


if __name__ == "__main__":
    unittest.main()
