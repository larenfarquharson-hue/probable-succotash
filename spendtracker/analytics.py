"""Period reporting: where the money went, and proof that none is missing.

Two obligations are kept separate on purpose.

*Accounting for every outflow* means the category and merchant breakdowns must
add up to the bank's own total, to the cent. :func:`period_summary` asserts
this and reports the residual if it ever fails, rather than quietly presenting
a breakdown that does not reconcile.

*Explaining* those outflows is a different, weaker claim. A cash withdrawal is
accounted for the moment the bank shows it, but it is not explained until a
till slip says what the cash bought. The reconciliation block reports both, so
"R8 400 of cash spend I cannot see inside" is visible rather than hidden in a
tidy-looking pie chart.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import taxonomy
from .config import Config
from .periods import Period, months_between


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class Bucket:
    """A named slice of spend."""

    name: str
    total_cents: int
    count: int
    kind: str = "discretionary"
    colour: str = "#9ca3af"
    share: float = 0.0          # fraction of the spend total
    per_month_cents: int = 0
    largest_cents: int = 0
    from_cash_slips_cents: int = 0   # portion revealed by cash till slips

    @property
    def average_cents(self) -> int:
        return int(self.total_cents / self.count) if self.count else 0


@dataclass
class CoverageGap:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class Reconciliation:
    """Evidence that the report is complete, and where it is not."""

    bank_outflow_cents: int = 0
    breakdown_total_cents: int = 0
    residual_cents: int = 0            # must be 0
    balances_agree: bool | None = None
    balance_expected_cents: int | None = None
    balance_actual_cents: int | None = None

    coverage_gaps: list[CoverageGap] = field(default_factory=list)
    days_covered: int = 0
    days_in_period: int = 0

    held_duplicates_count: int = 0
    held_duplicates_cents: int = 0
    pending_review_count: int = 0

    cash_withdrawn_cents: int = 0
    cash_explained_cents: int = 0

    receipts_matched: int = 0
    receipts_cash_allocated: int = 0
    receipts_unmatched: int = 0
    receipts_unmatched_cents: int = 0

    uncategorised_cents: int = 0
    uncategorised_count: int = 0

    warnings: list[str] = field(default_factory=list)

    @property
    def cash_unexplained_cents(self) -> int:
        return max(0, self.cash_withdrawn_cents - self.cash_explained_cents)

    @property
    def explained_share(self) -> float:
        """Fraction of outflows we can say something specific about."""
        if not self.bank_outflow_cents:
            return 1.0
        unexplained = self.cash_unexplained_cents + self.uncategorised_cents
        return max(0.0, 1 - unexplained / self.bank_outflow_cents)


@dataclass
class PeriodSummary:
    period: Period
    currency_symbol: str = "R"

    total_outflow_cents: int = 0     # every cent that left the account
    total_inflow_cents: int = 0
    spend_cents: int = 0             # outflows excluding own-money movements
    excluded_cents: int = 0          # transfers, savings, card repayments
    essential_cents: int = 0
    discretionary_cents: int = 0
    transaction_count: int = 0

    by_category: list[Bucket] = field(default_factory=list)
    by_merchant: list[Bucket] = field(default_factory=list)
    by_type: list[Bucket] = field(default_factory=list)
    daily_cents: list[tuple[date, int]] = field(default_factory=list)
    reconciliation: Reconciliation = field(default_factory=Reconciliation)

    @property
    def net_cents(self) -> int:
        return self.total_inflow_cents - self.total_outflow_cents

    @property
    def discretionary_share(self) -> float:
        return self.discretionary_cents / self.spend_cents if self.spend_cents else 0.0

    @property
    def spend_per_month_cents(self) -> int:
        return int(self.spend_cents / self.period.months)


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

_ACTIVE_OUTFLOW = "status = 'active' AND amount_cents < 0"


def _cash_slip_reclass(
    conn: sqlite3.Connection, period: Period
) -> tuple[dict[str, int], int]:
    """What cash till slips reveal, by category.

    Returns (category -> cents revealed, total cents revealed). These amounts
    are *moved* from "Cash Withdrawals" into real categories: the withdrawal is
    still the only outflow, so the total never changes.
    """
    start, end = period.as_iso()
    revealed: dict[str, int] = {}
    total = 0
    rows = conn.execute(
        """SELECT r.category AS category, a.amount_cents AS cents
           FROM cash_allocations a
           JOIN receipts r ON r.id = a.receipt_id
           JOIN transactions t ON t.id = a.withdrawal_id
           WHERE t.status = 'active' AND t.txn_date BETWEEN ? AND ?""",
        (start, end),
    ).fetchall()
    for row in rows:
        cat = row["category"] or "Uncategorised"
        cents = int(row["cents"])
        revealed[cat] = revealed.get(cat, 0) + cents
        total += cents
    return revealed, total


def period_summary(
    conn: sqlite3.Connection,
    period: Period,
    *,
    cfg: Config | None = None,
    account_id: int | None = None,
    merchant_limit: int = 40,
) -> PeriodSummary:
    """Full breakdown of one period, with a reconciliation block."""
    cfg = cfg or Config()
    cc_imported = cfg.credit_card_statements_imported
    start, end = period.as_iso()

    account_clause = " AND account_id = ?" if account_id is not None else ""
    base_params: list = [start, end] + ([account_id] if account_id is not None else [])

    summary = PeriodSummary(period=period, currency_symbol=cfg.currency_symbol)

    # --- totals -----------------------------------------------------------
    row = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0) AS outflow,
              COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0) AS inflow,
              COUNT(*) AS n
            FROM transactions
            WHERE status = 'active' AND txn_date BETWEEN ? AND ?{account_clause}""",
        base_params,
    ).fetchone()
    summary.total_outflow_cents = int(row["outflow"])
    summary.total_inflow_cents = int(row["inflow"])
    summary.transaction_count = int(row["n"])

    # --- by category ------------------------------------------------------
    cat_rows = conn.execute(
        f"""SELECT COALESCE(category, 'Uncategorised') AS category,
                   SUM(-amount_cents) AS total, COUNT(*) AS n,
                   MAX(-amount_cents) AS largest
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND txn_date BETWEEN ? AND ?{account_clause}
            GROUP BY 1""",
        base_params,
    ).fetchall()

    revealed, revealed_total = _cash_slip_reclass(conn, period)

    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    largest: dict[str, int] = {}
    for r in cat_rows:
        totals[r["category"]] = int(r["total"])
        counts[r["category"]] = int(r["n"])
        largest[r["category"]] = int(r["largest"])

    # Move cash that slips have explained out of Cash Withdrawals.
    cash_bucket = "Cash Withdrawals"
    movable = min(revealed_total, totals.get(cash_bucket, 0))
    if movable > 0:
        scale = movable / revealed_total if revealed_total else 0
        moved_total = 0
        for cat, cents in revealed.items():
            share = int(cents * scale)
            if share <= 0:
                continue
            totals[cat] = totals.get(cat, 0) + share
            counts[cat] = counts.get(cat, 0)
            moved_total += share
        totals[cash_bucket] -= moved_total

    buckets: list[Bucket] = []
    for cat, total in totals.items():
        if total <= 0 and cat == cash_bucket:
            continue
        kind = taxonomy.category_kind(cat)
        if cc_imported and cat == "Credit Card Repayment":
            kind = "excluded"
        buckets.append(
            Bucket(
                name=cat,
                total_cents=total,
                count=counts.get(cat, 0),
                kind=kind,
                colour=taxonomy.category_colour(cat),
                largest_cents=largest.get(cat, 0),
                from_cash_slips_cents=int(revealed.get(cat, 0) * (movable / revealed_total))
                if revealed_total and movable
                else 0,
            )
        )

    summary.excluded_cents = sum(b.total_cents for b in buckets if b.kind in taxonomy.EXCLUDED_KINDS)
    summary.spend_cents = summary.total_outflow_cents - summary.excluded_cents
    summary.essential_cents = sum(b.total_cents for b in buckets if b.kind == "essential")
    summary.discretionary_cents = sum(
        b.total_cents for b in buckets if b.kind in ("discretionary", "cash")
    )

    for b in buckets:
        b.share = b.total_cents / summary.spend_cents if summary.spend_cents else 0.0
        b.per_month_cents = int(b.total_cents / period.months)
    buckets.sort(key=lambda b: b.total_cents, reverse=True)
    summary.by_category = buckets

    # --- by merchant ------------------------------------------------------
    merch_rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(merchant_norm, ''), 'Unknown') AS merchant,
                   COALESCE(category, 'Uncategorised') AS category,
                   SUM(-amount_cents) AS total, COUNT(*) AS n, MAX(-amount_cents) AS largest
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND txn_date BETWEEN ? AND ?{account_clause}
            GROUP BY 1 ORDER BY total DESC LIMIT ?""",
        base_params + [merchant_limit],
    ).fetchall()
    summary.by_merchant = [
        Bucket(
            name=r["merchant"],
            total_cents=int(r["total"]),
            count=int(r["n"]),
            kind=taxonomy.category_kind(r["category"]),
            colour=taxonomy.category_colour(r["category"]),
            share=int(r["total"]) / summary.spend_cents if summary.spend_cents else 0.0,
            per_month_cents=int(int(r["total"]) / period.months),
            largest_cents=int(r["largest"]),
        )
        for r in merch_rows
    ]

    # --- by transaction type ---------------------------------------------
    type_rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(txn_type, ''), 'other') AS t,
                   SUM(-amount_cents) AS total, COUNT(*) AS n
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND txn_date BETWEEN ? AND ?{account_clause}
            GROUP BY 1 ORDER BY total DESC""",
        base_params,
    ).fetchall()
    summary.by_type = [
        Bucket(name=_type_label(r["t"]), total_cents=int(r["total"]), count=int(r["n"]))
        for r in type_rows
    ]

    # --- daily series -----------------------------------------------------
    day_rows = conn.execute(
        f"""SELECT txn_date, SUM(-amount_cents) AS total
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND txn_date BETWEEN ? AND ?{account_clause}
            GROUP BY txn_date ORDER BY txn_date""",
        base_params,
    ).fetchall()
    summary.daily_cents = [
        (date.fromisoformat(r["txn_date"]), int(r["total"])) for r in day_rows
    ]

    summary.reconciliation = reconcile(
        conn, period, summary, cfg=cfg, account_id=account_id
    )
    return summary


