"""Spending categories and how reducible each one is.

`discretion` is the share of a category that is *typically* optional — the lever
used to size a realistic saving rather than pretending an entire category could
go to zero. These are heuristic starting points, not facts about your life; edit
them in ~/.spendtrack/rules.json under "categories" to match reality.

`kind` drives how a line is treated in the period reconciliation:
  spend    — real consumption
  transfer — movement between your own accounts (nets to zero, excluded)
  saving   — money out of the account but still yours
  debt     — capital repayment on a loan or bond
  income   — money in
  refund   — money back that offsets earlier spend
  unknown  — cash out of the account, awaiting slips to explain it
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    name: str
    group: str
    discretion: float      # 0.0 untouchable .. 1.0 entirely optional
    kind: str = "spend"
    note: str = ""


_C = [
    # ---- Essentials -----------------------------------------------------
    Category("Housing & Rent", "Essentials", 0.00, note="Renegotiable only on renewal"),
    Category("Bond & Home Loan", "Essentials", 0.00, kind="debt"),
    Category("Rates & Levies", "Essentials", 0.00),
    Category("Electricity & Water", "Essentials", 0.15,
             note="Usage-driven; geyser timers and LED swaps are the real lever"),
    Category("Groceries", "Essentials", 0.20,
             note="Brand and shop choice, not quantity, is where this moves"),
    Category("Medical & Healthcare", "Essentials", 0.05),
    Category("Medical Aid", "Essentials", 0.10, note="Plan downgrade is the lever"),
    Category("Insurance", "Essentials", 0.20, note="Re-quote annually"),
    Category("Education & Childcare", "Essentials", 0.05),
    Category("Tax & SARS", "Essentials", 0.00),
    Category("Debt Repayment", "Essentials", 0.00, kind="debt"),

    # ---- Semi-discretionary ---------------------------------------------
    Category("Transport & Fuel", "Getting around", 0.20),
    Category("Ride Hailing", "Getting around", 0.45),
    Category("Vehicle & Maintenance", "Getting around", 0.20),
    Category("Vehicle Finance", "Getting around", 0.00, kind="debt"),
    Category("Tolls & Parking", "Getting around", 0.25),
    Category("Telecoms & Data", "Household", 0.35,
             note="Almost always over-specified; check actual usage against the plan"),
    Category("Home & Garden", "Household", 0.40),
    Category("Household Supplies", "Household", 0.20),
    Category("Beauty & Personal Care", "Lifestyle", 0.45),
    Category("Health & Fitness", "Lifestyle", 0.50,
             note="Only a saving if attendance is low"),
    Category("Pets", "Lifestyle", 0.20),
    Category("Gifts & Donations", "Lifestyle", 0.40),
    Category("Professional & Work", "Other", 0.20),

    # ---- Discretionary ---------------------------------------------------
    Category("Eating Out", "Discretionary", 0.70),
    Category("Takeaways & Fast Food", "Discretionary", 0.75),
    Category("Food Delivery", "Discretionary", 0.85,
             note="Delivery and service fees are pure premium over the same food collected"),
    Category("Coffee & Snacks", "Discretionary", 0.80,
             note="Small and frequent — the annualised number is the point"),
    Category("Alcohol & Tobacco", "Discretionary", 0.70),
    Category("Streaming & Subscriptions", "Discretionary", 0.80,
             note="Check for overlapping services and anything unwatched"),
    Category("Apps & Software", "Discretionary", 0.60),
    Category("Entertainment & Events", "Discretionary", 0.75),
    Category("Gaming", "Discretionary", 0.85),
    Category("Clothing & Apparel", "Discretionary", 0.65),
    Category("Electronics & Gadgets", "Discretionary", 0.80),
    Category("Online Shopping", "Discretionary", 0.70),
    Category("Travel & Accommodation", "Discretionary", 0.70),
    Category("Gambling & Betting", "Discretionary", 1.00,
             note="No budget depends on this; treated as fully reducible"),
    Category("Lottery", "Discretionary", 1.00),

    # ---- Avoidable costs -------------------------------------------------
    Category("Bank Charges & Fees", "Avoidable cost", 0.60,
             note="Often a wrong-account-type problem; one call fixes it"),
    Category("Interest & Penalties", "Avoidable cost", 0.85,
             note="The cost of timing, not of anything you received"),
    Category("Fines & Traffic", "Avoidable cost", 0.90),

    # ---- Money in --------------------------------------------------------
    Category("Income", "Money in", 0.00, kind="income"),
    Category("Interest Received", "Money in", 0.00, kind="income"),
    Category("Refunds & Reversals", "Money in", 0.00, kind="refund",
             note="Offsets earlier spend rather than being new money"),

    # ---- Not consumption -------------------------------------------------
    Category("Savings & Investments", "Not spend", 0.00, kind="saving",
             note="An outflow from the account, but still your money"),
    Category("Transfers", "Not spend", 0.00, kind="transfer"),
    Category("Cash Withdrawals", "Needs explaining", 0.50, kind="unknown",
             note="Add till slips to see what the cash actually bought"),
    Category("Uncategorised", "Needs explaining", 0.30,
             note="Categorise these before trusting the totals"),
]

CATEGORIES: dict[str, Category] = {c.name: c for c in _C}
UNCATEGORISED = "Uncategorised"
CASH = "Cash Withdrawals"


def get(name: str | None) -> Category:
    """Look up a category, falling back to a safe default for unknown names."""
    if name and name in CATEGORIES:
        return CATEGORIES[name]
    if name:
        # A user-invented category: assume mid-discretion consumption.
        return Category(name, "Other", 0.30)
    return CATEGORIES[UNCATEGORISED]


def apply_overrides(raw: dict) -> None:
    """Merge user-supplied category tweaks from rules.json into the taxonomy."""
    for name, spec in (raw or {}).items():
        base = CATEGORIES.get(name)
        CATEGORIES[name] = Category(
            name=name,
            group=spec.get("group", base.group if base else "Other"),
            discretion=float(spec.get("discretion",
                                      base.discretion if base else 0.30)),
            kind=spec.get("kind", base.kind if base else "spend"),
            note=spec.get("note", base.note if base else ""),
        )


def spend_categories() -> list[str]:
    return [c.name for c in CATEGORIES.values() if c.kind in ("spend", "unknown")]
