"""Period analysis: where the money went, and what could be cut.

The reconciliation is the backbone. Every outflow in the period is assigned to
exactly one bucket, and those buckets add back to the total — so the report can
say "this is all of it" rather than "this is most of it".

Cash is the one place where the picture is layered: the withdrawal is the outflow
that hit the account, and till slips reallocate portions of it into real
categories. Both views are reported, and neither inflates the total.
"""

from __future__ import annotations

import calendar
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config, matching, taxonomy


# --------------------------------------------------------------------------
# Period handling
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    start: str
    end: str
    label: str

    @property
    def days(self) -> int:
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1

    @property
    def months(self) -> float:
        return max(self.days / 30.44, 1e-9)


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}",
                  f"{calendar.month_name[month]} {year}")


def parse_period(spec: str | None, conn: sqlite3.Connection | None = None) -> Period:
    """Accept 'YYYY-MM', 'YYYY', 'YYYY-MM-DD:YYYY-MM-DD', 'last-month', 'all'."""
    text = (spec or "all").strip().lower()

    if text in ("all", "everything", "*"):
        if conn is None:
            raise ValueError("'all' needs a database connection to find the range")
        row = conn.execute(
            "SELECT MIN(txn_date) a, MAX(txn_date) b FROM transactions").fetchone()
        if not row or not row["a"]:
            today = date.today().isoformat()
            return Period(today, today, "no data")
        return Period(row["a"], row["b"], f"{row['a']} to {row['b']}")

    if text in ("this-month", "current-month"):
        today = date.today()
        return month_period(today.year, today.month)
    if text == "last-month":
        first = date.today().replace(day=1)
        prev = first - timedelta(days=1)
        return month_period(prev.year, prev.month)
    if text in ("ytd", "year-to-date"):
        today = date.today()
        return Period(f"{today.year}-01-01", today.isoformat(), f"{today.year} to date")

    if ":" in text:
        start, _, end = text.partition(":")
        return Period(start.strip(), end.strip(), f"{start.strip()} to {end.strip()}")

    if len(text) == 7 and text[4] == "-":
        year, month = int(text[:4]), int(text[5:7])
        return month_period(year, month)

    if len(text) == 4 and text.isdigit():
        year = int(text)
        return Period(f"{year}-01-01", f"{year}-12-31", str(year))

    if len(text) == 10 and text[4] == "-":
        return Period(text, text, text)

    raise ValueError(
        f"Cannot read period {spec!r}. Use YYYY-MM, YYYY, YYYY-MM-DD:YYYY-MM-DD, "
        "this-month, last-month, ytd or all."
    )


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------

@dataclass
class Bucket:
    name: str
    total: float = 0.0
    count: int = 0
    group: str = ""
    kind: str = "spend"
    discretion: float = 0.0
    slip_backed: float = 0.0     # value confirmed by a till slip
    from_cash: float = 0.0       # value moved here from a cash withdrawal

    @property
    def reducible(self) -> float:
        return round(self.total * self.discretion, 2)


@dataclass
class Reconciliation:
    """Every rand that left the account, split into non-overlapping buckets."""
    total_out: float = 0.0
    consumption: float = 0.0
    debt: float = 0.0
    savings: float = 0.0
    transfers: float = 0.0
    excluded: float = 0.0
    cash_withdrawn: float = 0.0
    cash_explained: float = 0.0
    cash_unexplained: float = 0.0
    income: float = 0.0
    refunds: float = 0.0

    @property
    def accounted(self) -> float:
        return round(self.consumption + self.debt + self.savings + self.transfers
                     + self.excluded, 2)

    @property
    def difference(self) -> float:
        return round(self.total_out - self.accounted, 2)

    @property
    def balances(self) -> bool:
        return abs(self.difference) < 0.02


@dataclass
class Insight:
    """One suggested reduction, with the evidence behind it.

    Savings are stated monthly and annualised from the period. Insights that
    would otherwise overlap are sized on transactions no earlier insight has
    already claimed, so the total at the bottom of the report is a real number
    rather than the same spend counted three times.

    `counts_to_total` is False for context items — a benchmark or a visibility
    gap is worth reporting but is not money you can decide to stop spending.
    """
    title: str
    detail: str
    period_amount: float
    annual_amount: float
    monthly_saving: float
    annual_saving: float
    confidence: str            # high | medium | low
    kind: str                  # frivolous | avoidable | subscription | habit | ...
    evidence: list[str] = field(default_factory=list)
    action: str = ""
    counts_to_total: bool = True

    @property
    def rank(self) -> float:
        weight = {"high": 1.0, "medium": 0.72, "low": 0.45}[self.confidence]
        base = self.annual_saving if self.counts_to_total else self.annual_amount * 0.1
        return base * weight


def _saving(monthly: float, factor: float = 1.0) -> tuple[float, float]:
    """A monthly saving and its annualised twin.

    `factor` is how much of the year this kind of spend actually occupies, so a
    one-off traffic fine is not projected as a monthly habit. See _Frequency.
    """
    return round(monthly, 2), round(monthly * 12 * factor, 2)