_TYPE_LABELS = {
    "card": "Card purchase",
    "debit_order": "Debit order",
    "eft": "EFT / payment",
    "atm": "Cash withdrawal",
    "fee": "Bank fee",
    "interest": "Interest",
    "transfer": "Transfer",
    "credit": "Money in",
    "other": "Other / unclassified",
}


def _type_label(key: str | None) -> str:
    return _TYPE_LABELS.get(key or "other", (key or "other").replace("_", " ").capitalize())


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(
    conn: sqlite3.Connection,
    period: Period,
    summary: PeriodSummary,
    *,
    cfg: Config,
    account_id: int | None = None,
) -> Reconciliation:
    """Check the report against the bank's own numbers and flag every gap."""
    start, end = period.as_iso()
    rec = Reconciliation()
    account_clause = " AND account_id = ?" if account_id is not None else ""
    params: list = [start, end] + ([account_id] if account_id is not None else [])

    rec.bank_outflow_cents = summary.total_outflow_cents
    rec.breakdown_total_cents = sum(b.total_cents for b in summary.by_category)
    rec.residual_cents = rec.bank_outflow_cents - rec.breakdown_total_cents
    if rec.residual_cents != 0:
        rec.warnings.append(
            f"The category breakdown does not add up to the bank total; "
            f"{cfg.currency_symbol}{abs(rec.residual_cents) / 100:,.2f} is unaccounted for. "
            "This is a bug - please report it rather than trusting the breakdown."
        )

    # --- statement coverage ----------------------------------------------
    rec.days_in_period = period.days
    ranges = [
        (date.fromisoformat(r["period_start"]), date.fromisoformat(r["period_end"]))
        for r in conn.execute(
            "SELECT period_start, period_end FROM statements "
            "WHERE period_start IS NOT NULL AND period_end IS NOT NULL"
            + (" AND account_id = ?" if account_id is not None else ""),
            ([account_id] if account_id is not None else []),
        )
    ]
    rec.coverage_gaps, rec.days_covered = _coverage_gaps(period, ranges)
    if rec.coverage_gaps:
        total_gap = sum(g.days for g in rec.coverage_gaps)
        rec.warnings.append(
            f"{total_gap} day(s) of this period are not covered by any imported statement, "
            f"so spend on those days is missing entirely. Gaps: "
            + ", ".join(f"{g.start.isoformat()} to {g.end.isoformat()}" for g in rec.coverage_gaps[:5])
            + ("..." if len(rec.coverage_gaps) > 5 else "")
        )

    # --- balance continuity check ----------------------------------------
    rec.balances_agree, rec.balance_expected_cents, rec.balance_actual_cents = _balance_check(
        conn, period, account_id
    )
    if rec.balances_agree is False:
        rec.warnings.append(
            "The closing balance implied by the imported rows does not match the closing "
            "balance on the statement. Rows may have been skipped or a statement may be "
            "partially imported."
        )

    # --- held-out duplicates ---------------------------------------------
    row = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents), 0) AS cents
            FROM transactions
            WHERE status = 'duplicate' AND amount_cents < 0
              AND txn_date BETWEEN ? AND ?{account_clause}""",
        params,
    ).fetchone()
    rec.held_duplicates_count = int(row["n"])
    rec.held_duplicates_cents = int(row["cents"])

    rec.pending_review_count = int(
        conn.execute(
            """SELECT COUNT(*) AS n FROM duplicate_candidates dc
               JOIN transactions t ON t.id = dc.txn_id
               WHERE dc.resolution = 'pending' AND t.txn_date BETWEEN ? AND ?""",
            (start, end),
        ).fetchone()["n"]
    )
    if rec.pending_review_count:
        rec.warnings.append(
            f"{rec.pending_review_count} suspected duplicate(s) are waiting for your decision. "
            f"{rec.held_duplicates_count} of them are currently held out of the totals "
            f"({cfg.currency_symbol}{rec.held_duplicates_cents / 100:,.2f}), so spend could be "
            "understated by that much until you review them."
        )

    # --- cash: accounted for vs explained --------------------------------
    rec.cash_withdrawn_cents = int(
        conn.execute(
            f"""SELECT COALESCE(SUM(-amount_cents), 0) AS c FROM transactions
                WHERE {_ACTIVE_OUTFLOW} AND is_cash_withdrawal = 1
                  AND txn_date BETWEEN ? AND ?{account_clause}""",
            params,
        ).fetchone()["c"]
    )
    rec.cash_explained_cents = int(
        conn.execute(
            """SELECT COALESCE(SUM(a.amount_cents), 0) AS c
               FROM cash_allocations a JOIN transactions t ON t.id = a.withdrawal_id
               WHERE t.status = 'active' AND t.txn_date BETWEEN ? AND ?""",
            (start, end),
        ).fetchone()["c"]
    )
    if rec.cash_unexplained_cents > 0:
        rec.warnings.append(
            f"{cfg.currency_symbol}{rec.cash_unexplained_cents / 100:,.2f} of cash was withdrawn "
            "with no till slip to say what it bought. It is counted in the totals but cannot be "
            "broken down - upload the slips to see inside it."
        )

    # --- receipts ---------------------------------------------------------
    for r in conn.execute(
        """SELECT link_status, COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS cents
           FROM receipts WHERE receipt_date BETWEEN ? AND ? GROUP BY link_status""",
        (start, end),
    ):
        if r["link_status"] == "matched":
            rec.receipts_matched = int(r["n"])
        elif r["link_status"] == "cash_allocated":
            rec.receipts_cash_allocated = int(r["n"])
        elif r["link_status"] == "unmatched":
            rec.receipts_unmatched = int(r["n"])
            rec.receipts_unmatched_cents = int(r["cents"])
    if rec.receipts_unmatched:
        rec.warnings.append(
            f"{rec.receipts_unmatched} till slip(s) worth "
            f"{cfg.currency_symbol}{rec.receipts_unmatched_cents / 100:,.2f} could not be tied to "
            "any bank row. They are excluded from the totals on purpose - adding them could "
            "double count. Check whether a statement is missing, or mark them as cash."
        )

    # --- uncategorised ----------------------------------------------------
    row = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents), 0) AS cents
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND COALESCE(category, 'Uncategorised') = 'Uncategorised'
              AND txn_date BETWEEN ? AND ?{account_clause}""",
        params,
    ).fetchone()
    rec.uncategorised_count = int(row["n"])
    rec.uncategorised_cents = int(row["cents"])
    if rec.uncategorised_cents and summary.total_outflow_cents:
        share = rec.uncategorised_cents / summary.total_outflow_cents
        if share > 0.05:
            rec.warnings.append(
                f"{share:.0%} of outflows are uncategorised "
                f"({cfg.currency_symbol}{rec.uncategorised_cents / 100:,.2f}). Assign those "
                "merchants once and every future import will pick them up."
            )

    return rec


