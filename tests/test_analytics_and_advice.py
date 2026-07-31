"""Reporting must reconcile, and advice must not promise money that isn't there."""

from __future__ import annotations

from datetime import date

from spendtracker import advice as advice_mod
from spendtracker import analytics, taxonomy
from spendtracker.ingest import loader
from spendtracker.periods import Period, parse_period

from .conftest import write_csv


def _statement(tmp_path, name, rows, opening=50_000.0):
    balance, out = opening, []
    for day, desc, amount in rows:
        balance += amount
        out.append([day, desc, f"{amount:.2f}", f"{balance:.2f}"])
    return write_csv(tmp_path / name, ["Date", "Description", "Amount", "Balance"], out)


MARCH = [
    ("02/03/2026", "BOND REPAYMENT ABSA HOMELOAN", -12_850.00),
    ("03/03/2026", "CARD PURCHASE CHECKERS FOURWAYS", -1_200.00),
    ("05/03/2026", "SALARY ACB CREDIT", 40_000.00),
    ("06/03/2026", "CARD PURCHASE UBER EATS ZA", -320.00),
    ("07/03/2026", "CARD PURCHASE HOLLYWOODBETS ONLINE", -800.00),
    ("08/03/2026", "CARD PURCHASE VIDA E CAFFE", -45.00),
    ("09/03/2026", "CARD PURCHASE VIDA E CAFFE", -45.00),
    ("10/03/2026", "ATM CASH WITHDRAWAL SANDTON", -2_000.00),
    ("11/03/2026", "DEBIT ORDER NETFLIX.COM", -199.00),
    ("12/03/2026", "MONTHLY ACCOUNT FEE", -125.00),
    ("13/03/2026", "TRANSFER TO SAVINGS OWN ACCOUNT", -5_000.00),
    ("14/03/2026", "TRAFFIC FINE AARTO", -750.00),
    ("31/03/2026", "CARD PURCHASE WOOLWORTHS FOOD", -650.00),
]


def _load(conn, cfg, tmp_path):
    loader.import_statement(conn, _statement(tmp_path, "march.csv", MARCH), cfg=cfg)
    return parse_period("2026-03")


