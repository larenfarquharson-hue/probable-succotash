"""Frivolous spend detection and reduction targets.

What this does and does not claim
---------------------------------
Every dedicated saving figure here is an *estimate* built from a stated
assumption, not a measurement. The app can see that R2 100 went to takeaways
last month; it cannot see whether that was three celebrations or thirty lazy
Tuesdays. So each finding carries the assumption it used, the evidence behind
it, and a difficulty rating - and the totals are deliberately conservative.

Savings are claimed through a ledger (:class:`_ClaimLedger`) so the same rand
is never counted twice. Without it, "your takeaway spend is high" and "you are
paying delivery fees on every order" would both bank the same money and the
headline number would be fiction. A category can also never have more than its
:func:`taxonomy.reducible_fraction` claimed in total.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import analytics, taxonomy
from .analytics import PeriodSummary, Recurring
from .config import Config
from .periods import Period

# Difficulty of actually making the cut stick.
EASY, MODERATE, HARD = "easy", "moderate", "hard"


@dataclass
class Finding:
    """One concrete, actionable reduction opportunity."""

    key: str
    title: str
    detail: str
    assumption: str
    monthly_saving_cents: int
    difficulty: str = MODERATE
    confidence: str = "medium"          # high|medium|low
    categories: list[str] = field(default_factory=list)
    merchants: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    severity: float = 0.0               # ranking score

    @property
    def annual_saving_cents(self) -> int:
        return self.monthly_saving_cents * 12


@dataclass
class FrivolousItem:
    """A single transaction judged discretionary, with a reason."""

    txn_id: int
    txn_date: date
    merchant: str
    category: str
    amount_cents: int
    score: float            # 0..1
    reasons: list[str] = field(default_factory=list)


@dataclass
class AdviceReport:
    period: Period
    currency_symbol: str = "R"
    findings: list[Finding] = field(default_factory=list)
    frivolous: list[FrivolousItem] = field(default_factory=list)
    frivolous_total_cents: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def monthly_total_cents(self) -> int:
        return sum(f.monthly_saving_cents for f in self.findings)

    @property
    def annual_total_cents(self) -> int:
        return self.monthly_total_cents * 12

    def by_difficulty(self, difficulty: str) -> list[Finding]:
        return [f for f in self.findings if f.difficulty == difficulty]

    def validate(self, monthly_spend_cents: int) -> list[str]:
        """Sanity-check the numbers we are about to show someone.

        The headline saving must never exceed the reducible headroom, and no
        single suggestion may claim more than total monthly spend. A breach is a
        bug in a generator, not a finding about the user's money.
        """
        problems: list[str] = []
        if self.monthly_total_cents > monthly_spend_cents:
            problems.append(
                f"total suggested saving ({self.monthly_total_cents}c/month) exceeds total "
                f"monthly spend ({monthly_spend_cents}c)"
            )
        for f in self.findings:
            if f.monthly_saving_cents > monthly_spend_cents:
                problems.append(f"finding {f.key!r} claims more than total monthly spend")
        return problems


class _ClaimLedger:
    """Stops two findings banking the same rand.

    Each category has a ceiling: its *monthly* spend times the fraction that
    could plausibly be cut. Findings draw against that ceiling in the order they
    are generated (most specific and most defensible first), and a finding that
    finds nothing left to claim is dropped rather than shown with a fake number.

    Everything in this ledger is per-month, matching
    ``Finding.monthly_saving_cents``. Mixing a period total into it would let a
    finding claim a larger saving than the category's entire monthly spend.
    """

    def __init__(self, category_spend: dict[str, int], months: float):
        self.months = max(months, 1 / 30.44)
        self.ceilings = {
            cat: int((cents / self.months) * taxonomy.reducible_fraction(cat))
            for cat, cents in category_spend.items()
        }
        self.claimed: dict[str, int] = {}

    def available(self, category: str) -> int:
        return max(0, self.ceilings.get(category, 0) - self.claimed.get(category, 0))

    def claim(self, category: str, cents: int) -> int:
        """Claim up to ``cents`` against a category; returns what was granted."""
        granted = min(cents, self.available(category))
        if granted > 0:
            self.claimed[category] = self.claimed.get(category, 0) + granted
        return granted

    def claim_across(self, categories: list[str], cents: int) -> int:
        """Spread a claim over several categories, in the order given."""
        remaining, granted = cents, 0
        for cat in categories:
            if remaining <= 0:
                break
            got = self.claim(cat, remaining)
            granted += got
            remaining -= got
        return granted


# ---------------------------------------------------------------------------
# Frivolity at transaction level
# ---------------------------------------------------------------------------

# Merchants whose whole purpose is discretionary, regardless of category.
_ALWAYS_DISCRETIONARY_HINTS = (
    "bet", "casino", "lotto", "lottery", "wager", "vape", "tobacco",
)


def score_frivolity(
    *,
    category: str,
    merchant: str,
    amount_cents: int,
    merchant_monthly_count: float,
    merchant_flag: int | None,
    large_txn_cents: int,
) -> tuple[float, list[str]]:
    """Score one transaction 0..1 on how discretionary it looks."""
    reasons: list[str] = []
    base = taxonomy.category_frivolity(category) / 3.0
    score = base
    if base >= 0.9:
        reasons.append(f"{category} is discretionary by nature")
    elif base >= 0.6:
        reasons.append(f"{category} is largely a choice")

    kind = taxonomy.category_kind(category)
    if kind == "essential":
        score *= 0.45
    elif kind in taxonomy.EXCLUDED_KINDS:
        return 0.0, ["not spending"]

    low = merchant.lower()
    if merchant_flag == 1 or any(h in low for h in _ALWAYS_DISCRETIONARY_HINTS):
        score = max(score, 0.9)
        reasons.append("this merchant is purely discretionary")

    # Habit spend: small amounts, many times a month. Individually trivial,
    # collectively the biggest reducible line most people have.
    if merchant_monthly_count >= 6 and amount_cents <= 15_000:
        score = min(1.0, score + 0.15)
        reasons.append(f"about {merchant_monthly_count:.0f} small purchases a month here")

    # A single large discretionary purchase is a decision, not a habit - worth
    # surfacing, but it is not "frivolous" merely for being big.
    if amount_cents >= large_txn_cents and kind == "discretionary":
        reasons.append("large one-off purchase")

    return min(1.0, round(score, 3)), reasons


def frivolous_transactions(
    conn: sqlite3.Connection,
    period: Period,
    *,
    cfg: Config,
    threshold: float = 0.6,
    limit: int = 200,
    account_id: int | None = None,
) -> tuple[list[FrivolousItem], int]:
    """Transactions scoring above ``threshold``, plus their total."""
    start, end = period.as_iso()
    clause = " AND t.account_id = ?" if account_id is not None else ""
    params: list = [start, end] + ([account_id] if account_id is not None else [])

    rows = conn.execute(
        f"""SELECT t.id, t.txn_date, t.amount_cents,
                   COALESCE(NULLIF(t.merchant_norm,''),'Unknown') AS merchant,
                   COALESCE(t.category,'Uncategorised') AS category,
                   m.is_frivolous AS merchant_flag
            FROM transactions t
            LEFT JOIN merchants m ON m.id = t.merchant_id
            WHERE t.status='active' AND t.amount_cents < 0
              AND t.txn_date BETWEEN ? AND ?{clause}""",
        params,
    ).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["merchant"]] = counts.get(r["merchant"], 0) + 1

    items: list[FrivolousItem] = []
    total = 0
    for r in rows:
        amount = -int(r["amount_cents"])
        score, reasons = score_frivolity(
            category=r["category"],
            merchant=r["merchant"],
            amount_cents=amount,
            merchant_monthly_count=counts[r["merchant"]] / period.months,
            merchant_flag=r["merchant_flag"],
            large_txn_cents=cfg.large_txn_cents,
        )
        if score >= threshold:
            total += amount
            items.append(
                FrivolousItem(
                    txn_id=int(r["id"]),
                    txn_date=date.fromisoformat(r["txn_date"]),
                    merchant=r["merchant"],
                    category=r["category"],
                    amount_cents=amount,
                    score=score,
                    reasons=reasons,
                )
            )

    items.sort(key=lambda i: (i.score, i.amount_cents), reverse=True)
    return items[:limit], total


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

# Overlapping service groups - paying for several at once is usually accidental.
_SERVICE_GROUPS = {
    "video streaming": [
        "netflix", "showmax", "dstv", "multichoice", "disney", "amazon prime",
        "apple tv", "youtube premium", "britbox",
    ],
    "music streaming": ["spotify", "apple music", "youtube music", "deezer", "tidal"],
    "cloud storage": ["dropbox", "icloud", "google one", "onedrive", "microsoft 365", "box"],
    "gym membership": ["virgin active", "planet fitness", "crossfit", "run/walk for life"],
    "AI assistants": ["openai", "anthropic", "chatgpt", "claude", "gemini", "copilot"],
}


def build_advice(
    conn: sqlite3.Connection,
    period: Period,
    *,
    cfg: Config,
    account_id: int | None = None,
    summary: PeriodSummary | None = None,
) -> AdviceReport:
    """Generate ranked reduction opportunities for a period."""
    summary = summary or analytics.period_summary(
        conn, period, cfg=cfg, account_id=account_id
    )
    report = AdviceReport(period=period, currency_symbol=cfg.currency_symbol)

    category_spend = {
        b.name: b.total_cents
        for b in summary.by_category
        if b.kind not in taxonomy.EXCLUDED_KINDS
    }
    months = period.months
    ledger = _ClaimLedger(category_spend, months)

    recurring = analytics.find_recurring(conn, account_id=account_id)
    subscriptions = [r for r in recurring if r.is_subscription and r.still_active]

    # Order matters: the most specific, most defensible findings claim first.
    generators = [
        _finding_pure_waste,
        _finding_bank_fees,
        _finding_overlapping_services,
        _finding_delivery_premium,
        _finding_habit_spend,
        _finding_gambling,
        _finding_small_subscriptions,
        _finding_price_creep,
        _finding_category_spike,
        _finding_top_discretionary,
    ]

    for gen in generators:
        for finding in gen(
            conn=conn,
            period=period,
            cfg=cfg,
            summary=summary,
            ledger=ledger,
            recurring=recurring,
            subscriptions=subscriptions,
            months=months,
            account_id=account_id,
        ):
            if finding.monthly_saving_cents > 0:
                report.findings.append(finding)

    report.findings.sort(
        key=lambda f: (f.monthly_saving_cents, f.severity), reverse=True
    )

    report.frivolous, report.frivolous_total_cents = frivolous_transactions(
        conn, period, cfg=cfg, account_id=account_id
    )

    report.notes.append(
        "Savings are estimates. Each one states the assumption behind it, and no rand is "
        "counted in more than one suggestion."
    )
    for problem in report.validate(int(summary.spend_cents / months)):
        report.notes.append(f"Internal check failed - please report this: {problem}")
    if summary.reconciliation.cash_unexplained_cents > 0:
        report.notes.append(
            f"{cfg.currency_symbol}"
            f"{summary.reconciliation.cash_unexplained_cents / 100:,.2f} of cash spend has no "
            "till slip, so none of it could be assessed. Real frivolous spend is likely higher "
            "than shown."
        )
    if summary.reconciliation.uncategorised_cents > 0:
        report.notes.append(
            f"{cfg.currency_symbol}"
            f"{summary.reconciliation.uncategorised_cents / 100:,.2f} is uncategorised and was "
            "left out of every suggestion."
        )
    return report


def _bucket(summary: PeriodSummary, name: str):
    for b in summary.by_category:
        if b.name == name:
            return b
    return None


def _fmt(cents: int, symbol: str) -> str:
    return f"{symbol}{cents / 100:,.2f}"


# --- individual generators --------------------------------------------------


def _finding_pure_waste(*, summary, ledger, cfg, months, **_) -> list[Finding]:
    """Fines and penalty interest: avoidable in full, not a lifestyle choice."""
    out: list[Finding] = []
    sym = cfg.currency_symbol
    for cat, label, note in [
        (
            "Fines & Penalties",
            "traffic and municipal fines",
            "Fines are avoidable in full. Paying them promptly also avoids the escalation fees.",
        ),
        (
            "Interest & Penalties",
            "overdraft and penalty interest",
            "Interest on an overdrawn account buys you nothing. Clearing the overdraft, even "
            "slowly, converts this straight into savings.",
        ),
    ]:
        b = _bucket(summary, cat)
        if not b or b.total_cents <= 0:
            continue
        monthly = int(b.total_cents / months)
        granted = ledger.claim(cat, int(monthly * 0.9))
        if granted <= 0:
            continue
        out.append(
            Finding(
                key=f"waste:{cat}",
                title=f"You paid {_fmt(b.total_cents, sym)} in {label}",
                detail=note,
                assumption="Assumes 90% of this is avoidable with no change to your lifestyle.",
                monthly_saving_cents=granted,
                difficulty=EASY,
                confidence="high",
                categories=[cat],
                evidence=[f"{b.count} charge(s) totalling {_fmt(b.total_cents, sym)}"],
                severity=3.0,
            )
        )
    return out


def _finding_bank_fees(*, summary, ledger, cfg, months, **_) -> list[Finding]:
    b = _bucket(summary, "Bank Fees")
    if not b or b.total_cents <= 0:
        return []
    sym = cfg.currency_symbol
    monthly = int(b.total_cents / months)
    if monthly < 2_000:  # under R20/month is not worth a person's attention
        return []
    granted = ledger.claim("Bank Fees", int(monthly * 0.45))
    if granted <= 0:
        return []
    return [
        Finding(
            key="fees:bank",
            title=f"Bank fees are costing you {_fmt(monthly, sym)} a month",
            detail=(
                "Fees are pure leakage - you get no goods for them. Bundled accounts, "
                "switching off paid SMS notifications in favour of free app notifications, "
                "and avoiding cash-handling and declined-transaction fees usually recover "
                "most of this. Compare your account's fee table against what you actually use."
            ),
            assumption="Assumes 45% is recoverable by changing account type and notification settings.",
            monthly_saving_cents=granted,
            difficulty=EASY,
            confidence="medium",
            categories=["Bank Fees"],
            evidence=[f"{b.count} fee charge(s), {_fmt(b.total_cents, sym)} over the period"],
            severity=2.5,
        )
    ]


def _finding_overlapping_services(*, subscriptions, ledger, cfg, **_) -> list[Finding]:
    """Several services doing the same job."""
    out: list[Finding] = []
    sym = cfg.currency_symbol
    for group, needles in _SERVICE_GROUPS.items():
        hits = [
            s
            for s in subscriptions
            if any(n in s.merchant.lower() for n in needles)
        ]
        if len(hits) < 2:
            continue
        hits.sort(key=lambda s: s.monthly_equivalent_cents, reverse=True)
        # Keep the most expensive (usually the one actually used); the rest are
        # the candidate saving.
        droppable = hits[1:]
        want = sum(s.monthly_equivalent_cents for s in droppable)
        cats = list(dict.fromkeys(s.category for s in droppable))
        granted = ledger.claim_across(cats, want)
        if granted <= 0:
            continue
        names = ", ".join(s.merchant for s in hits)
        out.append(
            Finding(
                key=f"overlap:{group}",
                title=f"You are paying for {len(hits)} {group} services",
                detail=(
                    f"{names}. Keeping only the one you use most would save "
                    f"{_fmt(granted, sym)} a month. Check which you actually opened this month "
                    "before cancelling."
                ),
                assumption=f"Assumes you keep {hits[0].merchant} and drop the rest.",
                monthly_saving_cents=granted,
                difficulty=EASY,
                confidence="medium",
                categories=cats,
                merchants=[s.merchant for s in hits],
                evidence=[
                    f"{s.merchant}: {_fmt(s.typical_cents, sym)} {s.cadence}, last charged "
                    f"{s.last_seen.isoformat()}"
                    for s in hits
                ],
                severity=2.8,
            )
        )
    return out


def _finding_delivery_premium(*, conn, period, summary, ledger, cfg, months, account_id, **_) -> list[Finding]:
    """Food delivery carries a structural premium over the same food collected."""
    sym = cfg.currency_symbol
    start, end = period.as_iso()
    clause = " AND account_id = ?" if account_id is not None else ""
    params: list = [start, end] + ([account_id] if account_id is not None else [])
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(merchant_norm,''),'Unknown') AS merchant,
                   COUNT(*) AS n, SUM(-amount_cents) AS total
            FROM transactions
            WHERE status='active' AND amount_cents < 0
              AND txn_date BETWEEN ? AND ?{clause}
              AND (LOWER(merchant_norm) LIKE '%uber eats%'
                OR LOWER(merchant_norm) LIKE '%mr d%'
                OR LOWER(merchant_norm) LIKE '%bolt food%'
                OR LOWER(merchant_norm) LIKE '%checkers sixty60%'
                OR LOWER(merchant_norm) LIKE '%woolies dash%')
            GROUP BY 1""",
        params,
    ).fetchall()
    if not rows:
        return []
    total = sum(int(r["total"]) for r in rows)
    orders = sum(int(r["n"]) for r in rows)
    if orders < 3:
        return []
    monthly = int(total / months)
    # Delivery apps typically add a delivery fee plus a menu mark-up. 25% is a
    # conservative view of the premium over collecting the same food.
    want = int(monthly * 0.25)
    cats = list(
        dict.fromkeys(
            ["Delivery Fees & Convenience", "Eating Out & Takeaways", "Groceries"]
        )
    )
    granted = ledger.claim_across(cats, want)
    if granted <= 0:
        return []
    per_order = int(total / orders)
    return [
        Finding(
            key="delivery:premium",
            title=f"{orders} delivery orders cost you {_fmt(total, sym)}",
            detail=(
                f"That is about {_fmt(per_order, sym)} an order. Delivery adds a fee plus a menu "
                "mark-up on top of the food itself, so collecting the same order - or cooking it - "
                "removes the premium without removing the meal. Halving the number of delivered "
                "orders is usually easier to sustain than cutting them out."
            ),
            assumption="Assumes the delivery premium is about 25% of the order value.",
            monthly_saving_cents=granted,
            difficulty=MODERATE,
            confidence="medium",
            categories=cats,
            merchants=[r["merchant"] for r in rows],
            evidence=[
                f"{r['merchant']}: {r['n']} order(s), {_fmt(int(r['total']), sym)}" for r in rows
            ],
            severity=2.4,
        )
    ]