def _coverage_gaps(
    period: Period, ranges: list[tuple[date, date]]
) -> tuple[list[CoverageGap], int]:
    """Days inside the period that no statement covers."""
    if not ranges:
        return [CoverageGap(period.start, period.end)], 0

    clipped = sorted(
        (max(s, period.start), min(e, period.end))
        for s, e in ranges
        if not (e < period.start or s > period.end)
    )
    if not clipped:
        return [CoverageGap(period.start, period.end)], 0

    merged: list[list[date]] = [list(clipped[0])]
    for s, e in clipped[1:]:
        if s <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    gaps: list[CoverageGap] = []
    cursor = period.start
    for s, e in merged:
        if s > cursor:
            gaps.append(CoverageGap(cursor, s - timedelta(days=1)))
        cursor = max(cursor, e + timedelta(days=1))
    if cursor <= period.end:
        gaps.append(CoverageGap(cursor, period.end))

    covered = sum((e - s).days + 1 for s, e in merged)
    return gaps, covered


def _balance_check(
    conn: sqlite3.Connection, period: Period, account_id: int | None
) -> tuple[bool | None, int | None, int | None]:
    """Does opening balance + net movement equal the closing balance?

    Only meaningful when the statements carry a balance column.
    """
    start, end = period.as_iso()
    clause = " AND account_id = ?" if account_id is not None else ""
    params: list = [start, end] + ([account_id] if account_id is not None else [])

    rows = conn.execute(
        f"""SELECT txn_date, balance_cents, amount_cents FROM transactions
            WHERE status = 'active' AND balance_cents IS NOT NULL
              AND txn_date BETWEEN ? AND ?{clause}
            ORDER BY txn_date, row_ordinal, id""",
        params,
    ).fetchall()
    if len(rows) < 2:
        return None, None, None

    opening = int(rows[0]["balance_cents"]) - int(rows[0]["amount_cents"])
    net = int(
        conn.execute(
            f"""SELECT COALESCE(SUM(amount_cents), 0) AS s FROM transactions
                WHERE status = 'active' AND balance_cents IS NOT NULL
                  AND txn_date BETWEEN ? AND ?{clause}""",
            params,
        ).fetchone()["s"]
    )
    expected = opening + net
    actual = int(rows[-1]["balance_cents"])
    return abs(expected - actual) <= 2, expected, actual