class TestReconciliation:
    def test_breakdown_sums_exactly_to_the_bank_total(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        assert s.reconciliation.residual_cents == 0
        assert sum(b.total_cents for b in s.by_category) == s.total_outflow_cents

    def test_totals_match_the_source_rows(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        expected_out = int(round(sum(-a for _d, _s, a in MARCH if a < 0) * 100))
        expected_in = int(round(sum(a for _d, _s, a in MARCH if a > 0) * 100))
        assert s.total_outflow_cents == expected_out
        assert s.total_inflow_cents == expected_in

    def test_transfers_are_excluded_from_spending_but_not_from_outflow(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        assert s.excluded_cents == 500_000       # the R5 000 transfer to savings
        assert s.spend_cents == s.total_outflow_cents - s.excluded_cents

    def test_balances_agree(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        assert s.reconciliation.balances_agree is True

    def test_coverage_gap_is_reported(self, conn, cfg, tmp_path):
        """A period wider than the imported statement must say so."""
        _load(conn, cfg, tmp_path)
        wide = Period(date(2026, 3, 1), date(2026, 4, 30), "Mar-Apr")
        s = analytics.period_summary(conn, wide, cfg=cfg)
        gaps = s.reconciliation.coverage_gaps
        assert gaps, "April is not covered by any statement and must be flagged"
        assert any("not covered" in w for w in s.reconciliation.warnings)

    def test_uncovered_period_is_fully_flagged(self, conn, cfg, tmp_path):
        _load(conn, cfg, tmp_path)
        other = Period(date(2025, 1, 1), date(2025, 1, 31), "Jan 2025")
        s = analytics.period_summary(conn, other, cfg=cfg)
        assert s.total_outflow_cents == 0
        assert s.reconciliation.coverage_gaps[0].days == 31

    def test_unexplained_cash_is_surfaced(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        rec = s.reconciliation
        assert rec.cash_withdrawn_cents == 200_000
        assert rec.cash_unexplained_cents == 200_000
        assert any("cash" in w for w in rec.warnings)

    def test_explained_share_is_below_one_when_cash_is_unexplained(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        assert 0 < s.reconciliation.explained_share < 1


class TestBreakdowns:
    def test_categories_are_assigned(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        totals = {b.name: b.total_cents for b in s.by_category}
        assert totals["Housing"] == 1_285_000
        assert totals["Gambling & Betting"] == 80_000
        assert totals["Fines & Penalties"] == 75_000
        assert totals["Bank Fees"] == 12_500
        assert totals["Groceries"] == 185_000       # Checkers + Woolworths

    def test_merchant_names_are_normalised(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        names = {b.name for b in s.by_merchant}
        assert "Checkers" in names
        assert "Uber Eats" in names
        assert "Vida e Caffè" in names

    def test_shares_are_of_spending_not_of_outflow(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        housing = next(b for b in s.by_category if b.name == "Housing")
        assert abs(housing.share - housing.total_cents / s.spend_cents) < 1e-9

    def test_essential_and_discretionary_partition_spending(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        assert s.essential_cents + s.discretionary_cents == s.spend_cents


class TestRecurring:
    def _three_months(self, conn, cfg, tmp_path):
        for index, month in enumerate(("01", "02", "03"), start=1):
            loader.import_statement(
                conn,
                _statement(
                    tmp_path,
                    f"m{index}.csv",
                    [
                        (f"05/{month}/2026", "DEBIT ORDER NETFLIX.COM", -199.00),
                        (f"07/{month}/2026", "BOND REPAYMENT ABSA HOMELOAN", -12_850.00),
                        (f"12/{month}/2026", "CARD PURCHASE CHECKERS FOURWAYS", -800.00 - index * 300),
                        (f"20/{month}/2026", "CARD PURCHASE CHECKERS FOURWAYS", -400.00 - index * 250),
                    ],
                    opening=50_000.0 + index,
                ),
                cfg=cfg,
            )

    def test_monthly_subscription_is_detected(self, conn, cfg, tmp_path):
        self._three_months(conn, cfg, tmp_path)
        items = {r.merchant: r for r in analytics.find_recurring(conn)}
        assert "Netflix" in items
        netflix = items["Netflix"]
        assert netflix.cadence == "monthly"
        assert netflix.is_subscription is True

    def test_annualisation_uses_twelve_months_not_the_raw_gap(self, conn, cfg, tmp_path):
        """Three payments ~29 days apart imply 12.6/year arithmetically, which
        would overstate a bond repayment by thousands."""
        self._three_months(conn, cfg, tmp_path)
        bond = next(r for r in analytics.find_recurring(conn) if r.merchant == "Home Loan")
        assert bond.annualised_cents == 1_285_000 * 12

    def test_variable_spend_is_not_offered_as_cancellable(self, conn, cfg, tmp_path):
        self._three_months(conn, cfg, tmp_path)
        subs = {r.merchant for r in analytics.find_recurring(conn, subscriptions_only=True)}
        assert "Checkers" not in subs, "a grocery shop is not a subscription you can cancel"

    def test_monthly_trend_covers_each_month(self, conn, cfg, tmp_path):
        self._three_months(conn, cfg, tmp_path)
        points = analytics.monthly_trend(conn, cfg=cfg, months=12)
        assert len(points) == 3
        assert all(p.spend_cents > 0 for p in points)


class TestAdvice:
    def test_suggested_savings_never_exceed_the_reducible_headroom(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        report = advice_mod.build_advice(conn, period, cfg=cfg, summary=s)

        monthly_spend = int(s.spend_cents / period.months)
        assert report.validate(monthly_spend) == []

        ceiling = sum(
            int((b.total_cents / period.months) * taxonomy.reducible_fraction(b.name))
            for b in s.by_category
            if b.kind not in taxonomy.EXCLUDED_KINDS
        )
        assert report.monthly_total_cents <= ceiling

    def test_no_finding_claims_more_than_its_category_spends(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        s = analytics.period_summary(conn, period, cfg=cfg)
        report = advice_mod.build_advice(conn, period, cfg=cfg, summary=s)
        monthly = {
            b.name: int(b.total_cents / period.months) for b in s.by_category
        }
        for finding in report.findings:
            for category in finding.categories:
                assert finding.monthly_saving_cents <= monthly.get(category, 0) + 1, finding.key

    def test_every_finding_states_its_assumption(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        for finding in report.findings:
            assert finding.assumption.strip(), finding.key
            assert finding.difficulty in ("easy", "moderate", "hard")
            assert finding.confidence in ("high", "medium", "low")

    def test_fines_are_flagged_as_avoidable(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        assert any(f.key == "waste:Fines & Penalties" for f in report.findings)

    def test_gambling_is_surfaced_with_high_confidence(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        finding = next(f for f in report.findings if f.key == "gambling")
        assert finding.confidence == "high"
        assert finding.monthly_saving_cents > 0

    def test_essential_spend_is_not_called_frivolous(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        flagged = {i.category for i in report.frivolous}
        assert "Housing" not in flagged
        assert "Transfers" not in flagged

    def test_gambling_transactions_are_flagged_frivolous(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        assert any(i.category == "Gambling & Betting" for i in report.frivolous)

    def test_unexplained_cash_is_disclosed_as_a_caveat(self, conn, cfg, tmp_path):
        period = _load(conn, cfg, tmp_path)
        report = advice_mod.build_advice(conn, period, cfg=cfg)
        assert any("cash" in n for n in report.notes)

    def test_empty_period_produces_no_findings_and_no_crash(self, conn, cfg, tmp_path):
        _load(conn, cfg, tmp_path)
        empty = Period(date(2020, 1, 1), date(2020, 1, 31), "Jan 2020")
        report = advice_mod.build_advice(conn, empty, cfg=cfg)
        assert report.findings == []
        assert report.monthly_total_cents == 0


def test_credit_card_repayment_switches_between_spend_and_transfer(conn, cfg, tmp_path):
    """Until the card's own statement is imported, the repayment is the only
    trace of that spending, so excluding it would understate everything."""
    loader.import_statement(
        conn,
        _statement(tmp_path, "cc.csv", [
            ("05/03/2026", "CREDIT CARD PAYMENT VISA", -3_000.00),
            ("06/03/2026", "CARD PURCHASE CHECKERS", -500.00),
        ]),
        cfg=cfg,
    )
    period = parse_period("2026-03")

    cfg.credit_card_statements_imported = False
    counted = analytics.period_summary(conn, period, cfg=cfg)
    assert counted.spend_cents == 350_000

    cfg.credit_card_statements_imported = True
    not_counted = analytics.period_summary(conn, period, cfg=cfg)
    assert not_counted.spend_cents == 50_000
    assert not_counted.total_outflow_cents == counted.total_outflow_cents