@dataclass
class PeriodReport:
    period: Period
    currency: str = "R"
    reconciliation: Reconciliation = field(default_factory=Reconciliation)
    categories: list[Bucket] = field(default_factory=list)
    merchants: list[Bucket] = field(default_factory=list)
    groups: list[Bucket] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    recurring: list[dict] = field(default_factory=list)
    daily: list[tuple[str, float]] = field(default_factory=list)
    largest: list[dict] = field(default_factory=list)
    uncategorised: list[dict] = field(default_factory=list)
    data_quality: list[str] = field(default_factory=list)
    slip_coverage: dict = field(default_factory=dict)
    accounts: list[str] = field(default_factory=list)
    # Distinct calendar months present in the database, which is what makes an
    # annual projection more than an assumption.
    months_observed: int = 0

    @property
    def consumption(self) -> float:
        return self.reconciliation.consumption

    @property
    def monthly_reducible(self) -> float:
        """Sum of the suggested monthly savings. Non-overlapping by construction."""
        return round(sum(i.monthly_saving for i in self.insights
                         if i.counts_to_total), 2)

    @property
    def annual_reducible(self) -> float:
        return round(sum(i.annual_saving for i in self.insights
                         if i.counts_to_total), 2)

    @property
    def cash_reallocated(self) -> list[dict]:
        return self.slip_coverage.get("cash_breakdown", [])


def build_report(conn: sqlite3.Connection, period: Period,
                 settings: config.Settings | None = None,
                 account: str | None = None) -> PeriodReport:
    """Assemble the full picture for a period."""
    cfg = settings or config.Settings.load()
    report = PeriodReport(period=period, currency=cfg.currency)

    where = ["t.txn_date BETWEEN ? AND ?"]
    params: list[object] = [period.start, period.end]
    if account:
        where.append("a.name = ?")
        params.append(account)
    clause = " AND ".join(where)

    rows = conn.execute(
        f"SELECT t.*, a.name AS account_name FROM transactions t"
        f" JOIN accounts a ON a.id = t.account_id WHERE {clause}"
        f" ORDER BY t.txn_date, t.id",
        params,
    ).fetchall()

    report.accounts = sorted({r["account_name"] for r in rows})
    _reconcile(report, rows)
    _summarise(conn, report, rows, cfg)
    _cash_view(conn, report, period, account)
    _reallocate_cash(report)
    _slip_coverage(conn, report, period)
    report.recurring = find_recurring(conn, period, account)
    report.insights = build_insights(conn, report, rows, cfg)
    report.insights.sort(key=lambda i: i.rank, reverse=True)
    _data_quality(report, rows, cfg)
    return report


def _reconcile(report: PeriodReport, rows: list[sqlite3.Row]) -> None:
    rec = report.reconciliation
    for row in rows:
        amount = float(row["amount"])
        category = taxonomy.get(row["category"])
        if amount > 0:
            if category.kind == "refund":
                rec.refunds += amount
            else:
                rec.income += amount
            continue

        out = -amount
        rec.total_out += out
        if row["excluded"]:
            rec.excluded += out
        elif row["is_internal"] or category.kind == "transfer":
            rec.transfers += out
        elif category.kind == "saving":
            rec.savings += out
        elif category.kind == "debt":
            rec.debt += out
        else:
            rec.consumption += out

    for name in ("total_out", "consumption", "debt", "savings", "transfers",
                 "excluded", "income", "refunds"):
        setattr(rec, name, round(getattr(rec, name), 2))


def _summarise(conn: sqlite3.Connection, report: PeriodReport,
               rows: list[sqlite3.Row], cfg: config.Settings) -> None:
    """Category, group and merchant totals over consumption spend only."""
    cats: dict[str, Bucket] = {}
    merchants: dict[str, Bucket] = {}
    groups: dict[str, Bucket] = {}
    daily: dict[str, float] = defaultdict(float)
    slip_backed_ids = _slip_backed_transaction_ids(conn)

    for row in rows:
        amount = float(row["amount"])
        if amount >= 0 or row["excluded"]:
            continue
        meta = taxonomy.get(row["category"])
        if meta.kind in ("transfer",):
            continue
        if row["is_internal"]:
            continue
        out = -amount
        daily[row["txn_date"]] += out

        name = row["category"] or taxonomy.UNCATEGORISED
        bucket = cats.setdefault(name, Bucket(name=name, group=meta.group,
                                              kind=meta.kind,
                                              discretion=meta.discretion))
        bucket.total += out
        bucket.count += 1
        if row["id"] in slip_backed_ids:
            bucket.slip_backed += out

        gbucket = groups.setdefault(meta.group, Bucket(name=meta.group,
                                                       group=meta.group,
                                                       kind=meta.kind))
        gbucket.total += out
        gbucket.count += 1

        if meta.kind in ("spend", "unknown"):
            label = row["merchant"] or "(unknown merchant)"
            mbucket = merchants.setdefault(label, Bucket(name=label,
                                                         group=name,
                                                         discretion=meta.discretion))
            mbucket.total += out
            mbucket.count += 1

        if row["category"] == taxonomy.UNCATEGORISED:
            report.uncategorised.append({
                "id": row["id"], "date": row["txn_date"],
                "description": row["description"], "amount": round(out, 2),
                "key": row["description_key"],
            })

    for bucket in list(cats.values()) + list(merchants.values()) + list(groups.values()):
        bucket.total = round(bucket.total, 2)
        bucket.slip_backed = round(bucket.slip_backed, 2)

    report.categories = sorted(cats.values(), key=lambda b: b.total, reverse=True)
    report.merchants = sorted(merchants.values(), key=lambda b: b.total, reverse=True)
    report.groups = sorted(groups.values(), key=lambda b: b.total, reverse=True)
    report.daily = sorted((d, round(v, 2)) for d, v in daily.items())
    report.largest = [
        {"date": r["txn_date"], "description": r["description"],
         "amount": round(-float(r["amount"]), 2), "category": r["category"],
         "merchant": r["merchant"]}
        for r in sorted((r for r in rows
                         if float(r["amount"]) < 0 and not r["excluded"]
                         and not r["is_internal"]
                         and taxonomy.get(r["category"]).kind not in ("transfer",)),
                        key=lambda r: float(r["amount"]))[:15]
    ]
    report.uncategorised.sort(key=lambda d: d["amount"], reverse=True)