# ---------------------------------------------------------------------------
# Trends and recurring spend
# ---------------------------------------------------------------------------


@dataclass
class MonthPoint:
    period: Period
    spend_cents: int
    essential_cents: int
    discretionary_cents: int
    outflow_cents: int
    inflow_cents: int


def monthly_trend(
    conn: sqlite3.Connection,
    *,
    cfg: Config,
    months: int = 12,
    account_id: int | None = None,
    until: date | None = None,
) -> list[MonthPoint]:
    """Spend per calendar month, for the trend chart."""
    row = conn.execute(
        "SELECT MIN(txn_date) AS lo, MAX(txn_date) AS hi FROM transactions WHERE status='active'"
    ).fetchone()
    if row is None or row["lo"] is None:
        return []
    hi = until or date.fromisoformat(row["hi"])
    lo = max(date.fromisoformat(row["lo"]), date(hi.year, hi.month, 1))
    from .periods import add_months

    lo = min(lo, add_months(date(hi.year, hi.month, 1), -(months - 1)))
    lo = max(lo, date.fromisoformat(row["lo"]))

    points: list[MonthPoint] = []
    for p in months_between(lo, hi)[-months:]:
        s = period_summary(conn, p, cfg=cfg, account_id=account_id, merchant_limit=1)
        points.append(
            MonthPoint(
                period=p,
                spend_cents=s.spend_cents,
                essential_cents=s.essential_cents,
                discretionary_cents=s.discretionary_cents,
                outflow_cents=s.total_outflow_cents,
                inflow_cents=s.total_inflow_cents,
            )
        )
    return points