def _finding_habit_spend(*, conn, period, ledger, cfg, months, account_id, **_) -> list[Finding]:
    """Small, frequent, same-merchant purchases: the classic invisible drain."""
    sym = cfg.currency_symbol
    start, end = period.as_iso()
    clause = " AND account_id = ?" if account_id is not None else ""
    params: list = [start, end] + ([account_id] if account_id is not None else [])
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(merchant_norm,''),'Unknown') AS merchant,
                   COALESCE(category,'Uncategorised') AS category,
                   COUNT(*) AS n, SUM(-amount_cents) AS total, AVG(-amount_cents) AS avg
            FROM transactions
            WHERE status='active' AND amount_cents < 0
              AND txn_date BETWEEN ? AND ?{clause}
            GROUP BY 1,2
            HAVING COUNT(*) >= 6 AND AVG(-amount_cents) <= 20000
            ORDER BY total DESC""",
        params,
    ).fetchall()

    out: list[Finding] = []
    for r in rows:
        cat = r["category"]
        if taxonomy.category_kind(cat) in taxonomy.EXCLUDED_KINDS:
            continue
        if taxonomy.category_frivolity(cat) < 2:
            continue
        total, n = int(r["total"]), int(r["n"])
        per_month = n / months
        if per_month < 4:
            continue
        monthly = int(total / months)
        want = int(monthly * 0.5)
        granted = ledger.claim(cat, want)
        if granted <= 0:
            continue
        avg = int(r["avg"])
        out.append(
            Finding(
                key=f"habit:{r['merchant']}",
                title=(
                    f"{r['merchant']}: {per_month:.0f} small purchases a month, "
                    f"{_fmt(monthly, sym)}"
                ),
                detail=(
                    f"Average {_fmt(avg, sym)} a time. No single one feels like a decision, which "
                    f"is exactly why it adds up to {_fmt(monthly * 12, sym)} a year. Halving the "
                    "frequency - not the amount - is the change that tends to stick."
                ),
                assumption="Assumes you halve how often you buy here, at the same price per visit.",
                monthly_saving_cents=granted,
                difficulty=MODERATE,
                confidence="medium",
                categories=[cat],
                merchants=[r["merchant"]],
                evidence=[f"{n} purchase(s) over {period.label}, {_fmt(total, sym)} total"],
                severity=2.2,
            )
        )
        if len(out) >= 6:
            break
    return out


def _finding_gambling(*, summary, ledger, cfg, months, **_) -> list[Finding]:
    b = _bucket(summary, "Gambling & Betting")
    if not b or b.total_cents <= 0:
        return []
    sym = cfg.currency_symbol
    monthly = int(b.total_cents / months)
    granted = ledger.claim("Gambling & Betting", int(monthly * 0.95))
    if granted <= 0:
        return []
    share = b.share
    return [
        Finding(
            key="gambling",
            title=f"Betting is {_fmt(monthly, sym)} a month, {share:.0%} of your spending",
            detail=(
                f"{b.count} transactions over {summary.period.label}, "
                f"{_fmt(b.total_cents, sym)} in total. This is the single largest fully "
                "discretionary line the app can see, and unlike most categories it returns "
                "nothing you keep. Most South African operators support deposit limits and "
                "self-exclusion, and your bank can block the merchant category outright. "
                "If the amount is larger than you expected, the National Responsible Gambling "
                "Programme counselling line is 0800 006 008."
            ),
            assumption="Assumes this is fully discretionary, which the category is by definition.",
            monthly_saving_cents=granted,
            difficulty=HARD,
            confidence="high",
            categories=["Gambling & Betting"],
            evidence=[f"{b.count} transaction(s), largest {_fmt(b.largest_cents, sym)}"],
            severity=4.0,
        )
    ]


def _finding_small_subscriptions(*, subscriptions, ledger, cfg, **_) -> list[Finding]:
    """Lots of small monthly charges nobody re-decides."""
    sym = cfg.currency_symbol
    small = [
        s
        for s in subscriptions
        if s.monthly_equivalent_cents <= 30_000
        and taxonomy.category_kind(s.category) not in taxonomy.EXCLUDED_KINDS
        and taxonomy.category_frivolity(s.category) >= 2
    ]
    if len(small) < 3:
        return []
    total = sum(s.monthly_equivalent_cents for s in small)
    cats = list(dict.fromkeys(s.category for s in small))
    granted = ledger.claim_across(cats, int(total * 0.4))
    if granted <= 0:
        return []
    return [
        Finding(
            key="subs:small",
            title=f"{len(small)} small subscriptions add up to {_fmt(total, sym)} a month",
            detail=(
                "Individually each is easy to ignore; together they are "
                f"{_fmt(total * 12, sym)} a year that renews without anyone deciding to renew it. "
                "Go through the list and cancel anything you have not deliberately used this "
                "month - you can always resubscribe, and most keep your history."
            ),
            assumption="Assumes about 40% of these are no longer genuinely used.",
            monthly_saving_cents=granted,
            difficulty=EASY,
            confidence="low",
            categories=cats,
            merchants=[s.merchant for s in small],
            evidence=[
                f"{s.merchant}: {_fmt(s.typical_cents, sym)} {s.cadence} "
                f"(last {s.last_seen.isoformat()})"
                for s in small
            ],
            severity=2.0,
        )
    ]


def _finding_price_creep(*, conn, recurring, ledger, cfg, account_id, **_) -> list[Finding]:
    """Recurring charges that have quietly gone up."""
    sym = cfg.currency_symbol
    out: list[Finding] = []
    risers: list[tuple[str, int, int, str]] = []
    for r in recurring:
        if not r.is_subscription or not r.still_active or r.occurrences < 4:
            continue
        clause = " AND account_id = ?" if account_id is not None else ""
        params: list = [r.merchant] + ([account_id] if account_id is not None else [])
        amounts = [
            int(x["c"])
            for x in conn.execute(
                f"""SELECT -amount_cents AS c FROM transactions
                    WHERE status='active' AND amount_cents<0 AND merchant_norm = ?{clause}
                    ORDER BY txn_date""",
                params,
            )
        ]
        if len(amounts) < 4:
            continue
        first_half = amounts[: len(amounts) // 2]
        second_half = amounts[len(amounts) // 2 :]
        old = sum(first_half) / len(first_half)
        new = sum(second_half) / len(second_half)
        if old <= 0:
            continue
        increase = (new - old) / old
        if increase >= 0.10 and (new - old) >= 1_000:
            risers.append((r.merchant, int(old), int(new), r.category))

    if not risers:
        return []
    want = sum(new - old for _m, old, new, _c in risers)
    cats = list(dict.fromkeys(c for *_x, c in risers))
    granted = ledger.claim_across(cats, want)
    if granted <= 0:
        return []
    return [
        Finding(
            key="subs:price-creep",
            title=f"{len(risers)} recurring charge(s) have gone up",
            detail=(
                "Price increases on debit orders rarely get renegotiated because nobody notices "
                "them. These are worth a phone call - insurers and connectivity providers in "
                "particular will often match a competitor's quote to keep you."
            ),
            assumption="Assumes you can negotiate back to the earlier price.",
            monthly_saving_cents=granted,
            difficulty=MODERATE,
            confidence="low",
            categories=cats,
            merchants=[m for m, *_x in risers],
            evidence=[
                f"{m}: {_fmt(old, sym)} -> {_fmt(new, sym)} ({(new - old) / old:+.0%})"
                for m, old, new, _c in risers
            ],
            severity=1.8,
        )
    ]
    return out


def _finding_category_spike(*, conn, period, cfg, ledger, account_id, **_) -> list[Finding]:
    """A category well above its own recent norm."""
    sym = cfg.currency_symbol
    trend = analytics.monthly_trend(conn, cfg=cfg, months=6, account_id=account_id)
    if len(trend) < 3:
        return []

    # Per-category history across the trailing months.
    history: dict[str, list[int]] = {}
    for point in trend:
        s = analytics.period_summary(
            conn, point.period, cfg=cfg, account_id=account_id, merchant_limit=1
        )
        seen = set()
        for b in s.by_category:
            if b.kind in taxonomy.EXCLUDED_KINDS:
                continue
            history.setdefault(b.name, []).append(b.total_cents)
            seen.add(b.name)
        for name in history:
            if name not in seen:
                history[name].append(0)

    out: list[Finding] = []
    for cat, series in history.items():
        if len(series) < 3:
            continue
        latest, prior = series[-1], series[:-1]
        baseline = sum(prior) / len(prior)
        if baseline <= 0 or latest <= baseline * 1.4 or latest - baseline < 20_000:
            continue
        if taxonomy.category_frivolity(cat) < 2:
            continue
        want = int((latest - baseline) * 0.6)
        granted = ledger.claim(cat, want)
        if granted <= 0:
            continue
        out.append(
            Finding(
                key=f"spike:{cat}",
                title=(
                    f"{cat} jumped to {_fmt(latest, sym)} in {trend[-1].period.label}, "
                    f"from a {_fmt(int(baseline), sym)} average"
                ),
                detail=(
                    "Returning to your own recent average - not to zero - would save "
                    f"{_fmt(granted, sym)} a month. Worth checking whether the jump was a "
                    "one-off you already know about or a new habit forming."
                ),
                assumption="Assumes 60% of the increase over your own average is reversible.",
                monthly_saving_cents=granted,
                difficulty=MODERATE,
                confidence="medium",
                categories=[cat],
                evidence=[
                    f"{p.period.label}: {_fmt(v, sym)}" for p, v in zip(trend, series)
                ],
                severity=2.1,
            )
        )
        if len(out) >= 3:
            break
    return out


def _finding_top_discretionary(*, summary, ledger, cfg, months, **_) -> list[Finding]:
    """Whatever reducible headroom is left, on the biggest discretionary lines."""
    sym = cfg.currency_symbol
    out: list[Finding] = []
    for b in summary.by_category:
        if b.kind not in ("discretionary", "cash"):
            continue
        if taxonomy.category_frivolity(b.name) < 2:
            continue
        available = ledger.available(b.name)
        monthly = int(b.total_cents / months)
        # Only bother if meaningful headroom remains and the line is material.
        if available < 5_000 or monthly < 10_000:
            continue
        granted = ledger.claim(b.name, available)
        if granted <= 0:
            continue
        pct = granted / monthly if monthly else 0
        out.append(
            Finding(
                key=f"trim:{b.name}",
                title=f"Trim {b.name}: {_fmt(monthly, sym)} a month currently",
                detail=(
                    f"A {pct:.0%} reduction here is {_fmt(granted, sym)} a month, "
                    f"{_fmt(granted * 12, sym)} a year. Spread across {b.count} transaction(s), "
                    "so it is a series of small choices rather than one sacrifice."
                ),
                assumption=(
                    f"Assumes {taxonomy.reducible_fraction(b.name):.0%} of this category is "
                    "reducible in principle, less anything already claimed above."
                ),
                monthly_saving_cents=granted,
                difficulty=MODERATE,
                confidence="low",
                categories=[b.name],
                evidence=[
                    f"{b.count} transaction(s), largest {_fmt(b.largest_cents, sym)}, "
                    f"average {_fmt(b.average_cents, sym)}"
                ],
                severity=1.5,
            )
        )
        if len(out) >= 5:
            break
    return out