def _slip_backed_transaction_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(r["matched_txn_id"]) for r in conn.execute(
            "SELECT matched_txn_id FROM slips WHERE matched_txn_id IS NOT NULL"
            " AND status IN (?, ?)", (matching.STATUS_MATCHED, matching.STATUS_MANUAL))
    }


def _cash_view(conn: sqlite3.Connection, report: PeriodReport, period: Period,
               account: str | None) -> None:
    """Layer slip detail over cash withdrawals without changing the total."""
    position = matching.cash_position(conn, period.start, period.end)
    rec = report.reconciliation
    rec.cash_withdrawn = position["withdrawn"]
    rec.cash_explained = position["explained"]
    rec.cash_unexplained = position["unexplained"]

    breakdown = conn.execute(
        "SELECT s.category, COALESCE(SUM(s.total), 0) total, COUNT(*) n"
        "  FROM slips s JOIN transactions t ON t.id = s.matched_txn_id"
        " WHERE s.status = ? AND t.txn_date BETWEEN ? AND ?"
        " GROUP BY s.category ORDER BY total DESC",
        (matching.STATUS_CASH, period.start, period.end),
    ).fetchall()
    report.slip_coverage["cash_breakdown"] = [
        {"category": r["category"] or taxonomy.UNCATEGORISED,
         "total": round(float(r["total"]), 2), "count": int(r["n"])}
        for r in breakdown
    ]

    by_merchant = conn.execute(
        "SELECT s.merchant, s.category, COALESCE(SUM(s.total), 0) total, COUNT(*) n"
        "  FROM slips s JOIN transactions t ON t.id = s.matched_txn_id"
        " WHERE s.status = ? AND t.txn_date BETWEEN ? AND ?"
        " GROUP BY s.merchant ORDER BY total DESC",
        (matching.STATUS_CASH, period.start, period.end),
    ).fetchall()
    report.slip_coverage["cash_merchants"] = [
        {"merchant": r["merchant"] or "(unknown merchant)",
         "category": r["category"] or taxonomy.UNCATEGORISED,
         "total": round(float(r["total"]), 2), "count": int(r["n"])}
        for r in by_merchant
    ]


def _reallocate_cash(report: PeriodReport) -> None:
    """Move slip-explained cash out of "Cash Withdrawals" into real categories.

    The consumption total is untouched — value is only ever moved sideways. What
    changes is that "R2,000 cash" becomes "R620 groceries, R480 eating out, R900
    still unexplained", which is the whole point of collecting slips.
    """
    breakdown = report.slip_coverage.get("cash_breakdown") or []
    if not breakdown:
        return

    cats = {b.name: b for b in report.categories}
    cash_bucket = cats.get(taxonomy.CASH)
    if cash_bucket is None:
        return

    moved = 0.0
    for entry in breakdown:
        amount = entry["total"]
        if amount <= 0:
            continue
        # Never move more than the withdrawal actually holds.
        amount = min(amount, round(cash_bucket.total - moved, 2))
        if amount <= 0:
            break
        target_name = entry["category"]
        meta = taxonomy.get(target_name)
        target = cats.get(target_name)
        if target is None:
            target = Bucket(name=target_name, group=meta.group, kind=meta.kind,
                            discretion=meta.discretion)
            cats[target_name] = target
        target.total = round(target.total + amount, 2)
        target.count += entry["count"]
        target.from_cash = round(target.from_cash + amount, 2)
        target.slip_backed = round(target.slip_backed + amount, 2)
        moved += amount

    cash_bucket.total = round(cash_bucket.total - moved, 2)
    if cash_bucket.total <= 0.01:
        cats.pop(taxonomy.CASH, None)
    report.categories = sorted(cats.values(), key=lambda b: b.total, reverse=True)

    # Groups follow the same reallocation so the two views agree.
    groups = {b.name: b for b in report.groups}
    for bucket in report.categories:
        if bucket.from_cash <= 0:
            continue
        target = groups.get(bucket.group)
        if target is None:
            groups[bucket.group] = Bucket(name=bucket.group, group=bucket.group,
                                          total=bucket.from_cash, count=0)
        else:
            target.total = round(target.total + bucket.from_cash, 2)
    cash_group = groups.get(taxonomy.get(taxonomy.CASH).group)
    if cash_group is not None:
        cash_group.total = round(cash_group.total - moved, 2)
        if cash_group.total <= 0.01:
            groups.pop(cash_group.name, None)
    report.groups = sorted(groups.values(), key=lambda b: b.total, reverse=True)

    _reallocate_cash_merchants(report, moved)


def _reallocate_cash_merchants(report: PeriodReport, moved: float) -> None:
    """Same move, applied to the merchant table."""
    merchants = {b.name: b for b in report.merchants}
    cash_label = next((name for name in merchants if name.lower() == "cash"), None)
    for entry in report.slip_coverage.get("cash_merchants") or []:
        meta = taxonomy.get(entry["category"])
        bucket = merchants.get(entry["merchant"])
        if bucket is None:
            bucket = Bucket(name=entry["merchant"], group=entry["category"],
                            discretion=meta.discretion)
            merchants[entry["merchant"]] = bucket
        bucket.total = round(bucket.total + entry["total"], 2)
        bucket.count += entry["count"]
        bucket.from_cash = round(bucket.from_cash + entry["total"], 2)
    if cash_label:
        remaining = round(merchants[cash_label].total - moved, 2)
        if remaining <= 0.01:
            merchants.pop(cash_label)
        else:
            merchants[cash_label].total = remaining
            merchants[cash_label].name = "Cash (unexplained)"
    report.merchants = sorted(merchants.values(), key=lambda b: b.total, reverse=True)