@dataclass
class Recurring:
    merchant: str
    category: str
    occurrences: int
    typical_cents: int
    total_cents: int
    first_seen: date
    last_seen: date
    median_gap_days: float
    cadence: str            # weekly|fortnightly|monthly|quarterly|irregular
    amount_varies: bool
    annualised_cents: int
    still_active: bool

    @property
    def monthly_equivalent_cents(self) -> int:
        return int(self.annualised_cents / 12)

    @property
    def is_subscription(self) -> bool:
        """A fixed commitment you could actually cancel.

        Requires a billing-like cadence *and* a stable amount. Weekly groceries
        repeat just as reliably as Netflix does, but calling them a
        subscription you could cancel would be nonsense.
        """
        return self.cadence in ("monthly", "quarterly", "annual") and not self.amount_varies


def find_recurring(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    min_occurrences: int = 3,
    as_of: date | None = None,
    subscriptions_only: bool = False,
) -> list[Recurring]:
    """Detect subscriptions, debit orders and other repeating commitments.

    Cadence comes from the median gap between charges, and "still active" means
    the last charge is within roughly one-and-a-half cycles of the newest data,
    which is how a cancelled subscription is told apart from a live one.
    """
    clause = " AND account_id = ?" if account_id is not None else ""
    params = [account_id] if account_id is not None else []
    rows = conn.execute(
        f"""SELECT merchant_norm, category, txn_date, -amount_cents AS cents
            FROM transactions
            WHERE {_ACTIVE_OUTFLOW} AND merchant_norm IS NOT NULL{clause}
            ORDER BY merchant_norm, txn_date""",
        params,
    ).fetchall()

    latest = as_of
    if latest is None:
        r = conn.execute(
            "SELECT MAX(txn_date) AS hi FROM transactions WHERE status='active'"
        ).fetchone()
        latest = date.fromisoformat(r["hi"]) if r and r["hi"] else date.today()

    grouped: dict[str, list[tuple[date, int, str]]] = {}
    for r in rows:
        grouped.setdefault(r["merchant_norm"], []).append(
            (date.fromisoformat(r["txn_date"]), int(r["cents"]), r["category"] or "Uncategorised")
        )

    out: list[Recurring] = []
    for merchant, entries in grouped.items():
        if len(entries) < min_occurrences:
            continue
        dates = [e[0] for e in entries]
        amounts = sorted(e[1] for e in entries)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if len(gaps) < min_occurrences - 1:
            continue
        median_gap = _median(gaps)
        cadence = _cadence(median_gap)
        if cadence == "irregular":
            continue

        typical = amounts[len(amounts) // 2]
        spread = (amounts[-1] - amounts[0]) / typical if typical else 1.0
        # A subscription's amount barely moves; groceries move a lot. Allow
        # more variance for weekly cadences where a shop is plausible.
        limit = 0.35 if cadence in ("monthly", "quarterly") else 0.6
        varies = spread > limit

        # Annualise off the canonical cadence, not the raw median gap. Three
        # monthly payments 29 days apart imply 12.6 a year arithmetically, which
        # would overstate a bond repayment by thousands.
        annualised = int(typical * _CYCLES_PER_YEAR[cadence])
        cycles_since = (latest - dates[-1]).days / median_gap if median_gap else 99

        out.append(
            Recurring(
                merchant=merchant,
                category=entries[0][2],
                occurrences=len(entries),
                typical_cents=typical,
                total_cents=sum(e[1] for e in entries),
                first_seen=dates[0],
                last_seen=dates[-1],
                median_gap_days=median_gap,
                cadence=cadence,
                amount_varies=varies,
                annualised_cents=annualised,
                still_active=cycles_since <= 1.6,
            )
        )

    if subscriptions_only:
        out = [r for r in out if r.is_subscription]
    out.sort(key=lambda r: r.annualised_cents, reverse=True)
    return out


def _median(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


_CYCLES_PER_YEAR = {
    "weekly": 52.0,
    "fortnightly": 26.0,
    "monthly": 12.0,
    "quarterly": 4.0,
    "annual": 1.0,
    "irregular": 0.0,
}


def _cadence(median_gap: float) -> str:
    if 5 <= median_gap <= 9:
        return "weekly"
    if 11 <= median_gap <= 18:
        return "fortnightly"
    if 25 <= median_gap <= 36:
        return "monthly"
    if 80 <= median_gap <= 100:
        return "quarterly"
    if 350 <= median_gap <= 380:
        return "annual"
    return "irregular"


def merchant_detail(
    conn: sqlite3.Connection,
    merchant: str,
    *,
    period: Period | None = None,
    account_id: int | None = None,
) -> list[sqlite3.Row]:
    """Every transaction for one merchant, newest first, with receipt links."""
    clauses = [_ACTIVE_OUTFLOW, "t.merchant_norm = ?"]
    params: list = [merchant]
    if period:
        clauses.append("t.txn_date BETWEEN ? AND ?")
        params.extend(period.as_iso())
    if account_id is not None:
        clauses.append("t.account_id = ?")
        params.append(account_id)
    where = " AND ".join(c.replace("status", "t.status").replace("amount_cents", "t.amount_cents")
                        if c == _ACTIVE_OUTFLOW else c for c in clauses)
    return conn.execute(
        f"""SELECT t.*, (SELECT COUNT(*) FROM receipts r WHERE r.transaction_id = t.id) AS receipts
            FROM transactions t WHERE {where} ORDER BY t.txn_date DESC, t.id DESC""",
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def summary_to_dict(summary: PeriodSummary) -> dict:
    """A JSON-safe view of a period summary, for `report --json`.

    Amounts stay in integer cents deliberately. Anything consuming this is
    doing arithmetic, and cents is the only representation that survives it
    unharmed. Formatting into currency is the caller's problem.
    """

    def bucket(b: Bucket) -> dict:
        return {
            "name": b.name,
            "total_cents": b.total_cents,
            "count": b.count,
            "kind": b.kind,
            "share": round(b.share, 6),
            "per_month_cents": b.per_month_cents,
            "largest_cents": b.largest_cents,
            "average_cents": b.average_cents,
            "from_cash_slips_cents": b.from_cash_slips_cents,
        }

    rec = summary.reconciliation
    return {
        "period": {
            "label": summary.period.label,
            "start": summary.period.start.isoformat(),
            "end": summary.period.end.isoformat(),
            "days": summary.period.days,
        },
        "currency_symbol": summary.currency_symbol,
        "totals": {
            "total_outflow_cents": summary.total_outflow_cents,
            "total_inflow_cents": summary.total_inflow_cents,
            "net_cents": summary.net_cents,
            "spend_cents": summary.spend_cents,
            "excluded_cents": summary.excluded_cents,
            "essential_cents": summary.essential_cents,
            "discretionary_cents": summary.discretionary_cents,
            "discretionary_share": round(summary.discretionary_share, 6),
            "spend_per_month_cents": summary.spend_per_month_cents,
            "transaction_count": summary.transaction_count,
        },
        "by_category": [bucket(b) for b in summary.by_category],
        "by_merchant": [bucket(b) for b in summary.by_merchant],
        "by_type": [bucket(b) for b in summary.by_type],
        "daily_cents": [[d.isoformat(), c] for d, c in summary.daily_cents],
        "reconciliation": {
            "bank_outflow_cents": rec.bank_outflow_cents,
            "breakdown_total_cents": rec.breakdown_total_cents,
            "residual_cents": rec.residual_cents,
            "reconciles": rec.residual_cents == 0,
            "balances_agree": rec.balances_agree,
            "days_covered": rec.days_covered,
            "days_in_period": rec.days_in_period,
            "coverage_gaps": [
                {"start": g.start.isoformat(), "end": g.end.isoformat()}
                for g in rec.coverage_gaps
            ],
            "cash_withdrawn_cents": rec.cash_withdrawn_cents,
            "cash_explained_cents": rec.cash_explained_cents,
            "cash_unexplained_cents": rec.cash_unexplained_cents,
            "receipts_matched": rec.receipts_matched,
            "receipts_cash_allocated": rec.receipts_cash_allocated,
            "receipts_unmatched": rec.receipts_unmatched,
            "receipts_unmatched_cents": rec.receipts_unmatched_cents,
            "held_duplicates_count": rec.held_duplicates_count,
            "held_duplicates_cents": rec.held_duplicates_cents,
            "pending_review_count": rec.pending_review_count,
            "uncategorised_cents": rec.uncategorised_cents,
            "uncategorised_count": rec.uncategorised_count,
            "explained_share": round(rec.explained_share, 6),
            "warnings": list(rec.warnings),
        },
    }
