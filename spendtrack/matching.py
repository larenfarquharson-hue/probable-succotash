"""Matching slips to statement lines, and allocating cash withdrawals.

This module is where double counting is prevented, so the rules it follows are
worth stating plainly:

1. A card slip and its statement line are *the same money*. Matching links them
   and copies the slip's better detail onto the transaction. Not one cent is
   added to any total.

2. A cash slip is money already counted when it left the account as an ATM
   withdrawal. It cannot be an extra outflow — counting both would invent
   spending. Instead the slip is *allocated against* the withdrawal, moving that
   portion out of "Cash Withdrawals" and into a real category. The withdrawal's
   total never changes, and whatever is left unexplained is reported as such.

3. A slip that matches nothing is reported, never counted. Either the statement
   covering it has not been imported, or it was paid from an account SpendTrack
   does not have. Both are the user's call, not something to silently absorb.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config, normalise, taxonomy

STATUS_UNMATCHED = "unmatched"
STATUS_MATCHED = "matched"
STATUS_CASH = "cash_allocation"
STATUS_OVER_CASH = "cash_over_allocated"
STATUS_MANUAL = "manual_match"

# Minimum confidence to accept an automatic link.
MATCH_THRESHOLD = 0.68
# Minimum merchant-name similarity, unless amount and date already agree exactly.
# Genuine matches score 0.85+; unrelated names sit below 0.30, so this sits in
# the empty space between them.
MERCHANT_FLOOR = 0.35
# How far back a cash slip may look for the withdrawal that funded it.
CASH_LOOKBACK_DAYS = 31
CASH_LOOKAHEAD_DAYS = 2


@dataclass
class MatchOutcome:
    slip_id: int
    merchant: str | None
    total: float | None
    slip_date: str | None
    status: str
    txn_id: int | None = None
    score: float | None = None
    reason: str = ""


@dataclass
class MatchReport:
    matched: list[MatchOutcome] = field(default_factory=list)
    cash_allocated: list[MatchOutcome] = field(default_factory=list)
    unmatched: list[MatchOutcome] = field(default_factory=list)
    over_cash: list[MatchOutcome] = field(default_factory=list)
    skipped: list[MatchOutcome] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return (len(self.matched) + len(self.cash_allocated) + len(self.unmatched)
                + len(self.over_cash) + len(self.skipped))

    def summary_line(self) -> str:
        return (f"{len(self.matched)} matched to statement lines, "
                f"{len(self.cash_allocated)} allocated against cash withdrawals, "
                f"{len(self.unmatched)} unmatched, {len(self.over_cash)} exceeding "
                f"available cash, {len(self.skipped)} incomplete")


def match_slips(conn: sqlite3.Connection, settings: config.Settings | None = None,
                rematch: bool = False, dry_run: bool = False) -> MatchReport:
    """Link every pending slip to the money it describes."""
    cfg = settings or config.Settings.load()
    report = MatchReport()

    if rematch:
        conn.execute(
            "UPDATE slips SET status = ?, matched_txn_id = NULL, match_score = NULL,"
            " match_reason = NULL WHERE status != ?", (STATUS_UNMATCHED, STATUS_MANUAL))
        conn.execute(
            "UPDATE transactions SET category_source = 'rule' WHERE category_source = 'slip'")
        conn.commit()

    slips = conn.execute(
        "SELECT * FROM slips WHERE status = ? ORDER BY slip_date, id",
        (STATUS_UNMATCHED,)).fetchall()

    for slip in slips:
        if slip["total"] is None or not slip["slip_date"]:
            report.skipped.append(MatchOutcome(
                slip_id=int(slip["id"]), merchant=slip["merchant"], total=slip["total"],
                slip_date=slip["slip_date"], status=STATUS_UNMATCHED,
                reason="needs a date and a total before it can be matched"))
            continue

        best = _best_transaction(conn, slip, cfg)
        if best is not None:
            txn_id, score, reason = best
            outcome = MatchOutcome(
                slip_id=int(slip["id"]), merchant=slip["merchant"],
                total=float(slip["total"]), slip_date=slip["slip_date"],
                status=STATUS_MATCHED, txn_id=txn_id, score=score, reason=reason)
            report.matched.append(outcome)
            if not dry_run:
                _link(conn, slip, txn_id, score, reason, STATUS_MATCHED)
                _enrich_transaction(conn, txn_id, slip)
            continue

        # No statement line of its own. If it could have been paid in cash, the
        # money is already on the statement as a withdrawal.
        if slip["payment_method"] in ("cash", "unknown"):
            allocation = _allocate_to_cash(conn, slip, dry_run)
            if allocation is not None:
                txn_id, remaining = allocation
                reason = (f"paid from cash; allocated against the withdrawal on "
                          f"{_txn_date(conn, txn_id)} "
                          f"({config.money(remaining, cfg.currency)} of it still unexplained)")
                outcome = MatchOutcome(
                    slip_id=int(slip["id"]), merchant=slip["merchant"],
                    total=float(slip["total"]), slip_date=slip["slip_date"],
                    status=STATUS_CASH, txn_id=txn_id, score=None, reason=reason)
                report.cash_allocated.append(outcome)
                continue
            if slip["payment_method"] == "cash":
                outcome = MatchOutcome(
                    slip_id=int(slip["id"]), merchant=slip["merchant"],
                    total=float(slip["total"]), slip_date=slip["slip_date"],
                    status=STATUS_OVER_CASH,
                    reason="cash purchase with no withdrawal left to explain it — "
                           "either a statement is missing or the cash came from "
                           "elsewhere. Not counted, to avoid inventing spending.")
                report.over_cash.append(outcome)
                if not dry_run:
                    conn.execute(
                        "UPDATE slips SET status = ?, match_reason = ? WHERE id = ?",
                        (STATUS_OVER_CASH, outcome.reason, slip["id"]))
                    conn.commit()
                continue

        report.unmatched.append(MatchOutcome(
            slip_id=int(slip["id"]), merchant=slip["merchant"],
            total=float(slip["total"]), slip_date=slip["slip_date"],
            status=STATUS_UNMATCHED,
            reason="no statement line within the matching window — import the "
                   "statement covering this date, or it was paid from another account"))

    if not dry_run:
        conn.commit()
    return report


# --------------------------------------------------------------------------
# Card matching
# --------------------------------------------------------------------------

def _best_transaction(conn: sqlite3.Connection, slip: sqlite3.Row,
                      cfg: config.Settings) -> tuple[int, float, str] | None:
    total = abs(float(slip["total"]))
    slip_day = date.fromisoformat(slip["slip_date"])
    window = timedelta(days=cfg.slip_match_window_days)
    # A slip total should equal the charge. Allow a small margin for tips and
    # rounding, but never enough for a different purchase to sneak in.
    margin = max(cfg.slip_match_amount_tolerance, min(0.02 * total, 20.0))

    # Cash withdrawals are excluded as candidates. A slip is never *for* a
    # withdrawal — the withdrawal is how the cash was obtained — and without this
    # a R2,000 card slip could latch onto a same-day R2,000 ATM draw purely on
    # the strength of the amount and date agreeing.
    rows = conn.execute(
        "SELECT t.id, t.txn_date, t.description, t.description_key, t.amount,"
        "       t.merchant, t.category"
        "  FROM transactions t"
        " WHERE t.amount < 0 AND t.excluded = 0 AND t.is_internal = 0"
        "   AND COALESCE(t.category, '') != ?"
        "   AND t.txn_date BETWEEN ? AND ?"
        "   AND ABS(ABS(t.amount) - ?) <= ?"
        "   AND t.id NOT IN (SELECT matched_txn_id FROM slips"
        "                     WHERE matched_txn_id IS NOT NULL AND status IN (?, ?))",
        (taxonomy.CASH,
         (slip_day - window).isoformat(), (slip_day + window).isoformat(),
         total, margin, STATUS_MATCHED, STATUS_MANUAL),
    ).fetchall()

    slip_key = slip["merchant_key"] or ""
    last4 = slip["card_last4"]
    best: tuple[int, float, str] | None = None

    for row in rows:
        txn_day = date.fromisoformat(row["txn_date"])
        day_gap = abs((txn_day - slip_day).days)
        amount_gap = abs(abs(float(row["amount"])) - total)

        date_score = max(0.0, 1.0 - day_gap / (cfg.slip_match_window_days + 1))
        amount_score = 1.0 if amount_gap <= cfg.slip_match_amount_tolerance else \
            max(0.0, 1.0 - amount_gap / margin) if margin else 0.0
        merchant_score = max(
            normalise.similarity(slip_key, row["description_key"] or ""),
            normalise.similarity(slip_key, normalise.merchant_key(row["merchant"] or "")),
        )
        card_bonus = 0.0
        if last4 and last4 in (row["description"] or ""):
            card_bonus = 0.12

        score = min(1.0, 0.34 * amount_score + 0.26 * date_score
                    + 0.40 * merchant_score + card_bonus)

        # An exact amount on the same day is convincing even when the merchant
        # name on the slip bears little resemblance to the bank's description.
        if amount_gap <= cfg.slip_match_amount_tolerance and day_gap == 0:
            score = max(score, 0.70)

        if merchant_score < MERCHANT_FLOOR and not (
                amount_gap <= cfg.slip_match_amount_tolerance and day_gap <= 1):
            continue
        if score < MATCH_THRESHOLD:
            continue

        reason = (f"amount {'exact' if amount_gap <= 0.05 else f'off by {amount_gap:.2f}'}, "
                  f"{'same day' if day_gap == 0 else f'{day_gap} day(s) apart'}, "
                  f"merchant similarity {merchant_score:.2f}"
                  + (", card number matches" if card_bonus else ""))
        if best is None or score > best[1]:
            best = (int(row["id"]), round(score, 3), reason)
    return best


def _link(conn: sqlite3.Connection, slip: sqlite3.Row, txn_id: int, score: float | None,
          reason: str, status: str) -> None:
    conn.execute(
        "UPDATE slips SET status = ?, matched_txn_id = ?, match_score = ?,"
        " match_reason = ? WHERE id = ?",
        (status, txn_id, score, reason, slip["id"]))


def _enrich_transaction(conn: sqlite3.Connection, txn_id: int, slip: sqlite3.Row) -> None:
    """Let a matched slip improve the transaction — detail only, never money."""
    txn = conn.execute(
        "SELECT category, merchant, category_source FROM transactions WHERE id = ?",
        (txn_id,)).fetchone()
    if txn is None:
        return
    slip_category = slip["category"]
    updates: dict[str, object] = {}

    # A slip names the shop directly, so it beats a guess made from a bank
    # description — but never a decision the user made by hand.
    if txn["category_source"] != "manual" and slip_category:
        current = taxonomy.get(txn["category"])
        if txn["category"] == taxonomy.UNCATEGORISED or current.kind == "unknown":
            updates["category"] = slip_category
    if slip["merchant"] and txn["category_source"] != "manual":
        updates["merchant"] = slip["merchant"]

    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE transactions SET {sets}, category_source = 'slip' WHERE id = ?",
            (*updates.values(), txn_id))


# --------------------------------------------------------------------------
# Cash allocation
# --------------------------------------------------------------------------

def _allocate_to_cash(conn: sqlite3.Connection, slip: sqlite3.Row,
                      dry_run: bool) -> tuple[int, float] | None:
    """Attach a cash slip to a withdrawal that still has unexplained value."""
    total = abs(float(slip["total"]))
    slip_day = date.fromisoformat(slip["slip_date"])

    withdrawals = conn.execute(
        "SELECT id, txn_date, amount FROM transactions"
        " WHERE amount < 0 AND excluded = 0 AND category = ?"
        "   AND txn_date BETWEEN ? AND ?"
        " ORDER BY txn_date DESC",
        (taxonomy.CASH,
         (slip_day - timedelta(days=CASH_LOOKBACK_DAYS)).isoformat(),
         (slip_day + timedelta(days=CASH_LOOKAHEAD_DAYS)).isoformat()),
    ).fetchall()

    for row in withdrawals:
        drawn = abs(float(row["amount"]))
        allocated = conn.execute(
            "SELECT COALESCE(SUM(total), 0) s FROM slips"
            " WHERE matched_txn_id = ? AND status = ?", (row["id"], STATUS_CASH)
        ).fetchone()["s"]
        remaining = drawn - float(allocated)
        if total <= remaining + 0.01:
            if not dry_run:
                _link(conn, slip, int(row["id"]), None,
                      "allocated against a cash withdrawal", STATUS_CASH)
                conn.commit()
            return int(row["id"]), round(remaining - total, 2)
    return None


def _txn_date(conn: sqlite3.Connection, txn_id: int) -> str:
    row = conn.execute("SELECT txn_date FROM transactions WHERE id = ?",
                       (txn_id,)).fetchone()
    return row["txn_date"] if row else "?"


# --------------------------------------------------------------------------
# Manual intervention
# --------------------------------------------------------------------------

def link_manually(conn: sqlite3.Connection, slip_id: int, txn_id: int) -> None:
    """Force a link the scorer would not make. Marked so re-matching leaves it."""
    slip = conn.execute("SELECT * FROM slips WHERE id = ?", (slip_id,)).fetchone()
    if slip is None:
        raise ValueError(f"No slip with id {slip_id}")
    txn = conn.execute("SELECT id, amount FROM transactions WHERE id = ?",
                       (txn_id,)).fetchone()
    if txn is None:
        raise ValueError(f"No transaction with id {txn_id}")
    _link(conn, slip, txn_id, 1.0, "linked by hand", STATUS_MANUAL)
    _enrich_transaction(conn, txn_id, slip)
    conn.commit()


def unlink(conn: sqlite3.Connection, slip_id: int) -> None:
    conn.execute(
        "UPDATE slips SET status = ?, matched_txn_id = NULL, match_score = NULL,"
        " match_reason = NULL WHERE id = ?", (STATUS_UNMATCHED, slip_id))
    conn.commit()


def cash_position(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """How much cash was drawn in a period, and how much of it slips explain."""
    drawn = conn.execute(
        "SELECT COALESCE(SUM(ABS(amount)), 0) s FROM transactions"
        " WHERE category = ? AND excluded = 0 AND txn_date BETWEEN ? AND ?",
        (taxonomy.CASH, start, end)).fetchone()["s"]
    explained = conn.execute(
        "SELECT COALESCE(SUM(s.total), 0) s FROM slips s"
        " JOIN transactions t ON t.id = s.matched_txn_id"
        " WHERE s.status = ? AND t.txn_date BETWEEN ? AND ?",
        (STATUS_CASH, start, end)).fetchone()["s"]
    return {
        "withdrawn": round(float(drawn), 2),
        "explained": round(float(explained), 2),
        "unexplained": round(float(drawn) - float(explained), 2),
    }