def _slip_coverage(conn: sqlite3.Connection, report: PeriodReport,
                   period: Period) -> None:
    counts = conn.execute(
        "SELECT status, COUNT(*) n, COALESCE(SUM(total), 0) t FROM slips"
        " WHERE slip_date BETWEEN ? AND ? GROUP BY status",
        (period.start, period.end)).fetchall()
    report.slip_coverage["by_status"] = {
        r["status"]: {"count": int(r["n"]), "total": round(float(r["t"]), 2)}
        for r in counts
    }
    matched_value = conn.execute(
        "SELECT COALESCE(SUM(ABS(t.amount)), 0) v FROM slips s"
        " JOIN transactions t ON t.id = s.matched_txn_id"
        " WHERE s.status IN (?, ?) AND t.txn_date BETWEEN ? AND ?",
        (matching.STATUS_MATCHED, matching.STATUS_MANUAL, period.start, period.end),
    ).fetchone()["v"]
    consumption = report.reconciliation.consumption or 1.0
    report.slip_coverage["value_matched"] = round(float(matched_value), 2)
    report.slip_coverage["pct_of_consumption"] = round(
        100.0 * float(matched_value) / consumption, 1)


# --------------------------------------------------------------------------
# Recurring charges
# --------------------------------------------------------------------------

def find_recurring(conn: sqlite3.Connection, period: Period,
                   account: str | None = None, months_back: int = 6) -> list[dict]:
    """Detect charges that repeat monthly, using history beyond the period.

    Grouped on the normalised description so "NETFLIX.COM 8829102" in June and
    "NETFLIX.COM 8830011" in July are recognised as the same charge.
    """
    start = (date.fromisoformat(period.start)
             - timedelta(days=int(30.44 * months_back))).isoformat()
    # Look past the period too: a charge seen in the following month is stronger
    # evidence that it recurs, and it tells us whether it has since stopped.
    latest = conn.execute("SELECT MAX(txn_date) m FROM transactions").fetchone()["m"]
    end = max(period.end, latest) if latest else period.end
    params: list[object] = [start, end]
    account_clause = ""
    if account:
        account_clause = " AND a.name = ?"
        params.append(account)

    rows = conn.execute(
        "SELECT t.description_key, t.merchant, t.category, t.txn_date, t.amount,"
        "       t.description"
        "  FROM transactions t JOIN accounts a ON a.id = t.account_id"
        f" WHERE t.amount < 0 AND t.excluded = 0 AND t.is_internal = 0"
        f"   AND t.txn_date BETWEEN ? AND ?{account_clause}"
        " ORDER BY t.txn_date",
        params,
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        # Cash withdrawals recur, but they are not a commitment to anyone — what
        # the cash bought is the question, and slips answer it elsewhere.
        if taxonomy.get(row["category"]).kind == "unknown":
            continue
        groups[row["description_key"]].append(row)

    out: list[dict] = []
    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        months = sorted({e["txn_date"][:7] for e in entries})
        if len(months) < 2:
            continue
        amounts = [abs(float(e["amount"])) for e in entries]
        median = statistics.median(amounts)
        spread = (max(amounts) - min(amounts)) / median if median else 1.0
        gaps = _month_gaps(months)
        # Monthly-ish means most consecutive appearances are one month apart.
        monthly = gaps and sum(1 for g in gaps if g == 1) >= max(1, len(gaps) // 2)
        if not monthly:
            continue
        # A commitment happens about once a month. Eight coffees across two
        # months is a habit, and belongs in the habit insight rather than here —
        # calling it a recurring charge would inflate "committed annually".
        cadence = len(entries) / len(months)
        if cadence > 1.4:
            continue
        # Fixed charges barely move: subscriptions, insurance, debit orders.
        # Variable ones recur monthly but by amount vary: utilities, a monthly
        # big shop. Both are useful to know, but only fixed ones are commitments.
        if spread > 0.45:
            continue
        fixed = spread <= 0.08

        in_period = [e for e in entries
                     if period.start <= e["txn_date"] <= period.end]
        last_month = months[-1]
        expected_next = _add_month(last_month)
        out.append({
            "key": key,
            "merchant": entries[-1]["merchant"] or key,
            "category": entries[-1]["category"],
            "description": entries[-1]["description"],
            "typical_amount": round(median, 2),
            "occurrences": len(entries),
            "months": months,
            "months_seen": len(months),
            "fixed": fixed,
            "spread": round(spread, 3),
            "min_amount": round(min(amounts), 2),
            "max_amount": round(max(amounts), 2),
            "annualised": round(median * 12, 2),
            "in_period_total": round(sum(abs(float(e["amount"])) for e in in_period), 2),
            "in_period_count": len(in_period),
            "last_seen": entries[-1]["txn_date"],
            "expected_next_month": expected_next,
            "amount_changed": round(amounts[-1] - amounts[0], 2)
            if len(amounts) > 1 else 0.0,
        })
    out.sort(key=lambda d: d["annualised"], reverse=True)
    return out


def _month_gaps(months: list[str]) -> list[int]:
    def index(m: str) -> int:
        year, mon = int(m[:4]), int(m[5:7])
        return year * 12 + mon
    return [index(b) - index(a) for a, b in zip(months, months[1:])]


def _add_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    mon += 1
    if mon > 12:
        year, mon = year + 1, 1
    return f"{year:04d}-{mon:02d}"


# --------------------------------------------------------------------------
# Insights
# --------------------------------------------------------------------------

class _Frequency:
    """How often a kind of spend actually occurs across the whole data set.

    Annualising a single month by multiplying by twelve turns one R750 traffic
    fine into a R9,000-a-year problem. This measures, per category, in how many
    of the observed months that category appears at all, and scales annual
    projections by that share. With only one month on record it can tell nothing,
    so it returns 1.0 and the report says the projection assumes a typical month.
    """

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute(
            "SELECT DISTINCT category, substr(txn_date, 1, 7) month FROM transactions"
            " WHERE amount < 0 AND excluded = 0").fetchall()
        self.months: set[str] = {r["month"] for r in rows}
        self.by_category: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            self.by_category[row["category"]].add(row["month"])

    @property
    def months_observed(self) -> int:
        return len(self.months)

    @property
    def reliable(self) -> bool:
        return self.months_observed >= 2

    def factor(self, rows: list[sqlite3.Row]) -> float:
        """Amount-weighted share of months in which these categories appear."""
        if not self.reliable or not rows:
            return 1.0
        total = sum(-float(r["amount"]) for r in rows)
        if total <= 0:
            return 1.0
        weighted = 0.0
        for row in rows:
            seen = len(self.by_category.get(row["category"], self.months))
            weighted += -float(row["amount"]) * (seen / self.months_observed)
        return max(0.08, min(1.0, weighted / total))


class _Claims:
    """Tracks which transactions an insight has already accounted for.

    Without this, the same R1,367 of streaming would appear in the
    subscriptions insight, the discretionary-category insight and the
    income-share benchmark, and the report would claim three times the
    available saving.
    """

    def __init__(self, rows: list[sqlite3.Row]):
        self.rows = rows
        self.claimed: set[int] = set()

    def available(self, predicate) -> list[sqlite3.Row]:
        return [r for r in self.rows
                if r["id"] not in self.claimed and predicate(r)]

    def take(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        for row in rows:
            self.claimed.add(row["id"])
        return rows

    def in_categories(self, *names: str) -> list[sqlite3.Row]:
        wanted = set(names)
        return self.take(self.available(lambda r: r["category"] in wanted))


def build_insights(conn: sqlite3.Connection, report: PeriodReport,
                   rows: list[sqlite3.Row], cfg: config.Settings) -> list[Insight]:
    """Suggest reductions, largest realistic saving first.

    Every number is derived from the period's own transactions. Savings are
    sized with each category's discretion weight rather than assuming a category
    can go to zero, and each transaction contributes to at most one suggestion,
    so the figures can be added up.
    """
    months = report.period.months
    spend_rows = [r for r in rows
                  if float(r["amount"]) < 0 and not r["excluded"] and not r["is_internal"]
                  and taxonomy.get(r["category"]).kind in ("spend", "unknown")]
    claims = _Claims(spend_rows)
    freq = _Frequency(conn)
    report.months_observed = freq.months_observed

    insights: list[Insight] = []
    # Order matters: the most clear-cut and specific claims go first, so the
    # broad "this category is optional" catch-all only sizes what is left.
    insights += _insight_gambling(claims, months, cfg, freq)
    insights += _insight_avoidable(claims, months, cfg, freq)
    insights += _insight_delivery_premium(claims, months, cfg, freq)
    insights += _insight_duplicate_services(report, claims, cfg)
    insights += _insight_subscriptions(report, claims, cfg)
    insights += _insight_small_frequent(claims, months, cfg, freq)
    insights += _insight_top_discretionary(report, claims, months, cfg, freq)
    # Context, not savings.
    insights += _insight_cash_gap(report, cfg)
    insights += _insight_income_share(report, cfg)
    return insights


def _sum(rows: list[sqlite3.Row]) -> float:
    return round(sum(-float(r["amount"]) for r in rows), 2)


def _evidence(rows: list[sqlite3.Row], cfg: config.Settings,
              limit: int = 8) -> list[str]:
    return [f"{r['txn_date']} {(r['merchant'] or r['description'])[:42]} "
            f"{config.money(-float(r['amount']), cfg.currency)}" for r in rows[:limit]]


def _insight_gambling(claims: _Claims, months: float, cfg: config.Settings,
                      freq: _Frequency) -> list[Insight]:
    hits = claims.in_categories("Gambling & Betting", "Lottery")
    total = _sum(hits)
    if total < 1:
        return []
    per_month = total / months
    monthly, annual = _saving(per_month, freq.factor(hits))
    return [Insight(
        title="Betting and lottery spend",
        detail=(f"{len(hits)} transaction(s) totalling {config.money(total, cfg.currency)} "
                f"— {config.money(per_month, cfg.currency)} a month. Nothing in a budget "
                f"depends on this, which makes it the cleanest cut available."),
        period_amount=total,
        annual_amount=round(per_month * 12, 2),
        monthly_saving=monthly,
        annual_saving=annual,
        confidence="high",
        kind="frivolous",
        evidence=_evidence(hits, cfg),
        action="Set a hard monthly cap, or self-exclude with the operator.",
    )]


def _insight_avoidable(claims: _Claims, months: float, cfg: config.Settings,
                       freq: _Frequency) -> list[Insight]:
    hits = claims.in_categories("Bank Charges & Fees", "Interest & Penalties",
                                "Fines & Traffic")
    total = _sum(hits)
    if total < 20:
        return []
    by_kind: dict[str, float] = defaultdict(float)
    for row in hits:
        by_kind[row["category"]] += -float(row["amount"])
    penalty_words = ("unsuccessful", "unpaid", "declined", "dishonour", "honouring",
                     "late", "penalty", "arrears", "reversal", "fine")
    penalties = [r for r in hits
                 if any(w in (r["description"] or "").lower() for w in penalty_words)]
    penalty_total = _sum(penalties)

    detail = (f"{config.money(total, cfg.currency)} went on bank charges, interest and "
              f"fines — money that bought nothing. ")
    if penalty_total:
        detail += (f"{config.money(penalty_total, cfg.currency)} of that is failed debit "
                   f"orders, penalties and fines, which is a timing and admin problem "
                   f"rather than a pricing one. ")
    detail += "Breakdown: " + ", ".join(
        f"{k} {config.money(v, cfg.currency)}" for k, v in sorted(
            by_kind.items(), key=lambda kv: kv[1], reverse=True))

    per_month = total / months
    # Recurring account fees rarely go to zero; penalties and fines can.
    recoverable = (total - penalty_total) * 0.4 + penalty_total * 0.9
    monthly, annual = _saving(recoverable / months, freq.factor(hits))
    return [Insight(
        title="Bank charges, interest and fines",
        detail=detail,
        period_amount=total,
        annual_amount=round(per_month * 12, 2),
        monthly_saving=monthly,
        annual_saving=annual,
        confidence="high" if total > 200 else "medium",
        kind="avoidable",
        evidence=_evidence(hits, cfg),
        action=("Move debit order dates to just after payday to stop the failed-order "
                "fees, then ask the bank to reprice the account against actual usage."),
    )]


def _insight_delivery_premium(claims: _Claims, months: float, cfg: config.Settings,
                              freq: _Frequency) -> list[Insight]:
    hits = claims.in_categories("Food Delivery")
    total = _sum(hits)
    if total < 100:
        return []
    per_month = total / months
    # Delivery, service and small-order fees plus menu mark-up. A quarter of
    # order value is a working estimate — the exact split only appears on the
    # platform's own invoice, never on the bank line, so this is flagged medium.
    premium = total * 0.25
    monthly, annual = _saving(premium / months, freq.factor(hits))
    return [Insight(
        title="Food delivery premium",
        detail=(f"{len(hits)} delivery order(s) totalling "
                f"{config.money(total, cfg.currency)} — "
                f"{config.money(per_month, cfg.currency)} a month. Around "
                f"{config.money(premium, cfg.currency)} of that is delivery, service and "
                f"small-order fees rather than food. The same meals collected would cost "
                f"meaningfully less."),
        period_amount=total,
        annual_amount=round(per_month * 12, 2),
        monthly_saving=monthly,
        annual_saving=annual,
        confidence="medium",
        kind="frivolous",
        evidence=_evidence(hits, cfg),
        action=("Collect instead of delivering, or batch orders into one. Check a "
                "platform invoice for the actual fee split before fixing a target."),
    )]


def _recurring_rows(claims: _Claims, keys: set[str]) -> list[sqlite3.Row]:
    return claims.take(claims.available(lambda r: r["description_key"] in keys))


def _insight_duplicate_services(report: PeriodReport, claims: _Claims,
                                cfg: config.Settings) -> list[Insight]:
    """Overlapping services in one category, paid for in parallel."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for entry in report.recurring:
        if entry["fixed"] and taxonomy.get(entry["category"]).discretion >= 0.55:
            by_category[entry["category"]].append(entry)

    out: list[Insight] = []
    for category, entries in by_category.items():
        if len(entries) < 2:
            continue
        entries = sorted(entries, key=lambda e: e["annualised"], reverse=True)
        keep, drop = entries[0], entries[1:]
        drop_annual = sum(e["annualised"] for e in drop)
        if drop_annual < 240:
            continue
        claimed = _recurring_rows(claims, {e["key"] for e in drop})
        monthly, annual = _saving(sum(e["typical_amount"] for e in drop) * 0.6)
        out.append(Insight(
            title=f"{len(entries)} parallel services in {category}",
            detail=(f"You pay for {len(entries)} services in the same category: "
                    + ", ".join(f"{e['merchant']} "
                                f"({config.money(e['typical_amount'], cfg.currency)} a month)"
                                for e in entries)
                    + f". Keeping only {keep['merchant']} would release "
                    f"{config.money(drop_annual, cfg.currency)} a year."),
            period_amount=_sum(claimed),
            annual_amount=round(drop_annual, 2),
            monthly_saving=monthly,
            annual_saving=annual,
            confidence="medium",
            kind="duplicate",
            evidence=[f"{e['merchant']}: {config.money(e['typical_amount'], cfg.currency)} "
                      f"a month, seen in {e['months_seen']} month(s)" for e in entries],
            action=("Pick the one you actually use and pause the rest. Any of them can "
                    "be resubscribed in a minute if you miss it."),
        ))
    return out


def _insight_subscriptions(report: PeriodReport, claims: _Claims,
                           cfg: config.Settings) -> list[Insight]:
    subs = [r for r in report.recurring
            if r["fixed"] and taxonomy.get(r["category"]).discretion >= 0.55]
    if not subs:
        return []
    claimed = _recurring_rows(claims, {s["key"] for s in subs})
    if not claimed:
        return []
    remaining = [s for s in subs
                 if any(r["description_key"] == s["key"] for r in claimed)]
    if not remaining:
        return []
    annual = sum(s["annualised"] for s in remaining)
    per_period = _sum(claimed)
    if per_period < 50:
        return []
    monthly, annual_saving = _saving(sum(s["typical_amount"] for s in remaining) * 0.35)
    return [Insight(
        title=f"{len(remaining)} discretionary recurring charges",
        detail=(f"Recurring discretionary charges cost "
                f"{config.money(per_period, cfg.currency)} this period and "
                f"{config.money(annual, cfg.currency)} a year at current prices. "
                f"Recurring spend is the easiest kind to reduce, because it was decided "
                f"once and then never revisited."),
        period_amount=per_period,
        annual_amount=round(annual, 2),
        monthly_saving=monthly,
        annual_saving=annual_saving,
        confidence="medium",
        kind="subscription",
        evidence=[f"{s['merchant']} {config.money(s['typical_amount'], cfg.currency)} a "
                  f"month ({config.money(s['annualised'], cfg.currency)} a year, seen in "
                  f"{s['months_seen']} month(s))" for s in remaining[:10]],
        action=("Cancel anything on this list you cannot remember using in the last "
                "month. The saving assumes a third goes, not all of it."),
    )]


def _insight_small_frequent(claims: _Claims, months: float, cfg: config.Settings,
                            freq: _Frequency) -> list[Insight]:
    """The habit spend that never feels like a decision."""
    small = claims.available(
        lambda r: -float(r["amount"]) <= cfg.small_txn_threshold
        and taxonomy.get(r["category"]).discretion >= 0.5)
    if len(small) < 6:
        return []
    by_merchant: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in small:
        by_merchant[row["merchant"] or row["description_key"]].append(row)

    worst = sorted(by_merchant.items(),
                   key=lambda kv: sum(-float(r["amount"]) for r in kv[1]), reverse=True)
    out: list[Insight] = []
    for merchant, entries in worst[:3]:
        if len(entries) < 4:
            continue
        claims.take(entries)
        total = _sum(entries)
        per_month = total / months
        average = total / len(entries)
        monthly, annual = _saving(per_month * 0.5, freq.factor(entries))
        out.append(Insight(
            title=f"Habit spend at {merchant}",
            detail=(f"{len(entries)} purchases averaging "
                    f"{config.money(average, cfg.currency)}, "
                    f"{config.money(total, cfg.currency)} over the period. Individually "
                    f"invisible; {config.money(per_month * 12, cfg.currency)} a year."),
            period_amount=total,
            annual_amount=round(per_month * 12, 2),
            monthly_saving=monthly,
            annual_saving=annual,
            confidence="high" if len(entries) >= 8 else "medium",
            kind="habit",
            evidence=[f"{len(entries)} transactions between {entries[0]['txn_date']} "
                      f"and {entries[-1]['txn_date']}",
                      f"average {config.money(average, cfg.currency)} each"],
            action=("Halving the frequency is what makes this stick — the saving above "
                    "assumes exactly that, not going cold turkey."),
        ))
    return out


def _insight_top_discretionary(report: PeriodReport, claims: _Claims, months: float,
                               cfg: config.Settings,
                               freq: _Frequency) -> list[Insight]:
    """Whatever optional spend the specific insights above have not claimed."""
    leftovers: dict[str, list[sqlite3.Row]] = defaultdict(list)
    claimed_before = set(claims.claimed)
    for row in claims.available(lambda r: taxonomy.get(r["category"]).discretion >= 0.6):
        leftovers[row["category"]].append(row)

    ordered = sorted(leftovers.items(),
                     key=lambda kv: sum(-float(r["amount"]) for r in kv[1]), reverse=True)
    out: list[Insight] = []
    for category, entries in ordered:
        total = _sum(entries)
        if total < 200:
            continue
        claims.take(entries)
        meta = taxonomy.get(category)
        per_month = total / months
        # Half of the discretionary portion, on the view that a category rarely
        # gives up everything it theoretically could.
        monthly, annual = _saving(per_month * meta.discretion * 0.5,
                                  freq.factor(entries))
        partial = any(r["category"] == category and r["id"] in claimed_before
                      for r in claims.rows)
        detail = (f"{config.money(total, cfg.currency)} across {len(entries)} "
                  f"transaction(s), {config.money(per_month, cfg.currency)} a month. ")
        if partial:
            detail += "This is the part of the category not already listed above. "
        if meta.note:
            detail += meta.note + "."
        out.append(Insight(
            title=f"{category} is largely optional",
            detail=detail,
            period_amount=total,
            annual_amount=round(per_month * 12, 2),
            monthly_saving=monthly,
            annual_saving=annual,
            confidence="medium" if len(entries) >= 3 else "low",
            kind="frivolous",
            evidence=_evidence(entries, cfg, limit=5) + [
                f"treated as {int(meta.discretion * 100)}% discretionary; the saving "
                f"assumes half of that is actually given up"],
            action="Set a monthly ceiling for this category and track against it.",
        ))
        if len(out) >= 4:
            break
    return out


def _insight_cash_gap(report: PeriodReport, cfg: config.Settings) -> list[Insight]:
    """A visibility gap, not a saving — reported without a rand claim."""
    rec = report.reconciliation
    if rec.cash_unexplained < 300:
        return []
    share = (rec.cash_unexplained / rec.consumption * 100) if rec.consumption else 0
    return [Insight(
        title="Cash spend with nothing to explain it",
        detail=(f"{config.money(rec.cash_unexplained, cfg.currency)} of the "
                f"{config.money(rec.cash_withdrawn, cfg.currency)} withdrawn has no till "
                f"slip against it — {share:.0f}% of consumption spend is a blind spot. "
                f"This is not a saving; it is the part of the picture that cannot be "
                f"examined yet."),
        period_amount=rec.cash_unexplained,
        annual_amount=round(rec.cash_unexplained / report.period.months * 12, 2),
        monthly_saving=0.0,
        annual_saving=0.0,
        confidence="low",
        kind="visibility",
        evidence=[f"withdrawn {config.money(rec.cash_withdrawn, cfg.currency)}",
                  f"explained by slips {config.money(rec.cash_explained, cfg.currency)}"],
        action=("Keep till slips for cash purchases and add them with "
                "`spendtrack slip add`. Cash is where budgets go to hide."),
        counts_to_total=False,
    )]


def _insight_income_share(report: PeriodReport, cfg: config.Settings) -> list[Insight]:
    """Context only, and only when income is known rather than assumed."""
    rec = report.reconciliation
    income = rec.income or (cfg.monthly_income or 0) * report.period.months
    if income <= 0:
        return []
    discretionary = sum(b.total for b in report.categories if b.discretion >= 0.6)
    share = discretionary / income * 100
    if share < 12:
        return []
    return [Insight(
        title=f"Discretionary spend is {share:.0f}% of income",
        detail=(f"{config.money(discretionary, cfg.currency)} of the "
                f"{config.money(income, cfg.currency)} that came in went on categories "
                f"that are mostly optional. A 10% target would mean "
                f"{config.money(income * 0.10, cfg.currency)} over this period. This is "
                f"context for the suggestions above, not an additional saving."),
        period_amount=round(discretionary, 2),
        annual_amount=round(discretionary / report.period.months * 12, 2),
        monthly_saving=0.0,
        annual_saving=0.0,
        confidence="low",
        kind="benchmark",
        evidence=[f"income observed: {config.money(income, cfg.currency)}",
                  f"discretionary categories: {config.money(discretionary, cfg.currency)}",
                  "10% is a working target, not a rule"],
        action="Cap the two largest discretionary categories first.",
        counts_to_total=False,
    )]


# --------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------

def _data_quality(report: PeriodReport, rows: list[sqlite3.Row],
                  cfg: config.Settings) -> None:
    """Say plainly what would make these numbers more trustworthy."""
    notes = report.data_quality
    rec = report.reconciliation

    if not rows:
        notes.append("No transactions in this period. Import a statement covering it.")
        return
    if not rec.balances:
        notes.append(
            f"Buckets differ from the outflow total by "
            f"{config.money(rec.difference, cfg.currency)}. That is a bug — please report it."
        )
    uncat_total = sum(u["amount"] for u in report.uncategorised)
    if uncat_total > 0:
        share = uncat_total / (rec.consumption or 1) * 100
        notes.append(
            f"{len(report.uncategorised)} transaction(s) worth "
            f"{config.money(uncat_total, cfg.currency)} ({share:.0f}% of consumption) are "
            f"uncategorised. Run `spendtrack review` to assign them."
        )
    if rec.cash_unexplained > 0:
        notes.append(
            f"{config.money(rec.cash_unexplained, cfg.currency)} of cash has no till slip "
            f"against it, so what it bought is unknown."
        )
    statuses = report.slip_coverage.get("by_status", {})
    unmatched = statuses.get(matching.STATUS_UNMATCHED)
    if unmatched and unmatched["count"]:
        notes.append(
            f"{unmatched['count']} slip(s) worth "
            f"{config.money(unmatched['total'], cfg.currency)} could not be matched to a "
            f"statement line, so they are in no total. Check whether the statement "
            f"covering them is imported."
        )
    over = statuses.get(matching.STATUS_OVER_CASH)
    if over and over["count"]:
        notes.append(
            f"{over['count']} cash slip(s) worth "
            f"{config.money(over['total'], cfg.currency)} exceed the cash withdrawn. "
            f"Either a statement is missing or the cash came from elsewhere; they are "
            f"not counted."
        )
    if len(report.accounts) > 1:
        notes.append(
            f"Combining {len(report.accounts)} accounts ({', '.join(report.accounts)}). "
            f"Transfers between them are excluded from spend."
        )
    if report.period.days < 25:
        notes.append(
            f"This period is {report.period.days} days, so the monthly and annual "
            f"figures are extrapolated from a short window and will be rough."
        )
    if report.months_observed < 2:
        notes.append(
            "Only one month of data is loaded, so annual figures assume this month is "
            "typical. Import a few more months and they will be based on how often "
            "each kind of spend actually occurs."
        )
    elif report.months_observed < 4:
        notes.append(
            f"{report.months_observed} months of data are loaded. Annual figures are "
            f"scaled by how often each category appears across those months, so they "
            f"will firm up as more history is added."
        )


# --------------------------------------------------------------------------
# Comparison across periods
# --------------------------------------------------------------------------

def compare(conn: sqlite3.Connection, periods: list[Period],
            settings: config.Settings | None = None) -> dict:
    """Category totals side by side across periods, for trend spotting."""
    cfg = settings or config.Settings.load()
    reports = [build_report(conn, p, cfg) for p in periods]
    names: set[str] = set()
    for report in reports:
        names.update(b.name for b in report.categories)
    table = []
    for name in sorted(names):
        values = []
        for report in reports:
            found = next((b.total for b in report.categories if b.name == name), 0.0)
            values.append(found)
        change = values[-1] - values[0] if len(values) > 1 else 0.0
        table.append({"category": name, "values": values, "change": round(change, 2)})
    table.sort(key=lambda r: abs(r["change"]), reverse=True)
    return {"periods": [p.label for p in periods], "rows": table, "reports": reports}
